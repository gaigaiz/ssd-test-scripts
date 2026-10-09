#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSD 自动化测试工具 (Modular Edition)
======================================
覆盖测试项目：
  1. 现场固件升级/降级 (Field Firmware Upgrade/Downgrade)
  2. 设备智能健康信息 (Device SMART Health Information)
  3. 设备容量 (Devices capacity)
  4. 完整性能特征 (Full Performance Characterization)
  5. 正常电源循环测试 (Normal Power Cycle Test)
  6. 意外电源循环测试 (Surprise Power Cycle Test, SPOR)
  7. 操作系统中断测试 (OS Interruption Test, OSINT)
  8. 读/写测试 (Read/Write Test)
  9. 设备功耗测量 (Device Power Consumption Measurement, 调用 oscill 示波器上位机)

适用平台：Linux Ubuntu 22.04+
Python  ：3.10+（仅标准库，GUI 使用 tkinter）

用法示例：
  # 命令行模式
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t smart

  # 可视化上位机模式（GUI）
  sudo python3 -m ssd_test_tool.main --gui
  python3 -m ssd_test_tool.main --gui          # 非 root 启动 GUI，执行测试时自动 pkexec 提权
"""

import argparse
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# GUI 相关库（条件导入，无显示环境时命令行模式不受影响）
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

from .common import (
    TestConfig, TestResult, PerfTask, TestReport,
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
from .i18n.translator import Translator, create_translator, I18N_ZH_CN, I18N_EN_US
from .testers.firmware import FirmwareTester
from .testers.smart import SmartTester
from .testers.capacity import CapacityTester
from .testers.performance import PerformanceTester
from .testers.power_cycle import PowerCycleTester
from .testers.spor import SPORTester, TimeboardController
from .testers.os_interruption import OSInterruptionTester
from .testers.read_write import ReadWriteTester
from .testers.power import PowerTester
from .gui.main_window import SSDTestGUI

# 包根目录（ssd_test_tool/），用于默认报告/日志路径
_PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_REPORT_DIR = os.path.join(_PACKAGE_ROOT, "reports")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="SSD 自动化测试脚本 - 固件升降级/SMART/容量/性能/电源循环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t all --fw-image fw.bin -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t smart
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t capacity
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t perf --perf-state fob -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t fw --fw-image fw.bin --fw-action upgrade
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t powercycle --pc-cycles 10 --ipmi-host 192.168.1.100 -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t powercycle --pc-cycles 5 --pc-power-mode manual -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t powercycle --pc-power-mode enhanced --pc-pattern-size-gb 20 -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t powercycle --pc-power-mode enhanced --ipmi-host 192.168.1.100 -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t spor --spor-power-mode enhanced --ipmi-host 192.168.1.100 -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t spor --spor-power-mode manual -y
  sudo python3 -m ssd_test_tool.main -d /dev/nvme0n1 -t spor --spor-power-mode timeboard --spor-cycles 20 -y
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
    parser.add_argument("--perf-state", default=PERF_UNKNOWN,
                        choices=[PERF_FOB, PERF_STEADY, PERF_UNKNOWN],
                        help="性能测试目标状态: fob/steady/unknown (默认: unknown 直接测试)")
    parser.add_argument("--perf-runtime", type=int, default=DEFAULT_PERF_RUNTIME,
                        help=f"单项 fio 运行时长（秒），默认 {DEFAULT_PERF_RUNTIME}")
    parser.add_argument("--precondition", default="on", choices=["on", "off"],
                        help="稳态预处理开关: on/off (默认: on)")
    parser.add_argument("--no-precondition", action="store_true",
                        help="跳过稳态预处理（等价于 --precondition off）")
    parser.add_argument("--precond-rand-speed", type=int, default=30,
                        help="稳态预处理4K随机写预估速度MB/s，用于超时计算 (默认: 30)")
    parser.add_argument("--precond-rand-max-sec", type=int, default=0,
                        help="随机写预处理最大时长秒，0=自动按速度估算 (默认: 0)")
    # 完整性能特征 - 全参数可配置（FIO原生参数对齐）
    parser.add_argument("--perf-name", default="perf-test",
                        help="FIO测试项目名称（--name），同步为日志文件名核心标识，默认: perf-test")
    parser.add_argument("--perf-direct", type=int, default=1, choices=[0, 1],
                        help="--direct，0/1切换，1=裸盘绕过系统缓存（默认）")
    parser.add_argument("--perf-ioengine", default="libaio",
                        help="--ioengine，direct=1时强制libaio，默认: libaio")
    parser.add_argument("--perf-bs", default="4k",
                        help="--bs 块大小，参考值: 4k/8k/16k/128k/1M，默认: 4k")
    parser.add_argument("--perf-iodepth", type=int, default=64,
                        help="--iodepth 队列深度，参考值: 1/8/32/64/128，默认: 64")
    parser.add_argument("--perf-numjobs", type=int, default=1,
                        help="--numjobs 并发任务数，参考值: 1/2/4/8，默认: 1")
    parser.add_argument("--perf-rw", default="randread",
                        choices=["randread", "randwrite", "randrw", "read", "write", "rw"],
                        help="--rw 读写模式，默认: randread")
    parser.add_argument("--perf-rwmixread", type=int, default=70,
                        help="--rwmixread 读占比(0-100)，仅混合模式(randrw/rw)生效，默认: 70")
    parser.add_argument("--perf-size", default="3%",
                        help="--size 测试范围，支持百分比(如3%%)或固定容量(如10G)，默认: 3%%")
    parser.add_argument("--perf-text-log", action="store_true",
                        help="同时输出文本格式日志（默认仅json+结构化日志）")
    parser.add_argument("--perf-task-file", default=None,
                        help="v1.8.0 多任务批量测试JSON文件路径，文件内为任务数组，每项包含perf_test_name/perf_rw/perf_bs等字段；指定后覆盖单组参数")
    # 电源循环测试参数
    parser.add_argument("--pc-cycles", type=int, default=DEFAULT_PC_CYCLES,
                        help=f"电源循环测试循环次数，默认 {DEFAULT_PC_CYCLES}")
    parser.add_argument("--pc-power-mode", default=DEFAULT_PC_POWER_MODE,
                        choices=["ipmi", "manual", "enhanced"],
                        help="电源控制模式: ipmi(远程自动)/manual(手动断电上电)/enhanced(增强模式:NVMe优雅移除+Pattern校验+PCIe Link校验) (默认: ipmi)")
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
    # enhanced（增强）模式专属参数
    parser.add_argument("--pc-pattern", default=DEFAULT_PC_PATTERN,
                        help=f"enhanced 模式写入数据 Pattern (默认: {DEFAULT_PC_PATTERN})")
    parser.add_argument("--pc-pattern-size-gb", type=int, default=DEFAULT_PC_PATTERN_SIZE_GB,
                        help=f"enhanced 模式 Pattern 数据写入量 GB (默认: {DEFAULT_PC_PATTERN_SIZE_GB})")
    parser.add_argument("--pc-no-link-check", action="store_true",
                        help="enhanced 模式禁用 PCIe Link 速率/宽度校验")
    # SPOR（意外电源循环测试）参数
    parser.add_argument("--spor-cycles", type=int, default=10,
                        help="SPOR 测试循环次数，默认 10")
    parser.add_argument("--spor-power-mode", default=DEFAULT_SPOR_POWER_MODE,
                        choices=["timeboard", "manual", "enhanced"],
                        help="SPOR 断电方式: timeboard(Timeboard硬件)/manual(手动断电)/enhanced(IPMI意外断电+高QD+增强校验) (默认: timeboard)")
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
    parser.add_argument("--spor-enhanced-iodepth", type=int, default=DEFAULT_SPOR_ENHANCED_IODEPTH,
                        help=f"enhanced 模式写入队列深度 (默认: {DEFAULT_SPOR_ENHANCED_IODEPTH})")
    parser.add_argument("--spor-enhanced-bs", default=DEFAULT_SPOR_ENHANCED_BS,
                        help=f"enhanced 模式写入块大小 (默认: {DEFAULT_SPOR_ENHANCED_BS})")
    # OSINT（操作系统中断测试）参数
    parser.add_argument("--osint-cycles", type=int, default=10,
                        help="OSINT 测试循环次数，默认 10")
    parser.add_argument("--osint-sleep-type", default="s3", choices=["s3", "s4", "both"],
                        help="OSINT sleep type: s3(suspend to RAM)/s4(suspend to disk)/both, default s3")
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
    # Read/Write 测试参数
    parser.add_argument("--rw-mode", default="all", choices=["full_disk", "file_cycle", "long_run", "all"],
                        help="读/写测试模式: full_disk(全磁盘验证)/file_cycle(多文件大小周期)/long_run(24h长期运行)/all，默认 all")
    parser.add_argument("--rw-file-sizes", nargs="+", default=["256MB", "1GB", "4GB", "16GB", "32GB"],
                        choices=["256MB", "1GB", "4GB", "16GB", "32GB"],
                        help="File cycle test file size list, multi-select: 256MB/1GB/4GB/16GB/32GB, default all")
    parser.add_argument("--rw-cycles", type=int, default=3,
                        help="File cycle test cycles per size, default 3")
    parser.add_argument("--rw-pattern", default="random", choices=["random", "0xAA", "0x55", "0x00", "0xFF"],
                        help="写入数据 pattern: random/0xAA/0x55/0x00/0xFF，默认 random")
    parser.add_argument("--rw-verify", default="md5", choices=["md5", "sha256", "crc32"],
                        help="数据校验方式: md5/sha256/crc32，默认 md5")
    parser.add_argument("--rw-long-hours", type=int, default=24,
                        help="长期运行测试小时数，默认 24")
    parser.add_argument("--rw-block-size", default="4k",
                        help="读写块大小，默认 4k")
    parser.add_argument("--rw-iodepth", type=int, default=32,
                        help="队列深度，默认 32")
    parser.add_argument("--rw-numjobs", type=int, default=4,
                        help="并发 job 数，默认 4")
    parser.add_argument("--rw-mixed-read-ratio", type=int, default=70,
                        help="长期运行混合读写读比例(%%)，默认 70")
    # v1.9.3 新增: 设备功耗测量参数
    parser.add_argument("--oscill-path", default="",
                        help="oscill 示波器项目根目录路径，默认空表示使用脚本所在目录（共置部署）")
    parser.add_argument("--power-limit", type=float, default=None,
                        help="设备功耗上限(W)，超过则判定 FAIL，默认空表示使用 oscill 侧限值")
    parser.add_argument("--power-plan", default="",
                        help="功耗测试规划 (POWER-01B~POWER-06), 留空则 oscill 手动选择")
    parser.add_argument("--power-total-duration", type=float, default=None,
                        help="功耗总采集时间(秒), 留空则手动控制, 设置后 oscill 自动停止")
    parser.add_argument("--power-sample-interval", type=float, default=None,
                        help="功耗采集间隔(秒), 留空则使用 oscill 默认 1 秒")
    parser.add_argument("--power-json-report", default="",
                        help="oscill JSON 功耗报告路径，将自动转换为英文日志文件")
    parser.add_argument("--power-channels", default="",
                        help="勾选的通道，逗号分隔，如 CH1,CH2，启动 oscill 时自动映射")
    parser.add_argument("-o", "--output-dir", default=_DEFAULT_REPORT_DIR,
                        help="Report output directory (default: ssd_test_tool/reports/)")
    parser.add_argument("--log-dir", default=get_default_log_dir(),
                        help="日志输出目录 (默认: 脚本同级 logs/ 目录，绝对路径)")
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式，只打印命令不执行")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过数据销毁确认提示")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志输出")
    # v1.9.2 新增: SSD 状态管理与稳态配置参数
    parser.add_argument("--action", default="run-tests",
                        choices=["run-tests", "enter-fob", "enter-steady", "status"],
                        help="动作类型: run-tests(执行测试)/enter-fob(仅进入FOB状态)/enter-steady(仅进入稳态)/status(查询当前状态) (默认: run-tests)")
    parser.add_argument("--purge-method", default="auto",
                        choices=["auto", "user-data", "blkdiscard"],
                        help="FOB擦除方式: auto(优先NVMe User Data Erase,失败回退blkdiscard)/user-data(仅NVMe Format --ses=1)/blkdiscard(仅blkdiscard) (默认: auto)")
    parser.add_argument("--steady-max-rounds", type=int, default=25,
                        help="WDPC稳态检测最大轮数 (默认: 25)")
    parser.add_argument("--steady-point-duration", type=int, default=60,
                        help="WDPC每个跟踪变量测试点运行秒数 (默认: 60)")
    parser.add_argument("--smart-ignore-media-errors", default="5353",
                        help="性能测试中SMART检查忽视的介质错误ID，逗号分隔 (默认: 5353)")
    parser.add_argument("--gui", action="store_true",
                        help="启动可视化上位机（GUI模式）")
    parser.add_argument("--version", action="version", version=f"ssd_test_tool v{SCRIPT_VERSION}")

    return parser.parse_args()


def main():
    """主入口。"""
    args = parse_args()

    # 初始化日志
    logger, log_file = setup_logging(args.log_dir, verbose=args.verbose)

    logger.info("=" * 55)
    logger.info(f"  SSD Automated Test Script v{SCRIPT_VERSION}")
    logger.info(f"  Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    # 1. root 权限检查
    if not check_root():
        logger.error("This script requires root privileges, please use sudo")
        sys.exit(1)

    # 2. 依赖检查
    deps_ok, missing = check_dependencies(logger)
    if not deps_ok:
        logger.error("Dependency tools not satisfied, cannot continue")
        sys.exit(1)

    # 3. 设备存在性检查
    if not os.path.exists(args.device):
        logger.error(f"Device does not exist: {args.device}")
        sys.exit(1)

    # 4. 构建设备配置
    test_items = args.test
    if TEST_ALL in test_items:
        test_items = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF, TEST_RW, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT]

    # v1.8.0 加载多任务批量测试配置（如果指定了 --perf-task-file）
    perf_task_list = []
    if args.perf_task_file:
        try:
            with open(args.perf_task_file, "r", encoding="utf-8") as f:
                task_data = json.load(f)
            if isinstance(task_data, list):
                for idx, td in enumerate(task_data):
                    task = PerfTask.from_dict(td)
                    perf_task_list.append(task)
                    logger.info(f"  Loaded test task {idx+1}: name={task.perf_test_name}, "
                                f"rw={task.perf_rw}, bs={task.perf_bs}, qd={task.perf_iodepth}"
                                + (f", note={task.note}" if task.note else ""))
            else:
                logger.error(f"Task file format error: top level should be array, got {type(task_data).__name__}")
                sys.exit(1)
            logger.info(f"  Loaded {len(perf_task_list)} test tasks total, will execute batch test")
        except FileNotFoundError:
            logger.error(f"Task file does not exist: {args.perf_task_file}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Task file JSON parse failed: {e}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to load task file: {e}")
            sys.exit(1)

    config = TestConfig(
        device=args.device,
        test_items=test_items,
        fw_image=args.fw_image,
        fw_action=args.fw_action,
        fw_slot=args.fw_slot,
        perf_state=args.perf_state,
        perf_runtime=args.perf_runtime,
        precondition=(args.precondition == "on") and (not args.no_precondition),
        precond_rand_speed_mb=args.precond_rand_speed,
        precond_rand_max_sec=args.precond_rand_max_sec,
        # 完整性能特征 - 全参数可配置
        perf_test_name=args.perf_name,
        perf_direct=args.perf_direct,
        perf_ioengine=args.perf_ioengine,
        perf_bs=args.perf_bs,
        perf_iodepth=args.perf_iodepth,
        perf_numjobs=args.perf_numjobs,
        perf_rw=args.perf_rw,
        perf_rwmixread=args.perf_rwmixread,
        perf_size=args.perf_size,
        perf_runtime_full=args.perf_runtime,
        perf_text_log=args.perf_text_log,
        perf_task_list=perf_task_list,
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
        pc_pattern=args.pc_pattern,
        pc_pattern_size_gb=args.pc_pattern_size_gb,
        pc_link_check=not args.pc_no_link_check,
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
        spor_power_mode=args.spor_power_mode,
        spor_enhanced_iodepth=args.spor_enhanced_iodepth,
        spor_enhanced_bs=args.spor_enhanced_bs,
        osint_cycles=args.osint_cycles,
        osint_sleep_type=args.osint_sleep_type,
        osint_sleep_duration=args.osint_sleep_duration,
        osint_io_active=not args.osint_io_idle,
        osint_io_duration=args.osint_io_duration,
        osint_mount_point=args.osint_mount_point,
        osint_state_file=args.osint_state_file,
        rw_mode=args.rw_mode,
        rw_file_sizes=args.rw_file_sizes,
        rw_cycles=args.rw_cycles,
        rw_pattern=args.rw_pattern,
        rw_verify=args.rw_verify,
        rw_long_run_hours=args.rw_long_hours,
        rw_block_size=args.rw_block_size,
        rw_io_depth=args.rw_iodepth,
        rw_numjobs=args.rw_numjobs,
        rw_mixed_read_ratio=args.rw_mixed_read_ratio,
        # v1.9.3 新增: 设备功耗测量配置
        oscill_path=args.oscill_path,
        power_limit=args.power_limit,
        power_plan=args.power_plan,
        power_total_duration=args.power_total_duration,
        power_sample_interval=args.power_sample_interval,
        power_json_report=args.power_json_report,
        power_channels=args.power_channels,
        output_dir=args.output_dir,
        log_dir=args.log_dir,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        verbose=args.verbose,
        # v1.9.2 新增
        purge_method=args.purge_method,
        steady_max_rounds=args.steady_max_rounds,
        steady_point_duration=args.steady_point_duration,
        smart_ignore_media_errors=args.smart_ignore_media_errors,
    )


    logger.info(f"  Device: {config.device} (type: {config.device_type})")
    logger.info(f"  Test items: {', '.join(config.test_items)}")
    if config.fw_image:
        logger.info(f"  Firmware image: {config.fw_image} ({config.fw_action})")
    logger.info(f"  Performance state: {config.perf_state}")
    # 全参数可配置 FIO 测试
    rw_desc = config.perf_rw
    if config.perf_rw in ("randrw", "rw"):
        rw_desc += f"(read {config.perf_rwmixread}%)"
    logger.info(f"  Full-param FIO: name={config.perf_test_name}, mode={rw_desc}, "
                f"bs={config.perf_bs}, QD={config.perf_iodepth}, numjobs={config.perf_numjobs}, "
                f"direct={config.perf_direct}, size={config.perf_size}, runtime={config.perf_runtime_full}s")
    if TEST_POWERCYCLE in config.test_items:
        _pc_extra = f", IPMI={config.ipmi_host}" if config.ipmi_host else ""
        if config.pc_power_mode == "enhanced":
            _pc_extra += f", Pattern={config.pc_pattern}, Size={config.pc_pattern_size_gb}GB"
            _pc_extra += f", LinkCheck={'On' if config.pc_link_check else 'Off'}"
        logger.info(f"  Power cycle: {config.pc_cycles} times, mode={config.pc_power_mode}{_pc_extra}")
    if TEST_SPOR in config.test_items:
        _spor_extra = ""
        if config.spor_power_mode == "enhanced":
            _spor_extra = f", iodepth={config.spor_enhanced_iodepth}, bs={config.spor_enhanced_bs}"
            if config.ipmi_host:
                _spor_extra += f", IPMI={config.ipmi_host}"
        logger.info(f"  SPOR unexpected power loss: {config.spor_cycles} times, mode={config.spor_power_mode}, "
                     f"delay={config.spor_delay}s, "
                     f"hardware_poweroff_delay={config.spor_poweroff_delay_ms}ms, "
                     f"mixed_rw={'On' if config.spor_mixed_rw else 'Off'}{_spor_extra}")
    if TEST_OSINT in config.test_items:
        logger.info(f"  OSINT interrupt: {config.osint_cycles} times, sleep={config.osint_sleep_type}, "
                     f"duration={config.osint_sleep_duration}s, "
                     f"active_io={'On' if config.osint_io_active else 'Off(idle)'}")
    if TEST_RW in config.test_items:
        logger.info(f"  Read/Write test: mode={config.rw_mode}, "
                     f"file_sizes={','.join(config.rw_file_sizes)}, "
                     f"cycles={config.rw_cycles}, verify={config.rw_verify}, "
                     f"long_run={config.rw_long_run_hours}h")
    logger.info(f"  Log file: {log_file}")
    logger.info("-" * 55)

    # 5. 数据销毁确认（性能测试、固件测试、电源循环测试、SPOR测试、OSINT测试、RW测试会破坏数据）
    destructive_items = {TEST_PERF, TEST_FW, TEST_SMART, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT, TEST_RW}
    if any(item in destructive_items for item in config.test_items):
        if not config.dry_run and not config.assume_yes:
            if not confirm_destructive(config.device, logger):
                logger.info("User cancelled operation, exiting")
                sys.exit(0)
        elif config.assume_yes:
            logger.info("-y specified, skipping data destruction confirmation")

    # 5.5 v1.9.2 新增: action 分发（状态管理动作）
    if args.action == "status":
        # 查询当前 SSD 状态
        serial = get_device_serial(config.device)
        state_info = load_ssd_state(serial)
        logger.info(f"  SSD serial: {serial}")
        logger.info(f"  Current state: {state_info['state'].upper()}")
        if state_info.get("timestamp"):
            logger.info(f"  State update time: {state_info['timestamp']}")
        if state_info.get("extra"):
            logger.info(f"  Additional info: {state_info['extra']}")
        print(f"SSD_STATE: {state_info['state']}")
        sys.exit(0)

    if args.action == "enter-fob":
        # 仅进入 FOB 状态，不执行性能测试
        logger.info("=" * 55)
        logger.info("  Action: Enter FOB state (NVMe User Data Erase)")
        logger.info("=" * 55)
        perf_tester = PerformanceTester(config, logger)
        purge_ok, purge_method = perf_tester._nvme_purge()
        if purge_ok:
            logger.info(f"  FOB erase succeeded (method={purge_method})")
            time.sleep(10)
            serial = get_device_serial(config.device)
            save_ssd_state(serial, SSD_STATE_FOB, {"method": purge_method})
            logger.info(f"  SSD state updated to FOB (serial={serial})")
            print("SSD_STATE: fob")
            sys.exit(0)
        else:
            logger.error(f"  FOB erase failed (method={purge_method})")
            print("SSD_STATE: error")
            sys.exit(1)

    if args.action == "enter-steady":
        # 仅进入稳态，不执行性能测试
        logger.info("=" * 55)
        logger.info("  Action: Enter steady state (WIPC + WDPC + steady state detection)")
        logger.info("=" * 55)
        perf_tester = PerformanceTester(config, logger)
        precond_ok, precond_info = perf_tester.precondition_steady_state()
        if precond_ok:
            logger.info(f"  Steady state preconditioning succeeded (rounds={precond_info.get('wdpc_rounds')}, "
                        f"steady_state_reached={precond_info.get('steady_reached')})")
            print("SSD_STATE: steady")
            sys.exit(0)
        else:
            logger.error(f"  Steady state preconditioning failed: {precond_info.get('error', 'unknown')}")
            print("SSD_STATE: error")
            sys.exit(1)

    # 6. 执行测试
    report = TestReport(config, logger)
    testers = {
        TEST_CAPACITY: CapacityTester(config, logger),
        TEST_SMART: SmartTester(config, logger),
        TEST_FW: FirmwareTester(config, logger),
        TEST_PERF: PerformanceTester(config, logger),
        TEST_RW: ReadWriteTester(config, logger),
        TEST_POWERCYCLE: PowerCycleTester(config, logger),
        TEST_SPOR: SPORTester(config, logger),
        TEST_OSINT: OSInterruptionTester(config, logger),
        TEST_POWER: PowerTester(config, logger),
    }

    # 按合理顺序执行：容量 -> SMART -> 固件 -> 性能 -> 读/写 -> 正常电源循环 -> 意外电源循环(SPOR) -> 操作系统中断(OSINT)
    execution_order = [TEST_CAPACITY, TEST_SMART, TEST_FW, TEST_PERF,
                       TEST_RW, TEST_POWERCYCLE, TEST_SPOR, TEST_OSINT, TEST_POWER]
    total = len(config.test_items)

    for idx, item in enumerate(execution_order, 1):
        if item not in config.test_items:
            continue
        tester = testers[item]
        item_name = {
            TEST_CAPACITY: "Device Capacity",
            TEST_SMART: "SMART Health Info",
            TEST_FW: "Firmware Upgrade/Downgrade",
            TEST_PERF: "Full Performance Characterization",
            TEST_RW: "Read/Write Test",
            TEST_POWERCYCLE: "Normal Power Cycle",
            TEST_SPOR: "Surprise Power Cycle (SPOR)",
            TEST_OSINT: "OS Interruption (OSINT)",
            TEST_POWER: "Device Power Consumption Measurement",
        }.get(item, item)
        # 测试项英文名称和编号（用于标准摘要行输出）
        item_enum = {
            TEST_CAPACITY: ("SSD_ST_001", "CAPACITY_TEST"),
            TEST_SMART: ("SSD_ST_002", "SMART_TEST"),
            TEST_FW: ("SSD_ST_003", "FW_TEST"),
            TEST_PERF: ("SSD_ST_004", "PERFORMANCE_TEST"),
            TEST_RW: ("SSD_ST_005", "RW_TEST"),
            TEST_POWERCYCLE: ("SSD_ST_006", "POWER_CYCLE_TEST"),
            TEST_SPOR: ("SSD_ST_007", "SPOR_TEST"),
            TEST_OSINT: ("SSD_ST_008", "OSINT_TEST"),
            TEST_POWER: ("SSD_ST_009", "POWER_CONSUMPTION_TEST"),
        }.get(item, (f"SSD_ST_{idx:03d}", item.upper()))

        logger.info(f"\n[{idx}/{total}] Starting test: {item_name}")
        logger.info("-" * 40)

        try:
            result = tester.run()
        except Exception as e:
            logger.exception(f"Test item {item_name} execution error: {e}")
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
            logger.info(f"  Error message: {result.error_message}")
        elif result.details.get("power_result"):
            logger.info(f"  Result: {result.details['power_result']}")

        # 输出标准摘要行（GUI 上位机识别此格式，只显示摘要行）
        # 格式: HH:MM:SS - [SSD_ST_XXX]ITEM_NAME,Status:PASS/FAIL,TestTime:NN S==========
        current_time = datetime.now().strftime("%H:%M:%S")
        test_time_sec = int(round(result.duration_sec))
        summary_line = (f"[SUMMARY] {current_time} - [{item_enum[0]}]{item_enum[1]},"
                        f"Status:{status_icon},TestTime:{test_time_sec} S"
                        + "=" * 10)
        logger.info(summary_line)

    # 7. 生成报告
    logger.info("\n")
    report_file = report.generate_json_report(config.output_dir)
    report.print_summary()

    logger.info(f"  Report file: {report_file}")
    logger.info(f"  Log file: {log_file}")
    logger.info("=" * 55)

    # 8. 退出码
    summary = report.get_summary()
    if summary["fail"] > 0 or summary["error"] > 0:
        sys.exit(1)
    sys.exit(0)


def run_gui():
    """启动可视化上位机。"""
    if not TKINTER_AVAILABLE:
        print("Error: tkinter is not available, please install python3-tk:")
        print("  sudo apt-get install python3-tk")
        sys.exit(1)

    root = tk.Tk()
    # 设置主题
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    app = SSDTestGUI(root)
    root.mainloop()


if __name__ == "__main__":
    # v1.9.2 新增: 全局异常捕获，确保任何未捕获错误都完整输出到 stderr
    try:
        # 检查是否为 GUI 模式
        if "--gui" in sys.argv:
            run_gui()
        else:
            main()
    except SystemExit:
        # sys.exit() 正常退出，不捕获
        raise
    except Exception as _e:
        import traceback
        print("\n" + "=" * 60, file=sys.stderr)
        print("  Uncaught exception during script execution", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"Exception type: {type(_e).__name__}", file=sys.stderr)
        print(f"Exception message: {_e}", file=sys.stderr)
        print("\nFull traceback:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"Command line args: {sys.argv}", file=sys.stderr)
        print(f"Python version: {sys.version}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        sys.exit(1)

