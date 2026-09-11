# 操作系统中断测试 (OSINT) 使用步骤

> 基于 `ssd_test_v1.8.2.py` 脚本，对应 DVT/EVT 测试计划中 **S3/S4 OS Interruption** 测试项。

---

## 一、测试目的

验证 SSD（作为次级驱动器）在**多操作系统**下持续 I/O 期间，**S3（挂起到内存）/ S4（挂起到磁盘/休眠）休眠与唤醒**时的稳定性与数据完整性。

对应文档要求：
- **DVT Test Plan — Table 10 S3/S4 Test**：验证 SSD 能从 S3/S4 休眠稳定唤醒，Windows 10 + CentOS Stream 9，工具 Burn-in 10 / MD5 check
- **EVT Test Plan — Table 13 S3/S4 OS interruption**：室温下 S3&S4 中断压力测试，工具 Burn-in 10 / Sleeper，1k 循环

---

## 二、测试环境要求

| 项目 | 要求 |
|------|------|
| 测试平台 | x86_64 PC（支持 S3/S4 休眠的主板） |
| Host Controller | PCIe 4.0 / NVMe 1.4 |
| 操作系统 | Linux（Ubuntu/CentOS Stream 等，脚本原生支持）；Windows 需使用 Passmark 工具（见附录） |
| 待测 SSD | 安装为**次级驱动器**（非系统盘），设备路径如 `/dev/nvme0n1` |
| 权限 | **必须 root**（`sudo` 运行） |
| 依赖工具 | `nvme-cli`, `smartmontools`, `fio`, `util-linux`（含 rtcwake）, `e2fsprogs`, `parted` |

### 依赖安装（Ubuntu/Debian）

```bash
sudo apt update
sudo apt install -y nvme-cli smartmontools fio util-linux e2fsprogs parted
```

### 验证 rtcwake 可用

```bash
which rtcwake   # 应输出 /usr/sbin/rtcwake
sudo rtcwake --help | head -5
```

---

## 三、工具说明

脚本在 Linux 下使用以下开源工具替代文档中的 Passmark 商业工具：

| 文档工具 | 脚本替代工具 | 用途 |
|----------|-------------|------|
| Passmark Burn-In | **fio** | 持续混合读写 I/O 负载 |
| Passmark Sleeper | **rtcwake** | S3/S4 休眠 + RTC 闹钟定时唤醒（无需人工干预） |
| systemctl suspend | **rtcwake -m mem** | S3 挂起到内存 |
| systemctl hibernate | **rtcwake -m disk** | S4 挂起到磁盘 |
| MD5 check | **SHA-256** | 数据完整性校验（更安全的哈希算法） |

> `rtcwake` 优势：设置 RTC 闹钟后进入指定休眠状态，到点自动唤醒，全程无人值守，适合千次级循环测试。

---

## 四、前置准备

### 4.1 确认待测设备

```bash
lsblk
# 确认待测 SSD 设备路径，例如 /dev/nvme0n1
# 注意：务必确认不是系统盘！测试会创建分区并写入数据
```

### 4.2 确认系统支持 S3/S4

```bash
# 查看系统支持的休眠状态
cat /sys/power/state
# 常见输出: freeze mem disk
#   mem  = S3 (挂起到内存)
#   disk = S4 (挂起到磁盘)

# 手动测试一次 S3 休眠唤醒（30秒后自动唤醒）
sudo rtcwake -m mem -s 30
# 如果系统能正常休眠并在30秒后自动唤醒，说明 S3 可用
```

### 4.3 备份数据

> ⚠️ **警告**：OSINT 测试会在待测 SSD 上创建分区、格式化 ext4、写入测试数据。请确保待测 SSD 上无重要数据，或已备份。

---

## 五、命令行模式使用步骤

### 5.1 基础命令（默认参数）

```bash
cd /path/to/script
sudo python3 ssd_test_v1.8.2.py \
  -d /dev/nvme0n1 \
  -t osint \
  -y
```

