# SSD 自动化测试脚本工程

## 项目解决什么问题

面向 SSD 测试工程师，在 Linux (Ubuntu) 平台下提供一套覆盖 SSD 全生命周期验证的自动化测试工具。解决手动执行固件升级、SMART 检查、性能测试、电源循环、意外断电等测试项时操作繁琐、数据记录不统一、难以复现的问题，支持命令行自动化集成与可视化上位机操作两种模式。

## 主要功能

覆盖以下八大测试项目：

| 序号 | 测试项目 | 核心目标 |
|------|----------|----------|
| 1 | 现场固件升级/降级 | 验证固件烧写、升降级兼容性，烧写后无掉盘/崩溃/功能异常 |
| 2 | 设备智能健康信息 (SMART) | 获取完整 SMART，检查电源循环/通电时间/温度/可用备件/介质错误 |
| 3 | 设备容量 | 通过 nvme-cli / 磁盘工具读取并校验容量 |
| 4 | 完整性能特征 | FOB（出厂空白）与稳态下测量带宽、IOPS、延迟、QoS，支持多任务批量执行 |
| 5 | 读/写测试 | 全磁盘写入+读取验证、多文件大小重复读写周期、24小时长期验证 |
| 6 | 正常电源循环测试 | 持续混合读写下正常关机→断电→开机，循环检查数据完整性 |
| 7 | 意外电源循环测试 (SPOR) | 读写过程中直接硬件断电，验证 PLP 掉电保护与 FTL 元数据备份能力 |
| 8 | 操作系统中断测试 (OSINT) | 持续 I/O 期间 S3/S4 休眠/唤醒的稳定性与数据完整性验证 |

**双模式运行**：
- **命令行模式**：适合自动化/CI 集成，支持参数化配置、批量任务 JSON 加载
- **可视化上位机模式 (GUI)**：基于 tkinter，图形化选择设备/测试项/参数，实时日志和进度显示，支持批量任务管理与排序

**v1.8 新增特性**：
- 性能测试多任务批量管理与顺序执行（稳态预处理只做一次，后续任务复用）
- GUI 批量任务面板：新增/删除/排序/导出 JSON
- 开始按钮上移、底部按钮可见性修复
- OSINT 操作系统中断测试 9 项 Bug 修复（fio 参数、文件系统保护、SMART 误报、状态残留等）

**v1.8.5 新增特性（读写测试修复）**：
- fio JSON 解析真正根因修复：自动提取 fio 输出中的纯 JSON 部分，过滤警告行
- verify 模式多 job 并发支持：使用 `offset_increment` 隔离各 job 写入区域，避免互相覆盖
- SMART 介质错误改为警告模式：历史介质错误不阻断测试，可用备件和温度为硬性指标
- GUI 布局彻底修复：从下往上 pack 策略，底部进度条/按钮/日志摘要全部可见
- 新增 `_extract_fio_json()` 静态方法，兼容 fio 多 job 警告输出

## 安装方法

### 系统要求

- 操作系统：Ubuntu 20.04 / 22.04 / 24.04 LTS
- Python：≥ 3.8（仅标准库，GUI 使用 tkinter）
- 权限：root 或 sudo 权限

### 安装系统依赖

```bash
sudo apt update
sudo apt install -y nvme-cli smartmontools fio util-linux ipmitool e2fsprogs parted jq
pip3 install pyserial
```

### 验证安装

```bash
nvme --version
smartctl --version
fio --version
python3 --version
```

## 使用方法

### 选择稳定版本

项目提供按测试项拆分的稳定版本，位于 `Stable version/` 目录：

| 稳定版本 | 覆盖测试项 | 适用场景 |
|----------|-----------|----------|
| `ssd_test_v1.5.3(SMART健康+设备容量).py` | SMART + 容量 | 基础健康检查 |
| `ssd_test_v1.7.5(FOB_稳态性能测试）.py` | FOB + 稳态性能 | 性能特征测试 |
| `ssd_test_v1.8.3(SMART+容量+操作系统中断).py` | SMART + 容量 + OSINT | 休眠唤醒稳定性验证 |
| `ssd_test_v1.8.5(V1.8.3+读写).py` | SMART + 容量 + OSINT + 读写 | 全功能测试（推荐） |

完整全功能版本位于 `V1.8/ssd_test_v1.8.5(V1.8.3+读写).py`。

### 命令行模式

```bash
# 运行全部测试项
sudo python3 ssd_test.py -d /dev/nvme0n1 -t all -y

# 仅运行 SMART 健康检查
sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart

# 性能测试（FOB 状态）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y

# 性能测试（批量任务，从 JSON 文件加载）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-task-file tasks.json -y

# 操作系统中断测试（10轮 S3 休眠）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t osint --osint-cycles 10 --osint-sleep-type s3 -y

# 电源循环测试（10次循环，IPMI 远程控电）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-cycles 10 --ipmi-host 192.168.1.100 -y
```

### GUI 模式

```bash
sudo python3 ssd_test.py --gui
```

GUI 支持：设备选择、测试项勾选、参数配置、批量性能任务管理（新增/删除/上下排序/导出 JSON）、实时日志、配置保存/加载。

### 主要参数

