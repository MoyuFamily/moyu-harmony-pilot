"""High-level HarmonyOS NEXT device controller combining lock, telemetry, app, power, and UI."""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from moyu_harmony_pilot.device.apps import AppInfo, parse_app_info, parse_app_list, validate_bundle
from moyu_harmony_pilot.device.diagnostics import (
    DiagnosticsStore,
    enforce_file_mode_0600,
    read_image_dimensions,
    resize_image_max_width,
)
from moyu_harmony_pilot.device.errors import DeviceConfigurationError, DeviceOverheatedError
from moyu_harmony_pilot.device.lock import DeviceLock, is_device_busy
from moyu_harmony_pilot.device.models import (
    BatteryTelemetry,
    DeviceHealth,
    DeviceInfo,
    DeviceProfile,
    ForegroundWindow,
    PowerTelemetry,
    ScreenshotResult,
)
from moyu_harmony_pilot.device.packages import prepare_install_packages
from moyu_harmony_pilot.device.telemetry import (
    parse_battery_dump,
    parse_foreground_window,
    parse_power_dump,
)
from moyu_harmony_pilot.hdc import ConnectionState, HdcClient, HdcError
from moyu_harmony_pilot.ui import CompactSnapshot, UiDriver, UiTree

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_VALID_IMAGE_FORMATS: dict[str, str] = {
    "jpeg": ".jpeg",
    "jpg": ".jpeg",
    "png": ".png",
}


def _validate_identifier(value: str, *, field_name: str) -> str:
    if not _IDENTIFIER_RE.match(value):
        raise DeviceConfigurationError(f"Invalid {field_name}: {value!r}")
    return value


