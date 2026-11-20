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

## Execute actions

Inspect `plugins list` and `plugins describe ACTION_ID` before constructing parameters. Select trusted plugins whose schemas match the task. Use `run --dry-run` to validate parameters and online device selection when that resolves uncertainty.

```bash
harmony-pilot plugins describe device.health --json
harmony-pilot run --device harmony-01 --action device.health --params-json '{}' --json
```

For authorized side effects, preserve a stable idempotency key for the same logical operation and preserve the shared state directory across retries. `already_done` confirms previous completion. For `unknown`, stop submission retries and use read-only reconciliation or human review. Changing the key to bypass uncertainty defeats the ledger.

## Local interactive work

Keep interaction within the user-authorized task. MCP defaults to observation tools and explicitly allowlisted actions. `--local-lab` adds `lab_observe`, `click_ref`, directional `swipe` and `back` with a fixed session budget.

Start with `lab_observe`, then pass its `layout_hash` with the selected reference or navigation step. Read the returned snapshot and `changed` feedback before the next step. A stale hash or uncertain step requires fresh observation. Report exhausted budgets with the last known state and remaining work.

If `sensitive_signals` or `human_required` indicates a password, verification challenge, payment confirmation or permission prompt, stop and hand the indicated step to the user. Keep screenshots, OCR results and layout text local.

## Interpret outcomes

- Exit `0` indicates success, including `already_done`.
- Exit `1` indicates failure or uncertainty. Inspect the structured code before retrying.
- Exit `2` indicates invalid parameters, configuration or unavailable optional dependencies.
- Exit `3` indicates an offline or unauthorized device.
- Exit `4` requires human intervention.
- Exit `5` requires cooling the device before further work.
- Exit `6` indicates a busy device. Wait for its current operation to finish.

For plugin development, use the public `actions`, `device`, `ui` and optional `vision` APIs. Keep application-specific business logic in an external plugin. The installed schema and plugin descriptions define accepted inputs.
