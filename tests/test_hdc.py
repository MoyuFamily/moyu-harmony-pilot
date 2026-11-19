"""Offline tests for Layer 1 (`moyu_harmony_pilot.hdc`) including real SubprocessHdcTransport."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from tests.fakes import FakeDeviceState, FakeHdcTransport

from moyu_harmony_pilot.hdc import (
    HDC_PATH_ENV,
    REMOTE_TMP_PREFIX,
    ConnectionState,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcClient,
    HdcCommandError,
    HdcErrorKind,
    HdcFileTransferError,
    HdcInvalidArgumentError,
    HdcNotFoundError,
    HdcTimeoutError,
    SubprocessHdcTransport,
    build_safe_shell_command,
    classify_hdc_error,
    discover_hdc,
    make_remote_tmp_path,
    mask_serial,
    parse_targets_output,
    validate_remote_tmp_path,
)


def _make_executable_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_mask_serial_redacts_hardware_identifiers() -> None:
    assert mask_serial("SYNTHETIC0012345") == "SYNT***2345"
    assert mask_serial("SHORT") == "***"
    assert mask_serial("") == "***"


def test_discover_hdc_priority_and_fail_closed(tmp_path: Path) -> None:
    explicit = _make_executable_script(tmp_path / "explicit" / "hdc", "print('Ver: 3.2.0c')")
    env_bin = _make_executable_script(tmp_path / "env" / "hdc", "print('Ver: 3.2.0c')")
    path_bin = _make_executable_script(tmp_path / "path" / "hdc", "print('Ver: 3.2.0c')")
    sdk_home = tmp_path / "home"
    sdk_old = _make_executable_script(
        sdk_home / "Library" / "OpenHarmony" / "Sdk" / "5.0-Release" / "toolchains" / "hdc",
        "print('Ver: 3.0.0')",
    )
    sdk_new = _make_executable_script(
        sdk_home / "Library" / "OpenHarmony" / "Sdk" / "6.1-Release" / "toolchains" / "hdc",
        "print('Ver: 3.2.0c')",
    )

    # 1. Explicit wins over env, PATH, and SDK
    assert (
        discover_hdc(
            explicit,
            env={HDC_PATH_ENV: str(env_bin), "PATH": str(path_bin.parent)},
            home_dir=sdk_home,
        )
        == explicit
    )

    # 2. Explicit invalid fails closed rather than silently falling back
    with pytest.raises(HdcNotFoundError):
        discover_hdc(
            tmp_path / "missing_hdc",
            env={HDC_PATH_ENV: str(env_bin), "PATH": str(path_bin.parent)},
            home_dir=sdk_home,
        )

    # 3. HARMONY_PILOT_HDC_PATH wins over PATH and SDK
    assert (
        discover_hdc(
            None,
            env={HDC_PATH_ENV: str(env_bin), "PATH": str(path_bin.parent)},
            home_dir=sdk_home,
        )
        == env_bin
    )

    # 4. Invalid HARMONY_PILOT_HDC_PATH fails closed
    with pytest.raises(HdcNotFoundError):
        discover_hdc(
            None,
            env={HDC_PATH_ENV: str(tmp_path / "nope"), "PATH": str(path_bin.parent)},
            home_dir=sdk_home,
        )

    # 5. PATH wins over SDK fallback
    assert discover_hdc(None, env={"PATH": str(path_bin.parent)}, home_dir=sdk_home) == path_bin

    # 6. SDK fallback picks newest version first (6.1 before 5.0)
    assert discover_hdc(None, env={"PATH": ""}, home_dir=sdk_home) == sdk_new
    assert sdk_old != sdk_new

    # 7. Missing everywhere raises HdcNotFoundError
    with pytest.raises(HdcNotFoundError):
        discover_hdc(None, env={"PATH": ""}, home_dir=tmp_path / "empty_home")


def test_parse_targets_output_all_four_states() -> None:
    assert parse_targets_output("[Empty]\n") == []

    sample = (
        "SERIAL_ONLINE_001\t\tUSB\tConnected\tlocalhost\n"
        "SERIAL_OFFLINE_02\t\tUSB\tOffline\tlocalhost\n"
        "SERIAL_UNAUTH_003\t\tUSB\tUnauthorized\tlocalhost\n"
        "192.168.1.99:5555\t\tTCP\tMysteryState\tlocalhost\n"
    )
    targets = parse_targets_output(sample)
    assert [t.connection_state for t in targets] == [
        ConnectionState.ONLINE,
        ConnectionState.OFFLINE,
        ConnectionState.UNAUTHORIZED,
        ConnectionState.UNKNOWN,
    ]
    assert targets[0].transport == "USB"
    assert targets[0].host == "localhost"
    assert targets[0].serial_masked == "SERI***_001"


def test_classify_hdc_error_handles_rc_zero_fail_markers() -> None:
    assert (
        classify_hdc_error(
            0,
            "[Fail][E000002]The device unauthorized. Please check confirmation dialog.\n",
            "",
        )
        == HdcErrorKind.DEVICE_UNAUTHORIZED
    )
    assert (
        classify_hdc_error(
            0,
            "[Fail]Not match target founded, check connect-key please\n",
            "",
        )
        == HdcErrorKind.DEVICE_OFFLINE
    )
    assert (
        classify_hdc_error(
            0,
            "[Fail]Error opening file: /data/local/tmp/missing.json\n",
            "",
            is_file_transfer=True,
        )
        == HdcErrorKind.FILE_TRANSFER_FAILED
    )
    # Normal hidumper output containing "Failure: INIT" must NOT false-positive
    assert (
        classify_hdc_error(
            0,
            "State: AWAKE\n   Failure: INIT   Reason:    From: AWAKE   Time: 0\n",
            "",
        )
        == HdcErrorKind.NONE
    )
    # Non-zero exit with partial stdout must NOT be swallowed as success
    assert classify_hdc_error(1, "partial output before crash", "") == HdcErrorKind.COMMAND_FAILED


def test_subprocess_hdc_transport_real_timeout_kills_process_and_classifies_errors(
    tmp_path: Path,
) -> None:
    script = _make_executable_script(
        tmp_path / "fake_hdc_bin",
        """
