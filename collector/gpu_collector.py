#!/usr/bin/env python3
"""GPU 指标采集（NVIDIA）。

通过 `pynvml`（NVML）调用，输出格式兼容 DCGM exporter：
`DCGM_FI_DEV_*` 共 18 项，外加自研的 `node_gpu_flops_peak`（峰值算力）。

`pynvml`（nvidia-ml-py）是**可选**依赖：未安装时本采集器降级为空输出，
主机侧的 CPU/内存/磁盘等指标照常上报。早前这里是硬 import，
目标机没装 nvidia-ml-py 会让整个 exporter 启动失败 —— 该主机不是「缺 GPU
指标」，而是在 Prometheus 里彻底消失，连 node_* 也一起没了。

指标含义见 `METRICS.md`。
"""
from __future__ import annotations

import json
import socket
from typing import Any, Callable

from promtext import MetricWriter

try:
    import pynvml
    PYNVML_IMPORT_ERROR = None
except Exception as _exc:  # ImportError，也兼容 pynvml 内部 import 失败
    pynvml = None
    PYNVML_IMPORT_ERROR = _exc

# 是否具备 GPU 采集能力。False 表示 pynvml 不可用或 NVML 初始化失败，
# 两种情况都不影响主机指标的采集。
PYNVML_AVAILABLE = pynvml is not None


# ---------------------------------------------------------------------------
# 标签字段（顺序即输出顺序）
# ---------------------------------------------------------------------------

_LABEL_KEYS = ('gpu', 'UUID', 'pci_bus_id', 'device', 'modelName',
               'hostname', 'DCGM_FI_DRIVER_VERSION')


# ---------------------------------------------------------------------------
# 峰值算力规格表（FLOPS/s）
# ---------------------------------------------------------------------------
# NVML 没有 FLOPS 接口，理论峰值只能按型号查表。三个口径：
#   fp32  —— 普通单精度向量单元，AI 推理与传统 HPC 都用它
#   fp64  —— 双精度向量单元，HPC/TOP500 的传统口径
#   fp16  —— Tensor Core 半精度，是 AI 训练真正关心的口径，通常是 fp32 的 8~16 倍。
#            一律取 dense（稠密）值，不含 2:4 结构化稀疏——规格书常把稀疏值标 *,
#            混用会让同一集群的总数失去可比性。
#
# 匹配方式是 modelName 子串包含，**顺序即优先级，特异性高的必须在前**
# （`A100-SXM4-40GB` 必须排在 `A100` 之前，否则会被后者抢走）。
# 数值取自 NVIDIA 官方规格表，单位 TFLOPS。
_FLOPS_SPECS = [
    ('H200',           67.0,   34.0,  989.0),
    ('H100 SXM',       67.0,   34.0,  989.0),
    ('H100 PCIe',      51.0,   26.0,  756.0),
    ('H100',           67.0,   34.0,  989.0),
    ('A100-SXM4-80',   19.5,    9.7,  312.0),
    ('A100-PCIE-80',   19.5,    9.7,  312.0),
    ('A100-SXM4-40',   19.5,    9.7,  312.0),
    ('A100-PCIE-40',   19.5,    9.7,  312.0),
    ('A100',           19.5,    9.7,  312.0),
    ('A30',            16.0,    7.4,  165.0),
    ('L40S',           91.6,    1.43, 362.0),
    ('L40',            90.5,    1.43, 181.0),
    ('A40',            37.4,    0.58, 149.7),
    ('V100-SXM2-32',   15.7,    7.8,  125.0),
    ('V100-SXM2-16',   14.0,    7.0,  112.0),
    ('V100-PCIE-32',   14.0,    7.0,  112.0),
    ('V100',           14.0,    7.0,  112.0),
    ('T4',              8.1,    0.33,  65.0),
    ('Quadro RTX 4000', 11.75,  0.294,  49.0),
    ('RTX 4090',       82.6,    1.29,  330.0),
    ('RTX 3090',       35.6,    0.56,  285.0),
    ('RTX A6000',      38.7,    0.6,   154.0),
]

