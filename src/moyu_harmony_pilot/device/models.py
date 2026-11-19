"""Data models for Layer 3 (`moyu_harmony_pilot.device`)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.hdc import ConnectionState, mask_serial

DEFAULT_MAX_TEMPERATURE_C = 42.0


class ScreenState(StrEnum):
    """Normalized HarmonyOS screen power state."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """Local mapping between a privacy-safe device alias and its raw `hdc` target serial."""

    alias: str
    serial: str
    max_temperature_c: float = DEFAULT_MAX_TEMPERATURE_C
    notes: str = ""

    @property
    def serial_masked(self) -> str:
        return mask_serial(self.serial)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Read-only HarmonyOS device identity and runtime metadata."""

    model: str | None = None
    software_version: str | None = None
    api_version: int | None = None
    os_fullname: str | None = None
    uitest_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "software_version": self.software_version,
            "api_version": self.api_version,
            "os_fullname": self.os_fullname,
            "uitest_version": self.uitest_version,
        }


@dataclass(frozen=True, slots=True)
class BatteryTelemetry:
    """Structured battery and thermal metrics parsed from `BatteryService`."""

    battery_percent: int | None = None
    charging: bool | None = None
    charging_status: int | None = None
    plugged_type: int | None = None
    temperature_c: float | None = None
    voltage_mv: float | None = None
    health_state: int | None = None
    present: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "battery_percent": self.battery_percent,
            "charging": self.charging,
            "charging_status": self.charging_status,
            "plugged_type": self.plugged_type,
            "temperature_c": self.temperature_c,
            "voltage_mv": self.voltage_mv,
            "health_state": self.health_state,
            "present": self.present,
        }


@dataclass(frozen=True, slots=True)
class PowerTelemetry:
    """Structured power and screen state parsed from `PowerManagerService`."""

    screen_state: ScreenState = ScreenState.UNKNOWN
    raw_state: str | None = None
    screen_off_timeout_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen_state": self.screen_state.value,
            "raw_state": self.raw_state,
            "screen_off_timeout_ms": self.screen_off_timeout_ms,
        }


@dataclass(frozen=True, slots=True)
class ForegroundWindow:
    """Focused foreground window metadata parsed from `WindowManagerService`."""

    bundle_name: str | None = None
    window_name: str | None = None
    window_id: int | None = None
    ability_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_name": self.bundle_name,
            "window_name": self.window_name,
            "window_id": self.window_id,
            "ability_name": self.ability_name,
        }


@dataclass(frozen=True, slots=True)
class DeviceHealth:
    """Aggregated privacy-safe device health snapshot (never exposes raw serial)."""

    alias: str
    connection_state: ConnectionState
    battery: BatteryTelemetry
    power: PowerTelemetry
    overheated: bool
    max_temperature_c: float
    info: DeviceInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "alias": self.alias,
            "connection_state": self.connection_state.value,
            "battery_percent": self.battery.battery_percent,
            "charging": self.battery.charging,
            "temperature_c": self.battery.temperature_c,
            "screen_state": self.power.screen_state.value,
            "overheated": self.overheated,
            "max_temperature_c": self.max_temperature_c,
            "battery": self.battery.to_dict(),
            "power": self.power.to_dict(),
        }
        if self.info is not None:
            payload["info"] = self.info.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    """Privacy-safe device row returned by device enumeration commands."""

    alias: str | None
    serial_masked: str
    transport: str
    connection_state: ConnectionState
    configured: bool
    busy: bool
    battery_percent: int | None = None
    charging: bool | None = None
    temperature_c: float | None = None
    screen_state: ScreenState = ScreenState.UNKNOWN
    overheated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "serial_masked": self.serial_masked,
            "transport": self.transport,
            "connection_state": self.connection_state.value,
            "configured": self.configured,
            "busy": self.busy,
            "battery_percent": self.battery_percent,
            "charging": self.charging,
            "temperature_c": self.temperature_c,
            "screen_state": self.screen_state.value,
            "overheated": self.overheated,
        }


@dataclass(frozen=True, slots=True)
class ScreenshotResult:
    """Structured metadata for a captured and stored local screenshot."""

    alias: str
    path: Path
    image_format: str
    width: int | None
    height: int | None
    size_bytes: int
    resized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "path": str(self.path),
            "format": self.image_format,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "resized": self.resized,
        }
