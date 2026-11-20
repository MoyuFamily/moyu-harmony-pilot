"""Bounded public bundle metadata parsed from official bm output."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from moyu_harmony_pilot.device.errors import DeviceConfigurationError, DeviceError


def validate_bundle(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
        raise DeviceConfigurationError("Invalid bundle or ability name.")
    return value


@dataclass(frozen=True)
class AppInfo:
    bundle_name: str
    version_code: int | None
    version_name: str | None
    entry_ability: str | None
    entry_module: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_app_list(output: str) -> list[str]:
    text = output.strip()
    if text.lower().startswith("all bundle names:"):
        values = text.split(":", 1)[1].split()
    else:
        try:
            values = json.loads(text)
        except ValueError as exc:
            raise DeviceError("Unrecognized bm application list.", code="app_query_failed") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise DeviceError("Invalid bm application list.", code="app_query_failed")
    return sorted({validate_bundle(value) for value in values})


def parse_app_info(output: str, bundle_name: str) -> AppInfo:
    try:
        start = output.index("{")
        payload = json.loads(output[start:])
        application = payload.get("applicationInfo", payload.get("appInfo", {}))
        name = payload.get("name", payload.get("bundleName", application.get("bundleName")))
        if name != bundle_name:
            raise ValueError("Bundle mismatch")
        version = payload.get("versionCode", application.get("versionCode"))
        version_name = payload.get("versionName", application.get("versionName"))
        if version is not None and (type(version) is not int or version < 0):
            raise ValueError("Invalid version")
        if version_name is not None and (
            not isinstance(version_name, str) or len(version_name) > 128
        ):
            raise ValueError("Invalid version name")
        modules = payload.get("hapModuleInfos", [])
        entries = []
        for module in modules:
            ability = module.get("mainElementName") or module.get("mainAbility")
            if ability:
                module_name = module.get("moduleName") or module.get("name")
                is_entry = module.get("isEntry") is True or module.get("moduleType") == "entry"
                entries.append((validate_bundle(ability), module_name, is_entry))
        preferred = [entry for entry in entries if entry[2] is True]
        candidates = preferred or entries
        ability, module = None, None
        if len(candidates) == 1:
            ability, module, _ = candidates[0]
        elif not candidates and payload.get("mainAbility"):
            ability = validate_bundle(payload["mainAbility"])
        if module is not None:
            module = validate_bundle(module)
        return AppInfo(bundle_name, version, version_name, ability, module)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise DeviceError("Unrecognized bm bundle metadata.", code="app_query_failed") from exc
