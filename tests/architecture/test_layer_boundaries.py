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
        "httpx",
        "logfire",
        "knowledge_bot.infrastructure",
        "knowledge_bot.adapters",
    ),
    "application": (
        "fastapi",
        "workers",
        "httpx",
        "logfire",
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


def test_the_grounded_answer_prompt_has_one_home() -> None:
    """Generator adapters import the prompt instead of copying it.

    The Workers AI and Ollama adapters each carried a private copy of the same
    system prompt, so a rule change had to be made twice and the local adapter
    could silently test something production never sends.
    """
    marker = "You answer questions using ONLY the evidence below."
    owners = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if any(marker in value for value in _string_constants(path))
    )
    assert owners == ["infrastructure/prompt.py"]
