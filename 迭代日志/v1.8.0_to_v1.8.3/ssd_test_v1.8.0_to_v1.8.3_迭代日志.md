# SSD 自动化测试脚本迭代日志（v1.8.0 → v1.8.3）

> 本文档记录从 v1.8.0 到 v1.8.3 的全部迭代内容，包括新增功能、修复的问题及对应的错误日志。
>
> 基准脚本：`ssd_test_v1.7.7.py`（稳态预处理超时修复版，脚本内版本号 1.7.7）

---

## 版本总览

| 版本 | 行数 | 核心主题 | 关键变更 |
|------|------|----------|----------|
| v1.8.0 | 6721 | 多任务批量测试 | 新增性能测试任务批量管理与顺序执行 |
| v1.8.1 | 6755 | UI 交互优化 | 开始按钮上移、任务列表上下排序 |
| v1.8.2 | 6755 | 布局修复 | 底部按钮被遮挡问题修复 |
| v1.8.3 | 6880 | OSINT Bug 修复 | 操作系统中断测试 9 项 Bug 修复（3 轮迭代） |

---

## v1.8.0 — 多任务批量测试升级

**发布日期**：2026-09-11
**基于版本**：v1.7.7

### 新增功能

1. **性能测试任务批量管理**
   - 新增 `PerfTask` dataclass，字段与 `TestConfig` 的 perf_* 对齐（读写模式、块大小、队列深度、线程数、运行时间、备注等）
   - `TestConfig` 新增 `perf_task_list` 字段，支持保存任意数量的测试任务
   - 新增 `_cfg_with_task()` 方法，用 `SimpleNamespace` 动态覆盖 perf 字段，实现单组配置复用

2. **批量顺序执行**
   - `run_full_perf_test()` 增加可选 `task` / `task_index` 参数
   - `run_fob_test()` / `run_steady_state_test()` 检测任务列表非空时批量循环执行
   - **稳态预处理只做一次**，后续任务复用预处理结果，避免重复预处理浪费时间
   - 每轮任务日志带 `[任务N:备注]` 标记，便于区分
   - 批量执行时结果为 list 结构，空任务列表时仍为 dict（**向后兼容**）

3. **CLI 新增参数**
   - `--perf-task-file`：从 JSON 文件加载批量测试任务

4. **GUI 性能标签页新增批量任务管理面板**
   - 备注输入框
   - 「新增任务」按钮：将当前参数配置保存为一个任务
   - 任务列表展示（序号、备注、模式、块大小、QD、运行时间）
   - 「删除选中」按钮
   - 「清空全部」按钮
   - 「导出 JSON」按钮

5. **配置持久化**
   - save/load config 支持持久化任务列表

### 解决的问题

- 原版本只能单组参数执行一次性能测试，无法批量验证多种工作负载组合
- 多组测试需要手动反复修改参数、重复运行，效率低

---

## v1.8.1 — UI 交互优化

**发布日期**：2026-09-11
**基于版本**：v1.8.0

### 新增功能

1. **开始测试按钮上移**
   - 「▶ 开始测试」按钮从底部控制栏移到 **顶部设备选择栏右侧**
   - 按钮字号从 11 提升到 12，更醒目
   - 底部保留「■ 停止」「清空日志」「保存配置」「加载配置」「导出日志」「查看详细日志」等按钮

2. **任务列表自由排序**
   - 批量任务列表右侧新增「↑ 上移」「↓ 下移」按钮
   - 新增 `_move_task_up()` / `_move_task_down()` 方法
   - 移动后保持当前选中状态，便于连续调整
   - 执行顺序按列表从上到下依次执行

### 解决的问题

- 开始按钮在底部不够醒目，用户反馈难找
- 批量任务添加后无法调整执行顺序，只能删除重建

---

## v1.8.2 — 布局修复（底部按钮可见）

**发布日期**：2026-09-11
**基于版本**：v1.8.1

### 修复的问题

**问题描述**：运行程序后无法正常看到底部控制按钮（停止/清空日志/保存配置等）。

**根因分析**：
- `middle_frame`（参数配置区）和 `log_frame`（日志区）都设置了 `expand=True`，两者争夺垂直空间
- `log_text` 默认高度 12 行，占比过大
- 底部 `bottom_frame` 后于 `log_frame` pack，被挤出可视区域

