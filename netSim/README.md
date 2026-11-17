# netSim/ — Mininet 仿真拓扑

本目录提供 CMNET(中国移动骨干网)的 Mininet 仿真脚本,与仓库其他模块无强依赖。

```
netSim/
└── cmnet.py        # CMNetTopo + run_cmnet()
```

| 入口 | 作用 |
|------|------|
| `python3 cmnet.py --help` | 仅做 argparse + 模块导入检查(无需 sudo) |
| `sudo python3 cmnet.py [-s scale] [--prov-depth N] [--prov-width N]` | 启动 CMNET Mininet + CLI(`net.start()` 需要 root) |

## 规模档位

| `--scale` | 省数 | 默认 | 用途 |
|-----------|------|------|------|
| `small`  | 5  | ✓ | 演示 |
| `medium` | 10 | ✓ | 区域测试 |
| `large`  | 23 | ✓ | 多数省份 |
| `full`   | 31 | ✓ | 全量 |

`--prov-depth`/`--prov-width` 控制每省城域树(`1≤width≤6`、`0≤depth≤4`)。

## 拓扑层级

```
L1 超级核心  S1~S3   (北京/上海/广州,全互联)
L2 大区中心  R4~R8   (武汉/成都/西安/南京/沈阳,5 节点 pentagon 环)
L3 省网核心  P1~P31  (按 GB/T 2260)
L4 城域接入  M…      (树形, depth × width)
```

## 主机 IP 网段与路由

主机分属不同 /24，**但全部挂在同一二层广播域**（Mininet 默认所有交换机同属一个 L2，
QoSController 的 `handle_arp` 也会把未知目的 IP 的 ARP 泛洪到所有交换机）：

| 主机 | 网段 | 示例 |
|------|------|------|
| `ts1~ts3` | `10.0.1.0/24` | `ts1` = `10.0.1.101` |
| `hp1~hpN` | `10.0.2.0/24` | `hp1` = `10.0.2.1` |
| `hm{省}{序号}` | `10.1.{省号}.0/24`（每省一个网段） | `hm101` = `10.1.10.1` |

Mininet **不会**为跨网段主机自动配置路由，`ip route` 里只有内核生成的那一条
`scope link` 直连路由，于是 `pingall` 表现为**同网段通、跨网段全 X**——是路由表
问题，不是链路/二层问题。

`cmnet.py` 启动时（`net.start()` 之后）自动执行 `setup_host_routes()`，为每台主机补：

```
ip route replace <其它每个网段> dev <本机接口>     # 各网段直连路由（link scope）
ip route replace default dev <本机接口>            # 默认路由（兜底）
```

`dev` 形式（无 next-hop）的路由是 link scope，邻居解析（ARP）就在该接口上完成；
由于共用同一二层域，ARP 请求会被交换机泛洪到目的主机并得到直接应答，**不需要额外
引入网关/路由器节点**（引入会污染拓扑、DPID 与 QoS 的主机采集）。

`--no-host-routes` 可跳过自动配置。

## 与 `controller/` 配合

1. 终端 A 启动 RYU 控制器:
   ```bash
   cd /path/to/CPQS
   python3.8 controller/app.py controller/ofctl_rest.py --wsapi-port 8080 --observe-links
   ```

2. 终端 B 启动 Mininet(用 `python3` 而非 `python3.8` — mininet 装在 3.10 上):
   ```bash
   cd /path/to/CPQS
   sudo python3 cmnet.py --scale medium --prov-depth 2 --prov-width 2 --controller-ip 127.0.0.1
   ```

3. RYU 应自动发现全部交换机并按 `--observe-links` 解析 LLDP 拓扑。

## 2026-10 重构修复记录

原始 `netSim/cmnet.py` 在新版 Mininet(2.3+ / 3.x)上有 4 类致命问题,已修复:

1. **`_build_region` `pairs` 误用 super_core code**
   原 `pairs` 用了 `'gz'`、`'sh'` 当 region 节点,但 `self.region` 只有
   `('wh','cd','xa','nj','sy')`,导致 `KeyError: 'gz'`。
   改为以全 5 个 region code 构成 pentagon 环 (`wh-xa-cd-sy-nj-wh`)。

2. **节点容器存的对象类型错误**
   原代码假定 `addSwitch()` 返回 Node 对象(`s.name` 可用),但当前 Mininet
   的 `addSwitch` 仅返回 name 字符串。`name_to_switch = {s.name: s for s in ...}`
   在新 API 上立刻 `AttributeError: 'str' object has no attribute 'name'`。
   改为:`self.super_core['bj']` 直接存名字字符串;`name_to_switch` 反查表彻底删除,
   上联父节点改成直接构造 `S{p_idx}`/`R{p_idx}` 名字。

3. **Metro 层 DPID 长度不对(14 hex)**
   原 metro dpid `00000000{04}{prov_idx:02x}{w:02x}` 只有 14 hex,
   与上层 16 hex 不一致。改为 `_dpid(level, idx)` 工具函数,统一 `00000000{L:02x}{idx:06x}`
   (16 hex)。`idx` 编码:
    - L1~L3: `(super_or_region_or_prov_idx << 16) | 1`
    - L4 metro: `(prov_idx << 16) | (depth << 8) | pos`

4. **`province_to_parent` 缺失 'bj' / 'sh' 显式映射**
   原代码靠 `dict.get(code, ('S', 1))` 兜底,导致 'sh' (上海) 被错误回退到 S1 北京。
   显式补齐:'bj' → S1, 'sh' → S2。

修复后可通过 dryrun 验证(无需 sudo):
```bash
python3 -c "
import importlib.util as u
m = u.module_from_spec(u.spec_from_file_location('m','netSim/cmnet.py'))
u.module_from_spec(u.spec_from_file_location('m','netSim/cmnet.py')).loader.exec_module(m)
t = m.CMNetTopo(scale='medium', prov_depth=2, prov_width=2)
print('sw=', len(t.switches()), 'ln=', len(t.links()), 'ho=', len(t.hosts()))
"
# 输出:sw= 58 ln= 96 ho= 33
```

注意:本目录 `__init__.py` 不存在。`netSim_cmnet.py` 顶层符号链接(`git show R`)
关联至 `netSim/cmnet.py`,两者同源。
