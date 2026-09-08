# SSD 自动化测试脚本说明文档

## 一、项目概述

本脚本面向 Linux (Ubuntu) 平台下的 SSD 测试工程师，覆盖以下六大测试项目：

| 序号 | 测试项目 | 英文名称 | 核心目标 |
|------|----------|----------|----------|
| 1 | 现场固件升级/降级 | Field Firmware Upgrade/Downgrade | 验证固件正常烧写、升降级兼容性、烧写后无掉盘/崩溃/功能异常，版本变更后功能正常 |
| 2 | 设备智能健康信息 | Device SMART Health Information | 确认 OS 枚举 SSD，获取完整 SMART，检查电源循环/通电时间/温度/可用备件/介质错误，R/W 后复检 SMART |
| 3 | 设备容量 | Devices capacity | 通过 nvme-cli / 磁盘工具读取并校验容量 |
| 4 | 完整性能特征 | Full Performance Characterization | FOB（出厂空白）与稳态两种状态下测量带宽、IOPS、延迟、QoS |
| 5 | 正常电源循环测试 | Normal Power Cycle Test | 持续混合读写下正常关机→断电→开机，循环检查磁盘/分区/文件系统/数据完整性，最终完整功能测试 |
| 6 | 意外电源循环测试 | Surprise Power Cycle Test (SPOR) | SSD读写过程中直接硬件断电（不待机、不sync），验证PLP掉电保护对FTL元数据的备份能力，上电后数据完整性校验 |

脚本语言：Python 3，依赖标准 Linux 工具链（nvme-cli、smartmontools、fio、ipmitool、pyserial）。

---

## 二、运行环境准备

### 2.1 系统要求

- 操作系统：Ubuntu 20.04 / 22.04 / 24.04 LTS（x86_64 或 arm64）
- 内核版本：≥ 5.4（NVMe 驱动稳定）
- Python：≥ 3.8
- 权限：需要 root 或 sudo 权限（操作块设备、安装固件、fio 直写）
- 待测 SSD：NVMe SSD（/dev/nvmeXnY）或 SATA SSD（/dev/sdX）

### 2.2 系统工具安装

```bash
# 更新软件源
sudo apt update

# 安装 NVMe 命令行工具（固件升级、SMART、容量读取核心依赖）
sudo apt install -y nvme-cli

# 安装 SMART 监控工具（SATA/NVMe 通用 SMART 读取）
sudo apt install -y smartmontools

# 安装性能测试工具（FOB/稳态性能、带宽、IOPS、延迟、QoS）
sudo apt install -y fio

# 安装块设备信息工具（lsblk、blkdiscard 等，通常系统自带）
sudo apt install -y util-linux

# 安装 IPMI 工具（电源循环测试的远程电源控制依赖，服务器环境）
sudo apt install -y ipmitool

# 安装文件系统校验工具（电源循环测试的数据完整性校验依赖）
sudo apt install -y e2fsprogs parted

# 安装 Python 串口库（SPOR 测试 Timeboard 硬件断电控制依赖）
pip3 install pyserial

# 配置串口权限（Timeboard 通常使用 /dev/ttyUSB0）
sudo usermod -aG dialout $USER
sudo chmod 666 /dev/ttyUSB0 2>/dev/null || true

# 安装 JSON 处理工具（可选，用于手动校验输出）
sudo apt install -y jq
```

验证安装：

```bash
nvme --version          # 应输出 nvme-cli 版本
smartctl --version      # 应输出 smartmontools 版本
fio --version           # 应输出 fio 版本
ipmitool -V             # 应输出 ipmitool 版本（电源循环测试需要）
python3 --version       # 应输出 Python 3.8+
```

### 2.3 Python 依赖

脚本仅使用 Python 标准库（subprocess、json、argparse、logging、datetime、os、sys、re、time、statistics），无需额外 pip 安装。

如需生成 HTML 报告（可选扩展），可安装：

```bash
pip3 install jinja2
```

### 2.4 待测设备确认

```bash
# 列出所有 NVMe 设备
sudo nvme list

# 列出所有块设备（含 SATA）
lsblk -d -o NAME,SIZE,MODEL,TRAN

# 确认设备未挂载（测试前必须卸载所有分区）
sudo umount /dev/nvme0n1p* 2>/dev/null || true
```

> **警告**：性能测试和固件升级会破坏待测 SSD 上的所有数据，请确保设备上无重要数据，或已完成备份。

---

## 三、脚本制作过程

### 3.1 整体架构

脚本采用模块化设计，单一入口 `ssd_test.py`，内部按测试项目拆分为独立类/函数：

