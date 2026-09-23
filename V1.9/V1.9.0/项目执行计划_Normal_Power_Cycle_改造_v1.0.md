# SSD Normal Power Cycle Test 改造项目执行计划

> 版本：v1.0 | 生成日期：2026-09-18 | 状态：待确认

---

## 1. 项目概述

本项目对 `ssd_test_v1.8.5.py` 中的 **Normal Power Cycle Test（正常电源循环测试）** 模块进行功能增强改造。核心目标是：在保留现有 IPMI 远程电源控制和 Manual 手动断电两种模式的基础上，新增第三种"增强方案（enhanced）"——该方案融合从 OKN 企业级 SSD 测试框架（`power_cycle_test.py`、`npor_test.py`、`spor_test.py`、`okn_common_function.py`）中学到的最佳实践，包括：断电前 NVMe 优雅移除（remove_device）、循环前写入已知 Pattern 数据、循环后 LBA 级 Pattern 校验读、PCIe Link 速率/宽度一致性校验及跨循环统计。用户可通过命令行参数 `--pc-power-mode` 或 GUI 上位机下拉框在 `ipmi` / `manual` / `enhanced` 三种模式间选择。

本电脑仅负责代码书写，不负责代码测试；所有代码修改须保证版本兼容（Python 3.10.12 / Ubuntu 22.04），且不干扰脚本中其他测试项（固件、SMART、容量、性能、SPOR、OSINT、RW）的正常功能。

---

## 2. 原始需求原文

> 以下为用户输入的原始需求，未经修改。

第 1 步：阅读我的所有代码（除了ssd_test_v1.8.5.py这个代码）；

第 2 步：阅读我的ssd_test_v1.8.5.py(平台：Linux+Ubuntu2204+python3.10.12)；

第 3 步：将原本我的（Normal Power Cycle Test 正常电源循环测试 运行持续混合读写；正常关机 → 断电；开机并启动系统；检查磁盘、分区、文件系统及数据；循环重复，最终完整功能测试）这个测试功能进行修改，保留ipmi工具的功能那个以及manual手动断电这两个功能，并添加你学习从第1步学习到的对于Normal Power Cycle Test 正常电源循环测试的测试方式并添加到我的ssd_test_v1.8.5.py中，并且我可以通过软件上位机选项进行选择是采用ipmi,manual,新方案（交付我1份修改好的完整脚本代码）；

第 4 步：交付1份代码修改的md文档，还有交付1份环境配置以及快速使用文档（以md文档交付）；

注意事项：1.每次进行代码修改时注意版本兼容，并且要确保其他的代码功能不会被干扰；2.本电脑只负责对于代码书写并不负责代码测试；根据我的skill交付我1份项目执行计划

### 需求冲突项

无冲突。需求表述清晰，三种模式为并列可选关系，不存在互斥矛盾。

---

## 3. 工程前置约束

### 3.1 技术栈与环境

| 项目 | 约束 |
|------|------|
| 操作系统 | Linux Ubuntu 22.04 LTS |
| Python 版本 | 3.10.12（仅标准库 + tkinter，不引入第三方依赖） |
| 目标脚本 | `ssd_test_v1.8.5.py`（单文件，6666 行） |
| 外部依赖工具 | nvme-cli, smartmontools, fio, util-linux, ipmitool, e2fsprogs, parted, rtcwake |
| 代码基线 | ssd_test_v1.8.5.py 当前版本（SCRIPT_VERSION = "1.8.5"） |
| 开发环境 | Windows 本机仅书写代码，不在本机执行测试 |

### 3.2 选定方案与备选

