"""Prometheus HTTP API 异步客户端。

封装 `/api/v1/query` 与 `/api/v1/query_range`，所有异常路径返回安全空值，让调用端用空状态展示而不是抛错。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class PrometheusClient:
    def __init__(self, base_url: str, timeout_s: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        """快速健康检查（访问 labels endpoint）。失败返回 False。"""
        try:
            r = await self._client.get(f"{self.base_url}/api/v1/labels", timeout=1.5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def query(self, promql: str) -> list[dict[str, Any]]:
        """即时查询。返回原始 result 数组（空列表表示无数据或失败）。"""
        r = await self._client.get(
            f"{self.base_url}/api/v1/query",
            params={"query": promql},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            return []
        return body.get("data", {}).get("result", []) or []

    async def targets(self) -> list[dict[str, Any]]:
        """获取 Prometheus 当前活跃抓取目标列表。

        等价于 `GET /api/v1/targets`，返回 `data.activeTargets` 数组。
        每个元素含 `labels`（job/cluster/instance）与 `health`（up/down）。
        失败或 `status != "success"` 时返回空列表。
        """
        r = await self._client.get(f"{self.base_url}/api/v1/targets")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            return []
        return body.get("data", {}).get("activeTargets", []) or []

    async def query_range(
        self,
        promql: str,
        start: datetime,
        end: datetime,
        step: str = "5m",
    ) -> list[dict[str, Any]]:
        """范围查询。返回原始 result 数组（每个元素是一个 series，包含 values 列表）。"""
        r = await self._client.get(
            f"{self.base_url}/api/v1/query_range",
            params={
                "query": promql,
                "start": f"{start.timestamp():.3f}",
                "end": f"{end.timestamp():.3f}",
                "step": step,
            },
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            return []
        return body.get("data", {}).get("result", []) or []

    async def __aenter__(self) -> "PrometheusClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()