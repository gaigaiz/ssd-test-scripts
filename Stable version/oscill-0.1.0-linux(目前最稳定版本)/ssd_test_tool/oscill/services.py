"""Application services shared by the GUI and non-interactive clients."""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import PathLike

from .controller import InvalidMeasurementError, Measurement, Oscilloscope, TektronixMSO4034, Waveform
from .transport import VisaTransport


@dataclass(frozen=True)
class MeasurementRequest:
    """One channel's independently configured scalar measurement."""

    parameter: str
    unit: str
    scale_factor: float = 1.0
    signal_unit: str = "V"


@dataclass(frozen=True)
class ChannelMeasurementResult:
    """A per-channel result that can represent an unavailable measurement."""

    request: MeasurementRequest
    measurement: Measurement | None
    error: str | None = None


@dataclass(frozen=True)
class PowerWindowStatistics:
    """Compact power statistics calculated from one synchronized V/I capture."""

    voltage_source: str
    current_source: str
    sample_count: int
    average_power_w: float
    peak_power_w: float
    current_scale_factor: float


def open_tektronix_scope(resource_name: str) -> tuple[TektronixMSO4034, str]:
    """Open and identify a scope, closing it if identification fails."""
    scope = TektronixMSO4034(VisaTransport(resource_name))
    try:
        return scope, scope.identify()
    except Exception:
        try:
            scope.close()
        finally:
            raise


def collect_measurements(
    scope: Oscilloscope,
    sources: Sequence[str],
    factor: float,
    unit: str,
) -> dict[str, tuple[Measurement, Measurement]]:
    """Collect peak-to-peak and frequency measurements for selected sources."""
    results: dict[str, tuple[Measurement, Measurement]] = {}
    for source in sources:
        vpp = scope.measure_vpp(source=source)
        if unit == "A":
            vpp = vpp.scaled(factor, unit)
        frequency = scope.measure_frequency(source=source)
        results[source] = (vpp, frequency)
    return results


def collect_configured_measurements(
    scope: Oscilloscope,
    requests: Mapping[str, MeasurementRequest],
) -> dict[str, ChannelMeasurementResult]:
    """Measure channels independently so one invalid signal does not stop the batch."""
    results: dict[str, ChannelMeasurementResult] = {}
    for source, request in requests.items():
        if isinstance(scope, TektronixMSO4034):
            scope.set_channel_enabled(int(source[2:]))
        try:
            measurement = scope.measure(request.parameter, source=source, unit=request.unit)
            if request.scale_factor != 1.0:
                measurement = measurement.scaled(request.scale_factor, request.unit)
        except InvalidMeasurementError as exc:
            results[source] = ChannelMeasurementResult(request, None, str(exc))
        else:
            results[source] = ChannelMeasurementResult(request, measurement)
    return results


def acquire_waveforms(
    scope: TektronixMSO4034,
    sources: Sequence[str],
    factor: float,
    unit: str,
    *,
    points: int = 2500,
) -> dict[str, Waveform]:
    """Acquire selected sources serially and apply an explicit unit conversion."""
    waveforms: dict[str, Waveform] = {}
    for source in sources:
        waveform = scope.read_waveform(source=source, points=points, unit="V")
        waveforms[source] = waveform if unit == "V" else waveform.scaled(factor, unit)
    return waveforms


def acquire_configured_waveforms(
    scope: TektronixMSO4034,
    requests: Mapping[str, MeasurementRequest],
    *,
    points: int = 2500,
) -> dict[str, Waveform]:
    """Acquire channels using each channel's voltage/current conversion."""
    waveforms: dict[str, Waveform] = {}
    for source, request in requests.items():
        scope.set_channel_enabled(int(source[2:]))
        waveform = scope.read_waveform(source=source, points=points, unit="V")
        if request.signal_unit == "A":
            waveform = waveform.scaled(request.scale_factor, "A")
        waveforms[source] = waveform
    return waveforms


def acquire_synchronized_power_statistics(
    scope: TektronixMSO4034,
    requests: Mapping[str, MeasurementRequest],
    *,
    points: int = 2500,
) -> PowerWindowStatistics:
    """Freeze one acquisition and calculate power without retaining power samples."""
    voltage = next(
        ((source, request) for source, request in requests.items() if request.unit == "V"),
        None,
    )
    current = next(
        ((source, request) for source, request in requests.items() if request.signal_unit == "A"),
        None,
    )
    if voltage is None or current is None:
        raise ValueError("synchronized power requires one voltage channel and one current channel")

    voltage_source, _ = voltage
    current_source, current_request = current
    scope.set_channel_enabled(int(voltage_source[2:]))
    scope.set_channel_enabled(int(current_source[2:]))
    scope.stop()
    try:
        voltage_waveform = scope.read_waveform(source=voltage_source, points=points, unit="V")
        current_waveform = scope.read_waveform(source=current_source, points=points, unit="V").scaled(
            current_request.scale_factor, "A"
        )
    finally:
        scope.run()

    if len(voltage_waveform.values) != len(current_waveform.values):
        raise RuntimeError("voltage and current waveforms have different sample counts")
    if not voltage_waveform.values:
        raise RuntimeError("synchronized power capture returned no samples")
    if not _time_axes_match(voltage_waveform.times, current_waveform.times):
        raise RuntimeError("voltage and current waveforms do not share the same time axis")

    powers = (
        abs(voltage_value * current_value)
        for voltage_value, current_value in zip(
            voltage_waveform.values,
            current_waveform.values,
            strict=True,
        )
    )
    total_power = 0.0
    peak_power = 0.0
    sample_count = 0
    for power in powers:
        total_power += power
        peak_power = max(peak_power, power)
        sample_count += 1
    return PowerWindowStatistics(
        voltage_source=voltage_source,
        current_source=current_source,
        sample_count=sample_count,
        average_power_w=total_power / sample_count,
        peak_power_w=peak_power,
        current_scale_factor=current_request.scale_factor,
    )


def _time_axes_match(first: Sequence[float], second: Sequence[float]) -> bool:
    if len(first) != len(second):
        return False
    if not first:
        return True
    tolerance = max(abs(first[-1] - first[0]), 1.0) * 1e-9
    return math.isclose(first[0], second[0], abs_tol=tolerance) and math.isclose(
        first[-1], second[-1], abs_tol=tolerance
    )


def save_waveforms_csv(path: str | PathLike[str], waveforms: Mapping[str, Waveform]) -> None:
    """Save a same-unit collection of channel waveforms in long CSV format."""
    if not waveforms:
        raise ValueError("at least one waveform is required")
    units = {waveform.unit for waveform in waveforms.values()}
    if len(units) != 1:
        raise ValueError("all waveforms must use the same unit")
    unit = units.pop()
    with open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("channel", "time_s", f"value_{unit.lower()}"))
        for channel, waveform in waveforms.items():
            writer.writerows(
                (channel, time, value) for time, value in zip(waveform.times, waveform.values, strict=True)
            )


def save_configured_waveforms_csv(path: str | PathLike[str], waveforms: Mapping[str, Waveform]) -> None:
    """Save mixed voltage/current waveforms with an explicit unit column."""
    if not waveforms:
        raise ValueError("at least one waveform is required")
    with open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("channel", "time_s", "value", "unit"))
        for channel, waveform in waveforms.items():
            writer.writerows(
                (channel, time, value, waveform.unit)
                for time, value in zip(waveform.times, waveform.values, strict=True)
            )
