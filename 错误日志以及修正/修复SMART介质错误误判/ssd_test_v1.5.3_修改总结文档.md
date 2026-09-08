# SSD 自动化测试脚本 v1.5.3 修改总结文档

> 基于版本：ssd_test_v1.5.2 → ssd_test_v1.5.3
> 修改日期：2026-09-08
> 测试平台：Linux Ubuntu
> 脚本语言：Python 3

---

## 一、修改概述

本次修改针对用户反馈的三个问题，对 `ssd_test_v1.5.2.py` 进行了三项改造：

| 序号 | 修改项 | 问题类型 | 影响范围 |
|------|--------|----------|----------|
| 1 | 介质错误 WARNING 改为友好提示 | 日志误报 | SmartTester 类 |
| 2 | SMART 日志格式改造（完整设备信息+SMART数据） | 日志格式 | SmartTester 类 |
| 3 | GUI 上位机只显示简洁摘要行 | 显示格式 | SSDTestGUI 类 + main() |

---

## 二、修改一：介质错误 WARNING 改为友好提示

### 2.1 问题描述

用户 SSD 的 NVMe SMART 显示 `Media and Data Integrity Errors: 5,353`（历史累积错误，R/W 前后无变化）。脚本原始逻辑在初始 SMART 检查时，介质错误 > 0 即输出 WARNING：

```
[WARNING] 初始 SMART 核心项存在问题: ['存在介质错误: 5353']
```

该 WARNING 会让用户误以为测试存在异常，但实际上 5353 是历史累积介质错误，R/W 后无新增，不影响测试结果。

### 2.2 修改方案

修改 `SmartTester.run()` 方法中初始 SMART 核心项检查的逻辑，区分两种情况：

- **仅介质错误 > 0**（无温度/可用备件等严重问题）：输出 INFO 级别友好提示，说明这是历史累积错误，R/W 后将检查是否新增
- **存在温度异常/可用备件过低等严重问题**：仍输出 WARNING

### 2.3 代码位置

文件：`ssd_test.py`
类：`SmartTester`
方法：`run()`
修改行：约第 923-934 行

### 2.4 修改前后对比

**修改前：**
```python
core_ok, core_result = self.check_smart_core_items(smart_before)
result.details["core_check_before"] = core_result
if not core_ok:
    self.log.warning(f"初始 SMART 核心项存在问题: {core_result['issues']}")
```

**修改后：**
```python
core_ok, core_result = self.check_smart_core_items(smart_before)
result.details["core_check_before"] = core_result
if not core_ok:
    # 区分：仅介质错误 -> 友好提示（历史累积，不影响初始判定）；
    # 温度/可用备件等严重问题 -> WARNING
    only_media = all("介质错误" in issue for issue in core_result["issues"])
    media_err = smart_before.get("media_errors", 0)
    if only_media and media_err > 0:
        self.log.info(f"检测到历史累积介质错误: {media_err}（R/W 后将检查是否新增，历史值不影响测试结果）")
    else:
        self.log.warning(f"初始 SMART 核心项存在问题: {core_result['issues']}")
```

### 2.5 日志输出对比

**修改前：**
```
[WARNING] 初始 SMART 核心项存在问题: ['存在介质错误: 5353']
```

**修改后：**
```
[INFO] 检测到历史累积介质错误: 5353（R/W 后将检查是否新增，历史值不影响测试结果）
```

---

## 三、修改二：SMART 日志格式改造

### 3.1 问题描述

用户提供的 `SMART设备智能健康信息.md` 文档显示，期望 SMART 测试的日志输出类似 `smartctl -a` 的完整格式，包含两段：

- `=== START OF INFORMATION SECTION ===`：设备基本信息（型号、序列号、固件版本、PCI Vendor ID、NVMe Version、Namespace Size、LBA Size、Power States、LBA Sizes 等）
- `=== START OF SMART DATA SECTION ===`：SMART/Health 完整数据（Critical Warning、Temperature、Available Spare、Percentage Used、Data Units Read/Written、Power Cycles、Power On Hours、Unsafe Shutdowns、Media and Data Integrity Errors、Error Information Log Entries、Temperature Sensors 等）

