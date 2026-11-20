"""Explicit, bounded popup rules operating on structural snapshot protocols."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from moyu_harmony_pilot.vision.models import VisionError


class PopupElement(Protocol):
    ref: str
    text: str
    description: str
    clickable: bool
    enabled: bool
    bundle_name: str


class PopupSnapshot(Protocol):
    layout_hash: str
    sensitive_signals: Sequence[str]
    elements: Sequence[PopupElement]


@dataclass(frozen=True)
class PopupRule:
    text: str
    bundle_name: str

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.bundle_name.strip():
            raise VisionError("Popup rules require an exact label and bundle name.")


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
            matches = [
                element
                for element in snapshot.elements
                if element.clickable
                and element.enabled
                and element.bundle_name == rule.bundle_name
                and (element.text or element.description) == rule.text
            ]
            if len(matches) == 1:
                self._handled.add(snapshot.layout_hash)
                return matches[0].ref
        return None
