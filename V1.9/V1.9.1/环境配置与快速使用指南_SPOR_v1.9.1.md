# ssd_test.py SPOR 三模式环境配置与快速使用指南（v1.9.1）

> 文档版本：v1.0 | 生成日期：2026-09-18 | 对应脚本：ssd_test_v1.9.1.py

---

## 1. 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|---------|---------|
| 操作系统 | Ubuntu 20.04 LTS | Ubuntu 22.04 LTS |
| Python | 3.8+ | 3.10.12 |
| 内核 | 5.4+ | 5.15+ |
| 权限 | root（sudo） | root |
| 待测设备 | NVMe SSD（/dev/nvmeXn1） | NVMe SSD（U.2 / M.2） |
| Timeboard（timeboard 模式） | Timeboard 板卡 + USB 串口 | 同左 |
| IPMI（enhanced 模式） | BMC 网络可达 | ipmitool 可正常通信 |

---

## 2. 依赖工具安装

在 Ubuntu 22.04 上执行以下命令安装全部依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
    nvme-cli \
    smartmontools \
    fio \
    util-linux \
    ipmitool \
    e2fsprogs \
    parted
```

### 2.1 各工具用途

| 工具 | 用途 | 哪些模式需要 |
|------|------|------------|
| nvme-cli | NVMe 设备识别、SMART 读取 | 全部模式 |
| smartmontools | SATA 设备 SMART 检查 | SATA 设备时 |
| fio | Pattern 写入/校验、混合读写负载 | 全部模式 |
| util-linux | lsblk、mount/umount 等 | 全部模式 |
| ipmitool | IPMI 远程意外断电上电 | enhanced 模式 |
| e2fsprogs | 文件系统工具 | 全部模式 |
| parted | 分区操作 | 全部模式 |

### 2.2 验证安装

```bash
nvme --version          # 期望 1.16+
fio --version           # 期望 3.25+
ipmitool -V             # 期望 1.8.18+
python3 --version       # 期望 3.10+
```

---

## 3. 脚本部署

```bash
sudo mkdir -p /opt/ssd_test
sudo cp ssd_test_v1.9.1.py /opt/ssd_test/ssd_test.py
sudo chmod +x /opt/ssd_test/ssd_test.py
sudo mkdir -p /var/lib/ssd_test

# 验证
cd /opt/ssd_test
sudo python3 ssd_test.py --version
# 期望: ssd_test.py v1.9.1
```

---

## 4. SPOR 三种断电方式快速上手

### 4.1 timeboard 模式（旧方案，Timeboard 硬件断电）

**适用场景**：配备 Timeboard 硬件板卡的测试环境，需要精确控制断电延时（毫秒级），完全自动化测试。

**前置条件**：
- Timeboard 板卡已连接，串口设备路径已知（默认 `/dev/ttyUSB0`）
- Timeboard 已正确接线控制测试机电源

**快速命令**：

```bash
# 基本用法：10 次循环，Timeboard 硬件意外断电
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode timeboard \
    --spor-cycles 10 \
    -y

# 自定义参数：20 次循环，写入延时 3 秒，硬件断电延时 200ms（越短越意外）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode timeboard \
    --spor-cycles 20 \
    --spor-delay 3 \
    --spor-poweroff-delay-ms 200 \
    --spor-timeboard-port /dev/ttyUSB1 \
    -y

# 混合读写模式（更接近真实负载）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode timeboard \
    --spor-mixed-rw \
    --spor-mixed-read-ratio 70 \
    -y
```

### 4.2 manual 模式（手动意外断电）

**适用场景**：无 Timeboard 硬件、无 IPMI，或需要人工精确控制断电时机的场景。

**前置条件**：
- 测试人员可物理接触电源开关或 PDU
- 脚本启动后，在写入进行中手动切断电源

**快速命令**：

```bash
# 基本用法：5 次循环，手动意外断电
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode manual \
    --spor-cycles 5 \
    -y
