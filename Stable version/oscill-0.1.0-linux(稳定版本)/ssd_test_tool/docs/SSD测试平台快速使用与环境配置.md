# SSD 测试平台快速使用与环境配置（模块化版 v1.9.3）

> 适用版本：ssd_test_tool v1.9.3（模块化重构版）
> 目标平台：Ubuntu 22.04 LTS x86_64，Python 3.10.12
> 示波器：Tektronix MSO4034（通过 NI-VISA / USB 连接）
> 文档日期：2026-09-29

---

## 1. 版本概述

本版本为 **模块化重构版**，将原单文件 `ssd_test_v1.9.3.py`（10389 行）拆分为按测试项目组织的多模块包结构，同时整合 oscill 示波器功耗采集工具作为子模块。

### 1.1 主要特性

| 特性 | 说明 |
|------|------|
| 模块化结构 | 按测试项目拆分到独立文件，中英文互转独立文件夹 |
| 9 项测试 | 固件升降级、SMART健康、设备容量、全性能特征、正常电源循环、意外断电(SPOR)、操作系统中断(OSINT)、读/写测试、设备功耗测量 |
| 中英文互转 | GUI 运行时切换中英文，详细日志固定纯英文输出 |
| 功耗测量集成 | 通过 subprocess 启动 oscill 示波器上位机，支持自动采集配置（测试规划/总采集时间/采集间隔/通道映射） |
| 无虚拟环境 | 直接使用系统 Python 3.10.12，无需创建 venv |

---

## 2. 环境配置

### 2.1 操作系统与 Python

- 操作系统：Ubuntu 22.04 LTS (Jammy Jellyfish) x86_64
- Python：系统 `python3` = **3.10.12**
- 项目根目录：`/home/test/max_tool/oscill/oscill-0.1.0-linux`
- 模块化包路径：`/home/test/max_tool/oscill/oscill-0.1.0-linux/ssd_test_tool/`

### 2.2 系统依赖安装

```bash
sudo apt update
sudo apt install -y python3-pip python3-tk fio nvme-cli smartmontools
```

### 2.3 NI-VISA 运行时安装（功耗测试需要）

功耗测量通过 oscill 子模块调用 Tektronix MSO4034 示波器，oscill 在 Linux 上**默认使用 NI-VISA 后端**（`@ni`），通过 Python `pyvisa` 库调用 `libvisa.so` 与示波器通信。

项目已内置 NI 驱动仓库配置包：

```
ssd_test_tool/docs/NI_VISA_deb/
└── ni-ubuntu2204-drivers-2026Q3.deb
```

该包是 NI 2026 Q3 驱动套件的 **apt 仓库配置包**（约 33 KB），作用是添加 NI 官方 apt 软件源、导入 GPG 密钥、注册驱动套件优先级。

#### 2.3.1 安装 NI 驱动仓库配置包

```bash
cd /home/test/max_tool/oscill/oscill-0.1.0-linux
sudo dpkg -i ssd_test_tool/docs/NI_VISA_deb/ni-ubuntu2204-drivers-2026Q3.deb
```

如果提示缺少依赖，执行：

```bash
sudo apt install -f
```

#### 2.3.2 更新软件源并安装 NI-VISA 运行时

```bash
sudo apt update
sudo apt install -y ni-visa
```

> `ni-visa` 会自动拉入依赖（包括 NI USB 驱动内核模块、`libvisa.so` 运行时库等）。如果 `ni-visa` 包名不可用，可执行 `apt search ni-visa` 查看实际包名（部分版本中运行时包名为 `ni-visa-runtime`）。

安装过程中可能出现 NI 最终用户许可协议（EULA）提示，按提示确认即可。

#### 2.3.3 重启系统

NI 内核模块需要在重启后加载：

```bash
sudo reboot
```

#### 2.3.4 验证 NI-VISA 安装

```bash
# 检查运行时库
ls /usr/lib/x86_64-linux-gnu/libvisa*
# 预期输出: libvisa.so.0, libvisa.so.0.0.0

# 检查内核模块
lsmod | grep ni
# 预期能看到 ni_usb 等相关模块

# 用 Python pyvisa 验证（无虚拟环境，直接用系统 python3）
python3 -c "import pyvisa; rm = pyvisa.ResourceManager('@ni'); print('NI-VISA backend OK'); print(rm.list_resources())"
```

