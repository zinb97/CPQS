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

    # 网络额定带宽（Mbps）。**仅作兜底**：QoS 雷达的「带宽余量」维度优先用
    # 控制器上报的 qos_link_capacity_bits（来自 OVS Interface 的 link_speed，
    # 即链路的真实协商速率）；拿不到时才退回这里的全局假设值。
    # 1000 Mbps 对应千兆口，是当前网络在没有 OVS 速率时的合理假设。
    network_rated_mbps: float = 1000.0

    # 前端轮询周期（仅记录用，实际由前端 JS 控制）
    cards_refresh_s: int = 5
    charts_refresh_s: int = 30

    # 后端响应缓存时长（前端可据此跳过未过期轮询）
    cards_cache_s: int = 4
    charts_cache_s: int = 28

    # ===== /health 浏览器侧时延测量 =====
    # 每台服务器采样次数（页面侧取中位数作为代表值）
    health_probe_samples: int = 3

    # ===== iframe 壳的外部子系统地址 =====
    # backen 不做反向代理，浏览器必须能直连这些地址才能渲染 iframe。
    # 默认值对应当前部署环境：
    #   - 101.34.209.104 是 NAT 后的统一入口（与 prometheus.yml 里的
    #     node-* target 同一批地址，端口是映射后的对外端口）
    #   - 192.168.10.31 是实验室内部直连地址，只有内网可达
    # 换环境（内外网、端口映射变更）时用对应的环境变量覆盖，不要改代码。
    shell_network_url: str = "http://192.168.10.31:8080/"
    shell_task_url: str = "http://101.34.209.104:32000/cluster-manage"
    shell_telemetry_url: str = "http://101.34.209.104:39280/pages/vis.html"
    shell_compute_url: str = "http://101.34.209.104:8765"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            prometheus_url=os.getenv("PROMETHEUS_URL", cls.prometheus_url),
            cluster_meta_path=os.getenv("CLUSTER_META_PATH", cls.cluster_meta_path),
            request_timeout_s=float(os.getenv("REQUEST_TIMEOUT_S", cls.request_timeout_s)),
            network_rated_mbps=float(
                os.getenv("NETWORK_RATED_MBPS", cls.network_rated_mbps)
            ),
            shell_network_url=os.getenv("SHELL_NETWORK_URL", cls.shell_network_url),
            shell_task_url=os.getenv("SHELL_TASK_URL", cls.shell_task_url),
            shell_telemetry_url=os.getenv(
                "SHELL_TELEMETRY_URL", cls.shell_telemetry_url
            ),
            shell_compute_url=os.getenv("SHELL_COMPUTE_URL", cls.shell_compute_url),
            health_probe_samples=int(
                os.getenv("HEALTH_PROBE_SAMPLES", cls.health_probe_samples)
            ),
        )