_TFLOPS = 1e12


def _lookup_flops_specs(model_name: str | None) -> dict:
    """按 modelName 查规格表，返回 {precision: FLOPS/s}；型号未收录返回 {}。

    未收录就不发指标。调用方靠 gpu_count 区分「这台机器没有 GPU 卡」与
    「有卡但型号未收录」，页面明确提示后者，不静默当成 0。
    """
    name = model_name or ''
    for matcher, fp32, fp64, fp16 in _FLOPS_SPECS:
        if matcher in name:
            return {
                'fp32': fp32 * _TFLOPS,
                'fp64': fp64 * _TFLOPS,
                'fp16': fp16 * _TFLOPS,
            }
    return {}


# ---------------------------------------------------------------------------
# NVML 取值辅助
# ---------------------------------------------------------------------------

def _safe(fn: Callable[[], Any], default: Any = 0) -> Any:
    """包裹 pynvml 调用，失败返回 default。"""
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _first_of_pair(fn: Callable[[], Any]) -> Any:
    """调用返回二元组的 pynvml 函数，取第一项；失败返回 0。"""
    try:
        return fn()[0]
    except Exception:
        return 0


def _mem_info(attr: str) -> Callable[[Any, Any], float]:
    """显存信息取值器（bytes → MiB）。"""
    def _fn(h: Any, _info: Any) -> float:
        try:
            info = pynvml.nvmlDeviceGetMemoryInfo(h)
            return getattr(info, attr, 0) / (1024 * 1024)
        except Exception:
            return 0
    return _fn


def _remapped(attr: str) -> Callable[[Any, Any], int]:
    """显存行重映射计数取值器。"""
    def _fn(h: Any, _info: Any) -> int:
        try:
            corr, uncorr, _pending, failure = pynvml.nvmlDeviceGetRemappedRows(h)
            return {'correctable': corr, 'uncorrectable': uncorr, 'failure': failure}[attr]
        except Exception:
            return 0
    return _fn


# ---------------------------------------------------------------------------
# 指标定义：(指标名, 取值器)
# 取值器签名 (handle, info) → 数值；单个指标失败一律降级为 0，不影响其他指标。
# ---------------------------------------------------------------------------

METRICS: tuple = (
    ("DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS", _remapped('correctable')),
    ("DCGM_FI_DEV_DEC_UTIL",
     lambda h, _i: _first_of_pair(lambda: pynvml.nvmlDeviceGetDecoderUtilization(h))),
    ("DCGM_FI_DEV_ENC_UTIL",
     lambda h, _i: _first_of_pair(lambda: pynvml.nvmlDeviceGetEncoderUtilization(h))),
    ("DCGM_FI_DEV_FB_FREE", _mem_info('free')),
    ("DCGM_FI_DEV_FB_RESERVED", _mem_info('reserved')),
    ("DCGM_FI_DEV_FB_USED", _mem_info('used')),
    ("DCGM_FI_DEV_GPU_TEMP",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU))),
    ("DCGM_FI_DEV_GPU_UTIL",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetUtilizationRates(h).gpu)),
    ("DCGM_FI_DEV_MEMORY_TEMP",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_MEMORY))),
    ("DCGM_FI_DEV_MEM_CLOCK",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM))),
    ("DCGM_FI_DEV_MEM_COPY_UTIL",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetUtilizationRates(h).memory)),
    ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetPcieReplayCounter(h))),
    ("DCGM_FI_DEV_POWER_USAGE",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetPowerUsage(h))),
    ("DCGM_FI_DEV_ROW_REMAP_FAILURE", _remapped('failure')),
    ("DCGM_FI_DEV_SM_CLOCK",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_SM))),
    ("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetTotalEnergyConsumption(h))),
    ("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", _remapped('uncorrectable')),
    ("DCGM_FI_DEV_VGPU_LICENSE_STATUS",
     lambda h, _i: _safe(lambda: pynvml.nvmlDeviceGetVgpuLicenseStatus(h))),
)


# ---------------------------------------------------------------------------
# 标签提取
# ---------------------------------------------------------------------------

