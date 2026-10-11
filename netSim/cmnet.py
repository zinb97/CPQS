"""
Mininet Topology Generator - 中国移动 CMNET 骨干网拓扑
- 与 netSim.py（ChinaNet/中国电信）解耦，独立文件
- 层级：超级核心 → 大区中心 → 省网核心 → 城域接入
- 规模可选：small/medium/large/full 四档（控制省数）
- DPID 规则与项目 ChinaNet 一致：00000000{L:02x}{idx:02x}0001
- 兼容新版 Mininet（`addSwitch`/`addHost` 仅返回名字字符串）

修复记录（与 `netSim_cmnet.py` 同源，作为净版）：
    1. `_build_region` 原 `pairs` 误把 super_core code ('gz','sh') 当 region，
       导致 KeyError。改为以 5 个 region code 构成的 pentagon 环。
    2. 节点容器不再存 Node 对象，改存 name 字符串（原 Mininet 老 API 中
       `addSwitch` 返回 Node；但当前 Mininet 仅返回 name，
       原代码 `s.name` 在新 API 全部炸 `AttributeError`）。
    3. 省 → 父节点反查改为直接构造名字 `S{p_idx}`/`R{p_idx}`，去掉
       `name_to_switch` 字典。
    4. metro 层 DPID 由 14 hex 修正为 16 hex（与上层一致）。
    5. `province_to_parent` 显式补 'bj'→S1、'sh'→S2（原代码靠
       `dict.get(...,default)` 兜底，'sh' 被错误回退到 S1 北京）。

用法：
    sudo python3 netSim_cmnet.py --scale small
    sudo python3 netSim_cmnet.py --scale medium --prov-depth 2 --prov-width 2

注意：实际 `Mininet.start()` 需 sudo（创建 netns、调用 ovs-vsctl）；
    pure-python 阶段（仅构造 Topo）不需要。
"""
import argparse
from enum import Enum
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

CLI.prompt = 'cmnet> '


# ==============================================================================
# 常量定义
# ==============================================================================
class CMNetLevel(Enum):
    """CMNET 层级标识（写入 DPID 第 1 位）"""
    SUPER_CORE = 1  # 超级核心（北京/上海/广州）
    REGION     = 2  # 大区中心
    PROVINCE   = 3  # 省网核心
    METRO      = 4  # 城域接入


# 3 超级核心（CMNET 全国三大国际出口/超级核心）
SUPER_CORE_CITIES = [
    ('bj', 1, '北京'),
    ('sh', 2, '上海'),
    ('gz', 3, '广州'),
]

# 5 大区中心（CMNET 骨干大区节点）
REGION_CITIES = [
    ('wh', 4, '武汉'),
    ('cd', 5, '成都'),
    ('xa', 6, '西安'),
    ('nj', 7, '南京'),
    ('sy', 8, '沈阳'),
]

# 31 省/直辖市/自治区（按 GB/T 2260 排序）
PROVINCES = [
    ('bj',  1, '北京'),   ('tj',  2, '天津'),   ('sh',  3, '上海'),
    ('cq',  4, '重庆'),   ('hb',  5, '河北'),   ('sx',  6, '山西'),
    ('ln',  7, '辽宁'),   ('jl',  8, '吉林'),   ('hl',  9, '黑龙江'),
    ('js', 10, '江苏'),   ('zj', 11, '浙江'),   ('ah', 12, '安徽'),
    ('fj', 13, '福建'),   ('jx', 14, '江西'),   ('sd', 15, '山东'),
    ('ha', 16, '河南'),   ('hb2',17, '湖北'),   ('hn', 18, '湖南'),
    ('gd', 19, '广东'),   ('gx', 20, '广西'),   ('hn2',21, '海南'),
    ('sc', 22, '四川'),   ('gz2',23, '贵州'),   ('yn', 24, '云南'),
    ('xz', 25, '西藏'),   ('sn', 26, '陕西'),   ('gs', 27, '甘肃'),
    ('qh', 28, '青海'),   ('nx', 29, '宁夏'),   ('xj', 30, '新疆'),
    ('nm', 31, '内蒙古'),
]

# 规模档位 → 启用的省数
SCALE_PROVINCES = {
    'small':  5,    # 演示用，5 省
    'medium': 10,   # 区域测试，10 省
    'large':  23,   # 多数省份
    'full':   31,   # 全量 31 省
}


