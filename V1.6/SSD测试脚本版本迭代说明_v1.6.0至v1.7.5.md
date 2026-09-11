# SSD测试脚本版本迭代说明 v1.6.0 至 v1.7.5

## 一、版本迭代总览

本迭代周期（v1.6.0 → v1.7.5）聚焦于**完整性能特征测试模块的整体重构与稳定性加固**，核心目标是将传统固定三项性能测试升级为全参数可配置的 FIO 测试框架，对齐 SNIA PTS 与 CrystalDiskMark 行业测试规范，并解决多轮实际测试中暴露的超时、参数冲突、GUI 兼容性等问题。

| 版本 | 发布日期 | 核心变更 | 状态 |
|------|----------|----------|------|
| v1.6.0 | 2026-09-10 | 新增 CrystalDiskMark 风格性能配置（测试项表格/预设/测量时间） | 已被 v1.7.0 替代 |
| v1.7.0 | 2026-09-10 | 性能测试整体重构：全参数可配置 FIO 测试，7 阶段改造完成 | 基础版本 |
| v1.7.1 | 2026-09-10 | 删除旧性能测试功能（传统三项+CDM），代码精简 589 行 | 功能清理 |
| v1.7.2 | 2026-09-10 | 修复 precondition_steady_state 误删 + 文本日志参数冲突 | Bug 修复 |
| v1.7.3 | 2026-09-10 | 修复 GUI 退出码 2（--no-precondition 未定义）+ fio 错误输出捕获 | Bug 修复 |
| v1.7.4 | 2026-09-10 | 修复文本日志 output-format=text 无效问题 | Bug 修复 |
| v1.7.5 | 2026-09-10 | 修复稳态预处理随机写超时不足，新增预估耗时日志 | Bug 修复+优化 |

---

## 二、修改原因梳理

### 2.1 v1.6.0 存在的核心问题

#### 问题 1：传统三项性能测试参数固定，无法满足多样化测试需求

**问题表现**：
- 传统性能测试仅支持固定三项：顺序读写（128K QD32）、随机读写（4K QD32）、延迟 QoS（4K QD1）
- 块大小、队列深度、并发任务数、读写模式等参数全部硬编码，无法通过命令行或 GUI 调整
- 无法模拟 CrystalDiskMark 的标准测试配置（Seq Q32T1、4K Q32T1、4K Q1T1 等）

**影响范围**：性能测试模块（PerformanceTester），所有依赖性能数据的测试场景

**触发背景**：测试工程师需要针对不同 SSD 产品规格（消费级/企业级、TLC/QLC、SLC 缓存大小）调整测试参数，传统固定三项无法覆盖。

#### 问题 2：稳态预处理耗时估算偏差大，频繁超时

**问题表现**：
- 随机写预处理超时固定为 `max(1800, cap_gb * 3)` 秒，500GB 盘仅 1800 秒（30 分钟）
- 实际 4K QD32 随机写稳态速度约 100-300 MB/s，500GB 盘 2x 容量需约 80-160 分钟
- 超时后直接判定预处理失败，跳过后续性能测试，导致整个测试项 FAIL

**影响范围**：稳态性能测试（Steady State），所有需要稳态数据的场景

**触发背景**：500GB 测试盘实际运行时，随机写预处理在 30 分钟时被强制终止，测试失败。

#### 问题 3：缺少初始安全擦除流程的标准化封装

**问题表现**：
- FOB 状态测试依赖 `blkdiscard` 命令，但错误处理不完善
- blkdiscard 失败时仅输出 WARNING 后继续测试，此时磁盘并非真正 FOB 状态，测试结果无效
- 未区分 NVMe 与 SATA 设备的擦除方式差异

**影响范围**：FOB 状态性能测试

**触发背景**：部分 SATA 设备不支持 blkdiscard，导致 FOB 测试结果不准确。

#### 问题 4：GC 等待固定时长不通用

**问题表现**：
- 稳态预处理后固定等待 300 秒让后台 GC/WL 完成
- 不同容量、不同介质（TLC/QLC）的 GC 完成时间差异大
- QLC 盘可能需要更长时间完成 GC，300 秒不足；TLC 盘可能 60 秒已完成，300 秒浪费时间

**影响范围**：稳态性能测试结果的准确性与测试效率

**触发背景**：测试多种介质 SSD 时，固定等待时间无法兼顾准确性与效率。

#### 问题 5：fio 测试时长不足，结果波动大

