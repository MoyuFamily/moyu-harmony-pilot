"""Strong immutable data models for HarmonyOS NEXT UI trees and compact snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

INTERACTIVE_NODE_TYPES: frozenset[str] = frozenset(
    {
        "Button",
        "TextInput",
        "TextArea",
        "Search",
        "Toggle",
        "Checkbox",
        "CheckboxGroup",
        "Radio",
        "Slider",
        "Select",
        "MenuItem",
        "TabBar",
        "Rating",
        "Counter",
    }
)


@dataclass(frozen=True, slots=True)
class Bounds:
    """Pixel rectangle `[left, top, right, bottom]` of a UI node."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def is_valid(self) -> bool:
        return self.right > self.left and self.bottom > self.top

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def contains_point(self, x: int, y: int) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom

    def intersects_viewport(self, screen_width: int, screen_height: int) -> bool:
        if screen_width <= 0 or screen_height <= 0:
            return True
        return (
            self.right > 0
            and self.bottom > 0
            and self.left < screen_width
            and self.top < screen_height
        )

    def normalized(
        self, screen_width: int, screen_height: int
    ) -> tuple[float, float, float, float]:
        if screen_width <= 0 or screen_height <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            round(max(0.0, min(1.0, self.left / screen_width)), 4),
            round(max(0.0, min(1.0, self.top / screen_height)), 4),
            round(max(0.0, min(1.0, self.right / screen_width)), 4),
            round(max(0.0, min(1.0, self.bottom / screen_height)), 4),
        )

    def center_normalized(self, screen_width: int, screen_height: int) -> tuple[float, float]:
        if screen_width <= 0 or screen_height <= 0:
            return (0.0, 0.0)
        cx, cy = self.center
        return (
            round(max(0.0, min(1.0, cx / screen_width)), 4),
            round(max(0.0, min(1.0, cy / screen_height)), 4),
        )

    def to_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]


@dataclass(frozen=True, slots=True)
class UiNode:
    """Immutable node parsed from HarmonyOS NEXT `uitest dumpLayout` JSON."""

    type: str
    text: str
    id: str
    key: str
    description: str
    hint: str
    bounds: Bounds
    clickable: bool
    long_clickable: bool
    scrollable: bool
    checkable: bool
    checked: bool
    enabled: bool
    visible: bool
    selected: bool
    focused: bool
    bundle_name: str
    page_path: str
    window_id: str
    path: str
    children: tuple[UiNode, ...] = ()

    @property
    def is_interactive(self) -> bool:
        return (
            self.visible
            and self.enabled
            and self.bounds.is_valid
            and (
                self.clickable
                or self.long_clickable
                or self.scrollable
                or self.checkable
                or self.type in INTERACTIVE_NODE_TYPES
            )
        )

    @property
    def has_semantic_text(self) -> bool:
        return bool(self.text.strip() or self.description.strip() or self.hint.strip())

    @property
    def display_label(self) -> str:
        for candidate in (self.text, self.description, self.hint):
            stripped = candidate.strip()
            if stripped:
                return stripped
        return ""

    def iter_nodes(self) -> Iterator[UiNode]:
        """Deterministic pre-order DFS traversal over this subtree."""
        yield self
        for child in self.children:
            yield from child.iter_nodes()

    def iter_with_parent(
        self, parent: UiNode | None = None
    ) -> Iterator[tuple[UiNode, UiNode | None]]:
        """Pre-order DFS yielding `(node, parent)` pairs."""
        yield (self, parent)
        for child in self.children:
            yield from child.iter_with_parent(self)


@dataclass(frozen=True, slots=True)
class UiTree:
    """Parsed HarmonyOS NEXT UI hierarchy with deterministic `layout_hash`."""

    root: UiNode
    screen_width: int
    screen_height: int
    node_count: int
    bundle_names: tuple[str, ...]
    layout_hash: str

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self.screen_width, self.screen_height)

    def iter_nodes(self) -> Iterator[UiNode]:
        return self.root.iter_nodes()


