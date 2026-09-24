# SPDX-License-Identifier: MIT
"""Architecture tests enforcing the Clean Architecture dependency rule."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "knowledge_bot"

FORBIDDEN_PREFIXES: dict[str, tuple[str, ...]] = {
    "domain": (
        "fastapi",
        "workers",
        "loguru",
        "httpx",
        "knowledge_bot.infrastructure",
        "knowledge_bot.adapters",
    ),
    "application": (
        "fastapi",
        "workers",
        "loguru",
        "httpx",
        "knowledge_bot.infrastructure",
        "knowledge_bot.adapters",
    ),
}


def _iter_modules(layer: str) -> list[Path]:
    layer_dir = SRC / layer
    return sorted(layer_dir.rglob("*.py")) if layer_dir.exists() else []


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _is_forbidden(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


@pytest.mark.parametrize("layer", sorted(FORBIDDEN_PREFIXES))
def test_layer_has_no_forbidden_imports(layer: str) -> None:
    """Domain and application must not import transport or infrastructure code."""
    prefixes = FORBIDDEN_PREFIXES[layer]
    for module_path in _iter_modules(layer):
        offending = sorted(
            module
            for module in _imported_modules(module_path)
            if _is_forbidden(module, prefixes)
        )
        assert not offending, f"{module_path} imports {offending}"


@pytest.mark.parametrize("layer", ("domain", "application", "ports"))
def test_core_has_no_connector_source_literals(layer: str) -> None:
    """Connector names and source kinds are declared outside the core."""
    forbidden = {"telegram", "whatsapp", "whatsapp_import", "web_seed"}
    for module_path in _iter_modules(layer):
        assert not _string_constants(module_path) & forbidden, module_path
