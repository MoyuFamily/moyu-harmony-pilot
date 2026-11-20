"""Spawned per-device execution slots with parent-owned leases and durable receipts."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event

from moyu_harmony_pilot.actions import ActionOutcome, ActionRunner
from moyu_harmony_pilot.actions.models import ActionBudgetExceeded
from moyu_harmony_pilot.bridge.config import validate_configuration
from moyu_harmony_pilot.bridge.lease import LeaseKeeper
from moyu_harmony_pilot.bridge.protocol import Job, timestamp, utc_now
from moyu_harmony_pilot.device import DeviceBusyError, DeviceConfigurationError, HarmonyDevice


def _run_action(config, hdc, registry, environment, job, channel, cancelled, parent_pid):
    """Each child owns its main-thread deadline timer and closes its result channel."""

    def interrupt(signum, frame):
        raise ActionBudgetExceeded

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        with redirect_stdout(sys.stderr):
            devices, actions = validate_configuration(config, registry=registry, env=environment)
            if job.action not in config.allowed_actions.get(job.device_alias, ()):
                raise DeviceConfigurationError("Action is outside the device allowlist.")
            profile = devices.get_by_alias(job.device_alias)
            if profile is None:
                raise DeviceConfigurationError("Unknown device alias.")
            device = HarmonyDevice(hdc, profile, state_dir=config.state_dir)
            outcome = ActionRunner(actions, state_dir=config.state_dir).run(
                device,
                job.action,
                job.params,
                remote=True,
                idempotency_key=job.idempotency_key,
                timeout=min(job.timeout_seconds, (job.expires_at - utc_now()).total_seconds()),
                cancelled=lambda: cancelled.is_set() or os.getppid() != parent_pid,
                on_stage=lambda stage: channel.send(("stage", stage)),
            )
    except DeviceConfigurationError:
        outcome = ActionOutcome(status="configuration_error", error_code="configuration_error")
    except DeviceBusyError:
        outcome = ActionOutcome(status="failed", error_code="device_busy")
    except BaseException:
        outcome = ActionOutcome(status="unknown", error_code="worker_error")
    try:
        channel.send(("outcome", outcome.to_dict()))
    finally:
        channel.close()


@dataclass
class ExecutionSlot:
    job: Job
    process: BaseProcess
    channel: Connection
    cancelled: Event
    lease: LeaseKeeper
    started_at: str
    started_clock: float
    deadline: float
    outcome: ActionOutcome | None = None


def execute_batch(worker, jobs) -> None:
    context = multiprocessing.get_context("spawn")
    slots = []
    try:
        for job in jobs:
            if not worker.store.reserve(job):
                continue
            started = time.monotonic()
            if worker.stop.is_set() or utc_now() >= min(job.expires_at, job.lease_expires_at):
                worker.store.finish(
                    job.job_id,
                    worker._receipt(
                        job,
                        ActionOutcome(status="failed", error_code="job_expired"),
                        timestamp(),
                        0,
                    ),
                )
                worker.counts["completed"] += 1
                continue
            reader, writer = context.Pipe(duplex=False)
            cancelled = context.Event()
            process = context.Process(
                target=_run_action,
                name="harmony-device-worker",
                args=(
                    worker.config,
                    worker.hdc,
                    worker.registry_override,
                    worker.environment,
                    job,
                    writer,
                    cancelled,
                    os.getpid(),
                ),
            )
            lease = LeaseKeeper(job, worker.config, worker.client, worker.stop, worker.heartbeat)
            slot = ExecutionSlot(
                job,
                process,
                reader,
                cancelled,
                lease,
                timestamp(),
                started,
                started
                + min(job.timeout_seconds, (job.expires_at - utc_now()).total_seconds())
                + 5,
            )
            try:
                process.start()
                worker.active_aliases.add(job.device_alias)
                lease.__enter__()
                slots.append(slot)
            except BaseException:
                if process.pid is not None:
                    process.terminate()
                    process.join(timeout=2)
                reader.close()
                worker.active_aliases.discard(job.device_alias)
                raise
            finally:
                writer.close()
        while slots:
            for slot in list(slots):
                if slot.lease.cancelled():
                    slot.cancelled.set()
                try:
                    while slot.channel.poll():
                        kind, value = slot.channel.recv()
                        if kind == "stage":
                            slot.lease.set_stage(value)
                        elif kind == "outcome":
                            slot.outcome = ActionOutcome(**value)
                except EOFError:
                    pass
                expired = time.monotonic() >= slot.deadline
                if slot.outcome is not None or expired or not slot.process.is_alive():
                    # A process can close its pipe immediately before is_alive changes.
                    if slot.outcome is None and slot.channel.poll():
                        try:
                            kind, value = slot.channel.recv()
                            if kind == "outcome":
                                slot.outcome = ActionOutcome(**value)
                        except EOFError:
                            pass
                    slots.remove(slot)
                    finish_slot(worker, slot)
            if slots:
                time.sleep(0.02)
    finally:
        cleanup_error = None
        for slot in slots:
            slot.cancelled.set()
            try:
                finish_slot(worker, slot)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and sys.exception() is None:
            raise cleanup_error


def finish_slot(worker, slot: ExecutionSlot) -> None:
    slot.process.join(timeout=0.2)
    if slot.process.is_alive():
        slot.process.terminate()
        slot.process.join(timeout=1)
    if slot.process.is_alive():
        slot.process.kill()
        slot.process.join(timeout=1)
    slot.lease.__exit__(None, None, None)
    slot.channel.close()
    slot.process.close()
    worker.active_aliases.discard(slot.job.device_alias)
    outcome = slot.outcome or ActionOutcome(status="unknown", error_code="worker_interrupted")
    worker.store.finish(
        slot.job.job_id,
        worker._receipt(slot.job, outcome, slot.started_at, time.monotonic() - slot.started_clock),
    )
    worker.counts["completed"] += 1