class HarmonyDevice:
    """High-level controller for one HarmonyOS NEXT physical device."""

    def __init__(
        self,
        hdc: HdcClient,
        profile: DeviceProfile,
        *,
        state_dir: str | Path = ".state",
    ) -> None:
        self._hdc = hdc
        self._profile = profile
        self._state_dir = Path(state_dir)
        self._lock_dir = self._state_dir / "locks"
        self._diagnostics = DiagnosticsStore(self._state_dir / "diagnostics")
        self._ui = UiDriver(hdc, profile.serial)

    @property
    def alias(self) -> str:
        return self._profile.alias

    @property
    def profile(self) -> DeviceProfile:
        return self._profile

    @property
    def masked_serial(self) -> str:
        return self._profile.serial_masked

    @property
    def ui(self) -> UiDriver:
        return self._ui

    @property
    def diagnostics(self) -> DiagnosticsStore:
        return self._diagnostics

    @property
    def lock_dir(self) -> Path:
        return self._lock_dir

    def is_busy(self) -> bool:
        """Return True if another process or thread currently holds this device's lock."""
        return is_device_busy(self._profile.serial, self._lock_dir)

    def acquire_lock(self, *, wait_timeout: float = 0.0) -> AbstractContextManager[DeviceLock]:
        """Return an exclusive per-device lock context manager."""
        return DeviceLock(
            self._profile.serial,
            alias=self._profile.alias,
            lock_dir=self._lock_dir,
            wait_timeout=wait_timeout,
        )

    def get_info(self, *, timeout: float = 10.0) -> DeviceInfo:
        """Fetch read-only HarmonyOS model, OS version, API level, and `uitest` version."""

        def _safe_param(key: str) -> str | None:
            try:
                value = self._hdc.get_param(self._profile.serial, key, timeout=timeout)
                return value or None
            except HdcError:
                return None

        api_raw = _safe_param("const.ohos.apiversion")
        api_version: int | None = None
        if api_raw is not None:
            try:
                api_version = int(api_raw)
            except ValueError:
                api_version = None

        uitest_ver: str | None = None
        try:
            uitest_ver = self._ui.uitest_version(timeout=timeout) or None
        except Exception:
            uitest_ver = None

        return DeviceInfo(
            model=_safe_param("const.product.model"),
            software_version=_safe_param("const.product.software.version"),
            api_version=api_version,
            os_fullname=_safe_param("const.ohos.fullname"),
            uitest_version=uitest_ver,
        )

    def get_battery(self, *, timeout: float = 10.0) -> BatteryTelemetry:
        """Collect battery percentage, charging state, and temperature via `BatteryService`."""
        result = self._hdc.shell(
            self._profile.serial,
            ["hidumper", "-s", "BatteryService", "-a", "-i"],
            timeout=timeout,
        )
        return parse_battery_dump(result.stdout)

    def get_power(self, *, timeout: float = 10.0) -> PowerTelemetry:
        """Collect screen and power state via `PowerManagerService`."""
        result = self._hdc.shell(
            self._profile.serial,
            ["hidumper", "-s", "PowerManagerService", "-a", "-s"],
            timeout=timeout,
        )
        return parse_power_dump(result.stdout)

    def get_health(
        self,
        *,
        include_info: bool = False,
        max_temperature_c: float | None = None,
        timeout: float = 10.0,
    ) -> DeviceHealth:
        """Aggregate battery, power, and thermal protection health telemetry."""
        battery = self.get_battery(timeout=timeout)
        power = self.get_power(timeout=timeout)
        info = self.get_info(timeout=timeout) if include_info else None
        threshold = (
            self._profile.max_temperature_c
            if max_temperature_c is None
            else float(max_temperature_c)
        )
        overheated = battery.temperature_c is not None and battery.temperature_c >= threshold
        return DeviceHealth(
            alias=self._profile.alias,
            connection_state=ConnectionState.ONLINE,
            battery=battery,
            power=power,
            overheated=overheated,
            max_temperature_c=threshold,
            info=info,
        )

    def assert_thermal_safe(
        self,
        *,
        max_temperature_c: float | None = None,
        timeout: float = 10.0,
    ) -> DeviceHealth:
        """Raise `DeviceOverheatedError` if battery temperature meets or exceeds threshold."""
        health = self.get_health(max_temperature_c=max_temperature_c, timeout=timeout)
        if health.overheated:
            raise DeviceOverheatedError(
                f"Device alias {self._profile.alias!r} battery temperature "
                f"{health.battery.temperature_c:.1f}C reached protection threshold "
                f"{health.max_temperature_c:.1f}C."
            )
        return health

    def wakeup(self, *, timeout: float = 10.0) -> None:
        """Wake the screen via official `power-shell wakeup`."""
        self._hdc.shell(self._profile.serial, ["power-shell", "wakeup"], timeout=timeout)

    def suspend(self, *, timeout: float = 10.0) -> None:
        """Turn off / suspend the screen via official `power-shell suspend`."""
        self._hdc.shell(self._profile.serial, ["power-shell", "suspend"], timeout=timeout)

    def unlock_swipe(
        self,
        *,
        screen_size: tuple[int, int] | None = None,
        timeout: float = 15.0,
    ) -> None:
        """Wake and swipe upward to dismiss a no-PIN lock screen.

        By security policy, PIN/passcode entry is intentionally not supported.
        """
        self.wakeup(timeout=timeout)
        width, height = screen_size if screen_size is not None else self._ui.dump_tree().screen_size
        if width <= 0 or height <= 0:
            width, height = 1080, 2340
        cx = width // 2
        self._ui.swipe(
            cx,
            int(height * 0.82),
            cx,
            int(height * 0.22),
            velocity=1200,
            timeout=timeout,
        )

    def start_app(
        self,
        bundle_name: str,
        ability_name: str | None = "EntryAbility",
        *,
        timeout: float = 15.0,
        module_name: str | None = None,
    ) -> None:
        """Start an application ability via official `aa start`."""
        valid_bundle = _validate_identifier(bundle_name, field_name="bundle_name")
        if ability_name is None:
            info = self.app_info(valid_bundle, timeout=timeout)
            if info is None or info.entry_ability is None:
                raise DeviceConfigurationError("Specify an ability when the entry is ambiguous.")
            ability_name = info.entry_ability
            module_name = module_name or info.entry_module
        valid_ability = _validate_identifier(ability_name, field_name="ability_name")
        argv = ["aa", "start", "-b", valid_bundle, "-a", valid_ability]
        if module_name is not None:
            argv.extend(["-m", _validate_identifier(module_name, field_name="module_name")])
        self._hdc.shell(
            self._profile.serial,
            argv,
            timeout=timeout,
        )

    def list_apps(self, *, timeout: float = 15.0) -> list[str]:
        result = self._hdc.shell(self._profile.serial, ["bm", "dump", "-a"], timeout=timeout)
        return parse_app_list(result.stdout)

    def app_info(self, bundle_name: str, *, timeout: float = 15.0) -> AppInfo | None:
        bundle = validate_bundle(bundle_name)
        if bundle not in self.list_apps(timeout=timeout):
            return None
        result = self._hdc.shell(
            self._profile.serial, ["bm", "dump", "-n", bundle], timeout=timeout
        )
        return parse_app_info(result.stdout, bundle)

    def install_app(self, packages: Sequence[str | Path], *, timeout: float = 120.0) -> None:
        """Submit a single intact APP on API 22+, otherwise submit the module set.

        Use ActionRunner for pinned package copies and durable idempotency.
        """
        if isinstance(packages, (str, bytes, Path)) or not 1 <= len(packages) <= 32:
            raise DeviceConfigurationError("Provide between 1 and 32 package paths.")
        has_app = any(Path(package).suffix.lower() == ".app" for package in packages)
        expand_apps = True
        if has_app and len(packages) == 1:
            api = self._hdc.get_param(
                self._profile.serial, "const.ohos.apiversion", timeout=timeout,
            )
            if not api.isdecimal() or int(api) <= 0:
                raise DeviceConfigurationError("Cannot determine device API for APP installation.")
            expand_apps = int(api) < 22
        with prepare_install_packages(packages, expand_apps=expand_apps) as modules:
            self._hdc.install(self._profile.serial, modules, timeout=timeout)

    def uninstall_app(self, bundle_name: str, *, timeout: float = 120.0) -> None:
        """Submit removal once. Use ActionRunner for durable idempotency."""
        self._hdc.uninstall(self._profile.serial, bundle_name, timeout=timeout)

    def force_stop(self, bundle_name: str, *, timeout: float = 15.0) -> None:
        """Force-stop an application via official `aa force-stop`."""
        valid_bundle = _validate_identifier(bundle_name, field_name="bundle_name")
        self._hdc.shell(
            self._profile.serial,
            ["aa", "force-stop", valid_bundle],
            timeout=timeout,
        )

    def get_foreground_window(self, *, timeout: float = 10.0) -> ForegroundWindow:
        """Inspect the currently focused window via `WindowManagerService`."""
        result = self._hdc.shell(
            self._profile.serial,
            ["hidumper", "-s", "WindowManagerService", "-a", "-a"],
            timeout=timeout,
        )
        return parse_foreground_window(result.stdout)

    def screenshot(
        self,
        output_path: str | Path,
        *,
        image_format: str | None = None,
        display_id: int | None = None,
        max_width: int | None = None,
        timeout: float = 20.0,
    ) -> ScreenshotResult:
        """Capture a screenshot via `snapshot_display` (JPEG) or `uitest screenCap` (PNG).

        Guarantees remote `/data/local/tmp/moyu_pilot_cap_*` cleanup and `0600` local mode.
        """
        dest = Path(output_path).expanduser()
        inferred = image_format
        if inferred is None:
            inferred = "png" if dest.suffix.lower() == ".png" else "jpeg"
        norm_fmt = inferred.strip().lower()
        if norm_fmt not in _VALID_IMAGE_FORMATS:
            raise DeviceConfigurationError(
                f"Unsupported screenshot format {image_format!r}; expected 'jpeg' or 'png'."
            )
        canonical_fmt = "png" if norm_fmt == "png" else "jpeg"
        remote_suffix = _VALID_IMAGE_FORMATS[norm_fmt]
        if display_id is not None and display_id < 0:
            raise DeviceConfigurationError(f"display_id must be non-negative, got {display_id!r}.")

        with self._hdc.remote_tmp_file(self._profile.serial, "cap", remote_suffix) as remote_path:
            if canonical_fmt == "jpeg":
                cmd = ["snapshot_display", "-f", remote_path]
                if display_id is not None:
                    cmd.extend(["-i", str(display_id)])
            else:
                cmd = ["uitest", "screenCap", "-p", remote_path]
                if display_id is not None:
                    cmd.extend(["-d", str(display_id)])

            self._hdc.shell(self._profile.serial, cmd, timeout=timeout)
            self._hdc.recv_file(self._profile.serial, remote_path, dest, timeout=timeout)

        enforce_file_mode_0600(dest)
        resized = False
        if max_width is not None:
            width, height, resized = resize_image_max_width(dest, max_width)
        else:
            width, height = read_image_dimensions(dest)

        size_bytes = dest.stat().st_size
        return ScreenshotResult(
            alias=self._profile.alias,
            path=dest,
            image_format=canonical_fmt,
            width=width,
            height=height,
            size_bytes=size_bytes,
            resized=resized,
        )

    def dump_ui_compact(
        self,
        *,
        max_elements: int = 120,
        include_attributes: bool = False,
        bundle_name: str | None = None,
        window_id: int | None = None,
        merge_windows: bool | None = None,
        display_id: int | None = None,
        timeout: float = 20.0,
    ) -> tuple[UiTree, CompactSnapshot]:
        """Capture UI hierarchy and build a compact `@eN` snapshot."""
        return self._ui.dump_compact(
            max_elements=max_elements,
            include_attributes=include_attributes,
            bundle_name=bundle_name,
            window_id=window_id,
            merge_windows=merge_windows,
            display_id=display_id,
            timeout=timeout,
        )
