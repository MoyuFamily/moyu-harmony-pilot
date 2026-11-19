"""Parsers for HarmonyOS NEXT `hidumper` battery, power, and window services."""

from __future__ import annotations

import re

from moyu_harmony_pilot.device.models import (
    BatteryTelemetry,
    ForegroundWindow,
    PowerTelemetry,
    ScreenState,
)

_KV_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*([^\r\n]+?)\s*$")
_POWER_STATE_RE = re.compile(r"Current State:\s*([A-Za-z_]+)", re.IGNORECASE)
_SCREEN_OFF_TIMEOUT_RE = re.compile(r"ScreenOffTime:\s*Timeout\s*=\s*(\d+)\s*ms", re.IGNORECASE)
_FOCUS_WINDOW_RE = re.compile(r"Focus\s+window\s*:\s*(\d+)", re.IGNORECASE)
_BUNDLE_TOKEN_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+){2,})\b")

_ON_POWER_STATES = frozenset({"AWAKE", "DIM"})
_OFF_POWER_STATES = frozenset({"INACTIVE", "SLEEP", "STAND_BY", "DOZE", "HIBERNATE", "SHUTDOWN"})


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = value.strip()
    match = re.match(r"^(-?\d+)", cleaned)
    if not match:
        return None
    return int(match.group(1))


def parse_battery_dump(output: str) -> BatteryTelemetry:
    """Parse `hidumper -s BatteryService -a '-i'` output.

    Verified against HarmonyOS 6.1 / API 23:
    - `capacity: 72` -> `72` (%)
    - `chargingStatus: 1` -> `charging=True` (`0`=NONE, `1`=ENABLE, `2`=DISABLE, `3`=FULL)
    - `temperature: 300` -> `30.0` (°C, reported in tenths of degree Celsius)
    - `voltage: 4210571` -> `4210.6` mV (reported in microvolts when > 100000)
    """
    fields: dict[str, str] = {}
    for line in output.splitlines():
        match = _KV_LINE_RE.match(line)
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()

    capacity = _parse_int(fields.get("capacity"))
    if capacity is not None and not (0 <= capacity <= 100):
        capacity = None

    charging_status = _parse_int(fields.get("chargingstatus"))
    charging: bool | None = None
    if charging_status is not None:
        if charging_status == 1:
            charging = True
        elif charging_status in {0, 2, 3}:
            charging = False
    elif "chargingstatus" in fields:
        raw_cs = fields["chargingstatus"].strip().upper()
        if raw_cs in {"ENABLE", "CHARGING"}:
            charging = True
        elif raw_cs in {"NONE", "DISABLE", "FULL", "NOT_CHARGING"}:
            charging = False

    plugged_type = _parse_int(fields.get("pluggedtype"))
    health_state = _parse_int(fields.get("healthstate"))
    present_int = _parse_int(fields.get("present"))
    present = None if present_int is None else bool(present_int)

    temperature_c: float | None = None
    raw_temp = fields.get("temperature")
    if raw_temp is not None:
        cleaned_temp = raw_temp.split()[0]
        try:
            if "." in cleaned_temp:
                temperature_c = round(float(cleaned_temp), 1)
            else:
                temperature_c = round(int(cleaned_temp) / 10.0, 1)
        except ValueError:
            temperature_c = None

    voltage_mv: float | None = None
    raw_voltage = _parse_int(fields.get("voltage"))
    if raw_voltage is not None and raw_voltage > 0:
        if raw_voltage > 100_000:
            voltage_mv = round(raw_voltage / 1000.0, 1)
        else:
            voltage_mv = float(raw_voltage)

    return BatteryTelemetry(
        battery_percent=capacity,
        charging=charging,
        charging_status=charging_status,
        plugged_type=plugged_type,
        temperature_c=temperature_c,
        voltage_mv=voltage_mv,
        health_state=health_state,
        present=present,
    )


def parse_power_dump(output: str) -> PowerTelemetry:
    """Parse `hidumper -s PowerManagerService -a '-s'` output."""
    state_match = _POWER_STATE_RE.search(output)
    raw_state = state_match.group(1).upper() if state_match else None

    if raw_state in _ON_POWER_STATES:
        screen_state = ScreenState.ON
    elif raw_state in _OFF_POWER_STATES:
        screen_state = ScreenState.OFF
    else:
        screen_state = ScreenState.UNKNOWN

    timeout_match = _SCREEN_OFF_TIMEOUT_RE.search(output)
    timeout_ms = int(timeout_match.group(1)) if timeout_match else None

    return PowerTelemetry(
        screen_state=screen_state,
        raw_state=raw_state,
        screen_off_timeout_ms=timeout_ms,
    )


def parse_foreground_window(output: str) -> ForegroundWindow:
    """Parse focused foreground window info from `hidumper -s WindowManagerService -a '-a'`."""
    focus_match = _FOCUS_WINDOW_RE.search(output)
    focus_id = int(focus_match.group(1)) if focus_match else None

    window_name: str | None = None
    bundle_name: str | None = None
    ability_name: str | None = None

    lines = output.splitlines()
    if focus_id is not None:
        focus_token = str(focus_id)
        for line in lines:
            tokens = line.split()
            if len(tokens) >= 3 and focus_token in tokens[1:4]:
                # Typical WMS table: `WindowName DisplayId Pid WinId Type Mode Flag ...`
                if tokens[0] not in {"WindowName", "Focus"} and not tokens[0].startswith("---"):
                    window_name = tokens[0]
                    bundle_hit = _BUNDLE_TOKEN_RE.search(window_name)
                    if bundle_hit:
                        bundle_name = bundle_hit.group(1)
                    break

    # Fallback key-value parsing if table format differs across OS versions
    if bundle_name is None:
        m_bundle = re.search(r"bundleName\s*[:=]\s*([A-Za-z0-9._-]+)", output)
        if m_bundle:
            bundle_name = m_bundle.group(1)
    m_ability = re.search(r"abilityName\s*[:=]\s*([A-Za-z0-9._-]+)", output)
    if m_ability:
        ability_name = m_ability.group(1)

    return ForegroundWindow(
        bundle_name=bundle_name,
        window_name=window_name,
        window_id=focus_id,
        ability_name=ability_name,
    )
