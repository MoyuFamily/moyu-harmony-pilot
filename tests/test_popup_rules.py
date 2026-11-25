"""Offline popup selection contracts using frozen snapshots and synthetic controls."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from moyu_harmony_pilot.ui import (
    CompactSnapshot,
    build_compact_snapshot,
    parse_layout_dict,
)
from moyu_harmony_pilot.vision import PopupRule, PopupWatcher, VisionError


def _snapshot(*attributes: dict[str, Any]) -> CompactSnapshot:
    return build_compact_snapshot(
        parse_layout_dict(
            {
                "attributes": {"type": "root", "bounds": "[0,0][400,800]"},
                "children": [
                    {
                        "attributes": {
                            "type": "Button",
                            "bounds": "[10,10][200,60]",
                            "clickable": True,
                            "bundleName": "com.example.demo",
                            "text": "Close",
                            **attrs,
                        }
                    }
                    for attrs in attributes
                ],
            }
        )
    )


def test_popup_bundle_inherits_only_its_own_window_ancestry() -> None:
    def button(identifier: str, **attrs: Any) -> dict[str, Any]:
        return {
            "attributes": {
                "type": "Button",
                "id": identifier,
                "text": "Close",
                "bounds": "[10,10][200,60]",
                "clickable": True,
                **attrs,
            }
        }

    tree = parse_layout_dict(
        {
            "attributes": {"type": "root", "bounds": "[0,0][400,800]"},
            "children": [
                {
                    "attributes": {"bundleName": "com.example.window", "hostWindowId": "1"},
                    "children": [
                        button("inherited"),
                        button("same", hostWindowId="1"),
                        button("other", hostWindowId="2"),
                        button("embedded", bundleName="com.example.embedded"),
                    ],
                },
                button("unresolved"),
            ],
        }
    )
    snapshot = build_compact_snapshot(tree)
    bundles = {element.id: element.bundle_name for element in snapshot.elements}
    assert bundles == {
        "inherited": "com.example.window",
        "same": "com.example.window",
        "other": "",
        "embedded": "com.example.embedded",
        "unresolved": "",
    }
    original = next(node for node in tree.iter_nodes() if node.id == "inherited")
    assert original.bundle_name == ""
    assert snapshot.layout_hash == tree.layout_hash
    inherited_rule = PopupRule(bundle_name="com.example.window", node_id="inherited")
    other_window_rule = PopupRule(bundle_name="com.example.window", node_id="other")
    assert PopupWatcher([inherited_rule]).choose(snapshot) == "@e1"
    assert PopupWatcher([other_window_rule]).choose(snapshot) is None


@pytest.mark.parametrize(
    "rule",
    [
        PopupRule(bundle_name="com.example.demo", text_regex=r"Close", node_id="dismiss_popup"),
    ],
    ids=['node-and-text-match'],
)
def test_all_rule_forms_require_unique_enabled_clickable_same_bundle_nodes(rule: PopupRule) -> None:
    good = _snapshot({"id": "dismiss_popup"})
    assert PopupWatcher([rule]).choose(good) == "@e1"
    assert (
        PopupWatcher([rule]).choose(_snapshot({"id": "dismiss_popup"}, {"id": "dismiss_popup"}))
        is None
    )
    for change in (
        {"bundleName": "com.example.other"},
        {"bundleName": "com.example.demo.extra"},
        {"bundleName": ""},
        {"enabled": False},
        {"clickable": False},
        {"visible": False},
    ):
        assert PopupWatcher([rule]).choose(_snapshot({"id": "dismiss_popup", **change})) is None


@pytest.mark.parametrize(
    "signal",
    [
        "future_sensitive_signal",
    ],
    ids=['unknown-sensitive-signal'],
)
def test_sensitive_gate_precedes_rules_and_does_not_consume_budget(signal: str) -> None:
    snapshot = _snapshot({"id": "dismiss"})
    watcher = PopupWatcher(
        [PopupRule(bundle_name="com.example.demo", node_id="dismiss")], max_actions=1
    )
    assert watcher.choose(replace(snapshot, sensitive_signals=(signal,))) is None
    assert watcher.choose(snapshot) == "@e1"
    assert watcher.choose(snapshot) is None
    assert watcher.choose(replace(snapshot, layout_hash="other-layout")) is None


def test_ambiguity_does_not_consume_budget_and_rules_keep_declared_priority() -> None:
    watcher = PopupWatcher([PopupRule("Close", "com.example.demo")], max_actions=1)
    assert watcher.choose(_snapshot({}, {})) is None
    assert watcher.choose(_snapshot({"text": "Other"})) is None
    assert watcher.choose(_snapshot({})) == "@e1"
    ordered = PopupWatcher(
        [
            PopupRule("Dismiss", "com.example.demo"),
            PopupRule("Close", "com.example.demo"),
        ]
    )
    assert ordered.choose(_snapshot({}, {"text": "Dismiss"})) == "@e2"
    assert PopupWatcher().choose(_snapshot({})) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "Close"},
    ],
    ids=['missing-bundle-scope'],
)
def test_invalid_rules_raise_structured_vision_errors(kwargs: dict[str, Any]) -> None:
    with pytest.raises(VisionError):
        PopupRule(**kwargs)
