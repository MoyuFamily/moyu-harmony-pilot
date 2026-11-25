"""Verify archive contents and one installed external-plugin workflow offline."""

import email
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest


def _run_offline(
    command: list[str], *, cwd: Path, timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PIP_")
    }
    environment.pop("PYTHONHOME", None)
    environment.update({
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "HARMONY_PILOT_PLUGIN_PATHS": "",
    })
    result = subprocess.run(  # noqa: S603 -- fixed local build, install and CLI commands
        command, cwd=cwd, env=environment, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    return result


@pytest.fixture(scope="module")
def distribution_artifacts(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, set[str], bytes]:
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path_factory.mktemp("distribution")
    source = workspace / "source"
    source.mkdir()
    files = (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "SECURITY.md",
        "AGENTS.md",
        "devices.example.yaml",
        "bridge.example.json",
    )
    for name in files:
        shutil.copy2(root / name, source / name)
    private_directories = (
        ".state", "artifacts", "diagnostics", "plugins_local", ".venv", "venv", "ENV",
    )
    private_files = (
        "devices.local.yaml", "devices.local.json", "devices.local.yml",
        "bridge.local.json", "bridge.local.yaml", "bridge-token.local",
        ".env", ".env.production", "actions.sqlite3", "actions.sqlite3-journal",
        "actions.sqlite3-wal", "actions.sqlite3-shm", "capture.tmp", "capture.log",
    )
    ignored_paths = shutil.ignore_patterns(
        "__pycache__", "*.egg-info", *private_directories,
        "devices.local.yaml", "devices.local.json", "devices.local.yml",
        "bridge.local.*", "bridge-token.local", ".env", ".env.*",
        "*.sqlite3", "*.sqlite3-journal", "*.sqlite3-wal", "*.sqlite3-shm", "*.tmp", "*.log",
    )

    def ignore_private_paths(directory: str, names: list[str]) -> set[str]:
        return ignored_paths(directory, names) - {".env.example"}

    for name in ("src", "tests", "docs", "skills", "examples", "deploy"):
        shutil.copytree(root / name, source / name, ignore=ignore_private_paths)

    # Exercise real archive contents using synthetic data only, including nested paths.
    private_marker = f"synthetic-private-{workspace.name}"
    private_content = json.dumps({"private": private_marker}).encode("utf-8")
    private_paths: set[str] = set()
    for parent in (
        ".", "examples/bridge", "examples/plugin-template", "tests", "src/moyu_harmony_pilot",
    ):
        for name in private_files:
            private_paths.add((Path(parent) / name).as_posix())
        for directory in private_directories:
            for name in ("private.py", "nested/layout.json", ".env.example"):
                private_paths.add((Path(parent) / directory / name).as_posix())
    for name in sorted(private_paths):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(private_content)
    (source / ".env.example").write_text("SYNTHETIC_SETTING=public-example\n", encoding="utf-8")

    artifacts = workspace / "artifacts"
    _run_offline(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts)],
        cwd=source, timeout=60,
    )
    return artifacts, private_paths, private_marker.encode("utf-8")


