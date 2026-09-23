# SSD 测试工具 FOB/稳态改造与上位机状态控制 执行计划

> 版本：v1.5 | 生成日期：2026-09-23 | 状态：待确认

---

## 1. 项目概述

本项目对 `ssd_test_v1.9.1.py` 进行定向改造：将性能测试模块中的 FOB 状态恢复方式从 `blkdiscard` 替换为符合 SNIA SSS PTS v2.0.2 规范的 NVMe User Data Erase（`nvme format --namespace-id=1 --ses=1`），并将稳态预处理从"固定写入量经验式"改造为"WIPC + WDPC 多轮循环 + 5轮滑动窗口稳态检测算法"的规范流程。同时在 tkinter 上位机中新增 SSD 当前状态实时显示和手动状态切换按键，使测试操作员可直观查看并控制设备状态。最终交付修改后的代码（输出路径 `~/max_tool/ssd_test_v1.9.2.py`）、风险说明文档和上位机快速使用指南。

---

## 2. 原始需求原文

> 以下为用户输入的原始需求，未经修改。

第 1 步：我使用的平台是 Linux+2204ubuntu，使用的语言是 python3.10.12；

第 2 步：阅读我的 ssd_test_v1.9.1.py 代码然后将 ssd_test_v1.9.1.py 代码中性能测试项目部分代码（SSD 进入状态 FOB 和 SSD 进入状态 steady）进行修改与替换，擦除方式采用 User Data Erase（命令：sudo nvme format /dev/nvme0n1 --namespace-id=1 --ses=1）（结合 SNIA SSS PTS v2.0.2 规范以及 SSD_FOB与稳态进入流程指南.md 完成代码修改）；

第 3 步：我要在软件上位机上看到目前我的 SSD 处于什么状态（FOB/steady），同时在软件上位机添加按键选项（可以手动控制 SSD 状态，比如我选择 FOB，点击按键，则 SSD 进入 FOB 状态），如果我在软件上位机选择此测试项目并点击开始测试则先进入我选择的 SSD 状态再进行对应的性能测试（日志输出以及 FOB/steady 状态进入后性能测试方面代码无需修改）；

第 5 步：说明修改代码执行的风险（以 md 文档交付）；

第 6 步：执行完前面步骤后交付我一份软件上位机就快速使用+环境配置 md 文档；

第 7 步（追加需求）：1.脚本启动时，统一默认为Unknown状态，此时如果我点击性能测试并开始测试，则直接进行性能测试，同时不会进入任何状态；2.如果我已经点击按钮让SSD进入FOB状态或者SSD进入steady状态，并选择性能测试后点击开始测试，先查看我目前的SSD状态以及性能测试中的选择状态是否一致，如果SSD状态和目标状态一致则直接进行性能测试，无需重复进入该状态，如果不一致在软件上位机上报错，并不进行后续测试；3.注意性能测试中进行SMART健康检查时忽视介质错误5353；

注意事项：
1. 执行代码修改时注意版本兼容问题；
2. 执行代码修改时注意修改是否会导致我的其他代码无法正常使用；
3. 本电脑不进行代码调试。

### 需求冲突项

| 冲突编号 | 涉及需求 | 冲突描述 | 待用户确认 |
|---------|---------|---------|-----------|
| C-01 | 第3步 vs 第7步 | 第3步原文要求"未手动进入状态时（Unknown），点击开始测试则先进入选择的SSD状态再进行性能测试"；第7步追加需求将Unknown状态行为修改为"直接测试不进入状态"，并新增已手动进入状态后的一致性校验逻辑。第7步中"不一致则报错停止"经用户进一步澄清，修改为"弹出警告对话框，用户可选择直接进入目标状态再测试，或取消后手动控制状态"。 | 以第7步追加需求及用户澄清为准：Unknown直接测试；状态一致直接测试；状态不一致弹出警告供用户选择（直接进入目标状态/取消手动控制）。 |

需求第 3 步明确"日志输出以及 FOB/steady 状态进入后性能测试方面代码无需修改"，与第 2 步的"修改进入状态代码"边界清晰：仅修改状态进入逻辑和开始测试时的状态校验逻辑，不修改状态进入后的性能测试执行和日志输出逻辑。第7步追加需求及用户澄清进一步明确了开始测试时的三分支状态校验逻辑，取代第3步中"Unknown时先进入状态"的原始描述。

---

## 3. 工程前置约束

### 3.1 技术栈与环境

- 语言/框架：Python 3.10.12（标准库 tkinter，无第三方 GUI 依赖）
- 运行环境：Linux Ubuntu 22.04 LTS，x86_64
- 关键系统依赖：`nvme-cli`（≥1.16，Ubuntu 22.04 默认源版本）、`fio`（≥3.0）、`blkdiscard`（util-linux）
- Python 第三方依赖：`numpy`（稳态检测线性回归计算）、现有代码已依赖的其他包保持不变
- 代码基线：`ssd_test_v1.9.1.py`（单文件，约 7800 行，包含 9 个测试类 + GUI）
- 交付脚本路径：`~/max_tool/ssd_test_v1.9.2.py`（用户指定的 Linux 环境目标路径）
- 参考规范：SNIA SSS PTS v2.0.2、`SSD_FOB与稳态进入流程指南.md`

### 3.2 选定方案与备选

| 需求项 | 选定方案 | 备选方案 | 选择理由 |
|-------|---------|---------|---------|
| FOB 擦除方式 | NVMe Format `--ses 1`（User Data Erase），命令为 `sudo nvme format /dev/nvme0n1 --namespace-id=1 --ses=1`，失败自动回退 `blkdiscard` | 方案 B：仅用 blkdiscard（保留原有行为作为兜底） | 用户明确指定 `--ses=1`；User Data Erase 兼容性好，消费级和企业级 NVMe SSD 均支持；blkdiscard 作为兜底保留原有行为 |
| 稳态进入方式 | WIPC（128K顺序写2X容量）+ WDPC（多轮56点矩阵循环）+ 5轮滑动窗口稳态检测（Range≤20% + Slope≤10%，3跟踪变量同时判定） | 方案 B：保留现有固定写入量方案，仅增加稳态验证步骤；方案 C：WSAT 式连续写至稳态 | 规范要求严格的 WDPC 循环+检测算法；方案 B 无法证明达到稳态；方案 C 仅适用于写饱和场景 |
| 状态持久化方式 | JSON 状态文件（`~/.ssd_test_state.json`），记录设备序列号+当前状态+时间戳 | 方案 B：内存变量（GUI 关闭即丢失）；方案 C：写入设备 SMART 日志 | 状态文件可跨 GUI 会话持久化，且不依赖设备厂商特性；内存变量在 GUI 重启后丢失 |
| 手动状态切换实现 | GUI 按钮触发独立子进程执行状态进入脚本（`--action enter-fob` / `--action enter-steady`），完成后更新状态文件 | 方案 B：GUI 直接调用 PerformanceTester 类方法 | 现有 GUI 架构通过 subprocess 调用命令行，保持架构一致性；避免 GUI 线程阻塞 |
| 稳态检测算法 | numpy polyfit 一元线性回归 + 极差计算 | 方案 B：纯 Python 手写最小二乘法 | numpy 已在数据处理场景常用，计算准确且代码简洁；纯 Python 易引入浮点误差 |

