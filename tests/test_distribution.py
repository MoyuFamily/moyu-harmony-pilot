"""Build distributable artifacts offline and verify downstream validation resources."""

import email
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


def test_sdist_and_wheel_include_required_community_deliverables(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    files = (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
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
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next(artifacts.glob("*.tar.gz"))) as archive:
        names = {"/".join(name.split("/")[1:]) for name in archive.getnames()}
        required = {
            "tests/__init__.py",
            "tests/fakes.py",
            "tests/fixtures/harmony_61_home_layout.json",
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
    with zipfile.ZipFile(next(artifacts.glob("*.whl"))) as archive:
        names = archive.namelist()
        assert "moyu_harmony_pilot/mcp/server.py" in names
        assert any(
            name.endswith("/share/moyu-harmony-pilot/skills/harmony-pilot/SKILL.md")
            for name in names
        )
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
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
        cwd=template, capture_output=True, text=True, timeout=60, check=False,
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
import runpy
import sys
import moyu_harmony_pilot

assert pathlib.Path(moyu_harmony_pilot.__file__).is_relative_to(sys.argv[1])
runpy.run_module('tests', run_name='__main__')
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", script, str(installed)],
        cwd=template, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ran 8 tests" in result.stderr
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
