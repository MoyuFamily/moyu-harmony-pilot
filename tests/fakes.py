"""Reusable offline FakeHdcTransport and HarmonyOS 6.1 golden fixture builders."""

from __future__ import annotations

import json
import shlex
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.hdc import CommandResult, HdcErrorKind, HdcTimeoutError

FIXTURES_DIR = Path(__file__).parent / "fixtures"

BATTERY_61_CHARGING = """
-------------------------------[ability]-------------------------------


----------------------------------BatteryService----------------------------------
Current time: 2026-11-19 22:43:46.366
capacity: 72 
batteryLevel: 3 
chargingStatus: 1 
healthState: 1 
pluggedType: 2 
voltage: 4210571 
present: 1 
technology: Li-poly 
nowCurrent: 1241 
currentAverage: 1202 
totalEnergy: 4767 
remainingEnergy: 3271 
remainingChargeTime: 5734512 
temperature: 300 
chargeType: 1 
"""

BATTERY_61_OVERHEATED = """
----------------------------------BatteryService----------------------------------
capacity: 85
chargingStatus: 1
pluggedType: 2
voltage: 4310000
present: 1
temperature: 435
"""

POWER_61_AWAKE = """
-------------------------------[ability]-------------------------------


----------------------------------PowerManagerService----------------------------------
POWER STATE DUMP:
Current State: AWAKE  Reason: 30  Time: 386069
ScreenOffTime: Timeout=30000ms
DUMP DETAILS:
Last Screen On: 443234
Last Screen Off: 209595
"""

POWER_61_INACTIVE = """
----------------------------------PowerManagerService----------------------------------
POWER STATE DUMP:
Current State: INACTIVE  Reason: 4  Time: 512000
ScreenOffTime: Timeout=30000ms
"""

WMS_61_FOCUS = """
----------------------------------WindowManagerService----------------------------------
WindowName           DisplayId Pid     WinId Type Mode Flag ZOrder [ x    y    w    h    ]
com.example.demo0    0         4210    42    1    1    0    100    [ 0    0    1260 2720 ]
SCBStatusBar1        0         1100    12    2108 1    0    200    [ 0    0    1260 120  ]
Focus window: 42
"""


def make_minimal_jpeg(width: int = 1260, height: int = 2720) -> bytes:
    """Create a valid minimal JPEG byte sequence with an SOF0 header of `(width, height)`."""
    components = b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof0_payload = struct.pack(">BHHB", 8, height, width, 3) + components
    sof0_len = struct.pack(">H", len(sof0_payload) + 2)
    return b"\xff\xd8" + b"\xff\xc0" + sof0_len + sof0_payload + b"\xff\xd9"


def make_minimal_png(width: int = 1260, height: int = 2720) -> bytes:
    """Create a minimal PNG header byte sequence carrying `(width, height)` in IHDR."""
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"


def load_home_layout_json() -> str:
    return (FIXTURES_DIR / "harmony_61_home_layout.json").read_text(encoding="utf-8")


