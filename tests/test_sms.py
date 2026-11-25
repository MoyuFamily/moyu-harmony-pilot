"""Offline SMS observation, wait, privacy and idempotent navigation contracts."""

import json
from pathlib import Path
from typing import Any

import pytest

from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner
from moyu_harmony_pilot.actions.ledger import ActionLedger
from moyu_harmony_pilot.actions.sms import (
    SMS_BUNDLE,
    SMS_RESULT_SCHEMA,
    SmsOpen,
    parse_sms_tree,
    read_sms,
)
from moyu_harmony_pilot.actions.validation import check_schema, validate_value
from moyu_harmony_pilot.cli import build_cli_schema
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    HumanRequiredError,
)
from moyu_harmony_pilot.hdc import CommandResult
from moyu_harmony_pilot.testing import (
    FakeClickTarget,
    FakeDeviceState,
    make_fake_device,
    offline_guard,
)
from moyu_harmony_pilot.ui import parse_layout_dict


def node(
    node_id: str, text: str = "", *, kind: str = "Text",
    bounds: tuple[int, int, int, int] = (20, 200, 900, 260),
    children: tuple[dict[str, Any], ...] = (), **attributes: Any,
) -> dict[str, Any]:
    return {
        "attributes": {
            "id": node_id, "text": text, "type": kind, "bounds": list(bounds),
            "visible": True, "enabled": True, **attributes,
        },
        "children": list(children),
    }


def sms_layout(
    *messages: tuple[str, str, str], thread: bool = False,
) -> str:
    """Synthetic values in the public control shape observed on HarmonyOS 6.1."""
    rows = []
    for index, (sender, body, timestamp) in enumerate(messages):
        top = 300 + index * 210
        if thread:
            children = (
                node("", timestamp, bounds=(300, top, 700, top + 30)),
                node(f"MessageList{index}", kind="Column", bounds=(30, top + 40, 850, top + 180),
                     clickable=True, children=(
                         node("msg_content_text", body, bounds=(50, top + 50, 800, top + 170),
                              clickable=True),
                     )),
            )
            rows.append(node(
                f"MessageListContent{index}", body, kind="Column",
                bounds=(0, top, 1080, top + 200), children=children,
            ))
        else:
            rows.append(node(
                f"ConversationList{index}", body, kind="RelativeContainer",
                bounds=(0, top, 1080, top + 200), clickable=True, children=(
                    node("name", kind="Row", bounds=(30, top, 600, top + 40),
                         children=(node("", sender, bounds=(30, top, 600, top + 40)),)),
                    node("time", timestamp, bounds=(700, top, 1000, top + 40)),
                    node("message_details_content", body, bounds=(30, top + 50, 1000, top + 180)),
                ),
            ))
    title = messages[0][0] if thread and messages else "信息"
    children = [node("HdsTitleBar", kind="Row", bounds=(0, 80, 1080, 180), children=(
        node("back_button", kind="Button", bounds=(20, 90, 100, 170), clickable=True),
        node("", title, bounds=(200, 90, 800, 170)),
    ))]
    if not thread:
        children.extend([
            node("conversationListSearchBar", kind="Row", bounds=(0, 180, 1080, 280)),
            node("HdsConversationList", kind="Button", bounds=(900, 90, 1050, 170),
                 clickable=True),
        ])
    children.append(node(
        "messageList" if thread else "", kind="List", bounds=(0, 280, 1080, 1900),
        children=tuple(rows), scrollable=True,
    ))
    return json.dumps(node(
        "", kind="WindowScene", bounds=(0, 0, 1080, 1920), children=tuple(children),
        bundleName=SMS_BUNDLE, hostWindowId="42",
    ))


def fake_sms(tmp_path: Path, *layouts: str):
    state = FakeDeviceState(
        layouts=list(layouts) or [sms_layout()], foreground_bundle=SMS_BUNDLE,
        click_targets=[],
    )
    state.shell_results[("bm", "dump", "-a")] = CommandResult(
        (), 0, json.dumps([SMS_BUNDLE]), "", 1,
    )
    state.shell_results[("bm", "dump", "-n", SMS_BUNDLE)] = CommandResult(
        (), 0, json.dumps({"name": SMS_BUNDLE, "mainAbility": "MainAbility"}), "", 1,
    )
    return make_fake_device(tmp_path, state=state, alias="phone")


