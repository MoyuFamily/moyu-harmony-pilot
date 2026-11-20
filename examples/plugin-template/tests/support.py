"""Synthetic device and isolated state shared by the offline contract examples."""

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moyu_harmony_pilot.device import DeviceLock, DeviceProfile


class FakeDevice:
    def __init__(self, state_dir):
        self.profile = DeviceProfile("test-phone", "SYNTHETIC_PLUGIN_DEVICE")
        self.lock = DeviceLock(
            self.profile.serial, alias=self.profile.alias, lock_dir=state_dir / "locks",
        )
        self.calls = []
        self.battery_percent = 72
        self.value = False

    @contextmanager
    def acquire_lock(self):
        with self.lock:
            self.calls.append("lock")
            try:
                yield
            finally:
                self.calls.append("unlock")

    def assert_thermal_safe(self, *, timeout):
        assert timeout > 0
        self.calls.append("thermal")

    def get_health(self, *, timeout):
        assert timeout > 0
        self.calls.append("health")
        return SimpleNamespace(battery=SimpleNamespace(battery_percent=self.battery_percent))

    def dump_ui_compact(self, *, timeout):
        assert timeout > 0
        self.calls.append("inspect_ui")
        return None, SimpleNamespace(sensitive_signals=())

    def set_value(self, value):
        self.calls.append("set_value")
        self.value = value


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pilot-plugin-contract-")
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.device = FakeDevice(self.state_dir)
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
            "socket.create_connection", "subprocess.Popen", "os.system",
        ):
            guard = patch(target, side_effect=AssertionError("Use offline device and HTTP fakes"))
            guard.start()
            self.addCleanup(guard.stop)
