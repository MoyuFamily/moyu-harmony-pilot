"""Parser for official HarmonyOS NEXT `uitest dumpLayout` JSON documents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from moyu_harmony_pilot.ui.errors import UiParseError
from moyu_harmony_pilot.ui.models import Bounds, UiNode, UiTree

_BOUNDS_STR_RE = re.compile(
    r"^\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]$"
)


def parse_bounds(raw: Any) -> Bounds:
    """Parse HarmonyOS bounds strings `"[l,t][r,b]"` (or dict/list variants)."""
    if isinstance(raw, Bounds):
        return raw
    if isinstance(raw, str):
        match = _BOUNDS_STR_RE.match(raw.strip())
        if match:
            return Bounds(
                left=int(match.group(1)),
                top=int(match.group(2)),
                right=int(match.group(3)),
                bottom=int(match.group(4)),
            )
        return Bounds(0, 0, 0, 0)
    if isinstance(raw, Mapping):
        try:
            return Bounds(
                left=int(raw.get("left", 0)),
                top=int(raw.get("top", 0)),
                right=int(raw.get("right", 0)),
                bottom=int(raw.get("bottom", 0)),
            )
        except (TypeError, ValueError):
            return Bounds(0, 0, 0, 0)
    if isinstance(raw, list | tuple) and len(raw) == 4:
        try:
            return Bounds(
                left=int(raw[0]),
                top=int(raw[1]),
                right=int(raw[2]),
                bottom=int(raw[3]),
            )
        except (TypeError, ValueError):
            return Bounds(0, 0, 0, 0)
    return Bounds(0, 0, 0, 0)


def parse_bool_attr(attrs: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    """Parse HarmonyOS `"true"` / `"false"` string or native boolean attributes."""
    if key not in attrs:
        return default
    value = attrs[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return default


def _str_attr(attrs: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            text = str(value)
            if text:
                return text
    return ""


def _parse_node(raw: Mapping[str, Any], *, path: str) -> UiNode:
    attrs_obj = raw.get("attributes")
    attrs: Mapping[str, Any] = attrs_obj if isinstance(attrs_obj, Mapping) else raw

    raw_children = raw.get("children", ())
    children_list: list[UiNode] = []
    if isinstance(raw_children, list):
        for idx, child_raw in enumerate(raw_children):
            if isinstance(child_raw, Mapping):
                children_list.append(_parse_node(child_raw, path=f"{path}/{idx}"))

    key_val = _str_attr(attrs, "key")
    id_val = _str_attr(attrs, "id") or key_val
    node_type = _str_attr(attrs, "type") or "Unknown"
    bounds = parse_bounds(attrs.get("bounds") or attrs.get("origBounds"))

    return UiNode(
        type=node_type,
        text=_str_attr(attrs, "text"),
        id=id_val,
        key=key_val,
        description=_str_attr(attrs, "description", "accessibilityText", "content"),
        hint=_str_attr(attrs, "hint"),
        bounds=bounds,
        clickable=parse_bool_attr(attrs, "clickable", default=False),
        long_clickable=parse_bool_attr(attrs, "longClickable", default=False),
        scrollable=parse_bool_attr(attrs, "scrollable", default=False),
        checkable=parse_bool_attr(attrs, "checkable", default=False),
        checked=parse_bool_attr(attrs, "checked", default=False),
        enabled=parse_bool_attr(attrs, "enabled", default=True),
        visible=parse_bool_attr(attrs, "visible", default=True),
        selected=parse_bool_attr(attrs, "selected", default=False),
        focused=parse_bool_attr(attrs, "focused", default=False),
        bundle_name=_str_attr(attrs, "bundleName"),
        page_path=_str_attr(attrs, "pagePath"),
        window_id=_str_attr(attrs, "hostWindowId", "windowId"),
        path=path,
        children=tuple(children_list),
    )


def compute_layout_hash(root: UiNode) -> str:
    """Compute a deterministic semantic structure digest (16-char SHA-256 hex).

    Only visible nodes contribute to the digest so hidden off-tree buffers or
    ephemeral internal hashcodes do not cause false diffs.
    """
    hasher = hashlib.sha256()

    def _visit(node: UiNode) -> None:
        if not node.visible:
            return
        if node.has_semantic_text or node.is_interactive or node.id or node.bundle_name:
            signature = (
                f"{node.path}|{node.type}|{node.id}|{node.bundle_name}|"
                f"{node.text}|{node.description}|{node.hint}|"
                f"{node.bounds.left},{node.bounds.top},{node.bounds.right},{node.bounds.bottom}|"
                f"{int(node.clickable)}{int(node.scrollable)}{int(node.enabled)}"
                f"{int(node.checked)}{int(node.selected)}{int(node.focused)}\n"
            )
            hasher.update(signature.encode("utf-8"))
        for child in node.children:
            _visit(child)

    _visit(root)
    return hasher.hexdigest()[:16]


def parse_layout_dict(raw: Mapping[str, Any]) -> UiTree:
    """Parse a decoded HarmonyOS `uitest dumpLayout` JSON dictionary into a `UiTree`."""
    if not isinstance(raw, Mapping):
        raise UiParseError("Layout root must be a JSON object.")
    if "attributes" not in raw and "children" not in raw and "type" not in raw:
        raise UiParseError(
            "Layout JSON does not look like a HarmonyOS dumpLayout object "
            "(expected 'attributes' or 'children')."
        )

    root = _parse_node(raw, path="0")
    node_count = 0
    seen_bundles: dict[str, None] = {}
    max_right = root.bounds.right if root.bounds.is_valid else 0
    max_bottom = root.bounds.bottom if root.bounds.is_valid else 0

    for node in root.iter_nodes():
        node_count += 1
        if node.bundle_name:
            seen_bundles.setdefault(node.bundle_name, None)
        if node.visible and node.bounds.is_valid:
            if max_right == 0 or (not root.bounds.is_valid and node.bounds.right > max_right):
                max_right = max(max_right, node.bounds.right)
            if max_bottom == 0 or (not root.bounds.is_valid and node.bounds.bottom > max_bottom):
                max_bottom = max(max_bottom, node.bounds.bottom)

    screen_width = root.bounds.right if root.bounds.is_valid else max_right
    screen_height = root.bounds.bottom if root.bounds.is_valid else max_bottom

    return UiTree(
        root=root,
        screen_width=max(0, screen_width),
        screen_height=max(0, screen_height),
        node_count=node_count,
        bundle_names=tuple(seen_bundles.keys()),
        layout_hash=compute_layout_hash(root),
    )


def parse_layout_json(raw_text: str) -> UiTree:
    """Parse raw UTF-8 JSON text returned by `uitest dumpLayout`."""
    if not raw_text or not raw_text.strip():
        raise UiParseError("Layout JSON text is empty.")
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise UiParseError(f"Failed to decode dumpLayout JSON: {exc.msg}") from exc
    if not isinstance(decoded, Mapping):
        raise UiParseError(
            f"Expected JSON object at layout root, got {type(decoded).__name__}."
        )
    return parse_layout_dict(decoded)