def build_large_harmony_layout(num_rows: int = 140) -> dict[str, Any]:
    """Generate a >120 KB realistic nested HarmonyOS NEXT layout with noise and real controls."""
    children: list[dict[str, Any]] = []
    # 12 realistic visible interactive cards
    for idx in range(12):
        top = 140 + idx * 180
        bottom = top + 150
        children.append(
            {
                "attributes": {
                    "bounds": f"[48,{top}][1212,{bottom}]",
                    "bundleName": "com.example.large",
                    "clickable": "true",
                    "enabled": "true",
                    "id": f"card_{idx}",
                    "text": "",
                    "type": "Row",
                    "visible": "true",
                },
                "children": [
                    {
                        "attributes": {
                            "bounds": f"[64,{top + 16}][800,{top + 80}]",
                            "bundleName": "com.example.large",
                            "clickable": "false",
                            "enabled": "true",
                            "text": f"Feature Item {idx}",
                            "type": "Text",
                            "visible": "true",
                        },
                        "children": [],
                    },
                    {
                        "attributes": {
                            "bounds": f"[840,{top + 30}][1180,{bottom - 30}]",
                            "bundleName": "com.example.large",
                            "clickable": "true",
                            "enabled": "true",
                            "id": f"action_{idx}",
                            "text": f"Open {idx}",
                            "type": "Button",
                            "visible": "true",
                        },
                        "children": [],
                    },
                ],
            }
        )

    # Add deep decorative empty container & invisible noise typical of 146KB real dumps
    for idx in range(num_rows):
        children.append(
            {
                "attributes": {
                    "accessibilityId": f"9000{idx}",
                    "bounds": f"[0,{idx}][1260,{idx + 2}]",
                    "bundleName": "com.example.large",
                    "checkable": "false",
                    "checked": "false",
                    "clickable": "false",
                    "description": "",
                    "enabled": "true",
                    "focused": "false",
                    "hashcode": f"0xdeadbeef{idx:04x}",
                    "hint": "",
                    "hostWindowId": "42",
                    "id": "",
                    "key": "",
                    "longClickable": "false",
                    "origBounds": f"[0,{idx}][1260,{idx + 2}]",
                    "pagePath": "pages/LargeDashboard",
                    "scrollable": "false",
                    "selected": "false",
                    "text": "",
                    "type": "Column",
                    "visible": "true",
                },
                "children": [
                    {
                        "attributes": {
                            "bounds": f"[0,{idx}][1260,{idx + 2}]",
                            "bundleName": "com.example.large",
                            "clickable": "false",
                            "description": "",
                            "enabled": "true",
                            "hashcode": f"0xfacefeed{idx:04x}",
                            "text": "",
                            "type": "Divider",
                            "visible": "true",
                        },
                        "children": [],
                    },
                    {
                        "attributes": {
                            "bounds": "[0,0][1260,100]",
                            "bundleName": "com.example.large",
                            "clickable": "true",
                            "enabled": "true",
                            "id": f"hidden_noise_{idx}",
                            "text": f"Hidden Noise {idx}",
                            "type": "Button",
                            "visible": "false",
                        },
                        "children": [],
                    },
                ],
            }
        )

    return {
        "attributes": {
            "bounds": "[0,0][1260,2720]",
            "bundleName": "com.example.large",
            "clickable": "false",
            "enabled": "true",
            "type": "root",
            "visible": "true",
        },
        "children": children,
    }


@dataclass
class FakeDeviceState:
    serial: str
    status: str = "Connected"
    transport: str = "USB"
    host: str = "localhost"
    model: str = "TEST-HMOS01"
    software_version: str = "TEST 6.1.0.117(SP6)"
    api_version: str = "23"
    os_fullname: str = "OpenHarmony-6.1.0.115"
    uitest_version: str = "6.0.2.3"
    battery_dump: str = BATTERY_61_CHARGING
    power_dump: str = POWER_61_AWAKE
    wms_dump: str = WMS_61_FOCUS
    layouts: list[str] = field(default_factory=lambda: [load_home_layout_json()])
    layout_index: int = 0
    jpeg_bytes: bytes = field(default_factory=make_minimal_jpeg)
    png_bytes: bytes = field(default_factory=make_minimal_png)
    remote_files: dict[str, bytes] = field(default_factory=dict)
    advance_layout_on_click: bool = False
    advance_layout_on_dump: bool = False
    fail_recv: bool = False
    fail_concurrent_uitest: bool = False
    timeout_on_cmd_prefix: str | None = None