@dataclass(frozen=True, slots=True)
class CompactElement:
    """Token-efficient semantic UI element referenced by deterministic `@eN` ID."""

    ref: str
    type: str
    text: str
    id: str
    description: str
    hint: str
    bounds: Bounds
    center: tuple[int, int]
    center_norm: tuple[float, float]
    interactive: bool
    clickable: bool
    long_clickable: bool
    scrollable: bool
    enabled: bool
    checked: bool
    selected: bool
    focused: bool
    path: str
    bundle_name: str = ""

    def to_dict(self, *, include_bundle: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ref": self.ref,
            "type": self.type,
            "bounds": self.bounds.to_list(),
            "center_norm": [
                round(self.center_norm[0], 3),
                round(self.center_norm[1], 3),
            ],
        }
        if self.text:
            payload["text"] = self.text
        if self.id:
            payload["id"] = self.id
        if self.description:
            payload["desc"] = self.description
        if self.hint:
            payload["hint"] = self.hint
        if self.clickable:
            payload["clickable"] = True
        if self.long_clickable:
            payload["long_clickable"] = True
        if self.scrollable:
            payload["scrollable"] = True
        if self.interactive and not (self.clickable or self.long_clickable or self.scrollable):
            payload["interactive"] = True
        if not self.enabled:
            payload["enabled"] = False
        if self.checked:
            payload["checked"] = True
        if self.selected:
            payload["selected"] = True
        if self.focused:
            payload["focused"] = True
        if include_bundle and self.bundle_name:
            payload["bundle"] = self.bundle_name
        return payload

    def to_line(self) -> str:
        flags: list[str] = []
        if self.clickable:
            flags.append("click")
        if self.scrollable:
            flags.append("scroll")
        if self.checked:
            flags.append("checked")
        if self.selected:
            flags.append("selected")
        if self.focused:
            flags.append("focused")
        if not self.enabled:
            flags.append("disabled")

        parts = [self.ref, f"[{self.type}]"]
        if self.text:
            parts.append(f'"{self.text}"')
        elif self.description:
            parts.append(f'desc="{self.description}"')
        elif self.hint:
            parts.append(f'hint="{self.hint}"')
        if self.id:
            parts.append(f"id={self.id}")
        if flags:
            parts.append(f"({','.join(flags)})")
        parts.append(f"@({self.center_norm[0]:.2f},{self.center_norm[1]:.2f})")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class CompactSnapshot:
    """Pruned, deterministic, low-token UI snapshot for scripts and AI Agents."""

    layout_hash: str
    screen_size: tuple[int, int]
    bundle_names: tuple[str, ...]
    total_nodes: int
    kept_elements: int
    truncated: bool
    elements: tuple[CompactElement, ...]
    sensitive_signals: tuple[str, ...] = ()

    def get_by_ref(self, ref: str) -> CompactElement | None:
        normalized = ref.strip()
        if not normalized.startswith("@"):
            normalized = f"@{normalized}"
        for elem in self.elements:
            if elem.ref == normalized:
                return elem
        return None

    def to_dict(self) -> dict[str, Any]:
        include_bundle = len(self.bundle_names) > 1
        return {
            "layout_hash": self.layout_hash,
            "screen_size": [self.screen_size[0], self.screen_size[1]],
            "bundles": list(self.bundle_names),
            "total_nodes": self.total_nodes,
            "kept_elements": self.kept_elements,
            "truncated": self.truncated,
            "sensitive_signals": list(self.sensitive_signals),
            "elements": [
                elem.to_dict(include_bundle=include_bundle) for elem in self.elements
            ],
        }

    def to_text(self) -> str:
        header = (
            f"layout_hash={self.layout_hash} "
            f"screen={self.screen_size[0]}x{self.screen_size[1]} "
            f"nodes={self.kept_elements}/{self.total_nodes}"
        )
        if self.bundle_names:
            header += f" bundles={','.join(self.bundle_names)}"
        if self.sensitive_signals:
            header += f" sensitive={','.join(self.sensitive_signals)}"
        lines = [header, *(elem.to_line() for elem in self.elements)]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LayoutDiff:
    """Before/after UI state comparison for feedback loops and anti-loop guardrails."""

    changed: bool
    before_hash: str
    after_hash: str
    before_bundles: tuple[str, ...]
    after_bundles: tuple[str, ...]
    bundle_changed: bool
    added_count: int
    removed_count: int
    added_samples: tuple[str, ...]
    removed_samples: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "before_bundles": list(self.before_bundles),
            "after_bundles": list(self.after_bundles),
            "bundle_changed": self.bundle_changed,
            "added_count": self.added_count,
            "removed_count": self.removed_count,
            "added_samples": list(self.added_samples),
            "removed_samples": list(self.removed_samples),
        }
