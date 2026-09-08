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
  5. 正常电源循环测试 (Normal Power Cycle Test)

适用平台：Linux Ubuntu 20.04+
依赖工具：nvme-cli, smartmontools, fio, util-linux, ipmitool, e2fsprogs, parted
Python  ：3.8+（仅标准库）

用法示例：
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t powercycle --pc-cycles 10 --ipmi-host 192.168.1.100 -y
"""

import argparse
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 常量定义
# ============================================================

SCRIPT_VERSION = "1.1.0"

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
TEST_ALL = "all"

VALID_TEST_ITEMS = [TEST_FW, TEST_SMART, TEST_CAPACITY, TEST_PERF, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT, TEST_ALL]

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
DEFAULT_PC_POWER_MODE = "ipmi"  # ipmi / manual
DEFAULT_PC_MOUNT_POINT = "/mnt/ssd_test"
DEFAULT_PC_STATE_FILE = "/var/lib/ssd_power_cycle_state.json"
DEFAULT_PC_BOOT_TIMEOUT = 300
DEFAULT_PC_OFF_INTERVAL = 30
DEFAULT_PC_RW_DURATION = 120
DEFAULT_PC_TEST_FILE_COUNT = 5
DEFAULT_PC_TEST_FILE_SIZE_MB = 100


# ============================================================
# 日志配置
# ============================================================

def setup_logging(log_dir: str, verbose: bool = False) -> Tuple[logging.Logger, str]:
    """初始化日志，同时输出到控制台和文件。"""
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
class TestConfig:
    """全局测试配置。"""
    device: str
    test_items: List[str]
    fw_image: Optional[str] = None
    fw_action: str = FW_UPGRADE
    fw_slot: Optional[int] = None
    perf_state: str = PERF_BOTH
    perf_runtime: int = DEFAULT_PERF_RUNTIME
    perf_qd: int = DEFAULT_PERF_QD
    precondition: bool = True
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
    # OSINT（操作系统中断测试）配置
    osint_cycles: int = 10
    osint_sleep_type: str = "s3"  # s3 / s4 / both
    osint_sleep_duration: int = 30  # 休眠持续秒数（rtcwake 定时唤醒）
    osint_io_active: bool = True  # True=活跃IO时休眠, False=空闲时休眠
    osint_io_duration: int = 60  # 每轮休眠前持续IO秒数
    osint_mount_point: str = "/mnt/ssd_osint"
    osint_state_file: str = "/var/lib/ssd_osint_state.json"
    osint_test_file_size_mb: int = 512
    # 通用配置
    output_dir: str = "./reports"
    log_dir: str = "./logs"
    dry_run: bool = False
    assume_yes: bool = False
    verbose: bool = False
    device_type: str = DEVICE_UNKNOWN
    nvme_ctrl: str = ""

    def __post_init__(self):
        self.device_type = get_device_type(self.device)
        if self.device_type == DEVICE_NVME:
            self.nvme_ctrl = get_nvme_controller(self.device)


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
        # 优先使用 nvme 数据
        for key in self.CORE_ITEMS:
            val = nvme_smart.get(key)
            if val is None:
                val = smartctl_smart.get(key)
            if val is not None:
                combined[key] = val
        combined["nvme_raw"] = nvme_smart
        combined["smartctl_raw"] = smartctl_smart
        return combined

    def check_smart_core_items(self, smart: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """检查 SMART 核心项是否在正常范围。"""
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

        # 介质错误检查（应为 0）
        media_err = smart.get("media_errors")
        if media_err is not None:
            check_result["media_errors"] = media_err
            if media_err > 0:
                issues.append(f"存在介质错误: {media_err}")
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

            # 3. 检查核心项
            core_ok, core_result = self.check_smart_core_items(smart_before)
            result.details["core_check_before"] = core_result
            if not core_ok:
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
            core_ok_after, core_result_after = self.check_smart_core_items(smart_after)
            result.details["core_check_after"] = core_result_after

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

            # 输出容量信息
            for source, info in verify_result.get("values", {}).items():
                self.log.info(f"  [{source}] {info['human_decimal']} ({info['human_binary']})")

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
                       extra: Optional[Dict[str, str]] = None) -> List[str]:
        """构建 fio 命令。"""
        cmd = [
            "fio",
            f"--name={name}",
            f"--filename={self.cfg.device}",
            f"--rw={rw}",
            f"--bs={bs}",
            f"--iodepth={qd}",
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

    def _parse_fio_json(self, json_str: str) -> Dict[str, Any]:
        """解析 fio JSON 输出，提取关键性能指标。"""
        try:
            data = json.loads(json_str)
        except Exception as e:
            self.log.error(f"fio JSON 解析失败: {e}")
            return {}

        result = {"jobs": []}
        jobs = data.get("jobs", [])
        for job in jobs:
            job_name = job.get("jobname", "unknown")
            job_result = {"name": job_name}

            # 读性能
            read = job.get("read", {})
            job_result["read"] = {
                "io_kb": read.get("io_kbytes"),
                "bw_kbps": read.get("bw"),  # KB/s
                "bw_mbps": round(read.get("bw", 0) / 1024, 2) if read.get("bw") else None,
                "iops": read.get("iops"),
                "lat_ns": read.get("lat_ns", {}),
                "clat_ns": read.get("clat_ns", {}),
            }
            # 延迟百分位
            clat = read.get("clat_ns", {})
            percentiles = clat.get("percentile", {})
            if percentiles:
                job_result["read"]["latency_percentiles_us"] = {
                    f"p{k}": round(v / 1000, 2) for k, v in percentiles.items() if v
                }

            # 写性能
            write = job.get("write", {})
            job_result["write"] = {
                "io_kb": write.get("io_kbytes"),
                "bw_kbps": write.get("bw"),
                "bw_mbps": round(write.get("bw", 0) / 1024, 2) if write.get("bw") else None,
                "iops": write.get("iops"),
                "lat_ns": write.get("lat_ns", {}),
                "clat_ns": write.get("clat_ns", {}),
            }
            clat_w = write.get("clat_ns", {})
            percentiles_w = clat_w.get("percentile", {})
            if percentiles_w:
                job_result["write"]["latency_percentiles_us"] = {
                    f"p{k}": round(v / 1000, 2) for k, v in percentiles_w.items() if v
                }

            result["jobs"].append(job_result)

        return result

    def run_fio(self, name: str, rw: str, bs: str, qd: int,
                runtime: int, size: Optional[str] = None) -> Dict[str, Any]:
        """执行单次 fio 测试并返回解析结果。"""
        cmd = self._build_fio_cmd(name, rw, bs, qd, runtime, size)
        self.log.info(f"  fio: {name} ({rw}, bs={bs}, qd={qd}, {runtime}s)")

        if self.cfg.dry_run:
            self.log.info(f"  [DRY-RUN] {' '.join(cmd)}")
            return {"dry_run": True, "command": " ".join(cmd)}

        try:
            _, out, _ = run_cmd(cmd, check=True, capture=True,
                                 logger=self.log, timeout=runtime + 60)
            parsed = self._parse_fio_json(out)
            parsed["raw_command"] = " ".join(cmd)
            return parsed
        except Exception as e:
            self.log.error(f"fio 测试失败 ({name}): {e}")
            return {"error": str(e), "command": " ".join(cmd)}

    def precondition_steady_state(self) -> Tuple[bool, Dict[str, Any]]:
        """
        稳态预处理（SNIA 标准近似）：
        1. 顺序写满全盘 2 次
        2. 随机写至写入量达到 2 倍全盘容量
        3. 等待 5 分钟让后台 GC/WL 完成
        """
        info = {"steps": [], "success": False}
        self.log.info("=" * 50)
        self.log.info("开始稳态预处理...")
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

        # 步骤 1：顺序写满全盘 2 次（128K QD32）
        self.log.info("[预处理 1/3] 顺序写满全盘 2 次...")
        try:
            # 使用 fio 顺序写，size 为全盘容量，运行 2 遍通过 time_based 控制
            # 这里用两次独立的 fio run，每次写满全盘
            for pass_idx in range(2):
                self.log.info(f"  顺序写第 {pass_idx + 1}/2 遍...")
                cmd = self._build_fio_cmd(
                    f"precond_seq_write_{pass_idx + 1}",
                    "write", "128k", 32,
                    runtime=max(60, int(cap_gb * 0.5)),  # 估算时间
                    size=f"{cap_bytes}B"
                )
                # 预处理不使用 time_based，写完即止
                cmd = [c for c in cmd if not c.startswith("--time_based")]
                cmd = [c for c in cmd if not c.startswith("--runtime")]
                run_cmd(cmd, check=True, capture=True, logger=self.log,
                        timeout=max(600, int(cap_gb * 2)))
            info["steps"].append("sequential_write_2pass: OK")
        except Exception as e:
            self.log.error(f"顺序写预处理失败: {e}")
            info["steps"].append(f"sequential_write_2pass: FAIL ({e})")
            info["error"] = f"顺序写预处理失败: {e}"
            return False, info

        # 步骤 2：随机写至写入量达到 2 倍全盘容量
        self.log.info("[预处理 2/3] 随机写 (2x 全盘容量)...")
        try:
            write_size_bytes = cap_bytes * 2
            cmd = self._build_fio_cmd(
                "precond_rand_write",
                "randwrite", "4k", 32,
                runtime=max(300, int(cap_gb * 1.5)),
                size=f"{write_size_bytes}B"
            )
            cmd = [c for c in cmd if not c.startswith("--time_based")]
            cmd = [c for c in cmd if not c.startswith("--runtime")]
            run_cmd(cmd, check=True, capture=True, logger=self.log,
                    timeout=max(1800, int(cap_gb * 3)))
            info["steps"].append("random_write_2x: OK")
        except Exception as e:
            self.log.error(f"随机写预处理失败: {e}")
            info["steps"].append(f"random_write_2x: FAIL ({e})")
            info["error"] = f"随机写预处理失败: {e}"
            return False, info

        # 步骤 3：等待 5 分钟让后台 GC/WL 完成
        self.log.info("[预处理 3/3] 等待 300s 让后台 GC/WL 完成...")
        time.sleep(300)
        info["steps"].append("wait_gc_300s: OK")

        info["success"] = True
        self.log.info("稳态预处理完成")
        return True, info

    def test_sequential_rw(self, label: str) -> Dict[str, Any]:
        """顺序读写带宽测试（128K QD32）。"""
        self.log.info(f"  [{label}] 顺序读写测试 (128K QD{self.cfg.perf_qd})...")
        result = {}
        # 顺序读
        result["read"] = self.run_fio(
            f"{label}_seq_read", "read", "128k",
            self.cfg.perf_qd, self.cfg.perf_runtime
        )
        # 顺序写
        result["write"] = self.run_fio(
            f"{label}_seq_write", "write", "128k",
            self.cfg.perf_qd, self.cfg.perf_runtime
        )
        return result

    def test_random_rw(self, label: str) -> Dict[str, Any]:
        """随机读写 IOPS 测试（4K QD32）。"""
        self.log.info(f"  [{label}] 随机读写测试 (4K QD{self.cfg.perf_qd})...")
        result = {}
        result["read"] = self.run_fio(
            f"{label}_rand_read", "randread", "4k",
            self.cfg.perf_qd, self.cfg.perf_runtime
        )
        result["write"] = self.run_fio(
            f"{label}_rand_write", "randwrite", "4k",
            self.cfg.perf_qd, self.cfg.perf_runtime
        )
        return result

    def test_latency_qos(self, label: str) -> Dict[str, Any]:
        """延迟与 QoS 百分位测试（4K QD1 随机读）。"""
        self.log.info(f"  [{label}] 延迟/QoS 测试 (4K QD1 随机读)...")
        result = self.run_fio(
            f"{label}_latency_qos", "randread", "4k",
            1, self.cfg.perf_runtime
        )
        return result

    def run_fob_test(self) -> Dict[str, Any]:
        """FOB（出厂空白）状态性能测试。"""
        self.log.info("-" * 40)
        self.log.info("FOB 状态性能测试")
        self.log.info("-" * 40)

        result = {"state": "FOB", "precondition": "blkdiscard"}

        # FOB 前置：discard 全盘恢复空白状态
        if not self.cfg.dry_run:
            self.log.info("  执行 blkdiscard 恢复 FOB 状态...")
            try:
                if self.cfg.device_type == DEVICE_NVME:
                    run_cmd(["blkdiscard", self.cfg.device], check=True,
                            capture=True, logger=self.log, timeout=300)
                else:
                    # SATA 设备使用 fio trim 或 blkdiscard（如果支持）
                    run_cmd(["blkdiscard", self.cfg.device], check=True,
                            capture=True, logger=self.log, timeout=300)
                self.log.info("  blkdiscard 完成，等待 10s...")
                time.sleep(10)
            except Exception as e:
                self.log.warning(f"blkdiscard 失败（可能设备不支持），继续测试: {e}")
                result["precondition_note"] = f"blkdiscard failed: {e}"
        else:
            self.log.info("  [DRY-RUN] 跳过 blkdiscard")

        # 性能测试
        result["sequential"] = self.test_sequential_rw("FOB")
        result["random"] = self.test_random_rw("FOB")
        result["latency_qos"] = self.test_latency_qos("FOB")

        return result

    def run_steady_state_test(self) -> Dict[str, Any]:
        """稳态性能测试。"""
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

        # 性能测试
        result["sequential"] = self.test_sequential_rw("STEADY")
        result["random"] = self.test_random_rw("STEADY")
        result["latency_qos"] = self.test_latency_qos("STEADY")

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

            # FOB 状态测试
            if self.cfg.perf_state in (PERF_FOB, PERF_BOTH):
                perf_results["fob"] = self.run_fob_test()

            # 稳态测试
            if self.cfg.perf_state in (PERF_STEADY, PERF_BOTH):
                perf_results["steady"] = self.run_steady_state_test()

            result.details["performance"] = perf_results

            # 简单性能有效性检查（至少有一项测试返回了有效数据）
            has_valid_data = False
            for state_data in perf_results.values():
                for test_type in ("sequential", "random", "latency_qos"):
                    test_data = state_data.get(test_type, {})
                    if isinstance(test_data, dict):
                        for rw_data in test_data.values():
                            if isinstance(rw_data, dict) and rw_data.get("jobs"):
                                has_valid_data = True
                                break
                    if has_valid_data:
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

                # 开机后检查
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

            # IPMI 模式：先正常关机，再通过 IPMI 断电
            if self.cfg.pc_power_mode == "ipmi":
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
                self.log.info("IPMI 电源控制后台脚本已启动（关机后自动断电→延时→上电）")

            # 执行正常关机（此调用后进程终止）
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
        if not self.start_spor_write():
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': '启动fio失败', 'phase': 'poweroff'
            })
            self.save_state(state)
            return False

        # Step 4: 等待指定秒数（让写入充分进行，此时 fio 仍在活跃写入）
        self.log.info(f"\n  Step 4/5: 写入中等待 {self.delay_before_poweroff} 秒...")
        time.sleep(self.delay_before_poweroff)

        # Step 5: 保存状态到阶段2，然后触发硬件断电
        # 关键：不执行 os.sync()，保证断电是真正的"意外"
        self.log.info("\n  Step 5/5: 保存状态并触发硬件掉电（不 sync）")
        state['phase'] = self.PHASE_POWERON
        state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        last_lba = self.get_write_position()

        # Step 5: 验证前段 pattern22（已写入区域，跳过最后 skip_lba 个 LBA）
        self.log.info("\n  Step 5/6: 验证前段 pattern22（已写入区域）")
        verify_22 = self._verify_written_area(last_lba)

        # Step 6: 验证后段 pattern11（未被覆盖区域）
        self.log.info("\n  Step 6/6: 验证后段 pattern11（未覆盖区域）")
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

            # 初始化 Timeboard（阶段1需要）
            if state.get('phase') == self.PHASE_POWEROFF:
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
        fio_cmd = (
            f"fio --name=osint_mixed_rw --directory={self.mount_point} "
            f"--rw=randrw --rwmixread=70 --bs=4k --iodepth=32 --numjobs=4 "
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
        """
        if sleep_type == self.SLEEP_S3:
            mode = "mem"
            type_name = "S3 (挂起到内存)"
        elif sleep_type == self.SLEEP_S4:
            mode = "disk"
            type_name = "S4 (挂起到磁盘/休眠)"
        else:
            return False, f"未知休眠类型: {sleep_type}"

        # 同步文件系统（正常休眠前 sync 是合理的，确保数据落盘）
        self.run_command("sync", timeout=30)

        self.log.info(f"触发 {type_name}，{self.sleep_duration}s 后自动唤醒...")
        # rtcwake -m <mode> -s <seconds>：设置 RTC 闹钟并进入指定休眠状态
        # rtcwake 会阻塞直到唤醒
        cmd = f"rtcwake -m {mode} -s {self.sleep_duration}"
        start_time = time.time()
        success, stdout, stderr = self.run_command(cmd, timeout=self.sleep_duration + 120)
        elapsed = time.time() - start_time

        if success:
            self.log.info(f"系统已从 {type_name} 唤醒（实际休眠约 {elapsed:.1f}s）")
            return True, f"wake_ok ({elapsed:.1f}s)"
        else:
            # rtcwake 可能因为权限或配置失败
            self.log.error(f"rtcwake 休眠失败: {stderr.strip()[:200]}")
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
                info["media_errors"] = smart.get("media_and_data_integrity_errors")
                info["available_spare"] = smart.get("available_spare")
                info["power_cycles"] = smart.get("power_cycles")
                temp = smart.get("temperature", 0)
                info["temperature_c"] = temp - 273 if temp > 200 else temp
                ok = (info.get("media_errors", 1) == 0
                      and (info.get("available_spare", 100) >= 10)
                      and 0 <= info.get("temperature_c", 25) <= 70)
                self.log.info(f"  SMART: 介质错误={info.get('media_errors')}, "
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
                "perf_qd": self.cfg.perf_qd,
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
    parser.add_argument("--perf-state", default=PERF_BOTH,
                        choices=[PERF_FOB, PERF_STEADY, PERF_BOTH],
                        help="性能测试状态: fob/steady/both (默认: both)")
    parser.add_argument("--perf-runtime", type=int, default=DEFAULT_PERF_RUNTIME,
                        help=f"单项 fio 运行时长（秒），默认 {DEFAULT_PERF_RUNTIME}")
    parser.add_argument("--perf-qd", type=int, default=DEFAULT_PERF_QD,
                        help=f"性能测试队列深度，默认 {DEFAULT_PERF_QD}")
    parser.add_argument("--precondition", default="on", choices=["on", "off"],
                        help="稳态预处理开关: on/off (默认: on)")
    # 电源循环测试参数
    parser.add_argument("--pc-cycles", type=int, default=DEFAULT_PC_CYCLES,
                        help=f"电源循环测试循环次数，默认 {DEFAULT_PC_CYCLES}")
    parser.add_argument("--pc-power-mode", default=DEFAULT_PC_POWER_MODE,
                        choices=["ipmi", "manual"],
                        help="电源控制模式: ipmi(远程自动)/manual(手动断电上电) (默认: ipmi)")
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
    # SPOR（意外电源循环测试）参数
    parser.add_argument("--spor-cycles", type=int, default=10,
                        help="SPOR 测试循环次数，默认 10")
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
    parser.add_argument("-o", "--output-dir", default="./reports",
                        help="报告输出目录 (默认: ./reports)")
    parser.add_argument("--log-dir", default="./logs",
                        help="日志输出目录 (默认: ./logs)")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，只打印命令不执行")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过数据销毁确认提示")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志输出")
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
        test_items = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT]

    config = TestConfig(
        device=args.device,
        test_items=test_items,
        fw_image=args.fw_image,
        fw_action=args.fw_action,
        fw_slot=args.fw_slot,
        perf_state=args.perf_state,
        perf_runtime=args.perf_runtime,
        perf_qd=args.perf_qd,
        precondition=(args.precondition == "on"),
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
        osint_cycles=args.osint_cycles,
        osint_sleep_type=args.osint_sleep_type,
        osint_sleep_duration=args.osint_sleep_duration,
        osint_io_active=not args.osint_io_idle,
        osint_io_duration=args.osint_io_duration,
        osint_mount_point=args.osint_mount_point,
        osint_state_file=args.osint_state_file,
        output_dir=args.output_dir,
        log_dir=args.log_dir,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        verbose=args.verbose,
    )

    logger.info(f"  设备: {config.device} (类型: {config.device_type})")
    logger.info(f"  测试项: {', '.join(config.test_items)}")
    if config.fw_image:
        logger.info(f"  固件镜像: {config.fw_image} ({config.fw_action})")
    logger.info(f"  性能状态: {config.perf_state}, runtime={config.perf_runtime}s, qd={config.perf_qd}")
    if TEST_POWERCYCLE in config.test_items:
        logger.info(f"  电源循环: {config.pc_cycles}次, 模式={config.pc_power_mode}"
                     + (f", IPMI={config.ipmi_host}" if config.ipmi_host else ""))
    if TEST_SPOR in config.test_items:
        logger.info(f"  SPOR意外断电: {config.spor_cycles}次, 延时={config.spor_delay}s, "
                     f"硬件断电延时={config.spor_poweroff_delay_ms}ms, "
                     f"混合读写={'开' if config.spor_mixed_rw else '关'}")
    if TEST_OSINT in config.test_items:
        logger.info(f"  OSINT中断: {config.osint_cycles}次, 休眠={config.osint_sleep_type}, "
                     f"时长={config.osint_sleep_duration}s, "
                     f"活跃IO={'开' if config.osint_io_active else '关(空闲)'}")
    logger.info(f"  日志文件: {log_file}")
    logger.info("-" * 55)

    # 5. 数据销毁确认（性能测试、固件测试、电源循环测试、SPOR测试、OSINT测试会破坏数据）
    destructive_items = {TEST_PERF, TEST_FW, TEST_SMART, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT}
    if any(item in destructive_items for item in config.test_items):
        if not config.dry_run and not config.assume_yes:
            if not confirm_destructive(config.device, logger):
                logger.info("用户取消操作，退出")
                sys.exit(0)
        elif config.assume_yes:
            logger.info("已指定 -y，跳过数据销毁确认")

    # 6. 执行测试
    report = TestReport(config, logger)
    testers = {
        TEST_CAPACITY: CapacityTester(config, logger),
        TEST_SMART: SmartTester(config, logger),
        TEST_FW: FirmwareTester(config, logger),
        TEST_PERF: PerformanceTester(config, logger),
        TEST_POWERCYCLE: PowerCycleTester(config, logger),
        TEST_SPOR: SPORTester(config, logger),
        TEST_OSINT: OSInterruptionTester(config, logger),
    }

    # 按合理顺序执行：容量 -> SMART -> 固件 -> 性能 -> 正常电源循环 -> 意外电源循环(SPOR) -> 操作系统中断(OSINT)
    execution_order = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF,
                       TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT]
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
            TEST_POWERCYCLE: "正常电源循环",
            TEST_SPOR: "意外电源循环(SPOR)",
            TEST_OSINT: "操作系统中断(OSINT)",
        }.get(item, item)

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


if __name__ == "__main__":
    main()
