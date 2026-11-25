"""Bounded observation of the standard HarmonyOS Messages application."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from moyu_harmony_pilot.actions.budget import bounded_call, ensure_deadline_available
from moyu_harmony_pilot.actions.models import (
    ActionBudgetExceeded,
    ActionContext,
    ActionOutcome,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.actions.sms_codes import extract_verification_codes
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.ui import UiNode, UiTree, detect_sensitive_signals

SMS_BUNDLE = "com.ohos.mms"
MAX_BODY_CHARS = 4096
MAX_RESULT_CHARS = 16384
_POLL_INTERVAL = 1.0
SMS_ERROR_HINTS = {
    "human_required": (
        "Unlock the device and complete any prompts manually. Use an idempotency key "
        "to open SMS via sms open or allowlisted run_action for sms.open with params={}, "
        "then read again."
    ),
    "sms_layout_unsupported": (
        "Display the supported system SMS conversation list or a text "
        "conversation detail page, then read again."
    ),
    "sms_view_changed": (
        "Start a new SMS read on the current page to establish a fresh "
        "message baseline before waiting again."
    ),
}
SMS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": [
        "alias", "status", "bundle", "page", "scope", "messages", "count",
        "truncated", "layout_hash", "observed_at", "reason",
    ],
    "properties": {
        "alias": {"type": "string"},
        "status": {"type": "string", "enum": ["succeeded"]},
        "bundle": {"type": "string", "enum": [SMS_BUNDLE]},
        "page": {"type": "string", "enum": ["conversations", "thread"]},
        "scope": {"type": "string", "enum": ["visible"]},
        "messages": {
            "type": "array", "maxItems": 50,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": [
                    "sender", "body", "time_text", "direction", "unread", "preview",
                    "body_truncated", "verification_codes",
                ],
                "properties": {
                    "sender": {"type": ["string", "null"], "maxLength": 256},
                    "body": {"type": "string", "maxLength": MAX_BODY_CHARS},
                    "time_text": {"type": ["string", "null"], "maxLength": 128},
                    "direction": {"type": "string", "enum": ["unknown"]},
                    "unread": {"type": "null"},
                    "preview": {"type": "boolean"},
                    "body_truncated": {"type": "boolean"},
                    "verification_codes": {
                        "type": "array", "maxItems": 1024,
                        "items": {"type": "string", "minLength": 4, "maxLength": 8},
                    },
                },
            },
        },
        "count": {"type": "integer", "minimum": 0, "maximum": 50},
        "truncated": {"type": "boolean"},
        "layout_hash": {"type": "string", "minLength": 16, "maxLength": 16},
        "observed_at": {"type": "string"},
        "reason": {"type": "string", "enum": ["snapshot", "new_message", "timeout"]},
    },
}


@dataclass(frozen=True)
class SmsMessage:
    sender: str | None
    body: str
    time_text: str | None
    preview: bool

    @property
    def fingerprint(self) -> str:
        # Relative time labels and layout indices change without a new message.
        payload = json.dumps([self.sender, self.body], ensure_ascii=False).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SmsPage:
    kind: str
    messages: tuple[SmsMessage, ...]
    sender: str | None = None


def _visible_sms_nodes(tree: UiTree, window_id: str | None = None) -> list[UiNode]:
    nodes: list[UiNode] = []

    def visit(node: UiNode, bundle: str = "", window: str = "") -> None:
        if not node.visible:
            return
        if node.window_id and node.window_id != window:
            bundle = ""
        bundle = node.bundle_name or bundle
        window = node.window_id or window
        if node.bounds.is_valid and not node.bounds.intersects_viewport(*tree.screen_size):
            return
        if (bundle == SMS_BUNDLE and node.bounds.is_valid
                and (window_id is None or window == window_id)):
            nodes.append(node)
        for child in node.children:
            visit(child, bundle, window)

    visit(tree.root)
    return nodes


def _within(root: UiNode, nodes: list[UiNode]) -> list[UiNode]:
    prefix = root.path + "/"
    return [node for node in nodes if node.path == root.path or node.path.startswith(prefix)]


def _text(node: UiNode, nodes: list[UiNode]) -> str:
    if node.text.strip():
        return node.text.strip()
    # Native text spans may carry the message while their parent has an empty label.
    parts = [child.text for child in _within(node, nodes) if child.text.strip()]
    return "".join(parts).strip()


def _unique(nodes: list[UiNode], node_id: str) -> UiNode | None:
    matches = [node for node in nodes if node.id == node_id]
    if len(matches) > 1:
        raise DeviceError("SMS fields are ambiguous. Observe again.", code="sms_layout_unsupported")
    return matches[0] if matches else None


def parse_sms_tree(tree: UiTree, *, window_id: str | None = None) -> SmsPage:
    """Read verified message roles from visible system-app nodes, preserving body text."""
    if detect_sensitive_signals(tree):
        raise HumanRequiredError("Complete the device verification or permission prompt manually.")
    nodes = _visible_sms_nodes(tree, window_id)
    identifiers = {node.id for node in nodes}
    messages: list[SmsMessage] = []
    if {"conversationListSearchBar", "HdsConversationList"} & identifiers:
        rows = [node for node in nodes if re.fullmatch(r"ConversationList[0-9]+", node.id)]
        for row in rows:
            children = _within(row, nodes)
            body = _unique(children, "message_details_content")
            name = _unique(children, "name")
            timestamp = _unique(children, "time")
            if body is None or name is None:
                raise DeviceError(
                    "The visible SMS conversation format is unsupported.",
                    code="sms_layout_unsupported",
                )
            messages.append(SmsMessage(
                _text(name, children) or None,
                _text(body, children),
                (_text(timestamp, children) or None) if timestamp else None,
                True,
            ))
        return SmsPage("conversations", tuple(messages))
    if "messageList" not in identifiers:
        raise DeviceError(
            "Open the standard SMS conversation list or a text conversation.",
            code="sms_layout_unsupported",
        )
    title = _unique(nodes, "HdsTitleBar")
    labels = [] if title is None else [
        node.text.strip() for node in _within(title, nodes)
        if node.type.rsplit(".", 1)[-1] == "Text" and node.text.strip()
    ]
    sender = labels[0] if len(labels) == 1 else None
    rows = [node for node in nodes if re.fullmatch(r"MessageListContent[0-9]+", node.id)]
    for row in rows:
        children = _within(row, nodes)
        body = _unique(children, "msg_content_text")
        if body is None:
            raise DeviceError(
                "This conversation contains an unsupported message format.",
                code="sms_layout_unsupported",
            )
        timestamps = [
            node.text.strip() for node in children
            if node.type.rsplit(".", 1)[-1] == "Text" and node.text.strip()
            and node.bounds.bottom <= body.bounds.top and node.path != body.path
        ]
        messages.append(SmsMessage(
            sender, _text(body, children), " ".join(timestamps) or None, False,
        ))
    return SmsPage("thread", tuple(messages), sender)


def validate_sms_options(
    *, limit: int, sender: str | None, wait_seconds: float, timeout: float,
) -> None:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise DeviceConfigurationError("SMS limit must be an integer between 1 and 50.")
    if sender is not None and (
        not isinstance(sender, str) or not sender.strip() or len(sender) > 128
    ):
        raise DeviceConfigurationError("SMS sender must contain 1..128 characters.")
    if (
        type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600
        or type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
        or not 0 <= wait_seconds <= 300 or wait_seconds >= timeout
    ):
        raise DeviceConfigurationError(
            "SMS timeout must be in (0, 3600] and wait_seconds in [0, 300], below timeout."
        )


def _observe(ctx: ActionContext) -> tuple[UiTree, SmsPage]:
    ctx.checkpoint()
    foreground = ctx.device.get_foreground_window(timeout=ctx.remaining_timeout)
    tree, _ = ctx.device.dump_ui_compact(timeout=ctx.remaining_timeout)
    after = ctx.device.get_foreground_window(timeout=ctx.remaining_timeout)
    if foreground != after:
        raise DeviceError(
            "The foreground window changed during SMS capture. Observe again.",
            code="sms_view_changed", retryable=True,
        )
    window_id = str(foreground.window_id) if foreground.window_id is not None else None
    if (foreground.bundle_name not in (None, SMS_BUNDLE) or window_id is None
            or not _visible_sms_nodes(tree, window_id)):
        raise HumanRequiredError(
            "The system SMS app must be in the foreground.",
            hint=(
                "Run sms open with an idempotency key, then read again. "
                "Unlock on device if needed."
            ),
        )
    page = parse_sms_tree(tree, window_id=window_id)
    return tree, page


def _result(
    device: HarmonyDevice, tree: UiTree, page: SmsPage, messages: list[SmsMessage],
    *, limit: int, reason: str,
) -> dict[str, Any]:
    selected = messages[-limit:] if page.kind == "thread" else messages[:limit]
    truncated = len(selected) < len(messages)
    if page.kind == "thread":
        selected = list(reversed(selected))
    rows = []
    available = MAX_RESULT_CHARS
    for message in selected:
        sender = message.sender[:256] if message.sender else None
        timestamp = message.time_text[:128] if message.time_text else None
        metadata_chars = len(sender or "") + len(timestamp or "")
        if available <= metadata_chars:
            truncated = True
            break
        body = message.body[:min(MAX_BODY_CHARS, available - metadata_chars)]
        shortened = len(body) < len(message.body)
        truncated = (
            truncated or shortened or sender != message.sender or timestamp != message.time_text
        )
        rows.append({
            "sender": sender, "body": body, "time_text": timestamp,
            "direction": "unknown", "unread": None, "preview": message.preview,
            "body_truncated": shortened or body.endswith(("…", "...")),
            "verification_codes": extract_verification_codes(message.body, max_chars=len(body)),
        })
        available -= metadata_chars + len(body)
    if page.kind == "thread":
        rows.reverse()
    return {
        "alias": device.alias, "status": "succeeded", "bundle": SMS_BUNDLE,
        "page": page.kind, "scope": "visible", "messages": rows, "count": len(rows),
        "truncated": truncated, "layout_hash": tree.layout_hash,
        "observed_at": datetime.now(UTC).isoformat(), "reason": reason,
    }


def read_sms(
    device: HarmonyDevice, *, limit: int = 10, sender: str | None = None,
    wait_seconds: float = 0, timeout: float = 30.0,
) -> dict[str, Any]:
    """Observe the foreground SMS page, optionally waiting for newly visible content.

    Waiting establishes an in-memory baseline. Existing message bodies and changes
    only to relative time labels never produce a new-message result. All output
    stays on the direct caller channel, with no message or code persisted to disk.
    """
    validate_sms_options(limit=limit, sender=sender, wait_seconds=wait_seconds, timeout=timeout)
    ensure_deadline_available()
    ctx = ActionContext(device, time.monotonic() + timeout, max_steps=1000)
    selected_sender = sender.strip() if sender is not None else None

    def collect() -> dict[str, Any]:
        device.assert_thermal_safe(timeout=ctx.remaining_timeout)
        tree, page = _observe(ctx)
        messages = [message for message in page.messages
                    if selected_sender is None or message.sender == selected_sender]
        if not wait_seconds:
            return _result(device, tree, page, messages, limit=limit, reason="snapshot")
        baseline = Counter(message.fingerprint for message in page.messages)
        identity = (page.kind, page.sender)
        wait_until = time.monotonic() + wait_seconds
        while time.monotonic() < wait_until:
            time.sleep(min(_POLL_INTERVAL, max(0, wait_until - time.monotonic())))
            tree, page = _observe(ctx)
            if (page.kind, page.sender) != identity:
                raise DeviceError(
                    "The SMS view changed during the wait. Start a new observation.",
                    code="sms_view_changed", retryable=True,
                )
            current: Counter[str] = Counter()
            new_messages = []
            for message in page.messages:
                fingerprint = message.fingerprint
                current[fingerprint] += 1
                if current[fingerprint] > baseline[fingerprint] and (
                    selected_sender is None or message.sender == selected_sender
                ):
                    new_messages.append(message)
            baseline |= current
            if new_messages:
                return _result(device, tree, page, new_messages, limit=limit, reason="new_message")
        return _result(device, tree, page, [], limit=limit, reason="timeout")

    with device.acquire_lock():
        try:
            return bounded_call(ctx, collect)
        except ActionBudgetExceeded as exc:
            raise DeviceError(
                "SMS observation exceeded its deadline. Read again with a sufficient timeout.",
                code="action_budget_exceeded", retryable=True,
            ) from exc


class SmsOpen(IdempotentAction):
    """Bring the standard SMS list to the foreground under the durable action ledger."""

    spec = ActionSpec(
        "sms.open", "1.0", "Wake the device and open the standard SMS conversation list.",
        side_effect="mutating", timeout_seconds=60, min_pilot_version="0.2.0.dev0",
        metrics_schema={
            "type": "object", "additionalProperties": False,
            "properties": {"ready": {"type": "boolean"}},
        },
        examples=({},),
    )

    def inspect(self, ctx: ActionContext, params: dict[str, Any]) -> Inspection:
        info = ctx.device.app_info(SMS_BUNDLE, timeout=ctx.remaining_timeout)
        if info is None or info.entry_ability is None:
            raise DeviceConfigurationError("The supported system SMS entry ability is unavailable.")
        try:
            _, page = _observe(ctx)
        except HumanRequiredError:
            return Inspection()
        if page.kind == "conversations":
            return Inspection(status="already_done", metrics={"ready": True})
        return Inspection()

    def submit(
        self, ctx: ActionContext, inspection: Inspection, params: dict[str, Any],
    ) -> None:
        ctx.device.wakeup(timeout=ctx.remaining_timeout)
        session = ctx.interact()
        snapshot = session.observe()
        session.start_app(SMS_BUNDLE, layout_hash=snapshot.layout_hash)
        _, page = _observe(ctx)
        if page.kind == "thread":
            snapshot = session.observe()
            buttons = [element for element in snapshot.elements
                       if element.bundle_name == SMS_BUNDLE and element.id == "back_button"
                       and element.enabled and element.clickable]
            if len(buttons) != 1:
                raise HumanRequiredError("Return to the SMS conversation list manually.")
            session.click_ref(buttons[0].ref, layout_hash=snapshot.layout_hash)

    def confirm(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        _, page = _observe(ctx)
        ready = page.kind == "conversations"
        return ActionOutcome(
            status="succeeded" if ready else "unknown",
            summary="SMS conversation list observed." if ready else "SMS navigation unconfirmed.",
            metrics={"ready": ready},
        )
