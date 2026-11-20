"""CLI adapters for applications, local interaction, reconciliation, and reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from moyu_harmony_pilot.actions import (
    ActionOutcome,
    ActionRegistry,
    ActionRunner,
    InteractionSession,
)
from moyu_harmony_pilot.actions.ledger import ActionLedger, digest
from moyu_harmony_pilot.actions.reports import capture_inspector, write_report
from moyu_harmony_pilot.actions.validation import MAX_JSON_BYTES, validate_value
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceLock,
    DeviceRegistry,
    HarmonyDevice,
    ensure_secure_dir,
)
from moyu_harmony_pilot.device.packages import download_hap, hap_metadata
from moyu_harmony_pilot.hdc import HdcClient
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

OPERATION_SCHEMAS = {
    "apps": {
        "description": "Query bundles and deploy verified local or downloaded HAPs.",
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
                    "--out": OUT,
                    "--sha256": {"type": "string"},
                    "--timeout": TIMEOUT,
                },
            },
            "install": {
                "side_effect": "mutating",
                "options": {
                    **DEVICE,
                    "--timeout": {**TIMEOUT, "default": 300},
                    "--hap": {"type": "string", "repeatable": True, "default": []},
                    "--url": {"type": "string"},
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
        "description": "Inspect, reconcile, resolve, and retain durable action records.",
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
}


def add_operation_parsers(subparsers, shared) -> None:
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
                kwargs = {"required": spec.get("required", False)}
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
                leaf.add_argument(option, **kwargs)


def _json_object(text: str) -> dict:
    def unique(pairs):
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


def _device(args, *, transport, cwd, env) -> HarmonyDevice:
    devices = DeviceRegistry.load_default(args.config, cwd=cwd, env=env)
    hdc = HdcClient(transport=transport, hdc_path=args.hdc_path, env=env)
    profile, _ = devices.resolve(args.device, hdc.list_targets(), require_online=True)
    return HarmonyDevice(hdc, profile, state_dir=args.state_dir)


def run_operation(args: argparse.Namespace, *, transport, cwd, env) -> tuple[int, dict, str]:
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
    if args.command == "apps":
        data = _apps(args, transport=transport, cwd=cwd, env=env)
    elif args.command == "ledger":
        data = _ledger(args, transport=transport, cwd=cwd, env=env)
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


def _apps(args, *, transport, cwd, env) -> dict:
    if args.operation == "download":
        return download_hap(args.url, args.out, sha256=args.sha256, timeout=args.timeout)
    params = {}
    if args.operation == "install":
        if bool(args.hap) == bool(args.url):
            raise DeviceConfigurationError("Provide --hap paths or one --url.")
        if args.sha256 and not args.url:
            raise DeviceConfigurationError("--sha256 applies to --url downloads.")
        if args.url:
            filename = hashlib.sha256(args.url.encode()).hexdigest() + ".hap"
            directory = ensure_secure_dir(Path(args.state_dir) / "packages")
            metadata = [
                download_hap(
                    args.url,
                    directory / filename,
                    sha256=args.sha256,
                    timeout=args.timeout,
                )
            ]
        else:
            metadata = [hap_metadata(path) for path in args.hap]
        identities = {(item["bundle_name"], item["version_code"]) for item in metadata}
        if len(identities) != 1:
            raise DeviceConfigurationError("All HAPs must have the same bundle and version.")
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


def _ledger(args, *, transport, cwd, env) -> dict:
    ledger = ActionLedger(Path(args.state_dir) / "actions.sqlite3")
    if args.operation == "prune":
        return {"removed": ledger.prune(older_than_days=args.older_than_days)}
    device_hash = None
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
    if args.operation == "resolve":
        actions = ActionRegistry.discover(env=env)
        runner = ActionRunner(actions, state_dir=args.state_dir)
        _, spec, params, _ = runner.prepare(
            args.action,
            _json_object(args.params_json),
            idempotency_key=args.idempotency_key,
            timeout=args.timeout,
        )
        if spec.side_effect != "mutating":
            raise DeviceConfigurationError("Resolution requires a mutating action.")
        key = ledger.make_key(
            profile.serial, args.action, args.idempotency_key, spec.version, params
        )
        with DeviceLock(
            profile.serial, alias=profile.alias, lock_dir=Path(args.state_dir) / "locks"
        ):
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


def _interact(args, device) -> dict:
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
