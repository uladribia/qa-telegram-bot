# SPDX-License-Identifier: MIT
"""Typer CLI for offline snapshots, imports, seeding, and operations (spec §3.2).

The CLI reuses the same parsers as the core and talks to the deployed Worker's
internal endpoints for anything that must touch D1. It never commits data.
"""

import json
import time
from pathlib import Path
from typing import cast

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


_SEED_MESSAGE_CHUNK = 50


def _post_seed(base_url: str, payload: dict[str, object]) -> dict[str, int]:
    """Push one seed payload to the Worker, retrying transient failures.

    The free plan's CPU limit makes a cold isolate drop the first heavy
    request; seeding is idempotent, so a retry just continues where it left
    off.
    """
    settings = Settings()
    response = None
    for attempt in range(3):
        response = httpx.post(
            f"{base_url}/internal/seed",
            json=payload,
            headers=_internal_headers(settings),
            timeout=300.0,
        )
        if response.status_code < 500:
            break
        typer.echo(
            f"  retrying after HTTP {response.status_code} (attempt {attempt + 2}/3)"
        )
        time.sleep(2)
    response.raise_for_status()
    return cast(dict[str, int], response.json())


@app.command()
def seed(
    qa: Path = _QA_FILE,
    messages: Path = _MESSAGES_FILE,
    scope: str = typer.Option(
        "global",
        "--scope",
        help='Knowledge scope: "global" or the group chat id this seed belongs to.',
    ),
    renew: bool = typer.Option(
        False,
        "--renew",
        help="Update entries whose answer changed instead of skipping them.",
    ),
    base_url: str = _BASE_URL,
) -> None:
    """Seed the parsed Q&A and/or messages into D1 (in idempotent batches)."""
    payload = _load_payload(qa, messages)
    if not payload:
        typer.echo("Nothing to seed: pass --qa and/or --messages")
        raise typer.Exit(code=1)
    messages_payload = cast("list[dict[str, object]]", payload.get("messages") or [])
    totals = {"qa": 0, "qa_skipped": 0, "qa_renewed": 0, "messages": 0, "indexed": 0}
    if "qa" in payload:
        totals.update(
            _post_seed(
                base_url,
                {
                    "qa": payload["qa"],
                    "scope": scope,
                    **({"renew": True} if renew else {}),
                },
            )
        )
    for start in range(0, len(messages_payload), _SEED_MESSAGE_CHUNK):
        chunk = messages_payload[start : start + _SEED_MESSAGE_CHUNK]
        counts = _post_seed(base_url, {"messages": chunk, "scope": scope})
        totals["messages"] += counts.get("messages", 0)
        typer.echo(f"  seeded {start + len(chunk)}/{len(messages_payload)} messages")
    typer.echo(
        f"qa={totals['qa']} qa_skipped={totals['qa_skipped']} "
        f"qa_renewed={totals['qa_renewed']} messages={totals['messages']} "
        f"indexed={totals.get('indexed', 0)}"
    )


group_app = typer.Typer(no_args_is_help=True, help="Register served groups.")
app.add_typer(group_app, name="group")


@group_app.command("add")
def group_add(
    chat_id: str = typer.Option(
        ..., "--chat-id", help="Telegram group chat id (negative for groups)."
    ),
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


@app.command("review")
def review(
    out: Path = typer.Option(
        None, "--out", help="Write the markdown report here (default: stdout)."
    ),
    base_url: str = _BASE_URL,
) -> None:
    """Build the human knowledge review report (read-only)."""
    settings = Settings()
    response = httpx.post(
        f"{base_url}/internal/review",
        headers=_internal_headers(settings),
        timeout=120.0,
    )
    response.raise_for_status()
    report = response.json()["report"]
    if out is None:
        typer.echo(report)
        return
    out.write_text(report, encoding="utf-8")
    typer.echo(f"Wrote review report to {out}")


@app.command()
def revert(
    qa_item_id: str = typer.Argument(help="The qa item id to roll back one version."),
    base_url: str = _BASE_URL,
) -> None:
    """Revert an approved correction to the version it superseded (CLI-only)."""
    settings = Settings()
    response = httpx.post(
        f"{base_url}/internal/revert",
        json={"qa_item_id": qa_item_id},
        headers=_internal_headers(settings),
        timeout=120.0,
    )
    if response.status_code == 404:
        typer.echo(f"Nothing to revert for {qa_item_id}")
        raise typer.Exit(code=1)
    response.raise_for_status()
    body = response.json()
    typer.echo(
        f"Reverted {qa_item_id}: current version is now {body['restored_version_id']}"
    )


@app.command()
def reindex(
    batch: int = typer.Option(
        50, "--batch", help="Records per request (keep small: free CPU limits)."
    ),
    qa_after: str | None = typer.Option(None, help="Resume cursor for QA versions."),
    msg_after: str | None = typer.Option(None, help="Resume cursor for messages."),
    base_url: str = _BASE_URL,
) -> None:
    """Rebuild the vector store from D1, in idempotent batches."""
    totals = {"qa": 0, "messages": 0}
    try:
        while True:
            settings = Settings()
            response = None
            for attempt in range(3):
                response = httpx.post(
                    f"{base_url}/internal/reindex",
                    json={"qa_after": qa_after, "msg_after": msg_after, "limit": batch},
                    headers=_internal_headers(settings),
                    timeout=300.0,
                )
                if response.status_code < 500:
                    break
                typer.echo(
                    "  retrying after HTTP"
                    f" {response.status_code} (attempt {attempt + 2}/3)"
                )
                time.sleep(2)
            response.raise_for_status()
            counts = response.json()
            qa_after = counts.get("qa_after")
            msg_after = counts.get("msg_after")
            totals["qa"] += counts.get("qa", 0)
            totals["messages"] += counts.get("messages", 0)
            typer.echo(f"  indexed {totals['qa']} qa, {totals['messages']} messages")
            if qa_after is None and msg_after is None:
                break
    except httpx.HTTPError as error:
        typer.echo(
            f"interrupted after {totals}: {error}; resume with:"
            f" --qa-after {qa_after or ''} --msg-after {msg_after or ''}"
        )
        raise
    typer.echo(f"done: qa={totals['qa']} messages={totals['messages']}")
    if qa_after is not None or msg_after is not None:
        typer.echo(
            f"resume with: --qa-after {qa_after or ''} --msg-after {msg_after or ''}"
        )


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
