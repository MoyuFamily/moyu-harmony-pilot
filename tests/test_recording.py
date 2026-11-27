"""Offline contract tests for HarmonyOS screen recording (CLI, action-attached, and MCP)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from moyu_harmony_pilot.actions import ActionOutcome, ActionPlugin, ActionRegistry, ActionSpec
from moyu_harmony_pilot.cli import ExitCode, build_cli_schema
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceLock,
    DeviceProfile,
    DeviceRegistry,
    HarmonyDevice,
    RecordingResult,
    RecordingSessionManager,
    ScreenRecorder,
    encode_frames_mp4,
)
from moyu_harmony_pilot.device import recording as recording_mod
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.mcp import PilotTools
from moyu_harmony_pilot.mcp.server import create_server
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_cli import _parse_single_json_envelope, _run_cli
from tests.test_mcp import invoke as invoke_mcp


def _fake_ffmpeg_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "fake-ffmpeg"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    return binary


def _install_fake_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, calls: list[list[str]] | None = None
) -> Path:
    binary = _fake_ffmpeg_binary(tmp_path)
    monkeypatch.setattr(recording_mod, "find_ffmpeg", lambda path=None: binary)

    def _fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(cmd))
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(recording_mod.subprocess, "run", _fake_run)
    return binary


def test_screen_recorder_captures_frames_encodes_mp4_and_cleans_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_calls: list[list[str]] = []
    _install_fake_ffmpeg(monkeypatch, tmp_path, ffmpeg_calls)

    raw_serial = "SYNTHETIC_RECORD_SN_001"
    transport = FakeHdcTransport([FakeDeviceState(serial=raw_serial)])
    hdc = HdcClient(transport=transport)
    device = HarmonyDevice(
        hdc, DeviceProfile(alias="lab-phone", serial=raw_serial), state_dir=tmp_path / ".state"
    )
    out_mp4 = tmp_path / "out" / "demo.mp4"

    # Screen recording must not block or collide with an active DeviceLock (uitest lock).
    with DeviceLock(raw_serial, alias="lab-phone", lock_dir=device.lock_dir):
        recorder = ScreenRecorder(
            device,
            out_mp4,
            fps=5.0,
            max_duration=5.0,
            max_width=720,
            encoder=encode_frames_mp4,
        )
        recorder.start(capture_initial_frame=True)
        result = recorder.stop(capture_final_frame=True, stopped_reason="stopped")

    assert out_mp4.is_file()
    if os.name == "posix":
        assert (out_mp4.stat().st_mode & 0o777) == 0o600
    assert result.alias == "lab-phone"
    assert result.video_format == "mp4"
    assert result.frame_count >= 2
    assert result.width == 720
    assert result.height is not None and result.height % 2 == 0
    assert result.size_bytes > 0
    assert result.stopped_reason == "stopped"
    assert transport.all_remote_tmp_files() == {}

    # Temporary frame directory is cleaned up after encoding.
    rec_root = tmp_path / ".state" / "recordings"
    assert list(rec_root.glob("work_*")) == []
    assert len(ffmpeg_calls) == 1
    assert "-pix_fmt" in ffmpeg_calls[0] and "yuv420p" in ffmpeg_calls[0]
    assert "scale='trunc(min(iw,720)/2)*2':-2" in ffmpeg_calls[0]


def test_recording_session_start_status_stop_and_privacy_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    raw_serial = "SENSITIVE_SESSION_SN_999"
    transport = FakeHdcTransport([FakeDeviceState(serial=raw_serial)])
    device = HarmonyDevice(
        HdcClient(transport=transport),
        DeviceProfile(alias="qa-phone", serial=raw_serial),
        state_dir=tmp_path / ".state",
    )

    spawned_argv: list[list[str]] = []
    stdin_payloads: list[dict[str, Any]] = []
    alive_pids: set[int] = set()
    next_pid = [42420]

    class _FakeStdin:
        def __init__(self, on_payload: Any) -> None:
            self._buf = bytearray()
            self._on_payload = on_payload

        def write(self, data: bytes) -> int:
            self._buf.extend(data)
            return len(data)

        def close(self) -> None:
            payload = json.loads(self._buf.decode("utf-8"))
            self._on_payload(payload)

    class _FakePopen:
        def __init__(self, argv: list[str], **_kwargs: Any) -> None:
            spawned_argv.append(list(argv))
            self.pid = next_pid[0]
            next_pid[0] += 1
            alive_pids.add(self.pid)
            self._payload: dict[str, Any] = {}
            self.stdin = _FakeStdin(self._accept_payload)

        def _accept_payload(self, payload: dict[str, Any]) -> None:
            self._payload = payload
            stdin_payloads.append(payload)
            Path(payload["ready_file"]).write_text(
                json.dumps({"pid": self.pid, "ready": True}), encoding="utf-8"
            )

        def poll(self) -> int | None:
            return None if self.pid in alive_pids else 0

        def terminate(self) -> None:
            alive_pids.discard(self.pid)

    monkeypatch.setattr(recording_mod.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(recording_mod, "_is_pid_alive", lambda pid: pid in alive_pids)

    orig_write_text = recording_mod.write_secure_text

    def _finish_on_stop_touch(path: str | Path, text: str) -> Path:
        written = orig_write_text(path, text)
        if Path(path).name == "stop" and stdin_payloads:
            payload = stdin_payloads[-1]
            out_file = Path(payload["output_path"])
            out_file.write_bytes(b"mp4-bytes")
            out_file.chmod(0o600)
            orig_write_text(
                payload["result_file"],
                json.dumps(
                    RecordingResult(
                        alias="qa-phone",
                        path=out_file,
                        video_format="mp4",
                        frame_count=4,
                        duration_seconds=2.0,
                        effective_fps=2.0,
                        width=720,
                        height=1554,
                        size_bytes=9,
                        stopped_reason="stopped",
                    ).to_dict()
                ),
            )
            alive_pids.clear()
        return written

    monkeypatch.setattr(recording_mod, "write_secure_text", _finish_on_stop_touch)

    manager = RecordingSessionManager(tmp_path / ".state")
    out_mp4 = tmp_path / "acceptance.mp4"
    info = manager.start(device, out_mp4, fps=2.0, max_duration=30.0)
    assert info.active is True
    assert info.alias == "qa-phone"

    # Raw serial must NEVER appear in worker CLI argv or on-disk session metadata/filenames.
    assert raw_serial not in json.dumps(spawned_argv)
    rec_dir = tmp_path / ".state" / "recordings"
    session_files = list(rec_dir.glob("session_*.json"))
    assert len(session_files) == 1
    assert raw_serial not in session_files[0].name
    assert raw_serial not in session_files[0].read_text(encoding="utf-8")

    # Active session blocks duplicate start with DeviceBusyError.
    with pytest.raises(DeviceBusyError):
        manager.start(device, out_mp4)

    status = manager.status(device)
    assert status is not None and status.active is True

    result = manager.stop(device)
    assert result.path == out_mp4.resolve()
    assert result.frame_count == 4
    assert manager.status(device) is None

    # Stale dead-PID session metadata is automatically reclaimed on next start.
    stale_file = rec_dir / f"session_{recording_mod._device_session_key(raw_serial)}.json"
    orig_write_text(
        stale_file,
        json.dumps(
            {
                "session_id": "stale123",
                "alias": "qa-phone",
                "pid": 999999,
                "output_path": str(out_mp4),
                "started_at": "2026-01-01T00:00:00Z",
                "max_duration": 30.0,
                "fps": 2.0,
                "max_width": 720,
                "display_id": None,
                "session_dir": str(rec_dir / "session_stale123"),
                "stop_file": str(rec_dir / "session_stale123" / "stop"),
                "result_file": str(rec_dir / "session_stale123" / "result.json"),
                "error_file": str(rec_dir / "session_stale123" / "error.json"),
            }
        ),
    )
    restarted = manager.start(device, out_mp4)
    assert restarted.active is True
    manager.stop(device)


def test_cli_record_commands_and_schema_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_serial = "CLI_RECORD_SN_001"
    transport = FakeHdcTransport([FakeDeviceState(serial=raw_serial)])
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": {"serial": raw_serial}}}), encoding="utf-8")
    config.chmod(0o600)
    base_flags = ["--config", str(config), "--state-dir", str(tmp_path / "state"), "--json"]

    # Schema exposes record subcommands and run recording options offline.
    schema = build_cli_schema()
    assert set(schema["commands"]["record"]["subcommands"]) == {
        "start",
        "stop",
        "status",
        "capture",
    }
    assert {"--record", "--record-out", "--record-on-failure"} <= set(
        schema["commands"]["run"]["options"]
    )
    assert "--enable-recording" in schema["mcp_adapter"]["options"]

    # Missing ffmpeg fails fast with CONFIG_ERROR (exit code 2) before touching hdc.
    monkeypatch.setattr(recording_mod, "find_ffmpeg", lambda path=None: None)
    code, out, err = _run_cli(
        base_flags + ["record", "start", "--device", "phone", "--out", str(tmp_path / "a.mp4")],
        transport=transport,
    )
    assert code == int(ExitCode.CONFIG_ERROR) and err == ""
    assert _parse_single_json_envelope(out)["error"]["code"] == "configuration_error"
    assert transport.calls == []

    # Invalid file extension rejected cleanly before device I/O.
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    code_bad, out_bad, _ = _run_cli(
        base_flags + ["record", "capture", "--duration", "1", "--out", str(tmp_path / "bad.avi")],
        transport=transport,
    )
    assert code_bad == int(ExitCode.CONFIG_ERROR)
    assert _parse_single_json_envelope(out_bad)["error"]["code"] == "configuration_error"

    # Foreground capture produces valid MP4 envelope and status reports inactive afterward.
    out_mp4 = tmp_path / "clip.mp4"
    orig_wait = recording_mod.threading.Event.wait
    monkeypatch.setattr(
        recording_mod.threading.Event,
        "wait",
        lambda self, timeout=None: orig_wait(self, timeout=min(timeout or 0.05, 0.05)),
    )
    code_cap, out_cap, err_cap = _run_cli(
        base_flags
        + ["record", "capture", "--device", "phone", "--duration", "1", "--out", str(out_mp4)],
        transport=transport,
    )
    assert code_cap == int(ExitCode.OK) and err_cap == ""
    cap_env = _parse_single_json_envelope(out_cap)
    assert cap_env["ok"] is True
    assert cap_env["data"]["path"] == str(out_mp4)
    assert out_mp4.is_file()
    assert raw_serial not in out_cap

    code_st, out_st, _ = _run_cli(
        base_flags + ["record", "status", "--device", "phone"],
        transport=transport,
    )
    assert code_st == int(ExitCode.OK)
    assert _parse_single_json_envelope(out_st)["data"] == {"alias": "phone", "active": False}


def test_cli_run_with_record_and_record_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])

    class _FailingReadonly(ActionPlugin):
        spec = ActionSpec(
            action_id="demo.always_fail",
            version="1.0",
            description="Deterministic failure plugin for recording retention check.",
            side_effect="readonly",
            params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )

        def execute(self, ctx: Any, params: dict[str, Any]) -> ActionOutcome:
            return ActionOutcome(status="failed", summary="Simulated check failure.")

    raw_serial = "ACTION_RECORD_SN_001"
    transport = FakeHdcTransport([FakeDeviceState(serial=raw_serial)])
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": {"serial": raw_serial}}}), encoding="utf-8")
    config.chmod(0o600)
    state_dir = tmp_path / "state"
    base_flags = ["--config", str(config), "--state-dir", str(state_dir), "--json"]

    # 1) --dry-run validates recording flags without capturing frames or creating output.
    dry_mp4 = tmp_path / "dry.mp4"
    code_dry, out_dry, _ = _run_cli(
        base_flags
        + ["run", "--device", "phone", "--action", "device.health", "--dry-run",
           "--record-out", str(dry_mp4)],
        transport=transport,
        env={},
    )
    assert code_dry == int(ExitCode.OK)
    assert _parse_single_json_envelope(out_dry)["data"]["recording"] == {
        "saved": False,
        "dry_run": True,
    }
    assert not dry_mp4.exists()

    # 2) --record-out saves MP4 on normal action run.
    ok_mp4 = tmp_path / "health.mp4"
    code_ok, out_ok, err_ok = _run_cli(
        base_flags
        + ["run", "--device", "phone", "--action", "device.health", "--record-out", str(ok_mp4)],
        transport=transport,
        env={},
    )
    assert code_ok == int(ExitCode.OK) and err_ok == ""
    ok_env = _parse_single_json_envelope(out_ok)
    assert ok_env["data"]["recording"]["saved"] is True
    assert ok_env["data"]["recording"]["path"] == str(ok_mp4)
    assert ok_mp4.is_file()

    # 3) --record-on-failure discards MP4 when action succeeds.
    discard_mp4 = tmp_path / "discard.mp4"
    code_dis, out_dis, _ = _run_cli(
        base_flags
        + ["run", "--device", "phone", "--action", "device.health",
           "--record-out", str(discard_mp4), "--record-on-failure"],
        transport=transport,
        env={},
    )
    assert code_dis == int(ExitCode.OK)
    dis_rec = _parse_single_json_envelope(out_dis)["data"]["recording"]
    assert dis_rec["saved"] is False and dis_rec["discarded_on_success"] is True
    assert not discard_mp4.exists()

    # 4) --record-on-failure preserves MP4 when action fails and keeps failure exit code.
    registry = ActionRegistry()
    registry.register(_FailingReadonly())
    monkeypatch.setattr(ActionRegistry, "discover", lambda **kwargs: registry)
    fail_mp4 = tmp_path / "failure.mp4"
    code_fail, out_fail, _ = _run_cli(
        base_flags
        + ["run", "--device", "phone", "--action", "demo.always_fail",
           "--record-out", str(fail_mp4), "--record-on-failure"],
        transport=transport,
        env={},
    )
    assert code_fail == int(ExitCode.FAILED)
    fail_env = _parse_single_json_envelope(out_fail)
    assert fail_env["ok"] is False
    assert fail_env["data"]["recording"]["saved"] is True
    assert fail_mp4.is_file()


def test_mcp_recording_opt_in_and_local_diagnostics_confinement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_ffmpeg(monkeypatch, tmp_path)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])

    state = FakeDeviceState("SYNTHETIC_MCP_REC_001")
    transport = FakeHdcTransport([state])
    devices = DeviceRegistry([DeviceProfile("phone", state.serial)])
    actions = ActionRegistry.discover(env={}, allowed_actions=["device.health"])

    # Default Safe Mode omits recording tools and rejects run_action(record=True).
    safe_facade = PilotTools(HdcClient(transport=transport), devices, actions, state_dir=tmp_path)
    safe_server = create_server(safe_facade)
    safe_tools = {tool.name for tool in asyncio.run(safe_server.list_tools())}
    assert {"record_start", "record_stop", "record_status"}.isdisjoint(safe_tools)
    denied = invoke_mcp(
        safe_server,
        "run_action",
        {"alias": "phone", "action_id": "device.health", "params": {}, "record": True},
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "configuration_error"

    # Opt-in --enable-recording exposes recording tools and confines files to diagnostics/.
    rec_facade = PilotTools(
        HdcClient(transport=transport),
        devices,
        actions,
        state_dir=tmp_path,
        enable_recording=True,
    )
    rec_server = create_server(rec_facade)
    rec_tools = {tool.name for tool in asyncio.run(rec_server.list_tools())}
    assert {"record_start", "record_status", "record_stop"} <= rec_tools

    status_res = invoke_mcp(rec_server, "record_status", {"alias": "phone"})
    assert status_res["ok"] is True and status_res["data"] == {"alias": "phone", "active": False}

    run_res = invoke_mcp(
        rec_server,
        "run_action",
        {"alias": "phone", "action_id": "device.health", "params": {}, "record": True},
    )
    assert run_res["ok"] is True
    rec_path = Path(run_res["data"]["recording"]["path"])
    assert rec_path.is_file()
    assert rec_path.is_relative_to(tmp_path / "diagnostics")
    assert state.serial not in json.dumps(run_res)
