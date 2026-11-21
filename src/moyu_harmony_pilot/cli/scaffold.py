"""Render the bundled standalone plugin template into a new local directory."""

from __future__ import annotations

import keyword
import os
import re
import stat
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from moyu_harmony_pilot.actions.validation import IDENTIFIER
from moyu_harmony_pilot.device import DeviceConfigurationError

_TEMPLATE_PREFIX = PurePosixPath("share/moyu-harmony-pilot/plugin-template")
_TEMPLATE_FILES = (
    ".github/workflows/contract.yml",
    ".gitignore",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "pyproject.toml",
    "requirements-test.txt",
    "src/harmony_pilot_example/__init__.py",
    "src/harmony_pilot_example/battery.py",
    "src/harmony_pilot_example/toggle.py",
    "tests/__init__.py",
    "tests/__main__.py",
    "tests/support.py",
    "tests/test_contract.py",
    "tests/test_mutating_contract.py",
)
_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_RESERVED_MODULES = sys.stdlib_module_names | {
    "tests", "moyu_harmony_pilot", "sitecustomize", "usercustomize",
}


def _template_files() -> dict[str, Path]:
    """Prefer a complete src checkout, then locate installed data using RECORD."""
    parents = Path(__file__).resolve().parents
    if len(parents) > 2 and parents[2].name == "src":
        root = parents[2].parent / "examples" / "plugin-template"
        source = {name: root / name for name in _TEMPLATE_FILES}
        if all(path.is_file() and not path.is_symlink() for path in source.values()):
            return source

    try:
        distribution = metadata.distribution("moyu-harmony-pilot")
    except metadata.PackageNotFoundError:
        distribution = None
    if distribution is not None:
        entries = distribution.files or ()
        installed: dict[str, Path] = {}
        for name in _TEMPLATE_FILES:
            suffix = (_TEMPLATE_PREFIX / name).parts
            matches = [entry for entry in entries if entry.parts[-len(suffix):] == suffix]
            if len(matches) > 1:
                raise DeviceConfigurationError("Installed plugin template records are ambiguous.")
            if matches:
                installed[name] = Path(str(distribution.locate_file(matches[0])))
        if installed:
            if len(installed) != len(_TEMPLATE_FILES):
                raise DeviceConfigurationError("Installed plugin template is incomplete.")
            return installed

    raise DeviceConfigurationError(
        "Plugin template is unavailable.",
        hint="Install a complete Harmony Pilot distribution or use its src checkout.",
    )


def _render_template(
    package_name: str, module_name: str, action_ids: list[str],
) -> dict[str, bytes]:
    replacements = {
        "harmony-pilot-example": package_name,
        "harmony_pilot_example": module_name,
        "example.battery_check": action_ids[0],
        "example.demo_toggle": action_ids[1],
    }
    pattern = re.compile("|".join(re.escape(name) for name in replacements))
    rendered: dict[str, bytes] = {}
    try:
        for name, source in _template_files().items():
            if source.is_symlink() or not source.is_file():
                raise DeviceConfigurationError("Plugin template requires regular files.")
            content = source.read_bytes().decode("utf-8")
            # One pass keeps replacement values containing template tokens intact.
            content = pattern.sub(lambda match: replacements[match[0]], content)
            relative = PurePosixPath(name)
            if relative.parts[:2] == ("src", "harmony_pilot_example"):
                relative = PurePosixPath("src", module_name, *relative.parts[2:])
            rendered[relative.as_posix()] = content.encode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise DeviceConfigurationError("Unable to read the bundled plugin template.") from exc
    return rendered


def _destination_path(destination: str | Path, cwd: Path | None) -> Path:
    if isinstance(destination, str) and not destination:
        raise DeviceConfigurationError("Destination path must be nonempty.")
    try:
        path = Path(destination)
        if not path.is_absolute():
            path = (Path.cwd() if cwd is None else cwd) / path
        # Resolve the parent only, so the final component can never follow a symlink.
        parent = path.parent.resolve()
        path = parent / path.name
        if path.is_symlink() or path.exists():
            raise DeviceConfigurationError("Destination must be a new directory.")
        return path
    except (OSError, ValueError, RuntimeError) as exc:
        raise DeviceConfigurationError("Unable to resolve the destination path.") from exc


