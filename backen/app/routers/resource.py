"""资源管控中心页面路由：集群列表 / 集群详情 / 主机详情 / Grafana 总览。

所有页面均挂载在 `/resource` 路径下，与侧边栏"资源管控中心"菜单共享激活态。
子项导航由 `base.html` 侧边栏渲染（menu.children），无顶部 tab 栏。

数据全部来自 Prometheus `/api/v1/targets` + `cluster_locations.yaml`，
首屏 SSR 直接渲染（避免前端空闪），后续通过 `/api/metrics/...` 轮询刷新。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.menu import base_context


router = APIRouter()


@router.get("/resource", response_class=RedirectResponse)
async def resource_index() -> RedirectResponse:
    """`/resource` 直接重定向到集群列表（侧边栏入口）。"""
    return RedirectResponse(url="/resource/clusters", status_code=307)


@router.get("/resource/clusters", response_class=HTMLResponse)
async def clusters_page(request: Request) -> HTMLResponse:
    """集群列表页。"""
    return request.app.state.templates.TemplateResponse(
        request,
        "clusters.html",
        {
            **base_context(request),
            "title": "集群列表 - 资源管控中心",
        },
    )


@router.get("/resource/clusters/{cluster_id}", response_class=HTMLResponse)
async def cluster_detail_page(cluster_id: str, request: Request) -> HTMLResponse:
    """单集群详情页。"""
    meta = (request.app.state.cluster_meta or {}).get(cluster_id, {})
    return request.app.state.templates.TemplateResponse(
        request,
        "cluster_detail.html",
        {
            **base_context(request),
            "title": f"集群 {meta.get('name', cluster_id)} - 资源管控中心",
            "cluster_id": cluster_id,
        },
    )


@router.get("/resource/hosts/{instance:path}", response_class=HTMLResponse)
async def host_detail_page(instance: str, request: Request) -> HTMLResponse:
    """单主机详情页。"""
    return request.app.state.templates.TemplateResponse(
        request,
        "host_detail.html",
        {
            **base_context(request),
            "title": f"主机 {instance} - 资源管控中心",
            "instance": instance,
        },
    )


@router.get("/resource/iframe", response_class=HTMLResponse)
async def resource_iframe_page(request: Request) -> HTMLResponse:
    """资源管控中心 - Grafana 总览（保留原 Grafana iframe）。"""
    return request.app.state.templates.TemplateResponse(
        request,
        "resource_iframe.html",
        {
            **base_context(request),
            "title": "Grafana 总览 - 资源管控中心",
            "iframe_url": "http://192.168.10.31:3000/goto/cg07s2sm4a134a?orgId=default",
            "sandbox": "allow-scripts allow-same-origin allow-forms",
        },
    )
