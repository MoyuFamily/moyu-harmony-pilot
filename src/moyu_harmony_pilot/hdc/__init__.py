"""L1: Official HarmonyOS NEXT `hdc` CLI wrapper and transport abstractions."""

from moyu_harmony_pilot.hdc.client import (
    REMOTE_TMP_PREFIX,
    HdcClient,
    build_safe_shell_command,
    make_remote_tmp_path,
    parse_targets_output,
    validate_remote_tmp_path,
)
from moyu_harmony_pilot.hdc.discovery import HDC_PATH_ENV, discover_hdc
from moyu_harmony_pilot.hdc.errors import (
    DeviceOfflineError,
    DeviceUnauthorizedError,
    HdcCommandError,
    HdcError,
    HdcFileTransferError,
    HdcInvalidArgumentError,
    HdcNotFoundError,
    HdcTimeoutError,
)
from moyu_harmony_pilot.hdc.models import (
    CommandResult,
    ConnectionState,
    HdcErrorKind,
    HdcTarget,
    mask_serial,
)
from moyu_harmony_pilot.hdc.transport import (
    HdcTransport,
    SubprocessHdcTransport,
    classify_hdc_error,
)

__all__ = [
    "CommandResult",
    "ConnectionState",
    "DeviceOfflineError",
    "DeviceUnauthorizedError",
    "HDC_PATH_ENV",
    "HdcClient",
    "HdcCommandError",
    "HdcError",
    "HdcErrorKind",
    "HdcFileTransferError",
    "HdcInvalidArgumentError",
    "HdcNotFoundError",
    "HdcTarget",
    "HdcTimeoutError",
    "HdcTransport",
    "REMOTE_TMP_PREFIX",
    "SubprocessHdcTransport",
    "build_safe_shell_command",
    "classify_hdc_error",
    "discover_hdc",
    "make_remote_tmp_path",
    "mask_serial",
    "parse_targets_output",
    "validate_remote_tmp_path",
]