def _cleanup_created(created: list[tuple[Path, os.stat_result]]) -> bool:
    """Remove owned entries, retaining replacements and unrelated files on failure."""
    complete = True
    for path, original in reversed(created):
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                complete = False
                continue
            if stat.S_ISDIR(original.st_mode):
                path.rmdir()
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            complete = False
    return complete


def _write_project(destination: Path, rendered: dict[str, bytes]) -> None:
    created: list[tuple[Path, os.stat_result]] = []
    try:
        missing_parents = []
        parent = destination.parent
        while not parent.exists():
            missing_parents.append(parent)
            parent = parent.parent
        if not parent.is_dir():
            raise DeviceConfigurationError("Destination parent must be a directory.")
        for parent in reversed(missing_parents):
            try:
                parent.mkdir()
            except FileExistsError:
                if not parent.is_dir():
                    raise DeviceConfigurationError(
                        "Destination parent must be a directory.",
                    ) from None
            else:
                created.append((parent, parent.lstat()))
        try:
            destination.mkdir()
        except FileExistsError as exc:
            raise DeviceConfigurationError("Destination must be a new directory.") from exc
        created.append((destination, destination.lstat()))
        directories = {
            parent
            for name in rendered
            for parent in PurePosixPath(name).parents
            if parent != PurePosixPath(".")
        }
        for relative in sorted(directories, key=lambda path: (len(path.parts), path.as_posix())):
            directory = destination / relative
            directory.mkdir()
            created.append((directory, directory.lstat()))
        for name in sorted(rendered):
            path = destination / name
            with path.open("xb") as stream:
                created.append((path, os.fstat(stream.fileno())))
                stream.write(rendered[name])
    except BaseException as exc:
        cleaned = _cleanup_created(created)
        if isinstance(exc, (OSError, ValueError)):
            hint = None if cleaned else "Files remain. Inspect the destination before retrying."
            raise DeviceConfigurationError(
                "Unable to write the plugin project.", hint=hint,
            ) from exc
        raise


def create_plugin_project(
    destination: str | Path,
    *,
    namespace: str,
    package_name: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Create a project and return commands to run in the same Python environment.

    Missing parents are created after rendering all files. The destination itself
    is created exclusively. Returned commands are suggestions for the caller.
    The harmony-pilot console script must belong to sys.executable's environment.
    """
    if not isinstance(namespace, str) or not IDENTIFIER.fullmatch(namespace):
        raise DeviceConfigurationError("Invalid plugin action namespace.")
    action_ids = [f"{namespace}.battery_check", f"{namespace}.demo_toggle"]
    if any(not IDENTIFIER.fullmatch(action_id) for action_id in action_ids):
        raise DeviceConfigurationError(
            "Plugin namespace produces action IDs exceeding 128 characters.",
        )

    path = _destination_path(destination, cwd)
    if package_name is None:
        package_name = path.name
    if not isinstance(package_name, str) or not _DISTRIBUTION_NAME.fullmatch(package_name):
        raise DeviceConfigurationError("Invalid plugin distribution name.")
    module_name = re.sub(r"[-_.]+", "_", package_name).lower()
    if (
        not module_name.isidentifier()
        or keyword.iskeyword(module_name)
        or module_name in _RESERVED_MODULES
    ):
        raise DeviceConfigurationError(
            "Plugin distribution name requires a valid, unreserved module.",
        )

    rendered = _render_template(package_name, module_name, action_ids)
    _write_project(path, rendered)
    commands = [
        [sys.executable, "-m", "pip", "install", "--no-deps", "-e", "."],
        [sys.executable, "-m", "tests"],
        *[
            ["harmony-pilot", "plugins", "describe", action_id, "--json"]
            for action_id in action_ids
        ],
    ]
    return {
        "path": str(path),
        "package_name": package_name,
        "module_name": module_name,
        "action_ids": action_ids,
        "files": sorted(rendered),
        "next_steps": [{"cwd": str(path), "argv": argv} for argv in commands],
    }
