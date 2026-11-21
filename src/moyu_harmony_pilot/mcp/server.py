"""MCP v2 stdio registration. All device logic lives in the shared core."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.device import (
    DeviceConfigurationError,
    DeviceError,
    DeviceRegistry,
    check_config_permissions,
)
from moyu_harmony_pilot.hdc import HdcClient, HdcError
from moyu_harmony_pilot.mcp.adapter import DEFAULT_MAX_IMAGE_BYTES, MAX_IMAGE_BYTES, PilotTools
from moyu_harmony_pilot.ui import UiError
from moyu_harmony_pilot.vision import VisionError

if TYPE_CHECKING:
    from mcp.server import MCPServer


def create_server(adapter: PilotTools) -> "MCPServer":
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver import Image
        from mcp.types import CallToolResult, ContentBlock, TextContent, ToolAnnotations
        from pydantic import Field
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
    readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
    mutating = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)

    def invoke(
        callback: Callable[[], dict[str, Any]], *, inline_image: bool = False,
    ) -> CallToolResult:
        image_path: Path | None = None
        try:
            with redirect_stdout(sys.stderr):
                data = callback()
                picture = None
                if inline_image:
                    image_path = Path(data["path"])
                    with image_path.open("rb") as stream:
                        image_data = stream.read(adapter.max_image_bytes + 1)
                    if not 0 < len(image_data) <= adapter.max_image_bytes:
                        raise DeviceError(
                            "Screenshot exceeds the inline image byte limit. Reduce max_width.",
                            code="image_too_large",
                        )
                    picture = Image(data=image_data, format="png").to_image_content()
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
            content: list[ContentBlock] = [TextContent(type="text", text=encoded)]
            if picture is not None:
                content.append(picture)
        except BaseException as exc:
            if image_path is not None:
                image_path.unlink(missing_ok=True)
            if not isinstance(exc, Exception):
                raise
            code = "internal_error"
            message = "Tool execution stopped."
            hint = None
            retryable = False
            if isinstance(exc, (DeviceError, UiError, HdcError, VisionError)):
                code, message, hint, retryable = exc.code, str(exc), exc.hint, exc.retryable
            if isinstance(exc, HdcError) and exc.result is not None:
                # Driver messages can embed raw subprocess output.
                message = f"hdc operation failed ({exc.code})."
            payload = {
                "ok": False,
                "data": None,
                "error": {
                    "code": code,
                    "message": message,
                    "hint": hint,
                    "retryable": retryable,
                },
            }
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            content = [TextContent(type="text", text=encoded)]
        return CallToolResult(
            content=content,
            structured_content=payload,
            is_error=not payload["ok"],
        )

    @server.tool(annotations=readonly)
    async def devices() -> CallToolResult:
        """List configured device aliases, connection states and lock state."""
        return invoke(adapter.list_devices)

    @server.tool(annotations=readonly)
    async def health(alias: str) -> CallToolResult:
        """Read bounded battery, charging, temperature and power telemetry."""
        return invoke(lambda: adapter.health(alias))

    @server.tool(annotations=readonly)
    async def dump_ui(alias: str) -> CallToolResult:
        """Return a compact local snapshot with references, layout hash and sensitive signals."""
        return invoke(lambda: adapter.dump_ui(alias))

    @server.tool(
        annotations=readonly,
        meta={
            "inline_images": adapter.inline_images,
            "max_image_bytes": adapter.max_image_bytes,
        },
    )
    async def screenshot(
        alias: str,
        max_width: Annotated[int, Field(ge=1, le=8192, strict=True)] = 1280,
        som: bool = False,
        inline: Annotated[bool, Field(strict=True)] = False,
    ) -> CallToolResult:
        """Capture file metadata and optional SoM refs. Inline images require --inline-images.

        max_width is 1..8192 pixels. inline=true checks sensitive screens before and after
        capture, then returns ImageContent within the server's max_image_bytes limit.
        """
        return invoke(
            lambda: adapter.screenshot(alias, max_width=max_width, som=som, inline=inline),
            inline_image=inline,
        )

    if adapter.enable_ocr:

        @server.tool(annotations=readonly)
        async def ocr(
            alias: str,
            backend: Literal["auto", "macos", "rapidocr"] = "auto",
            max_width: Annotated[int, Field(ge=1, le=8192, strict=True)] = 1280,
            timeout: Annotated[float, Field(gt=0, le=3600, strict=True)] = 30.0,
        ) -> CallToolResult:
            """Read local OCR text, confidence and bounds in image pixels with source scale.

            backend is auto, macos or rapidocr. max_width is 1..8192, timeout is (0, 3600].
            At most 1000 boxes and 32768 text characters are returned, with a truncated flag.
            Sensitive content requires human takeover. Temporary images are removed.
            """
            return invoke(
                lambda: adapter.ocr(alias, backend=backend, max_width=max_width, timeout=timeout)
            )

    @server.tool(annotations=readonly)
    async def list_actions() -> CallToolResult:
        """Describe explicitly allowed actions, parameter schemas and side-effect levels."""
        return invoke(adapter.list_actions)

    @server.tool(annotations=mutating)
    async def run_action(
        alias: str,
        action_id: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> CallToolResult:
        """Run an allowlisted action through the shared lock, ledger and safety gates."""
        return invoke(
            lambda: adapter.run_action(
                alias, action_id, params, idempotency_key=idempotency_key, timeout=timeout
            )
        )

    if adapter.local_lab:

        @server.tool(annotations=readonly)
        async def lab_observe(alias: str) -> CallToolResult:
            """Observe through the fixed-budget local interactive session."""
            return invoke(lambda: adapter.lab_observe(alias))

        @server.tool(annotations=mutating)
        async def click_ref(alias: str, ref: str, layout_hash: str) -> CallToolResult:
            """Click a fresh enabled reference and return layout/focus differences."""
            return invoke(lambda: adapter.click_ref(alias, ref, layout_hash))

        @server.tool(annotations=mutating)
        async def swipe(alias: str, direction: str, layout_hash: str) -> CallToolResult:
            """Perform a bounded directional swipe against the observed layout hash."""
            return invoke(lambda: adapter.swipe(alias, direction, layout_hash))

        @server.tool(annotations=mutating)
        async def back(alias: str, layout_hash: str) -> CallToolResult:
            """Navigate back with sensitive-screen checks and differential feedback."""
            return invoke(lambda: adapter.back(alias, layout_hash))

        @server.tool(annotations=mutating)
        async def home(alias: str, layout_hash: str) -> CallToolResult:
            """Return to the home screen through the fixed-budget local session."""
            return invoke(lambda: adapter.home(alias, layout_hash))

        @server.tool(annotations=mutating)
        async def long_click_ref(alias: str, ref: str, layout_hash: str) -> CallToolResult:
            """Long press a fresh reference supporting long press."""
            return invoke(lambda: adapter.long_click_ref(alias, ref, layout_hash))

        @server.tool(annotations=mutating)
        async def input_text_ref(
            alias: str, ref: str, text: str, layout_hash: str,
        ) -> CallToolResult:
            """Input single-line text into a fresh non-sensitive editable reference."""
            return invoke(lambda: adapter.input_text_ref(alias, ref, text, layout_hash))

        @server.tool(annotations=mutating)
        async def start_app(
            alias: str, bundle_name: str, layout_hash: str, ability_name: str | None = None
        ) -> CallToolResult:
            """Start a bundle with an explicit or discovered entry ability."""
            return invoke(lambda: adapter.start_app(alias, bundle_name, layout_hash, ability_name))

        @server.tool(annotations=readonly)
        async def wait_for(alias: str, selector: dict[str, Any]) -> CallToolResult:
            """Wait for a selector within the remaining session budget."""
            return invoke(lambda: adapter.wait_for(alias, selector))

        @server.tool(annotations=mutating)
        async def scroll_until(
            alias: str, selector: dict[str, Any], direction: str = "up", max_swipes: int = 10
        ) -> CallToolResult:
            """Scroll a bounded number of times, observing and checking guards on every step."""
            return invoke(lambda: adapter.scroll_until(alias, selector, direction, max_swipes))

    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harmony-pilot-mcp")
    parser.add_argument("--config", help="Private local device alias configuration.")
    parser.add_argument("--hdc-path")
    parser.add_argument("--state-dir", default=".state")
    parser.add_argument("--allow-action", action="append", default=[])
    parser.add_argument("--local-lab", action="store_true")
    parser.add_argument(
        "--inline-images", action="store_true",
        help="Allow screenshot(inline=true) to return bounded MCP ImageContent.",
    )
    parser.add_argument(
        "--max-image-bytes", type=int, default=DEFAULT_MAX_IMAGE_BYTES,
        help=f"Inline image byte limit, 1..{MAX_IMAGE_BYTES} (default {DEFAULT_MAX_IMAGE_BYTES}).",
    )
    parser.add_argument(
        "--enable-ocr", action="store_true",
        help="Expose the independent read-only local OCR tool.",
    )
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
                inline_images=args.inline_images,
                max_image_bytes=args.max_image_bytes,
                enable_ocr=args.enable_ocr,
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