```

**操作流程**：
1. 启动脚本，脚本执行 pattern11 打底写入 → 验证 → 启动 fio 高压力写入（pattern22）
2. 脚本打印提示："**请立即手动切断电源**（意外断电，不 sync、不 shutdown）"
3. **人工操作**：在 fio 写入进行中直接关闭电源开关（不要等待写入完成）
4. 等待 10-30 秒，打开电源
5. 系统启动后，重新运行相同命令，脚本通过状态文件自动恢复执行阶段2（上电后检查）
6. 阶段2完成后自动进入下一循环的阶段1
7. 重复步骤 1-6 直到完成指定循环次数

> **注意**：manual 模式下每次循环都需要人工断电上电操作。建议将 `--spor-delay` 设置为 3-5 秒，给操作人员足够时间准备但又不会让 fio 写入完成。

### 4.3 enhanced 模式（增强方案，IPMI 意外断电 + 高 QD + 增强校验）

**适用场景**：配备 IPMI BMC 的测试机，需要自动化意外断电测试，且希望更严格地验证 PLP（更高写入压力）和数据完整性（fio 内置 verify）。无需 Timeboard 专用硬件。

**前置条件**：
- 测试机配备 IPMI BMC，地址、用户名、密码已知
- 测试机与 BMC 网络可达
- ipmitool 已安装
- **注意**：enhanced 模式直接 `chassis power off` 会同时切断系统盘电源，建议使用独立测试机

**快速命令**：

```bash
# 用法1：enhanced + IPMI 自动意外断电（推荐）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode enhanced \
    --ipmi-host 192.168.1.100 \
    --ipmi-user ADMIN \
    --ipmi-pass ADMIN \
    -y

# 用法2：自定义 enhanced 参数（高 QD 写入压力）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode enhanced \
    --ipmi-host 192.168.1.100 \
    --spor-cycles 20 \
    --spor-enhanced-iodepth 256 \
    --spor-enhanced-bs 128k \
    --spor-delay 5 \
    -y

# 用法3：小规模验证（首次使用建议先跑 2 次循环）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode enhanced \
    --ipmi-host 192.168.1.100 \
    --spor-cycles 2 \
    --spor-test-size 5 \
    -y