| 需求项 | 选定方案 | 备选方案 | 选择理由 |
|-------|---------|---------|---------|
| 新方案命名 | `enhanced`（增强模式） | `new` / `okn` / `advanced` | `enhanced` 语义清晰，表明是在现有基础上增强而非替换；避免与"new"这种模糊词混淆 |
| 断电前优雅移除 | 使用 `nvme disconnect` / 写 NVMe CC 寄存器 Shutdown Notification（CC.SHN） | 仅 `umount` + `sync` | 参考 OKN `link_state.remove_device()` 实现，向 SSD 发送优雅关机通知，确保 FW 完成缓存刷写，比纯 OS shutdown 更彻底 |
| Pattern 数据校验 | 循环前用 fio 写入固定 Pattern（如 0xAA），循环后用 fio verify 读 + `--verify_pattern` 校验 | 仅 SHA-256 静态文件校验 | 参考 OKN `npor_test.py` 的 RANDOM_LBA pattern 写入 + verify_read(do_data_compare=True, verify_pattern=True)，能检测 LBA 级数据损坏，比文件级 SHA-256 更严格 |
| PCIe Link 校验 | 循环后用 `nvme get-phy` 或读取 `/sys/bus/pci/devices/` 获取 current link width/speed，与初始值比对并统计 | 不做 Link 校验 | 参考 OKN `check_pcie_link()` 和 width_qty/speed_qty 统计机制，能捕获掉盘后降速/降宽等隐性问题 |
| 模式选择入口 | `--pc-power-mode`  choices 扩展为 `["ipmi","manual","enhanced"]`，GUI Combobox 同步扩展 | 新增独立参数 `--pc-scheme` | 复用现有参数通道，改动最小，用户体验一致 |

### 3.3 其他约束

- **向后兼容**：`--pc-power-mode` 默认值仍为 `ipmi`；不传该参数时行为与 v1.8.5 完全一致。
- **不干扰其他测试项**：仅修改 `PowerCycleTester` 类、`TestConfig` 中 pc 相关字段、`parse_args()` 中 pc 参数、GUI `_build_powercycle_tab()` 和 `_build_cmd()` 中 pc 参数拼接；不触碰 FirmwareTester、SmartTester、CapacityTester、PerformanceTester、SPORTester、OSInterruptionTester、ReadWriteTester、TestReport 等类。
- **Python 3.10 兼容**：不使用 3.10+ 专属语法（如 match-case）；dataclass、typing 用法保持与现有代码一致。
- **状态文件兼容**：新增字段使用 `state.setdefault()` 读取，旧状态文件可正常加载；enhanced 模式使用独立状态文件后缀（如 `_enhanced`）避免与 ipmi/manual 模式状态混淆。

---

## 4. 任务拆解

### 4.1 任务总览

| 子任务编号 | 子任务名称 | 优先级 | 依赖 | 涉及模块/文件 |
|-----------|-----------|-------|------|-------------|
| T-01 | 常量与配置层扩展：新增 enhanced 模式常量、TestConfig 字段、默认参数 | P0 | 无 | ssd_test_v1.8.5.py 常量区(130-140)、TestConfig(452-462) |
| T-02 | PowerCycleTester 新增 enhanced 模式核心方法：NVMe 优雅移除、Pattern 写入/校验、PCIe Link 校验 | P0 | T-01 | PowerCycleTester 类(1957-2603) |
| T-03 | PowerCycleTester.run() 主流程分支改造：三模式调度、enhanced 循环逻辑 | P0 | T-02 | PowerCycleTester.run()(2427-2603) |
| T-04 | 命令行参数层扩展：parse_args choices 扩展、新增 enhanced 专属参数、main() TestConfig 传参 | P0 | T-01 | parse_args()(5088-5108)、main()(5274-5283) |
| T-05 | GUI 上位机层扩展：Combobox 三选项、enhanced 专属参数控件、命令拼接 | P1 | T-04 | _build_powercycle_tab()(6093-6132)、_build_cmd()(6518-6528) |
| T-06 | 版本号升级与文档头部更新 | P1 | T-03 | SCRIPT_VERSION(57)、文件头 docstring(3-29) |
| T-07 | 代码静态自检：语法编译、import 检查、未使用变量排查 | P0 | T-01~T-06 | 全文件 |
| T-08 | 交付代码修改说明文档（MD） | P1 | T-07 | 新建 `代码修改说明_v1.9.0.md` |
| T-09 | 交付环境配置与快速使用文档（MD） | P1 | T-07 | 新建 `环境配置与快速使用指南_v1.9.0.md` |