```
ssd_test.py
├── 配置与日志模块
│   ├── setup_logging()        # 日志初始化（控制台 + 文件）
│   └── TestConfig             # 测试参数配置（运行时长、队列深度等）
│
├── 工具函数模块
│   ├── run_cmd()              # 统一命令执行（超时、重试、错误捕获）
│   ├── check_root()           # root 权限校验
│   ├── check_dependencies()   # 依赖工具存在性检查
│   ├── get_device_type()      # 识别 NVMe / SATA 设备类型
│   └── bytes_to_human()       # 字节数人性化转换
│
├── 测试项 1：固件升级/降级
│   └── FirmwareTester
│       ├── get_current_fw_version()   # 读取当前固件版本
│       ├── fw_download()               # 下载固件到设备
│       ├── fw_commit()                 # 提交/激活固件（含复位）
│       ├── verify_device_after_flash() # 烧写后设备枚举与功能校验
│       ├── run_upgrade()               # 执行升级流程
│       └── run_downgrade()             # 执行降级流程
│
├── 测试项 2：SMART 健康信息
│   └── SmartTester
│       ├── get_smart_nvme()            # nvme smart-log 获取 SMART
│       ├── get_smart_smartctl()        # smartctl 获取 SMART
│       ├── check_smart_core_items()    # 核心项检查（电源循环/通电时间/温度/可用备件/介质错误）
│       ├── run_basic_rw()              # 基本读写操作
│       └── run_test()                  # SMART 测试主流程（含 R/W 后复检）
│
├── 测试项 3：设备容量
│   └── CapacityTester
│       ├── get_capacity_nvme()         # nvme id-ctrl 读取容量
│       ├── get_capacity_lsblk()        # lsblk 读取容量
│       ├── verify_capacity()            # 多源容量交叉校验
│       └── run_test()                   # 容量测试主流程
│
├── 测试项 4：完整性能特征
│   └── PerformanceTester
│       ├── precondition_steady_state() # 稳态预处理（顺序写满 + 随机写）
│       ├── run_fio()                    # 执行单次 fio 测试
│       ├── test_sequential_read_write() # 顺序读写带宽测试
│       ├── test_random_read_write()     # 随机读写 IOPS/延迟测试
│       ├── test_latency_qos()           # 延迟与 QoS 百分位测试
│       ├── run_fob_test()               # FOB 状态性能测试
│       └── run_steady_state_test()      # 稳态性能测试
│
├── 测试项 5：正常电源循环测试
│   └── PowerCycleTester
│       ├── setup_test_partition()       # 创建测试分区与文件系统
│       ├── write_integrity_data()       # 写入校验数据（带校验和）
│       ├── verify_integrity_data()      # 校验数据完整性
│       ├── start_mixed_rw_load()        # 启动持续混合读写负载（fio 后台）
│       ├── stop_mixed_rw_load()         # 停止混合读写负载
│       ├── ipmi_power_off()             # IPMI 远程断电
│       ├── ipmi_power_on()              # IPMI 远程上电
│       ├── ipmi_power_status()          # IPMI 电源状态查询
│       ├── graceful_shutdown()          # 正常关机
│       ├── check_disk_after_boot()      # 开机后磁盘/分区/文件系统/数据检查
│       ├── load_state() / save_state()  # 状态文件持久化（重启后恢复）
│       ├── run_final_functional_test()  # 最终完整功能测试
│       └── run()                         # 电源循环测试主流程
│
├── 测试项 6：意外电源循环测试 (SPOR)
│   ├── TimeboardController               # Timeboard 硬件断电控制器（Modbus RTU）
│   │   ├── modbus_crc16()               # Modbus CRC16 校验
│   │   ├── build_modbus_rtu_packet()    # 组装 0x10 写寄存器报文
│   │   ├── connect() / disconnect()      # 串口连接/断开（自动探测 ttyUSB/ACM）
│   │   └── trigger_poweroff()            # 触发延时硬件断电
│   └── SPORTester
│       ├── load_state() / save_state()   # 原子状态文件（临时文件+fsync+rename）
│       ├── check_device_present()        # 掉盘检测（上电后设备是否枚举）
│       ├── check_smart()                 # SMART 健康检查（介质错误/可用备件/温度）
│       ├── check_pcie_link()             # PCIe 链路状态检查（按 0108 类代码）
│       ├── write_pattern()               # 写入 pattern 打底（fio do_verify+crc32c）
│       ├── verify_pattern()              # 验证 pattern（fio buffer_pattern 比对）
│       ├── start_spor_write()            # 启动 SPOR 写入（高 iodepth，支持混合读写）
│       ├── get_write_position()          # 从 fio iolog 解析掉电时最后写入 LBA
│       ├── phase1_poweroff()             # 阶段1：掉电前（不 sync，直接硬件断电）
│       ├── phase2_poweron()              # 阶段2：上电后（掉盘+SMART+前段/后段验证）
│       ├── run_final_functional_test()   # 最终完整功能测试（容量+SMART+顺序读写性能）
│       ├── print_final_results()         # 打印最终结果汇总
│       └── run()                         # SPOR 测试主入口（两阶段状态机）
│
├── 报告模块
│   ├── TestResult             # 单条测试结果数据结构
│   ├── TestReport             # 报告汇总（PASS/FAIL 统计）
│   ├── generate_json_report() # 生成 JSON 报告
│   └── generate_text_summary()# 生成文本摘要
│
└── 主入口
    ├── parse_args()           # 命令行参数解析
    └── main()                 # 主流程调度
```