**问题表现**：
- 传统性能测试单项运行时长固定 60 秒，无法调整
- 短时间测试（如 10 秒）结果波动大，无法反映稳态性能
- 长时间测试（如 300 秒）需要修改代码，无法通过参数配置

**影响范围**：所有性能测试结果的稳定性与可重复性

**触发背景**：不同测试场景对运行时长要求不同（快速验证 10 秒，标准测试 60 秒，稳态验证 300 秒）。

### 2.2 v1.7.0 至 v1.7.5 迭代中暴露的问题

#### 问题 6：旧功能清理时误删稳态预处理方法

**问题表现**：
- v1.7.1 删除旧性能测试功能时，`precondition_steady_state` 方法被误删
- 运行稳态测试时报 `AttributeError: 'PerformanceTester' object has no attribute 'precondition_steady_state'`

**影响范围**：稳态性能测试完全不可用

**触发背景**：v1.7.1 代码清理范围过大，将稳态预处理方法与旧测试方法一并删除。

#### 问题 7：文本日志参数冲突，fio 报错

**问题表现**：
- 文本日志命令中保留了 `--lat_percentiles=1` 参数，该参数与 `--output-format=json+` 绑定
- 文本格式下 fio 报错或行为异常
- 后续又发现 `--output-format=text` 在 fio 3.28 中不是有效值（有效值为 normal/json/json+/terse），导致 `fio: invalid output format text`

**影响范围**：文本格式日志生成功能

**触发背景**：JSON+ 格式与文本格式的参数兼容性未充分验证。

#### 问题 8：GUI 传递未定义参数，argparse 退出码 2

**问题表现**：
- GUI 中取消勾选"稳态预处理"时传递 `--no-precondition` 参数，但 parse_args 中未定义
- argparse 遇到未定义参数立即返回退出码 2，测试未执行即失败
- 无错误日志生成，难以定位问题

**影响范围**：GUI 模式下所有跳过预处理的性能测试

**触发背景**：GUI 与命令行参数未同步更新，新增 GUI 功能时未同步添加命令行参数定义。

#### 问题 9：fio 错误输出未捕获，难以诊断

**问题表现**：
- fio 命令使用 `capture=False` 执行，错误输出直接打印到终端
- GUI 模式下子进程的 stderr 未被有效捕获，用户看不到 fio 具体错误信息
- 测试失败时仅显示"退出码 X"，无具体错误原因

**影响范围**：所有 fio 相关测试的问题诊断

**触发背景**：早期设计为实时输出 fio 日志，但牺牲了错误捕获能力。

---

## 三、功能添加明细

### 3.1 全参数可配置 FIO 测试框架（v1.7.0）

**模块**：PerformanceTester

**新增核心方法**：

| 方法名 | 功能 | 解决的问题 |
|--------|------|------------|
| `_build_full_fio_cmd(output_path)` | 构建双横杠规范格式 FIO 命令 | 参数固定、无法调整 |
| `run_full_perf_test(state_label)` | 执行单次全参数可配置 FIO 测试 | 传统三项测试不灵活 |
| `_parse_full_perf_json(data)` | 解析 json+ 格式结果，提取带宽/IOPS/延迟/百分位 | 结果解析不完整 |
| `_print_full_perf_summary(parsed, label)` | 输出性能结果汇总（带宽/IOPS/延迟/百分位） | 结果展示不规范 |
| `_generate_log_filename(state_label, suffix)` | 生成规范日志文件名 | 日志命名不统一 |

**新增可配置参数**：

| 参数 | 命令行 | 说明 | 默认值 |
|------|--------|------|--------|
| 测试名称 | `--perf-name` | FIO --name，同步为日志文件名核心标识 | perf-test |
| direct | `--perf-direct` | --direct，0/1 切换，1=裸盘绕过系统缓存 | 1 |
| ioengine | `--perf-ioengine` | --ioengine，direct=1 时强制 libaio | libaio |
| 块大小 bs | `--perf-bs` | --bs，参考值：4k/8k/16k/128k/1M | 4k |
| 队列深度 iodepth | `--perf-iodepth` | --iodepth，参考值：1/8/32/64/128 | 64 |
| 并发任务 numjobs | `--perf-numjobs` | --numjobs，参考值：1/2/4/8 | 1 |
| 读写模式 rw | `--perf-rw` | --rw：randread/randwrite/randrw/read/write/rw | randread |
| 读占比 rwmixread | `--perf-rwmixread` | --rwmixread，仅混合模式(randrw/rw)生效 | 70 |
| 测试范围 size | `--perf-size` | --size，支持百分比(如3%)或固定容量(如10G) | 3% |
| 运行时长 runtime | `--perf-runtime` | --runtime，time_based 固定开启 | 60 |
| 文本日志 | `--perf-text-log` | 同时输出文本格式日志 | 关 |
| 跳过预处理 | `--no-precondition` | 跳过稳态预处理，直接测当前状态 | 关 |

