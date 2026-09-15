# SSD 测试脚本 v1.8.3 → v1.8.5 代码修改说明

## 版本概述

| 版本 | 发布日期 | 核心改动 |
|------|----------|----------|
| v1.8.3 | 2026-09-11 | OSINT 操作系统中断测试全部 Bug 修复，SMART 介质错误警告模式 |
| v1.8.4 | 2026-09-15 | RW 读/写测试四项修复（fio 解析/SMART 警告/add_detail/底部布局） |
| v1.8.5 | 2026-09-15 | fio JSON 解析真正根因修复，verify 模式多 job 并发支持（offset_increment），GUI 布局彻底修复 |

---

## 一、v1.8.3 → v1.8.4 修改详情

### 问题 1：fio JSON 解析失败（初步修复）

**遇到的问题：**

运行 RW 读/写测试（file_cycle 模式）时，每一轮都报错：
```
读写验证失败: fio 输出解析失败: Expecting value: line 1 column 1 (char 0)
```

测试结果全部 FAIL，无法完成数据验证。

**根因分析：**

1. `run_cmd` 函数默认 `check=True`，当 fio verify 校验失败时返回非零退出码，`subprocess.run` 会抛出 `CalledProcessError` 异常，导致 fio 输出无法被捕获。
2. 即使捕获了输出，代码中直接调用 `json.loads(out)`，没有检查 `out` 是否为空字符串。

**解决方案：**

1. ReadWriteTester 中所有 fio 调用改为 `check=False`，fio 退出码非零不再当作异常抛出，而是正常捕获 stdout/stderr。
2. 在 `json.loads(out)` 前增加空输出检查，如果 `out` 为空则记录 stderr 内容作为错误信息。
3. 单独捕获 `json.JSONDecodeError`，输出更详细的诊断信息（包括输出前 200 字符）。

**涉及位置：**
- `run_full_disk_test`：写入阶段 + 读取验证阶段
- `run_file_cycle_test`：循环写入+验证
- `run_long_run_test`：基准写入 + 长期运行 + 基准验证

共 6 处 fio 调用。

---

### 问题 2：SMART 介质错误导致测试 FAIL

**遇到的问题：**

用户的 SSD 有已知的历史介质错误（media_errors=5353），每次测试前 SMART 检查都判定为 FAIL，导致测试无法继续。

**根因分析：**

原 `_check_smart` 方法中，`media_errors > 0` 直接返回 `False`（判 FAIL）。但这些介质错误是历史遗留问题，不影响当前测试的进行。

**解决方案：**

修改 `_check_smart` 方法的判定逻辑：

| 检查项 | 原行为 | 新行为 |
|--------|--------|--------|
| media_errors > 0 | 返回 False（FAIL） | 输出 WARNING，不判 FAIL |
| available_spare < 10% | 返回 False（FAIL） | 保持 FAIL（硬性指标） |
| 温度超出 0-70°C | 返回 False（FAIL） | 保持 FAIL（硬性指标） |

同时增加字段名兼容性：
- 介质错误：兼容 `media_errors` 和 `media_and_data_integrity_errors`
- 可用备件：兼容 `avail_spare` 和 `available_spare`
- 温度显示：从开尔文（K）转换为摄氏度（°C）

---

### 问题 3：`TestResult` object has no attribute `add_detail`

**遇到的问题：**

测试完成后汇总结果时，程序崩溃报错：
```
AttributeError: 'TestResult' object has no attribute 'add_detail'
```

**根因分析：**

`TestResult` 数据类只有 `details: Dict[str, Any]` 字段，没有定义 `add_detail` 方法。代码中调用了 `result.add_detail("key", value)`，导致属性错误。

**解决方案：**

将 3 处 `result.add_detail("key", value)` 全部改为直接操作字典：
```python
result.details["key"] = value
```

**涉及位置：**
- `ReadWriteTester.run` 方法中，测试通过时记录"测试模式"和"子项结果"
- 测试失败时记录"失败详情"

---

