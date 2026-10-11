#!/usr/bin/env python3
"""
GPU Collector (NVIDIA)
通过 pynvml 调用 NVML 采集 GPU 指标，输出格式兼容 DCGM exporter。
1.txt 中的 18 个 DCGM_FI_DEV_* 指标全部覆盖。
若主机无 NVIDIA 驱动/设备，指标列表为空。
"""

import os
import time
import socket
import pynvml


# ---------------------------------------------------------------------------
# 标签字段（顺序与 1.txt 保持一致）
# ---------------------------------------------------------------------------

_LABEL_KEYS = ('gpu', 'UUID', 'pci_bus_id', 'device', 'modelName',
               'hostname', 'DCGM_FI_DRIVER_VERSION')


def _safe(fn, default=0):
    """包裹 pynvml 调用，失败返回 default。"""
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _first_of_pair(fn):
    """调用返回二元组的 pynvml 函数，取第一项；失败返回 0。"""
    try:
        return fn()[0]
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 各指标的取值器（返回数值；标签由 _read_gpu 在外层统一附加）
# ---------------------------------------------------------------------------

def _value_gpu_temp(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetTemperature(
        h, pynvml.NVML_TEMPERATURE_GPU))


def _value_gpu_util(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetUtilizationRates(h).gpu)


def _value_mem_clock(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetClockInfo(
        h, pynvml.NVML_CLOCK_MEM))


def _value_sm_clock(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetClockInfo(
        h, pynvml.NVML_CLOCK_SM))


def _value_power_usage(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetPowerUsage(h))


def _value_mem_info(attr):
    def _fn(h, _info):
        try:
            info = pynvml.nvmlDeviceGetMemoryInfo(h)
            return getattr(info, attr, 0) / (1024 * 1024)  # bytes → MiB
        except Exception:
            return 0
    return _fn


def _value_dec_util(h, _info):
    return _first_of_pair(lambda: pynvml.nvmlDeviceGetDecoderUtilization(h))


def _value_enc_util(h, _info):
    return _first_of_pair(lambda: pynvml.nvmlDeviceGetEncoderUtilization(h))


def _value_mem_copy_util(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetUtilizationRates(h).memory)


def _value_memory_temp(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetTemperature(
        h, pynvml.NVML_TEMPERATURE_MEMORY))


def _value_remapped(attr):
    def _fn(h, _info):
        try:
            corr, uncorr, _pending, failure = pynvml.nvmlDeviceGetRemappedRows(h)
            if attr == 'failure':
                return 1 if failure else 0
            return corr if attr == 'correctable' else uncorr
        except Exception:
            return 0
    return _fn


def _value_pcie_replay(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetPcieReplayCounter(h))


def _value_total_energy(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetTotalEnergyConsumption(h))


def _value_vgpu_license(h, _info):
    return _safe(lambda: pynvml.nvmlDeviceGetVgpuLicenseStatus(h))


# (指标名, 取值器)
METRICS = [
    ("DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS", _value_remapped('correctable')),
    ("DCGM_FI_DEV_DEC_UTIL", _value_dec_util),
    ("DCGM_FI_DEV_ENC_UTIL", _value_enc_util),
    ("DCGM_FI_DEV_FB_FREE", _value_mem_info('free')),
    ("DCGM_FI_DEV_FB_RESERVED", _value_mem_info('reserved')),
    ("DCGM_FI_DEV_FB_USED", _value_mem_info('used')),
    ("DCGM_FI_DEV_GPU_TEMP", _value_gpu_temp),
    ("DCGM_FI_DEV_GPU_UTIL", _value_gpu_util),
    ("DCGM_FI_DEV_MEMORY_TEMP", _value_memory_temp),
    ("DCGM_FI_DEV_MEM_CLOCK", _value_mem_clock),
    ("DCGM_FI_DEV_MEM_COPY_UTIL", _value_mem_copy_util),
    ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", _value_pcie_replay),
    ("DCGM_FI_DEV_POWER_USAGE", _value_power_usage),
    ("DCGM_FI_DEV_ROW_REMAP_FAILURE", _value_remapped('failure')),
    ("DCGM_FI_DEV_SM_CLOCK", _value_sm_clock),
    ("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", _value_total_energy),
    ("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", _value_remapped('uncorrectable')),
    ("DCGM_FI_DEV_VGPU_LICENSE_STATUS", _value_vgpu_license),
]


# ---------------------------------------------------------------------------
# 标签提取
# ---------------------------------------------------------------------------

def _driver_version():
    try:
        return pynvml.nvmlSystemGetDriverVersion()
    except Exception:
        return ""


def _read_gpu(index):
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


def _format_pci_bus_id(handle):
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


# ---------------------------------------------------------------------------
# 采集器类
# ---------------------------------------------------------------------------

class GpuCollector:
    """NVIDIA GPU 指标采集器。"""

    def __init__(self):
        self._data = {'gpus': [], 'driver_version': ''}

    def collect(self) -> dict:
        self._collect_all()
        return self._data

    def _collect_all(self):
        self._data = {'gpus': [], 'driver_version': ''}
        try:
            pynvml.nvmlInit()
        except Exception:
            return
        try:
            count = pynvml.nvmlDeviceGetCount()
            self._data['driver_version'] = _driver_version()
            self._data['gpus'] = [
                g for g in (_read_gpu(i) for i in range(count)) if g is not None
            ]
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def to_json(self) -> str:
        import json
        return json.dumps(self.collect(), indent=4, ensure_ascii=False)

    def to_prometheus(self, **_kwargs) -> str:
        data = self.collect()
        lines = []
        for gpu in data.get('gpus', []):
            labels_str = _format_labels(gpu)
            for name, _extractor in METRICS:
                lines.append(
                    f'{name}{{{labels_str}}} {gpu["metrics"].get(name, 0)}'
                )
        return '\n'.join(lines) + '\n'


def _format_labels(gpu_info):
    parts = []
    for key in _LABEL_KEYS:
        val = str(gpu_info.get(key, '')).replace('\\', '\\\\').replace(
            '"', '\\"').replace('\n', '\\n')
        parts.append(f'{key}="{val}"')
    return ','.join(parts)


if __name__ == '__main__':
    c = GpuCollector()
    print(c.to_prometheus())
