"""Offline verification-code extraction tests using entirely synthetic messages."""

from __future__ import annotations

import pytest

from moyu_harmony_pilot.actions.sms_codes import extract_verification_codes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("合成短信验证码：012345，请勿分享。", ["012345"]),
        ("123456 is your verification code.", ["123456"]),
    ],
    ids=["leading-zero-after-label", "number-before-label"],
)
def test_explicit_context_before_and_after_number(text: str, expected: list[str]) -> None:
    assert extract_verification_codes(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("验证码：123456、654321、123456。", ["123456", "654321"])],
    ids=["distinct-candidates-in-message-order"],
)
def test_ambiguity_order_and_deduplication(text: str, expected: list[str]) -> None:
    assert extract_verification_codes(text) == expected


@pytest.mark.parametrize(
    "text",
    ["123456", "验证码：１２3456"],
    ids=["missing-label", "mixed-unicode-digits"],
)
def test_requires_explicit_local_context_and_ascii_digits(text: str) -> None:
    assert extract_verification_codes(text) == []


@pytest.mark.parametrize(
    "noise",
    ["000-000-1234", "2026-11-25"],
    ids=["phone-number", "calendar-date"],
)
def test_ignores_phone_date_and_duration_formats(noise: str) -> None:
    assert extract_verification_codes(f"验证码：{noise}。") == []


@pytest.mark.parametrize(
    "text",
    ["验证码：654321，有效期1200秒，发送日期2026-11-25。"],
    ids=["code-with-expiry-and-date"],
)
def test_noise_does_not_hide_the_real_candidate(text: str) -> None:
    assert extract_verification_codes(text) == ["654321"]


@pytest.mark.parametrize("code", ["20261125"], ids=["date-shaped-code"])
def test_numeric_shape_alone_does_not_disqualify_a_code(code: str) -> None:
    assert extract_verification_codes(f"验证码：{code}。") == [code]
