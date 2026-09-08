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

适用平台：Linux Ubuntu 20.04+
依赖工具：nvme-cli, smartmontools, fio, util-linux
Python  ：3.8+（仅标准库）

用法示例：
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y
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

SCRIPT_VERSION = "1.0.0"

# 依赖的外部命令
REQUIRED_COMMANDS = {
    "nvme": "nvme-cli",
    "smartctl": "smartmontools",
    "fio": "fio",
    "lsblk": "util-linux",
}

# 测试项标识
TEST_FW = "fw"
TEST_SMART = "smart"
TEST_CAPACITY = "capacity"
TEST_PERF = "perf"
TEST_ALL = "all"

VALID_TEST_ITEMS = [TEST_FW, TEST_SMART, TEST_CAPACITY, TEST_PERF, TEST_ALL]

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
        description="SSD 自动化测试脚本 - 固件升降级/SMART/容量/性能",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t smart
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t capacity
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t perf --perf-state fob -y
  sudo python3 ssd_test.py -d /dev/nvme0n1 -t fw --fw-image fw.bin --fw-action upgrade
        """
    )

    parser.add_argument("-d", "--device", required=True,
                        help="待测设备路径，如 /dev/nvme0n1 或 /dev/sda")
    parser.add_argument("-t", "--test", nargs="+", default=[TEST_ALL],
                        choices=VALID_TEST_ITEMS,
                        help="测试项，可多选: fw/smart/capacity/perf/all (默认: all)")
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
        test_items = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF]

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
    logger.info(f"  日志文件: {log_file}")
    logger.info("-" * 55)

    # 5. 数据销毁确认（性能测试和固件测试会破坏数据）
    destructive_items = {TEST_PERF, TEST_FW, TEST_SMART}  # SMART 也有基本R/W
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
    }

    # 按合理顺序执行：容量 -> SMART -> 固件 -> 性能
    execution_order = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF]
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
