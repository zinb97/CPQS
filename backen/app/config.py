"""应用配置（dataclass + 环境变量）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """应用运行时配置。可通过环境变量覆盖默认值。"""

    prometheus_url: str = "http://192.168.10.31:9090"
    cluster_meta_path: str = "config/cluster_locations.yaml"

    # HTTP 客户端
    request_timeout_s: float = 3.0

    # 网络额定带宽（Mbps）。QoS 雷达的「带宽余量」维度需要它做基准：
    # 控制器只上报当前占用 qos_link_bandwidth_usage_bits，不含链路额定速率，
    # 没有基准就算不出余量。1000 Mbps 对应千兆口，是当前网络的合理假设。
    network_rated_mbps: float = 1000.0

    # 前端轮询周期（仅记录用，实际由前端 JS 控制）
    cards_refresh_s: int = 5
    charts_refresh_s: int = 30

    # 后端响应缓存时长（前端可据此跳过未过期轮询）
    cards_cache_s: int = 4
    charts_cache_s: int = 28

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            prometheus_url=os.getenv("PROMETHEUS_URL", cls.prometheus_url),
            cluster_meta_path=os.getenv("CLUSTER_META_PATH", cls.cluster_meta_path),
            request_timeout_s=float(os.getenv("REQUEST_TIMEOUT_S", cls.request_timeout_s)),
            network_rated_mbps=float(
                os.getenv("NETWORK_RATED_MBPS", cls.network_rated_mbps)
            ),
        )