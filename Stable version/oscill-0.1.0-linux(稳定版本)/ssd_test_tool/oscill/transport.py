"""Transport implementations for VISA instruments.

Linux/Ubuntu port: defaults to the NI-VISA backend (@ni). Override with the
``OSCILL_VISA_BACKEND`` environment variable (e.g. ``@py`` for pyvisa-py,
or an absolute path to ``libvisa.so``).
"""

from __future__ import annotations

import os
from typing import Any, Protocol


def _get_visa_backend() -> str:
    """Return the VISA backend string, defaulting to NI-VISA.

    Supported values for ``OSCILL_VISA_BACKEND``:
      - ``@ni``   : NI-VISA (default; requires libvisa.so on Linux)
      - ``@py``   : pyvisa-py pure-Python backend (requires pyusb + libusb)
      - ``/path/to/libvisa.so`` : explicit shared-library path
    """
    return os.environ.get("OSCILL_VISA_BACKEND", "@ni")


class TransportError(RuntimeError):
    """A communication or resource-lifecycle failure."""


class Transport(Protocol):
    """Minimal interface required by :class:`Oscilloscope`."""

    def write(self, command: str) -> None: ...
    def query(self, command: str) -> str: ...
    def query_binary(self, command: str) -> list[int]: ...
    def clear(self) -> None: ...
    def close(self) -> None: ...


class VisaTransport:
    """Transport backed by PyVISA.

    ``resource_name`` can be a USB VISA resource such as
    ``USB0::0x0699::0x0368::C000000::INSTR``.

    On Linux the NI-VISA backend (``@ni``) is used by default. Set the
    ``OSCILL_VISA_BACKEND`` environment variable to choose another backend.
    """

    def __init__(self, resource_name: str, timeout_ms: int = 10000) -> None:
        import pyvisa

        if not resource_name.strip():
            raise ValueError("VISA resource name must not be empty")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be greater than zero")

        self._resource_manager: Any | None = None
        self._resource: Any | None = None
        try:
            manager = pyvisa.ResourceManager(_get_visa_backend())
            self._resource_manager = manager
            resource: Any = manager.open_resource(resource_name.strip())
            self._resource = resource
            resource.timeout = timeout_ms
            resource.write_termination = "\n"
            resource.read_termination = "\n"
        except Exception as exc:
            self._close_quietly()
            raise TransportError(f"could not open VISA resource {resource_name!r}: {exc}") from exc

    def write(self, command: str) -> None:
        resource = self._require_resource()
        try:
            resource.write(command)
        except Exception as exc:
            raise TransportError(f"VISA write failed for {command!r}: {exc}") from exc

    def query(self, command: str) -> str:
        resource = self._require_resource()
        try:
            return str(resource.query(command)).strip()
        except Exception as exc:
            raise TransportError(f"VISA query failed for {command!r}: {exc}") from exc

    def query_binary(self, command: str) -> list[int]:
        resource = self._require_resource()
        try:
            return list(resource.query_binary_values(command, datatype="b", is_big_endian=True, container=list))
        except Exception as exc:
            raise TransportError(f"VISA binary query failed for {command!r}: {exc}") from exc

    def clear(self) -> None:
        resource = self._require_resource()
        try:
            resource.clear()
        except Exception as exc:
            raise TransportError(f"VISA clear failed: {exc}") from exc

    def close(self) -> None:
        resource, manager = self._resource, self._resource_manager
        self._resource = None
        self._resource_manager = None
        close_error: Exception | None = None
        if resource is not None:
            try:
                resource.close()
            except Exception as exc:
                close_error = exc
        if manager is not None:
            try:
                manager.close()
            except Exception as exc:
                close_error = close_error or exc
        if close_error is not None:
            raise TransportError(f"could not close VISA resource: {close_error}") from close_error

    def _require_resource(self) -> Any:
        if self._resource is None:
            raise TransportError("VISA resource is closed")
        return self._resource

    def _close_quietly(self) -> None:
        resource, manager = self._resource, self._resource_manager
        self._resource = None
        self._resource_manager = None
        if resource is not None:
            try:
                resource.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass


def list_visa_resources() -> tuple[str, ...]:
    """Return available VISA resource names while always closing the manager."""
    import pyvisa

    manager: Any | None = None
    try:
        active_manager = pyvisa.ResourceManager(_get_visa_backend())
        manager = active_manager
        return tuple(str(resource) for resource in active_manager.list_resources())
    except Exception as exc:
        raise TransportError(f"could not enumerate VISA resources: {exc}") from exc
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass


class SimulatedTransport:
    """Deterministic instrument substitute for development and tests."""

    def __init__(self, identification: str = "OSCILL,SIMULATOR,0001,1.0") -> None:
        self.identification = identification
        self.commands: list[str] = []
        self.closed = False
        self.binary_values = [0, 64, 127, 64, 0, -64, -127, -64]
        self.data_start = 1
        self.data_stop = len(self.binary_values)

    def write(self, command: str) -> None:
        self.commands.append(command)
        normalized = command.upper()
        if normalized.startswith("DATA:START "):
            self.data_start = int(command.rsplit(maxsplit=1)[1])
        elif normalized.startswith("DATA:STOP "):
            self.data_stop = int(command.rsplit(maxsplit=1)[1])

    def query(self, command: str) -> str:
        self.commands.append(command)
        if command == "*IDN?":
            return self.identification
        if command.upper().startswith("MEASURE:") or command.upper() == "MEASU:IMM:VAL?":
            return "1.234"
        if command == "*OPC?":
            return "1"
        if command.upper() == "HORIZONTAL:RECORDLENGTH?":
            return str(len(self.binary_values))
        waveform_metadata = {
            "WFMPRE:XINCR?": "0.001",
            "WFMPRE:XZERO?": "0",
            "WFMPRE:PT_OFF?": "0",
            "WFMPRE:YMULT?": "0.01",
            "WFMPRE:YZERO?": "0",
            "WFMPRE:YOFF?": "0",
        }
        if command.upper() in waveform_metadata:
            return waveform_metadata[command.upper()]
        return "0"

    def query_binary(self, command: str) -> list[int]:
        self.commands.append(command)
        return list(self.binary_values[self.data_start - 1 : self.data_stop])

    def clear(self) -> None:
        self.commands.append("<clear>")

    def close(self) -> None:
        self.closed = True
