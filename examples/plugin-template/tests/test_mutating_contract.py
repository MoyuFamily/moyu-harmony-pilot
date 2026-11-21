"""Exercise the installed mutating plugin through public offline contracts."""

from unittest.mock import patch

from moyu_harmony_pilot.actions import ActionLedger, ActionRegistry, ActionRunner
from moyu_harmony_pilot.device import DeviceConfigurationError
from moyu_harmony_pilot.testing import (
    FakeDeviceState,
    assert_idempotent_success,
    assert_pre_submit_blocked,
    assert_readonly,
    assert_unknown_no_replay,
    make_fake_device,
)

from tests.support import OTHER_BUTTON_ID, TOGGLE_TARGET, OfflineTestCase, toggle_layout

ACTION_ID = "example.demo_toggle"
PARAMS = {"enabled": True}


class MutationContractTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.registry = ActionRegistry.discover(
            remote=True, allowed_actions=[ACTION_ID], plugin_paths=[],
        )
        self.plugin, self.spec = self.registry.get(ACTION_ID)
        self.runner = ActionRunner(self.registry, state_dir=self.state_dir)
        self.device_state.layouts = [toggle_layout(False), toggle_layout(True)]
        self.device_state.click_targets = [TOGGLE_TARGET]
        self.addCleanup(self.transport.assert_click_targets_consumed)

    def test_inspection_and_confirmation_are_readonly(self):
        self.device_state.click_targets = []
        self.device_state.layout_index = 1
        done = assert_readonly(self.transport, lambda: self.runner.run(
            self.device, ACTION_ID, PARAMS, idempotency_key="already-enabled", remote=True,
        ))
        self.assertEqual(done.status, "already_done")
        self.assertEqual(done.metrics, {"enabled": True})
        ledger = ActionLedger(self.state_dir / "actions.sqlite3")
        key = ledger.make_key(
            self.device.profile.serial, ACTION_ID, "pending", self.spec.version, PARAMS,
        )
        ledger.reserve(key, inspection_metrics={"current_enabled": False})
        self.device_state.layout_index = 0
        unconfirmed = assert_readonly(self.transport, lambda: self.runner.reconcile(
            self.device, ACTION_ID, PARAMS, idempotency_key="pending",
        ))
        self.assertEqual(unconfirmed.status, "unknown")
        self.device_state.layout_index = 1
        confirmed = assert_readonly(self.transport, lambda: self.runner.reconcile(
            self.device, ACTION_ID, PARAMS, idempotency_key="pending",
        ))
        self.assertEqual(confirmed.status, "succeeded")
        self.assertEqual(confirmed.validate(self.spec).metrics, {"enabled": True})

    def test_installed_contract_reserves_before_submit_and_blocks_success_replay(self):
        description = self.registry.describe(ACTION_ID)
        self.assertEqual(description["side_effect"], "mutating")
        self.assertEqual(description["min_pilot_version"], "0.2.0rc1")
        self.assertEqual(description["inspection_metrics_schema"]["required"], ["current_enabled"])
        self.assertEqual(description["metrics_schema"]["required"], ["enabled"])
        for params in description["examples"]:
            self.runner.prepare(ACTION_ID, params, idempotency_key="validate", remote=True)
        for params in ({}, {"enabled": 1}, {"enabled": True, "extra": 1}):
            with self.subTest(params=params), self.assertRaises(DeviceConfigurationError):
                self.runner.run(self.device, ACTION_ID, params, idempotency_key="invalid")
        with self.assertRaises(DeviceConfigurationError):
            self.runner.run(self.device, ACTION_ID, PARAMS)
        with self.assertRaises(DeviceConfigurationError):
            ActionRegistry(pilot_version="0.1.0").register(self.plugin)
        blocked = ActionRegistry()
        blocked.register(self.plugin)
        with self.assertRaises(DeviceConfigurationError):
            ActionRunner(blocked, state_dir=self.state_dir).run(
                self.device, ACTION_ID, PARAMS, idempotency_key="not-allowed", remote=True,
            )
        self.assertEqual(self.transport.calls, [])
        outcome = assert_idempotent_success(self.runner, self.fake, ACTION_ID, PARAMS, remote=True)
        self.assertEqual(outcome.metrics, {"enabled": True})
        disabled = make_fake_device(
            self.state_dir / "disable", state=FakeDeviceState(
                layouts=[toggle_layout(True), toggle_layout(False)], click_targets=[TOGGLE_TARGET],
            ),
        )
        outcome = assert_idempotent_success(
            ActionRunner(self.registry, state_dir=self.state_dir / "disable"),
            disabled, ACTION_ID, {"enabled": False}, remote=True,
        )
        self.assertEqual(outcome.metrics, {"enabled": False})
        disabled.transport.assert_click_targets_consumed()

    def test_timeout_stays_unknown_after_restart_and_allows_readonly_reconciliation(self):
        self.device_state.timeout_after_mutation = True
        assert_unknown_no_replay(self.runner, self.fake, ACTION_ID, PARAMS, remote=True)
        self.transport.assert_click_targets_consumed()
        self.assertEqual(self.device_state.layout_index, 1)
        reconciled = assert_readonly(self.transport, lambda: self.runner.reconcile(
            self.device, ACTION_ID, PARAMS, idempotency_key="contract-unknown",
        ))
        self.assertEqual(reconciled.status, "succeeded")
        self.assertEqual(reconciled.metrics, {"enabled": True})
        replay = self.runner.run(
            self.device, ACTION_ID, PARAMS, idempotency_key="contract-unknown", remote=True,
        )
        self.assertEqual(replay.status, "already_done")
        self.assertEqual(len(self.transport.mutating_calls), 1)

    def test_wrong_target_fails_contract_even_after_runner_reports_unknown(self):
        self.device_state.click_targets = []
        directory = self.state_dir / "wrong-target"
        fake = make_fake_device(directory, state=FakeDeviceState(
            layouts=[toggle_layout(False), toggle_layout(True)], click_targets=[TOGGLE_TARGET],
        ))

        def click_other_button(ctx, inspection, params):
            snapshot = ctx.interact().observe()
            other = next(
                element for element in snapshot.elements if element.id == OTHER_BUTTON_ID
            )
            ctx.interact().click_ref(other.ref, layout_hash=snapshot.layout_hash)

        with patch.object(self.plugin, "submit", click_other_button):
            with self.assertRaisesRegex(AssertionError, "Click target checks failed"):
                assert_unknown_no_replay(
                    ActionRunner(self.registry, state_dir=directory), fake, ACTION_ID, PARAMS,
                    remote=True,
                )
        self.assertEqual(fake.state.layout_index, 0)
        self.assertEqual(fake.state.click_target_index, 0)
        records = ActionLedger(directory / "actions.sqlite3").records()
        self.assertEqual(records[0]["status"], "unknown")
        with self.assertRaisesRegex(AssertionError, "Click target checks failed"):
            fake.transport.assert_click_targets_consumed()

    def test_unconfirmed_thermal_and_sensitive_boundaries(self):
        self.device_state.layouts = [toggle_layout(False)]
        assert_unknown_no_replay(self.runner, self.fake, ACTION_ID, PARAMS, remote=True)
        self.transport.assert_click_targets_consumed()
        for scenario, expected_status in (
            ("thermal", "device_overheated"), ("sensitive", "human_required"),
        ):
            with self.subTest(scenario=scenario):
                directory = self.state_dir / scenario
                fake = make_fake_device(directory, state=FakeDeviceState(
                    layouts=[toggle_layout(sensitive=scenario == "sensitive")],
                    temperature_c=43.0 if scenario == "thermal" else 30.0,
                    click_targets=[],
                ))
                assert_pre_submit_blocked(
                    ActionRunner(self.registry, state_dir=directory), fake, ACTION_ID, PARAMS,
                    expected_status=expected_status, remote=True,
                )
                fake.transport.assert_click_targets_consumed()
