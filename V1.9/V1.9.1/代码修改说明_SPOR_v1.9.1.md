# ssd_test.py SPOR 三模式改造代码修改说明（v1.9.0 → v1.9.1）

> 文档版本：v1.0 | 生成日期：2026-09-18 | 对应脚本：ssd_test_v1.9.1.py

---

## 1. 修改概述

本次修改对 **Surprise Power Cycle Test（意外电源循环测试，SPOR）** 模块进行三模式改造。当前 SPOR 仅支持 **Timeboard 硬件断电**（旧方案）一种方式，本次改造新增 **manual（手动意外断电）** 和 **enhanced（增强方案，融合 OKN 框架最佳实践）** 两种断电方式，用户可通过命令行参数 `--spor-power-mode` 或 GUI 上位机下拉框在 `timeboard` / `manual` / `enhanced` 三种模式间选择。

`enhanced` 模式的核心增强来自 OKN 企业级 SSD 测试框架 `spor_test.py`：

1. **IPMI 直接意外断电**——不经过 OS shutdown、不执行 sync，通过 `ipmitool chassis power off` 直接切断电源，实现真正自动化的意外断电（无需 Timeboard 专用硬件）
2. **更高写入压力**——iodepth=256（旧方案为 32），掉电瞬间更多 outstanding IO，更严格地验证 PLP（Power Loss Protection）
3. **线程级异步写入与进度追踪**——参考 OKN `MyThread` 模式，通过 `threading.Thread` + `/proc/<pid>/io` 定期读取精确追踪掉电时写入进度，替代 iolog 解析
4. **增强型 Pattern 校验**——fio `--verify=pattern --do_verify=1` 同时执行数据比对和 Pattern 验证，直接返回 mismatch 计数

---

## 2. 三种模式对比

| 对比维度 | timeboard（旧方案） | manual（手动） | enhanced（增强） |
|---------|-------------------|--------------|----------------|
| 断电方式 | Timeboard 硬件 Modbus RTU 触发断电 | 用户手动切断电源 | IPMI `chassis power off` 直接断电 |
| 自动化程度 | 完全自动化 | 需人工操作 | 完全自动化 |
| 硬件依赖 | Timeboard 板卡 + 串口 | 电源开关/PDU | IPMI BMC（测试机自带） |
| 断电前操作 | 不 sync、不 shutdown | 不 sync、不 shutdown | 不 sync、不 shutdown |
| 写入方式 | fio 顺序写，iodepth=32, numjobs=4 | 同 timeboard | fio 顺序写，iodepth=256, numjobs=1 |
| 写入进度追踪 | fio write_iolog 日志解析 | 同 timeboard | 线程级 `/proc/<pid>/io` 读取（fallback 到 iolog） |
| 数据校验方式 | 自定义 verify_pattern（fio read + 输出解析） | 同 timeboard | fio 内置 `--verify=pattern --do_verify=1` |
| 打底 Pattern | 0x11 | 0x11 | 0x11 |
| 写入 Pattern | 0x22 | 0x22 | 0x22 |
| 掉电边界容错 | 跳过最后 skip_lba（默认 8）个 LBA | 同左 | 同左 |
| 上电后检查 | 设备检测 + PCIe + SMART + Pattern 校验 | 同左 | 同左 |
| 最终功能测试 | 容量 + SMART + 基本性能 | 同左 | 同左 |

---

## 3. 逐区域修改清单

### 3.1 常量定义区

**变更类型**：新增

在 PC enhanced 模式常量之后新增：

```python
# SPOR 三模式默认配置
DEFAULT_SPOR_POWER_MODE = "timeboard"  # timeboard / manual / enhanced
DEFAULT_SPOR_ENHANCED_IODEPTH = 256
DEFAULT_SPOR_ENHANCED_BS = "128k"
```

### 3.2 TestConfig 配置类

**变更类型**：新增字段

在 `spor_state_file` 字段之后、OSINT 配置之前新增：

