# SPOR 意外电源循环测试三模式改造项目执行计划

> 版本：v1.0 | 生成日期：2026-09-18 | 状态：待确认

---

## 1. 项目概述

本项目对 `ssd_test_v1.9.0.py` 中的 **Surprise Power Cycle Test（意外电源循环测试，SPOR）** 模块进行三模式改造。当前 SPOR 仅支持 **Timeboard 硬件断电**（旧方案）一种方式，本次改造新增 **manual（手动意外断电）** 和 **enhanced（增强方案，融合 OKN 框架最佳实践）** 两种模式，用户可通过命令行参数 `--spor-power-mode` 或 GUI 上位机下拉框在 `timeboard` / `manual` / `enhanced` 三种模式间选择。

`enhanced` 模式的核心增强来自 OKN 企业级 SSD 测试框架 `spor_test.py`：
1. **IPMI 直接意外断电**——不经过 OS shutdown、不 sync，通过 `ipmitool chassis power off` 直接切断电源，实现真正自动化的意外断电（无需 Timeboard 专用硬件）
2. **更高写入压力**——iodepth=256（旧方案为 32），掉电瞬间更多 outstanding IO，更严格地验证 PLP（Power Loss Protection）
3. **线程级异步写入与进度追踪**——参考 OKN `MyThread` 模式，通过线程返回值精确追踪掉电时写入进度，替代 iolog 解析
4. **增强型 Pattern 校验**——fio `--verify=pattern --do_verify=1` 同时执行数据比对和 Pattern 验证

本电脑仅负责代码书写，不负责代码测试；所有修改须保证版本兼容（Python 3.10.12 / Ubuntu 22.04），且不干扰脚本中其他测试项功能。

---

## 2. 原始需求原文

> 以下为用户输入的原始需求，未经修改。

第 1 步：阅读我的所有代码（除了 ssd_test_v1.8.5.py和 ssd_test_v1.9.0.py 这个代码）；

第 2 步：阅读我的 ssd_test_v1.9.0.py (平台：Linux+Ubuntu2204+python3.10.12)；

第 3 步：将原本我的（Surprise Power Cycle Test 意外电源循环测试 检查固态硬盘在突然断电情况下的稳健性； SSD主机意外断电操作流程：主机在SSD读写过程中直接切断电源，不发送任何待机命令；SSD依赖于内置的PLP来备份FTL元数据，无需主机待命指令。）这个测试功能进行修改，保留原始测试工具的功能那个以及 manual 手动断电这两个功能，并添加你学习从第 1 步学习到的对于 Surprise Power Cycle Test 意外电源循环测试的测试方式并添加到我的ssd_test_v1.9.0.py中，并且我可以通过软件上位机选项进行选择是采用 旧方案,manual, 新方案（交付我 1 份修改好的完整脚本代码）；

第 4 步：交付 1 份代码修改的 md 文档，还有交付 1 份环境配置以及快速使用文档（以 md 文档交付）；

注意事项：1. 每次进行代码修改时注意版本兼容，并且要确保其他的代码功能不会被干扰；2. 本电脑只负责对于代码书写并不负责代码测试；根据我的 skill 交付我 1 份项目执行计划

### 需求冲突项

无冲突。三种模式为并列可选关系，不存在互斥矛盾。

---

## 3. 工程前置约束

### 3.1 技术栈与环境

| 项目 | 约束 |
|------|------|
| 操作系统 | Linux Ubuntu 22.04 LTS |
| Python 版本 | 3.10.12（仅标准库 + tkinter，不引入第三方依赖） |
| 目标脚本 | `ssd_test_v1.9.0.py`（当前版本，约 7400 行） |
| 外部依赖工具 | nvme-cli, smartmontools, fio, util-linux, ipmitool, e2fsprogs, parted |
| 代码基线 | ssd_test_v1.9.0.py（已包含 Normal Power Cycle 的 enhanced 模式改造） |
| 开发环境 | Windows 本机仅书写代码，不在本机执行测试 |

### 3.2 选定方案与备选