**FIO 命令强制参数**（对齐执行计划规范）：
```
--direct=1 --group_reporting --lat_percentiles=1 --output-format=json+
--time_based --buffered=0
随机模式额外加：--norandommap --randrepeat=0
```

**日志命名规范**：
```
{状态标识}_{测试名称}_{块大小}_{读写模式}_qd{队列深度}_{YYYYMMDD_HHMMSS}.json
示例：FOB_perf-test_4k_randread_qd64_20260910_142551.json
```

### 3.2 稳态预处理优化（v1.7.5）

**模块**：PerformanceTester.precondition_steady_state

**优化内容**：

| 优化项 | 旧实现 | 新实现 | 效果 |
|--------|--------|--------|------|
| 顺序写超时 | `max(600, cap_gb*2)` | `cap_bytes / 500MB/s + 300` | 按实际写入量估算，避免超时 |
| 随机写超时 | `max(1800, cap_gb*3)` | `write_bytes / 100MB/s + 600` | 500GB盘从30分钟增至169分钟 |
| 预估耗时日志 | 无 | 预处理开始时显示预估耗时与超时 | 用户可判断是否需要等待 |

**预处理流程（SNIA 标准近似）**：
1. 顺序写满全盘 2 次（128K QD32）
2. 随机写至写入量达到 2 倍全盘容量（4K QD32）
3. 等待 300 秒让后台 GC/WL 完成

### 3.3 GUI 性能测试全参数配置（v1.7.0）

**模块**：SSDTestGUI

**新增功能**：
- 性能测试配置页全参数可视化输入（name/direct/ioengine/bs/iodepth/numjobs/rw/rwmixread/size/runtime）
- 实时 FIO 命令预览（只读，随参数变化更新）
- 参数联动校验（direct=0 时 ioengine 可切换，混合模式时 rwmixread 生效）
- 稳态预处理开关复选框
- 文本日志输出开关复选框
- 测试状态下拉框（fob/steady/both）

### 3.4 错误处理与诊断增强（v1.7.3）

**模块**：PerformanceTester.run_full_perf_test

**增强内容**：
- fio 命令从 `capture=False` 改为 `capture=True`，捕获 stdout/stderr
- fio 失败时输出具体错误信息（stderr 前 1000 字符）
- 文本日志生成失败时同样输出 fio 错误详情
- 预处理失败时明确标记 `precondition_failed=True`，跳过后续性能测试

---

## 四、测试前风险校验方案

### 4.1 校验工具集

| 工具 | 用途 | 安装命令 |
|------|------|----------|
| `fio` | 性能基准验证，确认 fio 可用且版本兼容 | `sudo apt install fio` |
| `nvme-cli` | NVMe 磁盘状态巡检（容量/SMART/固件版本） | `sudo apt install nvme-cli` |
| `smartctl` | SMART 健康度检测（温度/介质错误/可用备件） | `sudo apt install smartmontools` |
| `lsblk` | 块设备信息确认（容量/挂载点/设备类型） | 系统自带（util-linux） |
| `blkdiscard` | 安全擦除验证（FOB 状态前置条件） | 系统自带（util-linux） |
| `findmnt` | 设备挂载状态检查 | 系统自带（util-linux） |

### 4.2 校验项清单

#### 校验项 1：磁盘初始状态校验

```bash
# 确认设备存在且可识别
sudo nvme list
lsblk -d -o NAME,SIZE,MODEL,TRAN /dev/nvme0n1

# 确认设备未挂载（性能测试必须卸载所有分区）
findmnt /dev/nvme0n1
sudo umount /dev/nvme0n1p* 2>/dev/null || true

# 确认不是系统盘（系统盘测试会导致系统无法启动）
df -h | grep nvme0n1
```

**风险等级**：高（误操作会导致数据丢失或系统损坏）

#### 校验项 2：测试环境依赖校验

```bash
# 验证核心工具版本
fio --version           # 建议 ≥ 3.16
nvme --version          # 建议 ≥ 1.9
smartctl --version      # 建议 ≥ 7.0
python3 --version       # 建议 ≥ 3.8

# 验证 root 权限
sudo -v
```

