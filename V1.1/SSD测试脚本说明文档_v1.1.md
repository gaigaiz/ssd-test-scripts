# SSD 自动化测试脚本说明文档

## 一、项目概述

本脚本面向 Linux (Ubuntu) 平台下的 SSD 测试工程师，覆盖以下五大测试项目：

| 序号 | 测试项目 | 英文名称 | 核心目标 |
|------|----------|----------|----------|
| 1 | 现场固件升级/降级 | Field Firmware Upgrade/Downgrade | 验证固件正常烧写、升降级兼容性、烧写后无掉盘/崩溃/功能异常，版本变更后功能正常 |
| 2 | 设备智能健康信息 | Device SMART Health Information | 确认 OS 枚举 SSD，获取完整 SMART，检查电源循环/通电时间/温度/可用备件/介质错误，R/W 后复检 SMART |
| 3 | 设备容量 | Devices capacity | 通过 nvme-cli / 磁盘工具读取并校验容量 |
| 4 | 完整性能特征 | Full Performance Characterization | FOB（出厂空白）与稳态两种状态下测量带宽、IOPS、延迟、QoS |
| 5 | 正常电源循环测试 | Normal Power Cycle Test | 持续混合读写下正常关机→断电→开机，循环检查磁盘/分区/文件系统/数据完整性，最终完整功能测试 |

脚本语言：Python 3，依赖标准 Linux 工具链（nvme-cli、smartmontools、fio、ipmitool）。

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

#### 示例 10：试运行模式（不实际执行，仅验证命令和参数）

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --dry-run
```

#### 示例 11：SATA SSD 测试

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

### 5.3 扩展建议

- 如需支持厂商专用工具，可在 `FirmwareTester` 中添加 `vendor_tool` 分支
- 如需 HTML 报告，可基于 JSON 报告使用 jinja2 模板生成
- 如需多盘并行测试，可使用多进程包装，每个设备一个测试实例
- 如需接入 CI/CD，可将 JSON 报告转换为 JUnit XML 格式

---

## 六、版本信息

- 脚本版本：v1.1
- 适用平台：Ubuntu 20.04+
- Python 版本：3.8+
- 依赖工具：nvme-cli ≥ 1.9，smartmontools ≥ 7.0，fio ≥ 3.16，ipmitool ≥ 1.8（电源循环测试），e2fsprogs/parted（电源循环测试）
- 更新记录：
  - v1.0：初始版本，支持固件升降级、SMART、容量、性能测试
  - v1.1：新增正常电源循环测试（IPMI/手动双模式、状态文件持久化、数据完整性校验、最终完整功能测试）
