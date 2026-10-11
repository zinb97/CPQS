# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库总览

仓库由三个相对独立的子系统组成，**互不强依赖**（但 backen 通过 Prometheus 间接消费 collector 的指标）：

| 目录 | 作用 | 关键技术 |
|------|------|----------|
| `collector/` | Linux 主机 + GPU 指标采集，HTTP 暴露为 Prometheus 抓取端点 | `pynvml`（硬依赖）+ 标准库（`http.server`/`socketserver`） |
| `backen/` | FastAPI Dashboard，从 Prometheus 拉取指标并渲染 SSR + JSON | FastAPI / uvicorn / httpx / jinja2 / pyyaml |
| `netSim/` | Mininet 中国移动 CMNET 骨干网拓扑仿真 | Mininet / Open vSwitch / RYU 控制器（外部） |

`backen/` 完整架构、API、环境变量与运行方式见 `backen/README.md`（**修改 backen 前先读它**）。`netSim/` 见 `netSim/README.md`（含 2026-10 重构修复记录）。本文件其余部分主要描述 `collector/` 与跨子系统的端到端数据流。

## 端到端数据流

```
主机 (Linux + GPU)
   └─ collector/metrics_exporter.py :9100/metrics
        └─ Prometheus (默认 192.168.10.31:9090)
             └─ backen/app/prometheus/client.py (PromQL 查询)
                  └─ backen FastAPI :8080 (SSR + /api/metrics/* JSON)
```

**`backen` 不直连 `collector`，必须经由 Prometheus 中转**。修改任一端时不要绕过这一约束。

## 启动方式

```bash
# collector（必须 cd 到 collector/ 内，因使用裸导入）
cd collector && python metrics_exporter.py --port 9100

# backen
cd backen && uvicorn app.main:app --host 0.0.0.0 --port 8080

# netSim（sudo 必需：创建 netns + ovs-vsctl）
cd netSim && sudo python3 cmnet.py --scale medium
```

`collector/metrics_exporter.py` 通过 `from host_collector import ...`（裸导入）加载同目录模块，因此 **不能用 `python -m collector.metrics_exporter` 形式启动**，且工作目录必须为 `collector/`。同理 `netSim/cmnet.py` 无 `__init__.py`，是脚本而非包。

## 根目录的杂项（不要修改或引用）

- `main.py` — PyCharm 自动生成的 `print_hi` 样板，**不是**任何入口
- `index.html` — 旧版独立 Dashboard（57 KB），已被 `backen/` 取代
- `controller/` — 空目录，是已被移除的 QoSController git 子模块的占位符；**勿添加文件**，避免与未来重新引入的子模块冲突
- `.idea/` — PyCharm 项目配置（已 `.gitignore`）

## collector/ 架构要点

- `host_collector.py` — `HostCollector` 类。`_read_*` 方法各自读取一个 procfs/sysfs 数据源；`_collect_all()` 一次性调用它们，结果存 `self._data`。输出两种格式：`to_json()` / `to_prometheus(per_cpu=True)`。
- `gpu_collector.py` — `GpuCollector` 类，通过 `pynvml` 调用 NVML，覆盖 `1.txt` 中列出的 18 个 `DCGM_FI_DEV_*` 指标。`to_prometheus(**_kwargs)` 忽略多余 kwargs，便于上层统一调用。**pynvml 为硬依赖**，无降级路径。
- `metrics_exporter.py` — `MetricsHandler` (`BaseHTTPRequestHandler`) 持有 `collectors` 列表，每次抓取对每个采集器调用 `to_prometheus(per_cpu=...)` 并拼接。`MetricsExporter` 默认挂载 `HostCollector + GpuCollector`。

**Prometheus 输出约定**（影响 PromQL 编写）：
- `node_cpu_seconds_total{mode=...}` 在 `_read_cpu_stat` 中已 `/100`，直接是秒
- 磁盘 `sectors_read`/`sectors_written` 在 `to_prometheus()` 中 `×512` 转字节
- 磁盘 `io_time_ms` `/1000` 转秒
- 计数器无 `# HELP`/`# TYPE` 头（Prometheus 自动推断）
- `node_uname_info` / `node_os_info` / `node_dmi_info` 标签值 **未做引号转义**，若值含 `"` 会破坏解析

`host_collector.py` 的数据源列表（/etc/os-release, /sys/class/dmi/id/*, /proc/stat, /proc/loadavg, /proc/meminfo, /proc/net/{snmp,dev,sockstat,netstat}, /proc/diskstats, /proc/mounts, /sys/class/thermal/thermal_zone*）和它们各自的过滤规则（跳过 `loop*`, `lo`, `br*`, `docker*`, `veth*`, `b.*`）详见该文件 `_collect_all()` 与各 `_read_*` 方法。

## backen/ 架构要点

- 路由分层：`routers/dashboard.py` (`GET /`)、`routers/shells.py`（5 个 iframe 壳）、`routers/resource.py`、`routers/api.py`（7 个 JSON 端点，TTL 缓存 4s/28s）
- PromQL 集中在 `app/prometheus/queries.py`（PromQL 常量 + `load_cluster_locations()` 加载 `config/cluster_locations.yaml`）
- `PrometheusClient` (`app/prometheus/client.py`) 异步封装 `/api/v1/{query,query_range,targets,labels}`；所有异常路径返回安全空值（让调用端走空状态展示而非抛错）
- 生命周期：`main.py` 的 `lifespan` 启动时构造 `PrometheusClient` 与 `cluster_meta`，关闭时 `aclose()`
- 缓存：`/api/metrics/overview` 4s，其他端点 28s（前端 5s/30s 轮询对齐）

## netSim/ 架构要点

- `cmnet.py` 是 CMNetTopo（继承 `mininet.topo.Topo`）+ `run_cmnet()` 启动脚本，**不是 Python 包**
- 拓扑 4 级：L1 超级核心 (3 节点全互联) → L2 大区中心 (5 节点 pentagon 环) → L3 省网核心 (5/10/23/31 省按 `--scale`) → L4 城域接入 (树形，depth × width 由 `--prov-depth`/`--prov-width` 控制)
- DPID 统一 16 hex（`00000000{L:02x}{idx:06x}`），由 `_dpid(level, idx)` 工具函数生成；idx 编码见该函数 docstring
- 与外部 RYU 控制器配合：终端 A 跑控制器 `--wsapi-port 8080 --observe-links`，终端 B `sudo python3 cmnet.py --controller-ip 127.0.0.1`

## 验证

仓库**无测试、无 linter 配置**。常用语法检查：

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
"
```

## 环境变量（backen）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PROMETHEUS_URL` | `http://192.168.10.31:9090` | Prometheus HTTP API |
| `CLUSTER_META_PATH` | `config/cluster_locations.yaml` | 集群经纬度元数据（相对 backen/） |
| `REQUEST_TIMEOUT_S` | `3.0` | 单次 Prometheus 查询超时 |