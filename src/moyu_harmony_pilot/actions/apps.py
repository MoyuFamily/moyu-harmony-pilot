"""Local package operations sharing the normal inspect/submit/confirm ledger."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from moyu_harmony_pilot.actions.models import (
    ActionContext,
    ActionOutcome,
    ActionSpec,
    IdempotentAction,
    Inspection,
)
from moyu_harmony_pilot.device import DeviceConfigurationError
from moyu_harmony_pilot.device.packages import (
    MAX_EXPANDED_BYTES,
    MAX_INSTALL_MODULES,
    package_metadata,
)

PACKAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "sha256"],
    "properties": {
        "path": {
            "type": "string", "minLength": 1, "maxLength": 4096,
            "description": "Original local .hap, application-internal .hsp, or .app path.",
        },
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


APP_INSTALL_INSPECTION_METRICS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["previous_version_code"],
    "properties": {
        "previous_version_code": {"type": ["integer", "null"]},
    },
}


class AppInstall(IdempotentAction):
    spec = ActionSpec(
        "app.install",
        "1.0",
        "Install or update local HAP/HSP/APP packages and verify the expected version.",
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
        inspection_metrics_schema=APP_INSTALL_INSPECTION_METRICS,
        min_pilot_version="0.2.0.dev0",
    )

    def inspect(self, ctx: ActionContext, params: dict[str, Any]) -> Inspection:
        module_count = 0
        expanded_bytes = 0
        for package in params["packages"]:
            metadata = package_metadata(package["path"])
            module_count += metadata.get("module_count", 1)
            expanded_bytes += metadata.get("expanded_bytes", 0)
            if (
                metadata["sha256"] != package["sha256"]
                or metadata["bundle_name"] != params["bundle_name"]
                or metadata["version_code"] != params["version_code"]
            ):
                raise DeviceConfigurationError("Package identity changed or versions differ.")
        if module_count > MAX_INSTALL_MODULES:
            raise DeviceConfigurationError("Installation exceeds 32 HAP/HSP modules.")
        if expanded_bytes > MAX_EXPANDED_BYTES:
            raise DeviceConfigurationError("Expanded APP modules exceed 512 MiB.")
        # Even an equal version may contain a new build. The idempotency key controls replay.
        info = ctx.device.app_info(params["bundle_name"], timeout=ctx.remaining_timeout)
        if info is not None and info.version_code is None:
            raise DeviceConfigurationError("Installed bundle version is unavailable.")
        return Inspection(metrics={"previous_version_code": info.version_code if info else None})

    def submit(
        self, ctx: ActionContext, inspection: Inspection, params: dict[str, Any]
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="moyu-install-") as directory:
            staged = []
            for index, package in enumerate(params["packages"]):
                suffix = Path(package["path"]).suffix.lower()
                path = Path(directory) / f"package-{index}{suffix}"
                shutil.copyfile(package["path"], path)
                path.chmod(0o600)
                metadata = package_metadata(path)
                if (
                    metadata["sha256"] != package["sha256"]
                    or metadata["bundle_name"] != params["bundle_name"]
                    or metadata["version_code"] != params["version_code"]
                ):
                    raise DeviceConfigurationError("Package changed before submission.")
                staged.append(path)
            ctx.device.install_app(staged, timeout=ctx.remaining_timeout)

    def confirm(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
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

    def inspect(self, ctx: ActionContext, params: dict[str, Any]) -> Inspection:
        info = ctx.device.app_info(params["bundle_name"], timeout=ctx.remaining_timeout)
        return Inspection(status="ready" if info else "already_done")

    def submit(
        self, ctx: ActionContext, inspection: Inspection, params: dict[str, Any]
    ) -> None:
        ctx.device.uninstall_app(params["bundle_name"], timeout=ctx.remaining_timeout)

    def confirm(self, ctx: ActionContext, params: dict[str, Any]) -> ActionOutcome:
        installed = params["bundle_name"] in ctx.device.list_apps(timeout=ctx.remaining_timeout)
        return ActionOutcome(
            status="unknown" if installed else "succeeded",
            summary="Removal unconfirmed." if installed else "Bundle absence verified.",
            metrics={"bundle_name": params["bundle_name"], "installed": installed},
        )
