# CPQS 代码部署工具

把本地项目代码通过 SSH 部署到多台服务器的 `/opt/CPQS`。

工具只依赖 **Python 标准库 + 系统 OpenSSH 客户端**，不需要 `rsync`、`scp` 或任何 pip 包。

## 特性

- 使用 `~/.ssh/id_rsa` 密钥登录，不保存密码；
- 支持多台服务器（可按名称、分组或全部选择）；
- 多台并发部署，默认 4 并发；
- 部署前自动检查 SSH、目标目录权限、远端 `tar`；
- `--dry-run` 只打包和预检，不写远端；
- 通过 `tar.gz` 流式传输，适合 Windows 本地环境；
- 默认不删除远端已有文件，避免误删日志、配置或虚拟环境；
- 从 git 索引读取文件权限，Windows 上也能保留脚本的可执行位；
- **可在远端后台拉起常驻进程**（如 `metrics_exporter.py`），并随时结束、查看状态；
- 分组写在 `servers.json` 顶层的 `group` 段，可用 `deploy.py group ...` 增删改（不必手改 JSON）。

## 目录结构

```text
deploy-tool/
├── deploy.py            # 主程序（入口）
├── deploy.ps1           # Windows PowerShell 启动器（可选）
├── servers.json         # 服务器与部署配置
├── .deployignore        # 额外排除规则
├── README.md            # 本文件
└── tests/
    └── test_deploy.py   # 离线单元测试
```

## 环境要求

本地：

- Python 3.9+（当前机器用 `py` 启动即可）；
- OpenSSH Client（Windows 10/11 默认自带 `ssh.exe`）；
- 私钥 `~/.ssh/id_rsa` 存在，且没有 passphrase，或已经加载到 `ssh-agent`。

远端：

- Linux；
- 有 `tar`（GNU tar 1.30+ 已验证）；
- `/opt/CPQS` 已存在且当前登录用户可写，或者父目录可写以便创建；
- 当前登录用户有权限写入部署目录。

## 快速开始

在项目根目录下执行：

```powershell
# 1. 查看服务器列表
py deploy-tool\deploy.py list

# 2. 检查全部服务器（只读，不部署）
py deploy-tool\deploy.py check --all

# 3. 单台预演：打包、检查，但不上传
py deploy-tool\deploy.py deploy --target X1 --dry-run

# 4. 部署到单台
py deploy-tool\deploy.py deploy --target X1

# 5. 按用户分组部署
py deploy-tool\deploy.py deploy --group xfusion
py deploy-tool\deploy.py deploy --group hello

# 6. 部署到全部服务器
py deploy-tool\deploy.py deploy --all

# 7. 在远端后台拉起 collector（metrics_exporter.py），脱离 SSH 会话常驻
py deploy-tool\deploy.py start --all

# 8. 查看/结束远端后台任务
py deploy-tool\deploy.py status --target D1
py deploy-tool\deploy.py stop --all

# 9. 查看分组、维护分组（写回 servers.json）
py deploy-tool\deploy.py group list
py deploy-tool\deploy.py group add demo X1 X2
py deploy-tool\deploy.py group add demo X3 --dry-run   # 只显示结果，不写文件
```

也可以使用 PowerShell 启动器：

```powershell
.\deploy-tool\deploy.ps1 list
.\deploy-tool\deploy.ps1 check --all
.\deploy-tool\deploy.ps1 deploy --target X1
```

> 部署到多台服务器时会要求输入 `yes` 确认；非交互环境（CI）请加 `--yes`。
>
> 单台部署默认不询问确认。部署前建议先跑一次 `--dry-run`。

### 常用参数

| 参数 | 说明 |
|------|------|
| `--all` | 选择全部服务器 |
| `--target NAME` | 按名称选择，可重复 |
| `--group GROUP` | 按顶层 `group` 段的分组选择，可重复（多组取并集） |
| `--dry-run` | 只打包和预检，不上传 |
| `--exclude PATTERN` | 追加排除规则，可重复 |
| `--jobs N` | 并发数，默认取配置中的 `jobs` |
| `--timeout N` | 单台部署超时秒数 |
| `-y`, `--yes` | 多台部署时跳过确认 |
| `--keep-archive PATH` | 保留生成的 tar.gz 到文件或目录 |
| `-v`, `--verbose` | 输出完整 SSH 命令，便于排错 |
| `--config PATH` | 指定其他配置文件 |

