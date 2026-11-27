"""Self-describing JSON Schema contract emitted by `harmony-pilot schema`."""

from __future__ import annotations

from typing import Any

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ACTION_API_VERSION, ActionStatus
from moyu_harmony_pilot.actions.models import PLUGIN_CALL_MIN_VERSION
from moyu_harmony_pilot.actions.validation import (
    MAX_JSON_BYTES,
    MAX_RESULT_BYTES,
    SCHEMA_KEYWORDS,
)
from moyu_harmony_pilot.cli.codes import EXIT_CODE_DESCRIPTIONS
from moyu_harmony_pilot.cli.envelope import SCHEMA_VERSION
from moyu_harmony_pilot.cli.operations import OPERATION_SCHEMAS
from moyu_harmony_pilot.device import ScreenState
from moyu_harmony_pilot.hdc import ConnectionState
from moyu_harmony_pilot.ui import SENSITIVE_SIGNAL_ENUM

ACTION_STATUS_ENUM: tuple[str, ...] = tuple(status.value for status in ActionStatus)

def build_cli_schema() -> dict[str, Any]:
    """Return the complete machine-readable CLI contract for zero-guess Agent invocation."""
    schema: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "pilot_version": __version__,
        "program": "harmony-pilot",
        "mcp_adapter": {
            "program": "harmony-pilot-mcp", "extra": "mcp", "sdk_major": 2,
            "transport": "stdio", "default_mode": "safe",
            "options": {
                "--config": {"type": "string"}, "--hdc-path": {"type": "string"},
                "--state-dir": {"type": "string", "default": ".state"},
                "--allow-action": {"type": "string", "repeatable": True, "default": []},
                "--local-lab": {"type": "boolean", "default": False},
                "--max-steps": {"type": "integer", "default": 20, "minimum": 1, "maximum": 1000},
                "--timeout": {"type": "number", "default": 60, "maximum": 3600},
                "--inline-images": {"type": "boolean", "default": False},
                "--max-image-bytes": {"type": "integer", "default": 2097152,
                                      "minimum": 1, "maximum": 16777216},
                "--enable-ocr": {"type": "boolean", "default": False},
                "--enable-sms": {"type": "boolean", "default": False},
                "--enable-recording": {"type": "boolean", "default": False},
            },
            "screenshot_inline_requires_startup_opt_in": True,
            "ocr_requires_startup_opt_in": True,
            "recording_requires_startup_opt_in": True,
        },
        "action_contract": {
            "api_version": ACTION_API_VERSION,
            "legacy_api_version": 1,
            "schema_keywords": sorted(SCHEMA_KEYWORDS),
            "max_json_bytes": MAX_JSON_BYTES,
            "max_nesting": 8,
            "max_metrics": 32,
            "max_metric_key_length": 64,
            "max_metric_string_length": 128,
            "max_summary_length": 256,
            "error_code_pattern": "[a-z][a-z0-9_]{0,63}",
            "objects_require_additionalProperties_false": True,
            "mutating_requires_idempotency_key": True,
            "unknown_replay_allowed": False,
            "plugin_paths_environment": "HARMONY_PILOT_PLUGIN_PATHS",
            "local_plugin_manifest": "actions.json",
            "execution_thread": "main",
            "context_api_min_pilot_version": "0.2.0.dev0",
            "plugin_sources": ["internal", "external"],
            "context_call": "ctx.call(action_id, params, idempotency_key=None, timeout=None)",
            "context_call_min_pilot_version": PLUGIN_CALL_MIN_VERSION,
            "context_call_max_nesting": 8,
            "context_call_mutating_stages": ["preparing", "submit"],
            "context_call_inherits_authorization": True,
            "result_schema_field": "result_schema",
            "max_result_json_bytes": MAX_RESULT_BYTES,
            "result_data_persisted": False,
            "minimum_version_field": "min_pilot_version",
            "inspection_metrics_schema_field": "inspection_metrics_schema",
            "required_metrics_scope": "successful_final_receipts",
            "context_mutation_stage": "submit",
        },
        "bridge_config": {
            "type": "object",
            "required": ["bridge_id", "broker_url", "devices_config", "allowed_actions"],
            "additionalProperties": False,
            "properties": {
                "bridge_id": {"type": "string"},
                "broker_url": {"type": "string"},
                "devices_config": {"type": "string"},
                "state_dir": {"type": "string", "default": ".state"},
                "token_env": {"type": "string", "default": "HARMONY_PILOT_BRIDGE_TOKEN"},
                "token_file": {"type": "string"},
                "allowed_actions": {
                    "type": "object", "minProperties": 1, "maxProperties": 32,
                    "additionalProperties": {
                        "type": "array", "maxItems": 64, "items": {"type": "string"},
                    },
                },
                "plugin_paths": {
                    "type": "array", "maxItems": 32,
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Config-relative plugin directories. When present, replaces "
                        "HARMONY_PILOT_PLUGIN_PATHS. Installed entry points remain available."
                    ),
                },
                "heartbeat_seconds": {
                    "type": "number", "minimum": 1, "maximum": 300, "default": 30,
                },
                "poll_seconds": {
                    "type": "number", "minimum": 0.1, "maximum": 300, "default": 15,
                },
                "wait_seconds": {
                    "type": "integer", "minimum": 0, "maximum": 25, "default": 15,
                },
                "lease_seconds": {
                    "type": "number", "minimum": 0.1, "maximum": 30, "default": 15,
                },
                "request_timeout": {
                    "type": "number", "minimum": 0.1, "maximum": 60, "default": 10,
                },
            },
        },
        "envelope_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["schema_version", "ok", "command", "data", "error"],
            "properties": {
                "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
                "ok": {"type": "boolean"},
                "command": {"type": "string"},
                "data": {},
                "error": {
                    "type": ["object", "null"],
                    "required": ["code", "message", "retryable"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "retryable": {"type": "boolean"},
                        "hint": {"type": ["string", "null"]},
                    },
                },
            },
        },
        "exit_codes": {str(code): meta for code, meta in EXIT_CODE_DESCRIPTIONS.items()},
        "enums": {
            "connection_states": [s.value for s in ConnectionState],
            "screen_states": [s.value for s in ScreenState],
            "action_statuses": list(ACTION_STATUS_ENUM),
            "sensitive_signals": list(SENSITIVE_SIGNAL_ENUM),
        },
        "global_options": {
            "--json": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Emit a single structured JSON envelope on stdout; logs go to stderr."
                ),
            },
            "--config": {
                "type": "string",
                "description": (
                    "Device alias config, or the private Bridge JSON config for serve."
                ),
            },
            "--hdc-path": {
                "type": "string",
                "description": "Explicit path to official HarmonyOS/OpenHarmony hdc binary.",
            },
            "--state-dir": {
                "type": "string",
                "default": ".state",
                "description": (
                    "Local state directory for mode-0700 device locks and diagnostics. "
                    "For serve, set state_dir in its Bridge config."
                ),
            },
            "--version": {
                "type": "boolean",
                "description": "Print package version and exit.",
            },
        },
        "commands": {
            "serve": {
                "side_effect": "action_defined",
                "description": "Outbound protocol-v1 worker with per-device capabilities.",
                "options": {
                    "--config": {"type": "string", "required": True,
                                 "description": "Private Bridge JSON configuration path."},
                    "--once": {"type": "boolean", "default": False},
                    "--check": {"type": "boolean", "default": False},
                    "--probe": {"type": "boolean", "default": False,
                                  "description": "Read telemetry and send a heartbeat only."},
                    "--health": {"type": "boolean", "default": False},
                },
                "mutually_exclusive": [["--once", "--check", "--probe", "--health"]],
                "examples": ["harmony-pilot serve --config bridge.local.json --check --json",
                             "harmony-pilot serve --config bridge.local.json --probe --json",
                             "harmony-pilot serve --config bridge.local.json --json"],
            },
            "plugins": {
                "side_effect": "subcommand_defined",
                "description": "Create projects and discover or validate trusted local plugins.",
                "options": {},
                "subcommands": {
                    "list": {"options": {}, "side_effect": "readonly"},
                    "describe": {"side_effect": "readonly", "arguments": {"action_id": {
                        "type": "string", "required": True,
                    }}},
                    "validate": {
                        "side_effect": "readonly",
                        "description": (
                            "Validate plugin metadata, parameters and any required idempotency "
                            "key offline; does not check a device or execute the action."
                        ),
                        "arguments": {"action_id": {"type": "string", "required": True}},
                        "options": {
                            "--params-json": {"type": "string", "default": "{}"},
                            "--idempotency-key": {
                                "type": "string",
                                "description": (
                                    "Required for mutating actions: 1–256 characters and not "
                                    "whitespace only. Ignored for readonly actions."
                                ),
                            },
                        },
                        "result_schema": {
                            "type": "object",
                            "required": [
                                "valid", "action_id", "api_version", "version", "side_effect",
                                "device_checked", "executed",
                            ],
                            "properties": {
                                "valid": {"const": True},
                                "action_id": {"type": "string"},
                                "api_version": {"type": "integer"},
                                "version": {"type": "string"},
                                "side_effect": {"enum": ["readonly", "mutating"]},
                                "device_checked": {"const": False},
                                "executed": {"const": False},
                            },
                        },
                    },
                    "init": {
                        "side_effect": "local_file",
                        "description": (
                            "Create a new independent plugin directory with two examples and "
                            "offline contracts using bundled files."
                        ),
                        "arguments": {"destination": {"type": "string", "required": True}},
                        "options": {
                            "--namespace": {
                                "type": "string", "required": True, "maxLength": 114,
                                "description": (
                                    "Action ID prefix starting with a letter, followed by "
                                    "letters, digits, underscores, periods or hyphens."
                                ),
                            },
                            "--package": {
                                "type": "string",
                                "description": (
                                    "Distribution name, defaults to directory name. Its normalized "
                                    "module must be a valid unreserved Python identifier."
                                ),
                            },
                        },
                        "result_schema": {
                            "type": "object",
                            "required": [
                                "path", "package_name", "module_name", "action_ids",
                                "files", "next_steps",
                            ],
                            "properties": {
                                "path": {"type": "string"},
                                "package_name": {"type": "string"},
                                "module_name": {"type": "string"},
                                "action_ids": {"type": "array", "items": {"type": "string"}},
                                "files": {"type": "array", "items": {"type": "string"}},
                                "next_steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["cwd", "argv"],
                                        "properties": {
                                            "cwd": {"type": "string"},
                                            "argv": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                "examples": ["harmony-pilot plugins list --json",
                             "harmony-pilot plugins describe device.health --json",
                             "harmony-pilot plugins validate device.health --json",
                             "harmony-pilot plugins init ./team-pilot --namespace team --json"],
            },
            "run": {
                "side_effect": "action_defined",
                "description": (
                    "Execute a validated action with device locking and durable deduplication."
                ),
                "options": {
                    "--device": {"type": "string"},
                    "--action": {"type": "string", "required": True},
                    "--params-json": {"type": "string", "default": "{}"},
                    "--idempotency-key": {"type": "string", "maxLength": 256},
                    "--timeout": {"type": "number", "exclusiveMinimum": 0,
                                  "description": "Total deadline capped by the plugin default."},
                    "--max-steps": {"type": "integer", "minimum": 1, "maximum": 10000,
                                    "default": 100,
                                    "description": "Maximum stage and plugin checkpoints."},
                    "--dry-run": {"type": "boolean", "default": False,
                                  "description": (
                                      "Validate selection and parameters using device enumeration."
                                  )},
                    "--record": {"type": "boolean", "default": False,
                                 "description": (
                                     "Record device screen to MP4 during action execution."
                                 )},
                    "--record-out": {"type": "string",
                                     "description": (
                                         "Save action recording MP4 to FILE (enables recording)."
                                     )},
                    "--record-on-failure": {"type": "boolean", "default": False,
                                            "description": (
                                                "Keep recorded MP4 only when action does not "
                                                "succeed (enables recording)."
                                            )},
                    "--record-fps": {"type": "number", "default": 2.0,
                                     "minimum": 0.5, "maximum": 5.0},
                    "--record-max-width": {"type": "integer", "default": 720,
                                           "minimum": 1, "maximum": 8192},
                },
                "examples": ["harmony-pilot run --device harmony-01 --action device.health --json"],
            },
            "devices": {
                "side_effect": "readonly",
                "description": (
                    "List connected HarmonyOS NEXT targets, configured alias mappings, "
                    "lock state, and basic battery/screen telemetry (raw serials are masked)."
                ),
                "options": {
                    "--no-health": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip per-device battery/screen telemetry queries.",
                    },
                    "--timeout": {
                        "type": "number",
                        "default": 10.0,
                        "description": "Per-command hdc timeout in seconds.",
                    },
                },
                "examples": [
                    "harmony-pilot devices --json",
                    "harmony-pilot devices --no-health --json",
                ],
            },
            "doctor": {
                "side_effect": "readonly",
                "description": (
                    "Validate hdc binary, Python runtime, local config/state permissions, "
                    "device connectivity, uitest availability, and optional deep smoke checks."
                ),
                "options": {
                    "--device": {
                        "type": "string",
                        "description": "Optional device alias to validate specifically.",
                    },
                    "--deep": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Also perform read-only dumpLayout + snapshot_display smoke check "
                            "with guaranteed temporary file cleanup."
                        ),
                    },
                    "--timeout": {
                        "type": "number",
                        "default": 15.0,
                        "description": "Per-command hdc timeout in seconds.",
                    },
                },
                "examples": [
                    "harmony-pilot doctor --json",
                    "harmony-pilot doctor --device harmony-01 --deep --json",
                ],
            },
            "dump-ui": {
                "side_effect": "readonly",
                "description": (
                    "Capture HarmonyOS NEXT UI hierarchy via `uitest dumpLayout`, clean up "
                    "remote temporary files, and emit a deterministic low-token `@eN` compact "
                    "snapshot with `layout_hash`."
                ),
                "options": {
                    "--device": {
                        "type": "string",
                        "description": (
                            "Device alias (optional when exactly one online device is attached)."
                        ),
                    },
                    "--compact": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Emit/write pruned `@eN` compact semantic snapshot. "
                            "When `--out` is omitted, stdout always returns compact output."
                        ),
                    },
                    "--out": {
                        "type": "string",
                        "description": "Optional local output file path (written with mode 0600).",
                    },
                    "--max-elements": {
                        "type": "integer",
                        "default": 120,
                        "description": (
                            "Maximum elements kept in compact snapshot (interactive prioritized)."
                        ),
                    },
                    "--include-attrs": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Pass `-a` to `uitest dumpLayout` to include extended attributes."
                        ),
                    },
                    "--bundle": {
                        "type": "string",
                        "description": "Optional target window bundleName (`-b`).",
                    },
                    "--window-id": {
                        "type": "integer",
                        "description": "Optional target window ID (`-w`).",
                    },
                    "--no-merge": {
                        "type": "boolean",
                        "default": False,
                        "description": "Disable multi-window merge (`-m false`).",
                    },
                    "--display-id": {
                        "type": "integer",
                        "description": "Optional target display ID (`-d`).",
                    },
                    "--wait-lock": {
                        "type": "number",
                        "default": 0.0,
                        "description": (
                            "Seconds to wait for exclusive device lock before exit code 6."
                        ),
                    },
                    "--timeout": {
                        "type": "number",
                        "default": 20.0,
                        "description": "Timeout in seconds for dumpLayout and file transfer.",
                    },
                },
                "examples": [
                    "harmony-pilot dump-ui --device harmony-01 --compact --json",
                    "harmony-pilot dump-ui --device harmony-01 --out .state/screen.json --json",
                ],
            },
            "screenshot": {
                "side_effect": "readonly",
                "description": (
                    "Capture a device screenshot via `snapshot_display` (JPEG) or "
                    "`uitest screenCap` (PNG), save locally with mode 0600, and clean up "
                    "remote temporary files."
                ),
                "options": {
                    "--device": {
                        "type": "string",
                        "description": (
                            "Device alias (optional when exactly one online device is attached)."
                        ),
                    },
                    "--out": {
                        "type": "string",
                        "required": True,
                        "description": (
                            "Local destination path for the screenshot (written with mode 0600)."
                        ),
                    },
                    "--format": {
                        "type": "string",
                        "enum": ["jpeg", "png"],
                        "description": (
                            "Image format (defaults from --out extension, otherwise jpeg)."
                        ),
                    },
                    "--display-id": {
                        "type": "integer",
                        "description": "Optional display ID for multi-display devices.",
                    },
                    "--max-width": {
                        "type": "integer",
                        "description": (
                            "Optional maximum pixel width; downscales wider screenshots in-place."
                        ),
                    },
                    "--som": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Set-of-Mark references and matching snapshot using the vision extra."
                        ),
                    },
                    "--wait-lock": {
                        "type": "number",
                        "default": 0.0,
                        "description": (
                            "Seconds to wait for exclusive device lock before exit code 6."
                        ),
                    },
                    "--timeout": {
                        "type": "number",
                        "default": 20.0,
                        "description": "Timeout in seconds for capture and file transfer.",
                    },
                },
                "examples": [
                    "harmony-pilot screenshot --device harmony-01 --out .state/cap.jpeg --json",
                    "harmony-pilot screenshot --device harmony-01 --out cap.jpeg --max-width 720",
                ],
            },
            "schema": {
                "side_effect": "readonly",
                "description": (
                    "Emit complete machine-readable CLI commands, options, envelope schema, "
                    "status enums, and semantic exit codes (100% offline)."
                ),
                "options": {},
                "examples": [
                    "harmony-pilot schema",
                    "harmony-pilot schema --json",
                ],
            },
        },
    }
    schema["commands"].update(OPERATION_SCHEMAS)
    schema["bridge_config"]["properties"]["max_jobs"] = {
        "type": "integer", "minimum": 1, "maximum": 32, "default": 1,
    }
    return schema
