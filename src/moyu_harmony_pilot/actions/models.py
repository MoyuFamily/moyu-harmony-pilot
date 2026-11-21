"""Public plugin metadata, bounded receipts, and execution context."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from moyu_harmony_pilot.actions.validation import IDENTIFIER, json_copy, validate_value
from moyu_harmony_pilot.device import DeviceConfigurationError, HarmonyDevice

if TYPE_CHECKING:
    from moyu_harmony_pilot.actions.interaction import InteractionSession
    from moyu_harmony_pilot.ui import CompactSnapshot
    from moyu_harmony_pilot.vision import PopupWatcher


class ActionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    ALREADY_DONE = "already_done"
    HUMAN_REQUIRED = "human_required"
    DEVICE_OFFLINE = "device_offline"
    DEVICE_OVERHEATED = "device_overheated"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CONFIGURATION_ERROR = "configuration_error"


ACTION_API_VERSION = 1
CONTEXT_API_MIN_VERSION = "0.2.0.dev0"


def empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _relax_required_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Preserve closed properties and scalar bounds while relaxing final-success `required`."""
    return {key: value for key, value in schema.items() if key != "required"}


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    version: str
    description: str
    side_effect: str = "readonly"
    params_schema: dict[str, Any] = field(default_factory=empty_schema)
    metrics_schema: dict[str, Any] = field(default_factory=empty_schema)
    timeout_seconds: float = 60.0
    local_only: bool = False
    diagnostic: bool = False
    examples: tuple[dict[str, Any], ...] = ()
    api_version: int = 1
    inspection_metrics_schema: dict[str, Any] | None = None
    min_pilot_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "examples", tuple(self.examples))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action_id": self.action_id,
            "version": self.version,
            "description": self.description,
            "side_effect": self.side_effect,
            "params_schema": self.params_schema,
            "metrics_schema": self.metrics_schema,
            "inspection_metrics_schema": self.inspection_metrics_schema,
            "timeout_seconds": self.timeout_seconds,
            "local_only": self.local_only,
            "diagnostic": self.diagnostic,
            "examples": list(self.examples),
            "api_version": self.api_version,
            "min_pilot_version": self.min_pilot_version,
        }
        copied: dict[str, Any] = json_copy(payload)
        return copied


@dataclass(frozen=True)
class ActionOutcome:
    status: ActionStatus | str = ActionStatus.SUCCEEDED
    summary: str = "Action completed."
    metrics: dict[str, int | float | str | bool | None] = field(default_factory=dict)
    error_code: str | None = None
    has_local_diagnostics: bool = False

    def to_dict(self) -> dict[str, Any]:
        try:
            status = ActionStatus(self.status)
        except (ValueError, TypeError) as exc:
            raise DeviceConfigurationError("Invalid action outcome status.") from exc
        if (
            not isinstance(self.summary, str)
            or len(self.summary) > 256
            or not isinstance(self.metrics, dict)
            or len(self.metrics) > 32
            or type(self.has_local_diagnostics) is not bool
        ):
            raise DeviceConfigurationError("Invalid action receipt fields.")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_code)
        ):
            raise DeviceConfigurationError("Invalid action receipt error code.")
        for key, value in self.metrics.items():
            if (
                not isinstance(key, str)
                or not IDENTIFIER.fullmatch(key)
                or len(key) > 64
                or type(value) not in (int, float, str, bool, type(None))
                or isinstance(value, str)
                and len(value) > 128
            ):
                raise DeviceConfigurationError("Invalid action metric.")
        return json_copy(
            {
                "status": status.value,
                "summary": self.summary,
                "metrics": self.metrics,
                "error_code": self.error_code,
                "has_local_diagnostics": self.has_local_diagnostics,
            }
        )

    def validate(self, spec: ActionSpec, *, stage: str = "final") -> ActionOutcome:
        if stage not in {"inspect", "final"}:
            raise DeviceConfigurationError("Unknown action receipt stage.")
        payload = self.to_dict()
        status = payload["status"]
        if stage == "inspect":
            if status == ActionStatus.ALREADY_DONE.value:
                schema = spec.metrics_schema
            elif status == ActionStatus.SUCCEEDED.value:
                schema = (
                    spec.inspection_metrics_schema
                    if spec.inspection_metrics_schema is not None
                    else _relax_required_schema(spec.metrics_schema)
                )
            else:
                base_schema = (
                    spec.inspection_metrics_schema
                    if spec.inspection_metrics_schema is not None
                    else spec.metrics_schema
                )
                schema = _relax_required_schema(base_schema)
        elif status in {ActionStatus.SUCCEEDED.value, ActionStatus.ALREADY_DONE.value}:
            schema = spec.metrics_schema
        else:
            schema = _relax_required_schema(spec.metrics_schema)
        validate_value(payload["metrics"], schema)
        return ActionOutcome(**payload)