def test_sdist_and_wheel_include_required_community_deliverables(
    distribution_artifacts: tuple[Path, set[str], bytes],
) -> None:
    artifacts, private_paths, private_marker = distribution_artifacts
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == []
    with tarfile.open(next(artifacts.glob("*.tar.gz"))) as archive:
        names = {"/".join(name.split("/")[1:]) for name in archive.getnames()}
        required = {
            "CHANGELOG.md",
            "src/moyu_harmony_pilot/py.typed",
            "tests/__init__.py",
            "tests/fakes.py",
            "tests/fixtures/harmony_61_home_layout.json",
            "tests/test_compatibility.py",
            "tests/fixtures/compatibility/synthetic_harmony_5_phone.json",
            "tests/fixtures/compatibility/synthetic_harmony_6_phone.json",
            "tests/fixtures/compatibility/synthetic_harmony_6_tablet.json",
            "tests/fixtures/compatibility/synthetic_harmony_6_foldable.json",
            "docs/compatibility.md",
            "docs/release-checklist.md",
            "docs/actions.md",
            "docs/plugin-catalog.md",
            "docs/mcp.md",
            "docs/bridge-worker.md",
            "docs/remote-integration.md",
            "skills/harmony-pilot/SKILL.md",
            "examples/actions/actions.json",
            "examples/actions/ui_summary.py",
            "examples/bridge/bridge.example.json",
            "examples/bridge/plugins/actions.json",
            "examples/bridge/plugins/health_check.py",
            "examples/plugin-template/pyproject.toml",
            "examples/plugin-template/README.md",
            "examples/plugin-template/LICENSE",
            "examples/plugin-template/.gitignore",
            "examples/plugin-template/.github/workflows/contract.yml",
            "examples/plugin-template/MANIFEST.in",
            "examples/plugin-template/requirements-test.txt",
            "examples/plugin-template/src/harmony_pilot_example/battery.py",
            "examples/plugin-template/tests/support.py",
            "examples/plugin-template/tests/test_contract.py",
            "examples/plugin-template/tests/test_mutating_contract.py",
            "examples/plugin-template/tests/__main__.py",
            "deploy/systemd/harmony-pilot.service",
            "deploy/launchd/org.example.harmony-pilot.plist",
        }
        assert required <= names
        assert not any(".state/" in name or "devices.local." in name for name in names)
        assert names.isdisjoint(private_paths), sorted(names & private_paths)
        assert ".env.example" in names
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                assert private_marker not in stream.read(), member.name
        metadata_path = next(
            name for name in archive.getnames()
            if name.count("/") == 1 and name.endswith("/PKG-INFO")
        )
        metadata_file = archive.extractfile(metadata_path)
        assert metadata_file is not None
        sdist_metadata = email.message_from_bytes(metadata_file.read())
        assert sdist_metadata["Name"] == project["name"]
        assert sdist_metadata["Version"] == project["version"]
        assert sdist_metadata["Requires-Python"] == project["requires-python"]
    with zipfile.ZipFile(next(artifacts.glob("*.whl"))) as archive:
        names = archive.namelist()
        for name in names:
            assert private_marker not in archive.read(name), name
        assert "moyu_harmony_pilot/mcp/server.py" in names
        assert archive.read("moyu_harmony_pilot/py.typed") == b""
        assert any(
            name.endswith("/share/moyu-harmony-pilot/skills/harmony-pilot/SKILL.md")
            for name in names
        )
        for resource in (
            "pyproject.toml", ".gitignore", ".github/workflows/contract.yml",
            "requirements-test.txt",
            "src/harmony_pilot_example/toggle.py", "tests/test_mutating_contract.py",
        ):
            assert any(
                name.endswith("/share/moyu-harmony-pilot/plugin-template/" + resource)
                for name in names
            ), resource
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        assert metadata["Name"] == project["name"]
        assert metadata["Version"] == project["version"]
        assert metadata["Requires-Python"] == project["requires-python"]
        assert metadata["License-Expression"] == "MIT"
        assert all(
            "extra ==" in requirement for requirement in metadata.get_all("Requires-Dist", [])
        )
        workflow_name = next(
            name for name in names
            if name.endswith("/plugin-template/.github/workflows/contract.yml")
        )
        assert (
            "vars.PILOT_REF || 'v" + project["version"] + "'"
            in archive.read(workflow_name).decode()
        )
        requirements_name = next(
            name for name in names if name.endswith("/plugin-template/requirements-test.txt")
        )
        assert (
            project["name"] + "==" + project["version"]
            in archive.read(requirements_name).decode().splitlines()
        )
        entries_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "harmony-pilot-mcp" in archive.read(entries_name).decode()


def test_installed_cli_generates_packaged_plugin_with_offline_contracts(
    distribution_artifacts: tuple[Path, set[str], bytes], tmp_path: Path,
) -> None:
    artifacts, _, _ = distribution_artifacts
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    consumer_environment = tmp_path / "consumer-venv"
    _run_offline(
        [sys.executable, "-m", "venv", "--symlinks", str(consumer_environment)],
        cwd=consumer, timeout=60,
    )
    consumer_python = consumer_environment / "bin" / "python"
    cli = consumer_environment / "bin" / "harmony-pilot"
    _run_offline(
        [
            str(consumer_python), "-m", "pip", "install", "--no-index", "--no-deps",
            str(next(artifacts.glob("*.whl"))),
        ],
        cwd=consumer, timeout=60,
    )

    generated = consumer / "community-plugin"
    result = _run_offline(
        [str(cli), "plugins", "init", str(generated), "--namespace", "community", "--json"],
        cwd=consumer,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    scaffold = payload["data"]
    assert scaffold["module_name"] == "community_plugin"
    assert scaffold["action_ids"] == ["community.battery_check", "community.demo_toggle"]

    # Build the generated sdist and its wheel with the already-installed build tools.
    generated_artifacts = tmp_path / "community-artifacts"
    _run_offline(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(generated_artifacts)],
        cwd=generated, timeout=60,
    )
    with tarfile.open(next(generated_artifacts.glob("*.tar.gz"))) as archive:
        names = {"/".join(name.split("/")[1:]) for name in archive.getnames()}
        assert {
            "tests/test_contract.py", "tests/test_mutating_contract.py", "tests/support.py",
            "tests/__main__.py", "requirements-test.txt", ".github/workflows/contract.yml",
        } <= names
    plugin_wheel = next(generated_artifacts.glob("*.whl"))
    with zipfile.ZipFile(plugin_wheel) as archive:
        assert not any(name.startswith("tests/") for name in archive.namelist())

    # Resolve plugin dependencies against the installed Pilot while staying offline.
    _run_offline(
        [str(consumer_python), "-m", "pip", "install", "--no-index", str(plugin_wheel)],
        cwd=consumer,
    )
    result = _run_offline([str(consumer_python), "-m", "tests"], cwd=generated)
    assert "Ran 9 tests" in result.stderr
    assert "skipped" not in result.stderr.lower()

    result = _run_offline([str(cli), "plugins", "list", "--json"], cwd=consumer)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    discovered = {plugin["action_id"] for plugin in payload["data"]["plugins"]}
    assert set(scaffold["action_ids"]) <= discovered
    assert not any(action_id.startswith("example.") for action_id in discovered)