参数说明：
- `-d /dev/nvme0n1`：待测 SSD 设备路径
- `-t osint`：指定执行操作系统中断测试
- `-y`：跳过数据销毁确认（自动确认）

默认参数：10 轮循环、S3 休眠、每次休眠 30 秒、活跃 IO 模式（休眠前 60 秒混合读写）。

### 5.2 完整参数示例（模拟 DVT/EVT 规范）

```bash
sudo python3 ssd_test_v1.8.2.py \
  -d /dev/nvme0n1 \
  -t osint \
  --osint-cycles 100 \
  --osint-sleep-type both \
  --osint-sleep-duration 60 \
  --osint-io-duration 120 \
  --osint-mount-point /mnt/ssd_osint \
  -y
```

该示例：100 轮循环，每轮依次执行 S3 和 S4 休眠（共 200 次休眠唤醒），每次休眠 60 秒，休眠前持续 120 秒混合读写 IO。

### 5.3 空闲模式（无 IO 负载休眠）

```bash
sudo python3 ssd_test_v1.8.2.py \
  -d /dev/nvme0n1 \
  -t osint \
  --osint-cycles 50 \
  --osint-sleep-type s3 \
  --osint-io-idle \
  -y
```

### 5.4 Dry-Run 试运行（不实际执行，仅打印配置）

```bash
sudo python3 ssd_test_v1.8.2.py \
  -d /dev/nvme0n1 \
  -t osint \
  --dry-run
```

---

## 六、GUI 模式使用步骤

### 6.1 启动 GUI

```bash
sudo python3 ssd_test_v1.8.2.py
```

### 6.2 配置测试

1. **顶部设备选择栏**：选择待测 SSD（如 `/dev/nvme0n1`），点击「扫描设备」确认
2. **左侧测试项选择**：勾选「操作系统中断」（OSINT），取消其他不需要的测试项
3. **右侧参数配置 → 切换到「操作系统中断(OSINT)」标签页**，配置以下参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 循环次数 | 10 | 总循环次数 |
| 休眠类型 | s3 | s3(挂起到内存) / s4(挂起到磁盘) / both(交替执行) |
| 休眠时长(秒) | 30 | 每次休眠持续时间，rtcwake 定时唤醒 |
| 空闲状态休眠 | 未勾选 | 勾选后不启动 IO 负载，空闲状态下休眠 |
| IO持续时长(秒) | 60 | 每轮休眠前持续混合读写的秒数 |
| 挂载点 | /mnt/ssd_osint | 测试分区挂载点 |

4. **点击顶部绿色「▶ 开始测试」按钮**启动

### 6.3 查看结果

- 底部「测试结果摘要」区域实时显示每轮测试日志
- 详细日志保存在 `./logs/` 目录
- JSON 报告保存在 `./reports/` 目录

---

## 七、全部参数说明

| CLI 参数 | 默认值 | 说明 |
|----------|--------|------|
| `-d, --device` | 必填 | 待测 SSD 设备路径，如 `/dev/nvme0n1` |
| `-t, --test` | 必填 | 测试项，填 `osint` |
| `-y, --yes` | 关闭 | 跳过数据销毁确认 |
| `--dry-run` | 关闭 | 试运行模式，不实际执行 |
| `--osint-cycles` | 10 | OSINT 测试循环次数 |
| `--osint-sleep-type` | s3 | 休眠类型：`s3` / `s4` / `both` |
| `--osint-sleep-duration` | 30 | 每次休眠持续秒数（rtcwake 定时唤醒） |
| `--osint-io-idle` | 关闭 | 空闲状态休眠（不启动 IO 负载） |
| `--osint-io-duration` | 60 | 每轮休眠前持续 IO 秒数 |
| `--osint-mount-point` | /mnt/ssd_osint | 测试分区挂载点 |
| `--osint-state-file` | /var/lib/ssd_osint_state.json | 状态文件路径（断点续测） |
| `-o, --output-dir` | ./reports | 报告输出目录 |
| `--log-dir` | ./logs | 日志输出目录 |