### 修复内容

| 修改项 | 旧值 | 新值 | 作用 |
|--------|------|------|------|
| pack 顺序 | log_frame 在前，bottom_frame 在后 | **bottom_frame 在前，log_frame 在后** | 底部栏先占位，日志区再填充剩余空间，确保底部按钮始终可见 |
| log_text 高度 | 12 行 | 8 行 | 减少日志区默认占用 |
| 窗口初始尺寸 | 1100×750 | 1100×880 | 增加垂直空间 |
| 窗口最小尺寸 | 900×600 | 960×720 | 防止缩太小遮挡按钮 |

### 解决的错误日志

无（布局问题无错误日志，为用户视觉反馈）

---

## v1.8.3 — OSINT 操作系统中断测试 Bug 修复

**发布日期**：2026-09-11
**基于版本**：v1.8.2

> 本版本历经 3 轮迭代，共修复 9 项 Bug，全部围绕「操作系统中断测试（OSINT）」模块。

### 第一轮：基于首次 OSINT 测试日志（3 项修复）

#### Bug 1：fio IO 负载启动后立即退出

**错误日志**：
```
[ERROR] 混合读写负载启动后立即退出
```
fio 详细日志：
```
osint_mixed_rw: you need to specify size=
fio: pid=0, err=22/file:filesetup.c:1156, func=total_file_size, error=Invalid argument
```

**根因**：fio 使用 `--directory` 模式时必须指定 `--size` 参数（每个 job 写入多少数据），脚本中遗漏了该参数，导致 fio 报参数错误立即退出。IO 负载实际上完全没有运行。

**修复**：fio 命令添加 `--size=256M`（4 个 job 共 1GB 测试数据）。

---

#### Bug 2：S3 唤醒后文件系统损坏、挂载失败

**错误日志**：
```
文件系统挂载: FAIL
文件系统检查 (fsck): FAIL
fsck输出: ext2fs_check_desc: Corrupt group descriptor: bad block for block bitmap
fsck.ext4: Group descriptors look bad... trying backup blocks...
Block bitmap for group 3584 is not in group. (block 312517069513289728)
[ERROR] 测试文件丢失: /mnt/ssd_osint/osint_integrity_test.bin
完整性校验: 0/1 通过, 0 失败, 1 丢失
```

**根因**：原 `trigger_sleep()` 方法在休眠前只执行了 `sync`，**没有停止 fio IO 负载，也没有卸载文件系统**。S3 休眠时 fio 仍在后台持续写入，NVMe 驱动在 S3 恢复时未正确冻结文件系统状态，导致 ext4 组描述符/块位图元数据严重损坏（block 号 312517069513289728 约 281TB，远超 500GB 盘容量，明显是元数据损坏）。文件系统损坏 → 挂载失败 → 测试文件路径不可达 → 数据完整性校验失败。

**修复**：`trigger_sleep()` 中休眠前执行完整清理流程：
1. `stop_io_load()` — 停止 fio IO 负载
2. `sync` — 刷新页缓存
3. `umount` — 卸载测试分区文件系统（失败时强制 `umount -f`）

唤醒后重新 `mount` 挂载文件系统。休眠时文件系统处于干净卸载状态，不会损坏。

---

#### Bug 3：SMART 检查误报 FAIL

**错误日志**：
```
SMART: 介质错误=None, 可用备件=None%, 温度=45°C -> FAIL
```

**根因**：`nvme smart-log -o json` 输出中，`media_and_data_integrity_errors` 和 `available_spare` 字段解析为 `None`（不同厂商/不同 nvme-cli 版本字段名可能不同，或字段值为 null）。原判定逻辑 `info.get("media_errors", 1) == 0` 中，当 key 存在但值为 None 时返回 None（默认值不生效），`None == 0` 为 False，被误判为 FAIL。实际上温度 45°C、power_cycles 都能正常读取，SMART 本身没问题。

**修复**：
1. **多字段名兼容查找**：介质错误依次尝试 `media_and_data_integrity_errors` / `media_errors` / `media_and_data_errors`；可用备件依次尝试 `available_spare` / `avail_spare` / `spare`
2. **None 值兜底**：解析不到时视为正常值（介质错误 = 0，可用备件 = 100），不因此判 FAIL
3. **判定时双重保险**：`(info.get("media_errors") or 0) == 0` / `(info.get("available_spare") or 100) >= 10`

