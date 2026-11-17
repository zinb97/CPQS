#!/usr/bin/env python3
"""Linux 主机指标采集。

从 procfs / sysfs 读取原始数据，转换成 Prometheus 文本格式。

结构分三层：

1. **读取层** `_read_*`：每个方法只负责一个数据源，返回普通 dict/list，
   不关心 Prometheus 的存在；
2. **发射层** `_emit_*`：把读取层的结果翻译成指标文本，通过
   `promtext.MetricWriter` 写出；
3. **对外接口** `collect()` / `to_json()` / `to_prometheus()`。

指标含义与口径见 `METRICS.md`。
"""
from __future__ import annotations

import glob
import json
import os
import platform
import re
import time
from typing import Any

from promtext import MetricWriter


# ---------------------------------------------------------------------------
# 常量：指标名 ↔ 内部字段的映射表
# ---------------------------------------------------------------------------

# /proc/stat 的 cpu 汇总行，字段顺序即内核给出的顺序
_CPU_MODES = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')

# /proc/stat 的标量行 → 指标名
_CPU_STAT_SCALARS = {
    'ctxt': 'node_context_switches_total',
    'btime': 'node_boot_time_seconds',
    'processes': 'node_forks_total',
    'procs_running': 'node_procs_running',
    'procs_blocked': 'node_procs_blocked',
}

# /proc/meminfo → 指标名（只暴露这 5 项，其余按需再加）
_MEMORY_METRICS = (
    ('node_memory_MemFree_bytes', 'MemFree'),
    ('node_memory_MemAvailable_bytes', 'MemAvailable'),
    ('node_memory_MemTotal_bytes', 'MemTotal'),
    ('node_memory_SwapFree_bytes', 'SwapFree'),
    ('node_memory_SwapTotal_bytes', 'SwapTotal'),
)

# /proc/loadavg → 指标名
_LOADAVG_METRICS = (
    ('node_load1', 'load1'),
    ('node_load5', 'load5'),
    ('node_load15', 'load15'),
    ('node_loadavg_running_process', 'running_process'),
    ('node_loadavg_total_process', 'total_process'),
)

# /proc/net/dev 的列位置 → 内部字段名
_NET_FIELDS = (
    ('rx_bytes', 1),
    ('rx_packets', 2),
    ('rx_errs', 3),
    ('rx_drop', 4),
    ('tx_bytes', 9),
    ('tx_packets', 10),
    ('tx_errs', 11),
    ('tx_drop', 12),
)

# 磁盘指标分三类：原样透传 / 扇区数换算成字节 / 毫秒换算成秒。
# 三类分开是因为换算方式不同——字节用整乘（保持整数），时间用除法
# （保持浮点数），混成「乘缩放系数」会让 138049/1000 变成 138.04900000000002。
_DISK_PLAIN_METRICS = (
    ('node_disk_reads_completed_total', 'reads_completed'),
    ('node_disk_reads_merged_total', 'reads_merged'),
    ('node_disk_writes_completed_total', 'writes_completed'),
    ('node_disk_writes_merged_total', 'writes_merged'),
)
_DISK_BYTE_METRICS = (
    ('node_disk_read_bytes_total', 'sectors_read'),
    ('node_disk_written_bytes_total', 'sectors_written'),
)
_DISK_SECOND_METRICS = (
    ('node_disk_read_time_seconds_total', 'read_time_ms'),
    ('node_disk_write_time_seconds_total', 'write_time_ms'),
    ('node_disk_io_time_seconds_total', 'io_time_ms'),
    ('node_disk_queue_total_wait_seconds_total', 'queue_total_wait_ms'),
)
# discard/flush 是 /proc/diskstats 的扩展列，**不是所有设备/内核都提供**。
# 没有这些列说明「不支持」，与「支持但计数为 0」是两回事，故只在有列时发射。
_DISK_EXT_METRICS = (
    ('node_disk_discards_completed_total', 'discard_completed'),
    ('node_disk_discards_merged_total', 'discard_merged'),
    ('node_disk_discards_bytes_total', 'discard_sectors'),
    ('node_disk_discards_time_seconds_total', 'discard_time_ms'),
    ('node_disk_flushes_completed_total', 'flush_completed'),
    ('node_disk_flushes_time_seconds_total', 'flush_time_ms'),
)

# 网卡过滤：网桥 / 容器虚拟网卡 / 旧式命名。bond* 是真实网卡，保留。
_NET_IGNORED_PREFIXES = ('br', 'docker', 'veth', 'b.')