### 3.3 其他约束

- **向后兼容硬约束**：所有命令行参数（`--perf-state`、`--precondition`、`--no-precondition` 等）必须保持原有行为不变，新增参数均为可选且有默认值。
- **非侵入硬约束**：不得修改 FirmwareTester、SmartTester、CapacityTester、ReadWriteTester、PowerCycleTester、SPORTester、OSInterruptionTester 等其他 7 个测试类的任何代码。
- **GUI 架构约束**：GUI 通过 subprocess 调用命令行模式执行测试，新增的手动状态切换也遵循此架构，不直接在 GUI 线程中执行耗时操作。
- **不调试约束**：本电脑（Windows）不进行代码调试，所有代码修改基于静态分析和规范对照，交付后由用户在 Linux 环境验证。
- **版本号约束**：修改后版本号升级为 v1.9.2，与项目目录 V1.9.2 对应。

---

## 4. 任务拆解

### 4.1 任务总览

| 子任务编号 | 子任务名称 | 优先级 | 依赖 | 涉及模块/文件 |
|-----------|-----------|-------|------|-------------|
| T-01 | 新增 SSD 状态管理模块（状态常量、状态文件读写、状态追踪） | P0 | 无 | ssd_test_v1.9.1.py（新增函数，约 80 行） |
| T-02 | 改造 FOB 状态进入：blkdiscard → NVMe User Data Erase (--ses=1) | P0 | T-01 | PerformanceTester.run_fob_test() |
| T-03 | 新增稳态检测算法工具函数（Range≤20% + Slope≤10% + 3跟踪变量） | P0 | T-01 | ssd_test_v1.9.1.py（新增静态方法，约 120 行） |
| T-04 | 改造稳态预处理：固定写入量 → WIPC + WDPC 多轮循环 + 稳态检测 | P0 | T-03 | PerformanceTester.precondition_steady_state()、新增 _run_wdpc_loop() |
| T-05 | 新增命令行参数：--action（enter-fob / enter-steady / status）、--purge-method、--steady-max-rounds | P1 | T-01, T-02, T-04 | parse_args()、main() |
| T-06 | GUI 新增 SSD 状态显示面板（当前状态标签 + 设备序列号 + 最后更新时间） | P1 | T-01 | SSDTestGUI._build_performance_tab() |
| T-07 | GUI 新增手动状态切换按键（进入 FOB / 进入稳态 / 刷新状态） | P1 | T-05, T-06 | SSDTestGUI 新增方法、build_command() 扩展 |
| T-08 | GUI 开始测试逻辑调整：Unknown直接测试/状态一致直接测试/不一致警告弹窗供用户选择 | P1 | T-07 | SSDTestGUI.start_test() |
| T-09 | 编写代码修改执行风险分析文档（md） | P1 | T-01 ~ T-08, T-11, T-12（所有代码修改完成后） | 新增独立 md 文件 |
| T-10 | 编写上位机快速使用 + 环境配置文档（md） | P2 | T-01 ~ T-08, T-11, T-12（所有代码修改完成后） | 新增独立 md 文件 |
| T-11 | 全局静态校验：语法检查、导入检查、其他测试类回归检查、版本号升级 | P0 | T-01 ~ T-08, T-12 | ssd_test_v1.9.1.py 全文件 |
| T-12 | 性能测试中SMART健康检查忽视介质错误5353 | P1 | 无 | PerformanceTester 中 SMART 检查逻辑 |

### 4.2 子任务详情

#### T-01：新增 SSD 状态管理模块

- **优先级**：P0
- **依赖**：无
- **目标**：建立统一的 SSD 状态追踪机制，支持 FOB / STEADY / UNKNOWN 三种状态，状态持久化到 JSON 文件，按设备序列号隔离。
- **操作步骤**：
  1. 在文件顶部常量区新增状态常量：`SSD_STATE_UNKNOWN = "unknown"`、`SSD_STATE_FOB = "fob"`、`SSD_STATE_STEADY = "steady"`
  2. 新增 `_get_state_file_path()` 函数：返回 `~/.ssd_test_state.json` 的绝对路径
  3. 新增 `load_ssd_state(serial: str) -> dict` 函数：从状态文件读取指定序列号设备的状态，不存在则返回 `{"state": "unknown", "serial": serial, "timestamp": None}`
  4. 新增 `save_ssd_state(serial: str, state: str, extra: dict = None)` 函数：写入状态文件，包含 state、serial、timestamp、extra（如 purge_method、steady_rounds 等）
  5. 新增 `get_device_serial(device: str) -> str` 函数：通过 `nvme id-ctrl` 或 `lsblk` 获取设备序列号，NVMe 优先
- **涉及文件/模块**：ssd_test_v1.9.1.py 工具函数区（约 line 150-360 之间插入）
- **验收标准**：状态文件读写功能完整；不同序列号设备状态互不干扰；状态文件不存在时自动创建；序列号获取失败时返回 "unknown-serial" 而非崩溃。

#### T-02：改造 FOB 状态进入：blkdiscard → NVMe User Data Erase (--ses=1)

- **优先级**：P0
- **依赖**：T-01
- **目标**：将 `run_fob_test()` 中的 `blkdiscard` 替换为 NVMe Format User Data Erase（命令：`sudo nvme format /dev/nvme0n1 --namespace-id=1 --ses=1`），符合 SNIA 规范 Purge 要求，执行后更新 SSD 状态为 FOB。
- **操作步骤**：
  1. 新增 `_nvme_purge(device: str, namespace_id: int = 1, purge_method: str = "user-data", logger=None) -> dict` 方法：
     - 优先执行 `nvme format <device> --namespace-id 1 --ses 1 --force`（User Data Erase）
     - 若返回失败，自动回退 `blkdiscard`（第二兜底，保留原有行为）
     - 返回 `{"method": "user_data_erase"|"blkdiscard"|"failed", "duration_sec": x, "error": None|str}`
  2. 修改 `run_fob_test()`：
     - 替换原有 blkdiscard 调用为 `_nvme_purge()`
     - Purge 成功后等待设备重新就绪（`nvme ns-rescan` + sleep 5s）
     - 调用 `save_ssd_state()` 将状态更新为 FOB，extra 记录 purge_method 和 duration
     - 保留原有的 blkdiscard 失败 warning 逻辑改为二级回退日志
     - **不修改**后续的 `run_full_perf_test("FOB")` 调用和日志输出
  3. SATA 设备兼容：`get_device_type()` 返回非 NVMe 时，仍使用 `blkdiscard`（SATA 不支持 NVMe Format）
