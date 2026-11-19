"""Offline tests for Layer 3 (`moyu_harmony_pilot.device`): registry, lock & controller."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceLock,
    DeviceOverheatedError,
    DeviceProfile,
    DeviceRegistry,
    DiagnosticsStore,
    HarmonyDevice,
    ScreenState,
    UnknownDeviceAliasError,
    is_device_busy,
    lock_file_path,
    parse_battery_dump,
    parse_foreground_window,
    parse_power_dump,
    read_image_dimensions,
)
from moyu_harmony_pilot.hdc import (
    ConnectionState,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcClient,
    HdcFileTransferError,
    HdcTarget,
)
from tests.fakes import (
    BATTERY_61_CHARGING,
    BATTERY_61_OVERHEATED,
    POWER_61_AWAKE,
    POWER_61_INACTIVE,
    WMS_61_FOCUS,
    FakeDeviceState,
    FakeHdcTransport,
    make_minimal_jpeg,
    make_minimal_png,
)


def test_registry_loads_yaml_without_pyyaml_and_json(tmp_path: Path) -> None:
    yaml_file = tmp_path / "devices.local.yaml"
    yaml_file.write_text(
        """
# Synthetic local device mapping
devices:
  harmony-01:
    serial: "SYNTHETIC_SERIAL_001"
    max_temperature_c: 41.5
    notes: "Primary test node"
  harmony-02: "SYNTHETIC_SERIAL_002"
