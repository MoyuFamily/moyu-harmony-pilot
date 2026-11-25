"""Synthetic, offline SMS content roles and sensitive-screen regression tests."""

from __future__ import annotations

from typing import Any

import pytest

from moyu_harmony_pilot.actions.sms import parse_sms_tree
from moyu_harmony_pilot.device import HumanRequiredError
from moyu_harmony_pilot.ui import (
    build_compact_snapshot,
    detect_sensitive_signals,
    parse_layout_dict,
)


def _node(*children: dict[str, Any], **attributes: Any) -> dict[str, Any]:
    return {
        "attributes": {
            "type": "Text", "bounds": [20, 240, 380, 320], "visible": True, **attributes,
        },
        "children": list(children),
    }


def _sms_page(
    page: str, message: str = "短信验证码123456", *, field: str = "text"
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Use public control shapes with synthetic labels and window IDs."""
    body = _node(
        id="message_details_content" if page == "list" else "msg_content_text",
        clickable=page == "detail", **{field: message},
    )
    if page == "list":
        container = _node(
            _node(_node(text="合成发件人"), type="Row", id="name"),
            _node(text="今天", id="time"),
            body,
            type="RelativeContainer", id="ConversationList0", clickable=True,
            **{field: message},
        )
        markers = [
            _node(type="Row", id="conversationListSearchBar"),
            _node(type="Button", id="HdsConversationList", clickable=True),
        ]
    else:
        container = _node(
            _node(text="今天"),
            _node(body, type="Column", id="MessageList0", clickable=True),
            type="Column", id="MessageListContent0", **{field: message},
        )
        markers = []
    root = _node(
        *markers,
        _node(
            container, type="List", id="messageList" if page == "detail" else "",
            bounds=[0, 200, 400, 760], scrollable=True,
        ),
        type="WindowScene", bounds=[0, 0, 400, 800],
        bundleName="com.ohos.mms", hostWindowId="1",
    )
    return root, container, body


def _signals(raw: dict[str, Any]) -> tuple[str, ...]:
    return detect_sensitive_signals(parse_layout_dict(raw))


@pytest.mark.parametrize(
    "page, field, message",
    [("list", "text", "短信验证码123456"), ("detail", "hint", "Enter your PIN")],
    ids=["list-code-text", "detail-pin-hint"],
)
def test_message_and_aggregate_content_are_not_interaction_prompts(
    page: str, field: str, message: str,
) -> None:
    raw, _, _ = _sms_page(page, message, field=field)
    assert _signals(raw) == ()
    snapshot = build_compact_snapshot(parse_layout_dict(raw))
    assert snapshot.sensitive_signals == ()
    assert any(getattr(element, field) == message for element in snapshot.elements)


@pytest.mark.parametrize(
    "page, attributes, signal, location",
    [
        ("list", {"type": "TextInput", "hint": "verification code"},
         "captcha_prompt", "container"),
        ("detail", {"type": "PasswordInput"}, "password_field", "body"),
    ],
    ids=["code-input-in-list-row", "password-input-in-thread-body"],
)
def test_real_controls_and_unrecognized_labels_are_checked_at_every_depth(
    page: str, attributes: dict[str, Any], signal: str, location: str,
) -> None:
    raw, container, body = _sms_page(page)
    parent = {"container": container, "body": body}[location]
    parent["children"].append(_node(**attributes))
    assert signal in _signals(raw)
    with pytest.raises(HumanRequiredError, match="manually"):
        parse_sms_tree(parse_layout_dict(raw))


@pytest.mark.parametrize(
    "page, label, signal",
    [("detail", "Password", "password_field")],
    ids=["unlabeled-input-with-password-label"],
)
def test_message_shaped_forms_keep_labels_for_unlabeled_inputs(
    page: str, label: str, signal: str,
) -> None:
    raw, container, body = _sms_page(page, label)
    body["attributes"]["clickable"] = False
    container["children"].append(_node(type="TextInput"))
    assert signal in _signals(raw)


@pytest.mark.parametrize(
    "page, bundle", [("list", "com.ohos.mms.extra")],
    ids=["sms-bundle-prefix-spoof"],
)
def test_message_roles_require_the_exact_effective_sms_bundle(page: str, bundle: str) -> None:
    raw, _, _ = _sms_page(page)
    raw["attributes"]["bundleName"] = bundle
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["detail"], ids=["marker-inside-message"])
def test_a_message_cannot_supply_its_own_page_marker(page: str) -> None:
    raw, _, body = _sms_page(page)
    raw["children"] = raw["children"][-1:]
    raw["children"][0]["attributes"]["id"] = ""
    body["children"].append(_node(
        type="Span", id="messageList",
        text="短信验证码123456",
    ))
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize(
    "page, attributes", [("list", {"hostWindowId": "2"})],
    ids=["message-span-in-foreign-window"],
)
def test_incomplete_span_subtrees_cannot_exempt_aggregate_text(
    page: str, attributes: dict[str, Any],
) -> None:
    raw, _, body = _sms_page(page)
    body["attributes"]["text"] = ""
    body["children"] = [
        _node(type="Span", text="合成消息"),
        _node(type="Span", text="短信验证码123456", **attributes),
    ]
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize(
    "page, bundle", [("detail", "")],
    ids=["unknown-window-with-sms-shaped-content"],
)
def test_page_proof_is_isolated_between_nested_windows(
    page: str, bundle: str,
) -> None:
    raw, _, _ = _sms_page(page, "普通合成短信")
    overlay, _, _ = _sms_page(page)
    overlay["attributes"].update(bundleName=bundle, hostWindowId="2")
    raw["children"].append(overlay)
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["detail"], ids=["pruned-thread"])
def test_sms_exemption_and_actual_prompt_detection_survive_compact_pruning(page: str) -> None:
    raw, _, _ = _sms_page(page)
    snapshot = build_compact_snapshot(parse_layout_dict(raw), max_elements=1, max_text_len=3)
    assert snapshot.truncated
    assert snapshot.sensitive_signals == ()
    raw["children"].append(_node(type="TextInput", hint="verification code"))
    snapshot = build_compact_snapshot(parse_layout_dict(raw), max_elements=1, max_text_len=3)
    assert snapshot.sensitive_signals == ("captcha_prompt",)