```python
# SPOR 三模式配置
spor_power_mode: str = DEFAULT_SPOR_POWER_MODE
spor_enhanced_iodepth: int = DEFAULT_SPOR_ENHANCED_IODEPTH
spor_enhanced_bs: str = DEFAULT_SPOR_ENHANCED_BS
```

### 3.3 SPORTester 类 — 新增 7 个方法

**变更类型**：新增（不修改任何现有方法）

在 `phase1_poweroff` 之前插入 5 个核心方法，在 `_verify_unwritten_area` 之后插入 2 个验证辅助方法：

#### 3.3.1 `enhanced_ipmi_surprise_poweroff(self, state) -> Tuple[bool, str]`

- **功能**：通过 IPMI 直接意外断电（不 sync、不 shutdown）
- **流程**：保存状态（phase=POWERON）→ 启动 nohup 后台脚本（等待 → `ipmitool chassis power off` → 延时 → `ipmitool chassis power on`）
- **关键约束**：后台脚本中**不包含** `shutdown` 命令，**不执行** `os.sync()`——这是 SPOR 意外断电与 Normal Power Cycle 正常关机的本质区别
- **参考来源**：OKN `power_down(safe_shutdown=False)` 直接 `link_state.power_off()`

#### 3.3.2 `start_enhanced_write_thread(self) -> bool`

- **功能**：启动高队列深度异步写入（线程级追踪）
- **实现**：使用 `threading.Thread` 启动 fio 顺序写（iodepth=256, bs=128k, buffer_pattern=0x22, write_iolog）；另起监控线程定期读取 `/proc/<pid>/io` 的 `write_bytes` 更新共享进度变量 `self._enhanced_write_bytes`
- **参考来源**：OKN `MyThread` 异步写入模式

#### 3.3.3 `get_enhanced_write_position(self) -> int`

- **功能**：从线程共享进度变量获取掉电时已写入 LBA
- **容错**：进度变量为 0 时 fallback 到现有 `get_write_position()`（iolog 解析）

#### 3.3.4 `enhanced_verify_pattern(self, pattern_hex, size, offset='0') -> Tuple[bool, int]`

- **功能**：使用 fio 内置 `--verify=pattern` 逐块校验
- **实现**：fio `--rw=read --verify=pattern --verify_pattern=0xXX --do_verify=1 --output-format=json`，解析 JSON 获取 `verify_errors` 计数
- **容错**：JSON 解析失败时 fallback 到文本匹配（检测 "mismatch"）
- **参考来源**：OKN `verify_read(do_data_compare=True, verify_pattern=True)`

#### 3.3.5 `manual_surprise_poweroff(self, state) -> Tuple[bool, str]`

- **功能**：手动意外断电
- **流程**：保存状态 → 打印倒计时提示（"请立即手动切断电源"）→ 等待（进程会被断电强制终止）
- **关键**：不执行任何断电操作，不 sync

#### 3.3.6 `_enhanced_verify_written_area(self, last_lba) -> bool`

- **功能**：enhanced 模式验证已写入区域（前段 pattern22），逻辑与现有 `_verify_written_area` 相同，但调用 `enhanced_verify_pattern()` 替代 `verify_pattern()`

#### 3.3.7 `_enhanced_verify_unwritten_area(self, last_lba) -> bool`

- **功能**：enhanced 模式验证未被覆盖区域（后段 pattern11），逻辑与现有 `_verify_unwritten_area` 相同，但调用 `enhanced_verify_pattern()`

### 3.4 SPORTester.__init__ — 新增参数读取

**变更类型**：修改

在 `self.timeboard_port` 之后新增：

```python
self.spor_power_mode = getattr(config, 'spor_power_mode', 'timeboard')
self.spor_enhanced_iodepth = getattr(config, 'spor_enhanced_iodepth', 256)
self.spor_enhanced_bs = getattr(config, 'spor_enhanced_bs', '128k')
```

### 3.5 SPORTester.run() — 两处修改

**变更类型**：修改

1. **dry_run 日志**：增加模式信息
2. **Timeboard 初始化条件化**：仅在 `spor_power_mode == 'timeboard'` 且 phase==POWEROFF 时初始化 Timeboard 连接（manual/enhanced 模式不需要 Timeboard）