""",
        encoding="utf-8",
    )

    reg = DeviceRegistry.from_file(yaml_file)
    p1 = reg.get_by_alias("harmony-01")
    p2 = reg.get_by_alias("harmony-02")
    assert p1 is not None
    assert p1.serial == "SYNTHETIC_SERIAL_001"
    assert p1.max_temperature_c == 41.5
    assert p1.notes == "Primary test node"
    assert p2 is not None
    assert p2.serial == "SYNTHETIC_SERIAL_002"

    # JSON format also works identically
    json_file = tmp_path / "devices.local.json"
    json_file.write_text(
        json.dumps(
            {"devices": {"h1": {"serial": "SYNTHETIC_JSON_001", "max_temperature_c": 42.0}}}
        ),
        encoding="utf-8",
    )
    reg_json = DeviceRegistry.from_file(json_file)
    assert reg_json.get_by_alias("h1") is not None

    # Duplicate serial mapping rejected
    dup_file = tmp_path / "dup.json"
    dup_file.write_text(
        json.dumps({"devices": {"a1": "SAME_SERIAL", "a2": "SAME_SERIAL"}}),
        encoding="utf-8",
    )
    with pytest.raises(DeviceConfigurationError):
        DeviceRegistry.from_file(dup_file)

    # Invalid temperature threshold rejected
    bad_temp = tmp_path / "bad_temp.json"
    bad_temp.write_text(
        json.dumps({"devices": {"a1": {"serial": "S1", "max_temperature_c": 99.0}}}),
        encoding="utf-8",
    )
    with pytest.raises(DeviceConfigurationError):
        DeviceRegistry.from_file(bad_temp)


def test_registry_target_resolution_and_error_classification() -> None:
    reg = DeviceRegistry(
        [
            DeviceProfile(alias="dev-online", serial="SER_ON"),
            DeviceProfile(alias="dev-offline", serial="SER_OFF"),
            DeviceProfile(alias="dev-unauth", serial="SER_UNAUTH"),
        ]
    )
    targets = [
        HdcTarget("SER_ON", "USB", "Connected", ConnectionState.ONLINE, "localhost"),
        HdcTarget("SER_OFF", "USB", "Offline", ConnectionState.OFFLINE, "localhost"),
        HdcTarget("SER_UNAUTH", "USB", "Unauthorized", ConnectionState.UNAUTHORIZED, "localhost"),
    ]

    profile, target = reg.resolve("dev-online", targets)
    assert profile.alias == "dev-online"
    assert target is not None and target.serial == "SER_ON"

    # Default resolves when exactly 1 online target exists
    def_profile, _ = reg.resolve(None, targets)
    assert def_profile.alias == "dev-online"

    # Offline / Unauthorized raise exit-3 errors
    with pytest.raises(DeviceOfflineError):
        reg.resolve("dev-offline", targets)
    with pytest.raises(DeviceUnauthorizedError):
        reg.resolve("dev-unauth", targets)

    # Unknown alias raises exit-2 configuration error
    with pytest.raises(UnknownDeviceAliasError):
        reg.resolve("no-such-alias", targets)

    # Ambiguous multi-online default raises UnknownDeviceAliasError
    two_online = [
        HdcTarget("SER_ON", "USB", "Connected", ConnectionState.ONLINE),
        HdcTarget("SER_TWO", "USB", "Connected", ConnectionState.ONLINE),
    ]
    with pytest.raises(UnknownDeviceAliasError):
        reg.resolve(None, two_online)


def test_device_lock_in_process_and_cross_process_contention(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    raw_serial = "SECRET_SERIAL_99999"

    # Lock filename never leaks raw serial
    lpath = lock_file_path(raw_serial, lock_dir)
    assert raw_serial not in lpath.name

    with DeviceLock(raw_serial, alias="harmony-01", lock_dir=lock_dir):
        assert is_device_busy(raw_serial, lock_dir) is True
        if os.name == "posix":
            assert (lock_dir.stat().st_mode & 0o777) == 0o700
            assert (lpath.stat().st_mode & 0o777) == 0o600
        # Lock file content contains alias, NOT raw serial
        assert raw_serial not in lpath.read_text(encoding="utf-8")

        # Second in-process acquisition fails with DeviceBusyError
        with pytest.raises(DeviceBusyError):
            with DeviceLock(raw_serial, alias="harmony-01", lock_dir=lock_dir, wait_timeout=0.0):
                pass

        # Real cross-process contention check via child Python process
        probe_code = (
            "from moyu_harmony_pilot.device import DeviceLock, DeviceBusyError\n"
            f"lock = DeviceLock({raw_serial!r}, alias='harmony-01', lock_dir={str(lock_dir)!r})\n"
            "try:\n"
            "    lock.acquire()\n"
            "except DeviceBusyError:\n"
            "    raise SystemExit(6)\n"
            "raise SystemExit(0)\n"
        )
        child = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe_code],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        assert child.returncode == 6

    assert is_device_busy(raw_serial, lock_dir) is False


def test_telemetry_parsers_calibrated_to_harmony_61() -> None:
    bat = parse_battery_dump(BATTERY_61_CHARGING)
    assert bat.battery_percent == 72
    assert bat.charging is True
    assert bat.charging_status == 1
    assert bat.plugged_type == 2
    assert bat.temperature_c == 30.0
    assert bat.voltage_mv == 4210.6
    assert bat.present is True

    hot = parse_battery_dump(BATTERY_61_OVERHEATED)
    assert hot.temperature_c == 43.5

    # Missing telemetry fields remain None (never fabricated!)
    empty = parse_battery_dump("no telemetry fields here\n")
    assert empty.battery_percent is None
    assert empty.charging is None
    assert empty.temperature_c is None

    awake = parse_power_dump(POWER_61_AWAKE)
    assert awake.screen_state == ScreenState.ON
    assert awake.raw_state == "AWAKE"
    assert awake.screen_off_timeout_ms == 30000

    asleep = parse_power_dump(POWER_61_INACTIVE)
    assert asleep.screen_state == ScreenState.OFF
    assert asleep.raw_state == "INACTIVE"

    fg = parse_foreground_window(WMS_61_FOCUS)
    assert fg.window_id == 42
    assert fg.bundle_name == "com.example.demo0"


def test_diagnostics_store_permissions_and_rolling_retention(tmp_path: Path) -> None:
    diag_dir = tmp_path / "diag"
    store = DiagnosticsStore(diag_dir, max_files=3, max_total_bytes=1024)

    paths: list[Path] = []
    for idx in range(5):
        p = store.save_text(f"snap_{idx}", f"payload-{idx}", suffix=".json")
        paths.append(p)

    if os.name == "posix":
        assert (diag_dir.stat().st_mode & 0o777) == 0o700
        assert (paths[-1].stat().st_mode & 0o777) == 0o600

    remaining = list(diag_dir.iterdir())
    assert len(remaining) == 3

    # Also test byte-cap pruning
    small_store = DiagnosticsStore(tmp_path / "diag_bytes", max_files=10, max_total_bytes=15)
    for idx in range(4):
        small_store.save_bytes(f"b{idx}", b"0123456789", suffix=".bin")
    kept = list((tmp_path / "diag_bytes").iterdir())
    assert len(kept) == 1


def test_harmony_device_health_thermal_guard_screenshot_and_controls(tmp_path: Path) -> None:
    dev_state = FakeDeviceState(
        serial="DEV_CTRL_0001",
        battery_dump=BATTERY_61_CHARGING,
        jpeg_bytes=make_minimal_jpeg(1260, 2720),
        png_bytes=make_minimal_png(1260, 2720),
    )
    fake = FakeHdcTransport([dev_state])
    client = HdcClient(transport=fake)
    profile = DeviceProfile(alias="harmony-01", serial=dev_state.serial, max_temperature_c=42.0)
    device = HarmonyDevice(client, profile, state_dir=tmp_path / ".state")

    health = device.assert_thermal_safe()
    assert health.overheated is False
    assert health.battery.temperature_c == 30.0
    # Privacy check: health dict contains alias and never raw serial
    assert dev_state.serial not in json.dumps(health.to_dict())

    # Overheat blocks mutating gate
    dev_state.battery_dump = BATTERY_61_OVERHEATED
    with pytest.raises(DeviceOverheatedError):
        device.assert_thermal_safe()

    # JPEG screenshot via snapshot_display + 0600 + pure-Python dimensions + 0 remote tmp leak
    jpeg_out = tmp_path / "cap.jpeg"
    shot = device.screenshot(jpeg_out)
    assert shot.image_format == "jpeg"
    assert (shot.width, shot.height) == (1260, 2720)
    assert read_image_dimensions(jpeg_out) == (1260, 2720)
    if os.name == "posix":
        assert (jpeg_out.stat().st_mode & 0o777) == 0o600
    assert fake.all_remote_tmp_files() == {}

    # PNG screenshot via uitest screenCap
    png_out = tmp_path / "cap.png"
    shot_png = device.screenshot(png_out)
    assert shot_png.image_format == "png"
    assert (shot_png.width, shot_png.height) == (1260, 2720)
    assert fake.all_remote_tmp_files() == {}

    # Recv failure still cleans up remote tmp file
    dev_state.fail_recv = True
    with pytest.raises(HdcFileTransferError):
        device.screenshot(tmp_path / "failed.jpeg")
    assert fake.all_remote_tmp_files() == {}

    # App & power control
    dev_state.fail_recv = False
    device.suspend()
    assert device.get_power().screen_state == ScreenState.OFF
    device.wakeup()
    assert device.get_power().screen_state == ScreenState.ON
    device.unlock_swipe(screen_size=(1260, 2720))
    device.start_app("com.example.demo", "EntryAbility")
    device.force_stop("com.example.demo")
    assert device.get_foreground_window().window_id == 42

    with pytest.raises(DeviceConfigurationError):
        device.start_app("bad; rm -rf /", "EntryAbility")