### 3.2 关键实现思路

#### 3.2.1 命令执行封装

所有外部命令通过 `run_cmd()` 统一执行，具备：
- 超时控制（默认 300s，fio 测试可配置）
- 实时输出捕获（stdout/stderr 分离）
- 重试机制（瞬时错误最多重试 2 次）
- 错误码检查与异常抛出
- 命令日志记录（便于问题追溯）

#### 3.2.2 固件升级/降级

NVMe 固件升级标准流程（基于 nvme-cli）：
1. `nvme fw-download` 将固件镜像下载到设备暂存区
2. `nvme fw-commit` 提交并激活固件（action=2 为立即复位激活，action=1 为下次复位激活）
3. 复位后等待设备重新枚举（轮询 `nvme list`，最长 120s）
4. 读取新固件版本并与目标版本比对
5. 执行基本功能校验（SMART 可读、容量正常、基本 R/W 正常）

降级流程与升级一致，仅镜像文件为旧版本。

> 注意：部分厂商 SSD 需要专用工具（如三星 magician、西数 nvmecmd），脚本预留 `vendor_tool` 参数接口。

#### 3.2.3 SMART 健康信息

- NVMe 设备优先使用 `nvme smart-log /dev/nvmeX`（输出 JSON 格式，字段标准化）
- SATA 设备或需要更多字段时使用 `smartctl -a /dev/sdX`（解析文本输出）
- 核心检查项：
  - 电源循环数（Power Cycles）：R/W 前后应增加合理值
  - 通电时间（Power On Hours）：应随测试时间增加
  - 温度（Temperature）：应在正常工作范围（0~70°C）
  - 可用备件（Available Spare / Percentage Used）：不应低于阈值
  - 介质错误（Media and Data Integrity Errors）：R/W 后不应新增
- 基本 R/W：使用 `dd` 或 `fio` 对设备前 1GB 进行读写，然后重新读取 SMART 对比变化

#### 3.2.4 设备容量

- NVMe：`nvme id-ctrl /dev/nvmeX` 读取 `tnvmcap`（总 NVM 容量）和 `unvmcap`（未分配 NVM 容量）
- 通用：`lsblk -b -d -n -o SIZE /dev/nvmeXnY` 读取块设备字节数
- 交叉校验：两种方式读取的容量差值应在合理范围（< 1%，因保留空间/计算方式差异）
- 容量换算：1GB = 10^9 字节（厂商十进制），1GiB = 2^30 字节（操作系统二进制），报告中同时标注

#### 3.2.5 完整性能特征

**FOB 状态（Fresh Out of Box）**：
- 前置条件：执行 `blkdiscard` 或 `nvme format` 清空全盘，恢复出厂空白状态
- 测试项：
  - 顺序读/写 128K QD32：测量带宽（MB/s）
  - 随机读/写 4K QD32：测量 IOPS
  - 随机读 4K QD1：测量平均延迟（μs）
  - 每项运行时长 ≥ 60s（稳态测量建议 ≥ 300s）

**稳态（Steady State）**：
- 预处理流程（SNIA 标准近似）：
  1. 顺序写满全盘 2 次（128K QD32）
  2. 随机写 4K QD32 持续至写入量达到 2 倍全盘容量
  3. 等待 5 分钟让后台 GC/WL 完成
- 稳态测试项与 FOB 相同，用于对比性能衰减

**QoS 测量**：
- fio 输出中读取延迟百分位：clat percentiles (p50/p90/p99/p99.9/p99.99)
- 报告 p99 延迟和最大延迟，评估 QoS 表现

**fio 关键参数**：
- `--direct=1`：绕过页缓存，直写设备
- `--ioengine=libaio`：Linux 异步 IO
- `--runtime=60`：运行时长
- `--time_based`：按时间运行（即使写完也循环）
- `--group_reporting`：汇总多 job 结果
- `--output-format=json`：JSON 输出便于解析

#### 3.2.6 正常电源循环测试

**测试流程**（每个循环）：
1. 在待测 SSD 上创建测试分区与 ext4 文件系统
2. 写入带校验和的测试数据（用于重启后验证数据完整性）
3. 启动持续混合读写负载（fio `randrw`，读占比 70%，后台运行）
4. 正常关机（`shutdown -h now` 或 `systemctl poweroff`）
5. 断电（IPMI `chassis power off`，或手动断电）
6. 等待 30 秒（模拟真实断电间隔）
7. 上电（IPMI `chassis power on`，或手动上电）
8. 等待系统启动并 SSH/本地可登录（轮询，最长 300s）
9. 开机后检查：
   - 磁盘枚举（`lsblk` / `nvme list`）
   - 分区表完整性（`parted` / `fdisk`）
   - 文件系统检查（`fsck`，只读模式）
   - 数据完整性校验（比对校验和）
   - SMART 检查（无新增介质错误、温度正常）