- 输出 `NI-VISA backend OK` 表示 pyvisa 成功加载 NI-VISA 后端
- `list_resources()` 会列出当前连接的 VISA 资源（未接设备时可能为空元组 `()`）

> **不要用 pip 升级 pyvisa**，系统 pyvisa 已与 NI-VISA 运行时匹配，pip 升级可能导致版本冲突。

#### 2.3.5 USB 设备权限排查

NI-VISA 安装时通常会自动配置 udev 规则。如果 `list_resources` 看不到设备或报 `VI_ERROR_PERM`：

```bash
# 确认设备已被系统识别
lsusb | grep -i tek
# 预期输出: Bus 003 Device 005: ID 0699:0408 Tektronix, Inc.

# 检查 NI udev 规则
ls /etc/udev/rules.d/ | grep -i ni

# 重新加载 udev 规则
sudo udevadm control --reload-rules
sudo udevadm trigger
# 然后重新插拔示波器 USB 线
```

如果 udev 规则未生效，可将当前用户加入 `plugdev` 组（注销重新登录后生效）：

```bash
sudo usermod -aG plugdev $USER
```

### 2.4 oscill 示波器 GUI 依赖（功耗测试需要）

```bash
sudo apt install -y python3-pyqt5 python3-numpy
pip3 install "pyqtgraph>=0.13,<0.14"
```

> oscill 作为 `ssd_test_tool` 的子包，由 SSD 测试平台自动启动，无需单独安装 oscill 包。GUI 依赖（PyQt5 / pyqtgraph）用于 oscill 上位机界面显示。

### 2.5 验证环境

```bash
python3 --version                    # 应输出 Python 3.10.12
python3 -c "import tkinter; print('tkinter OK')"
python3 -c "import PyQt5; print('PyQt5 OK')"   # 功耗测试需要
python3 -c "import pyvisa; print('pyvisa OK')"  # 功耗测试需要
```

---

## 3. 目录结构

```
oscill-0.1.0-linux/
├── ssd_test_tool/                    # 【SSD测试平台主包】
│   ├── __init__.py
│   ├── main.py                       # 入口点（CLI + GUI）
│   ├── common.py                     # 共享数据类与全局常量
│   │
│   ├── i18n/                         # 【中英文互转模块】
│   │   ├── __init__.py
│   │   └── translator.py             # Translator类 + 翻译字典
│   │
│   ├── testers/                      # 【按测试项目模块化】
│   │   ├── __init__.py
│   │   ├── firmware.py               # 固件升级/降级
│   │   ├── smart.py                  # SMART健康信息
│   │   ├── capacity.py               # 设备容量
│   │   ├── performance.py            # 全性能特征
│   │   ├── power_cycle.py            # 正常电源循环
│   │   ├── spor.py                   # 意外断电SPOR
│   │   ├── os_interruption.py        # 操作系统中断OSINT
│   │   ├── read_write.py             # 读/写测试
│   │   └── power.py                  # 功耗测量（调用oscill）
│   │
│   ├── oscill/                       # 【功耗测量模块】示波器采集工具
│   │   ├── __init__.py
│   │   ├── gui.py                    # oscill GUI上位机
│   │   ├── cli.py                    # 命令行接口
│   │   ├── controller.py             # 采集控制器
│   │   ├── services.py               # 测量服务
│   │   ├── transport.py              # VISA通信层
│   │   ├── worker.py                 # 后台工作线程
│   │   └── i18n.py                   # oscill自身翻译
│   │
│   ├── gui/                          # GUI上位机模块
│   │   ├── __init__.py
│   │   └── main_window.py            # SSDTestGUI
│   │
│   ├── docs/                         # 文档
│   │   ├── NI_VISA_deb/              # NI-VISA 驱动仓库配置包
│   │   │   └── ni-ubuntu2204-drivers-2026Q3.deb
│   │   ├── NI-VISA安装与使用说明.md
│   │   ├── SSD测试平台快速使用与环境配置.md
│   │   └── 模块化重构说明.md
│   ├── logs/                         # 运行日志输出目录
│   └── reports/                      # JSON报告输出目录
│
└── ssd_test_v1.9.3.py                # 原单文件版本（保留作备份）
```

---

## 4. 启动方式

### 4.1 进入项目目录

```bash
cd /home/test/max_tool/oscill/oscill-0.1.0-linux
```

### 4.2 启动 GUI 上位机

