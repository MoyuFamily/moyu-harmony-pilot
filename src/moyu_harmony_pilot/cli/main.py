"""Agent-ready CLI entrypoint for `harmony-pilot`."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, NoReturn, TextIO

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner
from moyu_harmony_pilot.actions.observation import capture_screenshot
from moyu_harmony_pilot.actions.validation import MAX_JSON_BYTES
from moyu_harmony_pilot.bridge import BridgeConfig, BridgeWorker
from moyu_harmony_pilot.bridge.config import validate_configuration
from moyu_harmony_pilot.bridge.worker import health_status
from moyu_harmony_pilot.cli.codes import ExitCode, classify_exception
from moyu_harmony_pilot.cli.envelope import CliEnvelope, error_envelope, ok_envelope
from moyu_harmony_pilot.cli.schema import build_cli_schema
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceProfile,
    DeviceRegistry,
    DeviceSummary,
    HarmonyDevice,
    ScreenState,
    check_config_permissions,
    ensure_secure_dir,
    is_device_busy,
    write_secure_text,
)
from moyu_harmony_pilot.hdc import (
    ConnectionState,
    HdcClient,
    HdcError,
    HdcTransport,
    discover_hdc,
)
from moyu_harmony_pilot.ui import build_compact_snapshot, parse_layout_json
from moyu_harmony_pilot.vision import require_pillow


class _CliArgumentError(Exception):
    """Internal exception raised when argparse rejects user input."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _PilotArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises `_CliArgumentError` so `--json` errors stay valid JSON."""

    def error(self, message: str) -> NoReturn:
        raise _CliArgumentError(message)