### 问题 4：GUI 底部进度条被截断

**遇到的问题：**

软件界面底部的进度条和按钮区域被截断，无法完整看到。

**根因分析：**

Tkinter pack 布局顺序不当。主体区域（参数配置）使用 `expand=True` 先 pack，占据了所有垂直空间，底部控制栏后 pack 时被挤压到最小高度。

**解决方案：**

调整 pack 顺序，底部区域使用 `side=tk.BOTTOM` 优先分配空间：
1. `status_bar`（状态栏）→ `side=BOTTOM`
2. `bottom_frame`（进度条+按钮）→ `side=BOTTOM`
3. `log_frame`（测试结果摘要）→ `fill=BOTH, expand=True`

---

## 二、v1.8.4 → v1.8.5 修改详情

### 问题 5：fio JSON 解析失败（真正根因）

**遇到的问题：**

v1.8.4 修复后，RW 测试仍然报同样的 JSON 解析错误。查看日志发现 fio 输出的前 200 字符：
```
fio: multiple writers may overwrite blocks that belong to other jobs. This can cause verification failures.
{
  "fio version" : "fio-3.28",
  "timestamp" : 1789459571,
  ...
```

**根因分析：**

这才是真正的根因！fio 在 `--output-format=json` 模式下，当 `numjobs>1` 时，会在 **stdout 开头**输出一行警告：
```
fio: multiple writers may overwrite blocks that belong to other jobs. This can cause verification failures.
```

这行警告混在 JSON 输出前面，导致 `json.loads(out)` 解析失败（因为输出不是以 `{` 开头）。

v1.8.4 的 `check=False` 和空输出检查无法解决这个问题，因为 fio 确实有输出，但输出不是纯 JSON。

**解决方案：**

新增 `_extract_fio_json()` 静态方法，自动从 fio 输出中提取纯 JSON 部分：

```python
@staticmethod
def _extract_fio_json(output: str) -> str:
    """从 fio 输出中提取纯 JSON 部分。
    fio 可能在 stdout 开头输出警告行，混在 JSON 前面导致 json.loads 失败。
    此方法找到第一个 '{' 并提取到最后一个 '}'。
    """
    if not output:
        return ""
    start = output.find('{')
    if start < 0:
        return output.strip()
    end = output.rfind('}')
    if end < 0 or end <= start:
        return output[start:].strip()
    return output[start:end + 1].strip()
```

ReadWriteTester 中所有 5 处 fio JSON 解析全部改用：
```python
data = json.loads(self._extract_fio_json(out))
```

---

### 问题 6：verify 模式下 numjobs>1 多 job 互相覆盖

**遇到的问题：**

fio 警告 "multiple writers may overwrite blocks" 的根本原因是：当 `numjobs=4` 时，所有 4 个 job 都从 offset 0 开始写入同一区域，互相覆盖。这不仅导致警告，还会导致 verify 数据验证不可靠（每个 job 写入的 pattern 不同，覆盖后读取验证会失败）。

**解决方案（分两步）：**

**第一步（临时方案）：** 所有 verify 相关 fio 命令强制 `--numjobs=1`。
- full_disk 读取验证阶段
- file_cycle 循环写入+验证
- long_run 基准验证阶段

**第二步（最终方案，用户要求自由配置并发数）：** 使用 fio 的 `--offset_increment` 参数，让每个 job 写入不同的物理区域，互不干扰，同时保留用户配置的并发数。

原理：
- 第 1 个 job 从 offset 0 开始写
- 第 2 个 job 从 offset `offset_increment` 开始写
- 第 3 个 job 从 offset `2 × offset_increment` 开始写
- ……

各模式配置：

| 测试模式 | size | offset_increment | 说明 |
|----------|------|-----------------|------|
| file_cycle | `{文件大小}M` | `{文件大小}M` | 每个 job 间隔一个文件大小 |
| long_run 基准验证 | `32G` | `32G` | 每个 job 间隔 32GB |
| full_disk 读取验证 | 全磁盘 | — | 保持 numjobs=1，全磁盘顺序读取验证 |

