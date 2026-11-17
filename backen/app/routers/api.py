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
    Q_BANDWIDTH_RX_RANGE,
    Q_BANDWIDTH_TX_RANGE,
    Q_CPU_CORES,
    Q_EVENTS,
    Q_HOSTS,
    Q_LINKS,
    Q_LINKS_BANDWIDTH,
    Q_LINKS_CAPACITY,
    Q_LINKS_LOSS,
    Q_LINKS_LOSS_CONFIGURED,
    Q_LINKS_QUALITY,
    Q_MEMORY_TOTAL,
    Q_PACKET_IN,
    Q_RULES_ACTIVE,
    Q_SEC_CONN_FAILS,
    Q_SEC_CONN_OPENS,
    Q_SEC_LISTEN_DROPS,
    Q_SEC_NO_ROUTES,
    Q_SEC_SYN_RETRANS,
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


def _flops_map(*pairs: tuple[list[dict[str, Any]], str] | None) -> dict[str, float]:
    """把若干 (查询结果, precision) 对合并成 {precision: FLOPS/s}。

    传入 None 表示该口径在本次查询中没有数据（例如主机无 GPU）。
    结果**按行求和**：`sum by(cluster)` 会按集群返回多条 series，只取第一条
    等于只算了一个集群，会让「总算力」在多集群下凭空缩水。

    `result` 也可能是 `asyncio.gather(..., return_exceptions=True)` 带回的
    Exception 实例（查询失败时）。Exception 本身是 truthy，`result or []`
    拦不住它，直接迭代会抛 TypeError 并让整个端点返回 500 —— 所以这里
    先做 isinstance 检查，把失败视作「该口径无数据」。
    """
    out: dict[str, float] = {}
    for item in pairs:
        if item is None:
            continue
        result, precision = item
        if not isinstance(result, list):
            continue
        total = sum(v for v in (_scalar([r]) for r in result) if v is not None)
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
    if unit == "FLOPS":
        return _auto_flops(value)
    return {"value": int(value), "unit": unit}


def _auto_flops(value: float) -> dict[str, Any]:
    """按量级自适应单位，保证数值落在 [1, 1000) 区间。

    单位写死成 PFLOPS 时，单机规模的集群（FP64 约 1.6 TF）会显示成 0.0016
    PFLOPS —— 既读不出数，也无法和别的卡横向比较。算力跨度能从单机到万卡集群，
    固定任何一种单位都会在一端崩掉，所以单位跟着数值走。
    """
    for unit, scale in (("EFLOPS", 1e18), ("PFLOPS", 1e15), ("TFLOPS", 1e12)):
        if value >= scale:
            return {"value": round(value / scale, 2), "unit": unit}
    return {"value": round(value / 1e9, 2), "unit": "GFLOPS"}


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


def _match_instance_by_ip(targets: list[dict[str, Any]], ip: str) -> str:
    """在 targets 里按 IP 找回带端口的 instance 标签。

    端口映射场景下同一个 IP 下挂着多台服务器（instance 形如 ip:9111），
    裸 IP 本身是有歧义的：优先返回在线（health == "up"）的那台，
    都不在线时退回第一个匹配项。
    """
    fallback = ""
    for t in targets:
        inst = (t.get("labels") or {}).get("instance") or ""
        if inst.rsplit(":", 1)[0] != ip:
            continue
        if t.get("health") == "up":
            return inst
        if not fallback:
            fallback = inst
    return fallback


async def _resolve_instance(request: Request, instance: str) -> str:
    """把 URL 里的裸主机名/IP 补全成 Prometheus 的 instance 标签。

    scrape config 产生的 instance 标签带端口（192.168.10.31:9100），
    PromQL 必须匹配完整标签。

    带端口的入参直接透传（页面上已按 ip:port 链接，端口映射下这是唯一
    无歧义的主机标识）；裸 IP 走 `/api/v1/targets` 反查，同 IP 多端口时
    优先取在线的那台。查不到就原样返回，让查询自然返回空、
    前端走「主机不存在」空状态。
    """
    if not instance or ":" in instance:
        return instance
    try:
        targets = await _client(request).targets()
    except Exception:
        return instance
    return _match_instance_by_ip(targets, instance) or instance


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
        cli.query(q_cluster_cpu_flops_peak("fp64")),
        cli.query(q_cluster_gpu_flops_peak("fp64")),
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
        # 总算力取 FP64 口径（HPC/TOP500 的通用口径）。CPU 与 GPU 分两条
        # 查询后在 Python 里相加——两族指标标签集不同，PromQL 里相加要写
        # on(instance) group_left()，错得很安静。
        "flops_fp64": metric_card(
            _sum_flops([_flops_map((results[7], "fp64")), _flops_map((results[8], "fp64"))])
            .get("fp64"),
            "FLOPS",
        ),
    }
    return cache_response(payload, max_age=request.app.state.settings.cards_cache_s)