### 4.2 子任务详情

#### T-01：常量与配置层扩展

- **优先级**：P0
- **依赖**：无
- **目标**：在不破坏现有常量和配置的前提下，新增 enhanced 模式所需的常量和 TestConfig 字段。
- **操作步骤**：
  1. 在常量区（约 130-140 行）新增：
     - `DEFAULT_PC_PATTERN = "0xAA"`（enhanced 模式写入 Pattern）
     - `DEFAULT_PC_PATTERN_SIZE_GB = 20`（enhanced 模式 Pattern 数据写入量，默认 20GB，参考 npor 80GB 但考虑测试时长取 20GB）
     - `DEFAULT_PC_LINK_CHECK = True`（是否启用 PCIe Link 校验）
  2. 在 `TestConfig` 类（约 452-462 行）pc 配置区新增字段：
     - `pc_pattern: str = DEFAULT_PC_PATTERN`
     - `pc_pattern_size_gb: int = DEFAULT_PC_PATTERN_SIZE_GB`
     - `pc_link_check: bool = DEFAULT_PC_LINK_CHECK`
  3. 确认 `pc_power_mode` 字段注释更新为 `# ipmi / manual / enhanced`。
- **涉及文件/模块**：`ssd_test_v1.8.5.py` 常量定义区、`TestConfig` dataclass
- **验收标准**：`TestConfig` 实例化不报错；新增字段有合理默认值；旧字段值不变。

#### T-02：PowerCycleTester 新增 enhanced 模式核心方法

- **优先级**：P0
- **依赖**：T-01
- **目标**：在 `PowerCycleTester` 类中新增 enhanced 模式所需的独立方法，与现有 ipmi/manual 方法并存，不修改现有方法签名。
- **操作步骤**：
  1. **新增 `nvme_graceful_remove()` 方法**（参考 OKN `link_state.remove_device()` / `power_down(safe_shutdown=True)`）：
     - 执行 `sync` 确保文件系统刷写
     - 卸载测试分区 `umount`
     - 执行 `nvme disconnect /dev/nvmeX`（或通过写 NVMe CC.SHN 寄存器触发 Shutdown Notification）
     - 等待设备节点消失（超时 30s）
     - 返回 (success: bool, message: str)
  2. **新增 `write_pattern_data()` 方法**（参考 OKN `npor_test.py` 顺序写 + Pattern）：
     - 使用 fio `--rw=write --bs=128k --iodepth=256 --buffer_pattern=0xAA` 写入指定 GB 量数据到裸设备或测试分区
     - 记录写入起始 LBA、大小、Pattern 到 state 文件
     - 返回 (success: bool, stats: dict)
  3. **新增 `verify_pattern_data()` 方法**（参考 OKN `npor_test.py` verify_read + verify_pattern）：
     - 使用 fio `--rw=read --bs=128k --iodepth=256 --verify=pattern --verify_pattern=0xAA --do_verify=1` 校验读
     - 解析 fio 输出获取 verify 错误数
     - 返回 (all_ok: bool, result: dict) 包含 total_blocks、verified_blocks、mismatch_blocks
  4. **新增 `check_pcie_link_state()` 方法**（参考 OKN `check_pcie_link()` + width_qty/speed_qty 统计）：
     - 通过 `nvme get-phy <ctrl> -o json` 或读取 `/sys/bus/pci/devices/<bdf>/current_link_width` 和 `current_link_speed` 获取当前 Link 状态
     - 与初始 Link 状态（setup 阶段记录）比对
     - 在 state 中维护 `link_width_stats` 和 `link_speed_stats` 计数器字典
     - 返回 (ok: bool, details: dict)
  5. **新增 `enhanced_post_cycle_check()` 方法**：整合 enhanced 模式开机后的完整检查流程：
     - 磁盘枚举（复用现有逻辑）
     - 分区表检查（复用现有逻辑）
     - 文件系统 fsck（复用现有逻辑）
     - **Pattern 数据校验**（调用 verify_pattern_data）
     - **PCIe Link 校验**（调用 check_pcie_link_state）
     - SMART 检查（复用现有逻辑）
     - 返回 (all_ok: bool, result: dict)