---

## 八、测试流程详解

### 8.1 初始化阶段（仅首次执行）

1. **创建测试分区**：在待测 SSD 末尾创建一个测试分区（不影响已有分区），格式化为 ext4
2. **写入完整性测试数据**：在挂载点写入指定大小（默认 512MB）的测试文件，计算并记录 SHA-256 校验和
3. **保存状态文件**：初始化状态到 `/var/lib/ssd_osint_state.json`

### 8.2 每轮循环（5 步）

```
┌─────────────────────────────────────────────────┐
│  Step 1/5: 启动 IO 负载                          │
│  ├─ 活跃模式: 启动 fio 混合读写 (randrw, 70%读)  │
│  │  持续运行 --osint-io-duration 秒 (默认60s)     │
│  └─ 空闲模式: 不启动 IO，直接进入休眠              │
├─────────────────────────────────────────────────┤
│  Step 2/5: 触发 S3/S4 休眠                       │
│  ├─ rtcwake -m mem -s <duration>   (S3)         │
│  ├─ rtcwake -m disk -s <duration>  (S4)         │
│  └─ 系统进入休眠，RTC 闹钟到点后自动唤醒           │
├─────────────────────────────────────────────────┤
│  Step 3/5: 停止 IO 负载                          │
│  └─ pkill fio，等待 IO 进程退出                   │
├─────────────────────────────────────────────────┤
│  Step 4/5: 唤醒后检查（4 项）                    │
│  ├─ [4a] 磁盘标识检查: /dev/disk/by-id/ 一致性    │
│  ├─ [4b] 分区和文件系统检查: 分区表可读取、可挂载  │
│  ├─ [4c] 数据完整性校验: SHA-256 校验和比对       │
│  └─ [4d] SMART 健康检查: nvme smart-log 无错误    │
├─────────────────────────────────────────────────┤
│  Step 5/5: 记录结果，保存状态，进入下一轮          │
└─────────────────────────────────────────────────┘
```

### 8.3 最终完整功能测试（全部循环完成后）

1. **容量检查**：`lsblk` 确认容量正常
2. **SMART 检查**：`nvme smart-log` 确认健康状态
3. **顺序读性能**：fio 128K QD32 读 30 秒，确认带宽 > 0
4. **顺序写性能**：fio 128K QD32 写 30 秒，确认带宽 > 0

### 8.4 断点续测

脚本使用状态文件（默认 `/var/lib/ssd_osint_state.json`）记录进度。如果测试中断（如系统崩溃、手动停止），重新运行相同命令会自动从断点继续，不会重复已完成的循环。

如需从头开始测试，删除状态文件即可：
```bash
sudo rm /var/lib/ssd_osint_state.json
```

---

## 九、结果判读

### 9.1 日志中的 PASS/FAIL

每轮循环结束后，日志会输出：
```
  Step 5/5: 本轮结果: PASS (磁盘标识=OK, 分区/FS=OK, 数据完整性=OK, SMART=OK)
```

四项检查全部 OK 则本轮 PASS，任一项 FAIL 则本轮 FAIL。

### 9.2 最终汇总

全部循环完成后，日志输出最终汇总：
```
  OSINT 测试完成 - 结果汇总
  总循环数: 100
  通过: 100
  失败: 0
  最终功能测试: PASS
```

### 9.3 JSON 报告

报告文件保存在 `./reports/ssd_test_report_*.json`，OSINT 部分包含：
- `cycles_executed`：实际执行轮数
- `cycles_passed`：通过轮数
- `cycles_failed`：失败轮数
- `final_test`：最终功能测试结果
- 每轮详细结果（磁盘标识/分区/数据完整性/SMART 各项状态）

---

## 十、多操作系统测试