@pytest.fixture
def sms_clock(monkeypatch):
    now = 0.0

    def advance(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr("moyu_harmony_pilot.actions.sms.time.monotonic", lambda: now)
    monkeypatch.setattr("moyu_harmony_pilot.actions.sms.time.sleep", advance)


@pytest.mark.parametrize("thread", [False, True], ids=["conversations", "thread"])
def test_body_preserved_codes_returned_and_no_message_files(tmp_path, thread):
    body = "合成示例说明。" * 30 + "短信验证码为 483921，有效期5分钟。"
    fake = fake_sms(tmp_path, sms_layout(("示例服务", body, "今天 10:23"), thread=thread))
    with offline_guard():
        result = read_sms(fake.device)
    schema = build_cli_schema()["commands"]["sms"]["subcommands"]["read"]["result_schema"]
    assert schema == SMS_RESULT_SCHEMA
    check_schema(schema)
    validate_value(result, schema)
    assert result["page"] == ("thread" if thread else "conversations")
    assert result["messages"] == [{
        "sender": "示例服务", "body": body, "time_text": "今天 10:23",
        "direction": "unknown", "unread": None, "preview": not thread,
        "body_truncated": False, "verification_codes": ["483921"],
    }]
    assert result["scope"] == "visible"
    assert result["reason"] == "snapshot"
    assert fake.state.serial not in json.dumps(result)
    assert not fake.transport.mutating_calls
    assert not fake.transport.all_remote_tmp_files()
    assert not fake.device.is_busy()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert body.encode() not in path.read_bytes()
            assert b"483921" not in path.read_bytes()


def test_sender_filter_and_limit_and_body_bounds(tmp_path):
    fake = fake_sms(tmp_path, sms_layout(
        ("甲", "a" * 5000, "昨天"), ("乙", "短信验证码123456", "今天"),
        ("甲", "second", "今天"),
    ))
    result = read_sms(fake.device, sender="甲", limit=1)
    assert result["count"] == 1
    assert result["messages"][0]["sender"] == "甲"
    assert result["messages"][0]["body"] == "a" * 4096
    assert result["messages"][0]["body_truncated"]
    assert result["truncated"]
    assert not fake.transport.mutating_calls


@pytest.mark.parametrize("suffix", ["12345678"], ids=["partial-numeric-code"])
def test_body_truncation_never_creates_partial_verification_code(tmp_path, suffix):
    body = "a" * 4085 + "短信验证码" + suffix
    result = read_sms(fake_sms(tmp_path, sms_layout(("示例", body, "今天"))).device)
    assert result["messages"][0]["body_truncated"]
    assert result["messages"][0]["body"].endswith("123456")
    assert result["messages"][0]["verification_codes"] == []


def test_native_window_name_uses_focus_id_and_layout_bundle(tmp_path):
    fake = fake_sms(tmp_path, sms_layout(("示例", "hello", "今天")))
    fake.state.shell_results[("hidumper", "-s", "WindowManagerService", "-a", "-a")] = (
        CommandResult((), 0, "mms0 0 100 43 1\nFocus window: 43\n", "", 1)
    )
    with pytest.raises(HumanRequiredError):
        read_sms(fake.device)
    assert not fake.transport.mutating_calls
    assert not fake.transport.all_remote_tmp_files()
    assert not fake.device.is_busy()


def test_hidden_offscreen_and_foreign_window_messages_excluded():
    raw = json.loads(sms_layout(("示例", "visible", "今天")))
    rows = raw["children"][-1]["children"]
    hidden = json.loads(sms_layout(("隐私", "hidden", "今天")))["children"][-1]["children"][0]
    hidden["attributes"]["visible"] = False
    rows.append(hidden)
    outside = json.loads(sms_layout(("隐私", "offscreen", "今天")))["children"][-1]["children"][0]
    outside["attributes"]["bounds"] = [0, 2500, 1080, 2700]
    rows.append(outside)
    foreign = json.loads(sms_layout(("隐私", "foreign", "今天")))
    foreign["attributes"].pop("bundleName")
    foreign["attributes"]["hostWindowId"] = "43"
    raw["children"].append(foreign)
    page = parse_sms_tree(parse_layout_dict(raw))
    assert [message.body for message in page.messages] == ["visible"]


def test_unknown_layout_and_incomplete_rows_fail_without_private_text(tmp_path):
    raw = json.loads(sms_layout(("示例", "PRIVATE_BODY", "今天")))
    raw["children"][-1]["children"][0]["children"].pop()
    fake = fake_sms(tmp_path, json.dumps(raw))
    with pytest.raises(DeviceError, match="unsupported") as caught:
        read_sms(fake.device)
    assert caught.value.code == "sms_layout_unsupported"
    assert "PRIVATE_BODY" not in str(caught.value)
    assert not fake.transport.mutating_calls
    assert not fake.transport.all_remote_tmp_files()
    assert not fake.device.is_busy()


def test_wait_excludes_baseline_and_relative_timestamp_changes(tmp_path, sms_clock):
    fake = fake_sms(tmp_path,
                    sms_layout(("示例", "短信验证码123456", "刚刚")),
                    sms_layout(("示例", "短信验证码123456", "1分钟前")),
                    sms_layout(("示例", "短信验证码654321", "刚刚")))
    fake.state.advance_layout_on_dump = True
    result = read_sms(fake.device, wait_seconds=3)
    assert result["reason"] == "new_message"
    assert result["messages"][0]["verification_codes"] == ["654321"]
    assert "123456" not in json.dumps(result)
    assert not fake.transport.mutating_calls


def test_wait_timeout_returns_empty_and_cannot_replay_old_code(tmp_path, sms_clock):
    fake = fake_sms(tmp_path, sms_layout(("示例", "短信验证码123456", "今天")))
    result = read_sms(fake.device, wait_seconds=2)
    assert result["reason"] == "timeout"
    assert result["messages"] == []
    assert "123456" not in json.dumps(result)
    assert not fake.device.is_busy()
    assert not fake.transport.mutating_calls


def test_wait_page_change_requires_new_observation(tmp_path, sms_clock):
    fake = fake_sms(tmp_path, sms_layout(), sms_layout(thread=True))
    fake.state.advance_layout_on_dump = True
    with pytest.raises(DeviceError) as caught:
        read_sms(fake.device, wait_seconds=2)
    assert caught.value.code == "sms_view_changed"
    assert not fake.device.is_busy()
    assert not fake.transport.mutating_calls


def test_sms_open_from_thread_uses_back_target_and_durable_ledger(tmp_path):
    fake = fake_sms(tmp_path, sms_layout(("示例", "短信验证码123456", "今天"), thread=True),
                    sms_layout(("示例", "短信验证码123456", "今天")))
    fake.state.click_targets = [FakeClickTarget("back_button", SMS_BUNDLE)]
    registry = ActionRegistry(allowed_actions=["sms.open"])
    registry.register(SmsOpen())
    runner = ActionRunner(registry, state_dir=tmp_path)
    with offline_guard():
        result = runner.run(fake.device, "sms.open", {}, idempotency_key="open-list", remote=True)
    assert result.status == "succeeded"
    fake.transport.assert_click_targets_consumed()
    assert result.metrics == {"ready": True}
    count = len(fake.transport.calls)
    repeated = runner.run(fake.device, "sms.open", {}, idempotency_key="open-list")
    assert repeated.status == "already_done"
    assert len(fake.transport.calls) == count
    for record in ActionLedger(tmp_path / "actions.sqlite3").records():
        assert "123456" not in json.dumps(record)
    assert not fake.transport.all_remote_tmp_files()


def test_sms_open_uncertain_submission_never_replayed(tmp_path):
    fake = fake_sms(tmp_path, sms_layout(("示例", "hello", "今天"), thread=True))
    fake.state.timeout_after_mutation = True
    registry = ActionRegistry()
    registry.register(SmsOpen())
    runner = ActionRunner(registry, state_dir=tmp_path)
    outcome = runner.run(fake.device, "sms.open", {}, idempotency_key="uncertain")
    assert outcome.status == "unknown"
    count = len(fake.transport.calls)
    assert runner.run(fake.device, "sms.open", {}, idempotency_key="uncertain").status == "unknown"
    assert len(fake.transport.calls) == count


@pytest.mark.parametrize(
    "options",
    [{"remote": True, "idempotency_key": "key"}, {}],
    ids=["remote-without-allowlist", "missing-idempotency-key"],
)
def test_sms_open_requires_remote_allowlist_and_idempotency_key(tmp_path, options):
    fake = fake_sms(tmp_path)
    registry = ActionRegistry()
    registry.register(SmsOpen())
    runner = ActionRunner(registry, state_dir=tmp_path)
    with pytest.raises(DeviceConfigurationError):
        runner.run(fake.device, "sms.open", {}, **options)
    assert fake.transport.calls == []