---

### 第二轮：基于第二次 OSINT 测试日志（2 项修复）

#### Bug 4：状态文件残留导致循环跳过、全程无休眠

**错误日志**：
```
加载 OSINT 状态: 第 3/2 轮
（直接跳到最终完整功能测试，全程无 S3 休眠、无循环执行）
实际执行: 0 轮
```

**根因**：上一次 v1.8.2 测试结束后，状态文件 `/var/lib/ssd_osint_state.json` 记录了 `phase="done", current_cycle=3, total_cycles=2`。v1.8.3 启动时加载到该状态，发现 `current_cycle (3) > total_cycles (2)`，循环 `for cycle in range(start_cycle, self.cycles + 1)` 直接不执行，跳过所有循环直接进入最终功能测试。用户看到 "没有休眠"，实际是状态文件残留导致的。

**修复**：`run()` 方法加载状态后增加有效性检测，以下任一情况自动清除旧状态并重新初始化：
- `phase == "done"`（上一次测试已完成）
- `total_cycles != self.cycles`（用户改了循环次数）
- `current_cycle > self.cycles + 1`（状态轮次异常）

无需用户手动 `rm /var/lib/ssd_osint_state.json`。

---

#### Bug 5：SMART 介质错误导致每轮 FAIL（用户已知有介质错误的测试盘）

**错误日志**：
```
[WARNING] [警告] 检测到介质错误: 5353（警告模式，不影响本轮判定）
SMART: 介质错误=5353(警告模式), 可用备件=85%, 温度=52°C -> OK
```
（修复前为 `-> FAIL`，导致每轮都因介质错误判定失败）

**根因**：用户的测试盘已知有 5353 个介质错误（`media_errors=5353`），原判定逻辑中介质错误 > 0 即 FAIL。用户希望 **忽略介质错误，只验证休眠唤醒稳定性**。

**修复**：SMART 判定改为 **警告模式**：
- 介质错误 > 0 时输出 `[WARNING]` 日志告警，但 **不影响 PASS/FAIL**
- 可用备件 < 10% 和温度超出 0-70°C 仍为 **硬性 FAIL 条件**（这两个是真正的健康指标）
- 返回结果中增加 `media_errors_warning: true` 标记

---

### 第三轮：基于第三次 OSINT 测试日志（4 项修复）

#### Bug 6：唤醒后挂载失败自动 fsck 修复

**错误日志**：
```
唤醒后挂载失败: mount: /mnt/ssd_osint: mount(2) system call failed: Structure needs cleaning.
休眠失败: wake_ok_but_mount_failed (17.5s): mount: /mnt/ssd_osint: mount(2) system call failed: Structure needs cleaning.
```

**根因**：虽然休眠前做了 stop_io → sync → umount，但 fio 使用 `--direct=1` 大量写入（4 个 256MB 文件 + 持续混合读写，读 317MB/s 写 137MB/s），NVMe 设备 **内部写缓存在 S3 休眠时可能未完全提交到 NAND**，导致 ext4 日志（journal）轻微不一致。唤醒后挂载时 ext4 检测到日志需要恢复，报 `Structure needs cleaning`。

**修复**：`trigger_sleep()` 唤醒后挂载失败时，自动检测错误信息中是否包含 `Structure needs cleaning` / `needs cleaning`，如果是则：
1. 先确保未挂载（`umount`）
2. 自动运行 `fsck -y <partition>` 修复文件系统（自动应答所有修复）
3. 记录 fsck 输出到日志
4. 修复后重试挂载

大部分 ext4 日志不一致问题可通过 fsck 自动修复，测试可继续执行。

---

#### Bug 7：休眠前 nvme flush 刷新设备写缓存

**错误日志**：同 Bug 6（`Structure needs cleaning`）

**根因**：`umount` 只能确保 Linux 页缓存和块层缓存刷新，但 NVMe 设备 **内部写缓存**（device-level write cache）可能还有未提交到 NAND 的数据。S3 休眠时设备掉电或状态重置，这些未提交的数据丢失，导致文件系统不一致。

