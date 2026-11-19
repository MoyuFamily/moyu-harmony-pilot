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

from moyu_harmony_pilot.actions.models import ActionPlugin, ActionSpec, IdempotentAction
from moyu_harmony_pilot.actions.validation import (
    IDENTIFIER,
    MAX_JSON_BYTES,
    check_schema,
    json_copy,
    validate_value,
)
from moyu_harmony_pilot.device import DeviceConfigurationError


class ActionRegistry:
    def __init__(self, *, allowed_actions: Iterable[str] = ()) -> None:
        if isinstance(allowed_actions, str):
            raise DeviceConfigurationError("allowed_actions must be a collection of action IDs.")
        self.allowed_actions = frozenset(allowed_actions)
        if any(
            not isinstance(name, str) or not IDENTIFIER.fullmatch(name)
            for name in self.allowed_actions
        ):
            raise DeviceConfigurationError("Invalid allowed action ID.")
        self._plugins: dict[str, ActionPlugin] = {}
        self._specs: dict[str, ActionSpec] = {}

    def register(self, plugin: ActionPlugin) -> None:
        if not isinstance(plugin, ActionPlugin) or not isinstance(
            getattr(plugin, "spec", None), ActionSpec
        ):
            raise DeviceConfigurationError("Plugins must implement ActionPlugin and ActionSpec.")
        spec = ActionSpec(**plugin.spec.to_dict())
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
        ):
            raise DeviceConfigurationError("Invalid action metadata.")
        if (spec.side_effect == "mutating") != isinstance(plugin, IdempotentAction):
            raise DeviceConfigurationError("Mutating actions must implement IdempotentAction.")
        if spec.side_effect == "mutating" and spec.diagnostic:
            raise DeviceConfigurationError("Mutating actions must use thermal protection.")
        for schema in (spec.params_schema, spec.metrics_schema):
            check_schema(schema)
            if schema["type"] != "object":
                raise DeviceConfigurationError("Parameters and metrics must be JSON objects.")
        metric_properties = spec.metrics_schema.get("properties", {})
        if len(metric_properties) > 32:
            raise DeviceConfigurationError("Metrics may declare up to 32 fields.")
        for name, schema in metric_properties.items():
            kinds = schema["type"]
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if len(name) > 64 or {"object", "array"} & set(kinds):
                raise DeviceConfigurationError(
                    "Metric fields require bounded names and scalar types."
                )
        for example in spec.examples:
            validate_value(json_copy(example), spec.params_schema)
        if spec.action_id in self._plugins:
            raise DeviceConfigurationError("Duplicate action ID in plugin discovery.")
        self._plugins[spec.action_id] = plugin
        self._specs[spec.action_id] = spec

    def get(self, action_id: str, *, remote: bool = False) -> tuple[ActionPlugin, ActionSpec]:
        if remote and action_id not in self.allowed_actions:
            raise DeviceConfigurationError("Action is outside the remote allowlist.")
        if action_id not in self._plugins:
            raise DeviceConfigurationError("Unknown action ID. Inspect plugins list.")
        spec = self._specs[action_id]
        if remote and spec.local_only:
            raise DeviceConfigurationError("This action is restricted to local execution.")
        return self._plugins[action_id], ActionSpec(**spec.to_dict())

    def describe(self, action_id: str) -> dict:
        _, spec = self.get(action_id)
        return {
            **spec.to_dict(),
            "remote_allowed": (action_id in self.allowed_actions and not spec.local_only),
        }

    def list(self) -> list[dict]:
        return [self.describe(action_id) for action_id in sorted(self._plugins)]

    @classmethod
    def discover(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        allowed_actions: Iterable[str] = (),
        remote: bool = False,
    ) -> ActionRegistry:
        from moyu_harmony_pilot.actions.builtin import builtin_plugins

        registry = cls(allowed_actions=allowed_actions)
        for plugin in builtin_plugins():
            registry.register(plugin)
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
            for raw_path in environment.get("HARMONY_PILOT_PLUGIN_PATHS", "").split(os.pathsep):
                if raw_path:
                    registry._load_directory(Path(raw_path).expanduser().resolve(), remote=remote)
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
