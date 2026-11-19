# 采集器指标说明（collector）

本文件说明 `collector/` 采集了哪些数据、每个指标是什么含义、数据从哪来、
以及各指标的口径与陷阱。**改动采集逻辑或新增指标后请同步更新本文件。**

采集器本体：`metrics_exporter.py`（HTTP 服务）+ `host_collector.py`（主机）
+ `gpu_collector.py`（GPU，走 NVML）。

- 启动：`cd collector && python3 metrics_exporter.py --port 9100`
  （**必须在 collector/ 目录内启动**，用的是裸导入 `from host_collector import ...`）
- 端点：`/metrics`（Prometheus 文本格式）、`/health`、`/`
  - `/health` 返回 `OK`（几十字节），是 backen「浏览器侧时延测量」页的探测目标
    （浏览器跨源请求各服务器的 `/health` 并计时，测的是「运行浏览器的那台设备 →
    各服务器」的路径）。该响应额外带 `Timing-Allow-Origin: *` 与
    `Access-Control-Allow-Origin: *`：前者让浏览器的 Resource Timing API 在跨源
    场景下也能给出 DNS/TCP/首字节分段耗时（否则这些字段会被浏览器清零），
    后者让响应体可读——这里只回一个 `OK`，无敏感信息。
    **`/metrics` 不带这两个头**，指标内容不跨源暴露。
- Prometheus 抓取配置见 `prometheus/prometheus.yml`；`cluster` / `location` /
  `instance` 标签由 Prometheus 注入，**采集器自己不上报**。

---

## 一、命名与标签约定

| 约定 | 说明 |
|------|------|
| `node_*` | 主机侧指标，命名对齐 node_exporter 惯例（`node_cpu_seconds_total`、`node_memory_MemTotal_bytes` …） |
| `DCGM_FI_DEV_*` | GPU 指标，命名对齐 NVIDIA DCGM exporter 惯例（`DCGM_FI_DEV_GPU_UTIL` …） |
| `node_gpu_flops_peak` | GPU 峰值算力（自研扩展，DCGM 里没有对应项） |
| `node_cpu_flops_peak` | CPU 峰值算力（自研扩展） |
| `_total` 后缀 | 累计计数器（可 `rate()` / `irate()`） |
| `_bytes` / `_ms` / `_seconds` | 单位分别是字节 / 毫秒 / 秒 |
| 标签 | 只有需要区分的维度才带标签：`device`（块设备）、`interface`（网卡）、`mountpoint`/`fstype`（文件系统）、`zone`/`type`（温度区）、`precision`（算力精度）、`gpu`/`UUID`/…（GPU） |

**标签集不是恒定的**：`node_cpu_info` 的 `max_mhz`/`min_mhz` 只有在 cpufreq
存在时才有；`node_dmi_info` 的 `system_vendor` 只有 root 可读时才有。
跨主机做聚合时要显式处理标签集差异（PromQL 里用 `ignoring(...)` 或先 `sum by(...)`）。

---

## 二、CPU

| 指标 | 标签 | 含义 |
|------|------|------|
| `node_cpu_cores` | — | 逻辑核数（由 `/proc/stat` 的 `cpuN` 行数得出） |
| `node_cpu_seconds_total` | `mode` | 各模式累计 CPU 时间（**秒**）。`mode` ∈ user/nice/system/idle/iowait/irq/softirq/steal，**全核心聚合，没有 cpu 标签** |
| `node_cpu_info` | `model_name,sockets,cores_per_socket,physical_cores,threads_per_core,max_mhz,min_mhz` | 型号与拓扑信息，值恒为 1 |
| `node_cpu_flops_peak` | `precision` | CPU 峰值算力（FLOPS/s），`precision` ∈ fp32/fp64 |

**口径提示**

- `node_cpu_seconds_total` 的值**已经是秒**：内核 `/proc/stat` 给的是 jiffies
  （USER_HZ = 100），采集器已 `/100`。算使用率时：
  `1 - rate(node_cpu_seconds_total{mode="idle"}[1m]) / ignoring(mode) node_cpu_cores`，
  **必须除以核数**（idle 的 rate 取值域是 [0, 核数]），且两侧标签集不同，
  `ignoring(mode)` 不能省。
