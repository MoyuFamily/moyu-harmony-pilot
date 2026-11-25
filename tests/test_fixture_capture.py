"""Synthetic offline fixture redaction, read-only capture, and atomic output failures."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from moyu_harmony_pilot.actions.fixtures import capture_fixture, write_sanitized_fixture
from moyu_harmony_pilot.cli import SCHEMA_VERSION, main
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceProfile,
    HarmonyDevice,
)
from moyu_harmony_pilot.hdc import HdcClient, HdcError
from moyu_harmony_pilot.ui import (
    UiInvalidInputError,
    UiParseError,
    build_compact_snapshot,
    parse_layout_dict,
    parse_layout_json,
    redact_layout_dict,
    redact_layout_json,
)
from tests.fakes import FakeDeviceState, FakeHdcTransport


def _private_layout() -> dict[str, Any]:
    """Generate intentionally private-looking synthetic values, never a device sample."""
    return {
        "attributes": {
            "type": "root",
            "bounds": "[0,0][400,800]",
            "bundleName": "com.example.synthetic_private",
            "hostWindowId": "42",
            "displayId": 7,
            "pagePath": "pages/SyntheticPrivate",
            "abilityName": "PrivateAbility",
        },
        "private_metadata": "synthetic-secret-metadata",
        "children": [
            {
                "attributes": {
                    "type": "Button",
                    "bounds": "[10,20][100,80]",
                    "origBounds": [10, 20, 100, 80],
                    "bundleName": "com.example.synthetic_private",
                    "clickable": "true",
                    "enabled": True,
                    "id": "synthetic_private_button",
                    "key": "synthetic_private_button",
                    "text": "synthetic-private-label",
                    "description": "fixture@example.invalid",
                    "hint": "synthetic-private-label",
                    "windowId": "42",
                    "hashcode": "synthetic-secret-hash",
                    "accessibilityId": "synthetic-secret-id",
                },
                "children": [],
            },
            {
                "attributes": {
                    "type": "SyntheticPrivateWidget",
                    "bounds": {
                        "left": 10,
                        "top": 90,
                        "right": 200,
                        "bottom": 150,
                        "private": "synthetic-secret-bounds",
                    },
                    "key": "synthetic_private_other",
                    "bundleName": "com.example.synthetic_other",
                    "accessibilityText": "synthetic-private-accessibility",
                    "content": "private-content",
                    "selected": "false",
                    "focused": False,
                    "visible": "true",
                },
            },
        ],
    }


def _fake_device(
    tmp_path: Path, raw: str
) -> tuple[HarmonyDevice, FakeDeviceState, FakeHdcTransport]:
    state = FakeDeviceState("SYNTHETIC_FIXTURE_DEVICE", layouts=[raw])
    transport = FakeHdcTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport),
        DeviceProfile("fixture-lab", state.serial),
        state_dir=tmp_path / "state",
    )
    return device, state, transport


def _assert_capture_cleanup(transport: FakeHdcTransport) -> None:
    assert transport.all_remote_tmp_files() == {}
    for _, arguments, _ in transport.calls:
        if arguments[:2] == ("file", "recv"):
            local_path = Path(arguments[3])
            assert not local_path.exists()
            assert not local_path.parent.exists()


def test_default_redaction_removes_private_literals_without_mutating_input() -> None:
    original = _private_layout()
    untouched = copy.deepcopy(original)
    sanitized, summary = redact_layout_dict(original)
    assert original == untouched
    assert redact_layout_dict(original) == (sanitized, summary)
    output = json.dumps(sanitized, ensure_ascii=False, allow_nan=False)
    for literal in (
        "synthetic_private",
        "synthetic-private",
        "synthetic-secret",
        "fixture@example.invalid",
        "SyntheticPrivate",
        "PrivateAbility",
        "private-content",
        "com.example.synthetic_other",
    ):
        assert literal not in output
    before, after = parse_layout_dict(original), parse_layout_dict(sanitized)
    assert before.node_count == after.node_count == summary.node_count == 3
    assert before.screen_size == after.screen_size == (400, 800)
    assert [node.bounds for node in before.iter_nodes()] == [
        node.bounds for node in after.iter_nodes()
    ]
    assert [node.clickable for node in before.iter_nodes()] == [
        node.clickable for node in after.iter_nodes()
    ]
    first = after.root.children[0]
    assert first.id == first.key
    assert first.text == first.hint
    assert first.window_id == after.root.window_id
    assert first.bundle_name == after.root.bundle_name
    assert first.bundle_name != after.root.children[1].bundle_name
    assert summary.redacted_texts == 5
    assert summary.redacted_bundles == 3
    assert summary.redacted_ids >= 6
    assert summary.dropped_attributes == 3
    assert summary.manual_review_required is True
    assert json.loads(json.dumps(summary.to_dict())) == summary.to_dict()
    assert build_compact_snapshot(after).kept_elements == 2


@pytest.mark.parametrize(
    "key",
    [
        "bounds",
        "clickable",
        "text",
    ],
    ids=['bounds', 'clickable', 'text'],
)
@pytest.mark.parametrize(
    "value",
    [
        {"private": "synthetic-structural-secret"},
    ],
    ids=['nested-private-value'],
)
def test_structural_keys_never_bypass_redaction(key: str, value: Any) -> None:
    sanitized, _ = redact_layout_dict({"attributes": {key: value}, "children": []})
    assert "synthetic-structural-secret" not in json.dumps(sanitized)
    parse_layout_dict(sanitized)


def test_keep_lists_are_exact_validated_and_cannot_preserve_ids_or_custom_types() -> None:
    original = _private_layout()
    attrs = original["children"][0]["attributes"]
    attrs.update(text="Close", description="Close window", id="Close", type="PrivateWidget")
    sanitized, summary = redact_layout_dict(
        original,
        keep_texts=(value for value in ["Close", "PrivateWidget"]),
        keep_bundles=(value for value in ["com.example.synthetic_private"]),
    )
    cleaned = sanitized["children"][0]["attributes"]
    assert cleaned["text"] == "Close"
    assert cleaned["description"] != "Close window"
    assert cleaned["id"] != "Close"
    assert cleaned["type"] != "PrivateWidget"
    assert cleaned["bundleName"] == "com.example.synthetic_private"
    assert sanitized["children"][1]["attributes"]["bundleName"] != "com.example.synthetic_other"
    assert summary.manual_review_required is True


@pytest.mark.parametrize(
    "raw",
    [
        '{"attributes": {}, "children": "synthetic-private"}',
    ],
    ids=['invalid-private-children'],
)
def test_malformed_input_preserves_existing_destination_and_reports_no_raw_values(
    tmp_path: Path,
    raw: str,
) -> None:
    output = tmp_path / "fixture.json"
    output.write_bytes(b"existing fixture")
    output.chmod(0o640)
    with pytest.raises(UiParseError) as error:
        write_sanitized_fixture(raw, output)
    assert "synthetic-private" not in str(error.value)
    assert output.read_bytes() == b"existing fixture"
    assert output.stat().st_mode & 0o777 == 0o640
    assert sorted(path.name for path in tmp_path.iterdir()) == ["fixture.json"]


def test_capture_uses_only_readonly_dump_transfer_cleanup_and_returns_safe_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(_private_layout())
    device, state, transport = _fake_device(tmp_path, raw)
    destination = tmp_path / "fixtures" / "candidate.json"
    original_replace = os.replace

    def check_private_staging(source: str | Path, target: str | Path) -> None:
        staged = Path(source)
        assert staged.stat().st_mode & 0o777 == 0o600
        assert "synthetic-private" not in staged.read_text(encoding="utf-8")
        assert target == destination
        _assert_capture_cleanup(transport)
        original_replace(source, target)

    monkeypatch.setattr("moyu_harmony_pilot.actions.fixtures.os.replace", check_private_staging)
    result = capture_fixture(device, destination, timeout=7.5)
    assert result == {
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "redaction": redact_layout_json(raw)[1].to_dict(),
        "manual_review_required": True,
    }
    assert json.loads(json.dumps(result)) == result
    assert state.serial not in json.dumps(result)
    assert "synthetic-private" not in json.dumps(result)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    parse_layout_json(destination.read_text(encoding="utf-8"))
    assert list(destination.parent.glob(".fixture-*")) == []
    commands = [shlex.split(args[1]) for _, args, _ in transport.calls if args[0] == "shell"]
    assert len(commands) == 2
    assert commands[0][:2] == ["uitest", "dumpLayout"]
    assert commands[1][:2] == ["rm", "-f"]
    assert transport.calls[0][2] == 7.5
    assert not device.is_busy()
    _assert_capture_cleanup(transport)


@pytest.mark.parametrize(
    "failure",
    [
        "receive",
        "write",
    ],
    ids=['receive-failure', 'partial-write'],
)
@pytest.mark.parametrize(
    "existing",
    [
        True,
    ],
    ids=['existing-destination'],
)
def test_capture_failure_preserves_destination_cleans_temporary_files_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    existing: bool,
) -> None:
    device, state, transport = _fake_device(tmp_path, json.dumps(_private_layout()))
    destination = tmp_path / "candidate.json"
    if existing:
        destination.write_bytes(b"previous fixture")
        destination.chmod(0o640)
    if failure == "capture":
        state.timeout_on_cmd_prefix = "uitest dumpLayout"
    elif failure == "receive":
        state.fail_recv = True
    elif failure == "parse":
        state.layouts = ["{invalid synthetic-private-layout"]
    elif failure == "write":
        create_file = tempfile.NamedTemporaryFile

        @contextmanager
        def partial_write(*args: Any, **kwargs: Any) -> Iterator[Any]:
            with create_file(*args, **kwargs) as stream:
                original_write = stream.write

                def fail_write(content: bytes) -> None:
                    original_write(content[:20])
                    raise OSError("Injected partial write failure.")

                monkeypatch.setattr(stream, "write", fail_write)
                yield stream

        monkeypatch.setattr(
            "moyu_harmony_pilot.actions.fixtures.tempfile.NamedTemporaryFile", partial_write
        )
    elif failure in {"permissions", "fsync", "replace"}:

        def fail(*args: Any, **kwargs: Any) -> None:
            raise OSError("Injected output failure.")

        operation = "fchmod" if failure == "permissions" else failure
        monkeypatch.setattr(f"moyu_harmony_pilot.actions.fixtures.os.{operation}", fail)
    keep_texts = ["fixture@example.invalid"] if failure == "keep_list" else []
    with pytest.raises((HdcError, UiParseError, UiInvalidInputError, OSError)):
        capture_fixture(device, destination, keep_texts=keep_texts)
    if existing:
        assert destination.read_bytes() == b"previous fixture"
        assert destination.stat().st_mode & 0o777 == 0o640
    else:
        assert not destination.exists()
    assert list(tmp_path.glob(".fixture-*")) == []
    assert not device.is_busy()
    _assert_capture_cleanup(transport)


def test_capture_respects_device_lock(tmp_path: Path) -> None:
    device, _, transport = _fake_device(tmp_path, json.dumps(_private_layout()))
    destination = tmp_path / "candidate.json"
    with device.acquire_lock(), pytest.raises(DeviceBusyError):
        capture_fixture(device, destination)
    assert not destination.exists()
    assert transport.calls == []


def test_atomic_success_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated.txt"
    target.write_bytes(b"untouched")
    destination = tmp_path / "candidate.json"
    destination.symlink_to(target)
    write_sanitized_fixture(json.dumps(_private_layout()), destination)
    assert not destination.is_symlink()
    assert target.read_bytes() == b"untouched"
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "failure, expected_code",
    [
        ("keep_list", 2),
    ],
    ids=['private-allowlist-rejected'],
)
def test_fixture_cli_emits_one_sanitized_json_envelope(
    tmp_path: Path,
    failure: str | None,
    expected_code: int,
) -> None:
    raw = (
        "{broken synthetic-private-layout" if failure == "parse" else json.dumps(_private_layout())
    )
    _, _, transport = _fake_device(tmp_path, raw)
    destination = tmp_path / "fixture.json"
    arguments = [
        "fixture",
        "capture",
        "--out",
        str(destination),
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
    ]
    if failure == "keep_list":
        arguments += ["--keep-text", "fixture@example.invalid"]
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(arguments, transport=transport, cwd=tmp_path, env={}, stdout=stdout, stderr=stderr)
    output = stdout.getvalue()
    assert code == expected_code, output
    assert len(output.splitlines()) == 1
    envelope = json.loads(output)
    assert set(envelope) == {"schema_version", "ok", "command", "data", "error"}
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["ok"] is (failure is None)
    assert "synthetic-private" not in output
    assert "example.invalid" not in output
    assert stderr.getvalue() == ""
    if failure is None:
        assert envelope["data"]["path"] == str(destination)
        assert envelope["data"]["manual_review_required"] is True
        assert envelope["data"]["redaction"]["manual_review_required"] is True
    else:
        assert not destination.exists()
    _assert_capture_cleanup(transport)
