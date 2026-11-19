"""Locked execution and durable inspect/submit/confirm orchestration."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.actions.budget import bounded_call as _bounded_call
from moyu_harmony_pilot.actions.budget import ensure_deadline_available
from moyu_harmony_pilot.actions.ledger import ActionLedger, LedgerKey
from moyu_harmony_pilot.actions.models import (
    ActionBudgetExceeded,
    ActionContext,
    ActionOutcome,
    ActionPlugin,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.actions.registry import ActionRegistry
from moyu_harmony_pilot.actions.validation import json_copy, validate_value
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceOverheatedError,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import DeviceOfflineError, DeviceUnauthorizedError
from moyu_harmony_pilot.ui import UiConcurrentCallError


def _failure(exc: BaseException, *, submitted: bool = False) -> ActionOutcome:
    if submitted:
        return ActionOutcome(
            status="unknown",
            summary="Submission could not be confirmed.",
            error_code="submission_uncertain",
        )
    if isinstance(exc, HumanRequiredError):
        status = "human_required"
    elif isinstance(exc, DeviceOverheatedError):
        status = "device_overheated"
    elif isinstance(exc, DeviceOfflineError | DeviceUnauthorizedError):
        status = "device_offline"
    elif isinstance(exc, DeviceConfigurationError):
        status = "configuration_error"
    else:
        status = "failed"
    code = "action_budget_exceeded" if isinstance(exc, ActionBudgetExceeded) else status
    return ActionOutcome(
        status=status, summary="Action execution stopped.", error_code=code
    )


class ActionRunner:
    def __init__(self, registry: ActionRegistry, *, state_dir: str | Path = ".state") -> None:
        self.registry = registry
        self.state_dir = Path(state_dir)

    def prepare(
        self,
        action_id: str,
        params: dict[str, Any],
        *,
        remote: bool = False,
        idempotency_key: str | None = None,
        timeout: float | None = None,
        max_steps: int = 100,
    ) -> tuple[ActionPlugin, ActionSpec, dict[str, Any], float]:
        plugin, spec = self.registry.get(action_id, remote=remote)
        clean_params = json_copy(params)
        validate_value(clean_params, spec.params_schema)
        if spec.side_effect == "mutating":
            ActionLedger.make_key(
                "validation", action_id, idempotency_key, spec.version, clean_params
            )
        seconds = spec.timeout_seconds if timeout is None else timeout
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise DeviceConfigurationError("Action timeout must be a finite positive number.")
        if type(max_steps) is not int or not 1 <= max_steps <= 10000:
            raise DeviceConfigurationError("max_steps must be an integer between 1 and 10000.")
        return plugin, spec, clean_params, min(seconds, spec.timeout_seconds)

    def run(
        self,
        device: HarmonyDevice,
        action_id: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
        max_steps: int = 100,
        remote: bool = False,
        dry_run: bool = False,
    ) -> ActionOutcome:
        plugin, spec, params, seconds = self.prepare(
            action_id,
            params,
            remote=remote,
            idempotency_key=idempotency_key,
            timeout=timeout,
            max_steps=max_steps,
        )
        if dry_run:
            return ActionOutcome(summary="Action parameters and device selection validated.")
        ensure_deadline_available()
        ctx = ActionContext(device, time.monotonic() + seconds, max_steps)
        with device.acquire_lock():
            try:
                if isinstance(plugin, IdempotentAction):
                    ledger = ActionLedger(self.state_dir / "actions.sqlite3")
                    key = ledger.make_key(
                        device.profile.serial,
                        action_id,
                        idempotency_key,
                        spec.version,
                        params,
                    )
                    prior = ledger.lookup(key)
                    if prior is not None:
                        return prior
                    return self._mutating(plugin, spec, ctx, params, ledger, key)
                if not spec.diagnostic:
                    _bounded_call(
                        ctx,
                        lambda: device.assert_thermal_safe(
                            timeout=ctx.remaining_timeout,
                        ),
                    )
                outcome = _bounded_call(ctx, lambda: plugin.execute(ctx, json_copy(params)))
                return outcome.validate(spec)
            except (DeviceBusyError, UiConcurrentCallError):
                raise
            except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
                return _failure(exc)

    def _mutating(
        self,
        plugin: IdempotentAction,
        spec: ActionSpec,
        ctx: ActionContext,
        params: dict[str, Any],
        ledger: ActionLedger,
        key: LedgerKey,
    ) -> ActionOutcome:
        _bounded_call(ctx, lambda: ctx.device.assert_thermal_safe(timeout=ctx.remaining_timeout))
        inspection = _bounded_call(ctx, lambda: plugin.inspect(ctx, json_copy(params)))
        if not isinstance(inspection, Inspection) or inspection.status not in {
            "ready",
            "already_done",
            "human_required",
            "failed",
        }:
            raise DeviceConfigurationError("Invalid plugin inspection result.")
        observed = ActionOutcome(
            status="succeeded" if inspection.status == "ready" else inspection.status,
            summary="Read-only inspection completed.",
            metrics=inspection.metrics,
        ).validate(spec)
        if inspection.status in {"human_required", "failed"}:
            return observed
        if inspection.status == "ready":
            # A sensitive screen appearing during inspect must stop the final submission.
            _, snapshot = _bounded_call(
                ctx,
                lambda: ctx.device.dump_ui_compact(
                    timeout=ctx.remaining_timeout,
                ),
            )
            if snapshot.sensitive_signals:
                raise HumanRequiredError("Sensitive device screen requires human intervention.")
            _bounded_call(
                ctx, lambda: ctx.device.assert_thermal_safe(timeout=ctx.remaining_timeout)
            )
        _ = ctx.remaining_timeout
        try:
            ledger.reserve(key)
        except (Exception, KeyboardInterrupt, SystemExit):
            # An interruption can land immediately after SQLite commits its reservation.
            try:
                prior = ledger.lookup(key)
            except Exception:
                return _failure(ActionBudgetExceeded(), submitted=True)
            if prior is not None:
                return prior
            raise
        outcome = _failure(ActionBudgetExceeded(), submitted=True)
        try:
            if inspection.status == "already_done":
                outcome = observed
            else:
                _bounded_call(ctx, lambda: plugin.submit(ctx, inspection, json_copy(params)))
                confirmed = _bounded_call(ctx, lambda: plugin.confirm(ctx, json_copy(params)))
                confirmed = confirmed.validate(spec)
                if confirmed.status in {"succeeded", "already_done"}:
                    outcome = confirmed
        except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
            # All post-reservation errors stay uncertain regardless of the plugin exception type.
            outcome = _failure(exc, submitted=True)
        finally:
            try:
                ledger.finish(key, outcome)
            except Exception:
                # The committed submitting row still blocks replay after a storage failure.
                outcome = _failure(ActionBudgetExceeded(), submitted=True)
        return outcome