| 需求项 | 选定方案 | 备选方案 | 选择理由 |
|-------|---------|---------|---------|
| 三模式命名 | `timeboard` / `manual` / `enhanced` | `old` / `manual` / `new`；`hardware` / `manual` / `ipmi` | `timeboard` 明确指代旧方案的硬件；`enhanced` 与 Normal Power Cycle 的增强模式命名一致，用户体验统一 |
| enhanced 断电方式 | IPMI `chassis power off` 直接断电（不 shutdown、不 sync） | 系统请求 + 人工断电；PDU 控制 | 参考 OKN `power_down(safe_shutdown=False)` 直接 `link_state.power_off()`，IPMI 是最通用的自动化断电方式，无需专用硬件 |
| enhanced 写入压力 | iodepth=256, numjobs=1, bs=128k | iodepth=32, numjobs=4（旧方案） | 参考 OKN spor_test 使用 iodepth=256，更高 QD 意味着掉电瞬间更多 outstanding IO，更严格验证 PLP |
| enhanced 写入进度追踪 | 线程级追踪（threading.Thread + 共享进度变量） | fio write_iolog 解析（旧方案） | 参考 OKN `MyThread` 模式，线程返回值更精确可靠，不依赖日志文件可能在掉电时损坏 |
| enhanced 验证方式 | fio `--verify=pattern --do_verify=1` 逐块校验 | 旧方案的 verify_pattern（基于 fio read + 输出解析） | 参考 OKN `verify_read(do_data_compare=True, verify_pattern=True)`，fio 内置 verify 更严格，直接返回 mismatch 计数 |
| 模式选择入口 | 新增 `--spor-power-mode` 参数，choices=["timeboard","manual","enhanced"] | 复用现有参数 + 隐式判断 | 新增独立参数语义清晰，与 Normal Power Cycle 的 `--pc-power-mode` 设计一致 |

### 3.3 其他约束

- **向后兼容**：`--spor-power-mode` 默认值为 `timeboard`；不传该参数时行为与 v1.9.0 完全一致。
- **不干扰其他测试项**：仅修改 `SPORTester` 类、`TestConfig` 中 spor 相关字段、`parse_args()` 中 spor 参数、GUI `_build_spor_tab()` 和 `_build_cmd()` 中 spor 参数拼接；不触碰其他测试类。
- **timeboard 模式代码不变**：旧方案的 Timeboard 断电逻辑、iolog 解析、验证逻辑逐行保持原样，仅通过 if/elif/else 分支隔离。
- **状态文件兼容**：新增字段使用 `state.setdefault()` 读取；不同模式建议使用不同 `--spor-state-file` 路径避免混淆。
- **Python 3.10 兼容**：不使用 3.10+ 专属语法；threading 用法为标准库。

---

## 4. 任务拆解

### 4.1 任务总览

| 子任务编号 | 子任务名称 | 优先级 | 依赖 | 涉及模块/文件 |
|-----------|-----------|-------|------|-------------|
| T-01 | 常量与配置层扩展：新增 spor_power_mode 常量、TestConfig 字段、enhanced 专属参数 | P0 | 无 | 常量区、TestConfig |
| T-02 | SPORTester 新增 enhanced 模式核心方法：IPMI 意外断电、高 QD 线程写入、增强校验 | P0 | T-01 | SPORTester 类 |
| T-03 | SPORTester phase1/phase2/run() 三模式分支改造 | P0 | T-02 | SPORTester.phase1_poweroff / phase2_poweron / run |
| T-04 | 命令行参数层扩展：新增 --spor-power-mode 及 enhanced 专属参数、main() 传参 | P0 | T-01 | parse_args()、main() |
| T-05 | GUI 上位机层扩展：Combobox 三选项、enhanced 专属控件、命令拼接 | P1 | T-04 | _build_spor_tab()、_build_cmd() |
| T-06 | 版本号升级与文件头更新 | P1 | T-03 | SCRIPT_VERSION、文件头 |
| T-07 | 代码静态自检：语法编译、import 检查、timeboard 路径回归比对 | P0 | T-01~T-06 | 全文件 |
| T-08 | 交付代码修改说明文档（MD） | P1 | T-07 | 新建文档 |
| T-09 | 交付环境配置与快速使用文档（MD） | P1 | T-07 | 新建文档 |

### 4.2 子任务详情

#### T-01：常量与配置层扩展

