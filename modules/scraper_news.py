"""
RSS, Google News, Google Trends, & Macro/Forex news scraper.

Aggregates items from three public feed classes:
1. Direct RSS/Atom feeds (`config.RSS_FEEDS` — Forex & remote job boards)
2. Google News search RSS (`config.GOOGLE_NEWS_QUERIES`)
3. Google Trends trending RSS (`config.GOOGLE_TRENDS_GEOS`)
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import nullcontext
from typing import Optional
from urllib.parse import quote_plus

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

_RSS_ACCEPT_HEADERS = {
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
}


def _parse_standard_rss(
    raw_xml: str,
    *,
    source_name: str,
    category_hint: Optional[str],
    max_entries: int = 30,
) -> list[Item]:
    feed = feedparser.parse(raw_xml)
    if getattr(feed, "bozo", False) and not feed.entries:
        log.warning(
            "Malformed feed from %s: %s",
            source_name,
            getattr(feed, "bozo_exception", "unknown parse error"),
        )
        return []

    items: list[Item] = []
    for entry in feed.entries[:max_entries]:
        title = clean_text(getattr(entry, "title", ""), max_len=240)
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue

        raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
        snippet = clean_text(raw_summary, max_len=360)
        # Google News descriptions frequently mirror the headline verbatim.
        if snippet and (snippet == title or title.startswith(snippet[:60])):
            snippet = ""

        published = struct_time_to_dt(
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        guid = (getattr(entry, "id", "") or "").strip()
        uid = (
            hashlib.sha1(f"{source_name}:{guid}".encode("utf-8")).hexdigest()
            if guid
            else ""
        )

        items.append(
            Item(
                source=source_name,
                title=title,
                url=link,
                snippet=snippet,
                published=published,
                category_hint=category_hint,
                uid=uid,
            )
        )

    return items


def _fetch_direct_feeds(client: httpx.Client) -> list[Item]:
    items: list[Item] = []
    for feed_name, (url, category_hint) in config.RSS_FEEDS.items():
        resp = get_with_retry(client, url, headers=_RSS_ACCEPT_HEADERS)
        if resp is None:
            continue
        parsed = _parse_standard_rss(
            resp.text,
            source_name=f"rss:{feed_name}",
            category_hint=category_hint,
        )
        items.extend(parsed)
    return items


def _fetch_google_news(client: httpx.Client) -> list[Item]:
    items: list[Item] = []
    for query, category_hint in config.GOOGLE_NEWS_QUERIES.items():
        url = (
            f"https://news.google.com/rss/search?"
            f"q={quote_plus(query)}&{config.GOOGLE_NEWS_LOCALE}"
        )
        resp = get_with_retry(client, url, headers=_RSS_ACCEPT_HEADERS)
        if resp is None:
            continue
        parsed = _parse_standard_rss(
            resp.text,
            source_name="gnews",
            category_hint=category_hint,
            max_entries=20,
        )
        items.extend(parsed)
    return items


def _fetch_google_trends(client: httpx.Client) -> list[Item]:
    items: list[Item] = []
    for geo in config.GOOGLE_TRENDS_GEOS:
        url = f"https://trends.google.com/trending/rss?geo={quote_plus(geo)}"
        resp = get_with_retry(client, url, headers=_RSS_ACCEPT_HEADERS)
        if resp is None:
            continue

        feed = feedparser.parse(resp.text)
        for entry in feed.entries[:25]:
            trend_term = clean_text(getattr(entry, "title", ""), max_len=120)
            if not trend_term:
                continue

            news_headline = clean_text(entry.get("ht_news_item_title", ""), max_len=220)
            news_url = (entry.get("ht_news_item_url", "") or "").strip()
            traffic = clean_text(entry.get("ht_approx_traffic", ""), max_len=40)
            fallback_link = (getattr(entry, "link", "") or "").strip()

            title = (
                f"Trending ({traffic}): {trend_term} — {news_headline}"
                if traffic and news_headline
                else f"Trending: {trend_term}" + (f" — {news_headline}" if news_headline else "")
            )
            snippet = clean_text(entry.get("ht_news_item_snippet", ""), max_len=320)
            published = struct_time_to_dt(
                getattr(entry, "published_parsed", None)
                or getattr(entry, "updated_parsed", None)
            )
            target_url = news_url or fallback_link
            if not target_url:
                continue

            items.append(
                Item(
                    source=f"gtrends:{geo}",
                    title=title,
                    url=target_url,
                    snippet=snippet,
                    published=published,
                    category_hint=None,  # Must earn score purely via keyword match
                    uid=f"gtrends:{geo}:{trend_term.lower()}",
                )
            )

    return items


def fetch_items(client: Optional[httpx.Client] = None) -> list[Item]:
    """Collect candidate news/RSS/Google Trends items across all configured feeds."""
    if not config.ENABLE_NEWS:
        return []

    all_items: list[Item] = []
    ctx = nullcontext(client) if client is not None else build_client()
    with ctx as http:
        for fetcher_name, fetcher in (
            ("Direct RSS", _fetch_direct_feeds),
            ("Google News", _fetch_google_news),
            ("Google Trends", _fetch_google_trends),
        ):
            try:
                batch = fetcher(http)
                all_items.extend(batch)
            except Exception as exc:
                log.exception("Unexpected error in %s scraper: %s", fetcher_name, exc)

    log.info("News/RSS scraper collected %d candidate item(s)", len(all_items))
    return all_items
