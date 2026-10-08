#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - SmartTester Module

Auto-extracted from ssd_test_v1.9.3.py for modularization.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


from ..common import (
    TestConfig, TestResult, PerfTask,
    STATUS_PASS, STATUS_FAIL, STATUS_ERROR, STATUS_SKIP,
    TEST_FW, TEST_SMART, TEST_CAPACITY, TEST_PERF, TEST_POWERCYCLE,
    TEST_SPOR, TEST_OSINT, TEST_RW, TEST_POWER, TEST_ALL, VALID_TEST_ITEMS,
    DEVICE_NVME, DEVICE_SATA, DEVICE_UNKNOWN,
    PERF_FOB, PERF_STEADY, PERF_BOTH, PERF_UNKNOWN,
    FW_UPGRADE, FW_DOWNGRADE,
    RW_MODE_FULL_DISK, RW_MODE_FILE_CYCLE, RW_MODE_LONG_RUN, RW_MODE_ALL,
    RW_FILE_SIZES, RW_PATTERN_RANDOM, RW_PATTERN_AA, RW_PATTERN_55, RW_PATTERN_00, RW_PATTERN_FF,
    DEFAULT_PERF_RUNTIME, DEFAULT_PERF_QD, DEFAULT_FW_COMMIT_TIMEOUT,
    DEFAULT_CMD_TIMEOUT, DEFAULT_SMART_RW_SIZE_MB,
    DEFAULT_PC_CYCLES, DEFAULT_PC_POWER_MODE, DEFAULT_PC_MOUNT_POINT,
    DEFAULT_PC_STATE_FILE, DEFAULT_PC_BOOT_TIMEOUT, DEFAULT_PC_OFF_INTERVAL,
    DEFAULT_PC_RW_DURATION, DEFAULT_PC_TEST_FILE_COUNT, DEFAULT_PC_TEST_FILE_SIZE_MB,
    DEFAULT_PC_PATTERN, DEFAULT_PC_PATTERN_SIZE_GB, DEFAULT_PC_LINK_CHECK,
    DEFAULT_SPOR_POWER_MODE, DEFAULT_SPOR_ENHANCED_IODEPTH, DEFAULT_SPOR_ENHANCED_BS,
    SSD_STATE_UNKNOWN, SSD_STATE_FOB, SSD_STATE_STEADY,
    REQUIRED_COMMANDS, SCRIPT_VERSION,
    run_cmd, check_root, check_dependencies, get_device_type, get_nvme_controller,
    bytes_to_human, human_to_bytes, wait_for_device, confirm_destructive,
    get_default_log_dir, resolve_log_dir, setup_logging,
    _get_state_file_path, load_ssd_state, save_ssd_state, get_device_serial,
)

