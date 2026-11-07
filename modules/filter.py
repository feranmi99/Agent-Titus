"""
Keyword relevance scoring, age filtering, and persistent deduplication.

Maintains `data/seen_items.json` using atomic writes so abrupt VM restarts or
power interruptions never corrupt state.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import config
from modules.common import Item

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CompiledKeyword:
    keyword: str
    weight: int
    pattern: re.Pattern[str]


_compiled_cache_id: Optional[int] = None
_compiled_categories: dict[str, list[_CompiledKeyword]] = {}


def _build_keyword_pattern(keyword: str, all_lower_keywords: set[str]) -> re.Pattern[str]:
    """
    Build a case-insensitive boundary-aware regex for `keyword`.

    Prevents classic substring false positives such as:
    - "war" matching "software" or "award"
    - "boe" matching "Boeing"
    - "turing" matching "manufacturing"
    - "yen" matching "Nguyen"
    """
    escaped = re.escape(keyword.strip())
    # Allow flexible whitespace between words in multi-word phrases.
    escaped = escaped.replace(r"\ ", r"\s+")

    prefix = r"\b" if keyword[0].isalnum() else ""
    if keyword[-1].isalnum():
        allow_plural = (
            len(keyword) >= 4
            and keyword[-1].isalpha()
            and not keyword.lower().endswith("s")
            and f"{keyword.lower()}s" not in all_lower_keywords
        )
        suffix = r"(?:s)?\b" if allow_plural else r"\b"
    else:
        suffix = ""

    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


def _get_compiled_categories() -> dict[str, list[_CompiledKeyword]]:
    global _compiled_cache_id, _compiled_categories
    if _compiled_cache_id == id(config.CATEGORIES) and _compiled_categories:
        return _compiled_categories

    compiled: dict[str, list[_CompiledKeyword]] = {}
    for cat_name, cat_cfg in config.CATEGORIES.items():
        raw_keywords: dict[str, int] = cat_cfg.get("keywords", {})
        lower_set = {k.strip().lower() for k in raw_keywords if k.strip()}
        compiled_list: list[_CompiledKeyword] = []
        for kw, weight in raw_keywords.items():
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            compiled_list.append(
                _CompiledKeyword(
                    keyword=kw_clean,
                    weight=int(weight),
                    pattern=_build_keyword_pattern(kw_clean, lower_set),
                )
            )
        compiled[cat_name] = compiled_list

    _compiled_categories = compiled
    _compiled_cache_id = id(config.CATEGORIES)
    return _compiled_categories


def score_item(item: Item) -> Item:
    """
    Evaluate `item` against all configured categories and mutate its
    `category`, `score`, and `matched` fields in place.
    """
    compiled = _get_compiled_categories()
    haystack = item.text

    best_category: Optional[str] = None
    best_score: Optional[int] = None
    best_matched: list[str] = []

    for cat_name, rules in compiled.items():
        score = 0
        positive_hits = 0
        matched: list[str] = []

        for rule in rules:
            if rule.pattern.search(haystack):
                score += rule.weight
                if rule.weight > 0:
                    positive_hits += 1
                    matched.append(rule.keyword)

        # Grant a +1 domain affinity bonus when the source matches the category
        # AND at least one positive keyword actually matched.
        if item.category_hint == cat_name and positive_hits > 0:
            score += 1

        is_better = (
            best_score is None
            or score > best_score
            or (score == best_score and item.category_hint == cat_name)
        )
        if is_better:
            best_category = cat_name
            best_score = score
            best_matched = matched

    item.category = best_category
    item.score = best_score or 0
    item.matched = best_matched
    return item


def is_stale(
    item: Item,
    *,
    now: Optional[datetime] = None,
    max_age_hours: Optional[int] = None,
) -> bool:
    """Return True if `item.published` is older than `max_age_hours`."""
    if item.published is None:
        return False
    limit_hours = config.MAX_ITEM_AGE_HOURS if max_age_hours is None else max_age_hours
    if limit_hours <= 0:
        return False
    ref = now or datetime.now(timezone.utc)
    pub = (
        item.published
        if item.published.tzinfo is not None
        else item.published.replace(tzinfo=timezone.utc)
    )
    return (ref - pub) > timedelta(hours=limit_hours)


class SeenStore:
    """
    JSON-backed persistent set of seen item UIDs with bounded size and atomic saves.
    """

    def __init__(
        self,
        path: Path = config.SEEN_ITEMS_PATH,
        max_items: int = config.SEEN_STORE_MAX_ITEMS,
    ) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.seeded: bool = False
        self._items: dict[str, float] = {}
        self.load()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, uid: str) -> bool:
        return uid in self._items

    def load(self) -> None:
        """Load state from disk if present; recover gracefully on corruption."""
        if not self.path.exists():
            self.seeded = False
            self._items = {}
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not parse %s (%s); starting with empty state.", self.path, exc)
            self._backup_corrupt_file()
            self.seeded = False
            self._items = {}
            return

        now_ts = time.time()
        if isinstance(raw, dict) and "items" in raw and isinstance(raw["items"], dict):
            self.seeded = bool(raw.get("seeded", True))
            self._items = {
                str(k): float(v) if isinstance(v, (int, float)) else now_ts
                for k, v in raw["items"].items()
            }
        elif isinstance(raw, list):
            self.seeded = True
            self._items = {str(k): now_ts for k in raw}
        elif isinstance(raw, dict):
            self.seeded = True
            self._items = {
                str(k): float(v) if isinstance(v, (int, float)) else now_ts
                for k, v in raw.items()
            }
        else:
            self.seeded = False
            self._items = {}

    def _backup_corrupt_file(self) -> None:
        try:
            backup = self.path.with_suffix(".corrupt.json")
            os.replace(self.path, backup)
        except OSError:
            pass

    def mark_seen(self, uids: Iterable[str]) -> None:
        now_ts = time.time()
        for uid in uids:
            if uid:
                self._items[uid] = now_ts

    def prune(self) -> None:
        if len(self._items) <= self.max_items:
            return
        # Keep the most recently recorded `max_items` UIDs.
        sorted_pairs = sorted(self._items.items(), key=lambda kv: kv[1], reverse=True)
        self._items = dict(sorted_pairs[: self.max_items])

    def save(self) -> None:
        """Atomically persist the dedup store to disk."""
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        payload = {
            "version": 1,
            "seeded": self.seeded,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": self._items,
        }
        try:
            tmp_path.write_text(
                json.dumps(payload, indent=2, sort_keys=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
        except OSError as exc:
            log.error("Failed to save seen store to %s: %s", self.path, exc)


@dataclass
class FilterResult:
    """Outcome of running a batch of scraped items through the filter pipeline."""

    alerts: list[Item]
    rejected_uids: list[str]
    overflow_uids: list[str]
    skipped_seen: int = 0
    skipped_stale: int = 0
    skipped_low_score: int = 0


def filter_items(
    items: Iterable[Item],
    store: SeenStore,
    *,
    min_score: Optional[int] = None,
    max_alerts: Optional[int] = None,
    now: Optional[datetime] = None,
) -> FilterResult:
    """
    Deduplicate, age-check, score, and rank candidate items.

    Note: Caller should mark `rejected_uids` + `overflow_uids` as seen right away,
    and mark each `alert.uid` as seen once dispatched so network failures during
    notification don't permanently drop high-value alerts.
    """
    threshold = config.MIN_RELEVANCE_SCORE if min_score is None else min_score
    cap = config.MAX_ALERTS_PER_CYCLE if max_alerts is None else max_alerts

    seen_in_batch_uids: set[str] = set()
    seen_in_batch_urls: set[str] = set()

    qualified: list[Item] = []
    rejected_uids: list[str] = []
    skipped_seen = 0
    skipped_stale = 0
    skipped_low_score = 0

    for item in items:
        norm_url = item.url.strip()
        if (
            item.uid in store
            or item.uid in seen_in_batch_uids
            or (norm_url and norm_url in seen_in_batch_urls)
        ):
            skipped_seen += 1
            continue

        seen_in_batch_uids.add(item.uid)
        if norm_url:
            seen_in_batch_urls.add(norm_url)

        if is_stale(item, now=now):
            skipped_stale += 1
            rejected_uids.append(item.uid)
            continue

        score_item(item)
        if item.score < threshold:
            skipped_low_score += 1
            rejected_uids.append(item.uid)
            continue

        qualified.append(item)

    # Highest relevance first; break ties by newest timestamp.
    epoch_zero = datetime.fromtimestamp(0, tz=timezone.utc)
    qualified.sort(
        key=lambda it: (
            it.score,
            it.published if (it.published and it.published.tzinfo) else epoch_zero,
        ),
        reverse=True,
    )

    if cap > 0 and len(qualified) > cap:
        alerts = qualified[:cap]
        overflow_uids = [it.uid for it in qualified[cap:]]
    else:
        alerts = qualified
        overflow_uids = []

    return FilterResult(
        alerts=alerts,
        rejected_uids=rejected_uids,
        overflow_uids=overflow_uids,
        skipped_seen=skipped_seen,
        skipped_stale=skipped_stale,
        skipped_low_score=skipped_low_score,
    )

