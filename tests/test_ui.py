"""Offline tests for Layer 2 (`moyu_harmony_pilot.ui`): parsing, selectors, compact & driver."""

from __future__ import annotations

import json

import pytest

from moyu_harmony_pilot.hdc import HdcClient, HdcFileTransferError
from moyu_harmony_pilot.ui import (
    Bounds,
    UiConcurrentCallError,
    UiDriver,
    UiElementNotFoundError,
    UiInvalidInputError,
    UiParseError,
    UiSelector,
    UiTimeoutError,
    build_compact_snapshot,
    diff_snapshots,
    find_all,
    find_first,
    parse_bounds,
    parse_layout_dict,
    parse_layout_json,
)
from tests.fakes import (
    FakeDeviceState,
    FakeHdcTransport,
    build_large_harmony_layout,
    load_home_layout_json,
    make_second_page_layout_json,
)


def test_parse_bounds_formats() -> None:
    assert parse_bounds("[48,120][1212,240]") == Bounds(48, 120, 1212, 240)
    assert parse_bounds("[-10, 0][100, 200]") == Bounds(-10, 0, 100, 200)
    assert parse_bounds({"left": 1, "top": 2, "right": 30, "bottom": 40}) == Bounds(1, 2, 30, 40)
    assert parse_bounds([5, 6, 50, 60]) == Bounds(5, 6, 50, 60)
    assert not parse_bounds("not-bounds").is_valid


def test_parse_harmony_61_fixture_attributes_and_errors() -> None:
    raw = load_home_layout_json()
    tree = parse_layout_json(raw)
    assert tree.screen_size == (1260, 2720)
    assert tree.node_count > 10
    assert "com.example.demo" in tree.bundle_names
    assert len(tree.layout_hash) == 16

    # Re-parsing identical JSON yields identical layout_hash
    assert parse_layout_json(raw).layout_hash == tree.layout_hash

    # Invalid/corrupt JSON raises UiParseError
    for broken in ["", "   ", "{truncated", "[]", '{"unrelated": 1}']:
        with pytest.raises(UiParseError):
            parse_layout_json(broken)


def test_selectors_text_id_key_regex_parent_child_and_index() -> None:
    tree = parse_layout_json(load_home_layout_json())

    # Key fallback -> id match
    settings = find_first(tree, UiSelector(id="btn_settings"))
    assert settings is not None
    assert settings.clickable is True
    assert settings.description == "Open settings"

    # Text regex & contains
    ready = find_first(tree, UiSelector(text_regex=r"^Status:\s+Ready$"))
    assert ready is not None
    assert ready.text == "Status: Ready"

    # Parent + child combinators
    card = find_first(
        tree,
        UiSelector(type="Row", clickable=True, child=UiSelector(id="btn_run_now")),
    )
    assert card is not None
    assert card.id == "card_daily_action"

    run_btn = find_first(
        tree,
        UiSelector(id="btn_run_now", parent=UiSelector(id="card_daily_action")),
    )
    assert run_btn is not None
    assert run_btn.text == "Run Now"

    # Hidden node excluded by default (visible=True), found when visible=None
    assert find_first(tree, UiSelector(id="hidden_button")) is None
    # Note: hidden_button has visible=true on itself while parent Column has visible=false
    list_items = find_all(tree, UiSelector(type="ListItem"))
    assert len(list_items) == 3
    assert find_first(tree, UiSelector(type="ListItem", index=1)) == list_items[1]
    assert find_first(tree, UiSelector(type="ListItem", index=99)) is None


