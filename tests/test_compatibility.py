"""Offline parser regressions using explicitly SYNTHETIC illustrative samples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from moyu_harmony_pilot.device import (
    ScreenState,
    parse_battery_dump,
    parse_foreground_window,
    parse_power_dump,
)
from moyu_harmony_pilot.ui import (
    UiSelector,
    build_compact_snapshot,
    diff_snapshots,
    find_first,
    parse_layout_dict,
    parse_layout_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "compatibility"
CASE_NAMES = (
    "synthetic_harmony_5_phone",
    "synthetic_harmony_6_phone",
    "synthetic_harmony_6_tablet",
    "synthetic_harmony_6_foldable",
)


def load_case(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(params=CASE_NAMES)
def case(request: pytest.FixtureRequest) -> dict[str, Any]:
    return load_case(request.param)


def test_compatibility_sample_provenance(case: dict[str, Any]) -> None:
    provenance = case["provenance"]
    assert provenance["kind"] == "SYNTHETIC"
    assert provenance["device_capture"] is False
    assert provenance["target_family"] in {"HarmonyOS NEXT 5.x", "HarmonyOS NEXT 6.x"}
    assert provenance["form_factor"] in {"phone", "tablet", "foldable"}
    for service in ("battery", "power", "window"):
        assert case[service]["dump"].startswith("SYNTHETIC illustrative ")


def test_synthetic_battery_units_and_optional_fields(case: dict[str, Any]) -> None:
    assert parse_battery_dump(case["battery"]["dump"]).to_dict() == case["battery"]["expected"]


def test_synthetic_power_state_formats(case: dict[str, Any]) -> None:
    assert parse_power_dump(case["power"]["dump"]).to_dict() == case["power"]["expected"]


def test_synthetic_window_focus_column_and_key_value_formats(case: dict[str, Any]) -> None:
    # Competing DisplayId/Pid values deliberately equal the focused WinId.
    assert parse_foreground_window(case["window"]["dump"]).to_dict() == case["window"]["expected"]


def test_synthetic_layout_schema_and_compact_snapshots(case: dict[str, Any]) -> None:
    for sample in case["layouts"]:
        raw = sample["dump"]
        expected = sample["expected"]
        tree = parse_layout_dict(raw)
        reordered = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        assert parse_layout_json(reordered) == tree
        assert tree.screen_size == tuple(expected["screen_size"])
        assert tree.node_count == expected["node_count"]
        assert tree.root.window_id == expected["root_window_id"]
        assert tree.bundle_names == (case["window"]["expected"]["bundle_name"],)

        snapshot = build_compact_snapshot(tree)
        assert snapshot == build_compact_snapshot(parse_layout_json(reordered))
        assert [element.id for element in snapshot.elements] == expected["compact_ids"]
        assert [element.ref for element in snapshot.elements] == [
            f"@e{index}" for index in range(1, len(snapshot.elements) + 1)
        ]
        assert snapshot.truncated is False
        for element in snapshot.elements:
            assert element.bounds.contains_point(*element.center)
            assert all(0 <= coordinate <= 1 for coordinate in element.center_norm)
        assert len(json.dumps(snapshot.to_dict(), ensure_ascii=False).encode()) < 5000


def test_synthetic_layout_attribute_fallbacks() -> None:
    phone5 = parse_layout_dict(load_case("synthetic_harmony_5_phone")["layouts"][0]["dump"])
    button5 = find_first(phone5, UiSelector(id="primary_5"))
    assert button5 is not None
    assert button5.key == button5.id == "primary_5"
    assert button5.description == "合成按钮说明"
    assert button5.enabled and button5.clickable
    assert find_first(phone5, UiSelector(id="hidden_5")) is None

    phone6 = parse_layout_dict(load_case("synthetic_harmony_6_phone")["layouts"][0]["dump"])
    button6 = find_first(phone6, UiSelector(id="primary_6"))
    assert button6 is not None
    assert button6.key == "unused_fallback"
    assert button6.description == "显式说明"
    assert button6.focused
    toggle = find_first(phone6, UiSelector(id="toggle_6"))
    assert toggle is not None and toggle.checked and toggle.selected and toggle.checkable

    tablet = parse_layout_dict(load_case("synthetic_harmony_6_tablet")["layouts"][0]["dump"])
    left = find_first(tablet, UiSelector(id="left_tablet"))
    assert left is not None and left.description == "内容字段后备说明"


def test_synthetic_foldable_uses_independent_layouts_and_first_power_state() -> None:
    case = load_case("synthetic_harmony_6_foldable")
    assert [sample["display_id"] for sample in case["layouts"]] == [0, 7]
    cover, inner = [
        build_compact_snapshot(parse_layout_dict(sample["dump"]))
        for sample in case["layouts"]
    ]
    assert cover.screen_size == (720, 1680)
    assert inner.screen_size == (2200, 1800)
    assert diff_snapshots(cover, inner).changed
    assert [element.ref for element in cover.elements] == ["@e1", "@e2"]
    assert [element.ref for element in inner.elements] == ["@e1", "@e2"]
    # PowerTelemetry is a single aggregate observation using the first matching line.
    assert parse_power_dump(case["power"]["dump"]).screen_state == ScreenState.OFF
    reversed_lines = "SYNTHETIC\nDisplay[7] State: ON\nDisplay[0] State: OFF\n"
    assert parse_power_dump(reversed_lines).screen_state == ScreenState.ON


def test_unknown_synthetic_telemetry_preserves_unknown_values() -> None:
    battery = parse_battery_dump(
        "SYNTHETIC\ncapacity: 140\nchargingStatus: unavailable\n"
        "temperature: unavailable\nvoltage: unavailable\n"
    )
    assert all(value is None for value in battery.to_dict().values())
    assert parse_power_dump("SYNTHETIC\nPower State: UNRECOGNIZED\n").to_dict() == {
        "screen_state": "unknown",
        "raw_state": "UNRECOGNIZED",
        "screen_off_timeout_ms": None,
    }
    window = parse_foreground_window(
        "SYNTHETIC\nWindowName DisplayId Pid WinId\n"
        "com.example.synthetic.inactive 99 99 12\nFocus window: 99\n"
    )
    assert window.window_id == 99
    assert window.bundle_name is None
    assert window.window_name is None