- **涉及文件/模块**：PerformanceTester 类（line 1649-1695 改造，新增 _nvme_purge 方法约 50 行）
- **验收标准**：NVMe 设备默认使用 User Data Erase（--ses=1）；不支持时自动回退 blkdiscard 且日志明确记录使用的方法；Purge 后状态文件更新为 fob；SATA 设备行为不变；原有 perf_state=fob 命令行参数行为不变。

#### T-03：新增稳态检测算法工具函数

- **优先级**：P0
- **依赖**：T-01
- **目标**：实现 SNIA SSS PTS v2.0.2 定义 2.1.24 的稳态检测算法，支持 5 轮滑动窗口、Range≤20%、Slope≤10%、3 个跟踪变量同时判定。
- **操作步骤**：
  1. 新增稳态跟踪变量常量列表：
     ```python
     STEADY_TRACKING_VARS = [
         {"rw_mix": "0/100", "block_size": "4k", "name": "RND_4K_Write"},
         {"rw_mix": "65/35", "block_size": "64k", "name": "RND_64K_Mixed65"},
         {"rw_mix": "100/0", "block_size": "1024k", "name": "RND_1024K_Read"},
     ]
     ```
  2. 新增 `@staticmethod check_steady_state(history: List[float]) -> Tuple[bool, dict]` 方法：
     - 输入：最近 5 轮的某跟踪变量 IOPS 值
     - 计算 Ave、Range（Max-Min）、Range% = Range/Ave×100
     - 使用 `numpy.polyfit([1,2,3,4,5], y, 1)` 计算最佳线性拟合斜率和截距
     - 计算拟合直线在 x=1 和 x=5 处的值差（Slope Range），Slope% = SlopeRange/Ave×100
     - 计算 R² 相关系数
     - 条件 a：Range% ≤ 20；条件 b：Slope% ≤ 10；两者同时满足返回 True
     - 返回详细字典：ave、max、min、range、range_pct、condition_a_pass、slope、slope_range、slope_pct、condition_b_pass、r_squared、is_steady
  3. 新增 `@staticmethod check_all_tracking_vars(tracking_history: Dict[str, List[float]]) -> Tuple[bool, Dict[str, dict]]` 方法：
     - 遍历 3 个跟踪变量，分别调用 check_steady_state
     - 全部通过返回 True，否则 False
     - 返回每个变量的详细检测结果
- **涉及文件/模块**：PerformanceTester 类新增静态方法（约 120 行，插入在 precondition_steady_state 之前）
- **验收标准**：5 个相等值输入返回 is_steady=True 且 range_pct=0、slope_pct=0；波动超过 20% 返回 condition_a_pass=False；线性趋势超过 10% 返回 condition_b_pass=False；不足 5 轮数据返回 False 且 error 字段说明；numpy 不可用时给出明确 ImportError 提示。

#### T-04：改造稳态预处理：固定写入量 → WIPC + WDPC 多轮循环 + 稳态检测

- **优先级**：P0
- **依赖**：T-03
- **目标**：将 `precondition_steady_state()` 从"顺序写2遍 + 随机写2X + 等300s"改造为符合 SNIA 规范的"WIPC + WDPC 多轮循环 + 稳态检测"流程，达到稳态后更新状态文件。
- **操作步骤**：
  1. 保留原 `precondition_steady_state()` 方法名和返回值签名（`Tuple[bool, Dict]`），确保 `run_steady_state_test()` 调用方式不变
  2. 方法内部重构为三步：
     - **Step 1 WIPC**（保留原有逻辑，参数对齐规范）：128KiB 顺序写，写入 2X 用户容量，QD=32，无 LBA 限制。保留原有的超时处理和进度监控逻辑。
     - **Step 2 WDPC 循环**（新增，替换原有随机写2X）：
       - 新增 `_run_wdpc_round(round_num: int) -> Dict[str, Dict[str, float]]` 方法：执行一轮完整测试矩阵（7种读写比例 × 8种块大小，每点运行时间可配置，默认 60 秒），返回每轮的 IOPS 矩阵
       - 循环执行最多 `max_rounds` 轮（默认 25，新增可配置参数）
       - 从第 5 轮起，每轮结束后对 3 个跟踪变量调用 `check_all_tracking_vars()`
       - 全部通过则停止循环，记录稳态达成轮次 x，测量窗口为 Round x-4 到 x
       - 25 轮未通过则取最后 5 轮，info 中标记 `steady_reached=False`
       - **规范合规**：WIPC 和 WDPC 之间不插入 sleep（删除原有 300s 等待）
     - **Step 3 状态更新**：调用 `save_ssd_state()` 更新为 steady，extra 记录 steady_reached、steady_round、tracking_vars 详情、measurement_window
  3. 保留原有的超时降级逻辑（WIPC 阶段），WDPC 阶段每轮有独立超时保护
  4. **不修改** `run_steady_state_test()` 中预处理后的 `run_full_perf_test("Steady")` 调用
- **涉及文件/模块**：PerformanceTester.precondition_steady_state()（line 1717-1858 重构）、新增 _run_wdpc_round() 方法（约 100 行）
- **验收标准**：方法签名和返回值结构不变；WIPC 参数对齐规范；WDPC 执行完整 56 点矩阵循环；稳态检测使用 3 跟踪变量 5 轮窗口；达到稳态后状态文件更新；WIPC 与 WDPC 之间无延迟；原有 `--no-precondition` 参数行为不变；原有 `precond_rand_speed_mb`、`precond_rand_max_sec` 参数保留（用于 WIPC 阶段超时估算）。

#### T-05：新增命令行参数

