#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - PowerTester Module

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


class PowerTester:
    """设备功耗测量测试项。

    通过子进程启动 oscill 示波器上位机（python3 -m oscill.gui），
    由用户在 oscill GUI 中完成电压/电流采集与功耗计算，关闭 oscill 窗口后
    自动解析其导出的 JSON 测试报告并判定 PASS/FAIL。

    本类不实现任何测量逻辑，仅负责启动 oscill、等待完成、解析报告。
    """

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self._oscill_proc: Optional[subprocess.Popen] = None
        self._orig_sigterm = None
        self._orig_sigint = None

    def _build_oscill_env(self) -> dict:
        """v1.9.3 新增: 构建 oscill 子进程环境变量，传递自动采集参数。"""
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if self.cfg.power_plan:
            env["OSCILL_AUTO_PLAN"] = self.cfg.power_plan
        if self.cfg.power_total_duration is not None:
            env["OSCILL_AUTO_DURATION"] = str(self.cfg.power_total_duration)
        if self.cfg.power_sample_interval is not None:
            env["OSCILL_AUTO_INTERVAL"] = str(self.cfg.power_sample_interval)
        if self.cfg.power_channels:
            env["OSCILL_AUTO_CHANNELS"] = self.cfg.power_channels
        # JSON 自动导出目录：使用 output_dir，oscill 将按间隔自动导出整合JSON
        env["OSCILL_AUTO_JSON_DIR"] = os.path.abspath(self.cfg.output_dir)
        return env

    def _resolve_oscill_path(self) -> str:
        """解析 oscill 项目根目录。

        优先使用 config.oscill_path；为空时使用脚本所在目录（共置部署）。
        """
        if self.cfg.oscill_path and os.path.isdir(self.cfg.oscill_path):
            return os.path.abspath(self.cfg.oscill_path)
        # 共置部署：ssd_test_tool/ 根目录与 oscill/ 包同级
        # power.py 位于 ssd_test_tool/testers/power.py，需向上两级
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return script_dir

    def _validate_oscill_path(self, oscill_path: str) -> bool:
        """验证 oscill 项目路径包含 oscill/gui.py。"""
        gui_file = os.path.join(oscill_path, "oscill", "gui.py")
        if not os.path.isfile(gui_file):
            self.log.error(f"oscill project path invalid: {oscill_path}")
            self.log.error(f"  File not found: {gui_file}")
            self.log.error("  Ensure ssd_test_tool/ package is in the same directory as oscill/ package (co-located deployment),")
            self.log.error("  or configure the oscill project path correctly in the GUI parameters.")
            return False
        return True

    def _signal_handler(self, signum, frame):
        """收到终止信号时关闭 oscill 子进程。"""
        self.log.info(f"Signal {signum} received, shutting down power measurement tool...")
        self.terminate()

    def terminate(self):
        """终止 oscill 子进程（含进程组）。"""
        if self._oscill_proc and self._oscill_proc.poll() is None:
            try:
                # 先终止整个进程组
                try:
                    pgid = os.getpgid(self._oscill_proc.pid)
                    os.killpg(pgid, 15)  # SIGTERM
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                self._oscill_proc.terminate()
                try:
                    self._oscill_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        pgid = os.getpgid(self._oscill_proc.pid)
                        os.killpg(pgid, 9)  # SIGKILL
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    self._oscill_proc.kill()
                    self._oscill_proc.wait()
                self.log.info("Power measurement tool closed")
            except Exception as e:
                self.log.error(f"Error closing power measurement tool: {e}")

    def _find_latest_power_report(self, search_dir: str) -> Optional[str]:
        """在输出目录中查找最新的 oscill JSON 测试报告（含 power 字段）。"""
        if not os.path.isdir(search_dir):
            return None
        candidates = []
        for root, dirs, files in os.walk(search_dir):
            for fname in files:
                if not fname.lower().endswith(".json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    # 检测是否为 oscill 功耗报告：
                    # 1. 旧格式: 包含 power 或 measurements 字段
                    # 2. 整合格式(v1.9.3): combined_report=true 或包含 samples 数组或 final_summary
                    if isinstance(data, dict):
                        has_power = "power" in data
                        has_measurements = "measurements" in data
                        has_avg = any(k in data for k in ("average_power_w", "avg_power_w", "average_power"))
                        has_peak = any(k in data for k in ("peak_power_w", "max_power_w", "peak_power"))
                        # v1.9.3 新增: 整合格式JSON检测
                        has_combined = data.get("combined_report") is True
                        has_samples = isinstance(data.get("samples"), list) and len(data.get("samples", [])) > 0
                        has_final_summary = isinstance(data.get("final_summary"), dict)
                        is_power_report = (
                            has_power or has_measurements or (has_avg and has_peak)
                            or has_combined or has_samples or has_final_summary
                        )
                        if is_power_report:
                            mtime = os.path.getmtime(fpath)
                            candidates.append((mtime, fpath))
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    # v1.9.3 新增: JSON中文字段到英文的兜底映射（确保log纯英文）
    _JSON_ZH_TO_EN: dict[str, str] = {
        # measurement_type
        "最大电压": "Maximum Voltage",
        "最小电压": "Minimum Voltage",
        "平均电压": "Mean Voltage",
        "有效值电压": "RMS Voltage",
        "纹波峰峰值": "Ripple Peak-to-Peak",
        "平均电流": "Mean Current",
        "峰值电流": "Peak Current",
        "电流纹波": "Current Ripple",
        "频率": "Frequency",
        # result
        "TBD：未填写限值": "TBD (limits not specified)",
        "TBD: 未填写限值": "TBD (limits not specified)",
        "无信号": "No Signal",
        "未选择": "Not Selected",
        "通过": "PASS",
        "失败": "FAIL",
        # workload state
        "空闲": "Idle",
        "活动": "Active",
        "读写": "Read/Write",
        # test_phase
        "EVT": "EVT",
        "DVT": "DVT",
        "PVT": "PVT",
    }

    @classmethod
    def _en_text(cls, text: str) -> str:
        """将JSON中的中文字段转换为英文（兜底）。"""
        if not isinstance(text, str):
            return text
        return cls._JSON_ZH_TO_EN.get(text, text)

    def _convert_json_to_log(self, json_path: str) -> Optional[str]:
        """v1.9.3 新增: 将 oscill JSON 功耗报告转换为纯英文日志文件（.log）。

        Args:
            json_path: JSON 报告文件路径

        Returns:
            生成的 .log 文件路径，失败返回 None
        """
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log.error(f"Failed to read JSON report for log conversion: {e}")
            return None

        # 迭代查找功耗数值（避免递归溢出）
        def _find_value(keys, obj=None):
            if obj is None:
                obj = data
            stack = [obj]
            visited = set()
            while stack:
                current = stack.pop()
                if id(current) in visited:
                    continue
                visited.add(id(current))
                if isinstance(current, dict):
                    for k, v in current.items():
                        if k.lower() in keys:
                            return v
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(current, list):
                    for item in current:
                        if isinstance(item, (dict, list)):
                            stack.append(item)
            return None

        avg_power = _find_value({"average_power_w", "avg_power_w", "average_power", "avg_power", "mean_power_w", "average_w"})
        peak_power = _find_value({"peak_power_w", "max_power_w", "peak_power", "max_power", "peak_w"})
        energy_wh = _find_value({"energy_wh", "total_energy_wh", "energy"})
        duration_s = _find_value({"duration_s", "duration", "total_duration_s"})
        test_id = _find_value({"test_id", "test_plan", "plan"})
        timestamp = _find_value({"timestamp", "time", "date"})
        result = _find_value({"result", "status", "verdict", "pass"})
        power_max = _find_value({"power_max_w", "power_limit", "max_power_limit"})

        # 构建英文日志
        log_lines = []
        log_lines.append("=" * 60)
        log_lines.append("SSD DEVICE POWER CONSUMPTION MEASUREMENT LOG")
        log_lines.append("=" * 60)
        log_lines.append(f"Generated from: {os.path.basename(json_path)}")
        if timestamp:
            log_lines.append(f"Timestamp: {timestamp}")
        if test_id:
            log_lines.append(f"Test Plan: {test_id}")
        workload_state = data.get("workload", {}).get("state", "")
        if workload_state:
            log_lines.append(f"Workload State: {self._en_text(workload_state)}")
        test_phase = data.get("test_phase", "")
        if test_phase:
            log_lines.append(f"Test Phase: {test_phase}")
        log_lines.append("")

        # v1.9.3 新增: 检测整合JSON格式（samples数组），输出每个时间点的采集数据
        samples = data.get("samples", [])
        is_combined = data.get("combined_report", False) or bool(samples)
        if is_combined and samples:
            log_lines.append("-" * 40)
            log_lines.append("TIME-SERIES SAMPLES (integrated report)")
            log_lines.append("-" * 40)
            log_lines.append(f"  Total samples: {len(samples)}")
            if data.get("sample_interval_s"):
                log_lines.append(f"  Sample interval: {data['sample_interval_s']}s")
            if data.get("total_duration_s"):
                log_lines.append(f"  Total duration: {data['total_duration_s']}s")
            log_lines.append("")
            for idx, sample in enumerate(samples, 1):
                elapsed = sample.get("elapsed_seconds", "?")
                ts = sample.get("timestamp", "")
                log_lines.append(f"  --- Sample #{idx} @ {elapsed}s ({ts}) ---")
                sample_channels = sample.get("channels", {})
                for ch_name, ch_data in sample_channels.items():
                    if isinstance(ch_data, dict):
                        mtype = self._en_text(ch_data.get("measurement_type", "N/A"))
                        value = ch_data.get("value", "N/A")
                        unit = ch_data.get("unit", "")
                        ch_result = self._en_text(ch_data.get("result", "N/A"))
                        log_lines.append(f"    {ch_name}: {mtype} = {value} {unit}  [{ch_result}]")
                sample_power = sample.get("power", {})
                if sample_power and sample_power.get("average_W") is not None:
                    log_lines.append(f"    Power: avg={sample_power['average_W']}W, peak={sample_power.get('peak_W')}W, energy={sample_power.get('energy_Wh')}Wh")
                log_lines.append("")
            # 整合JSON的最终汇总
            final_summary = data.get("final_summary", {})
            if final_summary:
                log_lines.append("-" * 40)
                log_lines.append("FINAL SUMMARY (integrated report)")
                log_lines.append("-" * 40)
                if final_summary.get("average_W") is not None:
                    log_lines.append(f"  Average Power: {final_summary['average_W']} W")
                if final_summary.get("peak_W") is not None:
                    log_lines.append(f"  Peak Power:    {final_summary['peak_W']} W")
                if final_summary.get("energy_Wh") is not None:
                    log_lines.append(f"  Total Energy:  {final_summary['energy_Wh']} Wh")
                if final_summary.get("duration_s") is not None:
                    log_lines.append(f"  Duration:      {final_summary['duration_s']} s")
                log_lines.append("")

        # 通道测量数据（旧格式兼容）
        measurements = data.get("measurements", {})
        channels = measurements.get("channels", {})
        if channels and not is_combined:
            log_lines.append("-" * 40)
            log_lines.append("CHANNEL MEASUREMENTS")
            log_lines.append("-" * 40)
            for ch_name, ch_data in channels.items():
                if isinstance(ch_data, dict):
                    mtype = self._en_text(ch_data.get("measurement_type", "N/A"))
                    value = ch_data.get("value", "N/A")
                    unit = ch_data.get("unit", "")
                    ch_result = self._en_text(ch_data.get("result", "N/A"))
                    log_lines.append(f"  {ch_name}: {mtype} = {value} {unit}  [{ch_result}]")
            log_lines.append("")

        # 功耗统计（整合JSON优先使用final_summary）
        power = measurements.get("power", {})
        if is_combined and final_summary:
            avg_power = final_summary.get("average_W", avg_power)
            peak_power = final_summary.get("peak_W", peak_power)
            energy_wh = final_summary.get("energy_Wh", energy_wh)
            duration_s = final_summary.get("duration_s", duration_s)
        if power or avg_power or peak_power or energy_wh:
            log_lines.append("-" * 40)
            log_lines.append("POWER STATISTICS")
            log_lines.append("-" * 40)
            if avg_power is not None:
                try:
                    log_lines.append(f"  Average Power: {float(avg_power):.4f} W")
                except (ValueError, TypeError):
                    log_lines.append(f"  Average Power: {avg_power} W")
            if peak_power is not None:
                try:
                    log_lines.append(f"  Peak Power:    {float(peak_power):.4f} W")
                except (ValueError, TypeError):
                    log_lines.append(f"  Peak Power:    {peak_power} W")
            if energy_wh is not None:
                try:
                    log_lines.append(f"  Total Energy:  {float(energy_wh):.6f} Wh")
                except (ValueError, TypeError):
                    log_lines.append(f"  Total Energy:  {energy_wh} Wh")
            if duration_s is not None:
                try:
                    log_lines.append(f"  Duration:      {float(duration_s):.2f} s")
                except (ValueError, TypeError):
                    log_lines.append(f"  Duration:      {duration_s} s")
            log_lines.append("")

        # 限值与判定
        log_lines.append("-" * 40)
        log_lines.append("LIMITS AND VERDICT")
        log_lines.append("-" * 40)
        if power_max is not None:
            log_lines.append(f"  Power Limit:   {power_max} W")
        if result is not None:
            log_lines.append(f"  Result:        {self._en_text(result)}")
        # 重新判定
        if peak_power is not None and power_max is not None:
            try:
                if float(peak_power) > float(power_max):
                    log_lines.append(f"  Verdict:       FAIL (peak {float(peak_power):.4f}W > limit {float(power_max)}W)")
                else:
                    log_lines.append(f"  Verdict:       PASS (peak {float(peak_power):.4f}W <= limit {float(power_max)}W)")
            except (ValueError, TypeError):
                pass
        log_lines.append("")
        log_lines.append("=" * 60)
        log_lines.append("END OF POWER MEASUREMENT LOG")
        log_lines.append("=" * 60)

        # 写入 .log 文件
        log_path = os.path.splitext(json_path)[0] + ".log"
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(log_lines) + "\n")
            self.log.info(f"Power measurement log generated: {log_path}")
            return log_path
        except Exception as e:
            self.log.error(f"Failed to write power measurement log: {e}")
            return None

    def _parse_power_report(self, report_path: str) -> Tuple[str, str]:
        """解析 oscill JSON 报告，返回 (status, message)。"""
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return STATUS_ERROR, f"报告解析失败: {e}"

        # 迭代查找功耗数值（兼容多种 JSON 结构，避免递归溢出）
        def _find_value(keys, obj=None):
            if obj is None:
                obj = data
            stack = [obj]
            visited = set()
            while stack:
                current = stack.pop()
                if id(current) in visited:
                    continue
                visited.add(id(current))
                if isinstance(current, dict):
                    for k, v in current.items():
                        if k.lower() in keys:
                            return v
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(current, list):
                    for item in current:
                        if isinstance(item, (dict, list)):
                            stack.append(item)
            return None

        avg_power = _find_value({"average_power_w", "avg_power_w", "average_power", "avg_power", "mean_power_w"})
        peak_power = _find_value({"peak_power_w", "max_power_w", "peak_power", "max_power"})
        energy_wh = _find_value({"energy_wh", "total_energy_wh", "energy"})
        oscill_pass = _find_value({"pass", "passed", "result", "status", "verdict"})

        # 构建消息（全英文）
        parts = []
        if avg_power is not None:
            try:
                parts.append(f"Avg Power {float(avg_power):.3f} W")
            except (ValueError, TypeError):
                parts.append(f"Avg Power {avg_power}")
        if peak_power is not None:
            try:
                parts.append(f"Peak Power {float(peak_power):.3f} W")
            except (ValueError, TypeError):
                parts.append(f"Peak Power {peak_power}")
        if energy_wh is not None:
            try:
                parts.append(f"Energy {float(energy_wh):.4f} Wh")
            except (ValueError, TypeError):
                parts.append(f"Energy {energy_wh}")

        # 判定逻辑
        status = STATUS_PASS
        # 优先使用 oscill 侧判定
        if oscill_pass is not None:
            oscill_pass_str = str(oscill_pass).lower()
            if oscill_pass_str in ("fail", "failed", "false", "0", "error"):
                status = STATUS_FAIL
            elif oscill_pass_str in ("skip", "skipped", "unknown"):
                status = STATUS_SKIP
        # 用户指定限值覆盖
        if self.cfg.power_limit is not None and peak_power is not None:
            try:
                if float(peak_power) > float(self.cfg.power_limit):
                    status = STATUS_FAIL
                    parts.append(f"Exceeds limit {self.cfg.power_limit} W")
            except (ValueError, TypeError):
                pass

        message = ", ".join(parts) if parts else "Report parsed, see oscill JSON output for details"
        message += f" (Report: {os.path.basename(report_path)})"
        return status, message

    def run(self) -> TestResult:
        """执行设备功耗测量：启动 oscill GUI，等待用户操作完成，解析报告。"""
        result = TestResult(test_item=TEST_POWER, test_name="Device Power Consumption Measurement", device=self.cfg.device)
        result.start()

        oscill_path = self._resolve_oscill_path()
        self.log.info(f"oscill project path: {oscill_path}")

        if not self._validate_oscill_path(oscill_path):
            result.finish(STATUS_ERROR, "oscill project path invalid, cannot launch power measurement tool")
            return result

        if self.cfg.dry_run:
            self.log.info("[dry-run] simulating start oscill power consumption measurement tool")
            result.finish(STATUS_PASS)
            return result

        # 构建启动命令
        cmd = [sys.executable, "-m", "oscill.gui"]
        self.log.info(f"Launching power measurement tool: {' '.join(cmd)}")
        self.log.info(f"  Working directory: {oscill_path}")
        self.log.info("  Please complete power measurement in the oscill window, then close the window to continue...")

        try:
            # 注册信号处理，确保停止测试时 oscill 子进程被关闭
            try:
                import signal as _signal
                self._orig_sigterm = _signal.signal(_signal.SIGTERM, self._signal_handler)
                self._orig_sigint = _signal.signal(_signal.SIGINT, self._signal_handler)
            except (ValueError, OSError):
                # 非主线程或不支持信号的环境下跳过
                pass

            self._oscill_proc = subprocess.Popen(
                cmd,
                cwd=oscill_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._build_oscill_env(),
                start_new_session=True,
            )
            self.log.info(f"Power measurement tool launched, PID={self._oscill_proc.pid}")

            # v1.9.3 改进: 不阻塞等待oscill窗口关闭，改为轮询检测自动导出的JSON报告
            # oscill窗口保持打开，用户可查看结果；ssd_test检测到JSON报告即认为测量完成
            import time as _time
            json_dir = os.path.abspath(self.cfg.output_dir)
            # 记录启动前已有的整合JSON文件（避免误检测旧报告）
            existing_reports = set()
            if os.path.isdir(json_dir):
                for fn in os.listdir(json_dir):
                    if fn.startswith("power_report_combined_") and fn.endswith(".json"):
                        existing_reports.add(fn)
            self.log.info(f"Waiting for oscill auto-export JSON report (dir={json_dir})...")
            self.log.info("  oscill window will stay open after measurement; close it manually if needed.")
            report_path = None
            returncode = None
            max_wait_sec = 3600  # 最大等待1小时
            start_wait = _time.time()
            last_size = -1
            stable_count = 0
            while _time.time() - start_wait < max_wait_sec:
                # 检查oscill进程是否已退出（用户手动关闭窗口）
                rc = self._oscill_proc.poll()
                if rc is not None:
                    returncode = rc
                    self.log.info(f"Power measurement tool exited (user closed window), return code={returncode}")
                    break
                # 检查是否有新的整合JSON报告生成
                if os.path.isdir(json_dir):
                    new_reports = []
                    for fn in os.listdir(json_dir):
                        if fn.startswith("power_report_combined_") and fn.endswith(".json") and fn not in existing_reports:
                            new_reports.append(fn)
                    if new_reports:
                        # 取最新的一个（按文件名中的时间戳排序）
                        new_reports.sort()
                        candidate = os.path.join(json_dir, new_reports[-1])
                        try:
                            cur_size = os.path.getsize(candidate)
                            if cur_size == last_size and cur_size > 0:
                                stable_count += 1
                            else:
                                stable_count = 0
                                last_size = cur_size
                            # 文件大小连续2次稳定（间隔1秒），认为写入完成
                            if stable_count >= 2:
                                report_path = candidate
                                self.log.info(f"New oscill JSON report detected (stable): {os.path.basename(candidate)} ({cur_size} bytes)")
                                break
                        except OSError:
                            pass
                _time.sleep(1)
            else:
                self.log.warning("Timeout waiting for oscill JSON report (1 hour)")
            if report_path is None and returncode is None:
                # 超时且进程仍在运行，检查是否有新报告
                if os.path.isdir(json_dir):
                    for fn in os.listdir(json_dir):
                        if fn.startswith("power_report_combined_") and fn.endswith(".json") and fn not in existing_reports:
                            report_path = os.path.join(json_dir, fn)
                            break

        except FileNotFoundError:
            result.finish(STATUS_ERROR, f"Failed to launch python3: {sys.executable}")
            return result
        except Exception as e:
            self.log.exception(f"Exception launching power measurement tool: {e}")
            result.finish(STATUS_ERROR, f"Failed to launch power measurement tool: {e}")
            return result
        finally:
            # 恢复原始信号处理
            try:
                import signal as _signal
                if self._orig_sigterm is not None:
                    _signal.signal(_signal.SIGTERM, self._orig_sigterm)
                if self._orig_sigint is not None:
                    _signal.signal(_signal.SIGINT, self._orig_sigint)
            except (ValueError, OSError):
                pass

        # 解析 oscill JSON 报告（轮询已找到则直接使用）
        if report_path is None:
            report_path = self._find_latest_power_report(self.cfg.output_dir)
        # 如果用户指定了 JSON 报告路径，优先使用
        if self.cfg.power_json_report and os.path.isfile(self.cfg.power_json_report):
            report_path = self.cfg.power_json_report
            self.log.info(f"Using user-specified JSON report: {report_path}")

        # 被终止但已有JSON报告的情况：说明测量已完成，不skip
        if returncode in (-15, -9, 143, 137) and not report_path:
            self.log.info("Power measurement tool terminated before any JSON report was generated (measurement not completed)")
            result.finish(STATUS_SKIP, "Power measurement tool terminated (user stopped test before measurement completed)")
            return result

        if report_path:
            self.log.info(f"Power test report found: {report_path}")
            if returncode in (-15, -9, 143, 137):
                self.log.info("Power measurement tool terminated, but JSON report exists (measurement completed before termination)")
            # v1.9.3 新增: 转换为纯英文日志文件
            self._convert_json_to_log(report_path)
            status, message = self._parse_power_report(report_path)
            # v1.9.3 改进: 结果消息存details而非error_message，避免摘要误标为"Error"
            self.log.info(f"Power test result: {message}")
            result.details["power_result"] = message
            result.finish(status)
        else:
            self.log.warning("No oscill JSON power report found in output directory")
            self.log.warning("  Please use the Export Report function in oscill GUI to save JSON report to output directory")
            result.finish(STATUS_SKIP, "No oscill JSON report detected, measurement not completed (export report in oscill GUI and retry)")

        return result

