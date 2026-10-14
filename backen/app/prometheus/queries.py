"""PromQL 常量与集群元数据加载。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


# ===== Dashboard 9 个指标卡 =====
# 集群数 / 总节点数 / 在线节点数 由 `/api/v1/targets` 推导（PrometheusClient.targets），
# 不再用 PromQL（up{job=~"node-.*"} 系列）。
Q_CPU_CORES = "sum(node_cpu_cores)"
Q_MEMORY_TOTAL = "sum(node_memory_MemTotal_bytes{})"
Q_SWITCHES = "qos_switches_count"
Q_LINKS = "qos_links_count"
Q_HOSTS = "qos_hosts_count"
Q_PACKET_IN = "sum(qos_switch_packet_in_total)"


# ===== 地图数据 =====
# 各集群在线节点数与集群列表均由 /api/v1/targets 推导（PrometheusClient.targets），
# 不再用 PromQL（up{job=~"node-.*"} 系列）。
# 各集群总算力（占位：实际 PromQL 需 collector 暴露 qos_cluster_total_flops 之类指标）
Q_CLUSTERS_FLOPS = "sum by(cluster) (qos_cluster_total_flops)"
Q_CLUSTERS_USED_FLOPS = "sum by(cluster) (qos_cluster_used_flops)"


# ===== 带宽趋势（24h，5m 步进）=====
# 源指标为 bytes/s：×8 转 bits/s、÷1e6 转 Mbps，与 dashboard 的 Mbps 口径一致。
Q_BANDWIDTH_RANGE = "sum(rate(node_network_transmit_bytes_total[5m])) * 8 / 1000000"
Q_LATENCY_RANGE = "qos_link_latency_ms"


# ===== 网络链路质量 =====
# 由 QoSController 的 /metrics 暴露，链路双向各上报一条 series。
# 注意标签名是 src_dpid/dst_dpid，不是 source/target。
Q_LINKS_QUALITY = "qos_link_delay_ms"
Q_LINKS_LOSS = "qos_link_loss_percent"


# ===== 告警事件 =====
Q_EVENTS = "ALERTS"


# ===== 资源管控中心页面 PromQL 模板 =====
# collector 暴露的指标命名遵循 Prometheus node_exporter 风格：
#   node_cpu_seconds_total{mode="..."}      CPU 各模式累计秒（全核心聚合，无 cpu 标签）
#   node_cpu_cores                          CPU 核心数（collector 新增，替代按 cpu 标签 count）
#   node_cpu_info{model_name,max_mhz,min_mhz,sockets,cores_per_socket,
#                 physical_cores,threads_per_core}   CPU 型号/频率/拓扑（全 1 条 series）
#   node_memory_MemTotal_bytes / MemAvailable_bytes / MemFree_bytes
#   node_load1 / node_load5 / node_load15
#   node_filesystem_size_bytes / node_filesystem_avail_bytes
#   node_thermal_zone_temp{zone, type}
#   node_network_receive_bytes_total / node_network_transmit_bytes_total
#   node_uname_info{...} / node_os_info{...} / node_dmi_info{...}
#   node_procs_running / node_procs_blocked / node_boot_time_seconds
#
# scrape 配置会注入 labels.cluster（每个节点归到自己的集群）和 labels.instance。
# 集群聚合用 `by(cluster)`，节点级过滤用 `{instance="..."}` 或 `{cluster="..."}`。


def q_cluster_cpu_usage(cluster_id: str) -> str:
    """集群 CPU 平均使用率（1m 窗口，0~1）。

    `node_cpu_seconds_total` 是全核心累加值，idle 的 rate 取值域为 [0, 核心数]，
    必须除以 node_cpu_cores 归一化回 [0,1] 后再做 1-x。
    除法两侧标签集不同：rate() 只去掉 __name__，仍保留 mode 标签，
    而 node_cpu_cores 没有 mode，所以必须写 ignoring(mode) 否则匹配为空。
    """
    return (
        f'avg by(cluster) (1 - ('
        f'rate(node_cpu_seconds_total{{cluster="{cluster_id}",mode="idle"}}[1m])'
        f' / ignoring(mode) node_cpu_cores{{cluster="{cluster_id}"}}'
        f'))'
    )

def q_cluster_mem_usage(cluster_id: str) -> str:
    """集群内存使用率：1 - MemAvailable / MemTotal。"""
    return (
        f'avg by(cluster) (1 - '
        f'node_memory_MemAvailable_bytes{{cluster="{cluster_id}"}}'
        f'/ node_memory_MemTotal_bytes{{cluster="{cluster_id}"}})'
    )


def q_cluster_load(cluster_id: str) -> str:
    """集群 1 分钟负载均值。"""
    return f'avg by(cluster) (node_load1{{cluster="{cluster_id}"}})'


def q_cluster_cpu_cores(cluster_id: str) -> str:
    """集群 CPU 核心总数（各节点 cores 之和）。"""
    return f'sum by(cluster) (node_cpu_cores{{cluster="{cluster_id}"}})'


def q_cluster_mem_total(cluster_id: str) -> str:
    """集群内存总字节数。"""
    return f'sum by(cluster) (node_memory_MemTotal_bytes{{cluster="{cluster_id}"}})'


def q_cluster_max_temp(cluster_id: str) -> str:
    """集群最高温度（°C）。"""
    return f'max by(cluster) (node_thermal_zone_temp{{cluster="{cluster_id}"}})'


def q_node_cpu_cores(instance: str) -> str:
    """单节点 CPU 核心数。"""
    return f'node_cpu_cores{{instance="{instance}"}}'


def q_node_disk_size(instance: str) -> str:
    """单节点所有挂载点文件系统总容量（bytes）。"""
    return (
        f'sum(node_filesystem_size_bytes{{instance="{instance}",mountpoint!=""}})'
    )


def q_node_cpu_usage(instance: str) -> str:
    """单节点 CPU 平均使用率（1m 窗口，0~1）。

    标准 CPU 利用率公式：CPU% = 1 - idle%。iowait 不应单独扣除——
    iowait 表示 CPU 在等待 I/O 完成，但仍然消耗了 CPU 时间片，应算作"忙碌"。
    这与 `top` / `node_exporter` 的算法一致。

    `node_cpu_seconds_total` 已聚合全部核心，idle 的 rate 取值域是 [0, 核心数]，
    需除以 node_cpu_cores 归一化，否则 1 - rate 会得到负值。
    ignoring(mode) 不可省：rate() 保留 mode 标签，两侧标签集需显式忽略后匹配。
    """
    return (
        f'1 - ('
        f'rate(node_cpu_seconds_total{{instance="{instance}",mode="idle"}}[1m])'
        f' / ignoring(mode) node_cpu_cores{{instance="{instance}"}}'
        f')'
    )

def q_node_load1(instance: str) -> str:
    return f'node_load1{{instance="{instance}"}}'


def q_node_mem_total(instance: str) -> str:
    return f'node_memory_MemTotal_bytes{{instance="{instance}"}}'


def q_node_mem_avail(instance: str) -> str:
    return f'node_memory_MemAvailable_bytes{{instance="{instance}"}}'


def q_node_procs_running(instance: str) -> str:
    return f'node_procs_running{{instance="{instance}"}}'


def q_node_boot_time(instance: str) -> str:
    return f'node_boot_time_seconds{{instance="{instance}"}}'


def q_node_max_temp(instance: str) -> str:
    return f'max(node_thermal_zone_temp{{instance="{instance}"}})'


def q_node_disk_total(instance: str) -> str:
    """单节点所有块设备读取+写入速率（bytes/s, 5m 窗口）— 反映磁盘 IO 繁忙程度。"""
    return (
        f'sum(rate(node_disk_read_bytes_total{{instance="{instance}"}}[5m]))'
        f' + sum(rate(node_disk_written_bytes_total{{instance="{instance}"}}[5m]))'
    )


def q_node_network_rx(instance: str) -> str:
    return f'sum(rate(node_network_receive_bytes_total{{instance="{instance}"}}[5m]))'


def q_node_network_tx(instance: str) -> str:
    return f'sum(rate(node_network_transmit_bytes_total{{instance="{instance}"}}[5m]))'


# ===== GPU（DCGM_FI_DEV_*，由 collector/gpu_collector.py 上报）=====
# collector 暴露的 GPU 指标命名遵循 DCGM 标准，标签含 gpu/UUID/pci_bus_id/device/
# modelName/hostname/DCGM_FI_DRIVER_VERSION。集群聚合需要 scrape config 注入 cluster
# 标签；未注入则返回空，调用端按 null 展示。
# 单机 GPU 卡数用 `count by(cluster) (DCGM_FI_DEV_GPU_UTIL)`：每张卡至少上报一个 GPU_UTIL
# 时间序列，因此计数即 GPU 卡数。利用率/温度用 `avg` / `max`。
# 节点级过滤用 instance；要求 scrape 把 instance 注入到 DCGM 指标，否则走 hostname 兜底。

Q_GPU_UTIL = "DCGM_FI_DEV_GPU_UTIL"
Q_GPU_TEMP = "DCGM_FI_DEV_GPU_TEMP"
Q_GPU_MEM_USED = "DCGM_FI_DEV_FB_USED"
Q_GPU_MEM_TOTAL = "DCGM_FI_DEV_FB_FREE"  # 仅 free 不够；总显存需另行计算；用 used + free 兜底

GPU_DEVICE_INFO = "DCGM_FI_DEV_GPU_UTIL"  # 占位：实际型号需 modelName 标签


def q_cluster_gpu_count(cluster_id: str) -> str:
    """集群 GPU 卡数（按 cluster 标签分组计数）。"""
    return f'count by(cluster) (DCGM_FI_DEV_GPU_UTIL{{cluster="{cluster_id}"}})'


def q_cluster_gpu_util(cluster_id: str) -> str:
    """集群 GPU 平均利用率（%）。"""
    return f'avg by(cluster) (DCGM_FI_DEV_GPU_UTIL{{cluster="{cluster_id}"}})'


def q_cluster_gpu_temp(cluster_id: str) -> str:
    """集群 GPU 最高温度（°C）。"""
    return f'max by(cluster) (DCGM_FI_DEV_GPU_TEMP{{cluster="{cluster_id}"}})'


def q_node_gpu_count(instance: str) -> str:
    """单节点 GPU 卡数。"""
    return f'count(DCGM_FI_DEV_GPU_UTIL{{instance="{instance}"}})'


def q_node_gpu_util(instance: str) -> str:
    """单节点 GPU 平均利用率（%）。"""
    return f'avg(DCGM_FI_DEV_GPU_UTIL{{instance="{instance}"}})'


def q_node_gpu_temp(instance: str) -> str:
    """单节点 GPU 最高温度（°C）。"""
    return f'max(DCGM_FI_DEV_GPU_TEMP{{instance="{instance}"}})'


def q_node_gpu_mem_used(instance: str) -> str:
    """单节点 GPU 显存已用总量（MiB）。"""
    return f'sum(DCGM_FI_DEV_FB_USED{{instance="{instance}"}})'


def load_cluster_locations(path: str | Path) -> dict[str, dict[str, Any]]:
    """加载集群元数据 → {cluster_id: {name, region, location, ...}}。

    若文件不存在或解析失败，返回空字典（地图降级为无坐标散点）。
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {c["id"]: c for c in data.get("clusters", []) if "id" in c}
