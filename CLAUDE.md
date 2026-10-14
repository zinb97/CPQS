# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库总览

仓库由四个相对独立的子系统组成，**代码上互不 import**；但 `backen` 通过 Prometheus 间接消费另外两者的指标，`netSim` 产生的拓扑由 `QoSController` 控制。

| 目录 | 作用 | 关键技术 |
|------|------|----------|
| `collector/` | Linux 主机 + GPU 指标采集，HTTP 暴露为 Prometheus 抓取端点 | `pynvml`（硬依赖）+ 标准库（`http.server`/`socketserver`） |
| `QoSController/` | **git 子模块**（上游 `https://github.com/zinb97/QoSController.git`）。RYU SDN 控制器 + Mininet 拓扑 + Web 可视化 + QoS/切片/组播 | RYU / OpenFlow 1.3 / gRPC / Mininet |
| `backen/` | FastAPI Dashboard，从 Prometheus 拉取指标并渲染 SSR + JSON | FastAPI / uvicorn / httpx / jinja2 / pyyaml |
| `netSim/` | Mininet 中国移动 CMNET 骨干网拓扑仿真（独立于子模块内的 `netSim.py`） | Mininet / Open vSwitch / RYU 控制器（外部） |

`QoSController/` 自带完整的 `CLAUDE.md`（含 REST 端点表、DSCP 策略、DPID 规范），**改动该子模块前先读它**。`backen/` 见 `backen/README.md`，`netSim/` 见 `netSim/README.md`。本文件其余部分描述跨子系统的数据流与本仓库特有的坑。

## 端到端数据流

```
主机的 node_* / DCGM_FI_DEV_* 指标          RYU 控制器的 qos_* 指标
collector/metrics_exporter.py :9100         QoSController/QoSController.py :8080
        │                                            │
        └──────────────┬─────────────────────────────┘
                       ▼
              Prometheus :9090
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
- `backen/app/routers/shells.py` 的 `/network` 指向 `http://192.168.10.31:8080/`，与 QoSController 的 `--wsapi-port 8080` 是**同一 host:port**（该端口同时是 RYU WS 端口和 Web 可视化端口）。backen 不做代理，控制器没起来这个页面就是空白 iframe。
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
```

- `collector/metrics_exporter.py` 通过 `from host_collector import ...`（裸导入）加载同目录模块，**不能用 `python -m collector.metrics_exporter` 启动**，工作目录必须为 `collector/`。
- `netSim/cmnet.py` 同理，是脚本而非包（无 `__init__.py`）。
- 多终端联调：终端 A 跑控制器，终端 B 跑 `sudo python3 cmnet.py --controller-ip 127.0.0.1`。

## 根目录的杂项（不要修改或引用）

- `main.py` — PyCharm 自动生成的 `print_hi` 样板，**不是**任何入口
- `references/index.html` — 旧版独立 Dashboard（57 KB），已被 `backen/` 取代，仅作历史参考
- `QoSController/` — git 子模块（见上表）。**不要在这里新增文件或直接改子模块内容**，改动应在上游仓库做；`git status` 会显示 `QoSController` 为 modified 属正常
- `.claude/` — Claude Code 配置（`settings.json` 强制全程简体中文回复），已 `.gitignore`
- `.idea/` — PyCharm 项目配置（已 `.gitignore`）

## collector/ 架构要点

- `host_collector.py` — `HostCollector` 类。`_read_*` 方法各自读取一个 procfs/sysfs 数据源；`_collect_all()` 一次性调用它们，结果存 `self._data`。输出两种格式：`to_json()` / `to_prometheus(per_cpu=True)`。
- `gpu_collector.py` — `GpuCollector` 类，通过 `pynvml` 调用 NVML，覆盖 `DCGM_FI_DEV_*` 指标。`to_prometheus(**_kwargs)` 忽略多余 kwargs，便于上层统一调用。**pynvml 为硬依赖**，无降级路径。
- `metrics_exporter.py` — `MetricsHandler` (`BaseHTTPRequestHandler`) 持有 `collectors` 列表，每次抓取对每个采集器调用 `to_prometheus(per_cpu=...)` 并拼接。`MetricsExporter` 默认挂载 `HostCollector + GpuCollector`。

**Prometheus 输出约定**（影响 PromQL 编写）：
- `node_cpu_seconds_total{mode=...}` 在 `_read_cpu_stat` 中已 `/100`，直接是秒
- 磁盘 `sectors_read`/`sectors_written` 在 `to_prometheus()` 中 `×512` 转字节
- 磁盘 `io_time_ms` `/1000` 转秒
- 计数器无 `# HELP`/`# TYPE` 头（Prometheus 自动推断）
- `node_uname_info` / `node_os_info` / `node_dmi_info` 标签值 **未做引号转义**，若值含 `"` 会破坏解析

数据源列表（/etc/os-release, /sys/class/dmi/id/*, /proc/stat, /proc/loadavg, /proc/meminfo, /proc/net/{snmp,dev,sockstat,netstat}, /proc/diskstats, /proc/mounts, /sys/class/thermal/thermal_zone*）和过滤规则（跳过 `loop*`, `lo`, `br*`, `docker*`, `veth*`, `b.*`）详见 `host_collector.py` 的 `_collect_all()` 与各 `_read_*` 方法。

`cluster` / `instance` 标签**由 Prometheus scrape config 注入**，collector 自己不上报。缺这两个标签会导致 `by(cluster)` 聚合返回空。

## backen/ 架构要点

- 路由分层：
  - `routers/dashboard.py` — `GET /`
  - `routers/resource.py` — 资源管控中心**原生页面**（`/resource` 重定向到 `/resource/clusters`，另有 cluster/host 详情页与 `/resource/iframe`）
  - `routers/shells.py` — **4 个** iframe 壳：`/network`、`/task`、`/telemetry`、`/compute`（`/resource` 已不在此列）
  - `routers/api.py` — **9 个** JSON 端点：`overview`、`bandwidth`、`map`、`links`、`events`、以及资源管控用的 `clusters`、`clusters/{cluster_id}`、`hosts/{instance:path}/timeseries`、`hosts/{instance:path}`
- PromQL 集中在 `app/prometheus/queries.py`：9 个 Dashboard 卡片常量（`Q_*`）+ 资源管控的模板函数（`q_cluster_*` / `q_node_*`，按 `cluster` 或 `instance` 参数化）+ GPU 段（`DCGM_FI_DEV_*`）+ `load_cluster_locations()` 加载 `config/cluster_locations.yaml`
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
```

端到端验证只能靠实际起服务：起 collector → Prometheus 里查 `up` → 起 backen 看 `/api/metrics/overview` 是否非空。

## 环境变量（backen）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PROMETHEUS_URL` | `http://192.168.10.31:9090` | Prometheus HTTP API |
| `CLUSTER_META_PATH` | `config/cluster_locations.yaml` | 集群经纬度元数据（相对 `backen/`） |
| `REQUEST_TIMEOUT_S` | `3.0` | 单次 Prometheus 查询超时 |
