"""PyQt5 operator console for connecting to and measuring the scope.

The GUI is built on PyQt5 and renders waveforms with PyQtGraph (replacing the
former Tkinter + matplotlib implementation). All instrument communication,
measurement and power calculations live in the untouched service/controller
layer; this module only calls those existing APIs.

All user-visible strings go through the :class:`~oscill.i18n.Translator` so the
interface can switch between Chinese and English at runtime.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from time import monotonic
from typing import Any

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from .controller import Oscilloscope, TektronixMSO4034, Waveform
from .i18n import (
    EN_US,
    MEASUREMENT_PROFILES,
    MEASUREMENT_PROFILE_ORDER,
    POWER_TASK_ORDER,
    POWER_TASK_WORKLOADS,
    ZH_CN,
    Translator,
)
from .services import (
    ChannelMeasurementResult,
    MeasurementRequest,
    PowerWindowStatistics,
    acquire_configured_waveforms,
    acquire_synchronized_power_statistics,
    collect_configured_measurements,
    open_tektronix_scope,
    save_configured_waveforms_csv,
)
from .transport import list_visa_resources
from .worker import SerialWorker

# PyQtGraph global appearance: white background, dark foreground, antialiasing.
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "#263238")
pg.setConfigOption("antialias", True)


class OscilloscopeApp(QMainWindow):
    """A small operator console for connecting to and measuring the scope."""

    _CHANNEL_COLORS = {
        "CH1": "#1565C0",
        "CH2": "#E65100",
        "CH3": "#2E7D32",
        "CH4": "#6A1B9A",
    }
    _ENGINEERING_PREFIXES = {
        -12: "p",
        -9: "n",
        -6: "u",
        -3: "m",
        0: "",
        3: "k",
        6: "M",
        9: "G",
    }

    def __init__(self) -> None:
        super().__init__()
        self.tr_obj = Translator(ZH_CN)
        self.tr_obj.on_language_changed(self._retranslate_ui)

        self.scope: TektronixMSO4034 | Oscilloscope | None = None
        self._worker = SerialWorker()
        self._closing = False
        self._connection_generation = 0
        self._measurement_generation = 0
        self._waveform_generation = 0
        self._measurement_inflight = False
        self._waveform_inflight = False
        self.polling = False
        self.waveform_polling = False
        self.channels = ("CH1", "CH2", "CH3", "CH4")

        # Channel configuration widgets/state.
        self.channel_checkboxes: dict[str, QCheckBox] = {}
        self.channel_profile_boxes: dict[str, QComboBox] = {}
        self.channel_factor_entries: dict[str, QLineEdit] = {}
        self.channel_min_entries: dict[str, QLineEdit] = {}
        self.channel_max_entries: dict[str, QLineEdit] = {}
        self.channel_value_labels: dict[str, QLabel] = {}
        self.channel_status_labels: dict[str, QLabel] = {}
        self._card_frames: dict[str, QGroupBox] = {}

        self.latest_measurements: dict[str, ChannelMeasurementResult] = {}
        self._last_channel_errors: dict[str, str] = {}

        # Power state.
        self.power_limit_entry: QLineEdit
        self.device_part_entry: QLineEdit
        self.firmware_entry: QLineEdit
        self.power_task_combo: QComboBox
        self.workload_combo: QComboBox
        self.power_summary_label: QLabel
        self.power_statistics_label: QLabel
        self.overall_result_label: QLabel
        self.latest_average_power_w: float | None = None
        self.latest_peak_power_w: float | None = None
        self.latest_energy_wh: float | None = None
        self._power_energy_j = 0.0
        self._power_started_at: float | None = None
        self._power_last_sample_at: float | None = None
        self._power_last_average_w: float | None = None
        self._power_duration_seconds = 0.0
        self._power_statistics_signature: tuple[str, str, float] | None = None
        self._last_power_error: str | None = None
        self._latest_overall = "TBD"
        # v1.9.3 新增: 自动采集参数（由 ssd_test 通过环境变量传递）
        self._auto_plan: str = ""
        self._auto_duration_s: float | None = None
        self._auto_interval_ms: int = 1000
        self._auto_stop_timer: QTimer | None = None
        self._auto_channels: list[str] = []
        self._auto_json_dir: str = ""
        self._auto_json_samples: list = []
        self._auto_next_export_s: float = 0.0
        self._auto_acq_start_s: float = 0.0
        self._auto_json_saved: bool = False

        self.last_waveforms: dict[str, Waveform] = {}
        self._plot_channels: list[str] | None = None
        self._plots: dict[str, pg.PlotItem] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}

        # Connection widgets.
        self.resource_combo: QComboBox
        self.scan_button: QPushButton
        self.connect_button: QPushButton
        self.identity_label: QLabel

        # Waveform buttons.
        self.waveform_button: QPushButton
        self.poll_button: QPushButton
        self.waveform_points_label: QLabel
        self.waveform_points_entry: QLineEdit

        # Log.
        self.log_text: QTextEdit
        self.log_toggle_button: QPushButton
        self.log_group: QGroupBox
        self._log_expanded = False

        self._build_ui()
        self._retranslate_ui()

    # ------------------------------------------------------------------ UI

    def set_auto_acquisition_params(self, plan: str = "", duration_s: float | None = None,
                                     interval_ms: int = 1000, channels: str = "",
                                     json_dir: str = "") -> None:
        """v1.9.3 新增: 设置自动采集参数。

        Args:
            plan: 测试规划 (POWER-01B~POWER-06)，空字符串表示不自动选择
            duration_s: 总采集时间(秒)，None 表示手动控制；设置后到达时间自动停止
            interval_ms: 采集间隔(毫秒)，默认 1000ms
            channels: 勾选的通道，逗号分隔，如 "CH1,CH2"
            json_dir: 自动导出 JSON 报告的目录，空字符串表示不自动导出
        """
        self._auto_plan = plan
        self._auto_duration_s = duration_s
        self._auto_interval_ms = max(100, int(interval_ms))
        self._auto_channels = [c.strip() for c in channels.split(",") if c.strip()] if channels else []
        self._auto_json_dir = json_dir
        # v1.9.3 新增: 输出自动采集配置日志
        parts = ["Auto-acquisition config received:"]
        if plan:
            parts.append(f"  plan={plan}")
        else:
            parts.append("  plan=(manual selection)")
        if duration_s is not None:
            parts.append(f"  total_duration={duration_s:.1f}s (auto-stop enabled)")
        else:
            parts.append("  total_duration=(manual stop)")
        parts.append(f"  sample_interval={self._auto_interval_ms}ms")
        if self._auto_channels:
            parts.append(f"  channels={','.join(self._auto_channels)}")
        else:
            parts.append("  channels=(manual selection)")
        if self._auto_json_dir:
            parts.append(f"  auto_json_dir={self._auto_json_dir}")
        else:
            parts.append("  auto_json_dir=(disabled)")
        self._log("\n".join(parts))

    def _apply_auto_channels(self) -> None:
        """v1.9.3 新增: 如果设置了自动通道，勾选对应的 channel_checkboxes。"""
        if not self._auto_channels:
            return
        for channel in self.channels:
            if channel in self.channel_checkboxes:
                self.channel_checkboxes[channel].setChecked(channel in self._auto_channels)
        self._log(f"Auto-selected channels: {','.join(self._auto_channels)}")

    def _apply_auto_plan(self) -> None:
        """v1.9.3 新增: 如果设置了自动规划，选择对应的 power_task_combo。"""
        if not self._auto_plan:
            return
        for index in range(self.power_task_combo.count()):
            if self.power_task_combo.itemData(index) == self._auto_plan:
                self.power_task_combo.setCurrentIndex(index)
                self._log(f"Auto-selected test plan: {self._auto_plan}")
                return
        # v1.9.3 新增: 规划未找到时输出警告
        available = ", ".join(
            str(self.power_task_combo.itemData(i))
            for i in range(self.power_task_combo.count())
        )
        self._log(f"WARNING: Auto plan '{self._auto_plan}' not found. Available: {available}")

    def _start_auto_stop_timer(self) -> None:
        """v1.9.3 新增: 如果设置了总采集时间，启动自动停止计时器。"""
        if self._auto_duration_s is None or self._auto_duration_s <= 0:
            return
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.stop()
        self._auto_stop_timer = QTimer(self)
        self._auto_stop_timer.setSingleShot(True)
        self._auto_stop_timer.timeout.connect(self._auto_stop_acquisition)
        self._auto_stop_timer.start(int(self._auto_duration_s * 1000))
        self._log(f"Auto-stop timer set: {self._auto_duration_s:.1f}s")

    # v1.9.3 新增: JSON导出强制使用英文（不随GUI语言切换）
    _EN_PROFILE_NAMES: dict[str, str] = {
        "mean_voltage": "Mean Voltage",
        "ripple_vpp": "Ripple Peak-to-Peak",
        "rms_voltage": "RMS Voltage",
        "min_voltage": "Minimum Voltage",
        "max_voltage": "Maximum Voltage",
        "mean_current": "Mean Current",
        "peak_current": "Peak Current",
        "current_ripple": "Current Ripple",
        "frequency": "Frequency",
    }

    @staticmethod
    def _en_result_text(text: str) -> str:
        """将通道结果文本转换为英文（JSON导出用）。"""
        _EN_RESULT_MAP = {
            "TBD": "TBD",
            "TBD：未填写限值": "TBD (limits not specified)",
            "TBD: 未填写限值": "TBD (limits not specified)",
            "PASS": "PASS",
            "FAIL": "FAIL",
            "无信号": "No Signal",
            "未选择": "Not Selected",
        }
        return _EN_RESULT_MAP.get(text, text)

    def _auto_append_json_sample(self, elapsed_s: float) -> None:
        """v1.9.3 新增: 将当前采集点追加到整合JSON的samples列表中（不立即写文件）。

        Args:
            elapsed_s: 已采集时长（秒）
        """
        if not self._auto_json_dir or not self.latest_measurements:
            return
        try:
            channels_data: dict[str, dict[str, Any]] = {}
            for channel, result in self.latest_measurements.items():
                measurement = result.measurement
                channels_data[channel] = {
                    "measurement_type": self._EN_PROFILE_NAMES.get(self._current_profile_key(channel), self._current_profile_key(channel)),
                    "scpi_parameter": result.request.parameter,
                    "value": measurement.value if measurement is not None else None,
                    "unit": result.request.unit,
                    "minimum_limit": self._optional_float_or_none(self.channel_min_entries[channel].text()),
                    "maximum_limit": self._optional_float_or_none(self.channel_max_entries[channel].text()),
                    "result": self._en_result_text(self.channel_status_labels[channel].text()),
                    "error": result.error,
                }
            sample = {
                "elapsed_seconds": round(elapsed_s, 3),
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "channels": channels_data,
                "power": {
                    "average_W": self.latest_average_power_w,
                    "peak_W": self.latest_peak_power_w,
                    "energy_Wh": self.latest_energy_wh,
                    "duration_s": round(self._power_duration_seconds, 3)
                    if self.latest_average_power_w is not None
                    else None,
                },
            }
            self._auto_json_samples.append(sample)
            self._log(f"Sample #{len(self._auto_json_samples)} recorded at {elapsed_s:.1f}s")
        except Exception as exc:  # noqa: BLE001
            self._log(f"WARNING: Failed to append JSON sample: {exc}")

    def _auto_save_combined_json(self) -> None:
        """v1.9.3 新增: 采集结束时导出整合的单文件JSON报告。"""
        if not self._auto_json_dir or not self._auto_json_samples or self._auto_json_saved:
            return
        try:
            import os as _os
            _os.makedirs(self._auto_json_dir, exist_ok=True)
            task_id = self._current_task_id()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"power_report_combined_{task_id}_{timestamp}.json"
            filepath = _os.path.join(self._auto_json_dir, filename)

            first_sample = self._auto_json_samples[0]
            last_sample = self._auto_json_samples[-1]
            combined = {
                "test_id": task_id,
                "device_part_no": self.device_part_entry.text().strip(),
                "firmware_version": self.firmware_entry.text().strip(),
                "test_phase": "DVT" if task_id == "POWER-06" else "EVT",
                "start_timestamp": first_sample.get("timestamp"),
                "end_timestamp": last_sample.get("timestamp"),
                "total_duration_s": round(last_sample.get("elapsed_seconds", 0), 3),
                "sample_count": len(self._auto_json_samples),
                "sample_interval_s": round(self._auto_interval_ms / 1000.0, 3),
                "workload": {"state": self.workload_combo.currentText()},
                "samples": self._auto_json_samples,
                "final_summary": {
                    "average_W": self.latest_average_power_w,
                    "peak_W": self.latest_peak_power_w,
                    "energy_Wh": self.latest_energy_wh,
                    "duration_s": round(self._power_duration_seconds, 3)
                    if self.latest_average_power_w is not None
                    else None,
                },
                "limits": {
                    "source": "SSD design spec / MRD / controller vendor spec",
                    "power_max_W": self._optional_float_or_none(self.power_limit_entry.text()),
                },
                "result": self._latest_overall,
                "auto_export": True,
                "combined_report": True,
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(combined, f, indent=2, ensure_ascii=False)
            self._auto_json_saved = True
            self._log(f"Combined JSON report saved: {filename} ({len(self._auto_json_samples)} samples)")
        except Exception as exc:  # noqa: BLE001
            self._log(f"WARNING: Failed to save combined JSON report: {exc}")

    def _auto_stop_acquisition(self) -> None:
        """v1.9.3 新增: 自动停止采集并输出结果汇总。"""
        if self.polling:
            self._log("Auto-stop: total duration reached, stopping acquisition...")
            # 停止前记录最后一个样本（确保总时长点也被记录）
            if self._auto_json_dir and self._auto_acq_start_s > 0 and not self._auto_json_saved:
                final_elapsed = monotonic() - self._auto_acq_start_s
                self._auto_append_json_sample(final_elapsed)
            self._toggle_polling()
            # v1.9.3 新增: 导出整合的单文件JSON
            self._auto_save_combined_json()
            # v1.9.3 新增: 更新状态栏为"测量已完成"
            self.statusBar().showMessage(self.tr("status.measurement_completed"))
            # v1.9.3 新增: 采集完成结果汇总
            summary_parts = ["=== Acquisition completed (auto-stop) ==="]
            summary_parts.append(f"  configured_duration={self._auto_duration_s:.1f}s")
            summary_parts.append(f"  actual_duration={self._power_duration_seconds:.1f}s")
            summary_parts.append(f"  sample_interval={self._auto_interval_ms}ms")
            if self.latest_average_power_w is not None:
                summary_parts.append(f"  average_power={self.latest_average_power_w:.4f}W")
            else:
                summary_parts.append("  average_power=N/A")
            if self.latest_peak_power_w is not None:
                summary_parts.append(f"  peak_power={self.latest_peak_power_w:.4f}W")
            else:
                summary_parts.append("  peak_power=N/A")
            if self.latest_energy_wh is not None:
                summary_parts.append(f"  total_energy={self.latest_energy_wh:.6f}Wh")
            else:
                summary_parts.append("  total_energy=N/A")
            try:
                limit = self._optional_float(self.power_limit_entry.text())
                if limit is not None and self.latest_peak_power_w is not None:
                    verdict = "PASS" if self.latest_peak_power_w <= limit else "FAIL"
                    summary_parts.append(f"  power_limit={limit:.2f}W => {verdict}")
            except (ValueError, Exception):
                pass
            summary_parts.append("  Measurement completed. JSON report auto-exported. You may close this window manually.")
            self._log("\n".join(summary_parts))

    def _build_ui(self) -> None:
        self.setWindowTitle("oscill")
        self.resize(1180, 800)
        self.setMinimumSize(980, 680)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        # Language selector row (top-right).
        lang_row = QHBoxLayout()
        lang_row.addStretch(1)
        self.language_label = QLabel()
        self.language_combo = QComboBox()
        for code in (ZH_CN, EN_US):
            self.language_combo.addItem(Translator.language_display_name(code), code)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self.language_label)
        lang_row.addWidget(self.language_combo)
        root_layout.addLayout(lang_row)

        # Connection group.
        self.connection_group = QGroupBox()
        conn_layout = QGridLayout(self.connection_group)
        self.resource_label = QLabel()
        self.resource_combo = QComboBox()
        self.resource_combo.setEditable(True)
        self.resource_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.scan_button = QPushButton()
        self.connect_button = QPushButton()
        self.disconnect_button = QPushButton()
        self.scan_button.clicked.connect(self._scan_resources)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        conn_layout.addWidget(self.resource_label, 0, 0)
        conn_layout.addWidget(self.resource_combo, 0, 1)
        conn_layout.addWidget(self.scan_button, 0, 2)
        conn_layout.addWidget(self.connect_button, 0, 3)
        conn_layout.addWidget(self.disconnect_button, 0, 4)
        self.identity_label = QLabel()
        self.identity_label.setWordWrap(True)
        conn_layout.addWidget(self.identity_label, 1, 0, 1, 5)
        conn_layout.setColumnStretch(1, 1)
        root_layout.addWidget(self.connection_group)

        # Body: left controls + right column.
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(body, 1)

        body_layout.addWidget(self._build_controls())

        right_column = QWidget()
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(right_column, 1)

        right_layout.addWidget(self._build_measurements())
        right_layout.addWidget(self._build_waveform(), 1)
        right_layout.addWidget(self._build_log())

        self.statusBar().showMessage("")

    def _build_controls(self) -> QGroupBox:
        self.controls_group = QGroupBox()
        layout = QVBoxLayout(self.controls_group)
        layout.setSpacing(6)

        self.autoscale_button = QPushButton()
        self.run_button = QPushButton()
        self.stop_button = QPushButton()
        self.autoscale_button.clicked.connect(self._autoscale)
        self.run_button.clicked.connect(self._run)
        self.stop_button.clicked.connect(self._stop)
        layout.addWidget(self.autoscale_button)
        layout.addWidget(self.run_button)
        layout.addWidget(self.stop_button)

        layout.addWidget(self._hline())
        self.enable_channels_label = QLabel()
        layout.addWidget(self.enable_channels_label)
        for channel in self.channels:
            checkbox = QCheckBox(channel)
            checkbox.setChecked(channel == "CH1")
            checkbox.stateChanged.connect(self._on_power_configuration_changed)
            self.channel_checkboxes[channel] = checkbox
            layout.addWidget(checkbox)

        self.scale_label = QLabel()
        layout.addWidget(self.scale_label)
        self.scale_entry = QLineEdit("0.5")
        layout.addWidget(self.scale_entry)
        self.apply_scale_button = QPushButton()
        self.apply_scale_button.clicked.connect(self._configure_channel)
        layout.addWidget(self.apply_scale_button)

        layout.addWidget(self._hline())
        self.power_task_label = QLabel()
        layout.addWidget(self.power_task_label)
        self.power_task_combo = QComboBox()
        for task_id in POWER_TASK_ORDER:
            self.power_task_combo.addItem(task_id, task_id)
        self.power_task_combo.currentIndexChanged.connect(self._on_power_task_changed)
        layout.addWidget(self.power_task_combo)

        self.workload_label = QLabel()
        layout.addWidget(self.workload_label)
        self.workload_combo = QComboBox()
        layout.addWidget(self.workload_combo)
        self.workload_combo.currentIndexChanged.connect(self._on_workload_changed)

        self.apply_preset_button = QPushButton()
        self.apply_preset_button.clicked.connect(self._apply_power_preset)
        layout.addWidget(self.apply_preset_button)

        self.device_part_label = QLabel("Device Part No.")
        layout.addWidget(self.device_part_label)
        self.device_part_entry = QLineEdit()
        layout.addWidget(self.device_part_entry)

        self.firmware_label = QLabel("FW Version")
        layout.addWidget(self.firmware_label)
        self.firmware_entry = QLineEdit()
        layout.addWidget(self.firmware_entry)

        self.power_limit_label = QLabel()
        layout.addWidget(self.power_limit_label)
        self.power_limit_entry = QLineEdit()
        self.power_limit_entry.editingFinished.connect(self._refresh_power_result)
        self.power_limit_entry.returnPressed.connect(self._refresh_power_result)
        layout.addWidget(self.power_limit_entry)

        self.power_summary_label = QLabel()
        self.power_summary_label.setWordWrap(True)
        layout.addWidget(self.power_summary_label)
        self.power_statistics_label = QLabel()
        self.power_statistics_label.setWordWrap(True)
        layout.addWidget(self.power_statistics_label)
        self.overall_result_label = QLabel()
        self.overall_result_label.setWordWrap(True)
        layout.addWidget(self.overall_result_label)

        self.save_result_button = QPushButton()
        self.save_result_button.clicked.connect(self._save_test_result)
        layout.addWidget(self.save_result_button)

        layout.addStretch(1)
        self.controls_group.setFixedWidth(250)
        return self.controls_group

    def _build_measurements(self) -> QGroupBox:
        self.measurements_group = QGroupBox()
        outer = QVBoxLayout(self.measurements_group)
        cards_row = QHBoxLayout()
        for channel in self.channels:
            cards_row.addWidget(self._build_measurement_card(channel))
        outer.addLayout(cards_row)
        self.poll_button = QPushButton()
        self.poll_button.clicked.connect(self._toggle_polling)
        outer.addWidget(self.poll_button)
        return self.measurements_group

    def _build_measurement_card(self, channel: str) -> QWidget:
        frame = QGroupBox(channel)
        layout = QVBoxLayout(frame)

        profile_box = QComboBox()
        for profile_key in MEASUREMENT_PROFILE_ORDER:
            profile_box.addItem(profile_key, profile_key)
        profile_box.currentIndexChanged.connect(lambda _idx, ch=channel: self._on_channel_profile_changed(ch))
        self.channel_profile_boxes[channel] = profile_box
        layout.addWidget(profile_box)

        factor_row = QWidget()
        factor_layout = QHBoxLayout(factor_row)
        factor_layout.setContentsMargins(0, 0, 0, 0)
        factor_label = QLabel(self.tr("common.av_ratio"))
        factor_entry = QLineEdit("1.0")
        factor_entry.setEnabled(False)
        factor_entry.editingFinished.connect(self._on_power_configuration_changed)
        factor_entry.returnPressed.connect(self._on_power_configuration_changed)
        self.channel_factor_entries[channel] = factor_entry
        factor_layout.addWidget(factor_label)
        factor_layout.addWidget(factor_entry)
        layout.addWidget(factor_row)

        limits_row = QWidget()
        limits_layout = QHBoxLayout(limits_row)
        limits_layout.setContentsMargins(0, 0, 0, 0)
        min_label = QLabel(self.tr("common.min"))
        min_entry = QLineEdit()
        max_label = QLabel(self.tr("common.max"))
        max_entry = QLineEdit()
        self.channel_min_entries[channel] = min_entry
        self.channel_max_entries[channel] = max_entry
        limits_layout.addWidget(min_label)
        limits_layout.addWidget(min_entry)
        limits_layout.addWidget(max_label)
        limits_layout.addWidget(max_entry)
        layout.addWidget(limits_row)

        value_label = QLabel("-- V")
        font = value_label.font()
        font.setBold(True)
        font.setPointSize(12)
        value_label.setFont(font)
        value_label.setAlignment(Qt.AlignCenter)
        self.channel_value_labels[channel] = value_label
        layout.addWidget(value_label)

        status_label = QLabel("TBD")
        status_label.setAlignment(Qt.AlignCenter)
        self.channel_status_labels[channel] = status_label
        layout.addWidget(status_label)

        self._card_frames[channel] = frame
        return frame

    def _build_waveform(self) -> QGroupBox:
        self.waveform_group = QGroupBox()
        layout = QVBoxLayout(self.waveform_group)

        self.graphics_layout = pg.GraphicsLayoutWidget()
        layout.addWidget(self.graphics_layout, 1)

        # Waveform sample-points setting row.
        points_row = QWidget()
        points_layout = QHBoxLayout(points_row)
        points_layout.setContentsMargins(0, 0, 0, 0)
        self.waveform_points_label = QLabel()
        self.waveform_points_entry = QLineEdit("2500")
        self.waveform_points_entry.setMaximumWidth(90)
        points_layout.addWidget(self.waveform_points_label)
        points_layout.addWidget(self.waveform_points_entry)
        points_layout.addStretch(1)
        layout.addWidget(points_row)

        button_row = QWidget()
        button_layout = QHBoxLayout(button_row)
        button_layout.setContentsMargins(0, 0, 0, 0)
        self.read_waveform_button = QPushButton()
        self.read_waveform_button.clicked.connect(self._read_waveform)
        self.waveform_button = QPushButton()
        self.waveform_button.clicked.connect(self._toggle_waveform_polling)
        self.save_waveform_button = QPushButton()
        self.save_waveform_button.clicked.connect(self._save_waveform)
        button_layout.addWidget(self.read_waveform_button)
        button_layout.addWidget(self.waveform_button)
        button_layout.addWidget(self.save_waveform_button)
        button_layout.addStretch(1)
        layout.addWidget(button_row)
        return self.waveform_group

    def _build_log(self) -> QGroupBox:
        self.log_group = QGroupBox()
        layout = QVBoxLayout(self.log_group)
        header_row = QHBoxLayout()
        header_row.addStretch(1)
        self.log_toggle_button = QPushButton()
        self.log_toggle_button.clicked.connect(self._toggle_log)
        header_row.addWidget(self.log_toggle_button)
        layout.addLayout(header_row)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setVisible(False)
        self.log_text.setMaximumHeight(160)
        layout.addWidget(self.log_text)
        return self.log_group

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    # ----------------------------------------------------------- i18n / text

    def tr(self, key: str, **kwargs: Any) -> str:
        """Shortcut to the translator."""
        return self.tr_obj.tr(key, **kwargs)

    def _retranslate_ui(self) -> None:
        """Refresh every static label/title after a language switch."""
        t = self.tr
        self.setWindowTitle(t("app.title"))
        self.language_label.setText(t("language.label"))

        self.connection_group.setTitle(t("connection.title"))
        self.resource_label.setText(t("connection.resource"))
        self.scan_button.setText(t("connection.scan"))
        self.connect_button.setText(t("connection.connect"))
        self.disconnect_button.setText(t("connection.disconnect"))
        if self.scope is None and not self.identity_label.text():
            self.identity_label.setText(t("connection.not_connected"))

        self.controls_group.setTitle(t("control.title"))
        self.autoscale_button.setText(t("control.autoscale"))
        self.run_button.setText(t("control.run"))
        self.stop_button.setText(t("control.stop"))
        self.enable_channels_label.setText(t("control.enable_channels"))
        self.scale_label.setText(t("control.vertical_scale"))
        self.apply_scale_button.setText(t("control.apply_scale"))
        self.power_task_label.setText(t("control.power_task"))
        self.workload_label.setText(t("control.workload"))
        self.apply_preset_button.setText(t("control.apply_preset"))
        self.device_part_label.setText(t("control.device_part"))
        self.firmware_label.setText(t("control.firmware"))
        self.power_limit_label.setText(t("control.power_limit"))
        self.save_result_button.setText(t("control.save_result"))

        self.measurements_group.setTitle(t("measurement.title"))
        self._refresh_poll_button_text()

        # Profile combo items.
        for channel in self.channels:
            box = self.channel_profile_boxes[channel]
            for index in range(box.count()):
                profile_key = box.itemData(index)
                box.setItemText(index, t(f"profile.{profile_key}"))

        # Power task combo items.
        for index in range(self.power_task_combo.count()):
            task_id = self.power_task_combo.itemData(index)
            self.power_task_combo.setItemText(index, t(f"task.{task_id}"))

        self.waveform_group.setTitle(t("waveform.title"))
        self.read_waveform_button.setText(t("waveform.read"))
        self.save_waveform_button.setText(t("waveform.save_csv"))
        self.waveform_points_label.setText(t("waveform.points_label"))
        self._refresh_waveform_button_text()

        self.log_group.setTitle(t("log.title"))
        self.log_toggle_button.setText(t("log.collapse") if self._log_expanded else t("log.expand"))

        # Re-render dynamic content so status strings switch language too.
        if self.scope is None and not self.identity_label.text():
            self.identity_label.setText(t("connection.not_connected"))
        self._reset_power_statistics()
        if self.latest_measurements:
            self._render_measurements(self.latest_measurements)
            self._render_power_summary(self.latest_measurements, None)
        else:
            for channel in self.channels:
                self._refresh_card_placeholder(channel)
        if self.last_waveforms:
            self._plot_waveforms(self.last_waveforms)
        if self.scope is None:
            self.statusBar().showMessage(t("status.not_connected"))

    def _refresh_poll_button_text(self) -> None:
        self.poll_button.setText(
            self.tr("measurement.stop") if self.polling else self.tr("measurement.start")
        )

    def _refresh_waveform_button_text(self) -> None:
        self.waveform_button.setText(
            self.tr("waveform.stop") if self.waveform_polling else self.tr("waveform.start")
        )

    def _on_language_changed(self, _index: int) -> None:
        code = self.language_combo.currentData()
        self.tr_obj.set_language(code)

    # --------------------------------------------------------------- logging

    def _log(self, message: str) -> None:
        self.log_text.append(message)

    # --------------------------------------------------------- async helpers

    def _submit(
        self,
        operation: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Future[Any]:
        """Run one serialized VISA operation without blocking Qt's event loop."""
        future = self._worker.submit(operation)
        self._watch_future(future, on_success, on_error)
        return future

    def _watch_future(
        self,
        future: Future[Any],
        on_success: Callable[[Any], None] | None,
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        def check() -> None:
            if self._closing:
                return
            if not future.done():
                QTimer.singleShot(50, check)
                return
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - surface any worker error
                if on_error is not None:
                    on_error(exc)
                else:
                    self._log(self.tr("log.background_failed", error=exc))
            else:
                if on_success is not None:
                    on_success(result)

        check()

    # ------------------------------------------------------------ connection

    def _scan_resources(self) -> None:
        self.scan_button.setEnabled(False)
        self.statusBar().showMessage(self.tr("status.scanning"))

        def complete(resources: tuple[str, ...]) -> None:
            self.scan_button.setEnabled(True)
            self.resource_combo.clear()
            self.resource_combo.addItems(resources)
            if resources and not self.resource_combo.currentText().strip():
                self.resource_combo.setCurrentIndex(0)
            self.statusBar().showMessage(self.tr("status.resources_found", count=len(resources)))
            self._log(self.tr("log.scan_done", count=len(resources)))

        def failed(exc: Exception) -> None:
            self.scan_button.setEnabled(True)
            self.statusBar().showMessage(self.tr("status.scan_failed"))
            QMessageBox.critical(self, self.tr("dialog.scan_failed"), str(exc))
            self._log(self.tr("log.scan_failed", error=exc))

        self._submit(list_visa_resources, complete, failed)

    def _connect(self) -> None:
        resource_name = self.resource_combo.currentText().strip()
        if not resource_name:
            QMessageBox.critical(self, self.tr("dialog.param_error"), self.tr("msg.need_resource"))
            return

        self._connection_generation += 1
        generation = self._connection_generation
        self._stop_periodic_operations()
        old_scope, self.scope = self.scope, None
        if old_scope is not None:
            self._submit(
                old_scope.close,
                on_error=lambda exc: self._log(self.tr("log.old_close_failed", error=exc)),
            )

        self.connect_button.setEnabled(False)
        self.identity_label.setText(self.tr("connection.connecting"))
        self.statusBar().showMessage(self.tr("status.connecting"))

        def connected(result: tuple[TektronixMSO4034, str]) -> None:
            scope, identity = result
            if generation != self._connection_generation:
                self._submit(
                    scope.close,
                    on_error=lambda exc: self._log(self.tr("log.stale_close_failed", error=exc)),
                )
                return
            self.scope = scope
            self.connect_button.setEnabled(True)
            self.identity_label.setText(identity)
            self.statusBar().showMessage(self.tr("status.connected"))
            self._log(self.tr("log.connected", identity=identity))

        def failed(exc: Exception) -> None:
            if generation != self._connection_generation:
                return
            self.connect_button.setEnabled(True)
            self.identity_label.setText(self.tr("connection.not_connected"))
            self.statusBar().showMessage(self.tr("status.connect_failed"))
            QMessageBox.critical(self, self.tr("dialog.connect_failed"), str(exc))
            self._log(self.tr("log.connect_failed", error=exc))

        self._submit(lambda: open_tektronix_scope(resource_name), connected, failed)

    def _disconnect(self) -> None:
        self._connection_generation += 1
        self._stop_periodic_operations()
        old_scope, self.scope = self.scope, None
        if old_scope is not None:
            self._submit(
                old_scope.close,
                on_success=lambda _: self._log(self.tr("log.connection_closed")),
                on_error=lambda exc: self._log(self.tr("log.disconnect_error", error=exc)),
            )
        self.connect_button.setEnabled(True)
        self.identity_label.setText(self.tr("connection.not_connected"))
        self.statusBar().showMessage(self.tr("status.not_connected"))

    def _stop_periodic_operations(self) -> None:
        was_polling = self.polling
        self.polling = False
        self.waveform_polling = False
        if was_polling:
            self._finalize_power_statistics()
        self._measurement_generation += 1
        self._waveform_generation += 1
        self._refresh_poll_button_text()
        self._refresh_waveform_button_text()

    def _require_scope(self) -> TektronixMSO4034 | Oscilloscope | None:
        if self.scope is None:
            QMessageBox.warning(
                self, self.tr("dialog.not_connected_title"), self.tr("dialog.please_connect")
            )
        return self.scope

    def _run(self) -> None:
        self._execute_scope(lambda scope: scope.run(), self.tr("log.run_started"))

    def _stop(self) -> None:
        self._execute_scope(lambda scope: scope.stop(), self.tr("log.run_stopped"))

    def _autoscale(self) -> None:
        self._execute_scope(lambda scope: scope.autoscale(), self.tr("log.autoscaled"))

    def _execute_scope(self, operation: Callable[[Oscilloscope], Any], success_message: str) -> None:
        scope = self._require_scope()
        if scope is None:
            return
        self.statusBar().showMessage(self.tr("status.communicating"))

        def complete(_: Any) -> None:
            if self.scope is scope:
                self.statusBar().showMessage(self.tr("status.connected"))
                self._log(success_message)

        def failed(exc: Exception) -> None:
            if self.scope is scope:
                self.statusBar().showMessage(self.tr("status.operation_failed"))
                QMessageBox.critical(self, self.tr("dialog.device_operation_failed"), str(exc))
                self._log(self.tr("log.device_op_failed", error=exc))

        self._submit(lambda: operation(scope), complete, failed)

    def _configure_channel(self) -> None:
        scope = self._require_scope()
        if scope is None:
            return
        try:
            selected = self._selected_channels()
            if not selected:
                raise ValueError(self.tr("msg.select_one_channel"))
            scale = float(self.scale_entry.text())
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError(self.tr("msg.scale_positive"))
        except ValueError as exc:
            QMessageBox.critical(self, self.tr("dialog.param_error"), str(exc))
            return

        def configure() -> None:
            for channel_name in selected:
                scope.configure_channel(int(channel_name[2:]), scale=scale)

        def complete(_: Any) -> None:
            if self.scope is scope:
                for channel_name in selected:
                    self.channel_checkboxes[channel_name].setChecked(True)
                self._log(
                    self.tr(
                        "log.scale_applied",
                        channels=", ".join(selected),
                        scale=scale,
                    )
                )

        def failed(exc: Exception) -> None:
            QMessageBox.critical(self, self.tr("dialog.channel_setup_failed"), str(exc))
            self._log(self.tr("log.channel_setup_failed", error=exc))

        self._submit(configure, complete, failed)

    def _selected_channels(self) -> list[str]:
        return [
            channel
            for channel in self.channels
            if self.channel_checkboxes[channel].isChecked()
        ]

    def _toggle_log(self) -> None:
        self._log_expanded = not self._log_expanded
        self.log_text.setVisible(self._log_expanded)
        self.log_toggle_button.setText(
            self.tr("log.collapse") if self._log_expanded else self.tr("log.expand")
        )

    # -------------------------------------------------------- measurement cfg

    def _current_profile_key(self, channel: str) -> str:
        return str(self.channel_profile_boxes[channel].currentData())

    def _on_channel_profile_changed(self, channel: str) -> None:
        _, unit, signal_unit = MEASUREMENT_PROFILES[self._current_profile_key(channel)]
        self.channel_factor_entries[channel].setEnabled(signal_unit == "A")
        self.channel_value_labels[channel].setText(f"-- {unit}")
        self.channel_status_labels[channel].setText(self.tr("common.tbd"))
        self._reset_power_statistics()

    def _on_power_configuration_changed(self, *_: object) -> None:
        self._reset_power_statistics()

    def _on_workload_changed(self, *_: object) -> None:
        self.power_limit_entry.setText("")
        self._reset_power_statistics()

    def _refresh_power_result(self, *_: object) -> None:
        if self.latest_measurements:
            self._render_power_summary(self.latest_measurements, None)

    def _reset_power_statistics(self) -> None:
        self.latest_average_power_w = None
        self.latest_peak_power_w = None
        self.latest_energy_wh = None
        self._power_energy_j = 0.0
        self._power_started_at = None
        self._power_last_sample_at = None
        self._power_last_average_w = None
        self._power_duration_seconds = 0.0
        self._power_statistics_signature = None
        self._last_power_error = None
        self.power_summary_label.setText(self.tr("power.waiting"))
        self.power_statistics_label.setText(self.tr("power.stats_waiting"))
        self.overall_result_label.setText(self.tr("power.verdict_need_limits"))
        self._latest_overall = "TBD"

    def _measurement_requests(self) -> dict[str, MeasurementRequest]:
        selected = self._selected_channels()
        if not selected:
            raise ValueError(self.tr("msg.select_one_channel"))
        return {channel: self._measurement_request(channel) for channel in selected}

    @staticmethod
    def _has_power_pair(requests: dict[str, MeasurementRequest]) -> bool:
        return any(request.unit == "V" for request in requests.values()) and any(
            request.unit == "A" for request in requests.values()
        )

    def _measurement_request(self, channel: str) -> MeasurementRequest:
        profile_key = self._current_profile_key(channel)
        parameter, unit, signal_unit = MEASUREMENT_PROFILES[profile_key]
        factor = 1.0
        if signal_unit == "A":
            try:
                factor = float(self.channel_factor_entries[channel].text())
            except ValueError as exc:
                raise ValueError(self.tr("msg.factor_number", channel=channel)) from exc
            if not math.isfinite(factor) or factor <= 0:
                raise ValueError(self.tr("msg.factor_positive", channel=channel))
        return MeasurementRequest(parameter, unit, factor, signal_unit)

    def _current_task_id(self) -> str:
        return str(self.power_task_combo.currentData())

    def _on_power_task_changed(self, *_: object) -> None:
        task_id = self._current_task_id()
        workloads = POWER_TASK_WORKLOADS[task_id]
        self.workload_combo.clear()
        self.workload_combo.addItems(workloads)
        self.power_limit_entry.setText("")
        self._reset_power_statistics()

    def _apply_power_preset(self) -> None:
        task_id = self._current_task_id()
        if task_id in ("POWER-05", "POWER-06"):
            self.channel_checkboxes["CH1"].setChecked(True)
            self.channel_checkboxes["CH2"].setChecked(True)
            self._set_profile("CH1", "mean_voltage")
            self._set_profile("CH2", "mean_current")
            configured = ("CH1", "CH2")
        else:
            configured = tuple(self._selected_channels()) or ("CH1",)
            profile_key = "ripple_vpp" if task_id == "POWER-04" else "mean_voltage"
            for channel in configured:
                self.channel_checkboxes[channel].setChecked(True)
                self._set_profile(channel, profile_key)
        for channel in configured:
            self._on_channel_profile_changed(channel)
        self.latest_measurements.clear()
        self._reset_power_statistics()
        self.overall_result_label.setText(self.tr("power.verdict_need_limits"))
        self._log(
            self.tr(
                "log.preset_applied",
                task=self.tr(f"task.{task_id}"),
                workload=self.workload_combo.currentText(),
            )
        )

    def _set_profile(self, channel: str, profile_key: str) -> None:
        index = self.channel_profile_boxes[channel].findData(profile_key)
        if index >= 0:
            self.channel_profile_boxes[channel].setCurrentIndex(index)

    # ----------------------------------------------------- scalar measurement

    def _toggle_polling(self) -> None:
        if self.polling:
            self._finalize_power_statistics()
            self.polling = False
            self._measurement_generation += 1
            self._refresh_poll_button_text()
            # v1.9.3 新增: 手动停止时也导出整合JSON（如果有自动采集配置且未保存）
            self._auto_save_combined_json()
        elif self._require_scope():
            try:
                requests = self._measurement_requests()
            except ValueError as exc:
                QMessageBox.critical(self, self.tr("dialog.param_error"), str(exc))
            else:
                self._reset_power_statistics()
                if self._has_power_pair(requests):
                    self._power_started_at = monotonic()
                # v1.9.3 新增: 初始化自动导出（单文件整合模式）
                self._auto_json_samples = []
                self._auto_json_saved = False
                self._auto_next_export_s = self._auto_interval_ms / 1000.0
                self._auto_acq_start_s = monotonic()
                self.polling = True
                self._measurement_generation += 1
                self._refresh_poll_button_text()
                # v1.9.3 新增: 采集开始日志
                start_parts = ["Acquisition started:"]
                current_plan = str(self.power_task_combo.currentData() or "N/A")
                start_parts.append(f"  test_plan={current_plan}")
                start_parts.append(f"  sample_interval={self._auto_interval_ms}ms")
                if self._auto_duration_s is not None:
                    start_parts.append(f"  auto_stop_after={self._auto_duration_s:.1f}s")
                else:
                    start_parts.append("  auto_stop=(disabled, manual control)")
                self._log("\n".join(start_parts))
                self._start_auto_stop_timer()
                self._poll_measurements()

    def _poll_measurements(self) -> None:
        scope = self.scope
        if not self.polling or scope is None:
            return
        if self._measurement_inflight:
            QTimer.singleShot(100, self._poll_measurements)
            return
        try:
            requests = self._measurement_requests()
        except ValueError as exc:
            self.polling = False
            self._measurement_generation += 1
            self._refresh_poll_button_text()
            QMessageBox.critical(self, self.tr("dialog.param_error"), str(exc))
            return

        generation = self._measurement_generation
        self._measurement_inflight = True

        def collect_cycle() -> tuple[
            dict[str, ChannelMeasurementResult],
            PowerWindowStatistics | None,
            str | None,
        ]:
            results = collect_configured_measurements(scope, requests)
            if not isinstance(scope, TektronixMSO4034) or not self._has_power_pair(requests):
                return results, None, None
            try:
                power_window = acquire_synchronized_power_statistics(scope, requests)
            except Exception as exc:  # noqa: BLE001 - power is best-effort
                return results, None, str(exc)
            return results, power_window, None

        def complete(
            payload: tuple[
                dict[str, ChannelMeasurementResult],
                PowerWindowStatistics | None,
                str | None,
            ],
        ) -> None:
            self._measurement_inflight = False
            if not self.polling or generation != self._measurement_generation or self.scope is not scope:
                return
            results, power_window, power_error = payload
            self.latest_measurements = results
            self._render_measurements(results)
            if power_error is not None and power_error != self._last_power_error:
                self._log(self.tr("log.power_unavailable", error=power_error))
            self._last_power_error = power_error
            self._render_power_summary(results, power_window)
            self.statusBar().showMessage(self.tr("status.measuring"))
            # v1.9.3 新增: 按间隔记录采集样本（整合到单文件JSON）
            if self._auto_json_dir and self._auto_acq_start_s > 0:
                elapsed = monotonic() - self._auto_acq_start_s
                while elapsed >= self._auto_next_export_s and self._auto_next_export_s > 0:
                    self._auto_append_json_sample(self._auto_next_export_s)
                    self._auto_next_export_s += self._auto_interval_ms / 1000.0
                    if self._auto_duration_s is not None and self._auto_next_export_s > self._auto_duration_s:
                        break
            QTimer.singleShot(self._auto_interval_ms, self._poll_measurements)

        def failed(exc: Exception) -> None:
            self._measurement_inflight = False
            if generation != self._measurement_generation:
                return
            self.polling = False
            self._measurement_generation += 1
            self._refresh_poll_button_text()
            self.statusBar().showMessage(self.tr("status.measurement_failed"))
            self._log(self.tr("log.measurement_failed", error=exc))

        self._submit(collect_cycle, complete, failed)

    def _render_measurements(self, results: dict[str, ChannelMeasurementResult]) -> None:
        for channel, result in results.items():
            measurement = result.measurement
            if measurement is None:
                self.channel_value_labels[channel].setText(f"-- {result.request.unit}")
                self.channel_status_labels[channel].setText(self.tr("result.no_signal"))
                if result.error and self._last_channel_errors.get(channel) != result.error:
                    self._log(self.tr("log.channel_unavailable", channel=channel))
                    self._last_channel_errors[channel] = result.error
                continue
            self._last_channel_errors.pop(channel, None)
            scale, display_unit = self._engineering_scale(abs(measurement.value), measurement.unit or "")
            self.channel_value_labels[channel].setText(f"{measurement.value * scale:.6g} {display_unit}")
            self.channel_status_labels[channel].setText(
                self._evaluate_channel_result(channel, measurement.value)
            )
        for channel in self.channels:
            if channel not in results:
                self._refresh_card_placeholder(channel)

    def _refresh_card_placeholder(self, channel: str) -> None:
        profile_key = self._current_profile_key(channel)
        _, unit, _ = MEASUREMENT_PROFILES[profile_key]
        self.channel_value_labels[channel].setText(f"-- {unit}")
        self.channel_status_labels[channel].setText(self.tr("result.not_selected"))

    def _evaluate_channel_result(self, channel: str, value: float) -> str:
        try:
            minimum = self._optional_float(self.channel_min_entries[channel].text())
            maximum = self._optional_float(self.channel_max_entries[channel].text())
        except ValueError:
            return self.tr("result.limit_format_error")
        if minimum is not None and maximum is not None and minimum > maximum:
            return self.tr("result.min_gt_max")
        if minimum is None and maximum is None:
            return self.tr("result.no_limits")
        if minimum is not None and value < minimum:
            return self.tr("result.below_min")
        if maximum is not None and value > maximum:
            return self.tr("result.above_max")
        return self.tr("common.pass")

    @staticmethod
    def _optional_float(text: str) -> float | None:
        if not text.strip():
            return None
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("limit must be finite")
        return value

    # ------------------------------------------------------------- power stats

    def _render_power_summary(
        self,
        results: dict[str, ChannelMeasurementResult],
        power_window: PowerWindowStatistics | None,
    ) -> None:
        if power_window is not None:
            self._accumulate_power_window(power_window)
            self.power_summary_label.setText(
                self.tr(
                    "power.window",
                    avg=power_window.average_power_w,
                    peak=power_window.peak_power_w,
                    voltage=power_window.voltage_source,
                    current=power_window.current_source,
                )
            )
        elif not self._has_power_pair(
            {channel: result.request for channel, result in results.items()}
        ):
            self.power_summary_label.setText(self.tr("power.need_pair"))
            self.power_statistics_label.setText(self.tr("power.stats_need_pair"))

        statuses = [self.channel_status_labels[channel].text() for channel in results]
        task_id = self._current_task_id()
        power_required = task_id in ("POWER-05", "POWER-06")
        power_status = self._evaluate_power_result()
        include_power = power_required or self.latest_average_power_w is not None
        if include_power:
            statuses.append(power_status)
        pass_text = self.tr("common.pass")
        if any(status.startswith("FAIL") for status in statuses):
            overall = "FAIL"
        elif statuses and all(status == pass_text for status in statuses):
            overall = "PASS"
        else:
            overall = "TBD"
        self._latest_overall = overall
        power_text = f" | {power_status}" if include_power else ""
        self.overall_result_label.setText(
            f"{self.tr('result.verdict_prefix')}{overall}{power_text} | "
            f"{self.tr(f'task.{task_id}')} | {self.workload_combo.currentText()}"
        )

    def _accumulate_power_window(self, power_window: PowerWindowStatistics) -> None:
        signature = (
            power_window.voltage_source,
            power_window.current_source,
            power_window.current_scale_factor,
        )
        if self._power_statistics_signature != signature:
            if self._power_statistics_signature is not None:
                self._reset_power_statistics()
            self._power_statistics_signature = signature
        captured_at = monotonic()
        if self._power_started_at is None:
            self._power_started_at = captured_at
        if self._power_last_sample_at is None:
            interval = max(captured_at - self._power_started_at, 0.0)
            self._power_energy_j += power_window.average_power_w * interval
        else:
            interval = max(captured_at - self._power_last_sample_at, 0.0)
            previous_power = self._power_last_average_w
            if previous_power is None:
                previous_power = power_window.average_power_w
            self._power_energy_j += (previous_power + power_window.average_power_w) * 0.5 * interval
        self._power_last_sample_at = captured_at
        self._power_last_average_w = power_window.average_power_w
        self._power_duration_seconds = max(captured_at - self._power_started_at, 0.0)
        self.latest_average_power_w = (
            self._power_energy_j / self._power_duration_seconds
            if self._power_duration_seconds > 0
            else power_window.average_power_w
        )
        self.latest_peak_power_w = max(self.latest_peak_power_w or 0.0, power_window.peak_power_w)
        self.latest_energy_wh = self._power_energy_j / 3600.0
        self._show_power_statistics()

    def _extend_power_energy_to(self, captured_at: float) -> None:
        if self._power_started_at is None or self._power_last_sample_at is None or self._power_last_average_w is None:
            return
        interval = max(captured_at - self._power_last_sample_at, 0.0)
        self._power_energy_j += self._power_last_average_w * interval
        self._power_last_sample_at = captured_at
        self._power_duration_seconds = max(captured_at - self._power_started_at, 0.0)
        if self._power_duration_seconds > 0:
            self.latest_average_power_w = self._power_energy_j / self._power_duration_seconds
        self.latest_energy_wh = self._power_energy_j / 3600.0

    def _finalize_power_statistics(self) -> None:
        self._extend_power_energy_to(monotonic())
        self._show_power_statistics()

    def _show_power_statistics(self) -> None:
        if self.latest_average_power_w is None or self.latest_peak_power_w is None or self.latest_energy_wh is None:
            return
        energy_scale, energy_unit = self._engineering_scale(abs(self.latest_energy_wh), "Wh")
        self.power_statistics_label.setText(
            self.tr(
                "power.stats",
                duration=self._power_duration_seconds,
                avg=self.latest_average_power_w,
                peak=self.latest_peak_power_w,
                energy=self.latest_energy_wh * energy_scale,
                unit=energy_unit,
            )
        )

    def _evaluate_power_result(self) -> str:
        if self.latest_peak_power_w is None:
            return self.tr("power.evaluate_waiting")
        try:
            maximum = self._optional_float(self.power_limit_entry.text())
        except ValueError:
            return self.tr("power.limit_format_error")
        if maximum is None:
            return self.tr("power.limit_missing")
        if maximum <= 0:
            return self.tr("power.limit_must_positive")
        if self.latest_peak_power_w > maximum:
            return self.tr("common.fail")
        return self.tr("common.pass")

    # -------------------------------------------------------------- waveforms

    def _read_waveform_points(self) -> int | None:
        """Parse the user-entered sample-point count; show an error and return None on failure."""
        try:
            points = int(self.waveform_points_entry.text().strip())
        except ValueError:
            QMessageBox.critical(self, self.tr("dialog.param_error"), self.tr("msg.points_invalid"))
            return None
        if not 2 <= points <= 1_000_000:
            QMessageBox.critical(self, self.tr("dialog.param_error"), self.tr("msg.points_invalid"))
            return None
        return points

    def _read_waveform(self) -> None:
        scope = self._require_scope()
        if not isinstance(scope, TektronixMSO4034):
            return
        try:
            requests = self._measurement_requests()
            selected = list(requests)
        except ValueError as exc:
            QMessageBox.critical(self, self.tr("dialog.param_error"), str(exc))
            return
        points = self._read_waveform_points()
        if points is None:
            return

        self.statusBar().showMessage(self.tr("status.reading_waveform"))

        def complete(waveforms: dict[str, Waveform]) -> None:
            if self.scope is not scope:
                return
            self.last_waveforms = waveforms
            self._plot_waveforms(waveforms)
            self.statusBar().showMessage(self.tr("status.waveform_read", count=len(selected)))
            self._log(self.tr("log.waveforms_read", channels=", ".join(selected)))

        def failed(exc: Exception) -> None:
            if self.scope is scope:
                self.statusBar().showMessage(self.tr("status.waveform_failed"))
                QMessageBox.critical(self, self.tr("dialog.waveform_failed"), str(exc))
                self._log(self.tr("log.waveform_failed", error=exc))

        self._submit(lambda: acquire_configured_waveforms(scope, requests, points=points), complete, failed)

    def _ensure_plots(self, selected: list[str]) -> None:
        if self._plot_channels == selected:
            return
        self.graphics_layout.clear()
        self._plots = {}
        self._curves = {}
        first_plot: pg.PlotItem | None = None
        for index, channel in enumerate(selected):
            plot: pg.PlotItem = self.graphics_layout.addPlot(row=index, col=0)
            if first_plot is None:
                first_plot = plot
            else:
                plot.setXLink(first_plot)
            if index < len(selected) - 1:
                plot.hideAxis("bottom")
            plot.showGrid(x=True, y=True, alpha=0.3)
            color = self._CHANNEL_COLORS.get(channel, "#1565C0")
            curve = plot.plot([], [], pen=pg.mkPen(color=color, width=1.5))
            zero_line = pg.InfiniteLine(
                pos=0,
                angle=0,
                pen=pg.mkPen(color="#607D8B", width=0.7, style=Qt.DashLine),
            )
            plot.addItem(zero_line)
            self._plots[channel] = plot
            self._curves[channel] = curve
        self._plot_channels = list(selected)

    def _plot_waveforms(self, waveforms: dict[str, Waveform]) -> None:
        selected = list(waveforms)
        if not selected:
            return
        self._ensure_plots(selected)

        maximum_time = max(abs(time) for waveform in waveforms.values() for time in waveform.times)
        time_scale, time_unit = self._engineering_scale(maximum_time, "s")
        last_channel = selected[-1]

        for channel, plot in self._plots.items():
            waveform = waveforms[channel]
            if waveform.unit == "A":
                quantity = self.tr("waveform.current")
            elif waveform.unit == "V":
                quantity = self.tr("waveform.voltage")
            else:
                quantity = self.tr("waveform.amplitude")
            maximum_value = max((abs(value) for value in waveform.values), default=0.0)
            value_scale, value_unit = self._engineering_scale(maximum_value, waveform.unit)
            display_times = [time * time_scale for time in waveform.times]
            display_values = [value * value_scale for value in waveform.values]
            peak_to_peak = (max(display_values) - min(display_values)) if display_values else 0.0

            self._curves[channel].setData(display_times, display_values)
            plot.setLabel("left", f"{quantity} ({value_unit})")
            color = self._CHANNEL_COLORS.get(channel, "#1565C0")
            stats = self.tr(
                "waveform.pkpk_samples",
                pkpk=peak_to_peak,
                unit=value_unit,
                samples=len(display_values),
            )
            plot.setTitle(
                f"<span style='color:{color};font-weight:bold'>{channel}</span> "
                f"- {quantity}   <span style='font-size:9pt;color:#37474F'>{stats}</span>"
            )
            plot.enableAutoRange()

        self._plots[last_channel].setLabel("bottom", f"{self.tr('waveform.time')} ({time_unit})")

    @classmethod
    def _engineering_scale(cls, magnitude: float, base_unit: str) -> tuple[float, str]:
        """Choose an SI-prefixed display unit that avoids axis offset notation."""
        if not math.isfinite(magnitude) or magnitude <= 0:
            return 1.0, base_unit
        exponent = math.floor(math.log10(magnitude) / 3) * 3
        exponent = max(min(exponent, max(cls._ENGINEERING_PREFIXES)), min(cls._ENGINEERING_PREFIXES))
        return 10.0 ** (-exponent), f"{cls._ENGINEERING_PREFIXES[exponent]}{base_unit}"

    def _toggle_waveform_polling(self) -> None:
        if self.waveform_polling:
            self.waveform_polling = False
            self._waveform_generation += 1
            self._refresh_waveform_button_text()
        elif self._require_scope():
            try:
                self._measurement_requests()
            except ValueError as exc:
                QMessageBox.critical(self, self.tr("dialog.param_error"), str(exc))
            else:
                self.waveform_polling = True
                self._waveform_generation += 1
                self._refresh_waveform_button_text()
                self._poll_waveforms()

    def _poll_waveforms(self) -> None:
        scope = self.scope
        if not self.waveform_polling or not isinstance(scope, TektronixMSO4034):
            return
        if self._waveform_inflight:
            QTimer.singleShot(100, self._poll_waveforms)
            return
        try:
            requests = self._measurement_requests()
            selected = list(requests)
        except ValueError as exc:
            self.waveform_polling = False
            self._waveform_generation += 1
            self._refresh_waveform_button_text()
            self._log(self.tr("log.realtime_failed", error=exc))
            QMessageBox.critical(self, self.tr("dialog.param_error"), str(exc))
            return
        points = self._read_waveform_points()
        if points is None:
            self.waveform_polling = False
            self._waveform_generation += 1
            self._refresh_waveform_button_text()
            return

        generation = self._waveform_generation
        self._waveform_inflight = True

        def complete(waveforms: dict[str, Waveform]) -> None:
            self._waveform_inflight = False
            if not self.waveform_polling or generation != self._waveform_generation or self.scope is not scope:
                return
            self.last_waveforms = waveforms
            self._plot_waveforms(waveforms)
            self.statusBar().showMessage(
                self.tr("status.realtime_waveform", channels=", ".join(selected))
            )
            QTimer.singleShot(500, self._poll_waveforms)

        def failed(exc: Exception) -> None:
            self._waveform_inflight = False
            if generation != self._waveform_generation:
                return
            self.waveform_polling = False
            self._waveform_generation += 1
            self._refresh_waveform_button_text()
            self.statusBar().showMessage(self.tr("status.realtime_failed"))
            self._log(self.tr("log.realtime_failed", error=exc))

        self._submit(lambda: acquire_configured_waveforms(scope, requests, points=points), complete, failed)

    def _save_waveform(self) -> None:
        if not self.last_waveforms:
            QMessageBox.information(
                self, self.tr("dialog.no_waveform_title"), self.tr("dialog.no_waveform_msg")
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("dialog.save_waveform_title"),
            "",
            f"{self.tr('dialog.csv_files')} (*.csv);;{self.tr('dialog.all_files')} (*.*)",
        )
        if path:
            try:
                save_configured_waveforms_csv(path, self.last_waveforms)
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, self.tr("dialog.save_failed"), str(exc))
                self._log(self.tr("log.waveform_save_failed", error=exc))
            else:
                self._log(self.tr("log.waveform_saved", path=path))

    # --------------------------------------------------------- JSON test result

    def _save_test_result(self) -> None:
        if not self.latest_measurements and not self._auto_json_samples:
            QMessageBox.information(
                self,
                self.tr("dialog.no_measurement_title"),
                self.tr("dialog.no_measurement_msg"),
            )
            return
        # v1.9.3 改进: 如果正在采集，先停止采集以确保数据完整
        if self.polling:
            self._extend_power_energy_to(monotonic())
            self._show_power_statistics()
            self._toggle_polling()
        saved_at = datetime.now().astimezone()
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("dialog.save_result_title"),
            self._suggested_result_filename(saved_at),
            f"{self.tr('dialog.json_files')} (*.json);;{self.tr('dialog.all_files')} (*.*)",
        )
        if not path:
            return

        task_id = self._current_task_id()

        # v1.9.3 改进: 如果有多次采集样本(_auto_json_samples)，导出整合格式JSON
        if self._auto_json_samples:
            first_sample = self._auto_json_samples[0]
            last_sample = self._auto_json_samples[-1]
            combined = {
                "test_id": task_id,
                "device_part_no": self.device_part_entry.text().strip(),
                "firmware_version": self.firmware_entry.text().strip(),
                "test_phase": "DVT" if task_id == "POWER-06" else "EVT",
                "start_timestamp": first_sample.get("timestamp"),
                "end_timestamp": last_sample.get("timestamp"),
                "total_duration_s": round(last_sample.get("elapsed_seconds", 0), 3),
                "sample_count": len(self._auto_json_samples),
                "sample_interval_s": round(self._auto_interval_ms / 1000.0, 3),
                "workload": {"state": self.workload_combo.currentText()},
                "samples": self._auto_json_samples,
                "final_summary": {
                    "average_W": self.latest_average_power_w,
                    "peak_W": self.latest_peak_power_w,
                    "energy_Wh": self.latest_energy_wh,
                    "duration_s": round(self._power_duration_seconds, 3)
                    if self.latest_average_power_w is not None
                    else None,
                },
                "limits": {
                    "source": "SSD design spec / MRD / controller vendor spec",
                    "power_max_W": self._optional_float_or_none(self.power_limit_entry.text()),
                },
                "result": self._latest_overall,
                "auto_export": False,
                "combined_report": True,
                "export_mode": "manual",
            }
            try:
                with open(path, "w", encoding="utf-8") as output:
                    json.dump(combined, output, ensure_ascii=False, indent=2)
            except OSError as exc:
                QMessageBox.critical(self, self.tr("dialog.save_failed"), str(exc))
                self._log(self.tr("log.result_save_failed", error=exc))
            else:
                self._log(f"Manual export: combined JSON report saved ({len(self._auto_json_samples)} samples): {path}")
            return

        # 单次测量格式（无多次采集样本时）
        channels: dict[str, dict[str, Any]] = {}
        for channel, result in self.latest_measurements.items():
            measurement = result.measurement
            channels[channel] = {
                "measurement_type": self._EN_PROFILE_NAMES.get(self._current_profile_key(channel), self._current_profile_key(channel)),
                "scpi_parameter": result.request.parameter,
                "value": measurement.value if measurement is not None else None,
                "unit": result.request.unit,
                "minimum_limit": self._optional_float_or_none(self.channel_min_entries[channel].text()),
                "maximum_limit": self._optional_float_or_none(self.channel_max_entries[channel].text()),
                "result": self._en_result_text(self.channel_status_labels[channel].text()),
                "error": result.error,
            }
        record = {
            "test_id": task_id,
            "device_part_no": self.device_part_entry.text().strip(),
            "firmware_version": self.firmware_entry.text().strip(),
            "test_phase": "DVT" if task_id == "POWER-06" else "EVT",
            "timestamp": saved_at.isoformat(timespec="seconds"),
            "workload": {"state": self.workload_combo.currentText()},
            "measurements": {
                "channels": channels,
                "power": {
                    "average_W": self.latest_average_power_w,
                    "peak_W": self.latest_peak_power_w,
                    "energy_Wh": self.latest_energy_wh,
                    "duration_s": round(self._power_duration_seconds, 3)
                    if self.latest_average_power_w is not None
                    else None,
                },
            },
            "limits": {
                "source": "SSD design spec / MRD / controller vendor spec",
                "power_max_W": self._optional_float_or_none(self.power_limit_entry.text()),
            },
            "events": {
                "current_surge": None,
                "reset": None,
                "disk_drop": None,
                "io_failure": None,
                "abnormal_spike": None,
            },
            "result": self._latest_overall,
            "notes": (
                "Energy is time-integrated from synchronized power windows. Power statistics retain only "
                "duration, average, peak and energy; raw power samples are not stored. "
                "Numeric limits are user-supplied; unspecified requirements remain TBD."
            ),
        }
        try:
            with open(path, "w", encoding="utf-8") as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
        except OSError as exc:
            QMessageBox.critical(self, self.tr("dialog.save_failed"), str(exc))
            self._log(self.tr("log.result_save_failed", error=exc))
        else:
            self._log(self.tr("log.result_saved", path=path))

    def _suggested_result_filename(self, saved_at: datetime) -> str:
        task_id = self._current_task_id()
        parts = (
            task_id,
            self.device_part_entry.text().strip(),
            self.firmware_entry.text().strip(),
            self.workload_combo.currentText().strip(),
            saved_at.strftime("%Y%m%d_%H%M%S"),
        )
        safe_parts = [self._safe_filename_part(part) for part in parts if part]
        return "_".join(safe_parts) + ".json"

    @staticmethod
    def _safe_filename_part(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*]+', "-", value.strip())
        value = re.sub(r"\s+", "_", value)
        return value.strip(" ._-") or "NA"

    @staticmethod
    def _optional_float_or_none(text: str) -> float | None:
        try:
            return OscilloscopeApp._optional_float(text)
        except ValueError:
            return None

    # ----------------------------------------------------------------- closing

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._closing = True
        self.polling = False
        self.waveform_polling = False
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.stop()
        self._connection_generation += 1
        scope, self.scope = self.scope, None
        self._worker.close(scope.close if scope is not None else None)
        event.accept()


