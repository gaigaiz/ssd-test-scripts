"""Lightweight internationalization (i18n) with runtime language switching.

A simple dictionary-based translator is used instead of ``gettext`` so that the
language can be changed at runtime without recompiling ``.mo`` files. The GUI
holds one :class:`Translator` instance; widgets register a retranslation
callback through :meth:`Translator.on_language_changed` and update their text
whenever the language is switched.

Supported languages:

* ``zh_CN`` — Chinese (default)
* ``en_US`` — English
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Language codes
ZH_CN = "zh_CN"
EN_US = "en_US"

#: Human-readable names shown in the language selector, keyed by language code.
LANGUAGE_NAMES: dict[str, str] = {
    ZH_CN: "中文",
    EN_US: "English",
}

#: Ordered languages offered in the selector.
SUPPORTED_LANGUAGES: tuple[str, ...] = (ZH_CN, EN_US)

# Measurement profile keys (language-independent). The tuple mirrors the
# original ``_MEASUREMENT_PROFILES`` values: (SCPI parameter, result unit,
# signal unit). Signal unit "A" marks a current channel whose A/V factor is
# editable; "V" marks a voltage channel.
MEASUREMENT_PROFILES: dict[str, tuple[str, str, str]] = {
    "mean_voltage": ("MEAN", "V", "V"),
    "ripple_vpp": ("VPP", "V", "V"),
    "rms_voltage": ("RMS", "V", "V"),
    "min_voltage": ("MIN", "V", "V"),
    "max_voltage": ("MAX", "V", "V"),
    "mean_current": ("MEAN", "A", "A"),
    "peak_current": ("MAX", "A", "A"),
    "current_ripple": ("VPP", "A", "A"),
    "frequency": ("FREQUENCY", "Hz", "V"),
}

#: Ordered profile keys so the combo box keeps a stable order.
MEASUREMENT_PROFILE_ORDER: tuple[str, ...] = (
    "mean_voltage",
    "ripple_vpp",
    "rms_voltage",
    "min_voltage",
    "max_voltage",
    "mean_current",
    "peak_current",
    "current_ripple",
    "frequency",
)

# Power task workloads keyed by task id. Workload values stay in English
# because they are standard test terms and are written verbatim to JSON
# result records.
POWER_TASK_WORKLOADS: dict[str, tuple[str, ...]] = {
    "POWER-01B": ("Idle", "Full Load"),
    "POWER-02": ("Power Up", "Power Down", "Power Cycle", "Sudden Power-off", "PLP"),
    "POWER-03": ("Idle", "Full Load"),
    "POWER-04": ("Idle", "Read", "Write", "70R30W"),
    "POWER-05": (
        "Idle",
        "Sequential Read",
        "Sequential Write",
        "Random Read",
        "Random Write",
        "Full Load",
    ),
    "POWER-06": (
        "Idle",
        "Sequential Read",
        "Sequential Write",
        "Random Read",
        "Random Write",
        "Full Load",
    ),
}

#: Ordered task ids.
POWER_TASK_ORDER: tuple[str, ...] = (
    "POWER-01B",
    "POWER-02",
    "POWER-03",
    "POWER-04",
    "POWER-05",
    "POWER-06",
)


ZH_CN_TEXTS: dict[str, str] = {
    # ---- Application / language ----
    "app.title": "Tektronix MSO4034 测量控制台",
    "language.label": "语言",
    # ---- Common ----
    "common.pass": "PASS",
    "common.fail": "FAIL",
    "common.tbd": "TBD",
    "common.min": "Min",
    "common.max": "Max",
    "common.av_ratio": "A/V",
    # ---- Connection group ----
    "connection.title": "设备连接",
    "connection.resource": "VISA 资源名",
    "connection.scan": "扫描",
    "connection.connect": "连接",
    "connection.disconnect": "断开",
    "connection.not_connected": "未连接",
    "connection.connecting": "正在连接…",
    # ---- Control group ----
    "control.title": "示波器控制",
    "control.autoscale": "自动量程",
    "control.run": "开始采集",
    "control.stop": "停止采集",
    "control.enable_channels": "启用测量通道",
    "control.vertical_scale": "垂直刻度 (V/div)",
    "control.apply_scale": "应用到已选通道",
    "control.power_task": "SSD 功耗测试任务",
    "control.workload": "负载 / 事件状态",
    "control.apply_preset": "应用任务预设",
    "control.device_part": "Device Part No.",
    "control.firmware": "FW Version",
    "control.power_limit": "功率上限 (W)",
    "control.save_result": "保存测试结果 JSON",
    # ---- Measurement group ----
    "measurement.title": "实时测量",
    "measurement.start": "开始连续测量",
    "measurement.stop": "停止连续测量",
    # ---- Measurement profiles ----
    "profile.mean_voltage": "平均电压",
    "profile.ripple_vpp": "纹波峰峰值",
    "profile.rms_voltage": "有效值电压",
    "profile.min_voltage": "最小电压",
    "profile.max_voltage": "最大电压",
    "profile.mean_current": "平均电流",
    "profile.peak_current": "峰值电流",
    "profile.current_ripple": "电流纹波",
    "profile.frequency": "频率",
    # ---- Power tasks ----
    "task.POWER-01B": "POWER-01B 电源轨检查",
    "task.POWER-02": "POWER-02 上下电时序",
    "task.POWER-03": "POWER-03 稳压器输出",
    "task.POWER-04": "POWER-04 纹波与噪声",
    "task.POWER-05": "POWER-05 EVT 功耗",
    "task.POWER-06": "POWER-06 DVT 功耗",
    # ---- Waveform group ----
    "waveform.title": "波形",
    "waveform.read": "读取当前波形",
    "waveform.start": "开始实时波形",
    "waveform.stop": "停止实时波形",
    "waveform.save_csv": "保存 CSV",
    "waveform.time": "时间",
    "waveform.voltage": "电压",
    "waveform.current": "电流",
    "waveform.amplitude": "幅度",
    "waveform.needs_dependency": "波形显示需要安装 PyQt5 与 pyqtgraph",
    "waveform.pkpk_samples": "峰峰值: {pkpk:.4g} {unit}   采样点: {samples}",
    "waveform.points_label": "读取点数",
    # ---- Log group ----
    "log.title": "操作日志",
    "log.expand": "展开日志",
    "log.collapse": "收起日志",
    # ---- Status bar ----
    "status.not_connected": "状态：未连接",
    "status.scanning": "状态：正在扫描 VISA 设备",
    "status.resources_found": "状态：发现 {count} 个 VISA 资源",
    "status.scan_failed": "状态：VISA 扫描失败",
    "status.connecting": "状态：正在连接",
    "status.connected": "状态：已连接",
    "status.connect_failed": "状态：连接失败",
    "status.communicating": "状态：正在与设备通信",
    "status.operation_failed": "状态：操作失败",
    "status.reading_waveform": "状态：正在读取波形",
    "status.waveform_read": "状态：已读取 {count} 个通道波形",
    "status.waveform_failed": "状态：波形读取失败",
    "status.realtime_waveform": "状态：实时波形 {channels}",
    "status.realtime_failed": "状态：实时波形失败",
    "status.measuring": "状态：测量中",
    "status.measurement_completed": "状态：测量已完成",
    "status.measurement_failed": "状态：测量失败",
    # ---- Power summary ----
    "power.waiting": "同步功率：等待电压/电流测量",
    "power.stats_waiting": "持续统计：等待开始",
    "power.verdict_need_limits": "判定：TBD（请填写设计限值）",
    "power.need_pair": "同步功率：需同时选择电压与电流通道",
    "power.stats_need_pair": "持续统计：等待有效 V/I 通道",
    "power.verdict_waiting_stats": "判定：TBD（等待有效统计和限值）",
    "power.window": "同步窗口：平均 {avg:.6g} W | 峰值 {peak:.6g} W ({voltage} × {current})",
    "power.stats": "持续统计：{duration:.1f} s | 平均 {avg:.6g} W | 峰值 {peak:.6g} W | 能耗 {energy:.6g} {unit}",
    "power.evaluate_waiting": "TBD（等待统计）",
    "power.limit_format_error": "TBD（上限格式错误）",
    "power.limit_missing": "TBD（未填上限）",
    "power.limit_must_positive": "TBD（上限须大于 0）",
    # ---- Channel result status ----
    "result.limit_format_error": "TBD：限值格式错误",
    "result.min_gt_max": "TBD：Min 大于 Max",
    "result.no_limits": "TBD：未填写限值",
    "result.below_min": "FAIL：低于 Min",
    "result.above_max": "FAIL：高于 Max",
    "result.no_signal": "N/A：无有效信号",
    "result.not_selected": "未选择",
    "result.verdict_prefix": "判定：",
    # ---- Dialogs ----
    "dialog.error": "错误",
    "dialog.param_error": "参数错误",
    "dialog.scan_failed": "扫描失败",
    "dialog.connect_failed": "连接失败",
    "dialog.not_connected_title": "尚未连接",
    "dialog.please_connect": "请先连接示波器",
    "dialog.missing_dependency": "缺少依赖",
    "dialog.install_hint": "请安装 GUI 依赖：pip install -e \".[gui]\"",
    "dialog.channel_setup_failed": "通道设置失败",
    "dialog.device_operation_failed": "设备操作失败",
    "dialog.waveform_failed": "波形读取失败",
    "dialog.save_failed": "保存失败",
    "dialog.no_waveform_title": "没有波形",
    "dialog.no_waveform_msg": "请先点击“读取当前波形”",
    "dialog.no_measurement_title": "没有测量结果",
    "dialog.no_measurement_msg": "请先开始连续测量并取得至少一个通道结果",
    "dialog.save_waveform_title": "保存波形 CSV",
    "dialog.save_result_title": "保存 SSD 功耗测试结果",
    "dialog.csv_files": "CSV 文件",
    "dialog.json_files": "JSON 文件",
    "dialog.all_files": "所有文件",
    # ---- Parameter validation messages ----
    "msg.need_resource": "请填写或扫描选择 VISA 资源名",
    "msg.select_one_channel": "请至少选择一个通道",
    "msg.scale_positive": "垂直刻度必须是大于零的有限数字",
    "msg.factor_number": "{channel} 的 A/V 换算系数必须是数字",
    "msg.factor_positive": "{channel} 的 A/V 换算系数必须是大于零的有限数字",
    "msg.points_invalid": "读取点数必须是 2 到 1000000 之间的整数",
    # ---- Log messages ----
    "log.scan_done": "VISA 扫描完成：{count} 个资源",
    "log.scan_failed": "VISA 扫描失败：{error}",
    "log.connected": "已连接：{identity}",
    "log.connect_failed": "连接失败：{error}",
    "log.old_close_failed": "旧连接关闭失败：{error}",
    "log.stale_close_failed": "过期连接关闭失败：{error}",
    "log.disconnect_error": "断开设备时出错：{error}",
    "log.connection_closed": "设备连接已关闭",
    "log.background_failed": "后台操作失败：{error}",
    "log.run_started": "已开始采集",
    "log.run_stopped": "已停止采集",
    "log.autoscaled": "已执行自动量程",
    "log.device_op_failed": "设备操作失败：{error}",
    "log.scale_applied": "已设置 {channels} 为 {scale} V/div",
    "log.channel_setup_failed": "通道设置失败：{error}",
    "log.channel_unavailable": "{channel} 测量暂不可用：请检查通道是否有有效信号",
    "log.power_unavailable": "同步功率统计暂不可用：{error}",
    "log.measurement_failed": "测量失败：{error}",
    "log.waveforms_read": "已读取 {channels} 波形",
    "log.waveform_failed": "波形读取失败：{error}",
    "log.realtime_failed": "实时波形失败：{error}",
    "log.waveform_saved": "波形已保存：{path}",
    "log.waveform_save_failed": "波形保存失败：{error}",
    "log.result_saved": "测试结果已保存：{path}",
    "log.result_save_failed": "测试结果保存失败：{error}",
    "log.preset_applied": "已应用 {task} / {workload} 预设；数值限值需依据设计规范填写",
    # ---- CLI ----
    "cli.no_resources": "未发现 VISA 资源",
    "cli.specify_resource": "请指定 --resource 或 --simulate",
}

EN_US_TEXTS: dict[str, str] = {
    # ---- Application / language ----
    "app.title": "Tektronix MSO4034 Measurement Console",
    "language.label": "Language",
    # ---- Common ----
    "common.pass": "PASS",
    "common.fail": "FAIL",
    "common.tbd": "TBD",
    "common.min": "Min",
    "common.max": "Max",
    "common.av_ratio": "A/V",
    # ---- Connection group ----
    "connection.title": "Device Connection",
    "connection.resource": "VISA Resource",
    "connection.scan": "Scan",
    "connection.connect": "Connect",
    "connection.disconnect": "Disconnect",
    "connection.not_connected": "Not connected",
    "connection.connecting": "Connecting…",
    # ---- Control group ----
    "control.title": "Oscilloscope Control",
    "control.autoscale": "Autoscale",
    "control.run": "Run Acquisition",
    "control.stop": "Stop Acquisition",
    "control.enable_channels": "Enabled Measurement Channels",
    "control.vertical_scale": "Vertical Scale (V/div)",
    "control.apply_scale": "Apply to Selected Channels",
    "control.power_task": "SSD Power Test Task",
    "control.workload": "Workload / Event State",
    "control.apply_preset": "Apply Task Preset",
    "control.device_part": "Device Part No.",
    "control.firmware": "FW Version",
    "control.power_limit": "Power Limit (W)",
    "control.save_result": "Save Test Result JSON",
    # ---- Measurement group ----
    "measurement.title": "Real-time Measurement",
    "measurement.start": "Start Continuous Measurement",
    "measurement.stop": "Stop Continuous Measurement",
    # ---- Measurement profiles ----
    "profile.mean_voltage": "Mean Voltage",
    "profile.ripple_vpp": "Ripple Peak-to-Peak",
    "profile.rms_voltage": "RMS Voltage",
    "profile.min_voltage": "Minimum Voltage",
    "profile.max_voltage": "Maximum Voltage",
    "profile.mean_current": "Mean Current",
    "profile.peak_current": "Peak Current",
    "profile.current_ripple": "Current Ripple",
    "profile.frequency": "Frequency",
    # ---- Power tasks ----
    "task.POWER-01B": "POWER-01B Power Rail Check",
    "task.POWER-02": "POWER-02 Power Up/Down Sequence",
    "task.POWER-03": "POWER-03 Regulator Output",
    "task.POWER-04": "POWER-04 Ripple & Noise",
    "task.POWER-05": "POWER-05 EVT Power",
    "task.POWER-06": "POWER-06 DVT Power",
    # ---- Waveform group ----
    "waveform.title": "Waveform",
    "waveform.read": "Read Current Waveform",
    "waveform.start": "Start Real-time Waveform",
    "waveform.stop": "Stop Real-time Waveform",
    "waveform.save_csv": "Save CSV",
    "waveform.time": "Time",
    "waveform.voltage": "Voltage",
    "waveform.current": "Current",
    "waveform.amplitude": "Amplitude",
    "waveform.needs_dependency": "Waveform display requires PyQt5 and pyqtgraph",
    "waveform.pkpk_samples": "Pk-Pk: {pkpk:.4g} {unit}   Samples: {samples}",
    "waveform.points_label": "Points",
    # ---- Log group ----
    "log.title": "Operation Log",
    "log.expand": "Expand Log",
    "log.collapse": "Collapse Log",
    # ---- Status bar ----
    "status.not_connected": "Status: Not connected",
    "status.scanning": "Status: Scanning for VISA devices",
    "status.resources_found": "Status: Found {count} VISA resource(s)",
    "status.scan_failed": "Status: VISA scan failed",
    "status.connecting": "Status: Connecting",
    "status.connected": "Status: Connected",
    "status.connect_failed": "Status: Connection failed",
    "status.communicating": "Status: Communicating with device",
    "status.operation_failed": "Status: Operation failed",
    "status.reading_waveform": "Status: Reading waveform",
    "status.waveform_read": "Status: Read {count} channel waveform(s)",
    "status.waveform_failed": "Status: Waveform read failed",
    "status.realtime_waveform": "Status: Real-time waveform {channels}",
    "status.realtime_failed": "Status: Real-time waveform failed",
    "status.measuring": "Status: Measuring",
    "status.measurement_completed": "Status: Measurement completed",
    "status.measurement_failed": "Status: Measurement failed",
    # ---- Power summary ----
    "power.waiting": "Synchronized power: waiting for voltage/current measurement",
    "power.stats_waiting": "Running statistics: waiting to start",
    "power.verdict_need_limits": "Verdict: TBD (please fill in design limits)",
    "power.need_pair": "Synchronized power: select both a voltage and a current channel",
    "power.stats_need_pair": "Running statistics: waiting for valid V/I channels",
    "power.verdict_waiting_stats": "Verdict: TBD (waiting for valid statistics and limits)",
    "power.window": "Sync window: avg {avg:.6g} W | peak {peak:.6g} W ({voltage} × {current})",
    "power.stats": "Running: {duration:.1f} s | avg {avg:.6g} W | peak {peak:.6g} W | energy {energy:.6g} {unit}",
    "power.evaluate_waiting": "TBD (waiting for statistics)",
    "power.limit_format_error": "TBD (invalid limit format)",
    "power.limit_missing": "TBD (no limit entered)",
    "power.limit_must_positive": "TBD (limit must be greater than 0)",
    # ---- Channel result status ----
    "result.limit_format_error": "TBD: invalid limit format",
    "result.min_gt_max": "TBD: Min greater than Max",
    "result.no_limits": "TBD: no limits entered",
    "result.below_min": "FAIL: below Min",
    "result.above_max": "FAIL: above Max",
    "result.no_signal": "N/A: no valid signal",
    "result.not_selected": "Not selected",
    "result.verdict_prefix": "Verdict: ",
    # ---- Dialogs ----
    "dialog.error": "Error",
    "dialog.param_error": "Parameter Error",
    "dialog.scan_failed": "Scan Failed",
    "dialog.connect_failed": "Connection Failed",
    "dialog.not_connected_title": "Not Connected",
    "dialog.please_connect": "Please connect the oscilloscope first",
    "dialog.missing_dependency": "Missing Dependency",
    "dialog.install_hint": "Please install GUI dependencies: pip install -e \".[gui]\"",
    "dialog.channel_setup_failed": "Channel Setup Failed",
    "dialog.device_operation_failed": "Device Operation Failed",
    "dialog.waveform_failed": "Waveform Read Failed",
    "dialog.save_failed": "Save Failed",
    "dialog.no_waveform_title": "No Waveform",
    "dialog.no_waveform_msg": "Please click \"Read Current Waveform\" first",
    "dialog.no_measurement_title": "No Measurement Result",
    "dialog.no_measurement_msg": "Please start continuous measurement and obtain at least one channel result first",
    "dialog.save_waveform_title": "Save Waveform CSV",
    "dialog.save_result_title": "Save SSD Power Test Result",
    "dialog.csv_files": "CSV Files",
    "dialog.json_files": "JSON Files",
    "dialog.all_files": "All Files",
    # ---- Parameter validation messages ----
    "msg.need_resource": "Please enter or scan/select a VISA resource name",
    "msg.select_one_channel": "Please select at least one channel",
    "msg.scale_positive": "Vertical scale must be a finite number greater than zero",
    "msg.factor_number": "{channel} A/V conversion factor must be a number",
    "msg.factor_positive": "{channel} A/V conversion factor must be a finite number greater than zero",
    "msg.points_invalid": "Points must be an integer between 2 and 1000000",
    # ---- Log messages ----
    "log.scan_done": "VISA scan completed: {count} resource(s)",
    "log.scan_failed": "VISA scan failed: {error}",
    "log.connected": "Connected: {identity}",
    "log.connect_failed": "Connection failed: {error}",
    "log.old_close_failed": "Failed to close previous connection: {error}",
    "log.stale_close_failed": "Failed to close stale connection: {error}",
    "log.disconnect_error": "Error disconnecting device: {error}",
    "log.connection_closed": "Device connection closed",
    "log.background_failed": "Background operation failed: {error}",
    "log.run_started": "Acquisition started",
    "log.run_stopped": "Acquisition stopped",
    "log.autoscaled": "Autoscale executed",
    "log.device_op_failed": "Device operation failed: {error}",
    "log.scale_applied": "Set {channels} to {scale} V/div",
    "log.channel_setup_failed": "Channel setup failed: {error}",
    "log.channel_unavailable": "{channel} measurement unavailable: please check the channel for a valid signal",
    "log.power_unavailable": "Synchronized power statistics unavailable: {error}",
    "log.measurement_failed": "Measurement failed: {error}",
    "log.waveforms_read": "Read waveform(s): {channels}",
    "log.waveform_failed": "Waveform read failed: {error}",
    "log.realtime_failed": "Real-time waveform failed: {error}",
    "log.waveform_saved": "Waveform saved: {path}",
    "log.waveform_save_failed": "Waveform save failed: {error}",
    "log.result_saved": "Test result saved: {path}",
    "log.result_save_failed": "Test result save failed: {error}",
    "log.preset_applied": "Applied preset {task} / {workload}; numeric limits must be filled per the design specification",
    # ---- CLI ----
    "cli.no_resources": "No VISA resources found",
    "cli.specify_resource": "Please specify --resource or --simulate",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    ZH_CN: ZH_CN_TEXTS,
    EN_US: EN_US_TEXTS,
}


class Translator:
    """Translate UI strings and notify listeners when the language changes."""

    def __init__(self, language: str = ZH_CN) -> None:
        if language not in TRANSLATIONS:
            raise ValueError(f"unsupported language: {language!r}")
        self._language = language
        self._listeners: list[Callable[[], None]] = []

    @property
    def language(self) -> str:
        """Current language code."""
        return self._language

    def set_language(self, language: str) -> None:
        """Switch language and notify all registered listeners."""
        if language not in TRANSLATIONS:
            raise ValueError(f"unsupported language: {language!r}")
        if language == self._language:
            return
        self._language = language
        for listener in list(self._listeners):
            listener()

    def tr(self, key: str, **kwargs: Any) -> str:
        """Return the translated text for ``key``, optionally formatting it.

        Falls back to the Chinese table, then to the raw key, so a missing
        translation never raises. Keyword arguments are applied with
        ``str.format`` when provided.
        """
        text = TRANSLATIONS[self._language].get(key)
        if text is None:
            text = ZH_CN_TEXTS.get(key, key)
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                # A formatting mismatch must never crash the UI; return the
                # unformatted template instead.
                pass
        return text

    def on_language_changed(self, listener: Callable[[], None]) -> None:
        """Register a callback invoked after every language switch."""
        self._listeners.append(listener)

    @staticmethod
    def language_display_name(language: str) -> str:
        """Return the human-readable name for a language code."""
        return LANGUAGE_NAMES.get(language, language)


def validate_translation_keys() -> list[str]:
    """Return keys missing from either language table (empty list == OK).

    Used by the static/delivery checks to guarantee the two dictionaries
    stay in sync.
    """
    zh_keys = set(ZH_CN_TEXTS)
    en_keys = set(EN_US_TEXTS)
    return sorted(zh_keys.symmetric_difference(en_keys))
