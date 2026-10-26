"""Dashboard 路由：SSR 渲染首页 + 初始快照数据。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.menu import base_context
from app.prometheus.client import PrometheusClient
from app.prometheus.queries import Q_BANDWIDTH_RX_RANGE, Q_BANDWIDTH_TX_RANGE


router = APIRouter()


def _to_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """query_range 结果 → [{time: 'HH:MM', value}, ...]；空或失败返回 []。"""
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for ts, v in rows[0].get("values", []):
        try:
            out.append({
                "time": datetime.fromtimestamp(float(ts)).strftime("%H:%M"),
                "value": round(float(v), 2),
            })
        except (ValueError, TypeError):
            continue
    return out


async def _network_trend(client: PrometheusClient) -> dict[str, list[dict[str, Any]]]:
    """24h 带宽趋势首屏快照：入向 / 出向。"""
    end = datetime.now()
    start = end - timedelta(hours=24)

    async def one(promql: str) -> list[dict[str, Any]]:
        try:
            return await client.query_range(promql, start, end, step="5m")
        except Exception:
            return []

    rx, tx = await asyncio.gather(one(Q_BANDWIDTH_RX_RANGE), one(Q_BANDWIDTH_TX_RANGE))
    return {"rx": _to_series(rx), "tx": _to_series(tx)}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard 首页（SSR，含初始快照，避免前端首屏空白）。"""
    client: PrometheusClient = request.app.state.prom
    settings = request.app.state.settings

    initial_bandwidth: dict[str, list[dict[str, Any]]] = {"rx": [], "tx": []}
    prom_ok = True
    try:
        if not await client.health():
            prom_ok = False
        else:
            initial_bandwidth = await _network_trend(client)
    except Exception:
        prom_ok = False

    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **base_context(request),
            "title": "算力网络服务质量控制原型系统",
            "prom_ok": prom_ok,
            "prometheus_url": settings.prometheus_url,
            "cards_refresh_s": settings.cards_refresh_s,
            "charts_refresh_s": settings.charts_refresh_s,
            "initial_bandwidth": initial_bandwidth,
        },
    )
