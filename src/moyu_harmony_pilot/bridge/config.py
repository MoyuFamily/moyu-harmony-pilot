"""Private JSON worker configuration and explicit per-alias capabilities."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.bridge.protocol import broker_url, decode_object, identifier, number, opaque
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceRegistry,
    check_config_permissions,
)


@dataclass(frozen=True)
class BridgeConfig:
    bridge_id: str
    broker_url: str
    devices_config: Path
    allowed_actions: dict[str, tuple[str, ...]]
    state_dir: Path
    token: str = field(repr=False)
    heartbeat_seconds: float = 30
    poll_seconds: float = 15
    wait_seconds: int = 15
    lease_seconds: float = 15
    request_timeout: float = 10
    plugin_paths: tuple[Path, ...] | None = None
    max_jobs: int = 1

    def __post_init__(self) -> None:
        if type(self.max_jobs) is not int or not 1 <= self.max_jobs <= 32:
            raise DeviceConfigurationError("max_jobs must be between 1 and 32.")
        identifier(self.bridge_id)
        object.__setattr__(self, "broker_url", broker_url(self.broker_url))
        opaque(self.token, limit=8192)
        if any(char.isspace() for char in self.token):
            raise DeviceConfigurationError("Bridge bearer token contains whitespace.")

    @classmethod
    def load(cls, path: str | Path, *, env: Mapping[str, str] | None = None) -> BridgeConfig:
        environment = os.environ if env is None else env
        source = Path(path).expanduser().resolve()
        try:
            if not check_config_permissions(source):
                raise ValueError("permissions")
            with source.open("rb") as stream:
                values = decode_object(stream.read(64 * 1024 + 1))
            required = {"bridge_id", "broker_url", "devices_config", "allowed_actions"}
            optional = {
                "state_dir",
                "token_env",
                "token_file",
                "heartbeat_seconds",
                "poll_seconds",
                "wait_seconds",
                "lease_seconds",
                "request_timeout",
                "plugin_paths",
                "max_jobs",
            }
            if not required <= values.keys() or values.keys() - required - optional:
                raise ValueError("fields")
            aliases = values["allowed_actions"]
            if not isinstance(aliases, dict) or not 1 <= len(aliases) <= 32:
                raise ValueError("aliases")
            allowed = {}
            for alias, actions in aliases.items():
                identifier(alias)
                if not isinstance(actions, list) or len(actions) > 64:
                    raise ValueError("actions")
                allowed[alias] = tuple(sorted({identifier(action) for action in actions}))
            token_env = values.get("token_env", "HARMONY_PILOT_BRIDGE_TOKEN")
            if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env):
                raise ValueError("token_env")
            token = environment.get(token_env, "")
            if not token and values.get("token_file"):
                token_path = cls._path(source.parent, values["token_file"])
                if not check_config_permissions(token_path):
                    raise ValueError("token permissions")
                with token_path.open("r", encoding="utf-8") as stream:
                    token = stream.read(8193).strip()
            token = opaque(token, limit=8192)
            if any(char.isspace() for char in token):
                raise ValueError("token whitespace")
            wait = values.get("wait_seconds", 15)
            if type(wait) is not int or not 0 <= wait <= 25:
                raise ValueError("wait_seconds")
            plugin_paths = None
            if "plugin_paths" in values:
                paths = values["plugin_paths"]
                if not isinstance(paths, list) or len(paths) > 32:
                    raise ValueError("plugin_paths")
                plugin_paths = tuple(cls._path(source.parent, path).resolve() for path in paths)
            return cls(
                bridge_id=identifier(values["bridge_id"]),
                broker_url=broker_url(values["broker_url"]),
                devices_config=cls._path(source.parent, values["devices_config"]),
                allowed_actions=allowed,
                state_dir=cls._path(source.parent, values.get("state_dir", ".state")),
                token=token,
                wait_seconds=wait,
                heartbeat_seconds=number(
                    values.get("heartbeat_seconds", 30), minimum=1, maximum=300
                ),
                poll_seconds=number(values.get("poll_seconds", 15), minimum=0.1, maximum=300),
                lease_seconds=number(values.get("lease_seconds", 15), minimum=0.1, maximum=30),
                request_timeout=number(values.get("request_timeout", 10), minimum=0.1, maximum=60),
                plugin_paths=plugin_paths,
                max_jobs=values.get("max_jobs", 1),
            )
        except Exception as exc:
            raise DeviceConfigurationError(
                "Invalid Bridge config. Check fields, credentials and owner-only permissions."
            ) from exc

    @staticmethod
    def _path(parent: Path, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path")
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else parent / candidate


def validate_configuration(
    config: BridgeConfig, *, registry: ActionRegistry | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[DeviceRegistry, ActionRegistry]:
    if not check_config_permissions(config.devices_config):
        raise DeviceConfigurationError(
            "Device alias configuration requires owner-only permissions."
        )
    try:
        devices = DeviceRegistry.from_file(config.devices_config)
    except DeviceConfigurationError as exc:
        raise DeviceConfigurationError("Invalid private device alias configuration.") from exc
    for alias in config.allowed_actions:
        if devices.get_by_alias(alias) is None:
            raise DeviceConfigurationError("Bridge allowlist refers to an unconfigured alias.")
    allowed = {action for actions in config.allowed_actions.values() for action in actions}
    actions = (
        registry
        if registry is not None
        else ActionRegistry.discover(
            env=env,
            remote=True,
            allowed_actions=allowed,
            plugin_paths=config.plugin_paths,
        )
    )
    for action in allowed:
        actions.get(action, remote=True)
    return devices, actions