- **涉及文件/模块**：`PowerCycleTester` 类内部新增方法
- **验收标准**：所有新增方法独立可调用；不修改现有 `ipmi_power_off/on`、`graceful_shutdown`、`check_disk_after_boot`、`run_final_functional_test` 等方法签名和内部逻辑。

#### T-03：PowerCycleTester.run() 主流程分支改造

- **优先级**：P0
- **依赖**：T-02
- **目标**：在 `run()` 方法中根据 `self.cfg.pc_power_mode` 分发到三种模式的执行逻辑，enhanced 模式走独立循环流程。
- **操作步骤**：
  1. 在 `run()` 方法的 IPMI host 检查之后，新增 enhanced 模式前置校验：
     - enhanced 模式下 `ipmi_host` 为可选项（enhanced 模式默认使用本机 NVMe 命令实现优雅移除，不依赖 IPMI；但如果用户同时指定了 ipmi-host，可在断电步骤复用 IPMI 断电）
     - 校验 `pc_pattern_size_gb > 0`
  2. enhanced 模式 setup 阶段扩展：
     - 复用现有 `setup_test_partition()` 创建分区
     - **新增**：调用 `write_pattern_data()` 写入 Pattern 数据并记录到 state
     - **新增**：记录初始 PCIe Link 状态到 state（`initial_link_width`、`initial_link_speed`）
     - 写入完整性测试文件（复用现有 `write_integrity_data`，作为补充校验）
  3. enhanced 模式循环体（替换 ipmi/manual 共用的混合读写+关机逻辑）：
     - 启动混合读写负载（复用 `start_mixed_rw_load`，持续 `pc_rw_duration` 秒）
     - 停止混合读写负载
     - **调用 `nvme_graceful_remove()`** 执行 NVMe 优雅移除（关键增强点）
     - 保存 state 为 `PHASE_BOOT_CHECK`
     - 断电策略：
       - 若指定了 `ipmi_host`：复用 IPMI 后台脚本断电→延时→上电
       - 若未指定：走 `graceful_shutdown()`（OS shutdown），提示用户手动断电上电（与 manual 模式一致）
  4. enhanced 模式 boot_check 阶段：
     - 重新挂载分区
     - **调用 `enhanced_post_cycle_check()`** 替代现有 `check_disk_after_boot()`
     - 记录 cycle_result（包含 pattern 校验结果、link 校验结果）
  5. 最终功能测试阶段：复用现有 `run_final_functional_test()`，不做修改。
  6. ipmi 和 manual 模式的执行逻辑保持与原代码完全一致，仅通过 `if/elif/else` 分支隔离。
- **涉及文件/模块**：`PowerCycleTester.run()` 方法
- **验收标准**：
  - `pc_power_mode="ipmi"` 时，执行路径与 v1.8.5 原逻辑逐行一致；
  - `pc_power_mode="manual"` 时，执行路径与 v1.8.5 原逻辑逐行一致；
  - `pc_power_mode="enhanced"` 时，走新增的 enhanced 流程；
  - 三种模式共享 `load_state/save_state`、`run_final_functional_test` 等基础设施。

#### T-04：命令行参数层扩展

