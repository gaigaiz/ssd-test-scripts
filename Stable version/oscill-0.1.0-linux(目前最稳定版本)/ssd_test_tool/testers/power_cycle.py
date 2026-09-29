#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - PowerCycleTester Module

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


class PowerCycleTester:
    """
    正常电源循环测试。
    流程：持续混合读写 -> 正常关机 -> power off -> 上电开机 -> 检查磁盘/分区/文件系统/数据
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
                self.log.info(f"Loading state file: current cycle {state.get('current_cycle', 0)}/"
                              f"{state.get('target_cycles', self.cfg.pc_cycles)}, "
                              f"phase {state.get('phase', 'unknown')}")
                return state
            except Exception as e:
                self.log.warning(f"State file read failed, reinitializing: {e}")
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
        self.log.debug(f"State file saved: cycle {state['current_cycle']}, phase {state['phase']}")

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
            self.log.info(f"Creating partition table and partitions: {self.cfg.device}")
            run_cmd(["parted", "-s", self.cfg.device, "mklabel", "gpt"],
                    check=True, capture=True, logger=self.log, timeout=30)
            run_cmd(["parted", "-s", self.cfg.device, "mkpart", "primary", "ext4",
                     "0%", "100%"],
                    check=True, capture=True, logger=self.log, timeout=30)
            time.sleep(2)  # 等待分区设备节点创建

            # 格式化 ext4
            self.log.info(f"Formatting ext4: {part_device}")
            run_cmd(["mkfs.ext4", "-F", "-L", "SSD_TEST", part_device],
                    check=True, capture=True, logger=self.log, timeout=120)

            # 挂载
            os.makedirs(mount_point, exist_ok=True)
            run_cmd(["mount", part_device, mount_point],
                    check=True, capture=True, logger=self.log, timeout=10)
            self.log.info(f"Test partition mounted: {part_device} -> {mount_point}")
            return True, part_device

        except Exception as e:
            self.log.error(f"Failed to create test partition: {e}")
            return False, part_device

    # ---------- 数据完整性 ----------

    def write_integrity_data(self, state: Dict[str, Any]) -> bool:
        """写入带 SHA-256 校验和的测试文件。校验和存入状态文件（不存待测盘）。"""
        mount_point = self.cfg.pc_mount_point
        checksums = {}
        try:
            self.log.info(f"Writing integrity test data: {DEFAULT_PC_TEST_FILE_COUNT} files, "
                          f"each {DEFAULT_PC_TEST_FILE_SIZE_MB}MB")
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
            self.log.info(f"Integrity data write completed, Total {len(checksums)} files")
            return True
        except Exception as e:
            self.log.error(f"Failed to write integrity data: {e}")
            return False

    def verify_integrity_data(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """校验测试文件的 SHA-256 校验和。返回 (全部通过, 详细结果)。"""
        result = {"total": 0, "passed": 0, "failed": 0, "missing": 0, "details": {}}
        checksums = state.get("integrity_checksums", {})
        if not checksums:
            self.log.warning("No checksum records in state file, skipping integrity check")
            return True, result

        for file_path, expected in checksums.items():
            result["total"] += 1
            if not os.path.exists(file_path):
                result["missing"] += 1
                result["details"][file_path] = "MISSING"
                self.log.error(f"Test file missing: {file_path}")
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
                    self.log.error(f"Data verification failed: {file_path}")
            except Exception as e:
                result["failed"] += 1
                result["details"][file_path] = f"ERROR: {e}"
                self.log.error(f"File verification error: {file_path}: {e}")

        all_ok = (result["failed"] == 0 and result["missing"] == 0)
        self.log.info(f"Integrity check: {result['passed']}/{result['total']} passed, "
                      f"{result['failed']} failed, {result['missing']} missing")
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
            self.log.info(f"Starting mixed R/W workload (randrw 70% read, {self.cfg.pc_rw_duration}s)...")
            self._fio_process = subprocess.Popen(
                fio_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(3)
            if self._fio_process.poll() is None:
                self.log.info("Mixed R/W workload started (PID: %d)", self._fio_process.pid)
                return True
            else:
                self.log.error("Mixed R/W workload exited immediately after start")
                return False
        except Exception as e:
            self.log.error(f"Starting mixed R/W workloadfailed: {e}")
            return False

    def stop_mixed_rw_load(self) -> Dict[str, Any]:
        """停止混合读写负载，返回读写统计。"""
        stats = {"stopped": False, "read_mb": 0, "write_mb": 0}
        if self._fio_process and self._fio_process.poll() is None:
            try:
                self._fio_process.terminate()
                self._fio_process.wait(timeout=10)
                stats["stopped"] = True
                self.log.info("Mixed R/W workload stopped")
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
            self.log.error(f"IPMI power status query failed: {e}")
        return None

    def ipmi_power_off(self) -> bool:
        """IPMI 远程断电。"""
        if self.cfg.pc_power_mode != "ipmi":
            return False
        try:
            self.log.info("IPMI remote power off...")
            run_cmd(self._build_ipmi_cmd("off"),
                    check=True, capture=True, logger=self.log, timeout=15)
            return True
        except Exception as e:
            self.log.error(f"IPMI power off failed: {e}")
            return False

    def ipmi_power_on(self) -> bool:
        """IPMI 远程上电。"""
        if self.cfg.pc_power_mode != "ipmi":
            return False
        try:
            self.log.info("IPMI remote power on...")
            run_cmd(self._build_ipmi_cmd("on"),
                    check=True, capture=True, logger=self.log, timeout=15)
            return True
        except Exception as e:
            self.log.error(f"IPMI power on failed: {e}")
            return False

    # ---------- 关机 ----------

    def graceful_shutdown(self):
        """执行正常关机。此函数调用后系统将关机，脚本进程终止。"""
        self.log.info("=" * 50)
        self.log.info("Executing normal shutdown...")
        self.log.info("=" * 50)
        # 同步文件系统
        run_cmd(["sync"], check=False, capture=True, logger=self.log, timeout=10)
        # 卸载测试分区
        run_cmd(["umount", self.cfg.pc_mount_point], check=False,
                capture=True, logger=self.log, timeout=10)

        if self.cfg.pc_power_mode == "manual":
            print("\n" + "=" * 60)
            print("  System is about to shut down. After system is fully powered off,")
            print(f"  Wait {self.cfg.pc_off_interval} seconds, then manually press power button to power on.")
            print("  After power on, system will boot and script will resume automatically (requires auto-start config).")
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
            self.log.info("  [1/5] Disk enumeration: OK")
        else:
            self.log.error("  [1/5] Disk enumeration: FAIL - device does not exist")
            return False, result

        # 2. 分区表检查
        try:
            _, out, _ = run_cmd(["parted", "-s", self.cfg.device, "print"],
                                check=True, capture=True, logger=self.log, timeout=15)
            if "gpt" in out.lower() or "msdos" in out.lower():
                result["partition_ok"] = True
                result["details"]["partition_table"] = "gpt" if "gpt" in out.lower() else "msdos"
                self.log.info("  [2/5] Partition table: OK")
            else:
                self.log.error("  [2/5] Partition table: FAIL")
        except Exception as e:
            self.log.error(f"  [2/5] Partition tablecheck failed: {e}")

        # 3. 文件系统检查（fsck 只读）
        try:
            run_cmd(["umount", part_device], check=False, capture=True, logger=self.log, timeout=10)
            _, out, _ = run_cmd(["fsck", "-n", part_device],
                                check=False, capture=True, logger=self.log, timeout=60)
            # fsck 返回 0 表示干净，1 表示有错误但已修复，其他为错误
            result["details"]["fsck_output"] = out.strip()[:500]
            if "clean" in out.lower() or "errors" not in out.lower():
                result["filesystem_ok"] = True
                self.log.info("  [3/5] File system: OK")
            else:
                self.log.error(f"  [3/5] File system: FAIL - {out.strip()[:200]}")
            # 重新挂载
            run_cmd(["mount", part_device, self.cfg.pc_mount_point],
                    check=False, capture=True, logger=self.log, timeout=10)
        except Exception as e:
            self.log.error(f"  [3/5] File system check error: {e}")

        # 4. 数据完整性校验
        integrity_ok, integrity_result = self.verify_integrity_data(state)
        result["data_integrity_ok"] = integrity_ok
        result["details"]["data_integrity"] = integrity_result
        self.log.info(f"  [4/5] Data integrity: {'OK' if integrity_ok else 'FAIL'}")

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
                    self.log.error(f"  [5/5] SMART: FAIL - Media errors={media_err}, temperature={temp}°C")
            else:
                run_cmd(["smartctl", "-H", self.cfg.device],
                        check=True, capture=True, logger=self.log, timeout=15)
                result["smart_ok"] = True
                self.log.info("  [5/5] SMART: OK")
        except Exception as e:
            self.log.error(f"  [5/5] SMART check failed: {e}")

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
        self.log.info("Executing final full functionality test...")
        self.log.info("=" * 50)
        result = {"capacity_ok": False, "smart_ok": False, "basic_perf_ok": False, "details": {}}

        # 1. 容量检查
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.cfg.device],
                                check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["details"]["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
            self.log.info(f"  Capacity check: {'OK' if cap > 0 else 'FAIL'} ({bytes_to_human(cap)})")
        except Exception as e:
            self.log.error(f"  Capacity check failed: {e}")

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
            self.log.info(f"  SMART check: {'OK' if result['smart_ok'] else 'FAIL'}")
        except Exception as e:
            self.log.error(f"  SMART check failed: {e}")

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
            self.log.info(f"  Basic performance (sequential read): {'OK' if read_bw > 0 else 'FAIL'} "
                          f"({read_bw / 1024:.2f} MB/s)")
        except Exception as e:
            self.log.error(f"  Basic performanceTest failed: {e}")

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
        self.log.info("[enhanced] Executing NVMe graceful removal(Shutdown Notification)")
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
            self.log.info("  [2/4] umount test partition: OK")

            # 3. NVMe disconnect（向控制器发送 Shutdown Notification）
            if self.cfg.device_type == DEVICE_NVME:
                ctrl = self.ctrl  # /dev/nvmeX
                try:
                    run_cmd(["nvme", "disconnect", ctrl],
                            check=True, capture=True, logger=self.log, timeout=15)
                    self.log.info(f"  [3/4] nvme disconnect {ctrl}: OK")
                except Exception as e:
                    self.log.warning(f"  [3/4] nvme disconnect failed (degraded to umount+sync only): {e}")
                    # 降级：不中断测试，继续断电
            else:
                self.log.info("  [3/4] Non-NVMe device, skipping nvme disconnect")

            # 4. 等待设备节点消失（最多 30 秒）
            self.log.info("  [4/4] Waiting for device node to disappear...")
            wait_start = time.time()
            while os.path.exists(self.cfg.device) and time.time() - wait_start < 30:
                time.sleep(2)
            if os.path.exists(self.cfg.device):
                self.log.warning("  Device node did not disappear within 30s, continuing power off (kernel may still hold reference)")
            else:
                self.log.info("  Device node disappeared")

            return True, "NVMe 优雅移除完成"
        except Exception as e:
            self.log.error(f"NVMe graceful removal error: {e}")
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
            self.log.info(f"[enhanced] Writing Pattern data: pattern={self.cfg.pc_pattern}, "
                          f"size={self.cfg.pc_pattern_size_gb}GB, bs=128k, QD=256")
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=3600)
            perf_data = json.loads(out)
            written = perf_data["jobs"][0]["write"]["io_bytes"]
            stats["written_bytes"] = written
            self.log.info(f"[enhanced] Pattern write completed: {bytes_to_human(written)}")

            # 记录到 state
            state["enhanced_pattern"] = {
                "pattern": self.cfg.pc_pattern,
                "size_bytes": size_bytes,
                "written_bytes": written,
                "lba_start": 0,
            }
            return True, stats
        except Exception as e:
            self.log.error(f"[enhanced] Pattern data write failed: {e}")
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
            self.log.warning("[enhanced] No Pattern record in state, skipping Pattern verification")
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
            self.log.info(f"[enhanced] Verifying Pattern data: pattern={pattern}, "
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
                self.log.warning(f"[enhanced] fio output parsing failed, trying text matching: {parse_err}")
                # fallback：从文本输出中查找 mismatch
                if "mismatch" in out.lower() or "verification failed" in out.lower():
                    result["mismatch_blocks"] = -1  # 标记为有错误但无法精确计数
                    result["details"] = "检测到 verify 错误（文本匹配）"

            all_ok = result["mismatch_blocks"] == 0
            if all_ok:
                self.log.info(f"[enhanced] Pattern Verification passed: {result['verified_blocks']} blocks all matched")
            else:
                self.log.error(f"[enhanced] Pattern Verification failed: mismatch={result['mismatch_blocks']} blocks")
            return all_ok, result
        except Exception as e:
            self.log.error(f"[enhanced] Pattern verification error: {e}")
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
            self.log.info("[enhanced] PCIe Link check disabled(--pc-no-link-check)")
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
            self.log.debug(f"[enhanced] sysfs Link status read failed: {e}")

        # 方法2：fallback 到 nvme get-phy
        if not current_width and self.cfg.device_type == DEVICE_NVME:
            try:
                _, out, _ = run_cmd(["nvme", "get-phy", self.ctrl, "-o", "json"],
                                    check=True, capture=True, logger=self.log, timeout=15)
                phy_data = json.loads(out)
                current_width = str(phy_data.get("number_of_lanes", ""))
                current_speed = str(phy_data.get("max_link_speed", ""))
            except Exception as e:
                self.log.debug(f"[enhanced] nvme get-phy Link status read failed: {e}")

        result["current_width"] = current_width
        result["current_speed"] = current_speed

        if not current_width and not current_speed:
            self.log.warning("[enhanced] Cannot get PCIe Link status (both sysfs and nvme get-phy failed), skipping Link check")
            result["consistent"] = True
            result["details"] = "无法获取 Link 状态，跳过"
            return True, result

        # 记录初始 Link 状态（首次调用时）
        initial = state.get("enhanced_initial_link", {})
        if not initial:
            state["enhanced_initial_link"] = {"width": current_width, "speed": current_speed}
            initial = state["enhanced_initial_link"]
            self.log.info(f"[enhanced] Initial PCIe Link: width={current_width}, speed={current_speed}")

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
            self.log.error(f"[enhanced] PCIe Link width changed: initial={initial['width']}, current={current_width}")
        if initial.get("speed") and current_speed and current_speed != initial["speed"]:
            consistent = False
            self.log.error(f"[enhanced] PCIe Link speed changed: initial={initial['speed']}, current={current_speed}")

        result["consistent"] = consistent
        if consistent:
            self.log.info(f"[enhanced] PCIe Link Verification passed: width={current_width}, speed={current_speed}")
        self.log.info(f"[enhanced] Link stats: width={width_stats}, speed={speed_stats}")
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
        self.log.info("[enhanced] Post-boot comprehensive check")
        self.log.info("=" * 50)

        # 1. 磁盘枚举
        if os.path.exists(self.cfg.device):
            result["disk_enumerated"] = True
            self.log.info("  [1/6] Disk enumeration: OK")
        else:
            self.log.error("  [1/6] Disk enumeration: FAIL - device does not exist")
            return False, result

        # 2. 分区表检查
        try:
            _, out, _ = run_cmd(["parted", "-s", self.cfg.device, "print"],
                                check=True, capture=True, logger=self.log, timeout=15)
            if "gpt" in out.lower() or "msdos" in out.lower():
                result["partition_ok"] = True
                self.log.info("  [2/6] Partition table: OK")
            else:
                self.log.error("  [2/6] Partition table: FAIL")
        except Exception as e:
            self.log.error(f"  [2/6] Partition tablecheck failed: {e}")

        # 3. 文件系统检查（fsck 只读）
        try:
            run_cmd(["umount", part_device], check=False, capture=True, logger=self.log, timeout=10)
            _, out, _ = run_cmd(["fsck", "-n", part_device],
                                check=False, capture=True, logger=self.log, timeout=60)
            result["details"]["fsck_output"] = out.strip()[:500]
            if "clean" in out.lower() or "errors" not in out.lower():
                result["filesystem_ok"] = True
                self.log.info("  [3/6] File system: OK")
            else:
                self.log.error(f"  [3/6] File system: FAIL - {out.strip()[:200]}")
            # 重新挂载
            run_cmd(["mount", part_device, self.cfg.pc_mount_point],
                    check=False, capture=True, logger=self.log, timeout=10)
        except Exception as e:
            self.log.error(f"  [3/6] File system check error: {e}")

        # 4. Pattern 数据校验（enhanced 核心增强点）
        pattern_ok, pattern_result = self.verify_pattern_data(state)
        result["pattern_integrity_ok"] = pattern_ok
        result["details"]["pattern_verify"] = pattern_result
        self.log.info(f"  [4/6] Pattern Data integrity: {'OK' if pattern_ok else 'FAIL'}")

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
                    self.log.error(f"  [6/6] SMART: FAIL - Media errors={media_err}, temperature={temp}°C")
            else:
                run_cmd(["smartctl", "-H", self.cfg.device],
                        check=True, capture=True, logger=self.log, timeout=15)
                result["smart_ok"] = True
                self.log.info("  [6/6] SMART: OK")
        except Exception as e:
            self.log.error(f"  [6/6] SMART check failed: {e}")

        all_ok = all([
            result["disk_enumerated"],
            result["partition_ok"],
            result["filesystem_ok"],
            result["pattern_integrity_ok"],
            result["pcie_link_ok"],
            result["smart_ok"],
        ])
        self.log.info(f"[enhanced] Comprehensive check result: {'PASS' if all_ok else 'FAIL'}")
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
            self.log.info(f"[DRY-RUN] Will execute power cycle test: {self.cfg.pc_cycles} , "
                          f"mode={self.cfg.pc_power_mode}")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        # IPMI 模式需要配置 host
        if self.cfg.pc_power_mode == "ipmi" and not self.cfg.ipmi_host:
            result.finish(STATUS_ERROR, "IPMI mode requires --ipmi-host")
            return result

        # enhanced 模式前置校验
        if self.cfg.pc_power_mode == "enhanced":
            if self.cfg.pc_pattern_size_gb <= 0:
                result.finish(STATUS_ERROR, "enhanced mode requires --pc-pattern-size-gb > 0")
                return result
            if self.cfg.ipmi_host:
                self.log.info("[enhanced] ipmi-host specified, power off step will reuse IPMI remote power off")
            else:
                self.log.info("[enhanced] ipmi-host not specified, power off step uses manual power off/on flow")

        try:
            # 加载状态文件（可能是重启后恢复）
            state = self.load_state()
            phase = state.get("phase", self.PHASE_SETUP)
            current_cycle = state.get("current_cycle", 0)
            target_cycles = state.get("target_cycles", self.cfg.pc_cycles)

            # 如果是全新开始（setup 阶段），执行初始化
            if phase == self.PHASE_SETUP:
                self.log.info("=" * 50)
                self.log.info(f"Power cycle test initialized: target {target_cycles} cycles")
                self.log.info("=" * 50)

                # 创建分区和文件系统
                ok, part_dev = self.setup_test_partition()
                if not ok:
                    result.finish(STATUS_FAIL, "Failed to create test partition")
                    return result

                # 写入完整性测试数据
                if not self.write_integrity_data(state):
                    result.finish(STATUS_FAIL, "Failed to write integrity test data")
                    return result

                # enhanced 模式：写入已知 Pattern 数据（LBA 级校验基准）
                if self.cfg.pc_power_mode == "enhanced":
                    pattern_ok, pattern_stats = self.write_pattern_data(state)
                    if not pattern_ok:
                        result.finish(STATUS_FAIL, "enhanced mode Pattern data write failed")
                        return result
                    # 记录初始 PCIe Link 状态
                    self.check_pcie_link_state(state)
                    self.save_state(state)

                state["phase"] = self.PHASE_RW_LOAD
                self.save_state(state)

            # 如果是重启后恢复（boot_check 阶段），执行开机检查
            if phase == self.PHASE_BOOT_CHECK:
                self.log.info("=" * 50)
                self.log.info(f"Detected reboot recovery: cycle {current_cycle}/{target_cycles} cycle boot check")
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
                    self.log.error(f"Round {current_cycle} cycle boot check failed")
                    state["phase"] = self.PHASE_FINAL_TEST
                    self.save_state(state)
                    # 即使失败也执行最终功能测试
                    final_ok, final_result = self.run_final_functional_test()
                    result.details["final_functional_test"] = final_result
                    result.details["cycle_results"] = state.get("cycle_results", [])
                    result.details["failed_cycle"] = current_cycle
                    result.finish(STATUS_FAIL, f"Cycle {current_cycle} boot check failed")
                    return result

                self.log.info(f"Round {current_cycle} cycle boot check passed")

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
                    self.log.info(f"Power cycle test all passed: {len(state.get('cycle_results', []))}/{target_cycles} cycles")
                else:
                    result.finish(STATUS_FAIL, "Final full functionality test failed")
                return result

            # 已完成状态
            if state.get("phase") == self.PHASE_DONE:
                result.details["cycle_results"] = state.get("cycle_results", [])
                result.details["total_cycles_completed"] = len(state.get("cycle_results", []))
                self.log.info("Test completed (state file indicates done)")
                result.finish(STATUS_PASS)
                return result

            # ===== 执行当前循环的混合读写 + 关机 =====
            # 此时 phase 应为 PHASE_RW_LOAD
            current_cycle = state.get("current_cycle", 0) + 1
            state["current_cycle"] = current_cycle
            self.log.info("=" * 50)
            self.log.info(f"Cycle {current_cycle}/{target_cycles} power cycle")
            self.log.info("=" * 50)

            # 启动混合读写负载
            rw_started = self.start_mixed_rw_load()
            if rw_started:
                self.log.info(f"Mixed R/W running ({self.cfg.pc_rw_duration}s)...")
                time.sleep(self.cfg.pc_rw_duration)
                rw_stats = self.stop_mixed_rw_load()
                self.log.info(f"Mixed R/W stats: read={rw_stats.get('read_mb', 0)}MB, "
                              f"write={rw_stats.get('write_mb', 0)}MB")
            else:
                self.log.warning("Mixed R/W workload start failed, continuing shutdown")

            # 同步并保存状态（标记为 boot_check，重启后恢复）
            run_cmd(["sync"], check=False, capture=True, logger=self.log, timeout=10)
            state["phase"] = self.PHASE_BOOT_CHECK
            state["last_shutdown_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_state(state)

            # enhanced 模式：先执行 NVMe 优雅移除（Shutdown Notification）
            if self.cfg.pc_power_mode == "enhanced":
                remove_ok, remove_msg = self.nvme_graceful_remove()
                if not remove_ok:
                    self.log.warning(f"[enhanced] NVMe graceful removal not fully successful: {remove_msg}, continuing power off")

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
                self.log.info(f"{mode_label} power control background script started (auto power off after shutdown -> delay -> power on)")

            # 执行正常关机（此调用后进程终止）
            # enhanced 无 ipmi-host 时走与 manual 相同的手动断电提示流程
            self.graceful_shutdown()

            # 理论上不会执行到这里
            result.finish(STATUS_ERROR, "Script did not terminate after shutdown (abnormal)")
            return result

        except Exception as e:
            self.log.exception(f"powercycleTest error: {e}")
            result.finish(STATUS_ERROR, str(e))
            return result

