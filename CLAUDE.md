# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库总览

仓库由五个相对独立的子系统组成，**代码上互不 import**；但 `backen` 通过 Prometheus 间接消费另外两者的指标，`netSim` 产生的拓扑由 `QoSController` 控制，`deploy-tool` 负责把整个仓库分发到多台服务器。

| 目录 | 作用 | 关键技术 |
|------|------|----------|
| `collector/` | Linux 主机 + GPU 指标采集，HTTP 暴露为 Prometheus 抓取端点 | `pynvml`（硬依赖）+ 标准库（`http.server`/`socketserver`） |
| `QoSController/` | **git 子模块**（上游 `https://github.com/zinb97/QoSController.git`）。RYU SDN 控制器 + Mininet 拓扑 + Web 可视化 + QoS/切片/组播 | RYU / OpenFlow 1.3 / gRPC / Mininet |
| `backen/` | FastAPI Dashboard，从 Prometheus 拉取指标并渲染 SSR + JSON | FastAPI / uvicorn / httpx / jinja2 / pyyaml |
| `netSim/` | Mininet 中国移动 CMNET 骨干网拓扑仿真（独立于子模块内的 `netSim.py`） | Mininet / Open vSwitch / RYU 控制器（外部） |
| `deploy-tool/` | 标准库实现的 SSH 多机部署工具（tar.gz 流水线），把仓库推到 `/opt/CPQS` | 仅 Python 标准库 + 系统 `ssh` 客户端 |

`QoSController/` 自带完整的 `CLAUDE.md`（含 REST 端点表、DSCP 策略、DPID 规范），**改动该子模块前先读它**。`backen/` 见 `backen/README.md`，`netSim/` 见 `netSim/README.md`，`deploy-tool/` 见 `deploy-tool/README.md`。本文件其余部分描述跨子系统的数据流与本仓库特有的坑。

## 端到端数据流

```
主机的 node_* / DCGM_FI_DEV_* 指标          RYU 控制器的 qos_* 指标
collector/metrics_exporter.py :9100         QoSController/QoSController.py :8080
        │                                            │
        └──────────────┬─────────────────────────────┘
                       ▼
              Prometheus :9090  (prometheus/prometheus.yml)
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
backen/app/prometheus/client.py    backen/routers/shells.py
(PromQL 查 node_* / qos_*)          /network → iframe 到 192.168.10.31:8080
        ▼                          （即 QoSController 的 vis-network Web UI）
backen FastAPI :8080 (SSR + /api/metrics/*)
```

**backen 不直连 collector / QoSController，必须经由 Prometheus 中转。** 修改任一端时不要绕过这一约束。

关键耦合点（改动时务必同步检查）：

- `backen/app/prometheus/queries.py` 中的 `Q_SWITCHES`/`Q_LINKS`/`Q_HOSTS`/`Q_PACKET_IN`/`Q_LATENCY_RANGE` 是 **`qos_*` 指标，由 QoSController 的 `/metrics` 提供**。改名会静默返回空值（不报错，前端只显示空状态）。
- **端口映射下的主机标识**：所有服务器共用同一批 IP，Prometheus `instance` 标签形如 `101.34.209.104:9111`，**只有端口能区分机器**。约定：界面主标识用**主机名**（`node_uname_info{nodename=...}`，由 `_node_hostnames()` 解析），`ip:port` 只作副标识；URL、路由、PromQL 一律用完整的 `ip:port`，只传裸 IP 有歧义（后端兜底会挑在线的那台，见 `_match_instance_by_ip`）。前端改 URL 时**不要剥掉端口**，否则会跳到同 IP 的另一台机器。
- `backen/app/routers/shells.py` 的 `/network` 指向 `http://192.168.10.31:8080/`，与 QoSController 的 `--wsapi-port 8080` 是**同一 host:port**（该端口同时是 RYU WS 端口和 Web 可视化端口）。backen 不做代理，控制器没起来这个页面就是空白 iframe。
- `prometheus/prometheus.yml` 里的 `scrape_configs` 决定了 `cluster` / `location` / `job` 标签的来源，**这些标签由 Prometheus 注入，collector 与控制器自己都不上报**。缺 `cluster` 标签会导致 `by(cluster)` 聚合（资源管控中心整套 `q_cluster_*` PromQL）返回空。
- 指标名分裂是有意为之：主机/GPU 用 `node_exporter` / DCGM 标准命名，网络侧用 `qos_` 前缀自研命名。

## 启动方式