- **优先级**：P0
- **依赖**：T-01
- **目标**：扩展 `parse_args()` 和 `main()` 以支持 enhanced 模式及专属参数。
- **操作步骤**：
  1. `parse_args()` 中 `--pc-power-mode` 的 `choices` 从 `["ipmi", "manual"]` 扩展为 `["ipmi", "manual", "enhanced"]`，help 文本同步更新。
  2. 新增 enhanced 专属命令行参数：
     - `--pc-pattern`：默认 `"0xAA"`，help="enhanced 模式写入数据 Pattern (默认: 0xAA)"
     - `--pc-pattern-size-gb`：type=int, default=20, help="enhanced 模式 Pattern 数据写入量 GB (默认: 20)"
     - `--pc-no-link-check`：action="store_true", help="enhanced 模式禁用 PCIe Link 校验"
  3. `main()` 中 `TestConfig(...)` 构造调用新增参数传递：
     - `pc_pattern=args.pc_pattern`
     - `pc_pattern_size_gb=args.pc_pattern_size_gb`
     - `pc_link_check=not args.pc_no_link_check`
  4. 更新 `parse_args()` 的 epilog 示例，新增 enhanced 模式示例：
     - `sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-power-mode enhanced --pc-pattern-size-gb 20 -y`
  5. 更新 `main()` 中电源循环配置打印日志（约 5332-5334 行），enhanced 模式时额外打印 Pattern 大小和 Link 校验开关。
- **涉及文件/模块**：`parse_args()`、`main()`
- **验收标准**：`python3 ssd_test.py --help` 正常输出，新增参数可见；`--pc-power-mode enhanced` 可被 argparse 接受；不传新参数时使用默认值。

#### T-05：GUI 上位机层扩展

- **优先级**：P1
- **依赖**：T-04
- **目标**：在 GUI 上位机的"正常电源循环"标签页中新增 enhanced 模式选项及专属参数控件，并在命令拼接时传递。
- **操作步骤**：
  1. `_build_powercycle_tab()` 中：
     - `pc_power_mode` Combobox 的 `values` 从 `["ipmi", "manual"]` 扩展为 `["ipmi", "manual", "enhanced"]`
     - 新增"数据 Pattern"标签 + Entry（`pc_pattern`，默认 "0xAA"）
     - 新增"Pattern写入量(GB)"标签 + Spinbox（`pc_pattern_size_gb`，默认 20，范围 1-500）
     - 新增"PCIe Link校验"标签 + Checkbutton（`pc_link_check`，默认勾选）
     - 为 Combobox 绑定 `<<ComboboxSelected>>` 事件：选择 enhanced 时启用 Pattern/Link 控件，选择 ipmi/manual 时禁用（灰显），提升用户体验
  2. `_build_cmd()` 中电源循环参数拼接部分（约 6518-6528 行）：
     - 新增 `--pc-pattern`、`--pc-pattern-size-gb` 参数传递
     - 若 `pc_link_check` 未勾选，追加 `--pc-no-link-check`
  3. 确认 GUI 启动时 `config_vars` 字典初始化包含新增 key。
- **涉及文件/模块**：`SSDTestGUI._build_powercycle_tab()`、`SSDTestGUI._build_cmd()`
- **验收标准**：GUI 正常启动无报错；"正常电源循环"标签页可见三个模式选项；选择 enhanced 时新控件可用；生成的命令行包含 enhanced 专属参数。

#### T-06：版本号升级与文档头部更新

- **优先级**：P1
- **依赖**：T-03
- **目标**：升级脚本版本号，更新文件头说明。
- **操作步骤**：
  1. `SCRIPT_VERSION` 从 `"1.8.5"` 升级为 `"1.9.0"`（次版本号升级，表示新增功能而非 bugfix）。
  2. 文件头 docstring（第 3-29 行）中：
     - "正常电源循环测试"描述更新为支持三种模式（ipmi/manual/enhanced）
     - 用法示例新增 enhanced 模式示例
     - 版本说明新增 v1.9.0 变更摘要
- **涉及文件/模块**：文件头、SCRIPT_VERSION 常量
- **验收标准**：`--version` 输出 `ssd_test.py v1.9.0`；文件头描述准确。

#### T-07：代码静态自检