- **优先级**：P1
- **依赖**：T-01, T-02, T-04
- **目标**：新增命令行参数支持手动状态切换和稳态轮次配置，供 GUI 手动状态按键调用。
- **操作步骤**：
  1. `parse_args()` 新增参数：
     - `--action`：choices=["run-tests", "enter-fob", "enter-steady", "status"]，默认 "run-tests"。用于指定执行动作。
     - `--purge-method`：choices=["auto", "user-data", "blkdiscard"]，默认 "auto"（auto=优先 user-data，失败回退 blkdiscard）。指定 FOB 擦除方法。
     - `--steady-max-rounds`：type=int，默认 25。WDPC 最大轮数。
     - `--steady-point-duration`：type=int，默认 60。WDPC 每个测试点运行秒数。
  2. `main()` 新增 action 分发逻辑：
     - `enter-fob`：执行 Purge（NVMe User Data Erase，`--ses=1`），更新状态文件，打印结果 JSON，退出
     - `enter-steady`：执行 WIPC + WDPC 至稳态，更新状态文件，打印结果 JSON，退出
     - `status`：读取状态文件，打印当前设备状态 JSON，退出
     - `run-tests`（默认）：保持原有测试流程不变
  3. `TestConfig` 数据类新增对应字段：`purge_method`、`steady_max_rounds`、`steady_point_duration`，在 main() 中从 args 赋值
- **涉及文件/模块**：parse_args()（line 5748+）、main()（line 5935+）、TestConfig 类（line 432+）
- **验收标准**：新增参数均有默认值，不传时行为与 v1.9.1 完全一致；`--action enter-fob` 可独立执行 Purge 并更新状态；`--action status` 可查询状态；原有所有参数和测试流程不受影响。

#### T-06：GUI 新增 SSD 状态显示面板

- **优先级**：P1
- **依赖**：T-01
- **目标**：在性能标签页顶部新增 SSD 状态显示区域，实时展示当前设备状态、设备序列号、最后更新时间。
- **操作步骤**：
  1. 在 `_build_performance_tab()` 的"测试状态"行之前（row=0 位置）插入状态显示面板：
     - LabelFrame "SSD 状态"，包含：
       - 当前状态标签：`self.ssd_state_label`（大号字体，颜色区分：FOB=蓝色、STEADY=绿色、UNKNOWN=灰色）
       - 设备序列号标签：`self.ssd_serial_label`
       - 最后更新时间标签：`self.ssd_state_time_label`
       - "刷新状态"按钮：调用 `_refresh_ssd_state()`
  2. 新增 `_refresh_ssd_state()` 方法：
     - 获取当前选中设备的序列号
     - 调用 `load_ssd_state(serial)` 读取状态
     - 更新三个标签的文本和颜色
     - 状态为 unknown 时显示"未检测（请先执行状态进入或测试）"
  3. 在 `scan_devices()` 完成后和设备选择变化时自动调用 `_refresh_ssd_state()`
  4. GUI 启动时（`__init__` 的 after 回调中）也调用一次刷新
- **涉及文件/模块**：SSDTestGUI._build_performance_tab()（line 6476+ 插入约 30 行）、新增 _refresh_ssd_state() 方法（约 25 行）
- **验收标准**：脚本启动时状态面板默认显示 Unknown（灰色）；状态面板显示在性能标签页顶部；状态颜色区分明显（FOB=蓝色、STEADY=绿色、UNKNOWN=灰色）；切换设备时状态自动刷新；状态文件不存在时显示 unknown 而非报错；不影响原有性能参数控件的布局和功能。

#### T-07：GUI 新增手动状态切换按键

- **优先级**：P1
- **依赖**：T-05, T-06
- **目标**：在状态显示面板旁新增"进入 FOB"和"进入稳态"两个按钮，点击后通过 subprocess 调用命令行 `--action` 执行状态进入，完成后自动刷新状态显示。
- **操作步骤**：
  1. 在状态显示面板中新增两个按钮：
     - "进入 FOB 状态"按钮（蓝色）：调用 `_enter_ssd_state("fob")`
     - "进入稳态"按钮（绿色）：调用 `_enter_ssd_state("steady")`
  2. 新增 `_enter_ssd_state(state: str)` 方法：
     - 弹出确认对话框（FOB 操作需二次确认，提示"将永久擦除设备所有数据"）
     - 确认后禁用两个按钮和开始测试按钮，显示"正在进入 FOB 状态..."进度提示
     - 构建命令：`[sys.executable, os.path.expanduser("~/max_tool/ssd_test_v1.9.2.py"), "-d", device, "--action", "enter-fob"|"enter-steady", "--purge-method", 当前选择, "-y"]`（脚本路径使用用户指定的 `~/max_tool/ssd_test_v1.9.2.py`，通过 `os.path.expanduser` 解析）
     - 使用 `subprocess.Popen` 启动，通过已有日志读取线程实时输出日志（复用现有 `_read_process_output` 机制）
     - 进程结束后调用 `_refresh_ssd_state()` 刷新状态
     - 恢复按钮可用状态
  3. 按钮执行期间防止重复点击和关闭窗口
  4. `build_command()` 无需修改（手动状态切换不通过 build_command，直接构建 action 命令）
- **涉及文件/模块**：SSDTestGUI 新增 _enter_ssd_state() 方法（约 60 行）、_build_performance_tab() 新增按钮
- **验收标准**：点击按钮弹出确认框；确认后执行对应状态进入；日志实时显示在 GUI 日志区域；完成后状态标签自动更新；执行期间按钮禁用防止重复操作；FOB 操作有明确的数据丢失警告。

#### T-08：GUI 开始测试逻辑调整：Unknown直接测试/状态一致直接测试/不一致警告弹窗供用户选择

