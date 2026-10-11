"""侧边栏菜单配置 + SVG 图标。

每项含 id / label / path / icon（内联 SVG 字符串）/ 可选 children（子菜单）。
供 Jinja2 模板直接渲染，模板只需遍历 menu_items 即可。
"""
from __future__ import annotations

from typing import TypedDict


class MenuItem(TypedDict, total=False):
    id: str
    label: str
    path: str
    icon: str  # 内联 SVG
    children: list["MenuItem"]  # 可选：子菜单


_DASHBOARD_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="3" y="3" width="7" height="7" rx="1"/>'
    '<rect x="14" y="3" width="7" height="7" rx="1"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1"/>'
    '<rect x="14" y="14" width="7" height="7" rx="1"/>'
    "</svg>"
)
_RESOURCE_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="2" y="3" width="20" height="6" rx="1"/>'
    '<rect x="2" y="11" width="20" height="6" rx="1"/>'
    '<circle cx="6" cy="6" r="1"/>'
    '<circle cx="6" cy="14" r="1"/>'
    "</svg>"
)
_NETWORK_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<circle cx="12" cy="5" r="3"/>'
    '<circle cx="5" cy="19" r="3"/>'
    '<circle cx="19" cy="19" r="3"/>'
    '<line x1="12" y1="8" x2="12" y2="12"/>'
    '<line x1="12" y1="12" x2="5" y2="16"/>'
    '<line x1="12" y1="12" x2="19" y2="16"/>'
    "</svg>"
)
_TASK_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="3" y="4" width="18" height="18" rx="2"/>'
    '<line x1="16" y1="2" x2="16" y2="6"/>'
    '<line x1="8" y1="2" x2="8" y2="6"/>'
    '<line x1="3" y1="10" x2="21" y2="10"/>'
    "</svg>"
)
_TELEMETRY_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'
    "</svg>"
)
_COMPUTE_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="4" y="4" width="16" height="16" rx="2"/>'
    '<rect x="9" y="9" width="6" height="6"/>'
    '<line x1="9" y1="1" x2="9" y2="4"/>'
    '<line x1="15" y1="1" x2="15" y2="4"/>'
    '<line x1="9" y1="20" x2="9" y2="23"/>'
    '<line x1="15" y1="20" x2="15" y2="23"/>'
    '<line x1="20" y1="9" x2="23" y2="9"/>'
    '<line x1="20" y1="14" x2="23" y2="14"/>'
    '<line x1="1" y1="9" x2="4" y2="9"/>'
    '<line x1="1" y1="14" x2="4" y2="14"/>'
    "</svg>"
)
_CLUSTERS_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="3" y="3" width="7" height="7" rx="1"/>'
    '<rect x="14" y="3" width="7" height="7" rx="1"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1"/>'
    '<rect x="14" y="14" width="7" height="7" rx="1"/>'
    "</svg>"
)
_GRAFANA_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<polyline points="3 17 9 11 13 15 21 7"/>'
    '<polyline points="14 7 21 7 21 14"/>'
    "</svg>"
)


MENU_ITEMS: list[MenuItem] = [
    {"id": "dashboard", "label": "态势总览大屏", "path": "/", "icon": _DASHBOARD_SVG},
    {
        "id": "resource",
        "label": "资源管控中心",
        "path": "/resource",
        "icon": _RESOURCE_SVG,
        "children": [
            {"id": "clusters", "label": "集群列表", "path": "/resource/clusters", "icon": _CLUSTERS_SVG},
            {"id": "resource_iframe", "label": "Grafana 总览", "path": "/resource/iframe", "icon": _GRAFANA_SVG},
        ],
    },
    {"id": "network", "label": "网络管控中心", "path": "/network", "icon": _NETWORK_SVG},
    {"id": "task", "label": "任务调度中心", "path": "/task", "icon": _TASK_SVG},
    {"id": "telemetry", "label": "带内全网遥测", "path": "/telemetry", "icon": _TELEMETRY_SVG},
    {"id": "compute", "label": "轻量算力感知", "path": "/compute", "icon": _COMPUTE_SVG},
]


def base_context(request) -> dict:
    """构造所有页面共享的模板上下文（菜单 + 当前路径）。"""
    return {
        "request": request,
        "menu_items": MENU_ITEMS,
        "current_path": request.url.path,
    }
