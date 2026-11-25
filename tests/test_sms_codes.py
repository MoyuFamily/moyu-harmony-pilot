"""Offline verification-code extraction tests using entirely synthetic messages."""

from __future__ import annotations

import pytest

from moyu_harmony_pilot.actions.sms_codes import extract_verification_codes


def test_visible_character_limit_preserves_complete_numeric_context():
    text = "短信验证码12345678"
    assert extract_verification_codes(text, max_chars=len(text) - 2) == []
    assert extract_verification_codes(text, max_chars=len(text)) == ["12345678"]
    assert extract_verification_codes("OTP: 1234 seconds", max_chars=9) == []
    assert extract_verification_codes("OTP: 1234. OTP: 5678", max_chars=9) == ["1234"]
    assert extract_verification_codes(text, max_chars=0) == []


@pytest.mark.parametrize("max_chars", [-1, True, 1.5, "4"])
def test_invalid_visible_character_limit(max_chars):
    with pytest.raises(ValueError):
        extract_verification_codes("OTP: 1234", max_chars=max_chars)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("【合成测试】您的验证码为123456，5分钟内有效。", ["123456"]),
        ("合成短信验证码：012345，请勿分享。", ["012345"]),
        ("本次登录验证码是 0000。", ["0000"]),
        ("校验码为12345。", ["12345"]),
        ("动态密码：1234567。", ["1234567"]),
        ("一次性密码：12345678。", ["12345678"]),
        ("驗證碼為（654321）。", ["654321"]),
        ("安全码：【987654】", ["987654"]),
        ("123456是您的验证码，请在页面输入。", ["123456"]),
        ("123456是您的登录验证码。", ["123456"]),
        ("请使用123456作为短信验证码。", ["123456"]),
        ("（0123）为本次验证码。", ["0123"]),
        ("654321（验证码）", ["654321"]),
        ("验证码\n123456", ["123456"]),
        ("验证码（5分钟内有效）：123456。", ["123456"]),
        ("验证码（有效期1200秒）为123456。", ["123456"]),
        ("Your verification code is 123456. Valid for 5 minutes.", ["123456"]),
        ("VERIFICATION CODE: 654321", ["654321"]),
        ("Your OTP is 4321.", ["4321"]),
        ("Your OTP for login is 4321.", ["4321"]),
        ("Your OTP (valid for 1200 seconds) is 4321.", ["4321"]),
        ("otp：000001", ["000001"]),
        ("Your one-time code is 12345678.", ["12345678"]),
        ("One time code: 00001234", ["00001234"]),
        ("One‑time code is 4321", ["4321"]),
        ("One-time password: 654321", ["654321"]),
        ("Security code: 0123456", ["0123456"]),
        ("Login code = 9876", ["9876"]),
        ("123456 is your verification code.", ["123456"]),
        ("4321 is your OTP.", ["4321"]),
        ("Use 4321 as your OTP.", ["4321"]),
        ("12345678 is the one-time code.", ["12345678"]),
        ("876543 is a security code.", ["876543"]),
        ("9876 is your login code.", ["9876"]),
        ("【合成测试】OTP：（123456）。", ["123456"]),
    ],
)
def test_explicit_context_before_and_after_number(text: str, expected: list[str]) -> None:
    assert extract_verification_codes(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("验证码：1234或5678。", ["1234", "5678"]),
        ("验证码可能是123456或者654321。", ["123456", "654321"]),
        ("验证码：123456、654321、123456。", ["123456", "654321"]),
        ("验证码：123456／654321", ["123456", "654321"]),
        ("验证码：【123456】、【654321】", ["123456", "654321"]),
        ("OTPs: '123456', '654321'", ["123456", "654321"]),
        ("验证码：123456 654321", ["123456", "654321"]),
        ("验证码：0000 12345678", ["0000", "12345678"]),
        (
            "验证码：0000，00000，000000，0000000，00000000",
            ["0000", "00000", "000000", "0000000", "00000000"],
        ),
        ("Your OTP may be either 123456 or 654321.", ["123456", "654321"]),
        ("Security codes are 4567 and 2345.", ["4567", "2345"]),
        ("1234或5678是您的验证码。", ["1234", "5678"]),
        ("123456 or 654321 is your OTP.", ["123456", "654321"]),
        ("1234, 5678 are your verification codes.", ["1234", "5678"]),
        ("OTP: 654321. 验证码：123456。Login code: 654321.", ["654321", "123456"]),
        ("订单123456。验证码：654321。验证码：123456。", ["654321", "123456"]),
    ],
)
def test_ambiguity_order_and_deduplication(text: str, expected: list[str]) -> None:
    assert extract_verification_codes(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \n\t",
        "123456",
        "订单号123456，稍后配送。",
        "Your code is 123456.",
        "Promotion code: 123456",
        "Postal code: 123456",
        "验证码已发送。订单号123456",
        "验证码已发送，订单号123456",
        "OTP unavailable; reference 123456",
        "pseudootp: 123456",
        "otp_value: 123456",
        "verification codename: 123456",
        "验证码：123",
        "验证码：123456789",
        "验证码：00000000000",
        "验证码：１２３４５６",
        "验证码：١٢٣٤٥٦",
        "验证码：１２3456",
        "验证码：1234５",
        "验证码：١1234",
        "验证码：AB123456CD",
        "验证码：x123456",
        "123456x is your OTP",
        "验证码：_123456_",
        "验证码：123-456",
        "验证码：12 34 56",
        "验证码：" + " " * 90 + "123456",
    ],
)
def test_requires_explicit_local_context_and_ascii_digits(text: str) -> None:
    assert extract_verification_codes(text) == []


