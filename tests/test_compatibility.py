"""Offline parser regressions using explicitly SYNTHETIC illustrative samples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from moyu_harmony_pilot.device import (
    parse_battery_dump,
    parse_foreground_window,
    parse_power_dump,
)
from moyu_harmony_pilot.ui import (
    build_compact_snapshot,
    parse_layout_dict,
    parse_layout_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "compatibility"
def load_case(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    "case_name, layout_index",
    [
        ("synthetic_harmony_5_phone", 0),
        ("synthetic_harmony_6_tablet", 0),
    ],
    ids=["harmony-5-phone", "tablet"],
)
def test_synthetic_layout_schema_and_compact_snapshots(case_name: str, layout_index: int) -> None:
    case = load_case(case_name)
    sample = case["layouts"][layout_index]
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