- **优先级**：P0
- **依赖**：无
- **目标**：新增 SPOR 三模式所需的常量和 TestConfig 字段。
- **操作步骤**：
  1. 常量区新增：
     - `DEFAULT_SPOR_POWER_MODE = "timeboard"`（默认旧方案）
     - `DEFAULT_SPOR_ENHANCED_IODEPTH = 256`（enhanced 模式写入队列深度）
     - `DEFAULT_SPOR_ENHANCED_BS = "128k"`（enhanced 模式写入块大小）
  2. `TestConfig` 类 spor 配置区新增字段：
     - `spor_power_mode: str = DEFAULT_SPOR_POWER_MODE`（timeboard / manual / enhanced）
     - `spor_enhanced_iodepth: int = DEFAULT_SPOR_ENHANCED_IODEPTH`
     - `spor_enhanced_bs: str = DEFAULT_SPOR_ENHANCED_BS`
  3. 确认 `spor_ipmi_host` 可复用现有 `ipmi_host` 字段（enhanced 模式断电需要 IPMI 地址）。
- **涉及文件/模块**：常量定义区、`TestConfig` dataclass
- **验收标准**：`TestConfig` 实例化不报错；新增字段有合理默认值。

#### T-02：SPORTester 新增 enhanced 模式核心方法

- **优先级**：P0
- **依赖**：T-01
- **目标**：在 `SPORTester` 类中新增 enhanced 模式所需的独立方法，与现有 timeboard 方法并存。
- **操作步骤**：
  1. **新增 `enhanced_ipmi_surprise_poweroff()` 方法**（参考 OKN `power_down(safe_shutdown=False)`）：
     - 不执行 sync、不执行 shutdown
     - 启动后台 nohup 脚本：等待指定秒数 → `ipmitool chassis power off` → 延时 → `ipmitool chassis power on`
     - 保存状态文件（标记为 PHASE_POWERON）
     - 返回 (success: bool, message: str)
     - 关键：断电前不 sync，保证真正"意外"
  2. **新增 `start_enhanced_write_thread()` 方法**（参考 OKN `MyThread` 异步写入）：
     - 使用 `threading.Thread` 启动 fio 顺序写（iodepth=256, bs=128k, buffer_pattern=0x22）
     - 通过共享变量 `self._enhanced_write_progress` 追踪已写入字节数（定期读取 fio 输出或 /proc/<pid>/io）
     - 线程对象保存为 `self._enhanced_write_thread`
     - 返回 (success: bool)
  3. **新增 `get_enhanced_write_position()` 方法**：
     - 从共享进度变量获取掉电时已写入字节数
     - 转换为 LBA（除以 lba_size）
     - 若进度变量不可用，fallback 到 iolog 解析（复用现有 `get_write_position()`）
     - 返回 last_lba: int
  4. **新增 `enhanced_verify_pattern()` 方法**（参考 OKN `verify_read(do_data_compare=True, verify_pattern=True)`）：
     - 使用 fio `--rw=read --verify=pattern --verify_pattern=<pat> --do_verify=1` 校验读
     - 解析 fio JSON 输出获取 verify_errors 计数
     - 支持 offset 和 size 参数（用于分段校验已写入/未写入区域）
     - 返回 (all_ok: bool, mismatch_count: int)
  5. **新增 `manual_surprise_poweroff()` 方法**：
     - 保存状态文件（标记为 PHASE_POWERON）
     - 打印提示信息："请在 10 秒内手动切断电源... 断电后等待 10 秒再上电"
     - 不执行任何断电操作（等待用户手动断电）
     - 返回 (success: bool, message: str)
- **涉及文件/模块**：`SPORTester` 类内部新增方法
- **验收标准**：所有新增方法独立可调用；不修改现有 `phase1_poweroff`、`phase2_poweron`、`start_spor_write`、`get_write_position`、`verify_pattern` 等方法签名和内部逻辑。

#### T-03：SPORTester phase1/phase2/run() 三模式分支改造