import sys, time
args = sys.argv[1:]
if "hang" in args:
    time.sleep(30)
elif "unauth" in args:
    print("[Fail][E000002]The device unauthorized. Please check your device.")
    sys.exit(0)
elif "offline" in args:
    print("[Fail]Not match target founded, check connect-key please")
    sys.exit(0)
elif "partial_fail" in args:
    print("some stdout before error")
    sys.exit(1)
elif args == ["-v"]:
    print("Ver: 3.2.0c")
else:
    print("ARGS=" + "|".join(args))
""",
    )

    transport = SubprocessHdcTransport(hdc_path=script, default_timeout=5.0)
    client = HdcClient(transport=transport)

    assert client.version() == "3.2.0c"

    # Real timeout kills child process quickly and raises HdcTimeoutError
    with pytest.raises(HdcTimeoutError) as exc_info:
        transport.execute(["shell", "hang"], target="DEV001", timeout=0.2)
    assert exc_info.value.result is not None
    assert exc_info.value.result.error_kind == HdcErrorKind.TIMEOUT

    # Real rc=0 + E000002 unauthorized -> DeviceUnauthorizedError
    with pytest.raises(DeviceUnauthorizedError):
        client.shell("DEV001", ["unauth"])

    # Real rc=0 + Not match target founded -> DeviceOfflineError
    with pytest.raises(DeviceOfflineError):
        client.shell("DEV001", ["offline"])

    # Real rc=1 with partial stdout -> HdcCommandError (not swallowed!)
    with pytest.raises(HdcCommandError):
        client.shell("DEV001", ["partial_fail"])


def test_safe_shell_quoting_prevents_injection() -> None:
    cmd = build_safe_shell_command(["uitest", "uiInput", "inputText", "10", "20", "a; rm -rf /"])
    assert "'a; rm -rf /'" in cmd

    with pytest.raises(HdcInvalidArgumentError):
        build_safe_shell_command([])
    with pytest.raises(HdcInvalidArgumentError):
        build_safe_shell_command(["echo", "bad\ninjection"])
    with pytest.raises(HdcInvalidArgumentError):
        build_safe_shell_command(["echo", "bad\x00byte"])


def test_remote_tmp_path_validation_and_cleanup_on_exception(tmp_path: Path) -> None:
    path = make_remote_tmp_path("layout", ".json")
    assert path.startswith(REMOTE_TMP_PREFIX)
    assert validate_remote_tmp_path(path) == path

    for unsafe in [
        "/data/local/tmp/other.json",
        "/data/local/tmp/moyu_pilot_../etc/passwd",
        "/data/local/tmp/moyu_pilot_a;rm -rf /",
        "/etc/hosts",
    ]:
        with pytest.raises(HdcInvalidArgumentError):
            validate_remote_tmp_path(unsafe)

    dev = FakeDeviceState(serial="DEV_TMP_0001")
    fake = FakeHdcTransport([dev])
    client = HdcClient(transport=fake)

    with pytest.raises(RuntimeError, match="boom"):
        with client.remote_tmp_file(dev.serial, "layout", ".json") as remote_path:
            dev.remote_files[remote_path] = b'{"ok": true}'
            raise RuntimeError("boom")

    # Zero remote tmp file residue even after exception
    assert fake.all_remote_tmp_files() == {}

    # Also test file send/recv round-trip and recv error handling
    src = tmp_path / "payload.txt"
    src.write_text("hello harmony", encoding="utf-8")
    client.send_file(dev.serial, src, f"{REMOTE_TMP_PREFIX}upload.txt")
    dst = tmp_path / "downloaded.txt"
    client.recv_file(dev.serial, f"{REMOTE_TMP_PREFIX}upload.txt", dst)
    assert dst.read_text(encoding="utf-8") == "hello harmony"
    client.cleanup_remote_tmp(dev.serial, f"{REMOTE_TMP_PREFIX}upload.txt")
    assert fake.all_remote_tmp_files() == {}

    with pytest.raises(HdcFileTransferError):
        client.recv_file(dev.serial, f"{REMOTE_TMP_PREFIX}nonexistent.txt", tmp_path / "x.txt")


def test_hdc_client_always_passes_explicit_target_and_rejects_empty() -> None:
    dev = FakeDeviceState(serial="DEV_TARGET_01")
    fake = FakeHdcTransport([dev])
    client = HdcClient(transport=fake)

    assert client.get_param(dev.serial, "const.ohos.apiversion") == "23"
    assert fake.calls[-1][0] == dev.serial

    with pytest.raises(HdcInvalidArgumentError):
        client.shell("", ["echo", "hi"])
    with pytest.raises(HdcInvalidArgumentError):
        client.get_param(dev.serial, "bad key; rm -rf /")
    assert os.environ is not None
