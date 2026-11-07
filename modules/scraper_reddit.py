"""
Reddit thread & community scanner.

Polls the public Atom `/new/.rss` feed of each configured subreddit in
`config.REDDIT_SUBREDDITS`, cleans Reddit's HTML boilerplate footer, and emits
normalized `Item` records for downstream relevance scoring and deduplication.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import nullcontext
from typing import Optional

import feedparser
import httpx

import config
from modules.common import (
    Item,
    build_client,
    clean_text,
    get_with_retry,
    struct_time_to_dt,
)

log = logging.getLogger(__name__)

# Matches Reddit's Atom feed footer: "submitted by /u/... [link] [comments]"
_REDDIT_FOOTER_RE = re.compile(
    r"\bsubmitted by\s+/u/\S+.*?\[comments\]\s*$",
    re.IGNORECASE | re.DOTALL,
)

SUBREDDIT_DELAY_SECONDS: float = 0.6


def _clean_reddit_summary(raw_html: str, max_len: int = 380) -> str:
    cleaned = clean_text(raw_html, max_len=max_len + 200)
    cleaned = _REDDIT_FOOTER_RE.sub("", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def fetch_subreddit(
    client: httpx.Client,
    subreddit: str,
    category_hint: Optional[str] = None,
    limit: int = 25,
) -> list[Item]:
    """Fetch newest posts from a single subreddit via its Atom RSS feed."""
    url = f"https://www.reddit.com/r/{subreddit}/new/.rss"
    resp = get_with_retry(
        client,
        url,
        params={"limit": limit},
        headers={"Accept": "application/atom+xml, application/rss+xml, application/xml, */*"},
    )
    if resp is None:
        return []

    feed = feedparser.parse(resp.text)
    if getattr(feed, "bozo", False) and not feed.entries:
        log.warning(
            "Malformed RSS feed for r/%s: %s",
            subreddit,
            getattr(feed, "bozo_exception", "unknown parse error"),
        )
        return []

    items: list[Item] = []
    for entry in feed.entries:
        title = clean_text(getattr(entry, "title", ""), max_len=240)
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue

        raw_body = ""
        if getattr(entry, "content", None):
            raw_body = entry.content[0].get("value", "")
        elif getattr(entry, "summary", None):
            raw_body = entry.summary

        snippet = _clean_reddit_summary(raw_body)
        published = struct_time_to_dt(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        entry_id = (getattr(entry, "id", "") or "").strip()
        uid = f"reddit:{entry_id}" if entry_id else ""

        items.append(
            Item(
                source=f"reddit:r/{subreddit}",
                title=title,
                url=link,
                snippet=snippet,
                published=published,
                category_hint=category_hint,
                uid=uid,
            )
        )

    return items


def fetch_items(
    client: Optional[httpx.Client] = None,
    *,
    delay_seconds: float = SUBREDDIT_DELAY_SECONDS,
) -> list[Item]:
    """Poll all configured subreddits and return collected candidate items."""
    if not config.ENABLE_REDDIT:
        return []

    all_items: list[Item] = []
    subreddits = list(config.REDDIT_SUBREDDITS.items())

    ctx = nullcontext(client) if client is not None else build_client()
    with ctx as http:
        for idx, (subreddit, category_hint) in enumerate(subreddits):
            try:
                sub_items = fetch_subreddit(http, subreddit, category_hint)
                all_items.extend(sub_items)
            except Exception as exc:  # defensive guard per subreddit
                log.exception("Unexpected error scraping r/%s: %s", subreddit, exc)

            if delay_seconds > 0 and idx < len(subreddits) - 1:
                time.sleep(delay_seconds)

    log.info(
        "Reddit scraper collected %d candidate post(s) across %d subreddit(s)",
        len(all_items),
        len(subreddits),
    )
    return all_items

