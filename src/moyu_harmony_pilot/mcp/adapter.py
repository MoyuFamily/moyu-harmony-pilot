"""Thin local tool facade with explicit action and interaction authorization."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from moyu_harmony_pilot.actions import ActionRegistry, ActionRunner, InteractionSession
from moyu_harmony_pilot.actions.observation import capture_screenshot
from moyu_harmony_pilot.device import DeviceConfigurationError, DeviceRegistry, HarmonyDevice
from moyu_harmony_pilot.hdc import ConnectionState, HdcClient


class PilotTools:
    def __init__(
        self,
        hdc: HdcClient,
        devices: DeviceRegistry,
        actions: ActionRegistry,
        *,
        state_dir: str | Path = ".state",
        local_lab: bool = False,
        max_steps: int = 20,
        timeout: float = 60,
    ) -> None:
        InteractionSession.validate_budget(max_steps, timeout)
        self.hdc = hdc
        self.devices = devices
        self.actions = actions
        self.state_dir = Path(state_dir)
        self.local_lab = local_lab
        self.max_steps = max_steps
        self.timeout = timeout
        self.runner = ActionRunner(actions, state_dir=state_dir)
        self._sessions: dict[str, InteractionSession] = {}
        for action_id in actions.allowed_actions:
            actions.get(action_id, remote=True)

    def _device(self, alias: str) -> HarmonyDevice:
        profile = self.devices.get_by_alias(alias)
        if profile is None:
            raise DeviceConfigurationError("Choose an alias from the configured device list.")
        self.devices.resolve(alias, self.hdc.list_targets(), require_online=True)
        return HarmonyDevice(self.hdc, profile, state_dir=self.state_dir)

    def list_devices(self) -> dict:
        targets = {target.serial: target for target in self.hdc.list_targets()}
        rows = []
        for profile in self.devices.profiles:
            target = targets.get(profile.serial)
            state = target.connection_state if target else ConnectionState.OFFLINE
            device = HarmonyDevice(self.hdc, profile, state_dir=self.state_dir)
            rows.append(
                {"alias": profile.alias, "connection_state": state.value, "busy": device.is_busy()}
            )
        return {"devices": rows}

    def health(self, alias: str) -> dict:
        device = self._device(alias)
        with device.acquire_lock():
            return device.get_health().to_dict()

    def dump_ui(self, alias: str) -> dict:
        device = self._device(alias)
        with device.acquire_lock():
            _, snapshot = device.dump_ui_compact()
        return {"alias": alias, "snapshot": snapshot.to_dict()}

    def screenshot(self, alias: str, *, max_width: int = 1280, som: bool = False) -> dict:
        if type(max_width) is not int or not 1 <= max_width <= 8192:
            raise DeviceConfigurationError("max_width must be an integer between 1 and 8192.")
        device = self._device(alias)
        directory = device.diagnostics.ensure_ready()
        fd, filename = tempfile.mkstemp(prefix="mcp-screen-", suffix=".png", dir=directory)
        os.close(fd)
        try:
            result, snapshot = capture_screenshot(
                device,
                filename,
                image_format="png",
                max_width=max_width,
                som=som,
            )
            device.diagnostics.prune()
            payload = result.to_dict()
            if snapshot is not None:
                payload["snapshot"] = snapshot.to_dict()
            return payload
        except BaseException:
            Path(filename).unlink(missing_ok=True)
            raise

    def list_actions(self) -> dict:
        return {
            "actions": [
                self.actions.describe(action_id)
                for action_id in sorted(self.actions.allowed_actions)
            ]
        }

    def run_action(
        self,
        alias: str,
        action_id: str,
        params: dict,
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        self.runner.prepare(
            action_id, params, remote=True, idempotency_key=idempotency_key, timeout=timeout
        )
        device = self._device(alias)
        outcome = self.runner.run(
            device, action_id, params, remote=True, idempotency_key=idempotency_key, timeout=timeout
        )
        return {"alias": alias, "action": action_id, **outcome.to_dict()}

    def _session(self, alias: str) -> InteractionSession:
        if not self.local_lab:
            raise DeviceConfigurationError(
                "Interactive tools require explicit --local-lab startup."
            )
        if alias not in self._sessions:
            self._sessions[alias] = InteractionSession(
                self._device(alias),
                max_steps=self.max_steps,
                timeout=self.timeout,
            )
        return self._sessions[alias]

    def lab_observe(self, alias: str) -> dict:
        return {"alias": alias, "snapshot": self._session(alias).observe().to_dict()}

    def click_ref(self, alias: str, ref: str, layout_hash: str) -> dict:
        return self._session(alias).click_ref(ref, layout_hash=layout_hash)

    def swipe(self, alias: str, direction: str, layout_hash: str) -> dict:
        return self._session(alias).swipe(direction, layout_hash=layout_hash)

    def back(self, alias: str, layout_hash: str) -> dict:
        return self._session(alias).back(layout_hash=layout_hash)

    def home(self, alias: str, layout_hash: str) -> dict:
        return self._session(alias).home(layout_hash=layout_hash)

    def long_click_ref(self, alias: str, ref: str, layout_hash: str) -> dict:
        return self._session(alias).long_click_ref(ref, layout_hash=layout_hash)

    def input_text_ref(self, alias: str, ref: str, text: str, layout_hash: str) -> dict:
        return self._session(alias).input_text_ref(ref, text, layout_hash=layout_hash)

    def start_app(
        self, alias: str, bundle_name: str, layout_hash: str, ability_name: str | None = None
    ) -> dict:
        return self._session(alias).start_app(
            bundle_name, ability_name=ability_name, layout_hash=layout_hash
        )

    def wait_for(self, alias: str, selector: dict) -> dict:
        from moyu_harmony_pilot.ui.selector import selector_from_dict

        node = self._session(alias).wait_for(selector_from_dict(selector))
        return {
            "id": node.id, "type": node.type, "text": node.text, "bounds": node.bounds.to_list(),
        }

    def scroll_until(
        self, alias: str, selector: dict, direction: str = "up", max_swipes: int = 10
    ) -> dict:
        from moyu_harmony_pilot.ui.selector import selector_from_dict

        node = self._session(alias).scroll_until(
            selector_from_dict(selector), direction=direction, max_swipes=max_swipes
        )
        return {
            "id": node.id, "type": node.type, "text": node.text, "bounds": node.bounds.to_list(),
        }