- `iowait` **不单独扣除**——它表示 CPU 在等 I/O，但仍然消耗了时间片，算「忙碌」。
  这与 `top` / node_exporter 一致。
- `node_cpu_flops_peak` = 物理核数 × 基础频率(Hz) × 每核每周期 FLOP 数。
  - 基础频率取型号名里的 `@ x.xxGHz`（TOP500 惯例以基础频率计，表示「装了多少算力」，
    是静态容量值，不随睿频抖动）；解析不出就**不发这个指标**，不猜。
  - 每核每周期 FLOP：AVX-512 → fp32 32 / fp64 16；AVX2 → fp32 16 / fp64 8；
    兜底 2 / 1。
  - **AMD 护栏**：Zen 4/5 在 `/proc/cpuinfo` 里宣称 `avx512f`，实际是 2×256-bit
    双泵 FMA、没有原生 512-bit 执行单元，按 flags 判定会高估 2 倍。
    因此 avx512 档位只对 GenuineIntel 开放，AMD 落到 avx2 档（两个方向都保守）。

---

## 三、内存

| 指标 | 含义 |
|------|------|
| `node_memory_MemTotal_bytes` | 物理内存总量 |
| `node_memory_MemFree_bytes` | 完全未使用的内存 |
| `node_memory_MemAvailable_bytes` | **无需换出即可分配**的内存（内核估算，估的是「应用还能拿到多少」） |
| `node_memory_SwapTotal_bytes` / `node_memory_SwapFree_bytes` | 交换区总量 / 空闲量 |

内存使用率 = `1 - MemAvailable / MemTotal`（用 `MemAvailable` 而非 `MemFree`，
后者不含可回收的 page cache，会把使用率算高）。

---

## 四、负载与进程

| 指标 | 来源 | 含义 |
|------|------|------|
| `node_load1` / `node_load5` / `node_load15` | `/proc/loadavg` | 1/5/15 分钟平均负载 |
| `node_loadavg_running_process` | `/proc/loadavg` | 运行中进程数（`r`） |
| `node_loadavg_total_process` | `/proc/loadavg` | 总进程数（`/total`） |
| `node_procs_running` | `/proc/stat` | 当前运行态进程数 |
| `node_procs_blocked` | `/proc/stat` | 阻塞在不可中断 IO 的进程数（**D 状态，IO 卡死的信号**） |
| `node_forks_total` | `/proc/stat` | 累计创建的进程数 |
| `node_context_switches_total` | `/proc/stat` | 累计上下文切换次数 |
| `node_boot_time_seconds` | `/proc/stat` | 开机时间（Unix 秒） |

---

## 五、磁盘

> **重要口径**：只上报**最上层**块设备。分区、RAID 成员盘、LVM/device-mapper
> 中间层全部过滤——它们统计的是同一份流量，全量求和会让字节数成倍虚高。
> 判定靠 sysfs 的 `holders`/`slaves`/`partition` 层次关系（`_disk_skip_devices()`），
> 不靠设备名前缀猜。`/sys` 不可见时**不过滤**（宁可少过滤也不丢指标）。
>
> 效果：普通盘 `sda+sda1` → 只留 sda；RAID1 `md0+sda+sdb` → 只留 md0；
> LVM `dm-0+md0+sda` → 只留 dm-0。

| 指标 | 标签 | 含义 |
|------|------|------|
| `node_disk_info` | `device,major,minor` | 设备号信息，值恒为 1 |
| `node_disk_reads_completed_total` | `device` | 读请求完成数 |
| `node_disk_reads_merged_total` | `device` | 相邻读请求被合并的次数（合并率高 = 顺序读） |
| `node_disk_read_bytes_total` | `device` | 读取字节数（**由扇区数 ×512 换算**） |
| `node_disk_read_time_seconds_total` | `device` | 读操作耗时累计 |
| `node_disk_writes_completed_total` | `device` | 写请求完成数 |
| `node_disk_writes_merged_total` | `device` | 相邻写请求被合并的次数 |
| `node_disk_written_bytes_total` | `device` | 写入字节数（扇区数 ×512） |
| `node_disk_write_time_seconds_total` | `device` | 写操作耗时累计 |
| `node_disk_io_time_seconds_total` | `device` | IO 忙碌时间累计（毫秒转秒） |
| `node_disk_queue_total_wait_seconds_total` | `device` | 排队等待时间累计（**延迟的直接来源**） |
| `node_disk_in_flight_requests` | `device` | 当前在途 IO 数（瞬时量） |
| `node_disk_discards_completed_total` | `device` | discard 操作次数（**SSD 写入放大/寿命的关键信号**） |
| `node_disk_discards_merged_total` | `device` | discard 合并次数 |
| `node_disk_discards_bytes_total` | `device` | discard 字节数 |
| `node_disk_discards_time_seconds_total` | `device` | discard 耗时累计 |
| `node_disk_flushes_completed_total` | `device` | flush（落盘）次数 |
| `node_disk_flushes_time_seconds_total` | `device` | flush 耗时累计 |