# 每物理核每周期可完成的浮点运算数（一条 FMA 计 2 FLOP：乘 + 加）。
#
#   AVX-512 : 512-bit = 16 字节 = 8 个 FP32 / 4 个 FP64，每核 2 条 FMA 流水线
#             → FP32 2×8×2 = 32，FP64 2×4×2 = 16
#   AVX2    : 256-bit → FP32 2×4×2 = 16，FP64 2×2×2 = 8
#
# AMD 护栏：Zen 4/5 对外在 /proc/cpuinfo 里宣称 avx512f，但实际是 2×256-bit 双泵 FMA，
# 没有原生 512-bit 执行单元，按 flags 判定会高估 2 倍。因此 avx512f 档位只对 GenuineIntel
# 开放，AMD 落到 avx2 档。两个方向都是保守估计。
_FLOPS_PER_CYCLE = {
    'avx512f': {'fp32': 32.0, 'fp64': 16.0},
    'avx2': {'fp32': 16.0, 'fp64': 8.0},
}
_FLOPS_PER_CYCLE_BASELINE = {'fp32': 2.0, 'fp64': 1.0}

# sysfs 块设备根目录。抽成常量便于单测替换（生产环境恒为 /sys/block）。
SYS_BLOCK_PATH = '/sys/block'


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------

