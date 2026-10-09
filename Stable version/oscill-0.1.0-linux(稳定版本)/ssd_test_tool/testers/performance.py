#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - PerformanceTester Module

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
from dataclasses import fields
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
            self.log.info("  Executing NVMe User Data Erase (--ses=1)...")
            try:
                # 用户指定命令: sudo nvme format /dev/nvme0n1 --namespace-id=1 --ses=1
                cmd = ["sudo", "nvme", "format", self.cfg.device,
                       "--namespace-id=1", "--ses=1"]
                run_cmd(cmd, check=True, capture=True, logger=self.log, timeout=600)
                self.log.info("  NVMe User Data Erase completed")
                return True, "nvme-user-data-erase"
            except Exception as e:
                self.log.warning(f"  NVMe User Data Erase failed: {e}")
                if purge_method == "user-data":
                    return False, "nvme-user-data-erase-failed"
                # auto 模式下继续回退 blkdiscard

        # 方式2: blkdiscard 回退
        if purge_method in ("auto", "blkdiscard"):
            self.log.info("  Executing blkdiscard (fallback method)...")
            try:
                run_cmd(["blkdiscard", self.cfg.device], check=True,
                        capture=True, logger=self.log, timeout=300)
                self.log.info("  blkdiscard completed")
                return True, "blkdiscard"
            except Exception as e:
                self.log.warning(f"  blkdiscard failed: {e}")
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
            self.log.info(f"    WDPC Round {round_idx} - {name}...")
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
                    self.log.warning(f"      Failed to parse fio output: {parse_err}")
                    results[name] = 0.0
            except Exception as e:
                self.log.warning(f"      WDPC {name} Test failed: {e}")
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
                        self.log.info(f"  SMART: Media error counter={media_errors}, "
                                      f"but all error log entries are ignored IDs ({ignore_ids})，considered normal")
                        details["media_errors_status"] = "ignored"
                    elif non_ignored:
                        self.log.warning(f"  SMART: Unignored media errors present: {non_ignored}")
                        details["media_errors_status"] = "warning"
                        details["non_ignored_errors"] = non_ignored
                        return False, details
                    else:
                        # 没有错误日志条目但计数器>0，可能是历史累积
                        self.log.info(f"  SMART: Media error counter={media_errors} (historical accumulation, no new error logs)")
                        details["media_errors_status"] = "historical"
                except Exception:
                    # 无法读取错误日志，按计数器>0警告
                    self.log.warning(f"  SMART: Media error counter={media_errors} (Cannot read error log details)")
                    details["media_errors_status"] = "warning"
                    return False, details
            else:
                details["media_errors_status"] = "normal"

            # 温度检查
            if temp_c > 80:
                self.log.warning(f"  SMART: Temperature too high {temp_c}°C")
                details["temperature_status"] = "warning"
                return False, details
            else:
                details["temperature_status"] = "normal"

            # 可用空间检查
            if spare < 10:
                self.log.warning(f"  SMART: Available spare too low {spare}%")
                details["spare_status"] = "warning"
                return False, details
            else:
                details["spare_status"] = "normal"

            return True, details
        except Exception as e:
            self.log.warning(f"  SMART health check failed (test not interrupted): {e}")
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
        self.log.info(f"[{state_label}] Full-param FIO test{task_tag}")
        self.log.info(f"  Name: {c.perf_test_name}, Mode: {c.perf_rw}, "
                      f"Block size: {c.perf_bs}, QD: {c.perf_iodepth}, "
                      f"numjobs: {c.perf_numjobs}")
        if c.perf_rw in ("randrw", "rw"):
            self.log.info(f"  Mixed R/W: Read ratio {c.perf_rwmixread}%")
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
            self.log.info("[DRY-RUN] skipping actual execution")
            result["dry_run"] = True
            return result

        # 确保日志目录存在（v1.9.2: 使用绝对路径）
        log_dir = resolve_log_dir(c.log_dir) if hasattr(c, "log_dir") and c.log_dir else get_default_log_dir()
        os.makedirs(log_dir, exist_ok=True)

        # 生成 json+ 日志文件名
        json_filename = self._generate_log_filename(state_label, "json", task=task)
        json_path = os.path.join(log_dir, json_filename)
        result["json_log"] = json_path
        self.log.info(f"  Structured log: {json_path}")

        # 构建并执行 FIO 命令
        cmd = self._build_full_fio_cmd(json_path, task=task)
        self.log.info(f"  Executing command: {' '.join(cmd)}")
        result["command"] = " ".join(cmd)

        try:
            timeout = c.perf_runtime_full + 120 if c.perf_time_based else 3600
            _, fio_out, fio_err = run_cmd(cmd, check=True, capture=True, logger=self.log, timeout=timeout)
            self.log.info(f"  FIO Execution completed, Log written: {json_path}")
            if fio_err.strip():
                self.log.debug(f"  fio stderr: {fio_err.strip()[:500]}")
            result["success"] = True
        except Exception as e:
            err_detail = ""
            if hasattr(e, 'stderr') and e.stderr:
                err_detail = f"\n  fio错误输出: {e.stderr[:1000]}"
            elif hasattr(e, 'output') and e.output:
                err_detail = f"\n  fio输出: {e.output[:1000]}"
            self.log.error(f"  FIO Execution failed: {e}{err_detail}")
            self.log.error(f"  Failed command: {' '.join(cmd)}")
            self.log.error(f"  Log path: {json_path}")
            # 检查 fio 版本
            try:
                _, fio_ver, _ = run_cmd(["fio", "--version"], check=False, capture=True, timeout=5)
                self.log.error(f"  fio version: {fio_ver.strip()}")
            except Exception:
                pass
            # 检查日志文件是否生成
            if os.path.exists(json_path):
                fsize = os.path.getsize(json_path)
                self.log.error(f"  Log file generated but may be incomplete: {json_path} ({fsize} bytes)")
            else:
                self.log.error(f"  Log file not generated: {json_path}")
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
            self.log.warning(f"  JSON Result parsing failed: {e}")
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
                self.log.info(f"  Text log: {text_path}")
            except Exception as e:
                err_detail = ""
                if hasattr(e, 'stderr') and e.stderr:
                    err_detail = f"\n  fio错误输出: {e.stderr[:500]}"
                self.log.warning(f"  Text logGeneration failed: {e}{err_detail}")

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
        self.log.info(f"  Performance test result summary [{state_label}]")
        self.log.info(f"  FIO version: {parsed.get('fio_version', 'unknown')}")
        self.log.info("=" * 60)

        for direction, label in [("read", "Read"), ("write", "Write")]:
            d = parsed.get(direction)
            if not d or d.get("bw_mibs", 0) == 0:
                continue
            self.log.info(f"  [{label}]")
            self.log.info(f"    Bandwidth: {d['bw_mibs']} MiB/s ({d['bw_mbs']} MB/s)")
            self.log.info(f"    IOPS: {d['iops']}")
            self.log.info(f"    Avg latency: {d['lat_mean_us']} us "
                          f"(min={d['lat_min_us']}, max={d['lat_max_us']})")
            pcts = d.get("percentiles_us", {})
            if pcts:
                pct_str = ", ".join([f"{k}={v}us" for k, v in list(pcts.items())[:8]])
                self.log.info(f"    Latency percentiles: {pct_str}")
            self.log.info("")

        if parsed.get("disk_util"):
            self.log.info(f"  Disk utilization: {parsed['disk_util']}%")
        self.log.info("=" * 60)
        self.log.info("")

    def run_fob_test(self) -> Dict[str, Any]:
        """FOB（出厂空白）状态性能测试。

        v1.8.0: 支持多任务批量执行。perf_task_list 非空时依次执行所有任务，
        否则执行单组参数测试（兼容旧版）。
        """
        self.log.info("-" * 40)
        self.log.info("FOB state performance test")
        self.log.info("-" * 40)

        result = {"state": "FOB", "precondition": "nvme-user-data-erase"}

        # FOB 前置：NVMe User Data Erase (--ses=1) 恢复空白状态，失败回退 blkdiscard
        # v1.9.2: 添加 precondition 检查，状态一致时可跳过擦除
        if not self.cfg.dry_run and self.cfg.precondition:
            purge_ok, purge_method = self._nvme_purge()
            result["precondition_method"] = purge_method
            if not purge_ok:
                self.log.error(f"  FOB erase failed (method={purge_method}), continuing test but state is unreliable")
                result["precondition_note"] = f"purge failed: {purge_method}"
            else:
                self.log.info(f"  FOB erase completed (method={purge_method}), Waiting 10s...")
                time.sleep(10)
            # 更新 SSD 状态文件为 FOB
            try:
                serial = get_device_serial(self.cfg.device)
                save_ssd_state(serial, SSD_STATE_FOB, {"method": purge_method})
                self.log.info(f"  SSD state updated to FOB (serial={serial})")
            except Exception as e:
                self.log.warning(f"  Failed to update SSD state file: {e}")
        elif not self.cfg.precondition:
            self.log.info("  skipping FOB erase (precondition=off, state is already consistent)")
            result["precondition"] = "skipped"
            result["precondition_method"] = "skipped"
        else:
            self.log.info("  [DRY-RUN] skipping FOB erase")

        # v1.8.0 多任务批量执行
        task_list = self.cfg.perf_task_list
        if task_list:
            result["batch_mode"] = True
            result["task_count"] = len(task_list)
            self.log.info(f"  [Batch mode] Total {len(task_list)} test tasks, executing sequentially...")
            perf_results = []
            for idx, task in enumerate(task_list, start=1):
                self.log.info(f"\n  ===== Batch task {idx}/{len(task_list)} =====")
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
        self.log.info("Starting steady-state preconditioning (SNIA SSS PTS v2.0.2)...")
        self.log.info("=" * 50)

        # 获取设备容量
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.cfg.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap_bytes = int(out.strip())
            cap_gb = cap_bytes / (1000 ** 3)
            info["device_capacity_gb"] = round(cap_gb, 2)
            self.log.info(f"Device capacity: {cap_gb:.2f} GB")
        except Exception as e:
            self.log.error(f"Cannot get device capacity, preconditioning failed: {e}")
            info["error"] = str(e)
            return False, info

        if self.cfg.dry_run:
            self.log.info("[DRY-RUN] skipping steady-state preconditioning execution")
            info["success"] = True
            return True, info

        # 步骤 1: WIPC - 顺序写满全盘 2 次（128K QD32）
        self.log.info("[Preconditioning 1/2] WIPC: sequential write full disk 2 passes...")
        seq_est_speed = 300 * 1024 * 1024
        seq_timeout_per_pass = max(600, int(cap_bytes / seq_est_speed) + 300)
        self.log.info(f"  Estimated single-pass duration: ~{int(cap_bytes / seq_est_speed)}s (at300MB/s), single-pass timeout: {seq_timeout_per_pass}s")
        try:
            for pass_idx in range(2):
                self.log.info(f"  Sequential write pass {pass_idx + 1}/2 pass...")
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
            self.log.error(f"WIPC sequential write preconditioning failed: {e}")
            info["steps"].append(f"wipc_sequential_write_2pass: FAIL ({e})")
            info["error"] = f"WIPC 顺序写预处理失败: {e}"
            return False, info

        # 步骤 2: WDPC - 多轮循环 + 稳态检测
        max_rounds = self.cfg.steady_max_rounds  # 默认25
        point_duration = self.cfg.steady_point_duration  # 默认60秒
        self.log.info(f"[Preconditioning 2/2] WDPC: Max {max_rounds} rounds, per point {point_duration}s, steady-state detection (5-round window)...")

        # 初始化每个跟踪变量的历史数据
        history = {var["name"]: [] for var in self.STEADY_TRACKING_VARS}
        steady_reached = False

        for round_idx in range(1, max_rounds + 1):
            self.log.info(f"  --- WDPC Round {round_idx}/{max_rounds} ---")
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
                    self.log.info(f"  *** Round {round_idx} steady state detected! 3 tracking variables all meet criteria ***")
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
                    self.log.info(f"  Round {round_idx} steady state not reached, unmet variables: {not_steady_vars}")
                    for name in not_steady_vars:
                        m = details[name]["metrics"]
                        if "range_pct" in m:
                            self.log.info(f"    {name}: range%={m['range_pct']}% "
                                          f"(<=20%? {'Y' if m['range_pct'] <= 20 else 'N'}), "
                                          f"slope%={m['slope_pct']}% "
                                          f"(<=10%? {'Y' if m['slope_pct'] <= 10 else 'N'})")
            else:
                self.log.info(f"  (Round {round_idx}, less than 5 rounds accumulated, skipping steady-state detection)")

        if not steady_reached:
            self.log.warning(f"  WDPC Max rounds reached {max_rounds}, steady state still not detected")
            info["steps"].append(f"wdpc_max_rounds_reached: {max_rounds} (未稳态)")
            # 达到最大轮数后视为接近稳态，告警继续（降级策略）
            self.log.warning("  Degraded: Max rounds reached, treating as near-steady-state, continuing performance test")
            info["precondition_note"] = f"WDPC达到最大轮数{max_rounds}未稳态，降级继续"

        # 更新 SSD 状态文件为 STEADY
        try:
            serial = get_device_serial(self.cfg.device)
            save_ssd_state(serial, SSD_STATE_STEADY, {
                "wdpc_rounds": info["wdpc_rounds"],
                "steady_reached": info["steady_reached"],
            })
            self.log.info(f"  SSD state updated to STEADY (serial={serial})")
        except Exception as e:
            self.log.warning(f"  Failed to update SSD state file: {e}")

        info["success"] = True
        self.log.info("Steady-state preconditioning completed")
        return True, info

    def run_steady_state_test(self) -> Dict[str, Any]:
        """稳态性能测试。

        v1.8.0: 支持多任务批量执行。稳态预处理只需执行一次，之后依次运行所有任务。
        perf_task_list 为空时执行单组参数测试（兼容旧版）。
        """
        self.log.info("-" * 40)
        self.log.info("Steady-state performance test")
        self.log.info("-" * 40)

        result = {"state": "steady"}

        # 稳态预处理
        if self.cfg.precondition:
            precond_ok, precond_info = self.precondition_steady_state()
            result["precondition"] = precond_info
            if not precond_ok:
                result["precondition_failed"] = True
                self.log.error("Steady-state preconditioning failed, skipping steady-state performance test")
                return result
        else:
            self.log.info("  skipping steady-state preconditioning (--precondition off)")
            result["precondition"] = {"skipped": True}

        # v1.8.0 多任务批量执行（预处理只需一次，之后依次运行所有任务）
        task_list = self.cfg.perf_task_list
        if task_list:
            result["batch_mode"] = True
            result["task_count"] = len(task_list)
            self.log.info(f"  [Batch mode] Total {len(task_list)} test tasks, executing sequentially...")
            perf_results = []
            for idx, task in enumerate(task_list, start=1):
                self.log.info(f"\n  ===== Batch task {idx}/{len(task_list)} =====")
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
            self.log.info("[DRY-RUN] Will execute full performance characterization test")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        try:
            # 确认设备未挂载
            try:
                _, out, _ = run_cmd(["findmnt", "-n", "-o", "TARGET", self.cfg.device],
                                     check=False, capture=True, logger=self.log, timeout=5)
                if out.strip():
                    self.log.warning(f"Device may be mounted: {out.strip()}, recommend unmounting before testing")
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
                self.log.info("Unknown state: executing performance test directly (skipping FOB erase and steady-state preconditioning)")
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
                    result.finish(STATUS_FAIL, "Steady-state preprocessing failed, performance test not executed")
                    return result

            result.finish(STATUS_PASS)
            self.log.info("Full performance characterization test completed")

        except Exception as e:
            self.log.exception(f"Performance test error: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result

