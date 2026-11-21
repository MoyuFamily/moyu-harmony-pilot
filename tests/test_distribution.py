"""Build distributable artifacts offline and verify downstream validation resources."""

import email
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import venv
import zipfile
from pathlib import Path


def test_sdist_and_wheel_include_required_community_deliverables(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == []
    build_environment = {
        **os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    source = tmp_path / "source"
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
    for name in ("src", "tests", "docs", "skills", "examples", "deploy"):
        shutil.copytree(
            root / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info")
        )
    artifacts = tmp_path / "artifacts"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts)],
        cwd=source,
        env=build_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
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
        assert "moyu_harmony_pilot/mcp/server.py" in names
        assert archive.read("moyu_harmony_pilot/py.typed") == b""
        assert any(
            name.endswith("/share/moyu-harmony-pilot/skills/harmony-pilot/SKILL.md")
            for name in names
        )
        for resource in (
            "pyproject.toml", ".gitignore", ".github/workflows/contract.yml",
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
        entries_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "harmony-pilot-mcp" in archive.read(entries_name).decode()
        installed = tmp_path / "installed"
        archive.extractall(installed)  # noqa: S202 -- locally built wheel ZIP

    # Build the standalone template and exercise both wheels outside the Pilot checkout.
    template = source / "examples" / "plugin-template"
    plugin_artifacts = tmp_path / "plugin-artifacts"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(plugin_artifacts)],
        cwd=template, env=build_environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next(plugin_artifacts.glob("*.tar.gz"))) as archive:
        names = {"/".join(name.split("/")[1:]) for name in archive.getnames()}
        assert {
            "tests/test_contract.py", "tests/test_mutating_contract.py",
            "tests/__main__.py", "requirements-test.txt", ".github/workflows/contract.yml",
        } <= names
    with zipfile.ZipFile(next(plugin_artifacts.glob("*.whl"))) as archive:
        assert not any(name.startswith("tests/") for name in archive.namelist())
        archive.extractall(installed)  # noqa: S202 -- locally built wheel ZIP

    environment = {**os.environ, "PYTHONPATH": str(installed), "PYTHONNOUSERSITE": "1"}
    script = """
import pathlib
import importlib.resources
import runpy
import sys
import moyu_harmony_pilot

assert pathlib.Path(moyu_harmony_pilot.__file__).is_relative_to(sys.argv[1])
assert importlib.resources.files('moyu_harmony_pilot').joinpath('py.typed').is_file()
runpy.run_module('tests', run_name='__main__')
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", script, str(installed)],
        cwd=template, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ran 9 tests" in result.stderr
    assert "skipped" not in result.stderr.lower()

    # The copied test entry point rejects skipped contracts and empty suites.
    gate_script = """
import unittest
from unittest.mock import patch
from tests.__main__ import main

skipped = unittest.FunctionTestCase(unittest.skip('synthetic skip')(lambda: None))
for suite in (unittest.TestSuite([skipped]), unittest.TestSuite()):
    with patch.object(unittest.defaultTestLoader, 'loadTestsFromNames', return_value=suite):
        assert main() == 1
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", gate_script], cwd=template, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped=1" in result.stderr

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-m", "tests"], cwd=template,
        env={**environment, "PYTHONPATH": ""},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No module named 'moyu_harmony_pilot'" in result.stderr

    # Exercise the installed console script and PEP 561 discovery from a clean venv.
    consumer_environment = tmp_path / "consumer-venv"
    venv.EnvBuilder(with_pip=True, symlinks=True).create(consumer_environment)
    bin_directory = consumer_environment / "bin"
    consumer_python = bin_directory / "python"
    cli = bin_directory / "harmony-pilot"
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    isolated_environment = {
        **build_environment, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1", "MYPYPATH": "",
    }
    result = subprocess.run(  # noqa: S603
        [
            str(consumer_python), "-m", "pip", "install", "--no-index", "--no-deps",
            str(next(artifacts.glob("*.whl"))),
        ],
        cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(  # noqa: S603
        [str(cli), "--version"], cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert project["version"] in result.stdout
    result = subprocess.run(  # noqa: S603
        [str(cli), "schema", "--json"], cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True

    # A wheel-only consumer can create and test its own renamed plugin, outside the checkout.
    generated = consumer / "community-plugin"
    result = subprocess.run(  # noqa: S603
        [str(cli), "plugins", "init", str(generated), "--namespace", "community", "--json"],
        cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    scaffold = json.loads(result.stdout)["data"]
    assert scaffold["module_name"] == "community_plugin"
    assert scaffold["action_ids"] == ["community.battery_check", "community.demo_toggle"]
    workflow = (generated / ".github/workflows/contract.yml").read_text(encoding="utf-8")
    assert "vars.PILOT_REF || 'v" + project["version"] + "'" in workflow
    generated_artifacts = tmp_path / "community-artifacts"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(generated_artifacts)],
        cwd=generated, env=build_environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(  # noqa: S603
        [
            str(consumer_python), "-m", "pip", "install", "--no-index", "--no-deps",
            str(next(generated_artifacts.glob("*.whl"))),
        ],
        cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(  # noqa: S603
        [
            str(consumer_python), "-m", "pip", "install", "--no-index", "--no-deps",
            "-r", str(generated / "requirements-test.txt"),
        ],
        cwd=generated, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(  # noqa: S603
        [str(consumer_python), "-m", "pip", "check"],
        cwd=generated, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(  # noqa: S603
        [str(consumer_python), "-m", "tests"], cwd=generated, env=isolated_environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ran 9 tests" in result.stderr
    for action_id in scaffold["action_ids"]:
        result = subprocess.run(  # noqa: S603
            [str(cli), "plugins", "describe", action_id, "--json"],
            cwd=consumer, env=isolated_environment,
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["data"]["action_id"] == action_id

    config = consumer / "mypy.ini"
    config.write_text("[mypy]\nstrict = true\n", encoding="utf-8")
    sample = consumer / "consumer.py"
    valid = (
        "from moyu_harmony_pilot.ui import Bounds\n"
        "bounds = Bounds(left=0, top=0, right=100, bottom=200)\n"
        "center: tuple[int, int] = bounds.center\n"
    )
    sample.write_text(valid, encoding="utf-8")
    mypy_command = [
        sys.executable, "-m", "mypy", "--config-file", str(config),
        "--python-executable", str(consumer_python), "--no-incremental", str(sample),
    ]
    result = subprocess.run(  # noqa: S603
        mypy_command, cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sample.write_text(valid.replace("left=0", 'left="invalid"'), encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        mypy_command, cwd=consumer, env=isolated_environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert '[arg-type]' in result.stdout
    assert 'Argument "left" to "Bounds"' in result.stdout