class FakeHdcTransport:
    """Stateful in-memory `HdcTransport` modeling HarmonyOS targets and `/data/local/tmp`."""

    def __init__(
        self,
        devices: Sequence[FakeDeviceState] = (),
        *,
        hdc_version: str = "Ver: 3.2.0c",
    ) -> None:
        self.hdc_version = hdc_version
        self.devices: dict[str, FakeDeviceState] = {d.serial: d for d in devices}
        self.calls: list[tuple[str | None, tuple[str, ...], float]] = []

    def all_remote_tmp_files(self) -> dict[str, list[str]]:
        return {
            serial: sorted(dev.remote_files.keys())
            for serial, dev in self.devices.items()
            if dev.remote_files
        }

    def execute(
        self,
        args: Sequence[str],
        *,
        target: str | None = None,
        timeout: float = 15.0,
    ) -> CommandResult:
        arg_tuple = tuple(args)
        self.calls.append((target, arg_tuple, timeout))

        if arg_tuple == ("-v",) or arg_tuple == ("version",):
            return CommandResult(arg_tuple, 0, f"{self.hdc_version}\n", "", 2)

        if arg_tuple in {("list", "targets", "-v"), ("list", "targets")}:
            if not self.devices:
                return CommandResult(arg_tuple, 0, "[Empty]\n", "", 2)
            lines: list[str] = []
            for dev in self.devices.values():
                if "-v" in arg_tuple:
                    lines.append(f"{dev.serial}\t\t{dev.transport}\t{dev.status}\t{dev.host}")
                elif dev.status.lower() == "connected":
                    lines.append(dev.serial)
            out = "\n".join(lines) + "\n" if lines else "[Empty]\n"
            return CommandResult(arg_tuple, 0, out, "", 2)

        if target is None or target not in self.devices:
            # Mirror exact real hdc 3.2.0c behavior: rc=0 with [Fail] on stdout!
            return CommandResult(
                arg_tuple,
                0,
                "[Fail]Not match target founded, check connect-key please\n",
                "",
                2,
            )

        dev = self.devices[target]
        if dev.status.lower() == "unauthorized":
            return CommandResult(
                arg_tuple,
                0,
                "[Fail][E000002]The device unauthorized. "
                "Please check for a confirmation dialog on your device.\n",
                "",
                2,
            )
        if dev.status.lower() != "connected":
            return CommandResult(
                arg_tuple,
                0,
                "[Fail]Not match target founded, check connect-key please\n",
                "",
                2,
            )

        if arg_tuple[0] == "shell" and len(arg_tuple) >= 2:
            return self._handle_shell(dev, arg_tuple, arg_tuple[1], timeout)

        if len(arg_tuple) == 4 and arg_tuple[0] == "file" and arg_tuple[1] == "recv":
            remote_path, local_path = arg_tuple[2], arg_tuple[3]
            if dev.fail_recv:
                return CommandResult(
                    arg_tuple,
                    0,
                    f"[Fail]Error opening file: {remote_path}\n",
                    "",
                    3,
                )
            if remote_path not in dev.remote_files:
                return CommandResult(
                    arg_tuple,
                    0,
                    f"[Fail]Error opening file: {remote_path}, no such file\n",
                    "",
                    3,
                )
            dest = Path(local_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(dev.remote_files[remote_path])
            return CommandResult(arg_tuple, 0, "FileTransfer finish\n", "", 4)

        if len(arg_tuple) == 4 and arg_tuple[0] == "file" and arg_tuple[1] == "send":
            local_path, remote_path = arg_tuple[2], arg_tuple[3]
            dev.remote_files[remote_path] = Path(local_path).read_bytes()
            return CommandResult(arg_tuple, 0, "FileTransfer finish\n", "", 4)

        return CommandResult(arg_tuple, 1, "", f"Unsupported fake hdc args: {arg_tuple}", 1)

    def _handle_shell(
        self,
        dev: FakeDeviceState,
        raw_args: tuple[str, ...],
        shell_cmd: str,
        timeout: float,
    ) -> CommandResult:
        if dev.timeout_on_cmd_prefix and shell_cmd.startswith(dev.timeout_on_cmd_prefix):
            res = CommandResult(raw_args, -9, "", "", int(timeout * 1000), HdcErrorKind.TIMEOUT)
            raise HdcTimeoutError(f"Simulated timeout on {shell_cmd}", result=res)

        tokens = shlex.split(shell_cmd)
        if not tokens:
            return CommandResult(raw_args, 1, "", "empty shell command", 1)

        head = tokens[0]
        if head == "rm":
            for token in tokens[1:]:
                if not token.startswith("-"):
                    dev.remote_files.pop(token, None)
            return CommandResult(raw_args, 0, "", "", 1)

        if head == "param" and len(tokens) == 3 and tokens[1] == "get":
            mapping = {
                "const.product.model": dev.model,
                "const.product.software.version": dev.software_version,
                "const.ohos.apiversion": dev.api_version,
                "const.ohos.fullname": dev.os_fullname,
            }
            return CommandResult(raw_args, 0, f"{mapping.get(tokens[2], '')} \n", "", 1)

        if head == "hidumper":
            if "BatteryService" in tokens:
                return CommandResult(raw_args, 0, dev.battery_dump, "", 2)
            if "PowerManagerService" in tokens:
                return CommandResult(raw_args, 0, dev.power_dump, "", 2)
            if "WindowManagerService" in tokens:
                return CommandResult(raw_args, 0, dev.wms_dump, "", 2)

        if head == "power-shell":
            if len(tokens) >= 2 and tokens[1] == "wakeup":
                dev.power_dump = POWER_61_AWAKE
                return CommandResult(raw_args, 0, "WakeupDevice is called\n", "", 2)
            if len(tokens) >= 2 and tokens[1] == "suspend":
                dev.power_dump = POWER_61_INACTIVE
                return CommandResult(raw_args, 0, "SuspendDevice is called\n", "", 2)

        if head == "aa":
            return CommandResult(raw_args, 0, "start ability successfully.\n", "", 2)

        if head == "snapshot_display":
            if "-f" in tokens:
                out_path = tokens[tokens.index("-f") + 1]
                dev.remote_files[out_path] = dev.jpeg_bytes
                return CommandResult(
                    raw_args,
                    0,
                    f"success: snapshot display 0 , write to {out_path} as jpeg\n",
                    "",
                    10,
                )

        if head == "uitest":
            if dev.fail_concurrent_uitest and "--version" not in tokens:
                return CommandResult(
                    raw_args,
                    1,
                    "Error: uitest-api does not allow calling concurrently\n",
                    "",
                    2,
                )
            if len(tokens) >= 2 and tokens[1] == "--version":
                return CommandResult(raw_args, 0, f"{dev.uitest_version}\n", "", 2)
            if len(tokens) >= 2 and tokens[1] == "dumpLayout" and "-p" in tokens:
                out_path = tokens[tokens.index("-p") + 1]
                idx = min(dev.layout_index, len(dev.layouts) - 1)
                payload = dev.layouts[idx]
                if dev.advance_layout_on_dump and dev.layout_index + 1 < len(dev.layouts):
                    dev.layout_index += 1
                dev.remote_files[out_path] = payload.encode("utf-8")
                return CommandResult(raw_args, 0, f"DumpLayout saved to:{out_path}\n", "", 8)
            if len(tokens) >= 2 and tokens[1] == "screenCap" and "-p" in tokens:
                out_path = tokens[tokens.index("-p") + 1]
                dev.remote_files[out_path] = dev.png_bytes
                return CommandResult(raw_args, 0, f"ScreenCap saved to {out_path}\n", "", 8)
            if len(tokens) >= 2 and tokens[1] == "uiInput":
                if dev.advance_layout_on_click and dev.layout_index + 1 < len(dev.layouts):
                    dev.layout_index += 1
                return CommandResult(raw_args, 0, "ok\n", "", 3)

        return CommandResult(raw_args, 1, "", f"Unknown fake shell command: {shell_cmd}", 1)


def make_second_page_layout_json() -> str:
    """Create a second page layout JSON for wait_for / layout diff transitions."""
    return json.dumps(
        {
            "attributes": {
                "bounds": "[0,0][1260,2720]",
                "bundleName": "com.example.settings",
                "clickable": "false",
                "enabled": "true",
                "type": "root",
                "visible": "true",
            },
            "children": [
                {
                    "attributes": {
                        "bounds": "[48,160][700,240]",
                        "bundleName": "com.example.settings",
                        "clickable": "false",
                        "enabled": "true",
                        "text": "Settings Updated",
                        "type": "Text",
                        "visible": "true",
                    },
                    "children": [],
                },
                {
                    "attributes": {
                        "bounds": "[48,320][1212,440]",
                        "bundleName": "com.example.settings",
                        "clickable": "true",
                        "enabled": "true",
                        "id": "btn_done",
                        "text": "Done",
                        "type": "Button",
                        "visible": "true",
                    },
                    "children": [],
                },
            ],
        }
    )
