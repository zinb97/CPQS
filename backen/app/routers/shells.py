"""iframe 壳路由：5 个外部子系统的页面。

页面内容由各自子系统服务控制，本路由仅渲染一个全屏 iframe + sandbox 属性。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.menu import base_context


router = APIRouter()


# 外部子系统配置（路径 → {标题, iframe URL, sandbox}）
# 顺序与侧边栏菜单一一对应。`/resource` 不在此处：
# 资源管控中心改由 `routers/resource.py` 提供原生页面（集群列表/详情/主机详情）。
SHELLS = {
    "/network": {
        "title": "网络管控中心",
        "iframe_url": "http://192.168.10.31:8080/",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
    "/task": {
        "title": "任务调度中心",
        "iframe_url": "http://192.168.10.21:32000/cluster-manage",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
    "/telemetry": {
        "title": "带内全网遥测",
        "iframe_url": "http://192.168.10.17:39280/pages/vis.html",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
    "/compute": {
        "title": "轻量算力感知",
        "iframe_url": "http://192.168.10.17:8765",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
}


def register_shell_routes(parent: APIRouter) -> None:
    """为每个 SHELL 配置注册 GET 路由。"""
    for path, cfg in SHELLS.items():
        # 使用闭包捕获 cfg，避免循环变量问题
        cfg_snapshot = dict(cfg)

        async def _render(request: Request, _cfg: dict = cfg_snapshot) -> HTMLResponse:
            return request.app.state.templates.TemplateResponse(
                request,
                "shell.html",
                {
                    **base_context(request),
                    "title": _cfg["title"],
                    "iframe_url": _cfg["iframe_url"],
                    "sandbox": _cfg["sandbox"],
                },
            )

        parent.add_api_route(path, _render, methods=["GET"], response_class=HTMLResponse)


# 自动注册
register_shell_routes(router)