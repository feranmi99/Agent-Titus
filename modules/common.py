"""Shared data model and HTTP helpers used by all scrapers."""

from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

import config

log = logging.getLogger(__name__)

_INLINE_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|s|a|code|mark|sub|sup)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class Item:
    """A single piece of content discovered by a scraper."""

    source: str  # e.g. "reddit:r/forhire", "news:ForexLive", "x"
    title: str
    url: str
    snippet: str = ""
    published: Optional[datetime] = None
    category_hint: Optional[str] = None  # "jobs" / "forex" bias from the source
    # Filled in by the filter stage:
    category: Optional[str] = None
    score: int = 0
    matched: list[str] = field(default_factory=list)
    uid: str = ""

    def __post_init__(self) -> None:
        if not self.uid:
            basis = self.url or f"{self.source}|{self.title}"
            self.uid = hashlib.sha1(basis.encode("utf-8")).hexdigest()

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.snippet}"


def clean_text(raw: str, max_len: int = 400) -> str:
    """Strip HTML tags/entities and collapse whitespace, truncating to max_len."""
    if not raw:
        return ""
    without_inline = _INLINE_TAG_RE.sub("", raw)
    text = html.unescape(_TAG_RE.sub(" ", without_inline))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def struct_time_to_dt(st) -> Optional[datetime]:
    """Convert a feedparser time.struct_time (UTC) into an aware datetime."""
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def build_client() -> httpx.Client:
    return httpx.Client(
        timeout=config.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": config.USER_AGENT},
        follow_redirects=True,
    )


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> Optional[httpx.Response]:
    """
    GET a URL with retries on network errors / 5xx / 429.

    Returns the response on success (2xx) or None if all attempts fail.
    Never raises, so a single bad source can't crash the agent.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code == 429:
                wait = _retry_after(resp, default=backoff * attempt * 5)
                log.warning("Rate limited by %s; waiting %.0fs", url, wait)
                time.sleep(min(wait, 120))
                continue
            if 500 <= resp.status_code < 600:
                log.warning("Server error %s from %s", resp.status_code, url)
                time.sleep(backoff * attempt)
                continue
            if resp.is_success:
                return resp
            log.warning("HTTP %s from %s", resp.status_code, url)
            return None
        except httpx.HTTPError as exc:
            log.warning("Network error for %s (attempt %d/%d): %s", url, attempt, retries, exc)
            time.sleep(backoff * attempt)
    log.error("Giving up on %s after %d attempts", url, retries)
    return None


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return float(resp.headers.get("retry-after", default))
    except ValueError:
        return default
