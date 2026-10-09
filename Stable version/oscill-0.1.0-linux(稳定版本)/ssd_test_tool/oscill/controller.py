"""High-level oscilloscope operations using common SCPI commands."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from typing import Any

from .transport import Transport, TransportError

_SCPI_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


class InvalidMeasurementError(RuntimeError):
    """The instrument could not calculate a requested measurement."""


def _parse_measurement_value(raw_value: str, *, command: str) -> float:
    """Parse a finite instrument value and reject Tektronix invalid sentinels."""
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise InvalidMeasurementError(f"instrument returned a non-numeric value for {command}: {raw_value!r}") from exc
    if not math.isfinite(value) or abs(value) >= 9.0e37:
        raise InvalidMeasurementError(f"instrument returned an invalid value for {command}: {raw_value!r}")
    return value


@dataclass(frozen=True)
class Measurement:
    """A scalar measurement returned by the instrument."""

    source: str
    parameter: str
    value: float
    unit: str | None = None

    def scaled(self, factor: float, unit: str) -> Measurement:
        """Return a converted measurement without changing the original."""
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("conversion factor must be a finite number greater than zero")
        return Measurement(self.source, self.parameter, self.value * factor, unit)


@dataclass(frozen=True)
class Waveform:
    """A waveform converted to engineering units."""

    source: str
    times: tuple[float, ...]
    values: tuple[float, ...]
    unit: str = "V"

    @property
    def voltages(self) -> tuple[float, ...]:
        """Backward-compatible alias for callers created before generic units."""
        return self.values

    def scaled(self, factor: float, unit: str) -> Waveform:
        """Return a waveform converted by an explicit engineering factor."""
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("conversion factor must be a finite number greater than zero")
        return Waveform(self.source, self.times, tuple(value * factor for value in self.values), unit)

    def save_csv(self, path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(("time_s", f"value_{self.unit.lower()}"))
            writer.writerows(zip(self.times, self.values, strict=True))


class Oscilloscope:
    """Control an oscilloscope through a VISA-compatible transport."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def __enter__(self) -> Oscilloscope:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._transport.close()

    def identify(self) -> str:
        return self._transport.query("*IDN?")

    def reset(self) -> None:
        self._transport.write("*RST")
        self._transport.query("*OPC?")

    def run(self) -> None:
        self._transport.write("RUN")

    def stop(self) -> None:
        self._transport.write("STOP")

    def autoscale(self) -> None:
        self._transport.write("AUToscale")

    def configure_channel(self, channel: int, *, scale: float | None = None, position: float | None = None) -> None:
        if channel < 1:
            raise ValueError("channel must be a positive integer")
        prefix = f"CHANnel{channel}"
        if scale is not None:
            if scale <= 0:
                raise ValueError("scale must be greater than zero")
            self._transport.write(f"{prefix}:SCALe {scale}")
        if position is not None:
            self._transport.write(f"{prefix}:POSition {position}")

    def measure(self, parameter: str, *, source: str = "CHANnel1", unit: str | None = None) -> Measurement:
        parameter = parameter.strip()
        source = source.strip()
        if not _SCPI_TOKEN.fullmatch(parameter):
            raise ValueError("parameter must be a single SCPI token")
        if not _SCPI_TOKEN.fullmatch(source):
            raise ValueError("source must be a single SCPI token")
        command = f"MEASure:{parameter}? {source}"
        raw_value = self._transport.query(command)
        value = _parse_measurement_value(raw_value, command=command)
        return Measurement(source=source, parameter=parameter, value=value, unit=unit)

    def measure_frequency(self, *, source: str = "CHANnel1") -> Measurement:
        return self.measure("FREQuency", source=source, unit="Hz")

    def measure_vpp(self, *, source: str = "CHANnel1") -> Measurement:
        return self.measure("VPP", source=source, unit="V")

    def measure_peak_to_peak(self, *, source: str = "CHANnel1", unit: str = "V") -> Measurement:
        return self.measure("VPP", source=source, unit=unit)

    def save_screenshot(self, path: str) -> None:
        """Save a screenshot when the instrument supports SCPI binary blocks."""
        raise NotImplementedError("screenshot transfer is model-specific; use the model adapter")


