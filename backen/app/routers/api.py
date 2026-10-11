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
    Q_BANDWIDTH_RANGE,
    Q_CPU_CORES,
    Q_EVENTS,
    Q_HOSTS,
    Q_LINKS,
    Q_LINKS_QUALITY,
    Q_MEMORY_TOTAL,
    Q_PACKET_IN,
    Q_POLICIES,
    Q_SLICES,
    Q_SWITCHES,
    q_cluster_cpu_cores,
    q_cluster_cpu_usage,
    q_cluster_gpu_count,
    q_cluster_gpu_temp,
    q_cluster_gpu_util,
    q_cluster_load,
    q_cluster_max_temp,
    q_cluster_mem_total,
    q_cluster_mem_usage,
    q_node_boot_time,
    q_node_cpu_cores,
    q_node_cpu_usage,
    q_node_disk_size,
    q_node_disk_total,
    q_node_gpu_count,
    q_node_gpu_mem_used,
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

    # 并发：1 个 /targets + 6 条 PromQL
    results = await asyncio.gather(
        cli.targets(),
        cli.query(Q_CPU_CORES),
        cli.query(Q_MEMORY_TOTAL),
        cli.query(Q_SWITCHES),
        cli.query(Q_LINKS),
        cli.query(Q_HOSTS),
        cli.query(Q_PACKET_IN),
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
    cluster_ids: set[str] = set()
    for t in targets:
        labels = t.get("labels") or {}
        if not (labels.get("job") or "").startswith("node-"):
            continue
        cid = labels.get("cluster", "")
        if not cid:
            continue
        cluster_ids.add(cid)
        if t.get("health") == "up":
            online_by_cluster[cid] = online_by_cluster.get(cid, 0) + 1

    # 2. 合并 yaml 元数据 → 前端可直接渲染
    clusters: list[dict[str, Any]] = []
    for cid in cluster_ids:
        meta = cluster_meta.get(cid, {})
        online = online_by_cluster.get(cid, 0)
        status = "healthy" if online > 0 else "offline"
        clusters.append({
            "id": cid,
            "name": meta.get("name", cid),
            "region": meta.get("region", ""),
            "province": meta.get("province", ""),
            "location": meta.get("location", [0, 0]),
            "online_nodes": online,
            "status": status,
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
                "status": "unknown",
            })

    return cache_response(
        {"clusters": clusters, "links": []},
        max_age=request.app.state.settings.charts_cache_s,
    )


@router.get("/slices")
async def slices(request: Request) -> JSONResponse:
    """网络切片占用数据。PromQL 不确定时返回空列表，前端显示空状态。"""
    results = await _safe_query(request, Q_SLICES)
    items: list[dict[str, Any]] = []
    for r in results:
        labels = r.get("metric", {})
        try:
            used = float(r["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
        items.append({
            "name": labels.get("slice", labels.get("name", "unknown")),
            "cpu_used": int(used),
        })
    return cache_response({"items": items}, max_age=request.app.state.settings.charts_cache_s)


@router.get("/links")
async def links_quality(request: Request) -> JSONResponse:
    """链路质量列表（延迟 / 丢包率）。"""
    results = await _safe_query(request, Q_LINKS_QUALITY)
    items: list[dict[str, Any]] = []
    for r in results:
        labels = r.get("metric", {})
        try:
            value = float(r["value"][1])
        except (KeyError, ValueError, TypeError):
            continue
        items.append({
            "source": labels.get("source", ""),
            "target": labels.get("target", ""),
            "latency": value,
        })
    return cache_response({"items": items}, max_age=request.app.state.settings.charts_cache_s)


@router.get("/policies")
async def policies(request: Request) -> JSONResponse:
    """QoS 策略列表。"""
    results = await _safe_query(request, Q_POLICIES)
    items: list[dict[str, Any]] = []
    for r in results:
        labels = r.get("metric", {})
        items.append({
            "name": labels.get("name", "unknown"),
            "type": labels.get("type", ""),
            "priority": labels.get("priority", ""),
            "status": labels.get("status", "active"),
        })
    return cache_response({"items": items}, max_age=request.app.state.settings.charts_cache_s)


@router.get("/events")
async def events(request: Request) -> JSONResponse:
    """实时事件流（Prometheus ALERTS 兜底，Prometheus 非事件型系统）。"""
    results = await _safe_query(request, Q_EVENTS)
    items: list[dict[str, Any]] = []
    for r in results:
        labels = r.get("metric", {})
        items.append({
            "alertname": labels.get("alertname", ""),
            "state": labels.get("alertstate", ""),
            "severity": labels.get("severity", ""),
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

    nodes_out: list[dict[str, Any]] = []
    online = total = 0
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

    # 并发查询：8 类指标 + 3 个元指标 + 4 个 GPU 指标
    (
        cpu, cpu_cores, load1, mem_total, mem_avail, max_temp, procs,
        boot_time, disk_io, disk_size, net_rx, net_tx,
        uname_info, os_info, dmi_info,
        gpu_count, gpu_util, gpu_temp, gpu_mem_used,
    ) = await asyncio.gather(
        _safe_query(request, q_node_cpu_usage(instance)),
        _safe_query(request, q_node_cpu_cores(instance)),
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
        return_exceptions=True,
    )

    def _q(r) -> float | None:
        return _scalar(r) if isinstance(r, list) else None

    def _info(r) -> dict[str, str]:
        if not isinstance(r, list) or not r:
            return {}
        out: dict[str, str] = {}
        for k, v in (r[0].get("metric") or {}).items():
            if k in ("__name__", "instance", "job", "cluster"):
                continue
            out[k] = str(v)
        return out

    mem_total_v = _q(mem_total)
    mem_avail_v = _q(mem_avail)
    mem_used_v = (mem_total_v - mem_avail_v) if (mem_total_v is not None and mem_avail_v is not None) else None

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

        # 系统信息
        "uname": _info(uname_info),
        "os": _info(os_info),
        "dmi": _info(dmi_info),

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