- **优先级**：P0
- **依赖**：T-01~T-06
- **目标**：在不执行实际测试的前提下，通过静态检查确保代码语法正确、无明显逻辑错误。
- **操作步骤**：
  1. **语法编译检查**：`python3 -m py_compile ssd_test_v1.9.0.py`（在 Windows 本机用 Python 执行，仅检查语法不执行）
  2. **import 检查**：确认所有 import 均为标准库或已有模块，无新增第三方依赖
  3. **未定义名称检查**：全局搜索新增方法名和变量名，确认所有引用处均有定义
  4. **缩进/格式检查**：确认新增代码缩进与现有代码一致（4 空格），无 Tab 混用
  5. **回归检查**：逐行比对 ipmi/manual 模式路径，确认未被意外修改
  6. **dataclass 字段检查**：确认 TestConfig 新增字段均有默认值，不影响位置参数构造
- **涉及文件/模块**：全文件
- **验收标准**：`py_compile` 无报错；无未定义名称；ipmi/manual 模式代码与 v1.8.5 逐行一致（除注释外）。

#### T-08：交付代码修改说明文档（MD）

- **优先级**：P1
- **依赖**：T-07
- **目标**：编写详细的代码修改说明文档，记录所有变更点。
- **操作步骤**：
  1. 文档结构：
     - 版本信息（v1.8.5 → v1.9.0）
     - 修改概述
     - 新增功能详细说明（enhanced 模式的每个增强点）
     - 逐区域修改清单（常量区、TestConfig、PowerCycleTester 新增方法、run() 分支、parse_args、main、GUI）
     - 三种模式对比表（ipmi / manual / enhanced 在各步骤的行为差异）
     - 兼容性说明（向后兼容、状态文件兼容、其他测试项不受影响）
     - 已知限制（enhanced 模式需要 nvme-cli 支持 disconnect、Pattern 写入耗时等）
- **涉及文件/模块**：新建 `代码修改说明_v1.9.0.md`
- **验收标准**：文档内容完整，覆盖所有代码变更；三种模式对比清晰；用户可通过文档理解 enhanced 模式的增强点。

#### T-09：交付环境配置与快速使用文档（MD）

- **优先级**：P1
- **依赖**：T-07
- **目标**：编写环境配置和快速使用指南，帮助用户在 Ubuntu 22.04 上快速部署和运行。
- **操作步骤**：
  1. 文档结构：
     - 系统要求（Ubuntu 22.04、Python 3.10.12、内核版本建议）
     - 依赖工具安装清单及命令（nvme-cli、smartmontools、fio、ipmitool、e2fsprogs、parted、rtcwake、python3-tk）
     - 脚本部署（文件放置、权限设置、可执行位）
     - 三种模式快速上手命令示例（每种模式至少 2 个示例）
     - GUI 上位机启动方式
     - enhanced 模式参数详解表
     - 状态文件说明与断点恢复机制
     - 常见问题排查（设备找不到、IPMI 连接失败、Pattern 校验失败、Link 降速等）
     - 日志与报告输出位置说明
- **涉及文件/模块**：新建 `环境配置与快速使用指南_v1.9.0.md`
- **验收标准**：文档可指导用户从零完成环境搭建到运行测试；命令均可复制执行；常见问题覆盖典型故障场景。

---

## 5. 代码变更规则

### 5.1 变更总览

| 子任务 | 变更类型 | 变更范围 | 影响面 | 兼容性 |
|-------|---------|---------|-------|-------|
| T-01 | 新增 | 常量区新增 3 个常量；TestConfig 新增 3 个字段 | 仅 PowerCycleTester 读取新字段 | 完全兼容（新字段有默认值） |
| T-02 | 新增 | PowerCycleTester 新增 5 个方法 | 仅 enhanced 模式调用 | 完全兼容（不修改现有方法） |
| T-03 | 修改 | PowerCycleTester.run() 内新增 if/elif 分支 | enhanced 模式走新逻辑；ipmi/manual 走原逻辑 | 完全兼容（原逻辑代码不变） |
| T-04 | 修改 | parse_args choices 扩展 + 新增 3 个参数；main() 传参 | 命令行参数层 | 完全兼容（旧参数不变，新参数有默认值） |
| T-05 | 修改 | GUI 标签页新增控件 + 命令拼接 | GUI 层 | 完全兼容（旧控件不变） |
| T-06 | 修改 | SCRIPT_VERSION + 文件头 | 版本标识 | 兼容 |
| T-07 | 验证 | 无代码变更 | — | — |