def _add_shared_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    if suppress_defaults:
        parser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit a single structured JSON envelope on stdout.",
        )
        parser.add_argument(
            "--config",
            default=argparse.SUPPRESS,
            metavar="PATH",
            help="Path to local devices.local.yaml or devices.local.json.",
        )
        parser.add_argument(
            "--hdc-path",
            default=argparse.SUPPRESS,
            metavar="PATH",
            help="Explicit path to official HarmonyOS/OpenHarmony hdc binary.",
        )
        parser.add_argument(
            "--state-dir",
            default=argparse.SUPPRESS,
            metavar="DIR",
            help="Local state directory for locks and diagnostics (default: .state).",
        )
    else:
        parser.add_argument(
            "--json",
            action="store_true",
            default=False,
            help="Emit a single structured JSON envelope on stdout.",
        )
        parser.add_argument(
            "--config",
            default=None,
            metavar="PATH",
            help="Path to local devices.local.yaml or devices.local.json.",
        )
        parser.add_argument(
            "--hdc-path",
            default=None,
            metavar="PATH",
            help="Explicit path to official HarmonyOS/OpenHarmony hdc binary.",
        )
        parser.add_argument(
            "--state-dir",
            default=".state",
            metavar="DIR",
            help="Local state directory for locks and diagnostics (default: .state).",
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level `harmony-pilot` CLI argument parser."""
    shared = argparse.ArgumentParser(add_help=False)
    _add_shared_options(shared, suppress_defaults=True)

    parser = _PilotArgumentParser(
        prog="harmony-pilot",
        description="HarmonyOS NEXT (5/6+) automation engine and secure pull bridge.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    _add_shared_options(parser, suppress_defaults=False)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # devices
    p_devices = subparsers.add_parser(
        "devices",
        parents=[shared],
        help="List connected HarmonyOS devices, alias mappings, and basic health.",
    )
    p_devices.add_argument(
        "--no-health",
        action="store_true",
        default=False,
        help="Skip battery/screen health telemetry queries.",
    )
    p_devices.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-command hdc timeout in seconds (default: 10).",
    )

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        parents=[shared],
        help="Check hdc binary, environment permissions, device connectivity, and uitest.",
    )
    p_doctor.add_argument(
        "--device",
        default=None,
        metavar="ALIAS",
        help="Specific device alias to check.",
    )
    p_doctor.add_argument(
        "--deep",
        action="store_true",
        default=False,
        help="Run read-only dumpLayout and snapshot_display smoke checks (with cleanup).",
    )
    p_doctor.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-command hdc timeout in seconds (default: 15).",
    )

    # dump-ui
    p_dump = subparsers.add_parser(
        "dump-ui",
        parents=[shared],
        help="Capture UI layout tree and emit a compact `@eN` semantic snapshot.",
    )
    p_dump.add_argument(
        "--device",
        default=None,
        metavar="ALIAS",
        help="Target device alias (optional if exactly one online device is attached).",
    )
    p_dump.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Write/output compact `@eN` semantic snapshot instead of raw layout JSON.",
    )
    p_dump.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="Optional local output path (saved with mode 0600).",
    )
    p_dump.add_argument(
        "--max-elements",
        type=int,
        default=120,
        help="Maximum elements in compact snapshot (default: 120).",
    )
    p_dump.add_argument(
        "--include-attrs",
        action="store_true",
        default=False,
        help="Include extended font attributes (`uitest dumpLayout -a`).",
    )
    p_dump.add_argument(
        "--bundle",
        default=None,
        help="Optional target bundleName (`-b`).",
    )
    p_dump.add_argument(
        "--window-id",
        type=int,
        default=None,
        help="Optional target windowId (`-w`).",
    )
    p_dump.add_argument(
        "--no-merge",
        action="store_true",
        default=False,
        help="Disable multi-window merge (`-m false`).",
    )
    p_dump.add_argument(
        "--display-id",
        type=int,
        default=None,
        help="Optional target displayId (`-d`).",
    )
    p_dump.add_argument(
        "--wait-lock",
        type=float,
        default=0.0,
        help="Seconds to wait for exclusive device lock (default: 0).",
    )
    p_dump.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Timeout in seconds for dumpLayout and file transfer (default: 20).",
    )

    # screenshot
    p_shot = subparsers.add_parser(
        "screenshot",
        parents=[shared],
        help="Capture current screen via snapshot_display (JPEG) or uitest screenCap (PNG).",
    )
    p_shot.add_argument(
        "--device",
        default=None,
        metavar="ALIAS",
        help="Target device alias (optional if exactly one online device is attached).",
    )
    p_shot.add_argument(
        "--out",
        required=True,
        metavar="FILE",
        help="Destination file path for screenshot (saved with mode 0600).",
    )
    p_shot.add_argument(
        "--format",
        choices=("jpeg", "png"),
        default=None,
        help="Image format (inferred from --out extension if omitted; defaults to jpeg).",
    )
    p_shot.add_argument(
        "--display-id",
        type=int,
        default=None,
        help="Optional displayId.",
    )
    p_shot.add_argument(
        "--max-width",
        type=int,
        default=None,
        help="Downscale screenshot in-place if wider than max-width pixels.",
    )
    p_shot.add_argument(
        "--som",
        action="store_true",
        default=False,
        help="Annotate screenshot references using the optional vision extra.",
    )
    p_shot.add_argument(
        "--wait-lock",
        type=float,
        default=0.0,
        help="Seconds to wait for exclusive device lock (default: 0).",
    )
    p_shot.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Timeout in seconds for screenshot capture and transfer (default: 20).",
    )

    # schema
    subparsers.add_parser(
        "schema",
        parents=[shared],
        help="Emit self-describing JSON Schema for commands, envelopes, and exit codes.",
    )
    p_plugins = subparsers.add_parser(
        "plugins", parents=[shared], help="Discover and describe trusted local action plugins.",
    )
    plugin_commands = p_plugins.add_subparsers(dest="plugin_command", required=True)
    plugin_commands.add_parser("list", parents=[shared], help="List discovered plugins.")
    p_describe = plugin_commands.add_parser(
        "describe", parents=[shared], help="Describe an action schema and safety metadata.",
    )
    p_describe.add_argument("action_id")

    p_run = subparsers.add_parser(
        "run", parents=[shared], help="Execute a local action under device and ledger guards.",
    )
    p_run.add_argument("--device", default=None, metavar="ALIAS")
    p_run.add_argument("--action", required=True, metavar="ID")
    p_run.add_argument("--params-json", default="{}", metavar="JSON")
    p_run.add_argument("--idempotency-key", default=None, metavar="KEY")
    p_run.add_argument(
        "--timeout", type=float, default=None, help="Total action deadline in seconds.",
    )
    p_run.add_argument("--max-steps", type=int, default=100)
    p_run.add_argument("--dry-run", action="store_true", default=False)

    p_serve = subparsers.add_parser(
        "serve", parents=[shared], help="Run the outbound Pull Bridge using a private JSON config.",
    )
    serve_mode = p_serve.add_mutually_exclusive_group()
    serve_mode.add_argument("--once", action="store_true", help="Run one heartbeat/claim cycle.")
    serve_mode.add_argument("--check", action="store_true", help="Validate configuration offline.")
    serve_mode.add_argument(
        "--probe", action="store_true", help="Send device telemetry and a broker heartbeat only.",
    )
    serve_mode.add_argument("--health", action="store_true", help="Probe heartbeat freshness.")

    return parser


def _detect_json_flag(argv: Sequence[str]) -> bool:
    return "--json" in argv


def _detect_command_name(argv: Sequence[str]) -> str:
    known = {"devices", "doctor", "dump-ui", "screenshot", "schema", "plugins", "run", "serve"}
    for token in argv:
        if token in known:
            return token
    return "harmony-pilot"


def _make_hdc_client(
    *,
    args: argparse.Namespace,
    transport: HdcTransport | None,
    env: Mapping[str, str] | None = None,
) -> HdcClient:
    if transport is not None:
        return HdcClient(transport=transport)
    return HdcClient(hdc_path=args.hdc_path, env=env)


def _probe_ocr_backends() -> list[str]:
    available: list[str] = []
    if sys.platform == "darwin" and importlib.util.find_spec("Vision") is not None:
        available.append("vision-macos")
    if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
        available.append("vision-rapidocr")
    return available


def _cmd_devices(
    args: argparse.Namespace,
    *,
    transport: HdcTransport | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[ExitCode, dict[str, Any], str]:
    registry = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    hdc = _make_hdc_client(args=args, transport=transport, env=env)
    targets = hdc.list_targets(timeout=args.timeout)
    state_dir = Path(args.state_dir)
    lock_dir = state_dir / "locks"

    summaries: list[DeviceSummary] = []
    seen_serials: set[str] = set()

    for target in targets:
        seen_serials.add(target.serial)
        profile = registry.get_by_serial(target.serial)
        alias = profile.alias if profile is not None else None
        busy = is_device_busy(target.serial, lock_dir)

        battery_pct: int | None = None
        charging: bool | None = None
        temp_c: float | None = None
        screen_state = ScreenState.UNKNOWN
        overheated = False

        if (
            not args.no_health
            and target.connection_state == ConnectionState.ONLINE
            and not busy
        ):
            temp_profile = profile or DeviceProfile(
                alias=alias or target.serial_masked,
                serial=target.serial,
            )
            dev = HarmonyDevice(hdc, temp_profile, state_dir=state_dir)
            try:
                health = dev.get_health(timeout=args.timeout)
                battery_pct = health.battery.battery_percent
                charging = health.battery.charging
                temp_c = health.battery.temperature_c
                screen_state = health.power.screen_state
                overheated = health.overheated
            except HdcError:
                pass

        summaries.append(
            DeviceSummary(
                alias=alias,
                serial_masked=target.serial_masked,
                transport=target.transport,
                connection_state=target.connection_state,
                configured=profile is not None,
                busy=busy,
                battery_percent=battery_pct,
                charging=charging,
                temperature_c=temp_c,
                screen_state=screen_state,
                overheated=overheated,
            )
        )

    # Also surface configured devices currently absent from hdc targets as offline.
    for profile in registry.profiles:
        if profile.serial in seen_serials:
            continue
        summaries.append(
            DeviceSummary(
                alias=profile.alias,
                serial_masked=profile.serial_masked,
                transport="UNKNOWN",
                connection_state=ConnectionState.OFFLINE,
                configured=True,
                busy=is_device_busy(profile.serial, lock_dir),
            )
        )

    data = {
        "config_path": str(registry.source_path) if registry.source_path else None,
        "count": len(summaries),
        "online_count": sum(
            1 for item in summaries if item.connection_state == ConnectionState.ONLINE
        ),
        "devices": [item.to_dict() for item in summaries],
    }

    if not summaries:
        human = "No HarmonyOS devices detected (hdc list targets returned [Empty])."
    else:
        lines = ["ALIAS\tSTATE\tTRANSPORT\tSERIAL\tBATTERY\tTEMP_C\tSCREEN\tBUSY"]
        for item in summaries:
            alias_col = item.alias or "-"
            bat_col = "-" if item.battery_percent is None else f"{item.battery_percent}%"
            temp_col = "-" if item.temperature_c is None else f"{item.temperature_c:.1f}"
            lines.append(
                f"{alias_col}\t{item.connection_state.value}\t{item.transport}\t"
                f"{item.serial_masked}\t{bat_col}\t{temp_col}\t"
                f"{item.screen_state.value}\t{str(item.busy).lower()}"
            )
        human = "\n".join(lines)

    return ExitCode.OK, data, human


def _cmd_doctor(
    args: argparse.Namespace,
    *,
    transport: HdcTransport | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[ExitCode, dict[str, Any], str]:
    checks: list[dict[str, Any]] = []
    exit_code = ExitCode.OK

    # 1. Python runtime
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(
        {
            "name": "python_runtime",
            "status": "pass" if sys.version_info >= (3, 11) else "fail",
            "detail": py_ver,
        }
    )

    # 2. Local device config
    registry = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    if registry.source_path is None:
        checks.append(
            {
                "name": "devices_config",
                "status": "warn",
                "detail": "No devices.local.{yaml,json} found (using standalone discovery).",
            }
        )
    else:
        strict_mode = check_config_permissions(registry.source_path)
        checks.append(
            {
                "name": "devices_config",
                "status": "pass" if strict_mode else "warn",
                "detail": str(registry.source_path),
                "permissions_0600": strict_mode,
                "configured_aliases": len(registry.profiles),
            }
        )

    # 3. Local state directories (mode 0700)
    state_dir = Path(args.state_dir)
    ensure_secure_dir(state_dir)
    ensure_secure_dir(state_dir / "locks")
    ensure_secure_dir(state_dir / "diagnostics")
    checks.append(
        {
            "name": "state_dir",
            "status": "pass",
            "detail": str(state_dir),
        }
    )

    # 4. Optional OCR backends (informational in M1)
    ocr_backends = _probe_ocr_backends()
    checks.append(
        {
            "name": "vision_ocr",
            "status": "pass" if ocr_backends else "warn",
            "detail": (
                ", ".join(ocr_backends)
                if ocr_backends
                else "No optional OCR backend installed (optional extra)."
            ),
            "backends": ocr_backends,
        }
    )

    # 5. Official hdc binary & version
    hdc_path_str: str | None = None
    hdc_version: str | None = None
    if transport is None:
        hdc_path = discover_hdc(args.hdc_path, env=env)
        hdc_path_str = str(hdc_path)
        hdc = HdcClient(hdc_path=hdc_path, env=env)
    else:
        hdc = HdcClient(transport=transport)
        hdc_path_str = "injected-transport"

    hdc_version = hdc.version(timeout=args.timeout)
    checks.append(
        {
            "name": "hdc_binary",
            "status": "pass",
            "path": hdc_path_str,
            "version": hdc_version,
        }
    )

    # 6. Targets enumeration
    targets = hdc.list_targets(timeout=args.timeout)
    online_targets = [t for t in targets if t.connection_state == ConnectionState.ONLINE]
    checks.append(
        {
            "name": "hdc_targets",
            "status": "pass" if online_targets else "warn",
            "total": len(targets),
            "online": len(online_targets),
            "targets": [
                {
                    "alias": (p.alias if (p := registry.get_by_serial(t.serial)) else None),
                    "serial_masked": t.serial_masked,
                    "transport": t.transport,
                    "connection_state": t.connection_state.value,
                }
                for t in targets
            ],
        }
    )

    # 7. Device-specific read-only probe
    device_report: dict[str, Any] | None = None
    should_probe_device = args.device is not None or bool(online_targets)
    if should_probe_device:
        profile, _target = registry.resolve(args.device, targets, require_online=True)
        device = HarmonyDevice(hdc, profile, state_dir=state_dir)
        health = device.get_health(include_info=True, timeout=args.timeout)
        device_report = health.to_dict()

        checks.append(
            {
                "name": "device_telemetry",
                "status": "warn" if health.overheated else "pass",
                "alias": device.alias,
                "battery_percent": health.battery.battery_percent,
                "temperature_c": health.battery.temperature_c,
                "screen_state": health.power.screen_state.value,
                "overheated": health.overheated,
            }
        )
        uitest_ver = health.info.uitest_version if health.info else None
        checks.append(
            {
                "name": "uitest_binary",
                "status": "pass" if uitest_ver else "fail",
                "version": uitest_ver,
            }
        )
        if not uitest_ver:
            exit_code = ExitCode.FAILED

        if args.deep:
            with device.acquire_lock(wait_timeout=0.0):
                tree, snap = device.dump_ui_compact(timeout=args.timeout)
                with tempfile.TemporaryDirectory(prefix="moyu_pilot_doctor_") as tmp_dir:
                    shot_path = Path(tmp_dir) / "doctor_probe.jpeg"
                    shot = device.screenshot(shot_path, timeout=args.timeout)
            checks.append(
                {
                    "name": "deep_readonly_probe",
                    "status": "pass",
                    "layout_hash": snap.layout_hash,
                    "total_nodes": tree.node_count,
                    "kept_elements": snap.kept_elements,
                    "screenshot_width": shot.width,
                    "screenshot_height": shot.height,
                    "screenshot_bytes": shot.size_bytes,
                }
            )

    has_fail = any(check["status"] == "fail" for check in checks)
    if has_fail and exit_code == ExitCode.OK:
        exit_code = ExitCode.FAILED

    data = {
        "healthy": not has_fail,
        "hdc_version": hdc_version,
        "checks": checks,
        "device": device_report,
    }

    lines = [f"doctor healthy={str(not has_fail).lower()} hdc={hdc_version}"]
    for check in checks:
        detail = check.get("detail") or check.get("version") or check.get("alias") or ""
        lines.append(f"  [{check['status'].upper()}] {check['name']}: {detail}")
    return exit_code, data, "\n".join(lines)


def _cmd_dump_ui(
    args: argparse.Namespace,
    *,
    transport: HdcTransport | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[ExitCode, dict[str, Any], str]:
    if args.max_elements <= 0:
        raise DeviceConfigurationError("--max-elements must be a positive integer.")

    registry = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    hdc = _make_hdc_client(args=args, transport=transport, env=env)
    targets = hdc.list_targets(timeout=args.timeout)
    profile, _target = registry.resolve(args.device, targets, require_online=True)
    device = HarmonyDevice(hdc, profile, state_dir=args.state_dir)

    merge_windows = False if args.no_merge else None
    with device.acquire_lock(wait_timeout=args.wait_lock):
        raw_text = device.ui.dump_layout_raw(
            include_attributes=args.include_attrs,
            bundle_name=args.bundle,
            window_id=args.window_id,
            merge_windows=merge_windows,
            display_id=args.display_id,
            timeout=args.timeout,
        )

    tree = parse_layout_json(raw_text)
    snapshot = build_compact_snapshot(tree, max_elements=args.max_elements)
    raw_bytes = len(raw_text.encode("utf-8"))

    saved_path: Path | None = None
    saved_mode: str | None = None
    if args.out:
        out_path = Path(args.out).expanduser()
        if args.compact:
            saved_path = write_secure_text(
                out_path,
                json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            )
            saved_mode = "compact"
        else:
            saved_path = write_secure_text(out_path, raw_text)
            saved_mode = "raw"

    # Never dump raw 100KB+ JSON to stdout (AGENTS.md low-token contract).
    data: dict[str, Any] = {
        "alias": device.alias,
        "raw_bytes": raw_bytes,
        "saved_path": str(saved_path) if saved_path else None,
        "saved_mode": saved_mode,
        "snapshot": snapshot.to_dict(),
    }

    if saved_path and saved_mode == "raw":
        human = (
            f"Saved raw layout ({raw_bytes} bytes, {tree.node_count} nodes, "
            f"layout_hash={snapshot.layout_hash}) to {saved_path}\n"
            f"{snapshot.to_text()}"
        )
    elif saved_path and saved_mode == "compact":
        human = f"Saved compact snapshot to {saved_path}\n{snapshot.to_text()}"
    else:
        human = snapshot.to_text()

    return ExitCode.OK, data, human


def _cmd_screenshot(
    args: argparse.Namespace,
    *,
    transport: HdcTransport | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[ExitCode, dict[str, Any], str]:
    if args.som:
        require_pillow()

    registry = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    hdc = _make_hdc_client(args=args, transport=transport, env=env)
    targets = hdc.list_targets(timeout=args.timeout)
    profile, _target = registry.resolve(args.device, targets, require_online=True)
    device = HarmonyDevice(hdc, profile, state_dir=args.state_dir)

    result, snapshot = capture_screenshot(
        device, args.out, som=args.som, image_format=args.format,
        display_id=args.display_id, max_width=args.max_width,
        timeout=args.timeout, wait_lock=args.wait_lock,
    )

    dims = (
        f"{result.width}x{result.height}"
        if result.width is not None and result.height is not None
        else "unknown"
    )
    human = (
        f"Captured {result.image_format} screenshot ({dims}, {result.size_bytes} bytes) "
        f"from {result.alias} -> {result.path}"
    )
    data = result.to_dict()
    if snapshot is not None:
        data["snapshot"] = snapshot.to_dict()
        data["som"] = True
    return ExitCode.OK, data, human


def _cmd_schema() -> tuple[ExitCode, dict[str, Any], str]:
    schema = build_cli_schema()
    return ExitCode.OK, schema, json.dumps(schema, ensure_ascii=False, indent=2)


def _cmd_plugins(
    args: argparse.Namespace, *, env: Mapping[str, str] | None,
) -> tuple[ExitCode, dict[str, Any], str]:
    registry = ActionRegistry.discover(env=env)
    if args.plugin_command == "describe":
        data = registry.describe(args.action_id)
    else:
        data = {"plugins": registry.list()}
    return ExitCode.OK, data, json.dumps(data, ensure_ascii=False, indent=2)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _cmd_run(
    args: argparse.Namespace, *, transport: HdcTransport | None, cwd: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[ExitCode, dict[str, Any], str]:
    try:
        if len(args.params_json.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("JSON size limit.")
        params = json.loads(args.params_json, object_pairs_hook=_unique_json_object)
    except (ValueError, RecursionError) as exc:
        raise DeviceConfigurationError(
            "--params-json requires a bounded valid JSON object."
        ) from exc
    registry = ActionRegistry.discover(env=env)
    runner = ActionRunner(registry, state_dir=args.state_dir)
    runner.prepare(
        args.action, params, idempotency_key=args.idempotency_key,
        timeout=args.timeout, max_steps=args.max_steps,
    )
    devices = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    hdc = _make_hdc_client(args=args, transport=transport, env=env)
    targets = hdc.list_targets()
    profile, _target = devices.resolve(args.device, targets, require_online=True)
    device = HarmonyDevice(hdc, profile, state_dir=args.state_dir)
    outcome = runner.run(
        device, args.action, params, idempotency_key=args.idempotency_key,
        timeout=args.timeout, max_steps=args.max_steps, dry_run=args.dry_run,
    )
    exit_code = {
        "succeeded": ExitCode.OK, "already_done": ExitCode.OK,
        "configuration_error": ExitCode.CONFIG_ERROR, "device_offline": ExitCode.DEVICE_OFFLINE,
        "human_required": ExitCode.HUMAN_REQUIRED, "device_overheated": ExitCode.DEVICE_OVERHEATED,
        "failed": ExitCode.FAILED, "unknown": ExitCode.FAILED,
    }[outcome.status]
    data = {"alias": device.alias, "action": args.action, "dry_run": args.dry_run,
            **outcome.to_dict()}
    return exit_code, data, json.dumps(data, ensure_ascii=False, indent=2)


def _emit_envelope(envelope: CliEnvelope, out: TextIO) -> None:
    out.write(envelope.to_json() + "\n")


def _cmd_serve(args, *, transport, env) -> tuple[ExitCode, dict, str]:
    if args.config is None:
        raise DeviceConfigurationError("serve requires --config pointing to a private JSON file.")
    config = BridgeConfig.load(args.config, env=env)
    if args.health:
        data = health_status(config)
        code = ExitCode.OK if data["healthy"] else ExitCode.FAILED
        return code, data, json.dumps(data)
    if args.check:
        _, registry = validate_configuration(config, env=env)
        allowed = {name for actions in config.allowed_actions.values() for name in actions}
        data = {
            "valid": True,
            "protocol_version": 1,
            "bridge_id": config.bridge_id,
            "device_aliases": sorted(config.allowed_actions),
            "allowed_actions": config.allowed_actions,
            "actions": [registry.describe(name) for name in sorted(allowed)],
        }
    else:
        hdc = _make_hdc_client(args=args, transport=transport, env=env)
        worker = BridgeWorker(config, hdc, env=env)
        if args.probe:
            data = worker.probe()
        else:
            data = worker.run_once() if args.once else worker.run()
    return ExitCode.OK, data, json.dumps(data, ensure_ascii=False, indent=2)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: HdcTransport | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run the `harmony-pilot` CLI and return a stable semantic `ExitCode` integer."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    json_requested = _detect_json_flag(raw_argv)
    guessed_cmd = _detect_command_name(raw_argv)
    parser = build_parser()
    parser_output = io.StringIO() if json_requested else out

    try:
        with redirect_stdout(parser_output):
            args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        if json_requested and code == 0:
            _emit_envelope(
                ok_envelope(guessed_cmd, {"text": parser_output.getvalue().strip()}), out,
            )
        return code
    except _CliArgumentError as exc:
        if json_requested:
            _emit_envelope(
                error_envelope(
                    guessed_cmd,
                    code="configuration_error",
                    message=exc.message,
                    retryable=False,
                    hint="Run `harmony-pilot schema` or `--help` to inspect valid flags.",
                ),
                out,
            )
        else:
            err.write(f"error: {exc.message}\n")
        return int(ExitCode.CONFIG_ERROR)

    use_json = bool(getattr(args, "json", False))
    command = args.command

    if command is None:
        if use_json:
            _emit_envelope(
                ok_envelope(
                    "harmony-pilot",
                    {
                        "version": __version__,
                        "commands": list(build_cli_schema()["commands"]),
                        "hint": "Run `harmony-pilot schema --json` for full CLI contract.",
                    },
                ),
                out,
            )
        else:
            parser.print_help(file=out)
        return int(ExitCode.OK)

    try:
        if command == "devices":
            exit_code, data, human = _cmd_devices(args, transport=transport, cwd=cwd, env=env)
        elif command == "doctor":
            exit_code, data, human = _cmd_doctor(args, transport=transport, cwd=cwd, env=env)
        elif command == "dump-ui":
            exit_code, data, human = _cmd_dump_ui(args, transport=transport, cwd=cwd, env=env)
        elif command == "screenshot":
            exit_code, data, human = _cmd_screenshot(args, transport=transport, cwd=cwd, env=env)
        elif command == "schema":
            exit_code, data, human = _cmd_schema()
        elif command == "plugins":
            with redirect_stdout(err), redirect_stderr(err):
                exit_code, data, human = _cmd_plugins(args, env=env)
        elif command == "run":
            with redirect_stdout(err), redirect_stderr(err):
                exit_code, data, human = _cmd_run(args, transport=transport, cwd=cwd, env=env)
        elif command == "serve":
            with redirect_stdout(err), redirect_stderr(err):
                exit_code, data, human = _cmd_serve(args, transport=transport, env=env)
        else:
            raise DeviceConfigurationError(f"Unsupported command: {command!r}")

        if use_json:
            if exit_code == ExitCode.OK:
                _emit_envelope(ok_envelope(command, data), out)
            else:
                _emit_envelope(
                    error_envelope(
                        command,
                        code=(data.get("error_code") or data["status"])
                        if command == "run" else "check_failed",
                        message=data["summary"] if command == "run"
                        else f"Command `{command}` reported failing checks.",
                        retryable=False,
                        hint={
                            "human_required": (
                                "Complete verification or sensitive confirmation manually."
                            ),
                            "unknown": (
                                "Reconcile the previous submission read-only before further action."
                            ),
                            "device_overheated": "Allow the device to cool before retrying.",
                        }.get(data.get("status")),
                        data=data,
                    ),
                    out,
                )
        else:
            out.write(human + "\n")
        return int(exit_code)

    except Exception as exc:
        exit_code, code, message, retryable, hint = classify_exception(exc)
        if use_json:
            _emit_envelope(
                error_envelope(
                    command,
                    code=code,
                    message=message,
                    retryable=retryable,
                    hint=hint,
                ),
                out,
            )
        else:
            err.write(f"error [{code}]: {message}\n")
            if hint:
                err.write(f"hint: {hint}\n")
        return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
