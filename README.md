# SSD 自动化测试平台

> 面向 SSD 测试工程师的全生命周期自动化验证工具，覆盖固件、SMART、容量、性能、读写、电源循环、意外断电、操作系统中断、功耗测量九大测试项目，支持命令行与可视化上位机双模式运行。

## 项目解决什么问题

在 SSD 研发与验证流程中，测试工程师需要手动执行固件烧写、SMART 检查、性能基准、电源循环、意外断电、休眠唤醒、功耗测量等多项测试，面临操作繁琐、数据记录不统一、测试条件难以复现、多工具切换效率低等问题。

本项目提供一套**模块化、可扩展**的 SSD 自动化测试平台，将所有测试项统一到同一框架下，支持参数化配置、批量任务调度、JSON 报告输出、GUI 可视化操作，并集成示波器功耗采集能力，实现从电特性到协议层的全方位验证。

## 主要功能

### 九大测试项目

| 序号 | 测试项目 | 模块文件 | 核心目标 |
|------|----------|----------|----------|
| 1 | 现场固件升级/降级 | `testers/firmware.py` | 验证固件烧写、升降级兼容性，烧写后无掉盘/崩溃/功能异常 |
| 2 | 设备智能健康信息 (SMART) | `testers/smart.py` | 获取完整 SMART，检查电源循环/通电时间/温度/可用备件/介质错误，支持可配置忽视指定错误号 |
| 3 | 设备容量 | `testers/capacity.py` | 通过 nvme-cli / 磁盘工具读取并校验容量 |
| 4 | 完整性能特征 | `testers/performance.py` | 真 FOB（nvme format）与 SNIA 合规稳态下测量带宽、IOPS、延迟、QoS，支持多任务批量执行 |
| 5 | 读/写测试 | `testers/read_write.py` | 全磁盘写入+读取验证、多文件大小重复读写周期、24小时长期验证，fio JSON 解析修复 |
| 6 | 正常电源循环测试 | `testers/power_cycle.py` | 持续混合读写下正常关机→断电→开机，循环检查数据完整性，支持 ipmi/manual/enhanced 三种模式 |
| 7 | 意外电源循环测试 (SPOR) | `testers/spor.py` | 读写过程中直接硬件断电，验证 PLP 掉电保护与 FTL 元数据备份能力，支持 timeboard/manual/enhanced 三种断电方式 |
| 8 | 操作系统中断测试 (OSINT) | `testers/os_interruption.py` | 持续 I/O 期间 S3/S4 休眠/唤醒的稳定性与数据完整性验证 |
| 9 | **设备功耗测量** | `testers/power.py` | 通过示波器采集 SSD 工作电流/电压，计算实时功耗与平均功耗，支持自动采集配置 |

### 核心架构特性

- **模块化包结构**：v1.9.3 将原 10389 行单文件拆分为 `ssd_test_tool/` 包，按测试项目、i18n、GUI、功耗测量等职责分离，每个测试项独立文件，便于维护和扩展
- **中英文互转 (i18n)**：GUI 运行时切换中英文界面，175 个翻译 key，详细日志固定纯英文输出
- **双模式运行**：
  - **命令行模式**：适合自动化/CI 集成，支持参数化配置、批量任务 JSON 加载、状态控制
  - **GUI 上位机模式**：基于 tkinter（约 2400 行），图形化选择设备/测试项/参数，SSD 状态控制面板，批量任务管理，实时日志和进度显示
- **SSD 状态管理**：FOB（蓝）/Steady（绿）/Unknown（灰）状态可视化，按设备序列号持久化，支持手动进入 FOB/进入稳态/重置 Unknown
- **真 FOB 进入**：使用 `nvme format --ses=1`（User Data Erase），失败自动回退 blkdiscard
- **SNIA 合规稳态**：WIPC（顺序写 2 遍）+ WDPC（多轮循环，3 个跟踪变量）+ 5 轮滑动窗口稳态检测（Range≤20%、|Slope|≤10%），最多 25 轮
- **功耗测量集成**：通过 subprocess 启动 oscill 示波器上位机（PyQt5 + pyqtgraph），支持 Tektronix MSO4034 示波器，自动采集配置（测试规划/总采集时间/采集间隔/通道映射）

### 功耗测量工具来源说明

本项目中的 **SSD 功耗测量功能**（`oscill/` 模块，包含示波器通信、采集控制、PyQt5 图形界面、实时波形绘制等）来源于作者的另一个独立开源项目：