```

**enhanced 模式测试流程**：
1. **Setup（每轮阶段1）**：
   - 写入 pattern11（0x11）打底数据 → 验证
   - 启动 enhanced 高 QD 写入（pattern22=0x22, iodepth=256, bs=128k, 线程级进度追踪）
   - 等待指定秒数（写入进行中）
   - **IPMI 直接意外断电**（不 sync、不 shutdown，后台脚本自动断电→延时→上电）
2. **阶段2（上电后）**：
   - 等待 SSD 初始化 + 掉盘检测
   - PCIe 链路检查
   - SMART 健康检查
   - 解析掉电时写入位置（线程进度，fallback 到 iolog）
   - 验证前段 pattern22（已写入区域，跳过边界 N 个 LBA，fio 内置 verify=pattern）
   - 验证后段 pattern11（未覆盖区域，fio 内置 verify=pattern）
   - 记录结果
3. 全部循环完成后：最终完整功能测试（容量 + SMART + 基本性能）

---

## 5. GUI 上位机使用

### 5.1 启动 GUI

```bash
sudo python3 ssd_test.py --gui
```

### 5.2 SPOR 标签页操作

1. 点击"意外电源循环(SPOR)"标签页
2. 设置"循环次数"（默认 10）
3. 在"断电方式"下拉框中选择：
   - `timeboard` — Timeboard 硬件意外断电
   - `manual` — 手动意外断电
   - `enhanced` — IPMI 意外断电 + 高 QD + 增强校验（选择后下方专属控件自动启用）
4. 设置"写入延时(秒)"（默认 5）—— fio 写入后等待多久触发断电
5. 设置"测试大小(GB)"（默认 20）
6. 设置"硬件断电延时(ms)"（默认 500，仅 timeboard 模式有效）
7. 可选：勾选"混合读写模式"
8. "Timeboard 串口"（仅 timeboard 模式可编辑，manual/enhanced 模式下灰显）
9. **enhanced 模式专属**（仅选择 enhanced 时可用）：
   - "写入队列深度"（默认 256，范围 1-1024）
   - "写入块大小"（默认 "128k"）
10. 可选：勾选"循环结束后执行最终完整功能测试"
11. 返回"基本配置"标签页，选择测试设备和测试项
12. 点击"生成命令"查看生成的命令行
13. 点击"开始测试"执行

> **注意**：enhanced 模式还需要在"正常电源循环"标签页或基本配置中填写 IPMI 主机地址（`--ipmi-host`），因为 enhanced 模式的 IPMI 断电复用全局 IPMI 配置。

---

## 6. enhanced 模式参数详解

| 参数 | 命令行 | GUI 控件 | 默认值 | 说明 |
|------|--------|---------|-------|------|
| 断电方式 | `--spor-power-mode enhanced` | 断电方式下拉框 | timeboard | 选择 enhanced 启用增强模式 |
| IPMI 主机 | `--ipmi-host <addr>` | （正常电源循环标签页） | 无 | enhanced 模式必填，用于 IPMI 意外断电 |
| 写入队列深度 | `--spor-enhanced-iodepth 256` | 写入队列深度 Spinbox | 256 | fio 写入队列深度，越大掉电瞬间 outstanding IO 越多，PLP 验证越严格。建议 128-512 |
| 写入块大小 | `--spor-enhanced-bs 128k` | 写入块大小 Entry | 128k | fio 写入块大小，支持 k/m/g 后缀 |
| 写入延时 | `--spor-delay 5` | 写入延时(秒) Spinbox | 5 | fio 启动后等待多少秒触发断电，确保写入已进入稳定状态 |
| 测试大小 | `--spor-test-size 20` | 测试大小(GB) Spinbox | 20 | Pattern 写入和校验的数据量 |
| 掉电边界跳过 | `--spor-skip-lba 8` | （无 GUI 控件） | 8 | 掉电边界最后 N 个 LBA 不校验（可能未完全写入） |

### 6.1 队列深度选择建议

| iodepth | 适用场景 |
|---------|---------|
| 32 | 与旧方案 timeboard 模式一致，轻度压力 |
| 128 | 中等压力，企业级 SSD 常规验证 |
| 256 | 默认值，较高压力，严格 PLP 验证 |
| 512 | 高压力，极限 PLP 验证（可能导致低端 SSD 超时） |

---

## 7. 状态文件与断点恢复

### 7.1 状态文件位置

默认路径：`/var/lib/ssd_spor_state.json`

可通过 `--spor-state-file` 自定义：

```bash
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode enhanced \
    --spor-state-file /var/lib/ssd_test/spor_enhanced_state.json \
    -y
```

### 7.2 断点恢复

SPOR 测试使用两阶段状态机：
- **阶段1（PHASE_POWEROFF = 1）**：掉电前流程，执行完后系统被断电，进程终止
- **阶段2（PHASE_POWERON = 2）**：上电后流程，系统重启后重新运行脚本自动恢复

系统重启后，重新运行相同命令即可自动从断点恢复：

```bash
# 重启后恢复执行（使用与之前相同的参数）
sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor \
    --spor-power-mode enhanced \
    --ipmi-host 192.168.1.100 \
    -y
