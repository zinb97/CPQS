# collector — Linux 主机 / GPU 指标采集器

把 procfs / sysfs / NVML 的数据转成 Prometheus 文本格式，通过 HTTP 暴露给
Prometheus 抓取。**只依赖 Python 标准库 + 可选的 `nvidia-ml-py`**。

## 启动

```bash
cd collector                      # 必须在 collector/ 目录内（裸导入）
pip install -r requirements.txt   # nvidia-ml-py，仅 GPU 采集需要；未装则只有 DCGM 指标缺失
python3 metrics_exporter.py --port 9100
```

端点：`/metrics`（Prometheus 格式）、`/health`、`/`。

后台常驻（deploy-tool 管理，PID 在 `/opt/CPQS/.run/collector.pid`，
日志在 `/opt/CPQS/logs/collector.log`）：

```bash
python3 deploy-tool/deploy.py restart --target D1 --task collector
```

## 文件

| 文件 | 职责 |
|------|------|
| `metrics_exporter.py` | HTTP 服务（`ThreadingHTTPServer`），聚合各采集器的输出 |
| `host_collector.py` | 主机侧采集：CPU / 内存 / 负载 / 磁盘 / 网络 / 文件系统 / 温度 / 静态信息 |
| `gpu_collector.py` | GPU 采集：走 `pynvml`（NVML），输出 DCGM 风格指标 |
| `promtext.py` | Prometheus 文本曝光格式的写出工具（标签转义、数值格式化，标签转义只此一处） |
| **`METRICS.md`** | **指标说明：采集了哪些内容、每个指标什么含义、口径与陷阱** |

## 代码结构

每个采集器都分三层：

1. **读取层** `_read_*`：每个方法只负责一个数据源，返回普通 dict/list；
2. **发射层** `_emit_*`：把读取层结果翻译成指标文本，统一经 `promtext.MetricWriter` 写出；
3. **对外接口** `collect()` / `to_json()` / `to_prometheus()`。

两条约定：

- 采集器实例被所有 HTTP 线程共享，故 `_data` 一律「**局部组装、整体赋值**」，
  绝不边采边改——否则并发抓取会读到半成品快照；
- 指标名与内部字段的映射写成**常量表**（`_MEMORY_METRICS` / `_DISK_*_METRICS` 等），
  新增指标改表即可，不必改发射逻辑。


## 指标说明

**见 [METRICS.md](METRICS.md)** —— 完整指标清单、来源、口径、以及 backen
侧的 PromQL 对照。改采集逻辑或新增指标后请同步更新该文件。
