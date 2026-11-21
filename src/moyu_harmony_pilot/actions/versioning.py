"""Zero-dependency PEP 440 version parser for plugin compatibility checks."""

from __future__ import annotations

import re

from moyu_harmony_pilot.device import DeviceConfigurationError

_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?P<pre_tag>a|b|rc)(?P<pre_num>0|[1-9]\d*))?"
    r"(?:\.post(?P<post_num>0|[1-9]\d*))?"
    r"(?:\.dev(?P<dev_num>0|[1-9]\d*))?$"
)

_PRE_RANK = {"a": 1, "b": 2, "rc": 3}


def parse_pilot_version(raw: str) -> tuple[int, int, int, int, int, int, int, int, int]:
    """Parse a canonical PEP 440 `X.Y.Z[aN|bN|rcN][.postN][.devN]` version string."""
    if not isinstance(raw, str) or len(raw) > 64:
        raise DeviceConfigurationError("Invalid Pilot version string.")
    match = _VERSION_RE.fullmatch(raw)
    if match is None:
        raise DeviceConfigurationError(
            "Invalid Pilot version string. Use canonical X.Y.Z (optional a/b/rc/post/dev)."
        )
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    pre_tag = match.group("pre_tag")
    pre_num = int(match.group("pre_num")) if match.group("pre_num") is not None else 0
    post_num = int(match.group("post_num")) if match.group("post_num") is not None else None
    dev_num = int(match.group("dev_num")) if match.group("dev_num") is not None else None

    if pre_tag is not None:
        pre_rank = _PRE_RANK[pre_tag]
    elif post_num is None and dev_num is not None:
        # Pure X.Y.Z.devN sorts before any X.Y.ZaN/bN/rcN/final release.
        pre_rank = 0
    else:
        pre_rank = 4

    post_flag = 1 if post_num is not None else 0
    post_val = post_num if post_num is not None else 0
    dev_flag = 0 if dev_num is not None else 1
    dev_val = dev_num if dev_num is not None else 0

    return (major, minor, patch, pre_rank, pre_num, post_flag, post_val, dev_flag, dev_val)


def is_version_compatible(current: str, minimum: str) -> bool:
    """Return True when `current` satisfies `>= minimum` under PEP 440 ordering."""
    return parse_pilot_version(current) >= parse_pilot_version(minimum)
