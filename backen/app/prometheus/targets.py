"""Prometheus 目标枚举的共享 helper。

`api.py` 与 `health.py` 都需要「当前有哪些采集器目标 / 目标对应哪台主机」，
抽到这里避免两处各写一遍（口径不一致时很容易看错机器）。
"""
from __future__ import annotations

from typing import Any

from app.prometheus.client import PrometheusClient


# 采集器 job 的前缀：Prometheus 里每个物理服务器对应一个 node-* 目标。
NODE_JOB_PREFIX = "node-"


async def list_node_targets(client: PrometheusClient) -> list[dict[str, Any]]:
    """取 `/api/v1/targets` 中 `job` 以 `node-` 开头的目标列表。

    失败返回空列表（与项目其它查询端点的兜底策略一致：宁可空状态，不抛错）。
    """
    try:
        targets = await client.targets()
    except Exception:
        return []
    return [t for t in targets if (t.get("labels") or {}).get("job", "").startswith(NODE_JOB_PREFIX)]


async def node_hostnames(
    client: PrometheusClient, instances: list[str] | None = None
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
    try:
        rows = await client.query("node_uname_info")
    except Exception:
        return {}
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