- **优先级**：P1
- **依赖**：T-07
- **目标**：当用户选择了"性能测试"项目并点击"开始测试"时，根据当前 SSD 状态和 perf_state 选择执行三分支逻辑：Unknown 状态直接测试；已进入状态且与目标一致则直接测试不重复进入；状态不一致则弹出警告对话框，用户可选择直接进入目标状态再测试，或取消后手动控制状态。
- **操作步骤**：
  1. 修改 `start_test()` 方法，在构建命令和启动进程之前，新增状态校验逻辑：
     - 检查选中的测试项是否包含 TEST_PERF，不包含则跳过状态校验，按原流程执行
     - 若包含性能测试，读取当前 perf_state 选择（fob/steady/both）和当前 SSD 状态（从状态文件读取）
     - **分支 A — 当前状态为 Unknown**：直接构建命令并启动测试，不进入任何状态，日志中提示"当前状态为 Unknown，直接执行性能测试"
     - **分支 B — 当前状态为 FOB 或 Steady，且与 perf_state 目标一致**：直接构建命令并启动测试，不重复进入状态，日志中提示"当前状态与目标状态一致（{当前状态}），直接执行性能测试"
     - **分支 C — 当前状态为 FOB 或 Steady，但与 perf_state 目标不一致**：弹出警告对话框（`tkinter.messagebox.askyesno`），标题"状态不一致警告"，内容为"SSD目前状态与目标状态不一致，请确认是否直接进入 {目标状态} 状态再执行后续测试？\n\n当前状态：{当前状态}\n目标状态：{目标状态}\n\n选择【是】：自动进入目标状态后开始性能测试\n选择【否】：取消测试，请手动控制SSD状态后再开始测试"
       - 用户点击"是"：复用 `_enter_ssd_state(目标状态)` 逻辑执行状态进入，完成后在回调中继续执行原 `start_test()` 的剩余流程（构建命令并启动测试）
       - 用户点击"否"：关闭对话框，不启动测试进程，日志中提示"已取消，请手动切换SSD状态后再开始测试"，直接返回
     - **perf_state=both 的特殊处理**：both 表示依次执行 FOB 和 Steady 测试，目标状态为 FOB（先执行 FOB 测试）。若当前状态为 Steady 则按分支 C 弹出警告，用户可选择直接进入 FOB 再测试；若当前状态为 Unknown 则按分支 A 直接测试
  2. 警告对话框使用 `askyesno`（是/否按钮），按钮文字明确对应操作含义
  3. 自动进入目标状态时，FOB 操作需保留二次确认（数据丢失警告），稳态进入直接执行
  4. 不修改 `build_command()` 的参数构建逻辑
  5. 不选择性能测试时（仅选 SMART/容量/电源循环等），行为完全不变
- **涉及文件/模块**：SSDTestGUI.start_test()（line 约 7500+，需定位具体行）、复用 _enter_ssd_state() 方法
- **验收标准**：Unknown 状态 + 选择性能测试 → 直接启动测试，无状态进入；已进入 FOB + 选择 perf_state=fob → 直接启动测试，不重复 Purge；已进入 FOB + 选择 perf_state=steady → 弹出警告对话框（是/否），选择"是"则先进入稳态再测试，选择"否"则取消不启动；已进入 Steady + 选择 perf_state=fob → 弹出警告对话框，选择"是"则先进入 FOB 再测试，选择"否"则取消；perf_state=both + 当前 FOB → 直接启动；perf_state=both + 当前 Steady → 弹出警告；不选性能测试 → 行为完全不变；警告信息明确包含当前状态和目标状态。

#### T-09：编写代码修改执行风险分析文档

- **优先级**：P1
- **依赖**：T-01 ~ T-08, T-11, T-12（必须在所有代码修改完成并通过 T-11 全局静态校验后才开始编写）
- **目标**：所有代码修改完成并通过静态校验后，交付独立的 md 文档，详细说明本次代码修改后执行测试的各类风险及缓解措施。文档内容基于最终代码实际实现编写，不提前编写。
- **操作步骤**：
  1. 文档结构：
     - 高风险（数据丢失）：User Data Erase (--ses=1) 误操作、设备节点识别错误、WDPC 长时间写入对寿命的影响
     - 中风险（测试失败/结果无效）：nvme format --ses=1 执行失败回退 blkdiscard、numpy 未安装、WDPC 循环超时、稳态误判/未达成、WIPC-WDPC 间意外延迟
     - 低风险（性能/资源）：测试时长显著增加（WDPC 每轮56分钟×最多25轮）、状态文件并发写入、GUI  subprocess 资源
     - 兼容性风险：Python 3.10 语法兼容、nvme-cli 版本差异、SATA 设备回退行为
     - 回归风险：其他 7 个测试类不受影响的验证方式
  2. 每个风险包含：风险描述、触发条件、影响程度、缓解措施、验证方法
- **涉及文件/模块**：新增 `代码修改执行风险分析.md`
- **验收标准**：覆盖所有新增和修改功能的风险；每个风险有可操作的缓解措施；明确标注与 v1.9.1 行为差异带来的风险。

#### T-10：编写上位机快速使用 + 环境配置文档

- **优先级**：P2
- **依赖**：T-01 ~ T-08, T-11, T-12（必须在所有代码修改完成并通过 T-11 全局静态校验后才开始编写）
- **目标**：所有代码修改完成并通过静态校验后，交付独立的 md 文档，指导用户在 Ubuntu 22.04 上配置环境并快速使用改造后的上位机软件。文档内容基于最终代码实际界面和参数编写，不提前编写。
- **操作步骤**：
  1. 文档结构：
     - 环境配置：系统要求（Ubuntu 22.04 + Python 3.10）、系统依赖安装（nvme-cli、fio、numpy）、权限配置（root/sudo、udev 规则可选）
     - 启动方式：命令行模式启动、GUI 模式启动、常用命令示例
     - 上位机界面说明：设备选择区、测试项选择区、性能参数标签页（重点说明新增的 SSD 状态面板和手动状态按钮）、日志区
     - SSD 状态管理操作指南：查看当前状态、手动进入 FOB（步骤+截图位说明）、手动进入稳态（步骤+预计时长）、状态文件说明
     - 性能测试操作流程：选择状态→配置参数→开始测试→查看结果
     - 常见问题：状态显示 unknown、nvme format --ses=1 失败回退 blkdiscard、稳态测试时间过长、numpy 导入错误
     - 命令行高级用法：--action 参数使用、--purge-method 选择、--steady-max-rounds 配置
- **涉及文件/模块**：新增 `上位机快速使用与环境配置指南.md`
- **验收标准**：从零开始的用户可按文档完成环境配置和首次测试；新增功能（状态显示、手动按钮）有详细操作说明；常见问题覆盖已知风险点。

#### T-11：全局静态校验与版本号升级

- **优先级**：P0
- **依赖**：T-01 ~ T-08
- **目标**：完成所有代码修改后进行静态校验，确保语法正确、导入完整、其他测试类未被误修改，版本号升级为 v1.9.2。
- **操作步骤**：
  1. 语法检查：`python -m py_compile ssd_test_v1.9.2.py`（在交付说明中告知用户在 Linux 执行，本电脑不调试但可做语法编译检查）
  2. 导入检查：确认 numpy 导入放在 try/except 中，不可用时给出友好提示而非崩溃
  3. 回归检查：全局搜索确认以下类的方法签名和核心逻辑未被修改：
     - FirmwareTester、SmartTester、CapacityTester、ReadWriteTester、PowerCycleTester、SPORTester、OSInterruptionTester、TestReport
  4. 命令行参数兼容性检查：确认 v1.9.1 的所有参数在 v1.9.2 中仍然存在且默认值不变
  5. 版本号升级：`SCRIPT_VERSION` 从 v1.9.1 改为 v1.9.2；GUI 标题同步更新
  6. 文件另存为 `ssd_test_v1.9.2.py`（保留原 v1.9.1 文件不覆盖）