```bash
# collector（必须 cd 到 collector/ 内，因使用裸导入）
cd collector && pip install -r requirements.txt   # nvidia-ml-py，硬依赖
python metrics_exporter.py --port 9100

# QoSController（在子模块目录内；需 Linux + RYU）
cd QoSController
ryu-manager QoSController.py --wsapi-port 8080 --observe-links     # 控制器 + Web UI + /metrics
ryu-manager QoSController.py ofctl_rest.py --wsapi-port 8080 --observe-links   # 额外加载流表 REST API

# backen
cd backen && pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# netSim（sudo 必需：创建 netns + ovs-vsctl）
cd netSim && sudo python3 cmnet.py --scale medium

# deploy-tool（在项目根目录执行）
python3 deploy-tool/deploy.py list
python3 deploy-tool/deploy.py check --all
python3 deploy-tool/deploy.py deploy --target D1 --dry-run
```

- `collector/metrics_exporter.py` 通过 `from host_collector import ...`（裸导入）加载同目录模块，**不能用 `python -m collector.metrics_exporter` 启动**，工作目录必须为 `collector/`。
- `netSim/cmnet.py` 同理，是脚本而非包（无 `__init__.py`）。
- 多终端联调：终端 A 跑控制器，终端 B 跑 `sudo python3 cmnet.py --controller-ip 127.0.0.1`。
- `backen` 也能 `python -m app.main`（自带 `uvicorn.run(reload=True)`）。

## 根目录的杂项（不要修改或引用）

- `main.py` — PyCharm 自动生成的 `print_hi` 样板，**不是**任何入口
- `config.js` — 空文件（0 字节），无内容
- `references/*.html` — 旧版独立 Dashboard（`大屏.html` / `集群管理平台.html`），已被 `backen/` 取代，仅作历史参考
- `doc/` — 科研文档（研究目标、测试大纲 + `images/`），非代码，改动前先确认是否属于交付物
- `QoSController/` — git 子模块（见上表）。**不要在这里新增文件或直接改子模块内容**，改动应在上游仓库做；`git status` 会显示 `QoSController` 为 modified 属正常。**注意本 checkout 中子模块尚未 `git submodule update --init`，目录是空的**，需要上游文件时先初始化
- `.claude/` — Claude Code 配置（`settings.json` 强制全程简体中文回复），已 `.gitignore`
- `.idea/` — PyCharm 项目配置（已 `.gitignore`）

## collector/ 架构要点

- `host_collector.py` — `HostCollector` 类。`_read_*` 方法各自读取一个 procfs/sysfs 数据源；`_collect_all()` 一次性调用它们，结果存 `self._data`。输出两种格式：`to_json()` / `to_prometheus()`。
- `gpu_collector.py` — `GpuCollector` 类，通过 `pynvml` 调用 NVML，覆盖 `DCGM_FI_DEV_*` 指标。`to_prometheus(**_kwargs)` 忽略多余 kwargs，便于上层统一调用。**pynvml 为硬依赖**，无降级路径。
- `metrics_exporter.py` — `MetricsHandler` (`BaseHTTPRequestHandler`) 持有 `collectors` 列表，每次抓取对每个采集器调用 `to_prometheus()` 并拼接。`MetricsExporter` 默认挂载 `HostCollector + GpuCollector`。用 `ThreadingHTTPServer`（非 `HTTPServer`）：单线程版会被一个慢连接阻塞所有抓取。

**Prometheus 输出约定**（影响 PromQL 编写）：
- `node_cpu_seconds_total{mode=...}` 在 `_read_cpu_stat` 中已 `/100`，直接是秒
- 磁盘 `sectors_read`/`sectors_written` 在 `to_prometheus()` 中 `×512` 转字节
- 磁盘 `io_time_ms` `/1000` 转秒
- 计数器无 `# HELP`/`# TYPE` 头（Prometheus 自动推断）
- `node_uname_info` / `node_os_info` / `node_dmi_info` 的标签值**已做引号/反斜杠转义**（`_escape_label_value()`）