### 5.2 功能移除校验

本项目**不涉及任何功能移除**。需求明确要求"保留 ipmi 工具的功能以及 manual 手动断电这两个功能"，仅新增 enhanced 模式。因此无需功能移除校验。

### 5.3 通用原则

1. 所有代码变更不得破坏原有项目功能——ipmi 和 manual 模式的执行路径代码逐行保持不变。
2. 新增方法遵循现有代码风格：方法命名 snake_case、返回值使用 `Tuple[bool, Dict]` 模式、日志使用 `self.log.info/error`、异常捕获后返回错误字典而非抛出。
3. 不引入任何第三方 Python 依赖——所有功能通过标准库 + 外部命令行工具（nvme-cli、fio）实现。
4. 状态文件读写使用 `state.setdefault()` 兼容旧状态文件；enhanced 模式新增字段不影响 ipmi/manual 模式状态加载。
5. 修改 `run()` 时使用清晰的 `if self.cfg.pc_power_mode == "enhanced":` 分支隔离，不将 enhanced 逻辑混入 ipmi/manual 共用代码块。

---

## 6. 风险与校验项

| 风险编号 | 风险描述 | 风险等级 | 关联任务 | 校验方式 | 校验时机 |
|---------|---------|---------|---------|---------|---------|
| R-01 | `nvme disconnect` 命令在部分 NVMe 驱动器/内核版本上行为不一致，可能导致设备无法重新枚举 | 中 | T-02 | 代码中增加重试机制和超时保护；disconnect 失败时降级为 umount+sync 并记录 warning，不中断测试 | 开发中 |
| R-02 | fio `--verify=pattern` 参数在不同 fio 版本中语法有差异（fio 3.x vs 3.25+） | 中 | T-02 | 代码中先执行 `fio --version` 检测版本，根据版本选择 verify 参数写法；提供 fallback 到 `--verify=crc32c` | 开发中 |
| R-03 | enhanced 模式 Pattern 写入 20GB 数据耗时较长（取决于盘性能），可能导致单循环时间超出预期 | 低 | T-02, T-03 | Pattern 写入仅在 setup 阶段执行一次，循环中不重复写入；参数 `--pc-pattern-size-gb` 可配置；文档中说明预计耗时 | 开发中 |
| R-04 | PCIe Link 信息获取方式（`nvme get-phy` vs sysfs）在不同内核版本上可用性不同 | 中 | T-02 | 代码中优先使用 sysfs（`/sys/bus/pci/devices/<bdf>/current_link_width`），fallback 到 `nvme get-phy`；均不可用时跳过 Link 校验并记录 warning，不判 FAIL | 开发中 |
| R-05 | GUI 新增控件的 `config_vars` key 未在 `__init__` 中初始化导致 KeyError | 低 | T-05 | 全局搜索 `config_vars` 初始化位置，确认新增 key 已注册；T-07 静态自检覆盖 | 提测前 |
| R-06 | 三种模式共用状态文件可能导致状态混乱（如 ipmi 模式运行到一半切换为 enhanced） | 中 | T-03 | 状态文件中记录 `power_mode` 字段；加载时若 state 中的 mode 与当前 cfg 不一致，提示用户并要求删除旧状态文件或自动重置；文档中说明 | 开发中 |
| R-07 | `run()` 方法分支改造时意外修改 ipmi/manual 共用代码 | 高 | T-03 | T-07 中逐行 diff 比对 ipmi/manual 路径代码与 v1.8.5 原文；代码审查时重点检查 | 提测前 |
| R-08 | Python 3.10 与开发环境 Python 版本差异导致语法不兼容 | 低 | 全部 | T-07 中用 `python3 -m py_compile` 检查；避免使用 3.10+ 专属语法 | 提测前 |