```bash
# 方式1：非 root 启动（执行测试时自动 pkexec 提权）
python3 -m ssd_test_tool.main --gui

# 方式2：sudo 启动（直接获得 root 权限）
sudo python3 -m ssd_test_tool.main --gui
```

> **注意**：必须在项目根目录（`oscill-0.1.0-linux/`）下执行，因为使用 `-m` 模块方式启动。

### 4.3 命令行模式（CLI）

```bash
# 运行全部测试
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t all -y

# 运行指定测试项
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t smart,capacity,rw -y

# 功耗测试（自动启动 oscill）
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t power \
    --power-plan POWER-05 --power-total-duration 10 \
    --power-sample-interval 2 --power-channels CH1,CH2 -y
```

### 4.4 常用 CLI 参数

| 参数 | 说明 |
|------|------|
| `-d, --device` | 待测设备路径，如 `/dev/nvme0n1` |
| `-t, --tests` | 测试项，逗号分隔：`smart,capacity,perf,rw,power,osint,spor,powercycle,firmware,all` |
| `-o, --output-dir` | 报告输出目录（默认：`ssd_test_tool/reports/`） |
| `--log-dir` | 日志输出目录（默认：`ssd_test_tool/logs/`） |
| `-y, --yes` | 跳过数据销毁确认 |
| `--gui` | 启动图形界面 |
| `--dry-run` | 试运行模式，只打印命令不执行 |

---

## 5. 报告与日志路径

### 5.1 默认路径（模块化版）

| 类型 | 默认路径 | 说明 |
|------|---------|------|
| JSON 报告 | `ssd_test_tool/reports/` | 所有测试项目的 JSON 报告统一输出到此目录 |
| 运行日志 | `ssd_test_tool/logs/` | 详细日志（.log 文件）统一输出到此目录 |
| oscill 功耗 JSON | `ssd_test_tool/reports/` | 功耗测试自动导出的 JSON 报告也输出到此目录 |

> 路径为基于包位置的**绝对路径**，不受当前工作目录影响。

### 5.2 自定义路径

- GUI：在"报告输出目录"输入框中手动修改
- CLI：使用 `-o /path/to/reports` 参数指定

### 5.3 日志文件命名

- SSD 测试日志：`ssd_test_YYYYMMDD_HHMMSS.log`
- OSINT 结果：`osint_results_YYYYMMDD_HHMMSS.json`
- JSON 报告：`ssd_test_report_YYYYMMDD_HHMMSS.json`

---

## 6. 功耗测试功能

### 6.1 两种启动方式

1. **GUI 独立启动**：在 SSD 测试平台 GUI 中点击"启动功耗测量工具"按钮，手动操作 oscill
2. **自动测试启动**：在 GUI 中勾选"设备功耗测量"测试项并点击开始测试，自动启动 oscill 并传入配置

### 6.2 自动采集配置

通过环境变量传递给 oscill：

| 环境变量 | 说明 |
|---------|------|
| `OSCILL_AUTO_PLAN` | 测试规划（如 POWER-05） |
| `OSCILL_AUTO_DURATION` | 总采集时间（秒） |
| `OSCILL_AUTO_INTERVAL` | 采集间隔（秒） |
| `OSCILL_AUTO_CHANNELS` | 通道选择（如 CH1,CH2） |
| `OSCILL_AUTO_JSON_DIR` | JSON 报告自动导出目录 |

### 6.3 操作流程

1. 在 SSD 测试平台 GUI 功耗测试参数页配置：测试规划、总采集时间、采集间隔、通道选择、JSON 报告路径
2. 勾选"设备功耗测量"测试项，点击开始测试
3. oscill 上位机自动启动并加载配置（通道和规划自动选中）
4. 在 oscill 中点击"开始连续测量"开始采集
5. 到达总采集时间后自动停止采集，状态显示"测试已完成"
6. oscill 窗口**保持打开**，可手动查看数据或关闭
7. 关闭 oscill 窗口后，SSD 测试平台自动解析 JSON 报告并输出结果

### 6.4 JSON 转 Log 功能

在 SSD 测试平台 GUI 功耗测试参数页：
1. 选择 JSON 报告路径
2. 点击"转换为 Log"按钮
3. 自动将 JSON 报告转换为纯英文功耗测试日志（.log 文件）

---

## 7. 中英文互转

### 7.1 GUI 语言切换

- GUI 顶部语言选择下拉框：中文 / English
- 运行时切换，**无需重启**
- 所有标签、按钮、参数页、状态提示立即切换

### 7.2 详细日志

