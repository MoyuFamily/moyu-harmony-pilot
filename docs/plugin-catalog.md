# Plugin directory and maintenance

Pilot maintains the action contract, generic examples and compatibility tooling.
External maintainers own their plugin source, package releases and business logic.
This directory records discovery and deployment information that users can review
before installing a plugin on their Pilot host.

## Available examples

The following examples are maintained by MoyuFamily under the repository's MIT
license. All implement action API v1 and run in Pilot's offline CI on macOS/Linux
with Python 3.11/3.12. Their tested Pilot revision is the commit of the CI run.

| Action | Source and deployment | Pilot requirement | Contract coverage |
| --- | --- | --- | --- |
| `example.battery_check` | [Standalone package template](../examples/plugin-template/README.md), installed through its entry point | `>=0.1.0.dev1,<0.2` | Built wheel discovery, parameter validation, remote allowlist and read-only device calls |
| `example.health_check` | [Bridge example](../examples/bridge/plugins/health_check.py), directory with `actions.json` | Current checkout, API v1 | Fake-device execution through the native Bridge and local HTTP Broker |
| `example.ui_summary` | [Local example](../examples/actions/ui_summary.py), directory with `actions.json` | Current checkout, API v1 | Fake-device discovery, local execution and compact results |

The package template contains a separate pinned Pilot revision for its standalone
CI. Its dependency range declares the intended release compatibility, and each
successful CI run records a tested revision. Device and application acceptance is
recorded separately by plugin maintainers.

## Register a community plugin

Submit a PR adding an entry to this document with the following information.

- Plugin name, distribution name, action IDs and a concise purpose.
- Source repository, installation instructions and maintainer contact.
- License and release tag or full revision used for installation.
- Action API version, Pilot dependency range, Python and host requirements.
- Side-effect classification and any local-only actions.
- Links to offline contract results for each claimed Pilot version.
- Device/application acceptance status, including versions tested and known limits.

Directory reviews check that this information is complete and matches the plugin's
declarations. The deployment owner reviews the code and dependencies before
installation, because plugins execute with the Pilot process's permissions.
Business implementations remain in their external repositories.

## Develop and validate

Start from the [plugin template](../examples/plugin-template/README.md) and use the
[action development contract](actions.md). The template's `python -m tests` command
checks the installed entry point and fails if required tests skip or fail. Its
synthetic mutation scenarios provide patterns for testing durable reservation,
confirmed completion and uncertain results. Adapt those cases to the real plugin
and its offline device fixtures.

Keep `actions.json` as an action-to-file/class mapping for directory plugins.
Package plugins use the standard `moyu_harmony_pilot.actions` entry-point group.
Compatibility metadata lives in `ActionSpec` and the package's `pyproject.toml`,
as described in the action guide. Directory plugins document their package and
Python requirements beside the manifest.

Before expanding a supported Pilot version range, run the plugin's contracts
against that release or development revision. A release record should retain the
plugin revision, Pilot revision, host/Python matrix and test result links.

## Deploy, update and retire

1. Install a reviewed plugin release into the service's Python environment, or
   deploy its reviewed directory revision and configure `plugin_paths`.
2. Run `plugins describe` for package or environment-discovered plugins. For a
   Bridge deployment, run `serve --check` using its private configuration and
   review the exported action IDs, API versions and schemas.
3. Add the selected actions to the target devices' allowlists. Restart the
   service to load the installed code and configuration.
4. During upgrades, let active work complete, preserve the shared state directory
   and run the contract suite against the new plugin/Pilot pair. Changes to action
   `version` or parameters affect existing idempotency keys. Reconcile outstanding
   `unknown` work before choosing new keys.
5. To roll back, restore the previous package or directory revision together with
   its configuration, retain the ledger and re-run preflight.
6. To retire a plugin, remove its remote authorization, let active work settle,
   restart the service, and remove its installed package or discovery path. Retain
   the ledger according to the deployment's state retention policy. Update its
   directory entry with maintenance status and a replacement when available.

Repository tags or fixed revisions identify source deployments. Version-pinned
packages and wheel hashes can identify release deployments. Pilot's package
metadata and tests remain independent of community plugins listed here.
