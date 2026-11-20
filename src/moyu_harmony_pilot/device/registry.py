"""Local device alias registry and privacy-preserving target resolution."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.device.errors import DeviceConfigurationError, UnknownDeviceAliasError
from moyu_harmony_pilot.device.models import DEFAULT_MAX_TEMPERATURE_C, DeviceProfile
from moyu_harmony_pilot.hdc import (
    ConnectionState,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcTarget,
)

DEVICES_CONFIG_ENV = "HARMONY_PILOT_DEVICES_CONFIG"
DEFAULT_CONFIG_FILENAMES = ("devices.local.yaml", "devices.local.yml", "devices.local.json")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


def _parse_yaml_scalar(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        if len(text) >= 2:
            if text[0] == '"':
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text[1:-1]
            return text[1:-1]
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the documented simple YAML mapping subset without requiring PyYAML."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        cleaned = _strip_yaml_comment(raw_line).rstrip()
        if not cleaned.strip():
            continue
        if "\t" in cleaned[: len(cleaned) - len(cleaned.lstrip(" \t"))]:
            raise DeviceConfigurationError(
                f"Tabs are not permitted in YAML indentation (line {lineno})."
            )
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        stripped = cleaned[indent:]
        if stripped.startswith("- "):
            raise DeviceConfigurationError(
                f"YAML sequences are not supported in devices.local.yaml (line {lineno}); "
                "use a mapping under `devices:` or use JSON."
            )
        if ":" not in stripped:
            raise DeviceConfigurationError(
                f"Expected `key: value` mapping entry in YAML config (line {lineno})."
            )
        key_raw, val_raw = stripped.split(":", 1)
        key = str(_parse_yaml_scalar(key_raw.strip()))
        if not key:
            raise DeviceConfigurationError(f"Empty YAML key at line {lineno}.")

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]

        value_text = val_raw.strip()
        if not value_text:
            nested: dict[str, Any] = {}
            current[key] = nested
            stack.append((indent, nested))
        else:
            current[key] = _parse_yaml_scalar(value_text)

    return root


def _load_structured_config_file(path: Path) -> Mapping[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeviceConfigurationError(
            f"Unable to read device configuration file {path}: {exc}"
        ) from exc

    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise DeviceConfigurationError(
                f"Invalid JSON in {path} (line {exc.lineno}): {exc.msg}"
            ) from exc
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            decoded = _parse_simple_yaml(raw_text)
        else:
            try:
                decoded = yaml.safe_load(raw_text)
            except Exception as exc:
                raise DeviceConfigurationError(f"Invalid YAML in {path}: {exc}") from exc
    else:
        # Attempt JSON first, then simple YAML fallback.
        try:
            decoded = json.loads(raw_text)
        except json.JSONDecodeError:
            decoded = _parse_simple_yaml(raw_text)

    if decoded is None:
        return {}
    if not isinstance(decoded, Mapping):
        raise DeviceConfigurationError(
            f"Device configuration root in {path} must be a mapping/object."
        )
    return decoded


def _validate_temperature(value: Any, *, alias: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DeviceConfigurationError(
            f"max_temperature_c for alias {alias!r} must be a number."
        )
    temp = float(value)
    if not math.isfinite(temp) or not (20.0 < temp <= 70.0):
        raise DeviceConfigurationError(
            f"max_temperature_c for alias {alias!r} must be in (20.0, 70.0], got {temp}."
        )
    return temp


def discover_devices_config(
    explicit_path: str | Path | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Locate local `devices.local.{yaml,yml,json}` if configured or present in `cwd`."""
    if explicit_path is not None and str(explicit_path).strip():
        candidate = Path(explicit_path).expanduser()
        if not candidate.is_file():
            raise DeviceConfigurationError(
                f"Specified device config file does not exist: {candidate}"
            )
        return candidate

    effective_env = os.environ if env is None else env
    env_path = effective_env.get(DEVICES_CONFIG_ENV, "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if not candidate.is_file():
            raise DeviceConfigurationError(
                f"{DEVICES_CONFIG_ENV} points to a missing file: {candidate}"
            )
        return candidate

    base_dir = Path.cwd() if cwd is None else cwd
    for filename in DEFAULT_CONFIG_FILENAMES:
        candidate = base_dir / filename
        if candidate.is_file():
            return candidate
    return None


def check_config_permissions(path: Path) -> bool:
    """Return True if file mode has no group/other read/write bits (e.g. `0600`)."""
    if os.name != "posix":
        return True
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return False
    return (mode & 0o077) == 0


class DeviceRegistry:
    """In-memory alias ↔ serial mapping with strict validation and isolation."""

    def __init__(
        self,
        profiles: Sequence[DeviceProfile] = (),
        *,
        source_path: Path | None = None,
    ) -> None:
        self._by_alias: dict[str, DeviceProfile] = {}
        self._by_serial: dict[str, DeviceProfile] = {}
        self._source_path = source_path

        for profile in profiles:
            self._register(profile)

    def _register(self, profile: DeviceProfile) -> None:
        if not _ALIAS_RE.match(profile.alias):
            raise DeviceConfigurationError(
                f"Invalid device alias {profile.alias!r}; "
                "must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$."
            )
        serial = profile.serial.strip()
        if not serial or any(ch.isspace() for ch in serial):
            raise DeviceConfigurationError(
                f"Invalid target serial configured for alias {profile.alias!r}."
            )
        _validate_temperature(profile.max_temperature_c, alias=profile.alias)

        if profile.alias in self._by_alias:
            raise DeviceConfigurationError(f"Duplicate device alias: {profile.alias!r}")
        if serial in self._by_serial:
            raise DeviceConfigurationError(
                f"Duplicate serial mapping detected for alias {profile.alias!r} "
                f"(already mapped to {self._by_serial[serial].alias!r})."
            )
        normalized = DeviceProfile(
            alias=profile.alias,
            serial=serial,
            max_temperature_c=float(profile.max_temperature_c),
            notes=profile.notes,
        )
        self._by_alias[normalized.alias] = normalized
        self._by_serial[normalized.serial] = normalized

    @classmethod
    def from_file(cls, path: str | Path) -> DeviceRegistry:
        config_path = Path(path).expanduser()
        data = _load_structured_config_file(config_path)
        devices_section = data.get("devices") if "devices" in data else data
        if devices_section is None:
            return cls((), source_path=config_path)
        if not isinstance(devices_section, Mapping):
            raise DeviceConfigurationError(
                f"`devices` section in {config_path} must be a mapping of alias -> device config."
            )

        profiles: list[DeviceProfile] = []
        for alias_key, entry in devices_section.items():
            alias = str(alias_key).strip()
            if isinstance(entry, str):
                profiles.append(DeviceProfile(alias=alias, serial=entry))
            elif isinstance(entry, Mapping):
                serial_val = entry.get("serial") or entry.get("target")
                if not isinstance(serial_val, str) or not serial_val.strip():
                    raise DeviceConfigurationError(
                        f"Alias {alias!r} in {config_path} is missing a non-empty `serial` field."
                    )
                max_temp = entry.get("max_temperature_c", DEFAULT_MAX_TEMPERATURE_C)
                notes = str(entry.get("notes", "") or "")
                profiles.append(
                    DeviceProfile(
                        alias=alias,
                        serial=serial_val.strip(),
                        max_temperature_c=_validate_temperature(max_temp, alias=alias),
                        notes=notes,
                    )
                )
            else:
                raise DeviceConfigurationError(
                    f"Unsupported entry type for alias {alias!r} in {config_path}."
                )
        return cls(profiles, source_path=config_path)

    @classmethod
    def load_default(
        cls,
        explicit_path: str | Path | None = None,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> DeviceRegistry:
        path = discover_devices_config(explicit_path, cwd=cwd, env=env)
        if path is None:
            return cls()
        return cls.from_file(path)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @property
    def profiles(self) -> tuple[DeviceProfile, ...]:
        return tuple(self._by_alias.values())

    def get_by_alias(self, alias: str) -> DeviceProfile | None:
        return self._by_alias.get(alias)

    def get_by_serial(self, serial: str) -> DeviceProfile | None:
        return self._by_serial.get(serial)

    def resolve(
        self,
        device_ref: str | None,
        targets: Sequence[HdcTarget],
        *,
        require_online: bool = True,
    ) -> tuple[DeviceProfile, HdcTarget | None]:
        """Resolve an alias (or standalone local target) against currently detected `hdc` targets.

        Raises:
        - `UnknownDeviceAliasError` (exit 2) for unknown alias or ambiguous multi-device default.
        - `DeviceUnauthorizedError` (exit 3) if matched target is unauthorized.
        - `DeviceOfflineError` (exit 3) if matched target is missing or offline.
        """
        target_by_serial = {t.serial: t for t in targets}

        if device_ref is None or not device_ref.strip():
            online_targets = [t for t in targets if t.connection_state == ConnectionState.ONLINE]
            if len(online_targets) == 1:
                chosen = online_targets[0]
                mapped = self.get_by_serial(chosen.serial)
                profile = mapped or DeviceProfile(alias="default", serial=chosen.serial)
                return profile, chosen
            if len(online_targets) > 1:
                raise UnknownDeviceAliasError(
                    "Multiple online HarmonyOS devices detected; "
                    "pass `--device <alias>` explicitly."
                )
            unauth = [t for t in targets if t.connection_state == ConnectionState.UNAUTHORIZED]
            if unauth:
                raise DeviceUnauthorizedError(
                    f"Detected unauthorized HarmonyOS device ({unauth[0].serial_masked})."
                )
            raise DeviceOfflineError("No online HarmonyOS device detected by hdc.")

        ref = device_ref.strip()
        profile = self.get_by_alias(ref)
        if profile is not None:
            matched_target = target_by_serial.get(profile.serial)
            if require_online:
                _assert_target_online(profile.alias, matched_target)
            return profile, matched_target

        # Standalone convenience: allow referencing a directly visible serial in local CLI,
        # while still honoring any configured alias if that serial is registered.
        direct_target = target_by_serial.get(ref)
        if direct_target is not None:
            mapped = self.get_by_serial(direct_target.serial)
            resolved_profile = mapped or DeviceProfile(
                alias=direct_target.serial_masked,
                serial=direct_target.serial,
            )
            if require_online:
                _assert_target_online(resolved_profile.alias, direct_target)
            return resolved_profile, direct_target

        if ref == "default":
            return self.resolve(None, targets, require_online=require_online)

        raise UnknownDeviceAliasError(
            f"Device alias {ref!r} is not configured and does not match any connected target."
        )


def _assert_target_online(alias: str, target: HdcTarget | None) -> None:
    if target is None:
        raise DeviceOfflineError(
            f"Device alias {alias!r} is not currently connected to hdc."
        )
    if target.connection_state == ConnectionState.UNAUTHORIZED:
        raise DeviceUnauthorizedError(
            f"Device alias {alias!r} is connected but unauthorized for USB debugging."
        )
    if target.connection_state != ConnectionState.ONLINE:
        raise DeviceOfflineError(
            f"Device alias {alias!r} is currently {target.connection_state.value}."
        )