### 3.6 SPORTester.phase1_poweroff() — 两处修改

**变更类型**：修改

1. **Step 3 写入启动方式三模式**：
   ```python
   if self.spor_power_mode == 'enhanced':
       write_ok = self.start_enhanced_write_thread()
   else:
       write_ok = self.start_spor_write()
   ```

2. **Step 5 断电方式三模式分支**（替换原 Timeboard 专属断电块）：
   - `timeboard`：原有 Timeboard 连接 + `trigger_poweroff()` 逻辑（逐行保持不变）
   - `manual`：调用 `manual_surprise_poweroff(state)`
   - `enhanced`：校验 ipmi-host 存在 → 调用 `enhanced_ipmi_surprise_poweroff(state)` → 等待后台脚本触发断电

### 3.7 SPORTester.phase2_poweron() — 两处修改

**变更类型**：修改

1. **Step 4 写入位置解析三模式**：
   ```python
   if self.spor_power_mode == 'enhanced':
       last_lba = self.get_enhanced_write_position()
   else:
       last_lba = self.get_write_position()
   ```

2. **Step 5-6 Pattern 验证三模式**：
   - enhanced 模式使用 `_enhanced_verify_written_area()` / `_enhanced_verify_unwritten_area()`
   - timeboard/manual 模式使用原有 `_verify_written_area()` / `_verify_unwritten_area()`

### 3.8 parse_args() — 命令行参数扩展

**变更类型**：修改

| 变更点 | 内容 |
|-------|------|
| 新增 `--spor-power-mode` | default="timeboard", choices=["timeboard","manual","enhanced"] |
| 新增 `--spor-enhanced-iodepth` | type=int, default=256 |
| 新增 `--spor-enhanced-bs` | default="128k" |
| epilog 示例 | 新增 3 条 SPOR 三模式示例 |

### 3.9 main() — TestConfig 构造与日志

**变更类型**：修改

- `TestConfig(...)` 构造新增 3 个参数传递：`spor_power_mode`、`spor_enhanced_iodepth`、`spor_enhanced_bs`
- SPOR 配置日志输出增强：enhanced 模式时额外打印 iodepth、bs、IPMI 地址

### 3.10 GUI 上位机 — _build_spor_tab()

**变更类型**：修改

| 变更点 | 内容 |
|-------|------|
| 新增"断电方式"Combobox | values=["timeboard","manual","enhanced"], default="timeboard" |
| 新增 enhanced 专属控件 | "写入队列深度" Spinbox（default=256）、"写入块大小" Entry（default="128k"） |
| Timeboard 串口控件改造 | 保存为 `self._spor_timeboard_entry` 引用，支持模式切换时禁用/启用 |
| 新增 `_on_spor_power_mode_change()` 方法 | 模式切换时：enhanced 控件仅 enhanced 模式启用；Timeboard 串口仅 timeboard 模式启用 |
| 初始状态调用 | 标签页构建末尾调用 `_on_spor_power_mode_change()` 设置初始控件状态 |

### 3.11 GUI 上位机 — _build_cmd()

**变更类型**：修改

SPOR 参数拼接部分新增：
- `--spor-power-mode` 传递
- enhanced 模式时传递 `--spor-enhanced-iodepth`、`--spor-enhanced-bs`

### 3.12 版本号与文件头

**变更类型**：修改

- `SCRIPT_VERSION` 从 `"1.9.0"` 升级为 `"1.9.1"`
- 文件头 SPOR 测试项描述更新为"支持 timeboard/manual/enhanced 三种断电方式"
- 文件头用法示例新增 SPOR enhanced 模式命令

---

## 4. 兼容性说明

### 4.1 向后兼容