class TektronixMSO4034(Oscilloscope):
    """Tektronix MSO4034/MSO4000-family command adapter."""

    _MEASUREMENT_TYPES = {
        "VPP": "PK2PK",
        "FREQUENCY": "FREQUENCY",
        "FREQ": "FREQUENCY",
        "PERIOD": "PERIOD",
        "MEAN": "MEAN",
        "MIN": "MINIMUM",
        "MAX": "MAXIMUM",
        "RMS": "RMS",
    }

    def run(self) -> None:
        self._transport.write("ACQuire:STATE RUN")

    def stop(self) -> None:
        self._transport.write("ACQuire:STATE STOP")

    def autoscale(self) -> None:
        self._transport.write("AUTOSet EXECute")

    def set_channel_enabled(self, channel: int, enabled: bool = True) -> None:
        if not 1 <= channel <= 4:
            raise ValueError("Tektronix MSO4034 channel must be between 1 and 4")
        state = "ON" if enabled else "OFF"
        self._transport.write(f"SELect:CH{channel} {state}")

    def configure_channel(self, channel: int, *, scale: float | None = None, position: float | None = None) -> None:
        if not 1 <= channel <= 4:
            raise ValueError("Tektronix MSO4034 channel must be between 1 and 4")
        prefix = f"CH{channel}"
        if scale is not None:
            if scale <= 0:
                raise ValueError("scale must be greater than zero")
            self._transport.write(f"{prefix}:SCAle {scale}")
        if position is not None:
            self._transport.write(f"{prefix}:POSition {position}")

    def measure(self, parameter: str, *, source: str = "CH1", unit: str | None = None) -> Measurement:
        parameter = parameter.strip().upper()
        if not _SCPI_TOKEN.fullmatch(parameter):
            raise ValueError("parameter must be a single SCPI token")
        source = self._normalize_source(source)
        measurement_type = self._MEASUREMENT_TYPES.get(parameter, parameter)
        self._transport.write(f"MEASU:IMM:TYPE {measurement_type}")
        self._transport.write(f"MEASU:IMM:SOUrce {source}")
        raw_value = self._transport.query("MEASU:IMM:VAL?")
        value = _parse_measurement_value(raw_value, command="MEASU:IMM:VAL?")
        return Measurement(source=source, parameter=parameter, value=value, unit=unit)

    def measure_frequency(self, *, source: str = "CH1") -> Measurement:
        return self.measure("FREQUENCY", source=source, unit="Hz")

    def measure_vpp(self, *, source: str = "CH1") -> Measurement:
        return self.measure("VPP", source=source, unit="V")

    def measure_peak_to_peak(self, *, source: str = "CH1", unit: str = "V") -> Measurement:
        """Measure peak-to-peak value with a caller-selected engineering unit."""
        return self.measure("VPP", source=source, unit=unit)

    def read_waveform(self, *, source: str = "CH1", points: int | None = None, unit: str = "V") -> Waveform:
        """Acquire signed 8-bit waveform data and convert it to seconds/volts."""
        source = self._normalize_source(source)
        self._transport.clear()
        self._transport.write(f"DATa:SOUrce {source}")
        self._transport.write("DATa:ENCdg RIBinary")
        self._transport.write("DATa:WIDth 1")
        record_length = int(self._query_float("HORizontal:RECOrdlength?"))
        if record_length < 2:
            raise RuntimeError(f"instrument returned an invalid record length: {record_length}")
        requested_points = record_length if points is None else points
        if requested_points < 2:
            raise ValueError("points must be at least 2")
        transfer_points = min(requested_points, record_length)
        self._transport.write("DATa:STARt 1")
        self._transport.write(f"DATa:STOP {transfer_points}")
        x_increment = self._query_float("WFMPRE:XINcr?")
        x_zero = self._query_float("WFMPRE:XZEro?")
        point_offset = self._query_float("WFMPRE:PT_Off?")
        y_multiplier = self._query_float("WFMPRE:YMUlt?")
        y_zero = self._query_float("WFMPRE:YZEro?")
        y_offset = self._query_float("WFMPRE:YOFf?")
        try:
            raw_values = self._transport.query_binary("CURVe?")
        except TransportError:
            self._transport.clear()
            try:
                raw_values = self._transport.query_binary("CURVe?")
            except TransportError as exc:
                raise RuntimeError(f"instrument waveform transfer failed: {exc}") from exc

        if not raw_values:
            raise RuntimeError("instrument returned an empty waveform")
        if len(raw_values) > transfer_points:
            raise RuntimeError(
                f"instrument returned {len(raw_values)} points after requesting at most {transfer_points}"
            )

        times = tuple((index - point_offset) * x_increment + x_zero for index in range(len(raw_values)))
        values = tuple((value - y_offset) * y_multiplier + y_zero for value in raw_values)
        return Waveform(source=source, times=times, values=values, unit=unit)

    def _query_float(self, command: str) -> float:
        try:
            raw_value = self._transport.query(command)
            return _parse_measurement_value(raw_value, command=command)
        except TransportError:
            self._transport.clear()
            try:
                raw_value = self._transport.query(command)
                return _parse_measurement_value(raw_value, command=command)
            except TransportError as exc:
                raise RuntimeError(f"instrument query failed for {command}: {exc}") from exc

    @staticmethod
    def _normalize_source(source: str) -> str:
        normalized = source.strip().upper()
        match = re.fullmatch(r"(?:CH|CHANNEL)([1-4])", normalized)
        if match is None:
            raise ValueError("Tektronix MSO4034 source must be CH1, CH2, CH3, or CH4")
        return f"CH{match.group(1)}"
