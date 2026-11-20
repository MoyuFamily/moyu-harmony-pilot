# Running the Pull Bridge

For a host setup walkthrough and an external action example, follow [Cross-machine Integration](remote-integration.md).

## Configuration

Copy `bridge.example.json` to `bridge.local.json` and edit the broker URL, configured aliases and capabilities. Set file mode `0600` on the Bridge config, device mapping and token file. Relative paths resolve against the Bridge config directory.

Credentials load from `HARMONY_PILOT_BRIDGE_TOKEN` or the configured `token_env` name. If the environment value is empty, `token_file` supplies the credential from an owner-only file. Keep runtime files outside version control.

Set `plugin_paths` to an array of external plugin directories containing `actions.json`. Paths resolve against the Bridge config directory, including when a service has a different working directory. An explicit array replaces `HARMONY_PILOT_PLUGIN_PATHS` for this worker. An empty array disables directory discovery. Omitting the field preserves environment-based discovery. Installed Python entry points remain available in every case, subject to the action allowlist.

The broker must implement protocol v1. HTTPS uses certificate validation and rejects redirects. Explicit `http://127.0.0.1` and `http://localhost` endpoints support local integration.

```bash
harmony-pilot serve --config bridge.local.json --check --json
harmony-pilot serve --config bridge.local.json --probe --json
harmony-pilot serve --config bridge.local.json --once --json
harmony-pilot serve --config bridge.local.json --json
harmony-pilot serve --config bridge.local.json --health --json
```

`--check` validates private configuration, alias mappings and allowlisted plugin metadata offline. Its JSON data includes `protocol_version`, `bridge_id`, `device_aliases`, per-device `allowed_actions` and the allowed action descriptions in `actions`, including parameter and metric schemas. This gives Broker implementers the deployed action contract. Plugin imports and constructors run during validation and must be free of device interactions.

`--probe` reads device availability and telemetry and sends one Broker heartbeat. It leaves queued jobs and saved receipts for the execution worker. The result contains `broker_reachable` and `devices`. Exit code `0` confirms an acknowledged heartbeat, including when a device is offline. Inspect each device's `connectionState` and `busy` fields separately. A Broker error returns `1`, and a running worker holding the same instance lock returns `6`. A probe creates local state files as needed and updates the acknowledged-heartbeat health file.

`--once` completes one heartbeat/claim cycle and flushes durable receipts. It can execute an allowlisted business action. A normal invocation runs continuously. `--health` reports the freshness of the last acknowledged heartbeat, returning exit code `0` for fresh data or `1` for missing/stale data. Configuration errors use exit code `2`. The four mode flags are mutually exclusive.

## Execution and recovery

Each worker claims at most one job at a time. Devices retain independent physical-device locks, and separate workers can operate different devices concurrently. A broker/bridge-ID instance lock prevents two local processes from recovering or dispatching the same worker state concurrently.

Each configured device advertises its own explicit capabilities. External discovery filters entry points before importing plugin code. Local-only actions are rejected during configuration validation. Claim fields, action parameters, timestamps and lease deadlines are validated before execution.

The worker renews leases in the background and sends busy heartbeats during long jobs. Heartbeat failures are logged independently of lease renewal. A rejected, expired or canceled lease stops preparation and inspection at the next deadline/checkpoint check. After the action ledger commits its `submitting` reservation, submission and confirmation can finish within their original budgets and the job expiration deadline. Execution failures and inconclusive confirmation produce `unknown`.

Idle cycles refresh device connectivity and lock state. Battery and power telemetry are sampled when a heartbeat is due. Long-poll time counts toward the recommended polling interval, and pending-job hints trigger immediate claims. Heartbeat state and health-file updates are serialized. Worker shutdown allows both in-flight lease and heartbeat requests to finish.

Job reservations and final receipts live in an owner-only SQLite outbox. Receipts are saved before HTTP completion, remain immutable during retries, and survive worker restarts. Unfinished jobs recover as `unknown`. Repeated jobs resend their recorded receipt. Completion conflicts (`409` or `410`) close local delivery while preserving the recorded result.

`SIGTERM` and `SIGINT` stop claiming work and request cancellation through the shared guard. An action with a committed `submitting` reservation reaches confirmation within its remaining budget. The worker then finishes local ledger/outbox handling and attempts final receipt delivery. Pending receipts are delivered on the next start. Network failures use capped exponential backoff with jitter. Authentication and protocol failures use a longer retry interval and sanitized stderr warnings.

Keep the state directory across upgrades and restarts. The action ledger protects physical-device/action/idempotency-key identity across workers. The receipt outbox is scoped to broker URL and Bridge ID.

Remote completion summaries are generated from the status. Metrics pass the declared action schema and receipt limits, with known credentials and hardware serials removed. Plugin authors remain responsible for declaring privacy-safe business metrics. UI snapshots, screenshots, arbitrary shell commands and arbitrary file operations stay outside the Bridge dispatch surface.

## Service installation

The templates under `deploy/systemd` and `deploy/launchd` use the same `serve` entry point. Replace installation paths, service account and configuration paths for the host. Give that account access to the official hdc executable and already-authorized devices. Prepare the state directory with mode `0700` and validate the configuration before enabling the service.

For systemd, install the unit under the host service directory and use the host service manager to enable/start it. For launchd, replace every `/ABSOLUTE/PATH` value, choose a unique label and load the plist as the user who owns the device connection. Token files use mode `0600`. Service shutdown allows up to 90 seconds for bounded network calls and local cleanup.
