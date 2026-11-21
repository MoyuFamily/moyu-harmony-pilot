"""Public, stdlib-only offline helpers for independently distributed plugins.

This optional consumer layer is imported explicitly by tests. Production drivers,
actions and adapters must never depend on it.
"""

from moyu_harmony_pilot.testing.broker import MockBroker
from moyu_harmony_pilot.testing.contracts import (
    assert_idempotent_success,
    assert_pre_submit_blocked,
    assert_readonly,
    assert_submitting,
    assert_unknown_no_replay,
)
from moyu_harmony_pilot.testing.fakes import (
    FakeDevice,
    FakeDeviceState,
    FakeHdcTransport,
    OfflineTestCase,
    make_fake_device,
    make_layout,
    offline_guard,
)

__all__ = [
    "FakeDevice",
    "FakeDeviceState",
    "FakeHdcTransport",
    "MockBroker",
    "OfflineTestCase",
    "assert_idempotent_success",
    "assert_pre_submit_blocked",
    "assert_readonly",
    "assert_submitting",
    "assert_unknown_no_replay",
    "make_fake_device",
    "make_layout",
    "offline_guard",
]