# ==============================================================================
# DPID 工具
# ==============================================================================
def _dpid(level, idx):
    """生成 16 hex 字符的 DPID，规则：`00000000{L:02x}{idx:06x}`。

    dp 前 8 个 0 是 32 位“零厂商”前缀；后 8 hex = level (1B) + idx (3B)。
    idx 编码语义：
        L1 超级核心 idx = (super_idx << 16) | 1
        L2 大区     idx = (region_idx << 16) | 1
        L3 省网     idx = (prov_idx  << 16) | 1
        L4 城域     idx = (prov_idx  << 16) | (depth << 8) | pos
            （depth=1 表示第一层城域, pos 为该层内的位号 1..N）

    最大 idx ≈ 31<<16 + 4<<8 + 6 = 0x1F0106 ≪ 0xFFFFFF，6 hex 字符够用。
    """
    return f'00000000{level:02x}{int(idx):06x}'


# ==============================================================================
# CMNET 拓扑类
# ==============================================================================
class CMNetTopo(Topo):
    """
    中国移动 CMNET 骨干网拓扑

    层级：
      L1 超级核心 (3 节点：北京/上海/广州，全互联)
      L2 大区中心 (5 节点：武汉/成都/西安/南京/沈阳，pentagon 环 + 上联超级核心)
      L3 省网核心 (5/10/23/31 节点，根据 scale)
      L4 城域接入 (每省 prov_width×prov_depth 树形)

    DPID 规则（与 ChinaNet 一致，十六进制 8 字节）：
      00000000{L:02x}{idx:06x}
      L1=超级核心 L2=大区 L3=省 L4=城域

    命名：
      S1~S3   超级核心
      R4~R8   大区
      P1~P31  省网
      M{a}{b}{c?}{d?}  城域（a=省号, b=第1层位号; c=层深, d=层内位号，可省略）

    链路参数（bw Mbps / delay ms / loss %）：
      超级核心互联  400 / 18~30 / 0.1
      大区上联核心  400 / 5~15 / 0.1
      大区双平面    200 / 8~12 / 0.1
      省上联        200 / 3~8  / 0.1
      城域接入      100 / 1~2  / 0
    """
    def __init__(self, scale='medium', prov_depth=2, prov_width=2, **params):
        if scale not in SCALE_PROVINCES:
            raise ValueError(
                f"scale 必须是 {list(SCALE_PROVINCES.keys())} 之一，得到 {scale!r}"
            )
        if prov_depth < 0 or prov_depth > 4:
            raise ValueError('prov_depth 必须在 [0, 4]')
        if prov_width < 1 or prov_width > 6:
            raise ValueError('prov_width 必须在 [1, 6]')

        self.scale      = scale
        self.prov_cnt   = SCALE_PROVINCES[scale]
        self.prov_depth = prov_depth
        self.prov_width = prov_width

        # 节点容器：仅存 name（字符串），兼容新旧 Mininet API。
        # （Mininet 2.x+ 的 `addSwitch` 仅返回 name 字符串；老 API 行为
        #   已废弃，原脚本假定返回值带 `.name` 会 AttributeError。）
        self.super_core = {}   # code -> str(Switch name)
        self.region     = {}   # code -> str(Switch name)
        self.province   = {}   # code -> str(Switch name)
        self.metro      = []   # list[(prov_idx, str(Metro name))]

        super().__init__(**params)
        self.build_topo()

    # ---------- 构建入口 ----------
    def build_topo(self):
        info(f'\n*** CMNET 拓扑 | 规模={self.scale}({self.prov_cnt}省) '
             f'| 城域树(深{self.prov_depth}×宽{self.prov_width})\n')
        self._build_super_core()
        self._build_region()
        self._build_province()
        if self.prov_depth > 0:
            self._build_metro()

    # ---------- L1 超级核心 ----------
    def _build_super_core(self):
        for code, idx, name in SUPER_CORE_CITIES:
            sw_name = f'S{idx}'
            dpid = _dpid(CMNetLevel.SUPER_CORE.value, idx * 0x10000 + 1)
            self.super_core[code] = self.addSwitch(
                sw_name, dpid=dpid, protocols='OpenFlow13',
            )
            info(f'  [L1 超级核心] {sw_name} {name}  dpid={dpid}\n')

        # 全互联（现实参考：北京-上海 RTT ~25ms、北京-广州 ~30ms、上海-广州 ~18ms）
        self._add(self.super_core['bj'], self.super_core['sh'], 400, '25ms', 0.1)
        self._add(self.super_core['bj'], self.super_core['gz'], 400, '30ms', 0.1)
        self._add(self.super_core['sh'], self.super_core['gz'], 400, '18ms', 0.1)

        # 每个超级核心挂 1 个测试主机
        for i, code in enumerate(['bj', 'sh', 'gz']):
            host_name = self.addHost(f'ts{i+1}', ip=f'10.0.1.10{i+1}/24')
            self.addLink(self.super_core[code], host_name)

    # ---------- L2 大区中心 ----------
    def _build_region(self):
        for code, idx, name in REGION_CITIES:
            sw_name = f'R{idx}'
            dpid = _dpid(CMNetLevel.REGION.value, idx * 0x10000 + 1)
            self.region[code] = self.addSwitch(
                sw_name, dpid=dpid, protocols='OpenFlow13',
            )
            info(f'  [L2 大区]     {sw_name} {name}  dpid={dpid}\n')

        # 大区上联最近的超级核心（按地理就近）
        uplink = [
            ('wh', 'bj', '15ms'),  # 武汉  -> 北京
            ('cd', 'gz', '20ms'),  # 成都  -> 广州
            ('xa', 'bj', '12ms'),  # 西安  -> 北京
            ('nj', 'sh', '5ms'),   # 南京  -> 上海
            ('sy', 'bj', '10ms'),  # 沈阳  -> 北京
        ]
        for code, core_code, delay in uplink:
            self._add(self.region[code], self.super_core[core_code], 400, delay, 0.1)

        # 大区之间 pentagon 环（双平面保护）。
        # 原脚本此处把 super_core code ('gz','sh') 当成 region code 用，导致
        # `self.region['gz']` KeyError。改用全 5 个 region code，绕一圈闭合。
        pairs = [
            ('wh', 'xa'), ('xa', 'cd'), ('cd', 'sy'),
            ('sy', 'nj'), ('nj', 'wh'),
        ]
        for a, b in pairs:
            self._add(self.region[a], self.region[b], 200, '10ms', 0.1)

    # ---------- L3 省网核心 ----------
    def _build_province(self):
        # 省 → 父节点 (大区或超级核心)，按现实地理就近映射
        # 北京上海两市：所在省 SW 上联到本市 S1/S2（同地但不形成自环）
        province_to_parent = {
            # 华北 -> S1 北京
            'bj': ('S', 1), 'tj': ('S', 1), 'hb': ('S', 1),
            'sx': ('S', 1), 'nm': ('S', 1), 'xj': ('S', 1),
            # 东北 -> R8 沈阳
            'ln': ('R', 8), 'jl': ('R', 8), 'hl': ('R', 8),
            # 华东 -> S2 上海 / R7 南京
            'sh': ('S', 2), 'js': ('R', 7), 'zj': ('S', 2), 'ah': ('R', 7),
            'fj': ('S', 2), 'jx': ('R', 7), 'sd': ('S', 1),
            # 华中 -> R4 武汉
            'ha': ('R', 4), 'hb2': ('R', 4), 'hn': ('R', 4),
            # 华南 -> S3 广州
            'gd': ('S', 3), 'gx': ('S', 3), 'hn2': ('S', 3),
            # 西南 -> R5 成都
            'cq': ('R', 5), 'sc': ('R', 5), 'gz2': ('R', 5), 'yn': ('R', 5),
            # 西北 -> R6 西安
            'xz': ('R', 6), 'sn': ('R', 6), 'gs': ('R', 6),
            'qh': ('R', 6), 'nx': ('R', 6),
        }

        for code, idx, name in PROVINCES[:self.prov_cnt]:
            sw_name = f'P{idx}'
            dpid = _dpid(CMNetLevel.PROVINCE.value, idx * 0x10000 + 1)
            self.province[code] = self.addSwitch(
                sw_name, dpid=dpid, protocols='OpenFlow13'
            )
            info(f'  [L3 省网]     {sw_name} {name}  dpid={dpid}\n')

            # 上联父节点：直接用名字定位
            kind, p_idx = province_to_parent.get(code, ('S', 1))
            parent_name = f'S{p_idx}' if kind == 'S' else f'R{p_idx}'
            self._add(self.province[code], parent_name, 200, '5ms', 0.1)

            # 每省 1 个主机
            host_name = self.addHost(f'hp{idx}', ip=f'10.0.2.{idx}/24')
            self.addLink(self.province[code], host_name)

    # ---------- L4 城域接入 ----------
    def _build_metro(self):
        # 每省下挂 prov_width × prov_depth 树
        for code, prov_sw_name in self.province.items():
            prov_idx = next(i for c, i, _ in PROVINCES if c == code)
            # 第 1 层城域直接连省核心
            layer1 = []
            for w in range(1, self.prov_width + 1):
                sw_name = f'M{prov_idx}1{w}'
                dpid = _dpid(CMNetLevel.METRO.value,
                             (prov_idx << 16) | (1 << 8) | w)
                self.addSwitch(sw_name, dpid=dpid, protocols='OpenFlow13')
                self._add(prov_sw_name, sw_name, 100, '1ms', 0)
                layer1.append(sw_name)

            # 后续层
            current = layer1
            for d in range(2, self.prov_depth + 1):
                nxt = []
                for i, parent_name in enumerate(current, start=1):
                    sw_name = f'M{prov_idx}{d}{i}'
                    dpid = _dpid(CMNetLevel.METRO.value,
                                 (prov_idx << 16) | (d << 8) | i)
                    self.addSwitch(sw_name, dpid=dpid, protocols='OpenFlow13')
                    self._add(parent_name, sw_name, 40, '1ms', 0)
                    nxt.append(sw_name)
                current = nxt

            # 末层挂主机
            for i, sw_name in enumerate(current, start=1):
                host_name = self.addHost(
                    f'hm{prov_idx}{i}', ip=f'10.0.3.{prov_idx}{i:02d}/24'
                )
                self.addLink(sw_name, host_name)
                self.metro.append((prov_idx, sw_name))

    # ---------- 辅助 ----------
    def _add(self, a, b, bw, delay, loss):
        self.addLink(a, b, bw=bw, delay=delay, loss=loss, cls=TCLink)


