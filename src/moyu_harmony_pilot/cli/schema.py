"""Self-describing JSON Schema contract emitted by `harmony-pilot schema`."""

from __future__ import annotations

from typing import Any

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ActionStatus
from moyu_harmony_pilot.actions.validation import MAX_JSON_BYTES, SCHEMA_KEYWORDS
from moyu_harmony_pilot.cli.codes import EXIT_CODE_DESCRIPTIONS
from moyu_harmony_pilot.cli.envelope import SCHEMA_VERSION
from moyu_harmony_pilot.device import ScreenState
from moyu_harmony_pilot.hdc import ConnectionState

ACTION_STATUS_ENUM: tuple[str, ...] = tuple(status.value for status in ActionStatus)

SENSITIVE_SIGNAL_ENUM: tuple[str, ...] = (
    "password_field",
    "captcha_prompt",
    "payment_confirmation",
    "system_permission_dialog",
)


def build_cli_schema() -> dict[str, Any]:
    """Return the complete machine-readable CLI contract for zero-guess Agent invocation."""
    return {
        "schema_version": SCHEMA_VERSION,
        "pilot_version": __version__,
        "program": "harmony-pilot",
        "action_contract": {
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
                    "Path to local device alias config (devices.local.yaml or .json)."
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
                    "Local state directory for mode-0700 device locks and diagnostics."
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
                    "--health": {"type": "boolean", "default": False},
                },
                "mutually_exclusive": [["--once", "--check", "--health"]],
                "examples": ["harmony-pilot serve --config bridge.local.json --check --json",
                             "harmony-pilot serve --config bridge.local.json --json"],
            },
            "plugins": {
                "side_effect": "readonly",
                "description": "Discover trusted installed and explicitly configured plugins.",
                "options": {},
                "subcommands": {
                    "list": {"options": {}},
                    "describe": {"arguments": {"action_id": {
                        "type": "string", "required": True,
                    }}},
                },
                "examples": ["harmony-pilot plugins list --json",
                             "harmony-pilot plugins describe device.health --json"],
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