def test_compact_snapshot_pruning_folding_and_determinism() -> None:
    tree = parse_layout_json(load_home_layout_json())
    snap1 = build_compact_snapshot(tree)
    snap2 = build_compact_snapshot(tree)

    # 100% deterministic refs and hashes
    assert snap1 == snap2
    assert [e.ref for e in snap1.elements] == [f"@e{i}" for i in range(1, len(snap1.elements) + 1)]

    by_id = {e.id: e for e in snap1.elements if e.id}
    # Invisible subtree & zero-area ghost & offscreen item are pruned
    assert "hidden_button" not in by_id
    assert "zero_area_ghost" not in by_id
    assert "item_offscreen" not in by_id

    # Clickable Row folds non-interactive child text without swallowing nested interactive Button!
    assert by_id["card_daily_action"].text == "Daily Health Check | Status: Ready"
    assert "btn_run_now" in by_id
    assert by_id["btn_run_now"].text == "Run Now"

    # Folded passive texts must NOT be duplicated as separate elements
    texts = [e.text for e in snap1.elements]
    assert texts.count("Diagnostics Logs") == 1
    assert by_id["item_0"].text == "Diagnostics Logs"

    # Scrollable List container is kept, and does NOT swallow all its ListItems' text
    assert by_id["main_list"].scrollable is True
    assert by_id["main_list"].text == ""

    # Every kept interactive element's center lies strictly within its own bounds
    for elem in snap1.elements:
        cx, cy = elem.center
        assert elem.bounds.contains_point(cx, cy)


def test_compact_snapshot_compresses_120kb_plus_layout_under_5kb() -> None:
    large_dict = build_large_harmony_layout(num_rows=180)
    raw_json = json.dumps(large_dict)
    assert len(raw_json.encode("utf-8")) > 120_000

    tree = parse_layout_dict(large_dict)
    snap = build_compact_snapshot(tree)
    compact_bytes = len(json.dumps(snap.to_dict(), ensure_ascii=False).encode("utf-8"))

    # Hard token-efficiency SLA: 120KB+ raw dump compresses to < 5 KB!
    assert compact_bytes < 5_000
    # Zero interactive controls lost (12 cards + 12 Open buttons = 24 interactive elements)
    interactive_ids = {e.id for e in snap.elements if e.interactive}
    for idx in range(12):
        assert f"card_{idx}" in interactive_ids
        assert f"action_{idx}" in interactive_ids
        assert f"hidden_noise_{idx}" not in interactive_ids

    # Truncation prioritizes interactive elements over passive text and preserves order
    limited = build_compact_snapshot(tree, max_elements=10)
    assert limited.truncated is True
    assert limited.kept_elements == 10
    assert all(e.interactive for e in limited.elements)


def test_sensitive_signals_and_layout_diff() -> None:
    sensitive_tree = parse_layout_dict(
        {
            "attributes": {
                "bounds": "[0,0][1260,2720]",
                "bundleName": "com.ohos.permissionmanager",
                "type": "root",
                "visible": "true",
            },
            "children": [
                {
                    "attributes": {
                        "bounds": "[80,600][1180,720]",
                        "bundleName": "com.example.pay",
                        "clickable": "true",
                        "hint": "请输入支付密码",
                        "type": "TextInput",
                        "visible": "true",
                    },
                    "children": [],
                },
                {
                    "attributes": {
                        "bounds": "[80,760][1180,860]",
                        "bundleName": "com.example.pay",
                        "text": "请完成图形验证码",
                        "type": "Text",
                        "visible": "true",
                    },
                    "children": [],
                },
            ],
        }
    )
    snap_sensitive = build_compact_snapshot(sensitive_tree)
    assert "system_permission_dialog" in snap_sensitive.sensitive_signals
    assert "password_field" in snap_sensitive.sensitive_signals
    assert "captcha_prompt" in snap_sensitive.sensitive_signals
    assert "payment_confirmation" in snap_sensitive.sensitive_signals

    home_snap = build_compact_snapshot(parse_layout_json(load_home_layout_json()))
    same_diff = diff_snapshots(home_snap, home_snap)
    assert same_diff.changed is False
    assert same_diff.bundle_changed is False

    page2_snap = build_compact_snapshot(parse_layout_json(make_second_page_layout_json()))
    changed_diff = diff_snapshots(home_snap, page2_snap)
    assert changed_diff.changed is True
    assert changed_diff.bundle_changed is True
    assert changed_diff.added_count > 0
    assert changed_diff.removed_count > 0