10. 记录本循环结果，进入下一循环
11. 达到设定循环次数后，执行最终完整功能测试（容量 + SMART + 基本性能）

**状态持久化与重启恢复**：
- 使用 JSON 状态文件（默认 `/var/lib/ssd_power_cycle_state.json`）记录：
  - 当前循环次数、目标循环次数
  - 每循环的检查结果
  - 测试数据校验和
  - 测试阶段标识（setup / rw_load / shutdown / boot_check / final_test / done）
- 系统重启后，脚本自动检测状态文件，从断点继续执行
- 支持通过 systemd service 实现开机自启，完成全自动循环

**IPMI 远程电源控制**（服务器环境推荐）：
- 依赖 `ipmitool`，通过 BMC 远程控制服务器电源
- 关键命令：
  - `ipmitool -H <BMC_IP> -U <user> -P <pass> chassis power status`：查询电源状态
  - `ipmitool ... chassis power off`：软关机后断电
  - `ipmitool ... chassis power on`：上电开机
  - `ipmitool ... chassis power cycle`：电源循环（off→on）
- IPMI 参数通过 `--ipmi-host`、`--ipmi-user`、`--ipmi-pass` 传入
- 无 IPMI 环境时使用 `--power-mode manual`，脚本在关机前提示用户手动断电上电

**混合读写负载**：
- fio 参数：`--rw=randrw --rwmixread=70 --bs=4k --iodepth=32 --numjobs=4`
- 后台运行，日志写入文件，关机前停止并统计读写量
- 模拟真实业务负载下的电源循环场景

**数据完整性校验**：
- 写入时生成多个测试文件（每个 100MB），每个文件附带 SHA-256 校验和
- 校验和记录在状态文件中（不存储在待测盘上，避免待测盘故障导致校验和丢失）
- 开机后重新计算校验和并比对，任何不一致即判定本循环 FAIL

#### 3.2.7 意外电源循环测试 (SPOR)

**与正常电源循环的核心区别**：
- 正常电源循环：先正常关机（`shutdown`），再断电——SSD 有机会完成 FTL 刷新
- SPOR 意外断电：在 SSD **活跃读写过程中直接硬件断电**，不执行 `sync`、不发送待机命令——SSD 完全依赖内置 PLP（Power Loss Protection，掉电保护）电容供电，在毫秒级时间内完成 FTL 映射表等关键元数据的备份

**测试流程（每轮两阶段）**：
- **阶段1（掉电前）**：
  1. 写入 pattern11（0x11）打底 20GB，使用 fio `do_verify=1 --verify=crc32c` 确保初始数据正确
  2. 验证 pattern11 完整性
  3. 启动 SPOR 写入（pattern22，0x22），使用高队列深度 `--iodepth=32 --numjobs=4` 保证写入压力；支持 `--spor-mixed-rw` 切换为混合读写（`randrw`，70%读）
  4. 等待指定秒数（`--spor-delay`，默认 5s），此时 fio 仍在活跃写入
  5. **不执行 `os.sync()`**，直接保存状态文件，然后通过 Timeboard 触发硬件断电（`--spor-poweroff-delay-ms`，默认 500ms，越短越"意外"）
- **阶段2（上电后）**：
  1. 等待 15s SSD 初始化，**掉盘检测**（设备节点是否存在）
  2. PCIe 链路状态检查（按 NVMe 类代码 0108 筛选）
  3. **SMART 健康检查**（介质错误数、可用备件、温度），记录每轮 SMART 历史
  4. 从 fio `write_iolog` 解析掉电时最后写入 LBA 位置（读取最后 20 行，LBA 对齐校验，跳过截断条目）
  5. **前段验证**：0 ~ 掉电位置（跳过最后 8 个 LBA 容错，因为掉电瞬间可能未完全写入），验证 pattern22
  6. **后段验证**：掉电位置 ~ 20GB 末尾，验证未被覆盖的 pattern11
  7. 记录本轮结果（pattern22 + pattern11 + SMART 全部通过才 PASS）
  8. 全部循环完成后执行**最终完整功能测试**（容量 + SMART + 顺序读/写性能 30s）

**PLP 有效性验证原理**：
- 如果 SSD 的 PLP 正常工作，掉电瞬间电容供电会将 FTL 映射表、缓存中的脏数据写入 NAND，上电后已写入区域的数据应完整可读（pattern22 验证通过），未写入区域应保持原始 pattern11
- 如果 PLP 失效，FTL 映射表可能损坏，导致已写入 LBA 映射错乱、数据读回错误（pattern22 验证失败），甚至 SSD 掉盘（设备不枚举）
- 跳过掉电边界最后 N 个 LBA 是因为这几个 LBA 可能恰好在掉电瞬间处于"已发送写命令但数据未完全落盘"的中间状态，属于正常现象，不代表 PLP 失效