# ==============================================================================
# 拓扑注册 & 启动入口
# ==============================================================================
topos = {
    'cmnet': CMNetTopo,
    'cmnet-small':  lambda **kw: CMNetTopo(scale='small',  **kw),
    'cmnet-medium': lambda **kw: CMNetTopo(scale='medium', **kw),
    'cmnet-large':  lambda **kw: CMNetTopo(scale='large',  **kw),
    'cmnet-full':   lambda **kw: CMNetTopo(scale='full',   **kw),
}


def run_cmnet(scale='medium', prov_depth=2, prov_width=2, controller_ip='127.0.0.1'):
    """启动 CMNET 拓扑"""
    topo = CMNetTopo(scale=scale, prov_depth=prov_depth, prov_width=prov_width)
    switch = partial(OVSSwitch, protocols='OpenFlow13')
    net = Mininet(
        topo=topo,
        controller=lambda n: RemoteController(n, ip=controller_ip),
        switch=switch,
    )
    net.start()
    info(
        f'\n✅ CMNET 拓扑启动完成 | scale={scale} '
        f'| 省数={topo.prov_cnt} | 城域树(深{prov_depth}×宽{prov_width})\n'
        f'   节点统计：超级核心 {len(topo.super_core)} | '
        f'大区 {len(topo.region)} | '
        f'省网 {len(topo.province)} | '
        f'城域 {len(topo.metro)}\n'
    )
    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    parser = argparse.ArgumentParser(description='Mininet CMNET 拓扑仿真工具')
    parser.add_argument(
        '-s', '--scale',
        default='medium',
        choices=list(SCALE_PROVINCES.keys()),
        help='规模档位（控制省数）：small=5 medium=10 large=23 full=31',
    )
    parser.add_argument(
        '--prov-depth', type=int, default=2,
        help='每省城域树深度（0=不建城域，1~4）',
    )
    parser.add_argument(
        '--prov-width', type=int, default=2,
        help='每省城域树每层宽度（1~6）',
    )
    parser.add_argument(
        '--controller-ip', default='127.0.0.1',
        help='RYU/ONOS 控制器 IP',
    )
    args = parser.parse_args()
    run_cmnet(
        scale=args.scale,
        prov_depth=args.prov_depth,
        prov_width=args.prov_width,
        controller_ip=args.controller_ip,
    )
