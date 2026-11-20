"""Official HarmonyOS NEXT `uitest` layout dumping, waits, and `uiInput` driver."""

from __future__ import annotations

import math
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from moyu_harmony_pilot.hdc import HdcClient, HdcCommandError
from moyu_harmony_pilot.ui.compact import build_compact_snapshot, diff_snapshots
from moyu_harmony_pilot.ui.errors import (
    UiConcurrentCallError,
    UiElementNotFoundError,
    UiError,
    UiInvalidInputError,
    UiTimeoutError,
)
from moyu_harmony_pilot.ui.models import CompactElement, CompactSnapshot, LayoutDiff, UiNode, UiTree
from moyu_harmony_pilot.ui.parser import parse_layout_json
from moyu_harmony_pilot.ui.selector import UiSelector, find_first

_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CONCURRENT_UITEST_MARKER = "does not allow calling concurrently"
_NAMED_KEYS: dict[str, str] = {
    "home": "Home",
    "back": "Back",
    "power": "Power",
}
_DIRECTION_MAP: dict[str, int] = {
    "left": 0,
    "right": 1,
    "up": 2,
    "down": 3,
}


def _check_uitest_output(stdout: str, stderr: str) -> None:
    combined = f"{stdout}\n{stderr}"
    if _CONCURRENT_UITEST_MARKER in combined.lower():
        raise UiConcurrentCallError(
            "HarmonyOS uitest rejected concurrent invocation on this device."
        )