**Timeboard 硬件控制**：
- 通过 USB 转串口（`/dev/ttyUSB0`，自动探测 ttyUSB0-2/ttyACM0-1）连接 Timeboard 继电器电源板
- 使用 **Modbus RTU 协议**（功能码 0x10 写多个寄存器），从站地址 0x01，起始地址 0x0020，写入 3 个寄存器（6 字节数据：CMD_TYPE + CMD_LENGTH + 24bit 延时 + 补零）
- 延时单位为 100ms，硬件收到命令后延时指定时间切断 SSD 供电
- 自带 Modbus CRC16 校验（多项式 0xA001），确保命令可靠传输

**状态文件原子写入**：
- 状态文件路径 `/var/lib/ssd_spor_state.json`，记录当前轮数、阶段、每轮结果、SMART 历史
- 采用**原子写入**：写入临时文件 → `fsync` 文件内容 → `os.replace` 原子重命名 → `fsync` 目录项
- 确保状态文件本身不会因意外断电而损坏（半写状态），上电后可可靠恢复

**关键设计决策（相比原始代码的改进）**：
1. **删除断电前 `os.sync()`**：原始代码在断电前执行 sync，会促使 SSD 刷新缓存，削弱"意外"性；移植版完全不 sync，在 fio 活跃写入时直接断电
2. **提高写入压力**：原始代码 SPOR 写入 `iodepth` 默认为 1（未设置），5 秒仅写 1.5GB；移植版设置 `--iodepth=32 --numjobs=4`，写入压力大幅提升，更能暴露 PLP 在高负载下的缺陷
3. **增加掉盘检测**：上电后先检查设备节点是否存在，明确区分"SSD 掉盘"和"数据错误"
4. **增加 SMART 检查**：每轮上电后检查介质错误数、可用备件、温度，记录历史变化，PLP 失效可能导致 NAND 写入损坏（介质错误增加）
5. **增加最终完整功能测试**：所有循环完成后执行容量 + SMART + 顺序读写性能测试，确认多轮意外断电后 SSD 功能正常、性能无衰减
6. **支持混合读写**：新增 `--spor-mixed-rw` 选项，可切换为 `randrw` 混合负载，更接近真实业务场景
7. **修复 PCIe 链路检查**：原始代码 `grep -i nvme` 可能匹配不到（lspci 输出为"Non-Volatile memory controller"）；移植版按 PCIe 类代码 `0108` 筛选
8. **硬件断电延时可配置**：`--spor-poweroff-delay-ms` 默认 500ms（原始代码固定 2000ms），延时越短越接近"真正意外"

### 3.3 测试结果数据结构

每条测试结果包含：

```python
{
    "test_item": "firmware_upgrade",       # 测试项标识
    "test_name": "现场固件升级",             # 测试项名称
    "device": "/dev/nvme0n1",              # 待测设备
    "start_time": "2026-09-07 10:00:00",  # 开始时间
    "end_time": "2026-09-07 10:05:00",    # 结束时间
    "duration_sec": 300,                    # 耗时
    "status": "PASS",                       # PASS / FAIL / ERROR / SKIP
    "details": { ... },                     # 详细测试数据
    "error_message": null                   # 失败时的错误信息
}
```

### 3.4 日志与报告

- 日志：`logs/ssd_test_YYYYMMDD_HHMMSS.log`，记录所有执行命令、输出摘要、测试进度
- JSON 报告：`reports/ssd_test_report_YYYYMMDD_HHMMSS.json`，完整结构化结果
- 文本摘要：控制台实时输出 + 报告文件末尾的 PASS/FAIL 汇总

---

## 四、脚本使用步骤

### 4.1 脚本获取与放置

将 `ssd_test.py` 放置到任意工作目录，例如：

```bash
mkdir -p ~/ssd_test
cp ssd_test.py ~/ssd_test/
cd ~/ssd_test
chmod +x ssd_test.py
```

### 4.2 命令行参数说明

```bash
sudo python3 ssd_test.py [选项]
```