原始脚本的 SMART 日志只输出简化的键值对（如"获取初始 SMART 信息..."、"执行基本 R/W 操作..."），缺少完整的设备信息和 SMART 数据。

### 3.2 修改方案

在 `SmartTester` 类中新增 `print_full_smart_info()` 方法，通过运行 `smartctl -a <device>` 获取完整输出，并按用户指定格式逐行写入日志。

在 `SmartTester.run()` 方法中，获取初始 SMART 信息后和 R/W 后重新获取 SMART 信息后，各调用一次 `print_full_smart_info()`。

### 3.3 新增方法

```python
def print_full_smart_info(self, label: str = "", section: str = "all"):
    """输出设备信息和/或 SMART 数据（smartctl 格式，参考测试项目文档）。

    Args:
        label: 标题标签
        section: "all"=设备信息+SMART数据, "information"=仅设备信息,
                 "smart"=仅SMART数据
    """
```

### 3.4 调用位置

1. **R/W 前**（初始 SMART 信息获取后）：
   ```python
   self.print_full_smart_info("初始 SMART 信息（R/W 前）")
   ```

2. **R/W 后**（重新获取 SMART 信息后）：
   ```python
   self.print_full_smart_info("R/W 后 SMART 信息")
   ```

### 3.5 输出格式示例

```
----- 初始 SMART 信息（R/W 前） -----
=== START OF INFORMATION SECTION ===
Model Number:                       UBPKI04500HCNT1-HHH-UGN
Serial Number:                      64400003
Firmware Version:                   1.A.7.O
PCI Vendor/Subsystem ID:            0x1cc2
IEEE OUI Identifier:                0x000000
Controller ID:                      0
NVMe Version:                       1.4
Number of Namespaces:               1
Namespace 1 Size/Capacity:          500,107,862,016 [500 GB]
Namespace 1 Formatted LBA Size:     512
Local Time is:                      Tue Sep  8 15:23:25 2026 CST
Firmware Updates (0x0e):            7 Slots
...
Supported Power States
St Op     Max   Active     Idle   RL RT WL WT  Ent_Lat  Ex_Lat
 0 +     3.50W       -        -    0  0  0  0        5       5
 1 +     3.30W       -        -    1  1  1  1       50     100
...
Supported LBA Sizes (NSID 0x1)
Id Fmt  Data  Metadt  Rel_Perf
 0 +     512       0         0

=== START OF SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

SMART/Health Information (NVMe Log 0x02)
Critical Warning:                   0x00
Temperature:                        32 Celsius
Available Spare:                    85%
Available Spare Threshold:          10%
Percentage Used:                    19%
Data Units Read:                    339,434,932 [173 TB]
Data Units Written:                 334,790,803 [171 TB]
Host Read Commands:                 425,965,080
Host Write Commands:                346,174,646
Controller Busy Time:               74
Power Cycles:                       136
Power On Hours:                     117
Unsafe Shutdowns:                   37
Media and Data Integrity Errors:    5,353
Error Information Log Entries:      1
Warning  Comp. Temperature Time:    0
Critical Comp. Temperature Time:    0
Temperature Sensor 1:               46 Celsius
Temperature Sensor 2:               32 Celsius
Temperature Sensor 3:               0 Celsius
Temperature Sensor 4:               0 Celsius

Error Information (NVMe Log 0x01, 16 of 64 entries)
No Errors Logged
```

### 3.6 依赖工具

- `smartctl`（smartmontools 包）：用于获取完整设备信息和 SMART 数据
- 安装命令：`sudo apt install smartmontools`

---

## 四、修改三：GUI 上位机只显示简洁摘要行

### 4.1 问题描述

