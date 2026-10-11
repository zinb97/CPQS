"""Dashboard 路由：SSR 渲染首页 + 初始快照数据。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.menu import base_context
from app.prometheus.client import PrometheusClient
from app.prometheus.queries import Q_BANDWIDTH_RANGE


router = APIRouter()


async def _bandwidth_series(client: PrometheusClient) -> list[tuple[str, float]]:
    """拉取 24h 带宽趋势（前端首屏填充，避免空白图）。"""
    end = datetime.now()
    start = end - timedelta(hours=24)
    try:
        result = await client.query_range(Q_BANDWIDTH_RANGE, start, end, step="5m")
    except Exception:
        return []
    if not result:
        return []
    values = result[0].get("values", [])
    out: list[tuple[str, float]] = []
    for ts, v in values:
        try:
            out.append((
                datetime.fromtimestamp(float(ts)).strftime("%H:%M"),
                float(v),
            ))
        except (ValueError, TypeError):
            continue
    return out


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard 首页（SSR，含初始快照，避免前端首屏空白）。"""
    client: PrometheusClient = request.app.state.prom
    settings = request.app.state.settings

    initial_bandwidth: list[tuple[str, float]] = []
    prom_ok = True
    try:
        if not await client.health():
            prom_ok = False
        else:
            initial_bandwidth = await _bandwidth_series(client)
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
            "initial_bandwidth": [
                {"time": t, "value": v} for t, v in initial_bandwidth
            ],
        },
    )