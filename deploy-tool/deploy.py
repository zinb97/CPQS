#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CPQS 代码部署工具。

通过系统自带的 OpenSSH 客户端，把本地项目打包成 tar.gz 后以标准输入
流式传输到远端，再由远端 ``tar -xzf -`` 解包到 /opt/CPQS。

设计目标：
  * 不依赖 rsync、scp 或任何第三方 Python 包；
  * 支持多台服务器并发部署；
  * 默认使用 ~/.ssh/id_rsa 密钥登录；
  * 默认只覆盖同名文件，不删除远端已有文件（避免误删运行数据）。

常用命令：
    py deploy.py list
    py deploy.py check --all
    py deploy.py deploy --target X1 --dry-run
    py deploy.py deploy --all
    py deploy.py start --all            # 远端后台启动 metrics_exporter.py
    py deploy.py stop --all             # 结束远端后台任务
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import gzip
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

VERSION = "1.0.0"

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = TOOL_DIR / "servers.json"
TOOL_IGNORE_PATH = TOOL_DIR / ".deployignore"
DEFAULT_SOURCE = TOOL_DIR.parent

# 这些目录/文件永远不进入归档，避免把版本库和工具自身部署出去。
BUILTIN_IGNORES: Tuple[str, ...] = (
    ".git",
    ".svn",
    ".hg",
)

ERROR_HINTS = {
    "parent_not_writable": "目标目录不存在，且父目录不可写（通常需要管理员先创建 /opt/CPQS 并授权）",
    "mkdir_failed": "无法创建目标目录",
    "target_not_writable": "目标目录不可写",
    "extract_failed": "远端解包失败（可能是磁盘空间不足、权限不足或 tar 不支持该归档）",
    "workdir_missing": "远端工作目录不存在（该服务器可能还没部署过代码）",
    "setup_failed": "远端准备命令执行失败",
    "start_failed": "进程启动后立即退出，请查看日志",
    "pid_mismatch": "PID 文件与实际进程不匹配（可能进程已重启导致 PID 复用），已清理 PID 文件",
}


class DeployError(Exception):
    """可预期错误，CLI 只打印消息，不打印 traceback。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _setup_console() -> None:
    """尽量让 Windows 终端/重定向文件都能正常输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


def one_line(text: object, limit: int = 800) -> str:
    """把多行错误压缩成一行，便于并发输出。"""
    value = " ".join(str(text).split())
    if len(value) > limit:
        value = value[: limit - 3] + "..."
    return value


