"""Synthetic HarmonyOS transport fixtures using only the standard library.

Commands are recorded as ``(target, args, timeout)`` tuples. Unsupported commands
fail explicitly. Layouts and remote files live in memory, while file transfers
use the temporary local paths supplied by the real drivers.
"""

from __future__ import annotations

import json
import math
import shlex
import tempfile
import unittest
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from moyu_harmony_pilot.device import DeviceProfile, HarmonyDevice
from moyu_harmony_pilot.hdc import CommandResult, HdcClient, HdcErrorKind, HdcTimeoutError

HdcCall = tuple[str | None, tuple[str, ...], float]


def make_layout(
    *elements: Mapping[str, Any],
    bundle_name: str = "com.example.demo",
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Build synthetic layout JSON from control attribute mappings.

    Each control defaults to an enabled, visible button in a separate row.
    Supply ``id``, ``type``, ``checked``, ``bounds`` or other dumpLayout attributes
    as needed. Assign arbitrary nested JSON directly to ``FakeDeviceState.layouts``
    when a scenario needs a more complex hierarchy.
    """
    children = []
    for index, attributes in enumerate(elements):
        top = 40 + index * 120
        children.append({
            "attributes": {
                "bundleName": bundle_name,
                "bounds": f"[40,{top}][{width - 40},{top + 80}]",
                "type": "Button",
                "clickable": "true",
                "enabled": "true",
                "visible": "true",
                **attributes,
            },
            "children": [],
        })
    return json.dumps({
        "attributes": {
            "bounds": f"[0,0][{width},{height}]",
            "bundleName": bundle_name,
            "type": "root",
            "visible": "true",
            "enabled": "true",
        },
        "children": children,
    })


@dataclass
class FakeDeviceState:
    """Mutable, per-target fixture data, including deterministic failure injection.

    Layouts advance at most once per click or dump when explicitly enabled.
    ``timeout_after_mutation`` applies the synthetic effect before losing its
    acknowledgement, allowing realistic uncertain-submission tests. Exact
    ``shell_results`` overrides support additional commands without subprocesses.
    """

    serial: str = "SYNTHETIC_PLUGIN_DEVICE"
    status: str = "Connected"
    battery_percent: int | None = 72
    temperature_c: float = 30.0
    screen_on: bool = True
    foreground_bundle: str = "com.example.demo"
    layouts: list[str] = field(default_factory=lambda: [make_layout()])
    layout_index: int = 0
    advance_layout_on_click: bool = False
    advance_layout_on_dump: bool = False
    remote_files: dict[str, bytes] = field(default_factory=dict)
    fail_recv: bool = False
    fail_concurrent_uitest: bool = False
    timeout_on_cmd_prefix: str | None = None
    timeout_after_mutation: bool = False
    shell_results: dict[tuple[str, ...], CommandResult | Exception] = field(default_factory=dict)

    def advance_layout(self) -> None:
        self.layout_index = min(self.layout_index + 1, len(self.layouts) - 1)


def _readonly_call(args: tuple[str, ...]) -> bool:
    if args in {("-v",), ("version",), ("list", "targets"), ("list", "targets", "-v")}:
        return True
    if args[:2] == ("file", "recv"):
        return True
    if len(args) != 2 or args[0] != "shell":
        return False
    try:
        tokens = tuple(shlex.split(args[1]))
    except ValueError:
        return False
    if tokens[:2] in {
        ("param", "get"), ("bm", "dump"), ("uitest", "--version"),
        ("uitest", "dumpLayout"), ("uitest", "screenCap"),
    }:
        return True
    if tokens[:1] in {("hidumper",), ("snapshot_display",)}:
        return True
    # Driver-managed observation artifacts are allowed during read-only stages.
    return (
        len(tokens) == 3 and tokens[:2] == ("rm", "-f")
        and tokens[2].startswith("/data/local/tmp/moyu_pilot_")
        and "/" not in tokens[2].removeprefix("/data/local/tmp/")
    )


class FakeHdcTransport:
    """In-memory HdcTransport with call recording and fail-closed command handling.

    ``mutating_calls`` includes attempted UI input, app/power commands, file sends,
    and unsupported commands. Layout capture, receive and managed cleanup are
    observation work. This conservatively catches unexpected plugin operations.
    """

    def __init__(self, devices: Sequence[FakeDeviceState] = ()) -> None:
        self.devices = {state.serial: state for state in devices}
        if len(self.devices) != len(devices):
            raise ValueError("Fake target serials must be unique.")
        self.calls: list[HdcCall] = []

    @property
    def mutating_calls(self) -> list[HdcCall]:
        return [call for call in self.calls if not _readonly_call(call[1])]

    def all_remote_tmp_files(self) -> dict[str, list[str]]:
        result = {}
        for serial, state in self.devices.items():
            paths = sorted(
                path for path in state.remote_files
                if path.startswith("/data/local/tmp/moyu_pilot_")
            )
            if paths:
                result[serial] = paths
        return result

    def execute(
        self, args: Sequence[str], *, target: str | None = None, timeout: float = 15.0,
    ) -> CommandResult:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Fake command timeout must be finite and positive.")
        argv = tuple(args)
        self.calls.append((target, argv, timeout))
        if argv in {("-v",), ("version",)}:
            return CommandResult(argv, 0, "Ver: 3.2.0c\n", "", 1)
        if argv in {("list", "targets"), ("list", "targets", "-v")}:
            lines = [
                f"{state.serial}\tUSB\t{state.status}\tlocalhost"
                for state in self.devices.values()
            ]
            return CommandResult(argv, 0, "\n".join(lines) or "[Empty]", "", 1)
        state = self.devices.get(target or "")
        if state is None or state.status.lower() not in {"connected", "online"}:
            unauthorized = state is not None and state.status.lower() == "unauthorized"
            kind = HdcErrorKind.DEVICE_UNAUTHORIZED if unauthorized else HdcErrorKind.DEVICE_OFFLINE
            return CommandResult(argv, 1, "", kind.value, 1, kind)
        if len(argv) == 4 and argv[:2] == ("file", "recv"):
            if state.fail_recv or argv[2] not in state.remote_files:
                return CommandResult(
                    argv, 1, "", "Synthetic file receive failed", 1,
                    HdcErrorKind.FILE_TRANSFER_FAILED,
                )
            Path(argv[3]).write_bytes(state.remote_files[argv[2]])
            return CommandResult(argv, 0, "FileTransfer finish\n", "", 1)
        if len(argv) == 4 and argv[:2] == ("file", "send"):
            state.remote_files[argv[3]] = Path(argv[2]).read_bytes()
            if state.timeout_after_mutation:
                self._timeout(argv, timeout)
            return CommandResult(argv, 0, "FileTransfer finish\n", "", 1)
        if len(argv) == 2 and argv[0] == "shell":
            return self._shell(state, argv, timeout)
        return CommandResult(argv, 1, "", f"Unsupported fake hdc command: {argv!r}", 1)

    @staticmethod
    def _timeout(argv: tuple[str, ...], timeout: float) -> None:
        result = CommandResult(argv, -9, "", "", int(timeout * 1000), HdcErrorKind.TIMEOUT)
        raise HdcTimeoutError("Synthetic hdc timeout.", result=result)

    def _shell(
        self, state: FakeDeviceState, argv: tuple[str, ...], timeout: float,
    ) -> CommandResult:
        command = argv[1]
        if state.timeout_on_cmd_prefix and command.startswith(state.timeout_on_cmd_prefix):
            self._timeout(argv, timeout)
        try:
            tokens = tuple(shlex.split(command))
        except ValueError:
            return CommandResult(argv, 1, "", "Invalid fake shell quoting", 1)
        override = state.shell_results.get(tokens)
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        output = ""
        if tokens[:2] == ("rm", "-f") and _readonly_call(argv):
            state.remote_files.pop(tokens[2], None)
        elif tokens[:2] == ("param", "get") and len(tokens) == 3:
            values = {
                "const.product.model": "SYNTHETIC-HARMONY",
                "const.product.software.version": "Synthetic 6.0",
                "const.ohos.apiversion": "20",
                "const.ohos.fullname": "OpenHarmony-6.0",
            }
            output = values.get(tokens[2], "")
        elif tokens[:3] == ("hidumper", "-s", "BatteryService"):
            output = f"temperature: {round(state.temperature_c * 10)}\nchargingStatus: 1\n"
            if state.battery_percent is not None:
                output += f"capacity: {state.battery_percent}\n"
        elif tokens[:3] == ("hidumper", "-s", "PowerManagerService"):
            output = "Current State: " + ("AWAKE" if state.screen_on else "INACTIVE")
        elif tokens[:3] == ("hidumper", "-s", "WindowManagerService"):
            output = f"{state.foreground_bundle} 0 100 42 1\nFocus window: 42\n"
        elif tokens == ("uitest", "--version"):
            output = "6.0.0"
        elif tokens[:1] == ("uitest",) and state.fail_concurrent_uitest:
            return CommandResult(
                argv, 1, "Error: uitest-api does not allow calling concurrently", "", 1,
            )
        elif tokens[:2] == ("uitest", "dumpLayout") and "-p" in tokens:
            if not 0 <= state.layout_index < len(state.layouts) or tokens[-1] == "-p":
                return CommandResult(argv, 1, "", "Invalid fake layout fixture or path", 1)
            path = tokens[tokens.index("-p") + 1]
            state.remote_files[path] = state.layouts[state.layout_index].encode("utf-8")
            if state.advance_layout_on_dump:
                state.advance_layout()
            output = f"DumpLayout saved to:{path}\n"
        elif tokens[:2] == ("uitest", "uiInput") and len(tokens) >= 3:
            if tokens[2] not in {
                "click", "doubleClick", "longClick", "swipe", "fling", "drag",
                "dircFling", "inputText", "keyEvent",
            }:
                return CommandResult(argv, 1, "", "Unsupported fake UI input", 1)
            if state.advance_layout_on_click and tokens[2] == "click":
                state.advance_layout()
            output = "ok"
        elif tokens in {("power-shell", "wakeup"), ("power-shell", "suspend")}:
            state.screen_on = tokens[1] == "wakeup"
        elif tokens[:2] == ("aa", "start") and "-b" in tokens:
            state.foreground_bundle = tokens[tokens.index("-b") + 1]
            output = "start ability successfully."
        elif tokens[:2] == ("aa", "force-stop") and len(tokens) == 3:
            if state.foreground_bundle == tokens[2]:
                state.foreground_bundle = ""
        else:
            return CommandResult(argv, 1, "", f"Unsupported fake shell command: {command}", 1)
        if state.timeout_after_mutation and not _readonly_call(argv):
            self._timeout(argv, timeout)
        return CommandResult(argv, 0, output, "", 1)


@dataclass(frozen=True)
class FakeDevice:
    """A real controller paired with its mutable fixture and recording transport."""

    device: HarmonyDevice
    state: FakeDeviceState
    transport: FakeHdcTransport


def make_fake_device(
    state_dir: str | Path, *, state: FakeDeviceState | None = None,
    alias: str = "test-phone", max_temperature_c: float = 42.0,
) -> FakeDevice:
    """Build real locking/UI/telemetry over an in-memory transport in an isolated directory."""
    fixture = FakeDeviceState() if state is None else state
    transport = FakeHdcTransport([fixture])
    device = HarmonyDevice(
        HdcClient(transport), DeviceProfile(alias, fixture.serial, max_temperature_c),
        state_dir=state_dir,
    )
    return FakeDevice(device, fixture, transport)


@contextmanager
def offline_guard() -> Iterator[None]:
    """Reject subprocesses, DNS and socket connection/send calls during a test.

    Patches are process-wide and nest safely. Use in single-threaded contract tests.
    """
    with ExitStack() as stack:
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.socket.send", "socket.socket.sendall", "socket.create_connection",
            "socket.getaddrinfo", "subprocess.Popen", "os.system",
        ):
            stack.enter_context(patch(
                target, side_effect=AssertionError("Use offline device and HTTP fakes."),
            ))
        yield


class OfflineTestCase(unittest.TestCase):
    """Optional unittest base with temporary state and public fake device helpers."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(offline_guard())
        self.state_dir = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="pilot-plugin-contract-",
        )))
        self.fake = make_fake_device(self.state_dir)
        self.device = self.fake.device
        self.device_state = self.fake.state
        self.transport = self.fake.transport