- **优先级**：P0
- **依赖**：T-02
- **目标**：在 phase1（掉电前）、phase2（上电后）、run() 中根据 `spor_power_mode` 分发到三种模式。
- **操作步骤**：
  1. **`__init__` 扩展**：新增 `self.spor_power_mode = getattr(config, 'spor_power_mode', 'timeboard')` 等 enhanced 参数读取。
  2. **`run()` 方法**：
     - dry_run 日志中增加模式信息
     - Timeboard 初始化仅在 timeboard 模式下执行（enhanced/manual 模式不需要 Timeboard）
  3. **`phase1_poweroff()` 方法**——断电方式分支：
     - Step 1-4（pattern11 打底、验证、启动写入、等待）三种模式共用
     - **写入启动方式分支**：
       - timeboard 模式：使用现有 `start_spor_write()`（iodepth=32, numjobs=4, iolog）
       - manual 模式：复用 `start_spor_write()`（与 timeboard 相同写入方式）
       - enhanced 模式：使用新增 `start_enhanced_write_thread()`（iodepth=256, 线程追踪）
     - **Step 5 断电方式分支**（替换现有 Timeboard 断电逻辑）：
       - timeboard 模式：现有 Timeboard `trigger_poweroff()` 逻辑（保持不变）
       - manual 模式：调用 `manual_surprise_poweroff()`
       - enhanced 模式：调用 `enhanced_ipmi_surprise_poweroff()`
  4. **`phase2_poweron()` 方法**——验证方式分支：
     - Step 1-3（设备检测、PCIe、SMART）三种模式共用
     - **Step 4 写入位置解析分支**：
       - timeboard/manual 模式：现有 `get_write_position()`（iolog 解析）
       - enhanced 模式：新增 `get_enhanced_write_position()`（线程进度，fallback 到 iolog）
     - **Step 5-6 Pattern 验证分支**：
       - timeboard/manual 模式：现有 `_verify_written_area()` / `_verify_unwritten_area()`（基于 `verify_pattern`）
       - enhanced 模式：使用新增 `enhanced_verify_pattern()`（fio 内置 verify=pattern）
     - 结果记录结构保持兼容（cycle_success、verify_22、verify_11、smart_ok）
- **涉及文件/模块**：`SPORTester.__init__`、`phase1_poweroff()`、`phase2_poweron()`、`run()`
- **验收标准**：
  - `spor_power_mode="timeboard"` 时，执行路径与 v1.9.0 原逻辑逐行一致；
  - `spor_power_mode="manual"` 时，写入逻辑同 timeboard，断电走手动提示；
  - `spor_power_mode="enhanced"` 时，走新增的高 QD 线程写入 + IPMI 意外断电 + fio verify 校验；
  - 三种模式共享状态文件管理、最终功能测试、结果统计等基础设施。

#### T-04：命令行参数层扩展

- **优先级**：P0
- **依赖**：T-01
- **目标**：新增 `--spor-power-mode` 参数及 enhanced 专属参数，更新 main() 传参。
- **操作步骤**：
  1. `parse_args()` 中 SPOR 参数区新增：
     - `--spor-power-mode`：default="timeboard", choices=["timeboard","manual","enhanced"], help="SPOR 断电方式: timeboard(Timeboard硬件)/manual(手动断电)/enhanced(IPMI意外断电+高QD+增强校验) (默认: timeboard)"
     - `--spor-enhanced-iodepth`：type=int, default=256, help="enhanced 模式写入队列深度 (默认: 256)"
     - `--spor-enhanced-bs`：default="128k", help="enhanced 模式写入块大小 (默认: 128k)"
  2. `main()` 中 `TestConfig(...)` 构造新增参数传递：
     - `spor_power_mode=args.spor_power_mode`
     - `spor_enhanced_iodepth=args.spor_enhanced_iodepth`
     - `spor_enhanced_bs=args.spor_enhanced_bs`
  3. `main()` 中 SPOR 配置日志输出增强：enhanced 模式时额外打印 iodepth、bs、IPMI 地址。
  4. `parse_args()` epilog 新增 enhanced 模式示例。
- **涉及文件/模块**：`parse_args()`、`main()`
- **验收标准**：`--help` 正常输出，新增参数可见；`--spor-power-mode enhanced` 可被接受；不传新参数时使用默认值 timeboard。

#### T-05：GUI 上位机层扩展