async def _safe_range(
    request: Request, promql: str, start: datetime, end: datetime, step: str,
) -> list[dict[str, Any]]:
    """query_range 的异常兜底版本，与 `_safe_query` 同思路：失败返回空列表。"""
    try:
        return await _client(request).query_range(promql, start, end, step=step)
    except Exception:
        return []


def _range_to_series(rows: list[dict[str, Any]], digits: int) -> list[dict[str, Any]]:
    """query_range 结果 → [{time: 'HH:MM', value}, ...]。

    多条 series（多网卡/多链路）时取首条——聚合已在 PromQL 侧用 sum/avg 完成，
    这里正常只会拿到一条。
    """
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for ts, v in rows[0].get("values", []):
        try:
            out.append({
                "time": datetime.fromtimestamp(float(ts)).strftime("%H:%M"),
                "value": round(float(v), digits),
            })
        except (ValueError, TypeError):
            continue
    return out


@router.get("/bandwidth")
async def bandwidth(request: Request) -> JSONResponse:
    """24h 带宽趋势（5m 步进）：入向 / 出向两个方向的真实 rate。

    链路时延不在这张图里——它量级与带宽差几个数量级，压在一根轴上只会变成贴底的
    直线；时延改由 /api/metrics/qos 的 QoS 雷达呈现。
    """
    end = datetime.now()
    start = end - timedelta(hours=24)
    rx_r, tx_r = await asyncio.gather(
        _safe_range(request, Q_BANDWIDTH_RX_RANGE, start, end, "5m"),
        _safe_range(request, Q_BANDWIDTH_TX_RANGE, start, end, "5m"),
    )

    return cache_response(
        {"rx": _range_to_series(rx_r, 2), "tx": _range_to_series(tx_r, 2)},
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

    # 2. 集群峰值算力
    cluster_flops = await _cluster_flops(request)

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
            # 原始 FLOPS/s，由前端按 G/T/P/EFLOPS 格式化
            "flops": _sum_flops([cf.get("cpu", {}), cf.get("gpu", {})]),
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
            })

    return cache_response(
        {"clusters": clusters, "links": []},
        max_age=request.app.state.settings.charts_cache_s,
    )


@router.get("/links")
async def links_quality(request: Request) -> JSONResponse:
    """链路质量列表（时延 / 丢包率 / 额定速率）。

    数据源为 QoSController 暴露的 `qos_link_delay_ms`、`qos_link_loss_percent`
    与 `qos_link_capacity_bits`，是独立 series，按 (src_dpid, dst_dpid)
    合并为一条链路记录。双向链路会各自上报一条，此处保持原样不去重。

    注意：latency 是 netem 的**配置**时延，loss 是**实测**丢包率。
    """
    delay_results, loss_results, cap_results = await asyncio.gather(
        _safe_query(request, Q_LINKS_QUALITY),
        _safe_query(request, Q_LINKS_LOSS),
        _safe_query(request, Q_LINKS_CAPACITY),
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
                {
                    "source": src,
                    "target": dst,
                    "latency": None,
                    "loss": None,
                    "capacity_bps": None,
                },
            )[field] = value

    _collect(delay_results, "latency")
    _collect(loss_results, "loss")
    _collect(cap_results, "capacity_bps")

    return cache_response(
        {"items": list(links.values())},
        max_age=request.app.state.settings.charts_cache_s,
    )


def _robustness_detail(
    fail_ratio: float | None,
    conn_fails: float | None,
    conn_opens: float | None,
    listen_drops: float | None,
    no_routes: float | None,
    syn_retrans: float | None,
    rules_active: float | None,
) -> str:
    """「抗扰动」维度的说明文案。

    这是代理指标（主机侧 TCP 异常事件），不是真正的攻击检测结果，
    文案里明说口径，避免被当成安全态势读。
    """
    parts: list[str] = []
    if fail_ratio is not None and conn_fails is not None and conn_opens is not None:
        parts.append(
            f"TCP 连接失败率 {fail_ratio * 100:.1f}%"
            f"（{conn_fails:.2f}/{conn_opens:.2f} 次每秒）"
        )
    if listen_drops is not None:
        parts.append(f"监听队列丢弃 {listen_drops:.2f}/秒")
    if no_routes is not None:
        parts.append(f"无路由报文 {no_routes:.2f}/秒")
    if syn_retrans is not None:
        parts.append(f"SYN 重传 {syn_retrans:.2f}/秒（旁证，不计分）")
    if rules_active is not None:
        parts.append(f"已下发 QoS 规则 {int(rules_active)} 条")
    if not parts:
        return "暂无指标采集"
    return "代理指标：" + "，".join(parts)


