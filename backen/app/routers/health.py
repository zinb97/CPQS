"""`GET /health`：**浏览器侧**实测到各服务器的时延。

为什么只能由浏览器测：
    「运行浏览器的设备 → 各台服务器」这条路径，服务器端是测不到的——那是客户端的
    网络出口（可能走不同的出口、不同的 NAT、不同的运营商链路）。服务器能测的只有
    「backen → 各台服务器」，那是另一条路径，数值没有可比性。

    因此本模块只提供两样东西：
      1. `/health/targets` —— 探测目标清单（各服务器的地址与探测 URL）；
      2. `/health` —— 测量页，页面里的 JS 自己发请求、自己计时。

测量的做法（页面侧，见 static/js/health.js）：
    浏览器用 `fetch(url, {mode:'no-cors'})` 请求采集器的 `GET /health`（返回 `OK`），
    用 `performance.now()` 计时，每台采样若干次取中位数。

    用采集器的 `/health` 而不是 `/metrics`：前者几十字节，后者几百 KB——下载那么多
    数据会反过来干扰并发探测的结果。

限制：
    JavaScript 不能建裸 TCP 连接、不能发 ICMP，浏览器只能发 HTTP 请求。所以测到的
    时延含 DNS 解析 + TCP 握手 + HTTP 首字节，**不等于纯 ICMP RTT**。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.menu import base_context
from app.prometheus.targets import list_node_targets, node_hostnames


router = APIRouter()


def _split_instance(instance: str) -> tuple[str, int] | None:
    """`ip:port` → (ip, port)。格式不对返回 None（跳过该目标）。"""
    if not instance or ":" not in instance:
        return None
    host, _, port_s = instance.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        return None
    if not host or not (0 < port < 65536):
        return None
    return host, port


@router.get("/health/targets")
async def health_targets(request: Request) -> JSONResponse:
    """浏览器侧探测用的目标清单。

    探测地址用采集器的 `GET /health`（返回 `OK`）：采集器本来就在每个 exporter
    端口上提供该路径，用它计时可以避免为了测个 RTT 去下载几百 KB 的 `/metrics`。
    """
    client = request.app.state.prom
    targets = await list_node_targets(client)
    instances = [(t.get("labels") or {}).get("instance", "") for t in targets]
    hostnames = await node_hostnames(client, [i for i in instances if i])

    items: list[dict[str, Any]] = []
    for t in targets:
        labels = t.get("labels") or {}
        instance = labels.get("instance", "")
        parsed = _split_instance(instance)
        if parsed is None:
            continue
        host, port = parsed
        items.append({
            "hostname": hostnames.get(instance, ""),
            "cluster": labels.get("cluster", ""),
            "instance": instance,
            "address": host,
            "port": port,
            # 探测用地址：采集器的轻量健康端点
            "url": f"http://{host}:{port}/health",
            "prom_health": t.get("health", ""),
        })
    return JSONResponse({"count": len(items), "targets": items})


@router.get("/health", response_class=HTMLResponse)
@router.get("/health/browser", response_class=HTMLResponse)
async def health_browser(request: Request) -> HTMLResponse:
    """浏览器侧时延测量页：时延由**运行浏览器的设备**实测。

    页面加载后自行拉取 `/health/targets`，再逐个向各采集器发请求计时。
    """
    return request.app.state.templates.TemplateResponse(
        request,
        "health.html",
        {
            **base_context(request),
            "title": "服务器时延 · 本机测量",
            "probe_samples": request.app.state.settings.health_probe_samples,
        },
    )