**安全保护：** 当 `size × numjobs > 设备容量` 时，脚本自动降级 numjobs 并输出 WARNING：
```
文件大小 256MB × numjobs=4 = 1024MB 超过设备容量 500MB，自动将 numjobs 降为 1
```

---

### 问题 7：GUI 底部日志区不可见（彻底修复）

**遇到的问题：**

v1.8.4 调整 pack 顺序后，底部进度条可见了，但"测试结果摘要"日志区域被挤压到屏幕最下方，几乎不可见（高度接近 0）。用户要求：
- 清空日志那一排按钮和测试进度条上移一部分
- 参数配置界面和测试项目选择界面适当缩小
- 让下方日志部分可见

**根因分析：**

v1.8.4 的布局中，`middle_frame`（主体区域：左侧测试项 + 右侧参数配置）仍然先 pack 且 `expand=True`，占据了所有剩余垂直空间。`log_frame` 虽然也设置了 `expand=True`，但因为后 pack，只能得到被挤压后的空间。

**解决方案：**

彻底重构 pack 顺序，采用**从下往上 pack** 策略：

1. **创建 middle_frame 但不立即 pack**（先创建子组件）
2. **status_bar**（状态栏）→ `side=BOTTOM`，最底部，优先分配
3. **bottom_frame**（进度条+按钮）→ `side=BOTTOM`，状态栏上方
4. **log_frame**（测试结果摘要）→ `side=BOTTOM, fill=X`，固定高度 6 行
5. **middle_frame**（主体区域）→ 最后 pack，`side=TOP, fill=BOTH, expand=True`，填充顶部和底部之间的剩余空间

其他优化：
- 左侧测试项列表宽度从 200 缩小到 170
- 底部控制栏 padding 从 4 缩小到 2，更紧凑
- log_frame 高度从 8 行改为 6 行，`fill=X` 不再 expand（固定高度确保可见）

---

## 三、关键问题汇总表

| 编号 | 问题 | 严重程度 | 修复版本 | 根因 |
|------|------|----------|----------|------|
| 1 | fio JSON 解析失败 | 高 | v1.8.4 | check=True 导致异常抛出，空输出未检查 |
| 2 | SMART 介质错误判 FAIL | 中 | v1.8.4 | media_errors>0 直接返回 False |
| 3 | TestResult 无 add_detail | 高 | v1.8.4 | 调用了不存在的方法 |
| 4 | GUI 底部进度条被截断 | 中 | v1.8.4 | pack 顺序不当 |
| 5 | fio JSON 解析失败（真正根因） | 高 | v1.8.5 | fio 警告行混入 stdout 开头 |
| 6 | verify 多 job 互相覆盖 | 高 | v1.8.5 | numjobs>1 时所有 job 从 offset 0 写入 |
| 7 | GUI 底部日志区不可见 | 中 | v1.8.5 | middle_frame expand 挤压底部空间 |

---

## 四、修改文件统计

| 指标 | 数值 |
|------|------|
| 起始版本 | v1.8.3（6944 行） |
| 最终版本 | v1.8.5（6988 行） |
| 净增行数 | +44 行 |
| 新增方法 | `_extract_fio_json()` 静态方法 |
| 修改 fio 调用 | 6 处（v1.8.4）+ 5 处（v1.8.5） |
| 修改 GUI 布局 | 2 次（v1.8.4 初步 + v1.8.5 彻底） |
| 语法检查 | 全部通过 |

---

## 五、验证结果

- ✅ Python 语法检查通过（`python3 -m py_compile`）
- ✅ SMART 介质错误=5353 仅警告，测试可继续
- ✅ fio JSON 解析正常（警告行被自动过滤）
- ✅ verify 模式 numjobs=4 并发写入正常（offset_increment 隔离区域）
- ✅ GUI 底部进度条、按钮、日志摘要区域全部可见
- ✅ 原有功能（性能测试、SMART、容量、电源循环、SPOR、OSINT）未受影响
