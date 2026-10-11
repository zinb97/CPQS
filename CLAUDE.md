# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目结构

仓库由三个相对独立的部分组成：

| 目录 | 类型 | 作用 | 依赖 |
|------|------|------|------|
| `collector/` | 本仓库 | Linux 主机 + GPU 指标采集，HTTP 暴露为 Prometheus 指标 | 纯标准库 + `pynvml`（无第三方依赖） |
| `backen/` | 本仓库 | FastAPI Dashboard，从 Prometheus 拉取指标并渲染 | FastAPI / uvicorn / httpx / jinja2 / pyyaml |
| `QoSController/` | git 子模块 | RYU SDN 控制器 + Mininet 仿真，QoS 路由策略 | RYU / Mininet / gRPC / OpenFlow 1.3 |

子模块指向 `https://github.com/zinb97/QoSController.git`，首次克隆后需 `git submodule update --init`。`QoSController/CLAUDE.md` 自带完整架构、命令、指标列表与 DSCP 路由策略——**修改子模块前先读它**。

`backen/README.md` 含 `backen/` 完整架构、API、环境变量与运行方式——**修改 `backen/` 前先读它**。本文件其余部分主要描述 `collector/`。

## 端到端数据流

```
主机 (Linux + GPU)
   └─ collector/metrics_exporter.py :9100/metrics
        └─ Prometheus (默认 192.168.10.31:9090)
             └─ backen/app/prometheus/client.py (PromQL 查询)
                  └─ backen FastAPI :8080 (SSR + /api/metrics/* JSON)
```

`backen` **不直连** `collector`，必须经由 Prometheus。

## 根目录的杂项

仓库根目录有与项目无关的旧文件，**不要修改或引用**：
- `main.py` — PyCharm 自动生成的样板（`print_hi`），非任何入口
- `index.html` — 旧版独立 Dashboard（1154 行），无后端连接，已被 `backen/` 取代

---

## collector/ 运行方式

入口为 `collector/metrics_exporter.py`（**不是** `main.py`，那是 PyCharm 自动生成的样板代码）。由于使用了裸导入（`from host_collector import ...`），必须从 `collector/` 目录下启动：

```bash
cd collector
python metrics_exporter.py --port 9100
nohup python3 metrics_exporter.py > run.log 2>&1 &
```

HTTP 端点：
- `/metrics` — Prometheus 文本格式
- `/health` — 返回 `OK`
- `/` — 带有链接的索引页

## collector/ 代码结构

- `collector/host_collector.py` — `HostCollector` 类。所有的 `_read_*` 方法各自读取一个 procfs/sysfs 数据源；`_collect_all()` 一次性调用它们并将结果存入 `self._data`。提供两种输出方法：`to_json()` 与 `to_prometheus(per_cpu=True)`。
- `collector/gpu_collector.py` — `GpuCollector` 类，通过 `pynvml` 调用 NVML。采集 `references/metrics_extracted.csv` 中全部 18 个 `DCGM_FI_DEV_*` 指标（命名约定参考）。提供 `to_json()` 与 `to_prometheus(**_kwargs)`（忽略多余 kwargs，便于统一调用）。依赖 `pynvml`（硬依赖，无降级）。
- `collector/metrics_exporter.py` — `MetricsHandler`（`http.server.BaseHTTPRequestHandler` 的子类）与 `MetricsExporter`（封装 `socketserver.TCPServer`）。处理器持有 `collectors` 列表，每次抓取时对每个采集器调用 `to_prometheus(per_cpu=...)` 并拼接输出；`MetricsExporter` 默认同时挂载 `HostCollector` 与 `GpuCollector`。

## 采集的数据源

依据 `host_collector.py` 中的 `_collect_all()`：

- `/etc/os-release` → `_read_os_info`（解析 `KEY="VALUE"` 形式的键值对）
- `/sys/class/dmi/id/*` → `_read_dmi`（bios_date、bios_vendor、bios_version、product_name、system_vendor）
- `platform.uname()` → `_read_uname`
- `/proc/stat` → `_read_cpu_stat`（始终读取 `cpu` 总计；仅当 `per_cpu=True` 时读取每核数据）
- `/proc/loadavg` → `_read_loadavg`（load1/5/15 + 运行中/总进程数）
- `/proc/meminfo` → `_read_meminfo`（值 × 1024 转换为字节）
- `/proc/net/snmp` → `_read_snmp`（成对的表头/值行）
- `/proc/net/dev` → `_read_network`（跳过 `lo`、`br*`、`docker*`、`veth*`、`b.*`）
- `/proc/net/sockstat` → `_read_sockstat`
- `/proc/net/netstat` → `_read_netstat`（成对的表头/值行）
- `/proc/diskstats` → `_read_diskstats`（跳过 `loop*`、`ram*`；兼容 14 列与 20 列两种格式）
- `/proc/mounts` → `_read_mounts`（使用 `os.statvfs`；按设备去重）
- `/sys/class/thermal/thermal_zone*` → `_read_thermal`（原始温度 `/1000` 转换为 °C）

## Prometheus 输出约定

- `node_cpu_seconds_total{mode=...}` — 在 `_read_cpu_stat` 中已除以 100，因此直接就是秒。
- 磁盘的 `sectors_read`/`sectors_written` 在 `to_prometheus()` 中乘以 512 转换为字节。
- 磁盘的 `io_time_ms` 除以 1000 转换为秒。
- 计数器不带 `# HELP` / `# TYPE` 头——由 Prometheus 自动推断类型。
- `node_uname_info`、`node_os_info`、`node_dmi_info` 的标签集即为原始键值映射（未做归一化）。**未对标签值做引号转义**；若值中包含 `"` 会破坏解析。

## 验证

仓库中**无测试、无 linter 配置**。

- `collector/`：
  ```bash
  python -m py_compile collector/host_collector.py
  python -m py_compile collector/metrics_exporter.py
  python -m py_compile collector/gpu_collector.py
  ```
- `backen/`：见 `backen/README.md` 的「验证」段落。
- `QoSController/`：见 `QoSController/CLAUDE.md` 的「常用命令」段落。
