"""Build distributable artifacts offline and verify downstream validation resources."""

import email
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
            "docs/mcp.md",
            "docs/bridge-worker.md",
            "skills/harmony-pilot/SKILL.md",
            "examples/actions/actions.json",
            "examples/actions/ui_summary.py",
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
