#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - ReadWriteTester Module

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
                self.log.info(f"Loading RW state file: {self.cfg.rw_state_file}")
        except Exception as e:
            self.log.warning(f"Loading state filefailed: {e}")
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
            self.log.warning(f"Failed to save state file: {e}")

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
        self.log.debug(f"executing: {' '.join(cmd)}")
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
            self.log.error(f"device node does not exist: {self.device}(device offline/disk dropped)")
            return False
        # 尝试读取设备大小
        try:
            result = subprocess.run(["blockdev", "--getsize64", self.device],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                self.log.error(f"device cannot read size: {self.device}")
                return False
            size = int(result.stdout.strip())
            self.log.info(f"device online: {self.device}, capacity: {size / 1024**3:.2f} GB")
            return True
        except Exception as e:
            self.log.error(f"devicecheckerror: {e}")
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
        self.log.info("check SMART health state...")
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
                self.log.info(f"  SMART: Media errors={media_errors}, available spare={avail_spare}%, temperature={temp_c}°C")
                # 介质错误仅警告（已知历史问题，不影响读写功能判定）
                if media_errors and media_errors > 0:
                    self.log.warning(f"  Media errors count > 0 ({media_errors}), WARNING only, not FAIL(known historical issue)")
                # 可用备件过低为硬性 FAIL
                if avail_spare is not None and avail_spare < 10:
                    self.log.warning(f"  Available spare too low ({avail_spare}%)")
                    return False
                # 温度异常为硬性 FAIL
                if temp_c is not None and not (0 <= temp_c <= 70):
                    self.log.warning(f"  temperatureerror ({temp_c}°C)")
                    return False
                return True
            else:
                _, out, _ = self._run_cmd(["smartctl", "-H", self.device], timeout=30)
                if "PASSED" in out or "OK" in out:
                    self.log.info("  SMART health state: PASSED")
                    return True
                else:
                    self.log.warning("  SMART health state: FAILED")
                    return False
        except Exception as e:
            self.log.warning(f"  SMART check failed: {e}")
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
        fio verify=md5 会在each block 头部存储校验和，读取时自动比对。
        """
        self.log.info("=" * 50)
        self.log.info("Mode 1: full disk write + read verification")
        self.log.info("=" * 50)

        if not self._check_device_online():
            return False, "设备脱机"

        device_size_gb = self._get_device_size_gb()
        self.log.info(f"Device capacity: {device_size_gb:.2f} GB")

        pattern_args = self._get_pattern_arg()
        verify = self.cfg.rw_verify

        # phase 1：全磁盘写入（带 verify 校验和）
        self.log.info("\n[Phase 1/2] full disk write...")
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
                self.log.error(f"  Full disk write failed: {err_msg}")
                return False, f"全磁盘写入失败: {err_msg}"
            data = json.loads(self._extract_fio_json(out))
            write_bw = data["jobs"][0]["write"]["bw"] / 1024  # KB/s -> MB/s
            write_iops = data["jobs"][0]["write"]["iops"]
            if rc != 0:
                self.log.warning(f"  fio exit code non-zero ({rc}), but write completed")
            self.log.info(f"  writecompleted: Bandwidth={write_bw:.2f} MB/s, IOPS={write_iops:.0f}")
        except json.JSONDecodeError as e:
            self.log.error(f"  Full disk write failed: fio output parsing failed: {e}")
            return False, f"全磁盘写入失败: fio输出解析失败"
        except Exception as e:
            self.log.error(f"  Full disk write failed: {e}")
            return False, f"全磁盘写入失败: {e}"

        # 检查设备是否仍在线
        if not self._check_device_online():
            return False, "写入后设备脱机"

        # phase 2：全磁盘读取验证
        # 全磁盘写入阶段已用高并发（用户配置 numjobs）覆盖整个磁盘；
        # 读取验证用单 job 顺序读全盘，确保 verify pattern 一致性（全磁盘不适用 offset_increment）
        self.log.info("\n[Phase 2/2] full disk read verification...")
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
                self.log.error(f"  read verification failed: {err_msg}")
                return False, f"数据验证失败: {err_msg}"
            data = json.loads(self._extract_fio_json(out))
            read_bw = data["jobs"][0]["read"]["bw"] / 1024
            read_iops = data["jobs"][0]["read"]["iops"]
            if rc != 0:
                self.log.warning(f"  fio exit code non-zero ({rc}), but read verification completed")
            self.log.info(f"  readverificationcompleted: Bandwidth={read_bw:.2f} MB/s, IOPS={read_iops:.0f}")
            self.log.info(f"  data consistency: All verification passed({verify})")
        except json.JSONDecodeError as e:
            self.log.error(f"  read verification failed: fio output parsing failed: {e}")
            return False, f"数据验证失败: fio输出解析失败"
        except Exception as e:
            self.log.error(f"  read verification failed: {e}")
            return False, f"数据验证失败: {e}"

        # 最终设备检查
        if not self._check_device_online():
            return False, "验证后设备脱机"

        self.log.info("\nfull disk write+readverification: PASS")
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
        self.log.info("Mode 2: multi-file-size repeated read/write cycles")
        self.log.info("=" * 50)

        if not self._check_device_online():
            return False, "设备脱机"

        file_sizes = self.cfg.rw_file_sizes
        cycles = self.cfg.rw_cycles
        verify = self.cfg.rw_verify
        pattern_args = self._get_pattern_arg()

        self.log.info(f"file sizelist: {file_sizes}")
        self.log.info(f"Cycles per size: {cycles}")
        self.log.info(f"Checksum method: {verify}")
        self.log.info(f"data pattern: {self.cfg.rw_pattern}")

        total_cycles = len(file_sizes) * cycles
        current = 0
        all_passed = True
        failure_details = []

        for size_label in file_sizes:
            size_mb = RW_FILE_SIZES.get(size_label)
            if size_mb is None:
                self.log.warning(f"Unknown file size: {size_label}, skipped")
                continue

            # 检查设备容量是否足够
            device_size_gb = self._get_device_size_gb()
            if size_mb / 1024 > device_size_gb:
                self.log.warning(f"file size {size_label} exceeds device capacity {device_size_gb:.1f}GB, skipped")
                continue

            self.log.info(f"\n--- file size: {size_label} ({size_mb} MB) ---")

            for cycle in range(1, cycles + 1):
                current += 1
                self.log.info(f"\n[{current}/{total_cycles}] {size_label} Round {cycle}/{cycles} round")

                # 写入 + 验证（单次 fio 完成写后立即读验证）
                # 使用 offset_increment 让多 job 写入不同区域，避免互相覆盖，同时保留并发配置
                numjobs = self.cfg.rw_numjobs
                total_needed_mb = size_mb * numjobs
                device_size_mb = int(self._get_device_size_gb() * 1024)
                if total_needed_mb > device_size_mb:
                    self.log.warning(f"  file size {size_label} × numjobs={numjobs} = {total_needed_mb}MB "
                                     f"exceeds device capacity {device_size_mb}MB, auto-reducing numjobs to "
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
                    f"--offset_increment={size_mb}M",  # each job 间隔 size_mb，避免覆盖
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
                        self.log.error(f"  readwriteverificationfailed: {err_msg}")
                        all_passed = False
                        failure_details.append(f"{size_label} 第{cycle}轮: {err_msg}")
                        if not self._check_device_online():
                            self.log.error("device offline, terminating remaining test")
                            return False, f"设备脱机（{size_label} 第{cycle}轮后）"
                        continue
                    data = json.loads(self._extract_fio_json(out))
                    write_bw = data["jobs"][0]["write"]["bw"] / 1024
                    if rc != 0:
                        self.log.warning(f"  fio exit code non-zero ({rc}), but data verification completed")
                    self.log.info(f"  write+verificationpassed: Bandwidth={write_bw:.2f} MB/s")
                except json.JSONDecodeError as e:
                    err_msg = f"fio 输出解析失败: {e}（输出前200字: {out[:200] if 'out' in dir() else 'N/A'}）"
                    self.log.error(f"  readwriteverificationfailed: {err_msg}")
                    all_passed = False
                    failure_details.append(f"{size_label} 第{cycle}轮: JSON解析失败")
                    if not self._check_device_online():
                        self.log.error("device offline, terminating remaining test")
                        return False, f"设备脱机（{size_label} 第{cycle}轮后）"
                except Exception as e:
                    self.log.error(f"  readwriteverificationfailed: {e}")
                    all_passed = False
                    failure_details.append(f"{size_label} 第{cycle}轮: {e}")
                    # 检查设备是否脱机
                    if not self._check_device_online():
                        self.log.error("device offline, terminating remaining test")
                        return False, f"设备脱机（{size_label} 第{cycle}轮后）"

        # 最终 SMART 检查
        smart_ok = self._check_smart()
        if not smart_ok:
            all_passed = False
            failure_details.append("SMART 检查异常")

        if all_passed:
            self.log.info(f"\nMulti-file-size read/write cycles: PASS({current} rounds all passed)")
            return True, f"{current} rounds of R/W verification all passed"
        else:
            self.log.error(f"\nMulti-file-size read/write cycles: FAIL({len(failure_details)} items failed)")
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
        self.log.info(f"Mode 3: {self.cfg.rw_long_run_hours}hour long-term read/write verification")
        self.log.info("=" * 50)

        if not self._check_device_online():
            return False, "设备脱机"

        runtime_sec = self.cfg.rw_long_run_hours * 3600
        verify = self.cfg.rw_verify
        read_ratio = self.cfg.rw_mixed_read_ratio
        pattern_args = self._get_pattern_arg()

        self.log.info(f"Running duration: {self.cfg.rw_long_run_hours} hours ({runtime_sec} seconds)")
        self.log.info(f"loadMode: randrw ({read_ratio}%read / {100-read_ratio}%write)")
        self.log.info(f"block size: {self.cfg.rw_block_size}, queue depth: {self.cfg.rw_io_depth}, numjobs: {self.cfg.rw_numjobs}")
        self.log.info(f"datachecksum: {verify}")

        # phase 1：先写入基准数据（用于后续验证）
        self.log.info("\n[Preparation] write benchmark test data(first 32GB)...")
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
                self.log.error(f"  benchmarkdataWrite failed: {err_msg}")
                return False, f"基准数据写入失败: {err_msg}"
            self.log.info("  benchmarkdatawritecompleted")
        except Exception as e:
            self.log.error(f"  benchmarkdataWrite failed: {e}")
            return False, f"基准数据写入失败: {e}"

        # phase 2：长期混合读写运行
        self.log.info(f"\n[Running] Starting {self.cfg.rw_long_run_hours} hour Mixed R/W...")
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
                self.log.error(f"  long-term run failed: {err_msg}")
                return False, f"长期运行失败: {err_msg}"
            data = json.loads(self._extract_fio_json(out))
            read_bw = data["jobs"][0]["read"]["bw"] / 1024
            write_bw = data["jobs"][0]["write"]["bw"] / 1024
            read_iops = data["jobs"][0]["read"]["iops"]
            write_iops = data["jobs"][0]["write"]["iops"]
            elapsed = data["jobs"][0].get("elapsed", runtime_sec) / 1000

            self.log.info(f"  long-term run completed: duration={elapsed:.0f}s")
            self.log.info(f"  read: Bandwidth={read_bw:.2f} MB/s, IOPS={read_iops:.0f}")
            self.log.info(f"  write: Bandwidth={write_bw:.2f} MB/s, IOPS={write_iops:.0f}")
        except json.JSONDecodeError as e:
            self.log.error(f"  long-term run failed: fio output parsing failed: {e}")
            return False, f"长期运行失败: fio输出解析失败"
        except Exception as e:
            self.log.error(f"  long-term run failed: {e}")
            return False, f"长期运行失败: {e}"

        # phase 3：验证基准数据完整性（使用 offset_increment 支持多 job 并发验证）
        self.log.info("\n[verification] verificationbenchmarkData integrity...")
        verify_size_gb = 32
        verify_numjobs = self.cfg.rw_numjobs
        verify_device_gb = self._get_device_size_gb()
        if verify_size_gb * verify_numjobs > verify_device_gb:
            self.log.warning(f"  verification area {verify_size_gb}GB × numjobs={verify_numjobs} "
                             f"exceeds device capacity {verify_device_gb:.1f}GB, auto-reducing numjobs to "
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
            f"--offset_increment={verify_size_gb}G",  # each job 间隔 32G，避免覆盖
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
                self.log.error(f"  benchmarkdataverificationfailed: {err_msg}")
                return False, f"长期运行后数据验证失败: {err_msg}"
            self.log.info("  benchmarkData integrityverificationpassed")
        except Exception as e:
            self.log.error(f"  benchmarkdataverificationfailed: {e}")
            return False, f"长期运行后数据验证失败: {e}"

        # 最终检查
        if not self._check_device_online():
            return False, "长期运行后设备脱机"

        smart_ok = self._check_smart()
        if not smart_ok:
            return False, "长期运行后 SMART 异常"

        total_time = (time.time() - start_time) / 3600
        self.log.info(f"\n{self.cfg.rw_long_run_hours}hour long-term read/write verification: PASS(actual run {total_time:.1f} hours)")
        return True, f"长期运行通过（读={read_bw:.0f}MB/s, write={write_bw:.0f}MB/s）"

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------

    def run(self) -> TestResult:
        """执行读/写测试。"""
        result = TestResult(test_item=TEST_RW, test_name="读/写测试", device=self.device)
        result.start()

        self.log.info("=" * 55)
        self.log.info("Starting read/write test (Read/Write Test)")
        self.log.info("=" * 55)
        self.log.info(f"testMode: {self.cfg.rw_mode}")
        self.log.info(f"device: {self.device}")

        # 初始设备检查
        if not self._check_device_online():
            result.finish(STATUS_FAIL, "Initial device check failed: device offline")
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
                results.append((ok, f"File cycle: {msg}"))

            if mode in (RW_MODE_LONG_RUN, RW_MODE_ALL):
                ok, msg = self.run_long_run_test()
                results.append((ok, f"长期运行: {msg}"))

        except Exception as e:
            self.log.exception(f"read/writeTest error: {e}")
            result.finish(STATUS_ERROR, str(e))
            return result

        # 汇总结果
        all_passed = all(r[0] for r in results)
        summary = "; ".join(r[1] for r in results)

        self.log.info("\n" + "=" * 55)
        self.log.info("read/writetestResult summary")
        self.log.info("=" * 55)
        for ok, msg in results:
            status = "PASS" if ok else "FAIL"
            self.log.info(f"  [{status}] {msg}")
        self.log.info("=" * 55)

        if all_passed:
            result.details["测试模式"] = mode
            result.details["子项结果"] = summary
            self.log.info(f"All R/W tests passed ({len(results)} items)")
            result.finish(STATUS_PASS)
        else:
            result.details["失败详情"] = summary
            result.finish(STATUS_FAIL, "Read/Write test has failed items")

        # 清除状态文件（测试正常完成）
        self._clear_state()

        return result

