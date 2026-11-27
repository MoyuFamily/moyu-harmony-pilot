"""Local file actions with shared CLI parameter and result contracts."""

from __future__ import annotations

from typing import Any

from moyu_harmony_pilot.actions.models import ActionContext, ActionOutcome, ActionPlugin, ActionSpec

FILES_QUERY_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {
            "type": "string", "minLength": 1, "maxLength": 255,
            "description": "Gallery display name including its extension.",
        },
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 1000,
            "description": "Maximum number of returned media URIs. Defaults to 100.",
        },
    },
}

FILES_QUERY_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["alias", "name", "uris", "count", "total", "truncated"],
    "properties": {
        "alias": {"type": "string"},
        "name": {"type": "string"},
        "uris": {"type": "array", "items": {"type": "string"}},
        "count": {"type": "integer", "minimum": 0},
        "total": {"type": "integer", "minimum": 0},
        "truncated": {"type": "boolean"},
    },
}

FILES_EXPORT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source", "out"],
    "properties": {
        "source": {
            "type": "string", "minLength": 1, "maxLength": 4096,
            "description": "Absolute device file path or file://media/Photo/<id> URI.",
        },
        "out": {
            "type": "string", "minLength": 1, "maxLength": 4096,
            "description": "Destination local filename on the host.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Replace an existing host file atomically. Defaults to false.",
        },
    },
}

FILES_EXPORT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "alias", "source", "path", "size_bytes", "sha256", "transport", "cleanup_pending",
    ],
    "properties": {
        "alias": {"type": "string"},
        "source": {"type": "string"},
        "path": {"type": "string"},
        "size_bytes": {"type": "integer", "minimum": 0},
        "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "transport": {"type": "string", "enum": ["hdc", "mediatool"]},
        "cleanup_pending": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["location", "path"],
                "properties": {
                    "location": {"type": "string", "enum": ["device", "host"]},
                    "path": {"type": "string"},
                },
            },
            "description": "Managed temporary paths requiring cleanup.",
        },
    },
}


class FilesQuery(ActionPlugin):
    spec = ActionSpec(
        "files.query",
        "1.0",
        "Find gallery media URIs by exact filename using mediatool.",
        side_effect="readonly",
        local_only=True,
        private_content=True,
        timeout_seconds=30.0,
        params_schema=FILES_QUERY_PARAMS_SCHEMA,
        result_schema=FILES_QUERY_RESULT_SCHEMA,
        min_pilot_version="0.2.1.dev0",
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        result = ctx.device.query_media(
            params["name"], limit=params.get("limit", 100), timeout=ctx.remaining_timeout,
        )
        return ActionOutcome(
            summary="Gallery media query completed.",
            data={"alias": ctx.device.alias, **result.to_dict()},
        )


class FilesExport(ActionPlugin):
    spec = ActionSpec(
        "files.export",
        "1.0",
        "Export one device file to a host artifact through hdc or mediatool staging.",
        side_effect="readonly",
        local_only=True,
        private_content=True,
        timeout_seconds=300.0,
        params_schema=FILES_EXPORT_PARAMS_SCHEMA,
        result_schema=FILES_EXPORT_RESULT_SCHEMA,
        min_pilot_version="0.2.1.dev0",
    )

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        ctx.checkpoint()
        result = ctx.device.export_file(
            params["source"], params["out"],
            overwrite=params.get("overwrite", False), timeout=ctx.remaining_timeout,
        )
        return ActionOutcome(
            summary="Device file exported to the host.",
            data={"alias": ctx.device.alias, **result.to_dict()},
        )
