"""Explicit action registration and local plugin discovery."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions.models import (
    ACTION_API_VERSION,
    CONTEXT_API_MIN_VERSION,
    PLUGIN_CALL_MIN_VERSION,
    ActionPlugin,
    ActionSpec,
    IdempotentAction,
)
from moyu_harmony_pilot.actions.validation import (
    IDENTIFIER,
    MAX_JSON_BYTES,
    check_schema,
    json_copy,
    validate_value,
)
from moyu_harmony_pilot.actions.versioning import is_version_compatible, parse_pilot_version
from moyu_harmony_pilot.device import DeviceConfigurationError


def _check_metrics_schema(schema: dict[str, Any]) -> None:
    check_schema(schema)
    if schema["type"] != "object":
        raise DeviceConfigurationError("Parameters and metrics must be JSON objects.")
    metric_properties = schema.get("properties", {})
    if len(metric_properties) > 32:
        raise DeviceConfigurationError("Metrics may declare up to 32 fields.")
    for name, prop_schema in metric_properties.items():
        kinds = prop_schema["type"]
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if len(name) > 64 or {"object", "array"} & set(kinds):
            raise DeviceConfigurationError(
                "Metric fields require bounded names and scalar types."
            )


class ActionRegistry:
    def __init__(
        self,
        *,
        allowed_actions: Iterable[str] = (),
        pilot_version: str | None = None,
    ) -> None:
        if isinstance(allowed_actions, str):
            raise DeviceConfigurationError("allowed_actions must be a collection of action IDs.")
        self.allowed_actions = frozenset(allowed_actions)
        if any(
            not isinstance(name, str) or not IDENTIFIER.fullmatch(name)
            for name in self.allowed_actions
        ):
            raise DeviceConfigurationError("Invalid allowed action ID.")
        resolved_version = __version__ if pilot_version is None else pilot_version
        parse_pilot_version(resolved_version)
        self.pilot_version = resolved_version
        self._plugins: dict[str, ActionPlugin] = {}
        self._specs: dict[str, ActionSpec] = {}
        self._sources: dict[str, str] = {}

    def register(self, plugin: ActionPlugin, *, source: str = "external") -> None:
        if source not in {"internal", "external"}:
            raise DeviceConfigurationError("Invalid plugin source.")
        if not isinstance(plugin, ActionPlugin) or not isinstance(
            getattr(plugin, "spec", None), ActionSpec
        ):
            raise DeviceConfigurationError("Plugins must implement ActionPlugin and ActionSpec.")
        spec = ActionSpec(**plugin.spec.to_dict())
        if type(spec.api_version) is not int or spec.api_version != ACTION_API_VERSION:
            raise DeviceConfigurationError(
                f"Unsupported action API version. This Pilot supports API {ACTION_API_VERSION}."
            )
        if spec.min_pilot_version is not None:
            parse_pilot_version(spec.min_pilot_version)
            if not is_version_compatible(self.pilot_version, spec.min_pilot_version):
                raise DeviceConfigurationError(
                    f"Action requires Pilot >= {spec.min_pilot_version} "
                    f"(running {self.pilot_version})."
                )
        if (
            not isinstance(spec.action_id, str)
            or not IDENTIFIER.fullmatch(spec.action_id)
            or not isinstance(spec.version, str)
            or not 1 <= len(spec.version) <= 64
            or not isinstance(spec.description, str)
            or not 1 <= len(spec.description) <= 512
            or spec.side_effect not in {"readonly", "mutating"}
            or type(spec.timeout_seconds) not in (int, float)
            or not math.isfinite(spec.timeout_seconds)
            or spec.timeout_seconds <= 0
            or type(spec.local_only) is not bool
            or type(spec.diagnostic) is not bool
            or type(spec.private_content) is not bool
            or type(spec.lock_device) is not bool
        ):
            raise DeviceConfigurationError("Invalid action metadata.")
        if (spec.side_effect == "mutating") != isinstance(plugin, IdempotentAction):
            raise DeviceConfigurationError("Mutating actions must implement IdempotentAction.")
        if spec.side_effect == "mutating" and spec.diagnostic:
            raise DeviceConfigurationError("Mutating actions must use thermal protection.")
        if spec.side_effect == "mutating" and not spec.lock_device:
            raise DeviceConfigurationError("Mutating plugins must acquire the device lock.")
        if spec.result_schema is not None or spec.private_content or not spec.lock_device:
            if spec.min_pilot_version is None or not is_version_compatible(
                spec.min_pilot_version, PLUGIN_CALL_MIN_VERSION
            ):
                raise DeviceConfigurationError(
                    "Plugin result metadata requires "
                    f"min_pilot_version >= {PLUGIN_CALL_MIN_VERSION}."
                )
        if spec.result_schema is not None:
            check_schema(spec.result_schema)
            if spec.result_schema["type"] != "object":
                raise DeviceConfigurationError("Plugin results must be JSON objects.")
        check_schema(spec.params_schema)
        if spec.params_schema["type"] != "object":
            raise DeviceConfigurationError("Parameters and metrics must be JSON objects.")
        _check_metrics_schema(spec.metrics_schema)
        if spec.inspection_metrics_schema is not None:
            if spec.min_pilot_version is None or not is_version_compatible(
                spec.min_pilot_version, CONTEXT_API_MIN_VERSION
            ):
                raise DeviceConfigurationError(
                    "inspection_metrics_schema requires "
                    f"min_pilot_version >= {CONTEXT_API_MIN_VERSION}."
                )
            if spec.side_effect != "mutating":
                raise DeviceConfigurationError(
                    "inspection_metrics_schema is only supported on mutating actions."
                )
            _check_metrics_schema(spec.inspection_metrics_schema)
        for example in spec.examples:
            validate_value(json_copy(example), spec.params_schema)
        if spec.action_id in self._plugins:
            raise DeviceConfigurationError("Duplicate action ID in plugin discovery.")
        self._plugins[spec.action_id] = plugin
        self._specs[spec.action_id] = spec
        self._sources[spec.action_id] = source

    def get(
        self, action_id: str, *, remote: bool = False, private_actions: Iterable[str] = (),
    ) -> tuple[ActionPlugin, ActionSpec]:
        if remote and action_id not in self.allowed_actions:
            raise DeviceConfigurationError("Action is outside the remote allowlist.")
        if action_id not in self._plugins:
            raise DeviceConfigurationError("Unknown action ID. Inspect plugins list.")
        spec = self._specs[action_id]
        if remote and spec.local_only:
            raise DeviceConfigurationError("This action is restricted to local execution.")
        if remote and spec.private_content and action_id not in private_actions:
            raise DeviceConfigurationError(
                "Private plugin requires explicit startup authorization."
            )
        return self._plugins[action_id], ActionSpec(**spec.to_dict())

    def describe(self, action_id: str, *, private_actions: Iterable[str] = ()) -> dict[str, Any]:
        _, spec = self.get(action_id)
        return {
            **spec.to_dict(),
            "source": self._sources[action_id],
            "remote_allowed": (
                action_id in self.allowed_actions and not spec.local_only
                and (not spec.private_content or action_id in private_actions)
            ),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.describe(action_id) for action_id in sorted(self._plugins)]

    @classmethod
    def discover(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        allowed_actions: Iterable[str] = (),
        remote: bool = False,
        plugin_paths: Iterable[str | Path] | None = None,
        pilot_version: str | None = None,
    ) -> ActionRegistry:
        from moyu_harmony_pilot.actions.builtin import builtin_plugins

        registry = cls(allowed_actions=allowed_actions, pilot_version=pilot_version)
        for plugin in builtin_plugins():
            registry.register(plugin, source="internal")
        environment = os.environ if env is None else env
        try:
            entries = sorted(
                importlib.metadata.entry_points(group="moyu_harmony_pilot.actions"),
                key=lambda entry: (entry.name, entry.value),
            )
            for entry in entries:
                if remote and entry.name not in registry.allowed_actions:
                    continue
                registry._register_factory(entry.name, entry.load())
            paths = plugin_paths
            if paths is None:
                paths = environment.get("HARMONY_PILOT_PLUGIN_PATHS", "").split(os.pathsep)
            if isinstance(paths, str | Path):
                raise DeviceConfigurationError("plugin_paths must be a collection of directories.")
            for raw_path in paths:
                if raw_path:
                    registry._load_directory(Path(raw_path).expanduser().resolve(), remote=remote)
        except DeviceConfigurationError:
            raise
        except (Exception, SystemExit) as exc:
            raise DeviceConfigurationError(
                "Plugin discovery failed. Check trusted plugin metadata and configuration."
            ) from exc
        return registry

    def _register_factory(self, expected_id: str, factory: object) -> None:
        if not isinstance(factory, type) or not issubclass(factory, ActionPlugin):
            raise DeviceConfigurationError("Plugin entry point must name an ActionPlugin class.")
        plugin = factory()
        if plugin.spec.action_id != expected_id:
            raise DeviceConfigurationError("Plugin action ID differs from its discovery name.")
        self.register(plugin)

    def _load_directory(self, directory: Path, *, remote: bool) -> None:
        manifest_path = directory / "actions.json"
        if manifest_path.stat().st_size > MAX_JSON_BYTES:
            raise DeviceConfigurationError("Plugin manifest exceeds 16 KiB.")
        manifest = json_copy(json.loads(manifest_path.read_text(encoding="utf-8")))
        if not isinstance(manifest, dict):
            raise DeviceConfigurationError("Plugin manifest must map action IDs to entry points.")
        for action_id, entry in sorted(manifest.items()):
            if not IDENTIFIER.fullmatch(action_id) or not isinstance(entry, str):
                raise DeviceConfigurationError("Invalid plugin manifest entry.")
            if remote and action_id not in self.allowed_actions:
                continue
            filename, separator, class_name = entry.partition(":")
            path = (directory / filename).resolve()
            if (
                not separator
                or not class_name.isidentifier()
                or path.parent != directory
                or path.suffix != ".py"
                or not path.is_file()
            ):
                raise DeviceConfigurationError(
                    "Plugin entry must name a local Python file and class."
                )
            module_name = "_harmony_plugin_" + hashlib.sha256(str(path).encode()).hexdigest()
            module = sys.modules.get(module_name)
            if module is None:
                module_spec = importlib.util.spec_from_file_location(module_name, path)
                if module_spec is None or module_spec.loader is None:
                    raise DeviceConfigurationError("Unable to load local plugin module.")
                module = importlib.util.module_from_spec(module_spec)
                sys.modules[module_name] = module
                try:
                    module_spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop(module_name, None)
                    raise
            self._register_factory(action_id, getattr(module, class_name))
