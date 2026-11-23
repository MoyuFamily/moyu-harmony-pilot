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
        {"text": "Password Manager"},
        {"text": "密码保险箱"},
        {"text": "管理登录密码"},
        {"text": "密码与安全"},
        {"text": "设置密码"},
        {"text": "锁屏密码"},
        {"text": "PIN 码"},
        {"text": "Enter password manager"},
        {"type": "PasswordManagerPanel", "id": "password_manager"},
        {"type": "MenuItem", "text": "Password settings", "clickable": True},
        {"type": "MenuItem", "text": "指纹解锁", "clickable": True},
        {"type": "MenuItem", "text": "Face ID", "clickable": True},
        {"type": "MenuItem", "id": "password_input_settings", "text": "Password options"},
        {"type": "Search", "hint": "Search passwords", "id": "password_search"},
        {"type": "TextInput", "hint": "Search passwords", "id": "passwordManagerSearch"},
        {"type": "TextInput", "hint": "搜索密码"},
        {"type": "TextInput", "id": "spinning_input", "hint": "Quantity"},
        {"type": "TextInput", "id": "shipping_input", "hint": "Address"},
        {"id": "password_input_settings", "text": "Password settings", "visible": False},
    ],
)
def test_password_management_and_menu_labels_are_not_prompts(attributes: dict[str, Any]) -> None:
    assert _signals(_node(**attributes)) == ()


@pytest.mark.parametrize(
    "attributes",
    [
        {"type": "TextInput", "hint": "Password"},
        {"type": "TextInput", "hint": "PIN"},
        {"type": "TextInput", "hint": "PIN码"},
        {"type": "TextInput", "hint": "Password", "text": "search"},
        {"type": "TextInput", "hint": "请输入支付密码"},
        {"type": "TextInput", "id": "pin"},
        {"type": "TextInput", "id": "password"},
        {"type": "TextInput", "id": "pinEntry"},
        {"type": "TextInput", "key": "passwordInput"},
        {"type": "TextInput", "id": "account_pin_input"},
        {"type": "TextInput", "id": "inputPin", "enabled": False},
        {"type": "PasswordInput"},
        {"type": "PinInput"},
        {"type": "example.PasswordInput"},
        {"type": "Column", "id": "pin_keypad"},
        {"type": "Column", "id": "pwd_field_1"},
        {"text": "Enter your PIN"},
        {"text": "Enter your 6-digit PIN"},
        {"text": "Confirm your payment passcode"},
        {"text": "请输入锁屏密码"},
        {"text": "输入 PIN 码"},
        {"text": "请输入PIN码"},
        {"text": "请设置密码"},
    ],
)
def test_password_controls_keypads_and_explicit_prompts_are_blocked(
    attributes: dict[str, Any],
) -> None:
    assert "password_field" in _signals(_node(**attributes))


@pytest.mark.parametrize("label", ["Password", "Current password:", "PIN", "锁屏密码", "支付密码"])
def test_passive_label_supplies_context_for_an_unlabeled_input(label: str) -> None:
    assert _signals(_node(text=label), _node(type="TextInput")) == ("password_field",)
    assert _signals(_node(text=label), _node(type="Search")) == ()
    assert _signals(_node(text=label), _node(type="TextInput", hint="Search entries")) == ()


@pytest.mark.parametrize("label", ["PIN", "锁屏密码", "支付密码"])
def test_password_label_with_numeric_keypad_is_blocked(label: str) -> None:
    buttons = [_node(type="Button", text=str(digit), clickable=True) for digit in range(10)]
    assert _signals(_node(text=label), *buttons) == ("password_field",)
    assert _signals(*buttons) == ()


def test_nested_menu_labels_and_hidden_context_do_not_trigger_prompts() -> None:
    menu = _node(type="Row", clickable=True)
    menu["children"] = [_node(text="Face ID"), _node(text="Password")]
    assert _signals(menu, _node(type="TextInput", hint="Username")) == ()
    assert _signals(_node(text="Password", visible=False), _node(type="TextInput")) == ()
    menu["children"].append(_node(text="Verify your identity using Face ID"))
    assert _signals(menu) == ("biometric_prompt",)


@pytest.mark.parametrize(
    "text",
    [
        "Fingerprint authentication",
        "Face verification",
        "Biometric authentication",
        "Verify your identity",
        "Place your finger on the sensor",
        "Scan your fingerprint",
        "Look at the camera",
        "指纹验证",
        "人脸认证",
        "请验证身份",
        "请按压指纹传感器",
        "请使用指纹",
        "请将手指放在传感器上",
        "请正对摄像头",
    ],
)
def test_biometric_prompts_are_blocked(text: str) -> None:
    assert "biometric_prompt" in _signals(_node(text=text))


@pytest.mark.parametrize("text", ["指纹验证", "人脸认证", "Fingerprint authentication"])
def test_explicit_biometric_verification_remains_blocked_in_clickable_containers(text: str) -> None:
    for node_type in ("Column", "Row", "MenuItem"):
        container = _node(type=node_type, clickable=True)
        container["children"] = [_node(text=text)]
        assert _signals(container) == ("biometric_prompt",)


@pytest.mark.parametrize(
    "context, grant",
    [
        ("相机权限", "允许"),
        ("麦克风", "仅使用期间允许"),
        ("通讯录", "本次允许"),
        ("位置信息", "始终允许"),
        ("读取屏幕内容和控制操作的无障碍权限", "启用"),
        ("屏幕录制", "开始录制"),
        ("屏幕共享", "立即开始"),
        ("读取通知", "授权"),
        ("Camera permission", "Allow"),
        ("Screen sharing", "Start now"),
        ("Accessibility", "Enable"),
        ("Notification access", "Grant"),
        ("VPN", "允许"),
        ("Display overlay permission", "Allow"),
        ("Install apps", "Allow"),
    ],
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


def test_permission_grant_text_remains_detectable_with_a_description() -> None:
    assert _signals(
        _node(text="Screen sharing"),
        _node(type="Button", text="Allow", description="Start capture", clickable=True),
    ) == ("system_permission_dialog",)


@pytest.mark.parametrize(
    "text",
    [
        "是否允许示例应用使用相机？",
        "请求获取麦克风权限",
        "Allow Example to access your camera?",
        "Allow Example to access your camera",
        "允许示例应用读取屏幕内容",
        "Example would like to record your screen. Screen recording permission",
        "Screen sharing\nStart now",
    ],
)
def test_permission_request_and_multiline_ocr_text_are_blocked(text: str) -> None:
    assert _signals(_node(text=text)) == ("system_permission_dialog",)


@pytest.mark.parametrize(
    "bundle, signal",
    [
        ("com.ohos.permissionmanager", "system_permission_dialog"),
        ("com.huawei.hmos.permissionmanager", "system_permission_dialog"),
        ("com.ohos.security.privacycenter", "system_permission_dialog"),
        ("com.ohos.useriam.authwidget", "biometric_prompt"),
        ("com.huawei.hmos.useriam.authwidget", "biometric_prompt"),
    ],
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
