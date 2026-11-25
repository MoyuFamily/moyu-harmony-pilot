"""Offline ledger migration, guarded interaction, waits and CLI contracts."""

import io
import json
import sqlite3

import pytest
from PIL import Image

from moyu_harmony_pilot.actions import ActionOutcome, InteractionSession
from moyu_harmony_pilot.actions.ledger import ActionLedger
from moyu_harmony_pilot.cli.main import build_parser
from moyu_harmony_pilot.cli.operations import OPERATION_SCHEMAS
from moyu_harmony_pilot.cli.schema import build_cli_schema
from moyu_harmony_pilot.device import DeviceConfigurationError, DeviceProfile, HarmonyDevice
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.ui import UiSelector
from moyu_harmony_pilot.ui.errors import UiElementNotFoundError, UiInvalidInputError, UiTimeoutError
from moyu_harmony_pilot.ui.selector import selector_from_dict
from tests.fakes import FakeDeviceState, FakeHdcTransport
from tests.test_cli import _run_cli


def test_v1_ledger_migration_resolution_and_retention(tmp_path):
    path = tmp_path / "actions.sqlite3"
    key = ActionLedger.make_key("SYNTHETIC_OLD_DEVICE", "example.action", "old", "1.0", {})
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE actions (device_hash TEXT, action_id TEXT, key_hash TEXT,
            request_hash TEXT, status TEXT, receipt TEXT, updated_at TEXT,
            PRIMARY KEY(device_hash, action_id, key_hash))""")
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO actions VALUES (?, ?, ?, ?, 'submitting', NULL, '2000-01-01')",
            (*key.identity, key.request_hash),
        )
    ledger = ActionLedger(path)
    assert ledger.lookup(key).status == "unknown"
    assert ledger.records()[0]["inspection_metrics"] == {}
    assert ledger.prune(older_than_days=1) == 0
    ledger.resolve(key, ActionOutcome(), note="Synthetic verification")
    assert ledger.lookup(key).status == "already_done"
    with pytest.raises(DeviceConfigurationError):
        ledger.resolve(key, ActionOutcome(), note="Duplicate")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM resolutions").fetchone()[0] == 1
        db.execute("UPDATE actions SET updated_at='2000-01-01'")
    assert ledger.prune(older_than_days=1) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_ledger_release_allows_retry_and_protects_confirmed_records(tmp_path):
    path = tmp_path / "actions.sqlite3"
    ledger = ActionLedger(path)
    key = ActionLedger.make_key("SYNTHETIC_DEV", "example.action", "day-1", "1.0", {"x": 1})
    other_params = ActionLedger.make_key(
        "SYNTHETIC_DEV", "example.action", "day-1", "1.0", {"x": 2}
    )

    with pytest.raises(DeviceConfigurationError, match="No matching uncertain reservation"):
        ledger.release(key, note="Nothing to release")

    ledger.reserve(key)
    with pytest.raises(DeviceConfigurationError, match="audit note"):
        ledger.release(key, note="   ")
    with pytest.raises(DeviceConfigurationError, match="different parameters"):
        ledger.release(other_params, note="Wrong params")

    # Release directly from submitting (e.g. crashed before finish).
    ledger.release(key, note="Verified no side effect after crash")
    assert ledger.lookup(key) is None

    # Re-reserve, mark unknown, then release and confirm audit trail.
    ledger.reserve(key)
    ledger.finish(
        key,
        ActionOutcome(status="unknown", summary="Uncertain.", error_code="submission_uncertain"),
    )
    assert ledger.lookup(key).status == "unknown"
    ledger.release(key, note="Verified blocked on boot wizard")
    assert ledger.lookup(key) is None

    # Confirmed records must never be released.
    ledger.reserve(key)
    ledger.finish(key, ActionOutcome(status="succeeded"))
    with pytest.raises(DeviceConfigurationError, match="Only uncertain reservations"):
        ledger.release(key, note="Attempted release on completed action")
    assert ledger.lookup(key).status == "already_done"

    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT status, note FROM resolutions ORDER BY rowid").fetchall()
    assert rows == [
        ("released", "Verified no side effect after crash"),
        ("released", "Verified blocked on boot wizard"),
    ]


@pytest.fixture
def local_device(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (400, 800), "#dbe3e7").save(image, format="PNG")
    layout = json.dumps(
        {
            "attributes": {"type": "Root", "bounds": "[0,0][400,800]"},
            "children": [
                {
                    "attributes": {
                        "type": "TextInput",
                        "id": "search",
                        "text": "查询",
                        "bounds": "[10,20][390,100]",
                        "clickable": "true",
                        "longClickable": "true",
                        "enabled": "true",
                        "visible": "true",
                    }
                }
            ],
        }
    )
    state = FakeDeviceState("SYNTHETIC_INTERACTION", layouts=[layout], png_bytes=image.getvalue())
    transport = FakeHdcTransport([state])
    device = HarmonyDevice(
        HdcClient(transport=transport), DeviceProfile("phone", state.serial), state_dir=tmp_path
    )
    return device, state, transport


def test_input_long_press_home_wait_scroll_and_unicode(local_device):
    device, _, transport = local_device
    session = InteractionSession(device)
    snapshot = session.observe()
    element = next(element for element in snapshot.elements if element.id == "search")
    text = "中文 空格 '引号' $HOME `literal` 😀"
    result = session.input_text_ref(element.ref, text, layout_hash=snapshot.layout_hash)
    assert result["snapshot"]
    assert session.long_click_ref(element.ref, layout_hash=snapshot.layout_hash)["snapshot"]
    assert session.home(layout_hash=snapshot.layout_hash)["snapshot"]
    assert session.wait_for(UiSelector(id="search")).id == "search"
    assert session.scroll_until(UiSelector(id="search"), max_swipes=0).id == "search"
    with pytest.raises(UiElementNotFoundError):
        session.scroll_until(UiSelector(id="missing"), max_swipes=0)
    with pytest.raises(DeviceConfigurationError):
        session.input_text_ref(element.ref, text, layout_hash="stale")
    with pytest.raises(DeviceConfigurationError):
        session.input_text_ref(element.ref, "a\nb", layout_hash=snapshot.layout_hash)
    import shlex

    commands = [shlex.split(args[1]) for _, args, _ in transport.calls if args[0] == "shell"]
    assert ["uitest", "uiInput", "inputText", "200", "60", text] in commands
    assert not transport.all_remote_tmp_files()


def test_wait_propagates_remaining_budget_and_window_filters(local_device, monkeypatch):
    device = local_device[0]
    tree = device.ui.dump_tree()
    now = [0.0]
    calls = []

    def dump(**kwargs):
        calls.append(kwargs)
        now[0] += 0.4
        return tree

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(device.ui, "dump_tree", dump)
    with pytest.raises(UiTimeoutError):
        device.ui.wait_for(
            UiSelector(id="missing"),
            timeout=1,
            interval=0.1,
            bundle_name="com.example.package",
            window_id=2,
            display_id=1,
            clock=lambda: now[0],
            sleeper=sleep,
        )
    assert calls[0]["timeout"] == 1
    assert calls[1]["timeout"] == pytest.approx(0.5)
    assert calls[1]["bundle_name"] == "com.example.package"
    assert calls[1]["window_id"] == 2
    assert calls[1]["display_id"] == 1
    now[0] = 0
    with pytest.raises(UiTimeoutError):
        device.ui.wait_gone(UiSelector(id="search"), timeout=0.1, clock=lambda: now[0])


@pytest.mark.parametrize(
    "selector",
    [
        {"unknown": "x"},
        {"text_regex": "["},
    ],
    ids=['unknown-field', 'invalid-pattern'],
)
def test_external_selectors_fail_closed(selector):
    with pytest.raises(UiInvalidInputError) as error:
        selector_from_dict(selector)
    assert error.value.code == "configuration_error"


def test_cli_schema_covers_every_new_parser():
    parser = build_parser()
    schema = build_cli_schema()
    for command, definition in OPERATION_SCHEMAS.items():
        assert schema["commands"][command] == definition
    assert parser.parse_args(["apps", "list", "--json"]).json
    assert parser.parse_args(["--json", "ledger", "list"]).json
    assert parser.parse_args(["interact", "--json", "observe", "--local-lab"]).json
    code, output, _ = _run_cli(["interact", "observe", "--json"], env={})
    assert code == 2
    assert json.loads(output)["error"]["code"] == "configuration_error"
    assert schema["bridge_config"]["properties"]["max_jobs"]["maximum"] == 32
