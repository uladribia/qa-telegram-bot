# SPDX-License-Identifier: MIT
"""Typer CLI for offline snapshots, imports, seeding, and operations (spec §3.2).

The CLI reuses the same parsers as the core and talks to the deployed Worker's
internal endpoints for anything that must touch D1. It never commits data.
"""

import json
from pathlib import Path

import httpx
import typer

from knowledge_bot.adapters.inbound.web_snapshot import fetch_html, parse_qa_html
from knowledge_bot.adapters.inbound.whatsapp_export import parse_export
from knowledge_bot.infrastructure.settings import Settings

MIN_QA_COUNT = 30

app = typer.Typer(no_args_is_help=True, help="Knowledge bot operations.")
_BASE_URL = typer.Option(
    ..., "--base-url", envvar="BOT_BASE_URL", help="Deployed Worker URL."
)
_QA_FILE = typer.Option(None, "--qa", help="Seed Q&A JSON file.")
_MESSAGES_FILE = typer.Option(None, "--messages", help="Imported messages JSONL file.")


def _internal_headers(settings: Settings) -> dict[str, str]:
    return {"X-Internal-Key": settings.internal_admin_key}


@app.command("snapshot-web")
def snapshot_web(
    url: str = typer.Option(..., "--url", help="Q&A page to snapshot."),
    out: Path = typer.Option(..., "--out", help="Output JSON file."),
) -> None:
    """Fetch and parse a Q&A page into a seed JSON file."""
    html = fetch_html(url)
    entries = parse_qa_html(html, source_url=url)
    if len(entries) < MIN_QA_COUNT:
        typer.echo(f"Refusing to write: only {len(entries)} Q&A entries found")
        raise typer.Exit(code=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = [entry.model_dump(mode="json") for entry in entries]
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    in_review = sum(1 for entry in entries if entry.status == "in_review")
    typer.echo(f"Wrote {len(entries)} Q&A to {out} ({in_review} in review)")


@app.command("import-whatsapp")
def import_whatsapp(
    path: Path = typer.Argument(..., help="WhatsApp text export."),
    out: Path = typer.Option(..., "--out", help="Output JSONL file."),
) -> None:
    """Parse a WhatsApp text export into a normalized-messages JSONL file."""
    result = parse_export(path.read_text(encoding="utf-8", errors="replace"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for message in result.messages:
            handle.write(
                json.dumps(message.model_dump(mode="json"), ensure_ascii=False) + "\n"
            )
    stats = result.stats
    typer.echo(
        f"messages={stats.messages} text={stats.text_messages} "
        f"media={stats.media_messages} system={stats.system_messages} "
        f"failures={stats.parse_failures}"
    )


def _load_payload(qa: Path | None, messages: Path | None) -> dict[str, object]:
    payload: dict[str, object] = {}
    if qa is not None:
        payload["qa"] = json.loads(qa.read_text(encoding="utf-8"))
    if messages is not None:
        payload["messages"] = [
            json.loads(line)
            for line in messages.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return payload


@app.command()
def seed(
    qa: Path = _QA_FILE,
    messages: Path = _MESSAGES_FILE,
    scope: str = typer.Option(
        "global",
        "--scope",
        help='Knowledge scope: "global" or the group chat id this seed belongs to.',
    ),
    base_url: str = _BASE_URL,
) -> None:
    """Seed the parsed Q&A and/or messages into D1."""
    payload = _load_payload(qa, messages)
    if not payload:
        typer.echo("Nothing to seed: pass --qa and/or --messages")
        raise typer.Exit(code=1)
    payload["scope"] = scope
    settings = Settings()
    response = httpx.post(
        f"{base_url}/internal/seed",
        json=payload,
        headers=_internal_headers(settings),
        timeout=300.0,
    )
    response.raise_for_status()
    typer.echo(response.text)


@app.command("group")
def group_add(
    chat_id: str = typer.Argument(..., help="Telegram group chat id."),
    title: str = typer.Option(None, "--title", help="Human-readable group name."),
    base_url: str = _BASE_URL,
) -> None:
    """Register a served Telegram group (idempotent; refreshes the title)."""
    settings = Settings()
    response = httpx.post(
        f"{base_url}/internal/groups",
        json={"chat_id": chat_id, "title": title},
        headers=_internal_headers(settings),
        timeout=60.0,
    )
    response.raise_for_status()
    typer.echo(response.text)


@app.command()
def reindex(base_url: str = _BASE_URL) -> None:
    """Rebuild the vector store from D1."""
    settings = Settings()
    response = httpx.post(
        f"{base_url}/internal/reindex",
        headers=_internal_headers(settings),
        timeout=600.0,
    )
    response.raise_for_status()
    typer.echo(response.text)


@app.command("set-webhook")
def set_webhook(base_url: str = _BASE_URL) -> None:
    """Register the Telegram webhook for the deployed Worker."""
    settings = Settings()
    url = f"{base_url.rstrip('/')}/telegram/webhook"
    response = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
        json={
            "url": url,
            "secret_token": settings.telegram_webhook_secret,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        },
        timeout=60.0,
    )
    response.raise_for_status()
    typer.echo(f"Webhook set to {url}")


@app.command("delete-webhook")
def delete_webhook() -> None:
    """Remove the Telegram webhook."""
    settings = Settings()
    response = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/deleteWebhook",
        timeout=60.0,
    )
    response.raise_for_status()
    typer.echo("Webhook deleted")
