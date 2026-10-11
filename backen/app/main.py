"""FastAPI 应用入口（uvicorn app.main:app）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.prometheus.client import PrometheusClient
from app.prometheus.queries import load_cluster_locations
from app.routers import api as api_router
from app.routers import dashboard as dashboard_router
from app.routers import resource as resource_router
from app.routers import shells as shells_router


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子：初始化 Prometheus 客户端、加载集群元数据。"""
    settings: Settings = app.state.settings
    app.state.prom = PrometheusClient(
        base_url=settings.prometheus_url,
        timeout_s=settings.request_timeout_s,
    )
    app.state.cluster_meta = load_cluster_locations(
        PROJECT_DIR / settings.cluster_meta_path,
    )
    try:
        yield
    finally:
        await app.state.prom.close()


def create_app() -> FastAPI:
    settings = Settings.from_env()

    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    app = FastAPI(
        title="算力网络 QoS 控制原型",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.templates = templates

    app.mount(
        "/static",
        StaticFiles(directory=BASE_DIR / "static"),
        name="static",
    )

    app.include_router(dashboard_router.router)
    app.include_router(shells_router.router)
    app.include_router(resource_router.router)
    app.include_router(api_router.router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
    )