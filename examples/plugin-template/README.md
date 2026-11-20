# Harmony Pilot plugin template

Copy this directory, including `.github`, into a new repository. It contains a
packaged read-only battery check, installed entry-point tests, and a synthetic
mutation example used only by the tests. The template is maintained by MoyuFamily
and distributed under the included MIT license.

## Compatibility

The action implements API v1. Package metadata declares Python 3.11+ and Pilot
`>=0.1.0.dev1,<0.2`. `requirements-test.txt` pins the reviewed development revision
used by CI. The template workflow runs Python 3.11 and 3.12 on macOS and Linux.
Record the exact Pilot revisions and host versions your own plugin passes, and
adjust the dependency range to the evidence from those tests.

## Install and verify

Run these commands from the copied template directory. Dependency installation
requires access to the configured package sources. The tests use temporary state
and a synthetic device with standard-library mocks.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/python -m tests
.venv/bin/harmony-pilot plugins describe example.battery_check --json
```

For development against a local Pilot checkout, install that checkout first and
then install this plugin with `python -m pip install --no-deps .`. Run `pip check`
and the same contract suite after installation.

`python -m tests` fails on an unavailable SDK or entry point, a failed contract,
an empty suite, or a skipped test. Tests discover the actual installed entry point
and use `ActionRunner` to exercise parameter validation, remote authorization and
bounded results. The fake records every device interaction to verify the read-only
action. Mutation examples verify read-only inspection, durable reservation before
submission, successful deduplication, and uncertain results across runner restarts.

## Adapt the template

1. Rename the distribution, Python package and `example.battery_check` action ID
   in `pyproject.toml`, implementation and tests. Use a namespace you maintain.
2. Declare parameters, returned metrics, timeout and side effects in `ActionSpec`.
   Keep `api_version=1` explicit and use `version` for the action behavior.
3. Extend the fake device with the methods your plugin uses and add fixtures for
   observed layouts. Keep module imports and constructors free of I/O.
4. For a mutating action, implement `IdempotentAction` and adapt the scenarios in
   `tests/test_mutating_contract.py` to execute your own installed action. Keep
   navigation and submission inside `submit`, after the ledger reservation.
5. Update the pinned Pilot revision, declared compatibility range, license and
   maintainer information for each release. Preserve host state during upgrades.

## Enable remote execution

Install the plugin in the interpreter used by the Pilot service. Add its action
ID to the target device's `allowed_actions` in the private Bridge configuration.
Then run the preflight and restart the worker.

```bash
harmony-pilot serve --config /private/bridge.local.json --check --json
```

The preflight exports the action API, parameter schema and metrics schema for the
Broker. MCP uses `--allow-action example.battery_check` at startup.

The full development and execution contract is in Pilot's `docs/actions.md`.
