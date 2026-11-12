"""iframe 壳路由：4 个外部子系统的页面。

页面内容由各自子系统服务控制，本路由仅渲染一个全屏 iframe + sandbox 属性。

外部子系统的地址不写死在这里，而是放在 `Settings`（环境变量可覆盖），
因为 backen 不做反向代理——浏览器必须能直连目标地址，换部署环境时
改环境变量即可，不必改代码重新构建。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.menu import base_context


router = APIRouter()


# 外部子系统配置（路径 → {标题, Settings 字段名, sandbox}）
# 顺序与侧边栏菜单一一对应。`/resource` 不在此处：
# 资源管控中心改由 `routers/resource.py` 提供原生页面（集群列表/详情/主机详情）。
#
# `url_key` 是 `Settings` 的字段名，实际 URL 在请求时从
# `request.app.state.settings` 取，因此环境变量改了立即生效。
SHELLS = {
    "/network": {
        "title": "网络管控中心",
        "url_key": "shell_network_url",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
    "/task": {
        "title": "任务调度中心",
        "url_key": "shell_task_url",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
    "/telemetry": {
        "title": "带内全网遥测",
        "url_key": "shell_telemetry_url",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
    "/compute": {
        "title": "轻量算力感知",
        "url_key": "shell_compute_url",
        "sandbox": "allow-scripts allow-same-origin allow-forms allow-popups",
    },
}


def register_shell_routes(parent: APIRouter) -> None:
    """为每个 SHELL 配置注册 GET 路由。"""
    for path, cfg in SHELLS.items():
        # 使用闭包捕获 cfg，避免循环变量问题
        cfg_snapshot = dict(cfg)

        async def _render(request: Request, _cfg: dict = cfg_snapshot) -> HTMLResponse:
            settings = request.app.state.settings
            return request.app.state.templates.TemplateResponse(
                request,
                "shell.html",
                {
                    **base_context(request),
                    "title": _cfg["title"],
                    "iframe_url": getattr(settings, _cfg["url_key"]),
                    "sandbox": _cfg["sandbox"],
                },
            )

        parent.add_api_route(path, _render, methods=["GET"], response_class=HTMLResponse)


# 自动注册
register_shell_routes(router)