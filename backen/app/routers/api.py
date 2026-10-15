"""Dashboard 实时数据 API：所有 `/api/metrics/*` 端点。

每个端点：
- 调用 Prometheus HTTP API
- 失败时返回空状态（前端显示 "—" 或空列表），不抛 500
- 设置 `Cache-Control: max-age=N` 与前端轮询周期对齐
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.prometheus.client import PrometheusClient
from app.prometheus.queries import (
    FLOPS_PRECISIONS,
    Q_BANDWIDTH_RANGE,
    Q_CPU_CORES,
    Q_EVENTS,
    Q_HOSTS,
    Q_LINKS,
    Q_LINKS_LOSS,
    Q_LINKS_QUALITY,
    Q_MEMORY_TOTAL,
    Q_PACKET_IN,
    Q_SWITCHES,
    q_cluster_cpu_cores,
    q_cluster_cpu_flops_peak,
    q_cluster_cpu_usage,
    q_cluster_gpu_count,
    q_cluster_gpu_flops_peak,
    q_cluster_gpu_temp,
    q_cluster_gpu_util,
    q_cluster_load,
    q_cluster_max_temp,
    q_cluster_mem_total,
    q_cluster_mem_usage,
    q_cpu_usage_by_cluster,
    q_gpu_util_by_cluster,
    q_node_boot_time,
    q_node_cpu_cores,
    q_node_cpu_flops_peak,
    q_node_cpu_usage,
    q_node_disk_size,
    q_node_disk_total,
    q_node_gpu_count,
    q_node_gpu_flops_peak,
    q_node_gpu_mem_used,
    q_node_gpu_models,
    q_node_gpu_temp,
    q_node_gpu_util,
    q_node_load1,
    q_node_max_temp,
    q_node_mem_avail,
    q_node_mem_total,
    q_node_network_rx,
    q_node_network_tx,
    q_node_procs_running,
)


router = APIRouter(prefix="/api/metrics")


# ===== Helpers =====

BYTES_PER_TB = 1024 ** 4
FLOPS_PER_PETA = 1e15


def _flops_map(*pairs: tuple[list[dict[str, Any]], str] | None) -> dict[str, float]:
    """把若干 (查询结果, precision) 对合并成 {precision: FLOPS/s}。

    传入 None 表示该口径在本次查询中没有数据（例如主机无 GPU）。
    结果**按行求和**：`sum by(cluster)` 会按集群返回多条 series，只取第一条
    等于只算了一个集群，会让「总算力」在多集群下凭空缩水。
    """
    out: dict[str, float] = {}
    for item in pairs:
        if item is None:
            continue
        result, precision = item
        total = sum(v for v in (_scalar([r]) for r in result or []) if v is not None)
        if total:
            out[precision] = total
    return out


def _sum_flops(maps: list[dict[str, float]]) -> dict[str, float]:
    """按精度把多组 {precision: FLOPS/s} 相加（主机/集群的 CPU + GPU）。"""
    out: dict[str, float] = {}
    for m in maps:
        for precision, value in m.items():
            out[precision] = out.get(precision, 0.0) + value
    return out


def _used_flops(
    cpu_peak: dict[str, float],
    gpu_peak: dict[str, float],
    cpu_ratio: float | None,
    gpu_percent: float | None,
) -> dict[str, float]:
    """已用算力 = CPU 峰值 × CPU 使用率 + GPU 峰值 × GPU 利用率。

    `cpu_ratio` 是 0~1 的比例（q_cluster_cpu_usage / q_node_cpu_usage 的口径）；
    `gpu_percent` 是 0~100 的百分数（DCGM_FI_DEV_GPU_UTIL 的口径），
    这里统一除 100 换算——两个量纲不同，漏了会让 GPU 已用算力放大 100 倍。

    fp16 只来自 GPU（Tensor Core 口径），乘上 GPU 向量单元的利用率并不成立，
    因此 fp16 不参与「已用」计算，只报峰值。
    """
    gpu_ratio = gpu_percent / 100.0 if gpu_percent is not None else None
    out: dict[str, float] = {}
    for precision, peak in cpu_peak.items():
        value = peak * cpu_ratio if cpu_ratio else 0.0
        if gpu_ratio and (gpu_p := gpu_peak.get(precision)) and precision != "fp16":
            value += gpu_p * gpu_ratio
        if value:
            out[precision] = value
    return out


def _scalar(result: list[dict[str, Any]]) -> float | None:
    """取第一条结果的数值。空列表或解析失败返回 None。"""
    if not result:
        return None
    try:
        value = result[0].get("value", [None, None])[1]
        return float(value) if value is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def metric_card(value: float | None, unit: str) -> dict[str, Any]:
    """统一指标卡数据形状：{value, unit}，无数据时 value=null。"""
    if value is None:
        return {"value": None, "unit": unit}
    if unit == "TB":
        return {"value": round(value / BYTES_PER_TB, 1), "unit": unit}
    if unit == "PFLOPS":
        # PF 起跳后 2 位小数只有 0.01 PF = 10 TF 的分辨率，单机规模的集群会被
        # 抹成 0.01，自适应降位到 PF 时给 4 位、否则 2 位。
        pf = value / FLOPS_PER_PETA
        return {"value": round(pf, 4 if pf < 1 else 2), "unit": unit}
    return {"value": int(value), "unit": unit}


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _round_pct(value: float | None) -> float | None:
    """百分比（0~1 → 0~100，保留 1 位小数）。None 透传。"""
    return round(value * 100, 1) if value is not None else None


def _first_scalar(results_list: Any, idx: int) -> float | None:
    """从并发结果集（list 或 Exception）中按索引取第一条数值。失败返回 None。"""
    if isinstance(results_list, Exception) or not isinstance(results_list, list):
        return None
    try:
        return _scalar(results_list[idx])
    except (IndexError, TypeError):
        return None


def cache_response(content: Any, max_age: int) -> JSONResponse:
    """带 Cache-Control 头的 JSON 响应。"""
    return JSONResponse(
        content=content,
        headers={"Cache-Control": f"public, max-age={max_age}"},
    )


def _client(request: Request) -> PrometheusClient:
    return request.app.state.prom


async def _safe_query(request: Request, promql: str) -> list[dict[str, Any]]:
    """统一的 Prometheus 查询包装：捕获所有异常，返回空列表。"""
    try:
        return await _client(request).query(promql)
    except Exception:
        return []


async def _resolve_instance(request: Request, instance: str) -> str:
    """把 URL 里的裸主机名/IP 补全成 Prometheus 的 instance 标签。

    页面 URL 用裸 IP 更干净（/resource/hosts/192.168.10.31），但 scrape config
    产生的 instance 标签带端口（192.168.10.31:9100），PromQL 必须匹配完整标签。

    带端口的入参直接透传；裸 IP 走 `/api/v1/targets` 反查，多个端口命中时取第一个。
    查不到就原样返回，让查询自然返回空、前端走「主机不存在」空状态。
    """
    if not instance or ":" in instance:
        return instance
    try:
        targets = await _client(request).targets()
    except Exception:
        return instance
    for t in targets:
        inst = (t.get("labels") or {}).get("instance") or ""
        if inst.rsplit(":", 1)[0] == instance:
            return inst
    return instance


def _summarize_targets(targets: list[dict[str, Any]]) -> tuple[int, int, int]:
    """从 `/api/v1/targets` 结果推导 (集群数, 总节点数, 在线节点数)。

    只统计 `labels.job` 以 `node-` 开头的目标（节点 exporter 抓取任务）；
    基础设施 job（prometheus、ryu-controller 等）自动排除。
    集群数取 `labels.cluster` 的 distinct 数。
    """
    clusters: set[str] = set()
    total = 0
    online = 0
    for t in targets:
        labels = t.get("labels") or {}
        if not (labels.get("job") or "").startswith("node-"):
            continue
        cluster_id = labels.get("cluster", "")
        if cluster_id:
            clusters.add(cluster_id)
        total += 1
        if t.get("health") == "up":
            online += 1
    return len(clusters), total, online


# ===== Endpoints =====


@router.get("/overview")
async def overview(request: Request) -> JSONResponse:
    """8 个 Dashboard 指标卡聚合查询。

    集群数 / 总节点数 / 在线节点数由 `/api/v1/targets` 推导；
    其余 6 个指标走 PromQL。
    """
    cli = _client(request)

    # 并发：1 个 /targets + 6 条 PromQL + 2 条算力
    results = await asyncio.gather(
        cli.targets(),
        cli.query(Q_CPU_CORES),
        cli.query(Q_MEMORY_TOTAL),
        cli.query(Q_SWITCHES),
        cli.query(Q_LINKS),
        cli.query(Q_HOSTS),
        cli.query(Q_PACKET_IN),
        cli.query(q_cluster_cpu_flops_peak("fp32")),
        cli.query(q_cluster_gpu_flops_peak("fp32")),
        return_exceptions=True,
    )
    prom_ok = not any(isinstance(r, Exception) for r in results)

    targets_data = results[0]
    if isinstance(targets_data, list):
        online_clusters, total_nodes, online_nodes = _summarize_targets(targets_data)
    else:
        online_clusters = total_nodes = online_nodes = None

    def _scalar_safe(idx: int) -> float | None:
        r = results[idx]
        return _scalar(r) if isinstance(r, list) else None

    payload = {
        "prom_ok": prom_ok,
        "online_clusters": metric_card(online_clusters, "个"),
        "total_nodes": metric_card(total_nodes, "个"),
        "online_nodes": metric_card(online_nodes, "个"),
        "cpu_cores": metric_card(_scalar_safe(1), "核"),
        "memory_total": metric_card(_scalar_safe(2), "TB"),
        "switches": metric_card(_scalar_safe(3), "个"),
        "links": metric_card(_scalar_safe(4), "条"),
        "hosts": metric_card(_scalar_safe(5), "个"),
        "packet_in": metric_card(_scalar_safe(6), "次"),
        # 总算力取 FP32 口径（AI 推理与传统 HPC 的通用口径）。CPU 与 GPU 分两条
        # 查询后在 Python 里相加——两族指标标签集不同，PromQL 里相加要写
        # on(instance) group_left()，错得很安静。
        "flops_fp32": metric_card(
            _sum_flops([_flops_map((results[7], "fp32")), _flops_map((results[8], "fp32"))])
            .get("fp32"),
            "PFLOPS",
        ),
    }
    return cache_response(payload, max_age=request.app.state.settings.cards_cache_s)


@router.get("/bandwidth")
async def bandwidth(request: Request) -> JSONResponse:
    """24h 带宽趋势（5m 步进）。"""
    end = datetime.now()
    start = end - timedelta(hours=24)
    try:
        results = await _client(request).query_range(
            Q_BANDWIDTH_RANGE, start, end, step="5m",
        )
    except Exception:
        results = []

    series: list[dict[str, float | str]] = []
    if results:
        for ts, v in results[0].get("values", []):
            try:
                series.append({
                    "time": datetime.fromtimestamp(float(ts)).strftime("%H:%M"),
                    "value": round(float(v), 2),
                })
            except (ValueError, TypeError):
                continue

    return cache_response(
        {"series": series, "unit": "Mbps"},
        max_age=request.app.state.settings.charts_cache_s,
    )


@router.get("/map")
async def map_data(request: Request) -> JSONResponse:
    """地图数据：集群散点 + 集群间链路。

    集群列表与在线节点数均从 `/api/v1/targets` 推导：
    - 集群枚举：distinct `labels.cluster`（仅统计 `job` 以 `node-` 开头的目标）
    - 在线节点数：每个集群下 `health == "up"` 的目标数
    - 名称/区域/经纬度：从 `cluster_locations.yaml` 按 `cluster.id` 匹配查表
    """
    cli = _client(request)
    cluster_meta: dict[str, dict[str, Any]] = request.app.state.cluster_meta

    # 1. 从 /api/v1/targets 聚合：cluster → 在线节点数 + 集群集合
    try:
        targets = await cli.targets()
    except Exception:
        targets = []

    online_by_cluster: dict[str, int] = {}
    total_by_cluster: dict[str, int] = {}
    cluster_ids: set[str] = set()
    for t in targets:
        labels = t.get("labels") or {}
        if not (labels.get("job") or "").startswith("node-"):
            continue
        cid = labels.get("cluster", "")
        if not cid:
            continue
        cluster_ids.add(cid)
        total_by_cluster[cid] = total_by_cluster.get(cid, 0) + 1
        if t.get("health") == "up":
            online_by_cluster[cid] = online_by_cluster.get(cid, 0) + 1

    # 2. 集群峰值算力 + 各自的 CPU/GPU 利用率（算「已用算力」用）
    cluster_flops = await _cluster_flops(request)
    util_rows, gpu_util_rows = await asyncio.gather(
        _safe_query(request, q_cpu_usage_by_cluster()),
        _safe_query(request, q_gpu_util_by_cluster()),
    )

    def _util_by_cluster(rows, label_key) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in rows or []:
            v = _scalar([r])
            if v is not None:
                out[(r.get("metric") or {}).get(label_key, "")] = v
        return out

    cpu_util_by_cluster = _util_by_cluster(util_rows, "cluster")
    gpu_util_by_cluster = _util_by_cluster(gpu_util_rows, "cluster")

    # 3. 合并 yaml 元数据 → 前端可直接渲染
    clusters: list[dict[str, Any]] = []
    for cid in cluster_ids:
        meta = cluster_meta.get(cid, {})
        online = online_by_cluster.get(cid, 0)
        total = total_by_cluster.get(cid, 0)
        if total == 0 or online == 0:
            status = "offline"
        elif online == total:
            status = "healthy"
        else:
            status = "warning"
        cf = cluster_flops.get(cid, {})
        clusters.append({
            "id": cid,
            "name": meta.get("name", cid),
            "region": meta.get("region", ""),
            "province": meta.get("province", ""),
            "location": meta.get("location", [0, 0]),
            "online_nodes": online,
            "total_nodes": total,
            "status": status,
            # 原始 FLOPS/s，由前端按 TF/PF/EF 格式化
            "flops": _sum_flops([cf.get("cpu", {}), cf.get("gpu", {})]),
            "flops_used": _used_flops(
                cf.get("cpu", {}), cf.get("gpu", {}),
                cpu_util_by_cluster.get(cid), gpu_util_by_cluster.get(cid),
            ),
        })

    # 兜底：targets 不可达时仍展示 yaml 中的元数据（地图基本形状）
    if not cluster_ids and cluster_meta:
        for cid, meta in cluster_meta.items():
            clusters.append({
                "id": cid,
                "name": meta.get("name", cid),
                "region": meta.get("region", ""),
                "province": meta.get("province", ""),
                "location": meta.get("location", [0, 0]),
                "online_nodes": 0,
                "total_nodes": 0,
                "status": "unknown",
                "flops": {},
                "flops_used": {},
            })

    return cache_response(
        {"clusters": clusters, "links": []},
        max_age=request.app.state.settings.charts_cache_s,
    )


@router.get("/links")
async def links_quality(request: Request) -> JSONResponse:
    """链路质量列表（延迟 / 丢包率）。

    数据源为 QoSController 暴露的 `qos_link_delay_ms` 与 `qos_link_loss_percent`，
    两者是独立 series，按 (src_dpid, dst_dpid) 合并为一条链路记录。
    双向链路会各自上报一条，此处保持原样不去重。
    """
    delay_results, loss_results = await asyncio.gather(
        _safe_query(request, Q_LINKS_QUALITY),
        _safe_query(request, Q_LINKS_LOSS),
    )

    links: dict[tuple[str, str], dict[str, Any]] = {}

    def _collect(results: list[dict[str, Any]], field: str) -> None:
        for r in results:
            labels = r.get("metric", {})
            src, dst = labels.get("src_dpid", ""), labels.get("dst_dpid", "")
            if not src or not dst:
                continue
            try:
                value = round(float(r["value"][1]), 2)
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            links.setdefault(
                (src, dst),
                {"source": src, "target": dst, "latency": None, "loss": None},
            )[field] = value

    _collect(delay_results, "latency")
    _collect(loss_results, "loss")

    return cache_response(
        {"items": list(links.values())},
        max_age=request.app.state.settings.charts_cache_s,
    )


@router.get("/events")
async def events(request: Request) -> JSONResponse:
    """实时事件流（Prometheus ALERTS 兜底，Prometheus 非事件型系统）。

    `lastUpdatedAt` 取自 sample 时间戳（Unix 秒），供前端渲染发生时刻。
    """
    results = await _safe_query(request, Q_EVENTS)
    items: list[dict[str, Any]] = []
    for r in results:
        labels = r.get("metric", {})
        try:
            updated_at = float(r["value"][0])
        except (KeyError, ValueError, TypeError, IndexError):
            updated_at = None
        items.append({
            "alertname": labels.get("alertname", ""),
            "state": labels.get("alertstate", ""),
            "severity": labels.get("severity", ""),
            "lastUpdatedAt": updated_at,
        })
    return cache_response({"items": items}, max_age=request.app.state.settings.cards_cache_s)


# ===== 资源管控中心页面数据 =====


async def _list_node_targets(request: Request) -> list[dict[str, Any]]:
    """取 /api/v1/targets 中 `job` 以 `node-` 开头的目标列表。"""
    try:
        targets = await _client(request).targets()
    except Exception:
        return []
    return [t for t in targets if (t.get("labels") or {}).get("job", "").startswith("node-")]


async def _cluster_flops(request: Request) -> dict[str, dict[str, dict[str, float]]]:
    """全部集群的峰值算力 → {cluster_id: {"cpu": {...}, "gpu": {...}}}。

    一次 `sum by(cluster)` 拿全部分组，比逐集群查省 3 倍请求。
    CPU 与 GPU 分开返回而非直接相加：「已用算力」需要分别乘各自设备的利用率
    （CPU 用 0~1 的比例，GPU 用 0~100 的百分数），提前合就没法算了。
    PromQL 里直接相加则要写 on(instance) group_left()，错得静默。
    """
    results = await asyncio.gather(
        *(
            _safe_query(request, fn(precision))
            for precision in FLOPS_PRECISIONS
            for fn in (q_cluster_cpu_flops_peak, q_cluster_gpu_flops_peak)
        )
    )
    out: dict[str, dict[str, dict[str, float]]] = {}
    for i, precision in enumerate(FLOPS_PRECISIONS):
        for key, row in (("cpu", results[2 * i]), ("gpu", results[2 * i + 1])):
            for entry in row:
                cid = (entry.get("metric") or {}).get("cluster")
                value = _scalar([entry])
                if cid and value is not None:
                    out.setdefault(cid, {"cpu": {}, "gpu": {}})[key][precision] = value
    return out


async def _node_flops(request: Request, instance: str) -> dict[str, dict[str, float]]:
    """单主机的 CPU / GPU 峰值算力 → {"cpu": {...}, "gpu": {...}}。"""
    results = await asyncio.gather(
        *(
            _safe_query(request, fn(instance, precision))
            for precision in FLOPS_PRECISIONS
            for fn in (q_node_cpu_flops_peak, q_node_gpu_flops_peak)
        )
    )
    out: dict[str, dict[str, float]] = {"cpu": {}, "gpu": {}}
    for i, precision in enumerate(FLOPS_PRECISIONS):
        for key, row in (("cpu", results[2 * i]), ("gpu", results[2 * i + 1])):
            value = _scalar(row)
            if value is not None:
                out[key][precision] = value
    return out


@router.get("/clusters")
async def clusters(request: Request) -> JSONResponse:
    """集群列表：按 cluster_id 聚合节点目标，叠加 yaml 元数据 + 实时聚合指标。

    每项含：id, name, region, province, location, online_nodes, total_nodes, status,
            cpu_usage, mem_usage, load1, cpu_cores, mem_total, max_temp。
    Prometheus 不可达时降级为仅展示 yaml 元数据（status='unknown'，指标字段为 null）。
    """
    cluster_meta: dict[str, dict[str, Any]] = request.app.state.cluster_meta
    node_targets = await _list_node_targets(request)

    online_by_cluster: dict[str, int] = {}
    total_by_cluster: dict[str, int] = {}
    for t in node_targets:
        labels = t.get("labels") or {}
        cid = labels.get("cluster", "")
        if not cid:
            continue
        total_by_cluster[cid] = total_by_cluster.get(cid, 0) + 1
        if t.get("health") == "up":
            online_by_cluster[cid] = online_by_cluster.get(cid, 0) + 1

    # 并发拉取每个集群的聚合指标（无集群则跳过）
    cids = list(cluster_meta.keys())
    prom_results = await asyncio.gather(
        *(
            asyncio.gather(
                _safe_query(request, q_cluster_cpu_usage(cid)),
                _safe_query(request, q_cluster_mem_usage(cid)),
                _safe_query(request, q_cluster_load(cid)),
                _safe_query(request, q_cluster_cpu_cores(cid)),
                _safe_query(request, q_cluster_mem_total(cid)),
                _safe_query(request, q_cluster_max_temp(cid)),
                _safe_query(request, q_cluster_gpu_count(cid)),
                _safe_query(request, q_cluster_gpu_util(cid)),
                _safe_query(request, q_cluster_gpu_temp(cid)),
                return_exceptions=True,
            )
            for cid in cids
        ),
        return_exceptions=True,
    )

    items: list[dict[str, Any]] = []
    cluster_flops = await _cluster_flops(request)
    for cid, meta in cluster_meta.items():
        try:
            row = prom_results[cids.index(cid)]
        except (ValueError, IndexError):
            row = None
        total = total_by_cluster.get(cid, 0)
        online = online_by_cluster.get(cid, 0)
        if total == 0 and not node_targets:
            status = "unknown"
        elif total == 0:
            status = "offline"
        elif online == total:
            status = "healthy"
        elif online == 0:
            status = "offline"
        else:
            status = "warning"

        cpu_usage = _first_scalar(row, 0)
        mem_usage = _first_scalar(row, 1)
        load1 = _first_scalar(row, 2)
        cpu_cores = _first_scalar(row, 3)
        mem_total = _first_scalar(row, 4)
        max_temp = _first_scalar(row, 5)
        gpu_count = _first_scalar(row, 6)
        gpu_util = _first_scalar(row, 7)
        gpu_temp = _first_scalar(row, 8)

        items.append({
            "id": cid,
            "name": meta.get("name", cid),
            "region": meta.get("region", ""),
            "province": meta.get("province", ""),
            "location": meta.get("location", [0, 0]),
            "online_nodes": online,
            "total_nodes": total,
            "status": status,
            "cpu_usage": _round_pct(cpu_usage),
            "mem_usage": _round_pct(mem_usage),
            "load1": _round(load1, 2),
            "cpu_cores": int(cpu_cores) if cpu_cores is not None else None,
            "mem_total_bytes": int(mem_total) if mem_total is not None else None,
            "max_temp_c": _round(max_temp, 1),
            "gpu_count": int(gpu_count) if gpu_count is not None else 0,
            "gpu_util": _round(gpu_util, 1),
            "gpu_max_temp_c": _round(gpu_temp, 1),
            "flops_peak": _sum_flops([cluster_flops.get(cid, {}).get("cpu", {}),
                                      cluster_flops.get(cid, {}).get("gpu", {})]),
            "flops_used": _used_flops(
                cluster_flops.get(cid, {}).get("cpu", {}),
                cluster_flops.get(cid, {}).get("gpu", {}),
                cpu_usage,
                gpu_util,
            ),
        })

    return cache_response({"items": items}, max_age=request.app.state.settings.cards_cache_s)


@router.get("/clusters/{cluster_id}")
async def cluster_detail(cluster_id: str, request: Request) -> JSONResponse:
    """集群详情：集群元数据 + 节点列表（每节点附实时指标）+ 集群聚合指标。"""
    cluster_meta: dict[str, dict[str, Any]] = request.app.state.cluster_meta
    meta = cluster_meta.get(cluster_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"cluster '{cluster_id}' not found")

    node_targets = await _list_node_targets(request)
    # 提取本集群下节点的 instance 列表
    cluster_nodes: list[dict[str, Any]] = []
    for t in node_targets:
        labels = t.get("labels") or {}
        if labels.get("cluster") != cluster_id:
            continue
        cluster_nodes.append({
            "instance": labels.get("instance", ""),
            "job": labels.get("job", ""),
            "health": t.get("health") or "unknown",
            "last_scrape": t.get("lastScrape"),
            "last_error": t.get("lastError"),
        })

    # 并发查询：本集群聚合 + 每节点 6 个指标
    agg_results = await asyncio.gather(
        _safe_query(request, q_cluster_cpu_usage(cluster_id)),
        _safe_query(request, q_cluster_mem_usage(cluster_id)),
        _safe_query(request, q_cluster_load(cluster_id)),
        _safe_query(request, q_cluster_cpu_cores(cluster_id)),
        _safe_query(request, q_cluster_mem_total(cluster_id)),
        _safe_query(request, q_cluster_max_temp(cluster_id)),
        _safe_query(request, q_cluster_gpu_count(cluster_id)),
        _safe_query(request, q_cluster_gpu_util(cluster_id)),
        _safe_query(request, q_cluster_gpu_temp(cluster_id)),
        return_exceptions=True,
    )

    # 每节点指标：10 个 PromQL × N 节点 → asyncio.gather 二维并发
    node_query_jobs = []
    for n in cluster_nodes:
        inst = n["instance"]
        if not inst:
            node_query_jobs.append(asyncio.gather(*(asyncio.sleep(0) for _ in range(10)), return_exceptions=True))
            continue
        node_query_jobs.append(asyncio.gather(
            _safe_query(request, q_node_cpu_usage(inst)),
            _safe_query(request, q_node_load1(inst)),
            _safe_query(request, q_node_mem_total(inst)),
            _safe_query(request, q_node_mem_avail(inst)),
            _safe_query(request, q_node_max_temp(inst)),
            _safe_query(request, q_node_procs_running(inst)),
            _safe_query(request, q_node_gpu_count(inst)),
            _safe_query(request, q_node_gpu_util(inst)),
            _safe_query(request, q_node_gpu_temp(inst)),
            _safe_query(request, q_node_gpu_mem_used(inst)),
            return_exceptions=True,
        ))
    per_node_results = await asyncio.gather(*node_query_jobs, return_exceptions=True)
    # 每节点算力：3 精度 × CPU/GPU 共 6 条，与上面的 10 条并发发起。
    # 按 instance 建索引而非用迭代器：上面 node_query_jobs 对缺 instance 的节点
    # 也占位，迭代器下标会与 cluster_nodes 错位。
    per_node_flops = dict(zip(
        [n["instance"] for n in cluster_nodes if n["instance"]],
        await asyncio.gather(*(_node_flops(request, n["instance"])
                               for n in cluster_nodes if n["instance"])),
    ))

    nodes_out: list[dict[str, Any]] = []
    online = total = 0
    # 集群峰值算力由逐节点累加而来，不再单独发 sum by(cluster)：
    # 少 6 次查询，且与下方节点表口径天然一致（不会出现表里加总与卡片对不上）。
    cluster_cpu_peak: list[dict[str, float]] = []
    cluster_gpu_peak: list[dict[str, float]] = []
    for n, nr in zip(cluster_nodes, per_node_results):
        if n["health"] == "up":
            online += 1
        total += 1
        cpu = _first_scalar(nr, 0)
        load1 = _first_scalar(nr, 1)
        mem_total = _first_scalar(nr, 2)
        mem_avail = _first_scalar(nr, 3)
        max_temp = _first_scalar(nr, 4)
        procs = _first_scalar(nr, 5)
        gpu_count = _first_scalar(nr, 6)
        gpu_util = _first_scalar(nr, 7)
        gpu_temp = _first_scalar(nr, 8)
        gpu_mem_used = _first_scalar(nr, 9)
        mem_usage = (
            (mem_total - mem_avail) / mem_total if (mem_total and mem_avail is not None and mem_total > 0) else None
        )
        nf = per_node_flops.get(n["instance"], {"cpu": {}, "gpu": {}})
        cluster_cpu_peak.append(nf["cpu"])
        cluster_gpu_peak.append(nf["gpu"])
        n_out = dict(n)
        n_out.update({
            "cpu_usage": _round_pct(cpu),
            "load1": _round(load1, 2),
            "mem_total_bytes": int(mem_total) if mem_total is not None else None,
            "mem_used_bytes": int(mem_total - mem_avail) if (mem_total is not None and mem_avail is not None) else None,
            "mem_usage": _round_pct(mem_usage),
            "max_temp_c": _round(max_temp, 1),
            "procs_running": int(procs) if procs is not None else None,
            "gpu_count": int(gpu_count) if gpu_count is not None else 0,
            "gpu_util": _round(gpu_util, 1),
            "gpu_max_temp_c": _round(gpu_temp, 1),
            "gpu_mem_used_mib": _round(gpu_mem_used, 0),
            "flops_peak": _sum_flops([nf["cpu"], nf["gpu"]]),
            "flops_used": _used_flops(nf["cpu"], nf["gpu"], cpu, gpu_util),
        })
        nodes_out.append(n_out)

    if total == 0 and not node_targets:
        status = "unknown"
    elif online == total and total > 0:
        status = "healthy"
    elif online == 0:
        status = "offline"
    else:
        status = "warning"

    def _agg(idx: int):
        r = agg_results[idx]
        return _scalar(r) if isinstance(r, list) else None

    payload = {
        **meta,
        "id": cluster_id,
        "status": status,
        "online_nodes": online,
        "total_nodes": total,
        "cpu_usage": _round_pct(_agg(0)),
        "mem_usage": _round_pct(_agg(1)),
        "load1": _round(_agg(2), 2),
        "cpu_cores": int(_agg(3)) if _agg(3) is not None else None,
        "mem_total_bytes": int(_agg(4)) if _agg(4) is not None else None,
        "max_temp_c": _round(_agg(5), 1),
        "gpu_count": int(_agg(6)) if _agg(6) is not None else 0,
        "gpu_util": _round(_agg(7), 1),
        "gpu_max_temp_c": _round(_agg(8), 1),
        "flops_peak": _sum_flops(cluster_cpu_peak + cluster_gpu_peak),
        "flops_used": _used_flops(
            _sum_flops(cluster_cpu_peak), _sum_flops(cluster_gpu_peak),
            _agg(0), _agg(7),
        ),
        "nodes": nodes_out,
    }
    return cache_response(payload, max_age=request.app.state.settings.cards_cache_s)


# 时序窗口预设：(时长, 步进)。前端通过 ?window= 选择。
_WINDOW_PRESETS: dict[str, tuple[timedelta, str]] = {
    "30m": (timedelta(minutes=30), "30s"),
    "1h":  (timedelta(hours=1),    "1m"),
    "6h":  (timedelta(hours=6),    "5m"),
    "24h": (timedelta(hours=24),   "15m"),
}


@router.get("/hosts/{instance:path}/timeseries")
async def host_timeseries(instance: str, request: Request, window: str = "30m") -> JSONResponse:
    """主机实时指标时序（窗口可调，默认 30m）。

    返回 11 个序列：cpu_usage / mem_usage / load1 / max_temp_c / procs_running /
    disk_io / net_rx / net_tx / gpu_util / gpu_max_temp_c / gpu_mem_used。
    每个序列均为 {time, value}[]，time 为 HH:MM:SS 字符串。

    `window` 取值：30m / 1h / 6h / 24h；不识别时回退到 30m。

    同一指标可能按 cpu/gpu/disk/iface 分裂为多条 series（Prometheus 默认行为）；
    这里按时间戳对齐并按 metric 指定的聚合方式（avg/max/sum）合并为一个序列。

    注意：本端点必须在 `/hosts/{instance:path}` 之前注册——FastAPI 按声明顺序匹配，
    否则 `:path` 贪婪匹配会把 `/timeseries` 一并吞进 instance。
    """
    span, step = _WINDOW_PRESETS.get(window, _WINDOW_PRESETS["30m"])
    cli = _client(request)
    instance = await _resolve_instance(request, instance)
    end = datetime.now()
    start = end - span

    # (name, promql, 聚合方式)
    # avg : 多核/多卡/多温度区取平均
    # max : 最高温度类
    # sum : 磁盘 IO、显存已用（多卡/多盘累加）
    # last: 单 series 指标（load1/mem_total/procs_running）
    metric_specs: list[tuple[str, str, str]] = [
        ("cpu_usage",      q_node_cpu_usage(instance),       "avg"),
        ("mem_usage", (
            f'1 - node_memory_MemAvailable_bytes{{instance="{instance}"}}'
            f' / node_memory_MemTotal_bytes{{instance="{instance}"}}'
        ),                                                                          "last"),
        ("load1",          q_node_load1(instance),            "last"),
        ("max_temp_c",     q_node_max_temp(instance),         "max"),
        ("procs_running",  q_node_procs_running(instance),    "last"),
        ("disk_io",        q_node_disk_total(instance),       "sum"),
        ("net_rx",         q_node_network_rx(instance),       "sum"),
        ("net_tx",         q_node_network_tx(instance),       "sum"),
        ("gpu_util",       q_node_gpu_util(instance),         "avg"),
        ("gpu_max_temp_c", q_node_gpu_temp(instance),         "max"),
        ("gpu_mem_used",   q_node_gpu_mem_used(instance),     "sum"),
    ]

    raw = await asyncio.gather(
        *(cli.query_range(pql, start, end, step="30s") for _, pql, _ in metric_specs),
        return_exceptions=True,
    )

    def _agg_series(result: Any, mode: str) -> list[dict[str, float | str]]:
        """把多条 series 合并为单条：按 ts 对齐，按 mode 聚合。"""
        if not isinstance(result, list) or not result:
            return []
        bucket: dict[float, list[float]] = {}
        ts_set: set[float] = set()
        for series in result:
            for ts, v in series.get("values", []):
                try:
                    fts = float(ts)
                    fv = float(v)
                except (ValueError, TypeError):
                    continue
                bucket.setdefault(fts, []).append(fv)
                ts_set.add(fts)
        if not ts_set:
            return []
        out: list[dict[str, float | str]] = []
        for fts in sorted(ts_set):
            vals = bucket[fts]
            if not vals:
                continue
            if mode == "avg":
                v = sum(vals) / len(vals)
            elif mode == "max":
                v = max(vals)
            elif mode == "sum":
                v = sum(vals)
            else:  # last
                v = vals[-1]
            out.append({
                "time": datetime.fromtimestamp(fts).strftime("%H:%M:%S"),
                "value": v,
            })
        return out

    series = {
        name: _agg_series(r, mode)
        for (name, _, mode), r in zip(metric_specs, raw)
    }
    # 百分比类指标从 0~1 转换为 0~100（与 host_detail 中 _round_pct 一致）。
    # 注：gpu_util 来源是 DCGM_FI_DEV_GPU_UTIL，本身已是 0~100，不需要再 *100。
    for name in ("cpu_usage", "mem_usage"):
        for p in series.get(name) or []:
            v = p.get("value")
            if isinstance(v, (int, float)):
                p["value"] = round(v * 100, 1)
    return cache_response(
        {"instance": instance, "series": series},
        max_age=request.app.state.settings.charts_cache_s,
    )


@router.get("/hosts/{instance:path}")
async def host_detail(instance: str, request: Request) -> JSONResponse:
    """主机详情：从 /api/v1/targets 中按 instance 字段定位单条 node-* 目标。

    返回：
    - 抓取信息（health/lastScrape/lastError）
    - 集群归属
    - 8 类实时指标（CPU/内存/负载/温度/进程/网络/磁盘/启动时间）
    - 系统信息（uname/os/dmi，从 node_*_info 元指标标签解析）
    """
    cluster_meta: dict[str, dict[str, Any]] = request.app.state.cluster_meta
    node_targets = await _list_node_targets(request)

    # URL 传的是裸 IP（/resource/hosts/192.168.10.31），instance 标签带端口，
    # 这里复用已取到的 targets 列表补全，不额外发请求。
    if instance and ":" not in instance:
        instance = next(
            (
                (t.get("labels") or {}).get("instance")
                for t in node_targets
                if ((t.get("labels") or {}).get("instance") or "").rsplit(":", 1)[0]
                == instance
            ),
            instance,
        )

    target = next(
        (
            t for t in node_targets
            if (t.get("labels") or {}).get("instance") == instance
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail=f"host '{instance}' not found")

    labels = target.get("labels") or {}
    cid = labels.get("cluster", "")
    cluster = cluster_meta.get(cid, {})

    # 并发查询：8 类指标 + 4 个元指标 + 4 个 GPU 指标 + 算力（内部再展开 6 条）
    (
        cpu, cpu_cores, cpu_info, load1, mem_total, mem_avail, max_temp, procs,
        boot_time, disk_io, disk_size, net_rx, net_tx,
        uname_info, os_info, dmi_info,
        gpu_count, gpu_util, gpu_temp, gpu_mem_used, gpu_models, host_flops,
    ) = await asyncio.gather(
        _safe_query(request, q_node_cpu_usage(instance)),
        _safe_query(request, q_node_cpu_cores(instance)),
        _safe_query(request, f'node_cpu_info{{instance="{instance}"}}'),
        _safe_query(request, q_node_load1(instance)),
        _safe_query(request, q_node_mem_total(instance)),
        _safe_query(request, q_node_mem_avail(instance)),
        _safe_query(request, q_node_max_temp(instance)),
        _safe_query(request, q_node_procs_running(instance)),
        _safe_query(request, q_node_boot_time(instance)),
        _safe_query(request, q_node_disk_total(instance)),
        _safe_query(request, q_node_disk_size(instance)),
        _safe_query(request, q_node_network_rx(instance)),
        _safe_query(request, q_node_network_tx(instance)),
        _safe_query(request, f'node_uname_info{{instance="{instance}"}}'),
        _safe_query(request, f'node_os_info{{instance="{instance}"}}'),
        _safe_query(request, f'node_dmi_info{{instance="{instance}"}}'),
        _safe_query(request, q_node_gpu_count(instance)),
        _safe_query(request, q_node_gpu_util(instance)),
        _safe_query(request, q_node_gpu_temp(instance)),
        _safe_query(request, q_node_gpu_mem_used(instance)),
        _safe_query(request, q_node_gpu_models(instance)),
        _node_flops(request, instance),
        return_exceptions=True,
    )

    def _q(r) -> float | None:
        return _scalar(r) if isinstance(r, list) else None

    def _info(r) -> dict[str, str]:
        """从 *_info 元指标取标签，只保留采集器自己上报的字段。

        `__name__/instance/job/cluster/location` 由 Prometheus scrape config
        注入或属于指标本身，不属于设备信息，统一剔除。
        """
        if not isinstance(r, list) or not r:
            return {}
        out: dict[str, str] = {}
        for k, v in (r[0].get("metric") or {}).items():
            if k in ("__name__", "instance", "job", "cluster", "location"):
                continue
            out[k] = str(v)
        return out

    mem_total_v = _q(mem_total)
    mem_avail_v = _q(mem_avail)
    mem_used_v = (mem_total_v - mem_avail_v) if (mem_total_v is not None and mem_avail_v is not None) else None

    # gather 带 return_exceptions，异常会以 BaseException 对象回来，这里退回空结构
    hf = host_flops if isinstance(host_flops, dict) else {"cpu": {}, "gpu": {}}

    payload = {
        "instance": instance,
        "job": labels.get("job", ""),
        "cluster_id": cid,
        "cluster_name": cluster.get("name", cid),
        "region": cluster.get("region", ""),
        "province": cluster.get("province", ""),
        "health": target.get("health") or "unknown",
        "last_scrape": target.get("lastScrape"),
        "last_error": target.get("lastError"),

        # 实时指标
        "cpu_usage": _round_pct(_q(cpu)),
        "cpu_cores": int(_q(cpu_cores)) if _q(cpu_cores) is not None else None,
        "load1": _round(_q(load1), 2),
        "mem_total_bytes": int(mem_total_v) if mem_total_v is not None else None,
        "mem_used_bytes": int(mem_used_v) if mem_used_v is not None else None,
        "mem_usage": _round_pct(mem_used_v / mem_total_v) if (mem_used_v is not None and mem_total_v) else None,
        "max_temp_c": _round(_q(max_temp), 1),
        "procs_running": int(_q(procs)) if _q(procs) is not None else None,
        "boot_time": int(_q(boot_time)) if _q(boot_time) is not None else None,
        "disk_io_bytes_per_sec": _round(_q(disk_io), 0),
        "disk_total_bytes": int(_q(disk_size)) if _q(disk_size) is not None else None,
        "net_rx_bytes_per_sec": _round(_q(net_rx), 0),
        "net_tx_bytes_per_sec": _round(_q(net_tx), 0),

        # GPU
        "gpu_count": int(_q(gpu_count)) if _q(gpu_count) is not None else 0,
        "gpu_util": _round(_q(gpu_util), 1),
        "gpu_max_temp_c": _round(_q(gpu_temp), 1),
        "gpu_mem_used_mib": _round(_q(gpu_mem_used), 0),
        # 型号 → 卡数；同机异构卡会有多项，按卡数降序
        "gpu_models": (
            sorted(
                (
                    {
                        "model": str((r.get("metric") or {}).get("modelName") or "未知"),
                        "count": int(_scalar([r]) or 0),
                    }
                    for r in gpu_models
                    if isinstance(r, dict)
                ),
                key=lambda m: -m["count"],
            )
            if isinstance(gpu_models, list)
            else []
        ),

        # 系统信息
        "cpu_info": _info(cpu_info),
        "uname": _info(uname_info),
        "os": _info(os_info),
        "dmi": _info(dmi_info),

        # 算力：原始 FLOPS/s，按精度分键（fp32/fp64/fp16）。fp16 仅 GPU 有。
        "flops_peak": _sum_flops([hf["cpu"], hf["gpu"]]),
        "flops_used": _used_flops(hf["cpu"], hf["gpu"], _q(cpu), _q(gpu_util)),

        # 同集群其他主机（用于下拉框切换）。按 instance 排序。
        "cluster_hosts": sorted(
            [
                {
                    "instance": (t.get("labels") or {}).get("instance", ""),
                    "health": t.get("health") or "unknown",
                }
                for t in node_targets
                if (t.get("labels") or {}).get("cluster") == cid
                and (t.get("labels") or {}).get("instance")
            ],
            key=lambda x: x["instance"],
        ),
    }
    return cache_response(payload, max_age=request.app.state.settings.cards_cache_s)