def _resolve_path(value: object, base: Path, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DeployError(f"{field_name} 必须是非空字符串")
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    path = Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _validate_remote_path(value: object, field_name: str = "deploy_path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeployError(f"{field_name} 必须是非空字符串")
    raw = value.strip()
    if "\\" in raw:
        raise DeployError(f"{field_name} 请使用 Linux 的 / 分隔: {raw!r}")
    if "\n" in raw or "\r" in raw or "\0" in raw:
        raise DeployError(f"{field_name} 包含非法字符")
    if not raw.startswith("/"):
        raise DeployError(f"{field_name} 必须是绝对路径: {raw!r}")
    normalized = posixpath.normpath(raw)
    if normalized == "/":
        raise DeployError(f"{field_name} 不能是根目录 /")
    return normalized


def _require_string(mapping: Dict[str, object], key: str, index: int) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeployError(f"servers[{index}].{key} 必须是非空字符串")
    return value.strip()


def _port(value: object, index: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeployError(f"servers[{index}].port 必须是整数")
    if not (1 <= value <= 65535):
        raise DeployError(f"servers[{index}].port 超出范围: {value}")
    return value


def parse_kv_output(text: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key:
            data[key.strip()] = value.strip()
    return data


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Server:
    name: str
    host: str
    port: int
    user: str
    deploy_path: str
    key_path: Path
    group: str = ""
    task_layers: Tuple[Tuple[str, Dict[str, object]], ...] = ()

    @property
    def target(self) -> str:
        host = self.host
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            host = f"[{host}]"
        return f"{self.user}@{host}"

    @property
    def endpoint(self) -> str:
        return f"{self.target}:{self.port}"


@dataclass(frozen=True)
class Config:
    path: Path
    source: Path
    deploy_path: str
    key_path: Path
    connect_timeout: int
    jobs: int
    deploy_timeout: int
    strict_host_key_checking: str
    exclude: Tuple[str, ...]
    servers: Tuple[Server, ...]
    task_layers: Tuple[Tuple[str, Dict[str, object]], ...] = ()


# ---------------------------------------------------------------------------
# 后台任务定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """需要在远端后台常驻的一个进程（例如 collector 的 metrics_exporter.py）。"""

    name: str
    command: str
    cwd: str = ""
    env: Tuple[Tuple[str, str], ...] = ()
    setup: str = ""
    stop_timeout: int = 10
    enabled: bool = True


# 内置默认任务。servers.json 中定义了同名任务时，以配置为准（字段级覆盖）。
DEFAULT_TASKS: Dict[str, Dict[str, object]] = {
    "collector": {
        # 所有服务器统一使用系统解释器 /usr/bin/python3，避免 PATH 上的 python3
        # 指向 conda / pyenv 等非系统环境（远端非交互 shell 的 PATH 往往与登录 shell 不同）。
        "command": "/usr/bin/python3 metrics_exporter.py --port 9100",
        "cwd": "collector",
        "env": {"PYTHONUNBUFFERED": "1"},
        "stop_timeout": 10,
    },
}

TASK_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")



def _parse_task_layers(
    raw: object, where: str
) -> Tuple[Tuple[str, Dict[str, object]], ...]:
    """解析 servers.json 里的 tasks 段落，返回 (任务名, 原始字段) 列表。"""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise DeployError(f"{where} 必须是 JSON 对象")

    layers: List[Tuple[str, Dict[str, object]]] = []
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise DeployError(f"{where} 的任务名必须是非空字符串")
        task_name = name.strip()
        if not TASK_NAME_PATTERN.fullmatch(task_name):
            raise DeployError(
                f"{where} 任务名非法: {task_name!r}（只允许字母、数字、下划线、点和短横线）"
            )
        if isinstance(value, str):
            entry: Dict[str, object] = {"command": value}
        elif isinstance(value, dict):
            entry = dict(value)
        else:
            raise DeployError(f"{where}.{task_name} 必须是字符串或 JSON 对象")
        layers.append((task_name, entry))
    return tuple(layers)


def _build_task(
    name: str, layers: Sequence[Dict[str, object]], where: str
) -> Task:
    """把默认任务、全局配置、服务器配置三层字段合并成一个任务定义。"""
    merged: Dict[str, object] = dict(DEFAULT_TASKS.get(name, {}))
    for layer in layers:
        merged.update(layer)

    command = merged.get("command", "")
    if not isinstance(command, str) or not command.strip():
        raise DeployError(f"{where}.{name} 缺少 command（或 command 为空）")

    cwd = merged.get("cwd", "")
    if cwd is None:
        cwd = ""
    if not isinstance(cwd, str):
        raise DeployError(f"{where}.{name}.cwd 必须是字符串")

    env_raw = merged.get("env", {})
    if env_raw is None:
        env_raw = {}
    if not isinstance(env_raw, dict):
        raise DeployError(f"{where}.{name}.env 必须是 JSON 对象")
    env: List[Tuple[str, str]] = []
    for key, value in env_raw.items():
        if not isinstance(key, str) or not key.strip():
            raise DeployError(f"{where}.{name}.env 的变量名必须是非空字符串")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise DeployError(f"{where}.{name}.env 变量名非法: {key!r}")
        env.append((key, "" if value is None else str(value)))

    setup = merged.get("setup", "")
    if setup is None:
        setup = ""
    if not isinstance(setup, str):
        raise DeployError(f"{where}.{name}.setup 必须是字符串")

    stop_timeout = merged.get("stop_timeout", 10)
    if isinstance(stop_timeout, bool) or not isinstance(stop_timeout, int):
        raise DeployError(f"{where}.{name}.stop_timeout 必须是整数")
    if not (1 <= stop_timeout <= 600):
        raise DeployError(f"{where}.{name}.stop_timeout 必须在 1..600 之间")

    enabled = merged.get("enabled", True)
    if not isinstance(enabled, bool):
        raise DeployError(f"{where}.{name}.enabled 必须是 true/false")

    return Task(
        name=name,
        command=command.strip(),
        cwd=cwd.strip(),
        env=tuple(env),
        setup=setup.strip(),
        stop_timeout=stop_timeout,
        enabled=enabled,
    )


def resolve_tasks(config: Config, server: Server) -> List[Task]:
    """合并全局与该服务器私有的任务定义，返回该服务器要运行的任务。"""
    names: List[str] = []
    seen: set[str] = set()
    for layer in (config.task_layers, server.task_layers):
        for name, _ in layer:
            if name not in seen:
                seen.add(name)
                names.append(name)
    for name in DEFAULT_TASKS:
        if name not in seen:
            seen.add(name)
            names.append(name)

    tasks: List[Task] = []
    for name in names:
        layers = [entry for layer_name, entry in config.task_layers if layer_name == name]
        layers += [
            entry for layer_name, entry in server.task_layers if layer_name == name
        ]
        task = _build_task(name, layers, "tasks")
        if task.enabled:
            tasks.append(task)
    return tasks


def select_tasks(
    config: Config, server: Server, names: Sequence[str]
) -> List[Task]:
    """挑出用户点名的任务；不点名时返回该服务器的全部启用任务。"""
    available = resolve_tasks(config, server)
    wanted = [item.strip() for item in names if item.strip()]
    if not wanted:
        return available

    by_name = {task.name: task for task in available}
    unknown = [item for item in wanted if item not in by_name]
    if unknown:
        known = ", ".join(sorted(by_name)) or "（无）"
        raise DeployError(
            f"服务器 {server.name} 没有任务 {', '.join(unknown)}；可用任务: {known}"
        )
    return [by_name[item] for item in wanted]


def task_workdir(server: Server, task: Task) -> str:
    """任务的远端工作目录（相对路径按该服务器的部署目录展开）。"""
    if not task.cwd:
        return server.deploy_path
    if task.cwd.startswith("/"):
        return posixpath.normpath(task.cwd)
    return posixpath.normpath(posixpath.join(server.deploy_path, task.cwd))


def task_pidfile(server: Server, task: Task) -> str:
    return posixpath.join(server.deploy_path, ".run", f"{task.name}.pid")


def task_logfile(server: Server, task: Task) -> str:
    return posixpath.join(server.deploy_path, "logs", f"{task.name}.log")


def task_marker(command: str) -> str:
    """从命令里挑一个特征串，用于停止前确认 PID 没有被系统回收后复用。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    for token in tokens:
        if token.startswith("-"):
            continue
        if "." in token and not token.startswith("/"):
            return token
    for token in tokens:
        if not token.startswith("-"):
            return token
    return tokens[0]


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve(strict=False)
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise DeployError(f"配置文件不存在: {path}") from exc
    except OSError as exc:
        raise DeployError(f"无法读取配置文件 {path}: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DeployError(
            f"配置文件 JSON 解析失败: {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(raw, dict):
        raise DeployError("配置文件根节点必须是 JSON 对象")

    base = path.parent
    source = _resolve_path(raw.get("source", ".."), base, "source")
    deploy_path = _validate_remote_path(raw.get("deploy_path", "/opt/CPQS"))
    key_path = _resolve_path(raw.get("key_path", "~/.ssh/id_rsa"), base, "key_path")

    connect_timeout = raw.get("connect_timeout", 10)
    if isinstance(connect_timeout, bool) or not isinstance(connect_timeout, int):
        raise DeployError("connect_timeout 必须是整数")
    if not (1 <= connect_timeout <= 120):
        raise DeployError("connect_timeout 必须在 1..120 之间")

    jobs = raw.get("jobs", 4)
    if isinstance(jobs, bool) or not isinstance(jobs, int):
        raise DeployError("jobs 必须是整数")
    if not (1 <= jobs <= 32):
        raise DeployError("jobs 必须在 1..32 之间")

    deploy_timeout = raw.get("timeout", 900)
    if isinstance(deploy_timeout, bool) or not isinstance(deploy_timeout, int):
        raise DeployError("timeout 必须是整数")
    if not (10 <= deploy_timeout <= 86400):
        raise DeployError("timeout 必须在 10..86400 之间")

    strict = str(raw.get("strict_host_key_checking", "accept-new")).strip().lower()
    if strict not in ("yes", "no", "ask", "accept-new"):
        raise DeployError(
            "strict_host_key_checking 只能是 yes / no / ask / accept-new"
        )

    raw_exclude = raw.get("exclude", [])
    if not isinstance(raw_exclude, list) or not all(
        isinstance(item, str) for item in raw_exclude
    ):
        raise DeployError("exclude 必须是字符串数组")

    task_layers = _parse_task_layers(raw.get("tasks"), "tasks")

    raw_servers = raw.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        raise DeployError("servers 必须是非空数组")

    servers: List[Server] = []
    names: set[str] = set()
    for index, item in enumerate(raw_servers, start=1):
        if not isinstance(item, dict):
            raise DeployError(f"servers[{index}] 必须是 JSON 对象")

        host = _require_string(item, "host", index)
        user = _require_string(item, "user", index)
        if any(ch.isspace() for ch in host) or host.startswith("-"):
            raise DeployError(f"servers[{index}].host 非法: {host!r}")
        if not re.fullmatch(r"[A-Za-z0-9._:\-\[\]]+", host):
            raise DeployError(f"servers[{index}].host 含非法字符: {host!r}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", user) or user.startswith("-"):
            raise DeployError(f"servers[{index}].user 非法: {user!r}")

        port = _port(item.get("port", 22), index)
        name = str(item.get("name") or f"{user}-{port}").strip()
        if not name:
            raise DeployError(f"servers[{index}].name 不能为空")
        if name in names:
            raise DeployError(f"服务器名称重复: {name}")
        names.add(name)

        server_deploy_path = _validate_remote_path(
            item.get("deploy_path", deploy_path),
            f"servers[{index}].deploy_path",
        )
        server_key = (
            _resolve_path(item["key_path"], base, f"servers[{index}].key_path")
            if "key_path" in item
            else key_path
        )
        group = str(item.get("group", "")).strip()
        server_task_layers = _parse_task_layers(
            item.get("tasks"), f"servers[{index}].tasks"
        )

        servers.append(
            Server(
                name=name,
                host=host,
                port=port,
                user=user,
                deploy_path=server_deploy_path,
                key_path=server_key,
                group=group,
                task_layers=server_task_layers,
            )
        )

    return Config(
        path=path,
        source=source,
        deploy_path=deploy_path,
        key_path=key_path,
        connect_timeout=connect_timeout,
        jobs=jobs,
        deploy_timeout=deploy_timeout,
        strict_host_key_checking=strict,
        exclude=tuple(item.strip() for item in raw_exclude if item.strip()),
        servers=tuple(servers),
        task_layers=task_layers,
    )


def select_servers(
    config: Config,
    names: Sequence[str],
    groups: Sequence[str],
    all_servers: bool,
) -> List[Server]:
    if all_servers and (names or groups):
        raise DeployError("--all 不能与 --target / --group 同时使用")

    if all_servers:
        return list(config.servers)

    wanted_names = {item.strip() for item in names if item.strip()}
    wanted_groups = {item.strip() for item in groups if item.strip()}
    if not wanted_names and not wanted_groups:
        raise DeployError("请用 --all、--target NAME 或 --group GROUP 选择服务器")

    known_names = {server.name for server in config.servers}
    unknown_names = wanted_names - known_names
    if unknown_names:
        raise DeployError(f"未知服务器名称: {', '.join(sorted(unknown_names))}")

    known_groups = {server.group for server in config.servers if server.group}
    unknown_groups = wanted_groups - known_groups
    if unknown_groups:
        raise DeployError(f"未知分组: {', '.join(sorted(unknown_groups))}")

    selected = [
        server
        for server in config.servers
        if server.name in wanted_names or (server.group and server.group in wanted_groups)
    ]
    if not selected:
        raise DeployError("没有匹配到任何服务器")
    return selected


# ---------------------------------------------------------------------------
# 排除规则与打包
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IgnoreRule:
    pattern: str
    dir_only: bool
    anchored: bool
    has_slash: bool


def _compile_ignore(lines: Sequence[str]) -> List[IgnoreRule]:
    rules: List[IgnoreRule] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("!"):
            raise DeployError(
                f"暂不支持 ! 取反排除规则: {text!r}；请改用更精确的普通规则"
            )
        dir_only = text.endswith("/")
        if dir_only:
            text = text.rstrip("/")
        if not text:
            continue
        anchored = text.startswith("/")
        if anchored:
            text = text[1:]
        if not text:
            continue
        rules.append(
            IgnoreRule(
                pattern=text,
                dir_only=dir_only,
                anchored=anchored,
                has_slash="/" in text,
            )
        )
    return rules


def _is_ignored(rel: str, is_dir: bool, rules: Sequence[IgnoreRule]) -> bool:
    for rule in rules:
        if rule.dir_only and not is_dir:
            continue
        if rule.anchored or rule.has_slash:
            matched = fnmatch.fnmatchcase(rel, rule.pattern)
        else:
            matched = any(
                fnmatch.fnmatchcase(part, rule.pattern) for part in rel.split("/")
            )
        if matched:
            return True
    return False


def _read_ignore_file(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise DeployError(f"无法读取排除规则文件 {path}: {exc}") from exc


def build_ignore_rules(source: Path, cli_patterns: Sequence[str]) -> List[IgnoreRule]:
    lines: List[str] = list(BUILTIN_IGNORES)

    # 如果工具目录位于源目录内，自动排除，避免把部署工具自己传到远端。
    try:
        tool_rel = TOOL_DIR.relative_to(source.resolve())
    except ValueError:
        tool_rel = None
    if tool_rel is not None and tool_rel.parts:
        lines.append("/" + tool_rel.as_posix())

    seen_files: set[Path] = set()
    for candidate in (TOOL_IGNORE_PATH, source / ".deployignore"):
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            resolved = candidate
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        lines.extend(_read_ignore_file(candidate))

    lines.extend(cli_patterns)
    return _compile_ignore(lines)


def _is_windows_reparse_dir(entry: os.DirEntry[str]) -> bool:
    if os.name != "nt":
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def iter_source(
    source: Path, rules: Sequence[IgnoreRule]
) -> Tuple[List[Tuple[str, Path, str]], int]:
    """返回 (条目列表, 跳过数量)。条目类型为 dir/file/symlink。"""
    entries: List[Tuple[str, Path, str]] = []
    skipped = 0

    def walk(rel_dir: str) -> None:
        nonlocal skipped
        abs_dir = source / rel_dir if rel_dir else source
        try:
            with os.scandir(abs_dir) as iterator:
                items = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise DeployError(f"读取目录失败: {abs_dir}: {exc}") from exc

        for entry in items:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise DeployError(f"读取文件属性失败: {entry.path}: {exc}") from exc

            # Windows junction 可能被 is_dir(follow_symlinks=False) 识别为目录，
            # 不再递归，避免越界或循环。
            if is_dir and _is_windows_reparse_dir(entry):
                is_dir = False

            if _is_ignored(rel, is_dir, rules):
                skipped += 1
                continue

            if is_dir:
                entries.append((rel, Path(entry.path), "dir"))
                walk(rel)
            elif is_link:
                entries.append((rel, Path(entry.path), "symlink"))
            elif is_file:
                entries.append((rel, Path(entry.path), "file"))
            else:
                skipped += 1

    walk("")
    return entries, skipped


def _git_modes(source: Path) -> Dict[str, int]:
    """从 git 索引读取可执行位，解决 Windows 上无法保留 +x 的问题。"""
    git = shutil.which("git")
    if not git:
        return {}

    try:
        proc = subprocess.run(
            [git, "ls-files", "--stage", "-z"],
            cwd=str(source),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    if proc.returncode != 0:
        return {}

    modes: Dict[str, int] = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        try:
            meta, raw_path = record.split(b"\t", 1)
        except ValueError:
            continue
        parts = meta.split()
        if len(parts) < 3:
            continue
        mode = parts[0]
        rel = raw_path.decode("utf-8", "surrogateescape").replace("\\", "/")
        if mode == b"100755":
            modes[rel] = 0o755
        elif mode == b"100644":
            modes[rel] = 0o644
    return modes


def _file_mode(path: Path, rel: str, git_modes: Dict[str, int]) -> int:
    if rel in git_modes:
        return git_modes[rel]
    if os.name == "nt":
        return 0o644
    try:
        mode = stat.S_IMODE(path.stat().st_mode) & 0o777
    except OSError:
        return 0o644
    # 确保部署用户至少能读；去掉 setuid/setgid/sticky。
    mode = (mode | 0o400) & ~0o7000
    return mode or 0o644


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    source: Path
    size: int
    sha256: str
    files: int
    dirs: int
    skipped: int


def build_archive(source: Path, rules: Sequence[IgnoreRule]) -> ArchiveInfo:
    entries, skipped = iter_source(source, rules)
    files = sum(1 for _, _, kind in entries if kind in ("file", "symlink"))
    dirs = sum(1 for _, _, kind in entries if kind == "dir")
    if files == 0:
        raise DeployError(f"源目录中没有可部署文件: {source}")

    modes = _git_modes(source)
    fd, temp_name = tempfile.mkstemp(prefix="cpqs-deploy-", suffix=".tar.gz")
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        with temp_path.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=6,
                mtime=0,
            ) as gz:
                with tarfile.open(
                    fileobj=gz,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                    dereference=False,
                ) as archive:
                    for rel, abs_path, kind in entries:
                        try:
                            info = archive.gettarinfo(str(abs_path), arcname=rel)
                        except OSError as exc:
                            raise DeployError(
                                f"读取待打包文件失败: {abs_path}: {exc}"
                            ) from exc
                        if info is None:
                            continue

                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""

                        if kind == "dir":
                            info.mode = 0o755
                            archive.addfile(info)
                        elif kind == "symlink":
                            info.mode = 0o777
                            if info.linkname and os.name == "nt":
                                info.linkname = info.linkname.replace("\\", "/")
                            archive.addfile(info)
                        else:
                            info.mode = _file_mode(abs_path, rel, modes)
                            try:
                                with abs_path.open("rb") as handle:
                                    archive.addfile(info, handle)
                            except OSError as exc:
                                raise DeployError(
                                    f"读取待打包文件失败: {abs_path}: {exc}"
                                ) from exc

        size = temp_path.stat().st_size
        return ArchiveInfo(
            path=temp_path,
            source=source,
            size=size,
            sha256=_sha256_file(temp_path),
            files=files,
            dirs=dirs,
            skipped=skipped,
        )
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# SSH 与远端脚本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    argv: Tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    server: Server
    ok: bool
    data: Dict[str, str]
    warnings: List[str]
    error: str
    elapsed: float


@dataclass(frozen=True)
class DeployResult:
    server: Server
    ok: bool
    message: str
    elapsed: float
    size: int
    dry_run: bool = False


def build_check_script(target: str) -> str:
    quoted_target = shlex.quote(target)
    return f"""set -u
target={quoted_target}
printf 'CPQS_CHECK_BEGIN\\n'
printf 'USER=%s\\n' "$(id -un 2>/dev/null || printf unknown)"
if [ -e "$target" ]; then printf 'TARGET_EXISTS=1\\n'; else printf 'TARGET_EXISTS=0\\n'; fi
if [ -L "$target" ]; then printf 'TARGET_SYMLINK=1\\n'; else printf 'TARGET_SYMLINK=0\\n'; fi
if [ -d "$target" ]; then printf 'TARGET_IS_DIR=1\\n'; else printf 'TARGET_IS_DIR=0\\n'; fi
if [ -w "$target" ]; then printf 'TARGET_WRITABLE=1\\n'; else printf 'TARGET_WRITABLE=0\\n'; fi
parent=$(dirname "$target")
if [ -w "$parent" ]; then printf 'PARENT_WRITABLE=1\\n'; else printf 'PARENT_WRITABLE=0\\n'; fi
if command -v tar >/dev/null 2>&1; then printf 'TAR=1\\n'; else printf 'TAR=0\\n'; fi
printf 'TAR_VERSION=%s\\n' "$(tar --version 2>/dev/null | head -n 1)"
if tar --no-same-owner -cf /dev/null /dev/null >/dev/null 2>&1; then printf 'TAR_NO_SAME_OWNER=1\\n'; else printf 'TAR_NO_SAME_OWNER=0\\n'; fi
if [ -x /usr/bin/python3 ]; then printf 'PYTHON3=1\\n'; else printf 'PYTHON3=0\\n'; fi
printf 'PYTHON3_PATH=%s\\n' "$(command -v python3 2>/dev/null || printf '')"
printf 'CPQS_CHECK_DONE\\n'
"""


def build_deploy_script(target: str) -> str:
    quoted_target = shlex.quote(target)
    return f"""set -eu
umask 022
target={quoted_target}
parent=$(dirname "$target")
if [ ! -d "$target" ]; then
    if [ ! -w "$parent" ]; then
        printf 'CPQS_ERROR=parent_not_writable\\n' >&2
        exit 4
    fi
fi
mkdir -p "$target" || {{ printf 'CPQS_ERROR=mkdir_failed\\n' >&2; exit 5; }}
if [ ! -w "$target" ]; then
    printf 'CPQS_ERROR=target_not_writable\\n' >&2
    exit 6
fi
extra=''
if tar --no-same-owner -cf /dev/null /dev/null >/dev/null 2>&1; then extra='--no-same-owner'; fi
tar $extra -xzf - -C "$target" || {{ printf 'CPQS_ERROR=extract_failed\\n' >&2; exit 7; }}
printf 'CPQS_DEPLOY_OK\\n'
"""


def build_start_script(server: Server, task: Task) -> str:
    """生成远端启动脚本：脱离 SSH 会话在后台常驻，并把 PID 写入 .run/ 目录。"""
    q = shlex.quote
    workdir = task_workdir(server, task)
    pidfile = task_pidfile(server, task)
    logfile = task_logfile(server, task)
    # 内层脚本由后台 shell 执行：先把自身 PID 写进 pidfile，再 exec 成真正的进程，
    # 这样 PID 文件里记录的就是最终进程（例如 python3）的 PID。
    inner = "set -f; echo $$ > \"$CPQS_PIDFILE\"; exec $CPQS_TASK_CMD"

    lines = [
        "set -u",
        "name=" + q(task.name),
        "command=" + q(task.command),
        "workdir=" + q(workdir),
        "pidfile=" + q(pidfile),
        "logfile=" + q(logfile),
        "setup_cmd=" + q(task.setup),
        "rundir=$(dirname \"$pidfile\")",
        "logdir=$(dirname \"$logfile\")",
        "printf 'CPQS_TASK_BEGIN\\n'",
        "printf 'TASK=%s\\n' \"$name\"",
        "mkdir -p \"$rundir\" \"$logdir\" || { printf 'CPQS_ERROR=mkdir_failed\\n' >&2; exit 5; }",
        "if [ -f \"$pidfile\" ]; then",
        "    old_pid=$(cat \"$pidfile\" 2>/dev/null || printf '')",
        "    if [ -n \"$old_pid\" ] && kill -0 \"$old_pid\" 2>/dev/null; then",
        "        printf 'CPQS_ALREADY_RUNNING=1\\n'",
        "        printf 'CPQS_PID=%s\\n' \"$old_pid\"",
        "        printf 'CPQS_LOG=%s\\n' \"$logfile\"",
        "        printf 'CPQS_TASK_DONE\\n'",
        "        exit 0",
        "    fi",
        "    rm -f \"$pidfile\"",
        "fi",
        "if [ ! -d \"$workdir\" ]; then",
        "    printf 'CPQS_ERROR=workdir_missing\\n' >&2",
        "    exit 8",
        "fi",
        "cd \"$workdir\" || { printf 'CPQS_ERROR=workdir_missing\\n' >&2; exit 8; }",
    ]
    for key, value in task.env:
        lines.append("export " + key + "=" + q(value))
    lines += [
        "if [ -n \"$setup_cmd\" ]; then",
        "    printf 'CPQS_SETUP_BEGIN\\n'",
        "    if ! sh -c \"$setup_cmd\"; then",
        "        printf 'CPQS_ERROR=setup_failed\\n' >&2",
        "        exit 9",
        "    fi",
        "fi",
        "printf 'CPQS_LAUNCH=1\\n'",
        "if command -v setsid >/dev/null 2>&1; then",
        "    CPQS_PIDFILE=\"$pidfile\" CPQS_TASK_CMD=\"$command\" nohup setsid sh -c "
        + q(inner)
        + " >>\"$logfile\" 2>&1 </dev/null &",
        "else",
        "    CPQS_PIDFILE=\"$pidfile\" CPQS_TASK_CMD=\"$command\" nohup sh -c "
        + q(inner)
        + " >>\"$logfile\" 2>&1 </dev/null &",
        "fi",
        "sleep 2",
        "pid=$(cat \"$pidfile\" 2>/dev/null || printf '')",
        "if [ -z \"$pid\" ] || ! kill -0 \"$pid\" 2>/dev/null; then",
        "    rm -f \"$pidfile\"",
        "    printf 'CPQS_ERROR=start_failed\\n' >&2",
        "    printf 'CPQS_LOG=%s\\n' \"$logfile\"",
        "    printf 'CPQS_LOG_TAIL_BEGIN\\n'",
        "    tail -n 20 \"$logfile\" 2>/dev/null || true",
        "    printf 'CPQS_LOG_TAIL_END\\n'",
        "    exit 11",
        "fi",
        "printf 'CPQS_STARTED=1\\n'",
        "printf 'CPQS_PID=%s\\n' \"$pid\"",
        "printf 'CPQS_LOG=%s\\n' \"$logfile\"",
        "printf 'CPQS_TASK_DONE\\n'",
    ]
    return "\n".join(lines) + "\n"


def build_stop_script(server: Server, task: Task) -> str:
    """生成远端停止脚本：先 TERM，必要时按进程组收尾，最后清掉 PID 文件。"""
    q = shlex.quote
    lines = [
        "set -u",
        "name=" + q(task.name),
        "pidfile=" + q(task_pidfile(server, task)),
        "marker=" + q(task_marker(task.command)),
        "stop_timeout=" + str(task.stop_timeout),
        "printf 'CPQS_TASK_BEGIN\\n'",
        "printf 'TASK=%s\\n' \"$name\"",
        "if [ ! -f \"$pidfile\" ]; then",
        "    printf 'CPQS_NOT_RUNNING=1\\n'",
        "    printf 'CPQS_TASK_DONE\\n'",
        "    exit 0",
        "fi",
        "pid=$(cat \"$pidfile\" 2>/dev/null || printf '')",
        "if [ -z \"$pid\" ] || ! kill -0 \"$pid\" 2>/dev/null; then",
        "    rm -f \"$pidfile\"",
        "    printf 'CPQS_NOT_RUNNING=1\\n'",
        "    printf 'CPQS_TASK_DONE\\n'",
        "    exit 0",
        "fi",
        # PID 复用保护：命令行对不上说明这个 PID 已经是别的进程，不能乱杀。
        "if [ -n \"$marker\" ] && [ -r \"/proc/$pid/cmdline\" ]; then",
        "    cmdline=$(tr '\\0' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null || printf '')",
        "    case \"$cmdline\" in",
        "        *\"$marker\"*) ;;",
        "        *) rm -f \"$pidfile\"; printf 'CPQS_ERROR=pid_mismatch\\n' >&2; exit 10 ;;",
        "    esac",
        "fi",
        # setsid 起的进程自成进程组，优先整组终止，避免留下孤儿进程。
        "pgrp=$(sed 's/.*) //' \"/proc/$pid/stat\" 2>/dev/null | awk '{print $3}')",
        "if [ -n \"$pgrp\" ] && [ \"$pgrp\" = \"$pid\" ]; then",
        "    kill -TERM \"-$pgrp\" 2>/dev/null || true",
        "fi",
        "kill -TERM \"$pid\" 2>/dev/null || true",
        "i=0",
        "while [ \"$i\" -lt \"$stop_timeout\" ]; do",
        "    kill -0 \"$pid\" 2>/dev/null || break",
        "    sleep 1",
        "    i=$((i + 1))",
        "done",
        "if kill -0 \"$pid\" 2>/dev/null; then",
        "    kill -KILL \"$pid\" 2>/dev/null || true",
        "    sleep 1",
        "    printf 'CPQS_FORCE_KILLED=1\\n'",
        "fi",
        "rm -f \"$pidfile\"",
        "printf 'CPQS_STOPPED=1\\n'",
        "printf 'CPQS_PID=%s\\n' \"$pid\"",
        "printf 'CPQS_TASK_DONE\\n'",
    ]
    return "\n".join(lines) + "\n"


def build_status_script(server: Server, tasks: Sequence[Task]) -> str:
    """生成远端状态脚本：一次性汇报所有任务的 PID 与状态。"""
    q = shlex.quote
    lines = ["set -u", "printf 'CPQS_STATUS_BEGIN\\n'"]
    for task in tasks:
        lines += [
            "name=" + q(task.name),
            "pidfile=" + q(task_pidfile(server, task)),
            "pid=''",
            "state='stopped'",
            "if [ -f \"$pidfile\" ]; then",
            "    pid=$(cat \"$pidfile\" 2>/dev/null || printf '')",
            "    if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then state='running'; else state='stale'; fi",
            "fi",
            "cmd=''",
            "if [ \"$state\" = 'running' ] && [ -r \"/proc/$pid/cmdline\" ]; then",
            "    cmd=$(tr '\\0' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null || printf '')",
            "fi",
            "printf 'CPQS_ROW|%s|%s|%s|%s\\n' \"$name\" \"$state\" \"$pid\" \"$cmd\"",
        ]
    lines.append("printf 'CPQS_STATUS_DONE\\n'")
    return "\n".join(lines) + "\n"


def explain_ssh_error(
    result: CommandResult,
    server: Optional[Server] = None,
) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    text = stderr or stdout
    lowered = text.lower()
    target = f"（目标: {server.endpoint}）" if server is not None else ""

    if result.returncode == 255:
        if "permission denied" in lowered:
            return "SSH 认证失败（Permission denied）：请确认私钥、用户名和端口，并确认密钥没有 passphrase 或已加入 ssh-agent"
        if "host key verification failed" in lowered:
            return "SSH 主机密钥校验失败：请检查 ~/.ssh/known_hosts，或确认目标主机未被替换"
        if "could not resolve hostname" in lowered:
            match = re.search(
                r"could not resolve hostname\s+([^:\s]+)",
                text,
                flags=re.IGNORECASE,
            )
            host = match.group(1) if match else (server.host if server else "目标主机")
            return (
                f"无法解析主机名 {host}：本机 DNS 解析失败，请检查 DNS、VPN 或 /etc/hosts{target}"
            )
        if (
            "network is unreachable" in lowered
            or "no route to host" in lowered
            or "网络不可达" in text
        ):
            return (
                f"网络不可达：本机没有到目标服务器的路由，请检查网络、网关或 VPN{target}"
            )
        if "connection refused" in lowered:
            return "SSH 连接被拒绝：目标端口未开放或被防火墙拦截"
        if "timed out" in lowered or "timeout" in lowered:
            return "SSH 连接超时：请检查网络和防火墙"
        return f"SSH 连接失败（退出码 255）：{one_line(text)}{target}"

    match = re.search(r"CPQS_ERROR=([A-Za-z0-9_]+)", text)
    if match:
        code = match.group(1)
        return ERROR_HINTS.get(code, f"远端部署失败（{code}）")

    if text:
        return f"远端命令失败（退出码 {result.returncode}）：{one_line(text)}"
    return f"远端命令失败（退出码 {result.returncode}）"


class SshRunner:
    def __init__(self, server: Server, config: Config, verbose: bool = False):
        self.server = server
        self.config = config
        self.verbose = verbose
        self.ssh = shutil.which("ssh")
        if not self.ssh:
            raise DeployError(
                "未找到 ssh 客户端。Windows 请安装 OpenSSH Client；Linux 请安装 openssh-client"
            )
        self._validate_key()

    def _validate_key(self) -> None:
        key = self.server.key_path
        if not key.exists():
            raise DeployError(
                f"私钥不存在: {key}。请确认路径，或生成密钥: ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa"
            )
        if not key.is_file():
            raise DeployError(f"私钥不是普通文件: {key}")
        if os.name != "nt":
            mode = stat.S_IMODE(key.stat().st_mode)
            if mode & 0o077:
                raise DeployError(
                    f"私钥权限过宽: {key} ({oct(mode)})。请执行 chmod 600 {key}"
                )

    def base_args(self) -> List[str]:
        return [
            self.ssh,
            "-T",
            "-x",
            "-i",
            str(self.server.key_path),
            "-p",
            str(self.server.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            f"StrictHostKeyChecking={self.config.strict_host_key_checking}",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout}",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "LogLevel=ERROR",
            self.server.target,
        ]

    def run(
        self,
        script: str,
        stdin_path: Optional[Path] = None,
        timeout: int = 60,
    ) -> CommandResult:
        remote_command = "sh -c " + shlex.quote(script)
        argv = self.base_args() + [remote_command]
        if self.verbose:
            printable = " ".join(shlex.quote(part) for part in argv)
            print(f"  [ssh] {printable}", flush=True)

        start = time.monotonic()
        try:
            if stdin_path is not None:
                with stdin_path.open("rb") as handle:
                    proc = subprocess.run(
                        argv,
                        stdin=handle,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=timeout,
                        check=False,
                    )
            else:
                proc = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise DeployError(f"SSH 操作超时（{timeout} 秒）") from exc
        except OSError as exc:
            raise DeployError(f"无法执行 ssh: {exc}") from exc

        elapsed = time.monotonic() - start
        return CommandResult(
            returncode=proc.returncode,
            stdout=(proc.stdout or b"").decode("utf-8", "replace"),
            stderr=(proc.stderr or b"").decode("utf-8", "replace"),
            elapsed=elapsed,
            argv=tuple(argv),
        )

    def check(self, timeout: Optional[int] = None) -> CheckResult:
        start = time.monotonic()
        if timeout is None:
            timeout = self.config.connect_timeout + 20
        try:
            result = self.run(
                build_check_script(self.server.deploy_path),
                timeout=timeout,
            )
        except DeployError as exc:
            return CheckResult(
                server=self.server,
                ok=False,
                data={},
                warnings=[],
                error=str(exc),
                elapsed=time.monotonic() - start,
            )

        if result.returncode != 0:
            return CheckResult(
                server=self.server,
                ok=False,
                data={},
                warnings=[],
                error=explain_ssh_error(result, self.server),
                elapsed=time.monotonic() - start,
            )

        if "CPQS_CHECK_DONE" not in result.stdout:
            return CheckResult(
                server=self.server,
                ok=False,
                data=parse_kv_output(result.stdout),
                warnings=[],
                error="远端未返回检查完成标记，可能不是预期的 Linux 环境",
                elapsed=time.monotonic() - start,
            )

        data = parse_kv_output(result.stdout)
        warnings: List[str] = []
        error = ""

        actual_user = data.get("USER", "")
        if actual_user and actual_user != self.server.user:
            warnings.append(
                f"远端实际登录用户为 {actual_user}，与配置的 {self.server.user} 不同"
            )

        if data.get("TAR") != "1":
            error = "远端缺少 tar 命令"
        elif data.get("TARGET_EXISTS") == "1":
            if data.get("TARGET_IS_DIR") != "1":
                error = "目标路径存在但不是目录"
            elif data.get("TARGET_WRITABLE") != "1":
                error = "目标目录不可写"
        else:
            if data.get("TARGET_SYMLINK") == "1":
                error = "目标路径是失效的符号链接"
            elif data.get("PARENT_WRITABLE") != "1":
                error = "目标目录不存在，且父目录不可写"

        if data.get("TARGET_SYMLINK") == "1" and data.get("TARGET_IS_DIR") == "1":
            warnings.append("目标路径是符号链接，文件会写入链接指向的目录")
        if data.get("TAR") == "1" and data.get("TAR_NO_SAME_OWNER") != "1":
            warnings.append(
                "远端 tar 不支持 --no-same-owner，将使用兼容模式解包，可能出现属主警告"
            )
        if data.get("PYTHON3") == "0":
            # 任务命令写死了 /usr/bin/python3，缺这个解释器时 start 会立刻失败。
            warnings.append(
                "远端缺少可执行的 /usr/bin/python3，后台任务（如 collector）将无法启动"
            )

        return CheckResult(
            server=self.server,
            ok=not error,
            data=data,
            warnings=warnings,
            error=error,
            elapsed=time.monotonic() - start,
        )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class Reporter:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._lock = threading.Lock()

    def line(self, text: str = "") -> None:
        with self._lock:
            print(text, flush=True)


def _format_check_line(result: CheckResult) -> str:
    data = result.data
    status = "[OK]  " if result.ok else "[FAIL]"
    if data.get("TARGET_EXISTS") == "1":
        target_state = (
            "存在/可写"
            if data.get("TARGET_WRITABLE") == "1"
            else "存在/不可写"
        )
    else:
        target_state = (
            "缺失/父目录可写"
            if data.get("PARENT_WRITABLE") == "1"
            else "缺失/父目录不可写"
        )
    tar_version = data.get("TAR_VERSION", "tar 缺失")
    return (
        f"{status} {result.server.name:<22} {result.server.endpoint:<30} "
        f"{target_state:<16} {tar_version}  ({result.elapsed:.1f}s)"
    )


def _format_deploy_line(result: DeployResult) -> str:
    status = "[OK]  " if result.ok else "[FAIL]"
    size = "-" if result.dry_run else human_size(result.size)
    return (
        f"{status} {result.server.name:<22} {size:>10}  "
        f"{result.elapsed:>6.1f}s  {one_line(result.message, 180)}"
    )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _effective_jobs(requested: Optional[int], configured: int, count: int) -> int:
    jobs = requested if requested is not None else configured
    if not (1 <= jobs <= 32):
        raise DeployError("--jobs 必须在 1..32 之间")
    return max(1, min(jobs, count))


def run_parallel(func, items: Sequence, jobs: int):
    if jobs <= 1:
        return [func(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [executor.submit(func, item) for item in items]
        return [future.result() for future in futures]


def check_one(
    server: Server,
    config: Config,
    reporter: Reporter,
    timeout: Optional[int],
) -> CheckResult:
    try:
        runner = SshRunner(server, config, verbose=reporter.verbose)
        result = runner.check(timeout=timeout)
    except DeployError as exc:
        result = CheckResult(server, False, {}, [], str(exc), 0.0)
    except Exception as exc:  # noqa: BLE001 - 单台失败不应中断其他服务器
        result = CheckResult(server, False, {}, [], f"未预期错误: {exc}", 0.0)

    reporter.line(_format_check_line(result))
    for warning in result.warnings:
        reporter.line(f"        [WARN] {warning}")
    if not result.ok:
        reporter.line(f"        原因: {one_line(result.error)}")
    return result


def deploy_one(
    server: Server,
    config: Config,
    archive: ArchiveInfo,
    reporter: Reporter,
    dry_run: bool,
    timeout: int,
    restart_tasks: bool = False,
) -> DeployResult:
    start = time.monotonic()
    try:
        runner = SshRunner(server, config, verbose=reporter.verbose)
        reporter.line(
            f"[{server.name}] 预检 SSH 与目标目录 ({server.endpoint}) ..."
        )
        check = runner.check()
        for warning in check.warnings:
            reporter.line(f"[{server.name}] [WARN] {warning}")
        if not check.ok:
            return DeployResult(
                server=server,
                ok=False,
                message=f"预检失败: {check.error}",
                elapsed=time.monotonic() - start,
                size=archive.size,
                dry_run=dry_run,
            )

        if dry_run:
            return DeployResult(
                server=server,
                ok=True,
                message="预检通过（dry-run，未上传）",
                elapsed=time.monotonic() - start,
                size=archive.size,
                dry_run=True,
            )

        reporter.line(
            f"[{server.name}] 上传 {human_size(archive.size)} -> {server.deploy_path} ..."
        )
        result = runner.run(
            build_deploy_script(server.deploy_path),
            stdin_path=archive.path,
            timeout=timeout,
        )
        if result.returncode != 0:
            return DeployResult(
                server=server,
                ok=False,
                message=explain_ssh_error(result, server),
                elapsed=time.monotonic() - start,
                size=archive.size,
            )
        if "CPQS_DEPLOY_OK" not in result.stdout:
            return DeployResult(
                server=server,
                ok=False,
                message="远端未返回部署成功标记",
                elapsed=time.monotonic() - start,
                size=archive.size,
            )

        message = "部署完成"
        if restart_tasks:
            message += "；" + _restart_tasks_on_server(server, config, reporter)

        return DeployResult(
            server=server,
            ok=True,
            message=message,
            elapsed=time.monotonic() - start,
            size=archive.size,
        )
    except DeployError as exc:
        return DeployResult(
            server=server,
            ok=False,
            message=str(exc),
            elapsed=time.monotonic() - start,
            size=archive.size,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        return DeployResult(
            server=server,
            ok=False,
            message=f"未预期错误: {exc}",
            elapsed=time.monotonic() - start,
            size=archive.size,
            dry_run=dry_run,
        )


@dataclass(frozen=True)
class TaskOutcome:
    """一次远端任务操作的结果。"""

    server: Server
    task: Task
    ok: bool
    action: str
    pid: str
    message: str
    elapsed: float


def _placeholder_task() -> Task:
    return Task(name="(未选择)", command="")


def _extract_log_tail(stdout: str, limit: int = 5) -> List[str]:
    """从远端输出里取出日志尾部（启动失败时给用户看现场）。"""
    lines = stdout.splitlines()
    try:
        begin = lines.index("CPQS_LOG_TAIL_BEGIN")
        end = lines.index("CPQS_LOG_TAIL_END")
    except ValueError:
        return []
    tail = [line for line in lines[begin + 1 : end] if line.strip()]
    return tail[-limit:]


def start_task_on(
    server: Server,
    config: Config,
    task: Task,
    reporter: Reporter,
    timeout: int,
) -> TaskOutcome:
    """在远端后台启动任务。"""
    start = time.monotonic()
    try:
        runner = SshRunner(server, config, verbose=reporter.verbose)
        result = runner.run(build_start_script(server, task), timeout=timeout)
    except DeployError as exc:
        return TaskOutcome(
            server, task, False, "error", "", str(exc), time.monotonic() - start
        )
    except Exception as exc:  # noqa: BLE001 - 单台失败不应中断其他服务器
        return TaskOutcome(
            server,
            task,
            False,
            "error",
            "",
            f"未预期错误: {exc}",
            time.monotonic() - start,
        )

    elapsed = time.monotonic() - start
    data = parse_kv_output(result.stdout)
    pid = data.get("CPQS_PID", "")

    if result.returncode != 0:
        message = explain_ssh_error(result, server)
        tail = _extract_log_tail(result.stdout)
        if tail:
            message += " | 日志尾部: " + " / ".join(one_line(line, 120) for line in tail)
        return TaskOutcome(server, task, False, "error", pid, message, elapsed)
    if "CPQS_ALREADY_RUNNING" in result.stdout:
        return TaskOutcome(
            server, task, True, "already_running", pid, "已在运行，未重复启动", elapsed
        )
    if "CPQS_STARTED" in result.stdout:
        message = "已启动"
        if pid:
            message += f"（pid={pid}）"
        logfile = data.get("CPQS_LOG", "")
        if logfile:
            message += f"，日志 {logfile}"
        return TaskOutcome(server, task, True, "started", pid, message, elapsed)
    return TaskOutcome(
        server, task, False, "error", pid, "远端未返回启动成功标记", elapsed
    )


def stop_task_on(
    server: Server,
    config: Config,
    task: Task,
    reporter: Reporter,
    timeout: int,
) -> TaskOutcome:
    """结束远端后台任务。"""
    start = time.monotonic()
    try:
        runner = SshRunner(server, config, verbose=reporter.verbose)
        result = runner.run(build_stop_script(server, task), timeout=timeout)
    except DeployError as exc:
        return TaskOutcome(
            server, task, False, "error", "", str(exc), time.monotonic() - start
        )
    except Exception as exc:  # noqa: BLE001
        return TaskOutcome(
            server,
            task,
            False,
            "error",
            "",
            f"未预期错误: {exc}",
            time.monotonic() - start,
        )

    elapsed = time.monotonic() - start
    data = parse_kv_output(result.stdout)
    pid = data.get("CPQS_PID", "")

    if result.returncode != 0:
        return TaskOutcome(
            server, task, False, "error", pid, explain_ssh_error(result, server), elapsed
        )
    if "CPQS_NOT_RUNNING" in result.stdout:
        return TaskOutcome(
            server, task, True, "not_running", "", "本来就没有在运行", elapsed
        )
    if "CPQS_STOPPED" in result.stdout:
        message = "已结束"
        if pid:
            message += f"（pid={pid}）"
        if "CPQS_FORCE_KILLED" in result.stdout:
            message += "，超时后强制终止"
        return TaskOutcome(server, task, True, "stopped", pid, message, elapsed)
    return TaskOutcome(
        server, task, False, "error", pid, "远端未返回停止成功标记", elapsed
    )


def status_tasks_on(
    server: Server,
    config: Config,
    tasks: Sequence[Task],
    reporter: Reporter,
    timeout: int,
) -> List[TaskOutcome]:
    """查询远端任务状态。"""
    start = time.monotonic()
    by_name = {task.name: task for task in tasks}
    try:
        runner = SshRunner(server, config, verbose=reporter.verbose)
        result = runner.run(build_status_script(server, tasks), timeout=timeout)
    except DeployError as exc:
        return [
            TaskOutcome(server, task, False, "error", "", str(exc), 0.0) for task in tasks
        ]
    except Exception as exc:  # noqa: BLE001
        return [
            TaskOutcome(
                server, task, False, "error", "", f"未预期错误: {exc}", 0.0
            )
            for task in tasks
        ]

    elapsed = time.monotonic() - start
    if result.returncode != 0:
        message = explain_ssh_error(result, server)
        return [
            TaskOutcome(server, task, False, "error", "", message, elapsed)
            for task in tasks
        ]

    rows: Dict[str, Tuple[str, str, str]] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("CPQS_ROW|"):
            continue
        parts = line.split("|", 4)
        if len(parts) < 4:
            continue
        rows[parts[1]] = (parts[2], parts[3], parts[4] if len(parts) > 4 else "")

    outcomes: List[TaskOutcome] = []
    for task in tasks:
        state, pid, cmd = rows.get(task.name, ("unknown", "", ""))
        if state == "running":
            message = f"运行中（pid={pid}）"
            if cmd:
                message += f" {cmd.strip()}"
            outcomes.append(
                TaskOutcome(server, task, True, "running", pid, message, elapsed)
            )
        elif state == "stale":
            outcomes.append(
                TaskOutcome(
                    server,
                    task,
                    True,
                    "stale",
                    pid,
                    "PID 文件残留但进程已不存在（可执行 stop 清理）",
                    elapsed,
                )
            )
        else:
            outcomes.append(
                TaskOutcome(server, task, True, "stopped", "", "未运行", elapsed)
            )
    return outcomes


def _restart_tasks_on_server(
    server: Server, config: Config, reporter: Reporter
) -> str:
    """重启该服务器上的全部启用任务，返回一句摘要。"""
    tasks = resolve_tasks(config, server)
    if not tasks:
        return "没有启用的后台任务"
    timeout = config.connect_timeout + 60
    parts: List[str] = []
    for task in tasks:
        stopped = stop_task_on(server, config, task, reporter, timeout)
        if not stopped.ok:
            parts.append(f"{task.name} 停止失败({one_line(stopped.message, 60)})")
            continue
        started = start_task_on(server, config, task, reporter, timeout)
        if started.ok:
            parts.append(f"{task.name} {started.action}")
        else:
            parts.append(f"{task.name} 启动失败({one_line(started.message, 60)})")
    return "后台任务: " + "; ".join(parts)


def _format_task_line(result: TaskOutcome) -> str:
    status = "[OK]  " if result.ok else "[FAIL]"
    action = result.action or "-"
    return (
        f"{status} {result.server.name:<12} {result.task.name:<16} {action:<16} "
        f"{result.elapsed:>6.1f}s  {one_line(result.message, 160)}"
    )


def _print_task_summary(results: Sequence[TaskOutcome], title: str, reporter: Reporter) -> None:
    reporter.line("")
    reporter.line("=" * 92)
    reporter.line(title)
    reporter.line("-" * 92)
    for result in results:
        reporter.line(_format_task_line(result))
    reporter.line("-" * 92)
    passed = sum(1 for result in results if result.ok)
    reporter.line(f"成功 {passed} / {len(results)}")
    reporter.line("=" * 92)


def _confirm_multi(
    targets: Sequence[Server], action: str, reporter: Reporter, assume_yes: bool
) -> None:
    """多台服务器执行写操作前的确认。"""
    if assume_yes or len(targets) <= 1:
        return
    if not sys.stdin.isatty():
        raise DeployError(f"多台服务器{action}需要确认；非交互环境请加 --yes")
    reporter.line(f"即将在以下服务器上{action}：")
    for server in targets:
        reporter.line(f"  - {server.name:<22} {server.endpoint:<30} {server.deploy_path}")
    answer = input("确认请输入 yes: ").strip().lower()
    if answer not in ("yes", "y"):
        raise DeployError(f"已取消{action}")


def _task_timeout(config: Config, args: argparse.Namespace, extra: int = 30) -> int:
    timeout = getattr(args, "timeout", None)
    if timeout is None:
        timeout = config.connect_timeout + extra
    if not (10 <= timeout <= 3600):
        raise DeployError("--timeout 必须在 10..3600 之间")
    return timeout


def _tasks_for_server(
    server: Server, config: Config, names: Sequence[str]
) -> List[Task]:
    return select_tasks(config, server, names)


def cmd_start(args: argparse.Namespace) -> int:
    """启动远端后台任务。"""
    config = _load_config_arg(args)
    targets = select_servers(config, args.target or [], args.group or [], args.all)
    jobs = _effective_jobs(args.jobs, config.jobs, len(targets))
    timeout = _task_timeout(config, args)
    reporter = Reporter(args.verbose)

    reporter.line(f"CPQS 后台任务启动 v{VERSION}")
    reporter.line(f"目标数量 : {len(targets)}")
    reporter.line(f"任务     : {', '.join(args.task) if args.task else '全部已启用任务'}")
    reporter.line("")
    _confirm_multi(targets, "启动后台任务", reporter, args.yes)

    def handle(server: Server) -> List[TaskOutcome]:
        try:
            tasks = _tasks_for_server(server, config, args.task or [])
        except DeployError as exc:
            return [TaskOutcome(server, _placeholder_task(), False, "error", "", str(exc), 0.0)]
        if not tasks:
            return [
                TaskOutcome(
                    server, _placeholder_task(), True, "skipped", "", "没有启用的任务", 0.0
                )
            ]
        return [
            start_task_on(server, config, task, reporter, timeout) for task in tasks
        ]

    results: List[TaskOutcome] = []
    for group in run_parallel(handle, targets, jobs):
        results.extend(group)
    _print_task_summary(results, "启动汇总", reporter)
    return 0 if all(result.ok for result in results) else 1


def cmd_stop(args: argparse.Namespace) -> int:
    """结束远端后台任务。"""
    config = _load_config_arg(args)
    targets = select_servers(config, args.target or [], args.group or [], args.all)
    jobs = _effective_jobs(args.jobs, config.jobs, len(targets))
    timeout = _task_timeout(config, args, extra=60)
    reporter = Reporter(args.verbose)

    reporter.line(f"CPQS 后台任务结束 v{VERSION}")
    reporter.line(f"目标数量 : {len(targets)}")
    reporter.line(f"任务     : {', '.join(args.task) if args.task else '全部已启用任务'}")
    reporter.line("")
    _confirm_multi(targets, "结束后台任务", reporter, args.yes)

    def handle(server: Server) -> List[TaskOutcome]:
        try:
            tasks = _tasks_for_server(server, config, args.task or [])
        except DeployError as exc:
            return [TaskOutcome(server, _placeholder_task(), False, "error", "", str(exc), 0.0)]
        if not tasks:
            return [
                TaskOutcome(
                    server, _placeholder_task(), True, "skipped", "", "没有启用的任务", 0.0
                )
            ]
        return [
            stop_task_on(server, config, task, reporter, timeout) for task in tasks
        ]

    results: List[TaskOutcome] = []
    for group in run_parallel(handle, targets, jobs):
        results.extend(group)
    _print_task_summary(results, "结束汇总", reporter)
    return 0 if all(result.ok for result in results) else 1


def cmd_status(args: argparse.Namespace) -> int:
    """查看远端后台任务状态。"""
    config = _load_config_arg(args)
    targets = select_servers(config, args.target or [], args.group or [], args.all)
    jobs = _effective_jobs(args.jobs, config.jobs, len(targets))
    timeout = _task_timeout(config, args)
    reporter = Reporter(args.verbose)

    reporter.line(f"CPQS 后台任务状态 v{VERSION}")
    reporter.line(f"目标数量 : {len(targets)}")
    reporter.line("")

    def handle(server: Server) -> List[TaskOutcome]:
        try:
            tasks = _tasks_for_server(server, config, args.task or [])
        except DeployError as exc:
            return [TaskOutcome(server, _placeholder_task(), False, "error", "", str(exc), 0.0)]
        if not tasks:
            return [
                TaskOutcome(
                    server, _placeholder_task(), True, "skipped", "", "没有启用的任务", 0.0
                )
            ]
        return status_tasks_on(server, config, tasks, reporter, timeout)

    results: List[TaskOutcome] = []
    for group in run_parallel(handle, targets, jobs):
        results.extend(group)
    _print_task_summary(results, "状态汇总", reporter)
    return 0 if all(result.ok for result in results) else 1


def cmd_restart(args: argparse.Namespace) -> int:
    """重启远端后台任务（先结束再启动）。"""
    config = _load_config_arg(args)
    targets = select_servers(config, args.target or [], args.group or [], args.all)
    jobs = _effective_jobs(args.jobs, config.jobs, len(targets))
    timeout = _task_timeout(config, args, extra=60)
    reporter = Reporter(args.verbose)

    reporter.line(f"CPQS 后台任务重启 v{VERSION}")
    reporter.line(f"目标数量 : {len(targets)}")
    reporter.line(f"任务     : {', '.join(args.task) if args.task else '全部已启用任务'}")
    reporter.line("")
    _confirm_multi(targets, "重启后台任务", reporter, args.yes)

    def handle(server: Server) -> List[TaskOutcome]:
        try:
            tasks = _tasks_for_server(server, config, args.task or [])
        except DeployError as exc:
            return [TaskOutcome(server, _placeholder_task(), False, "error", "", str(exc), 0.0)]
        if not tasks:
            return [
                TaskOutcome(
                    server, _placeholder_task(), True, "skipped", "", "没有启用的任务", 0.0
                )
            ]
        outcomes: List[TaskOutcome] = []
        for task in tasks:
            outcomes.append(
                stop_task_on(server, config, task, reporter, timeout)
            )
            if outcomes[-1].ok:
                outcomes.append(
                    start_task_on(server, config, task, reporter, timeout)
                )
        return outcomes

    results: List[TaskOutcome] = []
    for group in run_parallel(handle, targets, jobs):
        results.extend(group)
    _print_task_summary(results, "重启汇总", reporter)
    return 0 if all(result.ok for result in results) else 1


def _print_summary(results: Sequence, title: str, reporter: Reporter, dry_run: bool = False) -> None:
    reporter.line("")
    reporter.line("=" * 92)
    reporter.line(title)
    reporter.line("-" * 92)
    for result in results:
        if isinstance(result, DeployResult):
            reporter.line(_format_deploy_line(result))
        else:
            reporter.line(_format_check_line(result))
    reporter.line("-" * 92)
    passed = sum(1 for result in results if result.ok)
    reporter.line(f"通过 {passed} / {len(results)}")
    reporter.line("=" * 92)


# ---------------------------------------------------------------------------
# CLI 命令
# ---------------------------------------------------------------------------


def _load_config_arg(args: argparse.Namespace) -> Config:
    raw = args.config
    if raw is None:
        env_path = os.environ.get("CPQS_DEPLOY_CONFIG")
        raw = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    return load_config(Path(raw))


def _resolve_source(args: argparse.Namespace, config: Config) -> Path:
    raw = args.source if args.source is not None else config.source
    source = Path(raw).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    return source.resolve(strict=False)


def _validate_source(source: Path) -> None:
    if not source.exists():
        raise DeployError(f"源目录不存在: {source}")
    if not source.is_dir():
        raise DeployError(f"源路径不是目录: {source}")
    if source == TOOL_DIR or TOOL_DIR in source.parents:
        raise DeployError("源目录不能是部署工具目录或位于部署工具目录内")


def _save_archive(archive: ArchiveInfo, destination: Path, reporter: Reporter) -> Path:
    destination = destination.expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    if destination.exists() and destination.is_dir():
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        destination = destination / f"cpqs-deploy-{timestamp}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive.path, destination)
    reporter.line(f"归档已保存: {destination.resolve()}")
    return destination


def cmd_list(args: argparse.Namespace) -> int:
    config = _load_config_arg(args)
    if args.json:
        payload = {
            "config": str(config.path),
            "source": str(config.source),
            "deploy_path": config.deploy_path,
            "key_path": str(config.key_path),
            "tasks": [
                {
                    "name": task.name,
                    "command": task.command,
                    "cwd": task.cwd,
                    "env": dict(task.env),
                    "setup": task.setup,
                    "stop_timeout": task.stop_timeout,
                }
                for task in resolve_tasks(config, config.servers[0])
            ],
            "servers": [
                {
                    "name": server.name,
                    "host": server.host,
                    "port": server.port,
                    "user": server.user,
                    "group": server.group,
                    "deploy_path": server.deploy_path,
                    "key_path": str(server.key_path),
                    "tasks": [
                        task.name for task in resolve_tasks(config, server)
                    ],
                }
                for server in config.servers
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"配置文件 : {config.path}")
    print(f"源目录   : {config.source}")
    print(f"部署路径 : {config.deploy_path}")
    print(f"私钥     : {config.key_path}")
    print()
    print(
        f"{'名称':<22} {'用户':<10} {'主机':<24} {'端口':>5}  "
        f"{'分组':<10} {'后台任务':<16} 部署路径"
    )
    print("-" * 100)
    for server in config.servers:
        task_names = ",".join(task.name for task in resolve_tasks(config, server))
        print(
            f"{server.name:<22} {server.user:<10} {server.host:<24} "
            f"{server.port:>5}  {server.group:<10} {task_names:<16} {server.deploy_path}"
        )
    print("-" * 100)
    print(f"共 {len(config.servers)} 台服务器")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = _load_config_arg(args)
    targets = select_servers(config, args.target or [], args.group or [], args.all)
    jobs = _effective_jobs(args.jobs, config.jobs, len(targets))
    if args.timeout is not None and not (1 <= args.timeout <= 3600):
        raise DeployError("--timeout 必须在 1..3600 之间")
    reporter = Reporter(args.verbose)

    reporter.line(f"检查 {len(targets)} 台服务器（并发 {jobs}）...")
    reporter.line("")
    results = run_parallel(
        lambda server: check_one(server, config, reporter, args.timeout),
        targets,
        jobs,
    )
    _print_summary(results, "检查汇总", reporter)
    return 0 if all(result.ok for result in results) else 1


def cmd_deploy(args: argparse.Namespace) -> int:
    config = _load_config_arg(args)
    targets = select_servers(config, args.target or [], args.group or [], args.all)
    source = _resolve_source(args, config)
    _validate_source(source)

    jobs = _effective_jobs(args.jobs, config.jobs, len(targets))
    timeout = args.timeout if args.timeout is not None else config.deploy_timeout
    if not (10 <= timeout <= 86400):
        raise DeployError("--timeout 必须在 10..86400 之间")

    reporter = Reporter(args.verbose)
    rules = build_ignore_rules(source, args.exclude or [])

    reporter.line(f"CPQS 代码部署工具 v{VERSION}")
    reporter.line(f"源目录   : {source}")
    reporter.line(f"部署路径 : {config.deploy_path}")
    reporter.line(f"目标数量 : {len(targets)}")
    reporter.line("")
    reporter.line("正在生成归档 ...")

    archive: Optional[ArchiveInfo] = None
    try:
        archive = build_archive(source, rules)
        reporter.line(
            f"归档完成 : {human_size(archive.size)}，"
            f"{archive.files} 个文件，{archive.dirs} 个目录，"
            f"跳过 {archive.skipped} 项"
        )
        reporter.line(f"SHA256   : {archive.sha256}")
        if args.keep_archive:
            _save_archive(archive, args.keep_archive, reporter)
        reporter.line("")

        if not args.dry_run and not args.yes and len(targets) > 1:
            if not sys.stdin.isatty():
                raise DeployError("多台服务器部署需要确认；非交互环境请加 --yes")
            reporter.line("即将部署到以下服务器：")
            for server in targets:
                reporter.line(f"  - {server.name:<22} {server.endpoint:<30} {server.deploy_path}")
            answer = input("确认请输入 yes: ").strip().lower()
            if answer not in ("yes", "y"):
                raise DeployError("已取消部署")
            reporter.line("")

        mode = "预检（dry-run，不写入远端）" if args.dry_run else "部署"
        reporter.line(f"开始{mode}，并发 {jobs} ...")
        reporter.line("")
        results = run_parallel(
            lambda server: deploy_one(
                server,
                config,
                archive,
                reporter,
                args.dry_run,
                timeout,
                args.restart_tasks,
            ),
            targets,
            jobs,
        )
        _print_summary(
            results,
            "部署汇总（dry-run，未上传）" if args.dry_run else "部署汇总",
            reporter,
            dry_run=args.dry_run,
        )
        return 0 if all(result.ok for result in results) else 1
    finally:
        if archive is not None:
            archive.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--all",
        action="store_true",
        help="选择 servers.json 中的全部服务器",
    )
    parser.add_argument(
        "--target",
        action="append",
        metavar="NAME",
        help="按名称选择服务器，可重复使用",
    )
    parser.add_argument(
        "--group",
        action="append",
        metavar="GROUP",
        help="按分组选择服务器，可重复使用",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="CPQS 代码部署工具：通过 SSH 把项目代码部署到 /opt/CPQS。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  py deploy.py list\n"
            "  py deploy.py check --all\n"
            "  py deploy.py deploy --target X1 --dry-run\n"
            "  py deploy.py deploy --group xfusion\n"
            "  py deploy.py deploy --all\n"
            "  py deploy.py deploy --all --restart-tasks   # 部署后重启 collector\n"
            "  py deploy.py start --all                    # 后台启动 metrics_exporter.py\n"
            "  py deploy.py status --target X1\n"
            "  py deploy.py stop --all                     # 结束后台任务\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出 SSH 命令等调试信息",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list",
        parents=[common],
        help="列出服务器配置",
    )
    list_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    list_parser.set_defaults(func=cmd_list)

    check_parser = subparsers.add_parser(
        "check",
        parents=[common],
        help="检查 SSH 登录、目标目录和 tar",
    )
    _add_selection(check_parser)
    check_parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="并发数（默认取配置中的 jobs）",
    )
    check_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="单台检查超时秒数（默认 connect_timeout + 20）",
    )
    check_parser.set_defaults(func=cmd_check)

    deploy_parser = subparsers.add_parser(
        "deploy",
        parents=[common],
        help="打包并部署代码",
    )
    _add_selection(deploy_parser)
    deploy_parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="待部署的本地目录（默认取配置中的 source）",
    )
    deploy_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打包并预检，不上传任何文件",
    )
    deploy_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="额外排除规则，可重复使用",
    )
    deploy_parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="并发数（默认取配置中的 jobs）",
    )
    deploy_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="单台部署超时秒数（默认取配置中的 timeout）",
    )
    deploy_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="多台部署时跳过交互确认",
    )
    deploy_parser.add_argument(
        "--keep-archive",
        type=Path,
        default=None,
        metavar="PATH",
        help="把生成的 tar.gz 保存到指定文件或目录",
    )
    deploy_parser.add_argument(
        "--restart-tasks",
        action="store_true",
        help="部署成功后重启该服务器上的后台任务（如 collector）",
    )
    deploy_parser.set_defaults(func=cmd_deploy)

    for name, help_text, func in (
        ("start", "在远端后台启动任务（如 metrics_exporter.py）", cmd_start),
        ("stop", "结束远端后台任务", cmd_stop),
        ("restart", "重启远端后台任务", cmd_restart),
        ("status", "查看远端后台任务状态", cmd_status),
    ):
        task_parser = subparsers.add_parser(
            name,
            parents=[common],
            help=help_text,
        )
        _add_selection(task_parser)
        task_parser.add_argument(
            "--task",
            action="append",
            default=[],
            metavar="NAME",
            help="指定任务名，可重复使用；不填则处理全部已启用任务",
        )
        task_parser.add_argument(
            "--jobs",
            type=int,
            default=None,
            help="并发数（默认取配置中的 jobs）",
        )
        task_parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="单台操作超时秒数（默认 connect_timeout + 30）",
        )
        task_parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help="多台服务器时跳过交互确认",
        )
        task_parser.set_defaults(func=func)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_console()
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except DeployError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