数据源列表（/etc/os-release, /sys/class/dmi/id/*, /proc/stat, /proc/loadavg, /proc/meminfo, /proc/net/{snmp,dev,sockstat,netstat}, /proc/diskstats, /proc/mounts, /sys/class/thermal/thermal_zone*）和过滤规则（跳过 `loop*`, `lo`, `br*`, `docker*`, `veth*`, `b.*`）详见 `host_collector.py` 的 `_collect_all()` 与各 `_read_*` 方法。

## backen/ 架构要点

- 路由分层：
  - `routers/dashboard.py` — `GET /`
  - `routers/resource.py` — 资源管控中心**原生页面**（`/resource` 重定向到 `/resource/clusters`，另有 cluster/host 详情页）
  - `routers/shells.py` — **4 个** iframe 壳：`/network`、`/task`、`/telemetry`、`/compute`（`/resource` 已不在此列）
  - `routers/api.py` — **10 个** JSON 端点（prefix `/api/metrics`）：`overview`、`bandwidth`、`map`、`links`、**`qos`**、`events`、以及资源管控用的 `clusters`、`clusters/{cluster_id}`、`hosts/{instance:path}/timeseries`、`hosts/{instance:path}`
- `GET /api/metrics/qos` 返回**网络 QoS 六维评分**（时延/抖动/丢包/可用性/带宽余量/抗扰动），每个维度带 `measured` 标志区分实测值与占位值。抖动无对应指标，用链路时延标准差代理；带宽余量以 `Settings.network_rated_mbps` 为基准（控制器只上报占用、不含链路额定速率）
- **抗扰动维度（`key=robustness`）是「代理指标」**，不是真正的攻击检测：用 collector 上报的 TCP 异常计数器速率算分——`node_snmp_Tcp_AttemptFails`（连接失败）/ `node_snmp_Tcp_{Active,Passive}Opens`（新建连接）得失败率，`node_netstat_TcpExt_ListenDrops`（监听队列溢出=洪泛直证），`node_netstat_IpExt_InNoRoutes`（无路由报文）。三项按 0.6/0.25/0.15 加权，失败率缺失则整个维度不给分。`node_netstat_TcpExt_TCPSynRetrans` 只在 detail 里作旁证、不计分（它是链路丢包表征，已由「丢包率」维度覆盖）。原「安全性」维度无直接指标，已于 2026-10 改名为「抗扰动」以免被误读为安全态势
- PromQL 集中在 `app/prometheus/queries.py`：Dashboard 卡片常量（`Q_*`）+ 资源管控的模板函数（`q_cluster_*` / `q_node_*`，按 `cluster` 或 `instance` 参数化）+ GPU 段（`DCGM_FI_DEV_*`，含 FLOPs 推算 `q_*_flops_peak`）+ `load_cluster_locations()` 加载 `config/cluster_locations.yaml`
- `PrometheusClient` (`app/prometheus/client.py`) 异步封装 `/api/v1/{query,query_range,targets,labels}`；**所有异常路径返回安全空值**（让调用端走空状态展示而非抛错）
- 生命周期：`main.py` 的 `lifespan` 启动时构造 `PrometheusClient` 与 `cluster_meta`，关闭时 `aclose()`
- 配置：`app/config.py` 的 frozen dataclass `Settings`，`from_env()` 读取环境变量
- `cache_response()` **只设 `Cache-Control` 响应头**（`cards_cache_s=4` / `charts_cache_s=28`），**不是服务端缓存**——没有内存存储、没有 TTL、没有 single-flight。每次轮询都真实打到 Prometheus，别把它当性能优化手段
- 前端是原生 JS + ECharts（CDN），**无构建步骤**；SSR 首屏填充初始快照后靠轮询 5s / 30s

**大屏排障要点**：PromQL 写错是**静默失败**。聚合函数必须小写（`sum()` 而非 `SUM()`，大写会被 Prometheus 判解析错误返回 422）；指标名写错则返回空向量。两种情况都会被 `raise_for_status()` + `asyncio.gather(return_exceptions=True)` 吞掉，表现为指标卡显示 "—" 且**无任何报错**。改 PromQL 后若卡片仍空，先确认指标在 QoSController/collector 的 `/metrics` 中真实存在。

## netSim/ 架构要点

- `cmnet.py` 是 CMNetTopo（继承 `mininet.topo.Topo`）+ `run_cmnet()` 启动脚本，**不是 Python 包**
- 拓扑 4 级：L1 超级核心 (3 节点全互联) → L2 大区中心 (5 节点 pentagon 环) → L3 省网核心 (5/10/23/31 省按 `--scale`) → L4 城域接入 (树形，depth × width 由 `--prov-depth`/`--prov-width` 控制)
- DPID 统一 16 hex（`00000000{L:02x}{idx:06x}`），由 `_dpid(level, idx)` 工具函数生成；idx 编码见该函数 docstring
- 历史上为适配 Mininet 2.3+/3.x 修过 4 类致命问题（`pairs` 用错 region code、`addSwitch()` 返回值类型、Metro 层 DPID 只 14 hex、`province_to_parent` 缺 'bj'/'sh'）。详见 `netSim/README.md` 的「2026-10 重构修复记录」——改拓扑构造逻辑前先读，避免重新踩坑

## deploy-tool/ 架构要点

- `deploy.py`（约 1900 行）**只依赖 Python 标准库 + 系统 `ssh` 客户端**，不用 `rsync`/`scp`/pip 包
- 七个子命令：`list`（看服务器）、`check`（只读预检：SSH 登录、目标目录可写、远端 `tar`）、`deploy`（打包并上传）、`start`/`stop`/`restart`/`status`（远端后台任务管理）。服务器清单、任务定义与全局默认在 `servers.json`
- **远端后台任务**：`start` 用 `nohup setsid` 在远端拉起常驻进程（如 `collector/metrics_exporter.py`），PID 写 `/opt/CPQS/.run/<任务>.pid`，日志写 `/opt/CPQS/logs/<任务>.log`（两个目录已在 `.deployignore` 里排除，不会外发）。`stop` 停止前比对 `/proc/<pid>/cmdline` 防 PID 复用误杀；进程秒退会被检出并回报日志尾部。任务定义在 `servers.json` 的 `tasks` 段（内置默认 `collector`），支持每台服务器私有化覆盖（`enabled:false` 可关停）。`deploy --restart-tasks` 可在部署后自动重启任务
- **`--target` 填的是 `servers.json` 里的 `name`**，当前为 `X1`–`X7`（xfusion 组）、`L1`–`L4`（hello 组）、`D1`（lab 组 `192.168.10.31`）。以 `deploy.py list` 的输出为准
- 流水线：本地遍历源目录（应用排除规则）→ 生成 `tar.gz`（文件权限优先从 git 索引读取）→ 经 `ssh` 把归档从 stdin 传给远端 → 远端 `tar -xzf - -C /opt/CPQS` → 以 stdout 中的 `CPQS_DEPLOY_OK` 标记判定成功
- **默认覆盖式部署**：同名文件被替换，但远端已有、本次归档中没有的文件**不会删除**（保护日志、`.env`、虚拟环境）。需要完全一致时由人工备份后清理
- 排除规则来源顺序：内置（`.git`/`.svn`/`.hg` + 部署工具自身）→ `deploy-tool/.deployignore` → 根目录 `.deployignore` → `--exclude` → `servers.json` 的 `exclude`。语法不支持 `!` 取反（遇到直接报错而非静默失效）
- 私钥默认 `~/.ssh/id_rsa`，`BatchMode=yes` 不弹密码；带 passphrase 的私钥需先 `ssh-add`。主机密钥默认 `accept-new`
- 多台部署需交互输入 `yes`，非交互环境加 `--yes`；默认 4 并发
- README 里提到的 `deploy-tool/tests/test_deploy.py` **在本仓库中不存在**，离线单测命令暂时跑不通

## 验证

仓库**无测试、无 linter 配置、无 CI**。只能做语法检查和构造级 dryrun：

```bash
# collector（必须在 collector/ 目录下）
cd collector && python -m py_compile host_collector.py metrics_exporter.py gpu_collector.py

# backen
cd backen && python -m py_compile app/*.py app/*/*.py

