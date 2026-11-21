"""Offline popup selection contracts using frozen snapshots and synthetic controls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from moyu_harmony_pilot.ui import (
    SENSITIVE_SIGNAL_ENUM,
    CompactSnapshot,
    build_compact_snapshot,
    parse_layout_dict,
)
from moyu_harmony_pilot.vision import PopupRule, PopupWatcher, VisionError
from moyu_harmony_pilot.vision.watcher import PopupElement, PopupSnapshot


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


def test_frozen_snapshot_and_element_satisfy_readonly_protocols() -> None:
    snapshot = _snapshot({"id": "close_button"})
    compatible_snapshot: PopupSnapshot = snapshot
    compatible_element: PopupElement = snapshot.elements[0]
    assert PopupRule("Close", "com.example.demo").matches(compatible_element)
    assert (
        PopupWatcher([PopupRule("Close", "com.example.demo")]).choose(compatible_snapshot) == "@e1"
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


@dataclass(frozen=True)
class _LegacyElement:
    ref: str = "@e1"
    text: str = "Close"
    description: str = "Dismiss"
    clickable: bool = True
    enabled: bool = True
    bundle_name: str = "com.example.demo"


def test_legacy_elements_without_node_ids_keep_exact_text_behavior() -> None:
    element: PopupElement = _LegacyElement()
    assert PopupRule("Close", "com.example.demo").matches(element)
    assert not PopupRule("Dismiss", "com.example.demo").matches(element)
    assert not PopupRule("close", "com.example.demo").matches(element)
    assert not PopupRule(bundle_name="com.example.demo", node_id="close").matches(element)
    assert PopupRule("Dismiss", "com.example.demo").matches(_LegacyElement(text=""))


@pytest.mark.parametrize(
    "rule",
    [
        PopupRule("Close", "com.example.demo"),
        PopupRule(bundle_name="com.example.demo", text_regex=r"^(Close|Dismiss)$"),
        PopupRule(bundle_name="com.example.demo", node_id="dismiss_popup"),
        PopupRule("Close", "com.example.demo", node_id="dismiss_popup"),
        PopupRule(bundle_name="com.example.demo", text_regex=r"Close", node_id="dismiss_popup"),
    ],
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


def test_regex_search_description_fallback_and_node_id_intersection() -> None:
    snapshot = _snapshot(
        {"text": "Dismiss this popup", "id": "first"},
        {"text": "Close this popup", "id": "second"},
    )
    broad = PopupRule(bundle_name="com.example.demo", text_regex=r"this popup")
    assert PopupWatcher([broad]).choose(snapshot) is None
    narrowed = replace(broad, node_id="second")
    assert PopupWatcher([narrowed]).choose(snapshot) == "@e2"
    assert PopupWatcher([replace(narrowed, text_regex=r"^Dismiss")]).choose(snapshot) is None
    assert (
        PopupWatcher(
            [PopupRule("Dismiss this popup", "com.example.demo", node_id="second")]
        ).choose(snapshot)
        is None
    )
    fallback = _snapshot({"text": "", "description": "Dismiss popup"})
    assert (
        PopupWatcher([PopupRule(bundle_name="com.example.demo", text_regex=r"^Dismiss")]).choose(
            fallback
        )
        == "@e1"
    )
    node_only = _snapshot({"text": "", "id": "close_control"})
    assert (
        PopupWatcher([PopupRule(bundle_name="com.example.demo", node_id="close_control")]).choose(
            node_only
        )
        == "@e1"
    )


@pytest.mark.parametrize("signal", [*SENSITIVE_SIGNAL_ENUM, "future_sensitive_signal"])
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
        {},
        {"text": "Close"},
        {"text": "Close", "bundle_name": " "},
        {"text": "Close", "bundle_name": None},
        {"bundle_name": "com.example.demo"},
        {"text": "", "bundle_name": "com.example.demo"},
        {"text": 1, "bundle_name": "com.example.demo"},
        {"text_regex": "", "bundle_name": "com.example.demo"},
        {"text_regex": "[", "bundle_name": "com.example.demo"},
        {"text_regex": "a{999999999999999999999}", "bundle_name": "com.example.demo"},
        {"text_regex": "a" * 257, "bundle_name": "com.example.demo"},
        {"text_regex": 1, "bundle_name": "com.example.demo"},
        {"node_id": " ", "bundle_name": "com.example.demo"},
        {"node_id": "a" * 129, "bundle_name": "com.example.demo"},
        {"node_id": 1, "bundle_name": "com.example.demo"},
        {"text": "Close", "text_regex": "Close", "bundle_name": "com.example.demo"},
    ],
)
def test_invalid_rules_raise_structured_vision_errors(kwargs: dict[str, Any]) -> None:
    with pytest.raises(VisionError):
        PopupRule(**kwargs)


@pytest.mark.parametrize("max_actions", [0, -1, 21, True, 1.5, "3"])
def test_action_budget_validation(max_actions: Any) -> None:
    with pytest.raises(VisionError):
        PopupWatcher(max_actions=max_actions)
