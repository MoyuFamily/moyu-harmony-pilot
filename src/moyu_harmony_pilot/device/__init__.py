"""L3: Device aliases, exclusive locks, power/app control, telemetry, and diagnostics."""

from moyu_harmony_pilot.device.controller import HarmonyDevice
from moyu_harmony_pilot.device.diagnostics import (
    DiagnosticsStore,
    enforce_file_mode_0600,
    read_image_dimensions,
    resize_image_max_width,
    write_secure_bytes,
    write_secure_text,
)
from moyu_harmony_pilot.device.errors import (
    DeviceBusyError,
    DeviceConfigurationError,
    DeviceError,
    DeviceOverheatedError,
    HumanRequiredError,
    UnknownDeviceAliasError,
)
from moyu_harmony_pilot.device.lock import (
    DeviceLock,
    ensure_secure_dir,
    is_device_busy,
    lock_file_path,
)
from moyu_harmony_pilot.device.models import (
    DEFAULT_MAX_TEMPERATURE_C,
    BatteryTelemetry,
    DeviceHealth,
    DeviceInfo,
    DeviceProfile,
    DeviceSummary,
    ForegroundWindow,
    PowerTelemetry,
    ScreenshotResult,
    ScreenState,
)
from moyu_harmony_pilot.device.registry import (
    DEFAULT_CONFIG_FILENAMES,
    DEVICES_CONFIG_ENV,
    DeviceRegistry,
    check_config_permissions,
    discover_devices_config,
)
from moyu_harmony_pilot.device.telemetry import (
    parse_battery_dump,
    parse_foreground_window,
    parse_power_dump,
)

__all__ = [
    "BatteryTelemetry",
    "DEFAULT_CONFIG_FILENAMES",
    "DEFAULT_MAX_TEMPERATURE_C",
    "DEVICES_CONFIG_ENV",
    "DeviceBusyError",
    "DeviceConfigurationError",
    "DeviceError",
    "DeviceHealth",
    "DeviceInfo",
    "DeviceLock",
    "DeviceOverheatedError",
    "DeviceProfile",
    "DeviceRegistry",
    "DeviceSummary",
    "DiagnosticsStore",
    "ForegroundWindow",
    "HarmonyDevice",
    "HumanRequiredError",
    "PowerTelemetry",
    "ScreenState",
    "ScreenshotResult",
    "UnknownDeviceAliasError",
    "check_config_permissions",
    "discover_devices_config",
    "enforce_file_mode_0600",
    "ensure_secure_dir",
    "is_device_busy",
    "lock_file_path",
    "parse_battery_dump",
    "parse_foreground_window",
    "parse_power_dump",
    "read_image_dimensions",
    "resize_image_max_width",
    "write_secure_bytes",
    "write_secure_text",
]