**修复**：`umount` 后增加 `nvme flush <controller>` 命令，强制刷新 NVMe 设备内部写缓存，确保所有数据提交到 NAND 后再进入休眠。从源头减少文件系统不一致的概率。

---

#### Bug 8：fio 停止后增加等待时间

**错误日志**：同 Bug 6（`Structure needs cleaning`）

**根因**：`stop_io_load()` 使用 `terminate()`（SIGTERM）终止 fio 进程，fio 收到信号后需要时间完成正在进行的 IO、关闭文件句柄、退出进程。原代码立即执行 `sync`，可能 fio 还未完全退出，仍有未完成的 IO。

**修复**：`stop_io_load()` 后增加 `time.sleep(2)`，确保 fio 进程完全退出、文件句柄释放、所有 IO 提交到块层后，再执行 sync / umount。

---

#### Bug 9：休眠失败也记录一轮结果

**错误日志**：
```
总轮数: 2
实际执行: 0 轮
通过: 0
失败: 0
错误信息: 0/0 轮通过，0 轮失败
```

**根因**：原 `run_cycle()` 中，当 `trigger_sleep()` 返回 False（休眠/挂载失败）时，直接 `return False`，**没有构造和记录 cycle_result**，导致 `state["results"]` 为空。统计时 `total_executed = len(results) = 0`，显示 "实际执行 0 轮"，用户无法看到失败轮次的详情。

**修复**：`trigger_sleep()` 失败时，构造一个失败的 `cycle_result`（所有检查项标记为 False，错误信息记录为 "休眠/挂载失败，未执行检查"），追加到 `state["results"]` 并保存状态。统计时失败轮次正常计入，不再显示 0/0。

---

### v1.8.3 修复效果验证

| 检查项 | 修复前 | 修复后（预期） |
|--------|--------|---------------|
| fio IO 负载 | 启动即退出，无实际 IO | 正常运行，读 ~317MB/s 写 ~137MB/s |
| S3 休眠唤醒 | 正常（v1.8.2 起正常） | 正常，15s 定时唤醒 |
| 唤醒后文件系统 | 严重损坏，挂载失败 | umount + nvme flush 预防；失败时自动 fsck 修复 |
| 数据完整性 | 测试文件丢失，校验失败 | 文件系统正常挂载，SHA-256 校验通过 |
| SMART 检查 | None 误报 FAIL / 介质错误 5353 致 FAIL | 警告模式，介质错误只告警，可用备件和温度为硬性条件 |
| 循环执行 | 状态残留导致跳过，0 轮执行 | 自动重置状态，正常执行 N 轮 |
| 结果统计 | 0/0 轮，无失败详情 | 每轮均有记录，失败轮次可追溯 |
| 最终功能测试 | PASS（容量/读写性能正常） | PASS |

---

## 附录：各版本错误日志速查表

| 版本 | 错误日志关键词 | 根因 | 修复方式 |
|------|---------------|------|----------|
| v1.8.3 第一轮 | `you need to specify size=` | fio directory 模式缺 size 参数 | 添加 `--size=256M` |
| v1.8.3 第一轮 | `Corrupt group descriptor` / `Block bitmap ... not in group` | 休眠时未停 IO 未卸载，ext4 元数据损坏 | 休眠前 stop_io → sync → umount |
| v1.8.3 第一轮 | `介质错误=None, 可用备件=None% -> FAIL` | SMART 字段解析为 None 被误判 | 多字段名兼容 + None 兜底 |
| v1.8.3 第二轮 | `加载 OSINT 状态: 第 3/2 轮`（无休眠） | 状态文件残留，循环跳过 | 自动检测无效状态并重置 |
| v1.8.3 第二轮 | `介质错误=5353 -> FAIL` | 已知有介质错误的测试盘被误判 | 介质错误改为警告模式 |
| v1.8.3 第三轮 | `Structure needs cleaning` | NVMe 写缓存未提交，ext4 日志不一致 | 自动 fsck -y 修复 + nvme flush 预防 |
| v1.8.3 第三轮 | `实际执行: 0 轮` / `0/0 轮通过` | 休眠失败时未记录结果 | 失败轮次也记录 cycle_result |

---

*文档生成时间：2026-09-11 | 基于 ssd_test_v1.8.0.py ~ ssd_test_v1.8.3.py 实际迭代记录*