@dataclass(frozen=True)
class Inspection:
    """Read-only decision, with optional private state for the submit stage."""

    status: str = "ready"
    state: Any = None
    metrics: dict[str, int | float | str | bool | None] = field(default_factory=dict)
    summary: str | None = None
    error_code: str | None = None


class ActionBudgetExceeded(BaseException):
    """Interrupt execution even when plugin code catches ordinary exceptions."""


class ActionCancelled(ActionBudgetExceeded):
    """Stop at a deadline or checkpoint when the owner withdraws its execution lease."""


@dataclass
class ActionContext:
    device: HarmonyDevice
    deadline: float
    max_steps: int = 100
    steps: int = 0
    cancelled: Callable[[], bool] | None = None
    on_stage: Callable[[str], None] | None = None
    inspection_metrics: dict[str, int | float | str | bool | None] = field(default_factory=dict)
    reconciling: bool = False
    stage: str | None = None
    _plugin_min_version: str | None = field(default=None, repr=False, kw_only=True)
    _interaction: InteractionSession | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def remaining_timeout(self) -> float:
        if self.cancelled is not None and self.cancelled():
            raise ActionCancelled
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ActionBudgetExceeded
        return remaining

    def checkpoint(self) -> None:
        """Charge one plugin interaction step and check the wall-clock deadline."""
        self.steps += 1
        if self.steps > self.max_steps:
            raise ActionBudgetExceeded
        _ = self.remaining_timeout

    def interact(self) -> InteractionSession:
        """Return a context-bound UI session reusing this action's lock, budget, and stage."""
        from moyu_harmony_pilot.actions.versioning import is_version_compatible

        if self._plugin_min_version is None or not is_version_compatible(
            self._plugin_min_version, CONTEXT_API_MIN_VERSION
        ):
            raise DeviceConfigurationError(
                f"Context interaction requires min_pilot_version >= {CONTEXT_API_MIN_VERSION}."
            )
        if self._interaction is None:
            from moyu_harmony_pilot.actions.interaction import InteractionSession

            self._interaction = InteractionSession.for_context(self)
        return self._interaction

    def dismiss_popup(
        self, watcher: PopupWatcher, *, snapshot: CompactSnapshot | None = None
    ) -> dict[str, Any] | None:
        """Dismiss one matching non-sensitive popup during the submit stage."""
        return self.interact().dismiss_popup(watcher, snapshot=snapshot)


class ActionPlugin(ABC):
    spec: ActionSpec

    @abstractmethod
    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        """Execute a read-only action with bounded driver timeouts."""


class IdempotentAction(ActionPlugin):
    """Mutating plugins are invoked exclusively through the runner stage machine."""

    def execute(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        raise DeviceConfigurationError("Use ActionRunner for idempotent actions.")

    @abstractmethod
    def inspect(self, ctx: ActionContext, params: dict[str, Any]) -> Inspection:
        """Return ready, already_done, human_required, or failed without mutation."""

    @abstractmethod
    def submit(self, ctx: ActionContext, inspection: Inspection, params: dict[str, Any]) -> None:
        """Perform the mutation once after the runner commits its reservation."""

    @abstractmethod
    def confirm(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        """Read-only verification of the submitted operation."""