@router.get("/qos")
async def qos_score(request: Request) -> JSONResponse:
    """网络 QoS 六维评分：全部由真实指标推导。

    评分口径统一为「0~100，越高越好」，阈值来自经验值而非标准，注释里写明依据。

    每个维度带 `measured` 标志：True 表示有真实指标支撑，False 表示该指标
    目前不存在、值是占位。前端据此在图例上标注，避免占位分被当成实测结果。

    「抗扰动」维度没有直接对应的攻击检测指标，用主机侧 TCP 异常计数器作代理
    ——指标真实存在且持续上报，口径写在 detail 里交代清楚。

    指标的实测/配置之分（务必看清，否则会把配置值当实测）：
    - 时延 `qos_link_delay_ms`：**配置值**（netem 参数）。被动采集拿不到 RTT，
      实测时延需要主动探测，尚未实现。
    - 丢包率 `qos_link_loss_percent`：**实测**（控制器用 tc -s 计数差分算出）。
      同一指标的 `qos_link_loss_configured_percent` 是 netem 配置值，仅作对照。
    - 带宽占用 `qos_link_bandwidth_usage_bits`：**实测**（OpenFlow 端口统计差分，
      单向 src→dst 口径）。额定值取 OVS 的 `qos_link_capacity_bits`。
    """
    (
        delay_rows, loss_rows, bw_rows, cap_rows, sw_rows, rule_rows,
        fail_rows, open_rows, listen_rows, route_rows, syn_rows, loss_cfg_rows,
    ) = await asyncio.gather(
        _safe_query(request, Q_LINKS_QUALITY),
        _safe_query(request, Q_LINKS_LOSS),
        _safe_query(request, Q_LINKS_BANDWIDTH),
        _safe_query(request, Q_LINKS_CAPACITY),
        _safe_query(request, Q_SWITCHES),
        _safe_query(request, Q_RULES_ACTIVE),
        _safe_query(request, Q_SEC_CONN_FAILS),
        _safe_query(request, Q_SEC_CONN_OPENS),
        _safe_query(request, Q_SEC_LISTEN_DROPS),
        _safe_query(request, Q_SEC_NO_ROUTES),
        _safe_query(request, Q_SEC_SYN_RETRANS),
        _safe_query(request, Q_LINKS_LOSS_CONFIGURED),
    )

    delays = [v for v in (_scalar([r]) for r in delay_rows) if v is not None]
    losses = [v for v in (_scalar([r]) for r in loss_rows) if v is not None]

    # 时延：线性扣分，0ms 满分、50ms 归零。实测均值 4.8ms / 峰值 22ms。
    avg_delay = sum(delays) / len(delays) if delays else None
    # 抖动用同批链路时延的离散度（标准差）代理——控制器没导出真抖动指标，
    # 但各链路时延本就该在同一量级，离散度越大说明时延越不稳。
    delay_std = (
        (sum((d - avg_delay) ** 2 for d in delays) / len(delays)) ** 0.5
        if avg_delay is not None and len(delays) > 1
        else None
    )
    avg_loss = sum(losses) / len(losses) if losses else None
    # qos_link_loss_configured_percent 是 netem 的配置丢包率，与实测值对照
    # 可以看出「配置的损伤有没有真的生效」
    loss_cfgs = [v for v in (_scalar([r]) for r in loss_cfg_rows) if v is not None]
    avg_loss_configured = sum(loss_cfgs) / len(loss_cfgs) if loss_cfgs else None
    # qos_link_bandwidth_usage_bits 是 bit/s，评分按 Mbps 口径（换算 ÷1e6）
    bandwidth_mbps = None if (bw := _scalar(bw_rows)) is None else bw / 1e6
    # 额定速率：优先用 OVS link_speed 实测的链路额定值（sum 与占用同口径）；
    # 拿不到时退回 Settings.network_rated_mbps 的全局假设
    capacity_mbps = None if (cap := _scalar(cap_rows)) is None else cap / 1e6
    rated_mbps = capacity_mbps or request.app.state.settings.network_rated_mbps
    rated_from_capacity = bool(capacity_mbps)
    switch_total = _scalar(sw_rows)
    rules_active = _scalar(rule_rows)

    # 抗扰动信号（每秒发生率，跨全部采集节点求和）
    conn_fails = _scalar(fail_rows)
    conn_opens = _scalar(open_rows)
    listen_drops = _scalar(listen_rows)
    no_routes = _scalar(route_rows)
    syn_retrans = _scalar(syn_rows)
    # 连接失败率 = 失败 / 新建连接，与业务流量规模无关，可跨时段比较
    fail_ratio = (
        conn_fails / conn_opens
        if conn_fails is not None and conn_opens and conn_opens > 0
        else None
    )

    def _lin(v: float | None, zero_at: float) -> float | None:
        """越高越好：v=0 满分，v≥zero_at 归零。"""
        return None if v is None else round(max(0.0, min(100.0, 100.0 * (1 - v / zero_at))), 1)

    # 抗扰动子分：连接失败率 20% 归零、监听丢弃 1 次/s 归零、无路由 10 包/s 归零。
    # 三项按 0.6/0.25/0.15 加权——失败率是主信号，监听丢弃是最硬的洪泛证据。
    sub_fail = _lin(fail_ratio, 0.20)
    sub_listen = _lin(listen_drops, 1.0)
    sub_route = _lin(no_routes, 10.0)
    robustness_parts = [(sub_fail, 0.6), (sub_listen, 0.25), (sub_route, 0.15)]
    # 失败率缺失说明核心信号不可得，此时不算分（避免用旁证拼出假分数）
    robustness = None
    if sub_fail is not None:
        present = [(s, w) for s, w in robustness_parts if s is not None]
        robustness = round(
            sum(s * w for s, w in present) / sum(w for _, w in present), 1
        )

    dims = [
        {"key": "latency", "label": "时延",
         "score": _lin(avg_delay, 50.0), "measured": avg_delay is not None,
         "detail": f"均值 {avg_delay:.1f} ms" if avg_delay is not None else "无数据"},
        {"key": "jitter", "label": "抖动",
         "score": _lin(delay_std, 20.0), "measured": delay_std is not None,
         "detail": f"时延标准差 {delay_std:.1f} ms" if delay_std is not None else "无数据"},
        {"key": "loss", "label": "丢包率",
         "score": _lin(avg_loss, 5.0), "measured": avg_loss is not None,
         "detail": (
             f"实测均值 {avg_loss:.2f} %"
             + (f"（netem 配置 {avg_loss_configured:.2f} %）"
                if avg_loss_configured is not None else "")
         ) if avg_loss is not None else "无数据"},
        # 控制器活着 = 链路可用。target 数拿不到（ofctl_rest 未加载），
        # 故只按交换机数量是否上报来判断，不编造具体百分比。
        {"key": "availability", "label": "可用性",
         "score": 100.0 if switch_total else 0.0, "measured": switch_total is not None,
         "detail": f"交换机 {int(switch_total)} 台在线" if switch_total else "无数据"},
        # 带宽余量 = 1 - 占用/额定。
        # 额定值优先用 OVS Interface link_speed 上报的链路额定速率
        # （qos_link_capacity_bits，与占用同为「全部有向边之和」，口径一致）；
        # 拿不到时才退回 Settings.network_rated_mbps 的全局假设
        # （环境变量 NETWORK_RATED_MBPS 可覆盖）。
        # 占用超过额定时线性扣到 0，不出现负分。
        {"key": "bandwidth", "label": "带宽余量",
         "score": None if bandwidth_mbps is None else
             round(max(0.0, min(100.0, 100.0 * (1 - bandwidth_mbps / rated_mbps))), 1),
         "measured": bandwidth_mbps is not None,
         "detail": (f"占用 {bandwidth_mbps:.2f} / 额定 {rated_mbps:.0f} Mbps"
                    f"（{'OVS link_speed' if rated_from_capacity else '全局假设'}）")
                   if bandwidth_mbps is not None else "无数据"},
        # 抗扰动：用主机侧 TCP 异常事件代理，没有真正的攻击检测指标。
        # 旁证信号缺失时按剩余权重归一；核心信号（失败率）缺失则整个维度不给分。
        {"key": "robustness", "label": "抗扰动",
         "score": robustness, "measured": robustness is not None,
         "detail": _robustness_detail(
             fail_ratio, conn_fails, conn_opens,
             listen_drops, no_routes, syn_retrans, rules_active,
         )},
    ]

    return cache_response({"dimensions": dims}, max_age=request.app.state.settings.cards_cache_s)


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