def _driver_version() -> str:
    try:
        return pynvml.nvmlSystemGetDriverVersion()
    except Exception:
        return ""


def _format_pci_bus_id(handle: Any) -> str:
    """把 NVML 的 PCI 总线信息格式化为 'DDDD:BB:DD.F'。"""
    try:
        pci = pynvml.nvmlDeviceGetPciInfo(handle)
        bus = getattr(pci, 'busId', None) or getattr(pci, 'busIdLegacy', None)
        if isinstance(bus, str) and bus:
            return bus
        if bus is None:
            return ""
        # 旧驱动：按 domain/bus/device 拼装
        domain = getattr(pci, 'domain', 0)
        b = getattr(pci, 'bus', 0)
        device = getattr(pci, 'device', 0)
        return f'{domain:08x}:{b:02x}:{device:02x}.0'
    except Exception:
        return ""


def _read_gpu(index: int) -> dict | None:
    """读取单块 GPU 的标签 + 各指标值；返回 dict 或 None。"""
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(index)
    except Exception:
        return None

    info = {
        'gpu': str(index),
        'UUID': _safe(lambda: pynvml.nvmlDeviceGetUUID(h), default=""),
        'pci_bus_id': _format_pci_bus_id(h),
        'device': f'nvidia{index}',
        'modelName': _safe(lambda: pynvml.nvmlDeviceGetName(h), default=""),
        'hostname': socket.gethostname(),
        'DCGM_FI_DRIVER_VERSION': _driver_version(),
    }
    metrics = {name: extractor(h, info) for name, extractor in METRICS}
    return {**info, 'metrics': metrics}


def _label_pairs(gpu_info: dict) -> list:
    """按 _LABEL_KEYS 的固定顺序取出 (键, 值) 列表。"""
    return [(key, gpu_info.get(key, '')) for key in _LABEL_KEYS]


# ---------------------------------------------------------------------------
# 采集器类
# ---------------------------------------------------------------------------

class GpuCollector:
    """NVIDIA GPU 指标采集器。

    `available` 表示本机是否真的能采到 GPU 数据：pynvml 未安装、或 NVML
    初始化失败（无驱动/无权限/无设备）时为 False，此时 `to_prometheus()`
    返回空字符串，宿主机的 node_* 指标不受任何影响。
    """

    def __init__(self) -> None:
        self._data: dict = {'gpus': [], 'driver_version': ''}
        self.available = PYNVML_AVAILABLE

    def collect(self) -> dict:
        self._collect_all()
        return self._data

    def _collect_all(self) -> None:
        # 局部组装、最后整体赋值：采集器实例被所有 HTTP 线程共享，
        # 边采边改 self._data 会让并发抓取读到半成品快照。
        data: dict = {'gpus': [], 'driver_version': ''}
        self.available = False
        if pynvml is None:
            self._data = data
            return
        try:
            pynvml.nvmlInit()
        except Exception:
            self._data = data
            return
        try:
            count = pynvml.nvmlDeviceGetCount()
            data['driver_version'] = _driver_version()
            data['gpus'] = [
                g for g in (_read_gpu(i) for i in range(count)) if g is not None
            ]
            self.available = True
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
        self._data = data

    def to_json(self) -> str:
        return json.dumps(self.collect(), indent=4, ensure_ascii=False)

    def to_prometheus(self, **_kwargs: Any) -> str:
        data = self.collect()
        writer = MetricWriter()
        for gpu in data.get('gpus', []):
            labels = _label_pairs(gpu)
            for name, _extractor in METRICS:
                writer.add_pairs(name, gpu['metrics'].get(name, 0), labels)
            # 峰值算力：标签与 DCGM 指标一致，precision 追加在最后
            for precision, value in _lookup_flops_specs(gpu.get('modelName')).items():
                writer.add_pairs('node_gpu_flops_peak', f'{value:.0f}',
                                 [*labels, ('precision', precision)])
        return writer.render()


if __name__ == '__main__':
    c = GpuCollector()
    print(c.to_prometheus())