def _validate_coord(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise UiInvalidInputError(f"{name} must be a non-negative integer, got {value!r}.")
    return value


def _validate_velocity(velocity: int) -> int:
    if (
        not isinstance(velocity, int)
        or isinstance(velocity, bool)
        or not (200 <= velocity <= 40000)
    ):
        raise UiInvalidInputError(
            f"velocity must be an integer between 200 and 40000, got {velocity!r}."
        )
    return velocity


class UiDriver:
    """Stateless UI observation and input driver built strictly on official `uitest` CLI."""

    def __init__(self, hdc: HdcClient, target: str) -> None:
        self._hdc = hdc
        self._target = target

    @property
    def target(self) -> str:
        return self._target

    def _run_uitest(self, argv: list[str], *, timeout: float) -> str:
        try:
            result = self._hdc.shell(self._target, argv, timeout=timeout)
        except HdcCommandError as exc:
            if exc.result is not None:
                _check_uitest_output(exc.result.stdout, exc.result.stderr)
            raise
        _check_uitest_output(result.stdout, result.stderr)
        return result.stdout

    def uitest_version(self, *, timeout: float = 10.0) -> str:
        """Query on-device `uitest --version`."""
        return self._run_uitest(["uitest", "--version"], timeout=timeout).strip()

    def dump_layout_raw(
        self,
        *,
        include_attributes: bool = False,
        bundle_name: str | None = None,
        window_id: int | None = None,
        merge_windows: bool | None = None,
        display_id: int | None = None,
        not_merge_filter: bool = False,
        timeout: float = 20.0,
    ) -> str:
        """Dump raw HarmonyOS layout JSON and always clean up remote & local tmp files."""
        argv: list[str] = ["uitest", "dumpLayout"]
        if include_attributes:
            argv.append("-a")
        if not_merge_filter:
            argv.append("-i")
        if bundle_name is not None:
            if not _BUNDLE_NAME_RE.match(bundle_name):
                raise UiInvalidInputError(f"Invalid bundle_name: {bundle_name!r}")
            argv.extend(["-b", bundle_name])
        if window_id is not None:
            if window_id < 0:
                raise UiInvalidInputError(f"Invalid window_id: {window_id!r}")
            argv.extend(["-w", str(window_id)])
        if merge_windows is not None:
            argv.extend(["-m", "true" if merge_windows else "false"])
        if display_id is not None:
            if display_id < 0:
                raise UiInvalidInputError(f"Invalid display_id: {display_id!r}")
            argv.extend(["-d", str(display_id)])

        if not math.isfinite(timeout) or timeout <= 0:
            raise UiInvalidInputError("Layout timeout must be positive and finite.")
        deadline = time.monotonic() + timeout
        with self._hdc.remote_tmp_file(
            self._target, "layout", ".json", cleanup_timeout=min(timeout, 1.0)
        ) as remote_path:
            dump_argv = [*argv, "-p", remote_path]
            self._run_uitest(dump_argv, timeout=timeout)

            with tempfile.TemporaryDirectory(prefix="moyu_pilot_ui_") as tmp_dir:
                local_path = Path(tmp_dir) / "layout.json"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UiTimeoutError("Layout capture deadline exhausted.")
                self._hdc.recv_file(self._target, remote_path, local_path, timeout=remaining)
                return local_path.read_text(encoding="utf-8", errors="replace")

    def dump_tree(
        self,
        *,
        include_attributes: bool = False,
        bundle_name: str | None = None,
        window_id: int | None = None,
        merge_windows: bool | None = None,
        display_id: int | None = None,
        not_merge_filter: bool = False,
        timeout: float = 20.0,
    ) -> UiTree:
        """Capture and parse the current HarmonyOS UI hierarchy into a `UiTree`."""
        raw_text = self.dump_layout_raw(
            include_attributes=include_attributes,
            bundle_name=bundle_name,
            window_id=window_id,
            merge_windows=merge_windows,
            display_id=display_id,
            not_merge_filter=not_merge_filter,
            timeout=timeout,
        )
        return parse_layout_json(raw_text)

    def dump_compact(
        self,
        *,
        max_elements: int = 120,
        include_text_nodes: bool = True,
        max_text_len: int = 120,
        include_attributes: bool = False,
        bundle_name: str | None = None,
        window_id: int | None = None,
        merge_windows: bool | None = None,
        display_id: int | None = None,
        timeout: float = 20.0,
    ) -> tuple[UiTree, CompactSnapshot]:
        """Capture the current UI tree and build a deterministic `@eN` compact snapshot."""
        tree = self.dump_tree(
            include_attributes=include_attributes,
            bundle_name=bundle_name,
            window_id=window_id,
            merge_windows=merge_windows,
            display_id=display_id,
            timeout=timeout,
        )
        snapshot = build_compact_snapshot(
            tree,
            max_elements=max_elements,
            include_text_nodes=include_text_nodes,
            max_text_len=max_text_len,
        )
        return tree, snapshot

    def wait_for(
        self,
        selector: UiSelector,
        *,
        timeout: float = 10.0,
        interval: float = 0.5,
        bundle_name: str | None = None,
        window_id: int | None = None,
        display_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> UiNode:
        """Poll `dump_tree()` until `selector` matches or `timeout` seconds elapse."""
        if not math.isfinite(timeout) or not math.isfinite(interval) or timeout < 0 or interval < 0:
            raise UiInvalidInputError("timeout and interval must be non-negative.")
        if timeout == 0:
            tree = self.dump_tree(
                bundle_name=bundle_name, window_id=window_id, display_id=display_id
            )
            found = find_first(tree, selector)
            if found is not None:
                return found
            raise UiTimeoutError("Selector was absent in the single requested observation.")
        deadline = clock() + timeout
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise UiTimeoutError("Timed out waiting for selector.")
            tree = self.dump_tree(
                timeout=remaining, bundle_name=bundle_name, window_id=window_id,
                display_id=display_id,
            )
            found = find_first(tree, selector)
            if found is not None and clock() <= deadline:
                return found
            remaining = deadline - clock()
            if remaining <= 0:
                raise UiTimeoutError(
                    f"Timed out after {timeout:.1f}s waiting for selector: {selector!r}"
                )
            sleeper(min(interval, remaining))

    def wait_gone(
        self,
        selector: UiSelector,
        *,
        timeout: float = 10.0,
        interval: float = 0.5,
        bundle_name: str | None = None,
        window_id: int | None = None,
        display_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Poll `dump_tree()` until `selector` no longer matches any node."""
        if not math.isfinite(timeout) or not math.isfinite(interval) or timeout < 0 or interval < 0:
            raise UiInvalidInputError("timeout and interval must be non-negative.")
        if timeout == 0:
            tree = self.dump_tree(
                bundle_name=bundle_name, window_id=window_id, display_id=display_id
            )
            if find_first(tree, selector) is None:
                return True
            raise UiTimeoutError("Selector remained in the single requested observation.")
        deadline = clock() + timeout
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise UiTimeoutError("Timed out waiting for selector to disappear.")
            tree = self.dump_tree(
                timeout=remaining, bundle_name=bundle_name, window_id=window_id,
                display_id=display_id,
            )
            found = find_first(tree, selector)
            if found is None and clock() <= deadline:
                return True
            remaining = deadline - clock()
            if remaining <= 0:
                raise UiTimeoutError(
                    f"Timed out after {timeout:.1f}s waiting for selector to disappear: "
                    f"{selector!r}"
                )
            sleeper(min(interval, remaining))

    def _ui_input(self, args: list[str], *, timeout: float = 15.0) -> None:
        self._run_uitest(["uitest", "uiInput", *args], timeout=timeout)

    def click(self, x: int, y: int, *, timeout: float = 15.0) -> None:
        """Perform a single tap via `uitest uiInput click <x> <y>`."""
        self._ui_input(
            ["click", str(_validate_coord(x, "x")), str(_validate_coord(y, "y"))],
            timeout=timeout,
        )

    def double_click(self, x: int, y: int, *, timeout: float = 15.0) -> None:
        """Perform a double tap via `uitest uiInput doubleClick <x> <y>`."""
        self._ui_input(
            ["doubleClick", str(_validate_coord(x, "x")), str(_validate_coord(y, "y"))],
            timeout=timeout,
        )

    def long_click(self, x: int, y: int, *, timeout: float = 15.0) -> None:
        """Perform a long press via `uitest uiInput longClick <x> <y>`."""
        self._ui_input(
            ["longClick", str(_validate_coord(x, "x")), str(_validate_coord(y, "y"))],
            timeout=timeout,
        )

    def click_node(
        self,
        node: UiNode | CompactElement,
        *,
        offset_x: int = 0,
        offset_y: int = 0,
        timeout: float = 15.0,
    ) -> tuple[int, int]:
        """Click the validated safe center (plus optional offset) of a UI node."""
        if not node.bounds.is_valid:
            raise UiInvalidInputError("Cannot click a node with empty or invalid bounds.")
        cx, cy = node.bounds.center
        tx, ty = cx + offset_x, cy + offset_y
        if not node.bounds.contains_point(tx, ty):
            raise UiInvalidInputError(
                f"Click coordinate ({tx}, {ty}) falls outside node bounds {node.bounds.to_list()}."
            )
        self.click(tx, ty, timeout=timeout)
        return (tx, ty)

    def click_ref(
        self,
        snapshot: CompactSnapshot,
        ref: str,
        *,
        timeout: float = 15.0,
    ) -> CompactElement:
        """Resolve a `@eN` reference in `snapshot` and click its safe center."""
        elem = snapshot.get_by_ref(ref)
        if elem is None:
            raise UiElementNotFoundError(
                f"Compact element reference {ref!r} was not found in snapshot "
                f"(layout_hash={snapshot.layout_hash})."
            )
        self.click_node(elem, timeout=timeout)
        return elem

    def click_ref_with_diff(
        self,
        snapshot: CompactSnapshot,
        ref: str,
        *,
        timeout: float = 15.0,
    ) -> tuple[CompactElement, CompactSnapshot, LayoutDiff]:
        """Click `@eN`, re-capture compact snapshot, and return structured state diff."""
        elem = self.click_ref(snapshot, ref, timeout=timeout)
        _, after = self.dump_compact()
        return elem, after, diff_snapshots(snapshot, after)

    def click_ratio(
        self,
        rx: float,
        ry: float,
        *,
        screen_size: tuple[int, int] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, int]:
        """Click a normalized screen ratio `(0.0..1.0, 0.0..1.0)`."""
        if not (0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0):
            raise UiInvalidInputError(
                f"Normalized coordinates must be within [0.0, 1.0], got ({rx}, {ry})."
            )
        width, height = screen_size if screen_size is not None else self.dump_tree().screen_size
        if width <= 0 or height <= 0:
            raise UiError("Unable to resolve non-zero screen dimensions for ratio click.")
        x = min(width - 1, max(0, round(width * rx)))
        y = min(height - 1, max(0, round(height * ry)))
        self.click(x, y, timeout=timeout)
        return (x, y)

    def swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        *,
        velocity: int = 600,
        timeout: float = 15.0,
    ) -> None:
        """Swipe between two pixel points via `uitest uiInput swipe`."""
        self._ui_input(
            [
                "swipe",
                str(_validate_coord(from_x, "from_x")),
                str(_validate_coord(from_y, "from_y")),
                str(_validate_coord(to_x, "to_x")),
                str(_validate_coord(to_y, "to_y")),
                str(_validate_velocity(velocity)),
            ],
            timeout=timeout,
        )

    def fling(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        *,
        velocity: int = 2000,
        step_length: int = 50,
        timeout: float = 15.0,
    ) -> None:
        """Perform a fast fling gesture via `uitest uiInput fling`."""
        if step_length <= 0:
            raise UiInvalidInputError(f"step_length must be positive, got {step_length!r}.")
        self._ui_input(
            [
                "fling",
                str(_validate_coord(from_x, "from_x")),
                str(_validate_coord(from_y, "from_y")),
                str(_validate_coord(to_x, "to_x")),
                str(_validate_coord(to_y, "to_y")),
                str(_validate_velocity(velocity)),
                str(step_length),
            ],
            timeout=timeout,
        )

    def drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        *,
        velocity: int = 600,
        timeout: float = 15.0,
    ) -> None:
        """Drag between two pixel points via `uitest uiInput drag`."""
        self._ui_input(
            [
                "drag",
                str(_validate_coord(from_x, "from_x")),
                str(_validate_coord(from_y, "from_y")),
                str(_validate_coord(to_x, "to_x")),
                str(_validate_coord(to_y, "to_y")),
                str(_validate_velocity(velocity)),
            ],
            timeout=timeout,
        )

    def dirc_fling(
        self,
        direction: str | int,
        *,
        velocity: int = 2000,
        step_length: int = 50,
        timeout: float = 15.0,
    ) -> None:
        """Perform a directional fling (`left`/`right`/`up`/`down` or `0..3`)."""
        if isinstance(direction, str):
            mapped = _DIRECTION_MAP.get(direction.strip().lower())
            if mapped is None:
                raise UiInvalidInputError(
                    f"Unsupported direction {direction!r}; expected left/right/up/down or 0..3."
                )
            dir_code = mapped
        elif isinstance(direction, int) and not isinstance(direction, bool) and 0 <= direction <= 3:
            dir_code = direction
        else:
            raise UiInvalidInputError(f"Unsupported direction: {direction!r}")

        if step_length <= 0:
            raise UiInvalidInputError(f"step_length must be positive, got {step_length!r}.")
        self._ui_input(
            [
                "dircFling",
                str(dir_code),
                str(_validate_velocity(velocity)),
                str(step_length),
            ],
            timeout=timeout,
        )

    def input_text(self, x: int, y: int, text: str, *, timeout: float = 15.0) -> None:
        """Focus `(x, y)` and enter text via `uitest uiInput inputText`."""
        if not isinstance(text, str) or len(text) > 4096:
            raise UiInvalidInputError("Input must be a string of at most 4096 characters.")
        if "\x00" in text or "\n" in text or "\r" in text:
            raise UiInvalidInputError("inputText value must not contain NUL or newline characters.")
        self._ui_input(
            [
                "inputText",
                str(_validate_coord(x, "x")),
                str(_validate_coord(y, "y")),
                text,
            ],
            timeout=timeout,
        )

    def input_text_ref(
        self, snapshot: CompactSnapshot, ref: str, text: str, *, timeout: float = 15
    ) -> None:
        element = snapshot.get_by_ref(ref)
        if (snapshot.sensitive_signals or element is None or not element.enabled
                or element.type.rsplit(".", 1)[-1] not in {"TextInput", "TextArea", "Search"}):
            raise UiInvalidInputError("Reference must select a non-sensitive editable element.")
        x, y = element.center
        width, height = snapshot.screen_size
        if not element.bounds.is_valid or not (0 <= x < width and 0 <= y < height):
            raise UiInvalidInputError("Input reference is outside the display.")
        self.input_text(x, y, text, timeout=timeout)

    def key_event(self, key: str | int, *, timeout: float = 15.0) -> None:
        """Send a hardware/navigation key event via `uitest uiInput keyEvent`."""
        if isinstance(key, int) and not isinstance(key, bool):
            if key < 0:
                raise UiInvalidInputError(f"Key code must be non-negative, got {key!r}.")
            key_arg = str(key)
        elif isinstance(key, str):
            cleaned = key.strip()
            if not cleaned:
                raise UiInvalidInputError("key must not be empty.")
            lowered = cleaned.lower()
            if lowered in _NAMED_KEYS:
                key_arg = _NAMED_KEYS[lowered]
            elif cleaned.isdigit():
                key_arg = cleaned
            else:
                raise UiInvalidInputError(
                    f"Unsupported key event {key!r}; use Home, Back, Power, or numeric keyCode."
                )
        else:
            raise UiInvalidInputError(f"Unsupported key event: {key!r}")
        self._ui_input(["keyEvent", key_arg], timeout=timeout)
