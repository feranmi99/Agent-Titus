"""
Telegram broadcast dispatch service.

Formats `Item` alerts using Telegram MarkdownV2 and dispatches them via the
Telegram Bot HTTP API with retry, rate-limit (`retry_after`), and plain-text
fallback handling.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
from modules.common import Item, build_client

log = logging.getLogger(__name__)

# Characters that must be escaped in standard MarkdownV2 text nodes.
_MDV2_SPECIAL_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
# Inside MarkdownV2 inline link destinations `(...)`, only `)` and `\` need escaping.
_MDV2_URL_RE = re.compile(r"([)\\])")
# Inside MarkdownV2 inline code `` `...` ``, only `` ` `` and `\` need escaping.
_MDV2_CODE_RE = re.compile(r"([`\\])")

TELEGRAM_MAX_LEN = 4096


def escape_mdv2(text: str) -> str:
    """Escape arbitrary text for safe inclusion in Telegram MarkdownV2."""
    return _MDV2_SPECIAL_RE.sub(r"\\\1", text or "")


def escape_mdv2_url(url: str) -> str:
    """Escape a URL for safe use inside `[label](url)` in MarkdownV2."""
    return _MDV2_URL_RE.sub(r"\\\1", (url or "").strip())


def escape_mdv2_code(text: str) -> str:
    """Escape text for safe use inside inline backticks in MarkdownV2."""
    return _MDV2_CODE_RE.sub(r"\\\1", text or "")


def _category_label(category: Optional[str]) -> str:
    if category and category in config.CATEGORIES:
        return str(config.CATEGORIES[category].get("label", category.upper()))
    return "🔔 Alert"


def _format_timestamp(published: Optional[datetime]) -> str:
    dt = published or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_markdown_v2(item: Item) -> str:
    """Render an `Item` as a clean Telegram MarkdownV2 alert message."""
    label = escape_mdv2(_category_label(item.category))
    title = escape_mdv2(item.title.strip() or "Untitled")
    source = escape_mdv2_code(item.source)
    ts = escape_mdv2_code(_format_timestamp(item.published))

    lines: list[str] = [
        f"*{label}* · `score {item.score}`",
        "",
        f"*{title}*",
    ]

    snippet = item.snippet.strip()
    if snippet and snippet.lower() != item.title.strip().lower():
        lines.append("")
        lines.append(escape_mdv2(snippet))

    lines.append("")
    lines.append(f"📡 Source: `{source}`")
    lines.append(f"🕒 Time: `{ts}`")

    if item.matched:
        tags = ", ".join(item.matched[:6])
        lines.append(f"🏷 Signals: `{escape_mdv2_code(tags)}`")

    if item.url:
        lines.append(f"🔗 [View Original Post]({escape_mdv2_url(item.url)})")

    rendered = "\n".join(lines)
    if len(rendered) > TELEGRAM_MAX_LEN:
        rendered = rendered[: TELEGRAM_MAX_LEN - 20] + "\n…"
    return rendered


def format_plain(item: Item) -> str:
    """Render an `Item` as plain text (used for `--dry-run` and fallback)."""
    label = _category_label(item.category)
    ts = _format_timestamp(item.published)
    lines = [
        f"{label} [score {item.score}]",
        item.title.strip() or "Untitled",
    ]
    if item.snippet.strip() and item.snippet.strip().lower() != item.title.strip().lower():
        lines.append(item.snippet.strip())
    lines.append(f"Source: {item.source} | Time: {ts}")
    if item.matched:
        lines.append(f"Signals: {', '.join(item.matched[:6])}")
    if item.url:
        lines.append(f"Link: {item.url}")
    return "\n".join(lines)


def _post_telegram(
    client: httpx.Client,
    text: str,
    *,
    parse_mode: Optional[str] = "MarkdownV2",
    retries: int = 3,
) -> tuple[bool, int]:
    """
    Send a message payload to Telegram's `sendMessage` endpoint.

    Returns `(success, last_status_code)`.
    """
    endpoint = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    last_status = 0
    for attempt in range(1, retries + 1):
        try:
            resp = client.post(endpoint, json=payload)
            last_status = resp.status_code

            if resp.is_success:
                return True, last_status

            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = int(
                        (resp.json().get("parameters") or {}).get("retry_after", 5)
                    )
                except (ValueError, AttributeError):
                    pass
                log.warning("Telegram rate limit hit; sleeping %ds", retry_after)
                time.sleep(min(retry_after + 1, 120))
                continue

            if resp.status_code == 400 and parse_mode is not None:
                log.warning(
                    "Telegram rejected %s formatting (%s); will retry as plain text.",
                    parse_mode,
                    resp.text[:200],
                )
                return False, 400

            if 500 <= resp.status_code < 600:
                log.warning(
                    "Telegram server error %d (attempt %d/%d)",
                    resp.status_code,
                    attempt,
                    retries,
                )
                time.sleep(2.0 * attempt)
                continue

            log.error(
                "Telegram API error %d: %s",
                resp.status_code,
                resp.text[:250],
            )
            return False, last_status
        except httpx.HTTPError as exc:
            log.warning(
                "Telegram network error (attempt %d/%d): %s",
                attempt,
                retries,
                exc,
            )
            time.sleep(2.0 * attempt)

    return False, last_status


def send_alert(
    item: Item,
    *,
    client: Optional[httpx.Client] = None,
    dry_run: bool = False,
) -> bool:
    """
    Format and send a single `Item` alert to Telegram.

    In `dry_run` mode, prints the formatted alert to stdout and returns True.
    """
    if dry_run:
        sep = "-" * 72
        print(f"\n{sep}\n{format_plain(item)}\n{sep}", flush=True)
        return True

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.error(
            "Cannot dispatch alert: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID missing in .env"
        )
        return False

    md_text = format_markdown_v2(item)
    ctx = nullcontext(client) if client is not None else build_client()
    with ctx as http:
        ok, status = _post_telegram(http, md_text, parse_mode="MarkdownV2")
        if ok:
            return True
        if status == 400:
            ok_plain, _ = _post_telegram(http, format_plain(item), parse_mode=None)
            return ok_plain
        return False


def send_batch(
    items: list[Item],
    *,
    client: Optional[httpx.Client] = None,
    dry_run: bool = False,
    pacing_seconds: float = 0.35,
) -> list[str]:
    """
    Dispatch a list of alerts and return the UIDs that were successfully sent.
    """
    if not items:
        return []

    sent_uids: list[str] = []
    ctx = nullcontext(client) if client is not None else build_client()
    with ctx as http:
        for idx, item in enumerate(items):
            if send_alert(item, client=http, dry_run=dry_run):
                sent_uids.append(item.uid)
            if not dry_run and pacing_seconds > 0 and idx < len(items) - 1:
                time.sleep(pacing_seconds)

    return sent_uids


def send_test_message(client: Optional[httpx.Client] = None) -> bool:
    """Send a diagnostic ping to verify Telegram Bot Token & Chat ID."""
    now_utc = datetime.now(timezone.utc)
    test_item = Item(
        source="agent-titus:self-test",
        title="Agent Titus (Swarm Node 01) Connected",
        url="https://core.telegram.org/bots/api",
        snippet=(
            "Telemetry check passed. Monitoring pipelines are ready to dispatch "
            "Remote Job and Macro/Forex intelligence alerts."
        ),
        published=now_utc,
        category="forex",
        score=10,
        matched=["online", "self-test"],
        uid="self-test",
    )
    return send_alert(test_item, client=client, dry_run=False)

