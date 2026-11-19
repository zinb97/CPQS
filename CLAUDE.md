# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库总览

仓库由五个相对独立的子系统组成，**代码上互不 import**；但 `backen` 通过 Prometheus 间接消费另外两者的指标，`netSim` 产生的拓扑由 `QoSController` 控制，`deploy-tool` 负责把整个仓库分发到多台服务器。

| 目录 | 作用 | 关键技术 |
|------|------|----------|
| `collector/` | Linux 主机 + GPU 指标采集，HTTP 暴露为 Prometheus 抓取端点 | `pynvml`（**可选**依赖，缺失时仅 GPU 指标为空）+ 标准库（`http.server`/`socketserver`） |
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
(PromQL 查 node_* / qos_*)          /network → iframe 到 192.168.10.31:8080（地址见 Settings）
        ▼                          （即 QoSController 的 vis-network Web UI）
backen FastAPI :8080 (SSR + /api/metrics/*)
```

**backen 不直连 collector / QoSController，必须经由 Prometheus 中转。** 修改任一端时不要绕过这一约束。

关键耦合点（改动时务必同步检查）：

- `backen/app/prometheus/queries.py` 中的 `Q_SWITCHES`/`Q_LINKS`/`Q_HOSTS`/`Q_PACKET_IN`/`Q_LATENCY_RANGE` 是 **`qos_*` 指标，由 QoSController 的 `/metrics` 提供**。改名会静默返回空值（不报错，前端只显示空状态）。
- **`qos_link_*` 的「实测」与「配置」之分**（改链路采集方式时务必同步 backen 侧）：

  | 指标 | 性质 | 来源 |
  |------|------|------|
  | `qos_link_bandwidth_usage_bits` | **实测** | OpenFlow 端口统计字节差分，**单向 src→dst** 口径；对全部有向边求和 = 每条物理链路的双向总流量（早期版本取 rx+tx 且双向边各报一条，求和时重复计了两倍） |
  | `qos_link_capacity_bits` | **实测** | OVS Interface 表的 `link_speed`（网卡驱动上报的协商速率）；Mininet 的 veth 报 10 Gbps。读不到时为 0，backen 退回 `Settings.network_rated_mbps` 兜底 |
  | `qos_link_loss_percent` | **实测** | `tc -s qdisc show` 的累计计数差分：`Δdropped / (Δsent + Δdropped)`——内核 qdisc 丢包只累加 drops、**不计入 Sent**，所以分母要加上 dropped |
  | `qos_link_loss_configured_percent` | **配置** | netem 的 loss 参数，仅作对照（可验证损伤是否真的生效） |
  | `qos_link_delay_ms` | **配置** | netem 的 delay 参数。被动采集拿不到 RTT。**实测时延由 `backen` 的 `GET /health` 页面在浏览器侧主动探测给出**（浏览器 → 各采集器）；QoS 雷达里的「时延」维度仍评的是这个配置值 |

  推论：`qos_link_delay_ms` 恒定不变时，QoS 雷达的「时延」「抖动」两个维度**永远是满分**——它们现在评的是配置值，不是实测值。别当实测结果读。
- **端口映射下的主机标识**：所有服务器共用同一批 IP，Prometheus `instance` 标签形如 `101.34.209.104:9111`，**只有端口能区分机器**。约定：界面主标识用**主机名**（`node_uname_info{nodename=...}`，由 `_node_hostnames()` 解析），`ip:port` 只作副标识；URL、路由、PromQL 一律用完整的 `ip:port`，只传裸 IP 有歧义（后端兜底会挑在线的那台，见 `_match_instance_by_ip`）。前端改 URL 时**不要剥掉端口**，否则会跳到同 IP 的另一台机器。
- `backen/app/routers/shells.py` 的 `/network` 默认指向 `http://192.168.10.31:8080/`，与 QoSController 的 `--wsapi-port 8080` 是**同一 host:port**（该端口同时是 RYU WS 端口和 Web 可视化端口）。backen 不做代理，控制器没起来这个页面就是空白 iframe。四个壳的地址**都不写死在代码里**，而是放在 `Settings`（`SHELL_NETWORK_URL` / `SHELL_TASK_URL` / `SHELL_TELEMETRY_URL` / `SHELL_COMPUTE_URL`），请求时从 `app.state.settings` 取，改环境变量即生效。默认的 `192.168.10.31` 是**内网直连**地址，其余三个走 NAT 入口 `101.34.209.104`。**实测 `101.34.209.104:8080` 没有 frp 映射（连接不通）**，控制器的 8080 目前只有内网可达；外网访问 backen 时 `/network` 的 iframe 会空白，需先加 frpc 映射再把 `SHELL_NETWORK_URL` 指过去
- `prometheus/prometheus.yml` 里的 `scrape_configs` 决定了 `cluster` / `location` / `job` 标签的来源，**这些标签由 Prometheus 注入，collector 与控制器自己都不上报**。缺 `cluster` 标签会导致 `by(cluster)` 聚合（资源管控中心整套 `q_cluster_*` PromQL）返回空。
- 指标名分裂是有意为之：主机/GPU 用 `node_exporter` / DCGM 标准命名，网络侧用 `qos_` 前缀自研命名。

## 启动方式

```bash
# collector（必须 cd 到 collector/ 内，因使用裸导入）
cd collector && pip install -r requirements.txt   # nvidia-ml-py，仅 GPU 采集需要；未装则只有 DCGM 指标缺失
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
- `QoSController/` — git 子模块（见上表）。**允许直接修改**（2026-11 起取消「改动应在上游仓库做」的限制）：控制器侧的指标口径、采集方式需要与 backen 同步改，分两处维护成本高于收益。改动后 `git status` 会显示 `QoSController` 为 modified 属正常；需要同步到上游时在子模块目录内单独提交。**本 checkout 中子模块已初始化**（`git submodule status` 有输出，目录内含 `QoSController.py` / `ofctl_rest.py` / `controllers/` 等文件），需要上游文件时可直接读；若显示 `-`（未初始化）才需 `git submodule update --init`
  - **控制器跑在 Python 3.8**（`/usr/bin/python3.8 /usr/local/bin/ryu-manager`，不是系统默认的 3.10）。3.8 不支持 `float | None` 语法，写类型标注要用 `Optional[float]`；`ryu`/`eventlet` 在 3.10 下也装不起来（`collections.MutableMapping` 已移除），**改控制器代码必须用 3.8 验证**
- `.claude/` — Claude Code 配置（`settings.json` 强制全程简体中文回复），已 `.gitignore`
- `.idea/` — PyCharm 项目配置（已 `.gitignore`）

## collector/ 架构要点

- `host_collector.py` — `HostCollector` 类。分三层：`_read_*` 各读一个 procfs/sysfs 数据源 → `_emit_*` 翻译成指标文本 → `collect()`/`to_json()`/`to_prometheus()`。**指标名与内部字段的映射写成常量表**（`_MEMORY_METRICS`/`_DISK_*_METRICS`/`_NET_FIELDS` 等），新增指标改表即可。
- `gpu_collector.py` — `GpuCollector` 类，通过 `pynvml` 调用 NVML，覆盖 `DCGM_FI_DEV_*` 指标。指标定义是一张 `(指标名, 取值器)` 的声明式表。`to_prometheus(**_kwargs)` 忽略多余 kwargs，便于上层统一调用。**pynvml 是可选依赖**：`import pynvml` 包在 try/except 里，失败时 `PYNVML_AVAILABLE=False`、`GpuCollector.available=False`，`to_prometheus()` 返回空串——主机侧 `node_*` 指标照常上报。exporter 启动时打一条 `[WARN]` 提示缺 GPU 指标（避免「这台机没 GPU」与「采集器装坏了」混淆）。
- `promtext.py` — Prometheus 文本曝光格式的写出工具。**标签值的转义只此一处**（`_escape_label_value`）：漏转义会让 Prometheus 拒收整次抓取、该 target 全部指标丢失。
- `metrics_exporter.py` — `MetricsHandler` (`BaseHTTPRequestHandler`) + `MetricsServer` (`ThreadingHTTPServer`)。采集器列表挂在**服务器实例**上（不是 handler 的类属性），避免可变类属性做全局状态。用多线程版（非 `HTTPServer`）：单线程版会被一个慢连接阻塞所有抓取。**采集器实例被所有 HTTP 线程共享**，故 `HostCollector`/`GpuCollector` 都必须「局部组装、整体赋值」`self._data`——边采边改会让并发抓取读到半成品快照。
- **指标清单见 `collector/METRICS.md`**（采集了哪些内容、每个指标什么含义、口径与陷阱、backen 侧的 PromQL 对照）。改采集逻辑或新增指标后**必须同步更新该文件**；文档里提到的指标名要与 `/metrics` 实际输出严格一致（可用脚本比对）。
- 采集器的 `GET /health` 返回 `OK`，是 backen「浏览器侧时延测量」页的探测目标；该响应带 `Timing-Allow-Origin: *` + `Access-Control-Allow-Origin: *`（前者让浏览器跨源场景也能拿到 DNS/TCP握手/首字节分段耗时——没有它浏览器会把分段字段清零，页面只能给「总耗时」；后者让响应体可读——只回 `OK`，无敏感信息）。**`/metrics` 不带这两个头**。改了采集器要 `deploy.py deploy --all --restart-tasks` 才会生效

**Prometheus 输出约定**（影响 PromQL 编写）：
- `node_cpu_seconds_total{mode=...}` 在 `_read_cpu_stat` 中已 `/100`，直接是秒
- 磁盘 `sectors_read`/`sectors_written` 在 `to_prometheus()` 中 `×512` 转字节
- 磁盘 `io_time_ms` `/1000` 转秒
- 磁盘**只上报最上层块设备**（`_disk_skip_devices()`）：分区、RAID 成员盘、LVM/device-mapper 中间层全部过滤，否则同一份 IO 会被按层重复累加，`node_disk_*_bytes_total` 成倍虚高。判定靠 sysfs 的 `holders`/`slaves`/`partition` 层次关系，不猜设备名；`/sys` 不可见时**不过滤**（宁可少过滤也不丢指标）——**绝不能因为读不到 `/sys/block` 就让整次抓取失败**，否则该主机的全部指标都会消失
- 网卡错误/丢包计数（`node_network_{receive,transmit}_{errs,drop}_total`）与磁盘 discard/flush 计数（`node_disk_discard*` / `node_disk_flush*`）**只在内核提供对应列时才发射**——「不支持」与「计数为 0」是两回事
- 计数器无 `# HELP`/`# TYPE` 头（Prometheus 自动推断）
- `node_uname_info` / `node_os_info` / `node_dmi_info` 的标签值**已做引号/反斜杠转义**（`_escape_label_value()`）

数据源列表（/etc/os-release, /sys/class/dmi/id/*, /proc/stat, /proc/loadavg, /proc/meminfo, /proc/net/{snmp,dev,sockstat,netstat}, /proc/diskstats, /proc/mounts, /sys/class/thermal/thermal_zone*）和过滤规则（跳过 `loop*`, `lo`, `br*`, `docker*`, `veth*`, `b.*`，磁盘按 top-level 规则过滤）详见 `host_collector.py` 的 `_collect_all()` 与各 `_read_*` 方法。

## backen/ 架构要点

- 路由分层：
  - `routers/dashboard.py` — `GET /`
  - `routers/resource.py` — 资源管控中心**原生页面**（`/resource` 重定向到 `/resource/clusters`，另有 cluster/host 详情页）
  - `routers/shells.py` — **4 个** iframe 壳：`/network`、`/task`、`/telemetry`、`/compute`（`/resource` 已不在此列）
  - `routers/api.py` — **10 个** JSON 端点（prefix `/api/metrics`）：`overview`、`bandwidth`、`map`、`links`、**`qos`**、`events`、以及资源管控用的 `clusters`、`clusters/{cluster_id}`、`hosts/{instance:path}/timeseries`、`hosts/{instance:path}`
  - `routers/health.py` — `GET /health`（= `/health/browser`）：**浏览器侧时延测量页**（`templates/health.html` + `static/js/health.js` + `static/css/health.css`）。时延由**运行浏览器的设备**实测——服务器端测不到「客户端 → 其它服务器」这条路径（那是客户端的网络出口），只能由浏览器自己 `fetch(mode:'no-cors')` 探测采集器的 `GET /health`，每台采样 `HEALTH_PROBE_SAMPLES`(默认3) 次
    - **主指标 = TCP 握手 RTT**（`connectEnd - connectStart`），浏览器能拿到的最接近 ping 的量（不含 DNS/HTTP 往返/服务端处理）；另列 DNS、首字节(TTFB)、总耗时（用户感知）、采样波动。JS 不能建裸 TCP、不能发 ICMP，**没有任何一列等于纯 ICMP ping**
    - 分段数据依赖采集器的 `Timing-Allow-Origin` 头（跨源时浏览器会清零分段字段），未部署时页面只给「总耗时」+ 橙色提示条
    - 两层防假值：只在「起点>0 且终点>起点」时做差；且校验「各段之和 ≤ 该样本总耗时」，不满足则整组分段丢弃（否则会出现「DNS 613ms > 总耗时 505ms」这种浏览器清零字段造成的假值）
    - 另有 `GET /health/targets`（探测目标清单 JSON，页面自身用它取地址）。**没有服务器侧时延**——backen 不主动探测
- `GET /api/metrics/qos` 返回**网络 QoS 六维评分**（时延/抖动/丢包/可用性/带宽余量/抗扰动），每个维度带 `measured` 标志区分实测值与占位值。抖动无对应指标，用链路时延标准差代理（**注意时延本身是 netem 的配置值**，见上表）；带宽余量的额定值优先取 `qos_link_capacity_bits`（OVS link_speed 实测），拿不到才退回 `Settings.network_rated_mbps`；丢包率是控制器差分算出的**实测值**，`detail` 里同时给出 netem 的配置值作对照
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
- 九个子命令：`list`（看服务器）、`group`（查看/修改分组）、`check`（只读预检：SSH 登录、目标目录可写、远端 `tar`/`/usr/bin/python3`）、`deploy`（打包并上传）、`start`/`stop`/`restart`/`status`（远端后台任务管理）。服务器清单、任务定义与全局默认在 `servers.json`
- **远端后台任务**：`start` 用 `nohup setsid` 在远端拉起常驻进程（如 `collector/metrics_exporter.py`），PID 写 `/opt/CPQS/.run/<任务>.pid`，日志写 `/opt/CPQS/logs/<任务>.log`（两个目录已在 `.deployignore` 里排除，不会外发）。**所有服务器的 Python 任务一律用绝对路径 `/usr/bin/python3`**（非交互 SSH 的 PATH 可能命中 conda/pyenv；`check` 会探测该解释器并告警）。`stop` 停止前比对 `/proc/<pid>/cmdline` 防 PID 复用误杀；进程秒退会被检出并回报日志尾部。任务定义在 `servers.json` 的 `tasks` 段（内置默认 `collector`），支持每台服务器私有化覆盖（`enabled:false` 可关停）。`deploy --restart-tasks` 可在部署后自动重启任务
- **`--target` 填的是 `servers.json` 里的 `name`**，当前为 `X1`–`X5`（xfusion 组）、`L1`–`L4`（hello 组）、`D1`（lab 组 `192.168.10.31`）。以 `deploy.py list` 的输出为准
- **分组在 `servers.json` 顶层 `group` 段**（`{"g1": ["X1","X2"]}`），**不在 `servers[]` 里写 `group` 字段**；一台服务器可属于多个分组，`--group` 多选取并集。用 `deploy.py group list/add/remove/set/delete` 维护，写回是「临时文件 + 正式解析器校验 + `os.replace`」的原子操作。旧版 `servers[].group` 仍兼容读取（告警提示迁移），任一次 group 写操作会把它折叠进顶层 `group` 并删掉旧字段
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
| `SHELL_NETWORK_URL` | `http://192.168.10.31:8080/` | `/network` 壳的 iframe 地址（QoSController Web UI，**内网直连地址**） |
| `SHELL_TASK_URL` | `http://101.34.209.104:32000/cluster-manage` | `/task` 壳的 iframe 地址 |
| `SHELL_TELEMETRY_URL` | `http://101.34.209.104:39280/pages/vis.html` | `/telemetry` 壳的 iframe 地址 |
| `SHELL_COMPUTE_URL` | `http://101.34.209.104:8765` | `/compute` 壳的 iframe 地址 |
| `HEALTH_PROBE_SAMPLES` | `3` | `/health` 页面每台服务器采样次数（取中位数） |

backen 不做反向代理，**浏览器必须能直连上述地址**，否则 iframe 空白（页面本身仍正常渲染）。`101.34.209.104` 是 NAT 后的统一入口，`192.168.10.31` 只在 lab 内网可达。
