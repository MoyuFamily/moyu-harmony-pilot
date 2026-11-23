---
name: harmony-pilot
description: Inspect and operate HarmonyOS NEXT devices through the harmony-pilot CLI or its local MCP tools. Use for compact UI inspection, device diagnostics, trusted action plugins, or explicitly authorized local interaction.
---

# Harmony Pilot

Discover the installed contract with `harmony-pilot schema --json` and available aliases with `harmony-pilot devices --json`. Prefer configured aliases when selecting devices.

## Observe and diagnose

Use `doctor`, `dump-ui --compact` and `screenshot` for read-only inspection. JSON stdout contains one envelope. Check `ok`, `error.code`, `error.hint` and the exit code. Keep diagnostics local.

```bash
harmony-pilot doctor --device harmony-01 --json
harmony-pilot dump-ui --device harmony-01 --compact --json
harmony-pilot screenshot --device harmony-01 --out .state/screen.png --som --max-width 720 --json
```

Snapshots contain deterministic `@eN` references, `layout_hash` and `sensitive_signals`. `--som` requires the optional vision extra and returns the matching snapshot. For `layout_changed`, capture again before using the marks.

Use `ocr --backend auto --max-width 1280 --json` for bounded local text recognition with an installed OCR extra. Bounds use resized-image pixels, with original dimensions and x/y scale in the result. MCP registers read-only OCR after `--enable-ocr` startup. `screenshot(inline=true)` requires `--inline-images` and stays within `--max-image-bytes` (2 MiB default). Sensitive image or OCR observations require human takeover.

## Execute actions

Inspect `plugins list` and `plugins describe ACTION_ID` before constructing parameters. Select trusted plugins whose schemas match the task. Use `plugins validate ACTION_ID --params-json '...' --json` to check parameters offline; mutating actions also require `--idempotency-key`. This imports trusted plugin code but does not check a device, remote allowlists, or execute an action. Use `run --dry-run` when online device selection also needs validation.

```bash
harmony-pilot plugins describe device.health --json
harmony-pilot plugins validate device.health --params-json '{}' --json
harmony-pilot run --device harmony-01 --action device.health --params-json '{}' --json
```

For authorized side effects, preserve a stable idempotency key for the same logical operation and preserve the shared state directory across retries. `already_done` confirms previous completion. For `unknown`, stop submission retries and use read-only reconciliation (`ledger reconcile`) or human review (`ledger resolve --note` to confirm completion, or local-only `ledger release --note` after verifying no side effect occurred to allow retrying with the same key). Changing the key to bypass uncertainty defeats the ledger.

## Local interactive work

Keep interaction within the user-authorized task. MCP defaults to observation tools and explicitly allowlisted actions. `--local-lab` adds `lab_observe`, `click_ref`, directional `swipe`, `back`, `home`, `long_click_ref`, `input_text_ref`, `start_app`, `wait_for` and `scroll_until` with a fixed session budget.

Start with `lab_observe`, then pass its `layout_hash` with the selected reference or navigation step. Read the returned snapshot and `changed` feedback before the next step. A stale hash or uncertain step requires fresh observation. Report exhausted budgets with the last known state and remaining work.

If `sensitive_signals` or `human_required` indicates a password, verification challenge, payment confirmation or permission prompt, stop and hand the indicated step to the user. Keep screenshots, OCR results and layout text local.

CLI interaction uses `interact SUBCOMMAND --local-lab` with a fresh budget per invocation. Scroll search observes internally before each swipe.

For authorized package deployment, use `apps install --package <path>` with a stable idempotency key. HAP, application-internal HSP, and HarmonyOS APP archives are accepted; the existing `--hap` flag also works. A single APP is submitted intact on API 22+ devices. Earlier APIs and combined package inputs use privately expanded HAP/HSP modules. Action parameters retain the original archive path and digest. AppGallery upload signing can cover the outer APP while the device installer requires signed inner modules. URL installation recognizes `.app` and `.hsp` URL paths; for opaque download URLs, first use `apps download --out <path.app>` to select the format. An `unknown` result can be checked with `ledger reconcile` using the original action parameters. Record verified human completion with `ledger resolve --note`. `apps list` and `apps info` provide read-only bundle discovery.

Use `inspect --out PRIVATE.html` for a local visual inspection artifact and `ledger report` or `report --input RESULT.json` for execution reports. Keep these artifacts private.

## Interpret outcomes

- Exit `0` indicates success, including `already_done`.
- Exit `1` indicates failure or uncertainty. Inspect the structured code before retrying.
- Exit `2` indicates invalid parameters, configuration or unavailable optional dependencies.
- Exit `3` indicates an offline or unauthorized device.
- Exit `4` requires human intervention.
- Exit `5` requires cooling the device before further work.
- Exit `6` indicates a busy device. Wait for its current operation to finish.

For plugin development, start with `examples/plugin-template` in a Pilot source checkout. Keep application-specific logic in an external plugin and use the public `actions`, `device`, `ui` and optional `vision` APIs. Install the plugin into the Pilot environment, inspect it with `plugins describe`, validate sample parameters with `plugins validate`, and run the template's `python -m tests` offline contracts. For Bridge deployment, add an explicit per-device allowlist entry and inspect `serve --config PRIVATE_CONFIG --check --json` before execution. The installed CLI schema and plugin descriptions define accepted inputs.

Plugins using new context APIs declare `min_pilot_version="0.2.0.dev0"` (or `"0.2.0rc1"` when relying on `IdempotentAction.prepare`) and a matching package dependency. Use `ctx.interact()` inside Runner stages to reuse the lock and budget. `prepare` may run idempotent pre-submission setup (such as launching the app or navigating) before ledger reservation without locking the key on pre-submit failure; `submit` performs the final mutation after the durable reservation. `inspection_metrics_schema` describes inspect/prepare baselines, while final success must satisfy `metrics_schema.required`. Preserve `human_required` and `failed` inspection/prepare outcomes even when success metrics are absent.

Reuse `moyu_harmony_pilot.testing` for synthetic device transports and contract assertions. `fixture capture --out PRIVATE.json --json` produces a redacted candidate with `manual_review_required=true`. Review before committing. `testing.broker.MockBroker` validates the four Bridge endpoints entirely in memory and can be injected into `HttpBridgeClient` as its opener.

For receipt retention, `receipts prune --database PRIVATE.sqlite3 --bridge-id WORKER --older-than-days N` archives acknowledged success payloads and retains job tombstones. Archived job IDs fail closed on delivery. Pending and uncertain receipts remain available. Keep the Broker's acknowledged result for later reconciliation.
