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
| 4 | 完整性能特征 | FOB（出厂空白）与稳态下测量带宽、IOPS、延迟、QoS |
| 5 | 读/写测试 | 全磁盘写入+读取验证、多文件大小重复读写周期、24小时长期验证 |
| 6 | 正常电源循环测试 | 持续混合读写下正常关机→断电→开机，循环检查数据完整性 |
| 7 | 意外电源循环测试 (SPOR) | 读写过程中直接硬件断电，验证 PLP 掉电保护与 FTL 元数据备份能力 |
| 8 | 操作系统中断测试 | 持续 I/O 期间 S3/S4 休眠/唤醒的稳定性与数据完整性验证 |

**双模式运行**：
- **命令行模式**：适合自动化/CI 集成，支持参数化配置
- **可视化上位机模式 (GUI)**：基于 tkinter，图形化选择设备/测试项/参数，实时日志和进度显示

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

### 命令行模式

```bash
# 运行全部测试项
sudo python3 ssd_test.py -d /dev/nvme0n1 -t all -y

# 仅运行 SMART 健康检查
sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart

# 性能测试（FOB 状态）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y

# 电源循环测试（10次循环，IPMI 远程控电）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-cycles 10 --ipmi-host 192.168.1.100 -y
```

### GUI 模式

```bash
sudo python3 ssd_test.py --gui
```

### 主要参数

| 参数 | 说明 |
|------|------|
| `-d, --device` | 待测设备路径，如 `/dev/nvme0n1` |
| `-t, --test` | 测试项：`fw`/`smart`/`capacity`/`perf`/`rw`/`powercycle`/`spor`/`osint`/`all` |
| `-y, --yes` | 跳过交互确认，自动执行 |
| `--gui` | 启动可视化上位机模式 |
| `--fw-image` | 固件升级镜像路径 |
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

测试报告自动保存为 `ssd_test_report_YYYYMMDD_HHMMSS.json`，日志保存为 `ssd_test_YYYYMMDD_HHMMSS.log`。

---

## 版本历史

- **V1.5.3**：最新稳定版，完善 GUI、错误处理与数据校验
- **V1.0 ~ V1.4**：逐步迭代各测试项功能
- **意外电源测试**：独立 SPOR 测试脚本与 shell 封装
- **错误日志以及修正**：包含 SMART 介质错误误判修复记录 (v1.5.3)
