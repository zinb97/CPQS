# backen — 算力网络 QoS 控制原型（FastAPI 后端）

算力网络 QoS 控制原型的 FastAPI 后端。Dashboard 所有指标实时来自 Prometheus，无 mock 数据。

## 架构

```
HTTP 请求
  │
  ├─ /                  → Jinja2 SSR（Dashboard，初始快照来自 Prometheus）
  ├─ /resource /network /task /telemetry /compute
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
| `GET /api/metrics/overview` | 4s | 9 个 Dashboard 指标卡聚合 |
| `GET /api/metrics/bandwidth` | 28s | 24h 带宽趋势（5m 步进） |
| `GET /api/metrics/map` | 28s | 集群散点数据（合并经纬度元数据） |
| `GET /api/metrics/slices` | 28s | 切片使用情况（PromQL 待定） |
| `GET /api/metrics/links` | 28s | 链路质量（PromQL 待定） |
| `GET /api/metrics/policies` | 28s | QoS 策略（PromQL 待定） |
| `GET /api/metrics/events` | 4s | ALERTS 兜底（Prometheus 非事件型） |

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
│   │   ├── shells.py        # 5 个 iframe 页面
│   │   └── api.py           # 7 个 JSON 端点
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

`queries.py` 集中存放所有 PromQL。9 条已知可用；其他（slices/links/policies/events）的 PromQL 名称待真实 exporter 暴露后补全。当前实现：

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
- ✅ 5 个 iframe 页面保持不变
- ✅ components.css 662 → 115 行

## 验证

```bash
# 1. 语法检查
python -m py_compile app/*.py app/*/*.py

# 2. 启动
uvicorn app.main:app --reload

# 3. 浏览器
# - http://localhost:8080/  → Dashboard
# - http://localhost:8080/resource 等 5 个 iframe 页
# - http://localhost:8080/api/metrics/overview  → JSON
``` 