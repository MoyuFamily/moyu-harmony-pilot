"""Bounded, closed JSON Schema subset for zero-dependency plugin contracts."""

from __future__ import annotations

import json
import math
import re
from typing import Any, overload

from moyu_harmony_pilot.device import DeviceConfigurationError

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
MAX_JSON_BYTES = 16 * 1024
SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
)
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


@overload
def json_copy(value: dict[str, Any]) -> dict[str, Any]: ...


@overload
def json_copy(value: Any) -> Any: ...


def json_copy(value: Any) -> Any:
    """Copy a finite JSON value with a bounded encoded size."""
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
        if len(encoded) > MAX_JSON_BYTES:
            raise ValueError
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise DeviceConfigurationError("Expected finite JSON within the 16 KiB limit.") from exc


def _enum_equal(value: Any, candidate: Any) -> bool:
    """JSON numeric equality keeps booleans distinct, including inside containers."""
    if type(value) in (int, float) and type(candidate) in (int, float):
        return bool(value == candidate)
    if type(value) is not type(candidate):
        return False
    if isinstance(value, dict):
        return value.keys() == candidate.keys() and all(
            _enum_equal(item, candidate[key]) for key, item in value.items()
        )
    if isinstance(value, list):
        return len(value) == len(candidate) and all(
            _enum_equal(item, other) for item, other in zip(value, candidate, strict=True)
        )
    return bool(value == candidate)


def check_schema(schema: dict[str, Any], *, depth: int = 0) -> None:
    """Reject unsupported keywords instead of silently skipping validation."""
    if depth > 8 or not isinstance(schema, dict) or set(schema) - SCHEMA_KEYWORDS:
        raise DeviceConfigurationError("Unsupported action parameter schema.")
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if not kinds or any(not isinstance(k, str) or k not in _TYPES for k in kinds):
        raise DeviceConfigurationError("Schema requires supported explicit JSON types.")
    if "description" in schema and not isinstance(schema["description"], str):
        raise DeviceConfigurationError("Schema descriptions must be strings.")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise DeviceConfigurationError("Schema enum must be a nonempty array.")
    for lower, upper in (
        ("minimum", "maximum"),
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
    ):
        for key in (lower, upper):
            if key in schema:
                value = schema[key]
                numeric = type(value) is int or type(value) is float and math.isfinite(value)
                if not numeric or (
                    key not in {"minimum", "maximum"} and (type(value) is not int or value < 0)
                ):
                    raise DeviceConfigurationError("Invalid schema bound.")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise DeviceConfigurationError("Schema minimum exceeds maximum.")
    if "object" in kinds:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or schema.get("additionalProperties") is not False
            or not isinstance(required, list)
            or any(not isinstance(k, str) or k not in properties for k in required)
        ):
            raise DeviceConfigurationError("Object schemas must declare closed properties.")
        for name, child in properties.items():
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                raise DeviceConfigurationError("Invalid schema property name.")
            check_schema(child, depth=depth + 1)
    elif {"properties", "required", "additionalProperties"} & schema.keys():
        raise DeviceConfigurationError("Object keywords require the object type.")
    if "array" in kinds:
        items = schema.get("items")
        if not isinstance(items, dict):
            raise DeviceConfigurationError("Array schemas require an item schema.")
        check_schema(items, depth=depth + 1)
    elif {"items", "minItems", "maxItems"} & schema.keys():
        raise DeviceConfigurationError("Array keywords require the array type.")
    if {"minLength", "maxLength"} & schema.keys() and "string" not in kinds:
        raise DeviceConfigurationError("String bounds require the string type.")
    if {"minimum", "maximum"} & schema.keys() and not {"number", "integer"} & set(kinds):
        raise DeviceConfigurationError("Numeric bounds require a numeric type.")


def validate_value(
    value: Any, schema: dict[str, Any], *, depth: int = 0, _path: str = "$",
) -> None:
    """Validate values, reporting schema-declared paths without echoing user data."""
    if depth > 8:
        raise DeviceConfigurationError(f"JSON nesting exceeds the supported depth at {_path}.")
    kinds = schema["type"]
    kinds = kinds if isinstance(kinds, list) else [kinds]
    actual = {
        dict: "object",
        list: "array",
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }.get(type(value))
    if actual not in kinds and not (actual == "integer" and "number" in kinds):
        raise DeviceConfigurationError(f"Value does not match its declared JSON type at {_path}.")
    if "enum" in schema:
        if not any(_enum_equal(value, candidate) for candidate in schema["enum"]):
            raise DeviceConfigurationError(f"Value is outside the declared enum at {_path}.")
    if actual == "object":
        properties = schema.get("properties", {})
        missing = next((name for name in schema.get("required", []) if name not in value), None)
        if missing is not None:
            missing_path = f"{_path}[{json.dumps(missing)}]"
            raise DeviceConfigurationError(f"Missing required property at {missing_path}.")
        if set(value) - properties.keys():
            # Unknown keys are supplied by the caller and may themselves contain secrets.
            raise DeviceConfigurationError(f"Object has unknown properties at {_path}.")
        for name, item in value.items():
            child_path = f"{_path}[{json.dumps(name)}]"
            validate_value(item, properties[name], depth=depth + 1, _path=child_path)
    elif actual == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise DeviceConfigurationError(f"Array length exceeds schema bounds at {_path}.")
        for index, item in enumerate(value):
            validate_value(item, schema["items"], depth=depth + 1, _path=f"{_path}[{index}]")
    elif actual == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise DeviceConfigurationError(f"String length exceeds schema bounds at {_path}.")
    elif actual in {"integer", "number"}:
        if (type(value) is float and not math.isfinite(value)) or not (
            schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf)
        ):
            raise DeviceConfigurationError(f"Number exceeds schema bounds at {_path}.")