| 参数 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `--device` | `-d` | 待测设备路径（必填），如 /dev/nvme0n1 | 无 |
| `--test` | `-t` | 指定测试项，可多选：fw/smart/capacity/perf/all | all |
| `--fw-image` | | 固件镜像文件路径（fw 测试必填） | 无 |
| `--fw-action` | | 固件操作类型：upgrade/downgrade | upgrade |
| `--fw-slot` | | 固件槽位（1-7），默认自动选择 | 自动 |
| `--perf-state` | | 性能测试状态：fob/steady/both | both |
| `--perf-runtime` | | 单项 fio 运行时长（秒） | 60 |
| `--perf-qd` | | 性能测试队列深度 | 32 |
| `--perf-bs` | | 性能测试块大小（如 4k, 128k） | 自动 |
| `--precondition` | | 稳态预处理开关：on/off | on |
| `--pc-cycles` | | 电源循环测试循环次数 | 10 |
| `--pc-power-mode` | | 电源控制模式：ipmi/manual | ipmi |
| `--ipmi-host` | | IPMI BMC 地址（ipmi 模式必填） | 无 |
| `--ipmi-user` | | IPMI 用户名 | ADMIN |
| `--ipmi-pass` | | IPMI 密码 | ADMIN |
| `--pc-mount-point` | | 电源循环测试挂载点 | /mnt/ssd_test |
| `--pc-state-file` | | 电源循环状态文件路径 | /var/lib/ssd_power_cycle_state.json |
| `--pc-boot-timeout` | | 开机等待超时（秒） | 300 |
| `--pc-off-interval` | | 断电后等待上电间隔（秒） | 30 |
| `--pc-rw-duration` | | 每循环混合读写运行时长（秒） | 120 |
| `--spor-cycles` | | SPOR 意外断电循环次数 | 10 |
| `--spor-delay` | | SPOR 写入后触发硬件断电的等待秒数 | 5 |
| `--spor-test-size` | | SPOR 测试数据大小（GB） | 20 |
| `--spor-lba-size` | | SPOR LBA 大小（字节） | 4096 |
| `--spor-skip-lba` | | 掉电边界跳过验证的 LBA 数 | 8 |
| `--spor-poweroff-delay-ms` | | Timeboard 硬件断电延时（毫秒，越短越意外） | 500 |
| `--spor-mixed-rw` | | SPOR 使用混合读写负载（默认纯顺序写） | 关 |
| `--spor-mixed-read-ratio` | | 混合读写模式下读占比（%） | 70 |
| `--spor-no-final-test` | | 跳过 SPOR 最终完整功能测试 | 关 |
| `--spor-timeboard-port` | | Timeboard 串口设备路径 | /dev/ttyUSB0 |
| `--spor-state-file` | | SPOR 状态文件路径 | /var/lib/ssd_spor_state.json |
| `--output-dir` | `-o` | 报告输出目录 | ./reports |
| `--log-dir` | | 日志输出目录 | ./logs |
| `--dry-run` | | 试运行模式，只打印命令不执行 | off |
| `--yes` | `-y` | 跳过数据销毁确认提示 | off |
| `--verbose` | `-v` | 详细日志输出 | off |

### 4.3 使用示例

#### 示例 1：运行全部测试项（最常用）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t all \
    --fw-image /path/to/firmware.bin \
    --perf-state both \
    --perf-runtime 120 \
    -y
```

#### 示例 2：仅运行固件升级测试

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t fw \
    --fw-image /path/to/new_firmware.bin \
    --fw-action upgrade
```

#### 示例 3：仅运行固件降级测试

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t fw \
    --fw-image /path/to/old_firmware.bin \
    --fw-action downgrade
```

#### 示例 4：仅运行 SMART 健康信息测试

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
```

#### 示例 5：仅运行设备容量测试

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t capacity
```

#### 示例 6：仅运行 FOB 状态性能测试（跳过稳态预处理）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf \
    --perf-state fob \
    --perf-runtime 60 \
    -y
```

#### 示例 7：仅运行稳态性能测试（含预处理）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf \
    --perf-state steady \
    --precondition on \
    --perf-runtime 300 \
    -y
```

#### 示例 8：电源循环测试（IPMI 远程电源控制，全自动）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle \
    --pc-cycles 20 \
    --pc-power-mode ipmi \
    --ipmi-host 192.168.1.100 \
    --ipmi-user ADMIN \
    --ipmi-pass mypassword \
    --pc-rw-duration 120 \
    -y
```

#### 示例 9：电源循环测试（手动断电上电模式）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle \
    --pc-cycles 10 \
    --pc-power-mode manual \
    -y
# 脚本会在每次关机前提示用户手动断电，30秒后手动上电
```

#### 示例 10：SPOR 意外电源循环测试（Timeboard 硬件断电，纯写模式）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-cycles 20 \
    --spor-delay 5 \
    --spor-poweroff-delay-ms 500 \
    --spor-timeboard-port /dev/ttyUSB0 \
    -y
# 在 fio 活跃写入 5 秒后，Timeboard 硬件在 500ms 后直接切断 SSD 电源（不 sync、不待机）
# 上电后自动验证前段 pattern22 + 后段 pattern11 + SMART，循环 20 次
```

#### 示例 11：SPOR 测试（混合读写模式，高压力场景）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-cycles 10 \
    --spor-mixed-rw \
    --spor-mixed-read-ratio 70 \
    --spor-test-size 20 \
    -y
# 使用 randrw 混合读写（70%读+30%写，4K随机），更接近真实业务负载
# 混合读写对 FTL 映射表更新压力更大，更能暴露 PLP 缺陷
```

#### 示例 12：试运行模式（不实际执行，仅验证命令和参数）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --dry-run
```

