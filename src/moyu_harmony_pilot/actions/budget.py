"""Shared main-thread wall-clock enforcement for actions and local interaction sessions."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from moyu_harmony_pilot.actions.models import ActionBudgetExceeded, ActionContext
from moyu_harmony_pilot.device import DeviceConfigurationError

T = TypeVar("T")


def ensure_deadline_available() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise DeviceConfigurationError("Run actions in the main thread of a dedicated process.")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise DeviceConfigurationError("Action execution requires an available POSIX real timer.")


def _deadline_handler(signum: int, frame: Any) -> None:
    raise ActionBudgetExceeded


def bounded_call(ctx: ActionContext, callback: Callable[[], T]) -> T:
    ctx.checkpoint()
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _deadline_handler)
    try:
        signal.setitimer(signal.ITIMER_REAL, ctx.remaining_timeout)
        result = callback()
        _ = ctx.remaining_timeout
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