用户要求 GUI 上位机的日志显示区域只显示简洁的测试结果摘要行，不显示详细的 fio 输出、SMART 完整数据等冗长信息。

用户提供的实例格式：
```
08:20:56 - [SSD_ST_000]INIT,Status:PASS,TestTime:65 S==========
08:22:19 - [SSD_ST_001]PERFORMANCE_TEST,Status:PASS,TestTime:73 S==========
08:25:07 - [SSD_ST_002]POWER_CYCLE_TEST,Status:PASS,TestTime:167 S==========
```

格式要素：`HH:MM:SS - [SSD_ST_NNN]TEST_NAME,Status:PASS/FAIL,TestTime:XX S==========`

### 4.2 修改方案

#### 4.2.1 核心脚本输出标准摘要行

在 `main()` 函数的测试执行循环中，每个测试项完成后输出一行标准格式的摘要，用 `[SUMMARY]` 前缀标记，方便 GUI 识别过滤。

摘要行格式：
```
[SUMMARY] HH:MM:SS - [SSD_ST_NNN]TEST_ITEM_NAME,Status:PASS/FAIL,TestTime:NN S==========
```

#### 4.2.2 测试项编号与英文名称映射

| 测试项标识 | 样例编号 | 英文名称 |
|-----------|---------|---------|
| capacity | SSD_ST_001 | CAPACITY_TEST |
| smart | SSD_ST_002 | SMART_TEST |
| fw | SSD_ST_003 | FW_TEST |
| perf | SSD_ST_004 | PERFORMANCE_TEST |
| rw | SSD_ST_005 | RW_TEST |
| powercycle | SSD_ST_006 | POWER_CYCLE_TEST |
| spor | SSD_ST_007 | SPOR_TEST |
| osint | SSD_ST_008 | OSINT_TEST |

#### 4.2.3 GUI 只显示摘要行

修改 `SSDTestGUI._read_output()` 方法：
- 只显示 `[SUMMARY]` 开头的行（去掉 `[SUMMARY] ` 前缀）
- 其他详细输出不显示在 GUI 中（但仍完整写入日志文件）
- 根据 `Status:PASS/FAIL/ERROR` 决定摘要行颜色（PASS=绿色，FAIL/ERROR=红色）
- 保留进度条解析功能（从 `[x/y] 开始测试` 行解析进度）

#### 4.2.4 GUI 其他改动

- 日志区域标题改为"测试结果摘要（仅显示摘要行，详细日志见日志文件）"
- 新增"查看详细日志"按钮，点击打开最新的日志文件
- 开始/完成信息改为简洁单行格式

### 4.3 核心代码修改

#### 4.3.1 main() 中输出摘要行

```python
# 测试项编号和英文名称映射
item_enum = {
    TEST_CAPACITY: ("SSD_ST_001", "CAPACITY_TEST"),
    TEST_SMART: ("SSD_ST_002", "SMART_TEST"),
    TEST_FW: ("SSD_ST_003", "FW_TEST"),
    TEST_PERF: ("SSD_ST_004", "PERFORMANCE_TEST"),
    TEST_RW: ("SSD_ST_005", "RW_TEST"),
    TEST_POWERCYCLE: ("SSD_ST_006", "POWER_CYCLE_TEST"),
    TEST_SPOR: ("SSD_ST_007", "SPOR_TEST"),
    TEST_OSINT: ("SSD_ST_008", "OSINT_TEST"),
}.get(item, (f"SSD_ST_{idx:03d}", item.upper()))

# 输出标准摘要行
current_time = datetime.now().strftime("%H:%M:%S")
test_time_sec = int(round(result.duration_sec))
summary_line = (f"[SUMMARY] {current_time} - [{item_enum[0]}]{item_enum[1]},"
                f"Status:{status_icon},TestTime:{test_time_sec} S"
                + "=" * 10)
logger.info(summary_line)
```

#### 4.3.2 GUI _read_output() 过滤显示

