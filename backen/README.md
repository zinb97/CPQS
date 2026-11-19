# backen — 算力网络 QoS 控制原型（FastAPI 后端）

算力网络 QoS 控制原型的 FastAPI 后端。Dashboard 所有指标实时来自 Prometheus，无 mock 数据。

## 架构

```
HTTP 请求
  │
  ├─ /                  → Jinja2 SSR（Dashboard，初始快照来自 Prometheus）
  ├─ /resource/*        → 原生页面（集群列表 / 集群详情 / 主机详情）
  ├─ /network /task /telemetry /compute
  │                      → iframe 壳页面，嵌入外部子系统（不代理）
  ├─ /health            → 浏览器侧时延测量页（时延由运行浏览器的设备实测）
  └─ /api/metrics/*      → JSON 端点，前端轮询用
                            │
                            ▼
                       Prometheus HTTP API (192.168.10.31:9090)
```

- 前端实时刷新：首屏 SSR 填充初始快照，之后由原生 JS 每 5s（指标卡）/ 30s（图表）轮询 `/api/metrics/*`
- 静态资源：`/static/*` 由 FastAPI `StaticFiles` 直接吐出（含 `china.json`）
- ECharts：CDN 加载（`/static/js/dashboard.js` 假定 window.echarts 存在）

## 运行

```bash
cd backen
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

或：

```bash
python -m app.main
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROMETHEUS_URL` | `http://192.168.10.31:9090` | Prometheus HTTP API 地址 |
| `CLUSTER_META_PATH` | `config/cluster_locations.yaml` | 集群元数据文件（相对 backen/ 根） |
| `REQUEST_TIMEOUT_S` | `3.0` | 单次 Prometheus 查询超时 |
| `HEALTH_PROBE_SAMPLES` | `3` | `/health` 页面每台服务器采样次数（取中位数） |

## API 端点

| 路径 | 缓存 | 说明 |
|------|------|------|
| `GET /api/metrics/overview` | 4s | 9 个 Dashboard 指标卡聚合（1 次 targets + 6 条 PromQL 并发） |
| `GET /api/metrics/bandwidth` | 28s | 24h 带宽趋势（5m 步进，单位 Mbps） |
| `GET /api/metrics/map` | 28s | 集群散点数据（经纬度元数据 + 在线/总节点数 + 健康态） |
| `GET /api/metrics/links` | 28s | 链路时延、丢包与额定速率（`qos_link_delay_ms` + `qos_link_loss_percent` + `qos_link_capacity_bits`，按 `src_dpid`/`dst_dpid` 合并；时延是 netem 配置值，丢包是实测值） |
| `GET /api/metrics/events` | 4s | ALERTS 兜底（Prometheus 非事件型），含 `lastUpdatedAt` 与 firing/resolved 状态 |
| `GET /api/metrics/clusters` | 4s | 集群列表（资源管控中心，含每集群 9 条 PromQL） |
| `GET /api/metrics/clusters/{cluster_id}` | 4s | 集群详情（含每节点 10 条 PromQL） |
| `GET /api/metrics/hosts/{instance}` | 4s | 主机详情 |
| `GET /api/metrics/hosts/{instance}/timeseries` | 28s | 主机 CPU/内存/网络时间序列 |
| `GET /health` | 无 | **浏览器侧时延测量页**（时延由运行浏览器的设备实测） |
| `GET /health/targets` | 无 | 浏览器侧探测用的目标清单（各服务器的 `url`） |

## `/health` —— 浏览器侧实测时延

**服务器端测不到「运行浏览器的设备 → 其它服务器」这条路径**——那是客户端的网络出口
（可能走不同的出口、不同的 NAT、不同的运营商链路），服务器能测的只有「backen → 各台
服务器」，那是另一条路径。因此时延一律由**浏览器自己**探测。

打开 `GET /health`（即 `/health/browser`）后：

1. 页面从 `/health/targets` 取各服务器的探测地址（采集器的 `GET /health`，返回 `OK`）
2. 浏览器用 `fetch(url, {mode:'no-cors'})` 逐个请求，每台采样 `HEALTH_PROBE_SAMPLES`
   （默认 3）次
3. 表格给出分段耗时

| 列 | 来源 | 含义 |
|----|------|------|
| **TCP 握手 RTT**（主指标） | `connectEnd - connectStart` | 一次 TCP 三次握手的往返时间，**浏览器能拿到的最接近 ping 的量**；不含 DNS、不含 HTTP 往返、不含服务端处理 |
| DNS | `domainLookupEnd - domainLookupStart` | 域名解析耗时（目标是 IP 字面量时通常为 0） |
| 首字节 | `responseStart - requestStart` | 请求发出到收到首个响应字节，含服务端处理 + 一次往返 |
| 总耗时 | `fetch()` 墙钟时间 | 用户实际感知的打开时延（= DNS + 建连 + HTTP 往返 + 浏览器开销） |
| 采样波动 | 握手 RTT 的极差 | 链路抖动 |

**采集器必须返回 `Timing-Allow-Origin`**：跨源请求下浏览器会把 Resource Timing 的分段
字段清零，没有该头就只能给出「总耗时」（页面会显示橙色提示条说明这一点）。新版采集器的
`/health` 已带该头 —— **需要部署才生效**：

```bash
python3 deploy-tool/deploy.py deploy --all --restart-tasks
```

**浏览器的硬限制**：JavaScript 不能建裸 TCP 连接、不能发 ICMP，只能发 HTTP 请求。
所以没有任何一列等于纯 ICMP ping。