#### 示例 13：SATA SSD 测试

```bash
sudo python3 ssd_test.py -d /dev/sda -t smart,capacity -y
```

### 4.4 输出说明

#### 控制台输出

脚本运行时实时输出测试进度：

```
[2026-09-07 10:00:00] [INFO] ====================================
[2026-09-07 10:00:00] [INFO] SSD 自动化测试脚本启动
[2026-09-07 10:00:00] [INFO] 设备: /dev/nvme0n1
[2026-09-07 10:00:00] [INFO] 测试项: all
[2026-09-07 10:00:00] [INFO] ====================================
[2026-09-07 10:00:01] [INFO] [1/5] 设备容量测试 ...
[2026-09-07 10:00:02] [INFO]   NVMe 总容量: 1024.2 GB (953.9 GiB)
[2026-09-07 10:00:02] [INFO]   lsblk 容量: 1024.2 GB (953.9 GiB)
[2026-09-07 10:00:02] [PASS] 设备容量测试 - PASS (1.2s)
...
[2026-09-07 10:30:00] [INFO] ====================================
[2026-09-07 10:30:00] [INFO] 测试汇总
[2026-09-07 10:30:00] [INFO]   设备容量:       PASS
[2026-09-07 10:30:00] [INFO]   SMART健康信息:  PASS
[2026-09-07 10:30:00] [INFO]   固件升级:       PASS
[2026-09-07 10:30:00] [INFO]   完整性能特征:   PASS
[2026-09-07 10:30:00] [INFO]   正常电源循环:   PASS
[2026-09-07 10:30:00] [INFO]   总计: 5 PASS / 0 FAIL / 0 ERROR
[2026-09-07 10:30:00] [INFO] 报告: reports/ssd_test_report_20260907_100000.json
[2026-09-07 10:30:00] [INFO] 日志: logs/ssd_test_20260907_100000.log
[2026-09-07 10:30:00] [INFO] ====================================
```

#### 报告文件

- JSON 报告：`reports/ssd_test_report_YYYYMMDD_HHMMSS.json`，包含所有测试项的完整结构化数据
- 日志文件：`logs/ssd_test_YYYYMMDD_HHMMSS.log`，完整命令执行记录

### 4.5 典型测试流程

1. **准备阶段**：确认待测设备、备份数据、卸载分区、准备固件镜像、配置 IPMI（电源循环测试需要）
2. **容量测试**：读取并校验设备容量（最快，先确认设备基本识别）
3. **SMART 测试**：读取初始 SMART → 基本 R/W → 复检 SMART
4. **固件测试**：读取当前版本 → 下载/提交固件 → 复位等待 → 版本校验 → 功能校验
5. **性能测试**：FOB 状态测试 → 稳态预处理 → 稳态测试 → 性能对比
6. **电源循环测试**：创建分区/文件系统 → 写入校验数据 → 混合读写负载 → 正常关机断电 → 上电开机 → 磁盘/分区/文件系统/数据检查 → 循环 N 次 → 最终完整功能测试
7. **报告生成**：汇总所有结果，生成 JSON 报告和文本摘要

---

## 五、注意事项与常见问题

### 5.1 安全注意

1. **数据销毁**：性能测试（尤其稳态预处理）、固件升级和电源循环测试会破坏全盘数据，务必确认设备无重要数据
2. **设备选择**：`--device` 参数务必确认是待测 SSD，误填系统盘（如 /dev/sda）会导致系统损坏
3. **固件兼容性**：固件镜像必须与设备型号匹配，错误的固件可能导致设备变砖
4. **断电风险**：固件烧写过程中严禁断电，建议使用 UPS
5. **电源循环测试系统盘风险**：电源循环测试会反复关机断电，**测试脚本和系统必须运行在非待测盘上**（如系统盘 /dev/sda，待测盘 /dev/nvme0n1），否则脚本会随系统关机中断
6. **IPMI 密码安全**：`--ipmi-pass` 参数会出现在进程列表和日志中，生产环境建议使用 IPMI 配置文件或环境变量
7. **手动断电模式**：`--pc-power-mode manual` 时，脚本关机后需用户在 30 秒后手动上电，否则脚本无法恢复执行
8. **SPOR 硬件断电风险**：SPOR 测试通过 Timeboard 继电器直接切断 SSD 供电，**不经过系统关机**，运行 SPOR 测试时系统盘必须与待测 SSD 独立（系统盘走主板供电，待测 SSD 走 Timeboard 控制的独立电源），否则会导致系统异常断电损坏系统盘数据
9. **SPOR 测试需配置开机自启**：SPOR 阶段1触发硬件断电后脚本进程随系统终止，上电后需通过 systemd service 或桌面自启（.desktop）自动运行脚本，脚本检测到状态文件 phase=2 后自动恢复执行阶段2
10. **Timeboard 串口权限**：Timeboard 通常使用 `/dev/ttyUSB0`，需确保当前用户有 dialout 组权限或执行 `sudo chmod 666 /dev/ttyUSB0`

