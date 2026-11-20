"""Local package operations sharing the normal inspect/submit/confirm ledger."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from moyu_harmony_pilot.actions.models import (
    ActionOutcome,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.device import DeviceConfigurationError
from moyu_harmony_pilot.device.packages import hap_metadata

PACKAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "sha256"],
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
    },
}
APP_METRICS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "bundle_name": {"type": "string", "maxLength": 128},
        "version_code": {"type": ["integer", "null"]},
        "installed": {"type": "boolean"},
        "previous_version_code": {"type": ["integer", "null"]},
    },
}


class AppInstall(IdempotentAction):
    spec = ActionSpec(
        "app.install",
        "1.0",
        "Install or update local HAPs and verify the expected version.",
        side_effect="mutating",
        local_only=True,
        timeout_seconds=300,
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["bundle_name", "version_code", "packages"],
            "properties": {
                "bundle_name": {"type": "string", "minLength": 1, "maxLength": 128},
                "version_code": {"type": "integer", "minimum": 0},
                "packages": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": PACKAGE_SCHEMA,
                },
            },
        },
        metrics_schema=APP_METRICS,
    )

    def inspect(self, ctx, params):
        for package in params["packages"]:
            metadata = hap_metadata(package["path"])
            if (
                metadata["sha256"] != package["sha256"]
                or metadata["bundle_name"] != params["bundle_name"]
                or metadata["version_code"] != params["version_code"]
            ):
                raise DeviceConfigurationError("Package identity changed or HAP versions differ.")
        # Even an equal version may contain a new build. The idempotency key controls replay.
        info = ctx.device.app_info(params["bundle_name"], timeout=ctx.remaining_timeout)
        if info is not None and info.version_code is None:
            raise DeviceConfigurationError("Installed bundle version is unavailable.")
        return Inspection(metrics={"previous_version_code": info.version_code if info else None})

    def submit(self, ctx, inspection, params):
        with tempfile.TemporaryDirectory(prefix="moyu-install-") as directory:
            staged = []
            for index, package in enumerate(params["packages"]):
                path = Path(directory) / f"module-{index}.hap"
                shutil.copyfile(package["path"], path)
                path.chmod(0o600)
                metadata = hap_metadata(path)
                if metadata["sha256"] != package["sha256"]:
                    raise DeviceConfigurationError("Package changed before submission.")
                staged.append(path)
            ctx.device.install_app(staged, timeout=ctx.remaining_timeout)

    def confirm(self, ctx, params):
        info = ctx.device.app_info(params["bundle_name"], timeout=ctx.remaining_timeout)
        confirmed = info is not None and info.version_code == params["version_code"]
        if ctx.reconciling and (
            "previous_version_code" not in ctx.inspection_metrics
            or ctx.inspection_metrics["previous_version_code"] == params["version_code"]
        ):
            confirmed = False
        return ActionOutcome(
            status="succeeded" if confirmed else "unknown",
            summary="Expected installed version verified."
            if confirmed
            else "Installation unconfirmed.",
            metrics={
                "bundle_name": params["bundle_name"],
                "installed": info is not None,
                "version_code": info.version_code if info else None,
            },
        )


class AppUninstall(IdempotentAction):
    spec = ActionSpec(
        "app.uninstall",
        "1.0",
        "Remove a bundle and verify its absence.",
        side_effect="mutating",
        local_only=True,
        timeout_seconds=180,
        params_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["bundle_name"],
            "properties": {"bundle_name": {"type": "string", "minLength": 1, "maxLength": 128}},
        },
        metrics_schema=APP_METRICS,
    )

    def inspect(self, ctx, params):
        info = ctx.device.app_info(params["bundle_name"], timeout=ctx.remaining_timeout)
        return Inspection(status="ready" if info else "already_done")

    def submit(self, ctx, inspection, params):
        ctx.device.uninstall_app(params["bundle_name"], timeout=ctx.remaining_timeout)

    def confirm(self, ctx, params):
        installed = params["bundle_name"] in ctx.device.list_apps(timeout=ctx.remaining_timeout)
        return ActionOutcome(
            status="unknown" if installed else "succeeded",
            summary="Removal unconfirmed." if installed else "Bundle absence verified.",
            metrics={"bundle_name": params["bundle_name"], "installed": installed},
        )