async def _node_hostnames(
    request: Request, instances: list[str] | None = None
) -> dict[str, str]:
    """instance(ip:port) → 主机名。

    本项目所有服务器都挂在同一批 IP 上，靠端口映射区分（instance 形如
    `101.34.209.104:9111`），界面若只显示 IP，同一 IP 下的多台机器长得
    一模一样。主机名来自采集器上报的 `node_uname_info{nodename=...}`，
    是同 IP 不同端口之间唯一稳定的区分标识。

    一次查询取回全部 `node_uname_info` series（每个 exporter 目标一条），
    再按 instance 过滤，避免逐台查询（N 次 HTTP 请求）。
    `instances` 为 None 时返回全部映射。
    """
    rows = await _safe_query(request, "node_uname_info")
    wanted = set(instances) if instances is not None else None
    out: dict[str, str] = {}
    for r in rows:
        metric = r.get("metric") or {}
        inst = metric.get("instance")
        name = metric.get("nodename")
        if not inst or not name:
            continue
        if wanted is not None and inst not in wanted:
            continue
        out[inst] = name
    return out


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

    # 各集群节点的主机名：端口映射下多台服务器共用同一 IP，主机名是唯一能区分
    # 同集群内不同节点的标识（集群列表里光看 IP 只会看到一堆重复地址）。
    all_hostnames = await _node_hostnames(
        request, [(t.get("labels") or {}).get("instance", "") for t in node_targets]
    )
    hostnames_by_cluster: dict[str, list[str]] = {}
    for t in node_targets:
        labels = t.get("labels") or {}
        cid = labels.get("cluster", "")
        name = all_hostnames.get(labels.get("instance", ""))
        if cid and name:
            hostnames_by_cluster.setdefault(cid, []).append(name)

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
            # 节点主机名列表（界面按主机名区分同 IP 的不同服务器）
            "node_hostnames": sorted(hostnames_by_cluster.get(cid, [])),
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

    # 主机名：端口映射下同 IP 多台服务器，界面靠它区分（instance 只显示 IP 会重复）
    hostnames = await _node_hostnames(request, [n["instance"] for n in cluster_nodes])

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
            "hostname": hostnames.get(n["instance"], ""),
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
        *(cli.query_range(pql, start, end, step=step) for _, pql, _ in metric_specs),
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

    # URL 可能是裸 IP（旧链接），instance 标签带端口，这里复用已取到的
    # targets 列表补全，不额外发请求。端口映射下同 IP 多台机器，优先取在线的。
    if instance and ":" not in instance:
        instance = _match_instance_by_ip(node_targets, instance) or instance

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

    uname_map = _info(uname_info)

    # 同集群其他主机（用于下拉框切换）。按 instance 排序。
    cluster_host_rows = [
        {
            "instance": (t.get("labels") or {}).get("instance", ""),
            "health": t.get("health") or "unknown",
        }
        for t in node_targets
        if (t.get("labels") or {}).get("cluster") == cid
        and (t.get("labels") or {}).get("instance")
    ]
    # 主机名：端口映射下同 IP 多台服务器，下拉框只显示 IP 会分不清
    cluster_hostnames = await _node_hostnames(
        request, [h["instance"] for h in cluster_host_rows]
    )

    payload = {
        "instance": instance,
        # 主机名来自 node_uname_info{nodename=...}，同 IP 不同端口下用它区分
        "hostname": uname_map.get("nodename", ""),
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
        "uname": uname_map,
        "os": _info(os_info),
        "dmi": _info(dmi_info),

        # 算力：原始 FLOPS/s，按精度分键（fp32/fp64/fp16）。fp16 仅 GPU 有。
        "flops_peak": _sum_flops([hf["cpu"], hf["gpu"]]),

        # 同集群其他主机（用于下拉框切换）。按 instance 排序。
        "cluster_hosts": sorted(
            [
                {
                    "instance": h["instance"],
                    "hostname": cluster_hostnames.get(h["instance"], ""),
                    "health": h["health"],
                }
                for h in cluster_host_rows
            ],
            key=lambda x: x["instance"],
        ),
    }
    return cache_response(payload, max_age=request.app.state.settings.cards_cache_s)
