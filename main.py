#!/usr/bin/env python3
"""
Agent Titus (Swarm Node 01) — 24/7 Autonomous Intelligence Gatherer.

Polls X/Twitter, Reddit, RSS feeds, Google News, and Google Trends for:
  1. Early-stage Remote Software Engineering & AI Data Labeling roles
  2. High-volatility Macro & Forex / Gold (XAU) market catalysts

Usage examples:
  python main.py                     # Run 24/7 continuous polling daemon
  python main.py --once --dry-run    # Single test pass printed to console
  python main.py --once              # Single pass (ideal for cron / GitHub Actions)
  python main.py --test-telegram     # Send a test alert to verify Telegram setup
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from typing import Callable

import httpx

import config
from modules import scraper_news, scraper_reddit, scraper_x
from modules.common import Item, build_client
from modules.filter import SeenStore, filter_items
from notifier import telegram

log = logging.getLogger("agent_titus")

_stop_event = threading.Event()


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Keep third-party HTTP debug noise quiet unless explicitly debugging.
    if level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _install_signal_handlers() -> None:
    def _handle_signal(signum: int, _frame) -> None:
        sig_name = signal.Signals(signum).name
        log.info("Received %s — shutting down gracefully...", sig_name)
        _stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def collect_all(client: httpx.Client) -> list[Item]:
    """Run every enabled scraper inside isolated fault boundaries."""
    scrapers: list[tuple[str, Callable[[httpx.Client], list[Item]]]] = [
        ("X/Twitter", scraper_x.fetch_items),
        ("Reddit", scraper_reddit.fetch_items),
        ("News/RSS", scraper_news.fetch_items),
    ]

    collected: list[Item] = []
    for name, fn in scrapers:
        if _stop_event.is_set():
            break
        try:
            items = fn(client)
            collected.extend(items)
        except Exception as exc:
            log.exception("Unhandled error in %s module: %s", name, exc)
    return collected


def run_cycle(
    store: SeenStore,
    *,
    dry_run: bool = False,
    seed_first_run: bool = True,
    persist_state: bool = True,
) -> int:
    """
    Execute one end-to-end collection, filtering, and dispatch cycle.

    Returns the number of alerts dispatched.
    """
    cycle_start = time.monotonic()
    with build_client() as client:
        collected = collect_all(client)
        if not collected:
            log.info("Cycle complete: 0 items collected (check connectivity or source configs).")
            return 0

        # Seed baseline on first run (only when not dry-running and items were fetched).
        if not dry_run and seed_first_run and not store.seeded:
            store.mark_seen(it.uid for it in collected)
            store.seeded = True
            if persist_state:
                store.save()
            log.info(
                "Initial baseline seeded with %d item(s). Subsequent cycles will dispatch new alerts.",
                len(collected),
            )
            return 0

        result = filter_items(collected, store)
        log.info(
            "Filter summary: collected=%d | seen=%d | stale=%d | low_score=%d | qualified=%d",
            len(collected),
            result.skipped_seen,
            result.skipped_stale,
            result.skipped_low_score,
            len(result.alerts) + len(result.overflow_uids),
        )

        sent_uids = telegram.send_batch(result.alerts, client=client, dry_run=dry_run)

    if persist_state and not dry_run:
        store.mark_seen(result.rejected_uids)
        store.mark_seen(result.overflow_uids)
        store.mark_seen(sent_uids)
        store.seeded = True
        store.save()
    elif persist_state and dry_run:
        store.mark_seen(result.rejected_uids)
        store.mark_seen(result.overflow_uids)
        store.mark_seen(sent_uids)
        store.save()

    elapsed = time.monotonic() - cycle_start
    log.info(
        "Cycle finished in %.1fs — dispatched %d/%d alert(s).",
        elapsed,
        len(sent_uids),
        len(result.alerts),
    )
    return len(sent_uids)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent Titus (Swarm Node 01) — Autonomous Job & Forex Monitor"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling cycle and exit (useful for cron / GitHub Actions).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matching alerts to stdout instead of sending to Telegram.",
    )
    parser.add_argument(
        "--save-state",
        action="store_true",
        help="Persist seen_items.json even when --dry-run is active.",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Alert immediately on the very first run instead of seeding baseline state.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.POLL_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default: {config.POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a diagnostic test message to Telegram and exit.",
    )
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        help=f"Logging verbosity (default: {config.LOG_LEVEL}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.log_level)
    _install_signal_handlers()

    if args.test_telegram:
        log.info("Sending diagnostic message to Telegram...")
        ok = telegram.send_test_message()
        if ok:
            log.info("Telegram test message delivered successfully.")
            return 0
        log.error("Telegram test message failed. Verify TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return 1

    if not args.dry_run and (not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID):
        log.warning(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set in .env! "
            "Alerts cannot be delivered until configured (or pass --dry-run to print to console)."
        )

    store = SeenStore()
    seed_first_run = config.SEED_ON_FIRST_RUN and not args.no_seed
    persist_state = (not args.dry_run) or args.save_state

    log.info(
        "Agent Titus starting (mode=%s, dry_run=%s, interval=%ds, seen_cache=%d items)",
        "once" if args.once else "daemon",
        args.dry_run,
        args.interval,
        len(store),
    )

    while not _stop_event.is_set():
        cycle_started = time.monotonic()
        try:
            run_cycle(
                store,
                dry_run=args.dry_run,
                seed_first_run=seed_first_run,
                persist_state=persist_state,
            )
        except Exception as exc:
            log.exception("Top-level cycle failure (daemon will continue): %s", exc)

        if args.once or _stop_event.is_set():
            break

        elapsed = time.monotonic() - cycle_started
        wait_seconds = max(5.0, float(args.interval) - elapsed)
        log.info("Sleeping %.0fs until next polling cycle...", wait_seconds)
        _stop_event.wait(timeout=wait_seconds)

    log.info("Agent Titus stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