@pytest.mark.parametrize(
    "noise",
    [
        "00000000000",
        "000-000-1234",
        "000 000 1234",
        "000 0000 1234",
        "+00 000 000 1234",
        "+00 12345678",
        "(000) 12345678",
        "（000）1234-5678",
        "000 12345678",
        "1234-5678",
        "1234-56-7890",
        "000000000-1234",
        "电话：1234-5678",
        "phone number is 1234-5678",
        "2026-11-25",
        "2026/11/25",
        "2026．11．25",
        "2026／11／25",
        "2026年11月25日",
        "2026年",
        "2026-11",
        "25/11/2026",
        "11-25-2026",
        "November 25, 2026",
        "25 Nov 2026",
        "November 2026",
        "1200秒",
        "1200 秒钟",
        "1234分钟",
        "1234 分",
        "1234 seconds",
        "1234 seconds remaining",
        "1234 sec",
        "1234 s",
        "1234 minutes",
        "1234 min",
        "1234 m",
        "1234 hours",
    ],
)
def test_ignores_phone_date_and_duration_formats(noise: str) -> None:
    assert extract_verification_codes(f"验证码：{noise}。") == []
    assert extract_verification_codes(f"{noise} is your OTP.") == []
    assert extract_verification_codes(f"OTP: {noise}, 654321") == ["654321"]


@pytest.mark.parametrize(
    "text",
    [
        "手机号12345678，验证码：654321。",
        "电话12345678（验证码：654321）",
        "客服电话：12345678。验证码：654321。",
        "验证码已发送到尾号1234的手机。OTP: 654321",
        "Phone number is 12345678. Your OTP is 654321.",
        "Phone ending in 1234, OTP: 654321",
        "OTP: 12345678（电话），验证码：654321",
        "OTP: 654321 phone 12345678",
        "验证码：654321 电话：1234-5678",
        "日期20261125，验证码：654321。",
        "Date: 20261125. OTP: 654321",
        "验证码：654321，有效期1200秒，发送日期2026-11-25。",
        "OTP: 654321, valid for 1200 seconds. Call 000-000-1234.",
        "订单号1234，验证码：654321",
        "Reference 1234, OTP: 654321",
    ],
)
def test_noise_does_not_hide_the_real_candidate(text: str) -> None:
    assert extract_verification_codes(text) == ["654321"]


@pytest.mark.parametrize("code", ["2026", "20261125", "1200", "12345678"])
def test_numeric_shape_alone_does_not_disqualify_a_code(code: str) -> None:
    assert extract_verification_codes(f"验证码：{code}。") == [code]
    assert extract_verification_codes(f"{code} is your OTP.") == [code]


@pytest.mark.parametrize("label", ["OTP", "verification code", "验证码"])
def test_context_distance_is_measured_from_complete_labels(label: str) -> None:
    assert extract_verification_codes(label + " " * 80 + "1234") == ["1234"]
    assert extract_verification_codes("1234" + " " * 80 + label) == ["1234"]
    assert extract_verification_codes(label + " " * 81 + "1234") == []
    assert extract_verification_codes("1234" + " " * 81 + label) == []


def test_window_boundary_does_not_create_a_keyword_inside_a_word() -> None:
    assert extract_verification_codes("1234" + " " * 77 + "OTPword") == []


@pytest.mark.parametrize("value", [None, 123456, 1.0, True, b"OTP: 123456", [], {}])
def test_non_string_input_raises_type_error(value: object) -> None:
    with pytest.raises(TypeError, match="text must be a string"):
        extract_verification_codes(value)  # type: ignore[arg-type]


def test_calls_return_independent_lists_without_output(capsys: pytest.CaptureFixture[str]) -> None:
    first = extract_verification_codes("OTP: 012345")
    first.append("999999")
    assert extract_verification_codes("OTP: 012345") == ["012345"]
    assert capsys.readouterr() == ("", "")