**口径提示**

- `discard_*` / `flush_*` 是 `/proc/diskstats` 的扩展列，**不是所有设备/内核都提供**。
  没有这些列时**不发射**这批指标（区别于「支持但计数为 0」）。
- 主机详情页的「磁盘 IO 速率」= `sum(rate(node_disk_read_bytes_total[5m]))`
  + `sum(rate(node_disk_written_bytes_total[5m]))`。

---

## 六、网络

网卡级累计计数，来自 `/proc/net/dev`。**已过滤** `lo`、`br*`（网桥）、
`docker*`、`veth*`、`b.*`（bond 以外的旧式命名）；`bond*` 保留（是真实网卡）。

| 指标 | 标签 | 含义 |
|------|------|------|
| `node_network_receive_bytes_total` | `interface` | 接收字节数 |
| `node_network_receive_packets_total` | `interface` | 接收包数 |
| `node_network_receive_errs_total` | `interface` | 接收错误数（网卡层 CRC/帧错等） |
| `node_network_receive_drop_total` | `interface` | 接收丢包数（**内核缓冲区满导致，与协议层无关**） |
| `node_network_transmit_bytes_total` | `interface` | 发送字节数 |
| `node_network_transmit_packets_total` | `interface` | 发送包数 |
| `node_network_transmit_errs_total` | `interface` | 发送错误数 |
| `node_network_transmit_drop_total` | `interface` | 发送丢包数 |

---

## 七、文件系统

来自 `/proc/mounts` + `statvfs`。只保留设备路径含 `/dev` 的挂载点（排除 tmpfs、
overlay 等伪文件系统），同一设备挂多处时**只报第一个挂载点**（避免容量重复累加）。

| 指标 | 标签 | 含义 |
|------|------|------|
| `node_filesystem_size_bytes` | `mountpoint,device,fstype` | 文件系统总容量 |
| `node_filesystem_avail_bytes` | `mountpoint,device,fstype` | 非特权用户可用的空间（`f_bavail`） |
| `node_filesystem_free_bytes` | `mountpoint,device,fstype` | 含 root 保留块在内的空闲空间（`f_bfree`） |

`size - avail` ≠ 已用：`avail` 不含 root 保留块，两者差值是保留块。

---

## 八、温度

| 指标 | 标签 | 含义 |
|------|------|------|
| `node_thermal_zone_temp` | `zone,type` | 温度区当前温度（**°C**，内核给毫秒，已 /1000） |

来自 `/sys/class/thermal/thermal_zone*`。没有温度传感器的机器不会有这个指标。

---

## 九、静态信息

| 指标 | 标签 | 来源 | 说明 |
|------|------|------|------|
| `node_uname_info` | `sysname,nodename,release,version,machine` | `platform.uname()` | **`nodename` 就是主机名**，端口映射场景下区分同 IP 多台机器的唯一稳定标识 |
| `node_os_info` | `pretty_name,name,version_id,version,version_codename,id,id_like,…` | `/etc/os-release` | 键名统一转小写（对齐 node_exporter 惯例） |
| `node_dmi_info` | `bios_date,bios_vendor,bios_version,product_name[,system_vendor]` | `/sys/class/dmi/id/*` | `system_vendor` 多为 0400 root-only，非 root 运行时读不到；调用端应回退到 `bios_vendor` |
| `node_time_seconds` | — | 采集时刻 | 抓取时刻的 Unix 秒 |

---

## 十、GPU（DCGM_FI_DEV_*）

