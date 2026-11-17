"""Prometheus 文本曝光格式（text exposition format）的写出工具。

单独成模块的原因：主机采集与 GPU 采集都要做同样的事——把 Python 值转成
Prometheus 文本、转义标签值、拼装标签串。集中在这里可以保证：

- **标签值只在一处转义**（`_escape_label_value`），不会再出现某个发射器
  漏转义导致整次抓取被 Prometheus 拒收的情况；
- **数值格式只在一处决定**（`str()`，与重构前逐字符一致）；
- 标签的**顺序**由调用方显式给出，不依赖 dict 的插入顺序。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def escape_label_value(value: Any) -> str:
    """转义 Prometheus 标签值。

    `/proc/cpuinfo` 的 model name 含空格与括号，未来也可能含引号或反斜杠；
    未转义会破坏整行解析（Prometheus 拒收整次抓取，该 target 全部指标丢失）。
    裸 LF/CR 理论上不会出现，仍一并处理。
    """
    return (
        str(value)
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '')
    )


def format_labels(labels: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> str:
    """把标签映射拼成 `k1="v1",k2="v2"` 形式（不含外层花括号）。

    传入 Mapping 时按其插入顺序输出；传入二元组序列时按给定顺序输出——
    Prometheus 本身不关心顺序，但顺序固定才能让输出可逐字节比对。
    """
    items = labels.items() if isinstance(labels, Mapping) else labels
    return ','.join(f'{k}="{escape_label_value(v)}"' for k, v in items)


def format_value(value: Any) -> str:
    """指标值的文本形式。

    直接用 `str()`：整数值保持整数形态（`80` 而不是 `80.0`），浮点数用
    Python 的 repr 规则（`138.049`）。这是重构前的既有行为，保持不变。
    """
    return str(value)


class MetricWriter:
    """按行累积 Prometheus 指标文本。

    用法：

        w = MetricWriter()
        w.add('node_cpu_cores', 80)
        w.add('node_disk_read_bytes_total', 1234, device='sda')
        w.add_info('node_uname_info', {'nodename': 'host1'})
        text = w.render()
    """

    __slots__ = ('_lines',)

    def __init__(self) -> None:
        self._lines: list[str] = []

    def add(self, name: str, value: Any, **labels: Any) -> None:
        """写一条带标签的指标（无标签时只写指标名）。"""
        if labels:
            self._lines.append(f'{name}{{{format_labels(labels)}}} {format_value(value)}')
        else:
            self._lines.append(f'{name} {format_value(value)}')

    def add_pairs(self, name: str, value: Any,
                  labels: Iterable[tuple[str, Any]]) -> None:
        """写一条标签顺序由调用方给定的指标。

        与 `add()` 的区别：这里标签是「有序的 (键, 值) 序列」而不是关键字参数，
        用于标签集较大且顺序固定的场景（GPU 的一组标签）。
        """
        label_str = format_labels(labels)
        if label_str:
            self._lines.append(f'{name}{{{label_str}}} {format_value(value)}')
        else:
            self._lines.append(f'{name} {format_value(value)}')

    def add_labels(self, name: str, labels: Mapping[str, Any] | Iterable[tuple[str, Any]],
                   value: Any = 1) -> None:
        """写一条「标签即全部内容」的 info 型指标（值默认恒为 1）。"""
        label_str = format_labels(labels)
        if label_str:
            self._lines.append(f'{name}{{{label_str}}} {format_value(value)}')
        else:
            self._lines.append(f'{name} {format_value(value)}')

    def extend(self, other: 'MetricWriter') -> None:
        """把另一个 writer 的内容接在后面（用于合并各采集器的输出）。"""
        self._lines.extend(other._lines)

    def render(self) -> str:
        """输出完整文本（以换行结尾）。"""
        if not self._lines:
            return '\n'
        return '\n'.join(self._lines) + '\n'