from ..i18n.translator import Translator, create_translator, I18N_ZH_CN, I18N_EN_US


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
            self.log.warning(f"nvme smart-log Fetch failed: {e}")
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
            self.log.warning(f"smartctl Fetch failed: {e}")
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
            self.log.warning(f"Failed to fetch full SMART info: {e}")

    def print_smart_comparison(self, smart_before: Dict[str, Any], smart_after: Dict[str, Any]):
        """输出 R/W 前后 SMART 关键项变化对比表（替代重复输出完整 smartctl）。

        只输出可能变化的关键项：温度、可用备件、介质错误、读写数据量、
        电源周期、上电时长、不安全关机次数等。设备静态信息不重复输出。
        """
        self.log.info("")
        self.log.info("----- SMART key items comparison before/after R/W -----")
        self.log.info(f"{'Item':<30} {'R/W before':>15} {'R/W after':>15} {'Change':>10}")
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
            self.log.info(f"Media errors: no change before/after R/W ({media_before:,}), Test passed")
        elif media_delta > 0:
            self.log.warning(f"Media errors: increased after R/W {media_delta:,} (before:{media_before:,} after:{media_after:,})")
        else:
            self.log.info(f"Media errors: decreased after R/W {-media_delta:,} (before:{media_before:,} after:{media_after:,})")

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
            self.log.info(f"Executing basic write test ({size_mb}MB)...")
            run_cmd(
                ["dd", "if=/dev/zero", f"of={self.cfg.device}",
                 f"bs=1M", f"count={size_mb}", "oflag=direct", "conv=fsync"],
                check=True, capture=True, logger=self.log, timeout=120
            )
            info["write_ok"] = True

            # 读取测试
            self.log.info(f"Executing basic read test ({size_mb}MB)...")
            run_cmd(
                ["dd", f"if={self.cfg.device}", "of=/dev/null",
                 f"bs=1M", f"count={size_mb}", "iflag=direct"],
                check=True, capture=True, logger=self.log, timeout=120
            )
            info["read_ok"] = True

        except Exception as e:
            self.log.error(f"Basic R/W test failed: {e}")
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
            self.log.info("[DRY-RUN] Will execute SMART health info test")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        try:
            # 1. 确认 OS 枚举设备
            if not os.path.exists(self.cfg.device):
                result.finish(STATUS_FAIL, f"OS did not enumerate device: {self.cfg.device}")
                return result
            self.log.info(f"Device enumerated by OS: {self.cfg.device}")

            # 2. 获取初始 SMART
            self.log.info("Fetching initial SMART info...")
            smart_before = self.get_smart_combined()
            result.details["smart_before"] = {
                k: v for k, v in smart_before.items()
                if k not in ("nvme_raw", "smartctl_raw")
            }

            # 输出完整设备信息和 SMART 数据（smartctl 格式）
            self.print_full_smart_info("Initial SMART Info (Before R/W)")

            # 3. 检查核心项
            core_ok, core_result = self.check_smart_core_items(smart_before)
            result.details["core_check_before"] = core_result
            if not core_ok:
                # 区分：仅介质错误 -> 友好提示（历史累积，不影响初始判定）；
                # 温度/可用备件等严重问题 -> WARNING
                only_media = all("介质错误" in issue for issue in core_result["issues"])
                media_err = smart_before.get("media_errors", 0)
                if only_media and media_err > 0:
                    self.log.info(f"Detected historical accumulated media errors: {media_err}(will check for new errors after R/W; historical values do not affect test result)")
                else:
                    self.log.warning(f"initial SMART core items have issues: {core_result['issues']}")

            # 4. 基本 R/W 操作
            self.log.info("Executing basic R/W operation...")
            rw_ok, rw_info = self.run_basic_rw()
            result.details["basic_rw"] = rw_info
            if not rw_ok:
                result.finish(STATUS_FAIL, "Basic R/W operation failed")
                return result

            # 5. R/W 后重新获取 SMART
            self.log.info("Re-fetching SMART info after R/W...")
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
                result.finish(STATUS_FAIL, f"Media errors increased after R/W: {media_delta}")
                return result

            # 7. R/W 后核心项复检
            # strict_media=False：介质错误绝对值不判定（历史累积错误），
            # 介质错误增量已在上方第6步单独检查（delta > 0 才 FAIL）
            core_ok_after, core_result_after = self.check_smart_core_items(smart_after, strict_media=False)
            result.details["core_check_after"] = core_result_after

            # 记录介质错误历史情况（不影响 PASS/FAIL）
            media_after = smart_after.get("media_errors")
            if media_after and media_after > 0:
                self.log.info(f"  Note: Disk has historical accumulated media errors {media_after}, no new errors after R/W, does not affect test result")

            if not core_ok_after:
                result.finish(STATUS_FAIL, f"SMART core items abnormal after R/W: {core_result_after['issues']}")
                return result

            result.finish(STATUS_PASS)
            self.log.info("SMART health info test passed")

        except Exception as e:
            self.log.exception(f"SMART Test error: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result