### 5.2 常见问题

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| `nvme: command not found` | 未安装 nvme-cli | `sudo apt install nvme-cli` |
| `Permission denied` | 未使用 root 权限 | 命令前加 `sudo` |
| 固件 commit 后设备消失 | 设备复位中，正常现象 | 脚本会自动轮询等待，最长 120s |
| fio 报 `device is busy` | 设备有分区已挂载 | `sudo umount /dev/nvme0n1p*` |
| SMART 温度显示异常 | 部分厂商传感器读取方式不同 | 脚本同时尝试 nvme smart-log 和 smartctl |
| 容量显示与标称不符 | 厂商十进制 vs OS 二进制，且有保留空间 | 正常现象，报告中同时标注 GB 和 GiB |
| 稳态预处理时间过长 | 大容量盘预处理耗时较长 | 可通过 `--precondition off` 跳过，或减小盘容量测试 |
| 电源循环测试关机后脚本不恢复 | 脚本运行在待测盘上，关机后脚本丢失 | 确保脚本和系统运行在非待测盘上；配置 systemd 开机自启 |
| IPMI 连接失败 | BMC 地址/账号错误、网络不通 | `ping <BMC_IP>` 测试连通性；确认 IPMI 账号权限；检查防火墙 |
| 开机后设备未枚举 | SSD 掉盘或上电初始化慢 | 脚本会轮询等待最长 300s；检查 SSD 供电和数据线；查看 dmesg |
| 数据完整性校验失败 | 电源循环导致数据丢失或文件系统损坏 | 本循环标记 FAIL；检查文件系统日志（dmesg/journalctl）；确认 SSD 是否有掉电保护 |
| 手动模式下不知道何时上电 | 脚本关机后会打印提示 | 看到 `请在 30 秒后手动上电` 提示后，等待 30 秒再按电源键 |
| SPOR 测试后系统也断电了 | 系统盘和待测 SSD 接在同一电源上，Timeboard 断电时系统也掉电 | 确保系统盘走主板供电，待测 SSD 走 Timeboard 控制的独立电源通道 |
| Timeboard 连接失败 | 串口权限不足或串口设备名不对 | `sudo chmod 666 /dev/ttyUSB0`；检查 `ls /dev/ttyUSB* /dev/ttyACM*` 确认设备名；用 `--spor-timeboard-port` 指定正确端口 |
| SPOR 上电后脚本不自动恢复 | 未配置开机自启，或状态文件损坏 | 配置 systemd service 或桌面 .desktop 自启；检查 `/var/lib/ssd_spor_state.json` 是否存在且 phase=2 |
| SPOR pattern22 验证总是失败 | PLP 失效，或掉电边界跳过 LBA 数不够 | 增加 `--spor-skip-lba` 到 16 或 32；如果仍失败，说明 SSD PLP 可能无法在该负载下保护 FTL 元数据 |
| SPOR 写入量偏少 | iodepth 或 numjobs 太低，或 delay 太短 | 默认已使用 iodepth=32 numjobs=4；可增加 `--spor-delay` 到 10-30 秒增加写入量 |
| fio iolog 最后一行截断 | 掉电时 iolog 正在写入，最后一行不完整 | 正常现象，脚本通过 LBA 对齐校验自动跳过非对齐条目 |

### 5.3 扩展建议

- 如需支持厂商专用工具，可在 `FirmwareTester` 中添加 `vendor_tool` 分支
- 如需 HTML 报告，可基于 JSON 报告使用 jinja2 模板生成
- 如需多盘并行测试，可使用多进程包装，每个设备一个测试实例
- 如需接入 CI/CD，可将 JSON 报告转换为 JUnit XML 格式

---

## 六、版本信息

- 脚本版本：v1.2
- 适用平台：Ubuntu 20.04+
- Python 版本：3.8+
- 依赖工具：nvme-cli ≥ 1.9，smartmontools ≥ 7.0，fio ≥ 3.16，ipmitool ≥ 1.8（电源循环测试），e2fsprogs/parted（电源循环测试），pyserial ≥ 3.0（SPOR Timeboard 控制）
- 更新记录：
  - v1.0：初始版本，支持固件升降级、SMART、容量、性能测试
  - v1.1：新增正常电源循环测试（IPMI/手动双模式、状态文件持久化、数据完整性校验、最终完整功能测试）
  - v1.2：新增意外电源循环测试 SPOR（Timeboard Modbus RTU 硬件断电、两阶段状态机、fio iolog 掉电位置追踪、前段/后段 pattern 验证、掉盘检测、SMART 检查、最终完整功能测试、混合读写支持）；修复原始 SPOR 代码缺陷（删除断电前 sync、提高 iodepth 写入压力、修复 PCIe 类代码筛选）