```

脚本检测到状态文件中 `phase=2`，自动执行上电后检查，然后进入下一循环。

### 7.3 重置测试

```bash
sudo rm -f /var/lib/ssd_spor_state.json
```

---

## 8. 常见问题排查

### 8.1 Timeboard 连接失败

**现象**：报错 "Timeboard 连接失败"

**排查**：
```bash
# 检查串口设备
ls -la /dev/ttyUSB*
# 检查串口权限
sudo chmod 666 /dev/ttyUSB0
# 检查 Timeboard 接线
```

**解决**：确认串口路径正确（`--spor-timeboard-port`），串口权限足够，Timeboard 已正确接线。

### 8.2 enhanced 模式缺少 ipmi-host

**现象**：报错 "enhanced 模式需要指定 --ipmi-host"

**解决**：enhanced 模式通过 IPMI 实现自动化意外断电，必须指定 `--ipmi-host`。无 IPMI 时请使用 `--spor-power-mode manual`。

### 8.3 IPMI 断电失败

**现象**：enhanced 模式下系统未断电，或 ipmitool 报错

**排查**：
```bash
# 测试 IPMI 连通性
ipmitool -H 192.168.1.100 -U ADMIN -P ADMIN -I lanplus chassis status
# 检查网络
ping 192.168.1.100
```

**解决**：确认 BMC 地址、用户名、密码正确；确认网络可达；确认 BMC 已启用 lanplus 接口。

### 8.4 Pattern 校验失败

**现象**：上电后检查时 "pattern22 enhanced 验证失败" 或 "pattern11 enhanced 验证失败"，mismatch > 0

**可能原因**：
1. SSD PLP 不足，掉电时未完成 FTL 元数据备份，导致数据丢失或损坏
2. 掉电边界 LBA 数据未完全写入（已通过 skip_lba 容错，若仍失败说明问题更严重）
3. SSD 存在数据保持力问题

**排查**：
```bash
# 检查 SMART 介质错误
sudo nvme smart-log /dev/nvme0
# 手动执行 Pattern 写入和校验
sudo fio --name=test --filename=/dev/nvme0n1 --rw=write --bs=128k --size=1G --buffer_pattern=0x22
sudo fio --name=test --filename=/dev/nvme0n1 --rw=read --bs=128k --size=1G --verify=pattern --verify_pattern=0x22 --do_verify=1
```

### 8.5 manual 模式下 fio 写入完成后才断电

**现象**：手动断电时 fio 已完成写入，失去"意外"效果

**解决**：
- 减小 `--spor-delay`（如 2-3 秒），在 fio 写入进行中尽早断电
- 增大 `--spor-test-size`（如 50GB+），确保 fio 写入不会在短时间内完成
- 操作人员提前准备，看到提示后立即断电

### 8.6 系统盘文件系统损坏（enhanced 模式）

**现象**：enhanced 模式多次循环后，系统盘出现文件系统错误或无法启动

**原因**：enhanced 模式直接 `chassis power off` 不经过 OS 关机，系统盘可能未完成写入

**缓解措施**：
- 使用独立测试机（系统盘可随时重装）
- 将系统盘挂载为只读或使用临时文件系统
- 状态文件使用原子写入（已实现），降低状态文件损坏风险
- 定期检查系统盘文件系统：`sudo fsck /dev/sda1`

### 8.7 GUI 无法启动

**现象**：报错 "no display name and no $DISPLAY environment variable"

**解决**：
- 本地运行：确保在图形界面终端中执行
- SSH 远程：使用 `ssh -X user@host` 启用 X11 转发
- 无图形环境：使用命令行模式（不加 `--gui`）

---

## 9. 快速命令速查表

| 场景 | 命令 |
|------|------|
| 查看版本 | `sudo python3 ssd_test.py --version` |
| 查看帮助 | `sudo python3 ssd_test.py --help` |
| 启动 GUI | `sudo python3 ssd_test.py --gui` |
| timeboard 模式 10 次 | `sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode timeboard --spor-cycles 10 -y` |
| manual 模式 5 次 | `sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode manual --spor-cycles 5 -y` |
| enhanced + IPMI 模式 | `sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode enhanced --ipmi-host 192.168.1.100 -y` |
| enhanced 自定义参数 | `sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode enhanced --ipmi-host 192.168.1.100 --spor-cycles 20 --spor-enhanced-iodepth 256 --spor-enhanced-bs 128k -y` |
| enhanced 小规模验证 | `sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode enhanced --ipmi-host 192.168.1.100 --spor-cycles 2 --spor-test-size 5 -y` |
| 混合读写模式 | 在上述命令中添加 `--spor-mixed-rw --spor-mixed-read-ratio 70` |
| 自定义状态文件 | 在上述命令中添加 `--spor-state-file /path/to/state.json` |
| 详细日志 | 在上述命令中添加 `-v` |
| 重置测试 | `sudo rm -f /var/lib/ssd_spor_state.json` |