| 参数 | 说明 |
|------|------|
| `-d, --device` | 待测设备路径，如 `/dev/nvme0n1` |
| `-t, --test` | 测试项：`fw`/`smart`/`capacity`/`perf`/`rw`/`powercycle`/`spor`/`osint`/`all` |
| `-y, --yes` | 跳过交互确认，自动执行 |
| `--gui` | 启动可视化上位机模式 |
| `--fw-image` | 固件升级镜像路径 |
| `--perf-state` | 性能测试状态：`fob`/`steady`/`both` |
| `--perf-task-file` | 批量性能任务 JSON 文件路径 |
| `--osint-cycles` | OSINT 休眠唤醒循环次数 |
| `--osint-sleep-type` | OSINT 休眠类型：`s3`/`s4` |
| `--pc-cycles` | 电源循环次数 |
| `--ipmi-host` | IPMI 远程管理地址 |

## 输入输出示例

### 示例：SMART 健康检查

**输入命令：**

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
```

**预期输出：**

```
[INFO] 开始测试: Device SMART Health Information
[INFO] 设备: /dev/nvme0n1
[INFO] 模型: XXXXXXXX
[INFO] 序列号: XXXXXXXX
[INFO] 固件版本: XXXXXXXX
[INFO] 容量: 1024.2 GB
[INFO] 电源循环次数: 128
[INFO] 通电时间: 1024 小时
[INFO] 温度: 42°C
[INFO] 可用备件: 100%
[INFO] 介质错误: 0
[PASS] SMART 健康检查通过
```

### 示例：OSINT 操作系统中断测试

**输入命令：**

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t osint --osint-cycles 2 --osint-sleep-type s3 -y
```

**预期输出：**

```
[INFO] 开始测试: OS Interruption Test
[INFO] 设备: /dev/nvme0n1, 循环次数: 2, 休眠类型: s3
[INFO] 第 1/2 轮 - 启动混合读写 IO 负载...
[INFO] fio 负载运行中 (读 ~317MB/s, 写 ~137MB/s)
[INFO] 休眠前清理: 停止 IO → sync → umount → nvme flush
[INFO] 触发 S3 休眠 (15秒后唤醒)...
[INFO] 唤醒成功, 重新挂载文件系统
[INFO] 数据完整性校验: SHA-256 匹配
[INFO] 第 1/2 轮: PASS
[INFO] 第 2/2 轮: PASS
[INFO] 最终完整功能测试: PASS
[PASS] OSINT 测试通过 (2/2 轮)
```

测试报告自动保存为 `ssd_test_report_YYYYMMDD_HHMMSS.json`，日志保存为 `ssd_test_YYYYMMDD_HHMMSS.log`。

---

## 目录结构

```
.
├── Stable version/          # 按测试项拆分的稳定版本
│   ├── ssd_test_v1.5.3(SMART健康+设备容量).py
│   ├── ssd_test_v1.7.5(FOB_稳态性能测试）.py
│   ├── ssd_test_v1.8.3(SMART+容量+操作系统中断).py
│   └── ssd_test_v1.8.5(V1.8.3+读写).py
├── V1.0 ~ V1.5/             # 历史版本迭代
├── V1.6/                     # CrystalDiskMark 风格版本（已舍弃，见目录内说明）
├── V1.7/                     # 稳态性能测试版本
├── V1.8/                     # 最新全功能版本（v1.8.5，含读写测试）
│   ├── ssd_test_v1.8.3(SMART+容量+操作系统中断).py
│   ├── ssd_test_v1.8.5(V1.8.3+读写).py
│   ├── 脚本使用步骤_OSINT_操作系统中断测试_v1.8.3.md
│   └── 脚本使用步骤_读写测试_v1.8.5.md
├── 迭代日志/                  # 版本迭代详细记录
│   ├── v1.6.0_to_v1.7.5/
│   ├── v1.8.0_to_v1.8.3/
│   └── v1.8.3_to_v1.8.5/
├── 错误日志以及修正/           # Bug 修复记录与对应错误日志
│   ├── 修复SMART介质错误误判/
│   ├── 修复超时(失败)与操作系统中断/
│   └── fio JSON 解析失败等问题/
├── 测试项目.md                # 测试项目定义与目标说明
└── 目前未解决问题.md           # 已知待解决问题清单
```

## 已知问题

- **稳态预处理超时**：执行稳态预处理时可能出现超时，目前尚未完全解决
- **固件下载功能未测试**：目前未对固件下载功能进行测试
- **电源循环测试受硬件限制**：由于硬件方面问题，无法测试正常电源循环和意外掉电循环测试

（详见 `目前未解决问题.md`）

## 版本历史

| 版本 | 核心变更 |
|------|----------|
| **V1.8.5** | fio JSON 解析真正根因修复（自动提取纯 JSON）、verify 模式多 job 并发支持（offset_increment 区域隔离）、SMART 介质错误警告模式、GUI 布局彻底修复 |
| V1.8.4 | RW 读/写测试四项修复（fio check=False、SMART 警告、add_detail 修复、底部布局初步修复） |
| **V1.8.3** | OSINT 9 项 Bug 修复（fio 参数、文件系统保护、SMART 警告模式、状态自动重置、自动 fsck 修复、nvme flush 等） |
| V1.8.0~V1.8.2 | 多任务批量性能测试、GUI 任务管理面板、UI 交互优化、底部按钮布局修复 |
| **V1.7.5** | 修复稳态预处理随机写超时不足的问题，FOB/稳态性能测试稳定版 |
| V1.6.0 | 新增 CrystalDiskMark 风格性能测试配置+GUI 控制（因不满足读写比例控制需求已舍弃） |
| **V1.5.3** | SMART 介质错误误判修复，SMART+容量稳定版 |
| V1.0 ~ V1.4 | 逐步迭代各测试项基础功能 |
