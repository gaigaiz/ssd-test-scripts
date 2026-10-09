#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - Common Module (data classes, constants, utilities)

Auto-extracted from ssd_test_v1.9.3.py for modularization.
Contains: global constants, utility functions, TestResult, PerfTask, TestConfig, TestReport.
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

SCRIPT_VERSION = "1.9.3"

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
TEST_POWER = "power"
TEST_ALL = "all"

VALID_TEST_ITEMS = [TEST_FW, TEST_SMART, TEST_CAPACITY, TEST_PERF, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT, TEST_RW, TEST_POWER, TEST_ALL]

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
        logger.debug(f"Executing command: {cmd_str}")

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
                logger.warning(f"Command timeout ({timeout}s), attempt {attempt + 1}/{retry + 1}: {cmd_str}")
            if attempt < retry:
                time.sleep(2)
                continue
            raise
        except subprocess.CalledProcessError as e:
            last_err = e
            if attempt < retry:
                if logger:
                    logger.warning(f"Command returned non-zero ({e.returncode}), retry {attempt + 1}/{retry + 1}")
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
        logger.error(f"Missing dependency tools: {', '.join(missing)}")
        logger.error("Please run: sudo apt install -y " + " ".join(
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
        logger.warning(f"Unrecognized device type: {device}")
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
        logger.info(f"Waiting for device re-enumeration: {device} (timeout {timeout}s)")
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(device):
            # 额外等待设备就绪
            time.sleep(2)
            try:
                run_cmd(["lsblk", "-d", device], check=True, capture=True, logger=None)
                if logger:
                    logger.info(f"Device ready: {device}")
                return True
            except Exception:
                pass
        time.sleep(2)
    if logger:
        logger.error(f"Device did not reappear within {timeout}s: {device}")
    return False


def confirm_destructive(device: str, logger: Optional[logging.Logger] = None) -> bool:
    """交互式确认数据销毁风险。"""
    print("\n" + "=" * 60)
    print("  WARNING: The following operations will destroy all data on the device!")
    print(f"  Target device: {device}")
    print("  Please confirm there is no important data on this device, or backup has been completed.")
    print("=" * 60)
    try:
        answer = input("输入 'YES' 继续，其他任意键取消: ").strip()
        return answer.upper() == "YES"
    except (EOFError, KeyboardInterrupt):
        return False

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
        print(f"[WARNING] Failed to save SSD state file: {e}", file=sys.stderr)

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
    # v1.9.3 新增：设备功耗测量配置
    oscill_path: str = ""  # oscill 项目根目录，空字符串表示使用脚本所在目录（共置部署）
    power_limit: Optional[float] = None  # 功耗上限(W)，None 表示使用 oscill 侧限值
    # v1.9.3 新增：功耗自动采集配置
    power_plan: str = ""  # 测试规划（POWER-01B~POWER-06），空字符串表示 oscill 手动选择
    power_total_duration: Optional[float] = None  # 总采集时间(秒)，None 表示手动控制
    power_sample_interval: Optional[float] = None  # 采集间隔(秒)，None 表示使用 oscill 默认
    power_json_report: str = ""  # oscill JSON 报告路径（用于转换为英文日志）
    power_channels: str = ""  # 勾选的通道，逗号分隔，如 "CH1,CH2"

    def __post_init__(self):
        self.device_type = get_device_type(self.device)
        if self.device_type == DEVICE_NVME:
            self.nvme_ctrl = get_nvme_controller(self.device)
        # v1.9.2: 日志目录转为基于脚本所在目录的绝对路径
        self.log_dir = resolve_log_dir(self.log_dir)


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

        self.log.info(f"JSON report generated: {report_file}")
        return report_file

    def print_summary(self):
        """打印测试汇总到控制台。"""
        summary = self.get_summary()
        self.log.info("=" * 55)
        self.log.info("  Test Summary")
        self.log.info("=" * 55)
        item_names = {
            TEST_CAPACITY: "Device Capacity",
            TEST_SMART: "SMART Health Info",
            TEST_FW: "Firmware Upgrade/Downgrade",
            TEST_PERF: "Full Performance Characterization",
            TEST_RW: "Read/Write Test",
            TEST_POWERCYCLE: "Normal Power Cycle",
            TEST_SPOR: "Surprise Power Cycle (SPOR)",
            TEST_OSINT: "OS Interruption (OSINT)",
            TEST_POWER: "Device Power Consumption Measurement",
        }
        for r in self.results:
            name = item_names.get(r.test_item, r.test_item)
            self.log.info(f"  {name:<36s}: {r.status} ({r.duration_sec:.1f}s)")
            if r.error_message:
                self.log.info(f"    Error: {r.error_message}")
            elif r.details.get("power_result"):
                self.log.info(f"    Result: {r.details['power_result']}")
        self.log.info("-" * 55)
        self.log.info(f"  Total: {summary['pass']} PASS / {summary['fail']} FAIL / "
                       f"{summary['error']} ERROR / {summary['skip']} SKIP")
        self.log.info(f"  Total Duration: {summary['total_duration_sec']:.1f}s")
        self.log.info("=" * 55)

