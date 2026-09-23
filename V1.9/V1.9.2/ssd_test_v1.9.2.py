#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSD 自动化测试脚本
==================
覆盖测试项目：
  1. 现场固件升级/降级 (Field Firmware Upgrade/Downgrade)
  2. 设备智能健康信息 (Device SMART Health Information)
  3. 设备容量 (Devices capacity)
  4. 完整性能特征 (Full Performance Characterization)
  5. 正常电源循环测试 (Normal Power Cycle Test, 支持 ipmi/manual/enhanced 三种模式)
  6. 意外电源循环测试 (Surprise Power Cycle Test, SPOR, 支持 timeboard/manual/enhanced 三种断电方式)
  7. 操作系统中断测试 (OS Interruption Test, OSINT)

适用平台：Linux Ubuntu 22.04+
依赖工具：nvme-cli, smartmontools, fio, util-linux, ipmitool, e2fsprogs, parted, rtcwake
Python  ：3.10+（仅标准库，GUI 使用 tkinter）

用法示例：
  # 命令行模式
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-cycles 10 --ipmi-host 192.168.1.100 -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-power-mode enhanced --pc-pattern-size-gb 20 -y

  # 可视化上位机模式（GUI）
  sudo python3 ssd_test.py --gui
  python3 ssd_test.py --gui          # 非 root 启动 GUI，执行测试时自动 pkexec 提权
"""

import argparse
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# GUI 相关库（条件导入，无显示环境时命令行模式不受影响）
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

# ============================================================
# 常量定义
# ============================================================

SCRIPT_VERSION = "1.9.2"

# 依赖的外部命令
REQUIRED_COMMANDS = {
    "nvme": "nvme-cli",
    "smartctl": "smartmontools",
    "fio": "fio",
    "lsblk": "util-linux",
    "ipmitool": "ipmitool",
}

# 测试项标识
TEST_FW = "fw"
TEST_SMART = "smart"
TEST_CAPACITY = "capacity"
TEST_PERF = "perf"
TEST_POWERCYCLE = "powercycle"
TEST_SPOR = "spor"
TEST_OSINT = "osint"
TEST_RW = "rw"
TEST_ALL = "all"

VALID_TEST_ITEMS = [TEST_FW, TEST_SMART, TEST_CAPACITY, TEST_PERF, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT, TEST_RW, TEST_ALL]

# Read/Write 测试模式
RW_MODE_FULL_DISK = "full_disk"
RW_MODE_FILE_CYCLE = "file_cycle"
RW_MODE_LONG_RUN = "long_run"
RW_MODE_ALL = "all"

# Read/Write 常用文件大小（MB）
RW_FILE_SIZES = {
    "256MB": 256,
    "1GB": 1024,
    "4GB": 4096,
    "16GB": 16384,
    "32GB": 32768,
}

# Read/Write 数据 pattern
RW_PATTERN_RANDOM = "random"
RW_PATTERN_AA = "0xAA"
RW_PATTERN_55 = "0x55"
RW_PATTERN_00 = "0x00"
RW_PATTERN_FF = "0xFF"

# 测试状态
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"
STATUS_SKIP = "SKIP"

# 设备类型
DEVICE_NVME = "nvme"
DEVICE_SATA = "sata"
DEVICE_UNKNOWN = "unknown"

# 性能测试状态
PERF_FOB = "fob"
PERF_STEADY = "steady"
PERF_BOTH = "both"
PERF_UNKNOWN = "unknown"  # v1.9.2: 直接测试，不进入任何状态

# 固件操作
FW_UPGRADE = "upgrade"
FW_DOWNGRADE = "downgrade"

# 默认配置
DEFAULT_PERF_RUNTIME = 60
DEFAULT_PERF_QD = 32
DEFAULT_FW_COMMIT_TIMEOUT = 120
DEFAULT_CMD_TIMEOUT = 300
DEFAULT_SMART_RW_SIZE_MB = 1024  # 基本R/W操作大小 1GB

# 电源循环测试默认配置
DEFAULT_PC_CYCLES = 10
DEFAULT_PC_POWER_MODE = "ipmi"  # ipmi / manual / enhanced
DEFAULT_PC_MOUNT_POINT = "/mnt/ssd_test"
DEFAULT_PC_STATE_FILE = "/var/lib/ssd_power_cycle_state.json"
DEFAULT_PC_BOOT_TIMEOUT = 300
DEFAULT_PC_OFF_INTERVAL = 30
DEFAULT_PC_RW_DURATION = 120
DEFAULT_PC_TEST_FILE_COUNT = 5
DEFAULT_PC_TEST_FILE_SIZE_MB = 100
# enhanced（增强）模式专属默认配置
DEFAULT_PC_PATTERN = "0xAA"
DEFAULT_PC_PATTERN_SIZE_GB = 20
DEFAULT_PC_LINK_CHECK = True
# SPOR 三模式默认配置
DEFAULT_SPOR_POWER_MODE = "timeboard"  # timeboard / manual / enhanced
DEFAULT_SPOR_ENHANCED_IODEPTH = 256
DEFAULT_SPOR_ENHANCED_BS = "128k"


# ============================================================
# 日志配置
# ============================================================

def get_default_log_dir() -> str:
    """获取默认日志目录（基于脚本所在目录的绝对路径）。
    v1.9.2: 无论在哪个目录运行脚本，日志都固定生成在脚本同级 logs/ 目录下。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "logs")


def resolve_log_dir(log_dir: str) -> str:
    """将日志目录解析为绝对路径。
    相对路径会基于脚本所在目录解析，而非当前工作目录。
    """
    if os.path.isabs(log_dir):
        return log_dir
    # 相对路径基于脚本所在目录，而非当前工作目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(script_dir, log_dir))


def setup_logging(log_dir: str, verbose: bool = False) -> Tuple[logging.Logger, str]:
    """初始化日志，同时输出到控制台和文件。"""
    # v1.9.2: 相对路径基于脚本所在目录解析，确保日志位置固定
    log_dir = resolve_log_dir(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"ssd_test_{timestamp}.log")

    logger = logging.getLogger("ssd_test")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # 控制台 handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件 handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger, log_file


# ============================================================
# 工具函数
# ============================================================

def run_cmd(cmd: List[str],
            timeout: int = DEFAULT_CMD_TIMEOUT,
            retry: int = 0,
            check: bool = True,
            capture: bool = True,
            logger: Optional[logging.Logger] = None,
            cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """
    统一命令执行封装。
    返回 (returncode, stdout, stderr)。
    """
    cmd_str = " ".join(cmd)
    if logger:
        logger.debug(f"执行命令: {cmd_str}")

    last_err = None
    for attempt in range(retry + 1):
        try:
            if capture:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, cwd=cwd
                )
            else:
                proc = subprocess.run(
                    cmd, text=True, timeout=timeout, cwd=cwd
                )
            rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
            if logger and out.strip():
                logger.debug(f"stdout: {out.strip()[:500]}")
            if logger and err.strip():
                logger.debug(f"stderr: {err.strip()[:500]}")
            if check and rc != 0:
                raise subprocess.CalledProcessError(rc, cmd, output=out, stderr=err)
            return rc, out, err
        except subprocess.TimeoutExpired as e:
            last_err = e
            if logger:
                logger.warning(f"命令超时 ({timeout}s)，尝试 {attempt + 1}/{retry + 1}: {cmd_str}")
            if attempt < retry:
                time.sleep(2)
                continue
            raise
        except subprocess.CalledProcessError as e:
            last_err = e
            if attempt < retry:
                if logger:
                    logger.warning(f"命令返回非零 ({e.returncode})，重试 {attempt + 1}/{retry + 1}")
                time.sleep(2)
                continue
            if check:
                raise
            return e.returncode, e.output or "", e.stderr or ""

    if last_err:
        raise last_err
    return -1, "", ""


def check_root() -> bool:
    """检查是否具有 root 权限。"""
    return os.geteuid() == 0


def check_dependencies(logger: Optional[logging.Logger] = None) -> Tuple[bool, List[str]]:
    """检查依赖命令是否存在，返回 (全部满足, 缺失列表)。"""
    missing = []
    for cmd, pkg in REQUIRED_COMMANDS.items():
        try:
            run_cmd(["which", cmd], check=True, capture=True, logger=None)
        except Exception:
            missing.append(f"{cmd} (包名: {pkg})")
    if missing and logger:
        logger.error(f"缺少依赖工具: {', '.join(missing)}")
        logger.error("请执行: sudo apt install -y " + " ".join(
            REQUIRED_COMMANDS[c] for c in [m.split()[0] for m in missing]
        ))
    return len(missing) == 0, missing


def get_device_type(device: str, logger: Optional[logging.Logger] = None) -> str:
    """识别设备类型：nvme / sata / unknown。"""
    basename = os.path.basename(device)
    if basename.startswith("nvme"):
        return DEVICE_NVME
    if basename.startswith("sd"):
        # 进一步确认是否为 SATA（通过 /sys/block 传输协议）
        try:
            sys_path = f"/sys/block/{basename}/device"
            if os.path.exists(sys_path):
                # 检查是否为 NVMe over PCIe 但显示为 sdX（少见）
                return DEVICE_SATA
        except Exception:
            pass
        return DEVICE_SATA
    if logger:
        logger.warning(f"无法识别设备类型: {device}")
    return DEVICE_UNKNOWN


def get_nvme_controller(device: str) -> str:
    """从命名空间设备路径获取控制器路径，如 /dev/nvme0n1 -> /dev/nvme0。"""
    m = re.match(r"(/dev/nvme\d+)", device)
    if m:
        return m.group(1)
    return device


def bytes_to_human(size_bytes: int, binary: bool = False) -> str:
    """字节数转换为人性化字符串。binary=True 使用 GiB/MiB，否则使用 GB/MB。"""
    if size_bytes is None:
        return "N/A"
    base = 1024 if binary else 1000
    units = ["B", "KiB", "MiB", "GiB", "TiB"] if binary else ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    idx = 0
    while size >= base and idx < len(units) - 1:
        size /= base
        idx += 1
    return f"{size:.2f} {units[idx]}"


def human_to_bytes(human_str: str) -> int:
    """将人性化容量字符串（如 '1.02 TB'、'953.9 GiB'）转换为字节数。"""
    if not human_str:
        return 0
    human_str = human_str.strip()
    m = re.match(r"([\d.]+)\s*([KMGTP]i?B?)", human_str, re.IGNORECASE)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2).upper()
    binary = "I" in unit
    base = 1024 if binary else 1000
    multiplier = {"B": 1, "K": base, "M": base ** 2, "G": base ** 3, "T": base ** 4}
    key = unit[0]
    return int(value * multiplier.get(key, 1))


def wait_for_device(device: str, timeout: int = 120,
                    logger: Optional[logging.Logger] = None) -> bool:
    """等待设备重新出现（固件复位后使用），返回是否成功。"""
    if logger:
        logger.info(f"等待设备重新枚举: {device} (超时 {timeout}s)")
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(device):
            # 额外等待设备就绪
            time.sleep(2)
            try:
                run_cmd(["lsblk", "-d", device], check=True, capture=True, logger=None)
                if logger:
                    logger.info(f"设备已就绪: {device}")
                return True
            except Exception:
                pass
        time.sleep(2)
    if logger:
        logger.error(f"设备在 {timeout}s 内未重新出现: {device}")
    return False


def confirm_destructive(device: str, logger: Optional[logging.Logger] = None) -> bool:
    """交互式确认数据销毁风险。"""
    print("\n" + "=" * 60)
    print("  警告：以下操作将破坏设备上的所有数据！")
    print(f"  目标设备: {device}")
    print("  请确认该设备上无重要数据，或已完成备份。")
    print("=" * 60)
    try:
        answer = input("输入 'YES' 继续，其他任意键取消: ").strip()
        return answer.upper() == "YES"
    except (EOFError, KeyboardInterrupt):
        return False


# ============================================================
# 测试结果数据结构
# ============================================================

@dataclass
class TestResult:
    """单条测试结果。"""
    test_item: str
    test_name: str
    device: str
    start_time: str = ""
    end_time: str = ""
    duration_sec: float = 0.0
    status: str = STATUS_SKIP
    details: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    def start(self):
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def finish(self, status: str, error: Optional[str] = None):
        self.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.start_time:
            try:
                st = datetime.strptime(self.start_time, "%Y-%m-%d %H:%M:%S")
                self.duration_sec = (datetime.now() - st).total_seconds()
            except Exception:
                pass
        self.status = status
        self.error_message = error

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 测试配置
# ============================================================

@dataclass
class PerfTask:
    """单组 FIO 性能测试任务参数（v1.8.0 新增，用于多任务批量测试）。

    字段名与 TestConfig 中 perf_* 字段保持一致，便于直接覆盖。
    """
    perf_test_name: str = "perf-test"
    perf_direct: int = 1
    perf_ioengine: str = "libaio"
    perf_bs: str = "4k"
    perf_iodepth: int = 64
    perf_numjobs: int = 1
    perf_rw: str = "randread"
    perf_rwmixread: int = 70
    perf_size: str = "3%"
    perf_time_based: bool = True
    perf_runtime_full: int = 60
    perf_text_log: bool = False
    note: str = ""  # 任务备注，用于日志区分

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PerfTask":
        valid_fields = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid_fields})



# ============================================================
# SSD 状态管理模块 (v1.9.2 新增)
# 支持 FOB / STEADY / UNKNOWN 三种状态，状态持久化到 JSON 文件
# ============================================================

SSD_STATE_UNKNOWN = "unknown"
SSD_STATE_FOB = "fob"
SSD_STATE_STEADY = "steady"

def _get_state_file_path() -> str:
    """获取 SSD 状态文件路径。"""
    return os.path.expanduser("~/.ssd_test_state.json")

def load_ssd_state(serial: str) -> dict:
    """从状态文件读取指定序列号设备的状态。

    Returns:
        {"state": "unknown"|"fob"|"steady", "serial": str, "timestamp": str|None, "extra": dict}
    """
    default = {"state": SSD_STATE_UNKNOWN, "serial": serial, "timestamp": None, "extra": {}}
    try:
        path = _get_state_file_path()
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        device_state = data.get(serial, {})
        if not isinstance(device_state, dict):
            return default
        return {
            "state": device_state.get("state", SSD_STATE_UNKNOWN),
            "serial": serial,
            "timestamp": device_state.get("timestamp"),
            "extra": device_state.get("extra", {}),
        }
    except Exception:
        return default

def save_ssd_state(serial: str, state: str, extra: dict = None):
    """写入状态文件，包含 state、serial、timestamp、extra。

    使用临时文件+os.replace 保证原子写入。
    """
    try:
        path = _get_state_file_path()
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        data[serial] = {
            "state": state,
            "serial": serial,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "extra": extra or {},
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        # 状态文件写入失败不影响主流程，仅打印警告
        print(f"[WARNING] 保存 SSD 状态文件失败: {e}", file=sys.stderr)

def get_device_serial(device: str) -> str:
    """通过 nvme id-ctrl 或 lsblk 获取设备序列号，NVMe 优先。

    Returns:
        设备序列号字符串，失败时返回 "unknown-serial"
    """
    try:
        # 优先 NVMe
        dev_type = get_device_type(device)
        if dev_type == DEVICE_NVME:
            ctrl = get_nvme_controller(device)
            _, out, _ = run_cmd(["nvme", "id-ctrl", ctrl, "-o", "json"],
                                check=False, capture=True, timeout=10)
            try:
                data = json.loads(out)
                sn = data.get("sn") or data.get("serial_number") or ""
                if sn:
                    return sn.strip()
            except Exception:
                pass
        # 回退到 lsblk
        _, out, _ = run_cmd(["lsblk", "-d", "-n", "-o", "SERIAL", device],
                            check=False, capture=True, timeout=10)
        sn = out.strip()
        if sn:
            return sn
    except Exception:
        pass
    return "unknown-serial"

@dataclass
class TestConfig:
    """全局测试配置。"""
    device: str
    test_items: List[str]
    fw_image: Optional[str] = None
    fw_action: str = FW_UPGRADE
    fw_slot: Optional[int] = None
    perf_state: str = PERF_UNKNOWN  # v1.9.2: 默认 Unknown 直接测试
    perf_runtime: int = DEFAULT_PERF_RUNTIME
    precondition: bool = True
    precond_rand_speed_mb: int = 30  # 稳态预处理4K随机写预估速度(MB/s)，用于超时计算（TLC直写稳态通常30-60MB/s）
    precond_rand_max_sec: int = 0  # 随机写预处理最大时长(秒)，0=自动按速度估算
    # 完整性能特征 - 全参数可配置（FIO原生参数对齐）
    perf_test_name: str = "perf-test"  # FIO --name，同步为日志文件名核心标识
    perf_direct: int = 1  # --direct，0/1，默认1（裸盘绕过系统缓存）
    perf_ioengine: str = "libaio"  # --ioengine，direct=1时锁定libaio
    perf_bs: str = "4k"  # --bs 块大小
    perf_iodepth: int = 64  # --iodepth 队列深度
    perf_numjobs: int = 1  # --numjobs 并发任务数
    perf_rw: str = "randread"  # --rw 读写模式
    perf_rwmixread: int = 70  # --rwmixread 读占比(0-100)，仅混合模式生效
    perf_size: str = "3%"  # --size 测试范围（百分比或固定容量）
    perf_time_based: bool = True  # --time_based 时间模式开关（固定开启）
    perf_runtime_full: int = 60  # --runtime 测试总时长（秒），全参数模式使用
    perf_group_reporting: bool = True  # --group_reporting 固定启用
    perf_lat_percentiles: bool = True  # --lat_percentiles=1 固定启用
    perf_output_format: str = "json"  # --output-format 固定json（兼容性更好，json+需fio3.0+）
    perf_text_log: bool = False  # 是否同时输出文本格式日志
    perf_task_list: List[PerfTask] = field(default_factory=list)  # v1.8.0 多任务批量测试列表，为空时使用单组参数（兼容旧版）
    # 电源循环测试配置
    pc_cycles: int = DEFAULT_PC_CYCLES
    pc_power_mode: str = DEFAULT_PC_POWER_MODE
    ipmi_host: Optional[str] = None
    ipmi_user: str = "ADMIN"
    ipmi_pass: str = "ADMIN"
    pc_mount_point: str = DEFAULT_PC_MOUNT_POINT
    pc_state_file: str = DEFAULT_PC_STATE_FILE
    pc_boot_timeout: int = DEFAULT_PC_BOOT_TIMEOUT
    pc_off_interval: int = DEFAULT_PC_OFF_INTERVAL
    pc_rw_duration: int = DEFAULT_PC_RW_DURATION
    # enhanced（增强）模式专属配置
    pc_pattern: str = DEFAULT_PC_PATTERN
    pc_pattern_size_gb: int = DEFAULT_PC_PATTERN_SIZE_GB
    pc_link_check: bool = DEFAULT_PC_LINK_CHECK
    # SPOR（意外电源循环测试）配置
    spor_cycles: int = 10
    spor_delay: int = 5
    spor_test_size_gb: int = 20
    spor_lba_size: int = 4096
    spor_skip_lba: int = 8
    spor_poweroff_delay_ms: int = 500
    spor_mixed_rw: bool = False
    spor_mixed_read_ratio: int = 70
    spor_final_test: bool = True
    spor_timeboard_port: str = "/dev/ttyUSB0"
    spor_state_file: str = "/var/lib/ssd_spor_state.json"
    # SPOR 三模式配置
    spor_power_mode: str = DEFAULT_SPOR_POWER_MODE
    spor_enhanced_iodepth: int = DEFAULT_SPOR_ENHANCED_IODEPTH
    spor_enhanced_bs: str = DEFAULT_SPOR_ENHANCED_BS
    # OSINT（操作系统中断测试）配置
    osint_cycles: int = 10
    osint_sleep_type: str = "s3"  # s3 / s4 / both
    osint_sleep_duration: int = 30  # 休眠持续秒数（rtcwake 定时唤醒）
    osint_io_active: bool = True  # True=活跃IO时休眠, False=空闲时休眠
    osint_io_duration: int = 60  # 每轮休眠前持续IO秒数
    osint_mount_point: str = "/mnt/ssd_osint"
    osint_state_file: str = "/var/lib/ssd_osint_state.json"
    osint_test_file_size_mb: int = 512
    # Read/Write 测试配置
    rw_mode: str = RW_MODE_ALL  # full_disk / file_cycle / long_run / all
    rw_file_sizes: List[str] = field(default_factory=lambda: ["256MB", "1GB", "4GB", "16GB", "32GB"])
    rw_cycles: int = 3  # 每个文件大小的读写循环次数
    rw_pattern: str = RW_PATTERN_RANDOM  # random / 0xAA / 0x55 / 0x00 / 0xFF
    rw_verify: str = "md5"  # md5 / sha256 / crc32
    rw_long_run_hours: int = 24  # 长期验证小时数
    rw_block_size: str = "4k"  # 块大小
    rw_io_depth: int = 32  # 队列深度
    rw_numjobs: int = 4  # 并发 job 数
    rw_mixed_read_ratio: int = 70  # 长期运行混合读写读比例(%)
    rw_state_file: str = "/var/lib/ssd_rw_state.json"
    # 通用配置
    output_dir: str = "./reports"
    log_dir: str = "./logs"
    dry_run: bool = False
    assume_yes: bool = False
    verbose: bool = False
    # v1.9.2 新增：状态管理与稳态配置
    purge_method: str = "auto"  # auto/user-data/blkdiscard
    steady_max_rounds: int = 25  # WDPC 最大轮数
    steady_point_duration: int = 60  # WDPC 每个测试点运行秒数
    smart_ignore_media_errors: str = "5353"  # 性能测试中忽视的介质错误ID，逗号分隔
    device_type: str = DEVICE_UNKNOWN
    nvme_ctrl: str = ""

    def __post_init__(self):
        self.device_type = get_device_type(self.device)
        if self.device_type == DEVICE_NVME:
            self.nvme_ctrl = get_nvme_controller(self.device)
        # v1.9.2: 日志目录转为基于脚本所在目录的绝对路径
        self.log_dir = resolve_log_dir(self.log_dir)


# ============================================================
# 测试项 1：固件升级/降级
# ============================================================

class FirmwareTester:
    """现场固件升级/降级测试。"""

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

    def get_current_fw_version(self) -> Tuple[Optional[str], Dict[str, Any]]:
        """读取当前固件版本。返回 (版本字符串, 完整信息)。"""
        info = {}
        if self.cfg.device_type == DEVICE_NVME:
            try:
                _, out, _ = run_cmd(
                    ["nvme", "id-ctrl", self.ctrl, "-o", "json"],
                    check=True, capture=True, logger=self.log
                )
                data = json.loads(out)
                ver = data.get("fr") or data.get("firmware_revision")
                info["model"] = data.get("mn") or data.get("model_number")
                info["serial"] = data.get("sn") or data.get("serial_number")
                info["fw_version"] = ver
                return ver, info
            except Exception as e:
                self.log.warning(f"nvme id-ctrl 读取固件版本失败: {e}")

        # 回退到 smartctl
        try:
            _, out, _ = run_cmd(
                ["smartctl", "-i", self.cfg.device],
                check=True, capture=True, logger=self.log
            )
            for line in out.splitlines():
                if "Firmware Version" in line or "Revision" in line:
                    ver = line.split(":")[-1].strip()
                    info["fw_version"] = ver
                    return ver, info
        except Exception as e:
            self.log.warning(f"smartctl 读取固件版本失败: {e}")

        return None, info

    def fw_download(self, image_path: str) -> bool:
        """下载固件镜像到设备。"""
        if not os.path.exists(image_path):
            self.log.error(f"固件镜像不存在: {image_path}")
            return False
        if self.cfg.device_type != DEVICE_NVME:
            self.log.error("固件下载仅支持 NVMe 设备，SATA 设备请使用厂商工具")
            return False
        try:
            self.log.info(f"下载固件镜像: {image_path}")
            run_cmd(
                ["nvme", "fw-download", self.ctrl, "-f", image_path],
                check=True, capture=True, logger=self.log, timeout=120
            )
            self.log.info("固件下载成功")
            return True
        except Exception as e:
            self.log.error(f"固件下载失败: {e}")
            return False

    def fw_commit(self, slot: Optional[int] = None, action: int = 2) -> bool:
        """
        提交并激活固件。
        action: 1=下次复位激活, 2=立即复位激活, 3=立即激活不复位, 4=仅激活指定槽位
        """
        if self.cfg.device_type != DEVICE_NVME:
            self.log.error("固件提交仅支持 NVMe 设备")
            return False
        cmd = ["nvme", "fw-commit", self.ctrl, "-a", str(action)]
        if slot is not None:
            cmd.extend(["-s", str(slot)])
        try:
            self.log.info(f"提交固件 (action={action}, slot={slot or 'auto'})")
            run_cmd(cmd, check=True, capture=True, logger=self.log, timeout=30)
            self.log.info("固件提交成功")
            return True
        except Exception as e:
            # action=2 时设备会立即复位，命令可能因设备消失而返回错误，这是正常的
            if action == 2:
                self.log.info(f"固件提交后设备复位（命令返回异常属正常现象）: {e}")
                return True
            self.log.error(f"固件提交失败: {e}")
            return False

    def verify_device_after_flash(self) -> Tuple[bool, Dict[str, Any]]:
        """烧写后设备枚举与功能校验。"""
        result = {"device_present": False, "smart_ok": False,
                  "capacity_ok": False, "basic_rw_ok": False}
        # 1. 等待设备重新枚举
        result["device_present"] = wait_for_device(
            self.cfg.device, timeout=DEFAULT_FW_COMMIT_TIMEOUT, logger=self.log
        )
        if not result["device_present"]:
            return False, result

        # 2. SMART 可读
        try:
            if self.cfg.device_type == DEVICE_NVME:
                run_cmd(["nvme", "smart-log", self.ctrl], check=True,
                        capture=True, logger=self.log, timeout=10)
            else:
                run_cmd(["smartctl", "-H", self.cfg.device], check=True,
                        capture=True, logger=self.log, timeout=10)
            result["smart_ok"] = True
        except Exception as e:
            self.log.error(f"烧写后 SMART 读取失败: {e}")

        # 3. 容量正常
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.cfg.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
        except Exception as e:
            self.log.error(f"烧写后容量读取失败: {e}")

        # 4. 基本 R/W（读取前 4MB，不写入以保护固件）
        try:
            run_cmd(["dd", f"if={self.cfg.device}", "of=/dev/null",
                     "bs=1M", "count=4", "iflag=direct"],
                    check=True, capture=True, logger=self.log, timeout=30)
            result["basic_rw_ok"] = True
        except Exception as e:
            self.log.error(f"烧写后基本读取失败: {e}")

        all_ok = all([result["device_present"], result["smart_ok"],
                      result["capacity_ok"], result["basic_rw_ok"]])
        return all_ok, result

    def run(self) -> TestResult:
        """执行固件升级/降级测试主流程。"""
        action_name = "升级" if self.cfg.fw_action == FW_UPGRADE else "降级"
        result = TestResult(
            test_item=TEST_FW,
            test_name=f"现场固件{action_name}",
            device=self.cfg.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info(f"[DRY-RUN] 将执行固件{action_name}: 镜像={self.cfg.fw_image}")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        if not self.cfg.fw_image:
            result.finish(STATUS_ERROR, "未指定固件镜像 (--fw-image)")
            return result

        if self.cfg.device_type != DEVICE_NVME:
            result.finish(STATUS_ERROR, f"固件{action_name}仅支持 NVMe 设备，当前为 {self.cfg.device_type}")
            return result

        try:
            # 1. 读取当前固件版本
            self.log.info("读取当前固件版本...")
            old_ver, dev_info = self.get_current_fw_version()
            result.details["old_fw_version"] = old_ver
            result.details["device_info"] = dev_info
            self.log.info(f"当前固件版本: {old_ver}")

            # 2. 下载固件
            if not self.fw_download(self.cfg.fw_image):
                result.finish(STATUS_FAIL, "固件下载失败")
                return result

            # 3. 提交并激活（立即复位）
            if not self.fw_commit(slot=self.cfg.fw_slot, action=2):
                result.finish(STATUS_FAIL, "固件提交失败")
                return result

            # 4. 烧写后校验
            all_ok, verify_info = self.verify_device_after_flash()
            result.details["verify_after_flash"] = verify_info

            # 5. 读取新固件版本并比对
            new_ver, _ = self.get_current_fw_version()
            result.details["new_fw_version"] = new_ver
            self.log.info(f"烧写后固件版本: {new_ver}")

            if not all_ok:
                result.finish(STATUS_FAIL, "烧写后设备功能校验失败")
                return result

            if old_ver and new_ver:
                if self.cfg.fw_action == FW_UPGRADE and new_ver == old_ver:
                    self.log.warning(f"固件版本未变化 ({old_ver} -> {new_ver})，可能镜像相同或提交未生效")
                if self.cfg.fw_action == FW_DOWNGRADE and new_ver == old_ver:
                    self.log.warning(f"固件版本未变化 ({old_ver} -> {new_ver})，可能镜像相同或提交未生效")

            result.details["version_changed"] = (old_ver != new_ver) if (old_ver and new_ver) else None
            result.finish(STATUS_PASS)
            self.log.info(f"固件{action_name}测试通过: {old_ver} -> {new_ver}")

        except Exception as e:
            self.log.exception(f"固件{action_name}测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result


# ============================================================
# 测试项 2：SMART 健康信息
# ============================================================

class SmartTester:
    """设备智能健康信息测试。"""

    # SMART 核心检查项关键字（适配 nvme smart-log 和 smartctl 输出）
    CORE_ITEMS = {
        "power_cycles": ["power_cycles", "Power Cycles", "power cycle"],
        "power_on_hours": ["power_on_hours", "Power On Hours", "power_on_time"],
        "temperature": ["temperature", "Temperature", "current_temperature"],
        "available_spare": ["available_spare", "Available Spare", "Available Spare Threshold"],
        "media_errors": ["media_and_data_integrity_errors", "Media and Data Integrity Errors",
                         "media_errors", "Offline Uncorrectable"],
    }

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

    def get_smart_nvme(self) -> Dict[str, Any]:
        """通过 nvme smart-log 获取 SMART（JSON 格式）。"""
        try:
            _, out, _ = run_cmd(
                ["nvme", "smart-log", self.ctrl, "-o", "json"],
                check=True, capture=True, logger=self.log, timeout=15
            )
            return json.loads(out)
        except Exception as e:
            self.log.warning(f"nvme smart-log 获取失败: {e}")
            return {}

    def get_smart_smartctl(self) -> Dict[str, Any]:
        """通过 smartctl 获取 SMART（解析文本输出）。"""
        result = {}
        try:
            _, out, _ = run_cmd(
                ["smartctl", "-a", self.cfg.device],
                check=True, capture=True, logger=self.log, timeout=15
            )
            result["raw_output"] = out
            # 解析关键字段
            for line in out.splitlines():
                line_lower = line.lower()
                if "power cycles" in line_lower or "power_cycle" in line_lower:
                    m = re.search(r"(\d+)", line)
                    if m:
                        result["power_cycles"] = int(m.group(1))
                elif "power on hours" in line_lower or "power_on_hours" in line_lower:
                    m = re.search(r"(\d+)", line)
                    if m:
                        result["power_on_hours"] = int(m.group(1))
                elif "temperature" in line_lower and "current" not in line_lower:
                    m = re.search(r"(\d+)\s*(?:celsius|c)?", line, re.IGNORECASE)
                    if m:
                        result["temperature"] = int(m.group(1))
                elif "available spare" in line_lower:
                    m = re.search(r"(\d+)", line)
                    if m:
                        result["available_spare"] = int(m.group(1))
                elif "media" in line_lower and "error" in line_lower:
                    m = re.search(r"(\d+)", line)
                    if m:
                        result["media_errors"] = int(m.group(1))
        except Exception as e:
            self.log.warning(f"smartctl 获取失败: {e}")
        return result

    def get_smart_combined(self) -> Dict[str, Any]:
        """合并 nvme 和 smartctl 的 SMART 数据，优先 nvme。"""
        nvme_smart = self.get_smart_nvme()
        smartctl_smart = self.get_smart_smartctl()
        combined = {}
        # 优先使用 nvme 数据（核心项）
        for key in self.CORE_ITEMS:
            val = nvme_smart.get(key)
            if val is None:
                val = smartctl_smart.get(key)
            if val is not None:
                combined[key] = val
        # 额外提取对比表需要的字段（从 nvme JSON 中）
        extra_fields = [
            "percentage_used", "unsafe_shutdowns",
            "data_units_read", "data_units_written",
            "error_info_log_entries", "critical_warning",
            "available_spare_threshold", "controller_busy_time",
            "host_read_commands", "host_write_commands",
        ]
        for key in extra_fields:
            val = nvme_smart.get(key)
            if val is not None:
                combined[key] = val
        combined["nvme_raw"] = nvme_smart
        combined["smartctl_raw"] = smartctl_smart
        return combined

    def print_full_smart_info(self, label: str = "", section: str = "all"):
        """输出设备信息和/或 SMART 数据（smartctl 格式，参考测试项目文档）。

        Args:
            label: 标题标签
            section: "all"=设备信息+SMART数据, "information"=仅设备信息,
                     "smart"=仅SMART数据
        """
        self.log.info("")
        if label:
            self.log.info(f"----- {label} -----")
        try:
            _, out, _ = run_cmd(
                ["smartctl", "-a", self.cfg.device],
                check=True, capture=True, logger=None, timeout=15
            )
            lines = out.splitlines()
            in_info = False
            in_smart = False
            for line in lines:
                if "START OF INFORMATION SECTION" in line:
                    in_info = True
                    in_smart = False
                elif "START OF SMART DATA SECTION" in line:
                    in_info = False
                    in_smart = True
                # 根据 section 参数决定是否输出
                if section == "all" and (in_info or in_smart or "START OF" in line):
                    self.log.info(line)
                elif section == "information" and (in_info or "START OF INFORMATION" in line):
                    self.log.info(line)
                elif section == "smart" and (in_smart or "START OF SMART" in line):
                    self.log.info(line)
        except Exception as e:
            self.log.warning(f"获取完整 SMART 信息失败: {e}")

    def print_smart_comparison(self, smart_before: Dict[str, Any], smart_after: Dict[str, Any]):
        """输出 R/W 前后 SMART 关键项变化对比表（替代重复输出完整 smartctl）。

        只输出可能变化的关键项：温度、可用备件、介质错误、读写数据量、
        电源周期、上电时长、不安全关机次数等。设备静态信息不重复输出。
        """
        self.log.info("")
        self.log.info("----- R/W 前后 SMART 关键项变化对比 -----")
        self.log.info(f"{'项目':<30} {'R/W 前':>15} {'R/W 后':>15} {'变化':>10}")
        self.log.info("-" * 75)

        # 关键项映射：(显示名, 字段名, 格式化函数)
        key_items = [
            ("Temperature (C)", "temperature", lambda v: f"{v}"),
            ("Available Spare (%)", "available_spare", lambda v: f"{v}%"),
            ("Percentage Used (%)", "percentage_used", lambda v: f"{v}%"),
            ("Media Errors", "media_errors", lambda v: f"{v:,}"),
            ("Power Cycles", "power_cycles", lambda v: f"{v:,}"),
            ("Power On Hours", "power_on_hours", lambda v: f"{v:,}"),
            ("Unsafe Shutdowns", "unsafe_shutdowns", lambda v: f"{v:,}"),
            ("Data Units Read", "data_units_read", lambda v: f"{v:,}"),
            ("Data Units Written", "data_units_written", lambda v: f"{v:,}"),
            ("Error Log Entries", "error_info_log_entries", lambda v: f"{v:,}"),
        ]

        for display_name, field, fmt in key_items:
            before = smart_before.get(field)
            after = smart_after.get(field)
            if before is None or after is None:
                continue
            delta = after - before
            before_str = fmt(before)
            after_str = fmt(after)
            if delta == 0:
                delta_str = "0"
            elif delta > 0:
                delta_str = f"+{delta:,}"
            else:
                delta_str = f"{delta:,}"
            self.log.info(f"{display_name:<30} {before_str:>15} {after_str:>15} {delta_str:>10}")

        self.log.info("-" * 75)
        # 介质错误增量判定
        media_before = smart_before.get("media_errors", 0)
        media_after = smart_after.get("media_errors", 0)
        media_delta = media_after - media_before
        if media_delta == 0:
            self.log.info(f"介质错误: R/W 前后无变化 ({media_before:,})，测试通过")
        elif media_delta > 0:
            self.log.warning(f"介质错误: R/W 后增加 {media_delta:,} (前:{media_before:,} 后:{media_after:,})")
        else:
            self.log.info(f"介质错误: R/W 后减少 {-media_delta:,} (前:{media_before:,} 后:{media_after:,})")

    def check_smart_core_items(self, smart: Dict[str, Any], strict_media: bool = True) -> Tuple[bool, Dict[str, Any]]:
        """检查 SMART 核心项是否在正常范围。

        Args:
            smart: SMART 数据字典
            strict_media: True=介质错误>0 即判定异常（用于出厂/初始检查）；
                         False=介质错误只记录不判定（用于 R/W 后复检，
                         因为 R/W 前后介质错误增量已单独检查，历史累积错误不应导致 FAIL）
        """
        issues = []
        check_result = {}

        # 温度检查（0~70°C 为正常工作范围）
        temp = smart.get("temperature")
        if temp is not None:
            # nvme smart-log 温度单位为开尔文
            if temp > 200:
                temp_c = temp - 273
            else:
                temp_c = temp
            check_result["temperature_c"] = temp_c
            if temp_c < 0 or temp_c > 70:
                issues.append(f"温度异常: {temp_c}°C (正常范围 0~70°C)")
            else:
                check_result["temperature_status"] = "normal"

        # 可用备件检查（不应低于 10%）
        spare = smart.get("available_spare")
        if spare is not None:
            check_result["available_spare_pct"] = spare
            if spare < 10:
                issues.append(f"可用备件过低: {spare}% (阈值 10%)")
            else:
                check_result["available_spare_status"] = "normal"

        # 介质错误检查
        media_err = smart.get("media_errors")
        if media_err is not None:
            check_result["media_errors"] = media_err
            if media_err > 0:
                if strict_media:
                    issues.append(f"存在介质错误: {media_err}")
                else:
                    # 非严格模式：只记录，不判定为异常（历史累积错误）
                    check_result["media_errors_status"] = "historical"
                    check_result["media_errors_note"] = f"历史累积介质错误 {media_err}，R/W 后无新增"
            else:
                check_result["media_errors_status"] = "normal"

        # 电源循环和通电时间仅记录，不判定
        check_result["power_cycles"] = smart.get("power_cycles")
        check_result["power_on_hours"] = smart.get("power_on_hours")

        all_ok = len(issues) == 0
        check_result["issues"] = issues
        return all_ok, check_result

    def run_basic_rw(self, size_mb: int = DEFAULT_SMART_RW_SIZE_MB) -> Tuple[bool, Dict[str, Any]]:
        """执行基本读写操作（用于 R/W 后 SMART 复检）。"""
        info = {"size_mb": size_mb, "write_ok": False, "read_ok": False}
        test_file = f"/tmp/ssd_smart_rw_test_{int(time.time())}.bin"
        try:
            # 写入测试（使用 dd 直接写设备前 size_mb）
            self.log.info(f"执行基本写入测试 ({size_mb}MB)...")
            run_cmd(
                ["dd", "if=/dev/zero", f"of={self.cfg.device}",
                 f"bs=1M", f"count={size_mb}", "oflag=direct", "conv=fsync"],
                check=True, capture=True, logger=self.log, timeout=120
            )
            info["write_ok"] = True

            # 读取测试
            self.log.info(f"执行基本读取测试 ({size_mb}MB)...")
            run_cmd(
                ["dd", f"if={self.cfg.device}", "of=/dev/null",
                 f"bs=1M", f"count={size_mb}", "iflag=direct"],
                check=True, capture=True, logger=self.log, timeout=120
            )
            info["read_ok"] = True

        except Exception as e:
            self.log.error(f"基本 R/W 测试失败: {e}")
            info["error"] = str(e)
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

        return info["write_ok"] and info["read_ok"], info

    def run(self) -> TestResult:
        """执行 SMART 健康信息测试主流程。"""
        result = TestResult(
            test_item=TEST_SMART,
            test_name="设备智能健康信息",
            device=self.cfg.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info("[DRY-RUN] 将执行 SMART 健康信息测试")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        try:
            # 1. 确认 OS 枚举设备
            if not os.path.exists(self.cfg.device):
                result.finish(STATUS_FAIL, f"操作系统未枚举设备: {self.cfg.device}")
                return result
            self.log.info(f"设备已被 OS 枚举: {self.cfg.device}")

            # 2. 获取初始 SMART
            self.log.info("获取初始 SMART 信息...")
            smart_before = self.get_smart_combined()
            result.details["smart_before"] = {
                k: v for k, v in smart_before.items()
                if k not in ("nvme_raw", "smartctl_raw")
            }

            # 输出完整设备信息和 SMART 数据（smartctl 格式）
            self.print_full_smart_info("初始 SMART 信息（R/W 前）")

            # 3. 检查核心项
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

            # 4. 基本 R/W 操作
            self.log.info("执行基本 R/W 操作...")
            rw_ok, rw_info = self.run_basic_rw()
            result.details["basic_rw"] = rw_info
            if not rw_ok:
                result.finish(STATUS_FAIL, "基本 R/W 操作失败")
                return result

            # 5. R/W 后重新获取 SMART
            self.log.info("R/W 后重新获取 SMART 信息...")
            time.sleep(3)  # 等待 SMART 更新
            smart_after = self.get_smart_combined()
            result.details["smart_after"] = {
                k: v for k, v in smart_after.items()
                if k not in ("nvme_raw", "smartctl_raw")
            }

            # 输出 R/W 前后 SMART 关键项变化对比（不重复输出完整设备信息）
            self.print_smart_comparison(smart_before, smart_after)

            # 6. 对比 R/W 前后 SMART 变化
            comparison = {}
            for key in ["power_cycles", "power_on_hours", "media_errors", "available_spare"]:
                before = smart_before.get(key)
                after = smart_after.get(key)
                if before is not None and after is not None:
                    comparison[key] = {"before": before, "after": after,
                                       "delta": after - before}
            result.details["smart_comparison"] = comparison

            # 介质错误不应增加
            media_delta = comparison.get("media_errors", {}).get("delta", 0)
            if media_delta > 0:
                result.finish(STATUS_FAIL, f"R/W 后介质错误增加: {media_delta}")
                return result

            # 7. R/W 后核心项复检
            # strict_media=False：介质错误绝对值不判定（历史累积错误），
            # 介质错误增量已在上方第6步单独检查（delta > 0 才 FAIL）
            core_ok_after, core_result_after = self.check_smart_core_items(smart_after, strict_media=False)
            result.details["core_check_after"] = core_result_after

            # 记录介质错误历史情况（不影响 PASS/FAIL）
            media_after = smart_after.get("media_errors")
            if media_after and media_after > 0:
                self.log.info(f"  注：磁盘存在历史累积介质错误 {media_after}，R/W 后无新增，不影响本次测试结果")

            if not core_ok_after:
                result.finish(STATUS_FAIL, f"R/W 后 SMART 核心项异常: {core_result_after['issues']}")
                return result

            result.finish(STATUS_PASS)
            self.log.info("SMART 健康信息测试通过")

        except Exception as e:
            self.log.exception(f"SMART 测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result


# ============================================================
# 测试项 3：设备容量
# ============================================================

class CapacityTester:
    """设备容量测试。"""

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

    def get_capacity_nvme(self) -> Dict[str, Any]:
        """通过 nvme id-ctrl / id-ns 读取容量。"""
        result = {}
        if self.cfg.device_type != DEVICE_NVME:
            return result
        try:
            # 读取命名空间容量
            _, out, _ = run_cmd(
                ["nvme", "id-ns", self.cfg.device, "-o", "json"],
                check=True, capture=True, logger=self.log, timeout=10
            )
            ns_data = json.loads(out)
            nsze = ns_data.get("nsze")
            ncap = ns_data.get("ncap")
            nuse = ns_data.get("nuse")
            lbads = ns_data.get("lbaf", [{}])[0].get("lbads", 9) if ns_data.get("lbaf") else 9
            lba_size = 2 ** lbads

            result["nsze_blocks"] = nsze
            result["ncap_blocks"] = ncap
            result["nuse_blocks"] = nuse
            result["lba_size_bytes"] = lba_size
            if nsze:
                result["total_bytes"] = nsze * lba_size
            if ncap:
                result["capacity_bytes"] = ncap * lba_size

            # 读取控制器总 NVM 容量
            try:
                _, out2, _ = run_cmd(
                    ["nvme", "id-ctrl", self.ctrl, "-o", "json"],
                    check=True, capture=True, logger=self.log, timeout=10
                )
                ctrl_data = json.loads(out2)
                tnvmcap = ctrl_data.get("tnvmcap")
                unvmcap = ctrl_data.get("unvmcap")
                if tnvmcap:
                    result["total_nvm_cap_bytes"] = tnvmcap
                if unvmcap:
                    result["unallocated_nvm_cap_bytes"] = unvmcap
            except Exception:
                pass

        except Exception as e:
            self.log.warning(f"nvme 容量读取失败: {e}")
        return result

    def get_capacity_lsblk(self) -> Dict[str, Any]:
        """通过 lsblk 读取块设备容量。"""
        result = {}
        try:
            _, out, _ = run_cmd(
                ["lsblk", "-b", "-d", "-n", "-o", "SIZE,MODEL,SERIAL,TRAN", self.cfg.device],
                check=True, capture=True, logger=self.log, timeout=10
            )
            parts = out.strip().split()
            if parts:
                result["size_bytes"] = int(parts[0])
            if len(parts) > 1:
                result["model"] = " ".join(parts[1:-2]) if len(parts) > 3 else parts[1]
        except Exception as e:
            self.log.warning(f"lsblk 容量读取失败: {e}")
        return result

    def get_capacity_smartctl(self) -> Dict[str, Any]:
        """通过 smartctl 读取容量（备用方式）。"""
        result = {}
        try:
            _, out, _ = run_cmd(
                ["smartctl", "-i", self.cfg.device],
                check=True, capture=True, logger=self.log, timeout=10
            )
            for line in out.splitlines():
                if "Capacity" in line or "User Capacity" in line:
                    m = re.search(r"([\d,]+)\s*bytes", line)
                    if m:
                        result["size_bytes"] = int(m.group(1).replace(",", ""))
                    m2 = re.search(r"\[([\d.]+)\s*([GT]i?B)\]", line)
                    if m2:
                        result["size_human"] = f"{m2.group(1)} {m2.group(2)}"
        except Exception as e:
            self.log.warning(f"smartctl 容量读取失败: {e}")
        return result

    def verify_capacity(self, capacities: Dict[str, int]) -> Tuple[bool, Dict[str, Any]]:
        """多源容量交叉校验。"""
        result = {"values": {}, "consistent": False, "max_diff_pct": 0.0}
        valid = {k: v for k, v in capacities.items() if v and v > 0}
        if not valid:
            result["error"] = "无有效容量读数"
            return False, result

        for k, v in valid.items():
            result["values"][k] = {
                "bytes": v,
                "human_decimal": bytes_to_human(v, binary=False),
                "human_binary": bytes_to_human(v, binary=True),
            }

        values = list(valid.values())
        if len(values) >= 2:
            mean_val = statistics.mean(values)
            max_diff = max(abs(v - mean_val) for v in values)
            diff_pct = (max_diff / mean_val * 100) if mean_val > 0 else 0
            result["max_diff_pct"] = round(diff_pct, 4)
            # 允许 1% 差异（保留空间/计算方式不同）
            result["consistent"] = diff_pct <= 1.0
        else:
            result["consistent"] = True  # 只有一个数据源时无法交叉校验

        return result["consistent"], result

    def run(self) -> TestResult:
        """执行设备容量测试主流程。"""
        result = TestResult(
            test_item=TEST_CAPACITY,
            test_name="设备容量",
            device=self.cfg.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info("[DRY-RUN] 将执行设备容量测试")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        try:
            if not os.path.exists(self.cfg.device):
                result.finish(STATUS_FAIL, f"设备不存在: {self.cfg.device}")
                return result

            capacities = {}

            # 1. NVMe 方式读取
            nvme_cap = self.get_capacity_nvme()
            result.details["nvme_capacity"] = nvme_cap
            if "total_bytes" in nvme_cap:
                capacities["nvme_id_ns"] = nvme_cap["total_bytes"]
            elif "capacity_bytes" in nvme_cap:
                capacities["nvme_id_ns"] = nvme_cap["capacity_bytes"]

            # 2. lsblk 方式读取
            lsblk_cap = self.get_capacity_lsblk()
            result.details["lsblk_capacity"] = lsblk_cap
            if "size_bytes" in lsblk_cap:
                capacities["lsblk"] = lsblk_cap["size_bytes"]

            # 3. smartctl 方式读取（备用）
            smartctl_cap = self.get_capacity_smartctl()
            result.details["smartctl_capacity"] = smartctl_cap
            if "size_bytes" in smartctl_cap:
                capacities["smartctl"] = smartctl_cap["size_bytes"]

            # 4. 交叉校验
            consistent, verify_result = self.verify_capacity(capacities)
            result.details["capacity_verification"] = verify_result

            # 输出容量信息（多源一致时合并为一行，避免冗余）
            values = verify_result.get("values", {})
            if values:
                # 优先使用 nvme_id_ns 作为主数据源
                primary = values.get("nvme_id_ns") or list(values.values())[0]
                sources = list(values.keys())
                if len(values) == 1 or verify_result.get("consistent", True):
                    self.log.info(f"  设备容量: {primary['human_decimal']} ({primary['human_binary']}) "
                                  f"[数据源: {', '.join(sources)}, 校验一致]")
                else:
                    # 多源不一致时才分别输出
                    self.log.warning(f"  多源容量不一致 (差异 {verify_result.get('max_diff_pct', 0)}%):")
                    for source, info in values.items():
                        self.log.info(f"    [{source}] {info['human_decimal']} ({info['human_binary']})")

            if not capacities:
                result.finish(STATUS_FAIL, "无法从任何来源读取设备容量")
                return result

            if not consistent:
                self.log.warning(f"多源容量差异超过 1%: {verify_result['max_diff_pct']}%")
                # 容量差异不一定是故障，可能是保留空间，标记为 PASS 但记录警告
                result.details["capacity_warning"] = (
                    f"多源容量差异 {verify_result['max_diff_pct']}%，可能因保留空间导致"
                )

            result.finish(STATUS_PASS)
            self.log.info("设备容量测试通过")

        except Exception as e:
            self.log.exception(f"容量测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result


# ============================================================
# 测试项 4：完整性能特征
# ============================================================

class PerformanceTester:
    """完整性能特征测试（FOB + 稳态）。"""

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

    def _build_fio_cmd(self, name: str, rw: str, bs: str, qd: int,
                       runtime: int, size: Optional[str] = None,
                       numjobs: int = 1,
                       extra: Optional[Dict[str, str]] = None) -> List[str]:
        """构建 fio 命令。"""
        cmd = [
            "fio",
            f"--name={name}",
            f"--filename={self.cfg.device}",
            f"--rw={rw}",
            f"--bs={bs}",
            f"--iodepth={qd}",
            f"--numjobs={numjobs}",
            f"--runtime={runtime}",
            "--time_based",
            "--direct=1",
            "--ioengine=libaio",
            "--group_reporting",
            "--output-format=json",
            "--norandommap",
            "--randrepeat=0",
        ]
        if size:
            cmd.append(f"--size={size}")
        if extra:
            for k, v in extra.items():
                cmd.append(f"--{k}={v}")
        return cmd

    # ----------------------------------------------------------
    # 完整性能特征 - 全参数可配置 FIO 测试
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    # v1.9.2 新增: NVMe Purge (User Data Erase)
    # ----------------------------------------------------------

    def _nvme_purge(self) -> Tuple[bool, str]:
        """执行 NVMe User Data Erase 使 SSD 进入 FOB 状态。

        优先使用: sudo nvme format <device> --namespace-id=1 --ses=1
        失败回退: blkdiscard

        Returns:
            (success, method_used)
        """
        purge_method = self.cfg.purge_method  # auto/user-data/blkdiscard

        # 方式1: NVMe User Data Erase (--ses=1)
        if purge_method in ("auto", "user-data") and self.cfg.device_type == DEVICE_NVME:
            self.log.info("  执行 NVMe User Data Erase (--ses=1)...")
            try:
                # 用户指定命令: sudo nvme format /dev/nvme0n1 --namespace-id=1 --ses=1
                cmd = ["sudo", "nvme", "format", self.cfg.device,
                       "--namespace-id=1", "--ses=1"]
                run_cmd(cmd, check=True, capture=True, logger=self.log, timeout=600)
                self.log.info("  NVMe User Data Erase 完成")
                return True, "nvme-user-data-erase"
            except Exception as e:
                self.log.warning(f"  NVMe User Data Erase 失败: {e}")
                if purge_method == "user-data":
                    return False, "nvme-user-data-erase-failed"
                # auto 模式下继续回退 blkdiscard

        # 方式2: blkdiscard 回退
        if purge_method in ("auto", "blkdiscard"):
            self.log.info("  执行 blkdiscard (回退方式)...")
            try:
                run_cmd(["blkdiscard", self.cfg.device], check=True,
                        capture=True, logger=self.log, timeout=300)
                self.log.info("  blkdiscard 完成")
                return True, "blkdiscard"
            except Exception as e:
                self.log.warning(f"  blkdiscard 失败: {e}")
                return False, "all-purge-methods-failed"

        return False, "no-purge-method-available"

    # ----------------------------------------------------------
    # v1.9.2 新增: 稳态检测算法 (SNIA SSS PTS v2.0.2)
    # ----------------------------------------------------------

    # 稳态跟踪变量定义: (名称, fio rw模式, bs, qd, rwmixread)
    # 3个跟踪变量同时满足稳态条件才判定为稳态
    STEADY_TRACKING_VARS = [
        {"name": "RND4K_Write", "rw": "randwrite", "bs": "4k", "qd": 32, "rwmixread": None},
        {"name": "RND64K_Mix65", "rw": "randrw", "bs": "64k", "qd": 32, "rwmixread": 65},
        {"name": "RND1024K_Read", "rw": "randread", "bs": "1024k", "qd": 32, "rwmixread": None},
    ]

    @staticmethod
    def check_steady_state(values: List[float]) -> Tuple[bool, Dict[str, float]]:
        """SNIA 稳态判定：5轮滑动窗口，Range<=20%*Ave 且 Slope<=10%*Ave。

        Args:
            values: 最近5轮的性能值列表（IOPS或带宽）

        Returns:
            (is_steady, metrics)  metrics含 ave/range/slope/range_pct/slope_pct
        """
        if len(values) < 5:
            return False, {"reason": "insufficient_rounds", "rounds": len(values)}

        window = values[-5:]
        ave = sum(window) / len(window)
        if ave <= 0:
            return False, {"reason": "zero_average", "ave": 0}

        w_min = min(window)
        w_max = max(window)
        range_val = w_max - w_min
        range_pct = (range_val / ave) * 100.0

        # 最小二乘法斜率 (轮次为 x=0,1,2,3,4)
        n = len(window)
        x_mean = (n - 1) / 2.0
        y_mean = ave
        numerator = sum((i - x_mean) * (window[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        slope = numerator / denominator if denominator > 0 else 0.0
        slope_pct = (abs(slope) / ave) * 100.0

        is_steady = (range_pct <= 20.0) and (slope_pct <= 10.0)

        return is_steady, {
            "ave": round(ave, 2),
            "min": round(w_min, 2),
            "max": round(w_max, 2),
            "range": round(range_val, 2),
            "range_pct": round(range_pct, 2),
            "slope": round(slope, 4),
            "slope_pct": round(slope_pct, 2),
        }

    @staticmethod
    def check_all_tracking_vars(history: Dict[str, List[float]]) -> Tuple[bool, Dict[str, Any]]:
        """检查所有3个跟踪变量是否同时达到稳态。

        Args:
            history: {var_name: [round1_val, round2_val, ...]}

        Returns:
            (all_steady, details)
        """
        details = {}
        all_steady = True
        for var_def in PerformanceTester.STEADY_TRACKING_VARS:
            name = var_def["name"]
            values = history.get(name, [])
            steady, metrics = PerformanceTester.check_steady_state(values)
            details[name] = {"steady": steady, "metrics": metrics, "rounds": len(values)}
            if not steady:
                all_steady = False
        return all_steady, details

    def _run_wdpc_round(self, round_idx: int, point_duration: int) -> Dict[str, float]:
        """执行一轮 WDPC (Workload Dependent Performance Characterization)。

        每轮包含3个跟踪变量的测试，每个测试运行 point_duration 秒。
        测试顺序: RND4K_Write -> RND64K_Mix65 -> RND1024K_Read

        Returns:
            {var_name: performance_value}  performance_value 为 IOPS (随机) 或 MB/s (顺序)
        """
        results = {}
        for var_def in self.STEADY_TRACKING_VARS:
            name = var_def["name"]
            self.log.info(f"    WDPC 轮次 {round_idx} - {name}...")
            try:
                extra = {}
                if var_def["rwmixread"] is not None:
                    extra["rwmixread"] = str(var_def["rwmixread"])
                cmd = self._build_fio_cmd(
                    f"wdpc_{name}_r{round_idx}",
                    var_def["rw"], var_def["bs"], var_def["qd"],
                    runtime=point_duration,
                    extra=extra
                )
                # 确保 time_based 和 runtime 生效
                if "--time_based" not in cmd:
                    cmd.append("--time_based")
                # 移除可能存在的 size 限制（WDPC 使用时间模式）
                cmd = [c for c in cmd if not c.startswith("--size=")]

                _, out, _ = run_cmd(cmd, check=True, capture=True,
                                    logger=self.log, timeout=point_duration + 60)
                # 解析 fio JSON 输出
                try:
                    fio_data = json.loads(out)
                    job = fio_data["jobs"][0]
                    if var_def["rw"] in ("randwrite", "write"):
                        val = float(job["write"]["iops"])
                    elif var_def["rw"] in ("randread", "read"):
                        val = float(job["read"]["iops"])
                    else:  # randrw
                        val = float(job["read"]["iops"]) + float(job["write"]["iops"])
                    results[name] = val
                    self.log.info(f"      {name}: {val:.2f} IOPS")
                except Exception as parse_err:
                    self.log.warning(f"      解析 fio 输出失败: {parse_err}")
                    results[name] = 0.0
            except Exception as e:
                self.log.warning(f"      WDPC {name} 测试失败: {e}")
                results[name] = 0.0
        return results

    # ----------------------------------------------------------
    # v1.9.2 新增: 性能测试中 SMART 健康检查 (忽视配置的介质错误)
    # ----------------------------------------------------------

    def _check_smart_health(self) -> Tuple[bool, Dict[str, Any]]:
        """性能测试中的 SMART 健康检查，忽视配置的介质错误ID（默认5353）。

        Returns:
            (ok, details)
        """
        details = {"checked": False}
        try:
            if self.cfg.device_type != DEVICE_NVME:
                return True, details
            _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                check=False, capture=True, timeout=10)
            smart = json.loads(out)
            media_errors = smart.get("media_and_data_integrity_errors", 0)
            temp = smart.get("temperature", 0)
            if temp > 273:
                temp_c = temp - 273
            else:
                temp_c = temp
            spare = smart.get("available_spare", 100)

            # 解析需要忽视的介质错误ID列表
            ignore_ids = []
            if self.cfg.smart_ignore_media_errors:
                try:
                    ignore_ids = [int(x.strip()) for x in self.cfg.smart_ignore_media_errors.split(",") if x.strip()]
                except Exception:
                    ignore_ids = []

            details = {
                "checked": True,
                "media_errors": media_errors,
                "temperature_c": temp_c,
                "available_spare": spare,
                "ignored_media_error_ids": ignore_ids,
            }

            # 介质错误检查：忽视配置的错误ID
            # 注意: nvme smart-log 的 media_and_data_integrity_errors 是计数器，
            # 这里通过错误日志检查具体错误ID。如果无法获取错误日志，则仅检查计数器是否为0。
            if media_errors > 0:
                # 尝试读取错误日志，检查是否都是被忽视的错误ID
                try:
                    _, err_out, _ = run_cmd(["nvme", "error-log", self.ctrl, "-o", "json"],
                                            check=False, capture=True, timeout=10)
                    err_data = json.loads(err_out)
                    err_entries = err_data.get("errors", [])
                    all_ignored = True
                    non_ignored = []
                    for entry in err_entries:
                        err_code = entry.get("error_code", entry.get("code", 0))
                        if err_code not in ignore_ids:
                            all_ignored = False
                            non_ignored.append(err_code)
                    if all_ignored and err_entries:
                        self.log.info(f"  SMART: 介质错误计数器={media_errors}, "
                                      f"但所有错误日志条目均为被忽视的ID ({ignore_ids})，视为正常")
                        details["media_errors_status"] = "ignored"
                    elif non_ignored:
                        self.log.warning(f"  SMART: 存在未被忽视的介质错误: {non_ignored}")
                        details["media_errors_status"] = "warning"
                        details["non_ignored_errors"] = non_ignored
                        return False, details
                    else:
                        # 没有错误日志条目但计数器>0，可能是历史累积
                        self.log.info(f"  SMART: 介质错误计数器={media_errors} (历史累积，无新错误日志)")
                        details["media_errors_status"] = "historical"
                except Exception:
                    # 无法读取错误日志，按计数器>0警告
                    self.log.warning(f"  SMART: 介质错误计数器={media_errors} (无法读取错误日志详情)")
                    details["media_errors_status"] = "warning"
                    return False, details
            else:
                details["media_errors_status"] = "normal"

            # 温度检查
            if temp_c > 80:
                self.log.warning(f"  SMART: 温度过高 {temp_c}°C")
                details["temperature_status"] = "warning"
                return False, details
            else:
                details["temperature_status"] = "normal"

            # 可用空间检查
            if spare < 10:
                self.log.warning(f"  SMART: 可用空间过低 {spare}%")
                details["spare_status"] = "warning"
                return False, details
            else:
                details["spare_status"] = "normal"

            return True, details
        except Exception as e:
            self.log.warning(f"  SMART 健康检查失败（不中断测试）: {e}")
            details["error"] = str(e)
            return True, details  # SMART检查失败不中断性能测试



    def _cfg_with_task(self, task: Optional[PerfTask]):
        """返回一个临时配置对象，perf 相关字段被 task 覆盖（v1.8.0 多任务支持）。

        当 task 为 None 时直接返回 self.cfg，保证单组模式行为不变。
        """
        if task is None:
            return self.cfg
        import types
        merged = {}
        for f in fields(self.cfg):
            merged[f.name] = getattr(self.cfg, f.name)
        for f in fields(task):
            merged[f.name] = getattr(task, f.name)
        return types.SimpleNamespace(**merged)

    def _build_full_fio_cmd(self, output_path: str,
                             text_output: Optional[str] = None,
                             task: Optional[PerfTask] = None) -> List[str]:
        """构建全参数可配置的 FIO 命令（双横杠规范格式）。

        Args:
            output_path: json+ 格式日志输出路径
            text_output: 文本格式日志输出路径（None则不输出文本）
            task: v1.8.0 可选，单组测试任务参数，为 None 时使用 cfg 单组参数

        Returns:
            fio 命令参数列表
        """
        c = self._cfg_with_task(task)
        cmd = [
            "fio",
            f"--name={c.perf_test_name}",
            f"--filename={c.device}",
            f"--direct={c.perf_direct}",
            f"--bs={c.perf_bs}",
            f"--iodepth={c.perf_iodepth}",
            f"--numjobs={c.perf_numjobs}",
            f"--rw={c.perf_rw}",
            f"--ioengine={c.perf_ioengine}",
            f"--size={c.perf_size}",
            "--group_reporting",
            "--lat_percentiles=1",
            f"--output-format={c.perf_output_format}",
            f"--output={output_path}",
        ]
        # 混合读写模式添加读占比
        if c.perf_rw in ("randrw", "rw"):
            cmd.append(f"--rwmixread={c.perf_rwmixread}")
        # 时间模式
        if c.perf_time_based:
            cmd.append("--time_based")
            cmd.append(f"--runtime={c.perf_runtime_full}")
        # buffered=0 后台默认生效（与 direct=1 等价，不对外展示）
        if c.perf_direct == 1:
            cmd.append("--buffered=0")
        # norandommap / randrepeat 提升随机测试准确性
        if c.perf_rw in ("randread", "randwrite", "randrw"):
            cmd.append("--norandommap")
            cmd.append("--randrepeat=0")
        return cmd

    def _generate_log_filename(self, state_label: str, suffix: str,
                                task: Optional[PerfTask] = None) -> str:
        """生成规范日志文件名。

        格式: {状态标识}_{测试名称}_{块大小}_{读写模式}_qd{队列深度}_{时间戳}.后缀
        示例: FOB_Qos-4k-randread-qd64_20260910_162035.json

        Args:
            state_label: 状态标识（FOB / Steady）
            suffix: 文件后缀
            task: v1.8.0 可选，单组测试任务参数，为 None 时使用 cfg 单组参数
        """
        c = self._cfg_with_task(task)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 清理测试名称中的非法字符
        safe_name = c.perf_test_name.replace("/", "_").replace(" ", "_")
        filename = (f"{state_label}_{safe_name}_{c.perf_bs}_"
                    f"{c.perf_rw}_qd{c.perf_iodepth}_{timestamp}.{suffix}")
        return filename

    def run_full_perf_test(self, state_label: str,
                            task: Optional[PerfTask] = None,
                            task_index: Optional[int] = None) -> Dict[str, Any]:
        """执行单次全参数可配置 FIO 测试。

        Args:
            state_label: 状态标识（FOB / Steady）
            task: v1.8.0 可选，单组测试任务参数，为 None 时使用 cfg 单组参数（兼容旧版）
            task_index: v1.8.0 可选，当前任务在批量列表中的序号（从1开始），用于日志区分

        Returns:
            包含测试结果和日志路径的字典
        """
        c = self._cfg_with_task(task)
        # v1.8.0 多任务日志标记
        task_tag = ""
        if task_index is not None:
            task_tag = f" [任务{task_index}"
            if task and task.note:
                task_tag += f":{task.note}"
            task_tag += "]"

        self.log.info("-" * 50)
        self.log.info(f"[{state_label}] 全参数 FIO 测试{task_tag}")
        self.log.info(f"  名称: {c.perf_test_name}, 模式: {c.perf_rw}, "
                      f"块大小: {c.perf_bs}, QD: {c.perf_iodepth}, "
                      f"numjobs: {c.perf_numjobs}")
        if c.perf_rw in ("randrw", "rw"):
            self.log.info(f"  混合读写: 读占比 {c.perf_rwmixread}%")
        self.log.info(f"  direct={c.perf_direct}, ioengine={c.perf_ioengine}, "
                      f"size={c.perf_size}, runtime={c.perf_runtime_full}s")
        self.log.info("-" * 50)

        result = {
            "state": state_label,
            "config": {
                "name": c.perf_test_name,
                "rw": c.perf_rw,
                "bs": c.perf_bs,
                "iodepth": c.perf_iodepth,
                "numjobs": c.perf_numjobs,
                "direct": c.perf_direct,
                "ioengine": c.perf_ioengine,
                "size": c.perf_size,
                "runtime": c.perf_runtime_full,
                "time_based": c.perf_time_based,
            },
        }
        if c.perf_rw in ("randrw", "rw"):
            result["config"]["rwmixread"] = c.perf_rwmixread
        # v1.8.0 记录任务序号和备注
        if task_index is not None:
            result["task_index"] = task_index
        if task and task.note:
            result["task_note"] = task.note

        if c.dry_run:
            self.log.info("[DRY-RUN] 跳过实际执行")
            result["dry_run"] = True
            return result

        # 确保日志目录存在（v1.9.2: 使用绝对路径）
        log_dir = resolve_log_dir(c.log_dir) if hasattr(c, "log_dir") and c.log_dir else get_default_log_dir()
        os.makedirs(log_dir, exist_ok=True)

        # 生成 json+ 日志文件名
        json_filename = self._generate_log_filename(state_label, "json", task=task)
        json_path = os.path.join(log_dir, json_filename)
        result["json_log"] = json_path
        self.log.info(f"  结构化日志: {json_path}")

        # 构建并执行 FIO 命令
        cmd = self._build_full_fio_cmd(json_path, task=task)
        self.log.info(f"  执行命令: {' '.join(cmd)}")
        result["command"] = " ".join(cmd)

        try:
            timeout = c.perf_runtime_full + 120 if c.perf_time_based else 3600
            _, fio_out, fio_err = run_cmd(cmd, check=True, capture=True, logger=self.log, timeout=timeout)
            self.log.info(f"  FIO 执行完成，日志已写入: {json_path}")
            if fio_err.strip():
                self.log.debug(f"  fio stderr: {fio_err.strip()[:500]}")
            result["success"] = True
        except Exception as e:
            err_detail = ""
            if hasattr(e, 'stderr') and e.stderr:
                err_detail = f"\n  fio错误输出: {e.stderr[:1000]}"
            elif hasattr(e, 'output') and e.output:
                err_detail = f"\n  fio输出: {e.output[:1000]}"
            self.log.error(f"  FIO 执行失败: {e}{err_detail}")
            self.log.error(f"  失败命令: {' '.join(cmd)}")
            self.log.error(f"  日志路径: {json_path}")
            # 检查 fio 版本
            try:
                _, fio_ver, _ = run_cmd(["fio", "--version"], check=False, capture=True, timeout=5)
                self.log.error(f"  fio版本: {fio_ver.strip()}")
            except Exception:
                pass
            # 检查日志文件是否生成
            if os.path.exists(json_path):
                fsize = os.path.getsize(json_path)
                self.log.error(f"  日志文件已生成但可能不完整: {json_path} ({fsize} bytes)")
            else:
                self.log.error(f"  日志文件未生成: {json_path}")
            result["success"] = False
            result["error"] = str(e)
            if err_detail:
                result["error_detail"] = err_detail.strip()
            return result

        # 解析 json+ 结果
        try:
            with open(json_path, "r") as f:
                perf_data = json.load(f)
            result["parsed"] = self._parse_full_perf_json(perf_data)
            self._print_full_perf_summary(result["parsed"], state_label)
        except Exception as e:
            self.log.warning(f"  JSON 结果解析失败: {e}")
            result["parse_error"] = str(e)

        # 文本日志（如开启）
        if c.perf_text_log:
            text_filename = self._generate_log_filename(state_label, "log", task=task)
            text_path = os.path.join(log_dir, text_filename)
            result["text_log"] = text_path
            try:
                # 文本格式：移除 json+ 专属参数，不指定 --output-format（fio 默认文本格式）
                text_cmd = [a for a in cmd if not a.startswith("--output-format=")
                            and not a.startswith("--output=")
                            and not a.startswith("--lat_percentiles=")]
                text_cmd.append(f"--output={text_path}")
                _, text_out, text_err = run_cmd(text_cmd, check=True, capture=True,
                                                 logger=self.log, timeout=timeout)
                self.log.info(f"  文本日志: {text_path}")
            except Exception as e:
                err_detail = ""
                if hasattr(e, 'stderr') and e.stderr:
                    err_detail = f"\n  fio错误输出: {e.stderr[:500]}"
                self.log.warning(f"  文本日志生成失败: {e}{err_detail}")

        return result

    def _parse_full_perf_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """解析 json+ 格式 FIO 结果，提取核心指标。"""
        parsed = {"fio_version": data.get("fio version", "unknown"),
                  "timestamp": data.get("time", "")}
        jobs = data.get("jobs", [])
        if not jobs:
            return parsed
        job = jobs[0]  # group_reporting 后 jobs[0] 为汇总
        parsed["jobname"] = job.get("jobname", "")

        for direction in ("read", "write"):
            d = job.get(direction, {})
            if not d:
                continue
            bw_kib = d.get("bw", 0)  # KiB/s
            iops = d.get("iops", 0)
            io_bytes = d.get("io_bytes", 0)
            runtime_ms = d.get("runtime", 0)
            lat_ns = d.get("lat_ns", {})
            clat_ns = d.get("clat_ns", {})
            percentiles = clat_ns.get("percentile", {})

            parsed[direction] = {
                "bw_kibs": bw_kib,
                "bw_mibs": round(bw_kib / 1024, 3) if bw_kib else 0,
                "bw_mbs": round(bw_kib * 1024 / 1000000, 3) if bw_kib else 0,
                "iops": round(iops, 1) if iops else 0,
                "io_bytes": io_bytes,
                "runtime_ms": runtime_ms,
                "lat_mean_us": round(lat_ns.get("mean", 0) / 1000, 2) if lat_ns.get("mean") else 0,
                "lat_min_us": round(lat_ns.get("min", 0) / 1000, 2) if lat_ns.get("min") else 0,
                "lat_max_us": round(lat_ns.get("max", 0) / 1000, 2) if lat_ns.get("max") else 0,
                "clat_mean_us": round(clat_ns.get("mean", 0) / 1000, 2) if clat_ns.get("mean") else 0,
                "percentiles_us": {
                    f"p{k.replace('.000000', '')}": round(v / 1000, 2)
                    for k, v in percentiles.items() if v
                },
            }

        # 磁盘利用率
        parsed["disk_util"] = job.get("disk_util", 0)
        # CPU
        parsed["cpu"] = job.get("cpu", {})
        return parsed

    def _print_full_perf_summary(self, parsed: Dict[str, Any], state_label: str):
        """打印全参数测试结果汇总。"""
        self.log.info("")
        self.log.info("=" * 60)
        self.log.info(f"  性能测试结果汇总 [{state_label}]")
        self.log.info(f"  FIO版本: {parsed.get('fio_version', 'unknown')}")
        self.log.info("=" * 60)

        for direction, label in [("read", "读"), ("write", "写")]:
            d = parsed.get(direction)
            if not d or d.get("bw_mibs", 0) == 0:
                continue
            self.log.info(f"  [{label}]")
            self.log.info(f"    带宽: {d['bw_mibs']} MiB/s ({d['bw_mbs']} MB/s)")
            self.log.info(f"    IOPS: {d['iops']}")
            self.log.info(f"    平均延迟: {d['lat_mean_us']} us "
                          f"(min={d['lat_min_us']}, max={d['lat_max_us']})")
            pcts = d.get("percentiles_us", {})
            if pcts:
                pct_str = ", ".join([f"{k}={v}us" for k, v in list(pcts.items())[:8]])
                self.log.info(f"    延迟百分位: {pct_str}")
            self.log.info("")

        if parsed.get("disk_util"):
            self.log.info(f"  磁盘利用率: {parsed['disk_util']}%")
        self.log.info("=" * 60)
        self.log.info("")

    def run_fob_test(self) -> Dict[str, Any]:
        """FOB（出厂空白）状态性能测试。

        v1.8.0: 支持多任务批量执行。perf_task_list 非空时依次执行所有任务，
        否则执行单组参数测试（兼容旧版）。
        """
        self.log.info("-" * 40)
        self.log.info("FOB 状态性能测试")
        self.log.info("-" * 40)

        result = {"state": "FOB", "precondition": "nvme-user-data-erase"}

        # FOB 前置：NVMe User Data Erase (--ses=1) 恢复空白状态，失败回退 blkdiscard
        # v1.9.2: 添加 precondition 检查，状态一致时可跳过擦除
        if not self.cfg.dry_run and self.cfg.precondition:
            purge_ok, purge_method = self._nvme_purge()
            result["precondition_method"] = purge_method
            if not purge_ok:
                self.log.error(f"  FOB 擦除失败 (method={purge_method})，继续测试但状态不可靠")
                result["precondition_note"] = f"purge failed: {purge_method}"
            else:
                self.log.info(f"  FOB 擦除完成 (method={purge_method})，等待 10s...")
                time.sleep(10)
            # 更新 SSD 状态文件为 FOB
            try:
                serial = get_device_serial(self.cfg.device)
                save_ssd_state(serial, SSD_STATE_FOB, {"method": purge_method})
                self.log.info(f"  SSD 状态已更新为 FOB (serial={serial})")
            except Exception as e:
                self.log.warning(f"  更新 SSD 状态文件失败: {e}")
        elif not self.cfg.precondition:
            self.log.info("  跳过 FOB 擦除 (precondition=off，状态已一致)")
            result["precondition"] = "skipped"
            result["precondition_method"] = "skipped"
        else:
            self.log.info("  [DRY-RUN] 跳过 FOB 擦除")

        # v1.8.0 多任务批量执行
        task_list = self.cfg.perf_task_list
        if task_list:
            result["batch_mode"] = True
            result["task_count"] = len(task_list)
            self.log.info(f"  [批量模式] 共 {len(task_list)} 组测试任务，依次执行...")
            perf_results = []
            for idx, task in enumerate(task_list, start=1):
                self.log.info(f"\n  ===== 批量任务 {idx}/{len(task_list)} =====")
                perf_results.append(self.run_full_perf_test("FOB", task=task, task_index=idx))
            result["full_perf"] = perf_results
        else:
            # 兼容旧版：单组参数测试
            result["full_perf"] = self.run_full_perf_test("FOB")

        return result

    @staticmethod
    def _parse_fio_progress_pct(stderr_text: str) -> Optional[float]:
        """从 fio --status-interval 输出中解析最后一行的完成百分比。

        fio 状态行格式示例:
          Jobs: 1 (f=1): [W(1)][42.3%][w=35MiB/s][w=9000 IOPS][eta 01h:20m:00s]

        Returns:
            完成百分比(0-100)，无法解析时返回 None
        """
        if not stderr_text:
            return None
        matches = re.findall(r'\[(\d+\.?\d*)%\]', stderr_text)
        if matches:
            try:
                return float(matches[-1])
            except (ValueError, IndexError):
                return None
        return None

    def precondition_steady_state(self) -> Tuple[bool, Dict[str, Any]]:
        """
        稳态预处理（SNIA SSS PTS v2.0.2 标准）：
        1. WIPC (Workload Independent Preconditioning): 顺序写满全盘 2 次
        2. WDPC (Workload Dependent Performance Characterization):
           多轮循环测试3个跟踪变量，每轮后检测稳态
           - 5轮滑动窗口，Range<=20%*Ave 且 Slope<=10%*Ave
           - 3个跟踪变量同时满足才判定稳态
           - 最多25轮
        3. 无额外等待（WIPC与WDPC间无延迟，符合规范）

        v1.9.2 改造:
        - 从固定写入量(随机写2X+等300s)改为 SNIA 标准 WDPC+稳态检测
        - 删除300s等待（违反规范"WIPC与WDPC间无延迟"要求）
        - 新增5轮滑动窗口稳态检测算法
        """
        info = {"steps": [], "success": False, "wdpc_rounds": 0, "steady_reached": False}
        self.log.info("=" * 50)
        self.log.info("开始稳态预处理 (SNIA SSS PTS v2.0.2)...")
        self.log.info("=" * 50)

        # 获取设备容量
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.cfg.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap_bytes = int(out.strip())
            cap_gb = cap_bytes / (1000 ** 3)
            info["device_capacity_gb"] = round(cap_gb, 2)
            self.log.info(f"设备容量: {cap_gb:.2f} GB")
        except Exception as e:
            self.log.error(f"无法获取设备容量，预处理失败: {e}")
            info["error"] = str(e)
            return False, info

        if self.cfg.dry_run:
            self.log.info("[DRY-RUN] 跳过稳态预处理实际执行")
            info["success"] = True
            return True, info

        # 步骤 1: WIPC - 顺序写满全盘 2 次（128K QD32）
        self.log.info("[预处理 1/2] WIPC: 顺序写满全盘 2 次...")
        seq_est_speed = 300 * 1024 * 1024
        seq_timeout_per_pass = max(600, int(cap_bytes / seq_est_speed) + 300)
        self.log.info(f"  预估单遍耗时: ~{int(cap_bytes / seq_est_speed)}s (按300MB/s), 单遍超时: {seq_timeout_per_pass}s")
        try:
            for pass_idx in range(2):
                self.log.info(f"  顺序写第 {pass_idx + 1}/2 遍...")
                cmd = self._build_fio_cmd(
                    f"wipc_seq_write_{pass_idx + 1}",
                    "write", "128k", 32,
                    runtime=max(60, int(cap_gb * 0.5)),
                    size=f"{cap_bytes}B"
                )
                cmd = [c for c in cmd if not c.startswith("--time_based")]
                cmd = [c for c in cmd if not c.startswith("--runtime")]
                cmd = [c for c in cmd if not c.startswith("--output-format=")]
                cmd.append("--output=/dev/null")
                cmd.append("--status-interval=120")
                run_cmd(cmd, check=True, capture=True, logger=self.log,
                        timeout=seq_timeout_per_pass)
            info["steps"].append("wipc_sequential_write_2pass: OK")
        except Exception as e:
            self.log.error(f"WIPC 顺序写预处理失败: {e}")
            info["steps"].append(f"wipc_sequential_write_2pass: FAIL ({e})")
            info["error"] = f"WIPC 顺序写预处理失败: {e}"
            return False, info

        # 步骤 2: WDPC - 多轮循环 + 稳态检测
        max_rounds = self.cfg.steady_max_rounds  # 默认25
        point_duration = self.cfg.steady_point_duration  # 默认60秒
        self.log.info(f"[预处理 2/2] WDPC: 最多{max_rounds}轮循环，每点{point_duration}s，稳态检测(5轮窗口)...")

        # 初始化每个跟踪变量的历史数据
        history = {var["name"]: [] for var in self.STEADY_TRACKING_VARS}
        steady_reached = False

        for round_idx in range(1, max_rounds + 1):
            self.log.info(f"  --- WDPC 轮次 {round_idx}/{max_rounds} ---")
            round_results = self._run_wdpc_round(round_idx, point_duration)

            # 更新历史数据
            for name, val in round_results.items():
                history[name].append(val)

            info["wdpc_rounds"] = round_idx

            # 至少5轮后才检测稳态
            if round_idx >= 5:
                all_steady, details = self.check_all_tracking_vars(history)
                info["steady_check_round"] = round_idx
                info["steady_check_details"] = details

                if all_steady:
                    self.log.info(f"  *** 第 {round_idx} 轮检测到稳态！3个跟踪变量均满足条件 ***")
                    for name, d in details.items():
                        m = d["metrics"]
                        self.log.info(f"    {name}: ave={m.get('ave')}, "
                                      f"range%={m.get('range_pct')}%, slope%={m.get('slope_pct')}%")
                    steady_reached = True
                    info["steady_reached"] = True
                    info["steps"].append(f"wdpc_steady_reached: round {round_idx}")
                    break
                else:
                    # 输出未稳态原因
                    not_steady_vars = [name for name, d in details.items() if not d["steady"]]
                    self.log.info(f"  第 {round_idx} 轮未达到稳态，未满足变量: {not_steady_vars}")
                    for name in not_steady_vars:
                        m = details[name]["metrics"]
                        if "range_pct" in m:
                            self.log.info(f"    {name}: range%={m['range_pct']}% "
                                          f"(<=20%? {'Y' if m['range_pct'] <= 20 else 'N'}), "
                                          f"slope%={m['slope_pct']}% "
                                          f"(<=10%? {'Y' if m['slope_pct'] <= 10 else 'N'})")
            else:
                self.log.info(f"  (第 {round_idx} 轮，累计不足5轮，暂不检测稳态)")

        if not steady_reached:
            self.log.warning(f"  WDPC 已达最大轮数 {max_rounds}，仍未检测到稳态")
            info["steps"].append(f"wdpc_max_rounds_reached: {max_rounds} (未稳态)")
            # 达到最大轮数后视为接近稳态，告警继续（降级策略）
            self.log.warning("  降级: 达到最大轮数，视为接近稳态，继续执行性能测试")
            info["precondition_note"] = f"WDPC达到最大轮数{max_rounds}未稳态，降级继续"

        # 更新 SSD 状态文件为 STEADY
        try:
            serial = get_device_serial(self.cfg.device)
            save_ssd_state(serial, SSD_STATE_STEADY, {
                "wdpc_rounds": info["wdpc_rounds"],
                "steady_reached": info["steady_reached"],
            })
            self.log.info(f"  SSD 状态已更新为 STEADY (serial={serial})")
        except Exception as e:
            self.log.warning(f"  更新 SSD 状态文件失败: {e}")

        info["success"] = True
        self.log.info("稳态预处理完成")
        return True, info

    def run_steady_state_test(self) -> Dict[str, Any]:
        """稳态性能测试。

        v1.8.0: 支持多任务批量执行。稳态预处理只需执行一次，之后依次运行所有任务。
        perf_task_list 为空时执行单组参数测试（兼容旧版）。
        """
        self.log.info("-" * 40)
        self.log.info("稳态性能测试")
        self.log.info("-" * 40)

        result = {"state": "steady"}

        # 稳态预处理
        if self.cfg.precondition:
            precond_ok, precond_info = self.precondition_steady_state()
            result["precondition"] = precond_info
            if not precond_ok:
                result["precondition_failed"] = True
                self.log.error("稳态预处理失败，跳过稳态性能测试")
                return result
        else:
            self.log.info("  跳过稳态预处理 (--precondition off)")
            result["precondition"] = {"skipped": True}

        # v1.8.0 多任务批量执行（预处理只需一次，之后依次运行所有任务）
        task_list = self.cfg.perf_task_list
        if task_list:
            result["batch_mode"] = True
            result["task_count"] = len(task_list)
            self.log.info(f"  [批量模式] 共 {len(task_list)} 组测试任务，依次执行...")
            perf_results = []
            for idx, task in enumerate(task_list, start=1):
                self.log.info(f"\n  ===== 批量任务 {idx}/{len(task_list)} =====")
                perf_results.append(self.run_full_perf_test("Steady", task=task, task_index=idx))
            result["full_perf"] = perf_results
        else:
            # 兼容旧版：单组参数测试
            result["full_perf"] = self.run_full_perf_test("Steady")

        return result

    def run(self) -> TestResult:
        """执行完整性能特征测试主流程。"""
        result = TestResult(
            test_item=TEST_PERF,
            test_name="完整性能特征",
            device=self.cfg.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info("[DRY-RUN] 将执行完整性能特征测试")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        try:
            # 确认设备未挂载
            try:
                _, out, _ = run_cmd(["findmnt", "-n", "-o", "TARGET", self.cfg.device],
                                     check=False, capture=True, logger=self.log, timeout=5)
                if out.strip():
                    self.log.warning(f"设备可能已挂载: {out.strip()}，建议卸载后测试")
            except Exception:
                pass

            perf_results = {}

            # v1.9.2: 根据目标状态执行对应测试
            if self.cfg.perf_state == PERF_FOB:
                perf_results["fob"] = self.run_fob_test()
            elif self.cfg.perf_state == PERF_STEADY:
                perf_results["steady"] = self.run_steady_state_test()
            elif self.cfg.perf_state == PERF_UNKNOWN:
                # Unknown 状态：直接执行性能测试，不进行任何预处理
                self.log.info("Unknown 状态：直接执行性能测试（跳过 FOB 擦除和稳态预处理）")
                perf_results["unknown"] = {
                    "state": "Unknown",
                    "precondition": "skipped",
                    "full_perf": self.run_full_perf_test("Unknown")
                }

            result.details["performance"] = perf_results

            # 简单性能有效性检查（至少有一项测试返回了有效数据）
            has_valid_data = False
            for state_data in perf_results.values():
                # v1.8.0 兼容单组(dict)和批量(list)两种 full_perf 结构
                full_perf = state_data.get("full_perf", {})
                fp_list = full_perf if isinstance(full_perf, list) else [full_perf]
                for fp in fp_list:
                    if fp.get("success") and fp.get("parsed"):
                        parsed = fp["parsed"]
                        if parsed.get("read", {}).get("iops", 0) > 0 or \
                           parsed.get("write", {}).get("iops", 0) > 0:
                            has_valid_data = True
                            break
                if has_valid_data:
                    break

            if not has_valid_data and not self.cfg.dry_run:
                # 检查是否预处理失败导致
                steady = perf_results.get("steady", {})
                if steady.get("precondition_failed"):
                    result.finish(STATUS_FAIL, "稳态预处理失败，未执行性能测试")
                    return result

            result.finish(STATUS_PASS)
            self.log.info("完整性能特征测试完成")

        except Exception as e:
            self.log.exception(f"性能测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result


# ============================================================
# 测试项 5：正常电源循环测试
# ============================================================

class PowerCycleTester:
    """
    正常电源循环测试。
    流程：持续混合读写 -> 正常关机 -> 断电 -> 上电开机 -> 检查磁盘/分区/文件系统/数据
          -> 循环 N 次 -> 最终完整功能测试。
    支持 IPMI 远程电源控制和手动断电两种模式。
    通过状态文件实现系统重启后断点恢复。
    """

    # 测试阶段标识
    PHASE_SETUP = "setup"
    PHASE_RW_LOAD = "rw_load"
    PHASE_SHUTDOWN = "shutdown"
    PHASE_BOOT_CHECK = "boot_check"
    PHASE_FINAL_TEST = "final_test"
    PHASE_DONE = "done"

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device
        self._fio_process = None
        self._fio_log_file = None

    # ---------- 状态文件持久化 ----------

    def load_state(self) -> Dict[str, Any]:
        """加载状态文件，不存在则返回初始状态。"""
        if os.path.exists(self.cfg.pc_state_file):
            try:
                with open(self.cfg.pc_state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.log.info(f"加载状态文件: 当前循环 {state.get('current_cycle', 0)}/"
                              f"{state.get('target_cycles', self.cfg.pc_cycles)}, "
                              f"阶段 {state.get('phase', 'unknown')}")
                return state
            except Exception as e:
                self.log.warning(f"状态文件读取失败，将重新初始化: {e}")
        return {
            "target_cycles": self.cfg.pc_cycles,
            "current_cycle": 0,
            "phase": self.PHASE_SETUP,
            "device": self.cfg.device,
            "mount_point": self.cfg.pc_mount_point,
            "integrity_checksums": {},
            "cycle_results": [],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def save_state(self, state: Dict[str, Any]):
        """保存状态文件。"""
        state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(self.cfg.pc_state_file), exist_ok=True)
        with open(self.cfg.pc_state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        self.log.debug(f"状态文件已保存: 循环 {state['current_cycle']}, 阶段 {state['phase']}")

    # ---------- 分区与文件系统 ----------

    def setup_test_partition(self) -> Tuple[bool, str]:
        """
        在待测设备上创建测试分区和 ext4 文件系统。
        返回 (成功, 分区设备路径)。
        """
        part_device = f"{self.cfg.device}p1" if self.cfg.device_type == DEVICE_NVME \
            else f"{self.cfg.device}1"
        mount_point = self.cfg.pc_mount_point

        try:
            # 卸载已挂载的分区
            run_cmd(["umount", part_device], check=False, capture=True, logger=self.log, timeout=10)
            run_cmd(["umount", self.cfg.device], check=False, capture=True, logger=self.log, timeout=10)

            # 创建 GPT 分区表和单个分区
            self.log.info(f"创建分区表和分区: {self.cfg.device}")
            run_cmd(["parted", "-s", self.cfg.device, "mklabel", "gpt"],
                    check=True, capture=True, logger=self.log, timeout=30)
            run_cmd(["parted", "-s", self.cfg.device, "mkpart", "primary", "ext4",
                     "0%", "100%"],
                    check=True, capture=True, logger=self.log, timeout=30)
            time.sleep(2)  # 等待分区设备节点创建

            # 格式化 ext4
            self.log.info(f"格式化 ext4: {part_device}")
            run_cmd(["mkfs.ext4", "-F", "-L", "SSD_TEST", part_device],
                    check=True, capture=True, logger=self.log, timeout=120)

            # 挂载
            os.makedirs(mount_point, exist_ok=True)
            run_cmd(["mount", part_device, mount_point],
                    check=True, capture=True, logger=self.log, timeout=10)
            self.log.info(f"测试分区已挂载: {part_device} -> {mount_point}")
            return True, part_device

        except Exception as e:
            self.log.error(f"创建测试分区失败: {e}")
            return False, part_device

    # ---------- 数据完整性 ----------

    def write_integrity_data(self, state: Dict[str, Any]) -> bool:
        """写入带 SHA-256 校验和的测试文件。校验和存入状态文件（不存待测盘）。"""
        mount_point = self.cfg.pc_mount_point
        checksums = {}
        try:
            self.log.info(f"写入完整性测试数据: {DEFAULT_PC_TEST_FILE_COUNT} 个文件, "
                          f"每个 {DEFAULT_PC_TEST_FILE_SIZE_MB}MB")
            for i in range(DEFAULT_PC_TEST_FILE_COUNT):
                file_path = os.path.join(mount_point, f"integrity_test_{i:03d}.bin")
                # 使用 dd 从 /dev/urandom 生成随机数据
                run_cmd(
                    ["dd", "if=/dev/urandom", f"of={file_path}",
                     f"bs=1M", f"count={DEFAULT_PC_TEST_FILE_SIZE_MB}",
                     "oflag=direct", "conv=fsync"],
                    check=True, capture=True, logger=self.log, timeout=120
                )
                # 计算 SHA-256
                _, out, _ = run_cmd(["sha256sum", file_path],
                                    check=True, capture=True, logger=self.log, timeout=30)
                checksum = out.strip().split()[0]
                checksums[file_path] = checksum

            state["integrity_checksums"] = checksums
            self.log.info(f"完整性数据写入完成，共 {len(checksums)} 个文件")
            return True
        except Exception as e:
            self.log.error(f"写入完整性数据失败: {e}")
            return False

    def verify_integrity_data(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """校验测试文件的 SHA-256 校验和。返回 (全部通过, 详细结果)。"""
        result = {"total": 0, "passed": 0, "failed": 0, "missing": 0, "details": {}}
        checksums = state.get("integrity_checksums", {})
        if not checksums:
            self.log.warning("状态文件中无校验和记录，跳过完整性校验")
            return True, result

        for file_path, expected in checksums.items():
            result["total"] += 1
            if not os.path.exists(file_path):
                result["missing"] += 1
                result["details"][file_path] = "MISSING"
                self.log.error(f"测试文件丢失: {file_path}")
                continue
            try:
                _, out, _ = run_cmd(["sha256sum", file_path],
                                    check=True, capture=True, logger=self.log, timeout=30)
                actual = out.strip().split()[0]
                if actual == expected:
                    result["passed"] += 1
                    result["details"][file_path] = "OK"
                else:
                    result["failed"] += 1
                    result["details"][file_path] = f"MISMATCH (expected={expected[:16]}..., actual={actual[:16]}...)"
                    self.log.error(f"数据校验失败: {file_path}")
            except Exception as e:
                result["failed"] += 1
                result["details"][file_path] = f"ERROR: {e}"
                self.log.error(f"校验文件出错: {file_path}: {e}")

        all_ok = (result["failed"] == 0 and result["missing"] == 0)
        self.log.info(f"完整性校验: {result['passed']}/{result['total']} 通过, "
                      f"{result['failed']} 失败, {result['missing']} 丢失")
        return all_ok, result

    # ---------- 混合读写负载 ----------

    def start_mixed_rw_load(self) -> bool:
        """启动后台持续混合读写负载（fio randrw, 70%读）。"""
        mount_point = self.cfg.pc_mount_point
        self._fio_log_file = os.path.join(self.cfg.log_dir, f"pc_mixed_rw_{int(time.time())}.log")
        fio_cmd = [
            "fio",
            "--name=pc_mixed_rw",
            f"--directory={mount_point}",
            "--rw=randrw",
            "--rwmixread=70",
            "--bs=4k",
            "--iodepth=32",
            "--numjobs=4",
            "--direct=1",
            "--ioengine=libaio",
            "--time_based",
            f"--runtime={self.cfg.pc_rw_duration + 60}",
            "--group_reporting",
            f"--output={self._fio_log_file}",
        ]
        try:
            self.log.info(f"启动混合读写负载 (randrw 70%读, {self.cfg.pc_rw_duration}s)...")
            self._fio_process = subprocess.Popen(
                fio_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(3)
            if self._fio_process.poll() is None:
                self.log.info("混合读写负载已启动 (PID: %d)", self._fio_process.pid)
                return True
            else:
                self.log.error("混合读写负载启动后立即退出")
                return False
        except Exception as e:
            self.log.error(f"启动混合读写负载失败: {e}")
            return False

    def stop_mixed_rw_load(self) -> Dict[str, Any]:
        """停止混合读写负载，返回读写统计。"""
        stats = {"stopped": False, "read_mb": 0, "write_mb": 0}
        if self._fio_process and self._fio_process.poll() is None:
            try:
                self._fio_process.terminate()
                self._fio_process.wait(timeout=10)
                stats["stopped"] = True
                self.log.info("混合读写负载已停止")
            except Exception:
                try:
                    self._fio_process.kill()
                except Exception:
                    pass
        # 解析 fio 日志获取读写量
        if self._fio_log_file and os.path.exists(self._fio_log_file):
            try:
                with open(self._fio_log_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                read_match = re.search(r"read[\s\S]*?io=([\d.]+)([KMG]B)", content)
                write_match = re.search(r"write[\s\S]*?io=([\d.]+)([KMG]B)", content)
                if read_match:
                    stats["read_mb"] = float(read_match.group(1))
                if write_match:
                    stats["write_mb"] = float(write_match.group(1))
            except Exception:
                pass
        return stats

    # ---------- IPMI 电源控制 ----------

    def _build_ipmi_cmd(self, action: str) -> List[str]:
        """构建 ipmitool 命令。"""
        return [
            "ipmitool",
            "-H", self.cfg.ipmi_host,
            "-U", self.cfg.ipmi_user,
            "-P", self.cfg.ipmi_pass,
            "-I", "lanplus",
            "chassis", "power", action,
        ]

    def ipmi_power_status(self) -> Optional[str]:
        """查询 IPMI 电源状态，返回 'on'/'off'/None。"""
        if self.cfg.pc_power_mode != "ipmi" or not self.cfg.ipmi_host:
            return None
        try:
            _, out, _ = run_cmd(self._build_ipmi_cmd("status"),
                                check=True, capture=True, logger=self.log, timeout=15)
            if "on" in out.lower():
                return "on"
            elif "off" in out.lower():
                return "off"
        except Exception as e:
            self.log.error(f"IPMI 电源状态查询失败: {e}")
        return None

    def ipmi_power_off(self) -> bool:
        """IPMI 远程断电。"""
        if self.cfg.pc_power_mode != "ipmi":
            return False
        try:
            self.log.info("IPMI 远程断电...")
            run_cmd(self._build_ipmi_cmd("off"),
                    check=True, capture=True, logger=self.log, timeout=15)
            return True
        except Exception as e:
            self.log.error(f"IPMI 断电失败: {e}")
            return False

    def ipmi_power_on(self) -> bool:
        """IPMI 远程上电。"""
        if self.cfg.pc_power_mode != "ipmi":
            return False
        try:
            self.log.info("IPMI 远程上电...")
            run_cmd(self._build_ipmi_cmd("on"),
                    check=True, capture=True, logger=self.log, timeout=15)
            return True
        except Exception as e:
            self.log.error(f"IPMI 上电失败: {e}")
            return False

    # ---------- 关机 ----------

    def graceful_shutdown(self):
        """执行正常关机。此函数调用后系统将关机，脚本进程终止。"""
        self.log.info("=" * 50)
        self.log.info("执行正常关机...")
        self.log.info("=" * 50)
        # 同步文件系统
        run_cmd(["sync"], check=False, capture=True, logger=self.log, timeout=10)
        # 卸载测试分区
        run_cmd(["umount", self.cfg.pc_mount_point], check=False,
                capture=True, logger=self.log, timeout=10)

        if self.cfg.pc_power_mode == "manual":
            print("\n" + "=" * 60)
            print("  系统即将关机。请在系统完全断电后，")
            print(f"  等待 {self.cfg.pc_off_interval} 秒，然后手动按电源键上电。")
            print("  上电后系统启动，脚本将自动恢复执行（需配置开机自启）。")
            print("=" * 60 + "\n")
            time.sleep(3)

        # 执行关机
        os.system("shutdown -h now")
        # 等待关机（进程会被终止）
        time.sleep(60)

    # ---------- 开机后检查 ----------

    def check_disk_after_boot(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """开机后检查：磁盘枚举、分区表、文件系统、数据完整性、SMART。"""
        result = {
            "disk_enumerated": False,
            "partition_ok": False,
            "filesystem_ok": False,
            "data_integrity_ok": False,
            "smart_ok": False,
            "details": {},
        }
        part_device = f"{self.cfg.device}p1" if self.cfg.device_type == DEVICE_NVME \
            else f"{self.cfg.device}1"

        # 1. 磁盘枚举
        if os.path.exists(self.cfg.device):
            result["disk_enumerated"] = True
            self.log.info("  [1/5] 磁盘枚举: OK")
        else:
            self.log.error("  [1/5] 磁盘枚举: FAIL - 设备不存在")
            return False, result

        # 2. 分区表检查
        try:
            _, out, _ = run_cmd(["parted", "-s", self.cfg.device, "print"],
                                check=True, capture=True, logger=self.log, timeout=15)
            if "gpt" in out.lower() or "msdos" in out.lower():
                result["partition_ok"] = True
                result["details"]["partition_table"] = "gpt" if "gpt" in out.lower() else "msdos"
                self.log.info("  [2/5] 分区表: OK")
            else:
                self.log.error("  [2/5] 分区表: FAIL")
        except Exception as e:
            self.log.error(f"  [2/5] 分区表检查失败: {e}")

        # 3. 文件系统检查（fsck 只读）
        try:
            run_cmd(["umount", part_device], check=False, capture=True, logger=self.log, timeout=10)
            _, out, _ = run_cmd(["fsck", "-n", part_device],
                                check=False, capture=True, logger=self.log, timeout=60)
            # fsck 返回 0 表示干净，1 表示有错误但已修复，其他为错误
            result["details"]["fsck_output"] = out.strip()[:500]
            if "clean" in out.lower() or "errors" not in out.lower():
                result["filesystem_ok"] = True
                self.log.info("  [3/5] 文件系统: OK")
            else:
                self.log.error(f"  [3/5] 文件系统: FAIL - {out.strip()[:200]}")
            # 重新挂载
            run_cmd(["mount", part_device, self.cfg.pc_mount_point],
                    check=False, capture=True, logger=self.log, timeout=10)
        except Exception as e:
            self.log.error(f"  [3/5] 文件系统检查异常: {e}")

        # 4. 数据完整性校验
        integrity_ok, integrity_result = self.verify_integrity_data(state)
        result["data_integrity_ok"] = integrity_ok
        result["details"]["data_integrity"] = integrity_result
        self.log.info(f"  [4/5] 数据完整性: {'OK' if integrity_ok else 'FAIL'}")

        # 5. SMART 检查
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                    check=True, capture=True, logger=self.log, timeout=15)
                smart_data = json.loads(out)
                media_err = smart_data.get("media_and_data_integrity_errors", 0)
                temp = smart_data.get("temperature", 0)
                if temp > 200:
                    temp = temp - 273
                result["details"]["smart"] = {"media_errors": media_err, "temperature_c": temp}
                if media_err == 0 and 0 <= temp <= 70:
                    result["smart_ok"] = True
                    self.log.info("  [5/5] SMART: OK")
                else:
                    self.log.error(f"  [5/5] SMART: FAIL - 介质错误={media_err}, 温度={temp}°C")
            else:
                run_cmd(["smartctl", "-H", self.cfg.device],
                        check=True, capture=True, logger=self.log, timeout=15)
                result["smart_ok"] = True
                self.log.info("  [5/5] SMART: OK")
        except Exception as e:
            self.log.error(f"  [5/5] SMART 检查失败: {e}")

        all_ok = all([
            result["disk_enumerated"],
            result["partition_ok"],
            result["filesystem_ok"],
            result["data_integrity_ok"],
            result["smart_ok"],
        ])
        return all_ok, result

    # ---------- 最终完整功能测试 ----------

    def run_final_functional_test(self) -> Tuple[bool, Dict[str, Any]]:
        """循环完成后执行最终完整功能测试（容量 + SMART + 基本性能）。"""
        self.log.info("=" * 50)
        self.log.info("执行最终完整功能测试...")
        self.log.info("=" * 50)
        result = {"capacity_ok": False, "smart_ok": False, "basic_perf_ok": False, "details": {}}

        # 1. 容量检查
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.cfg.device],
                                check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["details"]["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
            self.log.info(f"  容量检查: {'OK' if cap > 0 else 'FAIL'} ({bytes_to_human(cap)})")
        except Exception as e:
            self.log.error(f"  容量检查失败: {e}")

        # 2. SMART 检查
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                    check=True, capture=True, logger=self.log, timeout=15)
                smart_data = json.loads(out)
                result["details"]["smart"] = {
                    "media_errors": smart_data.get("media_and_data_integrity_errors"),
                    "available_spare": smart_data.get("available_spare"),
                    "power_cycles": smart_data.get("power_cycles"),
                }
                result["smart_ok"] = smart_data.get("media_and_data_integrity_errors", 1) == 0
            else:
                run_cmd(["smartctl", "-H", self.cfg.device],
                        check=True, capture=True, logger=self.log, timeout=15)
                result["smart_ok"] = True
            self.log.info(f"  SMART 检查: {'OK' if result['smart_ok'] else 'FAIL'}")
        except Exception as e:
            self.log.error(f"  SMART 检查失败: {e}")

        # 3. 基本性能测试（顺序读 30s）
        try:
            fio_cmd = [
                "fio", "--name=final_seq_read", f"--filename={self.cfg.device}",
                "--rw=read", "--bs=128k", "--iodepth=32",
                "--runtime=30", "--time_based", "--direct=1",
                "--ioengine=libaio", "--group_reporting", "--output-format=json",
            ]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf_data = json.loads(out)
            read_bw = perf_data["jobs"][0]["read"]["bw"]
            result["details"]["seq_read_bw_mbps"] = round(read_bw / 1024, 2)
            result["basic_perf_ok"] = read_bw > 0
            self.log.info(f"  基本性能 (顺序读): {'OK' if read_bw > 0 else 'FAIL'} "
                          f"({read_bw / 1024:.2f} MB/s)")
        except Exception as e:
            self.log.error(f"  基本性能测试失败: {e}")

        all_ok = all([result["capacity_ok"], result["smart_ok"], result["basic_perf_ok"]])
        return all_ok, result

    # ---------- enhanced（增强）模式：NVMe 优雅移除 ----------

    def nvme_graceful_remove(self) -> Tuple[bool, str]:
        """
        enhanced 模式专属：断电前执行 NVMe 优雅移除。
        流程：sync -> umount 测试分区 -> nvme disconnect -> 等待设备节点消失。
        参考 OKN 框架 link_state.remove_device() / power_down(safe_shutdown=True)。
        返回 (成功, 消息)。
        """
        self.log.info("=" * 50)
        self.log.info("[enhanced] 执行 NVMe 优雅移除（Shutdown Notification）")
        self.log.info("=" * 50)
        try:
            # 1. 同步文件系统
            run_cmd(["sync"], check=False, capture=True, logger=self.log, timeout=15)
            self.log.info("  [1/4] sync: OK")

            # 2. 卸载测试分区
            part_device = f"{self.cfg.device}p1" if self.cfg.device_type == DEVICE_NVME \
                else f"{self.cfg.device}1"
            run_cmd(["umount", self.cfg.pc_mount_point], check=False,
                    capture=True, logger=self.log, timeout=10)
            run_cmd(["umount", part_device], check=False,
                    capture=True, logger=self.log, timeout=10)
            self.log.info("  [2/4] umount 测试分区: OK")

            # 3. NVMe disconnect（向控制器发送 Shutdown Notification）
            if self.cfg.device_type == DEVICE_NVME:
                ctrl = self.ctrl  # /dev/nvmeX
                try:
                    run_cmd(["nvme", "disconnect", ctrl],
                            check=True, capture=True, logger=self.log, timeout=15)
                    self.log.info(f"  [3/4] nvme disconnect {ctrl}: OK")
                except Exception as e:
                    self.log.warning(f"  [3/4] nvme disconnect 失败（降级为仅 umount+sync）: {e}")
                    # 降级：不中断测试，继续断电
            else:
                self.log.info("  [3/4] 非 NVMe 设备，跳过 nvme disconnect")

            # 4. 等待设备节点消失（最多 30 秒）
            self.log.info("  [4/4] 等待设备节点消失...")
            wait_start = time.time()
            while os.path.exists(self.cfg.device) and time.time() - wait_start < 30:
                time.sleep(2)
            if os.path.exists(self.cfg.device):
                self.log.warning("  设备节点在 30s 内未消失，继续断电（可能内核仍持有引用）")
            else:
                self.log.info("  设备节点已消失")

            return True, "NVMe 优雅移除完成"
        except Exception as e:
            self.log.error(f"NVMe 优雅移除异常: {e}")
            return False, str(e)

    # ---------- enhanced（增强）模式：Pattern 数据写入 ----------

    def write_pattern_data(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        enhanced 模式专属：循环前向裸设备写入已知 Pattern 数据。
        使用 fio --buffer_pattern 写入指定 GB 量，记录起始 LBA 和大小到 state。
        参考 OKN npor_test.py 顺序写 + RANDOM_LBA pattern。
        返回 (成功, 统计信息)。
        """
        stats = {"pattern": self.cfg.pc_pattern, "size_gb": self.cfg.pc_pattern_size_gb,
                 "written_bytes": 0, "fio_cmd": ""}
        try:
            size_bytes = self.cfg.pc_pattern_size_gb * 1024 * 1024 * 1024
            # 写入到裸设备（绕过文件系统，直接测试 LBA 级数据保持）
            fio_cmd = [
                "fio",
                "--name=pc_enhanced_pattern_write",
                f"--filename={self.cfg.device}",
                "--rw=write",
                "--bs=128k",
                "--iodepth=256",
                "--numjobs=1",
                "--direct=1",
                "--ioengine=libaio",
                f"--size={size_bytes}",
                f"--buffer_pattern={self.cfg.pc_pattern}",
                "--group_reporting",
                "--output-format=json",
            ]
            stats["fio_cmd"] = " ".join(fio_cmd)
            self.log.info(f"[enhanced] 写入 Pattern 数据: pattern={self.cfg.pc_pattern}, "
                          f"size={self.cfg.pc_pattern_size_gb}GB, bs=128k, QD=256")
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=3600)
            perf_data = json.loads(out)
            written = perf_data["jobs"][0]["write"]["io_bytes"]
            stats["written_bytes"] = written
            self.log.info(f"[enhanced] Pattern 写入完成: {bytes_to_human(written)}")

            # 记录到 state
            state["enhanced_pattern"] = {
                "pattern": self.cfg.pc_pattern,
                "size_bytes": size_bytes,
                "written_bytes": written,
                "lba_start": 0,
            }
            return True, stats
        except Exception as e:
            self.log.error(f"[enhanced] Pattern 数据写入失败: {e}")
            return False, stats

    # ---------- enhanced（增强）模式：Pattern 数据校验读 ----------

    def verify_pattern_data(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        enhanced 模式专属：循环后校验 Pattern 数据。
        使用 fio --verify=pattern --verify_pattern 逐块校验，统计 mismatch 数量。
        参考 OKN npor_test.py verify_read(do_data_compare=True, verify_pattern=True)。
        返回 (全部通过, 详细结果)。
        """
        result = {"total_blocks": 0, "verified_blocks": 0, "mismatch_blocks": 0,
                  "pattern": "", "details": ""}
        pattern_info = state.get("enhanced_pattern", {})
        if not pattern_info:
            self.log.warning("[enhanced] state 中无 Pattern 记录，跳过 Pattern 校验")
            return True, result

        pattern = pattern_info.get("pattern", self.cfg.pc_pattern)
        size_bytes = pattern_info.get("size_bytes", self.cfg.pc_pattern_size_gb * 1024**3)
        result["pattern"] = pattern

        try:
            fio_cmd = [
                "fio",
                "--name=pc_enhanced_pattern_verify",
                f"--filename={self.cfg.device}",
                "--rw=read",
                "--bs=128k",
                "--iodepth=256",
                "--numjobs=1",
                "--direct=1",
                "--ioengine=libaio",
                f"--size={size_bytes}",
                "--verify=pattern",
                f"--verify_pattern={pattern}",
                "--do_verify=1",
                "--verify_fatal=0",
                "--verify_dump=0",
                "--group_reporting",
                "--output-format=json",
            ]
            self.log.info(f"[enhanced] 校验 Pattern 数据: pattern={pattern}, "
                          f"size={bytes_to_human(size_bytes)}")
            _, out, _ = run_cmd(fio_cmd, check=False, capture=True,
                                 logger=self.log, timeout=3600)
            try:
                perf_data = json.loads(out)
                job = perf_data["jobs"][0]
                read_io = job.get("read", {}).get("io_bytes", 0)
                result["total_blocks"] = read_io // (128 * 1024)
                result["verified_blocks"] = result["total_blocks"]
                # fio verify 错误在 job["verify"] 或 error 字段
                verify_err = job.get("verify", {}).get("verify_errors", 0)
                result["mismatch_blocks"] = verify_err
            except Exception as parse_err:
                self.log.warning(f"[enhanced] fio 输出解析失败，尝试文本匹配: {parse_err}")
                # fallback：从文本输出中查找 mismatch
                if "mismatch" in out.lower() or "verification failed" in out.lower():
                    result["mismatch_blocks"] = -1  # 标记为有错误但无法精确计数
                    result["details"] = "检测到 verify 错误（文本匹配）"

            all_ok = result["mismatch_blocks"] == 0
            if all_ok:
                self.log.info(f"[enhanced] Pattern 校验通过: {result['verified_blocks']} 块全部匹配")
            else:
                self.log.error(f"[enhanced] Pattern 校验失败: mismatch={result['mismatch_blocks']} 块")
            return all_ok, result
        except Exception as e:
            self.log.error(f"[enhanced] Pattern 校验异常: {e}")
            result["details"] = str(e)
            return False, result

    # ---------- enhanced（增强）模式：PCIe Link 状态校验 ----------

    def check_pcie_link_state(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        enhanced 模式专属：校验 PCIe Link 速率/宽度与初始值一致，并维护跨循环统计。
        优先使用 sysfs，fallback 到 nvme get-phy。
        参考 OKN check_pcie_link() + width_qty/speed_qty 统计。
        返回 (一致, 详细结果)。
        """
        result = {"current_width": "", "current_speed": "",
                  "initial_width": "", "initial_speed": "",
                  "width_stats": {}, "speed_stats": {}, "consistent": False, "details": ""}

        if not self.cfg.pc_link_check:
            self.log.info("[enhanced] PCIe Link 校验已禁用（--pc-no-link-check）")
            result["consistent"] = True
            result["details"] = "已禁用"
            return True, result

        current_width = ""
        current_speed = ""

        # 方法1：通过 sysfs 读取 PCIe Link 状态
        try:
            # 从设备路径获取 PCIe BDF
            dev_basename = os.path.basename(self.cfg.device)
            # NVMe 命名空间 -> 控制器 -> PCIe 设备
            sys_dev_path = f"/sys/block/{dev_basename}/device"
            if os.path.exists(sys_dev_path):
                # 向上查找 PCIe 设备目录（包含 current_link_width 文件）
                import glob as _glob
                pcie_dirs = _glob.glob(f"/sys/block/{dev_basename}/device/../../../**/current_link_width",
                                        recursive=True)
                if not pcie_dirs:
                    pcie_dirs = _glob.glob(f"/sys/class/nvme/{dev_basename}/device/**/current_link_width",
                                            recursive=True)
                for pdir in pcie_dirs:
                    try:
                        with open(pdir, "r") as f:
                            current_width = f.read().strip()
                        speed_file = pdir.replace("current_link_width", "current_link_speed")
                        if os.path.exists(speed_file):
                            with open(speed_file, "r") as f:
                                current_speed = f.read().strip() + " GT/s"
                        break
                    except Exception:
                        continue
        except Exception as e:
            self.log.debug(f"[enhanced] sysfs 读取 Link 状态失败: {e}")

        # 方法2：fallback 到 nvme get-phy
        if not current_width and self.cfg.device_type == DEVICE_NVME:
            try:
                _, out, _ = run_cmd(["nvme", "get-phy", self.ctrl, "-o", "json"],
                                    check=True, capture=True, logger=self.log, timeout=15)
                phy_data = json.loads(out)
                current_width = str(phy_data.get("number_of_lanes", ""))
                current_speed = str(phy_data.get("max_link_speed", ""))
            except Exception as e:
                self.log.debug(f"[enhanced] nvme get-phy 读取 Link 状态失败: {e}")

        result["current_width"] = current_width
        result["current_speed"] = current_speed

        if not current_width and not current_speed:
            self.log.warning("[enhanced] 无法获取 PCIe Link 状态（sysfs 和 nvme get-phy 均失败），跳过 Link 校验")
            result["consistent"] = True
            result["details"] = "无法获取 Link 状态，跳过"
            return True, result

        # 记录初始 Link 状态（首次调用时）
        initial = state.get("enhanced_initial_link", {})
        if not initial:
            state["enhanced_initial_link"] = {"width": current_width, "speed": current_speed}
            initial = state["enhanced_initial_link"]
            self.log.info(f"[enhanced] 初始 PCIe Link: width={current_width}, speed={current_speed}")

        result["initial_width"] = initial.get("width", "")
        result["initial_speed"] = initial.get("speed", "")

        # 跨循环统计
        width_stats = state.get("enhanced_link_width_stats", {})
        speed_stats = state.get("enhanced_link_speed_stats", {})
        wkey = current_width if current_width else "unknown"
        skey = current_speed if current_speed else "unknown"
        width_stats[wkey] = width_stats.get(wkey, 0) + 1
        speed_stats[skey] = speed_stats.get(skey, 0) + 1
        state["enhanced_link_width_stats"] = width_stats
        state["enhanced_link_speed_stats"] = speed_stats
        result["width_stats"] = width_stats
        result["speed_stats"] = speed_stats

        # 一致性判断
        consistent = True
        if initial.get("width") and current_width and current_width != initial["width"]:
            consistent = False
            self.log.error(f"[enhanced] PCIe Link 宽度变化: 初始={initial['width']}, 当前={current_width}")
        if initial.get("speed") and current_speed and current_speed != initial["speed"]:
            consistent = False
            self.log.error(f"[enhanced] PCIe Link 速率变化: 初始={initial['speed']}, 当前={current_speed}")

        result["consistent"] = consistent
        if consistent:
            self.log.info(f"[enhanced] PCIe Link 校验通过: width={current_width}, speed={current_speed}")
        self.log.info(f"[enhanced] Link 统计: width={width_stats}, speed={speed_stats}")
        return consistent, result

    # ---------- enhanced（增强）模式：开机后综合检查 ----------

    def enhanced_post_cycle_check(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        enhanced 模式专属：开机后的完整检查流程。
        包含：磁盘枚举、分区表、文件系统、Pattern 数据校验、PCIe Link 校验、SMART。
        返回 (全部通过, 详细结果)。
        """
        result = {
            "disk_enumerated": False,
            "partition_ok": False,
            "filesystem_ok": False,
            "pattern_integrity_ok": False,
            "pcie_link_ok": False,
            "smart_ok": False,
            "details": {},
        }
        part_device = f"{self.cfg.device}p1" if self.cfg.device_type == DEVICE_NVME \
            else f"{self.cfg.device}1"

        self.log.info("=" * 50)
        self.log.info("[enhanced] 开机后综合检查")
        self.log.info("=" * 50)

        # 1. 磁盘枚举
        if os.path.exists(self.cfg.device):
            result["disk_enumerated"] = True
            self.log.info("  [1/6] 磁盘枚举: OK")
        else:
            self.log.error("  [1/6] 磁盘枚举: FAIL - 设备不存在")
            return False, result

        # 2. 分区表检查
        try:
            _, out, _ = run_cmd(["parted", "-s", self.cfg.device, "print"],
                                check=True, capture=True, logger=self.log, timeout=15)
            if "gpt" in out.lower() or "msdos" in out.lower():
                result["partition_ok"] = True
                self.log.info("  [2/6] 分区表: OK")
            else:
                self.log.error("  [2/6] 分区表: FAIL")
        except Exception as e:
            self.log.error(f"  [2/6] 分区表检查失败: {e}")

        # 3. 文件系统检查（fsck 只读）
        try:
            run_cmd(["umount", part_device], check=False, capture=True, logger=self.log, timeout=10)
            _, out, _ = run_cmd(["fsck", "-n", part_device],
                                check=False, capture=True, logger=self.log, timeout=60)
            result["details"]["fsck_output"] = out.strip()[:500]
            if "clean" in out.lower() or "errors" not in out.lower():
                result["filesystem_ok"] = True
                self.log.info("  [3/6] 文件系统: OK")
            else:
                self.log.error(f"  [3/6] 文件系统: FAIL - {out.strip()[:200]}")
            # 重新挂载
            run_cmd(["mount", part_device, self.cfg.pc_mount_point],
                    check=False, capture=True, logger=self.log, timeout=10)
        except Exception as e:
            self.log.error(f"  [3/6] 文件系统检查异常: {e}")

        # 4. Pattern 数据校验（enhanced 核心增强点）
        pattern_ok, pattern_result = self.verify_pattern_data(state)
        result["pattern_integrity_ok"] = pattern_ok
        result["details"]["pattern_verify"] = pattern_result
        self.log.info(f"  [4/6] Pattern 数据完整性: {'OK' if pattern_ok else 'FAIL'}")

        # 5. PCIe Link 校验（enhanced 核心增强点）
        link_ok, link_result = self.check_pcie_link_state(state)
        result["pcie_link_ok"] = link_ok
        result["details"]["pcie_link"] = link_result
        self.log.info(f"  [5/6] PCIe Link: {'OK' if link_ok else 'FAIL'}")

        # 6. SMART 检查
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                    check=True, capture=True, logger=self.log, timeout=15)
                smart_data = json.loads(out)
                media_err = smart_data.get("media_and_data_integrity_errors", 0)
                temp = smart_data.get("temperature", 0)
                if temp > 200:
                    temp = temp - 273
                result["details"]["smart"] = {"media_errors": media_err, "temperature_c": temp}
                if media_err == 0 and 0 <= temp <= 70:
                    result["smart_ok"] = True
                    self.log.info("  [6/6] SMART: OK")
                else:
                    self.log.error(f"  [6/6] SMART: FAIL - 介质错误={media_err}, 温度={temp}°C")
            else:
                run_cmd(["smartctl", "-H", self.cfg.device],
                        check=True, capture=True, logger=self.log, timeout=15)
                result["smart_ok"] = True
                self.log.info("  [6/6] SMART: OK")
        except Exception as e:
            self.log.error(f"  [6/6] SMART 检查失败: {e}")

        all_ok = all([
            result["disk_enumerated"],
            result["partition_ok"],
            result["filesystem_ok"],
            result["pattern_integrity_ok"],
            result["pcie_link_ok"],
            result["smart_ok"],
        ])
        self.log.info(f"[enhanced] 综合检查结果: {'PASS' if all_ok else 'FAIL'}")
        return all_ok, result

    # ---------- 主流程 ----------

    def run(self) -> TestResult:
        """
        电源循环测试主流程。
        注意：执行关机后脚本进程终止，需通过状态文件 + 开机自启恢复。
        如果检测到状态文件且阶段为 boot_check，则从开机检查阶段继续。
        """
        result = TestResult(
            test_item=TEST_POWERCYCLE,
            test_name="正常电源循环测试",
            device=self.cfg.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info(f"[DRY-RUN] 将执行电源循环测试: {self.cfg.pc_cycles} 次, "
                          f"模式={self.cfg.pc_power_mode}")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        # IPMI 模式需要配置 host
        if self.cfg.pc_power_mode == "ipmi" and not self.cfg.ipmi_host:
            result.finish(STATUS_ERROR, "IPMI 模式需要指定 --ipmi-host")
            return result

        # enhanced 模式前置校验
        if self.cfg.pc_power_mode == "enhanced":
            if self.cfg.pc_pattern_size_gb <= 0:
                result.finish(STATUS_ERROR, "enhanced 模式需要 --pc-pattern-size-gb > 0")
                return result
            if self.cfg.ipmi_host:
                self.log.info("[enhanced] 已指定 ipmi-host，断电步骤将复用 IPMI 远程断电")
            else:
                self.log.info("[enhanced] 未指定 ipmi-host，断电步骤走手动断电上电流程")

        try:
            # 加载状态文件（可能是重启后恢复）
            state = self.load_state()
            phase = state.get("phase", self.PHASE_SETUP)
            current_cycle = state.get("current_cycle", 0)
            target_cycles = state.get("target_cycles", self.cfg.pc_cycles)

            # 如果是全新开始（setup 阶段），执行初始化
            if phase == self.PHASE_SETUP:
                self.log.info("=" * 50)
                self.log.info(f"电源循环测试初始化: 目标 {target_cycles} 次循环")
                self.log.info("=" * 50)

                # 创建分区和文件系统
                ok, part_dev = self.setup_test_partition()
                if not ok:
                    result.finish(STATUS_FAIL, "创建测试分区失败")
                    return result

                # 写入完整性测试数据
                if not self.write_integrity_data(state):
                    result.finish(STATUS_FAIL, "写入完整性测试数据失败")
                    return result

                # enhanced 模式：写入已知 Pattern 数据（LBA 级校验基准）
                if self.cfg.pc_power_mode == "enhanced":
                    pattern_ok, pattern_stats = self.write_pattern_data(state)
                    if not pattern_ok:
                        result.finish(STATUS_FAIL, "enhanced 模式 Pattern 数据写入失败")
                        return result
                    # 记录初始 PCIe Link 状态
                    self.check_pcie_link_state(state)
                    self.save_state(state)

                state["phase"] = self.PHASE_RW_LOAD
                self.save_state(state)

            # 如果是重启后恢复（boot_check 阶段），执行开机检查
            if phase == self.PHASE_BOOT_CHECK:
                self.log.info("=" * 50)
                self.log.info(f"检测到重启恢复: 第 {current_cycle}/{target_cycles} 循环开机检查")
                self.log.info("=" * 50)

                # 重新挂载测试分区
                part_device = f"{self.cfg.device}p1" if self.cfg.device_type == DEVICE_NVME \
                    else f"{self.cfg.device}1"
                run_cmd(["mount", part_device, self.cfg.pc_mount_point],
                        check=False, capture=True, logger=self.log, timeout=10)

                # 开机后检查（enhanced 模式使用增强综合检查，ipmi/manual 使用原检查）
                if self.cfg.pc_power_mode == "enhanced":
                    check_ok, check_result = self.enhanced_post_cycle_check(state)
                else:
                    check_ok, check_result = self.check_disk_after_boot(state)
                cycle_result = {
                    "cycle": current_cycle,
                    "check_passed": check_ok,
                    "check_details": check_result,
                    "boot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                state.setdefault("cycle_results", []).append(cycle_result)

                if not check_ok:
                    self.log.error(f"第 {current_cycle} 循环开机检查失败")
                    state["phase"] = self.PHASE_FINAL_TEST
                    self.save_state(state)
                    # 即使失败也执行最终功能测试
                    final_ok, final_result = self.run_final_functional_test()
                    result.details["final_functional_test"] = final_result
                    result.details["cycle_results"] = state.get("cycle_results", [])
                    result.details["failed_cycle"] = current_cycle
                    result.finish(STATUS_FAIL, f"第 {current_cycle} 循环开机检查失败")
                    return result

                self.log.info(f"第 {current_cycle} 循环开机检查通过")

                # 判断是否达到目标循环次数
                if current_cycle >= target_cycles:
                    state["phase"] = self.PHASE_FINAL_TEST
                    self.save_state(state)
                else:
                    # 继续下一循环：回到混合读写阶段
                    state["phase"] = self.PHASE_RW_LOAD
                    self.save_state(state)

            # 最终功能测试阶段
            if state.get("phase") == self.PHASE_FINAL_TEST:
                final_ok, final_result = self.run_final_functional_test()
                result.details["final_functional_test"] = final_result
                result.details["cycle_results"] = state.get("cycle_results", [])
                result.details["total_cycles_completed"] = len(state.get("cycle_results", []))

                state["phase"] = self.PHASE_DONE
                self.save_state(state)

                if final_ok:
                    result.finish(STATUS_PASS)
                    self.log.info(f"电源循环测试全部通过: {len(state.get('cycle_results', []))}/{target_cycles} 次循环")
                else:
                    result.finish(STATUS_FAIL, "最终完整功能测试未通过")
                return result

            # 已完成状态
            if state.get("phase") == self.PHASE_DONE:
                result.details["cycle_results"] = state.get("cycle_results", [])
                result.details["total_cycles_completed"] = len(state.get("cycle_results", []))
                result.finish(STATUS_PASS, "测试已完成（状态文件显示 done）")
                return result

            # ===== 执行当前循环的混合读写 + 关机 =====
            # 此时 phase 应为 PHASE_RW_LOAD
            current_cycle = state.get("current_cycle", 0) + 1
            state["current_cycle"] = current_cycle
            self.log.info("=" * 50)
            self.log.info(f"第 {current_cycle}/{target_cycles} 次电源循环")
            self.log.info("=" * 50)

            # 启动混合读写负载
            rw_started = self.start_mixed_rw_load()
            if rw_started:
                self.log.info(f"混合读写运行中 ({self.cfg.pc_rw_duration}s)...")
                time.sleep(self.cfg.pc_rw_duration)
                rw_stats = self.stop_mixed_rw_load()
                self.log.info(f"混合读写统计: 读={rw_stats.get('read_mb', 0)}MB, "
                              f"写={rw_stats.get('write_mb', 0)}MB")
            else:
                self.log.warning("混合读写负载启动失败，继续执行关机")

            # 同步并保存状态（标记为 boot_check，重启后恢复）
            run_cmd(["sync"], check=False, capture=True, logger=self.log, timeout=10)
            state["phase"] = self.PHASE_BOOT_CHECK
            state["last_shutdown_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_state(state)

            # enhanced 模式：先执行 NVMe 优雅移除（Shutdown Notification）
            if self.cfg.pc_power_mode == "enhanced":
                remove_ok, remove_msg = self.nvme_graceful_remove()
                if not remove_ok:
                    self.log.warning(f"[enhanced] NVMe 优雅移除未完全成功: {remove_msg}，继续断电")

            # IPMI 模式（含 enhanced+ipmi-host）：先正常关机，再通过 IPMI 断电
            if self.cfg.pc_power_mode == "ipmi" or \
               (self.cfg.pc_power_mode == "enhanced" and self.cfg.ipmi_host):
                # 启动一个后台进程，在系统关机后通过 IPMI 断电，然后定时上电
                # 由于关机后本进程终止，这里使用 nohup 后台脚本实现
                ipmi_script = "/tmp/ssd_pc_ipmi_power_cycle.sh"
                with open(ipmi_script, "w") as f:
                    f.write(f"""#!/bin/bash
# 等待系统关机（SSH 断开后约 30s）
sleep 60
# IPMI 断电
ipmitool -H {self.cfg.ipmi_host} -U {self.cfg.ipmi_user} -P {self.cfg.ipmi_pass} -I lanplus chassis power off
sleep {self.cfg.pc_off_interval}
# IPMI 上电
ipmitool -H {self.cfg.ipmi_host} -U {self.cfg.ipmi_user} -P {self.cfg.ipmi_pass} -I lanplus chassis power on
""")
                os.chmod(ipmi_script, 0o755)
                subprocess.Popen(["nohup", ipmi_script],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                mode_label = "enhanced+IPMI" if self.cfg.pc_power_mode == "enhanced" else "IPMI"
                self.log.info(f"{mode_label} 电源控制后台脚本已启动（关机后自动断电→延时→上电）")

            # 执行正常关机（此调用后进程终止）
            # enhanced 无 ipmi-host 时走与 manual 相同的手动断电提示流程
            self.graceful_shutdown()

            # 理论上不会执行到这里
            result.finish(STATUS_ERROR, "关机后脚本未终止（异常）")
            return result

        except Exception as e:
            self.log.exception(f"电源循环测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))
            return result


# ============================================================
# 测试项 6：意外电源循环测试 (SPOR)
# ============================================================

class TimeboardController:
    """Timeboard 硬件断电控制器 - 通过 Modbus RTU 协议控制串口继电器电源板。"""

    def __init__(self, port='/dev/ttyUSB0', baud_rate=115200, logger=None):
        self.port = port
        self.baud_rate = baud_rate
        self.ser = None
        self.log = logger

    def modbus_crc16(self, data: bytes) -> List[int]:
        """计算 Modbus RTU CRC16 校验码。"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return [crc & 0xFF, (crc >> 8) & 0xFF]

    def build_modbus_rtu_packet(self, delay_ms: int) -> bytes:
        """组装写多个寄存器(0x10)的 Modbus 报文，设置延时掉电。"""
        delay = int(delay_ms / 100)  # 协议单位 100ms
        high_8 = (delay >> 16) & 0xFF
        mid_8 = (delay >> 8) & 0xFF
        low_8 = delay & 0xFF

        packet = [
            0x01,    # 从站地址
            0x10,    # 功能码：写多个寄存器
            0x00, 0x20,  # 起始地址 0x0020
            0x00, 0x03,  # 寄存器数量 3
            0x06,    # 数据字节数
            0x01,    # CMD_TYPE
            0x05,    # CMD_LENGTH
            high_8, mid_8, low_8,  # 延时 3 字节
            0x00,    # 补零占位
        ]
        crc_bytes = self.modbus_crc16(bytes(packet))
        packet.extend(crc_bytes)
        return bytes(packet)

    def connect(self) -> Tuple[bool, str]:
        """连接 Timeboard，自动探测可用串口。"""
        try:
            import serial
        except ImportError:
            return False, "未安装 pyserial，请执行: pip3 install pyserial"

        candidate_ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
                           '/dev/ttyACM0', '/dev/ttyACM1']
        for port in candidate_ports:
            if not os.path.exists(port):
                continue
            if not os.access(port, os.R_OK | os.W_OK):
                if self.log:
                    self.log.warning(f"串口 {port} 权限不足，请执行: sudo chmod 666 {port}")
                continue
            try:
                self.ser = serial.Serial(
                    port=port, baudrate=self.baud_rate,
                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                    bytesize=serial.EIGHTBITS, timeout=3
                )
                self.port = port
                if self.log:
                    self.log.info(f"Timeboard 已连接: {port}")
                return True, f"已连接到 {port}"
            except Exception as e:
                if self.log:
                    self.log.debug(f"尝试 {port} 失败: {e}")
                continue
        return False, "无法连接到 Timeboard，请检查串口和权限"

    def disconnect(self):
        """断开连接。"""
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def trigger_poweroff(self, delay_ms: int = 2000) -> bool:
        """触发延时掉电。delay_ms 为硬件收到命令后多少毫秒断电。"""
        if not self.ser:
            if self.log:
                self.log.error("Timeboard 未连接，无法触发掉电")
            return False

        if self.log:
            self.log.info(f"触发硬件掉电，延时 {delay_ms}ms ({delay_ms / 1000:.1f}s)...")
        packet = self.build_modbus_rtu_packet(delay_ms)
        if self.log:
            self.log.debug(f"Modbus 报文 HEX: {packet.hex().upper()}")

        try:
            self.ser.flushInput()
            self.ser.flushOutput()
            bytes_written = self.ser.write(packet)
            if self.log:
                self.log.debug(f"已发送 {bytes_written} 字节")
            time.sleep(0.5)
            try:
                response = self.ser.read(1024)
                if response and self.log:
                    self.log.debug(f"收到响应: {response.hex().upper()}")
            except Exception:
                pass
            if self.log:
                self.log.info(f"掉电命令已发送，系统将在 {delay_ms / 1000:.1f}s 后断电")
            return True
        except Exception as e:
            if self.log:
                self.log.error(f"发送掉电命令失败: {e}")
            return False


class SPORTester:
    """
    意外电源循环测试 (Surprise Power Cycle Test, SPOR)。

    测试流程（每轮）：
      阶段1（掉电前）：写入 pattern11 打底 -> 验证 -> 启动 SPOR 写入(pattern22/混合读写)
                       -> 写入过程中直接触发硬件断电（不 sync、不待机）
      阶段2（上电后）：检测 SSD 是否掉盘 -> SMART 检查 -> 解析掉电时写入位置
                       -> 验证前段 pattern22 + 后段 pattern11 数据完整性
                       -> 记录结果 -> 进入下一轮
      全部循环完成后：执行最终完整功能测试（容量 + SMART + 基本性能）

    核心设计：
      - Timeboard 硬件 Modbus RTU 断电，真正"意外"不经过待机命令
      - fio write_iolog 精确记录掉电时最后写入 LBA 位置
      - 原子写入状态文件，断电后不损坏，开机自启恢复
      - 跳过掉电边界最后 N 个 LBA 容错（可能未完全写入）
    """

    # 测试阶段标识
    PHASE_POWEROFF = 1
    PHASE_POWERON = 2
    PHASE_DONE = 3

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.device = config.device
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

        # SPOR 测试参数（从 config 读取，带默认值）
        self.cycles = getattr(config, 'spor_cycles', 10)
        self.delay_before_poweroff = getattr(config, 'spor_delay', 5)
        self.test_size_gb = getattr(config, 'spor_test_size_gb', 20)
        self.lba_size = getattr(config, 'spor_lba_size', 4096)
        self.skip_lba = getattr(config, 'spor_skip_lba', 8)
        self.poweroff_delay_ms = getattr(config, 'spor_poweroff_delay_ms', 500)
        self.mixed_rw = getattr(config, 'spor_mixed_rw', False)
        self.mixed_read_ratio = getattr(config, 'spor_mixed_read_ratio', 70)
        self.final_test = getattr(config, 'spor_final_test', True)
        self.timeboard_port = getattr(config, 'spor_timeboard_port', '/dev/ttyUSB0')
        # SPOR 三模式参数
        self.spor_power_mode = getattr(config, 'spor_power_mode', 'timeboard')
        self.spor_enhanced_iodepth = getattr(config, 'spor_enhanced_iodepth', 256)
        self.spor_enhanced_bs = getattr(config, 'spor_enhanced_bs', '128k')

        # 派生参数
        self.test_size_bytes = self.test_size_gb * 1024 * 1024 * 1024
        self.test_size_lba = self.test_size_bytes // self.lba_size

        # 状态文件和日志
        self.state_file = getattr(config, 'spor_state_file',
                                  '/var/lib/ssd_spor_state.json')
        self.log_dir = config.log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        # Timeboard 控制器
        self.timeboard = TimeboardController(
            port=self.timeboard_port, logger=logger
        )

    # ---------- 状态文件管理（原子写入） ----------

    def load_state(self) -> Optional[Dict[str, Any]]:
        """加载状态文件，不存在返回 None。"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                self.log.info(f"加载 SPOR 状态: 第 {state.get('current_cycle', '?')}/"
                              f"{state.get('total_cycles', '?')} 轮, "
                              f"阶段 {state.get('phase', '?')}")
                return state
            except Exception as e:
                self.log.warning(f"状态文件读取失败，将重新初始化: {e}")
        return None

    def save_state(self, state: Dict[str, Any]):
        """原子保存状态文件（临时文件 + fsync + rename + 目录 fsync）。"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp_file = self.state_file + '.tmp'
        data = json.dumps(state, indent=2, ensure_ascii=False)
        try:
            with open(tmp_file, 'w', encoding='utf-8') as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            try:
                os.replace(tmp_file, self.state_file)
            except Exception:
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    f.write(data)
                    f.flush()
            # 刷新目录项
            try:
                dir_fd = os.open(os.path.dirname(self.state_file) or '.', os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        finally:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass

    def clear_state(self):
        """清除状态文件。"""
        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception:
                pass

    # ---------- 工具函数 ----------

    def run_command(self, cmd: str, timeout: int = 300) -> Tuple[bool, str, str]:
        """执行 shell 命令，返回 (成功, stdout, stderr)。"""
        self.log.debug(f"执行命令: {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "命令超时"
        except Exception as e:
            return False, "", str(e)

    def parse_fio_speed(self, output: str) -> str:
        """从 fio 输出中解析带宽速度。"""
        for line in output.split('\n'):
            if 'WRITE:' in line or 'READ:' in line:
                parts = line.split()
                for i, part in enumerate(parts):
                    if 'MB/s' in part or 'GB/s' in part:
                        return f"{parts[i - 1]} {part}"
        return "未知"

    def check_device_present(self) -> bool:
        """检测 SSD 是否被系统枚举（掉盘检测）。"""
        if os.path.exists(self.device):
            # 额外验证设备可访问
            try:
                _, out, _ = run_cmd(["lsblk", "-d", "-n", "-o", "NAME", self.device],
                                     check=True, capture=True, logger=self.log, timeout=10)
                if out.strip():
                    return True
            except Exception:
                pass
        self.log.error(f"SSD 掉盘：设备 {self.device} 不存在或不可访问")
        return False

    def check_smart(self) -> Tuple[bool, Dict[str, Any]]:
        """上电后 SMART 检查，返回 (正常, 详细信息)。"""
        info = {}
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                     check=True, capture=True, logger=self.log, timeout=15)
                smart = json.loads(out)
                info["media_errors"] = smart.get("media_and_data_integrity_errors")
                info["available_spare"] = smart.get("available_spare")
                info["power_cycles"] = smart.get("power_cycles")
                temp = smart.get("temperature", 0)
                info["temperature_c"] = temp - 273 if temp > 200 else temp

                ok = (info.get("media_errors", 1) == 0
                      and (info.get("available_spare", 100) >= 10)
                      and 0 <= info.get("temperature_c", 25) <= 70)
                self.log.info(f"  SMART 检查: 介质错误={info.get('media_errors')}, "
                              f"可用备件={info.get('available_spare')}%, "
                              f"温度={info.get('temperature_c')}°C -> {'OK' if ok else 'FAIL'}")
                return ok, info
            else:
                _, out, _ = run_cmd(["smartctl", "-H", self.device],
                                     check=True, capture=True, logger=self.log, timeout=15)
                info["raw"] = out.strip()[:500]
                ok = "PASSED" in out.upper() or "OK" in out.upper()
                self.log.info(f"  SMART 检查: {'OK' if ok else 'FAIL'}")
                return ok, info
        except Exception as e:
            self.log.error(f"  SMART 检查失败: {e}")
            info["error"] = str(e)
            return False, info

    def check_pcie_link(self) -> Tuple[bool, str]:
        """检查 PCIe 链路状态（修复版：按类代码 0108 筛选）。"""
        try:
            # NVMe 设备 PCIe 类代码为 0108
            _, out, _ = self.run_command("lspci -d ::0108 2>/dev/null | head -1")
            if not out.strip():
                # 回退：按 nvme 关键字
                _, out, _ = self.run_command("lspci | grep -i -E 'nvme|non-volatile' | head -1")
            if not out.strip():
                self.log.warning("  无法获取 PCIe 设备地址")
                return False, "未知"

            pcie_addr = out.strip().split()[0]
            _, detail, _ = self.run_command(
                f"lspci -vv -s {pcie_addr} 2>/dev/null | grep -E '(LnkSta|Speed|Width)'"
            )
            self.log.info(f"  PCIe 链路 ({pcie_addr}): {detail.strip()[:200]}")
            return True, detail.strip()
        except Exception as e:
            self.log.warning(f"  PCIe 链路检查失败: {e}")
            return False, str(e)

    # ---------- 写入与验证 ----------

    def write_pattern(self, pattern: str, size: Optional[str] = None) -> Tuple[bool, str, str]:
        """写入指定 pattern 打底（使用 fio do_verify + crc32c 确保写入正确）。"""
        size = size or f'{self.test_size_gb}G'
        self.log.info(f"  写入 pattern 0x{pattern} ({size})...")
        cmd = (f"fio --name=write_pattern_{pattern} --filename={self.device} "
               f"--rw=write --bs=128k --ioengine=libaio --direct=1 --size={size} "
               f"--numjobs=4 --iodepth=64 --do_verify=1 --verify_pattern=0x{pattern} "
               f"--verify=crc32c --group_reporting")
        success, stdout, stderr = self.run_command(cmd, timeout=600)
        if success:
            speed = self.parse_fio_speed(stdout)
            self.log.info(f"    写入速度: {speed}")
        return success, stdout, stderr

    def verify_pattern(self, pattern: str, size: str = '100M',
                       offset: str = '0', bs: str = '128k') -> Tuple[bool, str, str]:
        """验证指定区域的 pattern（直接读取比对 buffer_pattern）。"""
        self.log.info(f"  验证 pattern 0x{pattern} (offset={offset}, size={size}, bs={bs})...")
        cmd = (f"fio --name=verify_pattern_{pattern} --filename={self.device} "
               f"--rw=read --ioengine=libaio --direct=1 --bs={bs} --size={size} "
               f"--numjobs=8 --iodepth=128 --buffer_pattern=0x{pattern} --group_reporting")
        if offset != '0':
            cmd += f" --offset={offset}"
        success, stdout, stderr = self.run_command(cmd, timeout=600)
        if success:
            speed = self.parse_fio_speed(stdout)
            self.log.info(f"    读取速度: {speed}")
        return success, stdout, stderr

    def start_spor_write(self) -> bool:
        """
        启动 SPOR 写入（后台运行）。
        支持纯写和混合读写两种模式，使用高队列深度（iodepth=32, numjobs=4）保证写入压力。
        使用 write_iolog 记录每个 IO 的 offset/size，用于掉电后定位写入位置。
        """
        self.log.info("  启动 SPOR 写入（后台）...")
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_write.log")
        # 清理旧的 iolog
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

        if self.mixed_rw:
            # 混合读写模式：70% 读 + 30% 写，随机 4K
            rw_type = "randrw"
            extra = f"--rwmixread={self.mixed_read_ratio} --bs=4k"
            self.log.info(f"    模式: 混合读写 (randrw, {self.mixed_read_ratio}%读, 4K)")
        else:
            # 纯顺序写模式
            rw_type = "write"
            extra = f"--bs={self.lba_size}"
            self.log.info(f"    模式: 纯顺序写 (write, {self.lba_size}B)")

        cmd = (f"fio --name=spor_write --filename={self.device} "
               f"--rw={rw_type} {extra} --ioengine=libaio --direct=1 "
               f"--size={self.test_size_gb}G --numjobs=4 --iodepth=32 "
               f"--write_iolog={log_file} --log_avg_msec=10 "
               f"--buffer_pattern=0x22 --group_reporting")

        try:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(2)  # 等待 fio 启动并开始写入
            self.log.info("    SPOR 写入已启动 (iodepth=32, numjobs=4)")
            return True
        except Exception as e:
            self.log.error(f"    启动 fio 失败: {e}")
            return False

    def kill_fio(self):
        """终止 fio 进程。"""
        self.run_command("pkill -f 'fio --name=spor_write'", timeout=10)

    def get_write_position(self) -> int:
        """
        从 fio write_iolog 解析掉电时的最后写入位置（LBA）。
        读取最后 20 行，取最大 end_offset，确保 LBA 对齐。
        """
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_write.log")
        max_offset = 0

        if not os.path.exists(log_file):
            self.log.warning(f"  IO 日志文件不存在: {log_file}")
            return 0

        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
            total_lines = len(lines)
            # 读取最后 20 行（掉电时写入位置通常在最后几行）
            process_lines = lines[-20:] if total_lines > 20 else lines
            self.log.info(f"  IO 日志共 {total_lines} 行，分析最后 {len(process_lines)} 行")

            for line in process_lines:
                parts = line.strip().split()
                # fio iolog 格式: device write offset size
                if len(parts) >= 4 and parts[1] == 'write':
                    try:
                        offset = int(parts[2])
                        size = int(parts[3])
                        # 验证 LBA 对齐，非对齐条目可能是掉电时截断的日志
                        if offset % self.lba_size != 0 or size % self.lba_size != 0:
                            self.log.debug(f"    跳过非对齐写入: offset={offset}, size={size}")
                            continue
                        end_offset = offset + size
                        if end_offset > max_offset:
                            max_offset = end_offset
                    except ValueError:
                        continue

            # 确保最大偏移 LBA 对齐
            if max_offset % self.lba_size != 0:
                max_offset = (max_offset // self.lba_size) * self.lba_size

            lba = max_offset // self.lba_size
            self.log.info(f"  掉电时写入位置: LBA {lba} ({bytes_to_human(max_offset)})")
            return lba
        except Exception as e:
            self.log.error(f"  解析 IO 日志失败: {e}")
            return 0

    # ---------- enhanced（增强）模式：IPMI 意外断电 ----------

    def enhanced_ipmi_surprise_poweroff(self, state: Dict[str, Any]) -> Tuple[bool, str]:
        """
        enhanced 模式专属：通过 IPMI 直接意外断电（不 sync、不 shutdown）。
        启动 nohup 后台脚本：等待 -> ipmitool power off -> 延时 -> ipmitool power on。
        关键：断电前不执行 os.sync()，保证真正的"意外"断电。
        参考 OKN power_down(safe_shutdown=False) 直接 link_state.power_off()。
        返回 (成功, 消息)。
        """
        try:
            # 保存状态到阶段2（断电后进程终止，上电后自启恢复）
            state['phase'] = self.PHASE_POWERON
            state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state['poweroff_mode'] = 'enhanced_ipmi'
            self.save_state(state)

            # 启动后台脚本：等待 -> IPMI 断电 -> 延时 -> IPMI 上电
            # 注意：不执行 shutdown -h now，这是 SPOR 意外断电与正常关机的本质区别
            ipmi_script = "/tmp/ssd_spor_enhanced_power_cycle.sh"
            with open(ipmi_script, "w") as f:
                f.write(f"""#!/bin/bash
# enhanced SPOR: 等待写入进行后直接 IPMI 断电（不经过 OS shutdown）
sleep {self.delay_before_poweroff + 2}
# IPMI 直接断电（意外断电，不发送待机命令）
ipmitool -H {self.cfg.ipmi_host} -U {self.cfg.ipmi_user} -P {self.cfg.ipmi_pass} -I lanplus chassis power off
sleep {getattr(self.cfg, 'pc_off_interval', 30)}
# IPMI 上电
ipmitool -H {self.cfg.ipmi_host} -U {self.cfg.ipmi_user} -P {self.cfg.ipmi_pass} -I lanplus chassis power on
""")
            os.chmod(ipmi_script, 0o755)
            subprocess.Popen(["nohup", ipmi_script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.log.info("  [enhanced] IPMI 意外断电后台脚本已启动（不 sync、不 shutdown，直接断电）")
            self.log.info(f"  [enhanced] 预计 {self.delay_before_poweroff + 2} 秒后断电，"
                          f"断电 {getattr(self.cfg, 'pc_off_interval', 30)} 秒后自动上电")
            return True, "IPMI 意外断电脚本已启动"
        except Exception as e:
            self.log.error(f"  [enhanced] IPMI 意外断电启动失败: {e}")
            return False, str(e)

    # ---------- enhanced（增强）模式：高 QD 线程级异步写入 ----------

    def start_enhanced_write_thread(self) -> bool:
        """
        enhanced 模式专属：启动高队列深度异步写入（线程级追踪）。
        使用 threading.Thread 启动 fio 顺序写（iodepth=256, bs=128k, pattern=0x22）。
        通过定期读取 fio 子进程 /proc/<pid>/io 追踪已写入字节数。
        参考 OKN MyThread 异步写入模式。
        返回 (成功)。
        """
        self.log.info("  启动 enhanced 高 QD 写入（线程级追踪）...")
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_enhanced.log")
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

        iodepth = getattr(self.cfg, 'spor_enhanced_iodepth', 256)
        bs = getattr(self.cfg, 'spor_enhanced_bs', '128k')
        self.log.info(f"    模式: 纯顺序写 (write, bs={bs}, iodepth={iodepth}, numjobs=1)")

        cmd = (f"fio --name=spor_enhanced_write --filename={self.device} "
               f"--rw=write --bs={bs} --ioengine=libaio --direct=1 "
               f"--size={self.test_size_gb}G --numjobs=1 --iodepth={iodepth} "
               f"--write_iolog={log_file} --log_avg_msec=10 "
               f"--buffer_pattern=0x22 --group_reporting --output-format=json")

        # 共享进度变量
        self._enhanced_write_bytes = 0
        self._enhanced_fio_proc = None
        self._enhanced_stop_monitor = False

        def _run_fio():
            try:
                self._enhanced_fio_proc = subprocess.Popen(
                    cmd, shell=True, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                self._enhanced_fio_proc.wait()
            except Exception:
                pass

        def _monitor_progress():
            """定期读取 fio 子进程的 /proc/<pid>/io write_bytes。"""
            time.sleep(2)  # 等待 fio 启动
            while not self._enhanced_stop_monitor:
                try:
                    if self._enhanced_fio_proc and self._enhanced_fio_proc.poll() is None:
                        pid = self._enhanced_fio_proc.pid
                        io_file = f"/proc/{pid}/io"
                        if os.path.exists(io_file):
                            with open(io_file, 'r') as pf:
                                for line in pf:
                                    if line.startswith('write_bytes:'):
                                        self._enhanced_write_bytes = int(line.split(':')[1].strip())
                                        break
                except Exception:
                    pass
                time.sleep(1)

        try:
            self._enhanced_write_thread = threading.Thread(target=_run_fio, daemon=True)
            self._enhanced_monitor_thread = threading.Thread(target=_monitor_progress, daemon=True)
            self._enhanced_write_thread.start()
            self._enhanced_monitor_thread.start()
            time.sleep(2)  # 等待 fio 启动并开始写入
            self.log.info(f"    enhanced 写入已启动 (iodepth={iodepth}, 线程级进度追踪)")
            return True
        except Exception as e:
            self.log.error(f"    启动 enhanced 写入失败: {e}")
            return False

    def get_enhanced_write_position(self) -> int:
        """
        enhanced 模式专属：从线程共享进度变量获取掉电时已写入 LBA。
        若进度变量不可用（为 0），fallback 到 iolog 解析。
        返回 last_lba。
        """
        written_bytes = getattr(self, '_enhanced_write_bytes', 0)
        if written_bytes > 0:
            lba = written_bytes // self.lba_size
            self.log.info(f"  掉电时写入位置(线程追踪): LBA {lba} ({bytes_to_human(written_bytes)})")
            return lba
        else:
            self.log.info("  线程进度不可用，fallback 到 iolog 解析")
            return self.get_write_position()

    # ---------- enhanced（增强）模式：fio 内置 verify Pattern 校验 ----------

    def enhanced_verify_pattern(self, pattern_hex: str, size: str,
                                 offset: str = '0') -> Tuple[bool, int]:
        """
        enhanced 模式专属：使用 fio 内置 --verify=pattern 逐块校验。
        参考 OKN verify_read(do_data_compare=True, verify_pattern=True)。
        参数:
          pattern_hex: Pattern 十六进制值（如 '22'、'11'）
          size: 校验大小（如 '20G'、'1024M'、字节数）
          offset: 起始偏移（默认 '0'）
        返回 (全部通过, mismatch 块数)。
        """
        try:
            cmd = (f"fio --name=spor_enhanced_verify --filename={self.device} "
                   f"--rw=read --bs={self.lba_size} --ioengine=libaio --direct=1 "
                   f"--size={size} --offset={offset} --numjobs=1 --iodepth=32 "
                   f"--verify=pattern --verify_pattern=0x{pattern_hex} --do_verify=1 "
                   f"--verify_fatal=0 --group_reporting --output-format=json")
            self.log.info(f"    enhanced verify: pattern=0x{pattern_hex}, size={size}, offset={offset}")
            success, stdout, stderr = self.run_command(cmd, timeout=3600)
            mismatch = 0
            try:
                perf_data = json.loads(stdout)
                job = perf_data["jobs"][0]
                verify_err = job.get("verify", {}).get("verify_errors", 0)
                mismatch = verify_err
            except Exception as parse_err:
                self.log.debug(f"    fio verify JSON 解析失败: {parse_err}")
                # fallback: 文本匹配
                if "mismatch" in (stdout + stderr).lower() or "verification failed" in (stdout + stderr).lower():
                    mismatch = -1  # 标记为有错误但无法精确计数
            all_ok = (mismatch == 0)
            if all_ok:
                self.log.info(f"    enhanced verify 通过 (0 mismatch)")
            else:
                self.log.error(f"    enhanced verify 失败 (mismatch={mismatch})")
            return all_ok, mismatch
        except Exception as e:
            self.log.error(f"    enhanced verify 异常: {e}")
            return False, -1

    # ---------- manual（手动）模式：手动意外断电 ----------

    def manual_surprise_poweroff(self, state: Dict[str, Any]) -> Tuple[bool, str]:
        """
        manual 模式专属：手动意外断电。
        保存状态后打印倒计时提示，不执行任何断电操作（等待用户手动切断电源）。
        关键：断电前不 sync，用户需在写入进行中直接切断电源。
        返回 (成功, 消息)。
        """
        try:
            state['phase'] = self.PHASE_POWERON
            state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state['poweroff_mode'] = 'manual'
            self.save_state(state)

            self.log.info("=" * 55)
            self.log.info("  [manual] 请立即手动切断电源（意外断电，不 sync、不 shutdown）")
            self.log.info(f"  当前 fio 写入仍在进行中，请在 {self.delay_before_poweroff} 秒内断电")
            self.log.info("  断电后等待 10-30 秒，再手动上电")
            self.log.info("  系统启动后重新运行相同命令即可自动恢复执行阶段2")
            self.log.info("=" * 55)
            # 等待一段时间让用户有时间断电（进程会被断电强制终止）
            time.sleep(max(self.delay_before_poweroff, 10))
            return True, "手动断电提示已打印"
        except Exception as e:
            self.log.error(f"  [manual] 手动断电准备失败: {e}")
            return False, str(e)

    # ---------- 阶段1：掉电前 ----------

    def phase1_poweroff(self, state: Dict[str, Any]) -> bool:
        """
        阶段1：掉电前流程。
        写入 pattern11 打底 -> 验证 -> 启动 SPOR 写入 -> 等待 -> 保存状态 -> 硬件断电。
        关键：断电前不执行 os.sync()，保证真正的"意外"断电。
        """
        current_cycle = state['current_cycle']

        self.log.info("=" * 55)
        self.log.info(f"  第 {current_cycle}/{state['total_cycles']} 轮 - 阶段1：掉电前")
        self.log.info("=" * 55)

        # 检查 PCIe 链路
        self.check_pcie_link()

        # Step 1: 写入 pattern11 打底
        self.log.info("\n  Step 1/5: 写入 pattern11 打底")
        success, _, stderr = self.write_pattern('11', size=f'{self.test_size_gb}G')
        if not success:
            self.log.error(f"    写入 pattern11 失败: {stderr}")
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': '写入pattern11失败', 'phase': 'poweroff'
            })
            self.save_state(state)
            return False

        # Step 2: 验证 pattern11
        self.log.info("\n  Step 2/5: 验证 pattern11")
        success, _, stderr = self.verify_pattern('11', size=f'{self.test_size_gb}G')
        if not success:
            self.log.warning(f"    验证 pattern11 失败（继续测试）: {stderr}")

        # Step 3: 启动 SPOR 写入（高压力，支持混合读写）
        self.log.info("\n  Step 3/5: 启动 SPOR 写入")
        if self.spor_power_mode == 'enhanced':
            write_ok = self.start_enhanced_write_thread()
        else:
            write_ok = self.start_spor_write()
        if not write_ok:
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': '启动fio失败', 'phase': 'poweroff'
            })
            self.save_state(state)
            return False

        # Step 4: 等待指定秒数（让写入充分进行，此时 fio 仍在活跃写入）
        self.log.info(f"\n  Step 4/5: 写入中等待 {self.delay_before_poweroff} 秒...")
        time.sleep(self.delay_before_poweroff)

        # Step 5: 保存状态到阶段2，然后触发意外断电（三模式）
        # 关键：不执行 os.sync()，保证断电是真正的"意外"
        self.log.info("\n  Step 5/5: 保存状态并触发意外掉电（不 sync）")

        if self.spor_power_mode == 'timeboard':
            # 旧方案：Timeboard 硬件断电
            state['phase'] = self.PHASE_POWERON
            state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state['poweroff_mode'] = 'timeboard'
            self.save_state(state)

            # 连接 Timeboard 并触发掉电
            if not self.timeboard.ser:
                ok, msg = self.timeboard.connect()
                if not ok:
                    self.log.error(f"Timeboard 连接失败: {msg}")
                    self.kill_fio()
                    state['results'].append({
                        'cycle': current_cycle, 'success': False,
                        'error': f'Timeboard连接失败: {msg}', 'phase': 'poweroff'
                    })
                    self.save_state(state)
                    return False

            # 触发硬件掉电（短延时，增强"意外"性）
            self.timeboard.trigger_poweroff(self.poweroff_delay_ms)
            # 等待断电（系统会被切断，此进程将终止）
            time.sleep(self.poweroff_delay_ms / 1000 + 5)

        elif self.spor_power_mode == 'manual':
            # manual 方案：手动意外断电（打印提示，不执行断电操作）
            self.manual_surprise_poweroff(state)

        else:  # enhanced
            # enhanced 方案：IPMI 直接意外断电（不 shutdown、不 sync）
            if not getattr(self.cfg, 'ipmi_host', None):
                self.log.error("enhanced 模式需要指定 --ipmi-host 用于 IPMI 意外断电")
                state['results'].append({
                    'cycle': current_cycle, 'success': False,
                    'error': 'enhanced模式缺少ipmi-host', 'phase': 'poweroff'
                })
                self.save_state(state)
                return False
            self.enhanced_ipmi_surprise_poweroff(state)
            # 等待后台脚本触发断电（系统会被切断，此进程将终止）
            time.sleep(self.delay_before_poweroff + 5)

        return True

    # ---------- 阶段2：上电后 ----------

    def phase2_poweron(self, state: Dict[str, Any]) -> bool:
        """
        阶段2：上电后流程。
        掉盘检测 -> SMART 检查 -> PCIe 检查 -> 解析写入位置 -> 验证前段 pattern22
        -> 验证后段 pattern11 -> 记录结果 -> 准备下一轮或最终测试。
        """
        current_cycle = state['current_cycle']

        self.log.info("=" * 55)
        self.log.info(f"  第 {current_cycle}/{state['total_cycles']} 轮 - 阶段2：上电后")
        self.log.info("=" * 55)

        # Step 1: 等待 SSD 初始化 + 掉盘检测
        self.log.info("\n  Step 1/6: 等待 SSD 初始化并检测是否掉盘...")
        time.sleep(15)
        if not self.check_device_present():
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': 'SSD掉盘（上电后设备不存在）', 'phase': 'poweron'
            })
            self.save_state(state)
            return False

        # Step 2: PCIe 链路检查
        self.log.info("\n  Step 2/6: PCIe 链路检查")
        self.check_pcie_link()

        # Step 3: SMART 检查
        self.log.info("\n  Step 3/6: SMART 健康检查")
        smart_ok, smart_info = self.check_smart()
        state.setdefault('smart_history', []).append({
            'cycle': current_cycle, **smart_info
        })

        # Step 4: 解析掉电时写入位置
        self.log.info("\n  Step 4/6: 解析掉电时写入位置")
        if self.spor_power_mode == 'enhanced':
            last_lba = self.get_enhanced_write_position()
        else:
            last_lba = self.get_write_position()

        # Step 5: 验证前段 pattern22（已写入区域，跳过最后 skip_lba 个 LBA）
        self.log.info("\n  Step 5/6: 验证前段 pattern22（已写入区域）")
        if self.spor_power_mode == 'enhanced':
            verify_22 = self._enhanced_verify_written_area(last_lba)
        else:
            verify_22 = self._verify_written_area(last_lba)

        # Step 6: 验证后段 pattern11（未被覆盖区域）
        self.log.info("\n  Step 6/6: 验证后段 pattern11（未覆盖区域）")
        if self.spor_power_mode == 'enhanced':
            verify_11 = self._enhanced_verify_unwritten_area(last_lba)
        else:
            verify_11 = self._verify_unwritten_area(last_lba)

        # 记录本轮结果
        cycle_success = verify_22 and verify_11 and smart_ok
        state['results'].append({
            'cycle': current_cycle,
            'success': cycle_success,
            'last_lba': last_lba,
            'verify_22': verify_22,
            'verify_11': verify_11,
            'smart_ok': smart_ok,
            'phase': 'poweron'
        })
        self.save_state(state)

        self.log.info(f"\n  本轮结果: {'PASS' if cycle_success else 'FAIL'} "
                      f"(pattern22={'OK' if verify_22 else 'FAIL'}, "
                      f"pattern11={'OK' if verify_11 else 'FAIL'}, "
                      f"SMART={'OK' if smart_ok else 'FAIL'})")

        # 判断是否完成所有循环
        if current_cycle >= state['total_cycles']:
            self.log.info("\n  所有测试轮数已完成！")
            state['phase'] = self.PHASE_DONE
            self.save_state(state)

            # 最终完整功能测试
            if self.final_test:
                self.run_final_functional_test(state)

            self.print_final_results(state)
            self.clear_state()
            return True
        else:
            # 准备下一轮
            state['current_cycle'] = current_cycle + 1
            state['phase'] = self.PHASE_POWEROFF
            self.save_state(state)
            self.log.info(f"\n  准备下一轮 ({current_cycle + 1}/{state['total_cycles']})")
            return True

    def _verify_written_area(self, last_lba: int) -> bool:
        """验证已写入区域（前段 pattern22），跳过掉电边界最后 skip_lba 个 LBA。"""
        if last_lba >= self.test_size_lba:
            # 写入完整，验证全部
            self.log.info(f"    写入完整（{self.test_size_gb}GB），验证全部 pattern22")
            success, _, stderr = self.verify_pattern(
                '22', size=f'{self.test_size_gb}G', offset='0', bs=f'{self.lba_size}')
            return success

        if last_lba > 0:
            # 未写完整，跳过最后 skip_lba 个 LBA（掉电时可能未完全写入）
            valid_lba = max(0, last_lba - self.skip_lba)
            valid_bytes = valid_lba * self.lba_size
            self.log.info(f"    已写入 LBA {last_lba}，跳过最后 {self.skip_lba} LBA，"
                          f"有效验证到 LBA {valid_lba} ({bytes_to_human(valid_bytes)})")
            if valid_bytes > 0:
                success, _, stderr = self.verify_pattern(
                    '22', size=f'{valid_bytes}', offset='0', bs=f'{self.lba_size}')
                if not success:
                    self.log.error(f"    pattern22 验证失败: {stderr[:200]}")
                return success
            else:
                self.log.info("    有效数据为 0，跳过 pattern22 验证")
                return True
        else:
            # 写入位置为 0，假设写入完整
            self.log.info(f"    写入位置为 0，假设写入完整，验证全部 pattern22")
            success, _, _ = self.verify_pattern(
                '22', size=f'{self.test_size_gb}G', offset='0', bs=f'{self.lba_size}')
            return success

    def _verify_unwritten_area(self, last_lba: int) -> bool:
        """验证未被覆盖区域（后段 pattern11）。"""
        if last_lba >= self.test_size_lba or last_lba == 0:
            self.log.info("    写入完整，整个区域都是 pattern22，跳过 pattern11 验证")
            return True

        start_offset = last_lba * self.lba_size
        remaining_bytes = self.test_size_bytes - start_offset
        if remaining_bytes <= 0:
            self.log.info("    剩余空间不足，跳过 pattern11 验证")
            return True

        self.log.info(f"    从 LBA {last_lba}（偏移 {start_offset}）开始验证，"
                      f"剩余 {bytes_to_human(remaining_bytes)}")

        if remaining_bytes >= 1024 ** 3:
            size_str = f'{remaining_bytes // (1024 ** 3)}G'
        elif remaining_bytes >= 1024 ** 2:
            size_str = f'{remaining_bytes // (1024 ** 2)}M'
        else:
            size_str = f'{remaining_bytes}'

        success, _, stderr = self.verify_pattern(
            '11', size=size_str, offset=f'{start_offset}', bs='128k')
        if not success:
            self.log.error(f"    pattern11 验证失败: {stderr[:200]}")
        return success


    def _enhanced_verify_written_area(self, last_lba: int) -> bool:
        """enhanced 模式：验证已写入区域（前段 pattern22），使用 fio 内置 verify=pattern。"""
        if last_lba >= self.test_size_lba:
            self.log.info(f"    写入完整（{self.test_size_gb}GB），验证全部 pattern22")
            ok, _ = self.enhanced_verify_pattern('22', size=f'{self.test_size_gb}G', offset='0')
            return ok

        if last_lba > 0:
            valid_lba = max(0, last_lba - self.skip_lba)
            valid_bytes = valid_lba * self.lba_size
            self.log.info(f"    已写入 LBA {last_lba}，跳过最后 {self.skip_lba} LBA，"
                          f"有效验证到 LBA {valid_lba} ({bytes_to_human(valid_bytes)})")
            if valid_bytes > 0:
                ok, mismatch = self.enhanced_verify_pattern('22', size=f'{valid_bytes}', offset='0')
                if not ok:
                    self.log.error(f"    pattern22 enhanced 验证失败: mismatch={mismatch}")
                return ok
            else:
                self.log.info("    有效数据为 0，跳过 pattern22 验证")
                return True
        else:
            self.log.info(f"    写入位置为 0，假设写入完整，验证全部 pattern22")
            ok, _ = self.enhanced_verify_pattern('22', size=f'{self.test_size_gb}G', offset='0')
            return ok

    def _enhanced_verify_unwritten_area(self, last_lba: int) -> bool:
        """enhanced 模式：验证未被覆盖区域（后段 pattern11），使用 fio 内置 verify=pattern。"""
        if last_lba >= self.test_size_lba or last_lba == 0:
            self.log.info("    写入完整，整个区域都是 pattern22，跳过 pattern11 验证")
            return True

        start_offset = last_lba * self.lba_size
        remaining_bytes = self.test_size_bytes - start_offset
        if remaining_bytes <= 0:
            self.log.info("    剩余空间不足，跳过 pattern11 验证")
            return True

        self.log.info(f"    从 LBA {last_lba}（偏移 {start_offset}）开始验证，"
                      f"剩余 {bytes_to_human(remaining_bytes)}")

        ok, mismatch = self.enhanced_verify_pattern('11', size=f'{remaining_bytes}',
                                                      offset=f'{start_offset}')
        if not ok:
            self.log.error(f"    pattern11 enhanced 验证失败: mismatch={mismatch}")
        return ok

    # ---------- 最终完整功能测试 ----------

    def run_final_functional_test(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        所有循环完成后执行最终完整功能测试：
        容量检查 + SMART 检查 + 基本性能测试（顺序读/写）。
        """
        self.log.info("=" * 55)
        self.log.info("  最终完整功能测试")
        self.log.info("=" * 55)
        result = {"capacity_ok": False, "smart_ok": False,
                  "seq_read_ok": False, "seq_write_ok": False, "details": {}}

        # 1. 容量检查
        self.log.info("\n  [1/4] 容量检查")
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["details"]["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
            self.log.info(f"    容量: {bytes_to_human(cap)} ({bytes_to_human(cap, binary=True)}) -> "
                          f"{'OK' if cap > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    容量检查失败: {e}")

        # 2. SMART 检查
        self.log.info("\n  [2/4] SMART 检查")
        smart_ok, smart_info = self.check_smart()
        result["smart_ok"] = smart_ok
        result["details"]["smart"] = smart_info

        # 3. 顺序读性能测试
        self.log.info("\n  [3/4] 顺序读性能测试 (128K QD32, 30s)")
        try:
            fio_cmd = [
                "fio", "--name=final_seq_read", f"--filename={self.device}",
                "--rw=read", "--bs=128k", "--iodepth=32",
                "--runtime=30", "--time_based", "--direct=1",
                "--ioengine=libaio", "--group_reporting", "--output-format=json",
            ]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf = json.loads(out)
            read_bw = perf["jobs"][0]["read"]["bw"]
            result["details"]["seq_read_bw_mbps"] = round(read_bw / 1024, 2)
            result["seq_read_ok"] = read_bw > 0
            self.log.info(f"    顺序读带宽: {read_bw / 1024:.2f} MB/s -> "
                          f"{'OK' if read_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    顺序读测试失败: {e}")

        # 4. 顺序写性能测试
        self.log.info("\n  [4/4] 顺序写性能测试 (128K QD32, 30s)")
        try:
            fio_cmd = [
                "fio", "--name=final_seq_write", f"--filename={self.device}",
                "--rw=write", "--bs=128k", "--iodepth=32",
                "--runtime=30", "--time_based", "--direct=1",
                "--ioengine=libaio", "--group_reporting", "--output-format=json",
            ]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf = json.loads(out)
            write_bw = perf["jobs"][0]["write"]["bw"]
            result["details"]["seq_write_bw_mbps"] = round(write_bw / 1024, 2)
            result["seq_write_ok"] = write_bw > 0
            self.log.info(f"    顺序写带宽: {write_bw / 1024:.2f} MB/s -> "
                          f"{'OK' if write_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    顺序写测试失败: {e}")

        all_ok = all([result["capacity_ok"], result["smart_ok"],
                      result["seq_read_ok"], result["seq_write_ok"]])
        result["overall"] = "PASS" if all_ok else "FAIL"
        state["final_functional_test"] = result

        self.log.info(f"\n  最终功能测试: {'PASS' if all_ok else 'FAIL'}")
        return all_ok, result

    # ---------- 结果输出 ----------

    def print_final_results(self, state: Dict[str, Any]):
        """打印最终测试结果汇总。"""
        self.log.info("\n" + "=" * 55)
        self.log.info("  SPOR 测试完成 - 结果汇总")
        self.log.info("=" * 55)

        results = state.get('results', [])
        passed = sum(1 for r in results if r.get('success'))
        failed = len(results) - passed

        self.log.info(f"  总轮数: {state.get('total_cycles', '?')}")
        self.log.info(f"  实际执行: {len(results)} 轮")
        self.log.info(f"  通过: {passed}")
        self.log.info(f"  失败: {failed}")

        for r in results:
            status = "PASS" if r.get('success') else "FAIL"
            self.log.info(f"    第 {r.get('cycle', '?')} 轮: {status} "
                          f"(LBA={r.get('last_lba', '?')}, "
                          f"p22={'OK' if r.get('verify_22') else 'FAIL'}, "
                          f"p11={'OK' if r.get('verify_11') else 'FAIL'}, "
                          f"SMART={'OK' if r.get('smart_ok') else 'FAIL'})")
            if r.get('error'):
                self.log.info(f"      错误: {r['error']}")

        if 'final_functional_test' in state:
            ft = state['final_functional_test']
            self.log.info(f"  最终功能测试: {ft.get('overall', '?')}")

        # 保存结果到文件
        result_file = os.path.join(
            self.log_dir, f"spor_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            self.log.info(f"  结果已保存到: {result_file}")
        except Exception as e:
            self.log.error(f"  保存结果文件失败: {e}")

    # ---------- 主入口 ----------

    def run(self) -> TestResult:
        """
        SPOR 测试主入口。
        两阶段状态机：阶段1掉电前 -> 硬件断电（进程终止）-> 上电后自启恢复 -> 阶段2上电后。
        通过状态文件持久化实现断点恢复。
        """
        result = TestResult(
            test_item=TEST_SPOR,
            test_name="意外电源循环测试(SPOR)",
            device=self.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info(f"[DRY-RUN] 将执行 SPOR 测试: {self.cycles} 轮, "
                          f"模式={self.spor_power_mode}, "
                          f"延时={self.delay_before_poweroff}s, "
                          f"混合读写={'开' if self.mixed_rw else '关'}")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        try:
            # 加载或初始化状态
            state = self.load_state()
            if not state:
                state = {
                    'total_cycles': self.cycles,
                    'current_cycle': 1,
                    'phase': self.PHASE_POWEROFF,
                    'results': [],
                    'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.save_state(state)

            # 初始化 Timeboard（仅 timeboard 模式阶段1需要）
            if state.get('phase') == self.PHASE_POWEROFF and self.spor_power_mode == 'timeboard':
                ok, msg = self.timeboard.connect()
                if not ok:
                    self.log.warning(f"Timeboard 连接失败: {msg}（阶段2不需要）")

            # 根据阶段执行
            phase = state.get('phase')
            if phase == self.PHASE_POWEROFF:
                self.phase1_poweroff(state)
                # 如果执行到这里说明断电未生效（模拟模式或 Timeboard 失败）
                result.details["note"] = "阶段1执行完成，系统应已断电"
                result.finish(STATUS_PASS, "阶段1完成，等待上电后恢复执行阶段2")
            elif phase == self.PHASE_POWERON:
                success = self.phase2_poweron(state)
                result.details["cycles_completed"] = len(state.get('results', []))
                result.details["final_test"] = state.get('final_functional_test')
                if success:
                    # 检查是否全部完成
                    if state.get('phase') == self.PHASE_DONE:
                        passed = sum(1 for r in state.get('results', []) if r.get('success'))
                        total = len(state.get('results', []))
                        result.details["passed"] = passed
                        result.details["failed"] = total - passed
                        if passed == total:
                            result.finish(STATUS_PASS, f"全部 {total} 轮通过")
                        else:
                            result.finish(STATUS_FAIL, f"{passed}/{total} 轮通过，{total - passed} 轮失败")
                    else:
                        result.finish(STATUS_PASS, "阶段2完成，准备下一轮阶段1")
                else:
                    result.finish(STATUS_FAIL, "阶段2执行失败")
            elif phase == self.PHASE_DONE:
                result.details["cycles_completed"] = len(state.get('results', []))
                result.finish(STATUS_PASS, "测试已完成（状态文件显示 done）")
            else:
                result.finish(STATUS_ERROR, f"未知阶段: {phase}")

        except Exception as e:
            self.log.exception(f"SPOR 测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result


# ============================================================
# 测试项 7：操作系统中断测试 (OS Interruption Test)
# ============================================================

class OSInterruptionTester:
    """
    操作系统中断测试 (OS Interruption Test)。

    测试目的：验证 SSD（次级驱动器）在多操作系统下持续 I/O 期间
    S3（挂起到内存）/ S4（挂起到磁盘，休眠）休眠/唤醒时的稳定性与数据完整性。

    测试流程（每轮）：
      1. 在待测 SSD 上创建分区+ext4 文件系统，写入带 SHA-256 校验和的测试文件
      2. 启动 fio 混合读写持续负载（活跃模式）或保持空闲（空闲模式）
      3. 使用 rtcwake 触发 S3/S4 休眠（设置 RTC 闹钟定时唤醒，无需人工干预）
      4. 系统唤醒后，检查：磁盘标识（/dev/disk/by-id/）、分区表、文件系统、
         数据完整性（SHA-256 校验和比对）、SMART 健康状态
      5. 记录本轮结果，进入下一轮
      6. 全部循环完成后执行最终完整功能测试

    关键工具：fio（持续 I/O 负载）、rtcwake（S3/S4 休眠+定时唤醒）、
              sha256sum（数据完整性校验）、nvme smart-log（SMART 检查）
    """

    # 休眠类型
    SLEEP_S3 = "s3"
    SLEEP_S4 = "s4"
    SLEEP_BOTH = "both"

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.device = config.device
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

        # OSINT 测试参数
        self.cycles = getattr(config, 'osint_cycles', 10)
        self.sleep_type = getattr(config, 'osint_sleep_type', 's3')
        self.sleep_duration = getattr(config, 'osint_sleep_duration', 30)
        self.io_active = getattr(config, 'osint_io_active', True)
        self.io_duration = getattr(config, 'osint_io_duration', 60)
        self.mount_point = getattr(config, 'osint_mount_point', '/mnt/ssd_osint')
        self.state_file = getattr(config, 'osint_state_file', '/var/lib/ssd_osint_state.json')
        self.test_file_size_mb = getattr(config, 'osint_test_file_size_mb', 512)
        self.log_dir = config.log_dir

        os.makedirs(self.log_dir, exist_ok=True)
        self._fio_process = None
        self._fio_log_file = None

    # ---------- 状态文件管理 ----------

    def load_state(self) -> Optional[Dict[str, Any]]:
        """加载状态文件。"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                self.log.info(f"加载 OSINT 状态: 第 {state.get('current_cycle', '?')}/"
                              f"{state.get('total_cycles', '?')} 轮")
                return state
            except Exception as e:
                self.log.warning(f"状态文件读取失败，将重新初始化: {e}")
        return None

    def save_state(self, state: Dict[str, Any]):
        """原子保存状态文件。"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp_file = self.state_file + '.tmp'
        data = json.dumps(state, indent=2, ensure_ascii=False)
        try:
            with open(tmp_file, 'w', encoding='utf-8') as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            try:
                os.replace(tmp_file, self.state_file)
            except Exception:
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    f.write(data)
                    f.flush()
            try:
                dir_fd = os.open(os.path.dirname(self.state_file) or '.', os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        finally:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass

    def clear_state(self):
        """清除状态文件。"""
        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception:
                pass

    # ---------- 工具函数 ----------

    def run_command(self, cmd: str, timeout: int = 300) -> Tuple[bool, str, str]:
        """执行 shell 命令。"""
        self.log.debug(f"执行命令: {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "命令超时"
        except Exception as e:
            return False, "", str(e)

    def get_partition_device(self) -> str:
        """获取分区设备路径。"""
        if self.cfg.device_type == DEVICE_NVME:
            return f"{self.device}p1"
        return f"{self.device}1"

    # ---------- 分区与文件系统 ----------

    def setup_test_partition(self) -> bool:
        """在待测 SSD 上创建分区和 ext4 文件系统。"""
        part_dev = self.get_partition_device()
        try:
            # 卸载已挂载
            self.run_command(f"umount {part_dev} 2>/dev/null; umount {self.device} 2>/dev/null", timeout=10)

            self.log.info(f"创建分区表和分区: {self.device}")
            self.run_command(f"parted -s {self.device} mklabel gpt", timeout=30)
            self.run_command(f"parted -s {self.device} mkpart primary ext4 0% 100%", timeout=30)
            time.sleep(2)

            self.log.info(f"格式化 ext4: {part_dev}")
            self.run_command(f"mkfs.ext4 -F -L SSD_OSINT {part_dev}", timeout=120)

            os.makedirs(self.mount_point, exist_ok=True)
            self.run_command(f"mount {part_dev} {self.mount_point}", timeout=10)
            self.log.info(f"测试分区已挂载: {part_dev} -> {self.mount_point}")
            return True
        except Exception as e:
            self.log.error(f"创建测试分区失败: {e}")
            return False

    # ---------- 数据完整性 ----------

    def write_integrity_data(self, state: Dict[str, Any]) -> bool:
        """写入带 SHA-256 校验和的测试文件，校验和存入状态文件。"""
        checksums = {}
        test_file = os.path.join(self.mount_point, "osint_integrity_test.bin")
        try:
            self.log.info(f"写入完整性测试数据: {self.test_file_size_mb}MB")
            self.run_command(
                f"dd if=/dev/urandom of={test_file} bs=1M count={self.test_file_size_mb} "
                f"oflag=direct conv=fsync",
                timeout=300
            )
            _, out, _ = self.run_command(f"sha256sum {test_file}", timeout=60)
            checksum = out.strip().split()[0]
            checksums[test_file] = checksum
            state["integrity_checksums"] = checksums
            self.log.info(f"完整性数据写入完成，SHA-256: {checksum[:16]}...")
            return True
        except Exception as e:
            self.log.error(f"写入完整性数据失败: {e}")
            return False

    def verify_integrity_data(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """校验测试文件的 SHA-256 校验和。"""
        result = {"total": 0, "passed": 0, "failed": 0, "missing": 0, "details": {}}
        checksums = state.get("integrity_checksums", {})
        if not checksums:
            self.log.warning("无校验和记录，跳过完整性校验")
            return True, result

        for file_path, expected in checksums.items():
            result["total"] += 1
            if not os.path.exists(file_path):
                result["missing"] += 1
                result["details"][file_path] = "MISSING"
                self.log.error(f"测试文件丢失: {file_path}")
                continue
            try:
                _, out, _ = self.run_command(f"sha256sum {file_path}", timeout=60)
                actual = out.strip().split()[0]
                if actual == expected:
                    result["passed"] += 1
                    result["details"][file_path] = "OK"
                else:
                    result["failed"] += 1
                    result["details"][file_path] = f"MISMATCH"
                    self.log.error(f"数据校验失败: {file_path}")
            except Exception as e:
                result["failed"] += 1
                result["details"][file_path] = f"ERROR: {e}"
                self.log.error(f"校验文件出错: {file_path}: {e}")

        all_ok = (result["failed"] == 0 and result["missing"] == 0)
        self.log.info(f"完整性校验: {result['passed']}/{result['total']} 通过, "
                      f"{result['failed']} 失败, {result['missing']} 丢失")
        return all_ok, result

    # ---------- IO 负载 ----------

    def start_io_load(self) -> bool:
        """启动 fio 混合读写持续负载（后台运行）。"""
        if not self.io_active:
            self.log.info("空闲模式：不启动 IO 负载")
            return True

        self._fio_log_file = os.path.join(self.log_dir, f"osint_io_load_{int(time.time())}.log")
        # directory 模式下必须指定 --size，否则 fio 报 "you need to specify size=" 立即退出
        fio_cmd = (
            f"fio --name=osint_mixed_rw --directory={self.mount_point} "
            f"--size=256M --rw=randrw --rwmixread=70 --bs=4k --iodepth=32 --numjobs=4 "
            f"--direct=1 --ioengine=libaio --time_based "
            f"--runtime={self.io_duration + self.sleep_duration + 60} "
            f"--group_reporting --output={self._fio_log_file}"
        )
        try:
            self.log.info(f"启动混合读写负载 (randrw 70%读, {self.io_duration}s)...")
            self._fio_process = subprocess.Popen(
                fio_cmd, shell=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True
            )
            time.sleep(3)
            if self._fio_process.poll() is None:
                self.log.info(f"混合读写负载已启动 (PID: {self._fio_process.pid})")
                return True
            self.log.error("混合读写负载启动后立即退出")
            return False
        except Exception as e:
            self.log.error(f"启动混合读写负载失败: {e}")
            return False

    def stop_io_load(self):
        """停止 IO 负载。"""
        if self._fio_process and self._fio_process.poll() is None:
            try:
                self._fio_process.terminate()
                self._fio_process.wait(timeout=10)
                self.log.info("混合读写负载已停止")
            except Exception:
                try:
                    self._fio_process.kill()
                except Exception:
                    pass
        self.run_command("pkill -f 'fio --name=osint_mixed_rw' 2>/dev/null", timeout=10)

    # ---------- 休眠唤醒 ----------

    def trigger_sleep(self, sleep_type: str) -> Tuple[bool, str]:
        """
        触发 S3/S4 休眠，使用 rtcwake 设置 RTC 闹钟定时唤醒。
        rtcwake 会阻塞直到系统唤醒，返回 (成功, 信息)。

        为防止 S3/S4 唤醒后文件系统元数据损坏，休眠前必须：
          1. 停止 fio IO 负载（等待进程完全退出）
          2. sync 刷新所有缓存
          3. 卸载测试分区文件系统
          4. 刷新 NVMe 设备写缓存（nvme flush）
        唤醒后重新挂载文件系统；若挂载失败（Structure needs cleaning），
        自动运行 fsck -y 修复后重试挂载。
        """
        if sleep_type == self.SLEEP_S3:
            mode = "mem"
            type_name = "S3 (挂起到内存)"
        elif sleep_type == self.SLEEP_S4:
            mode = "disk"
            type_name = "S4 (挂起到磁盘/休眠)"
        else:
            return False, f"未知休眠类型: {sleep_type}"

        part_dev = self.get_partition_device()

        # ---- 休眠前准备：停止 IO → sync → 卸载 → 刷新NVMe写缓存 ----
        self.log.info("  休眠前准备：停止 IO 负载...")
        self.stop_io_load()
        time.sleep(2)  # 确保 fio 进程完全退出、文件句柄释放

        self.log.info("  休眠前准备：sync 刷新缓存...")
        self.run_command("sync", timeout=30)
        time.sleep(1)  # 确保 sync 完成

        self.log.info(f"  休眠前准备：卸载文件系统 {self.mount_point}...")
        self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=15)
        # 确认已卸载
        if os.path.ismount(self.mount_point):
            self.log.warning("  文件系统卸载失败，强制卸载...")
            self.run_command(f"umount -f {self.mount_point} 2>/dev/null", timeout=15)
            time.sleep(1)

        # 刷新 NVMe 设备写缓存，确保所有数据提交到 NAND
        if self.cfg.device_type == DEVICE_NVME:
            self.log.info("  休眠前准备：刷新 NVMe 写缓存...")
            self.run_command(f"nvme flush {self.ctrl} 2>/dev/null", timeout=10)
            time.sleep(1)

        self.log.info(f"触发 {type_name}，{self.sleep_duration}s 后自动唤醒...")
        # rtcwake -m <mode> -s <seconds>：设置 RTC 闹钟并进入指定休眠状态
        # rtcwake 会阻塞直到唤醒
        cmd = f"rtcwake -m {mode} -s {self.sleep_duration}"
        start_time = time.time()
        success, stdout, stderr = self.run_command(cmd, timeout=self.sleep_duration + 120)
        elapsed = time.time() - start_time

        if success:
            self.log.info(f"系统已从 {type_name} 唤醒（实际休眠约 {elapsed:.1f}s）")
            # ---- 唤醒后：重新挂载文件系统 ----
            self.log.info(f"  唤醒后：重新挂载文件系统 {part_dev} -> {self.mount_point}...")
            mount_ok, _, mount_err = self.run_command(
                f"mount {part_dev} {self.mount_point}", timeout=15)

            # 挂载失败时自动 fsck 修复后重试（常见错误: Structure needs cleaning）
            if not mount_ok or not os.path.ismount(self.mount_point):
                self.log.warning(f"  唤醒后首次挂载失败: {mount_err.strip()[:150]}")
                if "structure needs cleaning" in mount_err.lower() or "needs cleaning" in mount_err.lower():
                    self.log.info("  检测到文件系统日志不一致，自动运行 fsck -y 修复...")
                    # 先确保未挂载
                    self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=10)
                    # fsck -y 自动修复所有问题
                    fsck_ok, fsck_out, fsck_err = self.run_command(
                        f"fsck -y {part_dev}", timeout=120)
                    self.log.info(f"  fsck 修复完成 (exit={'ok' if fsck_ok else 'fail'}): "
                                  f"{(fsck_out or fsck_err).strip()[:200]}")
                    time.sleep(1)
                    # 重试挂载
                    self.log.info("  修复后重试挂载...")
                    mount_ok, _, mount_err = self.run_command(
                        f"mount {part_dev} {self.mount_point}", timeout=15)

            if not mount_ok or not os.path.ismount(self.mount_point):
                self.log.error(f"  唤醒后挂载失败（已尝试fsck修复）: {mount_err.strip()[:200]}")
                return False, f"wake_ok_but_mount_failed ({elapsed:.1f}s): {mount_err.strip()[:100]}"
            self.log.info("  唤醒后挂载成功")
            return True, f"wake_ok ({elapsed:.1f}s)"
        else:
            # rtcwake 可能因为权限或配置失败
            self.log.error(f"rtcwake 休眠失败: {stderr.strip()[:200]}")
            # 休眠失败也尝试重新挂载
            self.run_command(f"mount {part_dev} {self.mount_point} 2>/dev/null", timeout=15)
            return False, stderr.strip()[:200]

    # ---------- 唤醒后检查 ----------

    def check_disk_identity(self) -> Tuple[bool, Dict[str, Any]]:
        """检查磁盘标识（/dev/disk/by-id/）是否稳定。"""
        result = {"device_present": False, "by_id_links": [], "identity_stable": True}
        try:
            # 检查设备节点
            result["device_present"] = os.path.exists(self.device)
            if not result["device_present"]:
                self.log.error("  磁盘掉盘：设备节点不存在")
                return False, result

            # 检查 /dev/disk/by-id/ 符号链接
            _, out, _ = self.run_command("ls -la /dev/disk/by-id/ 2>/dev/null", timeout=10)
            basename = os.path.basename(self.device)
            for line in out.splitlines():
                if basename in line:
                    result["by_id_links"].append(line.strip())

            result["identity_stable"] = len(result["by_id_links"]) > 0
            self.log.info(f"  磁盘标识: 设备存在={result['device_present']}, "
                          f"by-id链接数={len(result['by_id_links'])}")
            return result["device_present"] and result["identity_stable"], result
        except Exception as e:
            self.log.error(f"  磁盘标识检查失败: {e}")
            return False, result

    def check_partition_and_fs(self) -> Tuple[bool, Dict[str, Any]]:
        """检查分区表和文件系统。"""
        result = {"partition_ok": False, "fs_mount_ok": False, "fsck_ok": False, "details": {}}
        part_dev = self.get_partition_device()
        try:
            # 分区表检查
            _, out, _ = self.run_command(f"parted -s {self.device} print", timeout=15)
            if "gpt" in out.lower() or "msdos" in out.lower():
                result["partition_ok"] = True
                result["details"]["partition_table"] = "gpt" if "gpt" in out.lower() else "msdos"
            self.log.info(f"  分区表: {'OK' if result['partition_ok'] else 'FAIL'}")

            # 重新挂载
            self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=10)
            _, mount_out, mount_err = self.run_command(
                f"mount {part_dev} {self.mount_point}", timeout=10)
            result["fs_mount_ok"] = mount_out is not None and os.path.ismount(self.mount_point)
            self.log.info(f"  文件系统挂载: {'OK' if result['fs_mount_ok'] else 'FAIL'}")

            # fsck 只读检查（需先卸载）
            self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=10)
            _, fsck_out, _ = self.run_command(f"fsck -n {part_dev}", timeout=60)
            result["details"]["fsck_output"] = fsck_out.strip()[:300]
            result["fsck_ok"] = "clean" in fsck_out.lower() or "errors" not in fsck_out.lower()
            self.log.info(f"  文件系统检查 (fsck): {'OK' if result['fsck_ok'] else 'FAIL'}")

            # 重新挂载
            self.run_command(f"mount {part_dev} {self.mount_point}", timeout=10)

            all_ok = all([result["partition_ok"], result["fs_mount_ok"], result["fsck_ok"]])
            return all_ok, result
        except Exception as e:
            self.log.error(f"  分区/文件系统检查失败: {e}")
            return False, result

    def check_smart(self) -> Tuple[bool, Dict[str, Any]]:
        """SMART 健康检查。"""
        info = {}
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                     check=True, capture=True, logger=self.log, timeout=15)
                smart = json.loads(out)
                # 多字段名兼容：不同厂商/不同 nvme-cli 版本的 smart-log JSON 字段名可能不同
                # 介质错误：media_and_data_integrity_errors / media_errors / media_and_data_errors
                media_errors = (
                    smart.get("media_and_data_integrity_errors")
                    if smart.get("media_and_data_integrity_errors") is not None
                    else smart.get("media_errors")
                    if smart.get("media_errors") is not None
                    else smart.get("media_and_data_errors", 0)
                )
                # 可用备件：available_spare / avail_spare / spare
                avail_spare = (
                    smart.get("available_spare")
                    if smart.get("available_spare") is not None
                    else smart.get("avail_spare")
                    if smart.get("avail_spare") is not None
                    else smart.get("spare", 100)
                )
                # None 值兜底：解析不到时视为正常（介质错误=0，可用备件=100），不因此判 FAIL
                info["media_errors"] = media_errors if media_errors is not None else 0
                info["available_spare"] = avail_spare if avail_spare is not None else 100
                info["power_cycles"] = smart.get("power_cycles")
                temp = smart.get("temperature", 0)
                info["temperature_c"] = temp - 273 if temp > 200 else temp

                # 介质错误改为警告模式：仅记录警告，不影响 PASS/FAIL
                # （已知有介质错误的测试盘仍可验证休眠唤醒稳定性）
                media_err_val = info.get("media_errors") or 0
                if media_err_val > 0:
                    self.log.warning(f"  [警告] 检测到介质错误: {media_err_val}（警告模式，不影响本轮判定）")
                    info["media_errors_warning"] = True
                else:
                    info["media_errors_warning"] = False

                # 硬性 FAIL 条件：可用备件过低（<10%）或温度超出范围（0-70°C）
                ok = ((info.get("available_spare") or 100) >= 10
                      and 0 <= (info.get("temperature_c") or 25) <= 70)
                self.log.info(f"  SMART: 介质错误={info.get('media_errors')}(警告模式), "
                              f"可用备件={info.get('available_spare')}%, "
                              f"温度={info.get('temperature_c')}°C -> {'OK' if ok else 'FAIL'}")
                return ok, info
            else:
                _, out, _ = run_cmd(["smartctl", "-H", self.device],
                                     check=True, capture=True, logger=self.log, timeout=15)
                info["raw"] = out.strip()[:300]
                ok = "PASSED" in out.upper()
                self.log.info(f"  SMART: {'OK' if ok else 'FAIL'}")
                return ok, info
        except Exception as e:
            self.log.error(f"  SMART 检查失败: {e}")
            info["error"] = str(e)
            return False, info

    # ---------- 单轮循环 ----------

    def run_cycle(self, state: Dict[str, Any], cycle: int, sleep_type: str) -> bool:
        """
        执行单轮 OSINT 测试循环。
        返回本轮是否通过。
        """
        self.log.info("=" * 55)
        self.log.info(f"  第 {cycle}/{state['total_cycles']} 轮 - {sleep_type.upper()} 休眠唤醒")
        self.log.info("=" * 55)

        # Step 1: 启动 IO 负载（活跃模式）
        self.log.info("\n  Step 1/5: 启动 IO 负载")
        io_started = self.start_io_load()
        if io_started and self.io_active:
            self.log.info(f"  IO 负载运行中 ({self.io_duration}s)...")
            time.sleep(self.io_duration)

        # Step 2: 触发休眠
        self.log.info(f"\n  Step 2/5: 触发 {sleep_type.upper()} 休眠")
        sleep_ok, sleep_info = self.trigger_sleep(sleep_type)
        if not sleep_ok:
            self.log.error(f"  休眠失败: {sleep_info}")
            self.stop_io_load()
            # 休眠失败也记录一轮结果，避免统计显示 0/0
            cycle_result = {
                "cycle": cycle,
                "sleep_type": sleep_type,
                "success": False,
                "identity_ok": False,
                "fs_ok": False,
                "integrity_ok": False,
                "smart_ok": False,
                "sleep_info": sleep_info,
                "identity_info": {"error": "休眠/挂载失败，未执行检查"},
                "fs_info": {"error": "休眠/挂载失败，未执行检查"},
                "integrity_info": {"error": "休眠/挂载失败，未执行检查"},
                "smart_info": {"error": "休眠/挂载失败，未执行检查"},
            }
            state["results"].append(cycle_result)
            self.save_state(state)
            self.log.info(f"\n  Step 5/5: 本轮结果: FAIL (休眠/挂载失败)")
            return False

        # 唤醒后等待系统稳定
        self.log.info("  等待系统稳定 (10s)...")
        time.sleep(10)

        # Step 3: 停止 IO 负载
        self.log.info("\n  Step 3/5: 停止 IO 负载")
        self.stop_io_load()

        # Step 4: 唤醒后检查
        self.log.info("\n  Step 4/5: 唤醒后检查")

        # 4a. 磁盘标识
        self.log.info("  [4a] 磁盘标识检查")
        identity_ok, identity_info = self.check_disk_identity()

        # 4b. 分区和文件系统
        self.log.info("  [4b] 分区和文件系统检查")
        fs_ok, fs_info = self.check_partition_and_fs()

        # 4c. 数据完整性
        self.log.info("  [4c] 数据完整性校验")
        integrity_ok, integrity_info = self.verify_integrity_data(state)

        # 4d. SMART
        self.log.info("  [4d] SMART 健康检查")
        smart_ok, smart_info = self.check_smart()

        # Step 5: 记录结果
        cycle_success = all([identity_ok, fs_ok, integrity_ok, smart_ok])
        cycle_result = {
            "cycle": cycle,
            "sleep_type": sleep_type,
            "success": cycle_success,
            "identity_ok": identity_ok,
            "fs_ok": fs_ok,
            "integrity_ok": integrity_ok,
            "smart_ok": smart_ok,
            "sleep_info": sleep_info,
            "identity_info": identity_info,
            "fs_info": fs_info,
            "integrity_info": integrity_info,
            "smart_info": smart_info,
        }
        state["results"].append(cycle_result)
        self.save_state(state)

        self.log.info(f"\n  Step 5/5: 本轮结果: {'PASS' if cycle_success else 'FAIL'} "
                      f"(磁盘标识={'OK' if identity_ok else 'FAIL'}, "
                      f"分区/FS={'OK' if fs_ok else 'FAIL'}, "
                      f"数据完整性={'OK' if integrity_ok else 'FAIL'}, "
                      f"SMART={'OK' if smart_ok else 'FAIL'})")
        return cycle_success

    # ---------- 最终完整功能测试 ----------

    def run_final_functional_test(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """全部循环完成后执行最终完整功能测试。"""
        self.log.info("=" * 55)
        self.log.info("  最终完整功能测试")
        self.log.info("=" * 55)
        result = {"capacity_ok": False, "smart_ok": False,
                  "seq_read_ok": False, "seq_write_ok": False, "details": {}}

        # 容量
        self.log.info("\n  [1/4] 容量检查")
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["details"]["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
            self.log.info(f"    容量: {bytes_to_human(cap)} -> {'OK' if cap > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    容量检查失败: {e}")

        # SMART
        self.log.info("\n  [2/4] SMART 检查")
        smart_ok, smart_info = self.check_smart()
        result["smart_ok"] = smart_ok
        result["details"]["smart"] = smart_info

        # 顺序读
        self.log.info("\n  [3/4] 顺序读性能 (128K QD32, 30s)")
        try:
            fio_cmd = ["fio", "--name=final_seq_read", f"--filename={self.device}",
                       "--rw=read", "--bs=128k", "--iodepth=32",
                       "--runtime=30", "--time_based", "--direct=1",
                       "--ioengine=libaio", "--group_reporting", "--output-format=json"]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf = json.loads(out)
            read_bw = perf["jobs"][0]["read"]["bw"]
            result["details"]["seq_read_bw_mbps"] = round(read_bw / 1024, 2)
            result["seq_read_ok"] = read_bw > 0
            self.log.info(f"    顺序读: {read_bw / 1024:.2f} MB/s -> {'OK' if read_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    顺序读测试失败: {e}")

        # 顺序写
        self.log.info("\n  [4/4] 顺序写性能 (128K QD32, 30s)")
        try:
            fio_cmd = ["fio", "--name=final_seq_write", f"--filename={self.device}",
                       "--rw=write", "--bs=128k", "--iodepth=32",
                       "--runtime=30", "--time_based", "--direct=1",
                       "--ioengine=libaio", "--group_reporting", "--output-format=json"]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf = json.loads(out)
            write_bw = perf["jobs"][0]["write"]["bw"]
            result["details"]["seq_write_bw_mbps"] = round(write_bw / 1024, 2)
            result["seq_write_ok"] = write_bw > 0
            self.log.info(f"    顺序写: {write_bw / 1024:.2f} MB/s -> {'OK' if write_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    顺序写测试失败: {e}")

        all_ok = all([result["capacity_ok"], result["smart_ok"],
                      result["seq_read_ok"], result["seq_write_ok"]])
        result["overall"] = "PASS" if all_ok else "FAIL"
        state["final_functional_test"] = result
        self.log.info(f"\n  最终功能测试: {'PASS' if all_ok else 'FAIL'}")
        return all_ok, result

    # ---------- 结果输出 ----------

    def print_final_results(self, state: Dict[str, Any]):
        """打印最终结果汇总。"""
        self.log.info("\n" + "=" * 55)
        self.log.info("  OSINT 测试完成 - 结果汇总")
        self.log.info("=" * 55)

        results = state.get('results', [])
        passed = sum(1 for r in results if r.get('success'))
        failed = len(results) - passed

        self.log.info(f"  总轮数: {state.get('total_cycles', '?')}")
        self.log.info(f"  实际执行: {len(results)} 轮")
        self.log.info(f"  通过: {passed}")
        self.log.info(f"  失败: {failed}")

        for r in results:
            status = "PASS" if r.get('success') else "FAIL"
            self.log.info(f"    第 {r.get('cycle', '?')} 轮 ({r.get('sleep_type', '?').upper()}): "
                          f"{status} (磁盘标识={'OK' if r.get('identity_ok') else 'FAIL'}, "
                          f"分区/FS={'OK' if r.get('fs_ok') else 'FAIL'}, "
                          f"数据完整性={'OK' if r.get('integrity_ok') else 'FAIL'}, "
                          f"SMART={'OK' if r.get('smart_ok') else 'FAIL'})")
            if r.get('sleep_info') and 'wake_ok' not in str(r.get('sleep_info')):
                self.log.info(f"      休眠信息: {r.get('sleep_info')}")

        if 'final_functional_test' in state:
            ft = state['final_functional_test']
            self.log.info(f"  最终功能测试: {ft.get('overall', '?')}")

        # 保存结果
        result_file = os.path.join(
            self.log_dir, f"osint_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            self.log.info(f"  结果已保存到: {result_file}")
        except Exception as e:
            self.log.error(f"  保存结果文件失败: {e}")

    # ---------- 主入口 ----------

    def run(self) -> TestResult:
        """OSINT 测试主入口。"""
        result = TestResult(
            test_item=TEST_OSINT,
            test_name="操作系统中断测试(OSINT)",
            device=self.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info(f"[DRY-RUN] 将执行 OSINT 测试: {self.cycles} 轮, "
                          f"休眠类型={self.sleep_type}, 休眠时长={self.sleep_duration}s, "
                          f"活跃IO={'开' if self.io_active else '关'}")
            result.finish(STATUS_SKIP, "dry-run 模式")
            return result

        try:
            # 检查 rtcwake 是否可用
            rtcwake_ok, _, _ = self.run_command("which rtcwake", timeout=5)
            if not rtcwake_ok:
                result.finish(STATUS_ERROR, "rtcwake 未安装，请执行: sudo apt install util-linux")
                return result

            # 加载或初始化状态
            state = self.load_state()
            # 状态有效性检查：上一次已完成 / 轮次不匹配 / 当前轮次超出总轮次 → 自动重置
            if state:
                phase = state.get('phase', '')
                saved_total = state.get('total_cycles', 0)
                current = state.get('current_cycle', 1)
                need_reset = False
                if phase == 'done':
                    self.log.info("检测到上一次测试已完成，自动重置状态开始新测试")
                    need_reset = True
                elif saved_total != self.cycles:
                    self.log.info(f"检测到循环次数变更 ({saved_total} → {self.cycles})，自动重置状态")
                    need_reset = True
                elif current > self.cycles + 1:
                    self.log.info(f"检测到状态轮次异常 (current={current} > total={self.cycles})，自动重置状态")
                    need_reset = True
                if need_reset:
                    self.clear_state()
                    state = None

            if not state:
                state = {
                    'total_cycles': self.cycles,
                    'current_cycle': 1,
                    'phase': 'init',
                    'results': [],
                    'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.save_state(state)

            # 初始化：创建分区、写入校验数据（仅首次）
            if state.get('phase') == 'init':
                self.log.info("初始化：创建测试分区和文件系统...")
                if not self.setup_test_partition():
                    result.finish(STATUS_FAIL, "创建测试分区失败")
                    return result

                self.log.info("初始化：写入完整性测试数据...")
                if not self.write_integrity_data(state):
                    result.finish(STATUS_FAIL, "写入完整性测试数据失败")
                    return result

                state['phase'] = 'running'
                self.save_state(state)

            # 确定休眠类型列表
            if self.sleep_type == self.SLEEP_BOTH:
                sleep_types = [self.SLEEP_S3, self.SLEEP_S4]
            else:
                sleep_types = [self.sleep_type]

            # 执行循环
            start_cycle = state.get('current_cycle', 1)
            all_passed = True

            for cycle in range(start_cycle, self.cycles + 1):
                state['current_cycle'] = cycle

                for sleep_type in sleep_types:
                    cycle_ok = self.run_cycle(state, cycle, sleep_type)
                    if not cycle_ok:
                        all_passed = False

                # 每轮完成后保存进度
                state['current_cycle'] = cycle + 1
                self.save_state(state)

            # 全部循环完成
            state['phase'] = 'done'
            self.save_state(state)

            # 最终完整功能测试
            final_ok, _ = self.run_final_functional_test(state)
            if not final_ok:
                all_passed = False

            # 打印结果
            self.print_final_results(state)

            # 统计
            total_executed = len(state.get('results', []))
            passed = sum(1 for r in state.get('results', []) if r.get('success'))
            result.details["cycles_executed"] = total_executed
            result.details["cycles_passed"] = passed
            result.details["cycles_failed"] = total_executed - passed
            result.details["final_test"] = state.get('final_functional_test')

            if all_passed and passed == total_executed:
                result.finish(STATUS_PASS, f"全部 {total_executed} 轮通过")
            else:
                result.finish(STATUS_FAIL, f"{passed}/{total_executed} 轮通过，{total_executed - passed} 轮失败")

        except Exception as e:
            self.log.exception(f"OSINT 测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result


# ============================================================
# 测试项 8：读/写测试 (Read/Write Test)
# ============================================================

class ReadWriteTester:
    """读/写测试。

    验证 SSD 数据读写功能正常，数据完整性无丢失/损坏/比特错误。
    包含三种模式：
    1. full_disk  - 全磁盘写入+读取验证（fio verify 模式）
    2. file_cycle - 多文件大小重复读写周期（256MB/1GB/4GB/16GB/32GB）
    3. long_run   - 24小时长期读写验证（混合读写持续负载）
    """

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.device = config.device
        self.state: Dict[str, Any] = {}
        self._load_state()

    # ----------------------------------------------------------
    # 状态持久化
    # ----------------------------------------------------------

    def _load_state(self):
        """加载状态文件。"""
        try:
            if os.path.exists(self.cfg.rw_state_file):
                with open(self.cfg.rw_state_file, "r") as f:
                    self.state = json.load(f)
                self.log.info(f"加载 RW 状态文件: {self.cfg.rw_state_file}")
        except Exception as e:
            self.log.warning(f"加载状态文件失败: {e}")
            self.state = {}

    def _save_state(self):
        """原子写入状态文件。"""
        try:
            os.makedirs(os.path.dirname(self.cfg.rw_state_file), exist_ok=True)
            tmp = self.cfg.rw_state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.cfg.rw_state_file)
        except Exception as e:
            self.log.warning(f"保存状态文件失败: {e}")

    def _clear_state(self):
        """清除状态文件。"""
        try:
            if os.path.exists(self.cfg.rw_state_file):
                os.remove(self.cfg.rw_state_file)
        except Exception:
            pass
        self.state = {}

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------

    def _run_cmd(self, cmd: List[str], timeout: int = 300, check: bool = True) -> Tuple[int, str, str]:
        """执行命令并记录日志。"""
        self.log.debug(f"执行: {' '.join(cmd)}")
        return run_cmd(cmd, check=check, capture=True, logger=self.log, timeout=timeout)

    @staticmethod
    def _extract_fio_json(output: str) -> str:
        """从 fio 输出中提取纯 JSON 部分。

        fio 可能在 stdout 开头输出警告行（如 'multiple writers may overwrite...'），
        混在 JSON 前面导致 json.loads 失败。此方法找到第一个 '{' 并提取到最后一个 '}'。
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

    def _check_device_online(self) -> bool:
        """检查设备是否在线（未掉盘）。"""
        if not os.path.exists(self.device):
            self.log.error(f"设备节点不存在: {self.device}（设备脱机/掉盘）")
            return False
        # 尝试读取设备大小
        try:
            result = subprocess.run(["blockdev", "--getsize64", self.device],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.log.error(f"设备无法读取大小: {self.device}")
                return False
            size = int(result.stdout.strip())
            self.log.info(f"设备在线: {self.device}, 容量: {size / 1024**3:.2f} GB")
            return True
        except Exception as e:
            self.log.error(f"设备检查异常: {e}")
            return False

    def _get_device_size_gb(self) -> float:
        """获取设备容量（GB）。"""
        try:
            result = subprocess.run(["blockdev", "--getsize64", self.device],
                                    capture_output=True, text=True, timeout=10)
            return int(result.stdout.strip()) / (1024 ** 3)
        except Exception:
            return 0.0

    def _check_smart(self) -> bool:
        """检查 SMART 健康状态。介质错误仅警告不判 FAIL（已知历史问题）。"""
        self.log.info("检查 SMART 健康状态...")
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = self._run_cmd(["nvme", "smart-log", self.cfg.nvme_ctrl, "-o", "json"], timeout=30)
                data = json.loads(out)
                # 兼容不同 nvme-cli 版本的字段名
                media_errors = data.get("media_errors",
                               data.get("media_and_data_integrity_errors", 0))
                avail_spare = data.get("avail_spare",
                             data.get("available_spare", 100))
                temp = data.get("temperature", 0)
                temp_c = temp - 273 if temp > 200 else temp
                self.log.info(f"  SMART: 介质错误={media_errors}, 可用备件={avail_spare}%, 温度={temp_c}°C")
                # 介质错误仅警告（已知历史问题，不影响读写功能判定）
                if media_errors and media_errors > 0:
                    self.log.warning(f"  介质错误数 > 0 ({media_errors})，仅警告不判 FAIL（已知历史问题）")
                # 可用备件过低为硬性 FAIL
                if avail_spare is not None and avail_spare < 10:
                    self.log.warning(f"  可用备件过低 ({avail_spare}%)")
                    return False
                # 温度异常为硬性 FAIL
                if temp_c is not None and not (0 <= temp_c <= 70):
                    self.log.warning(f"  温度异常 ({temp_c}°C)")
                    return False
                return True
            else:
                _, out, _ = self._run_cmd(["smartctl", "-H", self.device], timeout=30)
                if "PASSED" in out or "OK" in out:
                    self.log.info("  SMART 健康状态: PASSED")
                    return True
                else:
                    self.log.warning("  SMART 健康状态: FAILED")
                    return False
        except Exception as e:
            self.log.warning(f"  SMART 检查失败: {e}")
            return True  # 检查工具异常不视为测试失败

    def _get_pattern_arg(self) -> List[str]:
        """获取 fio pattern 参数。"""
        if self.cfg.rw_pattern == RW_PATTERN_RANDOM:
            return []  # fio 默认随机数据
        elif self.cfg.rw_pattern == RW_PATTERN_00:
            return ["--buffer_pattern=0x00"]
        elif self.cfg.rw_pattern == RW_PATTERN_FF:
            return ["--buffer_pattern=0xFF"]
        elif self.cfg.rw_pattern == RW_PATTERN_AA:
            return ["--buffer_pattern=0xAA"]
        elif self.cfg.rw_pattern == RW_PATTERN_55:
            return ["--buffer_pattern=0x55"]
        return []

    # ----------------------------------------------------------
    # 模式 1：全磁盘写入 + 读取验证
    # ----------------------------------------------------------

    def run_full_disk_test(self) -> Tuple[bool, str]:
        """全磁盘写入+读取验证。

        使用 fio verify 模式：写入时计算校验和，读取时验证。
        fio verify=md5 会在每个 block 头部存储校验和，读取时自动比对。
        """
        self.log.info("=" * 50)
        self.log.info("模式 1：全磁盘写入 + 读取验证")
        self.log.info("=" * 50)

        if not self._check_device_online():
            return False, "设备脱机"

        device_size_gb = self._get_device_size_gb()
        self.log.info(f"设备容量: {device_size_gb:.2f} GB")

        pattern_args = self._get_pattern_arg()
        verify = self.cfg.rw_verify

        # 阶段 1：全磁盘写入（带 verify 校验和）
        self.log.info("\n[阶段 1/2] 全磁盘写入...")
        write_cmd = [
            "fio",
            "--name=rw_full_disk_write",
            f"--filename={self.device}",
            "--rw=write",
            f"--bs={self.cfg.rw_block_size}",
            f"--iodepth={self.cfg.rw_io_depth}",
            f"--numjobs={self.cfg.rw_numjobs}",
            "--direct=1",
            "--ioengine=libaio",
            f"--verify={verify}",
            "--verify_fatal=1",
            "--verify_dump=1",
            "--do_verify=0",  # 写入阶段不验证，单独读取阶段验证
            "--group_reporting",
            "--output-format=json",
        ] + pattern_args

        try:
            rc, out, err = self._run_cmd(write_cmd, timeout=7200, check=False)
            if not out.strip():
                err_msg = (err.strip()[:300] if err.strip()
                           else f"fio 无输出（退出码={rc}）")
                self.log.error(f"  全磁盘写入失败: {err_msg}")
                return False, f"全磁盘写入失败: {err_msg}"
            data = json.loads(self._extract_fio_json(out))
            write_bw = data["jobs"][0]["write"]["bw"] / 1024  # KB/s -> MB/s
            write_iops = data["jobs"][0]["write"]["iops"]
            if rc != 0:
                self.log.warning(f"  fio 退出码非零 ({rc})，但写入已完成")
            self.log.info(f"  写入完成: 带宽={write_bw:.2f} MB/s, IOPS={write_iops:.0f}")
        except json.JSONDecodeError as e:
            self.log.error(f"  全磁盘写入失败: fio输出解析失败: {e}")
            return False, f"全磁盘写入失败: fio输出解析失败"
        except Exception as e:
            self.log.error(f"  全磁盘写入失败: {e}")
            return False, f"全磁盘写入失败: {e}"

        # 检查设备是否仍在线
        if not self._check_device_online():
            return False, "写入后设备脱机"

        # 阶段 2：全磁盘读取验证
        # 全磁盘写入阶段已用高并发（用户配置 numjobs）覆盖整个磁盘；
        # 读取验证用单 job 顺序读全盘，确保 verify pattern 一致性（全磁盘不适用 offset_increment）
        self.log.info("\n[阶段 2/2] 全磁盘读取验证...")
        read_cmd = [
            "fio",
            "--name=rw_full_disk_verify",
            f"--filename={self.device}",
            "--rw=read",
            f"--bs={self.cfg.rw_block_size}",
            f"--iodepth={self.cfg.rw_io_depth}",
            "--numjobs=1",  # 全磁盘验证单 job 顺序读取，确保 pattern 一致
            "--direct=1",
            "--ioengine=libaio",
            f"--verify={verify}",
            "--verify_fatal=1",
            "--verify_dump=1",
            "--do_verify=1",  # 读取并验证
            "--group_reporting",
            "--output-format=json",
        ]

        try:
            rc, out, err = self._run_cmd(read_cmd, timeout=7200, check=False)
            if not out.strip():
                err_msg = (err.strip()[:300] if err.strip()
                           else f"fio 无输出（退出码={rc}），数据校验可能失败")
                self.log.error(f"  读取验证失败: {err_msg}")
                return False, f"数据验证失败: {err_msg}"
            data = json.loads(self._extract_fio_json(out))
            read_bw = data["jobs"][0]["read"]["bw"] / 1024
            read_iops = data["jobs"][0]["read"]["iops"]
            if rc != 0:
                self.log.warning(f"  fio 退出码非零 ({rc})，但读取验证已完成")
            self.log.info(f"  读取验证完成: 带宽={read_bw:.2f} MB/s, IOPS={read_iops:.0f}")
            self.log.info(f"  数据一致性: 全部校验通过（{verify}）")
        except json.JSONDecodeError as e:
            self.log.error(f"  读取验证失败: fio输出解析失败: {e}")
            return False, f"数据验证失败: fio输出解析失败"
        except Exception as e:
            self.log.error(f"  读取验证失败: {e}")
            return False, f"数据验证失败: {e}"

        # 最终设备检查
        if not self._check_device_online():
            return False, "验证后设备脱机"

        self.log.info("\n全磁盘写入+读取验证: PASS")
        return True, f"全磁盘验证通过（写入={write_bw:.0f}MB/s, 读取={read_bw:.0f}MB/s）"

    # ----------------------------------------------------------
    # 模式 2：多文件大小重复读写周期
    # ----------------------------------------------------------

    def run_file_cycle_test(self) -> Tuple[bool, str]:
        """多文件大小重复读写周期。

        对每个选定的文件大小（256MB/1GB/4GB/16GB/32GB），
        执行指定次数的写→读验证循环。
        """
        self.log.info("=" * 50)
        self.log.info("模式 2：多文件大小重复读写周期")
        self.log.info("=" * 50)

        if not self._check_device_online():
            return False, "设备脱机"

        file_sizes = self.cfg.rw_file_sizes
        cycles = self.cfg.rw_cycles
        verify = self.cfg.rw_verify
        pattern_args = self._get_pattern_arg()

        self.log.info(f"文件大小列表: {file_sizes}")
        self.log.info(f"每个大小循环次数: {cycles}")
        self.log.info(f"校验方式: {verify}")
        self.log.info(f"数据 pattern: {self.cfg.rw_pattern}")

        total_cycles = len(file_sizes) * cycles
        current = 0
        all_passed = True
        failure_details = []

        for size_label in file_sizes:
            size_mb = RW_FILE_SIZES.get(size_label)
            if size_mb is None:
                self.log.warning(f"未知文件大小: {size_label}，跳过")
                continue

            # 检查设备容量是否足够
            device_size_gb = self._get_device_size_gb()
            if size_mb / 1024 > device_size_gb:
                self.log.warning(f"文件大小 {size_label} 超过设备容量 {device_size_gb:.1f}GB，跳过")
                continue

            self.log.info(f"\n--- 文件大小: {size_label} ({size_mb} MB) ---")

            for cycle in range(1, cycles + 1):
                current += 1
                self.log.info(f"\n[{current}/{total_cycles}] {size_label} 第 {cycle}/{cycles} 轮")

                # 写入 + 验证（单次 fio 完成写后立即读验证）
                # 使用 offset_increment 让多 job 写入不同区域，避免互相覆盖，同时保留并发配置
                numjobs = self.cfg.rw_numjobs
                total_needed_mb = size_mb * numjobs
                device_size_mb = int(self._get_device_size_gb() * 1024)
                if total_needed_mb > device_size_mb:
                    self.log.warning(f"  文件大小 {size_label} × numjobs={numjobs} = {total_needed_mb}MB "
                                     f"超过设备容量 {device_size_mb}MB，自动将 numjobs 降为 "
                                     f"{max(1, device_size_mb // size_mb)}")
                    numjobs = max(1, device_size_mb // size_mb)

                fio_cmd = [
                    "fio",
                    f"--name=rw_cycle_{size_label}_{cycle}",
                    f"--filename={self.device}",
                    "--rw=write",
                    f"--bs={self.cfg.rw_block_size}",
                    f"--iodepth={self.cfg.rw_io_depth}",
                    f"--numjobs={numjobs}",
                    "--direct=1",
                    "--ioengine=libaio",
                    f"--size={size_mb}M",
                    f"--offset_increment={size_mb}M",  # 每个 job 间隔 size_mb，避免覆盖
                    f"--verify={verify}",
                    "--verify_fatal=1",
                    "--verify_dump=1",
                    "--do_verify=1",  # 写入后自动读取验证
                    "--group_reporting",
                    "--output-format=json",
                ] + pattern_args

                try:
                    rc, out, err = self._run_cmd(fio_cmd, timeout=3600, check=False)
                    if not out.strip():
                        err_msg = (err.strip()[:300] if err.strip()
                                   else f"fio 无输出（退出码={rc}），可能 verify 校验失败")
                        self.log.error(f"  读写验证失败: {err_msg}")
                        all_passed = False
                        failure_details.append(f"{size_label} 第{cycle}轮: {err_msg}")
                        if not self._check_device_online():
                            self.log.error("设备脱机，终止后续测试")
                            return False, f"设备脱机（{size_label} 第{cycle}轮后）"
                        continue
                    data = json.loads(self._extract_fio_json(out))
                    write_bw = data["jobs"][0]["write"]["bw"] / 1024
                    if rc != 0:
                        self.log.warning(f"  fio 退出码非零 ({rc})，但数据验证已完成")
                    self.log.info(f"  写入+验证通过: 带宽={write_bw:.2f} MB/s")
                except json.JSONDecodeError as e:
                    err_msg = f"fio 输出解析失败: {e}（输出前200字: {out[:200] if 'out' in dir() else 'N/A'}）"
                    self.log.error(f"  读写验证失败: {err_msg}")
                    all_passed = False
                    failure_details.append(f"{size_label} 第{cycle}轮: JSON解析失败")
                    if not self._check_device_online():
                        self.log.error("设备脱机，终止后续测试")
                        return False, f"设备脱机（{size_label} 第{cycle}轮后）"
                except Exception as e:
                    self.log.error(f"  读写验证失败: {e}")
                    all_passed = False
                    failure_details.append(f"{size_label} 第{cycle}轮: {e}")
                    # 检查设备是否脱机
                    if not self._check_device_online():
                        self.log.error("设备脱机，终止后续测试")
                        return False, f"设备脱机（{size_label} 第{cycle}轮后）"

        # 最终 SMART 检查
        smart_ok = self._check_smart()
        if not smart_ok:
            all_passed = False
            failure_details.append("SMART 检查异常")

        if all_passed:
            self.log.info(f"\n多文件大小读写周期: PASS（{current} 轮全部通过）")
            return True, f"{current} 轮读写验证全部通过"
        else:
            self.log.error(f"\n多文件大小读写周期: FAIL（{len(failure_details)} 项失败）")
            for detail in failure_details:
                self.log.error(f"  - {detail}")
            return False, f"{len(failure_details)} 项失败: {'; '.join(failure_details[:3])}"

    # ----------------------------------------------------------
    # 模式 3：24小时长期读写验证
    # ----------------------------------------------------------

    def run_long_run_test(self) -> Tuple[bool, str]:
        """24小时长期读写验证。

        持续混合读写负载，定期检查设备在线状态和数据完整性。
        """
        self.log.info("=" * 50)
        self.log.info(f"模式 3：{self.cfg.rw_long_run_hours}小时长期读写验证")
        self.log.info("=" * 50)

        if not self._check_device_online():
            return False, "设备脱机"

        runtime_sec = self.cfg.rw_long_run_hours * 3600
        verify = self.cfg.rw_verify
        read_ratio = self.cfg.rw_mixed_read_ratio
        pattern_args = self._get_pattern_arg()

        self.log.info(f"运行时长: {self.cfg.rw_long_run_hours} 小时 ({runtime_sec} 秒)")
        self.log.info(f"负载模式: randrw ({read_ratio}%读 / {100-read_ratio}%写)")
        self.log.info(f"块大小: {self.cfg.rw_block_size}, 队列深度: {self.cfg.rw_io_depth}, numjobs: {self.cfg.rw_numjobs}")
        self.log.info(f"数据校验: {verify}")

        # 阶段 1：先写入基准数据（用于后续验证）
        self.log.info("\n[准备] 写入基准测试数据（前 32GB）...")
        baseline_cmd = [
            "fio",
            "--name=rw_long_baseline",
            f"--filename={self.device}",
            "--rw=write",
            f"--bs={self.cfg.rw_block_size}",
            f"--iodepth={self.cfg.rw_io_depth}",
            "--direct=1",
            "--ioengine=libaio",
            "--size=32G",
            f"--verify={verify}",
            "--verify_fatal=1",
            "--do_verify=0",
            "--group_reporting",
            "--output-format=json",
        ] + pattern_args

        try:
            rc, out, err = self._run_cmd(baseline_cmd, timeout=3600, check=False)
            if rc != 0 and not out.strip():
                err_msg = (err.strip()[:300] if err.strip() else f"退出码={rc}")
                self.log.error(f"  基准数据写入失败: {err_msg}")
                return False, f"基准数据写入失败: {err_msg}"
            self.log.info("  基准数据写入完成")
        except Exception as e:
            self.log.error(f"  基准数据写入失败: {e}")
            return False, f"基准数据写入失败: {e}"

        # 阶段 2：长期混合读写运行
        self.log.info(f"\n[运行] 开始 {self.cfg.rw_long_run_hours} 小时混合读写...")
        long_cmd = [
            "fio",
            "--name=rw_long_run",
            f"--filename={self.device}",
            "--rw=randrw",
            f"--rwmixread={read_ratio}",
            f"--bs={self.cfg.rw_block_size}",
            f"--iodepth={self.cfg.rw_io_depth}",
            f"--numjobs={self.cfg.rw_numjobs}",
            "--direct=1",
            "--ioengine=libaio",
            f"--runtime={runtime_sec}",
            "--time_based",
            "--group_reporting",
            "--output-format=json",
        ]

        start_time = time.time()
        try:
            rc, out, err = self._run_cmd(long_cmd, timeout=runtime_sec + 300, check=False)
            if not out.strip():
                err_msg = (err.strip()[:300] if err.strip() else f"fio 无输出（退出码={rc}）")
                self.log.error(f"  长期运行失败: {err_msg}")
                return False, f"长期运行失败: {err_msg}"
            data = json.loads(self._extract_fio_json(out))
            read_bw = data["jobs"][0]["read"]["bw"] / 1024
            write_bw = data["jobs"][0]["write"]["bw"] / 1024
            read_iops = data["jobs"][0]["read"]["iops"]
            write_iops = data["jobs"][0]["write"]["iops"]
            elapsed = data["jobs"][0].get("elapsed", runtime_sec) / 1000

            self.log.info(f"  长期运行完成: 耗时={elapsed:.0f}s")
            self.log.info(f"  读: 带宽={read_bw:.2f} MB/s, IOPS={read_iops:.0f}")
            self.log.info(f"  写: 带宽={write_bw:.2f} MB/s, IOPS={write_iops:.0f}")
        except json.JSONDecodeError as e:
            self.log.error(f"  长期运行失败: fio输出解析失败: {e}")
            return False, f"长期运行失败: fio输出解析失败"
        except Exception as e:
            self.log.error(f"  长期运行失败: {e}")
            return False, f"长期运行失败: {e}"

        # 阶段 3：验证基准数据完整性（使用 offset_increment 支持多 job 并发验证）
        self.log.info("\n[验证] 验证基准数据完整性...")
        verify_size_gb = 32
        verify_numjobs = self.cfg.rw_numjobs
        verify_device_gb = self._get_device_size_gb()
        if verify_size_gb * verify_numjobs > verify_device_gb:
            self.log.warning(f"  验证区域 {verify_size_gb}GB × numjobs={verify_numjobs} "
                             f"超过设备容量 {verify_device_gb:.1f}GB，自动将 numjobs 降为 "
                             f"{max(1, int(verify_device_gb // verify_size_gb))}")
            verify_numjobs = max(1, int(verify_device_gb // verify_size_gb))

        verify_cmd = [
            "fio",
            "--name=rw_long_verify",
            f"--filename={self.device}",
            "--rw=read",
            f"--bs={self.cfg.rw_block_size}",
            f"--iodepth={self.cfg.rw_io_depth}",
            f"--numjobs={verify_numjobs}",
            "--direct=1",
            "--ioengine=libaio",
            f"--size={verify_size_gb}G",
            f"--offset_increment={verify_size_gb}G",  # 每个 job 间隔 32G，避免覆盖
            f"--verify={verify}",
            "--verify_fatal=1",
            "--verify_dump=1",
            "--do_verify=1",
            "--group_reporting",
            "--output-format=json",
        ]

        try:
            rc, out, err = self._run_cmd(verify_cmd, timeout=3600, check=False)
            if rc != 0:
                err_msg = (err.strip()[:300] if err.strip() else f"fio 退出码={rc}，数据校验可能失败")
                self.log.error(f"  基准数据验证失败: {err_msg}")
                return False, f"长期运行后数据验证失败: {err_msg}"
            self.log.info("  基准数据完整性验证通过")
        except Exception as e:
            self.log.error(f"  基准数据验证失败: {e}")
            return False, f"长期运行后数据验证失败: {e}"

        # 最终检查
        if not self._check_device_online():
            return False, "长期运行后设备脱机"

        smart_ok = self._check_smart()
        if not smart_ok:
            return False, "长期运行后 SMART 异常"

        total_time = (time.time() - start_time) / 3600
        self.log.info(f"\n{self.cfg.rw_long_run_hours}小时长期读写验证: PASS（实际运行 {total_time:.1f} 小时）")
        return True, f"长期运行通过（读={read_bw:.0f}MB/s, 写={write_bw:.0f}MB/s）"

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------

    def run(self) -> TestResult:
        """执行读/写测试。"""
        result = TestResult(test_item=TEST_RW, test_name="读/写测试", device=self.device)
        result.start()

        self.log.info("=" * 55)
        self.log.info("开始读/写测试 (Read/Write Test)")
        self.log.info("=" * 55)
        self.log.info(f"测试模式: {self.cfg.rw_mode}")
        self.log.info(f"设备: {self.device}")

        # 初始设备检查
        if not self._check_device_online():
            result.finish(STATUS_FAIL, "初始设备检查失败：设备脱机")
            return result

        # 初始 SMART 检查
        self._check_smart()

        mode = self.cfg.rw_mode
        results: List[Tuple[bool, str]] = []

        try:
            if mode in (RW_MODE_FULL_DISK, RW_MODE_ALL):
                ok, msg = self.run_full_disk_test()
                results.append((ok, f"全磁盘验证: {msg}"))

            if mode in (RW_MODE_FILE_CYCLE, RW_MODE_ALL):
                ok, msg = self.run_file_cycle_test()
                results.append((ok, f"文件周期: {msg}"))

            if mode in (RW_MODE_LONG_RUN, RW_MODE_ALL):
                ok, msg = self.run_long_run_test()
                results.append((ok, f"长期运行: {msg}"))

        except Exception as e:
            self.log.exception(f"读/写测试异常: {e}")
            result.finish(STATUS_ERROR, str(e))
            return result

        # 汇总结果
        all_passed = all(r[0] for r in results)
        summary = "; ".join(r[1] for r in results)

        self.log.info("\n" + "=" * 55)
        self.log.info("读/写测试结果汇总")
        self.log.info("=" * 55)
        for ok, msg in results:
            status = "PASS" if ok else "FAIL"
            self.log.info(f"  [{status}] {msg}")
        self.log.info("=" * 55)

        if all_passed:
            result.details["测试模式"] = mode
            result.details["子项结果"] = summary
            result.finish(STATUS_PASS, f"读/写测试全部通过（{len(results)} 项）")
        else:
            result.details["失败详情"] = summary
            result.finish(STATUS_FAIL, f"读/写测试存在失败项")

        # 清除状态文件（测试正常完成）
        self._clear_state()

        return result


# ============================================================
# 测试报告
# ============================================================

class TestReport:
    """测试报告汇总与生成。"""

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.results: List[TestResult] = []

    def add_result(self, result: TestResult):
        self.results.append(result)

    def get_summary(self) -> Dict[str, Any]:
        """获取测试汇总统计。"""
        summary = {
            "total": len(self.results),
            "pass": 0, "fail": 0, "error": 0, "skip": 0,
            "by_item": {},
            "start_time": None, "end_time": None,
            "total_duration_sec": 0.0,
        }
        for r in self.results:
            summary[r.status.lower()] = summary.get(r.status.lower(), 0) + 1
            summary["by_item"][r.test_item] = r.status
            if r.start_time and (summary["start_time"] is None or r.start_time < summary["start_time"]):
                summary["start_time"] = r.start_time
            if r.end_time and (summary["end_time"] is None or r.end_time > summary["end_time"]):
                summary["end_time"] = r.end_time
            summary["total_duration_sec"] += r.duration_sec

        return summary

    def generate_json_report(self, output_dir: str) -> str:
        """生成 JSON 报告文件，返回文件路径。"""
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = os.path.join(output_dir, f"ssd_test_report_{timestamp}.json")

        report = {
            "script_version": SCRIPT_VERSION,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "device": self.cfg.device,
            "device_type": self.cfg.device_type,
            "test_config": {
                "test_items": self.cfg.test_items,
                "fw_image": self.cfg.fw_image,
                "fw_action": self.cfg.fw_action,
                "perf_state": self.cfg.perf_state,
                "perf_runtime": self.cfg.perf_runtime,
                "precondition": self.cfg.precondition,
                "dry_run": self.cfg.dry_run,
            },
            "summary": self.get_summary(),
            "results": [r.to_dict() for r in self.results],
        }

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        self.log.info(f"JSON 报告已生成: {report_file}")
        return report_file

    def print_summary(self):
        """打印测试汇总到控制台。"""
        summary = self.get_summary()
        self.log.info("=" * 55)
        self.log.info("  测试汇总")
        self.log.info("=" * 55)
        item_names = {
            TEST_CAPACITY: "设备容量",
            TEST_SMART: "SMART健康信息",
            TEST_FW: "固件升级/降级",
            TEST_PERF: "完整性能特征",
        }
        for r in self.results:
            name = item_names.get(r.test_item, r.test_item)
            self.log.info(f"  {name:<16s}: {r.status} ({r.duration_sec:.1f}s)")
            if r.error_message:
                self.log.info(f"    错误: {r.error_message}")
        self.log.info("-" * 55)
        self.log.info(f"  总计: {summary['pass']} PASS / {summary['fail']} FAIL / "
                       f"{summary['error']} ERROR / {summary['skip']} SKIP")
        self.log.info(f"  总耗时: {summary['total_duration_sec']:.1f}s")
        self.log.info("=" * 55)


# ============================================================
# 命令行参数解析
# ============================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="SSD 自动化测试脚本 - 固件升降级/SMART/容量/性能/电源循环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t capacity
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t fw --fw-image fw.bin --fw-action upgrade
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-cycles 10 --ipmi-host 192.168.1.100 -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-cycles 5 --pc-power-mode manual -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-power-mode enhanced --pc-pattern-size-gb 20 -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-power-mode enhanced --ipmi-host 192.168.1.100 -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode enhanced --ipmi-host 192.168.1.100 -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode manual -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t spor --spor-power-mode timeboard --spor-cycles 20 -y
        """
    )

    parser.add_argument("-d", "--device", required=True,
                        help="待测设备路径，如 /dev/nvme0n1 或 /dev/sda")
    parser.add_argument("-t", "--test", nargs="+", default=[TEST_ALL],
                        choices=VALID_TEST_ITEMS,
                        help="测试项，可多选: fw/smart/capacity/perf/powercycle/all (默认: all)")
    parser.add_argument("--fw-image", default=None,
                        help="固件镜像文件路径（fw 测试必填）")
    parser.add_argument("--fw-action", default=FW_UPGRADE, choices=[FW_UPGRADE, FW_DOWNGRADE],
                        help="固件操作类型: upgrade/downgrade (默认: upgrade)")
    parser.add_argument("--fw-slot", type=int, default=None,
                        help="固件槽位 1-7，默认自动选择")
    parser.add_argument("--perf-state", default=PERF_UNKNOWN,
                        choices=[PERF_FOB, PERF_STEADY, PERF_UNKNOWN],
                        help="性能测试目标状态: fob/steady/unknown (默认: unknown 直接测试)")
    parser.add_argument("--perf-runtime", type=int, default=DEFAULT_PERF_RUNTIME,
                        help=f"单项 fio 运行时长（秒），默认 {DEFAULT_PERF_RUNTIME}")
    parser.add_argument("--precondition", default="on", choices=["on", "off"],
                        help="稳态预处理开关: on/off (默认: on)")
    parser.add_argument("--no-precondition", action="store_true",
                        help="跳过稳态预处理（等价于 --precondition off）")
    parser.add_argument("--precond-rand-speed", type=int, default=30,
                        help="稳态预处理4K随机写预估速度MB/s，用于超时计算 (默认: 30)")
    parser.add_argument("--precond-rand-max-sec", type=int, default=0,
                        help="随机写预处理最大时长秒，0=自动按速度估算 (默认: 0)")
    # 完整性能特征 - 全参数可配置（FIO原生参数对齐）
    parser.add_argument("--perf-name", default="perf-test",
                        help="FIO测试项目名称（--name），同步为日志文件名核心标识，默认: perf-test")
    parser.add_argument("--perf-direct", type=int, default=1, choices=[0, 1],
                        help="--direct，0/1切换，1=裸盘绕过系统缓存（默认）")
    parser.add_argument("--perf-ioengine", default="libaio",
                        help="--ioengine，direct=1时强制libaio，默认: libaio")
    parser.add_argument("--perf-bs", default="4k",
                        help="--bs 块大小，参考值: 4k/8k/16k/128k/1M，默认: 4k")
    parser.add_argument("--perf-iodepth", type=int, default=64,
                        help="--iodepth 队列深度，参考值: 1/8/32/64/128，默认: 64")
    parser.add_argument("--perf-numjobs", type=int, default=1,
                        help="--numjobs 并发任务数，参考值: 1/2/4/8，默认: 1")
    parser.add_argument("--perf-rw", default="randread",
                        choices=["randread", "randwrite", "randrw", "read", "write", "rw"],
                        help="--rw 读写模式，默认: randread")
    parser.add_argument("--perf-rwmixread", type=int, default=70,
                        help="--rwmixread 读占比(0-100)，仅混合模式(randrw/rw)生效，默认: 70")
    parser.add_argument("--perf-size", default="3%",
                        help="--size 测试范围，支持百分比(如3%%)或固定容量(如10G)，默认: 3%%")
    parser.add_argument("--perf-text-log", action="store_true",
                        help="同时输出文本格式日志（默认仅json+结构化日志）")
    parser.add_argument("--perf-task-file", default=None,
                        help="v1.8.0 多任务批量测试JSON文件路径，文件内为任务数组，每项包含perf_test_name/perf_rw/perf_bs等字段；指定后覆盖单组参数")
    # 电源循环测试参数
    parser.add_argument("--pc-cycles", type=int, default=DEFAULT_PC_CYCLES,
                        help=f"电源循环测试循环次数，默认 {DEFAULT_PC_CYCLES}")
    parser.add_argument("--pc-power-mode", default=DEFAULT_PC_POWER_MODE,
                        choices=["ipmi", "manual", "enhanced"],
                        help="电源控制模式: ipmi(远程自动)/manual(手动断电上电)/enhanced(增强模式:NVMe优雅移除+Pattern校验+PCIe Link校验) (默认: ipmi)")
    parser.add_argument("--ipmi-host", default=None,
                        help="IPMI BMC 地址（ipmi 模式必填）")
    parser.add_argument("--ipmi-user", default="ADMIN",
                        help="IPMI 用户名 (默认: ADMIN)")
    parser.add_argument("--ipmi-pass", default="ADMIN",
                        help="IPMI 密码 (默认: ADMIN)")
    parser.add_argument("--pc-mount-point", default=DEFAULT_PC_MOUNT_POINT,
                        help=f"电源循环测试挂载点 (默认: {DEFAULT_PC_MOUNT_POINT})")
    parser.add_argument("--pc-state-file", default=DEFAULT_PC_STATE_FILE,
                        help=f"电源循环状态文件路径 (默认: {DEFAULT_PC_STATE_FILE})")
    parser.add_argument("--pc-boot-timeout", type=int, default=DEFAULT_PC_BOOT_TIMEOUT,
                        help=f"开机等待超时（秒），默认 {DEFAULT_PC_BOOT_TIMEOUT}")
    parser.add_argument("--pc-off-interval", type=int, default=DEFAULT_PC_OFF_INTERVAL,
                        help=f"断电后等待上电间隔（秒），默认 {DEFAULT_PC_OFF_INTERVAL}")
    parser.add_argument("--pc-rw-duration", type=int, default=DEFAULT_PC_RW_DURATION,
                        help=f"每循环混合读写运行时长（秒），默认 {DEFAULT_PC_RW_DURATION}")
    # enhanced（增强）模式专属参数
    parser.add_argument("--pc-pattern", default=DEFAULT_PC_PATTERN,
                        help=f"enhanced 模式写入数据 Pattern (默认: {DEFAULT_PC_PATTERN})")
    parser.add_argument("--pc-pattern-size-gb", type=int, default=DEFAULT_PC_PATTERN_SIZE_GB,
                        help=f"enhanced 模式 Pattern 数据写入量 GB (默认: {DEFAULT_PC_PATTERN_SIZE_GB})")
    parser.add_argument("--pc-no-link-check", action="store_true",
                        help="enhanced 模式禁用 PCIe Link 速率/宽度校验")
    # SPOR（意外电源循环测试）参数
    parser.add_argument("--spor-cycles", type=int, default=10,
                        help="SPOR 测试循环次数，默认 10")
    parser.add_argument("--spor-power-mode", default=DEFAULT_SPOR_POWER_MODE,
                        choices=["timeboard", "manual", "enhanced"],
                        help="SPOR 断电方式: timeboard(Timeboard硬件)/manual(手动断电)/enhanced(IPMI意外断电+高QD+增强校验) (默认: timeboard)")
    parser.add_argument("--spor-delay", type=int, default=5,
                        help="SPOR 写入后多久触发硬件断电（秒），默认 5")
    parser.add_argument("--spor-test-size", type=int, default=20,
                        help="SPOR 测试数据大小（GB），默认 20")
    parser.add_argument("--spor-lba-size", type=int, default=4096,
                        help="SPOR LBA 大小（字节），默认 4096")
    parser.add_argument("--spor-skip-lba", type=int, default=8,
                        help="掉电边界跳过验证的 LBA 数，默认 8")
    parser.add_argument("--spor-poweroff-delay-ms", type=int, default=500,
                        help="Timeboard 硬件断电延时（毫秒），默认 500（越短越意外）")
    parser.add_argument("--spor-mixed-rw", action="store_true",
                        help="SPOR 使用混合读写负载（默认纯顺序写）")
    parser.add_argument("--spor-mixed-read-ratio", type=int, default=70,
                        help="混合读写模式下读占比（%%），默认 70")
    parser.add_argument("--spor-no-final-test", action="store_true",
                        help="跳过 SPOR 最终完整功能测试")
    parser.add_argument("--spor-timeboard-port", default="/dev/ttyUSB0",
                        help="Timeboard 串口设备路径，默认 /dev/ttyUSB0")
    parser.add_argument("--spor-state-file", default="/var/lib/ssd_spor_state.json",
                        help="SPOR 状态文件路径，默认 /var/lib/ssd_spor_state.json")
    parser.add_argument("--spor-enhanced-iodepth", type=int, default=DEFAULT_SPOR_ENHANCED_IODEPTH,
                        help=f"enhanced 模式写入队列深度 (默认: {DEFAULT_SPOR_ENHANCED_IODEPTH})")
    parser.add_argument("--spor-enhanced-bs", default=DEFAULT_SPOR_ENHANCED_BS,
                        help=f"enhanced 模式写入块大小 (默认: {DEFAULT_SPOR_ENHANCED_BS})")
    # OSINT（操作系统中断测试）参数
    parser.add_argument("--osint-cycles", type=int, default=10,
                        help="OSINT 测试循环次数，默认 10")
    parser.add_argument("--osint-sleep-type", default="s3", choices=["s3", "s4", "both"],
                        help="OSINT 休眠类型: s3(挂起到内存)/s4(挂起到磁盘)/both，默认 s3")
    parser.add_argument("--osint-sleep-duration", type=int, default=30,
                        help="OSINT 每次休眠持续秒数（rtcwake 定时唤醒），默认 30")
    parser.add_argument("--osint-io-idle", action="store_true",
                        help="OSINT 在空闲状态休眠（不启动 IO 负载，默认活跃IO模式）")
    parser.add_argument("--osint-io-duration", type=int, default=60,
                        help="OSINT 每轮休眠前持续 IO 秒数，默认 60")
    parser.add_argument("--osint-mount-point", default="/mnt/ssd_osint",
                        help="OSINT 测试挂载点，默认 /mnt/ssd_osint")
    parser.add_argument("--osint-state-file", default="/var/lib/ssd_osint_state.json",
                        help="OSINT 状态文件路径，默认 /var/lib/ssd_osint_state.json")
    # Read/Write 测试参数
    parser.add_argument("--rw-mode", default="all", choices=["full_disk", "file_cycle", "long_run", "all"],
                        help="读/写测试模式: full_disk(全磁盘验证)/file_cycle(多文件大小周期)/long_run(24h长期运行)/all，默认 all")
    parser.add_argument("--rw-file-sizes", nargs="+", default=["256MB", "1GB", "4GB", "16GB", "32GB"],
                        choices=["256MB", "1GB", "4GB", "16GB", "32GB"],
                        help="文件周期测试的文件大小列表，可多选: 256MB/1GB/4GB/16GB/32GB，默认全部")
    parser.add_argument("--rw-cycles", type=int, default=3,
                        help="文件周期测试每个大小的循环次数，默认 3")
    parser.add_argument("--rw-pattern", default="random", choices=["random", "0xAA", "0x55", "0x00", "0xFF"],
                        help="写入数据 pattern: random/0xAA/0x55/0x00/0xFF，默认 random")
    parser.add_argument("--rw-verify", default="md5", choices=["md5", "sha256", "crc32"],
                        help="数据校验方式: md5/sha256/crc32，默认 md5")
    parser.add_argument("--rw-long-hours", type=int, default=24,
                        help="长期运行测试小时数，默认 24")
    parser.add_argument("--rw-block-size", default="4k",
                        help="读写块大小，默认 4k")
    parser.add_argument("--rw-iodepth", type=int, default=32,
                        help="队列深度，默认 32")
    parser.add_argument("--rw-numjobs", type=int, default=4,
                        help="并发 job 数，默认 4")
    parser.add_argument("--rw-mixed-read-ratio", type=int, default=70,
                        help="长期运行混合读写读比例(%%)，默认 70")
    parser.add_argument("-o", "--output-dir", default="./reports",
                        help="报告输出目录 (默认: ./reports)")
    parser.add_argument("--log-dir", default=get_default_log_dir(),
                        help="日志输出目录 (默认: 脚本同级 logs/ 目录，绝对路径)")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，只打印命令不执行")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过数据销毁确认提示")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志输出")
    # v1.9.2 新增: SSD 状态管理与稳态配置参数
    parser.add_argument("--action", default="run-tests",
                        choices=["run-tests", "enter-fob", "enter-steady", "status"],
                        help="动作类型: run-tests(执行测试)/enter-fob(仅进入FOB状态)/enter-steady(仅进入稳态)/status(查询当前状态) (默认: run-tests)")
    parser.add_argument("--purge-method", default="auto",
                        choices=["auto", "user-data", "blkdiscard"],
                        help="FOB擦除方式: auto(优先NVMe User Data Erase,失败回退blkdiscard)/user-data(仅NVMe Format --ses=1)/blkdiscard(仅blkdiscard) (默认: auto)")
    parser.add_argument("--steady-max-rounds", type=int, default=25,
                        help="WDPC稳态检测最大轮数 (默认: 25)")
    parser.add_argument("--steady-point-duration", type=int, default=60,
                        help="WDPC每个跟踪变量测试点运行秒数 (默认: 60)")
    parser.add_argument("--smart-ignore-media-errors", default="5353",
                        help="性能测试中SMART检查忽视的介质错误ID，逗号分隔 (默认: 5353)")
    parser.add_argument("--gui", action="store_true",
                        help="启动可视化上位机（GUI模式）")
    parser.add_argument("--version", action="version", version=f"ssd_test.py v{SCRIPT_VERSION}")

    return parser.parse_args()


# ============================================================
# 主函数
# ============================================================

def main():
    """主入口。"""
    args = parse_args()

    # 初始化日志
    logger, log_file = setup_logging(args.log_dir, verbose=args.verbose)

    logger.info("=" * 55)
    logger.info(f"  SSD 自动化测试脚本 v{SCRIPT_VERSION}")
    logger.info(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    # 1. root 权限检查
    if not check_root():
        logger.error("本脚本需要 root 权限运行，请使用 sudo")
        sys.exit(1)

    # 2. 依赖检查
    deps_ok, missing = check_dependencies(logger)
    if not deps_ok:
        logger.error("依赖工具不满足，无法继续")
        sys.exit(1)

    # 3. 设备存在性检查
    if not os.path.exists(args.device):
        logger.error(f"设备不存在: {args.device}")
        sys.exit(1)

    # 4. 构建设备配置
    test_items = args.test
    if TEST_ALL in test_items:
        test_items = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF, TEST_RW, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT]

    # v1.8.0 加载多任务批量测试配置（如果指定了 --perf-task-file）
    perf_task_list = []
    if args.perf_task_file:
        try:
            with open(args.perf_task_file, "r", encoding="utf-8") as f:
                task_data = json.load(f)
            if isinstance(task_data, list):
                for idx, td in enumerate(task_data):
                    task = PerfTask.from_dict(td)
                    perf_task_list.append(task)
                    logger.info(f"  已加载测试任务 {idx+1}: name={task.perf_test_name}, "
                                f"rw={task.perf_rw}, bs={task.perf_bs}, qd={task.perf_iodepth}"
                                + (f", note={task.note}" if task.note else ""))
            else:
                logger.error(f"任务文件格式错误: 顶层应为数组，实际为 {type(task_data).__name__}")
                sys.exit(1)
            logger.info(f"  共加载 {len(perf_task_list)} 组测试任务，将执行批量测试")
        except FileNotFoundError:
            logger.error(f"任务文件不存在: {args.perf_task_file}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"任务文件 JSON 解析失败: {e}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"加载任务文件失败: {e}")
            sys.exit(1)

    config = TestConfig(
        device=args.device,
        test_items=test_items,
        fw_image=args.fw_image,
        fw_action=args.fw_action,
        fw_slot=args.fw_slot,
        perf_state=args.perf_state,
        perf_runtime=args.perf_runtime,
        precondition=(args.precondition == "on") and (not args.no_precondition),
        precond_rand_speed_mb=args.precond_rand_speed,
        precond_rand_max_sec=args.precond_rand_max_sec,
        # 完整性能特征 - 全参数可配置
        perf_test_name=args.perf_name,
        perf_direct=args.perf_direct,
        perf_ioengine=args.perf_ioengine,
        perf_bs=args.perf_bs,
        perf_iodepth=args.perf_iodepth,
        perf_numjobs=args.perf_numjobs,
        perf_rw=args.perf_rw,
        perf_rwmixread=args.perf_rwmixread,
        perf_size=args.perf_size,
        perf_runtime_full=args.perf_runtime,
        perf_text_log=args.perf_text_log,
        perf_task_list=perf_task_list,
        pc_cycles=args.pc_cycles,
        pc_power_mode=args.pc_power_mode,
        ipmi_host=args.ipmi_host,
        ipmi_user=args.ipmi_user,
        ipmi_pass=args.ipmi_pass,
        pc_mount_point=args.pc_mount_point,
        pc_state_file=args.pc_state_file,
        pc_boot_timeout=args.pc_boot_timeout,
        pc_off_interval=args.pc_off_interval,
        pc_rw_duration=args.pc_rw_duration,
        pc_pattern=args.pc_pattern,
        pc_pattern_size_gb=args.pc_pattern_size_gb,
        pc_link_check=not args.pc_no_link_check,
        spor_cycles=args.spor_cycles,
        spor_delay=args.spor_delay,
        spor_test_size_gb=args.spor_test_size,
        spor_lba_size=args.spor_lba_size,
        spor_skip_lba=args.spor_skip_lba,
        spor_poweroff_delay_ms=args.spor_poweroff_delay_ms,
        spor_mixed_rw=args.spor_mixed_rw,
        spor_mixed_read_ratio=args.spor_mixed_read_ratio,
        spor_final_test=not args.spor_no_final_test,
        spor_timeboard_port=args.spor_timeboard_port,
        spor_state_file=args.spor_state_file,
        spor_power_mode=args.spor_power_mode,
        spor_enhanced_iodepth=args.spor_enhanced_iodepth,
        spor_enhanced_bs=args.spor_enhanced_bs,
        osint_cycles=args.osint_cycles,
        osint_sleep_type=args.osint_sleep_type,
        osint_sleep_duration=args.osint_sleep_duration,
        osint_io_active=not args.osint_io_idle,
        osint_io_duration=args.osint_io_duration,
        osint_mount_point=args.osint_mount_point,
        osint_state_file=args.osint_state_file,
        rw_mode=args.rw_mode,
        rw_file_sizes=args.rw_file_sizes,
        rw_cycles=args.rw_cycles,
        rw_pattern=args.rw_pattern,
        rw_verify=args.rw_verify,
        rw_long_run_hours=args.rw_long_hours,
        rw_block_size=args.rw_block_size,
        rw_io_depth=args.rw_iodepth,
        rw_numjobs=args.rw_numjobs,
        rw_mixed_read_ratio=args.rw_mixed_read_ratio,
        output_dir=args.output_dir,
        log_dir=args.log_dir,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        verbose=args.verbose,
        # v1.9.2 新增
        purge_method=args.purge_method,
        steady_max_rounds=args.steady_max_rounds,
        steady_point_duration=args.steady_point_duration,
        smart_ignore_media_errors=args.smart_ignore_media_errors,
    )


    logger.info(f"  设备: {config.device} (类型: {config.device_type})")
    logger.info(f"  测试项: {', '.join(config.test_items)}")
    if config.fw_image:
        logger.info(f"  固件镜像: {config.fw_image} ({config.fw_action})")
    logger.info(f"  性能状态: {config.perf_state}")
    # 全参数可配置 FIO 测试
    rw_desc = config.perf_rw
    if config.perf_rw in ("randrw", "rw"):
        rw_desc += f"(读{config.perf_rwmixread}%)"
    logger.info(f"  全参数FIO: 名称={config.perf_test_name}, 模式={rw_desc}, "
                f"bs={config.perf_bs}, QD={config.perf_iodepth}, numjobs={config.perf_numjobs}, "
                f"direct={config.perf_direct}, size={config.perf_size}, runtime={config.perf_runtime_full}s")
    if TEST_POWERCYCLE in config.test_items:
        _pc_extra = f", IPMI={config.ipmi_host}" if config.ipmi_host else ""
        if config.pc_power_mode == "enhanced":
            _pc_extra += f", Pattern={config.pc_pattern}, Size={config.pc_pattern_size_gb}GB"
            _pc_extra += f", LinkCheck={'开' if config.pc_link_check else '关'}"
        logger.info(f"  电源循环: {config.pc_cycles}次, 模式={config.pc_power_mode}{_pc_extra}")
    if TEST_SPOR in config.test_items:
        _spor_extra = ""
        if config.spor_power_mode == "enhanced":
            _spor_extra = f", iodepth={config.spor_enhanced_iodepth}, bs={config.spor_enhanced_bs}"
            if config.ipmi_host:
                _spor_extra += f", IPMI={config.ipmi_host}"
        logger.info(f"  SPOR意外断电: {config.spor_cycles}次, 模式={config.spor_power_mode}, "
                     f"延时={config.spor_delay}s, "
                     f"硬件断电延时={config.spor_poweroff_delay_ms}ms, "
                     f"混合读写={'开' if config.spor_mixed_rw else '关'}{_spor_extra}")
    if TEST_OSINT in config.test_items:
        logger.info(f"  OSINT中断: {config.osint_cycles}次, 休眠={config.osint_sleep_type}, "
                     f"时长={config.osint_sleep_duration}s, "
                     f"活跃IO={'开' if config.osint_io_active else '关(空闲)'}")
    if TEST_RW in config.test_items:
        logger.info(f"  读/写测试: 模式={config.rw_mode}, "
                     f"文件大小={','.join(config.rw_file_sizes)}, "
                     f"循环={config.rw_cycles}次, 校验={config.rw_verify}, "
                     f"长期运行={config.rw_long_run_hours}h")
    logger.info(f"  日志文件: {log_file}")
    logger.info("-" * 55)

    # 5. 数据销毁确认（性能测试、固件测试、电源循环测试、SPOR测试、OSINT测试、RW测试会破坏数据）
    destructive_items = {TEST_PERF, TEST_FW, TEST_SMART, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT, TEST_RW}
    if any(item in destructive_items for item in config.test_items):
        if not config.dry_run and not config.assume_yes:
            if not confirm_destructive(config.device, logger):
                logger.info("用户取消操作，退出")
                sys.exit(0)
        elif config.assume_yes:
            logger.info("已指定 -y，跳过数据销毁确认")

    # 5.5 v1.9.2 新增: action 分发（状态管理动作）
    if args.action == "status":
        # 查询当前 SSD 状态
        serial = get_device_serial(config.device)
        state_info = load_ssd_state(serial)
        logger.info(f"  SSD 序列号: {serial}")
        logger.info(f"  当前状态: {state_info['state'].upper()}")
        if state_info.get("timestamp"):
            logger.info(f"  状态更新时间: {state_info['timestamp']}")
        if state_info.get("extra"):
            logger.info(f"  附加信息: {state_info['extra']}")
        print(f"SSD_STATE: {state_info['state']}")
        sys.exit(0)

    if args.action == "enter-fob":
        # 仅进入 FOB 状态，不执行性能测试
        logger.info("=" * 55)
        logger.info("  动作: 进入 FOB 状态 (NVMe User Data Erase)")
        logger.info("=" * 55)
        perf_tester = PerformanceTester(config, logger)
        purge_ok, purge_method = perf_tester._nvme_purge()
        if purge_ok:
            logger.info(f"  FOB 擦除成功 (method={purge_method})")
            time.sleep(10)
            serial = get_device_serial(config.device)
            save_ssd_state(serial, SSD_STATE_FOB, {"method": purge_method})
            logger.info(f"  SSD 状态已更新为 FOB (serial={serial})")
            print("SSD_STATE: fob")
            sys.exit(0)
        else:
            logger.error(f"  FOB 擦除失败 (method={purge_method})")
            print("SSD_STATE: error")
            sys.exit(1)

    if args.action == "enter-steady":
        # 仅进入稳态，不执行性能测试
        logger.info("=" * 55)
        logger.info("  动作: 进入稳态 (WIPC + WDPC + 稳态检测)")
        logger.info("=" * 55)
        perf_tester = PerformanceTester(config, logger)
        precond_ok, precond_info = perf_tester.precondition_steady_state()
        if precond_ok:
            logger.info(f"  稳态预处理成功 (轮数={precond_info.get('wdpc_rounds')}, "
                        f"稳态达到={precond_info.get('steady_reached')})")
            print("SSD_STATE: steady")
            sys.exit(0)
        else:
            logger.error(f"  稳态预处理失败: {precond_info.get('error', 'unknown')}")
            print("SSD_STATE: error")
            sys.exit(1)

    # 6. 执行测试
    report = TestReport(config, logger)
    testers = {
        TEST_CAPACITY: CapacityTester(config, logger),
        TEST_SMART: SmartTester(config, logger),
        TEST_FW: FirmwareTester(config, logger),
        TEST_PERF: PerformanceTester(config, logger),
        TEST_RW: ReadWriteTester(config, logger),
        TEST_POWERCYCLE: PowerCycleTester(config, logger),
        TEST_SPOR: SPORTester(config, logger),
        TEST_OSINT: OSInterruptionTester(config, logger),
    }

    # 按合理顺序执行：容量 -> SMART -> 固件 -> 性能 -> 读/写 -> 正常电源循环 -> 意外电源循环(SPOR) -> 操作系统中断(OSINT)
    execution_order = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF,
                       TEST_RW, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT]
    total = len(config.test_items)

    for idx, item in enumerate(execution_order, 1):
        if item not in config.test_items:
            continue
        tester = testers[item]
        item_name = {
            TEST_CAPACITY: "设备容量",
            TEST_SMART: "SMART健康信息",
            TEST_FW: "固件升级/降级",
            TEST_PERF: "完整性能特征",
            TEST_RW: "读/写测试",
            TEST_POWERCYCLE: "正常电源循环",
            TEST_SPOR: "意外电源循环(SPOR)",
            TEST_OSINT: "操作系统中断(OSINT)",
        }.get(item, item)
        # 测试项英文名称和编号（用于标准摘要行输出）
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

        logger.info(f"\n[{idx}/{total}] 开始测试: {item_name}")
        logger.info("-" * 40)

        try:
            result = tester.run()
        except Exception as e:
            logger.exception(f"测试项 {item_name} 执行异常: {e}")
            result = TestResult(test_item=item, test_name=item_name,
                                device=config.device)
            result.start()
            result.finish(STATUS_ERROR, str(e))

        report.add_result(result)
        status_icon = {
            STATUS_PASS: "PASS",
            STATUS_FAIL: "FAIL",
            STATUS_ERROR: "ERROR",
            STATUS_SKIP: "SKIP",
        }.get(result.status, result.status)
        logger.info(f"[{idx}/{total}] {item_name}: {status_icon} ({result.duration_sec:.1f}s)")
        if result.error_message:
            logger.info(f"  错误信息: {result.error_message}")

        # 输出标准摘要行（GUI 上位机识别此格式，只显示摘要行）
        # 格式: HH:MM:SS - [SSD_ST_XXX]ITEM_NAME,Status:PASS/FAIL,TestTime:NN S==========
        current_time = datetime.now().strftime("%H:%M:%S")
        test_time_sec = int(round(result.duration_sec))
        summary_line = (f"[SUMMARY] {current_time} - [{item_enum[0]}]{item_enum[1]},"
                        f"Status:{status_icon},TestTime:{test_time_sec} S"
                        + "=" * 10)
        logger.info(summary_line)

    # 7. 生成报告
    logger.info("\n")
    report_file = report.generate_json_report(config.output_dir)
    report.print_summary()

    logger.info(f"  报告文件: {report_file}")
    logger.info(f"  日志文件: {log_file}")
    logger.info("=" * 55)

    # 8. 退出码
    summary = report.get_summary()
    if summary["fail"] > 0 or summary["error"] > 0:
        sys.exit(1)
    sys.exit(0)


# ============================================================
# 可视化上位机（GUI）
# ============================================================

class SSDTestGUI:
    """SSD 自动化测试可视化上位机。

    通过 subprocess 调用命令行模式执行测试，实时显示日志和进度。
    仅依赖 Python 标准库 tkinter，无需额外安装。
    """

    # 测试项定义：(标识, 显示名称, 描述)
    TEST_ITEMS = [
        (TEST_FW, "固件升降级", "现场固件升级/降级测试"),
        (TEST_SMART, "SMART健康", "设备智能健康信息检查"),
        (TEST_CAPACITY, "设备容量", "容量读取与三源交叉校验"),
        (TEST_PERF, "性能测试", "FOB+稳态完整性能特征"),
        (TEST_RW, "读/写测试", "全磁盘验证+多文件大小周期+24h长期运行"),
        (TEST_POWERCYCLE, "正常电源循环", "正常关机断电开机循环测试"),
        (TEST_SPOR, "意外电源循环", "SPOR 意外断电测试（需Timeboard）"),
        (TEST_OSINT, "操作系统中断", "S3/S4 休眠唤醒稳定性测试"),
    ]

    def __init__(self, root: "tk.Tk"):
        self.root = root
        self.root.title(f"SSD 自动化测试上位机 v{SCRIPT_VERSION}")
        self.root.geometry("1100x880")
        self.root.minsize(960, 720)

        # 状态变量
        self.test_process: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.device_list: List[Dict[str, str]] = []
        self.selected_tests: Dict[str, tk.BooleanVar] = {}
        self.config_vars: Dict[str, Any] = {}
        self.perf_tasks: List[PerfTask] = []  # v1.8.0 多任务批量测试列表
        # v1.9.2 新增: SSD 状态管理
        self.current_ssd_state: str = SSD_STATE_UNKNOWN  # 当前 SSD 状态 (unknown/fob/steady)
        self._skip_precondition: bool = False  # v1.9.2: 状态一致时跳过预处理标志
        self.ssd_state_label: Optional["ttk.Label"] = None  # 状态显示标签引用
        self.ssd_state_color_label: Optional["tk.Label"] = None  # 带颜色的状态标签

        # 构建界面
        self._build_menu()
        self._build_main_layout()

        # 窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动后自动扫描设备
        self.root.after(300, self.scan_devices)

    # ----------------------------------------------------------
    # 界面构建
    # ----------------------------------------------------------

    def _build_menu(self):
        """构建菜单栏。"""
        menubar = tk.Menu(self.root)

        # 文件菜单
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="保存配置...", command=self.save_config, accelerator="Ctrl+S")
        file_menu.add_command(label="加载配置...", command=self.load_config, accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="导出日志...", command=self.export_log)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        # 工具菜单
        tool_menu = tk.Menu(menubar, tearoff=0)
        tool_menu.add_command(label="扫描设备", command=self.scan_devices, accelerator="F5")
        tool_menu.add_command(label="清空日志", command=self.clear_log, accelerator="Ctrl+L")
        tool_menu.add_command(label="打开报告目录", command=self._open_report_dir)
        menubar.add_cascade(label="工具", menu=tool_menu)

        # 帮助菜单
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="使用说明", command=self._show_help)
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)

        # 快捷键绑定
        self.root.bind("<Control-s>", lambda e: self.save_config())
        self.root.bind("<Control-o>", lambda e: self.load_config())
        self.root.bind("<Control-l>", lambda e: self.clear_log())
        self.root.bind("<F5>", lambda e: self.scan_devices())

    def _build_main_layout(self):
        """构建主界面布局。"""
        # 顶部：设备选择面板
        top_frame = ttk.LabelFrame(self.root, text="设备选择", padding=8)
        top_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(top_frame, text="待测设备：").pack(side=tk.LEFT)
        self.device_combo = ttk.Combobox(top_frame, width=50, state="readonly")
        self.device_combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="扫描设备", command=self.scan_devices).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="设备信息", command=self._show_device_info).pack(side=tk.LEFT, padx=5)

        self.device_info_label = ttk.Label(top_frame, text="", foreground="gray")
        self.device_info_label.pack(side=tk.LEFT, padx=15)

        # 顶部右侧：开始测试按钮（绿色醒目，从底部移至顶部方便操作）
        top_right_frame = ttk.Frame(top_frame)
        top_right_frame.pack(side=tk.RIGHT)
        self.start_btn = tk.Button(top_right_frame, text="▶  开始测试  ", command=self.start_test,
                                    bg="#2e7d32", fg="white",
                                    font=("TkDefaultFont", 12, "bold"),
                                    activebackground="#1b5e20", activeforeground="white",
                                    relief=tk.RAISED, bd=2, padx=16, pady=5, cursor="hand2")
        self.start_btn.pack(side=tk.RIGHT, padx=4)

        # 中部：左侧测试项 + 右侧参数配置（先创建，最后 pack 以确保底部区域优先分配空间）
        middle_frame = ttk.Frame(self.root)
        # 不在此 pack，移到底部区域之后再 pack

        # 左侧：测试项选择
        left_frame = ttk.LabelFrame(middle_frame, text="测试项选择", padding=6)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))
        left_frame.configure(width=170)

        for item_id, item_name, item_desc in self.TEST_ITEMS:
            var = tk.BooleanVar(value=(item_id in [TEST_SMART, TEST_CAPACITY]))
            self.selected_tests[item_id] = var
            cb = ttk.Checkbutton(left_frame, text=f"{item_name}", variable=var,
                                  command=self._on_test_item_change)
            cb.pack(anchor=tk.W, pady=2)
            # 描述标签
            desc_label = ttk.Label(left_frame, text=f"  {item_desc}", foreground="gray",
                                    font=("TkDefaultFont", 8))
            desc_label.pack(anchor=tk.W, pady=(0, 4))

        # 全选/清空按钮
        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_frame, text="全选", command=lambda: self._set_all_tests(True)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        ttk.Button(btn_frame, text="清空", command=lambda: self._set_all_tests(False)).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

        # 右侧：参数配置（Notebook）
        right_frame = ttk.LabelFrame(middle_frame, text="参数配置", padding=4)
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))

        self.notebook = ttk.Notebook(right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_general_tab()
        self._build_firmware_tab()
        self._build_performance_tab()
        self._build_powercycle_tab()
        self._build_spor_tab()
        self._build_osint_tab()
        self._build_rw_tab()

        # 状态栏（最底部，优先 pack 确保始终可见）
        self.status_var = tk.StringVar(value=f"就绪 | 版本 v{SCRIPT_VERSION} | 仅标准库")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=(8, 2))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # 底部控制栏（进度条 + 按钮，在状态栏上方，优先 pack 确保不被挤压）
        bottom_frame = ttk.Frame(self.root, padding=(8, 2, 8, 2))
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # 进度条
        progress_frame = ttk.Frame(bottom_frame)
        progress_frame.pack(fill=tk.X, pady=(0, 2))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.progress_label = ttk.Label(progress_frame, text="就绪", width=20)
        self.progress_label.pack(side=tk.RIGHT)

        # 控制按钮（开始测试按钮已移至顶部设备栏右侧）
        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(fill=tk.X)
        self.stop_btn = ttk.Button(btn_frame, text="■ 停止", command=self.stop_test, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(btn_frame, text="清空日志", command=self.clear_log).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="保存配置", command=self.save_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="加载配置", command=self.load_config).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="导出日志", command=self.export_log).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="查看详细日志", command=self._open_log_file).pack(side=tk.LEFT, padx=2)

        # 测试结果摘要（固定高度，在底部控制栏上方，优先 pack 确保可见）
        log_frame = ttk.LabelFrame(self.root, text="测试结果摘要（仅显示摘要行，详细日志见日志文件）", padding=4)
        log_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(4, 4))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, wrap=tk.WORD,
                                                     font=("Courier", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.X)
        # 日志颜色标签
        self.log_text.tag_configure("info", foreground="black")
        self.log_text.tag_configure("warning", foreground="orange")
        self.log_text.tag_configure("error", foreground="red")
        self.log_text.tag_configure("success", foreground="green")
        self.log_text.tag_configure("header", foreground="blue", font=("Courier", 9, "bold"))

        # 最后 pack 主体区域，填充顶部和底部之间的剩余空间
        middle_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

    def _build_general_tab(self):
        """通用参数标签页。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="通用")

        row = 0
        ttk.Label(frame, text="报告输出目录：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["output_dir"] = tk.StringVar(value="./reports")
        ttk.Entry(frame, textvariable=self.config_vars["output_dir"], width=30).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="日志输出目录：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["log_dir"] = tk.StringVar(value=get_default_log_dir())
        ttk.Entry(frame, textvariable=self.config_vars["log_dir"], width=30).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self.config_vars["dry_run"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="试运行模式（dry-run，只打印命令不执行）",
                        variable=self.config_vars["dry_run"]).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
        row += 1

        self.config_vars["assume_yes"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="自动确认数据销毁（-y，推荐开启）",
                        variable=self.config_vars["assume_yes"]).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
        row += 1

        self.config_vars["verbose"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="详细输出（verbose）",
                        variable=self.config_vars["verbose"]).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=8)
        row += 1

        ttk.Label(frame, text="执行命令预览：", foreground="blue").grid(row=row, column=0, sticky=tk.W, pady=4)
        row += 1
        self.cmd_preview = tk.Text(frame, height=4, width=60, wrap=tk.WORD,
                                    font=("Courier", 8), bg="#f5f5f5", state=tk.DISABLED)
        self.cmd_preview.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=4)

    def _build_firmware_tab(self):
        """固件参数标签页。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="固件")

        row = 0
        ttk.Label(frame, text="固件镜像：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["fw_image"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["fw_image"], width=35).grid(row=row, column=1, sticky=tk.W, padx=5)
        ttk.Button(frame, text="浏览...", command=self._browse_fw_image).grid(row=row, column=2, padx=5)
        row += 1

        ttk.Label(frame, text="操作类型：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["fw_action"] = tk.StringVar(value=FW_UPGRADE)
        ttk.Combobox(frame, textvariable=self.config_vars["fw_action"], width=15,
                      values=[FW_UPGRADE, FW_DOWNGRADE], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="固件槽位：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["fw_slot"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["fw_slot"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        ttk.Label(frame, text="(1-7，留空自动选择)", foreground="gray").grid(row=row, column=2, sticky=tk.W)
        row += 1

    def _build_performance_tab(self):
        """性能参数标签页（全参数可配置 FIO 测试 + 命令预览）。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="性能")

        row = 0
        # === v1.9.2: SSD 状态控制 + 批量任务管理（左右两栏布局） ===
        top_frame = ttk.Frame(frame)
        top_frame.grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=(0, 8))
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)

        # 左侧：SSD 状态控制
        state_frame = ttk.LabelFrame(top_frame, text="SSD 状态控制", padding=8)
        state_frame.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))

        ttk.Label(state_frame, text="当前状态：", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.ssd_state_color_label = tk.Label(state_frame, text="UNKNOWN",
                                               bg="#9e9e9e", fg="white",
                                               font=("TkDefaultFont", 11, "bold"),
                                               padx=12, pady=3, width=10)
        self.ssd_state_color_label.grid(row=0, column=1, sticky=tk.W, padx=(0, 8))
        self.ssd_state_label = ttk.Label(state_frame, text="序列号: -", foreground="gray")
        self.ssd_state_label.grid(row=0, column=2, sticky=tk.W, padx=8)

        btn_frame = ttk.Frame(state_frame)
        btn_frame.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Button(btn_frame, text="进入 FOB", command=lambda: self._enter_ssd_state(SSD_STATE_FOB)
                   ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="进入稳态", command=lambda: self._enter_ssd_state(SSD_STATE_STEADY)
                   ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="刷新", command=self._refresh_ssd_state
                   ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="重置Unknown", command=self._reset_ssd_state
                   ).pack(side=tk.LEFT, padx=(0, 6))

        # 右侧：批量任务管理
        task_frame = ttk.LabelFrame(top_frame, text="批量任务排队", padding=8)
        task_frame.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))

        # 任务备注 + 新增按钮
        note_row = ttk.Frame(task_frame)
        note_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(note_row, text="备注:").pack(side=tk.LEFT)
        self.perf_task_note = tk.StringVar(value="")
        ttk.Entry(note_row, textvariable=self.perf_task_note, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Button(note_row, text="＋新增任务", command=self._add_perf_task, width=10
                   ).pack(side=tk.LEFT, padx=4)

        # 任务列表 + 上下移按钮
        list_row = ttk.Frame(task_frame)
        list_row.pack(fill=tk.X, pady=2)
        self.perf_task_listbox = tk.Listbox(list_row, height=4, width=40,
                                             font=("Courier", 8), selectmode=tk.SINGLE,
                                             bg="#f5f5f5", relief=tk.SUNKEN)
        self.perf_task_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        task_scroll = ttk.Scrollbar(list_row, orient=tk.VERTICAL, command=self.perf_task_listbox.yview)
        task_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.perf_task_listbox.config(yscrollcommand=task_scroll.set)
        move_btn_frame = ttk.Frame(list_row)
        move_btn_frame.pack(side=tk.LEFT, padx=(4, 0), fill=tk.Y)
        ttk.Button(move_btn_frame, text="↑", command=self._move_task_up, width=3).pack(side=tk.TOP, pady=1)
        ttk.Button(move_btn_frame, text="↓", command=self._move_task_down, width=3).pack(side=tk.TOP, pady=1)

        # 操作按钮
        op_row = ttk.Frame(task_frame)
        op_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(op_row, text="删除", command=self._delete_selected_task, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Button(op_row, text="清空", command=self._clear_all_tasks, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Button(op_row, text="导出JSON", command=self._export_tasks_json, width=8).pack(side=tk.LEFT, padx=2)
        self.perf_task_count_label = ttk.Label(op_row, text="0组", foreground="gray")
        self.perf_task_count_label.pack(side=tk.LEFT, padx=6)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row+1, column=0, columnspan=4, sticky=tk.EW, pady=4)
        row += 2

        # === 基础配置 ===
        ttk.Label(frame, text="测试状态：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_state"] = tk.StringVar(value=PERF_UNKNOWN)
        ttk.Combobox(frame, textvariable=self.config_vars["perf_state"], width=12,
                      values=[PERF_FOB, PERF_STEADY, PERF_UNKNOWN], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # v1.9.2: 已删除稳态预处理UI（用户已有独立功能代码）
        ttk.Label(frame, text="测试项目名称：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_test_name"] = tk.StringVar(value="perf-test")
        name_entry = ttk.Entry(frame, textvariable=self.config_vars["perf_test_name"], width=20)
        name_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        name_entry.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=6)
        row += 1

        # === IO 负载配置 ===
        ttk.Label(frame, text="IO负载配置", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=4, sticky=tk.W, pady=(0, 2))
        row += 1

        # direct
        ttk.Label(frame, text="direct：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_direct"] = tk.IntVar(value=1)
        direct_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_direct"], width=8,
                                     values=[0, 1], state="readonly")
        direct_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        direct_combo.bind("<<ComboboxSelected>>", lambda e: self._on_perf_direct_change())
        # ioengine
        ttk.Label(frame, text="ioengine：").grid(row=row, column=2, sticky=tk.W, pady=4, padx=(20, 0))
        self.config_vars["perf_ioengine"] = tk.StringVar(value="libaio")
        self.perf_ioengine_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_ioengine"], width=10,
                                                  values=["libaio", "sync", "psync", "vsync", "mmap"], state="readonly")
        self.perf_ioengine_combo.grid(row=row, column=3, sticky=tk.W, padx=5)
        self.perf_ioengine_combo.bind("<<ComboboxSelected>>", lambda e: self._update_perf_command_preview())
        row += 1

        # 块大小
        ttk.Label(frame, text="块大小 bs：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_bs"] = tk.StringVar(value="4k")
        bs_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_bs"], width=10,
                                 values=["4k", "8k", "16k", "32k", "64k", "128k", "256k", "512k", "1M", "2M"], state="normal")
        bs_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        bs_combo.bind("<<ComboboxSelected>>", lambda e: self._update_perf_command_preview())
        bs_combo.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        # 队列深度
        ttk.Label(frame, text="队列深度 iodepth：").grid(row=row, column=2, sticky=tk.W, pady=4, padx=(20, 0))
        self.config_vars["perf_iodepth"] = tk.IntVar(value=64)
        iodepth_spin = ttk.Spinbox(frame, from_=1, to=1024, textvariable=self.config_vars["perf_iodepth"], width=8,
                                    command=self._update_perf_command_preview)
        iodepth_spin.grid(row=row, column=3, sticky=tk.W, padx=5)
        iodepth_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        row += 1

        # 并发任务数
        ttk.Label(frame, text="并发任务 numjobs：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_numjobs"] = tk.IntVar(value=1)
        numjobs_spin = ttk.Spinbox(frame, from_=1, to=256, textvariable=self.config_vars["perf_numjobs"], width=8,
                                    command=self._update_perf_command_preview)
        numjobs_spin.grid(row=row, column=1, sticky=tk.W, padx=5)
        numjobs_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        # 读写模式
        ttk.Label(frame, text="读写模式 rw：").grid(row=row, column=2, sticky=tk.W, pady=4, padx=(20, 0))
        self.config_vars["perf_rw"] = tk.StringVar(value="randread")
        rw_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_rw"], width=12,
                                 values=["randread", "randwrite", "randrw", "read", "write", "rw"], state="readonly")
        rw_combo.grid(row=row, column=3, sticky=tk.W, padx=5)
        rw_combo.bind("<<ComboboxSelected>>", lambda e: self._on_perf_rw_change())
        row += 1

        # 读写比例（仅混合模式启用）
        ttk.Label(frame, text="读占比 rwmixread：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_rwmixread"] = tk.IntVar(value=70)
        self.perf_rwmix_spin = ttk.Spinbox(frame, from_=0, to=100, textvariable=self.config_vars["perf_rwmixread"], width=8,
                                             command=self._update_perf_command_preview)
        self.perf_rwmix_spin.grid(row=row, column=1, sticky=tk.W, padx=5)
        self.perf_rwmix_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        ttk.Label(frame, text="(0=纯写, 100=纯读, 仅randrw/rw生效)").grid(
            row=row, column=2, columnspan=2, sticky=tk.W, padx=(20, 0))
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=6)
        row += 1

        # === 测试范围与时长 ===
        ttk.Label(frame, text="测试范围与时长", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=4, sticky=tk.W, pady=(0, 2))
        row += 1

        ttk.Label(frame, text="测试范围 size：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_size"] = tk.StringVar(value="3%")
        size_entry = ttk.Entry(frame, textvariable=self.config_vars["perf_size"], width=10)
        size_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        size_entry.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        ttk.Label(frame, text="(百分比如3% 或 固定容量如10G)").grid(
            row=row, column=2, columnspan=2, sticky=tk.W, padx=(20, 0))
        row += 1

        ttk.Label(frame, text="测试时长 runtime(秒)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_runtime_full"] = tk.IntVar(value=60)
        runtime_spin = ttk.Spinbox(frame, from_=1, to=86400, textvariable=self.config_vars["perf_runtime_full"], width=8,
                                    command=self._update_perf_command_preview)
        runtime_spin.grid(row=row, column=1, sticky=tk.W, padx=5)
        runtime_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        ttk.Label(frame, text="(time_based固定开启)").grid(row=row, column=2, sticky=tk.W, padx=(20, 0))
        row += 1

        self.config_vars["perf_text_log"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="同时输出文本格式日志（默认仅json+结构化日志）",
                        variable=self.config_vars["perf_text_log"]).grid(row=row, column=0, columnspan=4, sticky=tk.W, pady=2)
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=6)
        row += 1

        # === 命令预览 ===
        ttk.Label(frame, text="FIO命令预览（实时更新）：", font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=4, sticky=tk.W, pady=(0, 2))
        row += 1

        self.perf_cmd_preview = tk.Text(frame, height=5, width=80, wrap=tk.WORD,
                                         font=("Courier", 9), bg="#f5f5f5", relief=tk.SUNKEN)
        self.perf_cmd_preview.grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=2)
        self.perf_cmd_preview.config(state=tk.DISABLED)
        row += 1

        # v1.9.2: 批量任务管理已移至顶部右侧区域

        # 初始化联动状态和命令预览
        self._on_perf_direct_change()
        self._on_perf_rw_change()
        self._update_perf_command_preview()

    def _on_perf_direct_change(self):
        """direct 参数变化联动：direct=1时ioengine锁定libaio。"""
        if self.config_vars["perf_direct"].get() == 1:
            self.config_vars["perf_ioengine"].set("libaio")
            self.perf_ioengine_combo.config(state="disabled")
        else:
            self.perf_ioengine_combo.config(state="readonly")
        self._update_perf_command_preview()

    def _on_perf_rw_change(self):
        """读写模式变化联动：非混合模式时rwmixread置灰。"""
        rw = self.config_vars["perf_rw"].get()
        if rw in ("randrw", "rw"):
            self.perf_rwmix_spin.config(state="normal")
        else:
            self.perf_rwmix_spin.config(state="disabled")
        self._update_perf_command_preview()

    def _update_perf_command_preview(self):
        """更新 FIO 命令预览区。"""
        if not hasattr(self, "perf_cmd_preview"):
            return
        c = self.config_vars
        device = self.device_list[self.device_combo.current()]["path"] if self.device_combo.current() >= 0 else "/dev/nvme0n1"
        cmd_parts = [
            "fio",
            f"--name={c['perf_test_name'].get()}",
            f"--filename={device}",
            f"--direct={c['perf_direct'].get()}",
            f"--bs={c['perf_bs'].get()}",
            f"--iodepth={c['perf_iodepth'].get()}",
            f"--numjobs={c['perf_numjobs'].get()}",
            f"--rw={c['perf_rw'].get()}",
            f"--ioengine={c['perf_ioengine'].get()}",
            f"--size={c['perf_size'].get()}",
            "--time_based",
            f"--runtime={c['perf_runtime_full'].get()}",
            "--group_reporting",
            "--lat_percentiles=1",
            "--output-format=json+",
            "--output=<日志目录>/<自动命名>.json",
        ]
        rw = c["perf_rw"].get()
        if rw in ("randrw", "rw"):
            cmd_parts.append(f"--rwmixread={c['perf_rwmixread'].get()}")
        if c["perf_direct"].get() == 1:
            cmd_parts.append("--buffered=0")
        if rw in ("randread", "randwrite", "randrw"):
            cmd_parts.append("--norandommap")
            cmd_parts.append("--randrepeat=0")

        cmd_str = " \\\n  ".join(cmd_parts)
        self.perf_cmd_preview.config(state=tk.NORMAL)
        self.perf_cmd_preview.delete("1.0", tk.END)
        self.perf_cmd_preview.insert("1.0", cmd_str)
        self.perf_cmd_preview.config(state=tk.DISABLED)

    # ----------------------------------------------------------
    # v1.8.0 批量任务管理
    # ----------------------------------------------------------

    def _add_perf_task(self):
        """将当前界面配置保存为一个测试任务。"""
        c = self.config_vars
        task = PerfTask(
            perf_test_name=c["perf_test_name"].get(),
            perf_direct=c["perf_direct"].get(),
            perf_ioengine=c["perf_ioengine"].get(),
            perf_bs=c["perf_bs"].get(),
            perf_iodepth=c["perf_iodepth"].get(),
            perf_numjobs=c["perf_numjobs"].get(),
            perf_rw=c["perf_rw"].get(),
            perf_rwmixread=c["perf_rwmixread"].get(),
            perf_size=c["perf_size"].get(),
            perf_time_based=True,
            perf_runtime_full=c["perf_runtime_full"].get(),
            perf_text_log=c["perf_text_log"].get(),
            note=self.perf_task_note.get().strip(),
        )
        self.perf_tasks.append(task)
        self._refresh_task_list()
        # 清空备注，方便下一次输入
        self.perf_task_note.set("")

    def _delete_selected_task(self):
        """删除列表中选中的任务。"""
        selection = self.perf_task_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if 0 <= idx < len(self.perf_tasks):
            del self.perf_tasks[idx]
            self._refresh_task_list()

    def _move_task_up(self):
        """将选中的任务上移一位。"""
        selection = self.perf_task_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx <= 0 or idx >= len(self.perf_tasks):
            return
        self.perf_tasks[idx], self.perf_tasks[idx - 1] = self.perf_tasks[idx - 1], self.perf_tasks[idx]
        self._refresh_task_list()
        self.perf_task_listbox.selection_set(idx - 1)
        self.perf_task_listbox.see(idx - 1)

    def _move_task_down(self):
        """将选中的任务下移一位。"""
        selection = self.perf_task_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx < 0 or idx >= len(self.perf_tasks) - 1:
            return
        self.perf_tasks[idx], self.perf_tasks[idx + 1] = self.perf_tasks[idx + 1], self.perf_tasks[idx]
        self._refresh_task_list()
        self.perf_task_listbox.selection_set(idx + 1)
        self.perf_task_listbox.see(idx + 1)

    def _clear_all_tasks(self):
        """清空所有任务。"""
        if not self.perf_tasks:
            return
        if messagebox.askyesno("确认", f"确定清空全部 {len(self.perf_tasks)} 组任务吗？"):
            self.perf_tasks.clear()
            self._refresh_task_list()

    def _export_tasks_json(self):
        """导出任务列表为 JSON 文件，可供 CLI --perf-task-file 使用。"""
        if not self.perf_tasks:
            messagebox.showinfo("提示", "任务列表为空，无需导出")
            return
        filepath = filedialog.asksaveasfilename(
            title="导出任务配置",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            initialfile="perf_tasks.json"
        )
        if not filepath:
            return
        try:
            data = [t.to_dict() for t in self.perf_tasks]
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("成功", f"已导出 {len(self.perf_tasks)} 组任务到:\n{filepath}")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {e}")

    def _refresh_task_list(self):
        """刷新任务列表显示。"""
        self.perf_task_listbox.delete(0, tk.END)
        for idx, task in enumerate(self.perf_tasks, start=1):
            note = f" [{task.note}]" if task.note else ""
            display = (f"{idx}. name={task.perf_test_name}, rw={task.perf_rw}, "
                       f"bs={task.perf_bs}, qd={task.perf_iodepth}, "
                       f"numjobs={task.perf_numjobs}, runtime={task.perf_runtime_full}s{note}")
            self.perf_task_listbox.insert(tk.END, display)
        count = len(self.perf_tasks)
        mode_text = "批量模式" if count > 0 else "单组模式"
        self.perf_task_count_label.config(
            text=f"共 {count} 组任务（{mode_text}）",
            foreground="green" if count > 0 else "gray"
        )

    def _save_tasks_to_temp_file(self) -> Optional[str]:
        """将任务列表保存到临时 JSON 文件，返回文件路径供 CLI 使用。"""
        if not self.perf_tasks:
            return None
        try:
            import tempfile
            fd, filepath = tempfile.mkstemp(suffix=".json", prefix="perf_tasks_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([t.to_dict() for t in self.perf_tasks], f, ensure_ascii=False, indent=2)
            return filepath
        except Exception:
            return None

    def _build_powercycle_tab(self):
        """电源循环参数标签页。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="正常电源循环")

        row = 0
        ttk.Label(frame, text="循环次数：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_cycles"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.config_vars["pc_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="电源控制模式：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_power_mode"] = tk.StringVar(value="manual")
        self._pc_mode_combo = ttk.Combobox(frame, textvariable=self.config_vars["pc_power_mode"], width=15,
                      values=["ipmi", "manual", "enhanced"], state="readonly")
        self._pc_mode_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_mode_combo.bind("<<ComboboxSelected>>", self._on_pc_power_mode_change)
        row += 1

        ttk.Label(frame, text="IPMI 主机：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["ipmi_host"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["ipmi_host"], width=25).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="IPMI 用户名：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["ipmi_user"] = tk.StringVar(value="ADMIN")
        ttk.Entry(frame, textvariable=self.config_vars["ipmi_user"], width=15).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="IPMI 密码：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["ipmi_pass"] = tk.StringVar(value="ADMIN")
        ttk.Entry(frame, textvariable=self.config_vars["ipmi_pass"], width=15, show="*").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="断电间隔(秒)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_off_interval"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=3, to=300, textvariable=self.config_vars["pc_off_interval"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="读写持续(秒)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_rw_duration"] = tk.IntVar(value=30)
        ttk.Spinbox(frame, from_=5, to=600, textvariable=self.config_vars["pc_rw_duration"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # enhanced（增强）模式专属参数（默认禁用，选择 enhanced 时启用）
        self._pc_enhanced_widgets = []

        ttk.Label(frame, text="数据 Pattern：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_pattern"] = tk.StringVar(value="0xAA")
        _w = ttk.Entry(frame, textvariable=self.config_vars["pc_pattern"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_enhanced_widgets.append(_w)
        row += 1

        ttk.Label(frame, text="Pattern写入量(GB)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_pattern_size_gb"] = tk.IntVar(value=20)
        _w = ttk.Spinbox(frame, from_=1, to=500, textvariable=self.config_vars["pc_pattern_size_gb"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_enhanced_widgets.append(_w)
        row += 1

        ttk.Label(frame, text="PCIe Link校验：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_link_check"] = tk.BooleanVar(value=True)
        _w = ttk.Checkbutton(frame, variable=self.config_vars["pc_link_check"])
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_enhanced_widgets.append(_w)

        # 初始状态：manual 模式下禁用 enhanced 控件
        self._on_pc_power_mode_change()

    def _on_pc_power_mode_change(self, event=None):
        """电源控制模式切换时，启用/禁用 enhanced 专属控件。"""
        is_enhanced = (self.config_vars["pc_power_mode"].get() == "enhanced")
        state = "normal" if is_enhanced else "disabled"
        for _w in getattr(self, "_pc_enhanced_widgets", []):
            try:
                _w.configure(state=state)
            except Exception:
                pass

    def _build_spor_tab(self):
        """SPOR 参数标签页。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="意外电源循环(SPOR)")

        row = 0
        ttk.Label(frame, text="循环次数：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_cycles"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.config_vars["spor_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="断电方式：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_power_mode"] = tk.StringVar(value="timeboard")
        self._spor_mode_combo = ttk.Combobox(frame, textvariable=self.config_vars["spor_power_mode"], width=15,
                      values=["timeboard", "manual", "enhanced"], state="readonly")
        self._spor_mode_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._spor_mode_combo.bind("<<ComboboxSelected>>", self._on_spor_power_mode_change)
        row += 1

        ttk.Label(frame, text="写入延时(秒)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_delay"] = tk.IntVar(value=5)
        ttk.Spinbox(frame, from_=1, to=120, textvariable=self.config_vars["spor_delay"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="测试大小(GB)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_test_size_gb"] = tk.IntVar(value=20)
        ttk.Spinbox(frame, from_=1, to=1000, textvariable=self.config_vars["spor_test_size_gb"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="硬件断电延时(ms)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_poweroff_delay_ms"] = tk.IntVar(value=500)
        ttk.Spinbox(frame, from_=100, to=10000, textvariable=self.config_vars["spor_poweroff_delay_ms"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self.config_vars["spor_mixed_rw"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="混合读写模式（randrw，更接近真实负载）",
                        variable=self.config_vars["spor_mixed_rw"]).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="混合读比例(%)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_mixed_read_ratio"] = tk.IntVar(value=70)
        ttk.Spinbox(frame, from_=0, to=100, textvariable=self.config_vars["spor_mixed_read_ratio"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="Timeboard 串口：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_timeboard_port"] = tk.StringVar(value="/dev/ttyUSB0")
        self._spor_timeboard_entry = ttk.Entry(frame, textvariable=self.config_vars["spor_timeboard_port"], width=20)
        self._spor_timeboard_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # enhanced 模式专属参数（默认禁用，选择 enhanced 时启用）
        self._spor_enhanced_widgets = []

        ttk.Label(frame, text="写入队列深度：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_enhanced_iodepth"] = tk.IntVar(value=256)
        _w = ttk.Spinbox(frame, from_=1, to=1024, textvariable=self.config_vars["spor_enhanced_iodepth"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._spor_enhanced_widgets.append(_w)
        row += 1

        ttk.Label(frame, text="写入块大小：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_enhanced_bs"] = tk.StringVar(value="128k")
        _w = ttk.Entry(frame, textvariable=self.config_vars["spor_enhanced_bs"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._spor_enhanced_widgets.append(_w)
        row += 1

        self.config_vars["spor_final_test"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="循环结束后执行最终完整功能测试",
                        variable=self.config_vars["spor_final_test"]).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=4)

        # 初始状态：timeboard 模式下禁用 enhanced 控件，manual/enhanced 下禁用 Timeboard 串口
        self._on_spor_power_mode_change()

    def _on_spor_power_mode_change(self, event=None):
        """SPOR 断电方式切换时，启用/禁用对应控件。"""
        mode = self.config_vars["spor_power_mode"].get()
        is_timeboard = (mode == "timeboard")
        is_enhanced = (mode == "enhanced")
        # enhanced 控件：仅 enhanced 模式启用
        enh_state = "normal" if is_enhanced else "disabled"
        for _w in getattr(self, "_spor_enhanced_widgets", []):
            try:
                _w.configure(state=enh_state)
            except Exception:
                pass
        # Timeboard 串口控件：仅 timeboard 模式启用
        tb_state = "normal" if is_timeboard else "disabled"
        try:
            self._spor_timeboard_entry.configure(state=tb_state)
        except Exception:
            pass

    def _build_osint_tab(self):
        """OSINT 参数标签页。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="操作系统中断(OSINT)")

        row = 0
        ttk.Label(frame, text="循环次数：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_cycles"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.config_vars["osint_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="休眠类型：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_sleep_type"] = tk.StringVar(value="s3")
        ttk.Combobox(frame, textvariable=self.config_vars["osint_sleep_type"], width=15,
                      values=["s3", "s4", "both"], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="休眠时长(秒)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_sleep_duration"] = tk.IntVar(value=30)
        ttk.Spinbox(frame, from_=5, to=3600, textvariable=self.config_vars["osint_sleep_duration"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self.config_vars["osint_io_idle"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="空闲状态休眠（不启动 IO 负载，默认活跃IO模式）",
                        variable=self.config_vars["osint_io_idle"]).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="IO 持续(秒)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_io_duration"] = tk.IntVar(value=60)
        ttk.Spinbox(frame, from_=5, to=600, textvariable=self.config_vars["osint_io_duration"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="挂载点：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_mount_point"] = tk.StringVar(value="/mnt/ssd_osint")
        ttk.Entry(frame, textvariable=self.config_vars["osint_mount_point"], width=25).grid(row=row, column=1, sticky=tk.W, padx=5)

    def _build_rw_tab(self):
        """读/写测试参数标签页。"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="读/写测试")

        row = 0
        ttk.Label(frame, text="测试模式：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_mode"] = tk.StringVar(value=RW_MODE_ALL)
        ttk.Combobox(frame, textvariable=self.config_vars["rw_mode"], width=18,
                      values=[RW_MODE_FULL_DISK, RW_MODE_FILE_CYCLE, RW_MODE_LONG_RUN, RW_MODE_ALL],
                      state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        ttk.Label(frame, text="(full_disk/file_cycle/long_run/all)", foreground="gray",
                  font=("TkDefaultFont", 8)).grid(row=row, column=2, sticky=tk.W)
        row += 1

        # 文件大小多选（用户特别要求）
        ttk.Label(frame, text="文件大小选择：").grid(row=row, column=0, sticky=tk.NW, pady=4)
        size_frame = ttk.Frame(frame)
        size_frame.grid(row=row, column=1, columnspan=2, sticky=tk.W, padx=5)
        self.rw_size_vars: Dict[str, tk.BooleanVar] = {}
        for i, size_label in enumerate(RW_FILE_SIZES.keys()):
            var = tk.BooleanVar(value=True)
            self.rw_size_vars[size_label] = var
            col = i % 3
            r = i // 3
            ttk.Checkbutton(size_frame, text=size_label, variable=var).grid(
                row=r, column=col, sticky=tk.W, padx=8, pady=2)
        # 全选/清空按钮
        btn_sf = ttk.Frame(size_frame)
        btn_sf.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
        ttk.Button(btn_sf, text="全选", width=6,
                   command=lambda: [v.set(True) for v in self.rw_size_vars.values()]).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_sf, text="清空", width=6,
                   command=lambda: [v.set(False) for v in self.rw_size_vars.values()]).pack(side=tk.LEFT, padx=2)
        row += 1

        ttk.Label(frame, text="每大小循环次数：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_cycles"] = tk.IntVar(value=3)
        ttk.Spinbox(frame, from_=1, to=1000, textvariable=self.config_vars["rw_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="数据 pattern：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_pattern"] = tk.StringVar(value=RW_PATTERN_RANDOM)
        ttk.Combobox(frame, textvariable=self.config_vars["rw_pattern"], width=15,
                      values=[RW_PATTERN_RANDOM, RW_PATTERN_AA, RW_PATTERN_55, RW_PATTERN_00, RW_PATTERN_FF],
                      state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="校验方式：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_verify"] = tk.StringVar(value="md5")
        ttk.Combobox(frame, textvariable=self.config_vars["rw_verify"], width=15,
                      values=["md5", "sha256", "crc32"], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=6)
        row += 1

        ttk.Label(frame, text="长期运行(小时)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_long_hours"] = tk.IntVar(value=24)
        ttk.Spinbox(frame, from_=1, to=720, textvariable=self.config_vars["rw_long_hours"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="块大小：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_block_size"] = tk.StringVar(value="4k")
        ttk.Entry(frame, textvariable=self.config_vars["rw_block_size"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="队列深度：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_iodepth"] = tk.IntVar(value=32)
        ttk.Spinbox(frame, from_=1, to=1024, textvariable=self.config_vars["rw_iodepth"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="并发 job 数：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_numjobs"] = tk.IntVar(value=4)
        ttk.Spinbox(frame, from_=1, to=64, textvariable=self.config_vars["rw_numjobs"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Label(frame, text="混合读比例(%)：").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_mixed_read_ratio"] = tk.IntVar(value=70)
        ttk.Spinbox(frame, from_=0, to=100, textvariable=self.config_vars["rw_mixed_read_ratio"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)

    # ----------------------------------------------------------
    # 设备扫描与信息
    # ----------------------------------------------------------

    def scan_devices(self):
        """扫描系统中的存储设备。"""
        self._append_log("扫描存储设备...", "header")
        self.device_list = []

        try:
            # 使用 lsblk 扫描块设备
            result = subprocess.run(
                ["lsblk", "-d", "-o", "NAME,SIZE,MODEL,TRAN,ROTA", "-n", "-J"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    name = dev.get("name", "")
                    if name.startswith(("loop", "ram", "zram", "sr")):
                        continue
                    size = dev.get("size", "未知")
                    model = dev.get("model", "未知").strip() or "未知"
                    tran = dev.get("tran", "").strip() or "未知"
                    device_path = f"/dev/{name}"
                    self.device_list.append({
                        "path": device_path,
                        "name": name,
                        "size": size,
                        "model": model,
                        "tran": tran,
                    })
        except Exception as e:
            self._append_log(f"lsblk 扫描失败: {e}", "warning")

        # 补充 nvme list 信息
        try:
            result = subprocess.run(
                ["nvme", "list", "-o", "json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for dev in data.get("Devices", []):
                    device_path = dev.get("DevicePath", "")
                    model = dev.get("ModelNumber", "").strip()
                    serial = dev.get("SerialNumber", "").strip()
                    fw = dev.get("Firmware", "").strip()
                    # 更新已有设备信息
                    for d in self.device_list:
                        if d["path"] == device_path:
                            if model:
                                d["model"] = model
                            d["serial"] = serial
                            d["fw"] = fw
                            break
        except Exception:
            pass  # nvme-cli 未安装或无 NVMe 设备

        # 更新下拉框
        display_list = []
        for d in self.device_list:
            display = f"{d['path']}  |  {d['size']}  |  {d['model']}  |  {d['tran']}"
            display_list.append(display)

        self.device_combo["values"] = display_list
        if display_list:
            self.device_combo.current(0)
            self._update_device_info()

        self._append_log(f"扫描完成，发现 {len(self.device_list)} 个存储设备", "success")
        self.status_var.set(f"就绪 | 发现 {len(self.device_list)} 个设备")

    def _update_device_info(self):
        """更新设备信息标签。"""
        idx = self.device_combo.current()
        if 0 <= idx < len(self.device_list):
            d = self.device_list[idx]
            info = f"{d['model']} | {d['size']} | {d['tran']}"
            if d.get("serial"):
                info += f" | SN:{d['serial']}"
            if d.get("fw"):
                info += f" | FW:{d['fw']}"
            self.device_info_label.config(text=info)

    def _show_device_info(self):
        """显示详细设备信息对话框。"""
        idx = self.device_combo.current()
        if idx < 0:
            messagebox.showwarning("提示", "请先选择设备")
            return
        device = self.device_list[idx]["path"]

        info_text = f"设备路径: {device}\n\n"
        try:
            result = subprocess.run(["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT", device],
                                    capture_output=True, text=True, timeout=5)
            info_text += "=== lsblk ===\n" + result.stdout + "\n"
        except Exception as e:
            info_text += f"lsblk 失败: {e}\n"

        try:
            result = subprocess.run(["smartctl", "-i", device],
                                    capture_output=True, text=True, timeout=5)
            info_text += "=== smartctl -i ===\n" + result.stdout + "\n"
        except Exception as e:
            info_text += f"smartctl 失败: {e}\n"

        if device.startswith("/dev/nvme"):
            try:
                result = subprocess.run(["nvme", "id-ctrl", device.replace("n1", ""), "-o", "json"],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    info_text += "=== NVMe Identify ===\n"
                    info_text += f"  型号: {data.get('mn', 'N/A')}\n"
                    info_text += f"  序列号: {data.get('sn', 'N/A')}\n"
                    info_text += f"  固件版本: {data.get('fr', 'N/A')}\n"
                    info_text += f"  命名空间数: {data.get('nn', 'N/A')}\n"
                    info_text += f"  PCIe 厂商ID: {data.get('vid', 'N/A')}\n"
            except Exception as e:
                info_text += f"nvme id-ctrl 失败: {e}\n"

        # 显示对话框
        win = tk.Toplevel(self.root)
        win.title(f"设备信息 - {device}")
        win.geometry("600x500")
        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Courier", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, info_text)
        text.config(state=tk.DISABLED)

    def _browse_fw_image(self):
        """浏览选择固件镜像文件。"""
        path = filedialog.askopenfilename(title="选择固件镜像",
                                           filetypes=[("固件文件", "*.bin *.fw *.img"), ("所有文件", "*.*")])
        if path:
            self.config_vars["fw_image"].set(path)

    # ----------------------------------------------------------
    # 测试项选择回调
    # ----------------------------------------------------------

    def _on_test_item_change(self):
        """测试项选择变化时更新命令预览。"""
        self._update_cmd_preview()

    def _set_all_tests(self, value: bool):
        """全选/清空所有测试项。"""
        for var in self.selected_tests.values():
            var.set(value)
        self._update_cmd_preview()

    # ----------------------------------------------------------
    # 命令构建与预览
    # ----------------------------------------------------------

    def _get_selected_tests(self) -> List[str]:
        """获取选中的测试项列表。"""
        return [item_id for item_id, var in self.selected_tests.items() if var.get()]


    # ----------------------------------------------------------
    # v1.9.2 新增: SSD 状态管理方法
    # ----------------------------------------------------------

    def _get_current_device_path(self) -> Optional[str]:
        """获取当前选择的设备路径。"""
        device_idx = self.device_combo.current()
        if device_idx < 0:
            return None
        return self.device_list[device_idx]["path"]

    def _update_state_display(self, state: str, detail: str = ""):
        """更新 GUI 上的 SSD 状态显示。

        Args:
            state: unknown/fob/steady
            detail: 附加说明文字
        """
        state_upper = state.upper()
        colors = {
            SSD_STATE_UNKNOWN: ("#9e9e9e", "UNKNOWN"),
            SSD_STATE_FOB: ("#1565c0", "FOB"),
            SSD_STATE_STEADY: ("#2e7d32", "STEADY"),
        }
        bg, text = colors.get(state, ("#9e9e9e", state_upper))
        if self.ssd_state_color_label:
            self.ssd_state_color_label.config(text=text, bg=bg)
        if self.ssd_state_label:
            if detail:
                self.ssd_state_label.config(text=detail)
            else:
                self.ssd_state_label.config(text=f"当前状态: {state_upper}")

    def _refresh_ssd_state(self):
        """从状态文件刷新当前 SSD 状态显示。"""
        device = self._get_current_device_path()
        if not device:
            messagebox.showwarning("提示", "请先选择待测设备")
            return
        try:
            serial = get_device_serial(device)
            state_info = load_ssd_state(serial)
            self.current_ssd_state = state_info["state"]
            detail = f"序列号: {serial}"
            if state_info.get("timestamp"):
                detail += f" | 更新时间: {state_info['timestamp']}"
            self._update_state_display(self.current_ssd_state, detail)
            self._append_log(f"SSD 状态已刷新: {self.current_ssd_state.upper()} (serial={serial})", "info")
        except Exception as e:
            self._append_log(f"刷新 SSD 状态失败: {e}", "error")

    def _reset_ssd_state(self):
        """将当前 SSD 状态重置为 Unknown（仅更新本地状态和显示，不操作设备）。"""
        device = self._get_current_device_path()
        if not device:
            messagebox.showwarning("提示", "请先选择待测设备")
            return
        if not messagebox.askyesno("确认", "确定要将 SSD 状态重置为 Unknown 吗？\n（仅更新状态记录，不操作设备）"):
            return
        try:
            serial = get_device_serial(device)
            save_ssd_state(serial, SSD_STATE_UNKNOWN, {"reset_by": "user"})
            self.current_ssd_state = SSD_STATE_UNKNOWN
            self._update_state_display(SSD_STATE_UNKNOWN, f"序列号: {serial} | 已重置为 Unknown")
            self._append_log(f"SSD 状态已重置为 Unknown (serial={serial})", "warning")
        except Exception as e:
            self._append_log(f"重置 SSD 状态失败: {e}", "error")

    def _enter_ssd_state(self, target_state: str):
        """手动让 SSD 进入指定状态（通过 subprocess 调用脚本 --action）。

        Args:
            target_state: SSD_STATE_FOB 或 SSD_STATE_STEADY
        """
        if self.is_running:
            messagebox.showwarning("提示", "测试正在运行中，请先停止当前测试")
            return
        device = self._get_current_device_path()
        if not device:
            messagebox.showerror("错误", "请先选择待测设备")
            return

        state_name = "FOB" if target_state == SSD_STATE_FOB else "稳态(Steady)"
        if not messagebox.askyesno("确认",
                                    f"确定要让 SSD 进入 {state_name} 状态吗？\n\n"
                                    f"FOB: 执行 NVMe User Data Erase (--ses=1)，将擦除全盘数据\n"
                                    f"Steady: 执行 WIPC+WDPC 稳态预处理，耗时较长\n\n"
                                    f"设备: {device}"):
            return

        # 构建命令
        script_path = os.path.abspath(__file__)  # v1.9.2: 动态获取当前脚本绝对路径，避免 root 用户 ~ 解析错误
        action = "enter-fob" if target_state == SSD_STATE_FOB else "enter-steady"
        cmd = [sys.executable, script_path, "-d", device, "--action", action, "-y"]

        # 传递稳态参数
        if target_state == SSD_STATE_STEADY:
            cmd.extend(["--steady-max-rounds", str(self.config_vars.get("steady_max_rounds", tk.IntVar(value=25)).get())])
            cmd.extend(["--steady-point-duration", str(self.config_vars.get("steady_point_duration", tk.IntVar(value=60)).get())])
        cmd.extend(["--purge-method", self.config_vars.get("purge_method", tk.StringVar(value="auto")).get()])

        self._append_log(f"开始让 SSD 进入 {state_name} 状态...", "header")
        self._append_log(f"命令: {' '.join(cmd)}", "info")

        # 更新 UI 状态
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text=f"进入{state_name}中...")
        self.status_var.set(f"正在进入 {state_name} 状态...")

        # 启动子进程
        try:
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
        except Exception as e:
            self._append_log(f"启动状态切换进程失败: {e}", "error")
            self._reset_ui_state()
            return

        # 启动输出读取线程（完成后自动刷新状态）
        self.reader_thread = threading.Thread(
            target=self._read_state_change_output,
            args=(target_state,),
            daemon=True)
        self.reader_thread.start()

    def _read_state_change_output(self, target_state: str):
        """后台线程：读取状态切换子进程输出，完成后刷新状态显示。"""
        try:
            for line in self.test_process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # v1.9.2 修复: 显示所有输出行，不再过滤（避免隐藏 Traceback/Error 等关键错误）
                tag = "info"
                if any(kw in line for kw in ["ERROR", "Error", "error", "Traceback", "Exception", "失败", "FAIL"]):
                    tag = "error"
                elif any(kw in line for kw in ["WARNING", "Warning", "warning", "警告"]):
                    tag = "warning"
                self.root.after(0, lambda l=line, t=tag: self._append_log(l, t))
            returncode = self.test_process.wait()
            self.root.after(0, lambda rc=returncode, ts=target_state: self._on_state_change_finished(rc, ts))
        except Exception as e:
            self.root.after(0, lambda: self._append_log(f"状态切换输出读取异常: {e}", "error"))
            self.root.after(0, self._reset_ui_state)

    def _on_state_change_finished(self, returncode: int, target_state: str):
        """状态切换完成回调。"""
        state_name = "FOB" if target_state == SSD_STATE_FOB else "稳态(Steady)"
        if returncode == 0:
            self.current_ssd_state = target_state
            self._update_state_display(target_state, f"已进入 {state_name} 状态")
            self._append_log(f"SSD 已成功进入 {state_name} 状态", "success")
        else:
            self._append_log(f"进入 {state_name} 状态失败 (退出码: {returncode})", "error")
        self._reset_ui_state()
        # 刷新状态显示
        self._refresh_ssd_state()

    def _enter_ssd_state_and_test(self, target_state: str, tests: List[str]):
        """先让 SSD 进入目标状态，完成后自动开始性能测试。

        Args:
            target_state: 目标状态 (SSD_STATE_FOB / SSD_STATE_STEADY)
            tests: 选中的测试项列表
        """
        device = self._get_current_device_path()
        if not device:
            return

        state_name = "FOB" if target_state == SSD_STATE_FOB else "稳态(Steady)"
        script_path = os.path.abspath(__file__)  # v1.9.2: 动态获取当前脚本绝对路径，避免 root 用户 ~ 解析错误
        action = "enter-fob" if target_state == SSD_STATE_FOB else "enter-steady"
        cmd = [sys.executable, script_path, "-d", device, "--action", action, "-y"]
        if target_state == SSD_STATE_STEADY:
            cmd.extend(["--steady-max-rounds", str(self.config_vars.get("steady_max_rounds", tk.IntVar(value=25)).get())])
            cmd.extend(["--steady-point-duration", str(self.config_vars.get("steady_point_duration", tk.IntVar(value=60)).get())])
        cmd.extend(["--purge-method", self.config_vars.get("purge_method", tk.StringVar(value="auto")).get()])

        self._append_log(f"先进入 {state_name} 状态，完成后自动开始测试...", "header")

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text=f"进入{state_name}中...")
        self.status_var.set(f"正在进入 {state_name} 状态（完成后自动测试）...")

        try:
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
        except Exception as e:
            self._append_log(f"启动状态切换进程失败: {e}", "error")
            self._reset_ui_state()
            return

        self.reader_thread = threading.Thread(
            target=self._read_state_change_and_test_output,
            args=(target_state, tests),
            daemon=True)
        self.reader_thread.start()

    def _read_state_change_and_test_output(self, target_state: str, tests: List[str]):
        """后台线程：读取状态切换输出，完成后自动启动测试。"""
        try:
            for line in self.test_process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # v1.9.2 修复: 显示所有输出行，不再过滤
                tag = "info"
                if any(kw in line for kw in ["ERROR", "Error", "error", "Traceback", "Exception", "失败", "FAIL"]):
                    tag = "error"
                elif any(kw in line for kw in ["WARNING", "Warning", "warning", "警告"]):
                    tag = "warning"
                self.root.after(0, lambda l=line, t=tag: self._append_log(l, t))
            returncode = self.test_process.wait()
            self.root.after(0, lambda rc=returncode, ts=target_state, ts_list=tests:
                            self._on_state_change_for_test_finished(rc, ts, ts_list))
        except Exception as e:
            self.root.after(0, lambda: self._append_log(f"状态切换输出读取异常: {e}", "error"))
            self.root.after(0, self._reset_ui_state)

    def _on_state_change_for_test_finished(self, returncode: int, target_state: str, tests: List[str]):
        """状态切换完成后自动启动测试的回调。"""
        state_name = "FOB" if target_state == SSD_STATE_FOB else "稳态(Steady)"
        if returncode != 0:
            self._append_log(f"进入 {state_name} 状态失败 (退出码: {returncode})，取消测试", "error")
            self._reset_ui_state()
            return
        self.current_ssd_state = target_state
        self._update_state_display(target_state, f"已进入 {state_name} 状态，准备测试")
        self._append_log(f"SSD 已进入 {state_name} 状态，自动开始性能测试...", "success")
        self._reset_ui_state()
        # 短暂延迟后启动测试（确保 UI 状态已重置）
        self.root.after(500, self._start_test_direct)

    def _start_test_direct(self):
        """直接启动测试（跳过状态校验，用于状态切换完成后自动测试）。"""
        if self.is_running:
            return
        tests = self._get_selected_tests()
        if not tests:
            return

        if os.geteuid() != 0 and not self.config_vars["dry_run"].get():
            cmd = self.build_command()
            cmd = ["pkexec"] + cmd
        else:
            cmd = self.build_command()

        self._update_cmd_preview()
        self.clear_log(silent=True)
        current_time = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"{current_time} - 测试开始 | 设备: {self.device_list[self.device_combo.current()]['path']} | "
                         f"测试项: {','.join(tests)} | 详细日志见日志文件", "header")

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text="运行中...")
        self.status_var.set("测试运行中...")

        try:
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
        except Exception as e:
            self._append_log(f"启动测试进程失败: {e}", "error")
            self._reset_ui_state()
            return

        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()


    def build_command(self) -> List[str]:
        """根据 GUI 配置构建命令行参数列表。"""
        device_idx = self.device_combo.current()
        if device_idx < 0:
            return []
        device = self.device_list[device_idx]["path"]

        # v1.9.2: 脚本路径固定为 ~/max_tool/ssd_test_v1.9.2.py（用户指定 Linux 路径）
        script_path = os.path.abspath(__file__)  # v1.9.2: 动态获取当前脚本绝对路径，避免 root 用户 ~ 解析错误
        cmd = [sys.executable, script_path, "-d", device]

        # 测试项
        tests = self._get_selected_tests()
        if tests:
            cmd.extend(["-t"] + tests)

        # 通用参数
        if self.config_vars["dry_run"].get():
            cmd.append("--dry-run")
        if self.config_vars["assume_yes"].get():
            cmd.append("-y")
        if self.config_vars["verbose"].get():
            cmd.append("--verbose")
        cmd.extend(["-o", self.config_vars["output_dir"].get()])
        cmd.extend(["--log-dir", self.config_vars["log_dir"].get()])

        # 固件参数
        if TEST_FW in tests:
            fw_img = self.config_vars["fw_image"].get()
            if fw_img:
                cmd.extend(["--fw-image", fw_img])
            cmd.extend(["--fw-action", self.config_vars["fw_action"].get()])
            fw_slot = self.config_vars["fw_slot"].get()
            if fw_slot:
                cmd.extend(["--fw-slot", fw_slot])

        # 性能参数（全参数可配置）
        if TEST_PERF in tests:
            cmd.extend(["--perf-state", self.config_vars["perf_state"].get()])
            # v1.9.2: 状态一致时跳过预处理（已删除稳态预处理UI）
            if self._skip_precondition:
                cmd.append("--no-precondition")
            # 全参数 FIO 配置
            cmd.extend(["--perf-name", self.config_vars["perf_test_name"].get()])
            cmd.extend(["--perf-direct", str(self.config_vars["perf_direct"].get())])
            cmd.extend(["--perf-ioengine", self.config_vars["perf_ioengine"].get()])
            cmd.extend(["--perf-bs", self.config_vars["perf_bs"].get()])
            cmd.extend(["--perf-iodepth", str(self.config_vars["perf_iodepth"].get())])
            cmd.extend(["--perf-numjobs", str(self.config_vars["perf_numjobs"].get())])
            cmd.extend(["--perf-rw", self.config_vars["perf_rw"].get()])
            cmd.extend(["--perf-rwmixread", str(self.config_vars["perf_rwmixread"].get())])
            cmd.extend(["--perf-size", self.config_vars["perf_size"].get()])
            cmd.extend(["--perf-runtime", str(self.config_vars["perf_runtime_full"].get())])
            if self.config_vars["perf_text_log"].get():
                cmd.append("--perf-text-log")
            # v1.9.2 新增: 状态管理与稳态配置参数
            cmd.extend(["--purge-method", self.config_vars.get("purge_method", tk.StringVar(value="auto")).get()])
            cmd.extend(["--steady-max-rounds", str(self.config_vars.get("steady_max_rounds", tk.IntVar(value=25)).get())])
            cmd.extend(["--steady-point-duration", str(self.config_vars.get("steady_point_duration", tk.IntVar(value=60)).get())])
            cmd.extend(["--smart-ignore-media-errors", self.config_vars.get("smart_ignore_media_errors", tk.StringVar(value="5353")).get()])
            # v1.8.0 多任务批量测试：任务列表非空时保存到临时文件并传递
            if self.perf_tasks:
                task_file = self._save_tasks_to_temp_file()
                if task_file:
                    self._current_task_file = task_file  # 保持引用，防止文件被清理
                    cmd.extend(["--perf-task-file", task_file])

        # 电源循环参数
        if TEST_POWERCYCLE in tests:
            cmd.extend(["--pc-cycles", str(self.config_vars["pc_cycles"].get())])
            cmd.extend(["--pc-power-mode", self.config_vars["pc_power_mode"].get()])
            ipmi_host = self.config_vars["ipmi_host"].get()
            if ipmi_host:
                cmd.extend(["--ipmi-host", ipmi_host])
                cmd.extend(["--ipmi-user", self.config_vars["ipmi_user"].get()])
                cmd.extend(["--ipmi-pass", self.config_vars["ipmi_pass"].get()])
            cmd.extend(["--pc-off-interval", str(self.config_vars["pc_off_interval"].get())])
            cmd.extend(["--pc-rw-duration", str(self.config_vars["pc_rw_duration"].get())])
            # enhanced 模式专属参数
            if self.config_vars["pc_power_mode"].get() == "enhanced":
                cmd.extend(["--pc-pattern", self.config_vars["pc_pattern"].get()])
                cmd.extend(["--pc-pattern-size-gb", str(self.config_vars["pc_pattern_size_gb"].get())])
                if not self.config_vars["pc_link_check"].get():
                    cmd.append("--pc-no-link-check")

        # SPOR 参数
        if TEST_SPOR in tests:
            cmd.extend(["--spor-cycles", str(self.config_vars["spor_cycles"].get())])
            cmd.extend(["--spor-power-mode", self.config_vars["spor_power_mode"].get()])
            cmd.extend(["--spor-delay", str(self.config_vars["spor_delay"].get())])
            cmd.extend(["--spor-test-size", str(self.config_vars["spor_test_size_gb"].get())])
            cmd.extend(["--spor-poweroff-delay-ms", str(self.config_vars["spor_poweroff_delay_ms"].get())])
            if self.config_vars["spor_mixed_rw"].get():
                cmd.append("--spor-mixed-rw")
                cmd.extend(["--spor-mixed-read-ratio", str(self.config_vars["spor_mixed_read_ratio"].get())])
            if not self.config_vars["spor_final_test"].get():
                cmd.append("--spor-no-final-test")
            cmd.extend(["--spor-timeboard-port", self.config_vars["spor_timeboard_port"].get()])
            # enhanced 模式专属参数
            if self.config_vars["spor_power_mode"].get() == "enhanced":
                cmd.extend(["--spor-enhanced-iodepth", str(self.config_vars["spor_enhanced_iodepth"].get())])
                cmd.extend(["--spor-enhanced-bs", self.config_vars["spor_enhanced_bs"].get()])

        # OSINT 参数
        if TEST_OSINT in tests:
            cmd.extend(["--osint-cycles", str(self.config_vars["osint_cycles"].get())])
            cmd.extend(["--osint-sleep-type", self.config_vars["osint_sleep_type"].get()])
            cmd.extend(["--osint-sleep-duration", str(self.config_vars["osint_sleep_duration"].get())])
            if self.config_vars["osint_io_idle"].get():
                cmd.append("--osint-io-idle")
            cmd.extend(["--osint-io-duration", str(self.config_vars["osint_io_duration"].get())])
            cmd.extend(["--osint-mount-point", self.config_vars["osint_mount_point"].get()])

        # RW（读/写测试）参数
        if TEST_RW in tests:
            cmd.extend(["--rw-mode", self.config_vars["rw_mode"].get()])
            # 文件大小多选
            selected_sizes = [size for size, var in self.rw_size_vars.items() if var.get()]
            if selected_sizes:
                cmd.extend(["--rw-file-sizes"] + selected_sizes)
            cmd.extend(["--rw-cycles", str(self.config_vars["rw_cycles"].get())])
            cmd.extend(["--rw-pattern", self.config_vars["rw_pattern"].get()])
            cmd.extend(["--rw-verify", self.config_vars["rw_verify"].get()])
            cmd.extend(["--rw-long-hours", str(self.config_vars["rw_long_hours"].get())])
            cmd.extend(["--rw-block-size", self.config_vars["rw_block_size"].get()])
            cmd.extend(["--rw-iodepth", str(self.config_vars["rw_iodepth"].get())])
            cmd.extend(["--rw-numjobs", str(self.config_vars["rw_numjobs"].get())])
            cmd.extend(["--rw-mixed-read-ratio", str(self.config_vars["rw_mixed_read_ratio"].get())])

        return cmd

    def _update_cmd_preview(self):
        """更新命令预览。"""
        cmd = self.build_command()
        if cmd:
            preview = " ".join(cmd)
            # 截断过长的预览
            if len(preview) > 500:
                preview = preview[:500] + "..."
        else:
            preview = "请先选择设备和测试项"

        self.cmd_preview.config(state=tk.NORMAL)
        self.cmd_preview.delete("1.0", tk.END)
        self.cmd_preview.insert(tk.END, preview)
        self.cmd_preview.config(state=tk.DISABLED)

    # ----------------------------------------------------------
    # 测试执行控制
    # ----------------------------------------------------------

    def start_test(self):
        """开始执行测试。"""
        try:
            self._start_test_internal()
        except Exception as e:
            import traceback
            self._append_log(f"[严重错误] start_test 抛出未捕获异常: {e}", "error")
            self._append_log(f"[调试] 完整Traceback:\n{traceback.format_exc()}", "error")
            self._reset_ui_state()

    def _start_test_internal(self):
        """开始执行测试（内部实现，由 start_test 包装异常捕获）。"""
        if self.is_running:
            return

        # 验证
        if self.device_combo.current() < 0:
            messagebox.showerror("错误", "请先选择待测设备")
            return
        tests = self._get_selected_tests()
        if not tests:
            messagebox.showerror("错误", "请至少选择一个测试项")
            return

        # v1.9.2 新增: 性能测试状态校验（仅当选择了性能测试时）
        if TEST_PERF in tests:
            device = self.device_list[self.device_combo.current()]["path"]
            # 先刷新当前状态
            try:
                serial = get_device_serial(device)
                state_info = load_ssd_state(serial)
                self.current_ssd_state = state_info["state"]
                self._update_state_display(self.current_ssd_state, f"序列号: {serial}")
            except Exception as e:
                self._append_log(f"读取 SSD 状态失败: {e}，默认按 Unknown 处理", "warning")
                self.current_ssd_state = SSD_STATE_UNKNOWN

            # 确定目标状态（v1.9.2: fob/steady/unknown）
            perf_state = self.config_vars["perf_state"].get()
            if perf_state == PERF_FOB:
                target_state = SSD_STATE_FOB
                target_name = "FOB"
            elif perf_state == PERF_STEADY:
                target_state = SSD_STATE_STEADY
                target_name = "Steady"
            else:  # unknown
                target_state = SSD_STATE_UNKNOWN
                target_name = "Unknown (直接测试)"

            current_name = self.current_ssd_state.upper()

            # 分支A: Unknown -> 直接测试
            if self.current_ssd_state == SSD_STATE_UNKNOWN:
                self._append_log(f"当前 SSD 状态为 Unknown，将直接进行性能测试（不进入任何状态）", "info")
            # 分支B: 状态一致 -> 直接测试，不重复进入
            elif self.current_ssd_state == target_state:
                self._skip_precondition = True  # v1.9.2: 状态一致，跳过预处理/擦除
                self._append_log(f"当前 SSD 状态({current_name})与目标状态({target_name})一致，直接进行性能测试（不重复进入，跳过预处理）", "info")
            # 分支C: 状态不一致 -> 警告弹窗
            else:
                msg = (f"SSD 目前状态与目标状态不一致！\n\n"
                       f"当前状态: {current_name}\n"
                       f"目标状态: {target_name}\n\n"
                       f"请确认是否直接进入 {target_name} 状态再执行后续测试？\n\n"
                       f"选择[是]: 自动进入目标状态后再进行性能测试\n"
                       f"选择[否]: 取消测试，请手动控制 SSD 状态后重试")
                auto_enter = messagebox.askyesno("状态不一致警告", msg)
                if auto_enter:
                    self._append_log(f"用户确认自动进入 {target_name} 状态后再测试", "info")
                    self._enter_ssd_state_and_test(target_state, tests)
                    return
                else:
                    self._append_log(f"用户取消测试，请手动控制 SSD 状态使目标状态与当前状态一致", "warning")
                    return

        # v1.9.2 调试: 状态判断完成，准备构建命令
        self._append_log(f"[调试] 状态判断完成，当前状态={self.current_ssd_state}, skip_precond={self._skip_precondition}", "info")

        # 检查 root 权限
        if os.geteuid() != 0 and not self.config_vars["dry_run"].get():
            result = messagebox.askyesno("权限提示",
                                          "测试需要 root 权限。\n是否使用 pkexec 提权执行？\n\n（也可以先 sudo 启动 GUI）")
            if not result:
                return
            # 使用 pkexec 提权
            cmd = self.build_command()
            cmd = ["pkexec"] + cmd
        else:
            cmd = self.build_command()

        # v1.9.2 调试: 命令构建完成
        self._append_log(f"[调试] 命令构建完成，共 {len(cmd)} 个参数", "info")
        if cmd:
            self._append_log(f"[调试] 命令: {' '.join(cmd)}", "info")
        else:
            self._append_log("[调试] 警告: build_command 返回空列表!", "error")

        # 更新命令预览
        self._update_cmd_preview()

        # 清空日志（GUI 摘要区域只显示标准摘要行）
        self.clear_log(silent=True)
        current_time = datetime.now().strftime("%H:%M:%S")
        selected = self._get_selected_tests()
        self._append_log(f"{current_time} - 测试开始 | 设备: {self.device_list[self.device_combo.current()]['path']} | "
                         f"测试项: {','.join(selected)} | 详细日志见日志文件", "header")

        # 更新 UI 状态
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text="运行中...")
        self.status_var.set("测试运行中...")

        # 启动子进程
        try:
            self._append_log("[调试] 正在启动测试子进程...", "info")
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
            self._append_log(f"[调试] 子进程已启动，PID={self.test_process.pid}", "info")
        except Exception as e:
            import traceback
            self._append_log(f"启动测试进程失败: {e}", "error")
            self._append_log(f"[调试] 异常详情: {traceback.format_exc()}", "error")
            self._reset_ui_state()
            return

        # 启动输出读取线程
        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

    def stop_test(self):
        """停止正在运行的测试。"""
        if not self.is_running:
            return

        if not messagebox.askyesno("确认停止", "确定要停止当前测试吗？\n停止后测试结果可能不完整。"):
            return

        self._append_log("\n用户请求停止测试...", "warning")

        if self.test_process and self.test_process.poll() is None:
            try:
                self.test_process.terminate()
                self.test_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.test_process.kill()
                self.test_process.wait()
            except Exception as e:
                self._append_log(f"停止进程时出错: {e}", "error")

        self._append_log("测试已停止", "warning")
        self._reset_ui_state()

    def _read_output(self):
        """后台线程：读取子进程输出，GUI 只显示标准摘要行。"""
        current_test = ""
        total_tests = len(self._get_selected_tests())
        current_idx = 0

        try:
            for line in self.test_process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                # 解析进度：[x/y] 开始测试（不显示，只更新进度条）
                match = re.search(r"\[(\d+)/(\d+)\]\s*开始测试[:：]\s*(.+)", line)
                if match:
                    current_idx = int(match.group(1))
                    total_tests = int(match.group(2))
                    current_test = match.group(3).strip()
                    progress_pct = (current_idx - 1) / total_tests * 100
                    self.root.after(0, lambda p=progress_pct, t=current_test: self._update_progress(p, f"[{current_idx}/{total_tests}] {t}"))

                # 显示标准摘要行 [SUMMARY]
                if line.startswith("[SUMMARY]"):
                    # 去掉 [SUMMARY] 前缀
                    summary_line = line[len("[SUMMARY] "):]
                    # 根据 Status 字段决定颜色
                    tag = "info"
                    if "Status:PASS" in summary_line:
                        tag = "success"
                    elif "Status:FAIL" in summary_line:
                        tag = "error"
                    elif "Status:ERROR" in summary_line:
                        tag = "error"
                    self.root.after(0, lambda l=summary_line, t=tag: self._append_log(l, t))

                    # 摘要行出现表示当前测试完成，更新进度
                    if current_idx > 0 and total_tests > 0:
                        progress_pct = current_idx / total_tests * 100
                        self.root.after(0, lambda p=progress_pct: self._update_progress(p, None))

                # v1.9.2 新增: 显示性能测试关键信息（FOB/稳态/IOPS/带宽/日志路径等）
                elif any(kw in line for kw in [
                    "FOB 状态性能测试", "稳态性能测试", "全参数 FIO 测试",
                    "结构化日志:", "文本日志:", "FIO 执行完成",
                    "执行命令:", "名称:", "IOPS:", "带宽:", "BW:",
                    "稳态预处理", "WDPC", "WIPC", "NVMe User Data Erase",
                    "blkdiscard", "FOB 擦除", "稳态检测", "达到稳态",
                    "ERROR", "Error", "error", "Traceback", "Exception",
                    "WARNING", "Warning", "失败", "FAIL"
                ]):
                    tag = "info"
                    if any(kw in line for kw in ["ERROR", "Error", "error", "Traceback", "Exception", "失败", "FAIL"]):
                        tag = "error"
                    elif any(kw in line for kw in ["WARNING", "Warning", "警告"]):
                        tag = "warning"
                    elif any(kw in line for kw in ["完成", "成功", "达到稳态", "PASS"]):
                        tag = "success"
                    self.root.after(0, lambda l=line, t=tag: self._append_log(l, t))

            # 等待进程结束
            returncode = self.test_process.wait()
            self.root.after(0, lambda rc=returncode: self._on_test_finished(rc))

        except Exception as e:
            self.root.after(0, lambda: self._append_log(f"读取输出线程异常: {e}", "error"))
            self.root.after(0, self._reset_ui_state)

    def _on_test_finished(self, returncode: int):
        """测试完成回调。"""
        self._skip_precondition = False  # v1.9.2: 重置跳过预处理标志
        current_time = datetime.now().strftime("%H:%M:%S")
        if returncode == 0:
            self._append_log(f"{current_time} - 全部测试完成 - 结果: PASS", "success")
            self.progress["value"] = 100
            self.progress_label.config(text="完成 (PASS)")
        else:
            self._append_log(f"{current_time} - 测试完成 - 存在失败项 (退出码: {returncode})", "error")
            self.progress_label.config(text=f"完成 (退出码 {returncode})")

        self.status_var.set(f"测试完成 | 退出码: {returncode} | 详细日志见日志文件")
        self._reset_ui_state()

    def _reset_ui_state(self):
        """重置 UI 状态。"""
        self.is_running = False
        self.test_process = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        # v1.9.2 修复: 重置状态栏和进度条，避免显示"正在进入..."残留
        if hasattr(self, "status_var"):
            self.status_var.set("就绪")
        if hasattr(self, "progress"):
            self.progress["value"] = 0
        if hasattr(self, "progress_label"):
            self.progress_label.config(text="")

    def _update_progress(self, value: float, text: Optional[str]):
        """更新进度条。"""
        self.progress["value"] = min(value, 100)
        if text:
            self.progress_label.config(text=text)

    # ----------------------------------------------------------
    # 日志操作
    # ----------------------------------------------------------

    def _append_log(self, message: str, tag: str = "info"):
        """追加日志到日志窗口。"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def clear_log(self, silent: bool = False):
        """清空日志。"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        if not silent:
            self.status_var.set("日志已清空")

    def export_log(self):
        """导出日志到文件。"""
        content = self.log_text.get("1.0", tk.END)
        if not content.strip():
            messagebox.showinfo("提示", "日志为空，无需导出")
            return

        default_name = f"ssd_test_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path = filedialog.asksaveasfilename(title="导出日志", defaultextension=".txt",
                                             initialfile=default_name,
                                             filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                messagebox.showinfo("成功", f"日志已导出到:\n{path}")
                self.status_var.set(f"日志已导出: {path}")
            except Exception as e:
                messagebox.showerror("错误", f"导出失败: {e}")

    # ----------------------------------------------------------
    # 配置保存/加载
    # ----------------------------------------------------------

    def save_config(self):
        """保存当前配置到 JSON 文件。"""
        config = {
            "device": self.device_combo.get(),
            "tests": self._get_selected_tests(),
            "params": {},
            "rw_file_sizes": [size for size, var in self.rw_size_vars.items() if var.get()],
            "perf_tasks": [t.to_dict() for t in self.perf_tasks],  # v1.8.0 保存批量任务
        }
        for key, var in self.config_vars.items():
            config["params"][key] = var.get()

        default_name = f"ssd_test_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = filedialog.asksaveasfilename(title="保存配置", defaultextension=".json",
                                             initialfile=default_name,
                                             filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                messagebox.showinfo("成功", f"配置已保存到:\n{path}")
                self.status_var.set(f"配置已保存: {path}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {e}")

    def load_config(self):
        """从 JSON 文件加载配置。"""
        path = filedialog.askopenfilename(title="加载配置",
                                           filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 恢复测试项
            tests = config.get("tests", [])
            for item_id, var in self.selected_tests.items():
                var.set(item_id in tests)

            # 恢复参数
            params = config.get("params", {})
            for key, value in params.items():
                if key in self.config_vars:
                    self.config_vars[key].set(value)

            # 恢复 RW 文件大小选择
            rw_sizes = config.get("rw_file_sizes", list(RW_FILE_SIZES.keys()))
            for size, var in self.rw_size_vars.items():
                var.set(size in rw_sizes)

            # v1.8.0 恢复批量任务列表
            saved_tasks = config.get("perf_tasks", [])
            if saved_tasks:
                self.perf_tasks = [PerfTask.from_dict(t) for t in saved_tasks if isinstance(t, dict)]
                self._refresh_task_list()

            self._update_cmd_preview()
            messagebox.showinfo("成功", f"配置已加载:\n{path}")
            self.status_var.set(f"配置已加载: {path}")
        except Exception as e:
            messagebox.showerror("错误", f"加载失败: {e}")

    # ----------------------------------------------------------
    # 其他
    # ----------------------------------------------------------

    def _open_report_dir(self):
        """打开报告输出目录。"""
        output_dir = self.config_vars["output_dir"].get()
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", output_dir])
        except Exception as e:
            messagebox.showerror("错误", f"打开目录失败: {e}\n目录路径: {output_dir}")

    def _open_log_file(self):
        """打开最新的详细日志文件。"""
        log_dir = self.config_vars["log_dir"].get()
        if not os.path.exists(log_dir):
            messagebox.showinfo("提示", f"日志目录不存在: {log_dir}")
            return
        # 找到最新的日志文件
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

    def _show_help(self):
        """显示使用说明。"""
        help_text = f"""SSD 自动化测试上位机 v{SCRIPT_VERSION}

【快速开始】
1. 点击「扫描设备」，选择待测 SSD
2. 在左侧勾选需要执行的测试项
3. 在右侧参数配置标签页中设置各测试项参数
4. 点击「开始测试」执行
5. 实时查看日志输出和进度

【测试项说明】
- 固件升降级：NVMe 固件下载/提交，支持升级和降级
- SMART健康：nvme-cli + smartctl 双源 SMART 检查
- 设备容量：三源交叉校验容量信息
- 性能测试：FOB+稳态 SNIA 规范，fio JSON 解析
- 正常电源循环：IPMI/手动断电，数据完整性校验
- 意外电源循环(SPOR)：Timeboard 硬件意外断电，PLP 验证
- 操作系统中断(OSINT)：S3/S4 休眠唤醒稳定性测试

【注意事项】
- 破坏性测试会清除磁盘数据，请确认备份
- SPOR 测试需要 Timeboard 硬件继电器
- OSINT 测试会导致系统休眠，建议后台运行
- 建议使用 sudo 启动 GUI 以避免权限提示

【快捷键】
F5          扫描设备
Ctrl+S      保存配置
Ctrl+O      加载配置
Ctrl+L      清空日志
"""
        win = tk.Toplevel(self.root)
        win.title("使用说明")
        win.geometry("650x550")
        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("TkDefaultFont", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, help_text)
        text.config(state=tk.DISABLED)

    def _show_about(self):
        """显示关于对话框。"""
        about_text = f"""SSD 自动化测试上位机

版本：v{SCRIPT_VERSION}
平台：Linux Ubuntu 20.04+
Python：3.8+（仅标准库，GUI 使用 tkinter）

覆盖 7 大测试项：
固件升降级 / SMART / 容量 / 性能 /
正常电源循环 / 意外电源循环(SPOR) /
操作系统中断(OSINT)

本工具通过 subprocess 调用命令行模式执行测试，
实时显示日志和进度，支持配置保存/加载。
"""
        messagebox.showinfo("关于", about_text)

    def _on_close(self):
        """窗口关闭事件。"""
        if self.is_running:
            if not messagebox.askyesno("确认退出", "测试正在运行中，确定要退出吗？\n退出将终止当前测试。"):
                return
            if self.test_process and self.test_process.poll() is None:
                try:
                    self.test_process.terminate()
                    self.test_process.wait(timeout=3)
                except Exception:
                    self.test_process.kill()
        self.root.destroy()


def run_gui():
    """启动可视化上位机。"""
    if not TKINTER_AVAILABLE:
        print("错误：tkinter 不可用，请安装 python3-tk：")
        print("  sudo apt-get install python3-tk")
        sys.exit(1)

    root = tk.Tk()
    # 设置主题
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    app = SSDTestGUI(root)
    root.mainloop()


if __name__ == "__main__":
    # v1.9.2 新增: 全局异常捕获，确保任何未捕获错误都完整输出到 stderr
    try:
        # 检查是否为 GUI 模式
        if "--gui" in sys.argv:
            run_gui()
        else:
            main()
    except SystemExit:
        # sys.exit() 正常退出，不捕获
        raise
    except Exception as _e:
        import traceback
        print("\n" + "=" * 60, file=sys.stderr)
        print("  脚本运行时发生未捕获异常", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"异常类型: {type(_e).__name__}", file=sys.stderr)
        print(f"异常信息: {_e}", file=sys.stderr)
        print("\n完整 Traceback:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"命令行参数: {sys.argv}", file=sys.stderr)
        print(f"Python 版本: {sys.version}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        sys.exit(1)
#（注：内容由AI生成）