- **涉及文件/模块**：ssd_test_v1.9.2.py（新文件）、版本常量
- **验收标准**：py_compile 无语法错误；其他 7 个测试类代码与 v1.9.1 完全一致（diff 验证）；所有原有命令行参数保留；版本号正确更新；原 v1.9.1 文件未被修改。

#### T-12：性能测试中 SMART 健康检查忽视介质错误 5353

- **优先级**：P1
- **依赖**：无
- **目标**：在性能测试流程中执行 SMART 健康检查时，忽视介质错误（Media Errors）ID 为 5353 的告警，不将其视为测试失败或异常。
- **操作步骤**：
  1. 定位 PerformanceTester 类中执行 SMART 健康检查的代码位置（性能测试前后可能调用 SMART 检查）
  2. 在 SMART 检查结果解析逻辑中，新增介质错误 ID 过滤：
     - 识别 SMART 属性中 ID=5353 的介质错误计数
     - 当该属性值 > 0 时，不判定为测试失败，仅在日志中以 warning 级别记录"检测到介质错误 ID=5353，已按配置忽视"
     - 其他 SMART 错误（如关键警告、不可修复错误等）仍按原逻辑处理
  3. 新增可配置参数 `--smart-ignore-media-errors`，默认值包含 "5353"，支持逗号分隔多个 ID（如 "5353,1234"），便于后续扩展
  4. TestConfig 数据类新增 `smart_ignore_media_errors: str = "5353"` 字段
  5. parse_args() 新增 `--smart-ignore-media-errors` 参数
  6. **不修改**独立的 SmartTester 测试类（该类为单独的 SMART 测试项，不在本次修改范围内），仅修改性能测试流程中内嵌的 SMART 检查
- **涉及文件/模块**：PerformanceTester 类中 SMART 检查相关方法、parse_args()、TestConfig 类
- **验收标准**：性能测试中 SMART 检查遇到 ID=5353 介质错误时不报错、不中断测试，仅记录 warning；其他 SMART 错误仍正常处理；`--smart-ignore-media-errors` 参数可配置多个 ID；独立 SmartTester 测试类行为不变；参数默认值为 "5353"。

---

## 5. 代码变更规则

### 5.1 变更总览

| 子任务 | 变更类型 | 变更范围 | 影响面 | 兼容性 |
|-------|---------|---------|-------|-------|
| T-01 | 新增 | 工具函数区新增 5 个函数 + 3 个常量 | 被 T-02/T-04/T-06 调用；不影响现有代码 | 完全兼容（纯新增） |
| T-02 | 修改 | PerformanceTester.run_fob_test() 内部擦除逻辑替换；新增 _nvme_purge() 方法 | 仅影响 FOB 状态进入方式；run_full_perf_test("FOB") 调用不变 | 向后兼容（方法签名不变，行为更规范） |
| T-03 | 新增 | PerformanceTester 新增 2 个静态方法 + 1 个常量列表 | 被 T-04 调用；不影响现有代码 | 完全兼容（纯新增） |
| T-04 | 修改 | PerformanceTester.precondition_steady_state() 内部重构；新增 _run_wdpc_round() 方法 | 仅影响稳态预处理流程；run_steady_state_test() 调用方式和返回值结构不变 | 向后兼容（方法签名不变，删除 300s 等待是规范要求的行为变更） |
| T-05 | 修改 | parse_args() 新增 4 个参数；main() 新增 action 分发；TestConfig 新增 3 字段 | 新增参数均有默认值；原有 run-tests 流程不变 | 完全兼容（新增可选参数） |
| T-06 | 修改 | SSDTestGUI._build_performance_tab() 插入状态面板；新增 _refresh_ssd_state() | 仅影响性能标签页布局；其他标签页不变 | 完全兼容（UI 新增区域） |
| T-07 | 新增 | SSDTestGUI 新增 _enter_ssd_state() 方法；状态面板新增 2 按钮 | 仅性能标签页新增交互；不影响现有测试启动流程 | 完全兼容（纯新增 UI 和方法） |
| T-08 | 修改 | SSDTestGUI.start_test() 新增状态校验三分支逻辑（Unknown直接测试/一致直接测试/不一致警告弹窗供用户选择直接进入目标状态或取消手动控制） | 仅在选择性能测试时触发；不选性能测试时行为完全不变；复用 _enter_ssd_state() 执行状态进入 | 完全兼容（条件分支，默认行为不变） |
| T-12 | 修改 | PerformanceTester 中 SMART 检查新增介质错误 ID 过滤；parse_args/TestConfig 新增 --smart-ignore-media-errors 参数 | 仅影响性能测试内嵌的 SMART 检查；独立 SmartTester 类不变 | 完全兼容（新增可选参数，默认忽视5353） |
| T-11 | 修改 | 版本号升级；文件另存为 v1.9.2 | 全局版本标识 | 兼容（原文件保留） |

### 5.2 功能移除校验

本次修改**不包含任何功能移除**。原有 `blkdiscard` 逻辑作为第三兜底保留在 `_nvme_purge()` 中；原有固定写入量稳态预处理的 WIPC 部分被保留并规范对齐，随机写 2X 部分被 WDPC 循环替代（但 `precond_rand_speed_mb`、`precond_rand_max_sec` 参数保留用于 WIPC 超时估算）；原有 300s 等待被删除（这是 SNIA 规范明确禁止的行为，属于合规性修正而非功能移除）。

### 5.3 通用原则

1. 所有代码变更不得破坏原有项目功能，其他 7 个测试类零修改。
2. 修改现有方法时保持方法签名和返回值结构不变，确保调用方（run_steady_state_test、GUI build_command）无需改动。
3. 新增命令行参数均为可选且有默认值，不传时行为与 v1.9.1 一致。
4. 新增代码遵循项目现有代码风格（中文注释、类型注解、dataclass、run_cmd 封装等）。
5. numpy 导入使用 try/except 包裹，不可用时给出明确的安装提示，不导致整个脚本崩溃。
6. 修改后文件另存为 v1.9.2，原 v1.9.1 文件保留不覆盖，便于回滚对比。

---

## 6. 风险与校验项