- **优先级**：P1
- **依赖**：T-04
- **目标**：在 GUI"意外电源循环(SPOR)"标签页中新增模式选择和 enhanced 专属控件。
- **操作步骤**：
  1. `_build_spor_tab()` 中：
     - 新增"断电方式"标签 + Combobox（values=["timeboard","manual","enhanced"], default="timeboard"）
     - 新增 enhanced 专属控件（默认禁用，选择 enhanced 时启用）：
       - "写入队列深度" Spinbox（default=256, range=1-1024）
       - "写入块大小" Entry（default="128k"）
     - 为 Combobox 绑定 `<<ComboboxSelected>>` 事件，切换时启用/禁用 enhanced 控件和 Timeboard 串口控件（manual/enhanced 模式下 Timeboard 串口控件灰显）
  2. `_build_cmd()` 中 SPOR 参数拼接新增：
     - `--spor-power-mode` 传递
     - enhanced 模式时传递 `--spor-enhanced-iodepth`、`--spor-enhanced-bs`
- **涉及文件/模块**：`_build_spor_tab()`、`_build_cmd()`
- **验收标准**：GUI 正常启动无报错；SPOR 标签页可见三个模式选项；选择 enhanced 时新控件可用、Timeboard 串口控件灰显；生成命令行包含 enhanced 专属参数。

#### T-06：版本号升级与文件头更新

- **优先级**：P1
- **依赖**：T-03
- **目标**：升级脚本版本号，更新文件头说明。
- **操作步骤**：
  1. `SCRIPT_VERSION` 从 `"1.9.0"` 升级为 `"1.9.1"`（修订号升级，表示 SPOR 功能增强而非大版本变更）
  2. 文件头 SPOR 测试项描述更新为"支持 timeboard/manual/enhanced 三种断电方式"
  3. 文件头用法示例新增 SPOR enhanced 模式命令
- **涉及文件/模块**：文件头、SCRIPT_VERSION 常量
- **验收标准**：`--version` 输出 `ssd_test.py v1.9.1`。

#### T-07：代码静态自检

- **优先级**：P0
- **依赖**：T-01~T-06
- **目标**：通过静态检查确保代码语法正确、无明显逻辑错误、timeboard 路径未被意外修改。
- **操作步骤**：
  1. `python3 -m py_compile ssd_test_v1.9.1.py` 语法检查
  2. `python3 ssd_test_v1.9.1.py --help` 验证参数输出
  3. `python3 ssd_test_v1.9.1.py --version` 验证版本号
  4. 全局搜索新增方法名，确认所有引用处均有定义
  5. 逐行比对 timeboard 模式路径代码与 v1.9.0 原文（diff 仅新增分支）
  6. 确认无新增第三方 import
  7. dataclass 字段检查：确认新增字段均有默认值
- **涉及文件/模块**：全文件
- **验收标准**：`py_compile` 无报错；timeboard 模式代码与 v1.9.0 逐行一致（除注释外）；无未定义名称。

#### T-08：交付代码修改说明文档（MD）

- **优先级**：P1
- **依赖**：T-07
- **目标**：编写详细的代码修改说明文档。
- **操作步骤**：
  1. 文档结构：版本信息、修改概述、三种模式对比表、逐区域修改清单、新增方法说明、兼容性说明、已知限制
  2. 三种模式对比表覆盖：断电方式、写入压力、写入进度追踪、验证方式、自动化程度、硬件依赖
- **涉及文件/模块**：新建 `代码修改说明_SPOR_v1.9.1.md`
- **验收标准**：文档内容完整，覆盖所有代码变更。

#### T-09：交付环境配置与快速使用文档（MD）

- **优先级**：P1
- **依赖**：T-07
- **目标**：编写环境配置和快速使用指南，重点说明 SPOR 三种模式的使用方法。
- **操作步骤**：
  1. 文档结构：系统要求、依赖安装、三种模式快速上手命令、GUI 操作、enhanced 模式参数详解、状态文件说明、常见问题排查（Timeboard 连接失败、IPMI 断电失败、Pattern 校验失败等）、快速命令速查表
- **涉及文件/模块**：新建 `环境配置与快速使用指南_SPOR_v1.9.1.md`
- **验收标准**：文档可指导用户从零完成环境搭建到运行三种模式的 SPOR 测试。

