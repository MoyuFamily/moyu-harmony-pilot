"""Guarded local exploration, differential feedback, and popup waiting."""

import json
from dataclasses import replace

import pytest

from moyu_harmony_pilot.actions import InteractionSession
from moyu_harmony_pilot.device import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
    DeviceOverheatedError,
    DeviceProfile,
    HarmonyDevice,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.ui import UiSelector
from moyu_harmony_pilot.vision import PopupRule, PopupWatcher
from tests.fakes import (
    BATTERY_61_OVERHEATED,
    FakeDeviceState,
    FakeHdcTransport,
    make_second_page_layout_json,
)


@pytest.fixture
def interactive(tmp_path):
    state = FakeDeviceState("SYNTHETIC_LAB")
    transport = FakeHdcTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport), DeviceProfile("lab", state.serial), state_dir=tmp_path
    )
    return InteractionSession(device), state, transport


def test_click_diff_and_stale_reference_protection(interactive):
    session, state, transport = interactive
    before = session.observe()
    ref = next(element.ref for element in before.elements if element.clickable)
    with pytest.raises(DeviceConfigurationError):
        session.click_ref(ref, layout_hash="stale")
    state.layouts.append(make_second_page_layout_json())
    state.advance_layout_on_click = True
    result = session.click_ref(ref, layout_hash=before.layout_hash)
    assert result["changed"]
    assert result["difference"]["before_hash"] == before.layout_hash
    assert "after_focus" in result["difference"]
    assert result["foreground_bundle"] == "com.example.demo0"
    assert not transport.all_remote_tmp_files()


def test_budget_thermal_lock_and_sensitive_screen_guards(interactive):
    session, state, transport = interactive
    snapshot = session.observe()
    state.battery_dump = BATTERY_61_OVERHEATED
    with pytest.raises(DeviceOverheatedError):
        session.back(layout_hash=snapshot.layout_hash)
    with session.device.acquire_lock():
        with pytest.raises(DeviceBusyError):
            session.observe()
    layout = json.loads(state.layouts[0])
    layout["attributes"]["text"] = "captcha"
    state.layouts = [json.dumps(layout)]
    snapshot = session.observe()
    with pytest.raises(HumanRequiredError):
        session.back(layout_hash=snapshot.layout_hash)
    tiny = InteractionSession(session.device, max_steps=1)
    tiny.observe()
    with pytest.raises(DeviceError, match="budget"):
        tiny.observe()
    assert not any("uiInput" in " ".join(args) for _, args, _ in transport.calls)


def test_directional_swipe_back_and_popup_watcher(interactive):
    session, state, _ = interactive
    snapshot = session.observe()
    assert not session.swipe("up", layout_hash=snapshot.layout_hash)["changed"]
    assert not session.back(layout_hash=snapshot.layout_hash)["changed"]
    with pytest.raises(DeviceConfigurationError):
        session.swipe("arbitrary", layout_hash=snapshot.layout_hash)
    element = next(element for element in snapshot.elements if element.clickable and element.text)
    watcher = PopupWatcher([PopupRule(element.text, element.bundle_name)])
    assert PopupWatcher().choose(snapshot) is None
    assert watcher.choose(replace(snapshot, sensitive_signals=("password_field",))) is None
    assert watcher.choose(snapshot) == element.ref
    assert watcher.choose(snapshot) is None
    state.layouts.append(make_second_page_layout_json())
    state.advance_layout_on_click = True
    node = session.wait_for(
        UiSelector(id="btn_done"),
        watcher=PopupWatcher([PopupRule(element.text, element.bundle_name)]),
        interval=0,
    )
    assert node.id == "btn_done"


def test_post_input_failure_is_uncertain(interactive):
    session, state, _ = interactive
    before = session.observe()
    state.timeout_on_cmd_prefix = "uitest uiInput"
    with pytest.raises(DeviceError) as error:
        session.back(layout_hash=before.layout_hash)
    assert error.value.code == "unknown"
