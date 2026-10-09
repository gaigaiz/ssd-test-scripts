#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - GUI Main Window (SSDTestGUI)

Auto-extracted from ssd_test_v1.9.3.py for modularization.
Contains the full SSDTestGUI class (~2400 lines).
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
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..common import (
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
from ..i18n.translator import Translator, create_translator, I18N_ZH_CN, I18N_EN_US, _I18N_ZH_TEXTS
from ..testers.firmware import FirmwareTester
from ..testers.smart import SmartTester
from ..testers.capacity import CapacityTester
from ..testers.performance import PerformanceTester
from ..testers.power_cycle import PowerCycleTester
from ..testers.spor import SPORTester, TimeboardController
from ..testers.os_interruption import OSInterruptionTester
from ..testers.read_write import ReadWriteTester
from ..testers.power import PowerTester


class SSDTestGUI:
    """SSD 自动化测试可视化上位机。

    通过 subprocess 调用命令行模式执行测试，实时显示日志和进度。
    仅依赖 Python 标准库 tkinter，无需额外安装。
    """

    # 测试项定义：(标识, 显示名称, 描述)
    TEST_ITEMS = [
        (TEST_FW, "固件升降级", "现场固件升级/降级测试"),
        (TEST_SMART, "SMART健康", "设备智能健康信息检查"),
        (TEST_CAPACITY, "设备容量", "容量读取与三源交叉校验"),
        (TEST_PERF, "性能测试", "FOB+稳态完整性能特征"),
        (TEST_RW, "读/写测试", "全磁盘验证+多文件大小周期+24h长期运行"),
        (TEST_POWERCYCLE, "正常电源循环", "正常关机断电开机循环测试"),
        (TEST_SPOR, "意外电源循环", "SPOR 意外断电测试（需Timeboard）"),
        (TEST_OSINT, "操作系统中断", "S3/S4 休眠唤醒稳定性测试"),
        (TEST_POWER, "设备功耗测量", "验证SSD空闲/活动R/W功耗，调用oscill示波器上位机"),
    ]

    def __init__(self, root: "tk.Tk"):
        self.root = root
        # v1.9.3 i18n: 翻译器实例
        self.translator = Translator(I18N_ZH_CN)
        self._tr_widgets = {}  # 存储需要重翻译的 widget 引用
        self.root.title(self.translator.tr("app.title") + f" v{SCRIPT_VERSION}")
        self.root.geometry("1100x880")
        self.root.minsize(960, 720)

        # 状态变量
        self.test_process: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.device_list: List[Dict[str, str]] = []
        self.selected_tests: Dict[str, tk.BooleanVar] = {}
        self.config_vars: Dict[str, Any] = {}
        self.perf_tasks: List[PerfTask] = []  # v1.8.0 多任务批量测试列表
        # v1.9.2 新增: SSD 状态管理
        self.current_ssd_state: str = SSD_STATE_UNKNOWN  # 当前 SSD 状态 (unknown/fob/steady)
        self._skip_precondition: bool = False  # v1.9.2: 状态一致时跳过预处理标志
        self.ssd_state_label: Optional["ttk.Label"] = None  # 状态显示标签引用
        self.ssd_state_color_label: Optional["tk.Label"] = None  # 带颜色的状态标签
        # ssd_test_tool 包根目录，用于默认报告路径
        self._ssd_test_tool_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 构建界面
        self._build_menu()
        self._build_main_layout()

        # 窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动后自动扫描设备
        self.root.after(300, self.scan_devices)

    # ----------------------------------------------------------
    # 界面构建
    # ----------------------------------------------------------

    def _build_menu(self):
        """构建菜单栏（v1.9.3 i18n: 支持运行时重翻译）。"""
        tr = self.translator.tr
        self._menubar = tk.Menu(self.root)

        # 文件菜单
        self._menu_file = tk.Menu(self._menubar, tearoff=0)
        self._menu_file.add_command(label=tr("menu.save_config"), command=self.save_config, accelerator="Ctrl+S")
        self._menu_file.add_command(label=tr("menu.load_config"), command=self.load_config, accelerator="Ctrl+O")
        self._menu_file.add_separator()
        self._menu_file.add_command(label=tr("menu.export_log"), command=self.export_log)
        self._menu_file.add_separator()
        self._menu_file.add_command(label=tr("menu.exit"), command=self._on_close)
        self._menubar.add_cascade(label=tr("menu.file"), menu=self._menu_file)

        # 工具菜单
        self._menu_tools = tk.Menu(self._menubar, tearoff=0)
        self._menu_tools.add_command(label=tr("menu.scan_devices"), command=self.scan_devices, accelerator="F5")
        self._menu_tools.add_command(label=tr("menu.clear_log"), command=self.clear_log, accelerator="Ctrl+L")
        self._menu_tools.add_command(label=tr("menu.open_report_dir"), command=self._open_report_dir)
        self._menu_tools.add_separator()
        self._menu_tools.add_command(label=tr("menu.launch_power_tool"), command=self.launch_oscill_power_tool, accelerator="Ctrl+P")
        self._menubar.add_cascade(label=tr("menu.tools"), menu=self._menu_tools)

        # 帮助菜单（关于）
        self._menu_tool_usage = tk.Menu(self._menubar, tearoff=0)
        self._menu_tool_usage.add_command(label=tr("dialog.tool_usage_title"), command=self._show_about)
        self._menubar.add_cascade(label=tr("menu.tool_usage"), menu=self._menu_tool_usage)

        self.root.config(menu=self._menubar)

        # 快捷键绑定
        self.root.bind("<Control-s>", lambda e: self.save_config())
        self.root.bind("<Control-o>", lambda e: self.load_config())
        self.root.bind("<Control-l>", lambda e: self.clear_log())
        self.root.bind("<F5>", lambda e: self.scan_devices())

    def _build_main_layout(self):
        """构建主界面布局（v1.9.3 i18n: 支持运行时重翻译）。"""
        tr = self.translator.tr
        # 顶部：设备选择面板
        self._top_frame = ttk.LabelFrame(self.root, text=tr("frame.device_select"), padding=8)
        self._top_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        self._label_dut = ttk.Label(self._top_frame, text=tr("label.device_under_test"))
        self._label_dut.pack(side=tk.LEFT)
        self.device_combo = ttk.Combobox(self._top_frame, width=50, state="readonly")
        self.device_combo.pack(side=tk.LEFT, padx=5)
        self._btn_scan = ttk.Button(self._top_frame, text=tr("btn.scan"), command=self.scan_devices)
        self._btn_scan.pack(side=tk.LEFT, padx=5)
        self._btn_device_info = ttk.Button(self._top_frame, text=tr("btn.device_info"), command=self._show_device_info)
        self._btn_device_info.pack(side=tk.LEFT, padx=5)

        self.device_info_label = ttk.Label(self._top_frame, text="", foreground="gray")
        self.device_info_label.pack(side=tk.LEFT, padx=15)

        # 语言切换下拉框
        self._label_lang = ttk.Label(self._top_frame, text=tr("label.language"))
        self._label_lang.pack(side=tk.LEFT, padx=(15, 2))
        self._lang_var = tk.StringVar(value=Translator.language_display_name(self.translator.language))
        self._lang_combo = ttk.Combobox(self._top_frame, textvariable=self._lang_var, width=10,
                                         values=[Translator.language_display_name(l) for l in [I18N_ZH_CN, I18N_EN_US]],
                                         state="readonly")
        self._lang_combo.pack(side=tk.LEFT, padx=2)
        self._lang_combo.bind("<<ComboboxSelected>>", self._on_language_change)

        # 顶部右侧：开始测试按钮（绿色醒目，从底部移至顶部方便操作）
        top_right_frame = ttk.Frame(self._top_frame)
        top_right_frame.pack(side=tk.RIGHT)
        self.start_btn = tk.Button(top_right_frame, text=tr("btn.start_test"), command=self.start_test,
                                    bg="#2e7d32", fg="white",
                                    font=("TkDefaultFont", 12, "bold"),
                                    activebackground="#1b5e20", activeforeground="white",
                                    relief=tk.RAISED, bd=2, padx=16, pady=5, cursor="hand2")
        self.start_btn.pack(side=tk.RIGHT, padx=4)

        # 中部：左侧测试项 + 右侧参数配置（先创建，最后 pack 以确保底部区域优先分配空间）
        middle_frame = ttk.Frame(self.root)
        # 不在此 pack，移到底部区域之后再 pack

        # 左侧：测试项选择（v1.9.3 i18n: 测试项名称/描述通过 translator 翻译）
        self._left_frame = ttk.LabelFrame(middle_frame, text=tr("frame.test_items"), padding=6)
        self._left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))
        self._left_frame.configure(width=170)

        self._test_item_widgets = []  # (checkbutton, desc_label, item_id)
        for item_id, item_name, item_desc in self.TEST_ITEMS:
            var = tk.BooleanVar(value=(item_id in [TEST_SMART, TEST_CAPACITY]))
            self.selected_tests[item_id] = var
            tr_key_name = f"test.{item_id}.name"
            tr_key_desc = f"test.{item_id}.desc"
            display_name = tr(tr_key_name) if tr_key_name in _I18N_ZH_TEXTS else item_name
            display_desc = tr(tr_key_desc) if tr_key_desc in _I18N_ZH_TEXTS else item_desc
            cb = ttk.Checkbutton(self._left_frame, text=display_name, variable=var,
                                  command=self._on_test_item_change)
            cb.pack(anchor=tk.W, pady=2)
            desc_label = ttk.Label(self._left_frame, text=f"  {display_desc}", foreground="gray",
                                    font=("TkDefaultFont", 8))
            desc_label.pack(anchor=tk.W, pady=(0, 4))
            self._test_item_widgets.append((cb, desc_label, item_id))

        # 全选/清空按钮
        btn_frame = ttk.Frame(self._left_frame)
        btn_frame.pack(fill=tk.X, pady=(8, 0))
        self._btn_select_all = ttk.Button(btn_frame, text=tr("btn.select_all"), command=lambda: self._set_all_tests(True))
        self._btn_select_all.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        self._btn_clear_all = ttk.Button(btn_frame, text=tr("btn.clear_all"), command=lambda: self._set_all_tests(False))
        self._btn_clear_all.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

        # 右侧：参数配置（Notebook）
        self._right_frame = ttk.LabelFrame(middle_frame, text=tr("frame.param_config"), padding=4)
        self._right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))

        self.notebook = ttk.Notebook(self._right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_general_tab()
        self._build_firmware_tab()
        self._build_performance_tab()
        self._build_powercycle_tab()
        self._build_spor_tab()
        self._build_osint_tab()
        self._build_rw_tab()

        # 状态栏（最底部，优先 pack 确保始终可见）
        self.status_var = tk.StringVar(value=f"{tr('status.ready')} | v{SCRIPT_VERSION} | {tr('app.stdlib_only')}")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=(8, 2))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # 底部控制栏（进度条 + 按钮，在状态栏上方，优先 pack 确保不被挤压）
        bottom_frame = ttk.Frame(self.root, padding=(8, 2, 8, 2))
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        # 进度条
        progress_frame = ttk.Frame(bottom_frame)
        progress_frame.pack(fill=tk.X, pady=(0, 2))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.progress_label = ttk.Label(progress_frame, text=tr("status.ready"), width=20)
        self.progress_label.pack(side=tk.RIGHT)

        # 控制按钮（开始测试按钮已移至顶部设备栏右侧）
        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(fill=tk.X)
        self.stop_btn = ttk.Button(btn_frame, text=tr("btn.stop"), command=self.stop_test, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        self._btn_clear_log = ttk.Button(btn_frame, text=tr("btn.clear_log_bottom"), command=self.clear_log)
        self._btn_clear_log.pack(side=tk.LEFT, padx=2)
        self._btn_save_cfg = ttk.Button(btn_frame, text=tr("btn.save_config_bottom"), command=self.save_config)
        self._btn_save_cfg.pack(side=tk.LEFT, padx=2)
        self._btn_load_cfg = ttk.Button(btn_frame, text=tr("btn.load_config_bottom"), command=self.load_config)
        self._btn_load_cfg.pack(side=tk.LEFT, padx=2)
        self._btn_export_log = ttk.Button(btn_frame, text=tr("btn.export_log_bottom"), command=self.export_log)
        self._btn_export_log.pack(side=tk.LEFT, padx=2)
        self._btn_view_log = ttk.Button(btn_frame, text=tr("btn.view_detail_log"), command=self._open_log_file)
        self._btn_view_log.pack(side=tk.LEFT, padx=2)

        # 测试结果摘要（固定高度，在底部控制栏上方，优先 pack 确保可见）
        self._log_frame = ttk.LabelFrame(self.root, text=tr("frame.test_summary"), padding=4)
        self._log_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(4, 4))

        self.log_text = scrolledtext.ScrolledText(self._log_frame, height=6, wrap=tk.WORD,
                                                     font=("Courier", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.X)
        # 日志颜色标签
        self.log_text.tag_configure("info", foreground="black")
        self.log_text.tag_configure("warning", foreground="orange")
        self.log_text.tag_configure("error", foreground="red")
        self.log_text.tag_configure("success", foreground="green")
        self.log_text.tag_configure("header", foreground="blue", font=("Courier", 9, "bold"))

        # 最后 pack 主体区域，填充顶部和底部之间的剩余空间
        middle_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

    def _build_general_tab(self):
        """通用参数标签页（v1.9.3 i18n）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.general"))

        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.output_dir")), "label.output_dir").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["output_dir"] = tk.StringVar(value=os.path.join(self._ssd_test_tool_root, "reports"))
        ttk.Entry(frame, textvariable=self.config_vars["output_dir"], width=30).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.log_dir")), "label.log_dir").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["log_dir"] = tk.StringVar(value=get_default_log_dir())
        ttk.Entry(frame, textvariable=self.config_vars["log_dir"], width=30).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self.config_vars["dry_run"] = tk.BooleanVar(value=False)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.dry_run"),
                        variable=self.config_vars["dry_run"]), "cb.dry_run").grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
        row += 1

        self.config_vars["assume_yes"] = tk.BooleanVar(value=True)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.assume_yes"),
                        variable=self.config_vars["assume_yes"]), "cb.assume_yes").grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
        row += 1

        self.config_vars["verbose"] = tk.BooleanVar(value=False)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.verbose"),
                        variable=self.config_vars["verbose"]), "cb.verbose").grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=8)
        row += 1

        ttk.Label(frame, text="执行命令预览：", foreground="blue").grid(row=row, column=0, sticky=tk.W, pady=4)
        row += 1
        self.cmd_preview = tk.Text(frame, height=4, width=60, wrap=tk.WORD,
                                    font=("Courier", 8), bg="#f5f5f5", state=tk.DISABLED)
        self.cmd_preview.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=4)

    def _build_firmware_tab(self):
        """固件参数标签页（v1.9.3 i18n）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.firmware"))

        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.fw_image")), "label.fw_image").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["fw_image"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["fw_image"], width=35).grid(row=row, column=1, sticky=tk.W, padx=5)
        self._tr_widget(ttk.Button(frame, text=tr("btn.browse"), command=self._browse_fw_image), "btn.browse").grid(row=row, column=2, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.fw_action")), "label.fw_action").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["fw_action"] = tk.StringVar(value=FW_UPGRADE)
        ttk.Combobox(frame, textvariable=self.config_vars["fw_action"], width=15,
                      values=[FW_UPGRADE, FW_DOWNGRADE], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.fw_slot")), "label.fw_slot").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["fw_slot"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["fw_slot"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        self._tr_widget(ttk.Label(frame, text=tr("label.fw_slot_hint"), foreground="gray"), "label.fw_slot_hint").grid(row=row, column=2, sticky=tk.W)
        row += 1

    def _build_performance_tab(self):
        """性能参数标签页（v1.9.3 i18n: 全参数可配置 FIO 测试 + 命令预览）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.performance"))

        row = 0
        # === v1.9.2: SSD 状态控制 + 批量任务管理（左右两栏布局） ===
        top_frame = ttk.Frame(frame)
        top_frame.grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=(0, 8))
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)

        # 左侧：SSD 状态控制
        state_frame = ttk.LabelFrame(top_frame, text=tr("frame.ssd_state"), padding=8)
        state_frame.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))

        self._tr_widget(ttk.Label(state_frame, text=tr("label.current_state"), font=("TkDefaultFont", 10, "bold")), "label.current_state").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.ssd_state_color_label = tk.Label(state_frame, text="UNKNOWN",
                                               bg="#9e9e9e", fg="white",
                                               font=("TkDefaultFont", 11, "bold"),
                                               padx=12, pady=3, width=10)
        self.ssd_state_color_label.grid(row=0, column=1, sticky=tk.W, padx=(0, 8))
        self.ssd_state_label = self._tr_widget(ttk.Label(state_frame, text=tr("label.serial"), foreground="gray"), "label.serial")
        self.ssd_state_label.grid(row=0, column=2, sticky=tk.W, padx=8)

        btn_frame = ttk.Frame(state_frame)
        btn_frame.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        self._tr_widget(ttk.Button(btn_frame, text=tr("btn.enter_fob"), command=lambda: self._enter_ssd_state(SSD_STATE_FOB)
                   ), "btn.enter_fob").pack(side=tk.LEFT, padx=(0, 6))
        self._tr_widget(ttk.Button(btn_frame, text=tr("btn.enter_steady"), command=lambda: self._enter_ssd_state(SSD_STATE_STEADY)
                   ), "btn.enter_steady").pack(side=tk.LEFT, padx=(0, 6))
        self._tr_widget(ttk.Button(btn_frame, text=tr("btn.refresh"), command=self._refresh_ssd_state
                   ), "btn.refresh").pack(side=tk.LEFT, padx=(0, 6))
        self._tr_widget(ttk.Button(btn_frame, text=tr("btn.reset_unknown"), command=self._reset_ssd_state
                   ), "btn.reset_unknown").pack(side=tk.LEFT, padx=(0, 6))

        # 右侧：批量任务管理
        task_frame = ttk.LabelFrame(top_frame, text=tr("frame.task_queue"), padding=8)
        task_frame.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))

        # 任务备注 + 新增按钮
        note_row = ttk.Frame(task_frame)
        note_row.pack(fill=tk.X, pady=(0, 4))
        self._tr_widget(ttk.Label(note_row, text=tr("label.note")), "label.note").pack(side=tk.LEFT)
        self.perf_task_note = tk.StringVar(value="")
        ttk.Entry(note_row, textvariable=self.perf_task_note, width=12).pack(side=tk.LEFT, padx=4)
        self._tr_widget(ttk.Button(note_row, text=tr("btn.add_task"), command=self._add_perf_task, width=10
                   ), "btn.add_task").pack(side=tk.LEFT, padx=4)

        # 任务列表 + 上下移按钮
        list_row = ttk.Frame(task_frame)
        list_row.pack(fill=tk.X, pady=2)
        self.perf_task_listbox = tk.Listbox(list_row, height=4, width=40,
                                             font=("Courier", 8), selectmode=tk.SINGLE,
                                             bg="#f5f5f5", relief=tk.SUNKEN)
        self.perf_task_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        task_scroll = ttk.Scrollbar(list_row, orient=tk.VERTICAL, command=self.perf_task_listbox.yview)
        task_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.perf_task_listbox.config(yscrollcommand=task_scroll.set)
        move_btn_frame = ttk.Frame(list_row)
        move_btn_frame.pack(side=tk.LEFT, padx=(4, 0), fill=tk.Y)
        ttk.Button(move_btn_frame, text="↑", command=self._move_task_up, width=3).pack(side=tk.TOP, pady=1)
        ttk.Button(move_btn_frame, text="↓", command=self._move_task_down, width=3).pack(side=tk.TOP, pady=1)

        # 操作按钮
        op_row = ttk.Frame(task_frame)
        op_row.pack(fill=tk.X, pady=(4, 0))
        self._tr_widget(ttk.Button(op_row, text=tr("btn.delete"), command=self._delete_selected_task, width=6), "btn.delete").pack(side=tk.LEFT, padx=2)
        self._tr_widget(ttk.Button(op_row, text=tr("btn.clear_all"), command=self._clear_all_tasks, width=6), "btn.clear_all").pack(side=tk.LEFT, padx=2)
        self._tr_widget(ttk.Button(op_row, text=tr("btn.export_json"), command=self._export_tasks_json, width=8), "btn.export_json").pack(side=tk.LEFT, padx=2)
        self.perf_task_count_label = self._tr_widget(ttk.Label(op_row, text=tr("label.task_count"), foreground="gray"), "label.task_count")
        self.perf_task_count_label.pack(side=tk.LEFT, padx=6)

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row+1, column=0, columnspan=4, sticky=tk.EW, pady=4)
        row += 2

        # === 基础配置 ===
        self._tr_widget(ttk.Label(frame, text=tr("label.perf_state")), "label.perf_state").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_state"] = tk.StringVar(value=PERF_UNKNOWN)
        ttk.Combobox(frame, textvariable=self.config_vars["perf_state"], width=12,
                      values=[PERF_FOB, PERF_STEADY, PERF_UNKNOWN], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # v1.9.2: 已删除稳态预处理UI（用户已有独立功能代码）
        self._tr_widget(ttk.Label(frame, text=tr("label.perf_test_name")), "label.perf_test_name").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_test_name"] = tk.StringVar(value="perf-test")
        name_entry = ttk.Entry(frame, textvariable=self.config_vars["perf_test_name"], width=20)
        name_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        name_entry.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=6)
        row += 1

        # === IO 负载配置 ===
        self._tr_widget(ttk.Label(frame, text=tr("label.io_load_config"), font=("TkDefaultFont", 9, "bold")), "label.io_load_config").grid(
            row=row, column=0, columnspan=4, sticky=tk.W, pady=(0, 2))
        row += 1

        # direct
        self._tr_widget(ttk.Label(frame, text=tr("label.direct")), "label.direct").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_direct"] = tk.IntVar(value=1)
        direct_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_direct"], width=8,
                                     values=[0, 1], state="readonly")
        direct_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        direct_combo.bind("<<ComboboxSelected>>", lambda e: self._on_perf_direct_change())
        # ioengine
        self._tr_widget(ttk.Label(frame, text=tr("label.ioengine")), "label.ioengine").grid(row=row, column=2, sticky=tk.W, pady=4, padx=(20, 0))
        self.config_vars["perf_ioengine"] = tk.StringVar(value="libaio")
        self.perf_ioengine_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_ioengine"], width=10,
                                                  values=["libaio", "sync", "psync", "vsync", "mmap"], state="readonly")
        self.perf_ioengine_combo.grid(row=row, column=3, sticky=tk.W, padx=5)
        self.perf_ioengine_combo.bind("<<ComboboxSelected>>", lambda e: self._update_perf_command_preview())
        row += 1

        # 块大小
        self._tr_widget(ttk.Label(frame, text=tr("label.bs")), "label.bs").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_bs"] = tk.StringVar(value="4k")
        bs_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_bs"], width=10,
                                 values=["4k", "8k", "16k", "32k", "64k", "128k", "256k", "512k", "1M", "2M"], state="normal")
        bs_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        bs_combo.bind("<<ComboboxSelected>>", lambda e: self._update_perf_command_preview())
        bs_combo.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        # 队列深度
        self._tr_widget(ttk.Label(frame, text=tr("label.iodepth")), "label.iodepth").grid(row=row, column=2, sticky=tk.W, pady=4, padx=(20, 0))
        self.config_vars["perf_iodepth"] = tk.IntVar(value=64)
        iodepth_spin = ttk.Spinbox(frame, from_=1, to=1024, textvariable=self.config_vars["perf_iodepth"], width=8,
                                    command=self._update_perf_command_preview)
        iodepth_spin.grid(row=row, column=3, sticky=tk.W, padx=5)
        iodepth_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        row += 1

        # 并发任务数
        self._tr_widget(ttk.Label(frame, text=tr("label.numjobs")), "label.numjobs").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_numjobs"] = tk.IntVar(value=1)
        numjobs_spin = ttk.Spinbox(frame, from_=1, to=256, textvariable=self.config_vars["perf_numjobs"], width=8,
                                    command=self._update_perf_command_preview)
        numjobs_spin.grid(row=row, column=1, sticky=tk.W, padx=5)
        numjobs_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        # 读写模式
        self._tr_widget(ttk.Label(frame, text=tr("label.rw")), "label.rw").grid(row=row, column=2, sticky=tk.W, pady=4, padx=(20, 0))
        self.config_vars["perf_rw"] = tk.StringVar(value="randread")
        rw_combo = ttk.Combobox(frame, textvariable=self.config_vars["perf_rw"], width=12,
                                 values=["randread", "randwrite", "randrw", "read", "write", "rw"], state="readonly")
        rw_combo.grid(row=row, column=3, sticky=tk.W, padx=5)
        rw_combo.bind("<<ComboboxSelected>>", lambda e: self._on_perf_rw_change())
        row += 1

        # 读写比例（仅混合模式启用）
        self._tr_widget(ttk.Label(frame, text=tr("label.rwmixread")), "label.rwmixread").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_rwmixread"] = tk.IntVar(value=70)
        self.perf_rwmix_spin = ttk.Spinbox(frame, from_=0, to=100, textvariable=self.config_vars["perf_rwmixread"], width=8,
                                             command=self._update_perf_command_preview)
        self.perf_rwmix_spin.grid(row=row, column=1, sticky=tk.W, padx=5)
        self.perf_rwmix_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        self._tr_widget(ttk.Label(frame, text=tr("label.rwmixread_hint")), "label.rwmixread_hint").grid(
            row=row, column=2, columnspan=2, sticky=tk.W, padx=(20, 0))
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=6)
        row += 1

        # === 测试范围与时长 ===
        self._tr_widget(ttk.Label(frame, text=tr("label.test_range"), font=("TkDefaultFont", 9, "bold")), "label.test_range").grid(
            row=row, column=0, columnspan=4, sticky=tk.W, pady=(0, 2))
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.size")), "label.size").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_size"] = tk.StringVar(value="3%")
        size_entry = ttk.Entry(frame, textvariable=self.config_vars["perf_size"], width=10)
        size_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        size_entry.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        self._tr_widget(ttk.Label(frame, text=tr("label.size_hint")), "label.size_hint").grid(
            row=row, column=2, columnspan=2, sticky=tk.W, padx=(20, 0))
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.runtime")), "label.runtime").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["perf_runtime_full"] = tk.IntVar(value=60)
        runtime_spin = ttk.Spinbox(frame, from_=1, to=86400, textvariable=self.config_vars["perf_runtime_full"], width=8,
                                    command=self._update_perf_command_preview)
        runtime_spin.grid(row=row, column=1, sticky=tk.W, padx=5)
        runtime_spin.bind("<KeyRelease>", lambda e: self._update_perf_command_preview())
        self._tr_widget(ttk.Label(frame, text=tr("label.runtime_hint")), "label.runtime_hint").grid(row=row, column=2, sticky=tk.W, padx=(20, 0))
        row += 1

        self.config_vars["perf_text_log"] = tk.BooleanVar(value=False)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.text_log"),
                        variable=self.config_vars["perf_text_log"]), "cb.text_log").grid(row=row, column=0, columnspan=4, sticky=tk.W, pady=2)
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=6)
        row += 1

        # === 命令预览 ===
        self._tr_widget(ttk.Label(frame, text=tr("label.cmd_preview"), font=("TkDefaultFont", 9, "bold")), "label.cmd_preview").grid(
            row=row, column=0, columnspan=4, sticky=tk.W, pady=(0, 2))
        row += 1

        self.perf_cmd_preview = tk.Text(frame, height=5, width=80, wrap=tk.WORD,
                                         font=("Courier", 9), bg="#f5f5f5", relief=tk.SUNKEN)
        self.perf_cmd_preview.grid(row=row, column=0, columnspan=4, sticky=tk.EW, pady=2)
        self.perf_cmd_preview.config(state=tk.DISABLED)
        row += 1

        # v1.9.2: 批量任务管理已移至顶部右侧区域

        # 初始化联动状态和命令预览
        self._on_perf_direct_change()
        self._on_perf_rw_change()
        self._update_perf_command_preview()

    def _on_perf_direct_change(self):
        """direct 参数变化联动：direct=1时ioengine锁定libaio。"""
        if self.config_vars["perf_direct"].get() == 1:
            self.config_vars["perf_ioengine"].set("libaio")
            self.perf_ioengine_combo.config(state="disabled")
        else:
            self.perf_ioengine_combo.config(state="readonly")
        self._update_perf_command_preview()

    def _on_perf_rw_change(self):
        """读写模式变化联动：非混合模式时rwmixread置灰。"""
        rw = self.config_vars["perf_rw"].get()
        if rw in ("randrw", "rw"):
            self.perf_rwmix_spin.config(state="normal")
        else:
            self.perf_rwmix_spin.config(state="disabled")
        self._update_perf_command_preview()

    def _update_perf_command_preview(self):
        """更新 FIO 命令预览区。"""
        if not hasattr(self, "perf_cmd_preview"):
            return
        c = self.config_vars
        device = self.device_list[self.device_combo.current()]["path"] if self.device_combo.current() >= 0 else "/dev/nvme0n1"
        cmd_parts = [
            "fio",
            f"--name={c['perf_test_name'].get()}",
            f"--filename={device}",
            f"--direct={c['perf_direct'].get()}",
            f"--bs={c['perf_bs'].get()}",
            f"--iodepth={c['perf_iodepth'].get()}",
            f"--numjobs={c['perf_numjobs'].get()}",
            f"--rw={c['perf_rw'].get()}",
            f"--ioengine={c['perf_ioengine'].get()}",
            f"--size={c['perf_size'].get()}",
            "--time_based",
            f"--runtime={c['perf_runtime_full'].get()}",
            "--group_reporting",
            "--lat_percentiles=1",
            "--output-format=json+",
            "--output=<日志目录>/<自动命名>.json",
        ]
        rw = c["perf_rw"].get()
        if rw in ("randrw", "rw"):
            cmd_parts.append(f"--rwmixread={c['perf_rwmixread'].get()}")
        if c["perf_direct"].get() == 1:
            cmd_parts.append("--buffered=0")
        if rw in ("randread", "randwrite", "randrw"):
            cmd_parts.append("--norandommap")
            cmd_parts.append("--randrepeat=0")

        cmd_str = " \\\n  ".join(cmd_parts)
        self.perf_cmd_preview.config(state=tk.NORMAL)
        self.perf_cmd_preview.delete("1.0", tk.END)
        self.perf_cmd_preview.insert("1.0", cmd_str)
        self.perf_cmd_preview.config(state=tk.DISABLED)

    # ----------------------------------------------------------
    # v1.8.0 批量任务管理
    # ----------------------------------------------------------

    def _add_perf_task(self):
        """将当前界面配置保存为一个测试任务。"""
        c = self.config_vars
        task = PerfTask(
            perf_test_name=c["perf_test_name"].get(),
            perf_direct=c["perf_direct"].get(),
            perf_ioengine=c["perf_ioengine"].get(),
            perf_bs=c["perf_bs"].get(),
            perf_iodepth=c["perf_iodepth"].get(),
            perf_numjobs=c["perf_numjobs"].get(),
            perf_rw=c["perf_rw"].get(),
            perf_rwmixread=c["perf_rwmixread"].get(),
            perf_size=c["perf_size"].get(),
            perf_time_based=True,
            perf_runtime_full=c["perf_runtime_full"].get(),
            perf_text_log=c["perf_text_log"].get(),
            note=self.perf_task_note.get().strip(),
        )
        self.perf_tasks.append(task)
        self._refresh_task_list()
        # 清空备注，方便下一次输入
        self.perf_task_note.set("")

    def _delete_selected_task(self):
        """删除列表中选中的任务。"""
        selection = self.perf_task_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if 0 <= idx < len(self.perf_tasks):
            del self.perf_tasks[idx]
            self._refresh_task_list()

    def _move_task_up(self):
        """将选中的任务上移一位。"""
        selection = self.perf_task_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx <= 0 or idx >= len(self.perf_tasks):
            return
        self.perf_tasks[idx], self.perf_tasks[idx - 1] = self.perf_tasks[idx - 1], self.perf_tasks[idx]
        self._refresh_task_list()
        self.perf_task_listbox.selection_set(idx - 1)
        self.perf_task_listbox.see(idx - 1)

    def _move_task_down(self):
        """将选中的任务下移一位。"""
        selection = self.perf_task_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx < 0 or idx >= len(self.perf_tasks) - 1:
            return
        self.perf_tasks[idx], self.perf_tasks[idx + 1] = self.perf_tasks[idx + 1], self.perf_tasks[idx]
        self._refresh_task_list()
        self.perf_task_listbox.selection_set(idx + 1)
        self.perf_task_listbox.see(idx + 1)

    def _clear_all_tasks(self):
        """清空所有任务。"""
        if not self.perf_tasks:
            return
        tr = self.translator.tr
        if messagebox.askyesno(tr("dialog.confirm"), tr("dialog.clear_tasks_confirm").format(count=len(self.perf_tasks))):
            self.perf_tasks.clear()
            self._refresh_task_list()

    def _export_tasks_json(self):
        """导出任务列表为 JSON 文件，可供 CLI --perf-task-file 使用。"""
        tr = self.translator.tr
        if not self.perf_tasks:
            messagebox.showinfo(tr("dialog.info"), tr("dialog.task_list_empty"))
            return
        filepath = filedialog.asksaveasfilename(
            title=tr("dialog.export_tasks_title"),
            defaultextension=".json",
            filetypes=[(tr("dialog.json_files"), "*.json"), (tr("dialog.all_files"), "*.*")],
            initialfile="perf_tasks.json"
        )
        if not filepath:
            return
        try:
            data = [t.to_dict() for t in self.perf_tasks]
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            messagebox.showinfo(tr("dialog.success"), tr("dialog.tasks_exported").format(count=len(self.perf_tasks), path=filepath))
        except Exception as e:
            messagebox.showerror(tr("dialog.error"), tr("dialog.export_failed").format(error=e))

    def _refresh_task_list(self):
        """刷新任务列表显示。"""
        self.perf_task_listbox.delete(0, tk.END)
        for idx, task in enumerate(self.perf_tasks, start=1):
            note = f" [{task.note}]" if task.note else ""
            display = (f"{idx}. name={task.perf_test_name}, rw={task.perf_rw}, "
                       f"bs={task.perf_bs}, qd={task.perf_iodepth}, "
                       f"numjobs={task.perf_numjobs}, runtime={task.perf_runtime_full}s{note}")
            self.perf_task_listbox.insert(tk.END, display)
        count = len(self.perf_tasks)
        mode_text = "批量模式" if count > 0 else "单组模式"
        self.perf_task_count_label.config(
            text=f"共 {count} 组任务（{mode_text}）",
            foreground="green" if count > 0 else "gray"
        )

    def _save_tasks_to_temp_file(self) -> Optional[str]:
        """将任务列表保存到临时 JSON 文件，返回文件路径供 CLI 使用。"""
        if not self.perf_tasks:
            return None
        try:
            import tempfile
            fd, filepath = tempfile.mkstemp(suffix=".json", prefix="perf_tasks_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([t.to_dict() for t in self.perf_tasks], f, ensure_ascii=False, indent=2)
            return filepath
        except Exception:
            return None

    def _build_powercycle_tab(self):
        """电源循环参数标签页（v1.9.3 i18n）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.powercycle"))

        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.pc_cycles")), "label.pc_cycles").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_cycles"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.config_vars["pc_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.pc_power_mode")), "label.pc_power_mode").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_power_mode"] = tk.StringVar(value="manual")
        self._pc_mode_combo = ttk.Combobox(frame, textvariable=self.config_vars["pc_power_mode"], width=15,
                      values=["ipmi", "manual", "enhanced"], state="readonly")
        self._pc_mode_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_mode_combo.bind("<<ComboboxSelected>>", self._on_pc_power_mode_change)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.ipmi_host")), "label.ipmi_host").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["ipmi_host"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["ipmi_host"], width=25).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.ipmi_user")), "label.ipmi_user").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["ipmi_user"] = tk.StringVar(value="ADMIN")
        ttk.Entry(frame, textvariable=self.config_vars["ipmi_user"], width=15).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.ipmi_pass")), "label.ipmi_pass").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["ipmi_pass"] = tk.StringVar(value="ADMIN")
        ttk.Entry(frame, textvariable=self.config_vars["ipmi_pass"], width=15, show="*").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.pc_off_interval")), "label.pc_off_interval").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_off_interval"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=3, to=300, textvariable=self.config_vars["pc_off_interval"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.pc_rw_duration")), "label.pc_rw_duration").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_rw_duration"] = tk.IntVar(value=30)
        ttk.Spinbox(frame, from_=5, to=600, textvariable=self.config_vars["pc_rw_duration"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # enhanced（增强）模式专属参数（默认禁用，选择 enhanced 时启用）
        self._pc_enhanced_widgets = []

        self._tr_widget(ttk.Label(frame, text=tr("label.pc_pattern")), "label.pc_pattern").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_pattern"] = tk.StringVar(value="0xAA")
        _w = ttk.Entry(frame, textvariable=self.config_vars["pc_pattern"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_enhanced_widgets.append(_w)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.pc_pattern_size")), "label.pc_pattern_size").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_pattern_size_gb"] = tk.IntVar(value=20)
        _w = ttk.Spinbox(frame, from_=1, to=500, textvariable=self.config_vars["pc_pattern_size_gb"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_enhanced_widgets.append(_w)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.pc_link_check")), "label.pc_link_check").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["pc_link_check"] = tk.BooleanVar(value=True)
        _w = ttk.Checkbutton(frame, variable=self.config_vars["pc_link_check"])
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._pc_enhanced_widgets.append(_w)

        # 初始状态：manual 模式下禁用 enhanced 控件
        self._on_pc_power_mode_change()

    def _on_pc_power_mode_change(self, event=None):
        """电源控制模式切换时，启用/禁用 enhanced 专属控件。"""
        is_enhanced = (self.config_vars["pc_power_mode"].get() == "enhanced")
        state = "normal" if is_enhanced else "disabled"
        for _w in getattr(self, "_pc_enhanced_widgets", []):
            try:
                _w.configure(state=state)
            except Exception:
                pass

    def _build_spor_tab(self):
        """SPOR 参数标签页（v1.9.3 i18n）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.spor"))

        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.pc_cycles")), "label.pc_cycles").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_cycles"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.config_vars["spor_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_power_mode")), "label.spor_power_mode").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_power_mode"] = tk.StringVar(value="timeboard")
        self._spor_mode_combo = ttk.Combobox(frame, textvariable=self.config_vars["spor_power_mode"], width=15,
                      values=["timeboard", "manual", "enhanced"], state="readonly")
        self._spor_mode_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._spor_mode_combo.bind("<<ComboboxSelected>>", self._on_spor_power_mode_change)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_delay")), "label.spor_delay").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_delay"] = tk.IntVar(value=5)
        ttk.Spinbox(frame, from_=1, to=120, textvariable=self.config_vars["spor_delay"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_test_size")), "label.spor_test_size").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_test_size_gb"] = tk.IntVar(value=20)
        ttk.Spinbox(frame, from_=1, to=1000, textvariable=self.config_vars["spor_test_size_gb"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_poweroff_delay")), "label.spor_poweroff_delay").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_poweroff_delay_ms"] = tk.IntVar(value=500)
        ttk.Spinbox(frame, from_=100, to=10000, textvariable=self.config_vars["spor_poweroff_delay_ms"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self.config_vars["spor_mixed_rw"] = tk.BooleanVar(value=False)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.spor_mixed_rw"),
                        variable=self.config_vars["spor_mixed_rw"]), "cb.spor_mixed_rw").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=4)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_mixed_read_ratio")), "label.spor_mixed_read_ratio").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_mixed_read_ratio"] = tk.IntVar(value=70)
        ttk.Spinbox(frame, from_=0, to=100, textvariable=self.config_vars["spor_mixed_read_ratio"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_timeboard_port")), "label.spor_timeboard_port").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_timeboard_port"] = tk.StringVar(value="/dev/ttyUSB0")
        self._spor_timeboard_entry = ttk.Entry(frame, textvariable=self.config_vars["spor_timeboard_port"], width=20)
        self._spor_timeboard_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        # enhanced 模式专属参数（默认禁用，选择 enhanced 时启用）
        self._spor_enhanced_widgets = []

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_enhanced_iodepth")), "label.spor_enhanced_iodepth").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_enhanced_iodepth"] = tk.IntVar(value=256)
        _w = ttk.Spinbox(frame, from_=1, to=1024, textvariable=self.config_vars["spor_enhanced_iodepth"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._spor_enhanced_widgets.append(_w)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.spor_enhanced_bs")), "label.spor_enhanced_bs").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["spor_enhanced_bs"] = tk.StringVar(value="128k")
        _w = ttk.Entry(frame, textvariable=self.config_vars["spor_enhanced_bs"], width=10)
        _w.grid(row=row, column=1, sticky=tk.W, padx=5)
        self._spor_enhanced_widgets.append(_w)
        row += 1

        self.config_vars["spor_final_test"] = tk.BooleanVar(value=True)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.spor_final_test"),
                        variable=self.config_vars["spor_final_test"]), "cb.spor_final_test").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=4)

        # 初始状态：timeboard 模式下禁用 enhanced 控件，manual/enhanced 下禁用 Timeboard 串口
        self._on_spor_power_mode_change()

    def _on_spor_power_mode_change(self, event=None):
        """SPOR 断电方式切换时，启用/禁用对应控件。"""
        mode = self.config_vars["spor_power_mode"].get()
        is_timeboard = (mode == "timeboard")
        is_enhanced = (mode == "enhanced")
        # enhanced 控件：仅 enhanced 模式启用
        enh_state = "normal" if is_enhanced else "disabled"
        for _w in getattr(self, "_spor_enhanced_widgets", []):
            try:
                _w.configure(state=enh_state)
            except Exception:
                pass
        # Timeboard 串口控件：仅 timeboard 模式启用
        tb_state = "normal" if is_timeboard else "disabled"
        try:
            self._spor_timeboard_entry.configure(state=tb_state)
        except Exception:
            pass

    def _build_osint_tab(self):
        """OSINT 参数标签页（v1.9.3 i18n）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.osint"))

        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.pc_cycles")), "label.pc_cycles").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_cycles"] = tk.IntVar(value=10)
        ttk.Spinbox(frame, from_=1, to=10000, textvariable=self.config_vars["osint_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.osint_sleep_type")), "label.osint_sleep_type").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_sleep_type"] = tk.StringVar(value="s3")
        ttk.Combobox(frame, textvariable=self.config_vars["osint_sleep_type"], width=15,
                      values=["s3", "s4", "both"], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.osint_sleep_duration")), "label.osint_sleep_duration").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_sleep_duration"] = tk.IntVar(value=30)
        ttk.Spinbox(frame, from_=5, to=3600, textvariable=self.config_vars["osint_sleep_duration"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self.config_vars["osint_io_idle"] = tk.BooleanVar(value=False)
        self._tr_widget(ttk.Checkbutton(frame, text=tr("cb.osint_io_idle"),
                        variable=self.config_vars["osint_io_idle"]), "cb.osint_io_idle").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=4)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.osint_io_duration")), "label.osint_io_duration").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_io_duration"] = tk.IntVar(value=60)
        ttk.Spinbox(frame, from_=5, to=600, textvariable=self.config_vars["osint_io_duration"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.osint_mount_point")), "label.osint_mount_point").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["osint_mount_point"] = tk.StringVar(value="/mnt/ssd_osint")
        ttk.Entry(frame, textvariable=self.config_vars["osint_mount_point"], width=25).grid(row=row, column=1, sticky=tk.W, padx=5)

    def _build_rw_tab(self):
        """读/写测试参数标签页（v1.9.3 i18n）。"""
        tr = self.translator.tr
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.rw"))

        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.rw_mode")), "label.rw_mode").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_mode"] = tk.StringVar(value=RW_MODE_ALL)
        ttk.Combobox(frame, textvariable=self.config_vars["rw_mode"], width=18,
                      values=[RW_MODE_FULL_DISK, RW_MODE_FILE_CYCLE, RW_MODE_LONG_RUN, RW_MODE_ALL],
                      state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        self._tr_widget(ttk.Label(frame, text=tr("label.rw_mode_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.rw_mode_hint").grid(row=row, column=2, sticky=tk.W)
        row += 1

        # 文件大小多选（用户特别要求）
        self._tr_widget(ttk.Label(frame, text=tr("label.rw_size_select")), "label.rw_size_select").grid(row=row, column=0, sticky=tk.NW, pady=4)
        size_frame = ttk.Frame(frame)
        size_frame.grid(row=row, column=1, columnspan=2, sticky=tk.W, padx=5)
        self.rw_size_vars: Dict[str, tk.BooleanVar] = {}
        for i, size_label in enumerate(RW_FILE_SIZES.keys()):
            var = tk.BooleanVar(value=True)
            self.rw_size_vars[size_label] = var
            col = i % 3
            r = i // 3
            ttk.Checkbutton(size_frame, text=size_label, variable=var).grid(
                row=r, column=col, sticky=tk.W, padx=8, pady=2)
        # 全选/清空按钮
        btn_sf = ttk.Frame(size_frame)
        btn_sf.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
        self._tr_widget(ttk.Button(btn_sf, text=tr("btn.select_all"), width=6,
                   command=lambda: [v.set(True) for v in self.rw_size_vars.values()]), "btn.select_all").pack(side=tk.LEFT, padx=2)
        self._tr_widget(ttk.Button(btn_sf, text=tr("btn.clear_all"), width=6,
                   command=lambda: [v.set(False) for v in self.rw_size_vars.values()]), "btn.clear_all").pack(side=tk.LEFT, padx=2)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_cycles")), "label.rw_cycles").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_cycles"] = tk.IntVar(value=3)
        ttk.Spinbox(frame, from_=1, to=1000, textvariable=self.config_vars["rw_cycles"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_pattern")), "label.rw_pattern").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_pattern"] = tk.StringVar(value=RW_PATTERN_RANDOM)
        ttk.Combobox(frame, textvariable=self.config_vars["rw_pattern"], width=15,
                      values=[RW_PATTERN_RANDOM, RW_PATTERN_AA, RW_PATTERN_55, RW_PATTERN_00, RW_PATTERN_FF],
                      state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_verify")), "label.rw_verify").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_verify"] = tk.StringVar(value="md5")
        ttk.Combobox(frame, textvariable=self.config_vars["rw_verify"], width=15,
                      values=["md5", "sha256", "crc32"], state="readonly").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=6)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_long_hours")), "label.rw_long_hours").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_long_hours"] = tk.IntVar(value=24)
        ttk.Spinbox(frame, from_=1, to=720, textvariable=self.config_vars["rw_long_hours"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_block_size")), "label.rw_block_size").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_block_size"] = tk.StringVar(value="4k")
        ttk.Entry(frame, textvariable=self.config_vars["rw_block_size"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_iodepth")), "label.rw_iodepth").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_iodepth"] = tk.IntVar(value=32)
        ttk.Spinbox(frame, from_=1, to=1024, textvariable=self.config_vars["rw_iodepth"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_numjobs")), "label.rw_numjobs").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_numjobs"] = tk.IntVar(value=4)
        ttk.Spinbox(frame, from_=1, to=64, textvariable=self.config_vars["rw_numjobs"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        self._tr_widget(ttk.Label(frame, text=tr("label.rw_mixed_read_ratio")), "label.rw_mixed_read_ratio").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["rw_mixed_read_ratio"] = tk.IntVar(value=70)
        ttk.Spinbox(frame, from_=0, to=100, textvariable=self.config_vars["rw_mixed_read_ratio"], width=10).grid(row=row, column=1, sticky=tk.W, padx=5)

        # v1.9.3 新增: 设备功耗测量参数页（i18n）
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text=tr("tab.power"))
        row = 0
        self._tr_widget(ttk.Label(frame, text=tr("label.oscill_path")), "label.oscill_path").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["oscill_path"] = tk.StringVar(value="")
        oscill_path_entry = ttk.Entry(frame, textvariable=self.config_vars["oscill_path"], width=35)
        oscill_path_entry.grid(row=row, column=1, sticky=tk.W, padx=5)
        def _browse_oscill_path():
            path = filedialog.askdirectory(title="选择 oscill 项目根目录")
            if path:
                self.config_vars["oscill_path"].set(path)
        self._tr_widget(ttk.Button(frame, text=tr("btn.browse"), command=_browse_oscill_path), "btn.browse").grid(row=row, column=2, padx=5)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.oscill_path_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.oscill_path_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_limit")), "label.power_limit").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["power_limit"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["power_limit"], width=15).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_limit_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.power_limit_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=8)
        row += 1
        # v1.9.3 新增: 自动采集配置
        self._tr_widget(ttk.Label(frame, text=tr("label.auto_acq_config"), font=("TkDefaultFont", 9, "bold")), "label.auto_acq_config").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_plan")), "label.power_plan").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["power_plan"] = tk.StringVar(value="")
        power_plan_combo = ttk.Combobox(frame, textvariable=self.config_vars["power_plan"], width=18,
                                         values=["", "POWER-01B", "POWER-02", "POWER-03", "POWER-04", "POWER-05", "POWER-06"],
                                         state="readonly")
        power_plan_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_plan_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.power_plan_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_total_duration")), "label.power_total_duration").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["power_total_duration"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["power_total_duration"], width=15).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_total_duration_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.power_total_duration_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_sample_interval")), "label.power_sample_interval").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["power_sample_interval"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["power_sample_interval"], width=15).grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_sample_interval_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.power_sample_interval_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=8)
        row += 1
        # v1.9.3 新增: JSON 报告路径选择
        self._tr_widget(ttk.Label(frame, text=tr("label.power_json_report")), "label.power_json_report").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["power_json_report"] = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.config_vars["power_json_report"], width=30).grid(row=row, column=1, sticky=tk.W, padx=5)
        def _browse_json_report():
            path = filedialog.askopenfilename(title="Select oscill JSON power report",
                                                filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
            if path:
                self.config_vars["power_json_report"].set(path)
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row, column=2, sticky=tk.W, padx=5)
        self._tr_widget(ttk.Button(btn_frame, text=tr("btn.browse_json"), command=_browse_json_report), "btn.browse_json").pack(side=tk.LEFT, padx=(0, 3))
        def _convert_json_to_log_manual():
            json_path = self.config_vars.get("power_json_report", tk.StringVar(value="")).get()
            if not json_path or not os.path.isfile(json_path):
                messagebox.showwarning("Warning", "Please select a valid JSON report file first.")
                return
            try:
                import logging as _logging
                _tmp_logger = _logging.getLogger("ssd_test.gui")
                _tmp_cfg = TestConfig(device="", test_items=[], output_dir=os.path.dirname(json_path) or ".")
                _tmp_tester = PowerTester(_tmp_cfg, _tmp_logger)
                log_path = _tmp_tester._convert_json_to_log(json_path)
                if log_path:
                    messagebox.showinfo("Success", f"Log file generated:\n{log_path}")
                    self._append_log(f"JSON converted to log: {log_path}", "success")
                else:
                    messagebox.showerror("Error", "Failed to convert JSON to log.")
            except Exception as e:
                messagebox.showerror("Error", f"Conversion failed:\n{e}")
        self._tr_widget(ttk.Button(btn_frame, text=tr("btn.convert_to_log"), command=_convert_json_to_log_manual), "btn.convert_to_log").pack(side=tk.LEFT)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_json_report_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.power_json_report_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=8)
        row += 1
        # v1.9.3 新增: 通道选择
        self._tr_widget(ttk.Label(frame, text=tr("label.power_channels")), "label.power_channels").grid(row=row, column=0, sticky=tk.W, pady=4)
        self.config_vars["power_channels"] = tk.StringVar(value="CH1,CH2")
        ch_frame = ttk.Frame(frame)
        ch_frame.grid(row=row, column=1, columnspan=2, sticky=tk.W, padx=5)
        self._power_ch_vars = {}
        for idx, ch in enumerate(["CH1", "CH2", "CH3", "CH4"]):
            var = tk.BooleanVar(value=(ch in ["CH1", "CH2"]))
            self._power_ch_vars[ch] = var
            def _make_ch_callback(ch_name=ch):
                selected = [c for c, v in self._power_ch_vars.items() if v.get()]
                self.config_vars["power_channels"].set(",".join(selected))
            ttk.Checkbutton(ch_frame, text=ch, variable=var, command=_make_ch_callback).grid(row=0, column=idx, padx=4)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.power_channels_hint"), foreground="gray",
                  font=("TkDefaultFont", 8)), "label.power_channels_hint").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=8)
        row += 1
        self._tr_widget(ttk.Label(frame, text=tr("label.operation_guide"), font=("TkDefaultFont", 9, "bold")), "label.operation_guide").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)
        row += 1
        help_text = tr("power.help_text")
        self._tr_widget(ttk.Label(frame, text=help_text, foreground="gray", justify=tk.LEFT,
                  font=("TkDefaultFont", 8)), "power.help_text").grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=2)



    # ----------------------------------------------------------
    # v1.9.3 i18n: 运行时语言切换
    # ----------------------------------------------------------

    def _on_language_change(self, event=None):
        """语言切换下拉框回调。"""
        display = self._lang_var.get()
        for lang_code in (I18N_ZH_CN, I18N_EN_US):
            if Translator.language_display_name(lang_code) == display:
                if lang_code != self.translator.language:
                    self.translator.set_language(lang_code)
                    self.retranslate_ui()
                break

    def _tr_widget(self, widget, key):
        """v1.9.3 i18n: 给 widget 标记 i18n key，用于运行时重翻译。

        用法: self._tr_widget(ttk.Label(frame, text=tr("key")), "key")
        """
        widget._i18n_key = key
        return widget

    def _retranslate_children(self, parent):
        """v1.9.3 i18n: 递归遍历 parent 的所有子 widget，更新标记了 _i18n_key 的文本。"""
        try:
            for child in parent.winfo_children():
                if hasattr(child, "_i18n_key"):
                    try:
                        child.configure(text=self.translator.tr(child._i18n_key))
                    except Exception:
                        pass
                self._retranslate_children(child)
        except Exception:
            pass

    def retranslate_ui(self):
        """运行时重翻译所有界面文本（框架性元素 + 测试项 + 标签页标题）。

        注意：参数页内部的详细标签因未保存 widget 引用，语言切换后需重启应用生效。
        """
        tr = self.translator.tr

        # 窗口标题
        self.root.title(tr("app.title") + f" v{SCRIPT_VERSION}")

        # ---- 菜单栏 ----
        # 注意：Linux 上菜单栏级联菜单索引从 1 开始（不是 0）
        # 文件菜单: 子项索引 0=保存配置, 1=加载配置, 2=separator, 3=导出日志, 4=separator, 5=退出
        try:
            self._menu_file.entryconfigure(0, label=tr("menu.save_config"))
            self._menu_file.entryconfigure(1, label=tr("menu.load_config"))
            self._menu_file.entryconfigure(3, label=tr("menu.export_log"))
            self._menu_file.entryconfigure(5, label=tr("menu.exit"))
            self._menubar.entryconfigure(1, label=tr("menu.file"))
        except Exception:
            pass
        # 工具菜单: 0=扫描设备, 1=清空日志, 2=打开报告目录, 3=separator, 4=启动功耗测量工具
        try:
            self._menu_tools.entryconfigure(0, label=tr("menu.scan_devices"))
            self._menu_tools.entryconfigure(1, label=tr("menu.clear_log"))
            self._menu_tools.entryconfigure(2, label=tr("menu.open_report_dir"))
            self._menu_tools.entryconfigure(4, label=tr("menu.launch_power_tool"))
            self._menubar.entryconfigure(2, label=tr("menu.tools"))
        except Exception:
            pass
        # 帮助菜单: 0=关于
        try:
            self._menu_tool_usage.entryconfigure(0, label=tr("dialog.tool_usage_title"))
            self._menubar.entryconfigure(3, label=tr("menu.tool_usage"))
        except Exception:
            pass

        # ---- 设备选择栏 ----
        try:
            self._top_frame.configure(text=tr("frame.device_select"))
            self._label_dut.configure(text=tr("label.device_under_test"))
            self._btn_scan.configure(text=tr("btn.scan"))
            self._btn_device_info.configure(text=tr("btn.device_info"))
            self._label_lang.configure(text=tr("label.language"))
            self.start_btn.configure(text=tr("btn.start_test"))
        except Exception:
            pass

        # ---- 测试项选择 ----
        try:
            self._left_frame.configure(text=tr("frame.test_items"))
            self._btn_select_all.configure(text=tr("btn.select_all"))
            self._btn_clear_all.configure(text=tr("btn.clear_all"))
            for cb, desc_label, item_id in getattr(self, "_test_item_widgets", []):
                tr_key_name = f"test.{item_id}.name"
                tr_key_desc = f"test.{item_id}.desc"
                if tr_key_name in _I18N_ZH_TEXTS:
                    cb.configure(text=tr(tr_key_name))
                if tr_key_desc in _I18N_ZH_TEXTS:
                    desc_label.configure(text=f"  {tr(tr_key_desc)}")
        except Exception:
            pass

        # ---- 参数配置 ----
        try:
            self._right_frame.configure(text=tr("frame.param_config"))
            tab_keys = ["tab.general", "tab.firmware", "tab.performance", "tab.powercycle",
                        "tab.spor", "tab.osint", "tab.rw", "tab.power"]
            for i, key in enumerate(tab_keys):
                try:
                    self.notebook.tab(i, text=tr(key))
                except Exception:
                    pass
        except Exception:
            pass

        # ---- 底部控制栏 ----
        try:
            self.stop_btn.configure(text=tr("btn.stop"))
            self._btn_clear_log.configure(text=tr("btn.clear_log_bottom"))
            self._btn_save_cfg.configure(text=tr("btn.save_config_bottom"))
            self._btn_load_cfg.configure(text=tr("btn.load_config_bottom"))
            self._btn_export_log.configure(text=tr("btn.export_log_bottom"))
            self._btn_view_log.configure(text=tr("btn.view_detail_log"))
        except Exception:
            pass

        # ---- 日志区域 ----
        try:
            self._log_frame.configure(text=tr("frame.test_summary"))
        except Exception:
            pass

        # ---- 状态栏 & 进度 ----
        try:
            self.status_var.set(f"{tr('status.ready')} | v{SCRIPT_VERSION} | {tr('app.stdlib_only')}")
            self.progress_label.configure(text=tr("status.ready"))
        except Exception:
            pass

        # ---- 参数页内部 widget 递归重翻译 ----
        try:
            self._retranslate_children(self.notebook)
        except Exception:
            pass

    # ----------------------------------------------------------
    # 设备扫描与信息
    # ----------------------------------------------------------

    def scan_devices(self):
        """扫描系统中的存储设备。"""
        self._append_log("Scanning storage devices...", "header")
        self.device_list = []

        try:
            # 使用 lsblk 扫描块设备
            result = subprocess.run(
                ["lsblk", "-d", "-o", "NAME,SIZE,MODEL,TRAN,ROTA", "-n", "-J"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    name = dev.get("name", "")
                    if name.startswith(("loop", "ram", "zram", "sr")):
                        continue
                    size = dev.get("size", "未知")
                    model = dev.get("model", "未知").strip() or "未知"
                    tran = dev.get("tran", "").strip() or "未知"
                    device_path = f"/dev/{name}"
                    self.device_list.append({
                        "path": device_path,
                        "name": name,
                        "size": size,
                        "model": model,
                        "tran": tran,
                    })
        except Exception as e:
            self._append_log(f"lsblk scan failed: {e}", "warning")

        # 补充 nvme list 信息
        try:
            result = subprocess.run(
                ["nvme", "list", "-o", "json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for dev in data.get("Devices", []):
                    device_path = dev.get("DevicePath", "")
                    model = dev.get("ModelNumber", "").strip()
                    serial = dev.get("SerialNumber", "").strip()
                    fw = dev.get("Firmware", "").strip()
                    # 更新已有设备信息
                    for d in self.device_list:
                        if d["path"] == device_path:
                            if model:
                                d["model"] = model
                            d["serial"] = serial
                            d["fw"] = fw
                            break
        except Exception:
            pass  # nvme-cli 未安装或无 NVMe 设备

        # 更新下拉框
        display_list = []
        for d in self.device_list:
            display = f"{d['path']}  |  {d['size']}  |  {d['model']}  |  {d['tran']}"
            display_list.append(display)

        self.device_combo["values"] = display_list
        if display_list:
            self.device_combo.current(0)
            self._update_device_info()

        self._append_log(f"Scan complete, found {len(self.device_list)} storage devices", "success")
        self.status_var.set(f"就绪 | 发现 {len(self.device_list)} 个设备")

    def _update_device_info(self):
        """更新设备信息标签。"""
        idx = self.device_combo.current()
        if 0 <= idx < len(self.device_list):
            d = self.device_list[idx]
            info = f"{d['model']} | {d['size']} | {d['tran']}"
            if d.get("serial"):
                info += f" | SN:{d['serial']}"
            if d.get("fw"):
                info += f" | FW:{d['fw']}"
            self.device_info_label.config(text=info)

    def _show_device_info(self):
        """显示详细设备信息对话框。"""
        tr = self.translator.tr
        idx = self.device_combo.current()
        if idx < 0:
            messagebox.showwarning(tr("dialog.info"), tr("dialog.select_device"))
            return
        device = self.device_list[idx]["path"]

        info_text = f"设备路径: {device}\n\n"
        try:
            result = subprocess.run(["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT", device],
                                    capture_output=True, text=True, timeout=5)
            info_text += "=== lsblk ===\n" + result.stdout + "\n"
        except Exception as e:
            info_text += f"lsblk 失败: {e}\n"

        try:
            result = subprocess.run(["smartctl", "-i", device],
                                    capture_output=True, text=True, timeout=5)
            info_text += "=== smartctl -i ===\n" + result.stdout + "\n"
        except Exception as e:
            info_text += f"smartctl 失败: {e}\n"

        if device.startswith("/dev/nvme"):
            try:
                result = subprocess.run(["nvme", "id-ctrl", device.replace("n1", ""), "-o", "json"],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    info_text += "=== NVMe Identify ===\n"
                    info_text += f"  型号: {data.get('mn', 'N/A')}\n"
                    info_text += f"  序列号: {data.get('sn', 'N/A')}\n"
                    info_text += f"  固件版本: {data.get('fr', 'N/A')}\n"
                    info_text += f"  命名空间数: {data.get('nn', 'N/A')}\n"
                    info_text += f"  PCIe 厂商ID: {data.get('vid', 'N/A')}\n"
            except Exception as e:
                info_text += f"nvme id-ctrl 失败: {e}\n"

        # 显示对话框
        win = tk.Toplevel(self.root)
        win.title(f"设备信息 - {device}")
        win.geometry("600x500")
        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Courier", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, info_text)
        text.config(state=tk.DISABLED)

    def _browse_fw_image(self):
        """浏览选择固件镜像文件。"""
        path = filedialog.askopenfilename(title="选择固件镜像",
                                           filetypes=[("固件文件", "*.bin *.fw *.img"), ("所有文件", "*.*")])
        if path:
            self.config_vars["fw_image"].set(path)

    # ----------------------------------------------------------
    # 测试项选择回调
    # ----------------------------------------------------------

    def _on_test_item_change(self):
        """测试项选择变化时更新命令预览。"""
        self._update_cmd_preview()

    def _set_all_tests(self, value: bool):
        """全选/清空所有测试项。"""
        for var in self.selected_tests.values():
            var.set(value)
        self._update_cmd_preview()

    # ----------------------------------------------------------
    # 命令构建与预览
    # ----------------------------------------------------------

    def _get_selected_tests(self) -> List[str]:
        """获取选中的测试项列表。"""
        return [item_id for item_id, var in self.selected_tests.items() if var.get()]


    # ----------------------------------------------------------
    # v1.9.2 新增: SSD 状态管理方法
    # ----------------------------------------------------------

    def _get_current_device_path(self) -> Optional[str]:
        """获取当前选择的设备路径。"""
        device_idx = self.device_combo.current()
        if device_idx < 0:
            return None
        return self.device_list[device_idx]["path"]

    def _update_state_display(self, state: str, detail: str = ""):
        """更新 GUI 上的 SSD 状态显示。

        Args:
            state: unknown/fob/steady
            detail: 附加说明文字
        """
        state_upper = state.upper()
        colors = {
            SSD_STATE_UNKNOWN: ("#9e9e9e", "UNKNOWN"),
            SSD_STATE_FOB: ("#1565c0", "FOB"),
            SSD_STATE_STEADY: ("#2e7d32", "STEADY"),
        }
        bg, text = colors.get(state, ("#9e9e9e", state_upper))
        if self.ssd_state_color_label:
            self.ssd_state_color_label.config(text=text, bg=bg)
        if self.ssd_state_label:
            if detail:
                self.ssd_state_label.config(text=detail)
            else:
                self.ssd_state_label.config(text=f"当前状态: {state_upper}")

    def _refresh_ssd_state(self):
        """从状态文件刷新当前 SSD 状态显示。"""
        tr = self.translator.tr
        device = self._get_current_device_path()
        if not device:
            messagebox.showwarning(tr("dialog.info"), tr("dialog.no_device"))
            return
        try:
            serial = get_device_serial(device)
            state_info = load_ssd_state(serial)
            self.current_ssd_state = state_info["state"]
            detail = f"序列号: {serial}"
            if state_info.get("timestamp"):
                detail += f" | 更新时间: {state_info['timestamp']}"
            self._update_state_display(self.current_ssd_state, detail)
            self._append_log(f"SSD state refreshed: {self.current_ssd_state.upper()} (serial={serial})", "info")
        except Exception as e:
            self._append_log(f"Failed to refresh SSD state: {e}", "error")

    def _reset_ssd_state(self):
        """将当前 SSD 状态重置为 Unknown（仅更新本地状态和显示，不操作设备）。"""
        tr = self.translator.tr
        device = self._get_current_device_path()
        if not device:
            messagebox.showwarning(tr("dialog.info"), tr("dialog.no_device"))
            return
        if not messagebox.askyesno(tr("dialog.confirm"), tr("dialog.reset_state_confirm")):
            return
        try:
            serial = get_device_serial(device)
            save_ssd_state(serial, SSD_STATE_UNKNOWN, {"reset_by": "user"})
            self.current_ssd_state = SSD_STATE_UNKNOWN
            self._update_state_display(SSD_STATE_UNKNOWN, f"序列号: {serial} | 已重置为 Unknown")
            self._append_log(f"SSD state reset to Unknown (serial={serial})", "warning")
        except Exception as e:
            self._append_log(f"Failed to reset SSD state: {e}", "error")

    def _enter_ssd_state(self, target_state: str):
        """手动让 SSD 进入指定状态（通过 subprocess 调用脚本 --action）。

        Args:
            target_state: SSD_STATE_FOB 或 SSD_STATE_STEADY
        """
        tr = self.translator.tr
        if self.is_running:
            messagebox.showwarning(tr("dialog.info"), tr("dialog.test_running_stop"))
            return
        device = self._get_current_device_path()
        if not device:
            messagebox.showerror(tr("dialog.error"), tr("dialog.no_device"))
            return

        state_name = "FOB" if target_state == SSD_STATE_FOB else "Steady"
        if not messagebox.askyesno(tr("dialog.confirm"),
                                    tr("dialog.enter_state_confirm").format(state=state_name, device=device)):
            return

        # 构建命令
        self._package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        action = "enter-fob" if target_state == SSD_STATE_FOB else "enter-steady"
        cmd = [sys.executable, "-m", "ssd_test_tool.main", "-d", device, "--action", action, "-y"]

        # 传递稳态参数
        if target_state == SSD_STATE_STEADY:
            cmd.extend(["--steady-max-rounds", str(self.config_vars.get("steady_max_rounds", tk.IntVar(value=25)).get())])
            cmd.extend(["--steady-point-duration", str(self.config_vars.get("steady_point_duration", tk.IntVar(value=60)).get())])
        cmd.extend(["--purge-method", self.config_vars.get("purge_method", tk.StringVar(value="auto")).get()])

        self._append_log(f"Starting SSD state transition to {state_name}...", "header")
        self._append_log(f"Command: {' '.join(cmd)}", "info")

        # 更新 UI 状态
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text=f"进入{state_name}中...")
        self.status_var.set(f"正在进入 {state_name} 状态...")

        # 启动子进程
        try:
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=getattr(self, "_package_root", None),
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
        except Exception as e:
            self._append_log(f"Failed to start state transition process: {e}", "error")
            self._reset_ui_state()
            return

        # 启动输出读取线程（完成后自动刷新状态）
        self.reader_thread = threading.Thread(
            target=self._read_state_change_output,
            args=(target_state,),
            daemon=True)
        self.reader_thread.start()

    def _read_state_change_output(self, target_state: str):
        """后台线程：读取状态切换子进程输出，完成后刷新状态显示。"""
        try:
            for line in self.test_process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # v1.9.2 修复: 显示所有输出行，不再过滤（避免隐藏 Traceback/Error 等关键错误）
                tag = "info"
                if any(kw in line for kw in ["ERROR", "Error", "error", "Traceback", "Exception", "失败", "FAIL"]):
                    tag = "error"
                elif any(kw in line for kw in ["WARNING", "Warning", "warning", "警告"]):
                    tag = "warning"
                self.root.after(0, lambda l=line, t=tag: self._append_log(l, t))
            returncode = self.test_process.wait()
            self.root.after(0, lambda rc=returncode, ts=target_state: self._on_state_change_finished(rc, ts))
        except Exception as e:
            self.root.after(0, lambda: self._append_log(f"State transition output read exception: {e}", "error"))
            self.root.after(0, self._reset_ui_state)

    def _on_state_change_finished(self, returncode: int, target_state: str):
        """状态切换完成回调。"""
        state_name = "FOB" if target_state == SSD_STATE_FOB else "Steady"
        if returncode == 0:
            self.current_ssd_state = target_state
            self._update_state_display(target_state, f"已进入 {state_name} 状态")
            self._append_log(f"SSD successfully entered {state_name} state", "success")
        else:
            self._append_log(f"Failed to enter {state_name} state (exit code: {returncode})", "error")
        self._reset_ui_state()
        # 刷新状态显示
        self._refresh_ssd_state()

    def _enter_ssd_state_and_test(self, target_state: str, tests: List[str]):
        """先让 SSD 进入目标状态，完成后自动开始性能测试。

        Args:
            target_state: 目标状态 (SSD_STATE_FOB / SSD_STATE_STEADY)
            tests: 选中的测试项列表
        """
        device = self._get_current_device_path()
        if not device:
            return

        state_name = "FOB" if target_state == SSD_STATE_FOB else "Steady"
        self._package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        action = "enter-fob" if target_state == SSD_STATE_FOB else "enter-steady"
        cmd = [sys.executable, "-m", "ssd_test_tool.main", "-d", device, "--action", action, "-y"]
        if target_state == SSD_STATE_STEADY:
            cmd.extend(["--steady-max-rounds", str(self.config_vars.get("steady_max_rounds", tk.IntVar(value=25)).get())])
            cmd.extend(["--steady-point-duration", str(self.config_vars.get("steady_point_duration", tk.IntVar(value=60)).get())])
        cmd.extend(["--purge-method", self.config_vars.get("purge_method", tk.StringVar(value="auto")).get()])

        self._append_log(f"Entering {state_name} state first, test will start automatically after...", "header")

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text=f"进入{state_name}中...")
        self.status_var.set(f"正在进入 {state_name} 状态（完成后自动测试）...")

        try:
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=getattr(self, "_package_root", None),
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
        except Exception as e:
            self._append_log(f"Failed to start state transition process: {e}", "error")
            self._reset_ui_state()
            return

        self.reader_thread = threading.Thread(
            target=self._read_state_change_and_test_output,
            args=(target_state, tests),
            daemon=True)
        self.reader_thread.start()

    def _read_state_change_and_test_output(self, target_state: str, tests: List[str]):
        """后台线程：读取状态切换输出，完成后自动启动测试。"""
        try:
            for line in self.test_process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # v1.9.2 修复: 显示所有输出行，不再过滤
                tag = "info"
                if any(kw in line for kw in ["ERROR", "Error", "error", "Traceback", "Exception", "失败", "FAIL"]):
                    tag = "error"
                elif any(kw in line for kw in ["WARNING", "Warning", "warning", "警告"]):
                    tag = "warning"
                self.root.after(0, lambda l=line, t=tag: self._append_log(l, t))
            returncode = self.test_process.wait()
            self.root.after(0, lambda rc=returncode, ts=target_state, ts_list=tests:
                            self._on_state_change_for_test_finished(rc, ts, ts_list))
        except Exception as e:
            self.root.after(0, lambda: self._append_log(f"State transition output read exception: {e}", "error"))
            self.root.after(0, self._reset_ui_state)

    def _on_state_change_for_test_finished(self, returncode: int, target_state: str, tests: List[str]):
        """状态切换完成后自动启动测试的回调。"""
        state_name = "FOB" if target_state == SSD_STATE_FOB else "Steady"
        if returncode != 0:
            self._append_log(f"Failed to enter {state_name} state (exit code: {returncode}), test cancelled", "error")
            self._reset_ui_state()
            return
        self.current_ssd_state = target_state
        self._update_state_display(target_state, f"已进入 {state_name} 状态，准备测试")
        self._append_log(f"SSD entered {state_name} state, starting performance test automatically...", "success")
        self._reset_ui_state()
        # 短暂延迟后启动测试（确保 UI 状态已重置）
        self.root.after(500, self._start_test_direct)

    def _start_test_direct(self):
        """直接启动测试（跳过状态校验，用于状态切换完成后自动测试）。"""
        if self.is_running:
            return
        tests = self._get_selected_tests()
        if not tests:
            return

        if os.geteuid() != 0 and not self.config_vars["dry_run"].get():
            cmd = self.build_command()
            cmd = ["pkexec"] + cmd
        else:
            cmd = self.build_command()

        self._update_cmd_preview()
        self.clear_log(silent=True)
        current_time = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"{current_time} - Test started | Device: {self.device_list[self.device_combo.current()]['path']} | "
                         f"测试项: {','.join(tests)} | 详细日志见日志文件", "header")

        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text="运行中...")
        self.status_var.set("测试运行中...")

        try:
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=getattr(self, "_package_root", None),
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
        except Exception as e:
            self._append_log(f"Failed to start test process: {e}", "error")
            self._reset_ui_state()
            return

        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()


    def build_command(self) -> List[str]:
        """根据 GUI 配置构建命令行参数列表。"""
        device_idx = self.device_combo.current()
        if device_idx < 0:
            return []
        device = self.device_list[device_idx]["path"]

        # 模块化版: 使用 -m 方式运行包入口，cwd 需设置为包的上级目录
        self._package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cmd = [sys.executable, "-m", "ssd_test_tool.main", "-d", device]

        # 测试项
        tests = self._get_selected_tests()
        if tests:
            cmd.extend(["-t"] + tests)

        # 通用参数
        if self.config_vars["dry_run"].get():
            cmd.append("--dry-run")
        if self.config_vars["assume_yes"].get():
            cmd.append("-y")
        if self.config_vars["verbose"].get():
            cmd.append("--verbose")
        cmd.extend(["-o", self.config_vars["output_dir"].get()])
        cmd.extend(["--log-dir", self.config_vars["log_dir"].get()])

        # 固件参数
        if TEST_FW in tests:
            fw_img = self.config_vars["fw_image"].get()
            if fw_img:
                cmd.extend(["--fw-image", fw_img])
            cmd.extend(["--fw-action", self.config_vars["fw_action"].get()])
            fw_slot = self.config_vars["fw_slot"].get()
            if fw_slot:
                cmd.extend(["--fw-slot", fw_slot])

        # 性能参数（全参数可配置）
        if TEST_PERF in tests:
            cmd.extend(["--perf-state", self.config_vars["perf_state"].get()])
            # v1.9.2: 状态一致时跳过预处理（已删除稳态预处理UI）
            if self._skip_precondition:
                cmd.append("--no-precondition")
            # 全参数 FIO 配置
            cmd.extend(["--perf-name", self.config_vars["perf_test_name"].get()])
            cmd.extend(["--perf-direct", str(self.config_vars["perf_direct"].get())])
            cmd.extend(["--perf-ioengine", self.config_vars["perf_ioengine"].get()])
            cmd.extend(["--perf-bs", self.config_vars["perf_bs"].get()])
            cmd.extend(["--perf-iodepth", str(self.config_vars["perf_iodepth"].get())])
            cmd.extend(["--perf-numjobs", str(self.config_vars["perf_numjobs"].get())])
            cmd.extend(["--perf-rw", self.config_vars["perf_rw"].get()])
            cmd.extend(["--perf-rwmixread", str(self.config_vars["perf_rwmixread"].get())])
            cmd.extend(["--perf-size", self.config_vars["perf_size"].get()])
            cmd.extend(["--perf-runtime", str(self.config_vars["perf_runtime_full"].get())])
            if self.config_vars["perf_text_log"].get():
                cmd.append("--perf-text-log")
            # v1.9.2 新增: 状态管理与稳态配置参数
            cmd.extend(["--purge-method", self.config_vars.get("purge_method", tk.StringVar(value="auto")).get()])
            cmd.extend(["--steady-max-rounds", str(self.config_vars.get("steady_max_rounds", tk.IntVar(value=25)).get())])
            cmd.extend(["--steady-point-duration", str(self.config_vars.get("steady_point_duration", tk.IntVar(value=60)).get())])
            cmd.extend(["--smart-ignore-media-errors", self.config_vars.get("smart_ignore_media_errors", tk.StringVar(value="5353")).get()])
            # v1.8.0 多任务批量测试：任务列表非空时保存到临时文件并传递
            if self.perf_tasks:
                task_file = self._save_tasks_to_temp_file()
                if task_file:
                    self._current_task_file = task_file  # 保持引用，防止文件被清理
                    cmd.extend(["--perf-task-file", task_file])

        # 电源循环参数
        if TEST_POWERCYCLE in tests:
            cmd.extend(["--pc-cycles", str(self.config_vars["pc_cycles"].get())])
            cmd.extend(["--pc-power-mode", self.config_vars["pc_power_mode"].get()])
            ipmi_host = self.config_vars["ipmi_host"].get()
            if ipmi_host:
                cmd.extend(["--ipmi-host", ipmi_host])
                cmd.extend(["--ipmi-user", self.config_vars["ipmi_user"].get()])
                cmd.extend(["--ipmi-pass", self.config_vars["ipmi_pass"].get()])
            cmd.extend(["--pc-off-interval", str(self.config_vars["pc_off_interval"].get())])
            cmd.extend(["--pc-rw-duration", str(self.config_vars["pc_rw_duration"].get())])
            # enhanced 模式专属参数
            if self.config_vars["pc_power_mode"].get() == "enhanced":
                cmd.extend(["--pc-pattern", self.config_vars["pc_pattern"].get()])
                cmd.extend(["--pc-pattern-size-gb", str(self.config_vars["pc_pattern_size_gb"].get())])
                if not self.config_vars["pc_link_check"].get():
                    cmd.append("--pc-no-link-check")

        # SPOR 参数
        if TEST_SPOR in tests:
            cmd.extend(["--spor-cycles", str(self.config_vars["spor_cycles"].get())])
            cmd.extend(["--spor-power-mode", self.config_vars["spor_power_mode"].get()])
            cmd.extend(["--spor-delay", str(self.config_vars["spor_delay"].get())])
            cmd.extend(["--spor-test-size", str(self.config_vars["spor_test_size_gb"].get())])
            cmd.extend(["--spor-poweroff-delay-ms", str(self.config_vars["spor_poweroff_delay_ms"].get())])
            if self.config_vars["spor_mixed_rw"].get():
                cmd.append("--spor-mixed-rw")
                cmd.extend(["--spor-mixed-read-ratio", str(self.config_vars["spor_mixed_read_ratio"].get())])
            if not self.config_vars["spor_final_test"].get():
                cmd.append("--spor-no-final-test")
            cmd.extend(["--spor-timeboard-port", self.config_vars["spor_timeboard_port"].get()])
            # enhanced 模式专属参数
            if self.config_vars["spor_power_mode"].get() == "enhanced":
                cmd.extend(["--spor-enhanced-iodepth", str(self.config_vars["spor_enhanced_iodepth"].get())])
                cmd.extend(["--spor-enhanced-bs", self.config_vars["spor_enhanced_bs"].get()])

        # OSINT 参数
        if TEST_OSINT in tests:
            cmd.extend(["--osint-cycles", str(self.config_vars["osint_cycles"].get())])
            cmd.extend(["--osint-sleep-type", self.config_vars["osint_sleep_type"].get()])
            cmd.extend(["--osint-sleep-duration", str(self.config_vars["osint_sleep_duration"].get())])
            if self.config_vars["osint_io_idle"].get():
                cmd.append("--osint-io-idle")
            cmd.extend(["--osint-io-duration", str(self.config_vars["osint_io_duration"].get())])
            cmd.extend(["--osint-mount-point", self.config_vars["osint_mount_point"].get()])

        # RW（读/写测试）参数
        if TEST_RW in tests:
            cmd.extend(["--rw-mode", self.config_vars["rw_mode"].get()])
            # 文件大小多选
            selected_sizes = [size for size, var in self.rw_size_vars.items() if var.get()]
            if selected_sizes:
                cmd.extend(["--rw-file-sizes"] + selected_sizes)
            cmd.extend(["--rw-cycles", str(self.config_vars["rw_cycles"].get())])
            cmd.extend(["--rw-pattern", self.config_vars["rw_pattern"].get()])
            cmd.extend(["--rw-verify", self.config_vars["rw_verify"].get()])
            cmd.extend(["--rw-long-hours", str(self.config_vars["rw_long_hours"].get())])
            cmd.extend(["--rw-block-size", self.config_vars["rw_block_size"].get()])
            cmd.extend(["--rw-iodepth", str(self.config_vars["rw_iodepth"].get())])
            cmd.extend(["--rw-numjobs", str(self.config_vars["rw_numjobs"].get())])
            cmd.extend(["--rw-mixed-read-ratio", str(self.config_vars["rw_mixed_read_ratio"].get())])

        # v1.9.3 新增: 设备功耗测量参数
        if TEST_POWER in tests:
            oscill_path_val = self.config_vars["oscill_path"].get()
            if oscill_path_val:
                cmd.extend(["--oscill-path", oscill_path_val])
            power_limit_val = self.config_vars["power_limit"].get()
            if power_limit_val:
                cmd.extend(["--power-limit", str(power_limit_val)])
            # v1.9.3 新增: 功耗自动采集参数
            power_plan_val = self.config_vars.get("power_plan", tk.StringVar(value="")).get()
            if power_plan_val:
                cmd.extend(["--power-plan", power_plan_val])
            power_duration_val = self.config_vars.get("power_total_duration", tk.StringVar(value="")).get()
            if power_duration_val:
                cmd.extend(["--power-total-duration", str(power_duration_val)])
            power_interval_val = self.config_vars.get("power_sample_interval", tk.StringVar(value="")).get()
            if power_interval_val:
                cmd.extend(["--power-sample-interval", str(power_interval_val)])
            power_json_val = self.config_vars.get("power_json_report", tk.StringVar(value="")).get()
            if power_json_val:
                cmd.extend(["--power-json-report", power_json_val])
            power_ch_val = self.config_vars.get("power_channels", tk.StringVar(value="")).get()
            if power_ch_val:
                cmd.extend(["--power-channels", power_ch_val])

        return cmd

    def _update_cmd_preview(self):
        """更新命令预览。"""
        cmd = self.build_command()
        if cmd:
            preview = " ".join(cmd)
            # 截断过长的预览
            if len(preview) > 500:
                preview = preview[:500] + "..."
        else:
            preview = "请先选择设备和测试项"

        self.cmd_preview.config(state=tk.NORMAL)
        self.cmd_preview.delete("1.0", tk.END)
        self.cmd_preview.insert(tk.END, preview)
        self.cmd_preview.config(state=tk.DISABLED)

    # ----------------------------------------------------------
    # 测试执行控制
    # ----------------------------------------------------------

    def start_test(self):
        """开始执行测试。"""
        try:
            self._start_test_internal()
        except Exception as e:
            import traceback
            self._append_log(f"[CRITICAL] start_test raised uncaught exception: {e}", "error")
            self._append_log(f"[DEBUG] Full traceback:\n{traceback.format_exc()}", "error")
            self._reset_ui_state()

    def _start_test_internal(self):
        """开始执行测试（内部实现，由 start_test 包装异常捕获）。"""
        if self.is_running:
            return

        tr = self.translator.tr
        # 验证
        if self.device_combo.current() < 0:
            messagebox.showerror(tr("dialog.error"), tr("dialog.no_device"))
            return
        tests = self._get_selected_tests()
        if not tests:
            messagebox.showerror(tr("dialog.error"), tr("dialog.no_test_selected"))
            return

        # v1.9.2 新增: 性能测试状态校验（仅当选择了性能测试时）
        if TEST_PERF in tests:
            device = self.device_list[self.device_combo.current()]["path"]
            # 先刷新当前状态
            try:
                serial = get_device_serial(device)
                state_info = load_ssd_state(serial)
                self.current_ssd_state = state_info["state"]
                self._update_state_display(self.current_ssd_state, f"序列号: {serial}")
            except Exception as e:
                self._append_log(f"Failed to read SSD state: {e}, defaulting to Unknown", "warning")
                self.current_ssd_state = SSD_STATE_UNKNOWN

            # 确定目标状态（v1.9.2: fob/steady/unknown）
            perf_state = self.config_vars["perf_state"].get()
            if perf_state == PERF_FOB:
                target_state = SSD_STATE_FOB
                target_name = "FOB"
            elif perf_state == PERF_STEADY:
                target_state = SSD_STATE_STEADY
                target_name = "Steady"
            else:  # unknown
                target_state = SSD_STATE_UNKNOWN
                target_name = self.translator.tr("state.unknown_direct")

            current_name = self.current_ssd_state.upper()

            # 分支A: Unknown -> 直接测试
            if self.current_ssd_state == SSD_STATE_UNKNOWN:
                self._append_log(f"Current SSD state is Unknown, will run performance test directly (no state transition)", "info")
            # 分支B: 状态一致 -> 直接测试，不重复进入
            elif self.current_ssd_state == target_state:
                self._skip_precondition = True  # v1.9.2: 状态一致，跳过预处理/擦除
                self._append_log(f"Current SSD state ({current_name}) matches target state ({target_name}), running performance test directly (no re-entry, skip preconditioning)", "info")
            # 分支C: 状态不一致 -> 警告弹窗
            else:
                tr = self.translator.tr
                msg = (f"{tr('dialog.state_mismatch_header')}\n\n"
                       f"{tr('dialog.state_mismatch_current')}: {current_name}\n"
                       f"{tr('dialog.state_mismatch_target')}: {target_name}\n\n"
                       f"{tr('dialog.state_mismatch_confirm').format(target=target_name)}\n\n"
                       f"{tr('dialog.state_mismatch_yes')}\n"
                       f"{tr('dialog.state_mismatch_no')}")
                auto_enter = messagebox.askyesno(tr("dialog.state_mismatch_title"), msg)
                if auto_enter:
                    self._append_log(f"User confirmed auto-enter {target_name} state before testing", "info")
                    self._enter_ssd_state_and_test(target_state, tests)
                    return
                else:
                    self._append_log(f"User cancelled test, please manually control SSD state to match target state with current state", "warning")
                    return

        # v1.9.2 调试: 状态判断完成，准备构建命令
        self._append_log(f"[DEBUG] State check complete, current_state={self.current_ssd_state}, skip_precond={self._skip_precondition}", "info")

        # 检查 root 权限
        # v1.9.3: 仅选择「设备功耗测量」时不需要 root 权限（oscill GUI 以普通用户运行即可）
        only_power_test = (tests == [TEST_POWER])
        if only_power_test:
            self._append_log("Only device power measurement selected, no root required, executing directly", "info")
            cmd = self.build_command()
        elif os.geteuid() != 0 and not self.config_vars["dry_run"].get():
            result = messagebox.askyesno(tr("dialog.permission_title"), tr("dialog.permission_msg"))
            if not result:
                return
            # 使用 pkexec 提权
            cmd = self.build_command()
            cmd = ["pkexec"] + cmd
        else:
            cmd = self.build_command()

        # v1.9.2 调试: 命令构建完成
        self._append_log(f"[DEBUG] Command build complete, {len(cmd)} parameters total", "info")
        if cmd:
            self._append_log(f"[DEBUG] Command: {' '.join(cmd)}", "info")
        else:
            self._append_log("[DEBUG] Warning: build_command returned empty list!", "error")

        # 更新命令预览
        self._update_cmd_preview()

        # 清空日志（GUI 摘要区域只显示标准摘要行）
        self.clear_log(silent=True)
        current_time = datetime.now().strftime("%H:%M:%S")
        selected = self._get_selected_tests()
        self._append_log(f"{current_time} - Test started | Device: {self.device_list[self.device_combo.current()]['path']} | "
                         f"测试项: {','.join(selected)} | 详细日志见日志文件", "header")

        # 更新 UI 状态
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.progress_label.config(text="运行中...")
        self.status_var.set("测试运行中...")

        # 启动子进程
        try:
            self._append_log("[DEBUG] Starting test subprocess...", "info")
            self.test_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=getattr(self, "_package_root", None),
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )
            self._append_log(f"[DEBUG] Subprocess started, PID={self.test_process.pid}", "info")
        except Exception as e:
            import traceback
            self._append_log(f"Failed to start test process: {e}", "error")
            self._append_log(f"[DEBUG] Exception details: {traceback.format_exc()}", "error")
            self._reset_ui_state()
            return

        # 启动输出读取线程
        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

    def stop_test(self):
        """停止正在运行的测试。"""
        if not self.is_running:
            return

        tr = self.translator.tr
        if not messagebox.askyesno(tr("dialog.stop_title"), tr("dialog.stop_confirm_detail")):
            return

        self._append_log("\nUser requested to stop test...", "warning")

        if self.test_process and self.test_process.poll() is None:
            try:
                self.test_process.terminate()
                self.test_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.test_process.kill()
                self.test_process.wait()
            except Exception as e:
                self._append_log(f"Error while stopping process: {e}", "error")

        self._append_log("Test stopped", "warning")
        self._reset_ui_state()

    def _read_output(self):
        """后台线程：读取子进程输出，GUI 只显示标准摘要行。"""
        current_test = ""
        total_tests = len(self._get_selected_tests())
        current_idx = 0

        try:
            for line in self.test_process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                # 解析进度：[x/y] 开始测试（不显示，只更新进度条）
                match = re.search(r"\[(\d+)/(\d+)\]\s*开始测试[:：]\s*(.+)", line)
                if match:
                    current_idx = int(match.group(1))
                    total_tests = int(match.group(2))
                    current_test = match.group(3).strip()
                    progress_pct = (current_idx - 1) / total_tests * 100
                    self.root.after(0, lambda p=progress_pct, t=current_test: self._update_progress(p, f"[{current_idx}/{total_tests}] {t}"))

                # 显示标准摘要行 [SUMMARY]
                if line.startswith("[SUMMARY]"):
                    # 去掉 [SUMMARY] 前缀
                    summary_line = line[len("[SUMMARY] "):]
                    # 根据 Status 字段决定颜色
                    tag = "info"
                    if "Status:PASS" in summary_line:
                        tag = "success"
                    elif "Status:FAIL" in summary_line:
                        tag = "error"
                    elif "Status:ERROR" in summary_line:
                        tag = "error"
                    self.root.after(0, lambda l=summary_line, t=tag: self._append_log(l, t))

                    # 摘要行出现表示当前测试完成，更新进度
                    if current_idx > 0 and total_tests > 0:
                        progress_pct = current_idx / total_tests * 100
                        self.root.after(0, lambda p=progress_pct: self._update_progress(p, None))

                # v1.9.2 新增: 显示性能测试关键信息（FOB/稳态/IOPS/带宽/日志路径等）
                elif any(kw in line for kw in [
                    "FOB 状态性能测试", "稳态性能测试", "全参数 FIO 测试",
                    "结构化日志:", "文本日志:", "FIO 执行完成",
                    "执行命令:", "名称:", "IOPS:", "带宽:", "BW:",
                    "稳态预处理", "WDPC", "WIPC", "NVMe User Data Erase",
                    "blkdiscard", "FOB 擦除", "稳态检测", "达到稳态",
                    "ERROR", "Error", "error", "Traceback", "Exception",
                    "WARNING", "Warning", "失败", "FAIL"
                ]):
                    tag = "info"
                    if any(kw in line for kw in ["ERROR", "Error", "error", "Traceback", "Exception", "失败", "FAIL"]):
                        tag = "error"
                    elif any(kw in line for kw in ["WARNING", "Warning", "警告"]):
                        tag = "warning"
                    elif any(kw in line for kw in ["完成", "成功", "达到稳态", "PASS"]):
                        tag = "success"
                    self.root.after(0, lambda l=line, t=tag: self._append_log(l, t))

            # 等待进程结束
            returncode = self.test_process.wait()
            self.root.after(0, lambda rc=returncode: self._on_test_finished(rc))

        except Exception as e:
            self.root.after(0, lambda: self._append_log(f"Output read thread exception: {e}", "error"))
            self.root.after(0, self._reset_ui_state)

    def _on_test_finished(self, returncode: int):
        """测试完成回调。"""
        self._skip_precondition = False  # v1.9.2: 重置跳过预处理标志
        current_time = datetime.now().strftime("%H:%M:%S")
        if returncode == 0:
            self._append_log(f"{current_time} - All tests completed - Result: PASS", "success")
            self.progress["value"] = 100
            self.progress_label.config(text="完成 (PASS)")
        else:
            self._append_log(f"{current_time} - Test completed - Some items failed (exit code: {returncode})", "error")
            self.progress_label.config(text=f"完成 (退出码 {returncode})")

        self.status_var.set(f"测试完成 | 退出码: {returncode} | 详细日志见日志文件")
        self._reset_ui_state()

    def _reset_ui_state(self):
        """重置 UI 状态。"""
        self.is_running = False
        self.test_process = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        # v1.9.2 修复: 重置状态栏和进度条，避免显示"正在进入..."残留
        if hasattr(self, "status_var"):
            self.status_var.set("就绪")
        if hasattr(self, "progress"):
            self.progress["value"] = 0
        if hasattr(self, "progress_label"):
            self.progress_label.config(text="")

    def _update_progress(self, value: float, text: Optional[str]):
        """更新进度条。"""
        self.progress["value"] = min(value, 100)
        if text:
            self.progress_label.config(text=text)

    # ----------------------------------------------------------
    # 日志操作
    # ----------------------------------------------------------

    def _append_log(self, message: str, tag: str = "info"):
        """追加日志到日志窗口。"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def clear_log(self, silent: bool = False):
        """清空日志。"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        if not silent:
            self.status_var.set("日志已清空")

    def export_log(self):
        """导出日志到文件。"""
        tr = self.translator.tr
        content = self.log_text.get("1.0", tk.END)
        if not content.strip():
            messagebox.showinfo(tr("dialog.info"), tr("dialog.log_empty"))
            return

        default_name = f"ssd_test_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path = filedialog.asksaveasfilename(title=tr("dialog.export_log_title"), defaultextension=".txt",
                                             initialfile=default_name,
                                             filetypes=[(tr("dialog.text_files"), "*.txt"), (tr("dialog.all_files"), "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                messagebox.showinfo(tr("dialog.success"), tr("dialog.log_exported_to").format(path=path))
                self.status_var.set(f"日志已导出: {path}")
            except Exception as e:
                messagebox.showerror(tr("dialog.error"), tr("dialog.export_failed").format(error=e))

    # ----------------------------------------------------------
    # 配置保存/加载
    # ----------------------------------------------------------

    def save_config(self):
        """保存当前配置到 JSON 文件。"""
        tr = self.translator.tr
        config = {
            "device": self.device_combo.get(),
            "tests": self._get_selected_tests(),
            "params": {},
            "rw_file_sizes": [size for size, var in self.rw_size_vars.items() if var.get()],
            "perf_tasks": [t.to_dict() for t in self.perf_tasks],  # v1.8.0 保存批量任务
        }
        for key, var in self.config_vars.items():
            config["params"][key] = var.get()

        default_name = f"ssd_test_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = filedialog.asksaveasfilename(title=tr("dialog.save_config_title"), defaultextension=".json",
                                             initialfile=default_name,
                                             filetypes=[(tr("dialog.json_files"), "*.json"), (tr("dialog.all_files"), "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                messagebox.showinfo(tr("dialog.success"), tr("dialog.config_saved_to").format(path=path))
                self.status_var.set(f"配置已保存: {path}")
            except Exception as e:
                messagebox.showerror(tr("dialog.error"), tr("dialog.save_failed").format(error=e))

    def load_config(self):
        """从 JSON 文件加载配置。"""
        tr = self.translator.tr
        path = filedialog.askopenfilename(title=tr("dialog.load_config_title"),
                                           filetypes=[(tr("dialog.json_files"), "*.json"), (tr("dialog.all_files"), "*.*")])
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 恢复测试项
            tests = config.get("tests", [])
            for item_id, var in self.selected_tests.items():
                var.set(item_id in tests)

            # 恢复参数
            params = config.get("params", {})
            for key, value in params.items():
                if key in self.config_vars:
                    self.config_vars[key].set(value)

            # 恢复 RW 文件大小选择
            rw_sizes = config.get("rw_file_sizes", list(RW_FILE_SIZES.keys()))
            for size, var in self.rw_size_vars.items():
                var.set(size in rw_sizes)

            # v1.8.0 恢复批量任务列表
            saved_tasks = config.get("perf_tasks", [])
            if saved_tasks:
                self.perf_tasks = [PerfTask.from_dict(t) for t in saved_tasks if isinstance(t, dict)]
                self._refresh_task_list()

            self._update_cmd_preview()
            messagebox.showinfo(tr("dialog.success"), tr("dialog.config_loaded_from").format(path=path))
            self.status_var.set(f"配置已加载: {path}")
        except Exception as e:
            messagebox.showerror(tr("dialog.error"), tr("dialog.load_failed").format(error=e))

    # ----------------------------------------------------------
    # 其他
    # ----------------------------------------------------------

    def _open_report_dir(self):
        """打开报告输出目录。"""
        output_dir = self.config_vars["output_dir"].get()
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", output_dir])
        except Exception as e:
            tr = self.translator.tr
            messagebox.showerror(tr("dialog.error"), tr("dialog.open_dir_failed").format(error=e, path=output_dir))


    def launch_oscill_power_tool(self):
        """独立启动 oscill 示波器功耗测量工具（不阻塞 GUI 主线程）。"""
        # 解析 oscill 项目路径
        oscill_path = self.config_vars.get("oscill_path", tk.StringVar(value="")).get()
        if not oscill_path or not os.path.isdir(oscill_path):
            # 共置部署：使用脚本所在目录
            oscill_path = os.path.dirname(os.path.abspath(__file__))

        # 验证路径
        gui_file = os.path.join(oscill_path, "oscill", "gui.py")
        if not os.path.isfile(gui_file):
            messagebox.showerror("Error",
                f"oscill project path invalid!\n\n"
                f"Path: {oscill_path}\n"
                f"Not found: {gui_file}\n\n"
                f"Please ensure ssd_test_tool/ package is in the same directory as oscill/ package,\n"
                f"or configure the oscill project path correctly in the Power Measurement tab.")
            return

        # v1.9.3 新增: 构建自动采集参数环境变量（独立启动也传递参数）
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        power_plan_val = self.config_vars.get("power_plan", tk.StringVar(value="")).get()
        if power_plan_val:
            env["OSCILL_AUTO_PLAN"] = power_plan_val
        power_duration_val = self.config_vars.get("power_total_duration", tk.StringVar(value="")).get()
        if power_duration_val:
            try:
                env["OSCILL_AUTO_DURATION"] = str(float(power_duration_val))
            except ValueError:
                pass
        power_interval_val = self.config_vars.get("power_sample_interval", tk.StringVar(value="")).get()
        if power_interval_val:
            try:
                env["OSCILL_AUTO_INTERVAL"] = str(float(power_interval_val))
            except ValueError:
                pass
        power_channels_val = self.config_vars.get("power_channels", tk.StringVar(value="")).get()
        if power_channels_val:
            env["OSCILL_AUTO_CHANNELS"] = power_channels_val
        output_dir_val = self.config_vars.get("output_dir", tk.StringVar(value=os.path.join(getattr(self, "_ssd_test_tool_root", "."), "reports"))).get()
        if output_dir_val:
            env["OSCILL_AUTO_JSON_DIR"] = os.path.abspath(output_dir_val)

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "oscill.gui"],
                cwd=oscill_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            self.status_var.set(f"Power measurement tool launched (PID={proc.pid})")
            self._append_log(f"Power measurement tool launched independently, PID={proc.pid}, path={oscill_path}", "info")
            self._append_log("Please complete power measurement in the oscill window, you can close the oscill window directly when done", "info")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch power measurement tool:\n{e}\n\nPath: {oscill_path}")

    def _open_log_file(self):
        """打开最新的详细日志文件。"""
        tr = self.translator.tr
        log_dir = self.config_vars["log_dir"].get()
        if not os.path.exists(log_dir):
            messagebox.showinfo(tr("dialog.info"), tr("dialog.log_dir_not_exist").format(path=log_dir))
            return
        # 找到最新的日志文件
        try:
            log_files = [f for f in os.listdir(log_dir) if f.endswith(".log")]
            if not log_files:
                messagebox.showinfo(tr("dialog.info"), tr("dialog.no_log_files"))
                return
            log_files.sort(reverse=True)
            latest_log = os.path.join(log_dir, log_files[0])
            subprocess.Popen(["xdg-open", latest_log])
        except Exception as e:
            messagebox.showerror(tr("dialog.error"), tr("dialog.open_log_failed").format(error=e))

    def _show_tool_usage(self):
        """显示工具使用说明对话框（支持 i18n）。"""
        tr = self.translator.tr
        help_text = tr("dialog.help_content").format(version=SCRIPT_VERSION)
        win = tk.Toplevel(self.root)
        win.title(tr("dialog.tool_usage_title"))
        win.geometry("680x620")
        text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("TkDefaultFont", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text.insert(tk.END, help_text)
        text.config(state=tk.DISABLED)

    def _show_about(self):
        """显示关于对话框（支持 i18n）。"""
        tr = self.translator.tr
        about_text = tr("dialog.about_content").format(version=SCRIPT_VERSION)
        messagebox.showinfo(tr("dialog.about_title"), about_text)

    def _on_close(self):
        """窗口关闭事件。"""
        if self.is_running:
            tr = self.translator.tr
            if not messagebox.askyesno(tr("dialog.exit_title"), tr("dialog.exit_confirm")):
                return
            if self.test_process and self.test_process.poll() is None:
                try:
                    self.test_process.terminate()
                    self.test_process.wait(timeout=3)
                except Exception:
                    self.test_process.kill()
        self.root.destroy()

