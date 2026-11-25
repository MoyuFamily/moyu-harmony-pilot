"""Conservative, standard-library-only verification-code candidates from SMS text."""

from __future__ import annotations

import re

__all__ = ["extract_verification_codes"]

_CONTEXT_DISTANCE = 80
_NUMBER = r"(?<![\dA-Za-z_])[0-9]{4,8}(?![\dA-Za-z_])"
_CANDIDATES = re.compile(_NUMBER)
_CONTEXT = re.compile(
    r"(?:短信|登录|登陆|登錄|登入|手机|身份)?(?:验证码|驗證碼)|"
    r"校验码|校驗碼|动态码|動態碼|动态密码|動態密碼|"
    r"一次性密码|一次性密碼|一次性口令|登录码|登入碼|安全码|安全碼|"
    r"(?ai:\b(?:otps?|(?:verification|security|login)[\s-]+codes?|"
    r"one[\s‐‑–—－-]+time[\s‐‑–—－-]+(?:codes?|passwords?))\b)"
)

# Restrict the link to common copulas, wrappers, and explicit candidate lists.
# Unrelated prose between a context label and a number breaks the association.
_WRAPPER = r"[\s:：=＝()（）\[\]【】<>《》\"'“”‘’「」『』‐‑–—－-]"
_FORWARD_WORD = (
    r"(?:是|为|為|如下|可能|分别|分別|有效期|有效|内|內|请|請|使用|"
    r"(?ai:\b(?:is|are|may|be|either|valid|for|expires|in|within|login)\b))"
)
_BACKWARD_WORD = r"(?:是|为|為|作为|作為|您|你|的|本次|此次|(?ai:\b(?:is|are|as|your|the|a|an)\b))"
_LIST_SEPARATOR = rf"(?:{_WRAPPER}|[,，、/／|&]|或者|或|和|及|(?ai:\b(?:or|and)\b))+"
_FORWARD_LINK = re.compile(rf"(?:{_WRAPPER}|[,，]|{_FORWARD_WORD})*(?:{_NUMBER}{_LIST_SEPARATOR})*")
_BACKWARD_LINK = re.compile(rf"(?:{_LIST_SEPARATOR}{_NUMBER})*(?:{_WRAPPER}|{_BACKWARD_WORD})*")

_EXCLUDED_NUMBERS = re.compile(
    r"""
    # Consume entire separated phone/date runs before their shorter subformats.
    (?<!\d)[+＋]?[0-9]+(?:[‐‑–—－.．-][0-9]+)+(?!\d)

    # Calendar dates, including year/month, Chinese units, and English months.
    | (?<!\d)[0-9]{4}\s*[-/／.．年]\s*[0-9]{1,2}
        (?:\s*[-/／.．月]\s*[0-9]{1,2}日?)?(?!\d)
    | (?<!\d)[0-9]{1,2}\s*[-/／.．]\s*[0-9]{1,2}
        \s*[-/／.．]\s*[0-9]{4}(?!\d)
    | (?<!\d)[0-9]{4}\s*年
    | (?<![A-Za-z])(?:[0-9]{1,2}\s+)?
        (?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|
           Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?
        (?:\s+[0-9]{1,2},?)?\s+[0-9]{4}(?!\d)
    | (?:日期|时间|時間|(?ai:\b(?:date|timestamp)\b))
        [\s:：=＝]*[0-9]{4,8}(?!\d)

    # Phone-like groups and local numbers identified explicitly as phone numbers.
    | (?<!\d)(?:[+＋][0-9]{1,3}\s*)?
        [(（][0-9]{2,4}[)）]\s*
        (?:[0-9]{3,4}[\s-]+[0-9]{4}|[0-9]{6,8})(?!\d)
    | (?<!\d)(?:[+＋][0-9]{1,3}[\s-]+)?
        [0-9]{3}[\s-]+[0-9]{3,4}[\s-]+[0-9]{4}(?!\d)
    | (?<!\d)[+＋][0-9]{1,3}\s+[0-9]{4,8}(?!\d)
    | (?<!\d)0[0-9]{1,2}\s+[0-9]{7,8}(?!\d)
    | (?<!\d)[0-9]{9,}(?!\d)
    | (?:电话|電話|手机|手機|客服|热线|熱線|尾号|尾號|末四位|后四位|
         (?ai:\b(?:phone|telephone|tel|mobile|call|contact|ending)\b))
        (?:[\s:：=＝()（）.*＊xX+＋-]|号码|號碼|号|號|尾号|尾號|是|为|為|
           (?ai:\b(?:number|no|is|ending|in|at)\b))*
        [0-9]+(?:[ \t‐‑–—－.．()（）-]+[0-9]+)*(?!\d)
    | (?<!\d)[0-9]{4,8}\s*[(（]\s*
        (?:电话|電話|手机号|手機號|(?ai:\b(?:phone|telephone)\b))\s*[)）]

    # Durations also cover the uncommon case of four or more digits in a TTL.
    | (?<!\d)[0-9]+[\s-]*(?:秒钟|秒鐘|秒|分钟|分鐘|分|小时|小時|天|
        (?ai:\b(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b))
        (?:[ \t]+(?ai:remaining\b))?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _has_context(
    text: str,
    number: re.Match[str],
    before: re.Match[str] | None,
    after: re.Match[str] | None,
) -> bool:
    start, end = number.span()
    return (
        before is not None
        and start - before.end() <= _CONTEXT_DISTANCE
        and _FORWARD_LINK.fullmatch(text[before.end() : start]) is not None
    ) or (
        after is not None
        and after.start() - end <= _CONTEXT_DISTANCE
        and _BACKWARD_LINK.fullmatch(text[end : after.start()]) is not None
    )


def extract_verification_codes(text: str, *, max_chars: int | None = None) -> list[str]:
    """Return distinct 4–8 digit ASCII candidates in their original text order.

    A candidate needs an explicit Chinese verification-code label or an English
    verification code, OTP, one-time code/password, security code, or login code
    label within 80 characters, connected by a supported short phrase or list.
    Phone numbers, formatted dates, and durations are excluded. Numeric values
    keep leading zeros, and date-shaped values can still be valid candidates
    when explicitly labelled as codes. Full-width digits and alphanumeric or
    split codes are unsupported. Candidates do not establish validity or expiry.

    max_chars limits candidate positions while retaining the full text for context
    and numeric boundaries, so a truncated display cannot create a partial code.
    Raise TypeError for non-string input. No I/O or global state is modified.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
        raise ValueError("max_chars must be a nonnegative integer")

    # Keep offsets and separation while removing numbers with a known other role.
    contextual_text = _EXCLUDED_NUMBERS.sub(lambda match: " " * len(match.group()), text)
    contexts = iter(_CONTEXT.finditer(contextual_text))
    before = None
    after = next(contexts, None)
    codes: list[str] = []
    seen: set[str] = set()
    for number in _CANDIDATES.finditer(text):
        if max_chars is not None and number.end() > max_chars:
            continue
        code = number.group()
        if code in seen or contextual_text[number.start() : number.end()] != code:
            continue
        # A further label would have to cross the nearest label, which breaks a link.
        while after is not None and after.end() <= number.start():
            before = after
            after = next(contexts, None)
        if _has_context(contextual_text, number, before, after):
            seen.add(code)
            codes.append(code)
    return codes
