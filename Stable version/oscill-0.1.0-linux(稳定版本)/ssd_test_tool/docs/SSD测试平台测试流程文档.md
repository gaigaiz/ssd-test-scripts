# SSD 测试平台测试流程文档

> 版本：v1.0 | 生成日期：2026-10-09 | 适用版本：ssd_test_tool (模块化版)

## 目录

1. [整体测试流程](#1-整体测试流程)
2. [测试项目详解](#2-测试项目详解)
   - 2.1 [设备容量测试 (SSD_ST_001)](#21-设备容量测试-ssd_st_001)
   - 2.2 [SMART 健康信息测试 (SSD_ST_002)](#22-smart-健康信息测试-ssd_st_002)
   - 2.3 [固件升级/降级测试 (SSD_ST_003)](#23-固件升级降级测试-ssd_st_003)
   - 2.4 [完整性能特征测试 (SSD_ST_004)](#24-完整性能特征测试-ssd_st_004)
   - 2.5 [读/写测试 (SSD_ST_005)](#25-读写测试-ssd_st_005)
   - 2.6 [正常电源循环测试 (SSD_ST_006)](#26-正常电源循环测试-ssd_st_006)
   - 2.7 [意外电源循环测试 SPOR (SSD_ST_007)](#27-意外电源循环测试-spor-ssd_st_007)
   - 2.8 [操作系统中断测试 OSINT (SSD_ST_008)](#28-操作系统中断测试-osint-ssd_st_008)
   - 2.9 [设备功耗测量 (SSD_ST_009)](#29-设备功耗测量-ssd_st_009)
3. [测试状态管理](#3-测试状态管理)
4. [测试结果判定标准](#4-测试结果判定标准)
5. [日志与报告输出](#5-日志与报告输出)

---

## 1. 整体测试流程

### 1.1 测试执行顺序

工具按照以下固定顺序执行选中的测试项目：

```
设备容量 → SMART健康 → 固件升降级 → 性能测试 → 读/写测试 → 正常电源循环 → 意外电源循环(SPOR) → 操作系统中断(OSINT) → 设备功耗测量
```

### 1.2 全局前置检查流程

```
启动测试
    │
    ▼
[1] Root 权限检查
    │  失败 → 报错退出（提示使用 sudo）
    ▼
[2] 依赖工具检查
    │  检查 nvme-cli、smartctl、fio、lsblk 等
    │  失败 → 报错退出
    ▼
[3] 设备存在性检查
    │  检查 /dev/nvmeX 或 /dev/sdX 是否存在
    │  失败 → 报错退出
    ▼
[4] 构建设备配置
    │  解析测试项、参数、性能任务列表
    ▼
[5] 数据销毁确认
    │  性能/固件/电源循环/SPOR/OSINT/RW 测试会破坏数据
    │  未指定 -y 时弹出确认
    │  用户取消 → 退出
    ▼
[6] 按顺序执行各测试项目
    │  每个测试独立判定 PASS/FAIL/ERROR/SKIP
    ▼
[7] 汇总测试报告
    │  输出 JSON 报告 + 详细日志
    ▼
结束
```

### 1.3 命令行启动方式

```bash
# GUI 模式
python3 -m ssd_test_tool.main --gui

# 命令行模式 - 执行全部测试
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t all -y

# 命令行模式 - 执行指定测试
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t smart capacity perf -y
```

---

## 2. 测试项目详解

### 2.1 设备容量测试 (SSD_ST_001)

**测试目的**：验证 SSD 实际容量是否符合规格，多源交叉校验容量一致性。

**测试流程**：

```
开始
  │
  ▼
[1] 设备存在性检查
  │
  ▼
[2] NVMe 方式读取容量
  │  命令：nvme id-ns / nvme list
  │  输出：total_bytes / capacity_bytes
  │
  ▼
[3] lsblk 方式读取容量
  │  命令：lsblk -b -d -o SIZE
  │  输出：size_bytes
  │
  ▼
[4] smartctl 方式读取容量（备用）
  │  命令：smartctl -i
  │  输出：size_bytes
  │
  ▼
[5] 多源交叉校验
  │  比较三种方式读取的容量值
  │  差异 ≤ 1% → 一致
  │  差异 > 1% → 警告（可能因保留空间导致）
  │
  ▼
[6] 输出容量信息
  │  十进制：如 512.1 GB
  │  二进制：如 476.9 GiB
  │
  ▼
判定：PASS（至少一种方式成功读取容量）
```

**判定标准**：
- **PASS**：至少一种方式成功读取容量
- **FAIL**：所有方式均无法读取容量
- **注意**：多源容量差异 > 1% 仅标记警告，不判定 FAIL（可能因保留空间）

---

### 2.2 SMART 健康信息测试 (SSD_ST_002)

**测试目的**：验证 SSD SMART 健康状态，通过 R/W 前后 SMART 数据对比检测异常。

**测试流程**：

```
开始
  │
  ▼
[1] 确认 OS 枚举设备
  │
  ▼
[2] 获取初始 SMART 信息（R/W 前）
  │  命令：nvme smart-log + smartctl -a
  │  采集项：温度、可用备件、介质错误、上电次数、上电时长等
  │  输出完整 SMART 数据
  │
  ▼
[3] 检查核心项（R/W 前）
  │  检查项：可用备件、温度、介质错误等
  │  仅介质错误 → 友好提示（历史累积，不影响初始判定）
  │  其他严重问题 → WARNING
  │
  ▼
[4] 执行基本 R/W 操作
  │  写入测试数据 → 读取验证
  │  失败 → FAIL
  │
  ▼
[5] 等待 3 秒后重新获取 SMART（R/W 后）
  │
  ▼
[6] 对比 R/W 前后 SMART 关键项变化
  │  对比项：上电次数、上电时长、介质错误、可用备件
  │  介质错误增加 → FAIL
  │
  ▼
[7] R/W 后核心项复检
  │  strict_media=False（介质错误绝对值不判定，仅检查增量）
  │  异常 → FAIL
  │
  ▼
判定：PASS
```

**判定标准**：
- **PASS**：R/W 前后介质错误无增加，核心项正常
- **FAIL**：基本 R/W 失败，或介质错误增加，或 R/W 后核心项异常
- **注意**：历史累积介质错误（如 5353）不影响测试结果，仅检查 R/W 后的增量

---

### 2.3 固件升级/降级测试 (SSD_ST_003)

**测试目的**：验证 SSD 固件现场升级/降级功能，确保烧写后设备功能正常。

**适用设备**：仅支持 NVMe 设备

**测试流程**：

```
开始
  │
  ▼
[1] 参数校验
  │  必须指定 --fw-image（固件镜像文件）
  │  设备类型必须为 NVMe
  │
  ▼
[2] 读取当前固件版本
  │  命令：nvme id-ctrl
  │  记录旧版本号
  │
  ▼
[3] 下载固件到指定 Slot
  │  命令：nvme fw-download
  │  失败 → FAIL
  │
  ▼
[4] 提交并激活固件（立即复位）
  │  命令：nvme fw-commit --action=2
  │  失败 → FAIL
  │
  ▼
[5] 烧写后设备功能校验
  │  检查设备是否重新枚举
  │  基本功能验证
  │  失败 → FAIL
  │
  ▼
[6] 读取新固件版本并比对
  │  版本未变化 → WARNING（镜像可能相同或提交未生效）
  │
  ▼
判定：PASS
```

**判定标准**：
- **PASS**：固件下载、提交、校验全部成功
- **FAIL**：固件下载失败、提交失败、或烧写后功能校验失败
- **ERROR**：未指定固件镜像、或设备非 NVMe

---

### 2.4 完整性能特征测试 (SSD_ST_004)

**测试目的**：在指定 SSD 状态（FOB/Steady/Unknown）下，使用 fio 执行完整性能测试，采集读写 IOPS、带宽、延迟等指标。

**测试状态说明**：

| 状态 | 说明 | 预处理 |
|------|------|--------|
| FOB (Fresh Out of Box) | 全新状态 | NVMe User Data Erase 格式化 |
| Steady (稳态) | 稳定状态 | WIPC + WDPC + 稳态检测 |
| Unknown | 未知状态 | 不进行任何预处理，直接测试 |

**测试流程**：

```
开始
  │
  ▼
[1] 确认设备未挂载（警告提示）
  │
  ▼
[2] 根据目标状态执行对应测试
  │
  ├─ FOB 状态 ──────────────────────────
  │   [2.1] 执行 NVMe User Data Erase
  │   │     方法：自动选择（crypto-erase / user-data-erase）
  │   │     失败 → FAIL
  │   [2.2] 等待 10 秒稳定
  │   [2.3] 保存 SSD 状态为 FOB
  │   [2.4] 执行 fio 性能测试
  │
  ├─ Steady 状态 ───────────────────────
  │   [2.1] WIPC（Write Initialization Pattern Conditioning）
  │   │     顺序写入全盘 2 次
  │   [2.2] WDPC（Write Delta Performance Conditioning）
  │   │     随机写入，最多 25 轮
  │   [2.3] 稳态检测
  │   │     连续 5 轮性能波动 < 10% → 达到稳态
  │   │     超过最大轮数未达稳态 → 警告
  │   [2.4] 保存 SSD 状态为 Steady
  │   [2.5] 执行 fio 性能测试
  │
  └─ Unknown 状态 ──────────────────────
      [2.1] 跳过预处理
      [2.2] 直接执行 fio 性能测试
  │
  ▼
[3] fio 性能测试（全参数可配置）
  │  参数：rw、bs、iodepth、numjobs、size、runtime、direct、ioengine
  │  支持混合读写（randrw，可配置读比例）
  │  支持多任务批量测试（--perf-task-file）
  │
  ▼
[4] 解析 fio 输出
  │  采集：读 IOPS、写 IOPS、读带宽、写带宽、读延迟、写延迟
  │
  ▼
[5] 性能有效性检查
  │  至少一项测试返回有效 IOPS 数据
  │  预处理失败导致未执行 → FAIL
  │
  ▼
判定：PASS
```

**fio 测试参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| rw | randread | 读写模式（randread/randwrite/randrw/read/write） |
| bs | 4k | 块大小 |
| iodepth | 64 | IO 队列深度 |
| numjobs | 1 | 并发任务数 |
| size | 3% | 测试数据大小 |
| runtime | 10s | 测试运行时长 |
| direct | 1 | 使用 DIRECT IO（绕过页缓存） |
| ioengine | libaio | IO 引擎 |

**判定标准**：
- **PASS**：性能测试执行完成，返回有效数据
- **FAIL**：稳态预处理失败，或性能测试无有效数据
- **注意**：超过最大轮数未达到稳态仅标记警告，不判定 FAIL

---

### 2.5 读/写测试 (SSD_ST_005)

**测试目的**：验证 SSD 数据读写完整性，支持全磁盘验证、文件循环、长期运行三种模式。

**测试模式**：

| 模式 | 说明 |
|------|------|
| full_disk | 全磁盘读写验证 |
| file_cycle | 文件循环读写（多文件大小、多轮次） |
| long_run | 长期运行测试（可配置时长） |
| all | 执行以上全部模式 |

**测试流程**：

```
开始
  │
  ▼
[1] 初始设备在线检查
  │  失败 → FAIL
  │
  ▼
[2] 初始 SMART 检查
  │
  ▼
[3] 根据测试模式执行对应子测试
  │
  ├─ 全磁盘验证 (full_disk) ────────────
  │   [3.1] 写入测试 Pattern 到全盘
  │   [3.2] 读取并校验数据完整性
  │   [3.3] 支持 MD5 校验
  │
  ├─ 文件循环 (file_cycle) ──────────────
  │   [3.1] 创建测试分区和文件系统
  │   [3.2] 按配置的文件大小列表循环
  │   │     如：256MB, 1GB, 4GB, 16GB
  │   [3.3] 每轮：写入文件 → 读取校验 → 删除
  │   [3.4] 支持多轮次（--rw-cycles）
  │   [3.5] 支持随机/AA/55/00/FF Pattern
  │
  └─ 长期运行 (long_run) ────────────────
      [3.1] 持续读写测试，可配置时长（默认 24 小时）
      [3.2] 支持 fio 参数配置（bs、iodepth、numjobs）
      [3.3] 支持混合读写（可配置读比例）
      [3.4] 定期检查 SMART 状态
  │
  ▼
[4] 汇总各子测试结果
  │  全部通过 → PASS
  │  任一失败 → FAIL
  │
  ▼
[5] 清除状态文件（测试正常完成）
  │
  ▼
判定：PASS / FAIL
```

**文件循环测试参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| rw-file-sizes | 256MB | 测试文件大小列表（逗号分隔） |
| rw-cycles | 3 | 循环轮次 |
| rw-pattern | random | 数据 Pattern（random/aa/55/00/ff） |
| rw-verify | md5 | 校验方式（md5/crc） |

**长期运行测试参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| rw-long-hours | 24 | 测试时长（小时） |
| rw-block-size | 4k | 块大小 |
| rw-iodepth | 32 | IO 队列深度 |
| rw-numjobs | 4 | 并发任务数 |
| rw-mixed-read-ratio | 70 | 混合读写读比例（%） |

**判定标准**：
- **PASS**：所有选中的子测试模式全部通过
- **FAIL**：任一子测试模式失败

---

### 2.6 正常电源循环测试 (SSD_ST_006)

**测试目的**：验证 SSD 在正常开关机循环下的稳定性和数据完整性。

**测试模式**：

| 模式 | 说明 | 断电方式 |
|------|------|----------|
| manual | 手动模式 | 提示用户手动断电/上电 |
| ipmi | IPMI 远程模式 | 通过 IPMI 远程控制电源 |
| enhanced | 增强模式 | LBA 级 Pattern 校验 + PCIe Link 检查 |

**测试流程**（状态机 + 断点恢复）：

```
开始
  │
  ▼
[1] 加载状态文件（判断是否为重启后恢复）
  │
  ├─ 全新开始 (setup 阶段) ─────────────
  │   [1.1] 创建测试分区和文件系统
  │   [1.2] 写入完整性测试数据
  │   [1.3] enhanced 模式：写入已知 Pattern 数据
  │   [1.4] enhanced 模式：记录初始 PCIe Link 状态
  │   [1.5] 保存状态，进入 rw_load 阶段
  │
  ├─ 重启后恢复 (boot_check 阶段) ──────
  │   [1.1] 重新挂载测试分区
  │   [1.2] 开机后检查
  │   │     enhanced 模式：增强综合检查（数据完整性 + PCIe Link + SMART）
  │   │     其他模式：原检查（设备枚举 + 数据完整性）
  │   [1.3] 记录本轮检查结果
  │   [1.4] 检查失败 → 进入最终测试阶段
  │   [1.5] 检查通过 → 继续下一轮
  │
  ▼
[2] 执行 R/W 负载（每轮循环前）
  │  可配置 R/W 时长（--pc-rw-duration）
  │
  ▼
[3] 正常关机
  │  manual 模式：提示用户手动关机
  │  ipmi 模式：IPMI 远程关机
  │  enhanced 模式：IPMI 关机或手动关机
  │
  ▼
[4] 等待上电（进程终止，开机自启恢复）
  │
  ▼
[5] 重复步骤 [1]-[4]，直到完成目标轮次
  │
  ▼
[6] 最终功能测试（全部循环完成后）
  │  完整 SMART 检查 + 数据完整性校验
  │
  ▼
[7] 清除状态文件
  │
  ▼
判定：PASS / FAIL
```

**测试参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| pc-cycles | 100 | 电源循环次数 |
| pc-power-mode | manual | 电源模式（manual/ipmi/enhanced） |
| pc-mount-point | /mnt/ssd_pc | 测试分区挂载点 |
| pc-boot-timeout | 180s | 开机超时时间 |
| pc-off-interval | 10s | 断电间隔时间 |
| pc-rw-duration | 60s | 每轮 R/W 负载时长 |
| pc-pattern | random | enhanced 模式数据 Pattern |
| pc-pattern-size-gb | 10 | enhanced 模式 Pattern 数据大小（GB） |

**判定标准**：
- **PASS**：所有循环轮次开机检查通过，最终功能测试通过
- **FAIL**：任一轮次开机检查失败，或最终功能测试失败

---

### 2.7 意外电源循环测试 SPOR (SSD_ST_007)

**测试目的**：验证 SSD 在意外断电（非正常关机）情况下的数据完整性和恢复能力。

**测试模式**：

| 模式 | 说明 | 断电方式 |
|------|------|----------|
| manual | 手动模式 | 提示用户手动断电 |
| ipmi | IPMI 远程模式 | 通过 IPMI 远程硬断电 |
| timeboard | Timeboard 定时断电 | 通过 Timeboard 硬件定时精确断电 |
| enhanced | 增强模式 | 高 QD/BS 混合读写 + 精确断电延迟 |

**测试流程**（两阶段状态机）：

```
开始
  │
  ▼
[1] 加载或初始化状态文件
  │  状态：total_cycles、current_cycle、phase、results
  │
  ▼
[2] 根据阶段执行
  │
  ├─ 阶段1：掉电前 (PHASE_POWEROFF) ────
  │   [2.1] 初始化测试分区（首次）
  │   [2.2] 写入完整性测试数据
  │   [2.3] 启动 R/W 负载
  │   │     支持混合读写（可配置读比例）
  │   │     enhanced 模式：高 QD（默认 128）+ 大 BS（默认 128k）
  │   [2.4] 等待延迟时间（--spor-delay，默认 30s）
  │   [2.5] 执行意外断电
  │   │     manual：提示用户手动断电
  │   │     ipmi：IPMI 远程硬断电
  │   │     timeboard：Timeboard 定时精确断电（可配置延迟 ms）
  │   [2.6] 进程终止，等待上电自启恢复
  │
  └─ 阶段2：上电后 (PHASE_POWERON) ─────
      [2.1] 等待设备重新枚举
      [2.2] 上电后检查
      │     设备枚举检查
      │     数据完整性校验
      │     SMART 健康检查
      [2.3] 记录本轮结果
      [2.4] 未完成全部轮次 → 回到阶段1
      [2.5] 完成全部轮次 → 最终功能测试
  │
  ▼
[3] 最终功能测试（全部循环完成后）
  │  完整 SMART 检查 + 数据完整性校验
  │
  ▼
[4] 清除状态文件
  │
  ▼
判定：PASS / FAIL
```

**测试参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| spor-cycles | 100 | SPOR 循环次数 |
| spor-delay | 30s | R/W 后断电延迟时间 |
| spor-power-mode | manual | 电源模式（manual/ipmi/timeboard/enhanced） |
| spor-poweroff-delay-ms | 0 | 硬件断电延迟（毫秒，timeboard 模式） |
| spor-mixed-rw | Off | 是否启用混合读写 |
| spor-mixed-read-ratio | 70 | 混合读写读比例（%） |
| spor-test-size | 5GB | 测试数据大小 |
| spor-final-test | On | 是否执行最终功能测试 |
| spor-enhanced-iodepth | 128 | enhanced 模式 IO 队列深度 |
| spor-enhanced-bs | 128k | enhanced 模式块大小 |

**判定标准**：
- **PASS**：所有循环轮次上电后检查通过，最终功能测试通过
- **FAIL**：任一轮次上电后检查失败，或最终功能测试失败

---

### 2.8 操作系统中断测试 OSINT (SSD_ST_008)

**测试目的**：验证 SSD 在操作系统休眠/唤醒（S3/S4）中断情况下的稳定性和数据完整性。

**休眠类型**：

| 类型 | 说明 |
|------|------|
| s3 | 待机（Suspend to RAM） |
| s4 | 休眠（Suspend to Disk） |
| both | 交替执行 S3 和 S4 |

**测试流程**：

```
开始
  │
  ▼
[1] 检查 rtcwake 工具是否可用
  │  失败 → ERROR（提示安装 util-linux）
  │
  ▼
[2] 加载或初始化状态文件
  │  状态有效性检查：已完成/轮次不匹配/轮次错误 → 自动重置
  │
  ▼
[3] 初始化（首次）
  │  [3.1] 创建测试分区和文件系统
  │  [3.2] 写入完整性测试数据
  │  [3.3] 保存状态，进入 running 阶段
  │
  ▼
[4] 执行循环（从当前轮次到目标轮次）
  │
  │   ┌─────────────────────────────────┐
  │   │  每轮循环：                       │
  │   │                                   │
  │   │  [4.1] 启动 IO 负载（可选）      │
  │   │        active_io=On  → 持续读写  │
  │   │        active_io=Off → 空闲状态   │
  │   │        IO 时长可配置（默认 60s） │
  │   │                                   │
  │   │  [4.2] 执行系统休眠              │
  │   │        命令：rtcwake -m <type>   │
  │   │        -m mem  → S3 待机         │
  │   │        -m disk → S4 休眠         │
  │   │        休眠时长可配置（默认 10s） │
  │   │                                   │
  │   │  [4.3] 系统自动唤醒              │
  │   │                                   │
  │   │  [4.4] 唤醒后检查                │
  │   │        - 设备重新枚举             │
  │   │        - 数据完整性校验           │
  │   │        - SMART 健康检查           │
  │   │                                   │
  │   │  [4.5] 记录本轮结果              │
  │   └─────────────────────────────────┘
  │
  ▼
[5] 全部循环完成后
  │  汇总结果
  │
  ▼
[6] 清除状态文件
  │
  ▼
判定：PASS / FAIL
```

**测试参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| osint-cycles | 100 | OSINT 循环次数 |
| osint-sleep-type | s3 | 休眠类型（s3/s4/both） |
| osint-sleep-duration | 10s | 休眠时长 |
| osint-io-idle | Off | 是否空闲状态（默认活跃 IO） |
| osint-io-duration | 60s | 休眠前 IO 负载时长 |
| osint-mount-point | /mnt/ssd_osint | 测试分区挂载点 |

**判定标准**：
- **PASS**：所有循环轮次唤醒后检查通过
- **FAIL**：任一轮次唤醒后检查失败

---

### 2.9 设备功耗测量 (SSD_ST_009)

**测试目的**：通过示波器采集 SSD 在空闲和活动 R/W 状态下的电压和电流，计算功耗，验证电源是否符合设计规格。

**测试方式**：启动 oscill 示波器功耗测量工具（独立 GUI），用户在 oscill 中完成测量，ssd_test 自动检测 JSON 报告并解析。

**测试流程**：

```
开始
  │
  ▼
[1] 解析 oscill 项目路径
  │  默认：ssd_test_tool 同级目录下的 oscill 项目
  │  可通过 --oscill-path 指定
  │
  ▼
[2] 验证 oscill 路径有效性
  │  检查 oscill/gui.py 是否存在
  │  失败 → ERROR
  │
  ▼
[3] 构建启动命令并启动 oscill GUI
  │  命令：python3 -m oscill.gui
  │  工作目录：oscill 项目路径
  │  环境变量：传递自动采集配置
  │    - 测试计划（POWER-05 等）
  │    - 总采集时间
  │    - 采集时间间隔
  │    - 通道选择（CH1/CH2/CH3/CH4）
  │    - JSON 报告输出目录
  │
  ▼
[4] oscill 启动为自动采集模式
  │  自动选中测试计划
  │  自动选中通道（与 ssd_test 参数双向映射）
  │  自动设置总采集时间和间隔（单向映射）
  │  用户点击"开始连续测量"开始采集
  │  到达总采集时间后自动停止
  │  oscill 窗口保持打开（不自动关闭）
  │
  ▼
[5] ssd_test 轮询检测自动导出的 JSON 报告
  │  监控目录：output_dir（报告输出目录）
  │  检测文件名：power_report_combined_*.json
  │  排除启动前已有的旧报告
  │  文件大小连续 2 次稳定（间隔 1 秒）→ 认为写入完成
  │  最大等待时间：1 小时
  │
  ▼
[6] 检测到 JSON 报告后解析
  │  解析内容：
  │    - 各时间戳的电压、电流、功耗
  │    - 平均功耗
  │    - 峰值功耗
  │    - 总能耗
  │  与功耗限值（--power-limit）对比
  │  超过限值 → FAIL
  │
  ▼
[7] 输出功耗测试结果
  │  详细日志（纯英文）
  │  JSON 报告
  │  可转换为 LOG 日志文件
  │
  ▼
判定：PASS / FAIL
```

**oscill 自动采集配置映射**：

| ssd_test 参数 | oscill 配置 | 映射方向 |
|---------------|-------------|----------|
| --power-plan | 测试计划（如 POWER-05） | 单向 |
| --power-total-duration | 总采集时间 | 单向 |
| --power-sample-interval | 采集时间间隔 | 单向 |
| --power-channels | 通道选择（CH1/CH2/CH3/CH4） | **双向** |
| --power-json-report | JSON 报告输出目录 | 单向 |

**测试参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| oscill-path | 自动检测 | oscill 项目路径 |
| power-limit | 无 | 功耗限值（W），超过则 FAIL |
| power-plan | POWER-05 | 功耗测试计划 |
| power-total-duration | 10s | 总采集时间（秒） |
| power-sample-interval | 2s | 采集时间间隔（秒） |
| power-channels | CH1,CH2 | 采集通道（逗号分隔） |
| power-json-report | output_dir | JSON 报告输出目录 |

**JSON 报告格式**（整合多时间戳）：

```json
{
  "test_plan": "POWER-05",
  "total_duration": 10.0,
  "sample_interval": 2.0,
  "channels": ["CH1", "CH2"],
  "samples": [
    {
      "timestamp": "2026-10-09 10:00:02",
      "elapsed_sec": 2.0,
      "CH1": {"voltage": 3.30, "current": 0.50, "power": 1.65},
      "CH2": {"voltage": 12.0, "current": 0.20, "power": 2.40}
    },
    {
      "timestamp": "2026-10-09 10:00:04",
      "elapsed_sec": 4.0,
      "CH1": {"voltage": 3.31, "current": 0.52, "power": 1.72},
      "CH2": {"voltage": 11.98, "current": 0.21, "power": 2.52}
    }
  ],
  "summary": {
    "average_power": 2.07,
    "peak_power": 2.52,
    "total_energy": 20.7
  }
}
```

**判定标准**：
- **PASS**：成功采集功耗数据，且平均/峰值功耗未超过限值
- **FAIL**：功耗超过限值
- **ERROR**：oscill 路径无效，或超时未检测到 JSON 报告
- **SKIP**：用户未进行测量直接关闭 oscill（未生成 JSON 报告）

---

## 3. 测试状态管理

### 3.1 SSD 状态文件

工具通过状态文件持久化 SSD 当前状态，支持跨重启恢复。

**状态类型**：

| 状态 | 说明 | 触发方式 |
|------|------|----------|
| unknown | 未知状态 | 默认 |
| fob | Fresh Out of Box（全新） | NVMe User Data Erase 后 |
| steady | 稳态 | WIPC + WDPC + 稳态检测后 |

**状态文件位置**：`~/.ssd_test_state/<serial>.json`

**状态管理命令**：

```bash
# 查询当前 SSD 状态
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 --action status

# 仅进入 FOB 状态（不执行性能测试）
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 --action enter-fob

# 仅进入稳态（不执行性能测试）
sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 --action enter-steady
```

### 3.2 测试项目状态文件

电源循环、SPOR、OSINT 等需要重启的测试项目，通过独立的状态文件实现断点恢复。

**状态文件位置**：可通过 `--pc-state-file`、`--spor-state-file`、`--osint-state-file` 指定。

**状态恢复机制**：
- 测试启动时自动加载状态文件
- 判断当前阶段（setup/rw_load/boot_check/poweron/done）
- 从断点继续执行，无需从头开始
- 测试正常完成后自动清除状态文件

---

## 4. 测试结果判定标准

### 4.1 测试状态定义

| 状态 | 说明 |
|------|------|
| PASS | 测试通过 |
| FAIL | 测试失败（断言不通过） |
| ERROR | 测试错误（异常/配置错误/工具缺失） |
| SKIP | 测试跳过（dry-run 模式/用户未操作） |

### 4.2 标准摘要输出格式

每个测试项目完成后输出标准摘要行：

```
[SUMMARY] HH:MM:SS - [SSD_ST_XXX]TEST_NAME,Status:PASS/FAIL/ERROR,TestTime:X S
```

**测试项目编号对照**：

| 编号 | 测试项目 | 英文名称 |
|------|----------|----------|
| SSD_ST_001 | 设备容量 | CAPACITY_TEST |
| SSD_ST_002 | SMART 健康 | SMART_TEST |
| SSD_ST_003 | 固件升降级 | FW_TEST |
| SSD_ST_004 | 性能测试 | PERFORMANCE_TEST |
| SSD_ST_005 | 读/写测试 | RW_TEST |
| SSD_ST_006 | 正常电源循环 | POWER_CYCLE_TEST |
| SSD_ST_007 | 意外电源循环 | SPOR_TEST |
| SSD_ST_008 | 操作系统中断 | OSINT_TEST |
| SSD_ST_009 | 设备功耗测量 | POWER_CONSUMPTION_TEST |

### 4.3 整体测试汇总

全部测试完成后输出汇总：

```
Total: X PASS / Y FAIL / Z ERROR / W SKIP
Test completed - All passed (exit code: 0)
Test completed - Some items failed (exit code: 1)
```

**退出码**：
- `0`：全部测试通过
- `1`：存在失败或错误的测试项

---

## 5. 日志与报告输出

### 5.1 详细日志

**日志文件命名**：`ssd_test_YYYYMMDD_HHMMSS.log`

**默认日志目录**：
- 命令行模式：`./logs/`
- GUI 模式：`ssd_test_tool/logs/`
- 可通过 `--log-dir` 指定

**日志级别**：
- `INFO`：正常信息
- `WARNING`：警告信息（不影响测试结果）
- `ERROR`：错误信息
- `DEBUG`：调试信息（`--verbose` 启用）

**日志语言**：全部为英文输出

### 5.2 JSON 测试报告

**报告文件命名**：`test_report_YYYYMMDD_HHMMSS.json`

**默认报告目录**：
- 命令行模式：`./reports/`（`-o` 指定）
- GUI 模式：`ssd_test_tool/reports/`

**报告内容**：
- 测试配置
- 各测试项目结果（PASS/FAIL/ERROR/SKIP）
- 各测试项目详细数据
- 测试时间统计
- 整体汇总

### 5.3 功耗测试专用报告

**JSON 报告**：`power_report_combined_YYYYMMDD_HHMMSS.json`
- 整合多时间戳的功耗采集数据
- 包含平均功耗、峰值功耗、总能耗

**LOG 日志**：可通过 GUI 上的"转换为 Log"按钮将 JSON 报告转换为纯英文 LOG 日志文件。

---

## 附录：常用命令行参数速查

### 通用参数

| 参数 | 说明 |
|------|------|
| `-d, --device` | 测试设备路径（如 /dev/nvme0n1） |
| `-t, --test` | 测试项（smart/capacity/perf/rw/powercycle/spor/osint/power/all） |
| `-y, --yes` | 跳过数据销毁确认 |
| `-o, --output-dir` | 报告输出目录 |
| `--log-dir` | 日志输出目录 |
| `--dry-run` | 试运行模式（不执行实际操作） |
| `--verbose` | 详细调试日志 |
| `--gui` | 启动 GUI 模式 |

### 性能测试参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--perf-state` | unknown | SSD 状态（fob/steady/unknown） |
| `--perf-name` | perf-test | 测试名称 |
| `--perf-rw` | randread | 读写模式 |
| `--perf-bs` | 4k | 块大小 |
| `--perf-iodepth` | 64 | IO 队列深度 |
| `--perf-numjobs` | 1 | 并发任务数 |
| `--perf-size` | 3% | 测试数据大小 |
| `--perf-runtime` | 10 | 测试时长（秒） |
| `--perf-direct` | 1 | DIRECT IO |
| `--perf-ioengine` | libaio | IO 引擎 |
| `--perf-rwmixread` | 70 | 混合读写读比例（%） |
| `--perf-task-file` | 无 | 多任务批量测试配置文件（JSON） |

### 功耗测试参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--oscill-path` | 自动检测 | oscill 项目路径 |
| `--power-limit` | 无 | 功耗限值（W） |
| `--power-plan` | POWER-05 | 测试计划 |
| `--power-total-duration` | 10 | 总采集时间（秒） |
| `--power-sample-interval` | 2 | 采集间隔（秒） |
| `--power-channels` | CH1,CH2 | 采集通道 |
| `--power-json-report` | output_dir | JSON 报告目录 |

---

> 文档结束
