"""Offline regressions for template rendering, installation paths, and exclusive creation."""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from moyu_harmony_pilot.actions import ActionRegistry
from moyu_harmony_pilot.actions.validation import IDENTIFIER
from moyu_harmony_pilot.cli import scaffold
from moyu_harmony_pilot.device import DeviceConfigurationError
from moyu_harmony_pilot.testing import offline_guard

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "plugin-template"
TEMPLATE_FILES = {
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
}


@pytest.fixture(autouse=True)
def use_checkout_template(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_distribution(name: str) -> metadata.Distribution:
        assert name == "moyu-harmony-pilot"
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing_distribution)


def copy_template(destination: Path) -> None:
    for name in TEMPLATE_FILES:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((TEMPLATE / name).read_bytes())


def installed_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    site_packages = tmp_path / "site-packages"
    dist_info = site_packages / "moyu_harmony_pilot-0.2.0.dev0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Name: moyu-harmony-pilot\nVersion: 0.2.0.dev0\n", encoding="utf-8",
    )
    # RECORD can locate a user/target installation outside the Python prefix.
    template = tmp_path / "custom data prefix" / "share/moyu-harmony-pilot/plugin-template"
    copy_template(template)
    record = dist_info / "RECORD"
    with record.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        for name in sorted(TEMPLATE_FILES):
            relative = os.path.relpath(template / name, site_packages).replace(os.sep, "/")
            writer.writerow([relative, "", ""])
    distribution = metadata.Distribution.at(dist_info)
    monkeypatch.setattr(metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(
        scaffold, "__file__", str(site_packages / "moyu_harmony_pilot/cli/scaffold.py"),
    )
    return template, record


def test_default_names_render_exact_template_and_machine_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Scaffolding must remain independent of plugin discovery.")

    monkeypatch.setattr(ActionRegistry, "discover", forbidden)
    monkeypatch.setattr(metadata, "entry_points", forbidden)
    destination = tmp_path / "Community.Pilot--Tools"
    with offline_guard():
        result = scaffold.create_plugin_project(destination, namespace="community.tools")
    assert set(result) == {
        "path", "package_name", "module_name", "action_ids", "files", "next_steps",
    }
    assert result["path"] == str(destination)
    assert result["package_name"] == destination.name
    assert result["module_name"] == "community_pilot_tools"
    assert result["action_ids"] == ["community.tools.battery_check", "community.tools.demo_toggle"]
    assert all(IDENTIFIER.fullmatch(action_id) for action_id in result["action_ids"])
    expected_files = sorted(
        name.replace("src/harmony_pilot_example/", "src/community_pilot_tools/")
        for name in TEMPLATE_FILES
    )
    assert result["files"] == expected_files
    assert sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*") if path.is_file()
    ) == expected_files
    for name in TEMPLATE_FILES:
        expected = (TEMPLATE / name).read_text(encoding="utf-8")
        for old, new in (
            ("harmony-pilot-example", destination.name),
            ("harmony_pilot_example", "community_pilot_tools"),
            ("example.battery_check", result["action_ids"][0]),
            ("example.demo_toggle", result["action_ids"][1]),
        ):
            expected = expected.replace(old, new)
        output = destination / name.replace("harmony_pilot_example", "community_pilot_tools")
        assert output.read_text(encoding="utf-8") == expected
        if output.suffix == ".py":
            compile(expected, str(output), "exec")
    plugin = tomllib.loads((destination / "pyproject.toml").read_text(encoding="utf-8"))
    assert plugin["project"]["name"] == destination.name
    assert plugin["project"]["entry-points"]["moyu_harmony_pilot.actions"] == {
        "community.tools.battery_check": "community_pilot_tools.battery:BatteryCheck",
        "community.tools.demo_toggle": "community_pilot_tools.toggle:DemoToggle",
    }
    toggle = (destination / "src/community_pilot_tools/toggle.py").read_text(encoding="utf-8")
    assert 'DEMO_BUNDLE = "com.example.pilotdemo"' in toggle
    assert 'TOGGLE_ID = "demo_toggle"' in toggle
    assert '"demo_state_unconfirmed"' in toggle
    assert result["next_steps"] == [
        {"cwd": str(destination), "argv": argv}
        for argv in [
            [sys.executable, "-m", "pip", "install", "--no-deps", "-e", "."],
            [sys.executable, "-m", "tests"],
            ["harmony-pilot", "plugins", "describe", "community.tools.battery_check", "--json"],
            ["harmony-pilot", "plugins", "describe", "community.tools.demo_toggle", "--json"],
        ]
    ]
    assert json.loads(json.dumps(result)) == result
    assert capsys.readouterr() == ("", "")