- **详细日志固定为纯英文输出**，不随 GUI 语言切换
- 所有测试项目的日志消息、结果摘要、错误提示均为英文
- 确保日志文件可在英文环境下正常阅读

### 7.3 oscill 工具语言

- oscill 上位机自身支持中英文切换（独立于 SSD 测试平台）
- oscill 操作日志在自动采集模式下固定为英文输出

---

## 8. 常见问题（FAQ）

**Q1：启动报 `ModuleNotFoundError: No module named 'ssd_test_tool'`**
A：必须在项目根目录（`oscill-0.1.0-linux/`）下执行 `python3 -m ssd_test_tool.main`，不能在其他目录执行。

**Q2：GUI 启动报 `NameError: name '_I18N_ZH_TEXTS' is not defined`**
A：模块化版本已修复此问题，请确保使用最新的 `ssd_test_tool/gui/main_window.py`。

**Q3：CLI 运行报 `TypeError: TestConfig() takes no arguments`**
A：模块化版本已修复此问题（@dataclass 装饰器已补回），请确保使用最新代码。

**Q4：进入 FOB/Steady 状态报 `ImportError: attempted relative import with no known parent package`**
A：模块化版本已修复此问题，状态转换子进程使用 `-m ssd_test_tool.main` 方式启动。

**Q5：功耗测试 JSON 报告没有自动导出**
A：检查是否在 GUI 中配置了 JSON 报告路径，或使用 `--power-json-report` 参数指定。默认路径为 `ssd_test_tool/reports/`。

**Q6：oscill 窗口在采集完成后自动关闭**
A：模块化版本已修改为**保持 oscill 窗口打开**，仅修改 SSD 测试平台状态为"测试已完成"，不会自动关闭 oscill。

**Q7：详细日志中还有中文**
A：模块化版本已将所有测试项目的详细日志改为纯英文输出。如仍发现中文，请提供日志文件以便定位修复。

**Q8：性能测试无法添加多个任务**
A：模块化版本已修复此问题（PerfTask @dataclass 装饰器已补回），可正常添加多任务。

**Q9：oscill 启动报 `VI_ERROR_INV_SETUP` 或找不到 NI-VISA 后端**
A：NI-VISA 未正确安装或 `libvisa.so` 不在库路径中。重新执行第 2.3 节安装步骤，确认 `ls /usr/lib/x86_64-linux-gnu/libvisa*` 有输出，并重启系统。

**Q10：`oscill list-resources` 输出为空，但 `lsusb` 能看到示波器**
A：USB 权限问题。按第 2.3.5 节检查 udev 规则，重新加载规则并重新插拔 USB 线。确认示波器已开机，VISA 资源名格式为 `USB0::0x0699::0x0408::<序列号>::INSTR`。

**Q11：可以用 pyvisa-py（@py）替代 NI-VISA 吗？**
A：技术上可以，但需要额外安装 `pyvisa-py`、`pyusb` 和 `libusb`，且部分 Tektronix 二进制波形传输在 pyvisa-py 下可能不稳定。oscill 默认并推荐使用 NI-VISA（`@ni`）。

---

## 9. 验证清单

### 9.1 语法验证（Windows 侧）

```bash
# 在项目根目录执行
python -m py_compile ssd_test_tool/main.py
python -m py_compile ssd_test_tool/common.py
python -m py_compile ssd_test_tool/gui/main_window.py
# 全部25个 .py 文件应通过
```

### 9.2 真机验证（Ubuntu 侧）

- [ ] `python3 -m ssd_test_tool.main --gui` 能正常启动 GUI
- [ ] GUI 中英文切换正常
- [ ] SMART / 容量 / 读写测试正常运行
- [ ] 性能测试可添加多个任务
- [ ] 进入 FOB / Steady 状态正常
- [ ] NI-VISA 安装正常：`python3 -c "import pyvisa; pyvisa.ResourceManager('@ni')"` 无报错
- [ ] 示波器 USB 连接正常：`lsusb | grep tek` 能看到设备
- [ ] 功耗测试自动启动 oscill，通道/规划自动映射
- [ ] 功耗采集到达设定时间自动停止，oscill 窗口保持打开
- [ ] JSON 报告输出到 `ssd_test_tool/reports/`
- [ ] 详细日志为纯英文，输出到 `ssd_test_tool/logs/`

---

*如有问题，请先确认使用最新模块化代码，再按第 8 节 FAQ 逐项排查。*
