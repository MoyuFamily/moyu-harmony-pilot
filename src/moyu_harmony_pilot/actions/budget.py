"""Shared main-thread wall-clock enforcement for actions and local interaction sessions."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

from moyu_harmony_pilot.actions.models import ActionBudgetExceeded, ActionContext
from moyu_harmony_pilot.device import DeviceConfigurationError

T = TypeVar("T")
_active_context: ContextVar[ActionContext | None] = ContextVar("action_context", default=None)


def ensure_deadline_available() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise DeviceConfigurationError("Run actions in the main thread of a dedicated process.")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise DeviceConfigurationError("Action execution requires an available POSIX real timer.")


def bounded_call(ctx: ActionContext, callback: Callable[[], T], *, stage: str | None = None) -> T:
    if stage is not None and ctx.on_stage is not None:
        ctx.on_stage(stage)
    ctx.checkpoint()
    parent = _active_context.get()
    token = _active_context.set(ctx)
    if parent is not None:
        try:
            result = callback()
            _ = ctx.remaining_timeout
            return result
        finally:
            _active_context.reset(token)
            if signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0):
                interval = min(0.1, parent.remaining_timeout)
                signal.setitimer(signal.ITIMER_REAL, interval, interval)

    def check_deadline(signum: int, frame: Any) -> None:
        try:
            active = _active_context.get()
            _ = (active if active is not None else ctx).remaining_timeout
        except ActionBudgetExceeded:
            # Allow bounded driver finally blocks to clean up after the first interruption.
            signal.setitimer(signal.ITIMER_REAL, 0)
            raise

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, check_deadline)
    try:
        interval = min(0.1, ctx.remaining_timeout)
        signal.setitimer(signal.ITIMER_REAL, interval, interval)
        result = callback()
        _ = ctx.remaining_timeout
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        _active_context.reset(token)
