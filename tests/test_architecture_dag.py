"""Static architecture guardrails enforcing the 7-layer DAG and zero-dependency core."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

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


def _imported_subpackages(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[0] == "moyu_harmony_pilot":
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if len(parts) >= 2 and parts[0] == "moyu_harmony_pilot":
                found.add(parts[1])
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


def test_core_pyproject_dependencies_remain_empty() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject.get("project", {}).get("dependencies", [])
    assert deps == [], f"Core runtime dependencies must remain stdlib-only, found: {deps}"
