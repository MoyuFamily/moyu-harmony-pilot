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

Start a packaged plugin with the [standalone template](../examples/plugin-template/README.md).
It includes Python package metadata, an installed entry point, offline contract tests
and a macOS/Linux CI workflow pinned to a tested Pilot revision.

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

Bridge services can set the config-relative `plugin_paths` array in their private JSON configuration. The SDK also accepts an explicit directory collection through `ActionRegistry.discover(plugin_paths=...)`. Both override the directory environment variable for that invocation and retain installed entry-point discovery.

## Plugin contract

Import `ActionPlugin`, `ActionSpec`, `ActionContext` and `ActionOutcome` from `moyu_harmony_pilot.actions`. Read-only plugins implement `execute(ctx, params)`. Declare parameter and metric schemas, version, description, timeout and side-effect classification in `spec`.

### Compatibility declarations

Set `ActionSpec(api_version=1, ...)` to declare the action API implemented by a plugin.
Explicit API declarations are available starting with Pilot `0.1.0.dev1`.
The exported `ACTION_API_VERSION` constant and `harmony-pilot schema --json` report
the host's supported API. Registration checks this declaration and rejects an
unsupported API with `configuration_error` before action execution. Existing plugins
that omit the field default to API v1. `plugins list`, `plugins describe` and Bridge
`--check` include `api_version` in each action description. Plugin imports and
constructors still run during discovery and must remain free of device interactions.

Maintain three version declarations when distributing a plugin.

- `api_version` identifies the action interface and execution contract. Declare the
  implemented integer explicitly, so a host upgrade cannot silently change it.
- `version` identifies the action's behavior and participates in the ledger request
  fingerprint. Changing it causes reuse of an old idempotency key to fail validation.
  Preserve the ledger and reconcile outstanding work during upgrades.
- A packaged plugin declares its supported Pilot package range in `project.dependencies`
  and Python range in `project.requires-python`. Test each supported Pilot release or
  pinned development revision. Directory plugins document these requirements alongside
  `actions.json` and deploy into an environment with those versions installed.

Package dependency resolution checks the Pilot release range during installation.
The registry checks the action API at load time. An API match covers the action
interface, while driver methods used by the plugin also need version compatibility tests.

### Parameters and execution

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

The CLI provides local execution. Bridge and MCP use the registry and runner's `remote=True` gates with an explicit `allowed_actions` collection. The default collection is empty. Remote discovery filters external entry-point names and directory manifest IDs before loading their modules. Remote invocation checks the allowlist again and rejects actions marked `local_only`, including `ui.inspect`.

The Pull Bridge uses these gates through `serve`. MCP integration follows the same action contract.

## Local vision and guarded interaction

Install `moyu-harmony-pilot[vision]` for Pillow-based SoM annotation and screenshot resizing. `harmony-pilot screenshot --out .state/screen.png --som --max-width 720 --json` captures under the device lock, checks the layout before and after capture, scales marks to the saved image and returns the matching compact snapshot. Layout changes produce `layout_changed` so the caller can capture again.

Install `[vision-macos]` or `[vision-rapidocr]` for OCR. Use `create_ocr_engine("macos")` or `create_ocr_engine("rapidocr")` from `moyu_harmony_pilot.vision`, then call `recognize(local_image_path_or_bytes)`. Each `TextBox` has text, pixel bounds with a top-left origin, confidence and center coordinates. `find_text` accepts exact text or a regular expression and a minimum confidence. Backend imports are lazy and missing dependencies produce `VisionUnavailableError`. OCR observations remain local.

`InteractionSession` provides local `observe`, `click_ref`, directional `swipe`, `back` and `wait_for`. Each session has a fixed wall-clock lifetime and step budget. Mutating calls require the previously observed `layout_hash`, acquire the device lock, capture fresh references, check temperature and sensitive screens, then return the new snapshot, foreground bundle, focus references and layout difference. An uncertain post-input result returns `unknown` and requires observation before another step.

Pass a `PopupWatcher` to `wait_for` to enable exact-label, exact-bundle dismissal rules. The default rule collection is empty. Rules require a unique enabled clickable match, stop on sensitive screens and charge each layout at most once within the configured dismissal budget.
