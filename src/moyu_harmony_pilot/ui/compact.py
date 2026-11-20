"""Deterministic token-efficient Compact UI Snapshot builder and state differ."""

from __future__ import annotations

import re
from collections import Counter

from moyu_harmony_pilot.ui.models import (
    CompactElement,
    CompactSnapshot,
    LayoutDiff,
    UiNode,
    UiTree,
)

_FOLDABLE_CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "Button",
        "Row",
        "Column",
        "Flex",
        "Stack",
        "RelativeContainer",
        "ListItem",
        "GridItem",
        "TabContent",
        "MenuItem",
        "Toggle",
        "Checkbox",
        "Radio",
    }
)
_PASSWORD_RE = re.compile(r"(password|passcode|pin\s*code|密码|口令)", re.IGNORECASE)
_CAPTCHA_RE = re.compile(
    r"(captcha|verification\s*code|图形验证码|滑动验证|安全验证|人机验证|短信验证码)",
    re.IGNORECASE,
)
_PAYMENT_RE = re.compile(
    r"(confirm\s*pay|pay\s*now|确认支付|立即支付|输入支付密码|指纹支付|人脸支付)",
    re.IGNORECASE,
)
_PERMISSION_BUNDLES: frozenset[str] = frozenset(
    {
        "com.ohos.permissionmanager",
        "com.huawei.hmos.permissionmanager",
    }
)


def _truncate(value: str, max_len: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max(1, max_len - 1)]}…"


def _collect_passive_descendant_text(
    node: UiNode,
    *,
    max_len: int,
) -> tuple[str, set[str]]:
    """Aggregate labels from non-interactive visible descendants of a clickable container."""
    labels: list[str] = []
    folded_paths: set[str] = set()

    def _walk(current: UiNode) -> None:
        if not current.visible:
            return
        # Never fold across nested interactive controls; they keep their own @eN ref.
        if current.is_interactive:
            return
        label = current.display_label
        if label:
            if not labels or labels[-1] != label:
                labels.append(label)
            folded_paths.add(current.path)
        for child in current.children:
            _walk(child)

    for child in node.children:
        _walk(child)

    if not labels:
        return ("", folded_paths)
    return (_truncate(" | ".join(labels), max_len), folded_paths)


def detect_sensitive_signals(tree: UiTree) -> tuple[str, ...]:
    """Detect safety guardrail signals (password, captcha, payment, permission dialog).

    Returns only sanitized enum-like signal labels, never user text or secrets.
    """
    signals: dict[str, None] = {}

    for bundle in tree.bundle_names:
        if bundle in _PERMISSION_BUNDLES:
            signals.setdefault("system_permission_dialog", None)

    def _walk(node: UiNode) -> None:
        if not node.visible:
            return
        if node.bundle_name in _PERMISSION_BUNDLES:
            signals.setdefault("system_permission_dialog", None)
        combined = " ".join(
            part for part in (node.type, node.id, node.text, node.description, node.hint) if part
        )
        if combined:
            if "password" in node.type.lower() or _PASSWORD_RE.search(combined):
                signals.setdefault("password_field", None)
            if _CAPTCHA_RE.search(combined):
                signals.setdefault("captcha_prompt", None)
            if _PAYMENT_RE.search(combined):
                signals.setdefault("payment_confirmation", None)
        for child in node.children:
            _walk(child)

    _walk(tree.root)
    return tuple(signals.keys())