def _safe_int(value: Any, default: int = 0) -> int:
    """安全解析整数；失败返回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _listdir(path: str) -> list:
    """列目录，失败返回空列表（/sys 在容器里可能不可见）。"""
    try:
        return os.listdir(path)
    except OSError:
        return []


def _parse_paired_proto_stats(lines: list) -> dict:
    """解析 /proc/net/{snmp,netstat} 的成对表头/值行格式。

    这些文件每两行一组：第一行是「协议: 字段名 字段名 …」，第二行是同名
    的「协议: 数值 数值 …」。两行的字段顺序一致，按位置配对即可。
    """
    stats: dict = {}
    for i in range(0, len(lines) - 1, 2):
        try:
            proto_h, header_fields_str = lines[i].split(":", 1)
            _, value_fields_str = lines[i + 1].split(":", 1)
            proto_name = proto_h.strip()
            headers = header_fields_str.strip().split()
            values = value_fields_str.strip().split()
            proto_dict = {}
            for k, v in zip(headers, values):
                proto_dict[k] = int(v)
            stats[proto_name] = proto_dict
        except Exception:
            pass
    return stats


def _parse_base_ghz(model_name: str | None) -> float | None:
    """从型号名尾部提取基础频率（GHz）。

    形如 `Intel(R) Xeon(R) Gold 5218R CPU @ 2.10GHz` → 2.1。
    用基础频率而非 cpufreq 实测值或睿频上限：peak 算力按 TOP500 惯例以基础频率计，
    表示「装了多少算力」，是静态容量值，不随负载抖动。
    提取不到返回 None，调用方据此不发指标，不猜。
    """
    m = re.search(r'@\s*([\d.]+)\s*GHz', model_name or '', re.IGNORECASE)
    return float(m.group(1)) if m else None


def _flops_per_cycle(flags: str, vendor_id: str) -> dict:
    """按 SIMD 能力返回每核每周期 FLOP 数。"""
    if 'avx512f' in flags and vendor_id.strip().lower() == 'genuineintel':
        return _FLOPS_PER_CYCLE['avx512f']
    if 'avx2' in flags:
        return _FLOPS_PER_CYCLE['avx2']
    return _FLOPS_PER_CYCLE_BASELINE


def _disk_skip_devices() -> set:
    """返回 /proc/diskstats 中应当忽略的设备名集合（重复统计的那几层）。

    /proc/diskstats 对同一份物理 IO 会按层重复上报，直接 `sum` 会成倍虚高
    （node_disk_read_bytes_total 翻倍 → 主机详情页的磁盘 IO 速率虚高）：

    - 分区（sda1 / nvme0n1p1）：计数是父整盘的子集
    - 设备映射层（dm-N、LVM 逻辑卷）：底层物理盘的另一个视图
    - RAID 层（md0）与它的成员盘（slaves）：md0 与 sda/sdb 统计同一份流量

    规则：**只保留最上层（top-level）设备**，其下的分区/成员/中间层全部忽略。
    判定完全依赖 sysfs 的层次关系，不靠设备名前缀猜：

    - 有 holders（建立在它之上的设备）→ 它是中间层 → 忽略
    - 是别人的 slaves（成员盘）→ 忽略
    - 是分区 → 忽略
    - 分区被上层设备占用（分区上有 holders）→ 它的**父整盘**也要忽略
      （LVM 建在分区上时，IO 是 父盘 → 分区 → 逻辑卷 一路传下去的）

    效果：
      普通盘 sda+sda1            → 只留 sda
      RAID1 md0+sda+sdb          → 只留 md0
      LVM on RAID dm-0+md0+sda   → 只留 dm-0
      LVM 直接建在盘上 dm-0+sda  → 只留 dm-0
      LVM 建在分区上 dm-0+sda3+sda → 只留 dm-0

    /sys 不可见时返回空集合 → 不过滤（宁可少过滤，也不丢指标；更不能因为
    读不到 /sys 就让整次抓取失败）。
    """
    skip: set = set()
    if not os.path.isdir(SYS_BLOCK_PATH):
        return skip

    # 分区 → (父整盘名, 分区在 sysfs 下的完整路径)
    # 分区不在 /sys/block 顶层，而在父设备目录里（/sys/block/sda/sda1），
    # 查它的 holders 必须用完整路径，不能拼 /sys/block/<分区名>。
    partition_parent: dict = {}

    for name in _listdir(SYS_BLOCK_PATH):
        base = os.path.join(SYS_BLOCK_PATH, name)

        # 分区：自身带 partition 文件（少数布局下分区出现在顶层），
        # 或挂在父设备下（常见布局：/sys/block/sda/sda1、
        # NVMe：/sys/block/nvme0n1/nvme0n1p1）
        if os.path.exists(os.path.join(base, 'partition')):
            skip.add(name)
        for child in _listdir(base):
            child_base = os.path.join(base, child)
            if os.path.exists(os.path.join(child_base, 'partition')):
                skip.add(child)
                partition_parent[child] = (name, child_base)

        # 有人在本设备之上（holders 非空）→ 本设备是中间层
        if _listdir(os.path.join(base, 'holders')):
            skip.add(name)

        # 本设备的成员盘（md 的 slaves、dm 的 slaves）→ 与本设备重复统计。
        # loop 设备的 slaves 是它挂载的镜像源文件（可能是真实磁盘），
        # 不能据此把那块盘排除掉，故 loop* 不参与这条规则。
        if not name.startswith('loop'):
            for member in _listdir(os.path.join(base, 'slaves')):
                skip.add(member)

    # 分区被上层设备（LVM / md）占用时，父整盘的 IO 也流进了那一层，
    # 父整盘要一并忽略，否则 sda 与 dm-0 会被同时统计。
    for part_name, (parent_name, part_path) in partition_parent.items():
        if _listdir(os.path.join(part_path, 'holders')):
            skip.add(parent_name)

    return skip


# ---------------------------------------------------------------------------
# 采集器
# ---------------------------------------------------------------------------

class HostCollector:
    """Linux 主机指标采集器。"""

    def __init__(self) -> None:
        self._data: dict = {}  # 最近一次采集的原始数据

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def collect(self) -> dict:
        """采集一次并返回原始数据。"""
        self._collect_all()
        return self._data

    def to_json(self) -> str:
        """输出为 JSON 格式（调试用）。"""
        return json.dumps(self.collect(), indent=4, ensure_ascii=False)

    def to_prometheus(self) -> str:
        """输出为 Prometheus 文本格式。

        发射顺序与重构前保持一致（Prometheus 本身不关心顺序，但固定顺序
        才能让输出可逐字节比对，便于回归校验）。
        """
        data = self.collect()
        writer = MetricWriter()
        self._emit_info_metrics(writer, data)
        self._emit_cpu(writer, data)
        self._emit_cpu_info(writer, data)
        self._emit_cpu_flops(writer, data)
        self._emit_loadavg(writer, data)
        self._emit_memory(writer, data)
        self._emit_proto_flat(writer, 'node_snmp', data.get('snmp') or {})
        self._emit_network(writer, data)
        self._emit_sockstat(writer, data)
        self._emit_proto_flat(writer, 'node_netstat', data.get('netstat') or {})
        self._emit_diskstats(writer, data)
        self._emit_mounts(writer, data)
        self._emit_thermal(writer, data)
        writer.add('node_time_seconds', int(time.time()))
        return writer.render()

    # ------------------------------------------------------------------
    # 采集编排
    # ------------------------------------------------------------------

    def _collect_all(self) -> None:
        """一次性采集所有原始数据。

        先在局部变量里组装完整快照，最后整体赋给 self._data。
        采集器实例被所有 HTTP 线程共享（见 metrics_exporter 的类属性
        collectors），若边采集边改 self._data，另一个线程可能读到
        「半成品」快照——例如 cpu_flops 还没算出来、或者用了一半新一半旧的
        cpu_info。整体赋值是原子的，读到的永远是一份完整快照。
        """
        # /proc/cpuinfo 较大（每核一整段），拓扑与 SIMD 标志一次解析取出，
        # 避免为了 flags 再把整个文件读一遍。
        cpu_info, cpu_flags = self._read_cpu_info()

        data = {
            'os': self._read_os_info(),
            'dmi': self._read_dmi(),
            'uname': self._read_uname(),
            'cpu_stat': self._read_cpu_stat(),
            'cpu_info': cpu_info,
            'loadavg': self._read_loadavg(),
            'meminfo': self._read_meminfo(),
            'snmp': self._read_snmp(),
            'network': self._read_network(),
            'sockstat': self._read_sockstat(),
            'netstat': self._read_netstat(),
            'diskstats': self._read_diskstats(),
            'mounts': self._read_mounts(),
            'thermal': self._read_thermal(),
        }
        # 峰值算力依赖上面刚解析出的 cpu_info（型号名里的基础频率 + 物理核数）
        # 与 SIMD 标志，必须在字典构造完成后才能算。
        data['cpu_flops'] = self._read_cpu_flops(cpu_info, cpu_flags)
        self._data = data

    # ------------------------------------------------------------------
    # 读取层：文件原语
    # ------------------------------------------------------------------

    @staticmethod
    def _read_file(path: str, default: Any = None) -> Any:
        """安全读取整个文件（失败返回 default）。"""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception:
            return default

    @staticmethod
    def _read_lines(path: str) -> list:
        """读取文件所有行（去掉空行与首尾空白；失败返回空列表）。"""
        try:
            with open(path, 'r') as f:
                return [line.strip() for line in f.read().splitlines() if line.strip()]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 读取层：各数据源
    # ------------------------------------------------------------------

    def _read_os_info(self) -> dict:
        """读取 /etc/os-release。

        键名统一转小写：os-release 的键天然是大写（PRETTY_NAME/ID_LIKE...），
        而 node_exporter 的 node_os_info 用小写标签，直接透传会导致调用端
        按 node_exporter 惯例写 PromQL 时全部匹配不到。
        """
        info: dict = {}
        for line in self._read_lines('/etc/os-release'):
            if '=' in line and 'URL' not in line:
                key, value = line.split('=', 1)
                value = value.strip('"')
                info[key.strip().lower()] = value
        return info

    def _read_dmi(self) -> dict:
        """读取 DMI 信息。

        /sys/class/dmi/id/ 下多数文件是 0400 root-only，非 root 运行时
        system_vendor 常读为空；调用端应回退到 bios_vendor。
        """
        dmi: dict = {}
        for key in ('bios_date', 'bios_vendor', 'bios_version', 'product_name', 'system_vendor'):
            val = self._read_file(f'/sys/class/dmi/id/{key}')
            if val:
                dmi[key] = val
        return dmi

    def _read_uname(self) -> dict:
        """读取 uname 信息。"""
        try:
            uname = platform.uname()
            return {
                'sysname': uname.system,
                'nodename': uname.node,
                'release': uname.release,
                'version': uname.version,
                'machine': uname.machine,
            }
        except Exception:
            return {}

    def _read_cpu_stat(self) -> dict:
        """读取 /proc/stat。

        只保留 `cpu` 汇总行；`cpuN` 单核行仅用于统计核心数，不存储其明细。
        """
        stats: dict = {'cores': 0}
        for line in self._read_lines('/proc/stat'):
            fields = line.split()
            if not fields:
                continue

            name = fields[0]
            if name in _CPU_STAT_SCALARS or name in ('softirq',):
                stats[name] = self._parse_cpu_stat_line(name, fields)
            elif name.startswith('cpu') and len(fields) >= 4:
                if name == 'cpu':
                    # 字段顺序与 _CPU_MODES 一致，缺失的尾部 mode（steal 等）按 0 处理
                    stats['cpu'] = {
                        mode: int(fields[i + 1]) / 100 if i + 1 < len(fields) else 0
                        for i, mode in enumerate(_CPU_MODES)
                    }
                else:
                    stats['cores'] += 1
        return stats

    @staticmethod
    def _parse_cpu_stat_line(name: str, fields: list) -> Any:
        """解析 /proc/stat 里的标量行或 softirq 行。"""
        if name == 'softirq':
            return {
                'total': int(fields[1]),
                'hi': int(fields[2]),
                'timer': int(fields[3]),
                'net_trans': int(fields[4]),
                'net_recv': int(fields[5]),
                'sched': int(fields[6]),
                'rcu': int(fields[7]) if len(fields) > 7 else 0,
            }
        return int(fields[1])

    def _read_cpu_info(self) -> tuple[dict, tuple]:
        """读取 CPU 型号、频率上限与拓扑 → (info, (flags, vendor_id))。

        型号与拓扑来自 /proc/cpuinfo；频率上限来自 cpufreq（kHz → MHz）。
        cpufreq 在部分虚拟机/容器中不存在，此时仅上报型号与拓扑，不编造频率。

        flags 不作为标签上报（约 60 个 flag 会把 node_cpu_info 的标签集撑爆），
        只在本地用于推导每核每周期 FLOP 数。x86 每个核重复一遍相同的 flags 行，
        取第一段即可。
        """
        info: dict = {}
        sockets: set = set()
        siblings = cores = 0
        flags = vendor = ''

        for line in self._read_lines('/proc/cpuinfo'):
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key, value = key.strip(), value.strip()
            if key == 'model name':
                info.setdefault('model_name', value)
            elif key == 'physical id':
                sockets.add(value)
            elif key == 'siblings' and not siblings:
                siblings = _safe_int(value)
            elif key == 'cpu cores' and not cores:
                cores = _safe_int(value)
            elif key == 'flags' and not flags:
                flags = value
            elif key == 'vendor_id' and not vendor:
                vendor = value

        n_sockets = len(sockets) or 1
        info['sockets'] = n_sockets
        if cores:
            info['cores_per_socket'] = cores
            info['physical_cores'] = cores * n_sockets
            if siblings:
                info['threads_per_core'] = max(1, round(siblings / cores))

        cpufreq = '/sys/devices/system/cpu/cpu0/cpufreq'
        for key, filename in (
            ('max_mhz', 'cpuinfo_max_freq'),
            ('min_mhz', 'cpuinfo_min_freq'),
        ):
            raw = self._read_file(f'{cpufreq}/{filename}')
            if raw and raw.isdigit():
                info[key] = int(raw) // 1000

        return info, (flags, vendor)

    def _read_cpu_flops(self, cpu_info: dict, cpu_flags: tuple) -> dict:
        """推导 CPU 峰值算力 → {precision: FLOPS/s}。

        peak = physical_cores × 基础频率GHz × 每核每周期 FLOP 数。

        缺 physical_cores（部分虚拟化环境的 /proc/cpuinfo 没有 `cpu cores`）或
        型号名里解析不出基础频率时返回空 dict，对应指标直接不发——宁可没有，
        也不要发一个编造出来的数字。
        """
        cores = cpu_info.get('physical_cores')
        base_ghz = _parse_base_ghz(cpu_info.get('model_name'))
        if not cores or not base_ghz:
            return {}
        flags, vendor = cpu_flags
        per_cycle = _flops_per_cycle(flags, vendor)
        return {
            precision: float(cores) * base_ghz * 1e9 * flops
            for precision, flops in per_cycle.items()
        }

    def _read_loadavg(self) -> dict:
        """读取 /proc/loadavg。"""
        lines = self._read_lines('/proc/loadavg')
        if not lines:
            return {}
        fields = lines[0].split()
        run_proc = total_proc = 0
        if len(fields) >= 4 and '/' in fields[3]:
            run_proc, total_proc = fields[3].split("/")
        return {
            'load1': float(fields[0]) if len(fields) > 0 else 0.0,
            'load5': float(fields[1]) if len(fields) > 1 else 0.0,
            'load15': float(fields[2]) if len(fields) > 2 else 0.0,
            'running_process': int(run_proc) if run_proc.isdigit() else 0,
            'total_process': int(total_proc) if total_proc.isdigit() else 0,
            'last_pid': int(fields[4]) if len(fields) > 4 and fields[4].isdigit() else 0,
        }

    def _read_meminfo(self) -> dict:
        """读取 /proc/meminfo（值统一转成字节）。"""
        meminfo: dict = {}
        for line in self._read_lines('/proc/meminfo'):
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(':'):
                meminfo[parts[0][:-1]] = _safe_int(parts[1]) * 1024
        return meminfo

    def _read_diskstats(self) -> list:
        """读取 /proc/diskstats。

        只保留最上层的块设备（整盘 / md / dm），过滤掉分区、RAID 成员盘与
        设备映射中间层——这几层统计的是同一份流量，全量求和会让
        node_disk_read_bytes_total 成倍虚高。判定规则见 `_disk_skip_devices()`。

        `ext` 标记该设备的 /proc/diskstats 行是否带 discard/flush 扩展列：
        不带就说明内核没提供这些计数（而不是计数为 0），发射时要区分。
        """
        skip = _disk_skip_devices()
        disks: list = []
        for line in self._read_lines('/proc/diskstats'):
            fields = line.split()
            if len(fields) < 14:
                continue
            device = fields[2]
            if device.startswith(('loop', 'ram', 'zram', 'sr')) or device in skip:
                continue
            item: dict = {
                'major': int(fields[0]),
                'minor': int(fields[1]),
                'device': device,
                'reads_completed': int(fields[3]),
                'reads_merged': int(fields[4]),
                'sectors_read': int(fields[5]),
                'read_time_ms': int(fields[6]),
                'writes_completed': int(fields[7]),
                'writes_merged': int(fields[8]),
                'sectors_written': int(fields[9]),
                'write_time_ms': int(fields[10]),
                'in_flight_io': int(fields[11]),
                'io_time_ms': int(fields[12]),
                'queue_total_wait_ms': int(fields[13]),
                'ext': False,
                'discard_completed': 0,
                'discard_merged': 0,
                'discard_sectors': 0,
                'discard_time_ms': 0,
                'flush_completed': 0,
                'flush_time_ms': 0,
            }
            # 20 列时多出 discard/flush 扩展字段
            if len(fields) >= 20:
                item['ext'] = True
                item['discard_completed'] = int(fields[14])
                item['discard_merged'] = int(fields[15])
                item['discard_sectors'] = int(fields[16])
                item['discard_time_ms'] = int(fields[17])
                item['flush_completed'] = int(fields[18])
                item['flush_time_ms'] = int(fields[19])
            disks.append(item)
        return disks

    def _read_network(self) -> dict:
        """读取 /proc/net/dev。"""
        net: dict = {}
        for line in self._read_lines('/proc/net/dev')[2:]:
            fields = line.split()
            if len(fields) < 10:
                continue
            iface = fields[0].rstrip(':')
            if iface == 'lo' or iface.startswith(_NET_IGNORED_PREFIXES):
                continue
            net[iface] = {key: int(fields[idx]) for key, idx in _NET_FIELDS}
        return net

    def _read_snmp(self) -> dict:
        """读取 /proc/net/snmp。"""
        return _parse_paired_proto_stats(self._read_lines('/proc/net/snmp'))

    def _read_sockstat(self) -> dict:
        """读取 /proc/net/sockstat。"""
        sockstat: dict = {}
        for line in self._read_lines('/proc/net/sockstat'):
            parts = line.split()
            if len(parts) < 2:
                continue
            proto_name = parts[0].rstrip(":")
            data: dict = {}
            for i in range(1, len(parts) - 1, 2):
                try:
                    data[parts[i]] = int(parts[i + 1])
                except (ValueError, IndexError):
                    pass
            sockstat[proto_name] = data
        return sockstat

    def _read_netstat(self) -> dict:
        """读取 /proc/net/netstat。"""
        return _parse_paired_proto_stats(self._read_lines('/proc/net/netstat'))

    def _read_thermal(self) -> list:
        """读取 thermal zone 信息。"""
        zones: list = []
        for zone_path in glob.glob('/sys/class/thermal/thermal_zone*'):
            try:
                temp = self._read_file(os.path.join(zone_path, 'temp'))
                type_ = self._read_file(os.path.join(zone_path, 'type'))
                if temp is None or type_ is None:
                    continue
                val = int(temp)
                if val and type_:
                    zones.append({
                        'zone': zone_path[-1],
                        'type': type_,
                        'temp': val / 1000,
                    })
            except (OSError, ValueError, TypeError):
                continue
        return zones

    def _read_mounts(self) -> list:
        """读取挂载点信息。

        每行独立解析：/proc/mounts 里出现一行字段数不足的条目时，只跳过
        那一行，而不是让整个列表作废（否则后面的挂载点会静默消失，
        node_filesystem_* 少一片而没有任何报错）。
        """
        mounts: list = []
        seen_devices: set = set()
        for line in self._read_lines('/proc/mounts'):
            fields = line.split()
            if len(fields) < 4:
                continue
            device = fields[0]
            if '/dev' not in device or 'loop' in device:
                continue

            # 同一设备挂多处时只报第一个挂载点，避免容量重复累加
            if device in seen_devices:
                continue
            seen_devices.add(device)

            mountpoint = fields[1]
            try:
                stat = os.statvfs(mountpoint)
                total_bytes = stat.f_blocks * stat.f_frsize
                avail_bytes = stat.f_bavail * stat.f_frsize
                used_bytes = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            except OSError:
                total_bytes = avail_bytes = used_bytes = None

            mounts.append({
                'device': device,
                'mountpoint': mountpoint,
                'fstype': fields[2],
                'options': fields[3],
                'total_bytes': total_bytes,
                'avail_bytes': avail_bytes,
                'used_bytes': used_bytes,
            })
        return mounts

    # ------------------------------------------------------------------
    # 发射层：原始数据 → 指标文本
    # ------------------------------------------------------------------

    def _emit_info_metrics(self, writer: MetricWriter, data: dict) -> None:
        """静态信息类指标：标签即全部内容，值恒为 1。

        uname / os / dmi 三者的形状一致，共用同一段代码。
        """
        for key, metric in (
            ('uname', 'node_uname_info'),
            ('os', 'node_os_info'),
            ('dmi', 'node_dmi_info'),
        ):
            info = data.get(key) or {}
            if info:
                writer.add_labels(metric, info)

    def _emit_cpu_info(self, writer: MetricWriter, data: dict) -> None:
        """CPU 型号与拓扑（标签即全部内容）。"""
        info = data.get('cpu_info') or {}
        if info:
            writer.add_labels('node_cpu_info', info)

    def _emit_cpu(self, writer: MetricWriter, data: dict) -> None:
        """CPU 计数与峰值算力。"""
        cpu_stat = data.get('cpu_stat', {})

        cores = cpu_stat.get('cores')
        if cores:
            writer.add('node_cpu_cores', cores)

        cpu = cpu_stat.get('cpu')
        if cpu:
            for mode in _CPU_MODES:
                writer.add('node_cpu_seconds_total', cpu.get(mode, 0), mode=mode)

        for key, metric_name in _CPU_STAT_SCALARS.items():
            value = cpu_stat.get(key)
            if isinstance(value, (int, float)):
                writer.add(metric_name, value)

    def _emit_cpu_flops(self, writer: MetricWriter, data: dict) -> None:
        """CPU 峰值算力。

        装机容量而非时序量，刻意不加任何标签（instance/cluster 由 scrape
        注入），这样 sum by(cluster) 一次聚合就完。
        """
        for precision, value in (data.get('cpu_flops') or {}).items():
            writer.add('node_cpu_flops_peak', f'{value:.0f}', precision=precision)

    def _emit_loadavg(self, writer: MetricWriter, data: dict) -> None:
        """负载与进程数指标。"""
        loadavg = data.get('loadavg') or {}
        if not loadavg:
            return
        for metric_name, key in _LOADAVG_METRICS:
            writer.add(metric_name, loadavg.get(key, 0))

    def _emit_memory(self, writer: MetricWriter, data: dict) -> None:
        """内存指标。"""
        meminfo = data.get('meminfo') or {}
        if not meminfo:
            return
        for metric_name, key in _MEMORY_METRICS:
            writer.add(metric_name, meminfo.get(key, 0))

    def _emit_sockstat(self, writer: MetricWriter, data: dict) -> None:
        """socket 层计数。

        注意：**不要**在这里再发一份与 `_emit_proto_flat` 同名同标签集的指标。
        两条路径发出同一个 series 会违反 Prometheus 曝光格式的「同一标签集
        不得重复出现」，导致整次抓取被拒、该 target 的全部指标丢失。

        具体到 TCP：现代内核（4.x 起）的 /proc/net/netstat 已不含 Tcp: 段，
        基础 TCP 计数器只在 /proc/net/snmp 里，因此 CurrEstab 只有
        node_snmp_Tcp_CurrEstab 一条；sockstat 口径的在途连接数则是
        node_sockstat_TCP_inuse（不同名字、不同含义，不会冲突）。
        若将来在别处显式发射某个 node_netstat_* / node_snmp_* 指标，
        务必先确认 _emit_proto_flat 不会同时产出同名 series。
        """
        for proto, vals in (data.get('sockstat') or {}).items():
            if isinstance(vals, dict):
                for k, v in vals.items():
                    writer.add(f'node_sockstat_{proto}_{k}', v)

    def _emit_network(self, writer: MetricWriter, data: dict) -> None:
        """网卡级累计计数。"""
        for iface, vals in (data.get('network') or {}).items():
            if not isinstance(vals, dict):
                continue
            writer.add('node_network_receive_bytes_total', vals.get('rx_bytes', 0), interface=iface)
            writer.add('node_network_transmit_bytes_total', vals.get('tx_bytes', 0), interface=iface)
            writer.add('node_network_receive_packets_total', vals.get('rx_packets', 0), interface=iface)
            writer.add('node_network_transmit_packets_total', vals.get('tx_packets', 0), interface=iface)
            # 错误/丢包计数器：网卡层的真实异常信号，与「抗扰动」维度互补
            writer.add('node_network_receive_errs_total', vals.get('rx_errs', 0), interface=iface)
            writer.add('node_network_receive_drop_total', vals.get('rx_drop', 0), interface=iface)
            writer.add('node_network_transmit_errs_total', vals.get('tx_errs', 0), interface=iface)
            writer.add('node_network_transmit_drop_total', vals.get('tx_drop', 0), interface=iface)

    def _emit_diskstats(self, writer: MetricWriter, data: dict) -> None:
        """块设备级累计计数。"""
        for disk in data.get('diskstats') or []:
            dev = disk.get('device', '')
            writer.add('node_disk_info', 1, device=dev,
                       major=disk.get('major', ''), minor=disk.get('minor', ''))
            for metric_name, key in _DISK_PLAIN_METRICS:
                writer.add(metric_name, disk.get(key, 0), device=dev)
            for metric_name, key in _DISK_BYTE_METRICS:
                writer.add(metric_name, disk.get(key, 0) * 512, device=dev)
            for metric_name, key in _DISK_SECOND_METRICS:
                writer.add(metric_name, disk.get(key, 0) / 1000, device=dev)
            writer.add('node_disk_in_flight_requests', disk.get('in_flight_io', 0), device=dev)
            # discard/flush 只在内核提供了扩展列时才有意义
            if disk.get('ext'):
                for metric_name, key in _DISK_EXT_METRICS:
                    if key == 'discard_sectors':
                        writer.add(metric_name, disk.get(key, 0) * 512, device=dev)
                    elif key == 'discard_time_ms' or key == 'flush_time_ms':
                        writer.add(metric_name, disk.get(key, 0) / 1000, device=dev)
                    else:
                        writer.add(metric_name, disk.get(key, 0), device=dev)

    def _emit_mounts(self, writer: MetricWriter, data: dict) -> None:
        """文件系统容量。"""
        for mnt in data.get('mounts') or []:
            if not mnt.get('total_bytes'):
                continue
            labels = {
                'mountpoint': mnt.get('mountpoint', ''),
                'device': mnt.get('device', ''),
                'fstype': mnt.get('fstype', ''),
            }
            writer.add('node_filesystem_size_bytes', mnt['total_bytes'], **labels)
            writer.add('node_filesystem_avail_bytes', mnt.get('avail_bytes', 0), **labels)
            # free 含 root 保留块（f_bfree），与 avail（f_bavail）不同义
            writer.add('node_filesystem_free_bytes',
                       mnt.get('avail_bytes', 0) + mnt.get('used_bytes', 0), **labels)

    def _emit_thermal(self, writer: MetricWriter, data: dict) -> None:
        """温度区。"""
        for zone in data.get('thermal') or []:
            writer.add('node_thermal_zone_temp', zone.get('temp', 0),
                       zone=zone.get('zone', ''), type=zone.get('type', ''))

    # ------------------------------------------------------------------
    # 协议栈计数（/proc/net/{snmp,netstat,sockstat} 的平铺）
    # ------------------------------------------------------------------

    def _emit_proto_flat(self, writer: MetricWriter, prefix: str, proto_data: dict) -> None:
        """把嵌套 dict {'proto': {k: v, ...}} 平铺成 prefix_proto_k v。"""
        for proto, vals in proto_data.items():
            if isinstance(vals, dict):
                for k, v in vals.items():
                    writer.add(f'{prefix}_{proto}_{k}', v)


if __name__ == '__main__':
    collector = HostCollector()
    print(json.dumps(collector.collect().get('diskstats'), indent=4, ensure_ascii=False))
