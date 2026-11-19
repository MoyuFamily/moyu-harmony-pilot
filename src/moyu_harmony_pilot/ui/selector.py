"""Declarative UI node selector engine for HarmonyOS NEXT UI trees."""

from __future__ import annotations

import re
from dataclasses import dataclass

from moyu_harmony_pilot.ui.models import UiNode, UiTree


@dataclass(frozen=True, slots=True)
class UiSelector:
    """Declarative query over `UiNode` attributes and parent/child structure."""

    text: str | None = None
    text_contains: str | None = None
    text_regex: str | None = None
    id: str | None = None
    type: str | None = None
    description: str | None = None
    description_contains: str | None = None
    clickable: bool | None = None
    long_clickable: bool | None = None
    scrollable: bool | None = None
    enabled: bool | None = None
    visible: bool | None = True
    checked: bool | None = None
    selected: bool | None = None
    focused: bool | None = None
    bundle_name: str | None = None
    index: int | None = None
    parent: UiSelector | None = None
    child: UiSelector | None = None

    def matches(self, node: UiNode, *, parent_node: UiNode | None = None) -> bool:
        if self.visible is not None and node.visible != self.visible:
            return False
        if self.enabled is not None and node.enabled != self.enabled:
            return False
        if self.clickable is not None and node.clickable != self.clickable:
            return False
        if self.long_clickable is not None and node.long_clickable != self.long_clickable:
            return False
        if self.scrollable is not None and node.scrollable != self.scrollable:
            return False
        if self.checked is not None and node.checked != self.checked:
            return False
        if self.selected is not None and node.selected != self.selected:
            return False
        if self.focused is not None and node.focused != self.focused:
            return False
        if self.type is not None and node.type != self.type:
            return False
        if self.id is not None and node.id != self.id and node.key != self.id:
            return False
        if self.bundle_name is not None and node.bundle_name != self.bundle_name:
            return False
        if self.text is not None and node.text != self.text:
            return False
        if self.text_contains is not None and self.text_contains not in node.text:
            return False
        if self.text_regex is not None and not re.search(self.text_regex, node.text):
            return False
        if self.description is not None and node.description != self.description:
            return False
        if (
            self.description_contains is not None
            and self.description_contains not in node.description
        ):
            return False
        if self.parent is not None:
            if parent_node is None or not self.parent.matches(parent_node):
                return False
        if self.child is not None:
            if not any(
                self.child.matches(desc, parent_node=desc_parent)
                for desc, desc_parent in _iter_descendants(node)
            ):
                return False
        return True


def _iter_descendants(node: UiNode) -> list[tuple[UiNode, UiNode]]:
    result: list[tuple[UiNode, UiNode]] = []
    stack: list[tuple[UiNode, UiNode]] = [(child, node) for child in reversed(node.children)]
    while stack:
        current, parent = stack.pop()
        result.append((current, parent))
        for child in reversed(current.children):
            stack.append((child, current))
    return result


def find_all(target: UiTree | UiNode, selector: UiSelector) -> list[UiNode]:
    """Return all matching nodes in deterministic pre-order DFS traversal.

    When `selector.visible is True` (default), subtrees rooted at an invisible
    ancestor are skipped to match HarmonyOS ArkUI visibility inheritance.
    """
    root = target.root if isinstance(target, UiTree) else target
    matches: list[UiNode] = []
    prune_invisible = selector.visible is True

    def _visit(node: UiNode, parent: UiNode | None) -> None:
        if prune_invisible and not node.visible:
            return
        if selector.matches(node, parent_node=parent):
            matches.append(node)
        for child in node.children:
            _visit(child, node)

    _visit(root, None)
    return matches


def find_first(target: UiTree | UiNode, selector: UiSelector) -> UiNode | None:
    """Return the first (or `selector.index`-th) matching node, or None."""
    matches = find_all(target, selector)
    if not matches:
        return None
    if selector.index is not None:
        if 0 <= selector.index < len(matches):
            return matches[selector.index]
        return None
    return matches[0]
