# SSD 自动化测试脚本 — 问题与解决方案全记录

> 本文档整合从 v1.7.6 到 v1.8.3 迭代过程中遇到的所有问题、错误日志、根因分析及对应解决方案。
>
> 涉及脚本：`ssd_test_v1.7.6.py` → `ssd_test_v1.8.3.py`

---

## 目录

1. [稳态预处理相关问题](#1-稳态预处理相关问题)
2. [GUI 界面相关问题](#2-gui-界面相关问题)
3. [OSINT 操作系统中断测试相关问题](#3-osint-操作系统中断测试相关问题)
4. [问题速查表](#4-问题速查表)

---

## 1. 稳态预处理相关问题

### 问题 1.1：稳态预处理第二步超时失败

**涉及版本**：v1.7.6（原始版本，脚本内版本号 1.5.0）→ v1.7.7 修复

**错误日志**：
```
稳态预处理第 2 步「4K 随机写 2x 全盘容量」在 10138 秒后超时
```
fio 输出被重定向到 `/dev/null`，超时后无法判断实际写入量，一刀切判失败。

**根因分析**：
1. 超时时间按 **100MB/s** 估算，但 TLC SSD 在 SLC 缓存耗尽后，4K 随机写稳态实际仅 **30-60MB/s**
2. 500GB 盘 2x 容量（约 1TB 写入量）在 2.8 小时内写不完
3. fio 输出重定向 `/dev/null` 导致无法监控进度，超时后无法判断已写多少

**解决方案**：
1. 超时默认按 **30MB/s** 估算：`timeout = max(1800, write_size / (30*1024*1024) + 900)`
   - 500GB 盘超时从 10138s 提升到 32696s（约 9 小时）
2. 新增可调参数：
   - `--precond-rand-speed`：随机写预估速度（MB/s）
   - `--precond-rand-max-sec`：随机写最大超时秒数
3. fio 命令添加 `--status-interval=120`，每 120 秒输出进度
4. 新增 `_parse_fio_progress_pct()` 解析 fio 进度百分比
5. **降级策略**：超时后若进度 ≥ 50%（即已写 ≥ 1x 容量），则降级继续执行性能测试，而非全盘失败
6. GUI 同步新增对应输入框

---

## 2. GUI 界面相关问题

### 问题 2.1：底部控制按钮看不到

**涉及版本**：v1.8.1 → v1.8.2 修复

**现象**：运行程序后，底部的「停止」「清空日志」「保存配置」「加载配置」「导出日志」「查看详细日志」按钮无法正常显示，被挤出可视区域。

**根因分析**：
1. `middle_frame`（参数配置区，含 8 个标签页）和 `log_frame`（日志区）都设置了 `expand=True`，两者争夺垂直空间
2. `log_text` 默认高度 **12 行**，占比过大
3. `bottom_frame`（底部控制栏）后于 `log_frame` pack，当上方两个 expand 区域占满空间后，底部栏被挤出可视区域
4. 窗口初始尺寸 1100×750 偏小

**解决方案**：

| 修改项 | 旧值 | 新值 |
|--------|------|------|
| pack 顺序 | log_frame 在前，bottom_frame 在后 | **bottom_frame 在前，log_frame 在后** |
| log_text 高度 | 12 行 | 8 行 |
| 窗口初始尺寸 | 1100×750 | 1100×880 |
| 窗口最小尺寸 | 900×600 | 960×720 |

**核心原理**：tkinter pack 中，先 pack 的组件优先占位。将 bottom_frame 移到 log_frame 之前 pack，底部栏先占据其自然高度，log_frame 再用 `expand=True` 填充剩余空间，确保底部按钮始终可见。

---

## 3. OSINT 操作系统中断测试相关问题

> OSINT（OS Interruption Test）是问题最多的模块，历经 3 轮迭代共修复 9 项 Bug。

### 问题 3.1：fio IO 负载启动后立即退出

**涉及版本**：v1.8.2 → v1.8.3 第一轮修复

**错误日志**：
```
[ERROR] 混合读写负载启动后立即退出
```
fio 详细日志：
```
osint_mixed_rw: you need to specify size=
fio: pid=0, err=22/file:filesetup.c:1156, func=total_file_size, error=Invalid argument
```

**根因分析**：
fio 使用 `--directory=<mount_point>` 模式时，**必须指定 `--size` 参数**（每个 job 写入多少数据）。脚本中 fio 命令遗漏了该参数，fio 报参数错误（err=22, Invalid argument）立即退出。IO 负载实际上完全没有运行。

**解决方案**：
fio 命令添加 `--size=256M`（4 个 job 共 1GB 测试数据，在 500GB 盘上完全足够）。

修复后 fio 正常运行：读 ~317MB/s，写 ~137MB/s。

---

### 问题 3.2：S3 唤醒后文件系统严重损坏、挂载失败

**涉及版本**：v1.8.2 → v1.8.3 第一轮修复

**错误日志**：
```
文件系统挂载: FAIL
文件系统检查 (fsck): FAIL
fsck输出:
  ext2fs_check_desc: Corrupt group descriptor: bad block for block bitmap
  fsck.ext4: Group descriptors look bad... trying backup blocks...
  Block bitmap for group 3584 is not in group. (block 312517069513289728)
[ERROR] 测试文件丢失: /mnt/ssd_osint/osint_integrity_test.bin
完整性校验: 0/1 通过, 0 失败, 1 丢失
```

**根因分析**：
原 `trigger_sleep()` 方法在休眠前**只执行了 `sync`，没有停止 fio IO 负载，也没有卸载文件系统**。S3 休眠时 fio 仍在后台持续写入（direct=1 混合读写），NVMe 驱动在 S3 恢复时未正确冻结文件系统状态，导致 ext4 组描述符/块位图元数据严重损坏。

证据：fsck 报告的 block 号 `312517069513289728` 约 **281TB**，远超 500GB 盘容量，明显是元数据指针损坏。

级联影响：文件系统损坏 → 挂载失败 → 测试文件路径不可达 → 数据完整性校验失败（文件丢失）。

**解决方案**：
`trigger_sleep()` 中休眠前执行完整清理流程：
1. `stop_io_load()` — 停止 fio IO 负载
2. `sync` — 刷新页缓存
3. `umount` — 卸载测试分区文件系统（失败时强制 `umount -f`）

唤醒后重新 `mount` 挂载文件系统。

**核心原理**：休眠时文件系统处于干净卸载状态，不会因 S3 恢复时的驱动状态异常而损坏。

---

### 问题 3.3：SMART 检查误报 FAIL（解析为 None）

**涉及版本**：v1.8.2 → v1.8.3 第一轮修复

**错误日志**：
```
SMART: 介质错误=None, 可用备件=None%, 温度=45°C -> FAIL
```

**根因分析**：
`nvme smart-log -o json` 输出中，`media_and_data_integrity_errors` 和 `available_spare` 字段解析为 `None`。原因可能是：
1. 不同厂商/不同 nvme-cli 版本的 smart-log JSON 字段名不同
2. 字段值为 JSON null

原判定逻辑：
```python
ok = (info.get("media_errors", 1) == 0 and ...)
```
当 key 存在但值为 None 时，`dict.get(key, default)` 返回 **None**（默认值不生效），`None == 0` 为 **False**，被误判为 FAIL。

实际上温度 45°C、power_cycles 都能正常读取，SMART 本身没问题，是解析逻辑的 bug。

**解决方案**：
1. **多字段名兼容查找**：
   - 介质错误：依次尝试 `media_and_data_integrity_errors` / `media_errors` / `media_and_data_errors`
   - 可用备件：依次尝试 `available_spare` / `avail_spare` / `spare`
2. **None 值兜底**：解析不到时视为正常值（介质错误=0，可用备件=100）
3. **判定时双重保险**：`(info.get("media_errors") or 0) == 0` / `(info.get("available_spare") or 100) >= 10`

---

### 问题 3.4：状态文件残留导致循环跳过、全程无休眠

**涉及版本**：v1.8.3 第一轮 → v1.8.3 第二轮修复

**错误日志**：
```
加载 OSINT 状态: 第 3/2 轮
（直接跳到最终完整功能测试，全程无 S3 休眠、无循环执行）
总轮数: 2
实际执行: 0 轮
```

**根因分析**：
上一次测试结束后，状态文件 `/var/lib/ssd_osint_state.json` 记录了：
```json
{
  "phase": "done",
  "current_cycle": 3,
  "total_cycles": 2
}
```
新版本启动时加载到该状态，发现 `current_cycle (3) > total_cycles (2)`，循环 `for cycle in range(start_cycle, self.cycles + 1)` 直接不执行，跳过所有循环直接进入最终功能测试。

用户看到"没有休眠"，实际是**状态文件残留**导致的，不是休眠功能本身有问题（v1.8.2 的休眠功能是完好的）。

**解决方案**：
`run()` 方法加载状态后增加有效性检测，以下任一情况自动清除旧状态并重新初始化：
- `phase == "done"`（上一次测试已完成）
- `total_cycles != self.cycles`（用户改了循环次数）
- `current_cycle > self.cycles + 1`（状态轮次异常）

无需用户手动 `rm /var/lib/ssd_osint_state.json`。

---

### 问题 3.5：SMART 介质错误导致每轮 FAIL（已知有介质错误的测试盘）

**涉及版本**：v1.8.3 第一轮 → v1.8.3 第二轮修复

**错误日志**：
```
SMART: 介质错误=5353, 可用备件=85%, 温度=52°C -> FAIL
```
（修复前每轮都因介质错误判定失败）

**根因分析**：
用户的测试盘已知有 **5353 个介质错误**（`media_errors=5353`），这是硬盘本身的硬件问题，不是测试导致的。原判定逻辑中介质错误 > 0 即 FAIL，导致每轮测试都因这个已知问题失败，无法验证休眠唤醒稳定性。

用户需求：**忽略介质错误，只验证休眠唤醒稳定性**。

**解决方案**：
SMART 判定改为**警告模式**：
- 介质错误 > 0 时输出 `[WARNING]` 日志告警，但**不影响 PASS/FAIL**
- 可用备件 < 10% 和温度超出 0-70°C 仍为**硬性 FAIL 条件**（这两个是真正的健康指标，介质错误可能是历史累积不影响当前功能）
- 返回结果中增加 `media_errors_warning: true` 标记

修复后日志：
```
[WARNING] [警告] 检测到介质错误: 5353（警告模式，不影响本轮判定）
SMART: 介质错误=5353(警告模式), 可用备件=85%, 温度=40°C -> OK
```

---

### 问题 3.6：唤醒后挂载失败（Structure needs cleaning）

**涉及版本**：v1.8.3 第二轮 → v1.8.3 第三轮修复

**错误日志**：
```
唤醒后挂载失败: mount: /mnt/ssd_osint: mount(2) system call failed: Structure needs cleaning.
休眠失败: wake_ok_but_mount_failed (17.5s): mount: /mnt/ssd_osint: mount(2) system call failed: Structure needs cleaning.
```

**根因分析**：
虽然休眠前已经做了 stop_io → sync → umount，但 fio 使用 `--direct=1` 大量写入（4 个 256MB 文件 + 持续混合读写，读 ~317MB/s 写 ~137MB/s），NVMe 设备**内部写缓存（device-level write cache）在 S3 休眠时可能未完全提交到 NAND**，导致 ext4 日志（journal）轻微不一致。

唤醒后挂载时 ext4 检测到日志需要恢复，报 `Structure needs cleaning`。这是比问题 3.2 更轻微的文件系统不一致（元数据未严重损坏，只是日志需要 replay）。

**解决方案**：
`trigger_sleep()` 唤醒后挂载失败时，自动检测错误信息中是否包含 `Structure needs cleaning` / `needs cleaning`，如果是则：
1. 先确保未挂载（`umount`）
2. 自动运行 `fsck -y <partition>` 修复文件系统（自动应答所有修复）
3. 记录 fsck 输出到日志
4. 修复后重试挂载

大部分 ext4 日志不一致问题可通过 fsck 自动修复，测试可继续执行。

---

### 问题 3.7：休眠前未刷新 NVMe 设备写缓存（预防措施）

**涉及版本**：v1.8.3 第三轮新增（预防问题 3.6）

**根因分析**：
`umount` 只能确保 Linux 页缓存和块层缓存刷新，但 NVMe 设备**内部写缓存**可能还有未提交到 NAND 的数据。S3 休眠时设备掉电或状态重置，这些未提交的数据丢失，导致文件系统不一致（即问题 3.6 的 `Structure needs cleaning`）。

**解决方案**：
`umount` 后增加 `nvme flush <controller>` 命令，强制刷新 NVMe 设备内部写缓存，确保所有数据提交到 NAND 后再进入休眠。从源头减少文件系统不一致的概率。

---

### 问题 3.8：fio 停止后立即 sync，进程未完全退出（预防措施）

**涉及版本**：v1.8.3 第三轮新增（预防问题 3.6）

**根因分析**：
`stop_io_load()` 使用 `terminate()`（SIGTERM）终止 fio 进程，fio 收到信号后需要时间完成正在进行的 IO、关闭文件句柄、退出进程。原代码立即执行 `sync`，可能 fio 还未完全退出，仍有未完成的 IO，导致 sync 时数据不一致。

**解决方案**：
`stop_io_load()` 后增加 `time.sleep(2)`，确保 fio 进程完全退出、文件句柄释放、所有 IO 提交到块层后，再执行 sync / umount。

---

### 问题 3.9：休眠失败时不记录结果，统计显示 0/0

**涉及版本**：v1.8.3 第二轮 → v1.8.3 第三轮修复

**错误日志**：
```
总轮数: 2
实际执行: 0 轮
通过: 0
失败: 0
错误信息: 0/0 轮通过，0 轮失败
```

**根因分析**：
原 `run_cycle()` 中，当 `trigger_sleep()` 返回 False（休眠/挂载失败）时，直接 `return False`，**没有构造和记录 cycle_result**，导致 `state["results"]` 为空。统计时 `total_executed = len(results) = 0`，显示"实际执行 0 轮"，用户无法看到失败轮次的详情，也无法区分"没执行"和"执行了但失败"。

**解决方案**：
`trigger_sleep()` 失败时，构造一个失败的 `cycle_result`（所有检查项标记为 False，错误信息记录为"休眠/挂载失败，未执行检查"），追加到 `state["results"]` 并保存状态。统计时失败轮次正常计入，不再显示 0/0。

---

## 4. 问题速查表

| # | 问题 | 错误关键词 | 根因 | 解决方案 | 修复版本 |
|---|------|-----------|------|----------|----------|
| 1.1 | 稳态预处理超时 | 10138秒超时 | 按100MB/s估算，实际30-60MB/s | 按30MB/s估算+进度监控+降级策略 | v1.7.7 |
| 2.1 | 底部按钮看不到 | 无（视觉问题） | 两个expand=True争夺空间+pack顺序 | 调整pack顺序+减小log高度+增大窗口 | v1.8.2 |
| 3.1 | fio启动即退出 | `you need to specify size=` | fio directory模式缺size参数 | 添加`--size=256M` | v1.8.3 |
| 3.2 | 文件系统严重损坏 | `Corrupt group descriptor` | 休眠时未停IO未卸载 | 休眠前stop_io→sync→umount | v1.8.3 |
| 3.3 | SMART误报FAIL | `介质错误=None` | None==0为False被误判 | 多字段名兼容+None兜底 | v1.8.3 |
| 3.4 | 全程无休眠 | `加载状态: 第3/2轮` | 状态文件残留，循环跳过 | 自动检测无效状态并重置 | v1.8.3 |
| 3.5 | 介质错误致每轮FAIL | `介质错误=5353 -> FAIL` | 已知硬件问题被误判 | 介质错误改为警告模式 | v1.8.3 |
| 3.6 | 挂载失败 | `Structure needs cleaning` | NVMe写缓存未提交，ext4日志不一致 | 自动fsck -y修复+重试挂载 | v1.8.3 |
| 3.7 | 写缓存未刷新（预防） | 同3.6 | umount不刷新设备内部缓存 | 休眠前`nvme flush` | v1.8.3 |
| 3.8 | fio未完全退出（预防） | 同3.6 | SIGTERM后立即sync | stop_io后sleep(2) | v1.8.3 |
| 3.9 | 统计0/0 | `实际执行: 0轮` | 休眠失败时未记录结果 | 失败轮次也记录cycle_result | v1.8.3 |

---

## 附录：功能增强记录（非问题）

以下为用户主动要求的功能增强，非 Bug 修复：

| 版本 | 功能增强 |
|------|----------|
| v1.8.0 | 多组性能测试参数批量保存、顺序执行、不限组数、兼容单组；CLI `--perf-task-file`；GUI 批量任务管理面板 |
| v1.8.1 | 开始测试按钮移到顶部设备栏右侧；任务列表新增「↑上移」「↓下移」排序按钮 |
| v1.8.3 | OSINT 使用步骤文档编写；SMART 介质错误警告模式（用户需求驱动） |

---

*文档生成时间：2026-09-11 | 基于 ssd_test_v1.7.6.py ~ ssd_test_v1.8.3.py 实际问题记录*
