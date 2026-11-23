"""Static guardrails for the production DAG and stdlib-only testing consumer."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "src" / "moyu_harmony_pilot"

# Allowed intra-package imports per layer (see AGENTS.md section 2).
ALLOWED_LAYER_IMPORTS: dict[str, set[str]] = {
    "hdc": {"hdc"},
    "ui": {"hdc", "ui"},
    "device": {"hdc", "ui", "device"},
    "vision": {"vision"},
    "actions": {"hdc", "ui", "device", "vision", "actions"},
    "bridge": {"hdc", "ui", "device", "vision", "actions", "bridge"},
    "cli": {"hdc", "ui", "device", "vision", "actions", "bridge", "cli"},
    "mcp": {"hdc", "ui", "device", "vision", "actions", "mcp"},
}
# Public testing helpers may consume any production layer, including Bridge fakes.
ALLOWED_LAYER_IMPORTS["testing"] = set(ALLOWED_LAYER_IMPORTS) | {"testing"}


def _imported_subpackages(py_file: Path, package_root: Path = PKG_ROOT) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[0] == "moyu_harmony_pilot":
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if node.level:
                package = ["moyu_harmony_pilot", *py_file.relative_to(package_root).parts[:-1]]
                parts = package[:len(package) - node.level + 1] + parts
            if len(parts) >= 2 and parts[0] == "moyu_harmony_pilot":
                found.add(parts[1])
            elif parts == ["moyu_harmony_pilot"]:
                found.update(
                    alias.name for alias in node.names if alias.name in ALLOWED_LAYER_IMPORTS
                )
    return found


def test_strict_one_way_layer_dag() -> None:
    violations: list[str] = []
    for layer_dir in sorted(PKG_ROOT.iterdir()):
        if not layer_dir.is_dir() or layer_dir.name.startswith("__"):
            continue
        layer = layer_dir.name
        assert layer in ALLOWED_LAYER_IMPORTS, f"Unregistered layer directory: {layer}"
        allowed = ALLOWED_LAYER_IMPORTS[layer]

        for py_file in sorted(layer_dir.rglob("*.py")):
            imported = _imported_subpackages(py_file)
            disallowed = imported - allowed
            if disallowed:
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}: layer `{layer}` imported disallowed layer(s): {sorted(disallowed)}"
                )

    assert not violations, "DAG layer violation(s) detected:\n" + "\n".join(violations)


def test_production_never_imports_testing() -> None:
    for layer, allowed in ALLOWED_LAYER_IMPORTS.items():
        if layer != "testing":
            assert "testing" not in allowed
    for py_file in PKG_ROOT.rglob("*.py"):
        if py_file.relative_to(PKG_ROOT).parts[0] != "testing":
            assert "testing" not in _imported_subpackages(py_file), str(py_file)


@pytest.mark.parametrize("source", [
    "import moyu_harmony_pilot.testing",
    "import moyu_harmony_pilot.testing.fakes as fake",
    "from moyu_harmony_pilot.testing import FakeHdcTransport",
    "from moyu_harmony_pilot import testing as helper",
    "from ..testing import FakeHdcTransport",
    "from .. import testing",
])
def test_testing_import_guard_covers_absolute_and_relative_imports(tmp_path, source):
    module = tmp_path / "actions" / "example.py"
    module.parent.mkdir()
    module.write_text(source, encoding="utf-8")
    assert _imported_subpackages(module, tmp_path) == {"testing"}


def test_testing_helpers_have_no_external_runtime_dependencies() -> None:
    for py_file in (PKG_ROOT / "testing").rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root in sys.stdlib_module_names | {"moyu_harmony_pilot"}, (py_file, name)


def test_core_pyproject_dependencies_remain_empty() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject.get("project", {}).get("dependencies", [])
    assert deps == [], f"Core runtime dependencies must remain stdlib-only, found: {deps}"