---

## 5. 代码变更规则

### 5.1 变更总览

| 子任务 | 变更类型 | 变更范围 | 影响面 | 兼容性 |
|-------|---------|---------|-------|-------|
| T-01 | 新增 | 常量区新增 3 个常量；TestConfig 新增 3 个字段 | 仅 SPORTester 读取新字段 | 完全兼容 |
| T-02 | 新增 | SPORTester 新增 5 个方法 | 仅 enhanced/manual 模式调用 | 完全兼容（不修改现有方法） |
| T-03 | 修改 | SPORTester phase1/phase2/run/__init__ 新增 if/elif 分支 | timeboard 走原逻辑；manual/enhanced 走新逻辑 | 完全兼容（原逻辑代码不变） |
| T-04 | 修改 | parse_args 新增 3 个参数；main() 传参 | 命令行参数层 | 完全兼容 |
| T-05 | 修改 | GUI 标签页新增控件 + 命令拼接 | GUI 层 | 完全兼容 |
| T-06 | 修改 | SCRIPT_VERSION + 文件头 | 版本标识 | 兼容 |
| T-07 | 验证 | 无代码变更 | — | — |

### 5.2 功能移除校验

本项目**不涉及任何功能移除**。需求明确要求"保留原始测试工具的功能（timeboard）以及 manual 手动断电这两个功能"，仅新增 enhanced 模式。因此无需功能移除校验。

### 5.3 通用原则

1. 所有代码变更不得破坏原有项目功能——timeboard 模式的执行代码逐行保持原样。
2. 新增方法遵循现有代码风格：方法命名 snake_case、返回值使用 `Tuple[bool, ...]` 模式、日志使用 `self.log.info/error`、异常捕获后返回错误字典。
3. 不引入任何第三方 Python 依赖——threading、subprocess、json、time 均为标准库。
4. 状态文件读写使用 `state.setdefault()` 兼容旧状态文件。
5. 修改 phase1/phase2 时使用清晰的 `if self.spor_power_mode == "timeboard"` / `elif == "manual"` / `else (enhanced)` 分支隔离，不将 enhanced 逻辑混入 timeboard 共用代码块。
6. enhanced 模式的 IPMI 断电脚本复用 Normal Power Cycle 中已验证的 nohup 后台脚本模式（等待→断电→延时→上电），但**关键区别**是不执行 `shutdown -h now`（SPOR 是意外断电，不经过 OS 关机）。

---

## 6. 风险与校验项

| 风险编号 | 风险描述 | 风险等级 | 关联任务 | 校验方式 | 校验时机 |
|---------|---------|---------|---------|---------|---------|
| R-01 | enhanced 模式 IPMI 直接断电（不 shutdown）可能导致测试机文件系统损坏（系统盘而非待测盘） | 高 | T-02, T-03 | enhanced 模式的状态文件必须存放在系统盘以外或使用原子写入；文档中明确警告 IPMI 断电会影响系统盘，建议使用独立测试机或系统盘为只读/临时文件系统；断电前确保待测盘的写入不涉及系统盘 | 开发中 |
| R-02 | threading.Thread 启动 fio 子进程后，掉电时线程和 fio 进程被强制终止，进度变量可能未及时更新 | 中 | T-02 | 进度变量定期（每 1 秒）从 fio 子进程的 /proc/<pid>/io 读取 write_bytes，而非依赖线程回调；掉电后 fallback 到 iolog 解析 | 开发中 |
| R-03 | fio `--verify=pattern` 在不同版本中参数语法有差异 | 中 | T-02 | 代码中先检测 fio 版本，根据版本选择参数写法；提供 fallback 到旧方案的 verify_pattern | 开发中 |
| R-04 | manual 模式下用户未在提示时间内断电，导致 fio 写入完成后才断电，失去"意外"效果 | 低 | T-02 | manual 模式打印明确的倒计时提示；文档中说明操作要点；可配置 `--spor-delay` 控制等待时间 | 开发中 |
| R-05 | GUI 新增控件的 config_vars key 未在 __init__ 中初始化导致 KeyError | 低 | T-05 | 全局搜索 config_vars 初始化位置，确认新增 key 已注册；T-07 静态自检覆盖 | 提测前 |
| R-06 | 三种模式共用状态文件可能导致状态混乱 | 中 | T-03 | 状态文件中记录 power_mode 字段；加载时若 mode 不一致提示用户删除旧状态文件；文档中说明建议不同模式使用不同 --spor-state-file | 开发中 |
| R-07 | phase1/phase2 分支改造时意外修改 timeboard 共用代码 | 高 | T-03 | T-07 中逐行 diff 比对 timeboard 路径代码与 v1.9.0 原文 | 提测前 |
| R-08 | enhanced 模式 iodepth=256 可能导致部分低端 SSD 或控制器超时 | 低 | T-02 | iodepth 可通过 --spor-enhanced-iodepth 配置；文档中说明可根据盘性能调整 | 开发中 |