def main() -> None:
    # Import QApplication lazily so the module can be imported (for static
    # checks and tests) without a running display.
    import os as _os
    from PyQt5.QtWidgets import QApplication

    app = QApplication([])
    window = OscilloscopeApp()

    # v1.9.3 新增: 从环境变量读取自动采集参数（由 ssd_test 传递）
    auto_plan = _os.environ.get("OSCILL_AUTO_PLAN", "").strip()
    auto_duration_str = _os.environ.get("OSCILL_AUTO_DURATION", "").strip()
    auto_interval_str = _os.environ.get("OSCILL_AUTO_INTERVAL", "").strip()
    auto_duration = None
    if auto_duration_str:
        try:
            auto_duration = float(auto_duration_str)
        except ValueError:
            auto_duration = None
    auto_interval_ms = 1000
    if auto_interval_str:
        try:
            auto_interval_ms = int(float(auto_interval_str) * 1000)
        except ValueError:
            pass
    auto_channels_str = _os.environ.get("OSCILL_AUTO_CHANNELS", "").strip()
    auto_json_dir = _os.environ.get("OSCILL_AUTO_JSON_DIR", "").strip()
    if auto_plan or auto_duration is not None or auto_interval_str or auto_channels_str or auto_json_dir:
        window.set_auto_acquisition_params(
            plan=auto_plan,
            duration_s=auto_duration,
            interval_ms=auto_interval_ms,
            channels=auto_channels_str,
            json_dir=auto_json_dir,
        )
        window._apply_auto_plan()
        window._apply_auto_channels()
        # v1.9.3 新增: 启动时输出自动采集模式提示
        window._log("oscill launched in auto-acquisition mode by ssd_test.")
        window._log("Click 'Start Measurement' to begin; acquisition will stop automatically if duration is set.")
        if auto_json_dir:
            window._log(f"JSON reports will be auto-exported to: {auto_json_dir}")

    # Populate the workload combo to match the default task.
    window._on_power_task_changed()
    window.identity_label.setText(window.tr("connection.not_connected"))
    window.statusBar().showMessage(window.tr("status.not_connected"))
    window.show()
    app.exec_()


if __name__ == "__main__":
    main()