def build_compact_snapshot(
    tree: UiTree,
    *,
    max_elements: int = 120,
    include_text_nodes: bool = True,
    max_text_len: int = 120,
) -> CompactSnapshot:
    """Prune a raw HarmonyOS UI tree into a deterministic `@eN` compact snapshot.

    Guarantees:
    - Invisible subtrees and invalid/off-screen nodes are pruned.
    - Unlabeled clickable containers absorb non-interactive child text to eliminate noise.
    - Interactive nodes are prioritized ahead of passive text nodes if `max_elements` is hit.
    - `@e1`, `@e2`, ... numbering is 100% deterministic for identical layouts.
    """
    if max_elements <= 0:
        raise ValueError("max_elements must be positive.")

    screen_w, screen_h = tree.screen_size
    folded_paths: set[str] = set()
    raw_candidates: list[tuple[int, bool, UiNode, str]] = []
    order_counter = 0

    def _visit(node: UiNode) -> None:
        nonlocal order_counter
        if not node.visible:
            return

        valid_in_view = node.bounds.is_valid and node.bounds.intersects_viewport(
            screen_w, screen_h
        )

        if valid_in_view and node.is_interactive:
            effective_text = _truncate(node.text, max_text_len) if node.text.strip() else ""
            can_fold = (
                not effective_text
                and not node.description.strip()
                and not node.scrollable
                and (
                    node.clickable
                    or node.long_clickable
                    or node.checkable
                    or node.type in _FOLDABLE_CONTAINER_TYPES
                )
            )
            if can_fold:
                folded_text, paths = _collect_passive_descendant_text(node, max_len=max_text_len)
                if folded_text:
                    effective_text = folded_text
                    folded_paths.update(paths)

            raw_candidates.append((order_counter, True, node, effective_text))
            order_counter += 1
        elif (
            include_text_nodes
            and valid_in_view
            and node.path not in folded_paths
            and node.has_semantic_text
        ):
            effective_text = _truncate(node.text, max_text_len) if node.text.strip() else ""
            raw_candidates.append((order_counter, False, node, effective_text))
            order_counter += 1

        for child in node.children:
            _visit(child)

    _visit(tree.root)

    truncated = len(raw_candidates) > max_elements
    if truncated:
        interactive_items = [item for item in raw_candidates if item[1]]
        passive_items = [item for item in raw_candidates if not item[1]]
        if len(interactive_items) >= max_elements:
            selected = interactive_items[:max_elements]
        else:
            remaining = max_elements - len(interactive_items)
            selected = interactive_items + passive_items[:remaining]
        selected.sort(key=lambda item: item[0])
    else:
        selected = raw_candidates

    elements: list[CompactElement] = []
    for idx, (_order, is_interactive, node, effective_text) in enumerate(selected, start=1):
        elements.append(
            CompactElement(
                ref=f"@e{idx}",
                type=node.type,
                text=effective_text,
                id=node.id,
                description=_truncate(node.description, max_text_len)
                if node.description.strip()
                else "",
                hint=_truncate(node.hint, max_text_len) if node.hint.strip() else "",
                bounds=node.bounds,
                center=node.bounds.center,
                center_norm=node.bounds.center_normalized(screen_w, screen_h),
                interactive=is_interactive,
                clickable=node.clickable,
                long_clickable=node.long_clickable,
                scrollable=node.scrollable,
                enabled=node.enabled,
                checked=node.checked,
                selected=node.selected,
                focused=node.focused,
                path=node.path,
                bundle_name=node.bundle_name,
            )
        )

    return CompactSnapshot(
        layout_hash=tree.layout_hash,
        screen_size=tree.screen_size,
        bundle_names=tree.bundle_names,
        total_nodes=tree.node_count,
        kept_elements=len(elements),
        truncated=truncated,
        elements=tuple(elements),
        sensitive_signals=detect_sensitive_signals(tree),
    )


def _element_signature(elem: CompactElement) -> str:
    label = elem.text or elem.description or elem.hint or elem.id or elem.path
    return f"[{elem.type}] {label}"


def diff_snapshots(
    before: CompactSnapshot,
    after: CompactSnapshot,
    *,
    max_samples: int = 10,
) -> LayoutDiff:
    """Compute semantic diff between two `CompactSnapshot` captures."""
    before_counts = Counter(_element_signature(e) for e in before.elements)
    after_counts = Counter(_element_signature(e) for e in after.elements)

    added_counter = after_counts - before_counts
    removed_counter = before_counts - after_counts

    added_list = sorted(added_counter.keys())
    removed_list = sorted(removed_counter.keys())

    return LayoutDiff(
        changed=before.layout_hash != after.layout_hash,
        before_hash=before.layout_hash,
        after_hash=after.layout_hash,
        before_bundles=before.bundle_names,
        after_bundles=after.bundle_names,
        bundle_changed=before.bundle_names != after.bundle_names,
        added_count=sum(added_counter.values()),
        removed_count=sum(removed_counter.values()),
        added_samples=tuple(added_list[:max_samples]),
        removed_samples=tuple(removed_list[:max_samples]),
    )
