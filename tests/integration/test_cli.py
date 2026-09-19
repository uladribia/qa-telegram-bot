# SPDX-License-Identifier: MIT
"""Tests for the CLI commands (write to tmp_path, no repo data, no network)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from knowledge_bot.cli import app

runner = CliRunner()


def test_import_whatsapp_writes_jsonl(tmp_path: Path) -> None:
    """The import command normalises a chat export to JSONL."""
    source = tmp_path / "chat.txt"
    source.write_text(
        "19/09/26, 09:32 - Ada: Hola\n19/09/26, 09:33 - Bob: Adeu\n", encoding="utf-8"
    )
    out = tmp_path / "out.jsonl"
    result = runner.invoke(app, ["import-whatsapp", str(source), "--out", str(out)])
    assert result.exit_code == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2
    assert "messages=2" in result.output


def test_snapshot_web_refuses_suspiciously_small_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A page with too few Q&A entries is rejected."""
    monkeypatch.setattr("knowledge_bot.cli.fetch_html", lambda url: "<html></html>")
    out = tmp_path / "qa.json"
    result = runner.invoke(
        app, ["snapshot-web", "--url", "https://example.com/", "--out", str(out)]
    )
    assert result.exit_code == 1
    assert not out.exists()
