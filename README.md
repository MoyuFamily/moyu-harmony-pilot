# Moyu Harmony Pilot (`moyu-harmony-pilot`)

> **Production-grade, non-intrusive HarmonyOS NEXT (5 / 6+) Python automation engine and secure pull-based task bridge.**

`moyu-harmony-pilot` is an independent, general-purpose automation framework built exclusively for **HarmonyOS NEXT (API 12+)** physical devices. It drives real devices through Huawei's official `hdc` and native `uitest` toolchains with **zero on-device agent installation**, and optionally connects behind NAT to any compatible remote task broker via a server-agnostic HTTPS pull protocol.

---

## Key Design Principles

- **HarmonyOS NEXT Native Only**: Focused exclusively on HarmonyOS 5 / 6+ (`hdc`, `uitest`, `aa`, `hidumper`, `power-shell`, `snapshot_display`), without Android/ADB legacy baggage.
- **Zero On-Device Intrusion**: Does not require root, unlocked bootloaders, custom test HAP installation, or fragile reverse-engineered private daemon socket protocols.
- **Standalone & Ecosystem-Independent**: Works completely standalone via CLI/Python SDK. Remote orchestration systems are ordinary external consumers implementing a standard HTTP pull protocol.
- **Fail-Closed & Plugin-Driven**: Business workflows live in external or allowlisted `ActionPlugin` implementations. Remote dispatchers can only trigger explicitly allowlisted actions—never arbitrary shell commands.
- **Single-Repo Modular Packages**: Clean one-way layered Python subpackages (`hdc` → `ui` → `device` / `vision` → `actions` → `bridge` / `cli`) designed so the core driver SDK can be extracted cleanly in the future if needed.

---

## Documentation

- **[Requirements & Architecture Specification](docs/requirements.md)** — Scope, non-goals, responsibility boundaries, layered package design, plugin model, security invariants, and roadmap.
- **[Local Environment & Device Setup Guide](docs/device-setup.md)** — Official `hdc` installation (macOS ARM64 / Linux), Python 3.11 environment setup, HarmonyOS NEXT device prerequisites, read-only verification commands, and USB troubleshooting.
- **[Pull Bridge Protocol Specification (v1 Draft)](docs/bridge-protocol.md)** — Server-agnostic HTTPS pull protocol (`heartbeat` / `claim` / `lease` / `complete`) for integrating any external scheduler or control plane.
- **[Security Policy](SECURITY.md)** — Secret handling, local-only diagnostics, and fail-closed execution rules.

---

## Current Status

`M0` — Repository initialized and specification documents landed. Core driver, plugin framework, and bridge worker implementation will proceed incrementally according to [docs/requirements.md](docs/requirements.md).

---

## License

Released under the [MIT License](LICENSE).
