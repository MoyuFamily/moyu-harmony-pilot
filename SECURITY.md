# Security Policy

## Reporting a vulnerability

Please do not include live bearer tokens, raw device serial numbers, screenshots, UI layout dumps containing personal or account data, private endpoint URLs, or proprietary plugin code in a public GitHub Issue or Pull Request.

If a report requires sensitive evidence, contact the repository maintainers through a private channel first and share only the minimal redacted reproduction details.

## Sensitive material boundary

Treat the following as sensitive and local-only:

- Bridge / API authentication tokens (e.g. `HARMONY_PILOT_BRIDGE_TOKEN`)
- Raw HarmonyOS device serial numbers (`hdc list targets` output)
- Screenshots (`snapshot_display` / `uitest screenCap`) and UI layout JSON dumps (`uitest dumpLayout`)
- Local device mapping files (`devices.local.yaml`, `.env`, `.state/`, `diagnostics/`)
- Third-party or private action plugins and their runtime credentials

Screenshots and layout dumps frequently expose account names, phone numbers, balances, notifications, or personal identifiers. They must stay in local mode-`0700`/`0600` directories and must never be transmitted over the remote bridge protocol or committed to Git.

## Fail-closed execution boundary

`moyu-harmony-pilot` enforces a strict fail-closed execution boundary when operating in remote Pull Bridge mode:

- Remote task brokers may only invoke explicitly allowlisted action identifiers registered on the local host.
- Arbitrary `hdc shell` execution, arbitrary file transfer, and arbitrary raw coordinate injection must never be exposed through the remote bridge protocol.
- Device identifiers exposed to remote brokers use administrator-defined aliases rather than raw hardware serial numbers.
- Uncertain or timed-out executions transition to `unknown` and are never automatically replayed for side-effecting actions.

## Repository hygiene

Before making a private fork, branch, or repository public, audit the full Git history to verify that no credentials, real device serials, private endpoints, screenshots, or private app plugins were ever committed.

This repository ignores `.env`, `.env.*`, `.state/`, `artifacts/`, `diagnostics/`, `plugins_local/`, and `devices.local.*` by default.