文档要求在 Windows 和 Linux 多操作系统下验证。脚本原生支持 Linux，Windows 下测试方案：

### Linux 下（推荐，脚本全自动）

直接使用本脚本，支持 S3/S4/both，千次级循环无人值守。

### Windows 下（使用 Passmark 工具）

1. **Passmark BurnInTest**：配置持续磁盘读写负载（对应脚本中的 fio）
2. **Passmark Sleeper**：配置 S3/S4 休眠唤醒循环（对应脚本中的 rtcwake）
3. 测试完成后使用 CrystalDiskInfo 检查 SMART，使用 MD5 校验工具验证数据完整性

> 建议：同一 SSD 先在 Linux 下用脚本完成自动化测试，再在 Windows 下用 Passmark 工具交叉验证。

---

## 十一、注意事项与常见问题

### ⚠️ 安全注意

1. **确认设备路径**：`-d` 参数务必指向待测 SSD，**绝不能指向系统盘**（`/dev/sda` 或 `/dev/nvme0n1` 如果是系统盘）
2. **数据备份**：测试会在待测盘上创建分区并写入数据，执行前务必备份
3. **必须 root**：rtcwake、分区操作、挂载都需要 root 权限

### 常见问题

**Q: rtcwake 报错 "not found"？**
A: 安装 util-linux：`sudo apt install util-linux`

**Q: S4 (disk) 休眠失败？**
A: 确认系统配置了交换分区且交换分区大小 >= 内存大小；检查 `cat /sys/power/state` 是否包含 `disk`。部分主板 BIOS 需要启用 S4 支持。

**Q: 测试过程中系统无法唤醒？**
A: 这本身就是测试要发现的问题。强制重启后，脚本会通过状态文件断点续测。记录失败轮次用于分析。

**Q: fio IO 负载参数是什么？**
A: 混合读写模式：`--rw=randrw --rwmixread=70 --bs=4k --iodepth=32 --numjobs=1 --direct=1`，模拟典型应用负载。

**Q: 如何调整测试文件大小？**
A: 修改脚本中 `osint_test_file_size_mb` 配置（默认 512MB），或在 TestConfig 中设置。文件越大，数据完整性校验越严格。

**Q: 千次级循环需要多久？**
A: 每轮 ≈ IO持续时间(60s) + 休眠时间(30s) + 唤醒检查(15s) ≈ 105秒。1000轮 ≈ 29小时。建议使用 `both` 模式时注意时间翻倍。

---

## 附录：快速命令参考

```bash
# 1. 快速测试（10轮S3，验证环境可用）
sudo python3 ssd_test_v1.8.2.py -d /dev/nvme0n1 -t osint -y

# 2. 标准 DVT 测试（100轮 S3+S4 交替）
sudo python3 ssd_test_v1.8.2.py -d /dev/nvme0n1 -t osint \
  --osint-cycles 100 --osint-sleep-type both \
  --osint-sleep-duration 60 --osint-io-duration 120 -y

# 3. 长时间 EVT 压力测试（1000轮 S3）
sudo python3 ssd_test_v1.8.2.py -d /dev/nvme0n1 -t osint \
  --osint-cycles 1000 --osint-sleep-type s3 \
  --osint-sleep-duration 30 --osint-io-duration 60 -y

# 4. 空闲模式测试（无IO，50轮S4）
sudo python3 ssd_test_v1.8.2.py -d /dev/nvme0n1 -t osint \
  --osint-cycles 50 --osint-sleep-type s4 --osint-io-idle -y

# 5. 断点续测（中断后重新运行同一命令即可）
sudo python3 ssd_test_v1.8.2.py -d /dev/nvme0n1 -t osint -y

# 6. 重置测试（删除状态文件，从头开始）
sudo rm /var/lib/ssd_osint_state.json
```

---

*文档版本：v1.0 | 基于 ssd_test_v1.8.2.py | 对应 DVT/EVT Test Plan S3/S4 OS Interruption 测试项*
