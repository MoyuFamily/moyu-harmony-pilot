"""Synthetic, offline safety signals for HarmonyOS layouts and OCR text observations."""

from __future__ import annotations

from typing import Any

import pytest

from moyu_harmony_pilot.ui import (
    SENSITIVE_SIGNAL_ENUM,
    build_compact_snapshot,
    detect_sensitive_signals,
    parse_layout_dict,
)


def _node(**attributes: Any) -> dict[str, Any]:
    return {
        "attributes": {"type": "Text", "bounds": "[10,10][300,80]", **attributes},
        "children": [],
    }


def _signals(*children: dict[str, Any]) -> tuple[str, ...]:
    return detect_sensitive_signals(
        parse_layout_dict(
            {
                "attributes": {"type": "root", "bounds": "[0,0][400,800]"},
                "children": list(children),
            }
        )
    )


@pytest.mark.parametrize(
    "attributes",
    [
        {"type": "MenuItem", "text": "Password settings", "clickable": True},
        {"type": "TextInput", "hint": "Search passwords", "id": "passwordManagerSearch"},
    ],
    ids=['settings-menu', 'password-search'],
)
def test_password_management_and_menu_labels_are_not_prompts(attributes: dict[str, Any]) -> None:
    assert _signals(_node(**attributes)) == ()


@pytest.mark.parametrize(
    "attributes",
    [
        {"type": "TextInput", "hint": "Password"},
        {"type": "Column", "id": "pin_keypad"},
        {"text": "请输入锁屏密码"},
    ],
    ids=['password-input', 'pin-keypad', 'explicit-lockscreen-prompt'],
)
def test_password_controls_keypads_and_explicit_prompts_are_blocked(
    attributes: dict[str, Any],
) -> None:
    assert "password_field" in _signals(_node(**attributes))


@pytest.mark.parametrize(
    "label",
    [
        "支付密码",
    ],
    ids=['payment-password-context'],
)
def test_passive_label_supplies_context_for_an_unlabeled_input(label: str) -> None:
    assert _signals(_node(text=label), _node(type="TextInput")) == ("password_field",)
    assert _signals(_node(text=label), _node(type="Search")) == ()
    assert _signals(_node(text=label), _node(type="TextInput", hint="Search entries")) == ()


@pytest.mark.parametrize(
    "text",
    [
        "人脸认证",
    ],
    ids=['face-authentication'],
)
def test_biometric_prompts_are_blocked(text: str) -> None:
    assert "biometric_prompt" in _signals(_node(text=text))


@pytest.mark.parametrize(
    "context, grant",
    [
        ("相机权限", "允许"),
        ("读取屏幕内容和控制操作的无障碍权限", "启用"),
    ],
    ids=['camera-grant', 'accessibility-grant'],
)
def test_permission_grants_require_context_and_block_high_risk_access(
    context: str,
    grant: str,
) -> None:
    assert _signals(_node(text=context), _node(type="Button", text=grant, clickable=True)) == (
        "system_permission_dialog",
    )
    assert _signals(_node(type="Button", text=grant, clickable=True)) == ()
    assert _signals(_node(text=context)) == ()


@pytest.mark.parametrize(
    "text",
    [
        "Allow Example to access your camera?",
        "Screen sharing\nStart now",
    ],
    ids=['explicit-permission-request', 'multiline-screen-sharing'],
)
def test_permission_request_and_multiline_ocr_text_are_blocked(text: str) -> None:
    assert _signals(_node(text=text)) == ("system_permission_dialog",)


@pytest.mark.parametrize(
    "bundle, signal",
    [
        ("com.huawei.hmos.permissionmanager", "system_permission_dialog"),
        ("com.ohos.useriam.authwidget", "biometric_prompt"),
    ],
    ids=['huawei-permission', 'system-biometrics'],
)
def test_harmony_system_bundles_are_gated_only_in_visible_subtrees(
    bundle: str, signal: str
) -> None:
    assert _signals(_node(bundleName=bundle)) == (signal,)
    hidden = _node(visible=False)
    hidden["children"] = [_node(bundleName=bundle, text="Enter your password")]
    assert _signals(hidden) == ()


def test_signal_order_and_full_tree_scan_survive_compact_pruning() -> None:
    tree = parse_layout_dict(
        {
            "attributes": {"type": "root", "bounds": "[0,0][400,800]"},
            "children": [
                _node(text="Face verification"),
                _node(text="Allow camera access?"),
                _node(text="Pay now"),
                _node(text="请完成滑块验证"),
                _node(type="TextInput", hint="Password synthetic-private-value"),
            ],
        }
    )
    snapshot = build_compact_snapshot(tree, max_elements=1, max_text_len=5)
    assert snapshot.sensitive_signals == SENSITIVE_SIGNAL_ENUM
    assert snapshot.to_dict()["sensitive_signals"] == list(SENSITIVE_SIGNAL_ENUM)
    assert snapshot.truncated
