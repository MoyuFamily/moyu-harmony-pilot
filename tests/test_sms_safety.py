"""Synthetic, offline SMS content roles and sensitive-screen regression tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

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


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("field", ["text", "description", "hint"])
@pytest.mark.parametrize(
    "message",
    [
        "短信验证码123456",
        "Your verification code is 123456.",
        "VERIFICATION CODE 012345",
        "合成消息提醒确认支付",
        "Pay now",
        "输入支付密码",
        "Enter your PIN",
        "请验证身份",
        "Place your finger on the sensor",
        "Allow Example to access your camera?",
    ],
)
def test_message_and_aggregate_content_are_not_interaction_prompts(
    page: str, field: str, message: str,
) -> None:
    raw, _, _ = _sms_page(page, message, field=field)
    assert _signals(raw) == ()
    snapshot = build_compact_snapshot(parse_layout_dict(raw))
    assert snapshot.sensitive_signals == ()
    assert any(getattr(element, field) == message for element in snapshot.elements)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("field", ["text", "description", "hint"])
def test_nested_spans_can_supply_the_body_content(page: str, field: str) -> None:
    raw, _, body = _sms_page(page, field=field)
    body["attributes"][field] = ""
    body["children"] = [
        _node(
            _node(type="Span", clickable=True, **{field: "123456"}),
            type="Span", **{field: "短信验证码"},
        ),
        _node(type="Span", **{field: "verification code"}),
    ]
    assert _signals(raw) == ()


@pytest.mark.parametrize("page", ["list", "detail"])
def test_indexed_and_qualified_message_roles(page: str) -> None:
    raw, container, body = _sms_page(page)
    container["attributes"]["id"] = container["attributes"]["id"].removesuffix("0") + "12"
    container["attributes"]["type"] = "ohos." + container["attributes"]["type"]
    body["attributes"]["type"] = "ohos.Text"
    assert _signals(raw) == ()


@pytest.mark.parametrize("marker_id", ["conversationListSearchBar", "HdsConversationList"])
def test_either_verified_conversation_page_marker_is_sufficient(marker_id: str) -> None:
    raw, _, _ = _sms_page("list")
    raw["children"] = [
        node for node in raw["children"] if node["attributes"]["id"] in {marker_id, ""}
    ]
    assert _signals(raw) == ()


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "attributes, signal",
    [
        ({"type": "TextInput", "hint": "verification code"}, "captcha_prompt"),
        ({"type": "TextInput", "hint": "短信验证码"}, "captcha_prompt"),
        ({"type": "TextArea", "description": "verification code"}, "captcha_prompt"),
        ({"type": "Input", "text": "verification code"}, "captcha_prompt"),
        ({"type": "PasswordInput"}, "password_field"),
        ({"type": "PinInput"}, "password_field"),
        ({"type": "TextInput", "hint": "Password"}, "password_field"),
        ({"type": "TextInput", "id": "pinEntry"}, "password_field"),
        ({"type": "Button", "text": "Pay now", "clickable": True}, "payment_confirmation"),
        ({"text": "Allow Example to access your camera?"}, "system_permission_dialog"),
        ({"text": "请验证身份"}, "biometric_prompt"),
    ],
)
@pytest.mark.parametrize("location", ["window", "container", "body"])
def test_real_controls_and_unrecognized_labels_are_checked_at_every_depth(
    page: str, attributes: dict[str, Any], signal: str, location: str,
) -> None:
    raw, container, body = _sms_page(page)
    parent = {"window": raw, "container": container, "body": body}[location]
    parent["children"].append(_node(**attributes))
    assert signal in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("control_type", ["TextInput", "PasswordInput", "PinInput", "Button"])
def test_body_id_on_an_actual_control_does_not_prove_message_content(
    page: str, control_type: str,
) -> None:
    raw, _, body = _sms_page(page)
    body["attributes"].update(type=control_type, hint="verification code")
    assert "captcha_prompt" in _signals(raw)
    if control_type in {"PasswordInput", "PinInput"}:
        assert "password_field" in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("location", ["container", "body"])
@pytest.mark.parametrize(
    "label, signal", [("verification code", "captcha_prompt"), ("Password", "password_field")]
)
def test_message_shaped_forms_keep_labels_for_unlabeled_inputs(
    page: str, location: str, label: str, signal: str,
) -> None:
    raw, container, body = _sms_page(page, label)
    body["attributes"]["clickable"] = False
    parent = container if location == "container" else body
    parent["children"].append(_node(type="TextInput"))
    assert signal in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
def test_permission_buttons_inside_a_message_shaped_container_keep_their_context(page: str) -> None:
    raw, container, _ = _sms_page(page, "相机权限")
    container["children"].append(_node(type="Button", text="Allow", clickable=True))
    assert _signals(raw) == ("system_permission_dialog",)


@pytest.mark.parametrize("page", ["list", "detail"])
def test_pin_keypad_inside_a_message_shaped_container_keeps_its_label(page: str) -> None:
    raw, container, _ = _sms_page(page, "PIN")
    container["children"].extend(
        _node(type="Button", text=str(digit), clickable=True) for digit in range(10)
    )
    assert _signals(raw) == ("password_field",)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "attributes, signal",
    [
        ({"type": "captcha.Text"}, "captcha_prompt"),
        ({"type": "confirmPay.Text"}, "payment_confirmation"),
    ],
)
def test_body_control_type_is_scanned_even_when_content_is_exempt(
    page: str, attributes: dict[str, Any], signal: str,
) -> None:
    raw, _, body = _sms_page(page)
    body["attributes"].update(attributes)
    assert signal in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "node_id, signal",
    [
        ("captcha", "captcha_prompt"),
        ("confirmPay", "payment_confirmation"),
        ("passwordInput", "password_field"),
        ("pinEntry", "password_field"),
    ],
)
def test_span_identifiers_keep_structural_safety_checks(
    page: str, node_id: str, signal: str,
) -> None:
    raw, _, body = _sms_page(page)
    body["children"].append(_node(type="Span", id=node_id, text="verification code"))
    assert signal in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "bundle", ["", "com.example.sms", "com.ohos.mms.extra", "com.ohos.MMS"]
)
def test_message_roles_require_the_exact_effective_sms_bundle(page: str, bundle: str) -> None:
    raw, _, _ = _sms_page(page)
    raw["attributes"]["bundleName"] = bundle
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("damage", ["missing", "hidden", "offscreen", "invalid", "foreign"])
def test_page_markers_must_be_present_visible_and_in_the_sms_scope(
    page: str, damage: str,
) -> None:
    raw, _, _ = _sms_page(page)
    markers = raw["children"][:-1] if page == "list" else raw["children"]
    for marker in markers:
        attributes = marker["attributes"]
        if damage == "missing":
            attributes["id"] = "unknown_page"
        elif damage == "hidden":
            attributes["visible"] = False
        elif damage == "offscreen":
            attributes["bounds"] = [0, 900, 400, 1100]
        elif damage == "invalid":
            attributes["bounds"] = [0, 0, 0, 0]
        else:
            attributes["bundleName"] = "com.example.overlay"
    # A hidden page hides its messages too. Keep the row visible elsewhere so
    # the hidden/foreign/off-screen marker cannot prove the row is SMS content.
    if page == "detail":
        row = markers[0]["children"].pop()
        raw["children"].append(row)
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["list", "detail"])
def test_a_message_cannot_supply_its_own_page_marker(page: str) -> None:
    raw, _, body = _sms_page(page)
    raw["children"] = raw["children"][-1:]
    raw["children"][0]["attributes"]["id"] = ""
    body["children"].append(_node(
        type="Span", id="messageList" if page == "detail" else "HdsConversationList",
        text="短信验证码123456",
    ))
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing_body", "empty_body", "duplicate_body", "wrong_body_id", "wrong_container_type",
        "wrong_container_id", "missing_container", "body_outside_container",
    ],
)
def test_incomplete_or_forged_message_roles_keep_keyword_detection(page: str, damage: str) -> None:
    raw, container, body = _sms_page(page)
    if damage == "missing_body":
        container["children"] = []
    elif damage == "empty_body":
        body["attributes"]["text"] = ""
    elif damage == "duplicate_body":
        container["children"].append(deepcopy(body))
    elif damage == "wrong_body_id":
        body["attributes"]["id"] += "_prompt"
    elif damage == "wrong_container_type":
        container["attributes"]["type"] = "TextInput"
    elif damage == "wrong_container_id":
        container["attributes"]["id"] += "_prompt"
    elif damage == "missing_container":
        raw["children"][-1]["children"] = [body]
    else:
        container["children"] = []
        raw["children"].append(body)
    assert "captcha_prompt" in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("part", ["body", "container", "body_ancestor"])
@pytest.mark.parametrize("damage", ["hidden", "offscreen", "invalid"])
def test_invisible_or_unbounded_message_evidence_is_not_used(
    page: str, part: str, damage: str,
) -> None:
    raw, container, body = _sms_page(page)
    target = body if part == "body" else container
    if part == "body_ancestor":
        wrapper = _node(body, type="Column")
        container["children"] = [wrapper]
        target = wrapper
    if damage == "hidden":
        target["attributes"]["visible"] = False
    elif damage == "offscreen":
        target["attributes"]["bounds"] = [0, 900, 400, 1000]
    else:
        target["attributes"]["bounds"] = [0, 0, 0, 0]
    if part == "container" and damage == "hidden":
        assert _signals(raw) == ()
    elif part == "body_ancestor" and damage == "invalid":
        # Layout-only wrappers may lack geometry. Both proven roles have valid bounds.
        assert _signals(raw) == ()
    else:
        assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "attributes",
    [
        {"visible": False},
        {"bounds": [0, 900, 400, 1000]},
        {"bounds": [0, 0, 0, 0]},
        {"bundleName": "com.example.overlay"},
        {"hostWindowId": "2"},
    ],
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


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize(
    "attributes, signal",
    [
        ({"bundleName": "com.example.overlay"}, "captcha_prompt"),
        ({"hostWindowId": "2"}, "captcha_prompt"),
        ({"bundleName": "com.ohos.permissionmanager"}, "system_permission_dialog"),
        ({"bundleName": "com.ohos.useriam.authwidget"}, "biometric_prompt"),
    ],
)
@pytest.mark.parametrize("part", ["body", "span"])
def test_nested_overlays_and_new_windows_are_independently_detected(
    page: str, attributes: dict[str, Any], signal: str, part: str,
) -> None:
    raw, _, body = _sms_page(page)
    target = body
    if part == "span":
        target = _node(type="Span", text="短信验证码123456")
        body["children"].append(target)
    target["attributes"].update(attributes)
    assert signal in _signals(raw)


@pytest.mark.parametrize("page", ["list", "detail"])
@pytest.mark.parametrize("bundle", ["", "com.example.overlay", "com.ohos.mms"])
@pytest.mark.parametrize("keep_markers", [False, True])
def test_page_proof_is_isolated_between_nested_windows(
    page: str, bundle: str, keep_markers: bool,
) -> None:
    raw, _, _ = _sms_page(page, "普通合成短信")
    overlay, container, _ = _sms_page(page)
    overlay["attributes"].update(bundleName=bundle, hostWindowId="2")
    if not keep_markers:
        overlay["children"] = [container]
    raw["children"].append(overlay)
    expected = () if bundle == "com.ohos.mms" and keep_markers else ("captcha_prompt",)
    assert _signals(raw) == expected


@pytest.mark.parametrize("page", ["list", "detail"])
def test_unknown_window_bundle_stays_unknown_through_unlabeled_descendants(page: str) -> None:
    raw, _, _ = _sms_page(page, "普通合成短信")
    overlay, _, _ = _sms_page(page)
    overlay["attributes"].pop("bundleName")
    overlay["attributes"].pop("hostWindowId")
    raw["children"].append(_node(overlay, type="WindowScene", hostWindowId="2"))
    assert _signals(raw) == ("captcha_prompt",)


@pytest.mark.parametrize("page", ["list", "detail"])
def test_same_explicit_window_keeps_bundle_inheritance(page: str) -> None:
    raw, container, body = _sms_page(page)
    container["attributes"]["hostWindowId"] = "1"
    body["attributes"]["hostWindowId"] = "1"
    assert _signals(raw) == ()


@pytest.mark.parametrize("page", ["list", "detail"])
def test_message_labels_do_not_supply_password_or_permission_form_context(page: str) -> None:
    raw, _, body = _sms_page(page, "Password")
    body["attributes"]["clickable"] = False
    raw["children"].append(_node(type="TextInput"))
    assert _signals(raw) == ()
    raw["children"].append(_node(text="Password"))
    assert _signals(raw) == ("password_field",)

    raw, _, _ = _sms_page(page, "相机权限")
    raw["children"].append(_node(type="Button", text="Allow", clickable=True))
    assert _signals(raw) == ()
    raw["children"].append(_node(text="相机权限"))
    assert _signals(raw) == ("system_permission_dialog",)


def test_message_digits_are_not_a_pin_keypad() -> None:
    raw, _, _ = _sms_page("detail", "0")
    rows = raw["children"][-1]["children"]
    for digit in range(1, 10):
        _, container, _ = _sms_page("detail", str(digit))
        container["attributes"]["id"] = f"MessageListContent{digit}"
        rows.append(container)
    raw["children"].append(_node(text="PIN"))
    assert _signals(raw) == ()
    raw["children"].extend(
        _node(type="Button", text=str(digit), clickable=True) for digit in range(10)
    )
    assert _signals(raw) == ("password_field",)


@pytest.mark.parametrize("page", ["list", "detail"])
def test_sms_exemption_and_actual_prompt_detection_survive_compact_pruning(page: str) -> None:
    raw, _, _ = _sms_page(page)
    snapshot = build_compact_snapshot(parse_layout_dict(raw), max_elements=1, max_text_len=3)
    assert snapshot.truncated
    assert snapshot.sensitive_signals == ()
    raw["children"].append(_node(type="TextInput", hint="verification code"))
    snapshot = build_compact_snapshot(parse_layout_dict(raw), max_elements=1, max_text_len=3)
    assert snapshot.sensitive_signals == ("captcha_prompt",)