def test_relative_nested_destination_with_spaces_and_explicit_package(tmp_path: Path) -> None:
    result = scaffold.create_plugin_project(
        "parent with spaces/child/My plugin", namespace="team_demo",
        package_name="Team.Demo-plugin", cwd=tmp_path,
    )
    path = tmp_path / "parent with spaces/child/My plugin"
    assert result["path"] == str(path)
    assert result["package_name"] == "Team.Demo-plugin"
    assert result["module_name"] == "team_demo_plugin"
    assert (path / "src/team_demo_plugin/battery.py").is_file()
    assert all(step["cwd"] == str(path) for step in result["next_steps"])


def test_relative_destination_defaults_to_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = scaffold.create_plugin_project("local-plugin", namespace="local")
    assert result["path"] == str(tmp_path / "local-plugin")


@pytest.mark.parametrize("package_name", [
    "", "bad name", "-leading", "trailing.", "bad/name", "bad\\name", "插件",
    "private\nsecret", "private\x00secret", "name;echo-secret", "$(secret)",
    "1plugin", "class", "async", "tests", "TeStS", "sys", "json", "email", "typing",
    "pathlib", "unittest", "moyu-harmony-pilot", "MoYu..Harmony--Pilot",
    "sitecustomize", "usercustomize",
])
def test_invalid_names_are_redacted_and_create_nothing(
    tmp_path: Path, package_name: str, capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(DeviceConfigurationError) as caught:
        scaffold.create_plugin_project(
            tmp_path / "new-parent/plugin", namespace="team", package_name=package_name,
        )
    assert caught.value.code == "configuration_error"
    assert not caught.value.retryable
    assert str(caught.value) in {
        "Invalid plugin distribution name.",
        "Plugin distribution name requires a valid, unreserved module.",
    }
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("namespace", [
    "", "1team", ".team", "team space", "私有", "private\nsecret",
    "private\x00secret", "team/secret", "team;secret", "a" * 115, "a" * 129,
])
def test_invalid_namespace_creates_nothing(tmp_path: Path, namespace: str) -> None:
    with pytest.raises(DeviceConfigurationError) as caught:
        scaffold.create_plugin_project(tmp_path / "missing/plugin", namespace=namespace)
    assert str(caught.value) in {
        "Invalid plugin action namespace.",
        "Plugin namespace produces action IDs exceeding 128 characters.",
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("namespace", ["A", "team_1.actions-v2", "a" * 114])
def test_namespace_uses_final_action_identifier_limits(tmp_path: Path, namespace: str) -> None:
    result = scaffold.create_plugin_project(tmp_path / "plugin", namespace=namespace)
    assert result["action_ids"] == [f"{namespace}.battery_check", f"{namespace}.demo_toggle"]
    assert all(IDENTIFIER.fullmatch(action_id) for action_id in result["action_ids"])


def test_replacement_values_are_never_reprocessed(tmp_path: Path) -> None:
    result = scaffold.create_plugin_project(
        tmp_path / "plugin", namespace="harmony_pilot_example.example.demo_toggle",
        package_name="harmony_pilot_example.extra",
    )
    plugin = tomllib.loads((Path(result["path"]) / "pyproject.toml").read_text(encoding="utf-8"))
    assert plugin["project"]["name"] == "harmony_pilot_example.extra"
    assert plugin["project"]["entry-points"]["moyu_harmony_pilot.actions"] == {
        "harmony_pilot_example.example.demo_toggle.battery_check":
            "harmony_pilot_example_extra.battery:BatteryCheck",
        "harmony_pilot_example.example.demo_toggle.demo_toggle":
            "harmony_pilot_example_extra.toggle:DemoToggle",
    }


@pytest.mark.parametrize("kind", ["empty", "nonempty", "file", "symlink", "dangling"])
def test_existing_destination_is_always_preserved(tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "plugin"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("original", encoding="utf-8")
    if kind in {"empty", "nonempty"}:
        destination.mkdir()
        if kind == "nonempty":
            (destination / "keep.txt").write_text("original", encoding="utf-8")
    elif kind == "file":
        destination.write_text("original", encoding="utf-8")
    else:
        destination.symlink_to(outside if kind == "symlink" else tmp_path / "missing")
    before = destination.lstat()
    with pytest.raises(DeviceConfigurationError, match="new directory"):
        scaffold.create_plugin_project(destination, namespace="team")
    assert destination.lstat() == before
    assert marker.read_text(encoding="utf-8") == "original"
    if kind in {"empty", "nonempty"}:
        assert sorted(path.name for path in destination.iterdir()) == (
            [] if kind == "empty" else ["keep.txt"]
        )


@pytest.mark.parametrize("destination", ["", "private\x00secret", "parent/child/plugin"])
def test_invalid_paths_are_configuration_errors(tmp_path: Path, destination: str) -> None:
    parent = tmp_path / "parent"
    parent.write_text("existing file", encoding="utf-8")
    with pytest.raises(DeviceConfigurationError) as caught:
        scaffold.create_plugin_project(
            destination, namespace="team", package_name="team-plugin", cwd=tmp_path,
        )
    assert "private" not in str(caught.value)
    assert parent.read_text(encoding="utf-8") == "existing file"
    assert list(tmp_path.iterdir()) == [parent]


def test_concurrent_creators_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "nested/parent/plugin"
    barrier = threading.Barrier(2)
    original_render = scaffold._render_template

    def render(*args: Any) -> dict[str, bytes]:
        rendered = original_render(*args)
        barrier.wait(timeout=10)
        return rendered

    def create() -> dict[str, Any] | DeviceConfigurationError:
        try:
            return scaffold.create_plugin_project(destination, namespace="team")
        except DeviceConfigurationError as exc:
            return exc

    monkeypatch.setattr(scaffold, "_render_template", render)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    successes = [result for result in results if isinstance(result, dict)]
    errors = [result for result in results if isinstance(result, DeviceConfigurationError)]
    assert len(successes) == len(errors) == 1
    assert str(errors[0]) == "Destination must be a new directory."
    assert all((destination / name).is_file() for name in successes[0]["files"])


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_partial_write_failure_removes_only_created_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException],
) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("original", encoding="utf-8")
    destination = tmp_path / "new/parents/plugin"
    original_open = Path.open

    def failing_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = original_open(path, mode, *args, **kwargs)
        if mode == "xb" and path.name == "test_contract.py":
            original_write = stream.write

            def failing_write(content: bytes) -> int:
                original_write(content[:19])
                raise failure("synthetic write failure")

            monkeypatch.setattr(stream, "write", failing_write)
        return stream

    monkeypatch.setattr(Path, "open", failing_open)
    expected = DeviceConfigurationError if failure is OSError else failure
    with pytest.raises(expected):
        scaffold.create_plugin_project(destination, namespace="team")
    assert list(tmp_path.iterdir()) == [marker]
    assert marker.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("stage", ["parent", "destination", "source"])
def test_mkdir_failure_cleans_new_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    destination = tmp_path / "new/parent/plugin"
    failing_path = {
        "parent": destination.parent, "destination": destination, "source": destination / "src",
    }[stage]
    original_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == failing_path:
            raise PermissionError("synthetic permission failure")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(DeviceConfigurationError):
        scaffold.create_plugin_project(destination, namespace="team")
    assert list(tmp_path.iterdir()) == []


def test_write_collision_preserves_unrelated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "new/plugin"
    collision = destination / ".gitignore"
    original_open = Path.open

    def open_with_collision(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == collision and mode == "xb":
            collision.write_text("concurrent owner's content", encoding="utf-8")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_collision)
    with pytest.raises(DeviceConfigurationError) as caught:
        scaffold.create_plugin_project(destination, namespace="team")
    assert caught.value.hint is not None
    assert collision.read_text(encoding="utf-8") == "concurrent owner's content"
    assert list(destination.iterdir()) == [collision]


def test_target_created_after_preflight_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "new/plugin"
    original_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == destination:
            original_mkdir(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(DeviceConfigurationError, match="new directory"):
        scaffold.create_plugin_project(destination, namespace="team")
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_installed_record_locations_support_custom_data_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    template, _ = installed_template(tmp_path, monkeypatch)
    (template / "README.md").write_text("Installed example.battery_check\n", encoding="utf-8")
    result = scaffold.create_plugin_project(tmp_path / "plugin", namespace="team")
    assert (Path(result["path"]) / "README.md").read_text(encoding="utf-8") == (
        "Installed team.battery_check\n"
    )


def test_editable_checkout_prefers_live_source_over_installed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed, _ = installed_template(tmp_path, monkeypatch)
    (installed / "README.md").write_text("Stale installed template\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    source = checkout / "examples/plugin-template"
    copy_template(source)
    monkeypatch.setattr(
        scaffold, "__file__", str(checkout / "src/moyu_harmony_pilot/cli/scaffold.py"),
    )
    for revision in ("first", "updated"):
        (source / "README.md").write_text(
            f"{revision} source example.battery_check\n", encoding="utf-8",
        )
        result = scaffold.create_plugin_project(tmp_path / revision, namespace="team")
        assert (Path(result["path"]) / "README.md").read_text(encoding="utf-8") == (
            f"{revision} source team.battery_check\n"
        )


def test_incomplete_source_checkout_uses_complete_installed_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed, _ = installed_template(tmp_path, monkeypatch)
    (installed / "README.md").write_text("Complete installed template\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    source = checkout / "examples/plugin-template"
    copy_template(source)
    (source / "README.md").unlink()
    monkeypatch.setattr(
        scaffold, "__file__", str(checkout / "src/moyu_harmony_pilot/cli/scaffold.py"),
    )
    result = scaffold.create_plugin_project(tmp_path / "plugin", namespace="team")
    assert (Path(result["path"]) / "README.md").read_text(encoding="utf-8") == (
        "Complete installed template\n"
    )


@pytest.mark.parametrize(
    "damage", ["missing-record", "duplicate-record", "missing-file", "encoding"],
)
def test_incomplete_installed_template_fails_before_creating_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    template, record = installed_template(tmp_path, monkeypatch)
    lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
    if damage == "missing-record":
        record.write_text("".join(lines[1:]), encoding="utf-8")
    elif damage == "duplicate-record":
        record.write_text("".join([*lines, lines[0]]), encoding="utf-8")
    elif damage == "missing-file":
        (template / "README.md").unlink()
    else:
        (template / "README.md").write_bytes(b"\xff")
    with pytest.raises(DeviceConfigurationError):
        scaffold.create_plugin_project(tmp_path / "new/plugin", namespace="team")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("layout", ["src", "site-packages"])
def test_source_loading_is_bound_to_module_layout_and_fixed_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str,
) -> None:
    source = tmp_path / "checkout"
    template = source / "examples/plugin-template"
    copy_template(template)
    for name in (
        ".env", "devices.local.json", "artifacts/private.png", ".state/actions.sqlite3",
        "src/harmony_pilot_example/private.py", "src/harmony_pilot_example/__pycache__/private.pyc",
    ):
        path = template / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-private-marker", encoding="utf-8")
    monkeypatch.setattr(
        scaffold, "__file__", str(source / layout / "moyu_harmony_pilot/cli/scaffold.py"),
    )
    monkeypatch.chdir(source)
    destination = tmp_path / "plugin"
    if layout != "src":
        with pytest.raises(DeviceConfigurationError, match="unavailable"):
            scaffold.create_plugin_project(destination, namespace="team")
        assert not destination.exists()
        return
    result = scaffold.create_plugin_project(destination, namespace="team")
    assert set(result["files"]) == {
        name.replace("harmony_pilot_example", "plugin") for name in TEMPLATE_FILES
    }
    assert "synthetic-private-marker" not in json.dumps(result)
    assert all(
        b"synthetic-private-marker" not in (destination / name).read_bytes()
        for name in result["files"]
    )


def test_missing_record_table_never_searches_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, record = installed_template(tmp_path, monkeypatch)
    record.unlink()
    monkeypatch.setattr(
        scaffold, "__file__", str(tmp_path / "lib/moyu_harmony_pilot/cli/scaffold.py"),
    )
    monkeypatch.chdir(ROOT)
    with pytest.raises(DeviceConfigurationError, match="unavailable"):
        scaffold.create_plugin_project(tmp_path / "plugin", namespace="team")
    assert not (tmp_path / "plugin").exists()


def test_template_file_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    template, _ = installed_template(tmp_path, monkeypatch)
    (template / "README.md").unlink()
    outside = tmp_path / "unrelated.txt"
    outside.write_text("synthetic-private-marker", encoding="utf-8")
    (template / "README.md").symlink_to(outside)
    with pytest.raises(DeviceConfigurationError, match="regular files"):
        scaffold.create_plugin_project(tmp_path / "new/plugin", namespace="team")
    assert not (tmp_path / "new").exists()
    assert outside.read_text(encoding="utf-8") == "synthetic-private-marker"


def test_setuptools_mapping_matches_the_complete_template_allowlist() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    prefix = "share/moyu-harmony-pilot/plugin-template"
    mapped: dict[str, str] = {}
    for destination, sources in config["tool"]["setuptools"]["data-files"].items():
        if destination == prefix or destination.startswith(prefix + "/"):
            for source in sources:
                relative = (Path(destination) / Path(source).name).relative_to(prefix).as_posix()
                assert relative not in mapped
                mapped[relative] = source
    assert mapped == {name: f"examples/plugin-template/{name}" for name in TEMPLATE_FILES}
    assert set(scaffold._TEMPLATE_FILES) == TEMPLATE_FILES
    assert config["project"]["dependencies"] == []
