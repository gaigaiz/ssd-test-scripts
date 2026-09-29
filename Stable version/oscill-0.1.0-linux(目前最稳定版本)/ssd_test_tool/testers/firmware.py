#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - FirmwareTester Module

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

import time

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
                self.log.warning(f"nvme id-ctrl Failed to read firmware version: {e}")

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
            self.log.warning(f"smartctl Failed to read firmware version: {e}")

        return None, info

    def fw_download(self, image_path: str) -> bool:
        """下载固件镜像到设备。"""
        if not os.path.exists(image_path):
            self.log.error(f"Firmware image not found: {image_path}")
            return False
        if self.cfg.device_type != DEVICE_NVME:
            self.log.error("Firmware download only supports NVMe devices; use vendor tool for SATA")
            return False
        try:
            self.log.info(f"Downloading firmware image: {image_path}")
            run_cmd(
                ["nvme", "fw-download", self.ctrl, "-f", image_path],
                check=True, capture=True, logger=self.log, timeout=120
            )
            self.log.info("Firmware download succeeded")
            return True
        except Exception as e:
            self.log.error(f"Firmware download failed: {e}")
            return False

    def fw_commit(self, slot: Optional[int] = None, action: int = 2) -> bool:
        """
        提交并激活固件。
        action: 1=下次复位激活, 2=立即复位激活, 3=立即激活不复位, 4=仅激活指定槽位
        """
        if self.cfg.device_type != DEVICE_NVME:
            self.log.error("Firmware commit only supports NVMe devices")
            return False
        cmd = ["nvme", "fw-commit", self.ctrl, "-a", str(action)]
        if slot is not None:
            cmd.extend(["-s", str(slot)])
        try:
            self.log.info(f"Committing firmware (action={action}, slot={slot or 'auto'})")
            run_cmd(cmd, check=True, capture=True, logger=self.log, timeout=30)
            self.log.info("Firmware commit succeeded")
            return True
        except Exception as e:
            # action=2 时设备会立即复位，命令可能因设备消失而返回错误，这是正常的
            if action == 2:
                self.log.info(f"Device reset after firmware commit (abnormal return is expected): {e}")
                return True
            self.log.error(f"Firmware commit failed: {e}")
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
            self.log.error(f"SMART read failed after firmware flash: {e}")

        # 3. 容量正常
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.cfg.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
        except Exception as e:
            self.log.error(f"Capacity read failed after firmware flash: {e}")

        # 4. 基本 R/W（读取前 4MB，不写入以保护固件）
        try:
            run_cmd(["dd", f"if={self.cfg.device}", "of=/dev/null",
                     "bs=1M", "count=4", "iflag=direct"],
                    check=True, capture=True, logger=self.log, timeout=30)
            result["basic_rw_ok"] = True
        except Exception as e:
            self.log.error(f"Basic read failed after firmware flash: {e}")

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
            self.log.info(f"[DRY-RUN] Will execute firmware{action_name}: image={self.cfg.fw_image}")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        if not self.cfg.fw_image:
            result.finish(STATUS_ERROR, "Firmware image not specified (--fw-image)")
            return result

        if self.cfg.device_type != DEVICE_NVME:
            result.finish(STATUS_ERROR, f"Firmware {action_name} only supports NVMe devices, current is {self.cfg.device_type}")
            return result

        try:
            # 1. 读取当前固件版本
            self.log.info("Reading current firmware version...")
            old_ver, dev_info = self.get_current_fw_version()
            result.details["old_fw_version"] = old_ver
            result.details["device_info"] = dev_info
            self.log.info(f"Current firmware version: {old_ver}")

            # 2. 下载固件
            if not self.fw_download(self.cfg.fw_image):
                result.finish(STATUS_FAIL, "Firmware download failed")
                return result

            # 3. 提交并激活（立即复位）
            if not self.fw_commit(slot=self.cfg.fw_slot, action=2):
                result.finish(STATUS_FAIL, "Firmware commit failed")
                return result

            # 4. 烧写后校验
            all_ok, verify_info = self.verify_device_after_flash()
            result.details["verify_after_flash"] = verify_info

            # 5. 读取新固件版本并比对
            new_ver, _ = self.get_current_fw_version()
            result.details["new_fw_version"] = new_ver
            self.log.info(f"Firmware version after flash: {new_ver}")

            if not all_ok:
                result.finish(STATUS_FAIL, "Post-flash device functionality verification failed")
                return result

            if old_ver and new_ver:
                if self.cfg.fw_action == FW_UPGRADE and new_ver == old_ver:
                    self.log.warning(f"Firmware version unchanged ({old_ver} -> {new_ver}), image may be identical or commit did not take effect")
                if self.cfg.fw_action == FW_DOWNGRADE and new_ver == old_ver:
                    self.log.warning(f"Firmware version unchanged ({old_ver} -> {new_ver}), image may be identical or commit did not take effect")

            result.details["version_changed"] = (old_ver != new_ver) if (old_ver and new_ver) else None
            result.finish(STATUS_PASS)
            self.log.info(f"Firmware {action_name} test passed: {old_ver} -> {new_ver}")

        except Exception as e:
            self.log.exception(f"Firmware {action_name} test error: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result

