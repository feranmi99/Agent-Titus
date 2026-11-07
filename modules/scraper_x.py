"""
X / Twitter monitoring module.

Supports three ingestion modes (in priority order):
1. Browser Session Cookies (`X_AUTH_TOKEN` + `X_CT0` in `.env`) — Free, hits X's
   internal Web GraphQL `SearchTimeline` (`Latest` tab) just like a logged-in browser.
2. Official X API v2 Recent Search (`X_BEARER_TOKEN` in `.env`) — Paid Basic+ tier.
3. Unauthenticated Nitter-compatible RSS search fallback (`X_NITTER_BASE_URL` in `.env`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse, urlunparse

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

X_SEARCH_ENDPOINT = "https://api.x.com/2/tweets/search/recent"

# Public web client bearer token embedded in x.com's web bundle for OAuth2Session requests.
_X_WEB_PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Fallback SearchTimeline GraphQL query IDs + runtime auto-discovery regexes.
_FALLBACK_SEARCH_QUERY_IDS = [
    "UN1i3zUiCWa-6r-Uaho4fw",
    "gkjsKepM6gl_HmFWoWKfgg",
    "flaR-PUMshxFWZWPNpq4zA",
    "bshMIjqDk8LTXTq4w91WKw",
]
_MAIN_JS_URL_RE = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web/main\.[a-zA-Z0-9]+\.js"
)
_SEARCH_TIMELINE_QID_RE = re.compile(
    r'queryId:"([a-zA-Z0-9_-]+)",operationName:"SearchTimeline"'
)

# Standard feature switches expected by X's SearchTimeline GraphQL operation.
_GRAPHQL_FEATURES: dict[str, bool] = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_API_ONLY_TOKENS_RE = re.compile(r"\b(?:-is:retweet|lang:\w+)\b", re.IGNORECASE)

# Per-query watermark to avoid re-fetching identical tweets within a daemon run.
_since_ids: dict[str, str] = {}
_cached_search_query_id: Optional[str] = None
_warned_unconfigured: bool = False


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_twitter_legacy_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse X legacy timestamp e.g. 'Thu Nov 06 23:15:00 +0000 2026'."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _parse_iso_dt(value)


def _headline_from_tweet(text: str, max_len: int = 110) -> str:
    first_line = text.splitlines()[0].strip() if text else ""
    cleaned = clean_text(first_line or text, max_len=max_len)
    return cleaned or "X Post"


def _to_web_search_query(api_query: str) -> str:
    """Convert X API v2 operator `-is:retweet` to Web search `-filter:retweets`."""
    return re.sub(r"-is:retweet\b", "-filter:retweets", api_query, flags=re.IGNORECASE)


def _discover_search_query_id(client: httpx.Client) -> Optional[str]:
    """Attempt to extract the current `SearchTimeline` queryId from x.com's JS bundle."""
    try:
        home = client.get(
            "https://x.com/explore",
            headers={"User-Agent": _BROWSER_UA, "Accept": "text/html"},
        )
        if not home.is_success:
            return None
        match_js = _MAIN_JS_URL_RE.search(home.text)
        if not match_js:
            return None
        js_resp = client.get(match_js.group(0), headers={"User-Agent": _BROWSER_UA})
        if not js_resp.is_success:
            return None
        match_qid = _SEARCH_TIMELINE_QID_RE.search(js_resp.text)
        if match_qid:
            qid = match_qid.group(1)
            log.debug("Discovered X SearchTimeline queryId: %s", qid)
            return qid
    except httpx.HTTPError as exc:
        log.debug("Could not auto-discover SearchTimeline queryId: %s", exc)
    return None


def _extract_graphql_tweets(node: Any, out: list[dict[str, str]]) -> None:
    """Recursively walk X GraphQL timeline JSON and extract tweet objects."""
    if isinstance(node, dict):
        typename = node.get("__typename")
        if typename == "TweetWithVisibilityResults" and isinstance(node.get("tweet"), dict):
            _extract_graphql_tweets(node["tweet"], out)
            return

        legacy = node.get("legacy")
        rest_id = node.get("rest_id")
        if (
            isinstance(legacy, dict)
            and rest_id
            and "full_text" in legacy
            and typename in {None, "Tweet"}
        ):
            # Ignore retweet wrappers so we only index original posts.
            if isinstance(legacy.get("retweeted_status_result"), dict):
                return

            user_res = (
                ((node.get("core") or {}).get("user_results") or {}).get("result")
            ) or {}
            screen_name = (
                (user_res.get("core") or {}).get("screen_name")
                or (user_res.get("legacy") or {}).get("screen_name")
                or ""
            )

            # Prefer NoteTweet (long-form tweet) text when available.
            note_text = (
                (
                    ((node.get("note_tweet") or {}).get("note_tweet_results") or {}).get(
                        "result"
                    )
                    or {}
                ).get("text")
            )
            full_text = str(note_text or legacy.get("full_text") or "").strip()
            created_at = str(legacy.get("created_at") or "").strip()

            if full_text:
                out.append(
                    {
                        "id": str(rest_id),
                        "text": full_text,
                        "created_at": created_at,
                        "username": str(screen_name).strip(),
                    }
                )
            return

        for val in node.values():
            _extract_graphql_tweets(val, out)
    elif isinstance(node, list):
        for elem in node:
            _extract_graphql_tweets(elem, out)


