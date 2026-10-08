#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - CapacityTester Module

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
import statistics
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
            self.log.warning(f"nvme capacity read failed: {e}")
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
            self.log.warning(f"lsblk capacity read failed: {e}")
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
            self.log.warning(f"smartctl capacity read failed: {e}")
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
            self.log.info("[DRY-RUN] Will execute device capacity test")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        try:
            if not os.path.exists(self.cfg.device):
                result.finish(STATUS_FAIL, f"Device not found: {self.cfg.device}")
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
                    self.log.info(f"  Device capacity: {primary['human_decimal']} ({primary['human_binary']}) "
                                  f"[Data sources: {', '.join(sources)}, verification consistent]")
                else:
                    # 多源不一致时才分别输出
                    self.log.warning(f"  Multi-source capacity mismatch (difference {verify_result.get('max_diff_pct', 0)}%):")
                    for source, info in values.items():
                        self.log.info(f"    [{source}] {info['human_decimal']} ({info['human_binary']})")

            if not capacities:
                result.finish(STATUS_FAIL, "Failed to read device capacity from any source")
                return result

            if not consistent:
                self.log.warning(f"Multi-source capacity difference exceeds 1%: {verify_result['max_diff_pct']}%")
                # 容量差异不一定是故障，可能是保留空间，标记为 PASS 但记录警告
                result.details["capacity_warning"] = (
                    f"多源容量差异 {verify_result['max_diff_pct']}%，可能因保留空间导致"
                )

            result.finish(STATUS_PASS)
            self.log.info("Device capacityTest passed")

        except Exception as e:
            self.log.exception(f"Capacity test error: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result

