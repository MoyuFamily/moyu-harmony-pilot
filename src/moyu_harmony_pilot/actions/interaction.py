"""Local interactive sessions sharing the action/device safety boundaries."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from moyu_harmony_pilot.actions.budget import bounded_call, ensure_deadline_available
from moyu_harmony_pilot.actions.models import ActionBudgetExceeded, ActionContext
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.device.apps import validate_bundle
from moyu_harmony_pilot.ui import (
    CompactSnapshot,
    UiNode,
    UiSelector,
    UiTree,
    diff_snapshots,
    find_first,
)
from moyu_harmony_pilot.ui.errors import UiElementNotFoundError
from moyu_harmony_pilot.vision import PopupWatcher

T = TypeVar("T")


class InteractionSession:
    """Guarded UI interaction for local-lab exploration or bound plugin execution."""

    def __init__(
        self,
        device: HarmonyDevice,
        *,
        max_steps: int = 20,
        timeout: float = 60,
        _context: ActionContext | None = None,
    ) -> None:
        if _context is not None:
            self.device = _context.device
            self.context = _context
            self._context_bound = True
        else:
            self.validate_budget(max_steps, timeout)
            self.device = device
            self.context = ActionContext(device, time.monotonic() + timeout, max_steps)
            self._context_bound = False
        self._needs_observation = False

    @classmethod
    def for_context(cls, ctx: ActionContext) -> InteractionSession:
        """Bind guarded UI interactions to an active ActionContext without double locking."""
        return cls(ctx.device, _context=ctx)

    @staticmethod
    def validate_budget(max_steps: int, timeout: float) -> None:
        if (
            type(max_steps) is not int
            or not 1 <= max_steps <= 1000
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 3600
        ):
            raise DeviceConfigurationError("Invalid local interaction budget.")

    def _require_mutation_stage(self, operation: str = "Mutating UI interactions") -> None:
        if self._context_bound and self.context.stage not in {"preparing", "submit"}:
            raise DeviceConfigurationError(
                f"{operation} are only allowed during the preparing or submit stage."
            )

    def _locked(self, callback: Callable[[], T], *, mutating: bool = False) -> T:
        if mutating:
            self._require_mutation_stage()
        if self._context_bound:
            if threading.current_thread() is not threading.main_thread():
                raise DeviceConfigurationError(
                    "Context interactions require the runner main thread."
                )
            if self.context.stage not in {"readonly", "inspect", "preparing", "submit", "confirm"}:
                raise DeviceConfigurationError("Action context is no longer executing.")
            self.context.checkpoint()
            result = callback()
            _ = self.context.remaining_timeout
            return result
        ensure_deadline_available()
        with self.device.acquire_lock():
            try:
                return bounded_call(self.context, callback)
            except ActionBudgetExceeded as exc:
                raise DeviceError(
                    "Local interaction budget exhausted.", code="action_budget_exceeded"
                ) from exc

    def _capture(self) -> tuple[UiTree, CompactSnapshot]:
        return self.device.dump_ui_compact(timeout=self.context.remaining_timeout)

    def observe(self) -> CompactSnapshot:
        snapshot = self._locked(self._capture)[1]
        self._needs_observation = False
        return snapshot

    def dismiss_popup(
        self, watcher: PopupWatcher, *, snapshot: CompactSnapshot | None = None
    ) -> dict[str, Any] | None:
        """Dismiss at most one unambiguously matched non-sensitive popup."""
        self._require_mutation_stage("Popup dismissal")
        current = self.observe() if snapshot is None else snapshot
        ref = watcher.choose(current)
        if ref is None:
            return None
        return self.click_ref(ref, layout_hash=current.layout_hash)

    def click_ref(self, ref: str, *, layout_hash: str) -> dict[str, Any]:
        return self._step("click", layout_hash=layout_hash, ref=ref)

    def swipe(self, direction: str, *, layout_hash: str) -> dict[str, Any]:
        if direction not in {"up", "down", "left", "right"}:
            raise DeviceConfigurationError("Choose an up, down, left, or right swipe.")
        return self._step("swipe", layout_hash=layout_hash, direction=direction)

    def back(self, *, layout_hash: str) -> dict[str, Any]:
        return self._step("back", layout_hash=layout_hash)

    def home(self, *, layout_hash: str) -> dict[str, Any]:
        return self._step("home", layout_hash=layout_hash)

    def long_click_ref(self, ref: str, *, layout_hash: str) -> dict[str, Any]:
        return self._step("long_click", layout_hash=layout_hash, ref=ref)

    def input_text_ref(self, ref: str, text: str, *, layout_hash: str) -> dict[str, Any]:
        if (not isinstance(text, str) or len(text) > 4096
                or any(char in text for char in ("\x00", "\n", "\r"))):
            raise DeviceConfigurationError(
                "Input must be a single line of at most 4096 characters."
            )
        return self._step("input_text", layout_hash=layout_hash, ref=ref, text=text)

    def start_app(
        self, bundle_name: str, *, ability_name: str | None = None, layout_hash: str
    ) -> dict[str, Any]:
        validate_bundle(bundle_name)
        if ability_name is not None:
            validate_bundle(ability_name)
        return self._step(
            "start_app", layout_hash=layout_hash, bundle_name=bundle_name, ability_name=ability_name
        )

    def _step(
        self, action: str, *, layout_hash: str, ref: str = "", direction: str = "",
        text: str = "", bundle_name: str = "", ability_name: str | None = None,
    ) -> dict[str, Any]:
        self._require_mutation_stage()
        if self._needs_observation:
            raise DeviceConfigurationError("Observe the UI after an uncertain interaction.")
        submitted = False

        def perform() -> dict[str, Any]:
            nonlocal submitted
            _, before = self._capture()
            if before.layout_hash != layout_hash:
                raise DeviceConfigurationError("UI changed. Observe again before using references.")
            if before.sensitive_signals:
                raise HumanRequiredError("Sensitive screen requires human intervention.")
            self.device.assert_thermal_safe(timeout=self.context.remaining_timeout)
            if action in {"click", "long_click", "input_text"}:
                element = before.get_by_ref(ref)
                if element is None or not element.enabled or not element.bounds.is_valid:
                    raise DeviceConfigurationError(
                        "Reference must select an enabled element."
                    )
                if action == "click" and not element.clickable:
                    raise DeviceConfigurationError("Reference must be clickable.")
                if action == "long_click" and not element.long_clickable:
                    raise DeviceConfigurationError("Reference must support long press.")
                if (
                    action == "input_text"
                    and element.type.rsplit(".", 1)[-1] not in {"TextInput", "TextArea", "Search"}
                ):
                    raise DeviceConfigurationError("Reference must select an editable element.")
                x, y = element.center
                width, height = before.screen_size
                if not 0 <= x < width or not 0 <= y < height:
                    raise DeviceConfigurationError("Reference center is outside the display.")
                submitted = True
                if action == "input_text":
                    self.device.ui.input_text_ref(
                        before, ref, text, timeout=self.context.remaining_timeout
                    )
                elif action == "long_click":
                    self.device.ui.long_click(x, y, timeout=self.context.remaining_timeout)
                else:
                    self.device.ui.click_ref(before, ref, timeout=self.context.remaining_timeout)
            elif action == "swipe":
                width, height = before.screen_size
                points = {
                    "up": (0.5, 0.8, 0.5, 0.2),
                    "down": (0.5, 0.2, 0.5, 0.8),
                    "left": (0.8, 0.5, 0.2, 0.5),
                    "right": (0.2, 0.5, 0.8, 0.5),
                }[direction]
                if width <= 0 or height <= 0:
                    raise DeviceConfigurationError("Display dimensions are unavailable.")
                coordinates = [
                    round(value * (width if index % 2 == 0 else height))
                    for index, value in enumerate(points)
                ]
                submitted = True
                self.device.ui.swipe(*coordinates, timeout=self.context.remaining_timeout)
            elif action == "start_app":
                ability = ability_name
                module = None
                if ability is None:
                    info = self.device.app_info(bundle_name, timeout=self.context.remaining_timeout)
                    if info is None or info.entry_ability is None:
                        raise DeviceConfigurationError("Specify an unambiguous entry ability.")
                    ability, module = info.entry_ability, info.entry_module
                submitted = True
                self.device.start_app(
                    bundle_name, ability, module_name=module, timeout=self.context.remaining_timeout
                )
            else:
                submitted = True
                self.device.ui.key_event(
                    "Home" if action == "home" else "Back", timeout=self.context.remaining_timeout
                )
            _, after = self._capture()
            if after.sensitive_signals:
                raise HumanRequiredError(
                    "Interaction reached a sensitive screen. Take over manually."
                )
            foreground = self.device.get_foreground_window(timeout=self.context.remaining_timeout)
            difference = diff_snapshots(before, after).to_dict()
            difference["before_focus"] = [
                element.ref for element in before.elements if element.focused
            ]
            difference["after_focus"] = [
                element.ref for element in after.elements if element.focused
            ]
            return {
                "snapshot": after.to_dict(),
                "changed": difference["changed"],
                "difference": difference,
                "foreground_bundle": foreground.bundle_name,
                "remaining_steps": self.context.max_steps - self.context.steps,
            }

        try:
            return self._locked(perform, mutating=True)
        except ActionBudgetExceeded:
            self._needs_observation = submitted
            raise
        except HumanRequiredError:
            self._needs_observation = submitted
            raise
        except Exception as exc:
            if submitted:
                self._needs_observation = True
                raise DeviceError(
                    "Interaction result is uncertain. Observe before continuing.", code="unknown"
                ) from exc
            raise

    def wait_for(
        self, selector: UiSelector, *, watcher: PopupWatcher | None = None, interval: float = 0.25
    ) -> UiNode:
        if not math.isfinite(interval) or interval < 0:
            raise DeviceConfigurationError("Polling interval must be finite and non-negative.")
        if watcher is not None:
            self._require_mutation_stage("Popup dismissal")
        while True:
            snapshot, node = self._locked(lambda: self._find(selector))
            if node is not None:
                return node
            ref = watcher.choose(snapshot) if watcher is not None else None
            if ref is not None:
                self.click_ref(ref, layout_hash=snapshot.layout_hash)
            try:
                remaining = self.context.remaining_timeout
                delay = min(interval, remaining)
                if self._context_bound:
                    sleep_until = time.monotonic() + delay
                    while True:
                        _ = self.context.remaining_timeout
                        left = sleep_until - time.monotonic()
                        if left <= 0:
                            break
                        time.sleep(min(0.05, left))
                else:
                    time.sleep(delay)
            except ActionBudgetExceeded as exc:
                if self._context_bound:
                    raise
                raise DeviceError(
                    "Local interaction deadline exhausted.", code="action_budget_exceeded"
                ) from exc

    def scroll_until(
        self, selector: UiSelector, *, direction: str = "up", max_swipes: int = 10
    ) -> UiNode:
        self._require_mutation_stage("Scrolling")
        if type(max_swipes) is not int or not 0 <= max_swipes <= 100:
            raise DeviceConfigurationError("max_swipes must be between 0 and 100.")
        if direction not in {"up", "down", "left", "right"}:
            raise DeviceConfigurationError("Choose an up, down, left, or right swipe.")
        for attempt in range(max_swipes + 1):
            snapshot, node = self._locked(lambda: self._find(selector))
            if node is not None:
                return node
            if attempt < max_swipes:
                self.swipe(direction, layout_hash=snapshot.layout_hash)
        raise UiElementNotFoundError("Element was not found within the scrolling budget.")

    def _find(self, selector: UiSelector) -> tuple[CompactSnapshot, UiNode | None]:
        tree, snapshot = self._capture()
        if snapshot.sensitive_signals:
            raise HumanRequiredError("Sensitive screen requires human intervention.")
        return snapshot, find_first(tree, selector)
