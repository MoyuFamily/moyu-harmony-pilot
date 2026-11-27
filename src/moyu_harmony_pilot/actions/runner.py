"""Locked execution and durable inspect/submit/confirm orchestration."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.actions.budget import bounded_call as _bounded_call
from moyu_harmony_pilot.actions.budget import ensure_deadline_available
from moyu_harmony_pilot.actions.ledger import ActionLedger, LedgerKey
from moyu_harmony_pilot.actions.models import (
    ActionBudgetExceeded,
    ActionCallError,
    ActionCancelled,
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
    if isinstance(exc, ActionCallError):
        return exc.outcome
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
    if isinstance(exc, ActionCancelled):
        code = "action_cancelled"
    return ActionOutcome(status=status, summary="Action execution stopped.", error_code=code)


class ActionRunner:
    def __init__(
        self, registry: ActionRegistry, *, state_dir: str | Path = ".state",
        private_actions: Iterable[str] = (),
    ) -> None:
        self.registry = registry
        self.state_dir = Path(state_dir)
        if isinstance(private_actions, str):
            raise DeviceConfigurationError("private_actions must be a collection of action IDs.")
        self.private_actions = frozenset(private_actions)

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
        plugin, spec = self.registry.get(
            action_id, remote=remote, private_actions=self.private_actions,
        )
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
        cancelled: Callable[[], bool] | None = None,
        on_stage: Callable[[str], None] | None = None,
        allowed_actions: Iterable[str] | None = None,
    ) -> ActionOutcome:
        allowed = None if allowed_actions is None else frozenset(allowed_actions)
        if remote and allowed is not None and action_id not in allowed:
            raise DeviceConfigurationError("Action is outside the device allowlist.")
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
        ctx = ActionContext(
            device, time.monotonic() + seconds, max_steps, cancelled=cancelled, on_stage=on_stage,
            _plugin_min_version=spec.min_pilot_version,
            state_dir=self.state_dir, _action_path=(action_id,),
            _holds_device_lock=spec.lock_device,
            _allowed_actions=allowed,
        )
        with device.acquire_lock() if spec.lock_device else nullcontext():
            return self._execute(
                plugin, spec, ctx, params, idempotency_key=idempotency_key, remote=remote,
            )

    def _bind_calls(self, ctx: ActionContext, *, remote: bool) -> None:
        ctx._call_plugin = lambda action_id, params, key, timeout: self._call(
            ctx, action_id, params, idempotency_key=key, timeout=timeout, remote=remote,
        )

    def _call(
        self, parent: ActionContext, action_id: str, params: dict[str, Any], *,
        idempotency_key: str | None, timeout: float | None, remote: bool,
    ) -> ActionOutcome:
        if (
            remote and parent._allowed_actions is not None
            and action_id not in parent._allowed_actions
        ):
            raise DeviceConfigurationError("Called plugin is outside the device allowlist.")
        if action_id in parent._action_path or len(parent._action_path) >= 8:
            raise DeviceConfigurationError("Recursive or excessively nested plugin call.")
        plugin, spec, params, seconds = self.prepare(
            action_id, params, remote=remote, idempotency_key=idempotency_key, timeout=timeout,
            max_steps=parent.max_steps,
        )
        if spec.side_effect == "mutating" and parent.stage not in {"preparing", "submit"}:
            raise DeviceConfigurationError(
                "Mutating plugin calls require the parent's prepare or submit stage."
            )
        child = ActionContext(
            parent.device, min(parent.deadline, time.monotonic() + seconds),
            max_steps=parent.max_steps, cancelled=parent.cancelled, on_stage=parent.on_stage,
            reconciling=parent.reconciling, _plugin_min_version=spec.min_pilot_version,
            state_dir=self.state_dir, _parent=parent,
            _action_path=(*parent._action_path, action_id),
            _holds_device_lock=parent._holds_device_lock or spec.lock_device,
            _allowed_actions=parent._allowed_actions,
        )
        _ = parent.remaining_timeout
        lock = (
            parent.device.acquire_lock()
            if spec.lock_device and not parent._holds_device_lock else nullcontext()
        )
        try:
            with lock:
                outcome = self._execute(
                    plugin, spec, child, params, idempotency_key=idempotency_key, remote=remote,
                )
        except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
            if not (child._submitted or child._child_unknown):
                raise
            outcome = _failure(exc, submitted=True)
        if outcome.status == "unknown":
            parent._child_unknown = True
        return outcome

    def _execute(
        self, plugin: ActionPlugin, spec: ActionSpec, ctx: ActionContext,
        params: dict[str, Any], *, idempotency_key: str | None, remote: bool,
    ) -> ActionOutcome:
        self._bind_calls(ctx, remote=remote)
        ledger: ActionLedger | None = None
        key: LedgerKey | None = None
        with ExitStack() as stack:
            if spec.private_content:
                sink = stack.enter_context(open(os.devnull, "w"))
                stack.enter_context(redirect_stdout(sink))
                stack.enter_context(redirect_stderr(sink))
            try:
                if isinstance(plugin, IdempotentAction):
                    ledger = ActionLedger(self.state_dir / "actions.sqlite3")
                    key = ledger.make_key(
                        ctx.device.profile.serial,
                        spec.action_id,
                        idempotency_key,
                        spec.version,
                        params,
                    )
                    prior = ledger.lookup(key)
                    outcome = (
                        prior if prior is not None
                        else self._mutating(plugin, spec, ctx, params, ledger, key)
                    )
                else:
                    if not spec.diagnostic:
                        _bounded_call(
                            ctx,
                            lambda: ctx.device.assert_thermal_safe(timeout=ctx.remaining_timeout),
                        )
                    ctx.stage = "readonly"
                    outcome = _bounded_call(
                        ctx, lambda: plugin.execute(ctx, json_copy(params)), stage="inspect"
                    )
                    outcome = outcome.validate(spec, stage="final")
            except (DeviceBusyError, UiConcurrentCallError) as exc:
                if not (ctx._submitted or ctx._child_unknown):
                    raise
                outcome = _failure(exc, submitted=True)
            except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
                outcome = _failure(exc, submitted=ctx._submitted or ctx._child_unknown)
            finally:
                ctx.stage = None
                ctx._call_plugin = None
                ctx._reserve_submission = None
            if ctx._child_unknown:
                outcome = ActionOutcome(
                    status="unknown", error_code="submission_uncertain",
                    summary="A called plugin requires reconciliation.",
                )
                if ledger is not None and key is not None and not ctx._submitted:
                    try:
                        ledger.reserve(key, inspection_metrics=ctx.inspection_metrics)
                        ctx._submitted = True
                    except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
                        # A committed parent reservation prevents a composed request being replayed.
                        return _failure(exc, submitted=True)
            if ctx._submitted and ledger is not None and key is not None:
                if outcome.status not in {"succeeded", "already_done", "unknown"}:
                    outcome = _failure(ActionBudgetExceeded(), submitted=True)
                try:
                    ledger.finish(key, outcome)
                except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
                    # The reservation remains durable when finalization is interrupted.
                    outcome = _failure(exc, submitted=True)
            return outcome

    def _reserve_submission(
        self, ctx: ActionContext, ledger: ActionLedger, key: LedgerKey,
    ) -> None:
        if ctx._submitted:
            return
        if ctx._parent is not None and ctx._parent._reserve_submission is not None:
            ctx._parent._reserve_submission()
        _ = ctx.remaining_timeout
        try:
            ledger.reserve(key, inspection_metrics=ctx.inspection_metrics)
            ctx._submitted = True
        except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit):
            # An interruption can arrive just after SQLite committed the reservation.
            try:
                ctx._submitted = ledger.lookup(key) is not None
            except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit):
                ctx._submitted = True
            raise

    @staticmethod
    def _validate_inspection(
        spec: ActionSpec,
        inspection: Inspection,
        *,
        base_metrics: dict[str, int | float | str | bool | None] | None = None,
    ) -> ActionOutcome:
        if not isinstance(inspection, Inspection) or inspection.status not in {
            "ready",
            "already_done",
            "human_required",
            "failed",
        }:
            raise DeviceConfigurationError("Invalid plugin inspection result.")
        if not isinstance(inspection.metrics, dict):
            raise DeviceConfigurationError("Invalid action receipt fields.")
        metrics = (
            {**base_metrics, **inspection.metrics}
            if base_metrics and inspection.status == "ready"
            else inspection.metrics
        )
        summary = (
            "Read-only inspection completed."
            if inspection.summary is None
            else inspection.summary
        )
        return ActionOutcome(
            status="succeeded" if inspection.status == "ready" else inspection.status,
            summary=summary,
            metrics=metrics,
            error_code=inspection.error_code,
        ).validate(spec, stage="inspect")

    def _mutating(
        self,
        plugin: IdempotentAction,
        spec: ActionSpec,
        ctx: ActionContext,
        params: dict[str, Any],
        ledger: ActionLedger,
        key: LedgerKey,
    ) -> ActionOutcome:
        ctx._reserve_submission = lambda: self._reserve_submission(ctx, ledger, key)
        _bounded_call(ctx, lambda: ctx.device.assert_thermal_safe(timeout=ctx.remaining_timeout))
        ctx.stage = "inspect"
        inspection = _bounded_call(
            ctx, lambda: plugin.inspect(ctx, json_copy(params)), stage="inspect"
        )
        observed = self._validate_inspection(spec, inspection)
        ctx.inspection_metrics = dict(observed.metrics)
        if inspection.status in {"human_required", "failed"}:
            return observed
        if inspection.status == "ready" and type(plugin).prepare is not IdempotentAction.prepare:
            ctx.stage = "preparing"
            inspection = _bounded_call(
                ctx,
                lambda: plugin.prepare(ctx, inspection, json_copy(params)),
                stage="preparing",
            )
            observed = self._validate_inspection(
                spec, inspection, base_metrics=observed.metrics
            )
            ctx.inspection_metrics = dict(observed.metrics)
            if ctx._submitted:
                ledger.update_inspection(key, ctx.inspection_metrics)
            if inspection.status in {"human_required", "failed"}:
                return observed
        if ctx._child_unknown:
            return ActionOutcome(status="unknown", error_code="submission_uncertain")
        if inspection.status == "ready":
            # A sensitive screen appearing during inspect or prepare must stop submission.
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
        if not ctx._submitted:
            self._reserve_submission(ctx, ledger, key)
        # The durable reservation is the cancellation boundary. Finish confirmation
        # within the original deadline even if shutdown or lease loss arrives now.
        ctx.cancelled = None
        outcome = _failure(ActionBudgetExceeded(), submitted=True)
        try:
            if inspection.status == "already_done":
                outcome = observed
            else:
                ctx.stage = "submit"
                _bounded_call(
                    ctx, lambda: plugin.submit(ctx, inspection, json_copy(params)), stage="submit"
                )
                ctx.stage = "confirm"
                confirmed = _bounded_call(
                    ctx, lambda: plugin.confirm(ctx, json_copy(params)), stage="confirm"
                )
                confirmed = confirmed.validate(spec, stage="final")
                if confirmed.status in {"succeeded", "already_done"} and not ctx._child_unknown:
                    outcome = confirmed
        except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit) as exc:
            # All post-reservation errors stay uncertain regardless of the plugin exception type.
            outcome = _failure(exc, submitted=True)
        return outcome

    def reconcile(
        self, device: HarmonyDevice, action_id: str, params: dict[str, Any], *,
        idempotency_key: str,
        timeout: float | None = None, note: str | None = None,
    ) -> ActionOutcome:
        """Confirm a prior submission read-only, or explicitly record a human verification."""
        plugin, spec, params, seconds = self.prepare(
            action_id, params, idempotency_key=idempotency_key, timeout=timeout
        )
        if not isinstance(plugin, IdempotentAction):
            raise DeviceConfigurationError("Reconciliation requires an idempotent action.")
        ensure_deadline_available()
        with device.acquire_lock():
            ledger = ActionLedger(self.state_dir / "actions.sqlite3")
            key = ledger.make_key(
                device.profile.serial, action_id, idempotency_key, spec.version, params
            )
            previous = ledger.lookup(key)
            if previous is None:
                raise DeviceConfigurationError("No prior submission matches this request.")
            if previous.status != "unknown":
                return previous
            if note is not None:
                outcome = ActionOutcome(summary="Completion explicitly verified by operator.")
            else:
                record = ledger.records(
                    device_hash=key.device_hash, action_id=key.action_id, key_hash=key.key_hash
                )[0]
                ctx = ActionContext(
                    device, time.monotonic() + seconds,
                    inspection_metrics=record["inspection_metrics"], reconciling=True,
                    stage="confirm",
                    _plugin_min_version=spec.min_pilot_version,
                    state_dir=self.state_dir, _action_path=(action_id,),
                    _holds_device_lock=True,
                )
                self._bind_calls(ctx, remote=False)
                try:
                    with ExitStack() as stack:
                        if spec.private_content:
                            sink = stack.enter_context(open(os.devnull, "w"))
                            stack.enter_context(redirect_stdout(sink))
                            stack.enter_context(redirect_stderr(sink))
                        outcome = _bounded_call(
                            ctx, lambda: plugin.confirm(ctx, json_copy(params)), stage="confirm"
                        ).validate(spec, stage="final")
                except (Exception, ActionBudgetExceeded, KeyboardInterrupt, SystemExit):
                    return previous
                finally:
                    ctx.stage = None
                    ctx._call_plugin = None
                if ctx._child_unknown or outcome.status not in {"succeeded", "already_done"}:
                    return previous
            ledger.resolve(key, outcome, note=note or "Read-only plugin confirmation.")
            return outcome

    def release(
        self,
        device: HarmonyDevice,
        action_id: str,
        params: dict[str, Any],
        *,
        idempotency_key: str,
        note: str,
    ) -> None:
        """Explicitly release an uncertain reservation for retry after operator verification."""
        plugin, spec, params, _ = self.prepare(
            action_id, params, idempotency_key=idempotency_key
        )
        if not isinstance(plugin, IdempotentAction):
            raise DeviceConfigurationError("Release requires an idempotent action.")
        with device.acquire_lock():
            ledger = ActionLedger(self.state_dir / "actions.sqlite3")
            key = ledger.make_key(
                device.profile.serial, action_id, idempotency_key, spec.version, params
            )
            ledger.release(key, note=note)
