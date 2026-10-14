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

## API 端点

| 路径 | 缓存 | 说明 |
|------|------|------|
| `GET /api/metrics/overview` | 4s | 9 个 Dashboard 指标卡聚合（1 次 targets + 6 条 PromQL 并发） |
| `GET /api/metrics/bandwidth` | 28s | 24h 带宽趋势（5m 步进，单位 Mbps） |
| `GET /api/metrics/map` | 28s | 集群散点数据（经纬度元数据 + 在线/总节点数 + 健康态） |
| `GET /api/metrics/links` | 28s | 链路时延与丢包（`qos_link_delay_ms` + `qos_link_loss_percent`，按 `src_dpid`/`dst_dpid` 合并） |
| `GET /api/metrics/events` | 4s | ALERTS 兜底（Prometheus 非事件型），含 `lastUpdatedAt` 与 firing/resolved 状态 |
| `GET /api/metrics/clusters` | 4s | 集群列表（资源管控中心，含每集群 9 条 PromQL） |
| `GET /api/metrics/clusters/{cluster_id}` | 4s | 集群详情（含每节点 10 条 PromQL） |
| `GET /api/metrics/hosts/{instance}` | 4s | 主机详情 |
| `GET /api/metrics/hosts/{instance}/timeseries` | 28s | 主机 CPU/内存/网络时间序列 |

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
│   │   └── queries.py       # PromQL 常量 + 集群元数据加载
│   ├── routers/
│   │   ├── dashboard.py     # GET /
│   │   ├── resource.py      # 资源管控中心原生页面（集群/主机详情）
│   │   ├── shells.py        # 4 个 iframe 页面（network/task/telemetry/compute）
│   │   └── api.py           # 9 个 JSON 端点
│   ├── templates/
│   │   ├── base.html        # header + sidebar 布局
│   │   ├── dashboard.html   # Dashboard 内容
│   │   └── shell.html        # iframe 壳
│   └── static/
│       ├── css/             # 4 个 CSS（精简版）
│       ├── js/              # app.js + dashboard.js
│       └── data/china.json  # ECharts 中国 GeoJSON
└── config/
    └── cluster_locations.yaml
```

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
- ❌ 移除 Grafana iframe 的 sandbox 缺失（原版 4 个 Grafana iframe 无 sandbox）
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