```python
def _read_output(self):
    """后台线程：读取子进程输出，GUI 只显示标准摘要行。"""
    for line in self.test_process.stdout:
        line = line.rstrip()
        if not line:
            continue

        # 解析进度（不显示，只更新进度条）
        match = re.search(r"\[(\d+)/(\d+)\]\s*开始测试[:：]\s*(.+)", line)
        if match:
            # ... 更新进度条 ...

        # 只显示标准摘要行 [SUMMARY]
        if line.startswith("[SUMMARY]"):
            summary_line = line[len("[SUMMARY] "):]
            tag = "info"
            if "Status:PASS" in summary_line:
                tag = "success"
            elif "Status:FAIL" in summary_line or "Status:ERROR" in summary_line:
                tag = "error"
            self.root.after(0, lambda l=summary_line, t=tag: self._append_log(l, t))
```

#### 4.3.3 新增 _open_log_file() 方法

```python
def _open_log_file(self):
    """打开最新的详细日志文件。"""
    log_dir = self.config_vars["log_dir"].get()
    if not os.path.exists(log_dir):
        messagebox.showinfo("提示", f"日志目录不存在: {log_dir}")
        return
    try:
        log_files = [f for f in os.listdir(log_dir) if f.endswith(".log")]
        if not log_files:
            messagebox.showinfo("提示", "日志目录中没有 .log 文件")
            return
        log_files.sort(reverse=True)
        latest_log = os.path.join(log_dir, log_files[0])
        subprocess.Popen(["xdg-open", latest_log])
    except Exception as e:
        messagebox.showerror("错误", f"打开日志文件失败: {e}")
```

### 4.4 GUI 显示效果示例

```
15:19:40 - 测试开始 | 设备: /dev/nvme0n1 | 测试项: smart | 详细日志见日志文件
15:19:45 - [SSD_ST_002]SMART_TEST,Status:PASS,TestTime:5 S==========
15:19:45 - 全部测试完成 - 结果: PASS
```

### 4.5 详细日志查看方式

GUI 中不显示详细日志，但详细日志仍完整写入日志文件。查看方式：
1. 点击 GUI 底部的「查看详细日志」按钮，自动打开最新日志文件
2. 或直接打开日志目录下的 `ssd_test_YYYYMMDD_HHMMSS.log` 文件

---

## 五、验证方式

### 5.1 介质错误 WARNING 验证

```bash
sudo python3 ssd_test_v1.5.3.py -d /dev/nvme0n1 -t smart -y
```

检查日志中初始 SMART 检查部分，应输出：
```
[INFO] 检测到历史累积介质错误: 5353（R/W 后将检查是否新增，历史值不影响测试结果）
```
不应再出现 `[WARNING] 初始 SMART 核心项存在问题`。

### 5.2 SMART 日志格式验证

运行 SMART 测试后，检查日志中应包含：
- `----- 初始 SMART 信息（R/W 前） -----`
- `=== START OF INFORMATION SECTION ===` 及设备信息
- `=== START OF SMART DATA SECTION ===` 及 SMART 完整数据
- `----- R/W 后 SMART 信息 -----` 及 R/W 后完整信息

### 5.3 GUI 摘要显示验证

```bash
sudo python3 ssd_test_v1.5.3.py --gui
```

在 GUI 中选择测试项并启动，检查日志显示区域：
- 只显示 `HH:MM:SS - [SSD_ST_XXX]ITEM,Status:PASS,TestTime:NN S==========` 格式的摘要行
- 不显示 fio 输出、SMART 完整数据等详细信息
- 点击「查看详细日志」按钮可打开完整日志文件

---

## 六、版本信息

- 脚本版本：v1.5.3
- 基于版本：v1.5.2
- 修改文件：`ssd_test.py`
- 代码行数：约 5900 行
- 依赖：Python 3 标准库 + nvme-cli + smartmontools + fio + ipmitool + python3-tk
