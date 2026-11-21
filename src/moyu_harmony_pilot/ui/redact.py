"""Deterministic offline redaction for candidate `dumpLayout` test fixtures."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from moyu_harmony_pilot.ui.errors import UiInvalidInputError, UiParseError
from moyu_harmony_pilot.ui.models import INTERACTIVE_NODE_TYPES, Bounds
from moyu_harmony_pilot.ui.parser import parse_bool_attr, parse_bounds

_BOOLEAN_KEYS: frozenset[str] = frozenset(
    {
        "clickable",
        "longClickable",
        "scrollable",
        "checkable",
        "checked",
        "enabled",
        "focused",
        "selected",
        "visible",
    }
)
_TEXT_KEYS = frozenset({"text", "description", "hint", "accessibilityText", "content"})
# Only public component names are structural. Custom type names may contain private data.
_COMPONENT_TYPES = (
    frozenset(
        "root Root Unknown AbilityComponent AlphabetIndexer Badge Blank Button Calendar "
        "Canvas Checkbox CheckboxGroup Column ColumnSplit Counter DataPanel DatePicker Divider "
        "Flex FormComponent Gauge Grid GridCol GridContainer GridItem GridRow Hyperlink Image "
        "ImageAnimator ImageSpan Input Line List ListItem ListItemGroup LoadingProgress Menu "
        "MenuItem MenuItemGroup NavDestination Navigation Navigator Panel PasswordInput Path "
        "PatternLock PinInput PluginComponent Polygon Polyline Progress QRCode Radio Rating "
        "Rect Refresh RelativeContainer RichEditor RichText Row RowSplit Scroll ScrollBar "
        "Search Select Shape Slider Span Stack Stepper StepperItem Swiper SymbolGlyph "
        "TabContent Tabs Text TextArea TextClock TextInput TextPicker TextTimer TimePicker "
        "Toggle UIExtensionComponent Video WaterFlow Web XComponent".split()
    )
    | INTERACTIVE_NODE_TYPES
)
_IDENTIFIER_KINDS = {
    "id": ("id", "node_id_"),
    "key": ("id", "node_id_"),
    "pagePath": ("page", "pages/Page"),
    "abilityName": ("ability", "Ability"),
    "hostWindowId": ("window", ""),
    "windowId": ("window", ""),
    "displayId": ("display", ""),
}
_BUNDLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_PII_RE = re.compile(
    r"(?:"
    r"@|\b[A-Za-z][A-Za-z0-9+.-]*://|\bwww\."
    r"|\b\d{4,}\b|(?:\d[ ()+.-]*){6,}"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b"
    r"|\b(?:token|secret|password|api[ _-]?key)\s*[:=]\s*\S+"
    r"|\bbearer\s+\S+|\b[A-Za-z0-9_-]{32,}\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RedactionSummary:
    """Count attribute occurrences, including node, window, page, and ability identifiers."""

    node_count: int
    redacted_texts: int
    redacted_bundles: int
    redacted_ids: int
    dropped_attributes: int
    manual_review_required: bool = field(default=True, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_count": self.node_count,
            "redacted_texts": self.redacted_texts,
            "redacted_bundles": self.redacted_bundles,
            "redacted_ids": self.redacted_ids,
            "dropped_attributes": self.dropped_attributes,
            "manual_review_required": self.manual_review_required,
        }


def _validate_keep_list(values: Iterable[str], *, bundles: bool = False) -> set[str]:
    label = "keep_bundles" if bundles else "keep_texts"
    if isinstance(values, str | bytes):
        raise UiInvalidInputError(f"{label} must be an iterable of exact strings.")
    try:
        items = tuple(values)
    except TypeError as exc:
        raise UiInvalidInputError(f"{label} must be an iterable of exact strings.") from exc
    for value in items:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(unicodedata.category(char).startswith("C") for char in value)
            or _PII_RE.search(value)
            or (bundles and (len(value) > 255 or not _BUNDLE_RE.fullmatch(value)))
        ):
            raise UiInvalidInputError(f"{label} contains an unsafe or invalid literal.")
    return set(items)


def _structural_bounds(value: Any) -> str:
    try:
        bounds = parse_bounds(value)
        # Coordinates are spatial integers, never arbitrary attribute payloads.
        if any(abs(coordinate) > 2**31 - 1 for coordinate in bounds.to_list()):
            bounds = Bounds(0, 0, 0, 0)
    except (TypeError, ValueError, OverflowError):
        bounds = Bounds(0, 0, 0, 0)
    return f"[{bounds.left},{bounds.top}][{bounds.right},{bounds.bottom}]"


def redact_layout_dict(
    raw: dict[str, Any],
    *,
    keep_bundles: Iterable[str] = (),
    keep_texts: Iterable[str] = (),
) -> tuple[dict[str, Any], RedactionSummary]:
    """Redact user text, app identifiers, and extraneous attributes from a raw layout tree.

    The output is a candidate offline fixture that still requires manual human review
    before committing to any repository.
    """
    if not isinstance(raw, dict) or not {"attributes", "children", "type"}.intersection(raw):
        raise UiParseError("Layout root must contain attributes, children, or a node type.")

    allowed_bundles = _validate_keep_list(keep_bundles, bundles=True)
    allowed_texts = _validate_keep_list(keep_texts)
    pseudonyms: dict[str, dict[str, str]] = {}
    reserved = allowed_bundles | allowed_texts
    stats = {
        "nodes": 0,
        "texts": 0,
        "bundles": 0,
        "ids": 0,
        "dropped": 0,
    }

    def _pseudonym(kind: str, value: str, prefix: str) -> str:
        if not value:
            return ""
        mapping = pseudonyms.setdefault(kind, {})
        if value not in mapping:
            index = len(mapping) + 1
            candidate = f"{prefix}{index}"
            while candidate in reserved or candidate == value:
                index += 1
                candidate = f"{prefix}{index}"
            mapping[value] = candidate
            reserved.add(candidate)
        return mapping[value]

    def _walk(node: Any) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise UiParseError("Layout node must be a JSON object.")
        stats["nodes"] += 1
        # The parser also accepts flat nodes, including mixed flat/wrapped descendants.
        attrs = node.get("attributes", node)
        if not isinstance(attrs, dict) or not all(isinstance(key, str) for key in attrs):
            raise UiParseError("Layout attributes must be a JSON object with string keys.")
        if attrs is not node:
            stats["dropped"] += len(node.keys() - {"attributes", "children"})
        cleaned: dict[str, Any] = {}

        for key in sorted(attrs):
            val = attrs[key]
            if attrs is node and key == "children":
                continue
            if key in {"bounds", "origBounds"}:
                cleaned[key] = _structural_bounds(val) if val else ""
            elif key in _BOOLEAN_KEYS:
                boolean = parse_bool_attr(attrs, key, default=key in {"visible", "enabled"})
                cleaned[key] = str(boolean).lower() if isinstance(val, str) else boolean
            elif key in _TEXT_KEYS or key in _IDENTIFIER_KINDS or key in {"type", "bundleName"}:
                if val is None:
                    value = ""
                elif isinstance(val, str | int | float) and not (
                    isinstance(val, float) and not math.isfinite(val)
                ):
                    value = str(val)
                else:
                    stats["dropped"] += 1
                    continue
                if key == "type":
                    cleaned[key] = (
                        value
                        if value in _COMPONENT_TYPES
                        else _pseudonym("type", value, "CustomNode")
                    )
                elif key == "bundleName":
                    cleaned[key] = (
                        value
                        if value in allowed_bundles
                        else _pseudonym("bundle", value, "com.example.app")
                    )
                    stats["bundles"] += bool(value and value not in allowed_bundles)
                elif key in _TEXT_KEYS:
                    cleaned[key] = (
                        value if value in allowed_texts else _pseudonym("text", value, "Text ")
                    )
                    stats["texts"] += bool(value and value not in allowed_texts)
                else:
                    kind, prefix = _IDENTIFIER_KINDS[key]
                    replacement = _pseudonym(kind, value, prefix)
                    cleaned[key] = (
                        int(replacement)
                        if kind in {"window", "display"} and type(val) is int and replacement
                        else replacement
                    )
                    stats["ids"] += bool(value)
            else:
                stats["dropped"] += 1

        raw_children = node.get("children", [])
        if not isinstance(raw_children, list):
            raise UiParseError("Layout children must be a JSON array.")
        children = [_walk(child) for child in raw_children]
        return {"attributes": cleaned, "children": children}

    try:
        redacted = _walk(raw)
    except RecursionError as exc:
        raise UiParseError("Layout nesting is too deep or cyclic.") from exc
    summary = RedactionSummary(
        node_count=stats["nodes"],
        redacted_texts=stats["texts"],
        redacted_bundles=stats["bundles"],
        redacted_ids=stats["ids"],
        dropped_attributes=stats["dropped"],
    )
    return redacted, summary


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UiParseError("Layout JSON contains duplicate object keys.")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise UiParseError("Layout JSON must contain finite JSON numbers.")


def redact_layout_json(
    raw_json: str,
    *,
    keep_bundles: Iterable[str] = (),
    keep_texts: Iterable[str] = (),
) -> tuple[str, RedactionSummary]:
    """Parse and deterministically redact a raw `uitest dumpLayout` JSON string."""
    if not isinstance(raw_json, str) or not raw_json.strip():
        raise UiParseError("Empty layout JSON input.")
    try:
        parsed = json.loads(
            raw_json, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as exc:
        raise UiParseError("Invalid layout JSON.") from exc
    if not isinstance(parsed, dict):
        raise UiParseError("Layout JSON root must be a JSON object.")
    redacted, summary = redact_layout_dict(parsed, keep_bundles=keep_bundles, keep_texts=keep_texts)
    return json.dumps(redacted, ensure_ascii=False, indent=2, allow_nan=False), summary
