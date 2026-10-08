#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - i18n Translator Module

Auto-extracted from ssd_test_v1.9.3.py for modularization.
Contains: I18N constants, translation dictionaries, Translator class.
"""

I18N_ZH_CN = "zh_CN"
I18N_EN_US = "en_US"

I18N_LANGUAGE_NAMES = {
    I18N_ZH_CN: "中文",
    I18N_EN_US: "English",
}


_I18N_ZH_TEXTS = {
    # ---- 应用 ----
    "app.title": "SSD 自动化测试平台",
    "app.version_prefix": "版本 v",
    "app.stdlib_only": "仅标准库",

    # ---- 菜单栏 ----
    "menu.file": "文件",
    "menu.save_config": "保存配置...",
    "menu.load_config": "加载配置...",
    "menu.export_log": "导出日志...",
    "menu.exit": "退出",
    "menu.tools": "工具",
    "menu.scan_devices": "扫描设备",
    "menu.clear_log": "清空日志",
    "menu.open_report_dir": "打开报告目录",
    "menu.launch_power_tool": "启动功耗测量工具",
    "menu.help": "帮助",
    "menu.usage": "使用说明",
    "menu.about": "关于",

    # ---- 设备选择栏 ----
    "frame.device_select": "设备选择",
    "label.device_under_test": "待测设备：",
    "btn.scan": "扫描设备",
    "btn.device_info": "设备信息",
    "btn.start_test": "▶  开始测试  ",
    "btn.stop": "■ 停止",

    # ---- 测试项选择 ----
    "frame.test_items": "测试项选择",
    "btn.select_all": "全选",
    "btn.clear_all": "清空",

    # ---- 测试项名称与描述 ----
    "test.fw.name": "固件测试",
    "test.fw.desc": "升级/回退固件版本",
    "test.smart.name": "SMART 检查",
    "test.smart.desc": "读取 SMART 健康状态",
    "test.capacity.name": "容量测试",
    "test.capacity.desc": "验证磁盘实际容量",
    "test.perf.name": "性能测试",
    "test.perf.desc": "fio 读写性能基准",
    "test.rw.name": "读/写测试",
    "test.rw.desc": "数据读写完整性验证",
    "test.powercycle.name": "正常电源循环",
    "test.powercycle.desc": "正常关机断电开机循环测试",
    "test.spor.name": "意外电源循环(SPOR)",
    "test.spor.desc": "异常断电后数据一致性测试",
    "test.osint.name": "操作系统中断(OSINT)",
    "test.osint.desc": "OS 中断下数据一致性测试",
    "test.power.name": "设备功耗测量",
    "test.power.desc": "验证SSD空闲/活动R/W功耗，调用oscill示波器上位机",

    # ---- 参数配置 ----
    "frame.param_config": "参数配置",
    "tab.general": "通用",
    "tab.firmware": "固件",
    "tab.performance": "性能",
    "tab.powercycle": "正常电源循环",
    "tab.spor": "意外电源循环(SPOR)",
    "tab.osint": "操作系统中断(OSINT)",
    "tab.rw": "读/写测试",
    "tab.power": "功耗测量",

    # ---- 通用参数页 ----
    "label.output_dir": "报告输出目录：",
    "label.log_dir": "日志输出目录：",
    "cb.dry_run": "试运行模式（dry-run，只打印命令不执行）",
    "cb.assume_yes": "自动确认数据销毁（-y，推荐开启）",
    "cb.verbose": "详细输出（verbose）",

    # ---- 固件参数页 ----
    "label.fw_image": "固件镜像：",
    "btn.browse": "浏览...",
    "label.fw_action": "操作类型：",
    "label.fw_slot": "固件槽位：",
    "label.fw_slot_hint": "(1-7，留空自动选择)",

    # ---- 性能参数页 ----
    "frame.ssd_state": "SSD 状态控制",
    "label.current_state": "当前状态：",
    "label.serial": "序列号: -",
    "btn.enter_fob": "进入 FOB",
    "btn.enter_steady": "进入稳态",
    "btn.refresh": "刷新",
    "btn.reset_unknown": "重置Unknown",
    "frame.task_queue": "批量任务排队",
    "label.note": "备注:",
    "btn.add_task": "＋新增任务",
    "btn.delete": "删除",
    "btn.export_json": "导出JSON",
    "label.task_count": "0组",
    "label.perf_state": "测试状态：",
    "label.perf_test_name": "测试项目名称：",
    "label.io_load_config": "IO负载配置",
    "label.direct": "direct：",
    "label.ioengine": "ioengine：",
    "label.bs": "块大小 bs：",
    "label.iodepth": "队列深度 iodepth：",
    "label.numjobs": "并发任务 numjobs：",
    "label.rw": "读写模式 rw：",
    "label.rwmixread": "读占比 rwmixread：",
    "label.rwmixread_hint": "(0=纯写, 100=纯读, 仅randrw/rw生效)",
    "label.test_range": "测试范围与时长",
    "label.size": "测试范围 size：",
    "label.size_hint": "(百分比如3% 或 固定容量如10G)",
    "label.runtime": "测试时长 runtime(秒)：",
    "label.runtime_hint": "(time_based固定开启)",
    "cb.text_log": "同时输出文本格式日志（默认仅json+结构化日志）",
    "label.cmd_preview": "FIO命令预览（实时更新）：",

    # ---- 电源循环参数页 ----
    "label.pc_cycles": "循环次数：",
    "label.pc_power_mode": "电源控制模式：",
    "label.ipmi_host": "IPMI 主机：",
    "label.ipmi_user": "IPMI 用户名：",
    "label.ipmi_pass": "IPMI 密码：",
    "label.pc_off_interval": "断电间隔(秒)：",
    "label.pc_rw_duration": "读写持续(秒)：",
    "label.pc_pattern": "数据 Pattern：",
    "label.pc_pattern_size": "Pattern写入量(GB)：",
    "label.pc_link_check": "PCIe Link校验：",

    # ---- SPOR 参数页 ----
    "label.spor_cycles": "循环次数：",
    "label.spor_power_mode": "断电方式：",
    "label.spor_delay": "写入延时(秒)：",
    "label.spor_test_size": "测试大小(GB)：",
    "label.spor_poweroff_delay": "硬件断电延时(ms)：",
    "cb.spor_mixed_rw": "混合读写模式（randrw，更接近真实负载）",
    "label.spor_mixed_read_ratio": "混合读比例(%)：",
    "label.spor_timeboard_port": "Timeboard 串口：",
    "label.spor_enhanced_iodepth": "写入队列深度：",
    "label.spor_enhanced_bs": "写入块大小：",
    "cb.spor_final_test": "循环结束后执行最终完整功能测试",

    # ---- OSINT 参数页 ----
    "label.osint_cycles": "循环次数：",
    "label.osint_sleep_type": "休眠类型：",
    "label.osint_sleep_duration": "休眠时长(秒)：",
    "cb.osint_io_idle": "空闲状态休眠（不启动 IO 负载，默认活跃IO模式）",
    "label.osint_io_duration": "IO 持续(秒)：",
    "label.osint_mount_point": "挂载点：",

    # ---- 读/写测试参数页 ----
    "label.rw_mode": "测试模式：",
    "label.rw_mode_hint": "(full_disk/file_cycle/long_run/all)",
    "label.rw_size_select": "文件大小选择：",
    "label.rw_cycles": "每大小循环次数：",
    "label.rw_pattern": "数据 pattern：",
    "label.rw_verify": "校验方式：",
    "label.rw_long_hours": "长期运行(小时)：",
    "label.rw_block_size": "块大小：",
    "label.rw_iodepth": "队列深度：",
    "label.rw_numjobs": "并发 job 数：",
    "label.rw_mixed_read_ratio": "混合读比例(%)：",

    # ---- 功耗测量参数页 ----
    "label.oscill_path": "oscill 项目路径：",
    "label.oscill_path_hint": "（留空则使用脚本所在目录，共置部署默认即可）",
    "label.power_limit": "功耗限值(W)：",
    "label.power_limit_hint": "（留空则使用 oscill 侧限值判定）",
    "label.auto_acq_config": "自动采集配置：",
    "label.power_plan": "测试规划：",
    "label.power_plan_hint": "（选择 POWER-01B~POWER-06，或留空由 oscill 手动选择）",
    "label.power_total_duration": "总采集时间(秒)：",
    "label.power_total_duration_hint": "（留空则手动控制采集时长；设置后 oscill 将自动停止）",
    "label.power_sample_interval": "采集间隔(秒)：",
    "label.power_sample_interval_hint": "（留空则使用 oscill 默认 1 秒；设置后按此间隔轮询采集）",
    "label.power_json_report": "JSON报告路径：",
    "label.power_json_report_hint": "选择 oscill 导出的 JSON 功耗报告，点击右侧按钮转换为英文日志",
    "label.power_channels": "通道选择：",
    "label.power_channels_hint": "勾选后启动 oscill 时自动映射对应通道（CH1=电压, CH2=电流）",
    "btn.browse_json": "浏览...",
    "btn.convert_to_log": "转换为Log",
    "label.operation_guide": "操作说明：",
    "power.help_text": (
        "1. 勾选左侧「设备功耗测量」后点击开始测试，将自动启动 oscill 示波器上位机\n"
        "2. 在 oscill 窗口中连接示波器、选择电压/电流通道、执行功耗测量\n"
        "3. 测量完成后在 oscill 中导出 JSON 报告到输出目录\n"
        "4. 关闭 oscill 窗口，ssd_test 将自动解析报告并判定 PASS/FAIL\n"
        "5. 也可通过菜单栏「工具」→「启动功耗测量工具」独立打开 oscill"
    ),

    # ---- 底部控制栏 ----
    "btn.clear_log_bottom": "清空日志",
    "btn.save_config_bottom": "保存配置",
    "btn.load_config_bottom": "加载配置",
    "btn.export_log_bottom": "导出日志",
    "btn.view_detail_log": "查看详细日志",

    # ---- 日志区域 ----
    "frame.test_summary": "测试结果摘要（仅显示摘要行，详细日志见日志文件）",

    # ---- 状态栏 ----
    "status.ready": "就绪",
    "status.scanning": "正在扫描设备...",
    "status.testing": "测试进行中...",
    "status.stopped": "已停止",
    "status.done": "测试完成",

    # ---- 语言切换 ----
    "label.language": "语言：",

    # ---- 弹窗 ----
    "dialog.error": "错误",
    "dialog.warning": "警告",
    "dialog.info": "提示",
    "dialog.confirm": "确认",
    "dialog.no_device": "请先选择待测设备",
    "dialog.no_test_selected": "请至少选择一个测试项",
    "dialog.test_running": "测试正在进行中",
    "dialog.stop_confirm": "确定要停止当前测试吗？",
    "dialog.config_saved": "配置已保存",
    "dialog.config_loaded": "配置已加载",
    "dialog.log_exported": "日志已导出",
    "dialog.about_title": "关于",
    "dialog.about_text": "SSD 自动化测试平台\n版本 v{version}\n\n支持固件、SMART、容量、性能、读写、电源循环、SPOR、OSINT、功耗测量等测试项。",
    "dialog.permission_title": "权限提示",
    "dialog.permission_msg": "测试需要 root 权限。\n是否使用 pkexec 提权执行？\n\n（也可以先 sudo 启动 GUI）",
    "dialog.power_tool_title": "启动功耗测量工具",
    "dialog.power_tool_path_invalid": "oscill 项目路径无效，请检查路径配置。\n路径应包含 oscill/gui.py 文件。",
    "dialog.power_tool_launched": "oscill 功耗测量工具已启动（PID: {pid}）",
    "dialog.select_oscill_dir": "选择 oscill 项目根目录",
    "dialog.select_fw_image": "选择固件镜像文件",
    "dialog.save_config_title": "保存配置",
    "dialog.load_config_title": "加载配置",
    "dialog.export_log_title": "导出日志",
    "dialog.json_files": "JSON 文件",
    "dialog.all_files": "所有文件",

    # ---- 日志消息 ----
    "log.scanning": "扫描存储设备...",
    "log.scan_done": "扫描完成，发现 {count} 个设备",
    "log.scan_failed": "扫描设备失败: {error}",
    "log.no_device": "未发现存储设备",
    "log.test_start": "开始测试: {tests}",
    "log.test_done": "测试完成",
    "log.test_stopped": "测试已停止",
    "log.power_start": "启动 oscill 功耗测量工具...",
    "log.power_launched": "oscill 已启动 (PID: {pid})，等待用户完成测量后关闭窗口",
    "log.power_waiting": "等待 oscill 窗口关闭...",
    "log.power_closed": "oscill 窗口已关闭 (返回码: {code})",
    "log.power_parsing": "正在查找功耗报告...",
    "log.power_report_found": "找到功耗报告: {path}",
    "log.power_no_report": "未找到功耗报告，请在 oscill 中导出 JSON 报告后重试",
    "log.power_result": "功耗测量结果: {status} - {message}",
    "log.power_skip": "功耗测量被跳过（oscill 进程被终止）",
    "log.power_only_no_root": "仅选择设备功耗测量，无需 root 权限，直接执行",
}

# 英文字典
_I18N_EN_TEXTS = {
    # ---- Application ----
    "app.title": "SSD Automated Test Platform",
    "app.version_prefix": "v",
    "app.stdlib_only": "Stdlib Only",

    # ---- Menu Bar ----
    "menu.file": "File",
    "menu.save_config": "Save Config...",
    "menu.load_config": "Load Config...",
    "menu.export_log": "Export Log...",
    "menu.exit": "Exit",
    "menu.tools": "Tools",
    "menu.scan_devices": "Scan Devices",
    "menu.clear_log": "Clear Log",
    "menu.open_report_dir": "Open Report Dir",
    "menu.launch_power_tool": "Launch Power Tool",
    "menu.help": "Help",
    "menu.usage": "Usage",
    "menu.about": "About",

    # ---- Device Selection Bar ----
    "frame.device_select": "Device Selection",
    "label.device_under_test": "Device Under Test:",
    "btn.scan": "Scan",
    "btn.device_info": "Device Info",
    "btn.start_test": "▶  Start Test  ",
    "btn.stop": "■ Stop",

    # ---- Test Item Selection ----
    "frame.test_items": "Test Items",
    "btn.select_all": "Select All",
    "btn.clear_all": "Clear All",

    # ---- Test Item Names & Descriptions ----
    "test.fw.name": "Firmware Test",
    "test.fw.desc": "Upgrade/downgrade firmware",
    "test.smart.name": "SMART Check",
    "test.smart.desc": "Read SMART health status",
    "test.capacity.name": "Capacity Test",
    "test.capacity.desc": "Verify actual disk capacity",
    "test.perf.name": "Performance Test",
    "test.perf.desc": "fio R/W performance benchmark",
    "test.rw.name": "Read/Write Test",
    "test.rw.desc": "Data R/W integrity verification",
    "test.powercycle.name": "Normal Power Cycle",
    "test.powercycle.desc": "Normal shutdown power cycle test",
    "test.spor.name": "Sudden Power-off (SPOR)",
    "test.spor.desc": "Data consistency after sudden power-off",
    "test.osint.name": "OS Interrupt (OSINT)",
    "test.osint.desc": "Data consistency under OS interrupt",
    "test.power.name": "Device Power Measurement",
    "test.power.desc": "Measure SSD idle/active R/W power via oscilloscope",

    # ---- Parameter Configuration ----
    "frame.param_config": "Parameter Config",
    "tab.general": "General",
    "tab.firmware": "Firmware",
    "tab.performance": "Performance",
    "tab.powercycle": "Normal Power Cycle",
    "tab.spor": "Sudden Power-off (SPOR)",
    "tab.osint": "OS Interrupt (OSINT)",
    "tab.rw": "Read/Write Test",
    "tab.power": "Power Measurement",

    # ---- General Tab ----
    "label.output_dir": "Report Output Dir:",
    "label.log_dir": "Log Output Dir:",
    "cb.dry_run": "Dry-run mode (print commands only)",
    "cb.assume_yes": "Auto-confirm data destruction (-y, recommended)",
    "cb.verbose": "Verbose output",

    # ---- Firmware Tab ----
    "label.fw_image": "Firmware Image:",
    "btn.browse": "Browse...",
    "label.fw_action": "Action:",
    "label.fw_slot": "Firmware Slot:",
    "label.fw_slot_hint": "(1-7, auto if empty)",

    # ---- Performance Tab ----
    "frame.ssd_state": "SSD State Control",
    "label.current_state": "Current State:",
    "label.serial": "Serial: -",
    "btn.enter_fob": "Enter FOB",
    "btn.enter_steady": "Enter Steady",
    "btn.refresh": "Refresh",
    "btn.reset_unknown": "Reset Unknown",
    "frame.task_queue": "Batch Task Queue",
    "label.note": "Note:",
    "btn.add_task": "+Add Task",
    "btn.delete": "Delete",
    "btn.export_json": "Export JSON",
    "label.task_count": "0 tasks",
    "label.perf_state": "Test State:",
    "label.perf_test_name": "Test Name:",
    "label.io_load_config": "IO Load Config",
    "label.direct": "direct:",
    "label.ioengine": "ioengine:",
    "label.bs": "Block Size bs:",
    "label.iodepth": "Queue Depth iodepth:",
    "label.numjobs": "Concurrent Jobs numjobs:",
    "label.rw": "R/W Mode rw:",
    "label.rwmixread": "Read Ratio rwmixread:",
    "label.rwmixread_hint": "(0=write only, 100=read only, randrw/rw only)",
    "label.test_range": "Test Range & Duration",
    "label.size": "Test Range size:",
    "label.size_hint": "(percentage like 3% or fixed size like 10G)",
    "label.runtime": "Test Duration runtime(sec):",
    "label.runtime_hint": "(time_based always on)",
    "cb.text_log": "Also output text log (json+structured by default)",
    "label.cmd_preview": "FIO Command Preview (real-time):",

    # ---- Power Cycle Tab ----
    "label.pc_cycles": "Cycle Count:",
    "label.pc_power_mode": "Power Control Mode:",
    "label.ipmi_host": "IPMI Host:",
    "label.ipmi_user": "IPMI User:",
    "label.ipmi_pass": "IPMI Password:",
    "label.pc_off_interval": "Power-off Interval (sec):",
    "label.pc_rw_duration": "R/W Duration (sec):",
    "label.pc_pattern": "Data Pattern:",
    "label.pc_pattern_size": "Pattern Write Size (GB):",
    "label.pc_link_check": "PCIe Link Check:",

    # ---- SPOR Tab ----
    "label.spor_cycles": "Cycle Count:",
    "label.spor_power_mode": "Power-off Method:",
    "label.spor_delay": "Write Delay (sec):",
    "label.spor_test_size": "Test Size (GB):",
    "label.spor_poweroff_delay": "Hardware Power-off Delay (ms):",
    "cb.spor_mixed_rw": "Mixed R/W mode (randrw, closer to real workload)",
    "label.spor_mixed_read_ratio": "Mixed Read Ratio (%):",
    "label.spor_timeboard_port": "Timeboard Serial Port:",
    "label.spor_enhanced_iodepth": "Write Queue Depth:",
    "label.spor_enhanced_bs": "Write Block Size:",
    "cb.spor_final_test": "Run final full functional test after cycles",

    # ---- OSINT Tab ----
    "label.osint_cycles": "Cycle Count:",
    "label.osint_sleep_type": "Sleep Type:",
    "label.osint_sleep_duration": "Sleep Duration (sec):",
    "cb.osint_io_idle": "Idle sleep (no IO load, active IO by default)",
    "label.osint_io_duration": "IO Duration (sec):",
    "label.osint_mount_point": "Mount Point:",

    # ---- Read/Write Test Tab ----
    "label.rw_mode": "Test Mode:",
    "label.rw_mode_hint": "(full_disk/file_cycle/long_run/all)",
    "label.rw_size_select": "File Size Selection:",
    "label.rw_cycles": "Cycles per Size:",
    "label.rw_pattern": "Data Pattern:",
    "label.rw_verify": "Verification Method:",
    "label.rw_long_hours": "Long Run (hours):",
    "label.rw_block_size": "Block Size:",
    "label.rw_iodepth": "Queue Depth:",
    "label.rw_numjobs": "Concurrent Jobs:",
    "label.rw_mixed_read_ratio": "Mixed Read Ratio (%):",

    # ---- Power Measurement Tab ----
    "label.oscill_path": "oscill Project Path:",
    "label.oscill_path_hint": "(Empty = script directory, default for co-located deployment)",
    "label.power_limit": "Power Limit (W):",
    "label.power_limit_hint": "(Empty = use oscill-side limit)",
    "label.auto_acq_config": "Auto Acquisition Config:",
    "label.power_plan": "Test Plan:",
    "label.power_plan_hint": "(Select POWER-01B~POWER-06, or leave empty for manual selection in oscill)",
    "label.power_total_duration": "Total Duration (s):",
    "label.power_total_duration_hint": "(Empty = manual control; if set, oscill will auto-stop)",
    "label.power_sample_interval": "Sample Interval (s):",
    "label.power_sample_interval_hint": "(Empty = oscill default 1s; if set, poll at this interval)",
    "label.power_json_report": "JSON Report Path:",
    "label.power_json_report_hint": "Select oscill exported JSON power report, click button to convert to English log",
    "label.power_channels": "Channel Selection:",
    "label.power_channels_hint": "Checked channels will be auto-mapped to oscill on launch (CH1=Voltage, CH2=Current)",
    "btn.browse_json": "Browse...",
    "btn.convert_to_log": "Convert to Log",
    "label.operation_guide": "Operation Guide:",
    "power.help_text": (
        "1. Check 'Device Power Measurement' on the left and click Start Test to launch oscill\n"
        "2. In oscill window, connect scope, select V/I channels, run power measurement\n"
        "3. After measurement, export JSON report to output dir in oscill\n"
        "4. Close oscill window, ssd_test will auto-parse report and judge PASS/FAIL\n"
        "5. Or launch oscill independently via menu Tools -> Launch Power Tool"
    ),

    # ---- Bottom Control Bar ----
    "btn.clear_log_bottom": "Clear Log",
    "btn.save_config_bottom": "Save Config",
    "btn.load_config_bottom": "Load Config",
    "btn.export_log_bottom": "Export Log",
    "btn.view_detail_log": "View Detail Log",

    # ---- Log Area ----
    "frame.test_summary": "Test Result Summary (summary only, detail in log file)",

    # ---- Status Bar ----
    "status.ready": "Ready",
    "status.scanning": "Scanning devices...",
    "status.testing": "Testing in progress...",
    "status.stopped": "Stopped",
    "status.done": "Test completed",

    # ---- Language Switch ----
    "label.language": "Language:",

    # ---- Dialogs ----
    "dialog.error": "Error",
    "dialog.warning": "Warning",
    "dialog.info": "Info",
    "dialog.confirm": "Confirm",
    "dialog.no_device": "Please select a device under test first",
    "dialog.no_test_selected": "Please select at least one test item",
    "dialog.test_running": "Test is running",
    "dialog.stop_confirm": "Are you sure you want to stop the current test?",
    "dialog.config_saved": "Configuration saved",
    "dialog.config_loaded": "Configuration loaded",
    "dialog.log_exported": "Log exported",
    "dialog.about_title": "About",
    "dialog.about_text": "SSD Automated Test Platform\nVersion v{version}\n\nSupports firmware, SMART, capacity, performance, R/W, power cycle, SPOR, OSINT, power measurement tests.",
    "dialog.permission_title": "Permission",
    "dialog.permission_msg": "Test requires root permission.\nUse pkexec to elevate?\n\n(Or launch GUI with sudo)",
    "dialog.power_tool_title": "Launch Power Tool",
    "dialog.power_tool_path_invalid": "Invalid oscill project path. Please check path config.\nPath should contain oscill/gui.py.",
    "dialog.power_tool_launched": "oscill power tool launched (PID: {pid})",
    "dialog.select_oscill_dir": "Select oscill Project Root Directory",
    "dialog.select_fw_image": "Select Firmware Image File",
    "dialog.save_config_title": "Save Config",
    "dialog.load_config_title": "Load Config",
    "dialog.export_log_title": "Export Log",
    "dialog.json_files": "JSON Files",
    "dialog.all_files": "All Files",

    # ---- Log Messages ----
    "log.scanning": "Scanning storage devices...",
    "log.scan_done": "Scan complete, found {count} device(s)",
    "log.scan_failed": "Device scan failed: {error}",
    "log.no_device": "No storage devices found",
    "log.test_start": "Starting tests: {tests}",
    "log.test_done": "Test completed",
    "log.test_stopped": "Test stopped",
    "log.power_start": "Launching oscill power tool...",
    "log.power_launched": "oscill launched (PID: {pid}), waiting for user to finish and close window",
    "log.power_waiting": "Waiting for oscill window to close...",
    "log.power_closed": "oscill window closed (return code: {code})",
    "log.power_parsing": "Looking for power report...",
    "log.power_report_found": "Power report found: {path}",
    "log.power_no_report": "No power report found, please export JSON report in oscill and retry",
    "log.power_result": "Power result: {status} - {message}",
    "log.power_skip": "Power measurement skipped (oscill process terminated)",
    "log.power_only_no_root": "Only power measurement selected, no root permission required, executing directly",
}

_I18N_TRANSLATIONS = {
    I18N_ZH_CN: _I18N_ZH_TEXTS,
    I18N_EN_US: _I18N_EN_TEXTS,
}




class Translator:
    """轻量翻译器，支持运行时语言切换和回调通知。"""

    def __init__(self, language=I18N_ZH_CN):
        if language not in _I18N_TRANSLATIONS:
            raise ValueError(f"unsupported language: {language!r}")
        self._language = language
        self._listeners = []

    @property
    def language(self):
        return self._language

    def set_language(self, language):
        if language not in _I18N_TRANSLATIONS:
            raise ValueError(f"unsupported language: {language!r}")
        if language == self._language:
            return
        self._language = language
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                pass

    def tr(self, key, **kwargs):
        """返回 key 对应的翻译文本，支持 str.format 占位符。"""
        text = _I18N_TRANSLATIONS[self._language].get(key)
        if text is None:
            text = _I18N_ZH_TEXTS.get(key, key)
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                pass
        return text

    def on_language_changed(self, listener):
        """注册语言切换回调。"""
        self._listeners.append(listener)

    @staticmethod
    def language_display_name(language):
        return I18N_LANGUAGE_NAMES.get(language, language)


def create_translator(language=I18N_ZH_CN):
    return Translator(language)