### 关键校验清单

- [ ] `python3 -m py_compile ssd_test_v1.9.0.py` 无语法错误
- [ ] `python3 ssd_test_v1.9.0.py --help` 正常输出，新增参数可见
- [ ] `python3 ssd_test_v1.9.0.py --version` 输出 v1.9.0
- [ ] ipmi 模式 `run()` 路径代码与 v1.8.5 逐行一致（diff 仅新增分支）
- [ ] manual 模式 `run()` 路径代码与 v1.8.5 逐行一致
- [ ] TestConfig 所有新增字段有默认值，旧构造方式不报错
- [ ] GUI 启动无 KeyError，三个模式选项可见
- [ ] 无新增第三方 import
- [ ] 所有新增方法在类内有定义，无未定义名称引用

---

## 7. 交付物清单

| 交付物 | 类型 | 关联任务 | 交付标准 |
|-------|------|---------|---------|
| `ssd_test_v1.9.0.py` | 完整脚本代码 | T-01~T-07 | 可直接在 Ubuntu 22.04 + Python 3.10.12 运行；支持 ipmi/manual/enhanced 三种模式；通过 py_compile 语法检查；其他测试项功能不受影响 |
| `代码修改说明_v1.9.0.md` | 文档 | T-08 | 完整记录 v1.8.5→v1.9.0 所有变更点；包含三种模式对比表；包含兼容性说明和已知限制 |
| `环境配置与快速使用指南_v1.9.0.md` | 文档 | T-09 | 包含系统要求、依赖安装命令、三种模式快速上手示例、GUI 使用说明、参数详解、常见问题排查 |
| `项目执行计划_Normal_Power_Cycle_改造_v1.0.md` | 文档 | 本文档 | 项目执行计划，已交付 |

---

## 8. 回滚方案

### 8.1 回滚触发条件

- enhanced 模式在实际测试中出现不可修复的严重问题（如导致系统无法启动、数据丢失风险）
- 新增代码意外影响 ipmi/manual 模式或其他测试项功能，且短时间内无法定位修复
- 用户决定不采用 enhanced 模式，回退到 v1.8.5

### 8.2 回滚步骤

1. **代码回滚**：
   - 保留修改后的文件为 `ssd_test_v1.9.0.py.bak`
   - 将原始 `ssd_test_v1.8.5.py` 恢复为活跃版本（项目目录中原始文件未被覆盖，始终保留）
   - 若直接在原文件上修改，则通过文件备份恢复：`cp ssd_test_v1.8.5_backup.py ssd_test.py`
2. **状态文件清理**：
   - 删除 enhanced 模式产生的状态文件：`sudo rm -f /var/lib/ssd_power_cycle_state.json`（若其中包含 enhanced 模式数据）
3. **配置回滚**：无外部配置文件，所有配置通过命令行参数传入，回滚代码后自动恢复
4. **验证**：
   - `python3 ssd_test_v1.8.5.py --version` 确认版本为 1.8.5
   - `python3 ssd_test_v1.8.5.py --help` 确认参数列表为 v1.8.5 原始状态
   - 确认 `--pc-power-mode` choices 仅为 `["ipmi", "manual"]`

### 8.3 回滚责任人与时间预估

- 回滚操作人：用户或测试工程师
- 预估回滚耗时：5 分钟（文件替换 + 状态文件清理 + 验证）
- 数据丢失风险：无（回滚不涉及待测盘上的数据；状态文件为测试过程数据，删除不影响盘本身）

---

## 版本变更记录

| 版本 | 日期 | 变更内容 | 影响范围 | 操作人 |
|-----|------|---------|---------|-------|
| v1.0 | 2026-09-18 | 初始版本：基于完整代码探查（OKN 框架全部模块 + ssd_test_v1.8.5.py 全关键区域）生成执行计划 | 全部 | AI 生成 |