### 关键校验清单

- [ ] `python3 -m py_compile ssd_test_v1.9.1.py` 无语法错误
- [ ] `python3 ssd_test_v1.9.1.py --help` 正常输出，新增参数可见
- [ ] `python3 ssd_test_v1.9.1.py --version` 输出 v1.9.1
- [ ] timeboard 模式 phase1/phase2 路径代码与 v1.9.0 逐行一致（diff 仅新增分支）
- [ ] TestConfig 所有新增字段有默认值
- [ ] GUI 启动无 KeyError，三个模式选项可见
- [ ] 无新增第三方 import
- [ ] 所有新增方法在类内有定义，无未定义名称引用
- [ ] enhanced 模式 IPMI 断电脚本不包含 shutdown 命令（意外断电关键约束）

---

## 7. 交付物清单

| 交付物 | 类型 | 关联任务 | 交付标准 |
|-------|------|---------|---------|
| `ssd_test_v1.9.1.py` | 完整脚本代码 | T-01~T-07 | 可直接在 Ubuntu 22.04 + Python 3.10.12 运行；支持 timeboard/manual/enhanced 三种 SPOR 断电方式；通过 py_compile；其他测试项功能不受影响 |
| `代码修改说明_SPOR_v1.9.1.md` | 文档 | T-08 | 完整记录 v1.9.0→v1.9.1 所有 SPOR 相关变更；包含三种模式对比表；兼容性说明 |
| `环境配置与快速使用指南_SPOR_v1.9.1.md` | 文档 | T-09 | 包含系统要求、依赖安装、三种模式快速上手示例、GUI 使用说明、参数详解、常见问题排查 |
| `项目执行计划_SPOR_三模式改造_v1.0.md` | 文档 | 本文档 | 项目执行计划 |

---

## 8. 回滚方案

### 8.1 回滚触发条件

- enhanced 模式在实际测试中导致系统盘文件系统损坏或测试机无法启动
- 新增代码意外影响 timeboard 模式或其他测试项功能，且短时间内无法定位修复
- 用户决定不采用三模式改造，回退到 v1.9.0

### 8.2 回滚步骤

1. **代码回滚**：
   - 项目目录中 `ssd_test_v1.9.0.py` 始终保留未改动，直接恢复为活跃版本
   - 若直接在 v1.9.1 文件上修改，通过备份恢复：`cp ssd_test_v1.9.0.py ssd_test.py`
2. **状态文件清理**：
   - 删除 enhanced/manual 模式产生的状态文件：`sudo rm -f /var/lib/ssd_spor_state.json`
3. **验证**：
   - `python3 ssd_test_v1.9.0.py --version` 确认版本为 1.9.0
   - 确认 `--spor-power-mode` 参数不存在（v1.9.0 无此参数）

### 8.3 回滚责任人与时间预估

- 回滚操作人：用户或测试工程师
- 预估回滚耗时：5 分钟
- 数据丢失风险：无（回滚不涉及待测盘上的数据；状态文件为测试过程数据）

---

## 版本变更记录

| 版本 | 日期 | 变更内容 | 影响范围 | 操作人 |
|-----|------|---------|---------|-------|
| v1.0 | 2026-09-18 | 初始版本：基于完整代码探查（OKN 框架 spor_test.py + ssd_test_v1.9.0.py SPOR 模块全区域）生成执行计划 | 全部 | AI 生成 |
