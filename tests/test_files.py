"""Offline CLI contracts for device file queries and atomic media exports."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import stat
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from moyu_harmony_pilot.actions.validation import validate_value
from moyu_harmony_pilot.cli.operations import OPERATION_SCHEMAS
from moyu_harmony_pilot.device.lock import is_device_busy
from moyu_harmony_pilot.hdc import CommandResult, HdcErrorKind
from moyu_harmony_pilot.testing import offline_guard
from tests.fakes import FakeDeviceState, FakeHdcTransport, make_minimal_png
from tests.test_cli import _parse_single_json_envelope, _run_cli


class FileTransport(FakeHdcTransport):
    """Extend the shared fake with media lookup, staging and interrupted transfers."""

    def __init__(self, state: FakeDeviceState, lock_dir: Path) -> None:
        super().__init__([state])
        self.lock_dir = lock_dir
        self.query_results: dict[str, list[str]] = {}
        self.media_files: dict[str, bytes] = {}
        self.received_paths: list[Path] = []
        self.staging_modes: list[int] = []
        self.lock_observations: list[bool] = []
        self.partial_recv = False
        self.receive_observer: Callable[[Path], None] | None = None

    @property
    def shell_commands(self) -> list[list[str]]:
        return [
            shlex.split(args[1])
            for _, args, _ in self.calls
            if args[0] == "shell"
        ]

    def execute(
        self, args: Sequence[str], *, target: str | None = None, timeout: float = 15.0,
    ) -> CommandResult:
        argv = tuple(args)
        if target is not None:
            self.lock_observations.append(is_device_busy(target, self.lock_dir))
        if argv[:2] != ("file", "recv"):
            return super().execute(args, target=target, timeout=timeout)

        destination = Path(argv[3])
        self.received_paths.append(destination)
        self.staging_modes.append(stat.S_IMODE(destination.parent.stat().st_mode))
        if self.partial_recv:
            assert target is not None
            self.calls.append((target, argv, timeout))
            destination.write_bytes(self.devices[target].remote_files[argv[2]][:7])
            result = CommandResult(
                argv, 1, "", f"Synthetic interrupted receive for {target}", 1,
                HdcErrorKind.FILE_TRANSFER_FAILED,
            )
        else:
            result = super().execute(args, target=target, timeout=timeout)
        if self.receive_observer is not None:
            self.receive_observer(destination)
        return result

    def _handle_shell(
        self, dev: FakeDeviceState, raw_args: tuple[str, ...], shell_cmd: str, timeout: float,
    ) -> CommandResult:
        tokens = shlex.split(shell_cmd)
        if tokens[:1] == ["stat"]:
            assert len(tokens) == 4 and tokens[1:3] == ["-c", "%f %s"]
            contents = dev.remote_files[tokens[3]]
            output = f"{stat.S_IFREG | 0o600:x} {len(contents)}\n"
            return CommandResult(raw_args, 0, output, "", 1)
        if tokens[:2] == ["mediatool", "query"]:
            assert len(tokens) == 4 and tokens[3] == "-u"
            uris = self.query_results[tokens[2]]
            rows = "\n".join(json.dumps(uri, ensure_ascii=False) for uri in uris)
            output = f"find {len(uris)} result:\nuri\n{rows}\n"
            return CommandResult(raw_args, 0, output, "", 1)
        if tokens[:2] == ["mediatool", "recv"]:
            assert len(tokens) == 4
            source, remote = tokens[2:]
            assert remote.startswith("/data/local/tmp/moyu_pilot_export_")
            assert remote.endswith(".bin")
            assert Path(remote).parent == Path("/data/local/tmp")
            dev.remote_files[remote] = self.media_files[source]
            return CommandResult(raw_args, 0, f"Table Name: Photos\n{remote}\n", "", 1)
        return super()._handle_shell(dev, raw_args, shell_cmd, timeout)


@pytest.fixture
def files_setup(tmp_path: Path) -> Iterator[tuple[list[str], FakeDeviceState, FileTransport]]:
    state = FakeDeviceState("SYNTHETIC_FILE_EXPORT_DEVICE")
    state_dir = tmp_path / "state"
    transport = FileTransport(state, state_dir / "locks")
    config = tmp_path / "devices.local.json"
    config.write_text(json.dumps({"devices": {"phone": state.serial}}), encoding="utf-8")
    config.chmod(0o600)
    flags = [
        "--json", "--config", str(config), "--state-dir", str(state_dir), "files",
    ]
    with offline_guard():
        yield flags, state, transport


def _assert_staging_cleaned(transport: FileTransport, destination: Path) -> None:
    assert len(transport.received_paths) == 1
    receive_index = next(
        index for index, (_, args, _) in enumerate(transport.calls)
        if args[:2] == ("file", "recv")
    )
    metadata = transport.calls[receive_index - 1][1]
    remote = transport.calls[receive_index][1][2]
    assert metadata[0] == "shell"
    assert shlex.split(metadata[1]) == ["stat", "-c", "%f %s", remote]
    staged = transport.received_paths[0]
    assert staged.parent.parent == destination.parent
    assert staged.parent.name.startswith(".moyu-export-")
    assert not staged.parent.exists()
    assert list(destination.parent.glob(".moyu-export-*")) == []
    if os.name == "posix":
        assert transport.staging_modes == [0o700]
    assert transport.lock_observations and all(transport.lock_observations)
    assert not is_device_busy(next(iter(transport.devices)), transport.lock_dir)


def _assert_export_result(
    envelope: dict[str, Any], *, source: str, destination: Path, contents: bytes,
    transport_name: str, command: str = "files",
) -> None:
    assert envelope["ok"] is True and envelope["command"] == command
    assert envelope["error"] is None
    data = envelope["data"]["data"] if command == "run" else envelope["data"]
    assert data == {
        "alias": "phone",
        "source": source,
        "path": str(destination),
        "size_bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
        "transport": transport_name,
        "cleanup_pending": [],
    }
    schema = OPERATION_SCHEMAS["files"]["subcommands"]["export"]["result_schema"]
    assert set(schema["properties"]) == set(data)
    assert set(schema["required"]) == set(data)
    validate_value(data, schema)
    assert destination.read_bytes() == contents
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_files_export_png_without_hardlinks_preserves_bytes_and_schema(
    files_setup, tmp_path, monkeypatch,
):
    flags, state, transport = files_setup
    source = "/data/local/tmp/synthetic_capture.png"
    contents = make_minimal_png(width=2, height=3) + b"\x00\xffsynthetic-pixels"
    state.remote_files[source] = contents
    destination = tmp_path / "capture.png"

    def unsupported_link(source_path: Path, destination_path: Path) -> None:
        assert source_path.parent.parent == destination_path.parent
        raise OSError(errno.EOPNOTSUPP, "Synthetic volume without hard links")

    monkeypatch.setattr("moyu_harmony_pilot.device.files.os.link", unsupported_link)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])

    code, out, err = _run_cli(
        [*flags[:-1], "run", "--device", "phone", "--action", "files.export",
         "--params-json", json.dumps({"source": source, "out": str(destination)})],
        transport=transport, env={},
    )

    assert code == 0, out
    assert err == "" and state.serial not in out + err
    _assert_export_result(
        _parse_single_json_envelope(out), source=source, destination=destination,
        contents=contents, transport_name="hdc", command="run",
    )
    _assert_staging_cleaned(transport, destination)
    assert transport.shell_commands == [
        ["hidumper", "-s", "BatteryService", "-a", "-i"],
        ["hidumper", "-s", "PowerManagerService", "-a", "-s"],
        ["stat", "-c", "%f %s", source],
    ]
    assert state.remote_files == {source: contents}
    receives = [args for _, args, _ in transport.calls if args[:2] == ("file", "recv")]
    assert receives == [("file", "recv", source, str(transport.received_paths[0]))]
    options = OPERATION_SCHEMAS["files"]["subcommands"]["export"]["options"]
    assert options["--timeout"]["default"] == 300


def test_files_query_quoted_name_then_export_media_uri_mp4(files_setup, tmp_path):
    flags, state, transport = files_setup
    name = "合成 旅途' $片段.mp4"
    uri = f"file://media/Photo/1/VID_0001/{quote(name, safe='')}"
    other_uri = f"file://media/Photo/2/VID_0002/{quote(name, safe='')}"
    contents = (
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        b"\x00\x00\x00\x10mdat\x00\xffmoyu!!"
    )
    transport.query_results[name] = [uri, other_uri]
    transport.media_files[uri] = contents
    destination = tmp_path / "clip.mp4"

    code, out, err = _run_cli(
        [*flags, "query", "--device", "phone", "--name", name,
         "--limit", "1", "--timeout", "17"],
        transport=transport, env={},
    )

    assert code == 0, out
    assert err == "" and state.serial not in out + err
    query = _parse_single_json_envelope(out)
    assert query["ok"] is True and query["command"] == "files"
    assert query["error"] is None
    assert query["data"] == {
        "alias": "phone", "name": name, "count": 1, "total": 2,
        "truncated": True, "uris": [uri],
    }
    schema = OPERATION_SCHEMAS["files"]["subcommands"]["query"]["result_schema"]
    assert set(schema["properties"]) == set(query["data"])
    assert set(schema["required"]) == set(query["data"])
    validate_value(query["data"], schema)
    options = OPERATION_SCHEMAS["files"]["subcommands"]["query"]["options"]
    assert options["--limit"]["default"] == 100
    assert options["--timeout"]["default"] == 30
    assert transport.shell_commands == [["mediatool", "query", name, "-u"]]
    query_timeouts = [timeout for target, _, timeout in transport.calls if target is not None]
    assert len(query_timeouts) == 1 and 0 < query_timeouts[0] <= 17

    code, out, err = _run_cli(
        [*flags, "export", "--device", "phone", "--source", query["data"]["uris"][0],
         "--out", str(destination), "--timeout", "41"],
        transport=transport, env={},
    )

    assert code == 0, out
    assert err == "" and state.serial not in out + err
    _assert_export_result(
        _parse_single_json_envelope(out), source=uri, destination=destination,
        contents=contents, transport_name="mediatool",
    )
    _assert_staging_cleaned(transport, destination)
    query_command, receive, metadata, cleanup = transport.shell_commands
    assert query_command == ["mediatool", "query", name, "-u"]
    assert receive[:3] == ["mediatool", "recv", uri]
    remote = receive[3]
    assert metadata == ["stat", "-c", "%f %s", remote]
    assert cleanup[0] == "rm" and remote in cleanup[1:]
    receives = [args for _, args, _ in transport.calls if args[:2] == ("file", "recv")]
    assert receives == [("file", "recv", remote, str(transport.received_paths[0]))]
    receive_timeouts = [
        timeout for _, args, timeout in transport.calls if args[:2] == ("file", "recv")
    ]
    assert len(receive_timeouts) == 1 and 0 < receive_timeouts[0] <= 41
    assert state.remote_files == {}
    assert transport.media_files == {uri: contents}


def test_files_export_existing_destination_requires_atomic_overwrite(files_setup, tmp_path):
    flags, state, transport = files_setup
    source = "/data/local/tmp/synthetic_replacement.png"
    contents = make_minimal_png(width=4, height=5)
    state.remote_files[source] = contents
    destination = tmp_path / "capture.png"
    original = b"synthetic original bytes\x00\xff"
    destination.write_bytes(original)
    original_inode = destination.stat().st_ino
    argv = [
        *flags, "export", "--device", "phone", "--source", source,
        "--out", str(destination),
    ]

    code, out, err = _run_cli(argv, transport=transport, env={})

    assert code == 2, out
    assert err == ""
    rejected = _parse_single_json_envelope(out)
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "configuration_error"
    assert destination.read_bytes() == original
    assert transport.received_paths == []
    assert transport.shell_commands == []
    assert list(tmp_path.glob(".moyu-export-*")) == []

    def observe_receive(staged: Path) -> None:
        assert destination.read_bytes() == original
        assert staged != destination and staged.read_bytes() == contents

    transport.receive_observer = observe_receive
    code, out, err = _run_cli([*argv, "--overwrite"], transport=transport, env={})

    assert code == 0, out
    assert err == ""
    _assert_export_result(
        _parse_single_json_envelope(out), source=source, destination=destination,
        contents=contents, transport_name="hdc",
    )
    _assert_staging_cleaned(transport, destination)
    if os.name == "posix":
        assert destination.stat().st_ino != original_inode
    assert state.remote_files == {source: contents}


def test_files_export_partial_media_receive_preserves_original_and_cleans_up(files_setup, tmp_path):
    flags, state, transport = files_setup
    source = "/storage/media/100/local/files/Photo/2/IMG_0002.png"
    contents = make_minimal_png(width=6, height=7)
    transport.media_files[source] = contents
    transport.partial_recv = True
    destination = tmp_path / "capture.png"
    original = b"synthetic previous export\x00\xff"
    destination.write_bytes(original)

    def observe_partial_receive(staged: Path) -> None:
        assert staged.read_bytes() == contents[:7]
        assert destination.read_bytes() == original

    transport.receive_observer = observe_partial_receive
    code, out, err = _run_cli(
        [*flags, "export", "--device", "phone", "--source", source,
         "--out", str(destination), "--overwrite"],
        transport=transport, env={},
    )

    assert code == 1, out
    assert err == "" and state.serial not in out + err
    envelope = _parse_single_json_envelope(out)
    assert envelope["ok"] is False and envelope["command"] == "files"
    assert envelope["error"]["code"] == "hdc_file_transfer_failed"
    assert envelope["error"]["message"] == "File export stopped."
    assert envelope["error"]["retryable"] is True
    assert "Synthetic interrupted receive" not in out + err
    assert destination.read_bytes() == original
    _assert_staging_cleaned(transport, destination)
    receive, metadata, cleanup = transport.shell_commands
    assert receive[:3] == ["mediatool", "recv", source]
    remote = receive[3]
    assert metadata == ["stat", "-c", "%f %s", remote]
    assert cleanup[0] == "rm" and remote in cleanup[1:]
    receives = [args for _, args, _ in transport.calls if args[:2] == ("file", "recv")]
    assert receives == [("file", "recv", remote, str(transport.received_paths[0]))]
    assert state.remote_files == {}
    assert transport.media_files == {source: contents}


def test_files_export_traversing_media_uri_fails_before_device_access(files_setup, tmp_path):
    flags, _, transport = files_setup
    source = "file://media/Photo/1/../../synthetic.png"
    destination = tmp_path / "capture.png"

    code, out, err = _run_cli(
        [*flags, "export", "--device", "phone", "--source", source,
         "--out", str(destination)],
        transport=transport, env={},
    )

    assert code == 2, out
    assert err == ""
    envelope = _parse_single_json_envelope(out)
    assert envelope["ok"] is False and envelope["command"] == "files"
    assert envelope["error"]["code"] == "configuration_error"
    assert envelope["data"] is None
    assert transport.calls == []
    assert not destination.exists()
    assert list(tmp_path.glob(".moyu-export-*")) == []
