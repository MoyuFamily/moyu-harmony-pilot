"""MCP v2 stdio registration. All device logic lives in the shared core."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from typing import Any

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceRegistry,
    check_config_permissions,
)
from moyu_harmony_pilot.hdc import HdcClient
from moyu_harmony_pilot.mcp.adapter import PilotTools


def create_server(adapter: PilotTools):
    try:
        from mcp.server import MCPServer
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
    except ImportError as exc:
        raise DeviceConfigurationError(
            "Install moyu-harmony-pilot[mcp] to use the MCP adapter."
        ) from exc

    server = MCPServer(
        "harmony-pilot",
        version=__version__,
        log_level="WARNING",
        instructions=(
            "Use configured aliases and inspect action schemas before invocation. "
            "Sensitive screens require human intervention. Preserve idempotency keys "
            "and stop replaying uncertain submissions."
        ),
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    mutating = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

    def invoke(callback: Callable[[], dict]) -> Any:
        try:
            with redirect_stdout(sys.stderr):
                data = callback()
            ok = data.get("status", "succeeded") in {"succeeded", "already_done"}
            error = (
                None
                if ok
                else {
                    "code": data.get("error_code") or data["status"],
                    "message": "Action requires attention.",
                    "retryable": False,
                }
            )
            payload = {"ok": ok, "data": data, "error": error}
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            payload = {
                "ok": False,
                "data": None,
                "error": {
                    "code": getattr(exc, "code", "internal_error"),
                    "message": "Tool execution stopped.",
                    "hint": getattr(exc, "hint", None),
                    "retryable": False,
                },
            }
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return CallToolResult(
            content=[TextContent(type="text", text=encoded)],
            structuredContent=payload,
            isError=not payload["ok"],
        )

    @server.tool(annotations=readonly)
    async def devices() -> Any:
        """List configured device aliases, connection states and lock state."""
        return invoke(adapter.list_devices)

    @server.tool(annotations=readonly)
    async def health(alias: str) -> Any:
        """Read bounded battery, charging, temperature and power telemetry."""
        return invoke(lambda: adapter.health(alias))

    @server.tool(annotations=readonly)
    async def dump_ui(alias: str) -> Any:
        """Return a compact local snapshot with references, layout hash and sensitive signals."""
        return invoke(lambda: adapter.dump_ui(alias))

    @server.tool(annotations=readonly)
    async def screenshot(alias: str, max_width: int = 1280, som: bool = False) -> Any:
        """Capture local diagnostics and return file metadata and optional SoM references."""
        return invoke(lambda: adapter.screenshot(alias, max_width=max_width, som=som))

    @server.tool(annotations=readonly)
    async def list_actions() -> Any:
        """Describe explicitly allowed actions, parameter schemas and side-effect levels."""
        return invoke(adapter.list_actions)

    @server.tool(annotations=mutating)
    async def run_action(
        alias: str,
        action_id: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Run an allowlisted action through the shared lock, ledger and safety gates."""
        return invoke(
            lambda: adapter.run_action(
                alias, action_id, params, idempotency_key=idempotency_key, timeout=timeout
            )
        )

    if adapter.local_lab:

        @server.tool(annotations=readonly)
        async def lab_observe(alias: str) -> Any:
            """Observe through the fixed-budget local interactive session."""
            return invoke(lambda: adapter.lab_observe(alias))

        @server.tool(annotations=mutating)
        async def click_ref(alias: str, ref: str, layout_hash: str) -> Any:
            """Click a fresh enabled reference and return layout/focus differences."""
            return invoke(lambda: adapter.click_ref(alias, ref, layout_hash))

        @server.tool(annotations=mutating)
        async def swipe(alias: str, direction: str, layout_hash: str) -> Any:
            """Perform a bounded directional swipe against the observed layout hash."""
            return invoke(lambda: adapter.swipe(alias, direction, layout_hash))

        @server.tool(annotations=mutating)
        async def back(alias: str, layout_hash: str) -> Any:
            """Navigate back with sensitive-screen checks and differential feedback."""
            return invoke(lambda: adapter.back(alias, layout_hash))

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harmony-pilot-mcp")
    parser.add_argument("--config", help="Private local device alias configuration.")
    parser.add_argument("--hdc-path")
    parser.add_argument("--state-dir", default=".state")
    parser.add_argument("--allow-action", action="append", default=[])
    parser.add_argument("--local-lab", action="store_true")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            devices = DeviceRegistry.load_default(args.config)
            if devices.source_path is None or not check_config_permissions(devices.source_path):
                raise DeviceConfigurationError(
                    "MCP requires an owner-only device alias configuration."
                )
            actions = ActionRegistry.discover(allowed_actions=args.allow_action, remote=True)
            adapter = PilotTools(
                HdcClient(hdc_path=args.hdc_path),
                devices,
                actions,
                state_dir=args.state_dir,
                local_lab=args.local_lab,
                max_steps=args.max_steps,
                timeout=args.timeout,
            )
            server = create_server(adapter)
        server.run(transport="stdio")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        sys.stderr.write(
            "MCP startup/execution failed. Check the optional SDK, config and hdc path.\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