分段数据做了两层防假值处理：① 只在「起点 > 0 且终点 > 起点」时才做差（Chrome 跨源
清零字段时可能只清零其中一个，否则会算出「DNS 613ms > 总耗时 505ms」这种假值）；
② 校验「各段之和 ≤ 该样本总耗时」，不满足的样本整组分段丢弃。

其它浏览器侧拦截：页面为 HTTPS 时目标为 HTTP 会被判「混合内容」；公网页面访问内网
地址（`192.168.10.x`）可能被 Private Network Access 策略拦截。这类失败会显示具体
错误信息。

`/health/targets` 返回的字段：`hostname`（来自 `node_uname_info`，同 IP 不同端口的
唯一区分标识）、`cluster`、`instance`（`ip:port`，端口映射后的对外地址）、`url`
（探测用地址）、`prom_health`（Prometheus 视角的抓取健康态）。

> 「缓存」列指响应头 `Cache-Control: max-age=N`（`cache_response()`）。**服务端没有内存缓存、没有 TTL 存储、没有 single-flight** —— 该头仅提示浏览器/中间代理缓存。每个标签页的每次轮询都会真实打到 Prometheus。

## 目录结构

```
backen/
├── requirements.txt
├── README.md
├── app/
│   ├── main.py              # FastAPI 入口 + 生命周期
│   ├── config.py            # dataclass 配置
│   ├── menu.py              # 侧边栏菜单 + base_context
│   ├── prometheus/
│   │   ├── client.py        # 异步 httpx 客户端
│   │   ├── queries.py       # PromQL 常量 + 集群元数据加载
│   │   └── targets.py       # 目标枚举 + 主机名映射（api.py 与 /health 共用）
│   ├── routers/
│   │   ├── dashboard.py     # GET /
│   │   ├── resource.py      # 资源管控中心原生页面（集群/主机详情）
│   │   ├── shells.py        # 4 个 iframe 页面（network/task/telemetry/compute）
│   │   ├── health.py        # GET /health（浏览器侧测量页）/ /health/targets
│   │   └── api.py           # 9 个 JSON 端点
│   ├── templates/
│   │   ├── base.html        # header + sidebar 布局
│   │   ├── dashboard.html   # Dashboard 内容
│   │   ├── health.html      # 浏览器侧时延测量页
│   │   └── shell.html        # iframe 壳
│   └── static/
│       ├── css/             # 4 个 CSS（精简版）+ health.css
│       ├── js/              # app.js + dashboard.js + health.js
│       └── data/china.json  # ECharts 中国 GeoJSON
└── config/
    └── cluster_locations.yaml
```

## 集群元数据

`config/cluster_locations.yaml` 里每个集群的字段：

| 字段 | 说明 |
|------|------|
| `id` | 与 Prometheus `labels.cluster` 对齐 |
| `name` | 集群名（界面主标识） |
| `province` | 省份，**与 `china.json` 的 `properties.name` 对齐**（用于地图下钻）；大区（华中/东北这类）概念已移除，界面统一按省份展示与聚合 |
| `location` | 经纬度 |

## PromQL 适配

`queries.py` 集中存放所有 PromQL，按数据来源分三段：

| 前缀 | 来源 | 覆盖内容 |
|------|------|----------|
| `node_*` / `DCGM_FI_DEV_*` | `collector/` 的 `/metrics` | 主机 CPU/内存/磁盘/网络/温度、GPU |
| `qos_*` | `QoSController/` 的 `GET /metrics` | 交换机/链路/主机数、packet-in、链路时延与丢包、组播 |
| `ALERTS` | Prometheus 内建 | 告警事件 |

大屏 9 张指标卡的 PromQL 与链路质量均已对接真实指标。资源管控中心另有一组 `q_cluster_*` / `q_node_*` 模板函数，按 `cluster` 或 `instance` 参数化。

> **排障要点**：PromQL 聚合函数必须**小写**（`sum()`，写成 `SUM()` 会被 Prometheus 判为解析错误并返回 HTTP 422）。`client.py` 的 `raise_for_status()` 会把它转成异常，异常又被 `asyncio.gather(return_exceptions=True)` 吞掉，最终表现为指标卡**静默显示 "—"，全程无报错**。指标名写错同理（Prometheus 对未知指标返回空向量而非报错）。

当前实现：

- 命中真实数据 → 正常显示
- 命中空结果 → 前端显示空状态
- Prometheus 不可达 → Dashboard 顶部红条提示

## 设计取舍

- ❌ 移除 Vite 构建步骤
- ❌ 移除 mock.js（所有数据真实）
- ❌ 移除 80% 死代码（components.js 中 11 个未用组件）
- ❌ 移除 Grafana 总览页面（原版 `/resource/iframe` 的 Grafana iframe 已整体下线）
- ✅ 新增 `/api/metrics/*` JSON 端点
- ✅ 新增 SSR 初始快照（首屏无 Loading）
- ✅ 新增 Prometheus 不可用横幅提示
- ✅ 4 个 iframe 页面保持不变（`/resource` 已改为原生页面）
- ✅ components.css 662 → 115 行

## 验证

```bash
# 1. 语法检查
python -m py_compile app/*.py app/*/*.py

# 2. 启动
uvicorn app.main:app --reload

# 3. 浏览器
# - http://localhost:8080/  → Dashboard
# - http://localhost:8080/resource/clusters  资源管控中心
# - http://localhost:8080/network 等 4 个 iframe 页
# - http://localhost:8080/api/metrics/overview  → JSON
``` 