- **默认行为不变**：`--spor-power-mode` 默认值为 `timeboard`，不传该参数时行为与 v1.9.0 完全一致
- **timeboard 模式代码不变**：Timeboard 断电逻辑、iolog 解析、验证逻辑逐行保持原样，仅通过 if/elif/else 分支与 manual/enhanced 模式隔离
- **新参数有默认值**：所有新增命令行参数和 TestConfig 字段均有合理默认值
- **其他测试项不受影响**：固件、SMART、容量、性能、Normal Power Cycle、OSINT、读写等测试项的代码完全未改动

### 4.2 状态文件兼容

- enhanced/manual 模式新增的 state 字段（`poweroff_mode`）使用直接赋值方式
- 旧状态文件（timeboard 模式产生的）可正常加载
- 建议不同模式使用不同的 `--spor-state-file` 路径避免状态混淆

### 4.3 Python 版本兼容

- 不使用 Python 3.10+ 专属语法
- `threading` 为标准库，无需额外安装
- dataclass、typing 用法保持与现有代码一致

---

## 5. 关键设计约束验证

### 5.1 enhanced 模式是真正的"意外断电"

已通过代码审查确认 `enhanced_ipmi_surprise_poweroff()` 方法中的 IPMI 后台脚本仅包含：

```bash
sleep <delay>
ipmitool -H <host> -U <user> -P <pass> -I lanplus chassis power off
sleep <off_interval>
ipmitool -H <host> -U <user> -P <pass> -I lanplus chassis power on
```

**不包含** `shutdown` 命令，**不执行** `os.sync()`。这与 Normal Power Cycle 的 enhanced 模式（先 `nvme disconnect` 再 `shutdown -h now` 再 IPMI 断电）有本质区别——SPOR 是直接切断电源，不经过任何 OS 关机流程。

### 5.2 timeboard 模式原始代码完整性

已通过静态检查确认以下 timeboard 模式原始方法和调用均保留未改：
- `self.timeboard.trigger_poweroff(self.poweroff_delay_ms)` ✓
- `self.start_spor_write()` ✓
- `self.get_write_position()` ✓
- `self._verify_written_area(last_lba)` ✓
- `self._verify_unwritten_area(last_lba)` ✓

---

## 6. 已知限制

1. **enhanced 模式需要 IPMI**：enhanced 模式通过 IPMI 实现自动化意外断电，测试机必须配备 IPMI BMC 且网络可达。无 IPMI 时可使用 manual 模式。
2. **IPMI 直接断电影响系统盘**：enhanced 模式直接 `chassis power off` 会同时切断测试机系统盘电源，可能导致系统盘文件系统不一致。建议使用独立测试机或系统盘为临时文件系统。状态文件使用原子写入（临时文件 + fsync + rename）降低损坏风险。
3. **线程进度追踪依赖 /proc**：enhanced 模式的写入进度通过读取 `/proc/<pid>/io` 实现，仅在 Linux 上可用。掉电时进度可能未更新到最新值，已提供 fallback 到 iolog 解析。
4. **fio verify 参数版本差异**：`--verify=pattern` 在不同 fio 版本中语法可能有差异，已提供 JSON 解析失败时的文本匹配 fallback。
5. **manual 模式依赖人工操作**：manual 模式需要用户在提示时间内手动切断电源，若 fio 写入完成后才断电则失去"意外"效果。建议 `--spor-delay` 设置为较短时间（如 3-5 秒）。
6. **本电脑不负责测试**：所有代码修改在 Windows 本机完成，未在 Linux 目标环境执行实际测试。首次使用前建议小规模验证（`--spor-cycles 2`）。

---

## 7. 文件清单

| 文件 | 说明 |
|------|------|
| `ssd_test_v1.9.1.py` | 修改后的完整脚本（v1.9.0 → v1.9.1） |
| `ssd_test_v1.9.0.py` | 上一版本脚本（保留未改动，可用于对比和回滚） |
| `ssd_test_v1.8.5.py` | 原始脚本（保留未改动） |
| `代码修改说明_SPOR_v1.9.1.md` | 本文档 |
| `环境配置与快速使用指南_SPOR_v1.9.1.md` | 环境配置与快速使用文档 |
| `项目执行计划_SPOR_三模式改造_v1.0.md` | 项目执行计划 |