def _fetch_via_cookies(client: httpx.Client) -> list[Item]:
    """Search X's `Latest` feed using browser session cookies (`auth_token` & `ct0`)."""
    global _cached_search_query_id

    ct0 = config.X_CT0
    auth_token = config.X_AUTH_TOKEN
    headers = {
        "User-Agent": _BROWSER_UA,
        "Authorization": f"Bearer {_X_WEB_PUBLIC_BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "Cookie": f"auth_token={auth_token}; ct0={ct0}",
        "Referer": "https://x.com/explore",
        "Accept": "*/*",
    }

    candidate_qids: list[str] = []
    if _cached_search_query_id:
        candidate_qids.append(_cached_search_query_id)
    for qid in _FALLBACK_SEARCH_QUERY_IDS:
        if qid not in candidate_qids:
            candidate_qids.append(qid)

    items: list[Item] = []
    count = max(10, min(50, config.X_MAX_RESULTS_PER_QUERY))
    queries = list(config.X_QUERIES.items())

    for idx, (raw_query, category_hint) in enumerate(queries):
        web_query = _to_web_search_query(raw_query)
        variables = {
            "rawQuery": web_query,
            "count": count,
            "querySource": "typed_query",
            "product": "Latest",
        }
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(_GRAPHQL_FEATURES, separators=(",", ":")),
        }

        resp: Optional[httpx.Response] = None
        for qid in list(candidate_qids):
            url = f"https://x.com/i/api/graphql/{qid}/SearchTimeline"
            try:
                r = client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("Network error querying X GraphQL: %s", exc)
                break

            # Refresh CSRF token if X rotated `ct0` in Set-Cookie.
            new_ct0 = r.cookies.get("ct0")
            if new_ct0 and new_ct0 != ct0:
                ct0 = new_ct0
                headers["x-csrf-token"] = ct0
                headers["Cookie"] = f"auth_token={auth_token}; ct0={ct0}"

            if r.status_code in {400, 404}:
                continue
            if r.status_code in {401, 403}:
                log.error(
                    "X cookie session rejected (HTTP %d). Check X_AUTH_TOKEN and X_CT0 in .env.",
                    r.status_code,
                )
                return items
            if r.status_code == 429:
                log.warning("X GraphQL rate limit hit (HTTP 429); backing off until next cycle.")
                return items
            if r.is_success:
                _cached_search_query_id = qid
                resp = r
                break

        # If all static queryIds failed with 400/404, auto-discover from x.com main.js once.
        if resp is None:
            discovered = _discover_search_query_id(client)
            if discovered and discovered not in candidate_qids:
                candidate_qids.insert(0, discovered)
                url = f"https://x.com/i/api/graphql/{discovered}/SearchTimeline"
                r = get_with_retry(client, url, params=params, headers=headers, retries=1)
                if r is not None and r.is_success:
                    _cached_search_query_id = discovered
                    resp = r

        if resp is None:
            log.warning("Could not fetch X SearchTimeline for query: %s", web_query)
            continue

        try:
            payload = resp.json()
        except ValueError as exc:
            log.warning("Invalid JSON from X GraphQL: %s", exc)
            continue

        extracted: list[dict[str, str]] = []
        _extract_graphql_tweets(payload, extracted)

        for tw in extracted:
            tweet_id = tw["id"]
            text = tw["text"]
            username = tw["username"]
            if username:
                tweet_url = f"https://x.com/{username}/status/{tweet_id}"
                source = f"x:@{username}"
            else:
                tweet_url = f"https://x.com/i/web/status/{tweet_id}"
                source = "x"

            items.append(
                Item(
                    source=source,
                    title=_headline_from_tweet(text),
                    url=tweet_url,
                    snippet=clean_text(text, max_len=380),
                    published=_parse_twitter_legacy_dt(tw["created_at"]),
                    category_hint=category_hint,
                    uid=f"x:{tweet_id}",
                )
            )

        if idx < len(queries) - 1:
            time.sleep(1.2)

    log.info("X cookie scraper collected %d candidate post(s)", len(items))
    return items