**风险等级**：中（依赖缺失会导致测试中途失败）

#### 校验项 3：参数合法性校验

```bash
# 试运行模式（不实际执行，仅验证命令和参数）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --dry-run

# 验证 FIO 命令可正常解析（手动执行预览命令，加 --parse-only 或短时间运行）
sudo fio --name=test --filename=/dev/nvme0n1 --direct=1 --bs=4k \
  --iodepth=64 --rw=randread --ioengine=libaio --size=1% \
  --time_based --runtime=5 --group_reporting
```

**风险等级**：中（参数错误会导致 fio 立即失败）

#### 校验项 4：测试数据备份风险提示

> **警告**：以下操作会破坏待测 SSD 上的所有数据，请务必确认：
> 1. 目标设备无重要数据，或已完成备份
> 2. 目标设备不是系统盘（/dev/sda 或当前根分区所在盘）
> 3. 目标设备所有分区已卸载
> 4. 固件升级测试的固件镜像与设备型号匹配

**风险等级**：高（数据不可恢复）

### 4.3 快速校验脚本（建议测试前执行）

```bash
#!/bin/bash
# SSD 测试前快速校验脚本
DEVICE=${1:-/dev/nvme0n1}

echo "=== 1. 设备存在性检查 ==="
lsblk -d -o NAME,SIZE,MODEL,TRAN $DEVICE || { echo "FAIL: 设备不存在"; exit 1; }

echo "=== 2. 挂载状态检查 ==="
if findmnt $DEVICE > /dev/null 2>&1; then
    echo "WARNING: 设备已挂载，请先卸载"
    findmnt $DEVICE
else
    echo "OK: 设备未挂载"
fi

echo "=== 3. 系统盘风险检查 ==="
if df -h | grep -q $DEVICE; then
    echo "WARNING: 设备可能是系统盘，测试会导致系统损坏!"
    df -h | grep $DEVICE
else
    echo "OK: 设备不是系统盘"
fi

echo "=== 4. 依赖工具检查 ==="
for tool in fio nvme smartctl python3; do
    if command -v $tool > /dev/null 2>&1; then
        echo "OK: $tool 已安装"
    else
        echo "FAIL: $tool 未安装，请执行 sudo apt install $tool"
    fi
done

echo "=== 5. SMART 健康预检 ==="
sudo nvme smart-log $DEVICE 2>/dev/null | head -20 || echo "WARNING: SMART 读取失败"

echo "=== 校验完成 ==="
```

---

## 五、核心修改要点补充

### 5.1 稳态判定窗口波动阈值的取值依据

**设计逻辑**：
- 本脚本采用 SNIA PTS 标准的近似稳态预处理流程，不实现动态稳态判定窗口（滑动窗口斜率检测）
- 替代方案：固定预处理量（顺序写 2x + 随机写 2x）+ 固定 GC 等待时间（300s）
- 取值依据：
  - 顺序写 2 次：确保 SLC 缓存被填满，后续写入进入直写 TLC/QLC 模式
  - 随机写 2x 容量：确保所有 LBA 至少被随机写覆盖 2 次，FTL 映射表充分更新，GC 压力达到稳态
  - 等待 300s：给后台 GC/WL 足够时间完成脏块回收和磨损均衡，避免测试时 GC 活动干扰性能测量

**取舍依据**：
- 动态稳态判定（滑动窗口）更精确，但实现复杂，且不同 SSD 收敛速度差异大，容易陷入"永远不收敛"的死循环
- 固定预处理量简单可靠，虽可能对某些盘"过度预处理"（浪费时间），但能保证所有盘都达到稳态
- 可通过 `--no-precondition` 跳过预处理，或修改代码调整预处理量

### 5.2 SLC 缓存速率差异对耗时估算的影响机制

**影响机制**：
- 消费级 SSD 通常配备 SLC 缓存（动态或固定），写入时先进入 SLC 缓存，速度极快（1000-3000 MB/s）
- SLC 缓存满后，写入进入直写 TLC/QLC 模式，速度骤降（100-500 MB/s）
- 顺序写预处理前半段（SLC 缓存未满）速度快，后半段（SLC 缓存已满）速度慢
- 随机写由于 LBA 随机分布，SLC 缓存命中率低，整体速度更接近直写速度

**耗时估算策略**：
- 顺序写按 500 MB/s 保守估算（兼顾 SLC 缓存阶段和直写阶段的平均速度）
- 随机写按 100 MB/s 保守估算（4K QD32 随机写稳态速度，QLC 盘可能更低）
- 实际速度因盘而异，估算值偏保守以避免超时

