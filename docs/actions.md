# Action plugins and local execution

## Inspect and run built-in actions

```bash
harmony-pilot plugins list --json
harmony-pilot plugins describe device.health --json
harmony-pilot run --device harmony-01 --action device.health --json
harmony-pilot run --device harmony-01 --action ui.inspect --params-json '{"max_elements":40}' --json
harmony-pilot run --device harmony-01 --action device.health --dry-run --json
```

`device.ping` checks the official uitest response. `device.health` reports bounded battery and power metrics. `ui.inspect` saves a compact snapshot under the local state directory and returns its layout hash and element count. Diagnostics use directory mode `0700` and file mode `0600`.

The default state directory is `.state`. Use the same `--state-dir` across invocations sharing a physical device so that locks and the SQLite ledger share one location.

`--dry-run` validates the plugin, parameters, idempotency key and online device selection through target enumeration. Plugin discovery imports Python modules and constructs plugin classes. Keep module initialization and constructors free of device interactions.

## External plugin discovery

Plugin code executes with the host process privileges. Install and configure reviewed, trusted plugins.

For an installed Python package, register its class using the action ID as the entry-point name.

```toml
[project.entry-points."moyu_harmony_pilot.actions"]
"example.ui_summary" = "example_package.ui_summary:UiSummary"
```

For a local directory, create an `actions.json` manifest mapping action IDs to Python file and class names. Each file must reside directly inside that directory.

```json
{
  "example.ui_summary": "ui_summary.py:UiSummary"
}
```

The repository includes a complete read-only example in `examples/actions`. From the repository root, load it with the following commands.

```bash
export HARMONY_PILOT_PLUGIN_PATHS="$PWD/examples/actions"
harmony-pilot plugins describe example.ui_summary --json
harmony-pilot run --device harmony-01 --action example.ui_summary --json
```

Separate multiple directories with the host path separator (`:` on supported macOS and Linux hosts). Duplicate action IDs and inconsistent metadata stop discovery with `configuration_error`. Restart the process to load edited plugin modules.

## Plugin contract

Import `ActionPlugin`, `ActionSpec`, `ActionContext` and `ActionOutcome` from `moyu_harmony_pilot.actions`. Read-only plugins implement `execute(ctx, params)`. Declare parameter and metric schemas, version, description, timeout and side-effect classification in `spec`.

The supported JSON Schema subset includes explicit types, closed object properties, required properties, arrays with item schemas, enums, numeric bounds and string/array length bounds. `harmony-pilot schema --json` lists the accepted keywords. Object schemas require `additionalProperties: false`. Integer parameters use JSON integer literals. Boolean values have a distinct type.

Parameters, plugin metadata and individual receipts each have a 16 KiB canonical JSON limit. Nesting is limited to eight levels. Receipt metrics are declared scalar fields with at most 32 keys, 64-character key names, 128-character string values and finite numbers. Summaries are limited to 256 characters and error codes to 64 lowercase snake-case characters. Plugin authors must keep summaries, metrics and logs free of credentials, hardware serials, UI text and other personal data.

Use `ctx.checkpoint()` before each interaction and inside loops. Pass `ctx.remaining_timeout` to device operations. `--max-steps` counts runner stage boundaries and explicit plugin checkpoints. The default is 100.

The runner uses POSIX `SIGALRM` for plugin-stage deadlines on macOS and Linux. Execute the SDK runner in a process main thread with an available real-time timer. A configured `--timeout` can shorten the plugin default. Deadline handling also checks elapsed time after a stage returns. Plugin code must propagate cancellation and use bounded driver operations, including when calling native extensions. Device cleanup and ledger finalization run after cancellation.

All actions acquire the existing physical-device lock. Non-diagnostic actions pass the thermal gate. Mutating actions also check sensitive UI signals and temperature immediately before submission. Keep each submit stage small and check for sensitive screens when navigating within a plugin.

## Mutating actions and the ledger

Implement `IdempotentAction` with three stages.

1. `inspect(ctx, params)` performs read-only checks and returns `Inspection` with `ready`, `already_done`, `human_required` or `failed`. Optional private inspection state is passed to the submit stage.
2. `submit(ctx, inspection, params)` performs the side effect once after the runner commits a `submitting` reservation.
3. `confirm(ctx, params)` independently verifies completion and returns a bounded `ActionOutcome`.

Every mutating execution requires an explicit `--idempotency-key`. The SQLite identity combines hashed physical-device identity, action ID and hashed key. A request fingerprint binds the key to its parameters and plugin version. Reusing the key for a changed request returns `configuration_error`.

Confirmed records return `already_done` before plugin callbacks or device wake-up. Submission errors, timeouts, interruptions and inconclusive confirmation yield `unknown`. A process restart recovers leftover `submitting` records as `unknown` while holding the device lock. A failed final database write preserves the committed reservation and returns `unknown`.

An `unknown` record requires read-only reconciliation or human review. Repeated calls with that key return `unknown`. Preserve the state directory during recovery so that the ledger retains the submission history. Automatic reconciliation and operator resolution commands remain future work.

Runner-generated failures contain stable status codes and generic summaries. Plugin progress printed during CLI discovery or execution is routed to stderr, preserving the single JSON envelope on stdout.

## Remote execution boundary

The current CLI provides local execution. The registry and runner expose `remote=True` for future adapters, together with an explicit `allowed_actions` collection. The default collection is empty. Remote discovery filters external entry-point names and directory manifest IDs before loading their modules. Remote invocation checks the allowlist again and rejects actions marked `local_only`, including `ui.inspect`.

The Pull Bridge and MCP adapters are planned for M3.