def _fetch_via_api(client: httpx.Client) -> list[Item]:
    items: list[Item] = []
    headers = {"Authorization": f"Bearer {config.X_BEARER_TOKEN}"}
    max_results = max(10, min(100, config.X_MAX_RESULTS_PER_QUERY))

    for query, category_hint in config.X_QUERIES.items():
        params: dict[str, str | int] = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": "created_at,author_id,text",
            "expansions": "author_id",
            "user.fields": "username",
        }
        if query in _since_ids:
            params["since_id"] = _since_ids[query]

        resp = get_with_retry(client, X_SEARCH_ENDPOINT, params=params, headers=headers)
        if resp is None:
            continue

        try:
            payload = resp.json()
        except ValueError as exc:
            log.warning("Invalid JSON from X API for query %r: %s", query, exc)
            continue

        meta = payload.get("meta") or {}
        newest_id = meta.get("newest_id")
        if newest_id:
            _since_ids[query] = str(newest_id)

        users_by_id: dict[str, str] = {}
        for user in (payload.get("includes") or {}).get("users") or []:
            uid = user.get("id")
            uname = user.get("username")
            if uid and uname:
                users_by_id[str(uid)] = str(uname)

        for tweet in payload.get("data") or []:
            tweet_id = str(tweet.get("id", "")).strip()
            raw_text = str(tweet.get("text", "")).strip()
            if not tweet_id or not raw_text:
                continue

            author_id = str(tweet.get("author_id", ""))
            username = users_by_id.get(author_id)
            if username:
                url = f"https://x.com/{username}/status/{tweet_id}"
                source = f"x:@{username}"
            else:
                url = f"https://x.com/i/web/status/{tweet_id}"
                source = "x"

            items.append(
                Item(
                    source=source,
                    title=_headline_from_tweet(raw_text),
                    url=url,
                    snippet=clean_text(raw_text, max_len=380),
                    published=_parse_iso_dt(tweet.get("created_at")),
                    category_hint=category_hint,
                    uid=f"x:{tweet_id}",
                )
            )

    log.info("X API scraper collected %d candidate post(s)", len(items))
    return items


def _canonical_x_url(raw_link: str) -> str:
    """Rewrite a Nitter instance URL to canonical https://x.com/..."""
    if not raw_link:
        return ""
    parsed = urlparse(raw_link)
    if not parsed.path:
        return raw_link
    return urlunparse(("https", "x.com", parsed.path, "", "", ""))


def _fetch_via_nitter_rss(client: httpx.Client) -> list[Item]:
    items: list[Item] = []
    base = config.X_NITTER_BASE_URL

    for query, category_hint in config.X_QUERIES.items():
        cleaned_query = _API_ONLY_TOKENS_RE.sub("", query)
        cleaned_query = " ".join(cleaned_query.split())
        rss_url = f"{base}/search/rss?f=tweets&q={quote_plus(cleaned_query)}"

        resp = get_with_retry(
            client,
            rss_url,
            headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"},
        )
        if resp is None:
            continue

        feed = feedparser.parse(resp.text)
        for entry in feed.entries:
            raw_link = getattr(entry, "link", "") or ""
            url = _canonical_x_url(raw_link)
            title = clean_text(getattr(entry, "title", ""), max_len=140)
            summary = clean_text(getattr(entry, "summary", ""), max_len=380)
            if not url and not title:
                continue

            published = struct_time_to_dt(
                getattr(entry, "published_parsed", None)
                or getattr(entry, "updated_parsed", None)
            )
            author = clean_text(getattr(entry, "author", ""), max_len=40).lstrip("@")
            source = f"x:@{author}" if author else "x"

            items.append(
                Item(
                    source=source,
                    title=title or _headline_from_tweet(summary),
                    url=url or raw_link,
                    snippet=summary,
                    published=published,
                    category_hint=category_hint,
                )
            )

    log.info("X RSS fallback scraper collected %d candidate post(s)", len(items))
    return items


def fetch_items(client: Optional[httpx.Client] = None) -> list[Item]:
    """Fetch candidate X/Twitter items using session cookies, API v2, or RSS fallback."""
    global _warned_unconfigured

    if not config.ENABLE_X:
        return []

    has_cookies = bool(config.X_AUTH_TOKEN and config.X_CT0)
    if not has_cookies and not config.X_BEARER_TOKEN and not config.X_NITTER_BASE_URL:
        if not _warned_unconfigured:
            log.info(
                "X scraper inactive: set X_AUTH_TOKEN + X_CT0 (or X_BEARER_TOKEN) in .env to enable."
            )
            _warned_unconfigured = True
        return []

    ctx = nullcontext(client) if client is not None else build_client()
    with ctx as http:
        if has_cookies:
            return _fetch_via_cookies(http)
        if config.X_BEARER_TOKEN:
            return _fetch_via_api(http)
        return _fetch_via_nitter_rss(http)
