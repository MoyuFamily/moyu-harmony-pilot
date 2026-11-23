"""Reusable offline assertions through the public registry, runner and ledger contracts.

Supply a fresh fake and state directory for each scenario. Configure the fixture
to complete, time out, remain unconfirmed or expose a protected screen before
calling the appropriate assertion. Helpers return the first receipt for further
plugin-specific metric assertions and work with both unittest and pytest.
Configured click targets must all be consumed without mismatches, including when
the runner converts a failed input into an unknown receipt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any, TypeVar

from moyu_harmony_pilot.actions import ActionLedger, ActionOutcome, ActionRunner, IdempotentAction
from moyu_harmony_pilot.device import HarmonyDevice
from moyu_harmony_pilot.testing.fakes import FakeDevice, FakeHdcTransport

T = TypeVar("T")


def assert_readonly(transport: FakeHdcTransport, callback: Callable[[], T]) -> T:
    """Run a callback and reject any attempted mutating transport operation."""
    before = list(transport.mutating_calls)
    try:
        return callback()
    finally:
        assert transport.mutating_calls == before, "Read-only stage attempted a mutation."


def _reservation(
    runner: ActionRunner, device: HarmonyDevice, action_id: str, params: dict[str, Any],
    idempotency_key: str,
) -> tuple[str, str] | None:
    _, spec = runner.registry.get(action_id)
    key = ActionLedger.make_key(
        device.profile.serial, action_id, idempotency_key, spec.version, params,
    )
    path = runner.state_dir / "actions.sqlite3"
    if not path.exists():
        return None
    # A separate read-only connection proves the reservation is already committed.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT request_hash, status FROM actions "
            "WHERE device_hash=? AND action_id=? AND key_hash=?", key.identity,
        ).fetchone()
    if row is None:
        return None
    request_hash, status = row
    assert isinstance(request_hash, str) and isinstance(status, str)
    assert request_hash == key.request_hash, "Contract key belongs to another request."
    return request_hash, status


def assert_submitting(
    runner: ActionRunner, device: HarmonyDevice, action_id: str, params: dict[str, Any], *,
    idempotency_key: str,
) -> None:
    """Assert the exact request has a durable submitting row before its side effects."""
    row = _reservation(runner, device, action_id, params, idempotency_key)
    assert row is not None, "Submission started without a durable reservation."
    assert row[1] == "submitting", f"Expected submitting before mutation, got {row[1]!r}."


def _run_mutating(
    runner: ActionRunner, fake: FakeDevice, action_id: str, params: dict[str, Any], *,
    idempotency_key: str, remote: bool, expected_mutations: int,
) -> ActionOutcome:
    plugin, spec = runner.registry.get(action_id, remote=remote)
    assert isinstance(plugin, IdempotentAction), "Contract requires an IdempotentAction."
    assert type(expected_mutations) is int and expected_mutations >= 0
    assert _reservation(runner, fake.device, action_id, params, idempotency_key) is None, (
        "Use a fresh contract key and state directory."
    )
    before = len(fake.transport.mutating_calls)
    stages: list[tuple[str, int]] = []
    stage_errors: list[Exception] = []

    def observe_stage(stage: str) -> None:
        stages.append((stage, len(fake.transport.mutating_calls)))
        if stage == "preparing":
            try:
                assert _reservation(
                    runner, fake.device, action_id, params, idempotency_key,
                ) is None, "Preparation started after a durable reservation."
            except Exception as exc:
                stage_errors.append(exc)
                raise
        if stage == "submit":
            try:
                assert_submitting(
                    runner, fake.device, action_id, params, idempotency_key=idempotency_key,
                )
            except Exception as exc:
                # ActionRunner converts plugin exceptions into receipts. Re-raise assertion
                # failures after run() so an expected unknown cannot conceal a broken guard.
                stage_errors.append(exc)
                raise

    outcome = runner.run(
        fake.device, action_id, params, idempotency_key=idempotency_key, remote=remote,
        on_stage=observe_stage,
    )
    if stage_errors:
        raise AssertionError("Submission reservation contract failed.") from stage_errors[0]
    fake.transport.assert_click_targets_consumed()
    names = [stage for stage, _ in stages]
    assert names in (
        ["inspect", "submit"],
        ["inspect", "submit", "confirm"],
        ["inspect", "preparing", "submit"],
        ["inspect", "preparing", "submit", "confirm"],
    ), (
        f"Expected one inspect/submit cycle, got {names!r}."
    )
    assert stages[1][1] == before, "Inspection attempted a mutation before reservation."
    after = len(fake.transport.mutating_calls)
    if names[-1] == "confirm":
        assert stages[-1][1] == after, "Confirmation attempted a mutation."
    assert after - before == expected_mutations, (
        f"Expected {expected_mutations} mutation attempts, observed {after - before}."
    )
    assert not fake.transport.all_remote_tmp_files(), "Driver left remote temporary files."
    outcome.validate(spec)
    return outcome


def _assert_replays(
    runner: ActionRunner, fake: FakeDevice, action_id: str, params: dict[str, Any], *,
    idempotency_key: str, remote: bool, expected_status: str, metrics: dict[str, Any] | None = None,
) -> None:
    # Recreate the runner to exercise persisted state, including submitting recovery.
    restarted = ActionRunner(runner.registry, state_dir=runner.state_dir)
    for current in (runner, restarted):
        before = list(fake.transport.calls)
        stages: list[str] = []
        replay = current.run(
            fake.device, action_id, params, idempotency_key=idempotency_key, remote=remote,
            on_stage=stages.append,
        )
        assert replay.status == expected_status, replay.to_dict()
        assert not stages, "Replay entered a plugin stage."
        assert fake.transport.calls == before, "Replay accessed the device."
        if metrics is not None:
            assert replay.metrics == metrics, "Replay lost the confirmed metrics."


def assert_idempotent_success(
    runner: ActionRunner, fake: FakeDevice, action_id: str, params: dict[str, Any], *,
    idempotency_key: str = "contract-success", remote: bool = False, expected_mutations: int = 1,
) -> ActionOutcome:
    """Require a real successful submission, committed reservation and zero-effect replays.

    The fixture must begin in a ready state. ``expected_mutations`` counts attempted
    transport writes and can include several submit-stage navigation operations.
    Both same-runner and restarted-runner replays must return cached final metrics.
    """
    assert expected_mutations > 0, "A successful mutation contract must exercise a write."
    outcome = _run_mutating(
        runner, fake, action_id, params, idempotency_key=idempotency_key, remote=remote,
        expected_mutations=expected_mutations,
    )
    assert outcome.status == "succeeded", outcome.to_dict()
    row = _reservation(runner, fake.device, action_id, params, idempotency_key)
    assert row is not None and row[1] == "succeeded", "Success was not persisted."
    _assert_replays(
        runner, fake, action_id, params, idempotency_key=idempotency_key, remote=remote,
        expected_status="already_done", metrics=outcome.metrics,
    )
    return outcome


def assert_unknown_no_replay(
    runner: ActionRunner, fake: FakeDevice, action_id: str, params: dict[str, Any], *,
    idempotency_key: str = "contract-unknown", remote: bool = False, expected_mutations: int = 1,
) -> ActionOutcome:
    """Require uncertainty after reservation and blocked replays across runner recreation.

    Configure a transport timeout or unconfirmed layout before calling this helper.
    A zero-write failure during submit can be covered with ``expected_mutations=0``.
    """
    outcome = _run_mutating(
        runner, fake, action_id, params, idempotency_key=idempotency_key, remote=remote,
        expected_mutations=expected_mutations,
    )
    assert outcome.status == "unknown", outcome.to_dict()
    assert outcome.error_code == "submission_uncertain", outcome.to_dict()
    row = _reservation(runner, fake.device, action_id, params, idempotency_key)
    assert row is not None and row[1] in {"submitting", "unknown"}
    _assert_replays(
        runner, fake, action_id, params, idempotency_key=idempotency_key, remote=remote,
        expected_status="unknown",
    )
    return outcome


def assert_pre_submit_blocked(
    runner: ActionRunner, fake: FakeDevice, action_id: str, params: dict[str, Any], *,
    expected_status: str, idempotency_key: str = "contract-blocked", remote: bool = False,
) -> ActionOutcome:
    """Check a configured thermal or sensitive-screen boundary before submission.

    Set ``fake.state.temperature_c`` or supply a sensitive layout before invoking.
    The result must have no writes, no submit/confirm stage and no reserved request.
    """
    assert expected_status in {"device_overheated", "human_required"}
    stages: list[str] = []
    outcome = assert_readonly(fake.transport, lambda: runner.run(
        fake.device, action_id, params, idempotency_key=idempotency_key, remote=remote,
        on_stage=stages.append,
    ))
    assert outcome.status == expected_status, outcome.to_dict()
    assert "submit" not in stages and "confirm" not in stages
    assert _reservation(runner, fake.device, action_id, params, idempotency_key) is None, (
        "Protected request acquired a submission reservation."
    )
    assert not fake.transport.all_remote_tmp_files(), "Driver left remote temporary files."
    return outcome
