"""Adapt these synthetic scenarios when implementing an IdempotentAction."""

import sqlite3
import time

from moyu_harmony_pilot.actions import (
    ActionContext,
    ActionOutcome,
    ActionRegistry,
    ActionRunner,
    ActionSpec,
    IdempotentAction,
    Inspection,
)

from tests.support import OfflineTestCase


class MemoryToggle(IdempotentAction):
    spec = ActionSpec(
        "example.test_toggle", "1.0", "Synthetic in-memory mutation for contract tests.",
        api_version=1, side_effect="mutating",
    )

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.reservation = None
        self.interrupt_submit = False
        self.confirmed = True

    def inspect(self, ctx, params):
        ctx.checkpoint()
        return Inspection(status="already_done" if ctx.device.value else "ready")

    def submit(self, ctx, inspection, params):
        ctx.checkpoint()
        with sqlite3.connect(self.state_dir / "actions.sqlite3") as db:
            self.reservation = db.execute("SELECT status FROM actions").fetchone()[0]
        ctx.device.set_value(True)
        if self.interrupt_submit:
            raise TimeoutError

    def confirm(self, ctx, params):
        ctx.checkpoint()
        return ActionOutcome(
            status="succeeded" if self.confirmed and ctx.device.value else "failed"
        )


class MutationContractTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.plugin = MemoryToggle(self.state_dir)
        registry = ActionRegistry(allowed_actions=[self.plugin.spec.action_id])
        registry.register(self.plugin)
        self.runner = ActionRunner(registry, state_dir=self.state_dir)

    def run_action(self):
        return self.runner.run(
            self.device, self.plugin.spec.action_id, {},
            idempotency_key="synthetic-contract-key", remote=True,
        )

    def test_inspection_is_readonly(self):
        context = ActionContext(self.device, time.monotonic() + 5)
        self.assertEqual(self.plugin.inspect(context, {}).status, "ready")
        self.assertFalse(self.device.value)
        self.assertEqual(self.device.calls, [])

    def test_reservation_precedes_submit_and_success_blocks_replay(self):
        self.assertEqual(self.run_action().status, "succeeded")
        self.assertEqual(self.plugin.reservation, "submitting")
        self.assertEqual(self.run_action().status, "already_done")
        self.assertEqual(self.device.calls.count("set_value"), 1)

    def test_timeout_stays_unknown_after_runner_restart(self):
        self.plugin.interrupt_submit = True
        self.assertEqual(self.run_action().status, "unknown")
        self.assertEqual(self.plugin.reservation, "submitting")
        self.runner = ActionRunner(self.runner.registry, state_dir=self.state_dir)
        self.assertEqual(self.run_action().status, "unknown")
        self.assertEqual(self.device.calls.count("set_value"), 1)

    def test_unconfirmed_result_blocks_replay(self):
        self.plugin.confirmed = False
        self.assertEqual(self.run_action().status, "unknown")
        self.assertEqual(self.run_action().status, "unknown")
        self.assertEqual(self.device.calls.count("set_value"), 1)