| 风险编号 | 风险描述 | 风险等级 | 关联任务 | 校验方式 | 校验时机 |
|---------|---------|---------|---------|---------|---------|
| R-01 | NVMe User Data Erase (--ses=1) 误操作导致目标设备数据永久丢失，若设备节点选错可能擦除系统盘 | 高 | T-02, T-07 | GUI 手动进入 FOB 时二次确认弹窗；命令行需 -y 参数；代码中 Purge 前检查 findmnt 挂载状态 | 提测前 |
| R-02 | 部分设备 nvme format --ses=1 执行失败（如 namespace 被占用、固件异常），导致 Purge 失败 | 中 | T-02 | 代码实现二级回退（user-data→blkdiscard）；日志明确记录实际使用的方法和失败原因；交付文档说明 | 开发中 |
| R-03 | numpy 未安装导致稳态检测算法导入失败，脚本启动崩溃 | 中 | T-03 | numpy 导入放在 try/except 中；不可用时打印 `pip install numpy` 提示并以非零退出码退出；环境配置文档明确列出依赖 | 开发中 |
| R-04 | WDPC 稳态测试时长显著增加（每轮 56 分钟 × 最多 25 轮 = 最长 23 小时），用户可能误以为程序卡死 | 中 | T-04, T-07 | 每轮结束打印轮次进度和已用时间；GUI 日志实时输出；使用文档明确说明预计时长；新增 --steady-max-rounds 和 --steady-point-duration 参数可缩短 | 提测前 |
| R-05 | 稳态误判（5 轮数据偶然达标）或 25 轮未达成稳态，测试结果标注为 steady 但实际未达标 | 中 | T-03, T-04 | 3 个跟踪变量同时判定（降低偶然达标概率）；未达成时 info 中明确标记 steady_reached=False；状态文件 extra 记录详情；报告中必须声明 | 开发中 |
| R-06 | nvme-cli 版本差异（Ubuntu 22.04 默认 v1.16 vs 新版 v2.x）导致 --ses 参数或输出格式不兼容 | 中 | T-02, T-05 | 代码中使用 `--force` 参数兼容新旧版本；解析 nvme 输出时做容错处理；环境配置文档指定最低版本 | 提测前 |
| R-07 | GUI 手动状态切换的 subprocess 与开始测试的 subprocess 并发执行，导致设备同时被两个进程访问 | 中 | T-07, T-08 | 状态切换执行期间禁用开始测试按钮；状态切换完成后才恢复；代码中用 is_running 标志互斥 | 开发中 |
| R-08 | 状态文件并发写入冲突（GUI 刷新状态时同时有测试进程在更新状态） | 低 | T-01 | 状态文件写入使用临时文件+os.replace 原子操作；读取时做 JSON 解析异常容错 | 开发中 |
| R-09 | SATA 设备执行性能测试时，_nvme_purge 回退到 blkdiscard，但 blkdiscard 对部分 SATA 设备不支持 | 低 | T-02 | 代码中 get_device_type() 判断设备类型，SATA 直接走 blkdiscard 分支；blkdiscard 失败时记录 warning 并继续（保留原有行为） | 开发中 |
| R-10 | 修改 precondition_steady_state() 时意外影响 run_steady_state_test() 的调用逻辑或返回值解析 | 中 | T-04, T-11 | 保持方法签名 `-> Tuple[bool, Dict[str, Any]]` 不变；返回字典保留原有 key（steps、success、error），新增 key 为附加；T-11 全局 diff 校验 | 提测前 |
| R-11 | Python 3.10 语法兼容性：使用了 3.10+ 才有的语法（如 match-case、int|str 联合类型注解）导致在更低版本报错 | 低 | 全部 | 代码中不使用 match-case；类型注解使用 Optional[X] 而非 X|None；T-11 py_compile 验证 | 开发中 |
| R-12 | WDPC 循环中 fio 命令构建错误（如 ActiveRange 计算、读写比例参数）导致每轮测试数据无效 | 中 | T-04 | 复用现有 _build_fio_cmd() 方法构建 fio 命令，不重新手写；每轮结束记录 IOPS 矩阵，异常值（IOPS=0）时打 warning | 开发中 |
| R-13 | 状态不一致警告弹窗逻辑误判：状态文件读取异常或序列号不匹配导致已进入状态却被误判为 Unknown，或 Unknown 被误判为已进入状态 | 中 | T-08 | 状态读取时做 JSON 解析异常容错，异常时视为 Unknown；序列号严格匹配，不匹配则视为 Unknown；日志中明确记录读取到的状态值和序列号；警告弹窗中明确显示当前状态和目标状态供用户确认 | 开发中 |
| R-14 | perf_state=both 时状态校验逻辑复杂（先 FOB 后 Steady），可能导致误警告或漏校验；用户在警告弹窗中选择"直接进入目标状态"后，状态进入失败的处理 | 中 | T-08 | both 模式目标状态明确为 FOB（先执行 FOB 测试）；Steady 状态下选择 both 时弹出警告；用户选择"是"后复用 _enter_ssd_state() 逻辑，状态进入失败时在日志中明确报错并停止测试，不静默继续；代码中单独处理 both 分支并加注释 | 开发中 |
| R-15 | SMART 介质错误 ID=5353 过滤逻辑误过滤其他关键 SMART 错误，导致真实硬件故障被忽视 | 中 | T-12 | 仅过滤 ID 精确匹配 5353 的介质错误属性；其他 SMART 属性（关键警告、不可修复错误等）仍按原逻辑处理；过滤时日志明确记录"已忽视介质错误 ID=5353"；参数可配置，默认仅 5353 | 开发中 |

### 关键校验清单

- [ ] `python -m py_compile ssd_test_v1.9.2.py` 语法检查通过（用户在 Linux 执行）
- [ ] numpy 导入 try/except 容错，未安装时给出友好提示
- [ ] 其他 7 个测试类（FirmwareTester/SmartTester/CapacityTester/ReadWriteTester/PowerCycleTester/SPORTester/OSInterruptionTester/TestReport）与 v1.9.1 diff 完全一致
- [ ] v1.9.1 所有命令行参数在 v1.9.2 中保留且默认值不变
- [ ] run_fob_test() 和 precondition_steady_state() 的方法签名和返回值结构不变
- [ ] NVMe 设备 Purge 使用 User Data Erase (--ses=1)，失败自动回退 blkdiscard 且日志记录
- [ ] 稳态检测使用 3 跟踪变量 + 5 轮窗口 + Range≤20% + Slope≤10%
- [ ] WIPC 与 WDPC 之间无 sleep 延迟
- [ ] GUI 状态面板显示正常，切换设备自动刷新
- [ ] GUI 手动进入 FOB 有二次确认弹窗，执行期间按钮禁用
- [ ] 脚本启动时状态面板默认显示 Unknown（灰色）
- [ ] Unknown 状态 + 选择性能测试 → 直接启动测试，不进入任何状态
- [ ] 已进入 FOB + perf_state=fob → 直接启动测试，不重复 Purge
- [ ] 已进入 FOB + perf_state=steady → 弹出警告对话框（是/否），选"是"先进入稳态再测试，选"否"取消不启动
- [ ] 已进入 Steady + perf_state=fob → 弹出警告对话框（是/否），选"是"先进入 FOB 再测试，选"否"取消不启动
- [ ] perf_state=both + 当前 FOB → 直接启动；perf_state=both + 当前 Steady → 弹出警告对话框
- [ ] 不选性能测试时开始测试行为完全不变
- [ ] 性能测试中 SMART 检查遇到介质错误 ID=5353 时不报错不中断，仅记录 warning
- [ ] 独立 SmartTester 测试类行为不变（未被修改）
- [ ] 状态文件按设备序列号隔离，读写原子操作
- [ ] 版本号升级为 v1.9.2，原 v1.9.1 文件保留未修改
- [ ] 风险分析文档和使用指南文档已交付

