"""Smoke tests for package structure and CLI bootstrap."""

import tomllib
from pathlib import Path

from moyu_harmony_pilot import __version__
from moyu_harmony_pilot.cli.main import build_parser


def test_package_version_and_cli_bootstrap() -> None:
    assert __version__
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == __version__
    parser = build_parser()
    assert parser.prog == "harmony-pilot"
