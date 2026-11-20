"""Validate the installed entry point through Pilot's normal registry and runner."""

from moyu_harmony_pilot.actions import ACTION_API_VERSION, ActionRegistry, ActionRunner
from moyu_harmony_pilot.device import DeviceConfigurationError

from tests.support import OfflineTestCase

ACTION_ID = "example.battery_check"


class BatteryContractTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.registry = ActionRegistry.discover(
            remote=True, allowed_actions=[ACTION_ID], plugin_paths=[],
        )
        self.runner = ActionRunner(self.registry, state_dir=self.state_dir)

    def test_installed_entry_point_declares_contract(self):
        description = self.registry.describe(ACTION_ID)
        self.assertEqual(description["api_version"], ACTION_API_VERSION)
        self.assertEqual(description["api_version"], 1)
        self.assertEqual(description["side_effect"], "readonly")
        self.assertTrue(description["remote_allowed"])
        for params in description["examples"]:
            self.runner.prepare(ACTION_ID, params, remote=True)
        self.assertEqual(self.device.calls, [])

    def test_readonly_execution_and_bounded_receipts(self):
        for battery, ready in ((72, True), (10, False), (None, False)):
            with self.subTest(battery=battery):
                self.device.battery_percent = battery
                self.device.calls.clear()
                outcome = self.runner.run(
                    self.device, ACTION_ID, {"minimum_battery": 20}, remote=True,
                )
                self.assertEqual(outcome.status, "succeeded" if ready else "failed")
                self.assertEqual(outcome.metrics, {"battery_percent": battery, "ready": ready})
                self.assertEqual(self.device.calls, ["lock", "thermal", "health", "unlock"])
                self.assertFalse((self.state_dir / "actions.sqlite3").exists())
                self.assertFalse(self.device.value)

    def test_invalid_params_fail_before_device_access(self):
        for params in ({}, {"minimum_battery": True}, {"minimum_battery": 101}, {"extra": 1}):
            with self.subTest(params=params), self.assertRaises(DeviceConfigurationError):
                self.runner.run(self.device, ACTION_ID, params, remote=True)
        self.assertEqual(self.device.calls, [])

    def test_remote_execution_requires_allowlist(self):
        registry = ActionRegistry.discover(remote=True, plugin_paths=[])
        with self.assertRaises(DeviceConfigurationError):
            ActionRunner(registry, state_dir=self.state_dir).run(
                self.device, ACTION_ID, {"minimum_battery": 20}, remote=True,
            )
        self.assertEqual(self.device.calls, [])