**[VisaPowerScope](https://github.com/gaigaiz/VisaPowerScope)** — 基于 NI-VISA 的示波器功耗采集上位机工具

oscill 模块在本项目中作为独立子包集成，通过 `subprocess` 方式启动，与主测试框架解耦，保持独立运行窗口。如需单独使用或参与功耗工具的开发，请访问上述仓库。

## 快速使用指南

完整的环境配置与快速使用文档位于：

```
V1.9/V1.9.3/oscill-0.1.0-linux/ssd_test_tool/docs/SSD测试平台快速使用与环境配置.md
Stable version/oscill-0.1.0-linux(目前最稳定版本)/ssd_test_tool/docs/SSD测试平台快速使用与环境配置.md
```

该文档包含：操作系统与 Python 版本要求、系统依赖安装、NI-VISA 运行时安装步骤（含内置 deb 包）、USB 设备权限排查、Python 依赖安装、目录结构说明、命令行/GUI 启动方式、测试项参数详解、功耗测量配置、常见问题排查等。

其他配套文档：
- `docs/模块化重构说明.md` — v1.9.3 模块化架构详解、模块职责、依赖关系、回滚方案
- `V1.9/V1.9.2/上位机快速使用与环境配置指南.md` — v1.9.2 单文件版使用指南

## 安装方法

### 系统要求

- 操作系统：Ubuntu 22.04 LTS x86_64（推荐）/ 20.04 / 24.04
- Python：≥ 3.10（v1.9.3 模块化版测试环境 3.10.12）
- 权限：root 或 sudo 权限（nvme format、块设备操作需要）
- 示波器（功耗测试可选）：Tektronix MSO4034（通过 NI-VISA / USB 连接）

### 系统依赖安装

```bash
sudo apt update
sudo apt install -y python3-pip python3-tk fio nvme-cli smartmontools util-linux ipmitool e2fsprogs parted
```

### Python 依赖安装

```bash
pip3 install pyserial pyvisa PyQt5 pyqtgraph numpy
```

| 依赖 | 用途 |
|------|------|
| `pyserial` | SPOR Timeboard 串口控制 |
| `pyvisa` | 示波器 VISA 通信（功耗测量） |
| `PyQt5` | oscill 功耗采集 GUI |
| `pyqtgraph` | oscill 实时波形绘制 |
| `numpy` | oscill 数据处理 |

### NI-VISA 运行时安装（功耗测试需要）

项目已内置 NI 驱动仓库配置包：`ssd_test_tool/docs/NI_VISA_deb/ni-ubuntu2204-drivers-2026Q3.deb`

```bash
# 1. 安装 NI 驱动仓库配置包
sudo dpkg -i ssd_test_tool/docs/NI_VISA_deb/ni-ubuntu2204-drivers-2026Q3.deb
sudo apt install -f   # 如提示缺少依赖

# 2. 更新软件源并安装 NI-VISA 运行时
sudo apt update
sudo apt install -y ni-visa

# 3. 重启系统加载 NI 内核模块
sudo reboot

# 4. 验证安装
python3 -c "import pyvisa; rm = pyvisa.ResourceManager('@ni'); print('NI-VISA OK'); print(rm.list_resources())"
```

### 验证安装

```bash
nvme --version
smartctl --version
fio --version
python3 --version
```

## 使用方法

### 选择版本

| 版本 | 位置 | 特点 |
|------|------|------|
| **v1.9.3 模块化版（推荐）** | `V1.9/V1.9.3/oscill-0.1.0-linux/` | 模块化包结构，9 项测试，i18n，功耗测量，目前最稳定版本 |
| v1.9.2 单文件版 | `V1.9/V1.9.2/ssd_test_v1.9.2.py` | 单文件，真 FOB + SNIA 稳态 + 状态控制，无功耗测量 |
| v1.8.5 单文件版 | `V1.8/ssd_test_v1.8.5(V1.8.3+读写).py` | 单文件，8 项测试，读写修复 |

Stable version 目录下提供按测试项拆分的稳定版本，以及 `oscill-0.1.0-linux(目前最稳定版本)` 完整模块化版。

### 命令行模式（v1.9.3 模块化版）

```bash
cd /path/to/oscill-0.1.0-linux

# 运行全部测试项
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t all -y

# 仅运行 SMART 健康检查
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t smart

# 性能测试（FOB 状态）
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t perf --perf-state fob -y

# 功耗测量（启动 oscill 示波器上位机）
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t power -y

# SSD 状态控制
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 --action enter-fob -y
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 --action enter-steady -y
python3 -m ssd_test_tool.main -d /dev/nvme0n1 --action status
```

### GUI 上位机模式

```bash
cd /path/to/oscill-0.1.0-linux
sudo python3 -m ssd_test_tool.main --gui
# 或非 root 启动（执行测试时自动 pkexec 提权）
python3 -m ssd_test_tool.main --gui
```

GUI 支持：设备选择、测试项勾选、中英文切换、参数配置、SSD 状态控制面板（FOB/Steady/Unknown）、批量性能任务管理（新增/删除/上下排序/导出 JSON）、实时日志、配置保存/加载。

### 主要参数

| 参数 | 说明 |
|------|------|
| `-d, --device` | 待测设备路径，如 `/dev/nvme0n1` |
| `-t, --test` | 测试项：`fw`/`smart`/`capacity`/`perf`/`rw`/`powercycle`/`spor`/`osint`/`power`/`all` |
| `-y, --yes` | 跳过交互确认，自动执行 |
| `--gui` | 启动可视化上位机模式 |
| `--action` | 操作模式：`run-tests`/`enter-fob`/`enter-steady`/`status` |
| `--perf-state` | 性能测试目标状态：`fob`/`steady`/`unknown`（默认 unknown） |
| `--perf-task-file` | 批量性能任务 JSON 文件路径 |
| `--purge-method` | FOB 擦除方式：`auto`/`user-data`/`blkdiscard` |
| `--steady-max-rounds` | 稳态 WDPC 最大轮数（默认 25） |
| `--steady-point-duration` | 稳态每个测试点时长秒（默认 60） |
| `--smart-ignore-media-errors` | SMART 忽视的介质错误号（默认 "5353"） |
| `--osint-cycles` | OSINT 休眠唤醒循环次数 |
| `--osint-sleep-type` | OSINT 休眠类型：`s3`/`s4` |
| `--pc-cycles` | 电源循环次数 |
| `--pc-power-mode` | 电源循环模式：`ipmi`/`manual`/`enhanced` |
| `--pc-pattern-size-gb` | enhanced 模式下 pattern 大小（GB） |
| `--fw-image` | 固件升级镜像路径 |
| `--ipmi-host` | IPMI 远程管理地址 |

## 输入输出示例

### 示例：SMART 健康检查

**输入命令：**

```bash
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t smart
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

### 示例：功耗测量

**输入命令：**

```bash
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t power -y
```

**预期行为：**
- 自动启动 oscill 示波器上位机窗口（PyQt5 + pyqtgraph）
- 通过 NI-VISA 连接 Tektronix MSO4034 示波器
- 配置采集参数（测试规划/总采集时间/采集间隔/通道映射）
- 实时显示电流/电压波形与功耗曲线
- 窗口保持打开，用户可手动保存采集数据

测试报告自动保存为 `reports/ssd_test_report_YYYYMMDD_HHMMSS.json`，日志保存为 `logs/ssd_test_YYYYMMDD_HHMMSS.log`。

---

## 模块化架构（v1.9.3）

```
ssd_test_tool/
├── __init__.py                  # 包标识，版本号 1.9.3
├── main.py                      # 入口点（CLI/GUI 启动，参数解析，测试调度）
├── common.py                    # 共享模块（常量/数据类/工具函数/TestReport）
├── requirements.txt             # 依赖清单
│
├── i18n/                        # 中英文互转模块
│   └── translator.py            # Translator 类 + 175 个 key 的翻译字典
│
├── testers/                     # 按测试项目模块化（每个测试项一个文件）
│   ├── firmware.py              # FirmwareTester（固件升级/降级）
│   ├── smart.py                 # SmartTester（SMART 健康信息）
│   ├── capacity.py              # CapacityTester（设备容量）
│   ├── performance.py           # PerformanceTester（全性能特征，真FOB+SNIA稳态）
│   ├── power_cycle.py           # PowerCycleTester（正常电源循环，三模式）
│   ├── spor.py                  # SPORTester + TimeboardController（意外断电 SPOR，三模式）
│   ├── os_interruption.py       # OSInterruptionTester（操作系统中断 OSINT）
│   ├── read_write.py            # ReadWriteTester（读/写测试）
│   └── power.py                 # PowerTester（设备功耗测量，调用 oscill）
│
├── gui/                         # GUI 上位机模块
│   └── main_window.py           # SSDTestGUI 类（约 2400 行，完整上位机）
│
├── oscill/                      # 功耗测量模块（示波器采集工具，来源：VisaPowerScope）
│   ├── cli.py                   # 命令行接口
│   ├── controller.py            # 采集控制器
│   ├── gui.py                   # oscill GUI 上位机（PyQt5）
│   ├── services.py              # 测量服务
│   ├── transport.py             # VISA 通信层
│   ├── worker.py                # 后台工作线程
│   └── i18n.py                  # oscill 自身的翻译
│
├── docs/                        # 文档目录
│   ├── SSD测试平台快速使用与环境配置.md
│   ├── 模块化重构说明.md
│   └── NI_VISA_deb/ni-ubuntu2204-drivers-2026Q3.deb
├── logs/                        # 运行日志输出目录
└── reports/                     # JSON 报告输出目录
```

**模块依赖关系**：
- 所有 Tester 类依赖 `common.py` 中的 `TestConfig`、`TestResult` 及全局常量
- `PowerTester` 通过 `subprocess.Popen(["python3", "-m", "oscill.gui"])` 启动 oscill，oscill 保持独立运行，不被直接 import
- oscill 有自己独立的 `i18n.py`，与 ssd_test 的 `i18n/translator.py` 互不干扰

## 目录结构

```
.
├── Stable version/              # 稳定版本
│   ├── ssd_test_v1.5.3(SMART健康+设备容量).py
│   ├── ssd_test_v1.7.5(伪FOB_稳态性能测试）.py
│   ├── ssd_test_v1.8.3(SMART+容量+操作系统中断).py
│   ├── ssd_test_v1.8.5(V1.8.3+读写).py
│   ├── ssd_test_v1.9.2(v1.8.5+真FOB_稳态性能测试).py
│   └── oscill-0.1.0-linux(目前最稳定版本)/   # v1.9.3 模块化完整版
├── V1.0 ~ V1.8/                 # 历史单文件版本迭代
├── V1.9/
│   ├── V1.9.0/                  # Normal Power Cycle 三模式改造
│   ├── V1.9.1/                  # SPOR 三模式改造
│   ├── V1.9.2/                  # 真FOB + SNIA稳态 + GUI状态控制（单文件）
│   └── V1.9.3/                  # 模块化重构版（9项测试 + i18n + 功耗测量）
├── 迭代日志/                      # 版本迭代详细记录
├── 错误日志以及修正/               # Bug 修复记录与对应错误日志
├── 测试项目.md                    # 测试项目定义与目标说明
├── 目前未解决问题.md               # 已知待解决问题清单
├── LICENSE                        # MIT License
└── README.md
```

## 已知问题

- **稳态预处理未完整测试**：v1.9.2 已实现 SNIA 合规稳态预处理算法，已成功测试 FOB + Unknown 态性能测试，但由于时间原因尚未对稳态进行完整测试
- **固件下载功能未测试**：目前未对固件下载功能进行测试
- **电源循环测试受硬件限制**：由于硬件方面问题，无法测试正常电源循环和意外掉电循环测试
- **模块化版 Ubuntu 实测**：v1.9.3 模块化重构在 Windows 侧仅做语法检查和导入测试，实际运行需在 Ubuntu 测试平台验证

（详见 `目前未解决问题.md`）

## 版本历史

| 版本 | 核心变更 |
|------|----------|
| **V1.9.3** | 重大模块化重构：10389 行单文件拆分为 ssd_test_tool/ 包，9 项测试（新增功耗测量），中英文互转 i18n，oscill 示波器功耗采集集成，NI-VISA 支持 |
| V1.9.2 | 真 FOB（nvme format --ses=1）、SNIA SSS PTS v2.0.2 合规稳态检测、GUI SSD 状态控制面板、电源循环/SPOR 三模式、9 项 Bug 修复 |
| V1.9.1 | SPOR 意外电源循环三模式改造（timeboard/manual/enhanced） |
| V1.9.0 | Normal Power Cycle 正常电源循环三模式改造（ipmi/manual/enhanced） |
| **V1.8.5** | fio JSON 解析真正根因修复、verify 模式多 job 并发支持（offset_increment）、SMART 介质错误警告模式、GUI 布局彻底修复 |
| V1.8.4 | RW 读/写测试四项修复（fio check=False、SMART 警告、add_detail 修复、底部布局初步修复） |
| **V1.8.3** | OSINT 9 项 Bug 修复（fio 参数、文件系统保护、SMART 警告模式、状态自动重置、自动 fsck 修复、nvme flush 等） |
| V1.8.0~V1.8.2 | 多任务批量性能测试、GUI 任务管理面板、UI 交互优化、底部按钮布局修复 |
| **V1.7.5** | 修复稳态预处理随机写超时不足的问题（伪FOB，仅 blkdiscard） |
| V1.6.0 | 新增 CrystalDiskMark 风格性能测试配置+GUI 控制（因不满足读写比例控制需求已舍弃） |
| **V1.5.3** | SMART 介质错误误判修复，SMART+容量稳定版 |
| V1.0 ~ V1.4 | 逐步迭代各测试项基础功能 |

## License

MIT License — 详见 [LICENSE](LICENSE) 文件。

## 相关项目

- [VisaPowerScope](https://github.com/gaigaiz/VisaPowerScope) — 本项目功耗测量模块（oscill）的来源项目，基于 NI-VISA 的示波器功耗采集上位机工具