## 服务器列表

配置在 [`servers.json`](servers.json)，当前包含：

| 名称 | 分组 | 主机 | 端口 | 用户 | 部署路径 | 后台任务 |
|------|------|------|------|------|----------|----------|
| `X1` | xfusion | S501.site | 1011 | xfusion | `/opt/CPQS` | collector |
| `X2` | xfusion | S501.site | 1012 | xfusion | `/opt/CPQS` | collector |
| `X3` | xfusion | S501.site | 1013 | xfusion | `/opt/CPQS` | collector |
| `X4` | xfusion | S501.site | 1014 | xfusion | `/opt/CPQS` | collector |
| `X5` | xfusion | S501.site | 1015 | xfusion | `/opt/CPQS` | collector |
| `L1` | hello | S501.site | 1021 | hello | `/opt/CPQS` | collector |
| `L2` | hello | S501.site | 1022 | hello | `/opt/CPQS` | collector |
| `L3` | hello | S501.site | 1023 | hello | `/opt/CPQS` | collector |
| `L4` | hello | S501.site | 1024 | hello | `/opt/CPQS` | collector |
| `D1` | lab | 192.168.10.31 | 22 | u | `/opt/CPQS` | collector |

> 表内容以 `deploy.py list` 的实时输出为准；`--target` 填的是这里的**名称**。
> 「分组」一列来自 `servers.json` 顶层的 `group` 段（见[分组管理](#分组管理)），一台服务器可以同时属于多个分组。

## 配置说明

[`servers.json`](servers.json) 是标准 JSON，不能写注释。字段如下：

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `deploy_path` | 否 | `/opt/CPQS` | 全局远端部署目录，必须是绝对路径 |
| `key_path` | 否 | `~/.ssh/id_rsa` | 全局私钥路径，支持 `~` 和环境变量 |
| `source` | 否 | `..` | 本地源目录；相对路径相对于 `servers.json` 所在目录 |
| `connect_timeout` | 否 | `10` | SSH 连接超时秒数，1–120 |
| `jobs` | 否 | `4` | 默认并发数，1–32 |
| `timeout` | 否 | `900` | 单台部署超时秒数，10–86400 |
| `strict_host_key_checking` | 否 | `accept-new` | `yes` / `no` / `ask` / `accept-new` |
| `exclude` | 否 | `[]` | 额外排除规则数组，与 `.deployignore` 叠加 |
| `tasks` | 否 | 内置 `collector` | 后台任务定义，见「后台任务」一节 |
| `group` | 否 | `{}` | 分组表：`{"组名": ["服务器名", ...]}`，见[分组管理](#分组管理) |
| `servers` | 是 | — | 服务器数组，不能为空 |

每个服务器对象：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 否 | 唯一名称；不填时用 `用户-端口` |
| `host` | 是 | 主机名或 IP |
| `port` | 否 | SSH 端口，默认 22 |
| `user` | 是 | SSH 用户 |
| `deploy_path` | 否 | 覆盖全局部署路径 |
| `key_path` | 否 | 覆盖全局私钥路径 |
| `tasks` | 否 | 该服务器私有的任务定义，字段级覆盖全局 `tasks` |

新增服务器示例：

```json
{
  "name": "demo-2201",
  "host": "demo.example.com",
  "port": 2201,
  "user": "demo"
}
```

再把它加入分组（`group` 段与 `servers` 平级）：

```json
"group": {
  "demo": ["demo-2201"]
}
```

## 分组管理

分组统一写在 `servers.json` **顶层**的 `group` 段，键是分组名、值是该组的服务器名称数组：

```json
"group": {
  "g1": ["X1", "X2"],
  "g2": ["X3", "X4"]
}
```

- 成员名必须能在 `servers` 里找到，写错会直接报错（不会静默忽略）；
- 一台服务器可以出现在多个分组里，`--group` 同时选多个分组时取并集；
- 分组名不能包含空白或逗号（逗号用于在列表里连接多个分组名）。

日常用 `group` 子命令维护即可，不必手改 JSON：

| 命令 | 作用 |
|------|------|
| `group list [--json]` | 列出所有分组、成员，以及未分组的服务器 |
| `group add GROUP [SERVER...]` | 新建分组，或把服务器追加进分组（重复的会跳过） |
| `group remove GROUP SERVER...` | 把服务器移出分组；分组保留，可能变成空分组 |
| `group set GROUP [SERVER...]` | 用给定服务器整体覆盖分组；不写服务器表示清空该分组 |
| `group delete GROUP...` | 删除分组（只删分组，不动 `servers` 里的服务器） |

```powershell
py deploy-tool\deploy.py group list
py deploy-tool\deploy.py group add g1 X1 X2
py deploy-tool\deploy.py group add g1 X3 --dry-run   # 只显示结果，不写文件
py deploy-tool\deploy.py group remove g1 X1
py deploy-tool\deploy.py group set g2 X3 X4
py deploy-tool\deploy.py group delete g2
```

写回行为：

- **原子写入**：先写临时文件、用正式解析器校验通过后 `os.replace` 覆盖 `servers.json`，不会留下半截文件；
- **格式化**：保持 2 空格缩进，`group` 段一行一个分组，其余字段顺序不变，diff 干净；
- `--group` 选择到的空分组会报「分组没有成员」，避免把“没选中任何机器”误当成成功。

> 兼容旧配置：早期版本把分组写在 `servers[].group`（一台机器一个分组）。这种写法仍能读，加载时会并入分组表并给出告警；执行任意会写文件的 `group` 子命令后，该字段会被迁移到顶层 `group` 段并删除。

## 排除规则

工具按以下顺序收集排除规则：

1. 内置规则：`.git`、`.svn`、`.hg`，以及部署工具目录自身；
2. [`deploy-tool/.deployignore`](.deployignore)；
3. 项目根目录下的 `.deployignore`（如果存在）；
4. 命令行 `--exclude`；
5. `servers.json` 中的 `exclude`。

规则语法：

- 一行一个模式，空行和 `#` 开头的行忽略；
- 不带 `/` 的模式匹配任意层级上的同名文件或目录，例如 `__pycache__`、`*.pyc`；
- 带 `/` 的模式从源目录根开始匹配，例如 `docs/build`；
- 结尾带 `/` 表示只匹配目录；
- 暂不支持 `!` 取反规则，遇到会直接报错，避免静默失效。

当前默认排除：IDE 配置、Python 缓存/虚拟环境、`node_modules`、日志和临时文件、部署工具自身等。**`.env` 不会被自动排除**，如果其中有密钥，请自行加入 `.deployignore`。

## 工作方式

1. 本地遍历源目录，应用排除规则；
2. 生成 `tar.gz`，文件权限优先从 git 索引读取；
3. 通过 `ssh` 把归档从标准输入传给远端；
4. 远端执行 `tar -xzf - -C /opt/CPQS` 解包；
5. 通过标准输出中的 `CPQS_DEPLOY_OK` 标记判断成功。

部署到单台时的大致过程：

```text
本地 tar.gz  --ssh stdin-->  远端: mkdir -p /opt/CPQS && tar -xzf - -C /opt/CPQS
```

### 覆盖语义

默认是 **覆盖式部署**：同名文件会被新文件替换，但远端已有、本次归档中没有的文件**不会删除**。

这样做是为了避免误删：

- 运行日志；
- 远端 `.env` 或本地配置；
- 虚拟环境、缓存；
- 用户上传的数据。

如果需要“目标目录与本地完全一致”，请先备份远端目录，再手动清理后部署。例如：

```bash
# 在远端执行，先确认路径无误
cp -a /opt/CPQS /opt/CPQS.bak.$(date +%Y%m%d-%H%M%S)
find /opt/CPQS -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
```

> 本工具不会自动执行上述删除操作。

## 后台任务：让远端常驻运行 `metrics_exporter.py`

除了把代码传过去，工具还可以在远端**后台拉起常驻进程**（脱离 SSH 会话，SSH 断开后继续运行），并结束、查看它们。

```bash
py deploy-tool\deploy.py start   --all                        # 启动所有已启用任务
py deploy-tool\deploy.py start   --target D1 --task collector # 只启动指定任务
py deploy-tool\deploy.py status  --all                        # 查看状态
py deploy-tool\deploy.py restart --target D1                  # 先停后起
py deploy-tool\deploy.py stop    --all                        # 结束任务
```

对多台服务器执行 `start` / `stop` / `restart` 时需要输入 `yes` 确认，非交互环境加 `--yes`。

### 任务定义

任务写在 [`servers.json`](servers.json) 的 `tasks` 段；不写则使用内置默认任务 `collector`：

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `command` | 是 | — | 启动命令，按空白切分成 `argv` 执行，**不支持管道/重定向等 shell 语法**（需要时写成 `sh -c "..."`） |
| `cwd` | 否 | 部署目录 | 工作目录；相对路径按该服务器的部署目录展开（`collector` → `/opt/CPQS/collector`） |
| `env` | 否 | `{}` | 启动前 `export` 的环境变量 |
| `setup` | 否 | 空 | 启动前执行一次的准备命令（完整 shell 语法），例如装依赖；失败则不启动 |
| `stop_timeout` | 否 | `10` | 停止时等待进程优雅退出的秒数，超时后 `SIGKILL` |
| `enabled` | 否 | `true` | 置 `false` 可停用该任务 |

当前默认任务：

```json
"tasks": {
  "collector": {
    "command": "/usr/bin/python3 metrics_exporter.py --port 9100",
    "cwd": "collector",
    "env": { "PYTHONUNBUFFERED": "1" },
    "stop_timeout": 10
  }
}
```

**Python 解释器一律写绝对路径 `/usr/bin/python3`**，不要用裸 `python3`：远端进程由 `nohup setsid sh -c` 在非交互 SSH 会话里拉起，PATH 可能与登录 shell 不同（conda / pyenv / 自编译 Python 会抢先命中），导致装依赖和跑脚本用的不是同一个解释器。`check` 会顺带探测 `/usr/bin/python3`，缺失时给出告警。

某个服务器可以私有化任务定义（在 `servers[]` 对象里写 `tasks`），字段级覆盖全局配置；写 `"enabled": false` 即可在该服务器上关掉某个任务：

```json
{
  "name": "D1",
  "host": "192.168.10.31",
  "user": "u",
  "tasks": { "collector": { "enabled": false } }
}
```

### 远端运行时文件

| 路径 | 内容 |
|------|------|
| `/opt/CPQS/.run/<任务名>.pid` | PID 文件 |
| `/opt/CPQS/logs/<任务名>.log` | 进程 stdout/stderr（追加写） |

这两个目录已写进 `.deployignore`，不会被打包发到其他服务器。

### 实现要点

- 启动用 `nohup setsid` 拉起，进程自成会话、脱离 SSH；远端没有 `setsid` 时退回 `nohup`；
- PID 由内层 shell 先写入再 `exec` 成目标进程，因此 PID 文件里就是真实进程 PID；
- 停止前会比对 `/proc/<pid>/cmdline`，避免 PID 被系统回收后误杀无关进程；
- 启动后等待 2 秒再探活，进程秒退会报「进程启动后立即退出」并附上日志尾部；
- `stop` 对没在运行的任务是幂等的，不会报错。

### 部署后重启

```bash
py deploy-tool\deploy.py deploy --all --restart-tasks
```

部署成功后自动在该服务器上重启全部已启用任务，让新代码立即生效。

## 安全与注意事项

- **私钥**：默认 `~/.ssh/id_rsa`。Linux/macOS 上权限必须是 `600`，否则工具会拒绝执行；Windows 由 OpenSSH 的 ACL 检查。
- **passphrase**：工具使用 `BatchMode=yes`，不会弹出密码输入。带 passphrase 的私钥请先 `ssh-add ~/.ssh/id_rsa`，或换用无 passphrase 的部署专用密钥。
- **主机密钥**：默认 `accept-new`，首次连接自动记录主机密钥，之后密钥变化会拒绝连接。生产环境建议首次 `check` 后改为 `yes`。
- **权限**：工具不会使用 `sudo`。如果 `/opt/CPQS` 属于其他用户或不可写，需要管理员提前创建并授权。
- **并发**：默认 4 台并发；如果网络不稳定或希望逐台观察，使用 `--jobs 1`。
- **敏感文件**：归档会把所有未被排除的文件上传到远端，提交前请确认 `.env`、证书、数据文件等不会被误传。
- **不删除**：重申默认不删除远端文件，避免误删运行数据。

## 常见问题

### `python` 找不到

Windows 上优先用：

```powershell
py deploy-tool\deploy.py list
```

或者使用 `.\deploy-tool\deploy.ps1 list`。

### `Permission denied (publickey)`

确认：

1. 私钥路径是否正确（`list` 会显示实际展开后的路径）；
2. 用户名和端口是否匹配；
3. 公钥是否已加入远端 `~/.ssh/authorized_keys`；
4. 私钥是否有 passphrase、是否已 `ssh-add`。

手工验证：

```powershell
ssh -i $HOME\.ssh\id_rsa -p 1011 xfusion@S501.site "id -un"
```

### `无法解析主机名` / `网络不可达`

先注意：`--target X1` 里的 `X1` 是 `servers.json` 中的 **name**，不是主机名。实际连接地址看 `list` 输出中的“主机”和“端口”两列。

如果报“无法解析主机名”或“网络不可达”，说明**执行部署的这台机器**没有可用的 DNS 或到目标网络的路由，不是密钥问题。可以在执行部署的机器上验证：

```bash
getent hosts S501.site
resolvectl query S501.site          # systemd-resolved 环境
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/101.34.209.104/1011' && echo TCP_OK || echo TCP_FAIL
```

- `getent` 失败但 TCP 能通：配置 DNS，或临时加 hosts：
  ```bash
  echo '101.34.209.104 S501.site' | sudo tee -a /etc/hosts
  ```
- `getent` 和 TCP 都失败：需要先接 VPN、配置到目标网段的路由，或换一台能访问 `S501.site` 的机器执行。**只加 `/etc/hosts` 不能解决 TCP 不可达。**

本项目当前验证过的可用执行环境是 Windows 主机（`D:\Project\CPQS`）；如果 `dell` 上的项目副本才是最新代码，不能简单改用 Windows 部署，否则会少部署 `dell` 上多出来的文件。需要先让 `dell` 能访问目标网络，或把 `dell` 的项目目录同步到 Windows 后再部署。

### 主机密钥校验失败

目标主机重装或密钥变化时会报 `Host key verification failed`。确认主机可信后，删除 `~/.ssh/known_hosts` 中对应条目，再重新 `check`。

### `/opt/CPQS` 不可写

`check` 会明确提示“目标目录不可写”或“父目录不可写”。需要管理员执行类似：

```bash
sudo mkdir -p /opt/CPQS
sudo chown -R <部署用户>:<部署用户> /opt/CPQS
```

### 远端没有 `tar`

`check` 会提示“远端缺少 tar 命令”。需要安装 `tar`（绝大多数 Linux 已自带）。

### 远端没有 `/usr/bin/python3`

任务命令固定用 `/usr/bin/python3`。`check` 会告警“远端缺少可执行的 /usr/bin/python3”，此时 `start` 会报 `start_failed` 并回显日志尾部。安装系统 Python 3：

```bash
sudo apt install -y python3        # Debian/Ubuntu
sudo yum install -y python3        # RHEL/CentOS
ls -l /usr/bin/python3             # 确认存在且可执行
```

如果该机器的系统 Python 确实装在别处，只改这台服务器的 `tasks.collector.command` 即可，不必改 `deploy.py`。

### 部署到多台时卡在确认

多台部署需要输入 `yes`。CI 或脚本中请加：

```powershell
py deploy-tool\deploy.py deploy --all --yes
```

### 想保留本次归档排错

```powershell
py deploy-tool\deploy.py deploy --target X1 --dry-run --keep-archive .\deploy-tool\last.tar.gz
```

## 验证

离线单元测试（不连接服务器）：

```powershell
py -m unittest discover -s deploy-tool\tests -v
```

真实环境只读检查：

```powershell
py deploy-tool\deploy.py check --all
```

## 移动或复制工具

`deploy.py`、`servers.json`、`.deployignore` 应放在同一目录。`source` 默认是配置文件上一级目录，因此整个 `deploy-tool` 目录可以直接放在项目根目录下；如果移到其他位置，请把 `servers.json` 中的 `source` 改成项目绝对路径。

---

维护者：CPQS 项目组 · 版本 1.0.0
