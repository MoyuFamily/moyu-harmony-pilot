"""Offline Agent-friendly contract tests for Layer 5 (`moyu_harmony_pilot.cli`)."""

from __future__ import annotations

import importlib
import io
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.cli import (
    SCHEMA_VERSION,
    ExitCode,
    build_cli_schema,
    build_parser,
    classify_exception,
    main,
)
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceLock,
    DeviceOverheatedError,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import (
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcNotFoundError,
    HdcTimeoutError,
)
from moyu_harmony_pilot.ui import UiConcurrentCallError
from tests.fakes import (
    FakeDeviceState,
    FakeHdcTransport,
    build_large_harmony_layout,
)


def _run_cli(
    argv: list[str],
    *,
    transport: FakeHdcTransport | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = main(argv, transport=transport, stdout=out, stderr=err, cwd=cwd, env=env)
    return code, out.getvalue(), err.getvalue()


def _parse_single_json_envelope(stdout: str) -> dict[str, Any]:
    """Assert stdout contains strictly ONE valid JSON envelope with zero stray text."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"Expected single JSON line on stdout, got: {stdout!r}"
    payload = json.loads(lines[0])
    assert set(payload.keys()) == {"schema_version", "ok", "command", "data", "error"}
    assert payload["schema_version"] == SCHEMA_VERSION
    return payload


def test_semantic_exit_code_classification_covers_all_7_codes() -> None:
    assert classify_exception(HdcTimeoutError("timeout"))[0] == ExitCode.FAILED
    assert classify_exception(RuntimeError("boom"))[0] == ExitCode.FAILED
    assert classify_exception(HdcNotFoundError("no hdc"))[0] == ExitCode.CONFIG_ERROR
    assert classify_exception(DeviceConfigurationError("bad cfg"))[0] == ExitCode.CONFIG_ERROR
    assert classify_exception(DeviceOfflineError("offline"))[0] == ExitCode.DEVICE_OFFLINE
    assert classify_exception(DeviceUnauthorizedError("unauth"))[0] == ExitCode.DEVICE_OFFLINE
    assert classify_exception(HumanRequiredError("captcha"))[0] == ExitCode.HUMAN_REQUIRED
    assert classify_exception(DeviceOverheatedError("hot"))[0] == ExitCode.DEVICE_OVERHEATED
    assert classify_exception(DeviceBusyError("busy"))[0] == ExitCode.DEVICE_BUSY
    assert classify_exception(UiConcurrentCallError("concurrent"))[0] == ExitCode.DEVICE_BUSY


def test_schema_command_offline_and_synced_with_parser() -> None:
    # Works completely offline without hdc or devices
    code, out, err = _run_cli(["schema", "--json"])
    assert code == int(ExitCode.OK)
    assert err == ""
    env = _parse_single_json_envelope(out)
    assert env["ok"] is True
    assert env["command"] == "schema"
    schema = env["data"]
    assert set(schema["exit_codes"].keys()) == {"0", "1", "2", "3", "4", "5", "6"}

    # Every CLI subcommand registered in argparse must be documented in schema
    parser = build_parser()
    assert parser.prog == "harmony-pilot"
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == __version__ == schema["pilot_version"]
    registered_commands: set[str] = set()
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            registered_commands.update(action.choices.keys())
    assert registered_commands == set(build_cli_schema()["commands"].keys())

    # Without --json, schema emits valid raw pretty JSON
    code2, out2, _ = _run_cli(["schema"])
    assert code2 == 0
    assert json.loads(out2)["program"] == "harmony-pilot"


def test_devices_command_masks_serials_and_supports_flag_order(tmp_path: Path) -> None:
    raw_serial_1 = "SENSITIVE_REAL_SN_0001"
    raw_serial_2 = "SENSITIVE_REAL_SN_0002"
    config = tmp_path / "devices.local.yaml"
    config.write_text(
        f"""
devices:
  phone-main:
    serial: "{raw_serial_1}"
  phone-backup:
    serial: "{raw_serial_2}"
""",
        encoding="utf-8",
    )

    fake = FakeHdcTransport([FakeDeviceState(serial=raw_serial_1)])
    state_dir = tmp_path / ".state"

    # Both `--json devices` and `devices --json` work identically
    for argv in [
        ["--json", "--config", str(config), "--state-dir", str(state_dir), "devices"],
        ["devices", "--json", "--config", str(config), "--state-dir", str(state_dir)],
    ]:
        code, out, err = _run_cli(argv, transport=fake)
        assert code == int(ExitCode.OK)
        assert err == ""
        assert raw_serial_1 not in out
        assert raw_serial_2 not in out

        env = _parse_single_json_envelope(out)
        assert env["ok"] is True
        assert env["command"] == "devices"
        assert env["data"]["count"] == 2
        assert env["data"]["online_count"] == 1
        by_alias = {d["alias"]: d for d in env["data"]["devices"]}
        assert by_alias["phone-main"]["connection_state"] == "online"
        assert by_alias["phone-main"]["battery_percent"] == 72
        assert by_alias["phone-main"]["temperature_c"] == 30.0
        assert by_alias["phone-backup"]["connection_state"] == "offline"


def test_doctor_command_deep_probe_and_offline_error_exit_3(tmp_path: Path) -> None:
    raw_serial = "SENSITIVE_DOC_SN_0001"
    config = tmp_path / "devices.local.json"
    config.write_text(
        json.dumps({"devices": {"harmony-01": {"serial": raw_serial}}}),
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(config, 0o600)

    fake = FakeHdcTransport([FakeDeviceState(serial=raw_serial)])
    state_dir = tmp_path / ".state"

    code, out, err = _run_cli(
        [
            "doctor",
            "--device",
            "harmony-01",
            "--deep",
            "--json",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
        ],
        transport=fake,
    )
    assert code == int(ExitCode.OK)
    assert err == ""
    assert raw_serial not in out
    assert fake.all_remote_tmp_files() == {}

    env = _parse_single_json_envelope(out)
    assert env["ok"] is True
    assert env["data"]["healthy"] is True
    check_names = {c["name"] for c in env["data"]["checks"]}
    assert {"hdc_binary", "device_telemetry", "uitest_binary", "deep_readonly_probe"} <= check_names

    # Explicit unauthorized device returns exit code 3 with structured JSON error
    unauth_fake = FakeHdcTransport([FakeDeviceState(serial=raw_serial, status="Unauthorized")])
    code_unauth, out_unauth, _ = _run_cli(
        [
            "doctor",
            "--device",
            "harmony-01",
            "--json",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
        ],
        transport=unauth_fake,
    )
    assert code_unauth == int(ExitCode.DEVICE_OFFLINE)
    env_unauth = _parse_single_json_envelope(out_unauth)
    assert env_unauth["ok"] is False
    assert env_unauth["error"]["code"] == "device_unauthorized"


def test_dump_ui_low_token_contract_lock_contention_and_file_mode(tmp_path: Path) -> None:
    raw_serial = "SENSITIVE_UI_SN_0001"
    large_json = json.dumps(build_large_harmony_layout(num_rows=180))
    assert len(large_json.encode("utf-8")) > 120_000

    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"harmony-01": raw_serial}}), encoding="utf-8")
    fake = FakeHdcTransport([FakeDeviceState(serial=raw_serial, layouts=[large_json])])
    state_dir = tmp_path / ".state"
    out_raw_file = tmp_path / "raw_dump.json"

    # Even without --compact, stdout NEVER dumps 120KB raw JSON; --out saves raw with 0600!
    code, out, err = _run_cli(
        [
            "dump-ui",
            "--device",
            "harmony-01",
            "--out",
            str(out_raw_file),
            "--json",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
        ],
        transport=fake,
    )
    assert code == int(ExitCode.OK)
    assert err == ""
    assert len(out.encode("utf-8")) < 6_000
    assert out_raw_file.is_file()
    assert out_raw_file.stat().st_size > 120_000
    if os.name == "posix":
        assert (out_raw_file.stat().st_mode & 0o777) == 0o600
    assert fake.all_remote_tmp_files() == {}

    env = _parse_single_json_envelope(out)
    assert env["ok"] is True
    assert env["data"]["saved_mode"] == "raw"
    assert env["data"]["snapshot"]["kept_elements"] == 24

    # When device is already locked, CLI exits with semantic code 6 (device_busy)
    with DeviceLock(raw_serial, alias="harmony-01", lock_dir=state_dir / "locks"):
        code_busy, out_busy, _ = _run_cli(
            [
                "dump-ui",
                "--device",
                "harmony-01",
                "--compact",
                "--json",
                "--config",
                str(config),
                "--state-dir",
                str(state_dir),
            ],
            transport=fake,
        )
    assert code_busy == int(ExitCode.DEVICE_BUSY)
    env_busy = _parse_single_json_envelope(out_busy)
    assert env_busy["ok"] is False
    assert env_busy["error"]["code"] == "device_busy"
    assert env_busy["error"]["retryable"] is True


def test_screenshot_command_and_invalid_cli_args_json_envelope(tmp_path: Path, monkeypatch) -> None:
    raw_serial = "SENSITIVE_SHOT_SN_01"
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"harmony-01": raw_serial}}), encoding="utf-8")
    fake = FakeHdcTransport([FakeDeviceState(serial=raw_serial)])
    state_dir = tmp_path / ".state"
    shot_file = tmp_path / "screen.jpeg"

    code, out, err = _run_cli(
        [
            "screenshot",
            "--device",
            "harmony-01",
            "--out",
            str(shot_file),
            "--json",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
        ],
        transport=fake,
    )
    assert code == int(ExitCode.OK)
    assert err == ""
    assert raw_serial not in out
    assert shot_file.is_file()
    assert fake.all_remote_tmp_files() == {}

    env = _parse_single_json_envelope(out)
    assert env["data"]["width"] == 1260
    assert env["data"]["height"] == 2720

    # Missing optional rendering dependencies fail before device access.
    from moyu_harmony_pilot.vision import VisionUnavailableError

    def missing_pillow():
        raise VisionUnavailableError("Optional renderer unavailable.")

    monkeypatch.setattr(importlib.import_module("moyu_harmony_pilot.cli.main"),
                        "require_pillow", missing_pillow)
    code_som, out_som, _ = _run_cli(
        [
            "screenshot",
            "--device",
            "harmony-01",
            "--out",
            str(tmp_path / "som.jpeg"),
            "--som",
            "--json",
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
        ],
        transport=fake,
    )
    assert code_som == int(ExitCode.CONFIG_ERROR)
    assert _parse_single_json_envelope(out_som)["error"]["code"] == "vision_unavailable"

    # Missing required --out STILL emits valid JSON envelope on stdout with exit code 2!
    code_arg, out_arg, _ = _run_cli(["screenshot", "--json"])
    assert code_arg == int(ExitCode.CONFIG_ERROR)
    env_arg = _parse_single_json_envelope(out_arg)
    assert env_arg["ok"] is False
    assert env_arg["command"] == "screenshot"
    assert env_arg["error"]["code"] == "configuration_error"

    # Missing hdc binary (no injected transport, empty PATH) -> exit code 2 JSON envelope
    code_no_hdc, out_no_hdc, _ = _run_cli(
        ["devices", "--json", "--hdc-path", str(tmp_path / "missing_hdc")],
        env={"PATH": ""},
    )
    assert code_no_hdc == int(ExitCode.CONFIG_ERROR)
    env_no_hdc = _parse_single_json_envelope(out_no_hdc)
    assert env_no_hdc["error"]["code"] == "hdc_not_found"