### 5.3 不同闪存介质的预处理量适配原则

| 介质类型 | SLC 缓存 | 顺序写速度 | 随机写稳态速度 | GC 完成时间 | 预处理建议 |
|----------|----------|------------|----------------|-------------|------------|
| 企业级 TLC | 无/小 | 1000-3000 MB/s | 100-300 MB/s | 快（60-120s） | 标准预处理量即可 |
| 消费级 TLC | 中（10-30GB） | 1500-3500 MB/s | 50-200 MB/s | 中（120-300s） | 标准预处理量 |
| 消费级 QLC | 大（20-50GB） | 1000-2500 MB/s | 30-100 MB/s | 慢（300-600s） | 建议增加随机写超时，GC 等待可增至 600s |
| 企业级 QLC | 无/小 | 800-2000 MB/s | 50-150 MB/s | 中（120-300s） | 标准预处理量 |

**适配原则**：
- 预处理量（顺序写 2x + 随机写 2x）对所有介质通用，不区分介质类型
- 超时时间按保守速度（100 MB/s 随机写）估算，覆盖 QLC 盘的慢速场景
- 如测试 QLC 盘且发现 300s GC 等待不足，可修改代码中 `time.sleep(300)` 为 `time.sleep(600)`
- 企业级盘预处理速度快，会提前完成，超时时间不会影响实际耗时（fio 写完即返回，不会等到超时）

### 5.4 关键决策点与方案取舍

#### 决策 1：全参数可配置 vs 固定测试项

**选择**：全参数可配置（v1.7.0）

**取舍依据**：
- 固定三项（顺序/随机/延迟）实现简单，但无法覆盖多样化测试需求
- 全参数可配置增加了参数校验和 GUI 复杂度，但灵活性大幅提升
- 通过默认值（4k QD64 randread）保证开箱即用，高级用户可调整参数

#### 决策 2：JSON+ 格式 vs 纯 JSON 格式

**选择**：JSON+ 格式（`--output-format=json+`）

**取舍依据**：
- JSON+ 格式在 JSON 基础上增加了延迟百分位（clat percentiles）和磁盘利用率等扩展字段
- `--lat_percentiles=1` 与 JSON+ 绑定，可获取 p1/p5/p10/.../p99.99 完整百分位数据
- 纯 JSON 格式不支持 `--lat_percentiles`，无法获取详细百分位
- 代价：文本格式日志需移除 `--lat_percentiles` 参数（v1.7.2 修复）

#### 决策 3：固定预处理量 vs 动态稳态判定

**选择**：固定预处理量（顺序写 2x + 随机写 2x + 等待 300s）

**取舍依据**：
- 动态稳态判定（滑动窗口斜率检测）更精确，但实现复杂，且可能陷入不收敛死循环
- 固定预处理量简单可靠，虽可能过度预处理，但保证所有盘都达到稳态
- 可通过 `--no-precondition` 跳过，或修改代码调整

#### 决策 4：GUI subprocess 调用 vs 直接调用

**选择**：GUI 通过 subprocess 调用命令行模式

**取舍依据**：
- 两种模式行为完全一致，避免 GUI 特有 bug
- 子进程崩溃不影响 GUI 主进程
- 停止测试时可直接 terminate 整个进程树
- 命令预览直接显示实际执行命令，便于调试
- 代价：进程间通信增加复杂度，日志读取需后台线程

---

## 六、版本兼容性说明

### 6.1 命令行参数变更

| 参数 | v1.5 状态 | v1.7.5 状态 | 说明 |
|------|-----------|-------------|------|
| `--perf-qd` | 存在 | **已删除** | 全参数模式使用 `--perf-iodepth` 替代 |
| `--perf-bs` | 存在（自动） | 存在（可配置） | 默认 4k |
| `--perf-runtime` | 存在 | 存在 | 全参数模式复用此参数 |
| `--perf-state` | 存在 | 存在 | fob/steady/both |
| `--precondition` | 存在 | 存在 | on/off |
| `--no-precondition` | 不存在 | **新增** | 跳过稳态预处理 |
| `--perf-name` | 不存在 | **新增** | FIO 测试名称 |
| `--perf-direct` | 不存在 | **新增** | direct 开关 |
| `--perf-ioengine` | 不存在 | **新增** | ioengine 选择 |
| `--perf-iodepth` | 不存在 | **新增** | 队列深度 |
| `--perf-numjobs` | 不存在 | **新增** | 并发任务数 |
| `--perf-rw` | 不存在 | **新增** | 读写模式 |
| `--perf-rwmixread` | 不存在 | **新增** | 混合读占比 |
| `--perf-size` | 不存在 | **新增** | 测试范围 |
| `--perf-text-log` | 不存在 | **新增** | 文本日志开关 |

