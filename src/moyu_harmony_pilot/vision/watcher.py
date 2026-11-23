"""Explicit, bounded popup rules operating on structural snapshot protocols."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from moyu_harmony_pilot.vision.models import VisionError


class PopupElement(Protocol):
    @property
    def ref(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def clickable(self) -> bool: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def bundle_name(self) -> str: ...


class PopupSnapshot(Protocol):
    @property
    def layout_hash(self) -> str: ...

    @property
    def sensitive_signals(self) -> Sequence[str]: ...

    @property
    def elements(self) -> Sequence[PopupElement]: ...


@dataclass(frozen=True)
class PopupRule:
    text: str | None = None
    bundle_name: str = ""
    text_regex: str | None = field(default=None, kw_only=True)
    node_id: str | None = field(default=None, kw_only=True)
    _compiled_regex: re.Pattern[str] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_name, str) or not self.bundle_name.strip():
            raise VisionError("Popup rules require an explicit bundle name.")
        if self.text is not None and (not isinstance(self.text, str) or not self.text.strip()):
            raise VisionError("Popup rule text must be a non-empty string.")
        if self.text_regex is not None and (
            not isinstance(self.text_regex, str)
            or not self.text_regex.strip()
            or len(self.text_regex) > 256
        ):
            raise VisionError("Popup rule text_regex must be a bounded non-empty pattern.")
        if self.node_id is not None and (
            not isinstance(self.node_id, str)
            or not self.node_id.strip()
            or len(self.node_id) > 128
        ):
            raise VisionError("Popup rule node_id must be a bounded non-empty string.")
        if self.text is not None and self.text_regex is not None:
            raise VisionError("Popup rule cannot set both text and text_regex.")
        if self.text is None and self.text_regex is None and self.node_id is None:
            raise VisionError("Popup rules require text, text_regex, or node_id.")
        if self.text_regex is not None:
            try:
                compiled = re.compile(self.text_regex)
            except (re.error, OverflowError, RecursionError) as exc:
                raise VisionError("Invalid popup rule regex.") from exc
            object.__setattr__(self, "_compiled_regex", compiled)

    def matches(self, element: PopupElement) -> bool:
        if not (element.clickable and element.enabled and element.bundle_name == self.bundle_name):
            return False
        # Older structural elements may omit IDs. They remain usable by exact-text rules.
        if self.node_id is not None and getattr(element, "id", "") != self.node_id:
            return False
        label = element.text or element.description
        if self.text is not None and label != self.text:
            return False
        if self._compiled_regex is not None and not self._compiled_regex.search(label):
            return False
        return True


class PopupWatcher:
    def __init__(self, rules: Sequence[PopupRule] = (), *, max_actions: int = 3) -> None:
        if type(max_actions) is not int or not 1 <= max_actions <= 20:
            raise VisionError("Popup max_actions must be between 1 and 20.")
        self.rules = tuple(rules)
        self.max_actions = max_actions
        self._handled: set[str] = set()

    def choose(self, snapshot: PopupSnapshot) -> str | None:
        """Charge a matching dismissal once per layout. The caller performs a guarded click."""
        if (
            snapshot.sensitive_signals
            or snapshot.layout_hash in self._handled
            or len(self._handled) >= self.max_actions
        ):
            return None
        for rule in self.rules:
            matches = [element for element in snapshot.elements if rule.matches(element)]
            if len(matches) == 1:
                self._handled.add(snapshot.layout_hash)
                return matches[0].ref
        return None