通过 `pynvml`（NVML）采集，命名对齐 NVIDIA DCGM exporter。所有指标都带
**同一组标签**：`gpu`（卡序号）、`UUID`、`pci_bus_id`、`device`、`modelName`、
`hostname`、`DCGM_FI_DRIVER_VERSION`。

| 指标 | 含义 |
|------|------|
| `DCGM_FI_DEV_GPU_UTIL` | GPU 利用率（%） |
| `DCGM_FI_DEV_GPU_TEMP` | GPU 核心温度（°C） |
| `DCGM_FI_DEV_MEMORY_TEMP` | 显存温度（°C） |
| `DCGM_FI_DEV_FB_USED` / `DCGM_FI_DEV_FB_FREE` / `DCGM_FI_DEV_FB_RESERVED` | 显存已用 / 空闲 / 保留（MiB） |
| `DCGM_FI_DEV_SM_CLOCK` / `DCGM_FI_DEV_MEM_CLOCK` | SM 频率 / 显存频率（MHz） |
| `DCGM_FI_DEV_POWER_USAGE` | 功耗（mW） |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | 累计能耗（mJ） |
| `DCGM_FI_DEV_DEC_UTIL` / `DCGM_FI_DEV_ENC_UTIL` | 编码器 / 解码器利用率（%） |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | 显存拷贝利用率（%） |
| `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | PCIe 重传计数 |
| `DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS` / `DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS` | 可纠正 / 不可纠正的显存行重映射次数（ECC 错误） |
| `DCGM_FI_DEV_ROW_REMAP_FAILURE` | 行重映射已失效（显存即将报废） |
| `DCGM_FI_DEV_VGPU_LICENSE_STATUS` | vGPU 许可状态 |
| `node_gpu_flops_peak` | GPU 峰值算力（FLOPS/s），标签同上 + `precision` ∈ fp32/fp64/fp16 |

**口径提示**

- **pynvml 是可选依赖**（`nvidia-ml-py`）。没装或 NVML 初始化失败时，**只有
  这批 GPU 指标缺失**，主机侧 `node_*` 照常上报；exporter 启动时会打一条
  `[WARN]`。主机没装驱动 ≠ 采集器坏了，看日志能区分。
- `node_gpu_flops_peak` 按**型号规格表**查得（NVML 没有 FLOPS 接口）：
  fp32/fp64/fp16 三个口径，一律取 dense（稠密）值，**不含 2:4 结构化稀疏**
  （规格书常把稀疏值标 `*`，混用会让集群总数失去可比性）。
  型号未收录 → **不发这个指标**；页面据此区分「这台机器没有 GPU 卡」与
  「有卡但型号未收录」，不会静默当成 0。

---

## 十一、协议栈计数（node_snmp_* / node_netstat_* / node_sockstat_*）

这三族是**内核协议栈的累计计数器**，由 `/proc/net/snmp`、`/proc/net/netstat`、
`/proc/net/sockstat` 平铺而来。命名规则：

```
node_<表名>_<协议>_<字段名>      例：node_snmp_Tcp_AttemptFails
node_netstat_TcpExt_ListenDrops
node_sockstat_TCP_inuse
```

字段集合由内核版本决定（不同内核的 `/proc/net/netstat` 行数不同），因此
**指标数量随内核变化**，本文件不逐个列举（共约 280 个）。常用的：

| 指标 | 含义 | 用途 |
|------|------|------|
| `node_snmp_Tcp_AttemptFails` | TCP 连接尝试失败数 | **抗扰动维度**：连接失败率分子 |
| `node_snmp_Tcp_ActiveOpens` | 客户端主动建连数 | 抗扰动：新建连接（分母的一部分） |
| `node_snmp_Tcp_PassiveOpens` | 服务端被动建连数 | 同上 |
| `node_snmp_Tcp_CurrEstab` / `EstabResets` / `RetransSegs` | 当前连接数 / 重置数 / 重传段数 | 连接健康度 |
| `node_netstat_TcpExt_ListenDrops` | accept 队列溢出丢弃数 | **抗扰动维度**：连接洪泛的最硬证据 |
| `node_netstat_TcpExt_ListenOverflows` | accept 队列溢出次数 | 同上 |
| `node_netstat_TcpExt_TCPSynRetrans` | SYN 重传数 | 丢包表征（**已由「丢包率」维度覆盖，评分时不重复计**） |
| `node_netstat_IpExt_InNoRoutes` | 无可用路由的报文数 | 抗扰动：扫描/路由误配迹象 |
| `node_sockstat_TCP_inuse` / `TCP_tw` / `TCP_orphan` / `TCP_alloc` | TCP socket 状态计数 | 连接堆积 |

> **重复曝光陷阱**：同一个 series 被两条路径各发一次，会违反 Prometheus 曝光
> 格式的「同一标签集不得重复出现」，导致**整次抓取被拒**、该 target 的**全部**
> 指标丢失（不只是重复的那一条）。采集器里对此有显式注释，勿动。
>
> 具体到 TCP：现代内核（4.x 起）的 `/proc/net/netstat` 已不含 `Tcp:` 段，
> 基础 TCP 计数器只在 `/proc/net/snmp` 里，因此 `CurrEstab` 只有
> `node_snmp_Tcp_CurrEstab` 一条；`/proc/net/sockstat` 口径的在途连接数是
> `node_sockstat_TCP_inuse`——两者名字与含义都不同，不会冲突。
> **若要新增显式发射的 `node_snmp_*` / `node_netstat_*` 指标，务必先确认
> `_emit_proto_flat` 不会同时产出同名 series。**

---

## 十二、算力（FLOPS）汇总

| 指标 | 含义 |
|------|------|
| `node_cpu_flops_peak{precision}` | CPU 峰值算力（FLOPS/s），fp32/fp64 |
| `node_gpu_flops_peak{...,precision}` | 单卡峰值算力（FLOPS/s），fp32/fp64/fp16 |

**已用算力不采集**，由「峰值 × 设备利用率」在后端算出。CPU 与 GPU 的相加
放在 Python 侧做，**不写进 PromQL**：两族指标标签集不同（GPU 多了
gpu/UUID/modelName/pci_bus_id），跨族相加要写 `on(instance) group_left()`，
极易写错且**错得静默**。

---

## 十三、消费方对照（backen 的 PromQL）

| backen 用途 | 指标 |
|-------------|------|
| 集群/主机 CPU 使用率 | `node_cpu_seconds_total` + `node_cpu_cores`（须 `ignoring(mode)`） |
| 内存使用率 | `node_memory_MemAvailable_bytes` / `node_memory_MemTotal_bytes` |
| 负载 | `node_load1` |
| 磁盘 IO 速率 | `node_disk_read_bytes_total` + `node_disk_written_bytes_total`（`rate()`） |
| 磁盘容量 | `node_filesystem_size_bytes` |
| 温度 | `node_thermal_zone_temp` |
| 进程数 | `node_procs_running` |
| 网卡吞吐 | `node_network_receive_bytes_total` / `node_network_transmit_bytes_total` |
| GPU 利用率 / 卡数 / 型号 | `DCGM_FI_DEV_GPU_UTIL`（`count by(cluster)` = 卡数） |
| 峰值算力 | `node_cpu_flops_peak` / `node_gpu_flops_peak` |
| 抗扰动（代理指标） | `node_snmp_Tcp_AttemptFails` / `node_snmp_Tcp_{Active,Passive}Opens` / `node_netstat_TcpExt_ListenDrops` / `node_netstat_IpExt_InNoRoutes` |
| 主机名（端口映射下的主标识） | `node_uname_info{nodename=...}` |

---

## 十四、验证

```bash
cd collector && python -m py_compile host_collector.py metrics_exporter.py gpu_collector.py

# 本地起一个实例看输出
python3 metrics_exporter.py --port 9199 &
curl -s localhost:9199/metrics | head -40

# 检查是否有重复标签集（重复会导致 Prometheus 拒收整次抓取）
curl -s localhost:9199/metrics | grep -v '^#' | grep -v '^$' | \
  sed 's/ [0-9.e+-]*$//' | sort | uniq -d
```

端到端：起 collector → Prometheus 目标 `up` → `backen` 的
`/api/metrics/overview` 非空。

> **提示**：PromQL 写错是**静默失败**——指标名写错返回空向量、聚合函数大写
> 返回 422，两者都会被后端 `raise_for_status()` 吞掉，表现为指标卡显示 "—"
> 且**无任何报错**。改 PromQL 后若卡片仍空，先确认指标在采集器 `/metrics`
> 中真实存在。