### 6.2 性能测试方法变更

| 方法 | v1.5 状态 | v1.7.5 状态 | 说明 |
|------|-----------|-------------|------|
| `test_sequential_rw` | 存在 | **已删除** | 传统顺序读写测试 |
| `test_random_rw` | 存在 | **已删除** | 传统随机读写测试 |
| `test_latency_qos` | 存在 | **已删除** | 传统延迟 QoS 测试 |
| `run_cdm_profile` | 不存在（v1.6.0新增） | **已删除** | CDM 风格测试 |
| `run_fio` | 存在 | **已删除** | 基础 fio 执行 |
| `_parse_fio_json` | 存在 | **已删除** | 旧 JSON 解析 |
| `run_full_perf_test` | 不存在 | **新增** | 全参数可配置 fio 测试 |
| `_build_full_fio_cmd` | 不存在 | **新增** | 构建规范 fio 命令 |
| `_parse_full_perf_json` | 不存在 | **新增** | 解析 json+ 结果 |
| `_print_full_perf_summary` | 不存在 | **新增** | 输出性能汇总 |
| `_generate_log_filename` | 不存在 | **新增** | 生成规范日志名 |
| `precondition_steady_state` | 存在 | 存在（v1.7.2恢复） | 稳态预处理 |

---

## 七、升级建议

### 7.1 从 v1.5 升级到 v1.7.5

1. **性能测试参数需调整**：
   - 旧参数 `--perf-qd 32` 需改为 `--perf-iodepth 32`
   - 旧参数 `--perf-bs auto` 需改为具体值如 `--perf-bs 4k`
   - 如需传统三项测试效果，需分别运行三次全参数测试：
     ```bash
     # 顺序读写（对应旧 test_sequential_rw）
     sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-rw rw --perf-bs 128k --perf-iodepth 32
     # 随机读写（对应旧 test_random_rw）
     sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-rw randrw --perf-bs 4k --perf-iodepth 32
     # 延迟测试（对应旧 test_latency_qos）
     sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-rw randread --perf-bs 4k --perf-iodepth 1
     ```

2. **GUI 配置文件不兼容**：v1.5 保存的 JSON 配置文件无法直接在 v1.7.5 中加载，需重新配置

3. **日志格式变更**：性能测试日志文件名格式变更为 `{状态}_{名称}_{bs}_{rw}_qd{QD}_{时间戳}.json`

### 7.2 从 v1.6.0 升级到 v1.7.5

1. **CDM 风格测试已移除**：v1.6.0 新增的 CrystalDiskMark 风格测试在 v1.7.1 中已删除，全参数可配置测试可完全替代
2. **性能配置页全面重构**：GUI 性能配置页从 CDM 表格改为全参数输入框
3. **建议直接使用 v1.7.5**：v1.6.0 为过渡版本，不建议长期使用

---

## 八、已知限制与后续规划

### 8.1 已知限制

1. **稳态预处理参数硬编码**：顺序写次数（2次）、随机写倍数（2x）、GC 等待时间（300s）仍为硬编码，无法通过命令行参数调整
2. **动态稳态判定未实现**：未实现 SNIA PTS 标准的滑动窗口斜率检测，固定预处理量可能对某些盘过度预处理
3. **多介质适配未自动化**：未自动识别 TLC/QLC/企业级介质并调整预处理策略
4. **HTML 报告未实现**：当前仅支持 JSON 报告和文本摘要，HTML 可视化报告需后续扩展

### 8.2 后续规划

1. **预处理参数可配置**：新增 `--precond-passes`、`--precond-rand-mult`、`--precond-wait` 参数
2. **动态稳态判定**：实现滑动窗口斜率检测，自动判断稳态收敛
3. **介质自动识别**：通过 `nvme id-ctrl` 识别介质类型，自动调整预处理策略
4. **HTML 报告**：基于 JSON 报告生成可视化 HTML 报告（性能对比图表、SMART 趋势图等）
5. **多盘并行测试**：支持多设备同时测试，提高测试效率

---

*文档版本：v1.0*
*适用脚本版本：ssd_test.py v1.7.5*
*生成日期：2026-09-10*