def test_ui_driver_dump_cleanup_wait_and_inputs() -> None:
    dev = FakeDeviceState(
        serial="DEV_UI_0001",
        layouts=[load_home_layout_json(), make_second_page_layout_json()],
        advance_layout_on_click=True,
    )
    fake = FakeHdcTransport([dev])
    client = HdcClient(transport=fake)
    driver = UiDriver(client, dev.serial)

    tree, snap = driver.dump_compact()
    assert tree.node_count > 10
    assert snap.kept_elements > 0
    # Zero remote /data/local/tmp residue after dump_compact
    assert fake.all_remote_tmp_files() == {}

    # Click @eN with diff triggers transition to second page
    run_elem = next(e for e in snap.elements if e.id == "btn_run_now")
    clicked, after_snap, diff = driver.click_ref_with_diff(snap, run_elem.ref)
    assert clicked.id == "btn_run_now"
    assert diff.changed is True
    assert diff.bundle_changed is True
    assert after_snap.get_by_ref("@e1") is not None
    assert fake.all_remote_tmp_files() == {}

    with pytest.raises(UiElementNotFoundError):
        driver.click_ref(after_snap, "@e999")

    # Input primitives
    driver.double_click(100, 200)
    driver.long_click(100, 200)
    driver.click_ratio(0.5, 0.5, screen_size=(1000, 2000))
    driver.swipe(500, 1800, 500, 400, velocity=800)
    driver.fling(500, 1800, 500, 400, velocity=2000, step_length=50)
    driver.drag(100, 200, 300, 400, velocity=600)
    driver.dirc_fling("up", velocity=2000, step_length=50)
    driver.input_text(200, 300, "hello world")
    driver.key_event("Home")
    driver.key_event("Back")
    driver.key_event(2)

    # Validation guards
    with pytest.raises(UiInvalidInputError):
        driver.click(-1, 10)
    with pytest.raises(UiInvalidInputError):
        driver.click_ratio(1.5, 0.5, screen_size=(1000, 2000))
    with pytest.raises(UiInvalidInputError):
        driver.click_node(run_elem, offset_x=5000)
    with pytest.raises(UiInvalidInputError):
        driver.key_event("NotAKey")

    # Even when file recv fails, remote temp layout file MUST be cleaned up!
    dev.fail_recv = True
    with pytest.raises(HdcFileTransferError):
        driver.dump_layout_raw()
    assert fake.all_remote_tmp_files() == {}

    # Concurrent uitest rejection maps cleanly to UiConcurrentCallError
    dev.fail_recv = False
    dev.fail_concurrent_uitest = True
    with pytest.raises(UiConcurrentCallError):
        driver.dump_layout_raw()
    assert fake.all_remote_tmp_files() == {}


def test_ui_driver_wait_for_and_wait_gone_instant_clock() -> None:
    dev = FakeDeviceState(
        serial="DEV_WAIT_0001",
        layouts=[load_home_layout_json(), make_second_page_layout_json()],
        advance_layout_on_dump=True,
    )
    fake = FakeHdcTransport([dev])
    driver = UiDriver(HdcClient(transport=fake), dev.serial)

    now = [0.0]

    def fake_clock() -> float:
        return now[0]

    def fake_sleep(dt: float) -> None:
        now[0] += dt

    # 1st dump sees Home, 2nd dump sees Settings Updated!
    node = driver.wait_for(
        UiSelector(text="Settings Updated"),
        timeout=5.0,
        interval=0.5,
        clock=fake_clock,
        sleeper=fake_sleep,
    )
    assert node.text == "Settings Updated"
    assert fake.all_remote_tmp_files() == {}

    # wait_gone succeeds immediately since Home's "Device Dashboard" is already gone
    assert (
        driver.wait_gone(
            UiSelector(text="Device Dashboard"),
            timeout=2.0,
            interval=0.5,
            clock=fake_clock,
            sleeper=fake_sleep,
        )
        is True
    )

    # wait_for timeout raises UiTimeoutError cleanly
    with pytest.raises(UiTimeoutError):
        driver.wait_for(
            UiSelector(text="Never Appears"),
            timeout=1.0,
            interval=0.4,
            clock=fake_clock,
            sleeper=fake_sleep,
        )
