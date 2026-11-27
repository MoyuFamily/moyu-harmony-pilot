"""CLI adapters for applications, local interaction, reconciliation, and reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from moyu_harmony_pilot.actions import (
    ActionOutcome,
    ActionRegistry,
    ActionRunner,
    InteractionSession,
)
from moyu_harmony_pilot.actions.files import (
    FILES_EXPORT_RESULT_SCHEMA,
    FILES_QUERY_RESULT_SCHEMA,
)
from moyu_harmony_pilot.actions.ledger import ActionLedger, digest
from moyu_harmony_pilot.actions.recording import (
    RECORDING_START_RESULT_SCHEMA,
    RECORDING_STATUS_RESULT_SCHEMA,
    RECORDING_STOP_RESULT_SCHEMA,
)
from moyu_harmony_pilot.actions.reports import capture_inspector, write_report
from moyu_harmony_pilot.actions.sms import SMS_ERROR_HINTS, SMS_RESULT_SCHEMA
from moyu_harmony_pilot.actions.validation import MAX_JSON_BYTES, validate_value
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    DeviceLock,
    DeviceRegistry,
    HarmonyDevice,
    ensure_secure_dir,
)
from moyu_harmony_pilot.device.files import validate_file_export, validate_media_query
from moyu_harmony_pilot.device.packages import (
    PACKAGE_SUFFIXES,
    download_package,
    package_metadata,
)
from moyu_harmony_pilot.hdc import HdcClient, HdcError, HdcTransport
from moyu_harmony_pilot.ui import UiError
from moyu_harmony_pilot.ui.selector import selector_from_dict

DEVICE = {"--device": {"type": "string"}}
TIMEOUT = {"type": "number", "default": 60, "exclusiveMinimum": 0, "maximum": 3600}
BUNDLE = {"type": "string", "required": True, "minLength": 1, "maxLength": 128}
KEY = {"type": "string", "required": True, "minLength": 1, "maxLength": 256}
OUT = {"type": "string", "required": True}
FILTERS = {
    **DEVICE,
    "--action": {"type": "string"},
    "--status": {"type": "string", "enum": ["submitting", "unknown", "succeeded", "already_done"]},
    "--limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
    "--offset": {"type": "integer", "default": 0, "minimum": 0},
}
REQUEST = {
    "--device": {"type": "string", "required": True},
    "--action": KEY,
    "--idempotency-key": KEY,
    "--params-json": {"type": "string", "default": "{}"},
    "--timeout": TIMEOUT,
}
LOCAL = {
    **DEVICE,
    "--timeout": TIMEOUT,
    "--max-steps": {"type": "integer", "default": 20, "minimum": 1, "maximum": 1000},
    "--local-lab": {"type": "boolean", "required": True},
}
HASH = {"--layout-hash": {"type": "string", "required": True}}
REF = {"--ref": {"type": "string", "required": True}}
SELECTOR = {"--selector-json": {"type": "string", "required": True}}
DIRECTION = {"type": "string", "enum": ["up", "down", "left", "right"], "default": "up"}

OPERATION_SCHEMAS: dict[str, Any] = {
    "files": {
        "description": "Find gallery photos/videos and export individual device files locally.",
        "side_effect": "subcommand_defined",
        "local_only": True,
        "private_content": True,
        "options": {},
        "subcommands": {
            "query": {
                "description": "Find gallery media URIs by exact filename using mediatool.",
                "action_id": "files.query",
                "side_effect": "readonly",
                "private_content": True,
                "options": {
                    **DEVICE,
                    "--name": {
                        "type": "string", "required": True, "minLength": 1, "maxLength": 255,
                        "description": "Gallery display name including its extension.",
                    },
                    "--limit": {
                        "type": "integer", "default": 100, "minimum": 1, "maximum": 1000,
                    },
                    "--timeout": {**TIMEOUT, "default": 30},
                },
                "result_schema": FILES_QUERY_RESULT_SCHEMA,
            },
            "export": {
                "description": "Export one file through hdc or gallery mediatool staging.",
                "action_id": "files.export",
                "side_effect": "local_file",
                "private_content": True,
                "options": {
                    **DEVICE,
                    "--source": {
                        "type": "string", "required": True, "minLength": 1, "maxLength": 4096,
                        "description": "Absolute device file path or file://media/Photo/<id> URI.",
                    },
                    "--out": {
                        **OUT, "minLength": 1, "maxLength": 4096,
                        "description": "Destination local filename.",
                    },
                    "--overwrite": {"type": "boolean", "default": False},
                    "--timeout": {
                        **TIMEOUT, "default": 300,
                        "description": "Transfer/checksum budget in seconds, plus up to 10 "
                        "seconds for gallery temporary-file cleanup.",
                    },
                },
                "result_schema": FILES_EXPORT_RESULT_SCHEMA,
            },
        },
    },
    "sms": {
        "description": "Read the current system SMS page or explicitly open the SMS app.",
        "side_effect": "subcommand_defined",
        "options": {},
        "subcommands": {
            "read": {
                "description": "Read private SMS content and verification codes on screen.",
                "action_id": "sms.read",
                "side_effect": "readonly",
                "private_content": True,
                "scope": "current-screen",
                "result_schema": SMS_RESULT_SCHEMA,
                "options": {
                    **DEVICE,
                    "--limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "--sender": {
                        "type": "string", "minLength": 1, "maxLength": 128,
                        "description": "Filter by sender using a nonblank string.",
                    },
                    "--wait-seconds": {
                        "type": "number", "default": 0, "minimum": 0, "maximum": 300,
                        "description": "Wait for messages newer than the initial observation. "
                        "Must be less than --timeout.",
                    },
                    "--timeout": {**TIMEOUT, "default": 30},
                },
            },
            "open": {
                "action_id": "sms.open",
                "side_effect": "mutating",
                "options": {**DEVICE, "--idempotency-key": KEY, "--timeout": TIMEOUT},
            },
        },
    },
    "ocr": {
        "description": "Recognize bounded text locally using an optional offline OCR backend.",
        "side_effect": "readonly",
        "local_only": True,
        "options": {
            **DEVICE,
            "--backend": {"type": "string", "enum": ["auto", "macos", "rapidocr"],
                          "default": "auto"},
            "--max-width": {"type": "integer", "minimum": 1, "maximum": 8192,
                            "default": 1280},
            "--timeout": {**TIMEOUT, "default": 30},
        },
    },
    "fixture": {
        "description": "Capture a redacted candidate layout fixture for manual review.",
        "side_effect": "subcommand_defined",
        "local_only": True,
        "options": {},
        "subcommands": {
            "capture": {
                "side_effect": "readonly",
                "options": {
                    **DEVICE,
                    "--out": OUT,
                    "--timeout": {**TIMEOUT, "default": 30},
                    "--keep-text": {"type": "string", "repeatable": True, "default": []},
                    "--keep-bundle": {"type": "string", "repeatable": True, "default": []},
                },
                "manual_review_required": True,
            },
        },
    },
    "receipts": {
        "description": "Retain Bridge acknowledgement history and permanent job deduplication.",
        "options": {},
        "side_effect": "subcommand_defined",
        "subcommands": {
            "prune": {
                "side_effect": "local_ledger",
                "options": {
                    "--database": OUT,
                    "--bridge-id": {"type": "string", "required": True},
                    "--older-than-days": {"type": "integer", "required": True,
                                          "minimum": 1, "maximum": 365000},
                },
                "preserves_job_deduplication": True,
            },
        },
    },
    "apps": {
        "description": "Query bundles and deploy local or downloaded HAP/HSP/APP packages.",
        "options": {},
        "side_effect": "subcommand_defined",
        "subcommands": {
            "list": {"side_effect": "readonly", "options": {**DEVICE, "--timeout": TIMEOUT}},
            "info": {
                "side_effect": "readonly",
                "options": {**DEVICE, "--timeout": TIMEOUT, "--bundle": BUNDLE},
            },
            "download": {
                "side_effect": "local_file",
                "options": {
                    "--url": {"type": "string", "required": True},
                    "--out": {**OUT, "description": "Destination .hap, .hsp, or .app file."},
                    "--sha256": {"type": "string"},
                    "--timeout": TIMEOUT,
                },
            },
            "install": {
                "side_effect": "mutating",
                "options": {
                    **DEVICE,
                    "--timeout": {**TIMEOUT, "default": 300},
                    "--package": {
                        "type": "string", "repeatable": True, "default": [],
                        "description": "Local .hap, application-internal .hsp, or .app file.",
                    },
                    "--hap": {
                        "type": "string", "repeatable": True, "default": [],
                        "description": "Backward-compatible package flag, also accepting .app.",
                    },
                    "--url": {
                        "type": "string",
                        "description": "Package URL. The URL path determines .app/.hsp format; "
                        "other paths default to .hap. Use download --out for opaque URLs.",
                    },
                    "--sha256": {"type": "string"},
                    "--idempotency-key": KEY,
                },
            },
            "uninstall": {
                "side_effect": "mutating",
                "options": {
                    **DEVICE,
                    "--timeout": TIMEOUT,
                    "--bundle": BUNDLE,
                    "--idempotency-key": KEY,
                },
            },
        },
    },
    "interact": {
        "description": "Explicit local-lab interaction with per-invocation budgets.",
        "options": {},
        "side_effect": "subcommand_defined",
        "subcommands": {
            "observe": {"options": LOCAL, "side_effect": "readonly"},
            "click": {"options": {**LOCAL, **HASH, **REF}, "side_effect": "mutating"},
            "long-click": {"options": {**LOCAL, **HASH, **REF}, "side_effect": "mutating"},
            "input": {
                "options": {
                    **LOCAL,
                    **HASH,
                    **REF,
                    "--text": {"type": "string", "required": True, "maxLength": 4096},
                },
                "side_effect": "mutating",
            },
            "swipe": {
                "options": {**LOCAL, **HASH, "--direction": DIRECTION},
                "side_effect": "mutating",
            },
            "back": {"options": {**LOCAL, **HASH}, "side_effect": "mutating"},
            "home": {"options": {**LOCAL, **HASH}, "side_effect": "mutating"},
            "start": {
                "options": {**LOCAL, **HASH, "--bundle": BUNDLE, "--ability": {"type": "string"}},
                "side_effect": "mutating",
            },
            "wait": {"options": {**LOCAL, **SELECTOR}, "side_effect": "readonly"},
            "scroll": {
                "options": {
                    **LOCAL,
                    **SELECTOR,
                    "--direction": DIRECTION,
                    "--max-swipes": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 0,
                        "maximum": 100,
                    },
                },
                "side_effect": "mutating",
            },
        },
    },
    "ledger": {
        "description": "Inspect, reconcile, resolve, release, and retain durable action records.",
        "options": {},
        "side_effect": "subcommand_defined",
        "subcommands": {
            "list": {"options": FILTERS, "side_effect": "readonly"},
            "inspect": {"options": REQUEST, "side_effect": "readonly"},
            "reconcile": {"options": REQUEST, "side_effect": "local_ledger"},
            "resolve": {
                "options": {
                    **REQUEST,
                    "--note": {
                        "type": "string",
                        "required": True,
                        "minLength": 1,
                        "maxLength": 256,
                    },
                },
                "side_effect": "local_ledger",
            },
            "release": {
                "options": {
                    **REQUEST,
                    "--note": {
                        "type": "string",
                        "required": True,
                        "minLength": 1,
                        "maxLength": 256,
                    },
                },
                "side_effect": "local_ledger",
            },
            "prune": {
                "options": {
                    "--older-than-days": {"type": "integer", "required": True, "minimum": 1}
                },
                "side_effect": "local_ledger",
            },
            "report": {"options": {**FILTERS, "--out": OUT}, "side_effect": "local_file"},
        },
    },
    "inspect": {
        "description": "Capture a private, self-contained visual UI inspector.",
        "side_effect": "readonly",
        "options": {**DEVICE, "--timeout": TIMEOUT, "--out": OUT},
    },
    "report": {
        "description": "Render a saved JSON envelope as a private HTML report.",
        "side_effect": "local_file",
        "options": {"--input": OUT, "--out": OUT},
    },
    "record": {
        "description": "Record device screen to local MP4 via snapshot_display and host ffmpeg.",
        "side_effect": "subcommand_defined",
        "local_only": True,
        "options": {},
        "subcommands": {
            "start": {
                "description": "Start a detached background screen recording session.",
                "action_id": "record.start",
                "result_schema": RECORDING_START_RESULT_SCHEMA,
                "side_effect": "readonly",
                "options": {
                    **DEVICE,
                    "--out": OUT,
                    "--fps": {"type": "number", "default": 2.0, "minimum": 0.5, "maximum": 5.0},
                    "--max-duration": {
                        "type": "number", "default": 120.0, "minimum": 1.0, "maximum": 600.0,
                    },
                    "--max-width": {
                        "type": "integer", "default": 720, "minimum": 1, "maximum": 8192,
                    },
                    "--display-id": {"type": "integer", "minimum": 0},
                },
            },
            "stop": {
                "description": "Stop an active recording session and finalize its MP4 file.",
                "action_id": "record.stop",
                "result_schema": RECORDING_STOP_RESULT_SCHEMA,
                "side_effect": "readonly",
                "options": {
                    **DEVICE,
                    "--timeout": {**TIMEOUT, "default": 45},
                },
            },
            "status": {
                "description": "Inspect whether a recording session is active for the device.",
                "action_id": "record.status",
                "result_schema": RECORDING_STATUS_RESULT_SCHEMA,
                "side_effect": "readonly",
                "options": {**DEVICE},
            },
            "capture": {
                "description": "Record the device screen for a fixed duration in the foreground.",
                "side_effect": "readonly",
                "options": {
                    **DEVICE,
                    "--out": OUT,
                    "--duration": {
                        "type": "number", "required": True, "minimum": 1.0, "maximum": 600.0,
                    },
                    "--fps": {"type": "number", "default": 2.0, "minimum": 0.5, "maximum": 5.0},
                    "--max-width": {
                        "type": "integer", "default": 720, "minimum": 1, "maximum": 8192,
                    },
                    "--display-id": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}


def add_operation_parsers(
    subparsers: argparse._SubParsersAction[Any], shared: argparse.ArgumentParser
) -> None:
    for name, schema in OPERATION_SCHEMAS.items():
        parser = subparsers.add_parser(name, parents=[shared], help=schema["description"])
        leaves = [(parser, schema)]
        if "subcommands" in schema:
            commands = parser.add_subparsers(dest="operation", required=True)
            leaves = [
                (commands.add_parser(command, parents=[shared]), child)
                for command, child in schema["subcommands"].items()
            ]
        for leaf, definition in leaves:
            for option, spec in definition["options"].items():
                kwargs: dict[str, Any] = {"required": spec.get("required", False)}
                if spec["type"] == "boolean":
                    kwargs["action"] = "store_true"
                else:
                    kwargs["type"] = {"string": str, "number": float, "integer": int}[spec["type"]]
                if "default" in spec:
                    kwargs["default"] = spec["default"]
                if "enum" in spec:
                    kwargs["choices"] = spec["enum"]
                if spec.get("repeatable"):
                    kwargs["action"] = "append"
                if "description" in spec:
                    kwargs["help"] = spec["description"]
                leaf.add_argument(option, **kwargs)


def _json_object(text: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    try:
        if len(text.encode()) > MAX_JSON_BYTES:
            raise ValueError("Too large")
        result = json.loads(text, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise ValueError("Object required")
        return result
    except (ValueError, RecursionError) as exc:
        raise DeviceConfigurationError("Expected a bounded JSON object with unique keys.") from exc


def _device(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> HarmonyDevice:
    devices = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    hdc = HdcClient(transport=transport, hdc_path=args.hdc_path, env=env)
    profile, _ = devices.resolve(args.device, hdc.list_targets(), require_online=True)
    return HarmonyDevice(hdc, profile, state_dir=args.state_dir)


def run_operation(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> tuple[int, dict[str, Any], str]:
    definition = OPERATION_SCHEMAS[args.command]
    if "subcommands" in definition:
        definition = definition["subcommands"][args.operation]
    for option, spec in definition["options"].items():
        value = getattr(args, option[2:].replace("-", "_"))
        if value is not None:
            schema = {
                key: item
                for key, item in spec.items()
                if key not in {"required", "default", "repeatable"}
            }
            for item in value if spec.get("repeatable") else [value]:
                validate_value(item, schema)
    if args.command == "files":
        data = _files(args, transport=transport, cwd=cwd, env=env)
    elif args.command == "sms":
        data = _sms(args, transport=transport, cwd=cwd, env=env)
    elif args.command == "record":
        data = _record(args, transport=transport, cwd=cwd, env=env)
    elif args.command == "apps":
        data = _apps(args, transport=transport, cwd=cwd, env=env)
    elif args.command == "ledger":
        data = _ledger(args, transport=transport, cwd=cwd, env=env)
    elif args.command == "receipts":
        from moyu_harmony_pilot.bridge.protocol import identifier
        from moyu_harmony_pilot.bridge.store import ReceiptStore

        path = Path(args.database)
        if not path.is_file():
            raise DeviceConfigurationError("Choose an existing Bridge receipt database.")
        store = ReceiptStore(path, identifier(args.bridge_id))
        data = {"archived": store.prune(older_than_days=args.older_than_days),
                "job_tombstones_retained": True}
    elif args.command == "report":
        source = Path(args.input)
        if source.stat().st_size > 8 * 1024 * 1024:
            raise DeviceConfigurationError("Report input exceeds 8 MiB.")
        try:
            payload = json.loads(source.read_text())
            records = payload if isinstance(payload, list) else [payload]
            if not all(isinstance(record, dict) for record in records):
                raise ValueError("Objects required")
        except (ValueError, TypeError) as exc:
            raise DeviceConfigurationError("Report input must contain JSON records.") from exc
        data = {"path": str(write_report(payload, args.out))}
    else:
        device = _device(args, transport=transport, cwd=cwd, env=env)
        if args.command == "inspect":
            data = capture_inspector(device, args.out, timeout=args.timeout)
        elif args.command == "ocr":
            from moyu_harmony_pilot.actions.observation import capture_ocr

            data = capture_ocr(
                device, backend=args.backend, max_width=args.max_width, timeout=args.timeout
            )
        elif args.command == "fixture":
            from moyu_harmony_pilot.actions.fixtures import capture_fixture

            data = capture_fixture(
                device, args.out, timeout=args.timeout,
                keep_texts=args.keep_text, keep_bundles=args.keep_bundle,
            )
        else:
            data = _interact(args, device)
    code = {
        "succeeded": 0,
        "already_done": 0,
        "configuration_error": 2,
        "device_offline": 3,
        "human_required": 4,
        "device_overheated": 5,
    }.get(data.get("status", "succeeded"), 1)
    return code, data, json.dumps(data, ensure_ascii=False, indent=2)


def _files(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> dict[str, Any]:
    if args.operation == "query":
        validate_media_query(args.name, limit=args.limit, timeout=args.timeout)
    else:
        validate_file_export(
            args.source, args.out, overwrite=args.overwrite, timeout=args.timeout
        )
    device = _device(args, transport=transport, cwd=cwd, env=env)
    with device.acquire_lock():
        if args.operation == "query":
            result = device.query_media(args.name, limit=args.limit, timeout=args.timeout).to_dict()
        else:
            result = device.export_file(
                args.source, args.out, overwrite=args.overwrite, timeout=args.timeout
            ).to_dict()
    return {"alias": device.alias, **result}


def _sms(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> dict[str, Any]:
    if args.timeout <= 0:
        raise DeviceConfigurationError("SMS timeout must be positive.")
    if args.operation == "read":
        from moyu_harmony_pilot.actions.sms import read_sms, validate_sms_options

        validate_sms_options(
            limit=args.limit, sender=args.sender, wait_seconds=args.wait_seconds,
            timeout=args.timeout,
        )

        try:
            # Private message text belongs only in the requested result.
            with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                return read_sms(
                    _device(args, transport=transport, cwd=cwd, env=env),
                    limit=args.limit, sender=args.sender, wait_seconds=args.wait_seconds,
                    timeout=args.timeout,
                )
        except (DeviceError, HdcError, UiError) as exc:
            raise type(exc)(
                "SMS observation stopped.", code=exc.code, retryable=exc.retryable,
                hint=SMS_ERROR_HINTS.get(exc.code, type(exc).default_hint),
            ) from None
    runner = ActionRunner(ActionRegistry.discover(env=env), state_dir=args.state_dir)
    runner.prepare(
        "sms.open", {}, idempotency_key=args.idempotency_key, timeout=args.timeout
    )
    device = _device(args, transport=transport, cwd=cwd, env=env)
    outcome = runner.run(
        device, "sms.open", {}, idempotency_key=args.idempotency_key, timeout=args.timeout
    )
    return {"alias": device.alias, "action": "sms.open", "params": {}, **outcome.to_dict()}


def _apps(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> dict[str, Any]:
    if args.operation == "download":
        return download_package(args.url, args.out, sha256=args.sha256, timeout=args.timeout)
    params = {}
    if args.operation == "install":
        packages = [*args.hap, *args.package]
        if bool(packages) == bool(args.url):
            raise DeviceConfigurationError("Provide --package/--hap paths or one --url.")
        if args.sha256 and not args.url:
            raise DeviceConfigurationError("--sha256 applies to --url downloads.")
        if args.url:
            try:
                suffix = Path(unquote(urlsplit(args.url).path)).suffix.lower()
            except ValueError as exc:
                raise DeviceConfigurationError("Invalid package URL.") from exc
            if suffix not in PACKAGE_SUFFIXES:
                suffix = ".hap"
            filename = hashlib.sha256(args.url.encode()).hexdigest() + suffix
            directory = ensure_secure_dir(Path(args.state_dir) / "packages")
            metadata = [
                download_package(
                    args.url,
                    directory / filename,
                    sha256=args.sha256,
                    timeout=args.timeout,
                )
            ]
        else:
            metadata = [package_metadata(path) for path in packages]
        identities = {(item["bundle_name"], item["version_code"]) for item in metadata}
        if len(identities) != 1:
            raise DeviceConfigurationError("All packages must have the same bundle and version.")
        bundle, version = identities.pop()
        params = {
            "bundle_name": bundle,
            "version_code": version,
            "packages": [{"path": item["path"], "sha256": item["sha256"]} for item in metadata],
        }
    device = _device(args, transport=transport, cwd=cwd, env=env)
    if args.operation in {"list", "info"}:
        with device.acquire_lock():
            if args.operation == "list":
                return {"alias": device.alias, "apps": device.list_apps(timeout=args.timeout)}
            info = device.app_info(args.bundle, timeout=args.timeout)
            return {
                "alias": device.alias,
                "installed": info is not None,
                "app": info.to_dict() if info else None,
            }
    if args.operation == "uninstall":
        params = {"bundle_name": args.bundle}
    action = "app." + args.operation
    runner = ActionRunner(ActionRegistry.discover(env=env), state_dir=args.state_dir)
    outcome = runner.run(
        device, action, params, idempotency_key=args.idempotency_key, timeout=args.timeout
    )
    return {"alias": device.alias, "action": action, "params": params, **outcome.to_dict()}


def _ledger(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> dict[str, Any]:
    ledger = ActionLedger(Path(args.state_dir) / "actions.sqlite3")
    if args.operation == "prune":
        return {"removed": ledger.prune(older_than_days=args.older_than_days)}
    device_hash = None
    profile = None
    if args.device:
        registry = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
        profile = registry.get_by_alias(args.device)
        if profile is None:
            raise DeviceConfigurationError("Unknown configured device alias.")
        device_hash = digest(profile.serial)
    if args.operation in {"list", "report"}:
        rows = ledger.records(
            device_hash=device_hash,
            action_id=args.action,
            status=args.status,
            limit=args.limit,
            offset=args.offset,
        )
        data = {"records": rows, "offset": args.offset, "limit": args.limit}
        if args.operation == "report":
            data["path"] = str(write_report(rows, args.out))
        return data
    if args.operation == "inspect":
        rows = ledger.records(
            device_hash=device_hash, action_id=args.action, key_hash=digest(args.idempotency_key)
        )
        return {"record": rows[0] if rows else None}
    if args.operation in {"resolve", "release"}:
        label = "Resolution" if args.operation == "resolve" else "Release"
        if profile is None:
            raise DeviceConfigurationError(f"{label} requires a configured device alias.")
        actions = ActionRegistry.discover(env=env)
        runner = ActionRunner(actions, state_dir=args.state_dir)
        _, spec, params, _ = runner.prepare(
            args.action,
            _json_object(args.params_json),
            idempotency_key=args.idempotency_key,
            timeout=args.timeout,
        )
        if spec.side_effect != "mutating":
            raise DeviceConfigurationError(f"{label} requires a mutating action.")
        key = ledger.make_key(
            profile.serial, args.action, args.idempotency_key, spec.version, params
        )
        with DeviceLock(
            profile.serial, alias=profile.alias, lock_dir=Path(args.state_dir) / "locks"
        ):
            if args.operation == "release":
                ledger.release(key, note=args.note)
                return {
                    "alias": profile.alias,
                    "action": args.action,
                    "released": True,
                    "summary": "Uncertain reservation released by operator for retry.",
                }
            outcome = ledger.lookup(key)
            if outcome is None:
                raise DeviceConfigurationError("No matching prior submission.")
            if outcome.status == "unknown":
                outcome = ActionOutcome(summary="Completion explicitly verified by operator.")
                ledger.resolve(key, outcome, note=args.note)
        return {"alias": profile.alias, "action": args.action, **outcome.to_dict()}
    device = _device(args, transport=transport, cwd=cwd, env=env)
    runner = ActionRunner(ActionRegistry.discover(env=env), state_dir=args.state_dir)
    outcome = runner.reconcile(
        device,
        args.action,
        _json_object(args.params_json),
        idempotency_key=args.idempotency_key,
        timeout=args.timeout,
        note=getattr(args, "note", None),
    )
    return {"alias": device.alias, "action": args.action, **outcome.to_dict()}


def _interact(args: argparse.Namespace, device: HarmonyDevice) -> dict[str, Any]:
    session = InteractionSession(device, max_steps=args.max_steps, timeout=args.timeout)
    operation = args.operation
    if operation == "observe":
        return {"alias": device.alias, "snapshot": session.observe().to_dict()}
    if operation in {"wait", "scroll"}:
        selector = selector_from_dict(_json_object(args.selector_json))
        if operation == "wait":
            node = session.wait_for(selector)
        else:
            node = session.scroll_until(
                selector, direction=args.direction, max_swipes=args.max_swipes
            )
        return {
            "id": node.id,
            "text": node.text,
            "type": node.type,
            "bounds": node.bounds.to_list(),
        }
    kwargs = {"layout_hash": args.layout_hash}
    if operation == "input":
        return session.input_text_ref(args.ref, args.text, **kwargs)
    if operation == "start":
        return session.start_app(args.bundle, ability_name=args.ability, **kwargs)
    if operation == "swipe":
        return session.swipe(args.direction, **kwargs)
    if operation in {"click", "long-click"}:
        method = session.click_ref if operation == "click" else session.long_click_ref
        return method(args.ref, **kwargs)
    return session.home(**kwargs) if operation == "home" else session.back(**kwargs)


def _record(
    args: argparse.Namespace, *, transport: HdcTransport | None,
    cwd: Path | None, env: Mapping[str, str] | None,
) -> dict[str, Any]:
    from moyu_harmony_pilot.device import (
        RecordingSessionManager,
        ScreenRecorder,
        require_ffmpeg,
        validate_recording_options,
    )

    if args.operation == "start":
        validate_recording_options(
            output_path=args.out,
            fps=args.fps,
            max_duration=args.max_duration,
            max_width=args.max_width,
            display_id=args.display_id,
        )
        require_ffmpeg()
        device = _device(args, transport=transport, cwd=cwd, env=env)
        manager = RecordingSessionManager(args.state_dir)
        info = manager.start(
            device,
            args.out,
            fps=args.fps,
            max_duration=args.max_duration,
            max_width=args.max_width,
            display_id=args.display_id,
            hdc_path=args.hdc_path,
        )
        return info.to_dict()

    if args.operation == "stop":
        device = _device(args, transport=transport, cwd=cwd, env=env)
        manager = RecordingSessionManager(args.state_dir)
        return manager.stop(device, timeout=args.timeout).to_dict()

    if args.operation == "status":
        device = _device(args, transport=transport, cwd=cwd, env=env)
        manager = RecordingSessionManager(args.state_dir)
        active_info = manager.status(device)
        if active_info is None:
            return {"alias": device.alias, "active": False}
        return active_info.to_dict()

    validate_recording_options(
        output_path=args.out,
        fps=args.fps,
        max_duration=args.duration,
        max_width=args.max_width,
        display_id=args.display_id,
    )
    require_ffmpeg()
    device = _device(args, transport=transport, cwd=cwd, env=env)
    device.assert_thermal_safe(timeout=10.0)
    recorder = ScreenRecorder(
        device,
        args.out,
        fps=args.fps,
        max_duration=args.duration,
        max_width=args.max_width,
        display_id=args.display_id,
    )
    recorder.start(capture_initial_frame=True)
    stopped_reason = "duration_limit"
    try:
        recorder._stop_event.wait(timeout=float(args.duration) + 1.0)
    except KeyboardInterrupt:
        stopped_reason = "interrupted"
    result = recorder.stop(
        capture_final_frame=(stopped_reason == "interrupted"),
        stopped_reason=stopped_reason,
    )
    return result.to_dict()
