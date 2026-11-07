"""Unit test suite for Agent Titus filtering, deduplication, and formatting."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

import config
import main as titus_main
from modules import scraper_news, scraper_reddit, scraper_x
from modules.common import Item, clean_text
from modules.filter import SeenStore, filter_items, is_stale, score_item
from notifier import telegram
from notifier.telegram import escape_mdv2, format_markdown_v2, format_plain


class KeywordScoringTests(unittest.TestCase):
    def test_job_opportunity_scores_high(self) -> None:
        item = Item(
            source="reddit:r/forhire",
            title="[Hiring] Remote AI Trainer & Data Annotation Specialist",
            url="https://www.reddit.com/r/forhire/comments/abc123/hiring_ai_trainer/",
            snippet="We're hiring remote RLHF contributors worldwide. Apply now.",
            category_hint="jobs",
        )
        score_item(item)
        self.assertEqual(item.category, "jobs")
        self.assertGreaterEqual(item.score, 10)
        self.assertIn("[hiring]", item.matched)
        self.assertIn("data annotation", item.matched)

    def test_for_hire_noise_is_penalised(self) -> None:
        item = Item(
            source="reddit:r/forhire",
            title="[For Hire] Full stack developer looking for work",
            url="https://www.reddit.com/r/forhire/comments/xyz999/for_hire_dev/",
            snippet="Available for freelance projects.",
            category_hint="jobs",
        )
        score_item(item)
        self.assertLess(item.score, 3)

    def test_word_boundaries_prevent_substring_false_positives(self) -> None:
        # "software" contains "war", "Boeing" contains "boe", "manufacturing" contains "turing"
        item = Item(
            source="rss:Generic",
            title="Boeing manufacturing award update for Nguyen",
            url="https://example.com/news/1",
            snippet="General corporate update on desktop software releases.",
        )
        score_item(item)
        self.assertEqual(item.score, 0)
        self.assertEqual(item.matched, [])

    def test_forex_gold_catalyst_scores_high(self) -> None:
        item = Item(
            source="rss:ForexLive",
            title="BREAKING: FOMC signals surprise rate cut as XAUUSD surges",
            url="https://www.forexlive.com/news/fomc-xauusd-1",
            snippet="Powell press conference triggers sharp move in gold and DXY.",
            category_hint="forex",
        )
        score_item(item)
        self.assertEqual(item.category, "forex")
        self.assertGreaterEqual(item.score, 12)
        self.assertIn("fomc", item.matched)
        self.assertIn("xauusd", item.matched)


class SeenStoreAndFilterTests(unittest.TestCase):
    def test_persistence_pruning_and_corruption_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen_items.json"
            store = SeenStore(path=path, max_items=3)
            self.assertFalse(store.seeded)

            store.mark_seen(["id1", "id2", "id3", "id4"])
            store.seeded = True
            store.save()

            reloaded = SeenStore(path=path, max_items=3)
            self.assertTrue(reloaded.seeded)
            self.assertEqual(len(reloaded), 3)

            # Corrupt file recovery
            path.write_text("{broken json", encoding="utf-8")
            recovered = SeenStore(path=path, max_items=3)
            self.assertFalse(recovered.seeded)
            self.assertEqual(len(recovered), 0)
            self.assertTrue(path.with_suffix(".corrupt.json").exists())

    def test_filter_pipeline_dedupes_and_filters_stale(self) -> None:
        now = datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc)
        fresh_job = Item(
            source="reddit:r/forhire",
            title="[Hiring] Remote Python Developer",
            url="https://example.com/job/1",
            published=now - timedelta(hours=1),
            category_hint="jobs",
        )
        duplicate_url = Item(
            source="gnews",
            title="[Hiring] Remote Python Developer (Mirror)",
            url="https://example.com/job/1",
            published=now - timedelta(minutes=30),
            category_hint="jobs",
        )
        stale_forex = Item(
            source="rss:ForexLive",
            title="FOMC rate decision shakes XAUUSD",
            url="https://example.com/fx/old",
            published=now - timedelta(hours=48),
            category_hint="forex",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(path=Path(tmp) / "seen.json")
            res = filter_items(
                [fresh_job, duplicate_url, stale_forex],
                store,
                min_score=3,
                now=now,
            )
            self.assertEqual(len(res.alerts), 1)
            self.assertEqual(res.alerts[0].url, "https://example.com/job/1")
            self.assertEqual(res.skipped_seen, 1)
            self.assertEqual(res.skipped_stale, 1)

    def test_is_stale(self) -> None:
        now = datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc)
        old_item = Item(
            source="x",
            title="Old",
            url="https://x.com/1",
            published=now - timedelta(hours=30),
        )
        self.assertTrue(is_stale(old_item, now=now, max_age_hours=24))


class FormattingTests(unittest.TestCase):
    def test_clean_text_strips_html(self) -> None:
        raw = "<p>Hello &amp; <b>world</b>!</p>   <span>Line 2</span>"
        self.assertEqual(clean_text(raw), "Hello & world! Line 2")

    def test_markdown_v2_escapes_special_characters(self) -> None:
        item = Item(
            source="reddit:r/forhire",
            title="[Hiring] C++ / Python_Dev ($80-$120/hr)!",
            url="https://example.com/jobs?id=1&role=(senior)",
            snippet="Apply at jobs_team@example.com - urgent!",
            published=datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc),
            category="jobs",
            score=9,
            matched=["[hiring]", "python developer"],
        )
        md = format_markdown_v2(item)
        self.assertIn(r"\[Hiring\]", md)
        self.assertIn(r"Python\_Dev", md)
        self.assertIn(r"(senior\)", md)

        plain = format_plain(item)
        self.assertIn("Link: https://example.com/jobs?id=1&role=(senior)", plain)
        self.assertEqual(escape_mdv2("a_b*c"), r"a\_b\*c")


class ScraperAndNotifierIntegrationTests(unittest.TestCase):
    def test_reddit_rss_parses_and_strips_footer(self) -> None:
        sample_atom = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>t3_abc123</id>
            <title>[Hiring] Remote Backend Python Engineer ($70/hr)</title>
            <link href="https://www.reddit.com/r/forhire/comments/abc123/hiring/" />
            <updated>2026-11-05T22:00:00+00:00</updated>
            <content type="html">&lt;div&gt;Looking for async Python developer. &#32; submitted by &#32; &lt;a href="https://www.reddit.com/user/outlier_fan"&gt; /u/outlier_fan &lt;/a&gt; &lt;span&gt;[link]&lt;/span&gt; &lt;span&gt;[comments]&lt;/span&gt;&lt;/div&gt;</content>
          </entry>
        </feed>"""
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=sample_atom))
        with httpx.Client(transport=transport) as client:
            items = scraper_reddit.fetch_subreddit(client, "forhire", "jobs")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "reddit:r/forhire")
        self.assertNotIn("submitted by", items[0].snippet)
        self.assertNotIn("outlier_fan", items[0].snippet)

    def test_x_api_v2_parses_tweets_and_tracks_since_id(self) -> None:
        payload = {
            "data": [
                {
                    "id": "199001",
                    "author_id": "42",
                    "created_at": "2026-11-05T22:10:00.000Z",
                    "text": "We're hiring remote Data Annotation trainers! Apply now.",
                }
            ],
            "includes": {"users": [{"id": "42", "username": "aitalent"}]},
            "meta": {"newest_id": "199001", "result_count": 1},
        }
        seen_params: list[dict] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen_params.append(dict(req.url.params))
            return httpx.Response(200, json=payload)

        scraper_x._since_ids.clear()
        with (
            mock.patch.object(config, "X_AUTH_TOKEN", ""),
            mock.patch.object(config, "X_CT0", ""),
            mock.patch.object(config, "X_BEARER_TOKEN", "test-bearer"),
            mock.patch.object(config, "X_QUERIES", {"hiring lang:en": "jobs"}),
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
        ):
            first = scraper_x.fetch_items(client)
            second = scraper_x.fetch_items(client)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].url, "https://x.com/aitalent/status/199001")
        self.assertNotIn("since_id", seen_params[0])
        self.assertEqual(seen_params[1].get("since_id"), "199001")
        self.assertEqual(len(second), 1)

    def test_x_cookie_graphql_parses_tweets(self) -> None:
        gql_payload = {
            "data": {
                "search_by_raw_query": {
                    "search_timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": "2026999",
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "core": {
                                                                            "screen_name": "fxdesk"
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            "legacy": {
                                                                "created_at": "Thu Nov 06 23:15:00 +0000 2026",
                                                                "full_text": "BREAKING: FOMC emergency rate decision sends XAUUSD surging",
                                                            },
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
        with (
            mock.patch.object(config, "X_AUTH_TOKEN", "tok123"),
            mock.patch.object(config, "X_CT0", "ct0123"),
            mock.patch.object(config, "X_QUERIES", {"FOMC -is:retweet": "forex"}),
            httpx.Client(
                transport=httpx.MockTransport(lambda req: httpx.Response(200, json=gql_payload))
            ) as client,
        ):
            items = scraper_x.fetch_items(client)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://x.com/fxdesk/status/2026999")
        self.assertEqual(items[0].source, "x:@fxdesk")
        self.assertEqual(items[0].uid, "x:2026999")

    def test_news_and_google_trends_rss_parsing(self) -> None:
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss">
          <channel>
            <item>
              <title>FOMC</title>
              <link>https://trends.google.com/trending/rss?geo=US</link>
              <pubDate>Thu, 05 Nov 2026 22:00:00 +0000</pubDate>
              <ht:approx_traffic>200K+</ht:approx_traffic>
              <ht:news_item_title>Federal Reserve holds emergency rate decision as gold surges</ht:news_item_title>
              <ht:news_item_url>https://example.com/fed-gold</ht:news_item_url>
            </item>
          </channel>
        </rss>"""
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=rss_xml))
        with (
            mock.patch.object(config, "RSS_FEEDS", {}),
            mock.patch.object(config, "GOOGLE_NEWS_QUERIES", {}),
            mock.patch.object(config, "GOOGLE_TRENDS_GEOS", ["US"]),
            httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=rss_xml))) as client,
        ):
            items = scraper_news.fetch_items(client)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://example.com/fed-gold")
        score_item(items[0])
        self.assertEqual(items[0].category, "forex")
        self.assertGreaterEqual(items[0].score, 8)

    def test_telegram_fallback_on_400_bad_request(self) -> None:
        calls: list[dict] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
            return httpx.Response(200, json={"ok": True})

        item = Item(
            source="rss:ForexLive",
            title="XAUUSD breaks record high after Powell remarks",
            url="https://example.com/xau",
            category="forex",
            score=9,
        )
        with (
            mock.patch.object(config, "TELEGRAM_BOT_TOKEN", "123:ABC"),
            mock.patch.object(config, "TELEGRAM_CHAT_ID", "999"),
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
        ):
            ok = telegram.send_alert(item, client=client)

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get("parse_mode"), "MarkdownV2")
        self.assertNotIn("parse_mode", calls[1])

    def test_run_cycle_first_run_seeding_then_alerting(self) -> None:
        item1 = Item(
            source="reddit:r/forhire",
            title="[Hiring] Remote AI Trainer at Outlier",
            url="https://example.com/job/seed1",
            category_hint="jobs",
        )
        item2 = Item(
            source="rss:ForexLive",
            title="FOMC rate cut sends XAUUSD surging",
            url="https://example.com/fx/new2",
            category_hint="forex",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(path=Path(tmp) / "seen_items.json")
            with mock.patch.object(titus_main, "collect_all", return_value=[item1]):
                sent_seed = titus_main.run_cycle(store, dry_run=False, seed_first_run=True)
            self.assertEqual(sent_seed, 0)
            self.assertTrue(store.seeded)
            self.assertIn(item1.uid, store)

            with (
                mock.patch.object(titus_main, "collect_all", return_value=[item1, item2]),
                mock.patch.object(telegram, "send_batch", return_value=[item2.uid]) as send_mock,
            ):
                sent_second = titus_main.run_cycle(store, dry_run=False, seed_first_run=True)
            self.assertEqual(sent_second, 1)
            send_mock.assert_called_once()
            self.assertIn(item2.uid, store)


if __name__ == "__main__":
    unittest.main()