---

## 7. 交付物清单

| 交付物 | 类型 | 关联任务 | 交付标准 |
|-------|------|---------|---------|
| ssd_test_v1.9.2.py | 代码 | T-01 ~ T-08, T-11, T-12 | 改造后的完整脚本，语法正确，功能完整，原 v1.9.1 保留 |
| 代码修改执行风险分析.md | 文档 | T-09 | 覆盖高/中/低/兼容性/回归五类风险，每个风险有缓解措施和验证方法 |
| 上位机快速使用与环境配置指南.md | 文档 | T-10 | 包含环境配置步骤、界面说明、状态管理操作、常见问题、命令行高级用法 |
| 本执行计划文档 | 文档 | — | 任务拆解、代码变更规则、风险校验、回滚方案完整 |

---

## 8. 回滚方案

### 8.1 回滚触发条件

- 改造后的 v1.9.2 在 Linux 环境执行时出现语法错误或导入错误且 30 分钟内无法修复
- 性能测试模块（FOB/稳态）执行结果与 v1.9.1 出现不可解释的重大偏差
- 新增的 GUI 状态控制功能导致原有测试启动流程异常
- NVMe User Data Erase (--ses=1) 在目标设备上持续失败且回退机制异常

### 8.2 回滚步骤

1. **代码回滚**：本次修改采用"另存新文件"策略，原 `ssd_test_v1.9.1.py` 文件未被修改。回滚时直接停止使用 v1.9.2，恢复使用 v1.9.1 即可，无需 git revert 或文件恢复操作。
2. **状态文件清理**（可选）：删除 `~/.ssd_test_state.json` 状态文件，v1.9.1 不使用此文件，残留无影响。
3. **配置回滚**：v1.9.2 新增的命令行参数（--action、--purge-method、--steady-max-rounds、--steady-point-duration）在 v1.9.1 中不存在，回滚后不再传递这些参数即可。若有保存的配置文件包含新参数，删除对应字段。
4. **验证**：使用 v1.9.1 执行一次 `--dry-run` 冒烟测试，确认所有原有测试项正常加载，GUI 正常启动。

### 8.3 回滚责任人与时间预估

- 回滚操作人：测试操作员（用户本人）
- 预估回滚耗时：≤ 5 分钟（切换文件即可）
- 数据丢失风险：无（原文件未被覆盖；状态文件为新增，删除不影响任何功能）
- 注意：若已使用 v1.9.2 的 NVMe User Data Erase (--ses=1) 擦除了测试设备，回滚代码无法恢复设备数据（数据已被物理擦除），但这不影响代码回滚本身。

---

## 版本变更记录

| 版本 | 日期 | 变更内容 | 影响范围 | 操作人 |
|-----|------|---------|---------|-------|
| v1.0 | 2026-09-22 | 初始版本：FOB/稳态改造 + GUI 状态控制 + 风险文档 + 使用指南的完整执行计划 | 全部 | AI 生成 |
| v1.1 | 2026-09-22 | 1. FOB 擦除方式从 Cryptographic Erase (--ses=2) 改为 User Data Erase (--ses=1)，命令为 `sudo nvme format /dev/nvme0n1 --namespace-id=1 --ses=1`，回退逻辑从三级改为二级（user-data→blkdiscard）；2. 明确交付脚本路径为 `~/max_tool/ssd_test_v1.9.2.py`；3. 同步更新 T-02/T-05/T-07/T-09/T-10、风险表、校验清单、回滚方案中所有擦除方式相关描述 | T-02, T-05, T-07, T-09, T-10, 风险表, 回滚方案 | AI 生成 |
| v1.2 | 2026-09-22 | 彻底删除文档中所有 Cryptographic Erase (--ses=2) 相关内容：1. 用户原始需求原文中擦除方式改为 User Data Erase (--ses=1)；2. 选定方案表移除备选方案 B（--ses 2 Cryptographic Erase），仅保留 --ses=1 选定方案和 blkdiscard 兜底方案；3. 方案选择理由中移除 Cryptographic Erase 对比描述 | 原始需求, 选定方案表 | AI 生成 |
| v1.3 | 2026-09-23 | 追加三项需求：1. 脚本启动默认 Unknown 状态，Unknown 下选择性能测试直接测试不进入状态；2. 已手动进入状态后开始测试需校验状态一致性，一致则直接测试不重复进入，不一致则 GUI 报错并停止测试（完全重写 T-08 逻辑，取代原"提示并可自动进入状态"方案）；3. 性能测试中 SMART 健康检查忽视介质错误 ID=5353（新增 T-12 子任务）；同步更新需求冲突项、任务总览、代码变更规则、风险表（新增 R-13/R-14/R-15）、校验清单、交付物清单 | T-06, T-08, T-12, 风险表, 校验清单 | AI 生成 |
| v1.4 | 2026-09-23 | 调整交付顺序：T-09（风险分析文档）和 T-10（使用指南文档）的依赖从部分代码任务改为 T-01~T-08, T-11, T-12 全部代码修改完成并通过静态校验后才开始编写；明确文档内容基于最终代码实际实现编写，不提前编写 | T-09, T-10 | AI 生成 |
| v1.5 | 2026-09-23 | 澄清第3步与第7步需求关系并修改T-08逻辑：1. 明确第3步原始行为为"Unknown时先进入选择状态再测试"，第7步追加需求将其改为"Unknown直接测试不进入状态"；2. 将第7步中"不一致则报错停止"修改为"弹出警告对话框，用户可选择直接进入目标状态再测试，或取消后手动控制状态"（使用askyesno是/否按钮）；3. 同步更新需求冲突项C-01、任务总览、T-08详情、代码变更规则、风险表R-13/R-14、校验清单 | T-08, 需求冲突项, 风险表, 校验清单 | AI 生成 |

<!-- 迭代更新时在此追加新版本记录，不删除历史记录 -->
