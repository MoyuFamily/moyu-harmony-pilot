# Moyu Harmony Pilot (`moyu-harmony-pilot`)

> **Production-grade, non-intrusive HarmonyOS NEXT (5 / 6+) Python automation engine and secure pull-based task bridge.**

`moyu-harmony-pilot` is an independent, general-purpose automation framework built exclusively for **HarmonyOS NEXT (API 12+)** physical devices. It drives real devices through Huawei's official `hdc` and native `uitest` toolchains with **zero on-device agent installation**, and optionally connects behind NAT to any compatible remote task broker via a server-agnostic HTTPS pull protocol.

---

## Key Design Principles

- **HarmonyOS NEXT Native Only**: Focused exclusively on HarmonyOS 5 / 6+ (`hdc`, `uitest`, `aa`, `hidumper`, `power-shell`, `snapshot_display`), without Android/ADB legacy baggage.
- **Zero On-Device Intrusion**: Does not require root, unlocked bootloaders, custom test HAP installation, or fragile reverse-engineered private daemon socket protocols.
- **Zero Core Heavy Dependencies**: Default installation depends exclusively on the Python 3.11+ standard library.
- **Standalone & Ecosystem-Independent**: Works completely standalone via CLI/Python SDK. Remote orchestration systems are ordinary external consumers implementing a standard HTTP pull protocol.
- **Fail-Closed & Plugin-Driven**: Business workflows live in external or allowlisted `ActionPlugin` implementations. Remote dispatchers can only trigger explicitly allowlisted actions—never arbitrary shell commands.
- **Agent-Friendly First**: Machine-first CLI with deterministic JSON envelopes and semantic exit codes (`0`–`6`), token-efficient compact UI snapshots (`@e1`/`@e2` refs + `layout_hash`), self-describing JSON schemas (`harmony-pilot schema`), built-in safety guardrails, repository `AGENTS.md`, standard Agent Skill, and an optional thin MCP adapter.
- **Single-Repo Modular Packages**: Clean one-way layered Python subpackages (`hdc` → `ui` → `device` / `vision` → `actions` → `bridge` / `cli` / optional `mcp`) enforced by automated DAG architecture tests.

---

## Quick Start (M1 Core Driver & Agent-Ready CLI)

```bash
# 1. Create Python 3.11+ environment and install editable package
uv venv --python python3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 2. (Optional) Configure local device alias isolation (ignored by Git)
cp devices.example.yaml devices.local.yaml
chmod 600 devices.local.yaml

# 3. Inspect CLI contract offline (no device required)
.venv/bin/harmony-pilot schema --json

# 4. Inspect connected HarmonyOS NEXT targets and host environment
.venv/bin/harmony-pilot devices --json
.venv/bin/harmony-pilot doctor --json

# 5. Export a token-efficient compact UI snapshot (@e1, @e2, ...) and screenshot
.venv/bin/harmony-pilot dump-ui --device harmony-01 --compact --json
.venv/bin/harmony-pilot screenshot --device harmony-01 --out .state/diagnostics/screen.jpeg --json
```

See [docs/device-setup.md](docs/device-setup.md) for installing official `hdc 3.x` and enabling USB debugging on HarmonyOS NEXT devices.

---

## Documentation

- **[Requirements & Architecture Specification](docs/requirements.md)** — Scope, non-goals, responsibility boundaries, layered package design, plugin model, Agent-friendly contracts, security invariants, and roadmap.
- **[Local Environment & Device Setup Guide](docs/device-setup.md)** — Official `hdc` installation (macOS ARM64 / Linux), Python 3.11 environment setup, HarmonyOS NEXT device prerequisites, read-only verification commands, and USB troubleshooting.
- **[Action Plugins & Local Execution](docs/actions.md)** — Plugin discovery, parameter and receipt schemas, execution budgets, SQLite deduplication, and an external read-only example.
- **[Pull Bridge Protocol Specification (v1 Draft)](docs/bridge-protocol.md)** — Server-agnostic HTTPS pull protocol (`heartbeat` / `claim` / `lease` / `complete`) for integrating any external scheduler or control plane.
- **[AI Agent Guide](AGENTS.md)** — Architecture DAG, verification commands, testing boundaries, and security guardrails for AI coding agents working in this repository.
- **[Security Policy](SECURITY.md)** — Secret handling, local-only diagnostics, and fail-closed execution rules.

---

## Current Status

- **`M0`** ✅ — Repository initialized and specification documents landed.
- **`M1`** ✅ — Core zero-dependency driver (`hdc`, `ui` with `@eN` compact semantic snapshots & `layout_hash`, `device` with alias isolation, exclusive locks, `BatteryService`/`PowerManagerService` telemetry & thermal guardrails), Agent-ready CLI (`devices`, `doctor`, `dump-ui`, `screenshot`, `schema`), and full offline CI test suite.
- **`M2`** ✅ — Plugin execution, SQLite deduplication, external discovery, CLI introspection, optional macOS Vision / RapidOCR adapters, `--som` screenshot annotation, explicit popup rules and budgeted local interaction with fresh-reference checks and difference feedback. Offline tests cover the adapters and guardrails.
- **`M3`** ⏳ — Outbound Pull Bridge worker (`serve`), protocol conformance suite, optional `[mcp]` thin adapter, and `skills/harmony-pilot/SKILL.md`.

---

## License

Released under the [MIT License](LICENSE).
