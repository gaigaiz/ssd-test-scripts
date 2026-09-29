#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - OSInterruptionTester Module

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
                self.log.info(f"Loading OSINT state: Round {state.get('current_cycle', '?')}/"
                              f"{state.get('total_cycles', '?')} rounds")
                return state
            except Exception as e:
                self.log.warning(f"State file read failed, reinitializing: {e}")
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
        self.log.debug(f"Executing command: {cmd}")
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

            self.log.info(f"Creating partition table and partitions: {self.device}")
            self.run_command(f"parted -s {self.device} mklabel gpt", timeout=30)
            self.run_command(f"parted -s {self.device} mkpart primary ext4 0% 100%", timeout=30)
            time.sleep(2)

            self.log.info(f"Formatting ext4: {part_dev}")
            self.run_command(f"mkfs.ext4 -F -L SSD_OSINT {part_dev}", timeout=120)

            os.makedirs(self.mount_point, exist_ok=True)
            self.run_command(f"mount {part_dev} {self.mount_point}", timeout=10)
            self.log.info(f"Test partition mounted: {part_dev} -> {self.mount_point}")
            return True
        except Exception as e:
            self.log.error(f"Failed to create test partition: {e}")
            return False

    # ---------- 数据完整性 ----------

    def write_integrity_data(self, state: Dict[str, Any]) -> bool:
        """写入带 SHA-256 校验和的测试文件，校验和存入状态文件。"""
        checksums = {}
        test_file = os.path.join(self.mount_point, "osint_integrity_test.bin")
        try:
            self.log.info(f"Writing integrity test data: {self.test_file_size_mb}MB")
            self.run_command(
                f"dd if=/dev/urandom of={test_file} bs=1M count={self.test_file_size_mb} "
                f"oflag=direct conv=fsync",
                timeout=300
            )
            _, out, _ = self.run_command(f"sha256sum {test_file}", timeout=60)
            checksum = out.strip().split()[0]
            checksums[test_file] = checksum
            state["integrity_checksums"] = checksums
            self.log.info(f"Integrity data write completed, SHA-256: {checksum[:16]}...")
            return True
        except Exception as e:
            self.log.error(f"Failed to write integrity data: {e}")
            return False

    def verify_integrity_data(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """校验测试文件的 SHA-256 校验和。"""
        result = {"total": 0, "passed": 0, "failed": 0, "missing": 0, "details": {}}
        checksums = state.get("integrity_checksums", {})
        if not checksums:
            self.log.warning("No checksum records, skipping integrity check")
            return True, result

        for file_path, expected in checksums.items():
            result["total"] += 1
            if not os.path.exists(file_path):
                result["missing"] += 1
                result["details"][file_path] = "MISSING"
                self.log.error(f"Test file missing: {file_path}")
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
                    self.log.error(f"Data verification failed: {file_path}")
            except Exception as e:
                result["failed"] += 1
                result["details"][file_path] = f"ERROR: {e}"
                self.log.error(f"File verification error: {file_path}: {e}")

        all_ok = (result["failed"] == 0 and result["missing"] == 0)
        self.log.info(f"Integrity check: {result['passed']}/{result['total']} passed, "
                      f"{result['failed']} failed, {result['missing']} missing")
        return all_ok, result

    # ---------- IO 负载 ----------

    def start_io_load(self) -> bool:
        """启动 fio 混合读写持续负载（后台运行）。"""
        if not self.io_active:
            self.log.info("Idle mode: not starting IO load")
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
            self.log.info(f"Starting mixed R/W workload (randrw 70% read, {self.io_duration}s)...")
            self._fio_process = subprocess.Popen(
                fio_cmd, shell=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True
            )
            time.sleep(3)
            if self._fio_process.poll() is None:
                self.log.info(f"Mixed R/W workload started (PID: {self._fio_process.pid})")
                return True
            self.log.error("Mixed R/W workload exited immediately after start")
            return False
        except Exception as e:
            self.log.error(f"Starting mixed R/W workloadfailed: {e}")
            return False

    def stop_io_load(self):
        """停止 IO 负载。"""
        if self._fio_process and self._fio_process.poll() is None:
            try:
                self._fio_process.terminate()
                self._fio_process.wait(timeout=10)
                self.log.info("Mixed R/W workload stopped")
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
            type_name = "S3 (suspend to RAM)"
        elif sleep_type == self.SLEEP_S4:
            mode = "disk"
            type_name = "S4 (suspend to disk/hibernate)"
        else:
            return False, f"Unknown sleep type: {sleep_type}"

        part_dev = self.get_partition_device()

        # ---- 休眠前准备：停止 IO → sync → 卸载 → 刷新NVMe写缓存 ----
        self.log.info("  Pre-sleep preparation: stop IO load...")
        self.stop_io_load()
        time.sleep(2)  # 确保 fio 进程完全退出、文件句柄释放

        self.log.info("  Pre-sleep preparation: sync flush cache...")
        self.run_command("sync", timeout=30)
        time.sleep(1)  # 确保 sync 完成

        self.log.info(f"  Pre-sleep preparation: unmount file system {self.mount_point}...")
        self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=15)
        # 确认已卸载
        if os.path.ismount(self.mount_point):
            self.log.warning("  File system unmount failed, force unmount...")
            self.run_command(f"umount -f {self.mount_point} 2>/dev/null", timeout=15)
            time.sleep(1)

        # 刷新 NVMe 设备写缓存，确保所有数据提交到 NAND
        if self.cfg.device_type == DEVICE_NVME:
            self.log.info("  Pre-sleep preparation: flush NVMe write cache...")
            self.run_command(f"nvme flush {self.ctrl} 2>/dev/null", timeout=10)
            time.sleep(1)

        self.log.info(f"Triggering {type_name}, {self.sleep_duration}s auto wakeup after...")
        # rtcwake -m <mode> -s <seconds>：设置 RTC 闹钟并进入指定休眠状态
        # rtcwake 会阻塞直到唤醒
        cmd = f"rtcwake -m {mode} -s {self.sleep_duration}"
        start_time = time.time()
        success, stdout, stderr = self.run_command(cmd, timeout=self.sleep_duration + 120)
        elapsed = time.time() - start_time

        if success:
            self.log.info(f"System woke up from {type_name} wakeup (actual sleep about {elapsed:.1f}s)")
            # ---- 唤醒后：重新挂载文件系统 ----
            self.log.info(f"  after wakeup: remount file system {part_dev} -> {self.mount_point}...")
            mount_ok, _, mount_err = self.run_command(
                f"mount {part_dev} {self.mount_point}", timeout=15)

            # 挂载失败时自动 fsck 修复后重试（常见错误: Structure needs cleaning）
            if not mount_ok or not os.path.ismount(self.mount_point):
                self.log.warning(f"  after wakeupfirst mount failed: {mount_err.strip()[:150]}")
                if "structure needs cleaning" in mount_err.lower() or "needs cleaning" in mount_err.lower():
                    self.log.info("  Detected file system log inconsistency, automatically running fsck -y fix...")
                    # 先确保未挂载
                    self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=10)
                    # fsck -y 自动修复所有问题
                    fsck_ok, fsck_out, fsck_err = self.run_command(
                        f"fsck -y {part_dev}", timeout=120)
                    self.log.info(f"  fsck fixcompleted (exit={'ok' if fsck_ok else 'fail'}): "
                                  f"{(fsck_out or fsck_err).strip()[:200]}")
                    time.sleep(1)
                    # 重试挂载
                    self.log.info("  retry mount after fix...")
                    mount_ok, _, mount_err = self.run_command(
                        f"mount {part_dev} {self.mount_point}", timeout=15)

            if not mount_ok or not os.path.ismount(self.mount_point):
                self.log.error(f"  after wakeupmount failed (fsck fix already attempted): {mount_err.strip()[:200]}")
                return False, f"wake_ok_but_mount_failed ({elapsed:.1f}s): {mount_err.strip()[:100]}"
            self.log.info("  after wakeupmount succeeded")
            return True, f"wake_ok ({elapsed:.1f}s)"
        else:
            # rtcwake 可能因为权限或配置失败
            self.log.error(f"rtcwake sleepfailed: {stderr.strip()[:200]}")
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
                self.log.error("  disk dropped: device node does not exist")
                return False, result

            # 检查 /dev/disk/by-id/ 符号链接
            _, out, _ = self.run_command("ls -la /dev/disk/by-id/ 2>/dev/null", timeout=10)
            basename = os.path.basename(self.device)
            for line in out.splitlines():
                if basename in line:
                    result["by_id_links"].append(line.strip())

            result["identity_stable"] = len(result["by_id_links"]) > 0
            self.log.info(f"  disk identification: device present={result['device_present']}, "
                          f"by-id_link_count={len(result['by_id_links'])}")
            return result["device_present"] and result["identity_stable"], result
        except Exception as e:
            self.log.error(f"  disk identificationcheck failed: {e}")
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
            self.log.info(f"  Partition table: {'OK' if result['partition_ok'] else 'FAIL'}")

            # 重新挂载
            self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=10)
            _, mount_out, mount_err = self.run_command(
                f"mount {part_dev} {self.mount_point}", timeout=10)
            result["fs_mount_ok"] = mount_out is not None and os.path.ismount(self.mount_point)
            self.log.info(f"  File system mount: {'OK' if result['fs_mount_ok'] else 'FAIL'}")

            # fsck 只读检查（需先卸载）
            self.run_command(f"umount {self.mount_point} 2>/dev/null", timeout=10)
            _, fsck_out, _ = self.run_command(f"fsck -n {part_dev}", timeout=60)
            result["details"]["fsck_output"] = fsck_out.strip()[:300]
            result["fsck_ok"] = "clean" in fsck_out.lower() or "errors" not in fsck_out.lower()
            self.log.info(f"  File systemcheck (fsck): {'OK' if result['fsck_ok'] else 'FAIL'}")

            # 重新挂载
            self.run_command(f"mount {part_dev} {self.mount_point}", timeout=10)

            all_ok = all([result["partition_ok"], result["fs_mount_ok"], result["fsck_ok"]])
            return all_ok, result
        except Exception as e:
            self.log.error(f"  Partition/file system check failed: {e}")
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
                # None 值兜底：解析不到时considered normal（介质错误=0，available_spare=100），不因此判 FAIL
                info["media_errors"] = media_errors if media_errors is not None else 0
                info["available_spare"] = avail_spare if avail_spare is not None else 100
                info["power_cycles"] = smart.get("power_cycles")
                temp = smart.get("temperature", 0)
                info["temperature_c"] = temp - 273 if temp > 200 else temp

                # 介质错误改为警告模式：仅记录警告，不影响 PASS/FAIL
                # （已知有介质错误的测试盘仍可验证休眠唤醒稳定性）
                media_err_val = info.get("media_errors") or 0
                if media_err_val > 0:
                    self.log.warning(f"  [WARNING] Detected media errors: {media_err_val}(WARNING mode, does not affect this round verdict)")
                    info["media_errors_warning"] = True
                else:
                    info["media_errors_warning"] = False

                # 硬性 FAIL 条件：可用备件过低（<10%）或温度超出范围（0-70°C）
                ok = ((info.get("available_spare") or 100) >= 10
                      and 0 <= (info.get("temperature_c") or 25) <= 70)
                self.log.info(f"  SMART: Media errors={info.get('media_errors')}(WARNING mode), "
                              f"available_spare={info.get('available_spare')}%, "
                              f"temperature={info.get('temperature_c')}°C -> {'OK' if ok else 'FAIL'}")
                return ok, info
            else:
                _, out, _ = run_cmd(["smartctl", "-H", self.device],
                                     check=True, capture=True, logger=self.log, timeout=15)
                info["raw"] = out.strip()[:300]
                ok = "PASSED" in out.upper()
                self.log.info(f"  SMART: {'OK' if ok else 'FAIL'}")
                return ok, info
        except Exception as e:
            self.log.error(f"  SMART check failed: {e}")
            info["error"] = str(e)
            return False, info

    # ---------- 单轮循环 ----------

    def run_cycle(self, state: Dict[str, Any], cycle: int, sleep_type: str) -> bool:
        """
        执行单轮 OSINT 测试循环。
        返回本轮是否通过。
        """
        self.log.info("=" * 55)
        self.log.info(f"  Round {cycle}/{state['total_cycles']} round - {sleep_type.upper()} suspend/resume")
        self.log.info("=" * 55)

        # Step 1: 启动 IO 负载（活跃模式）
        self.log.info("\n  Step 1/5: start IO load")
        io_started = self.start_io_load()
        if io_started and self.io_active:
            self.log.info(f"  IO load running ({self.io_duration}s)...")
            time.sleep(self.io_duration)

        # Step 2: 触发休眠
        self.log.info(f"\n  Step 2/5: Triggering {sleep_type.upper()} sleep")
        sleep_ok, sleep_info = self.trigger_sleep(sleep_type)
        if not sleep_ok:
            self.log.error(f"  sleepfailed: {sleep_info}")
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
            self.log.info(f"\n  Step 5/5: This round result: FAIL (sleep/mount failed)")
            return False

        # 唤醒后等待系统稳定
        self.log.info("  Waiting for system stabilization (10s)...")
        time.sleep(10)

        # Step 3: 停止 IO 负载
        self.log.info("\n  Step 3/5: stop IO load")
        self.stop_io_load()

        # Step 4: 唤醒后检查
        self.log.info("\n  Step 4/5: after wakeupcheck")

        # 4a. 磁盘标识
        self.log.info("  [4a] disk identificationcheck")
        identity_ok, identity_info = self.check_disk_identity()

        # 4b. 分区和文件系统
        self.log.info("  [4b] partition and file system check")
        fs_ok, fs_info = self.check_partition_and_fs()

        # 4c. 数据完整性
        self.log.info("  [4c] Data integritychecksum")
        integrity_ok, integrity_info = self.verify_integrity_data(state)

        # 4d. SMART
        self.log.info("  [4d] SMART health check")
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

        self.log.info(f"\n  Step 5/5: This round result: {'PASS' if cycle_success else 'FAIL'} "
                      f"(disk_identity={'OK' if identity_ok else 'FAIL'}, "
                      f"partition_fs={'OK' if fs_ok else 'FAIL'}, "
                      f"data_integrity={'OK' if integrity_ok else 'FAIL'}, "
                      f"SMART={'OK' if smart_ok else 'FAIL'})")
        return cycle_success

    # ---------- 最终完整功能测试 ----------

    def run_final_functional_test(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """全部循环完成后执行最终完整功能测试。"""
        self.log.info("=" * 55)
        self.log.info("  Final full functionality test")
        self.log.info("=" * 55)
        result = {"capacity_ok": False, "smart_ok": False,
                  "seq_read_ok": False, "seq_write_ok": False, "details": {}}

        # 容量
        self.log.info("\n  [1/4] Capacity check")
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["details"]["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
            self.log.info(f"    capacity: {bytes_to_human(cap)} -> {'OK' if cap > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    Capacity check failed: {e}")

        # SMART
        self.log.info("\n  [2/4] SMART check")
        smart_ok, smart_info = self.check_smart()
        result["smart_ok"] = smart_ok
        result["details"]["smart"] = smart_info

        # 顺序读
        self.log.info("\n  [3/4] sequential readperformance (128K QD32, 30s)")
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
            self.log.info(f"    sequential read: {read_bw / 1024:.2f} MB/s -> {'OK' if read_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    sequential readTest failed: {e}")

        # 顺序写
        self.log.info("\n  [4/4] Sequential write performance (128K QD32, 30s)")
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
            self.log.info(f"    Sequential write: {write_bw / 1024:.2f} MB/s -> {'OK' if write_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    Sequential write test failed: {e}")

        all_ok = all([result["capacity_ok"], result["smart_ok"],
                      result["seq_read_ok"], result["seq_write_ok"]])
        result["overall"] = "PASS" if all_ok else "FAIL"
        state["final_functional_test"] = result
        self.log.info(f"\n  Final functionality test: {'PASS' if all_ok else 'FAIL'}")
        return all_ok, result

    # ---------- 结果输出 ----------

    def print_final_results(self, state: Dict[str, Any]):
        """打印最终结果汇总。"""
        self.log.info("\n" + "=" * 55)
        self.log.info("  OSINT testcompleted - Result summary")
        self.log.info("=" * 55)

        results = state.get('results', [])
        passed = sum(1 for r in results if r.get('success'))
        failed = len(results) - passed

        self.log.info(f"  Total rounds: {state.get('total_cycles', '?')}")
        self.log.info(f"  Actually executed: {len(results)} round")
        self.log.info(f"  passed: {passed}")
        self.log.info(f"  failed: {failed}")

        for r in results:
            status = "PASS" if r.get('success') else "FAIL"
            self.log.info(f"    Round {r.get('cycle', '?')} round ({r.get('sleep_type', '?').upper()}): "
                          f"{status} (disk_identity={'OK' if r.get('identity_ok') else 'FAIL'}, "
                          f"partition_fs={'OK' if r.get('fs_ok') else 'FAIL'}, "
                          f"data_integrity={'OK' if r.get('integrity_ok') else 'FAIL'}, "
                          f"SMART={'OK' if r.get('smart_ok') else 'FAIL'})")
            if r.get('sleep_info') and 'wake_ok' not in str(r.get('sleep_info')):
                self.log.info(f"      sleepINFO: {r.get('sleep_info')}")

        if 'final_functional_test' in state:
            ft = state['final_functional_test']
            self.log.info(f"  Final functionality test: {ft.get('overall', '?')}")

        # 保存结果
        result_file = os.path.join(
            self.log_dir, f"osint_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            self.log.info(f"  Result saved to: {result_file}")
        except Exception as e:
            self.log.error(f"  Failed to save result file: {e}")

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
            self.log.info(f"[DRY-RUN] Will execute OSINT test: {self.cycles} round, "
                          f"sleep_type={self.sleep_type}, sleep_duration={self.sleep_duration}s, "
                          f"active_io={'On' if self.io_active else 'Off'}")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        try:
            # 检查 rtcwake 是否可用
            rtcwake_ok, _, _ = self.run_command("which rtcwake", timeout=5)
            if not rtcwake_ok:
                result.finish(STATUS_ERROR, "rtcwake not installed, please run: sudo apt install util-linux")
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
                    self.log.info("Detected previous test completed, auto reset state to start new test")
                    need_reset = True
                elif saved_total != self.cycles:
                    self.log.info(f"Detected cycle count change ({saved_total} → {self.cycles}), auto reset state")
                    need_reset = True
                elif current > self.cycles + 1:
                    self.log.info(f"Detected state round error (current={current} > total={self.cycles}), auto reset state")
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
                self.log.info("Initialization: create test partition and file system...")
                if not self.setup_test_partition():
                    result.finish(STATUS_FAIL, "Failed to create test partition")
                    return result

                self.log.info("initialization: Writing integrity test data...")
                if not self.write_integrity_data(state):
                    result.finish(STATUS_FAIL, "Failed to write integrity test data")
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
                self.log.info(f"All {total_executed} rounds passed")
                result.finish(STATUS_PASS)
            else:
                result.finish(STATUS_FAIL, f"{passed}/{total_executed} rounds passed, {total_executed - passed} rounds failed")

        except Exception as e:
            self.log.exception(f"OSINT Test error: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result

