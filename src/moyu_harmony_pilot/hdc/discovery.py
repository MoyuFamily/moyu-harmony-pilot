"""Official `hdc` binary discovery across macOS and Linux environments."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from moyu_harmony_pilot.hdc.errors import HdcNotFoundError

HDC_PATH_ENV = "HARMONY_PILOT_HDC_PATH"


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _default_candidate_paths(home: Path) -> list[Path]:
    candidates: list[Path] = []

    # OpenHarmony Public SDK toolchains installed under ~/Library/OpenHarmony/Sdk/<ver>/toolchains
    ohos_sdk_root = home / "Library" / "OpenHarmony" / "Sdk"
    if ohos_sdk_root.is_dir():
        try:
            sdk_dirs = sorted(
                (p for p in ohos_sdk_root.iterdir() if p.is_dir()),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            sdk_dirs = []
        for sdk_dir in sdk_dirs:
            candidates.append(sdk_dir / "toolchains" / "hdc")

    # DevEco Studio / Huawei Command Line Tools on macOS
    candidates.extend(
        [
            Path("/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"),
            home / "Library" / "Huawei" / "Sdk" / "openharmony" / "toolchains" / "hdc",
        ]
    )

    huawei_sdk_root = home / "Library" / "Huawei" / "Sdk"
    if huawei_sdk_root.is_dir():
        try:
            huawei_dirs = sorted(
                (p for p in huawei_sdk_root.iterdir() if p.is_dir()),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            huawei_dirs = []
        for sdk_dir in huawei_dirs:
            candidates.append(sdk_dir / "openharmony" / "toolchains" / "hdc")
            candidates.append(sdk_dir / "toolchains" / "hdc")

    # Common user and Linux paths
    candidates.extend(
        [
            home / ".local" / "bin" / "hdc",
            home / "ohos-sdk" / "linux" / "toolchains" / "hdc",
            Path("/opt/openharmony/toolchains/hdc"),
            Path("/usr/local/bin/hdc"),
        ]
    )

    return candidates


def discover_hdc(
    explicit_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Locate an executable official `hdc` binary.

    Resolution priority:
    1. Explicit `explicit_path` argument (fail-closed if specified and invalid).
    2. `HARMONY_PILOT_HDC_PATH` environment variable (fail-closed if set and invalid).
    3. `PATH` lookup via `shutil.which("hdc")`.
    4. Standard official macOS / Linux SDK installation directories.
    """
    effective_env = os.environ if env is None else env

    if explicit_path is not None and str(explicit_path).strip():
        candidate = Path(explicit_path).expanduser()
        if _is_executable_file(candidate):
            return candidate
        raise HdcNotFoundError(
            f"Explicit hdc binary path is not executable: {candidate}",
            hint="Verify the --hdc-path argument points to an executable official hdc binary.",
        )

    env_path = effective_env.get(HDC_PATH_ENV, "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if _is_executable_file(candidate):
            return candidate
        raise HdcNotFoundError(
            f"{HDC_PATH_ENV} does not point to an executable hdc binary: {candidate}",
            hint=f"Fix or unset {HDC_PATH_ENV} so discovery can locate official hdc.",
        )

    which_hit = shutil.which("hdc", path=effective_env.get("PATH"))
    if which_hit:
        candidate = Path(which_hit)
        if _is_executable_file(candidate):
            return candidate

    home = Path.home() if home_dir is None else home_dir
    for candidate in _default_candidate_paths(home):
        if _is_executable_file(candidate):
            return candidate

    raise HdcNotFoundError(
        "Official `hdc` binary was not found in HARMONY_PILOT_HDC_PATH, PATH, "
        "or standard OpenHarmony/DevEco SDK directories."
    )