# netSim（dryrun，无需 sudo）
python3 -c "
import importlib.util as u
s = u.spec_from_file_location('m','netSim/cmnet.py')
m = u.module_from_spec(s); s.loader.exec_module(m)
t = m.CMNetTopo(scale='medium', prov_depth=2, prov_width=2)
print('sw=', len(t.switches()), 'ln=', len(t.links()), 'ho=', len(t.hosts()))
"   # 期望 sw=58 ln=96 ho=33

# deploy-tool（语法检查 + 只读预检，不写远端）
python3 -m py_compile deploy-tool/deploy.py
python3 deploy-tool/deploy.py list
python3 deploy-tool/deploy.py status --all   # 只读：查远端后台任务状态
```

端到端验证只能靠实际起服务：起 collector → Prometheus 里查 `up` → 起 backen 看 `/api/metrics/overview` 是否非空。

## 环境变量（backen）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PROMETHEUS_URL` | `http://192.168.10.31:9090` | Prometheus HTTP API |
| `CLUSTER_META_PATH` | `config/cluster_locations.yaml` | 集群经纬度元数据（相对 `backen/`） |
| `REQUEST_TIMEOUT_S` | `3.0` | 单次 Prometheus 查询超时 |
| `NETWORK_RATED_MBPS` | `1000.0` | 网络额定带宽，QoS 雷达「带宽余量」维度的基准 |
