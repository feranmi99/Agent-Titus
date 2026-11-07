"""
Agent Titus - central configuration.

Secrets come from environment variables (loaded from a local `.env` file via
python-dotenv). Non-secret tuning knobs (keywords, subreddits, feeds) live here
as plain Python so they are easy to read and version-control.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Secrets / credentials
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# X/Twitter configuration:
# 1. Cookie auth (Free — recommended): set X_AUTH_TOKEN and X_CT0 from a browser session.
# 2. Official X API v2 (Paid Basic+ tier): set X_BEARER_TOKEN.
# 3. Nitter-compatible RSS fallback: set X_NITTER_BASE_URL.
X_AUTH_TOKEN: str = os.getenv("X_AUTH_TOKEN", "").strip()
X_CT0: str = os.getenv("X_CT0", "").strip()
X_BEARER_TOKEN: str = os.getenv("X_BEARER_TOKEN", "").strip()
X_NITTER_BASE_URL: str = os.getenv("X_NITTER_BASE_URL", "").strip().rstrip("/")

# --------------------------------------------------------------------------- #
# Runtime behaviour
# --------------------------------------------------------------------------- #
POLL_INTERVAL_SECONDS: int = _env_int("POLL_INTERVAL_SECONDS", 300)  # 5 min
HTTP_TIMEOUT_SECONDS: int = _env_int("HTTP_TIMEOUT_SECONDS", 20)
USER_AGENT: str = os.getenv(
    "USER_AGENT", "python:agent-titus:v0.1 (personal monitoring bot)"
)

# Ignore items older than this (prevents alerting on stale content).
MAX_ITEM_AGE_HOURS: int = _env_int("MAX_ITEM_AGE_HOURS", 24)

# Hard cap of alerts per polling cycle to avoid flooding Telegram.
MAX_ALERTS_PER_CYCLE: int = _env_int("MAX_ALERTS_PER_CYCLE", 15)

# On the very first run (no state file yet) mark everything as seen without
# alerting, so you don't get a burst of 100+ old posts.
SEED_ON_FIRST_RUN: bool = _env_bool("SEED_ON_FIRST_RUN", True)

# Minimum relevance score an item needs to trigger an alert.
MIN_RELEVANCE_SCORE: int = _env_int("MIN_RELEVANCE_SCORE", 3)

# How many seen IDs to retain in the dedup store (oldest are pruned).
SEEN_STORE_MAX_ITEMS: int = _env_int("SEEN_STORE_MAX_ITEMS", 20000)

ENABLE_X: bool = _env_bool("ENABLE_X", True)
ENABLE_REDDIT: bool = _env_bool("ENABLE_REDDIT", True)
ENABLE_NEWS: bool = _env_bool("ENABLE_NEWS", True)

DATA_DIR: Path = BASE_DIR / "data"
SEEN_ITEMS_PATH: Path = DATA_DIR / "seen_items.json"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# --------------------------------------------------------------------------- #
# Categories & relevance scoring
#
# Each category defines weighted keywords. An item's score for a category is the
# sum of weights of matched keywords (case-insensitive, whole-phrase match).
# Negative weights penalise noise. The best-scoring category wins.
# --------------------------------------------------------------------------- #
CATEGORIES: dict[str, dict] = {
    "jobs": {
        "label": "💼 Job Opportunity",
        "keywords": {
            # strong intent signals
            "hiring": 3,
            "[hiring]": 4,
            "we're hiring": 4,
            "we are hiring": 4,
            "now hiring": 4,
            "job opening": 3,
            "open role": 3,
            "apply now": 2,
            # role types
            "data annotation": 4,
            "data annotator": 4,
            "data labeling": 4,
            "data labelling": 4,
            "ai trainer": 4,
            "ai training": 3,
            "rlhf": 3,
            "prompt engineer": 2,
            "software engineer": 2,
            "backend engineer": 2,
            "python developer": 2,
            "full stack": 1,
            "contract": 1,
            "freelance": 1,
            # companies known for AI training work
            "outlier": 3,
            "scale ai": 3,
            "turing": 2,
            "remotasks": 3,
            "dataannotation": 3,
            "mercor": 3,
            "alignerr": 3,
            "invisible technologies": 3,
            # location
            "remote": 2,
            "worldwide": 1,
            "work from home": 2,
            # noise
            "[for hire]": -6,
            "for hire": -2,
            "looking for work": -3,
            "scam": -3,
        },
    },
    "forex": {
        "label": "📈 Macro / Forex Alert",
        "keywords": {
            # central banks & policy
            "federal reserve": 3,
            "fomc": 4,
            "powell": 3,
            "rate decision": 4,
            "rate hike": 4,
            "rate cut": 4,
            "interest rate": 2,
            "ecb": 3,
            "lagarde": 2,
            "bank of england": 3,
            "boe": 2,
            "bank of japan": 3,
            "boj": 3,
            "snb": 2,
            "rba": 2,
            "pboc": 2,
            # data releases
            "nonfarm": 4,
            "non-farm": 4,
            "nfp": 4,
            "cpi": 3,
            "inflation": 2,
            "gdp": 2,
            "pmi": 2,
            "unemployment": 2,
            "jobs report": 3,
            # instruments
            "gold": 2,
            "xau": 4,
            "xauusd": 4,
            "eurusd": 3,
            "gbpusd": 3,
            "usdjpy": 3,
            "dollar index": 3,
            "dxy": 3,
            "forex": 2,
            "yen": 1,
            "treasury yields": 2,
            # geopolitics / volatility
            "breaking": 2,
            "sanctions": 2,
            "tariff": 2,
            "tariffs": 2,
            "intervention": 3,
            "war": 1,
            "ceasefire": 2,
            "emergency": 2,
            "surge": 1,
            "plunge": 1,
            "crash": 1,
        },
    },
}

# --------------------------------------------------------------------------- #
# Source definitions
# --------------------------------------------------------------------------- #

# Reddit: public RSS feed of /new for each subreddit. category_hint biases scoring.
REDDIT_SUBREDDITS: dict[str, str] = {
    "forhire": "jobs",
    "remotework": "jobs",
    "remotejs": "jobs",
    "WorkOnline": "jobs",
    "DataAnnotationTech": "jobs",
    "slavelabour": "jobs",
    "forex": "forex",
    "Forexstrategy": "forex",
    "algotrading": "forex",
    "Gold": "forex",
}

# Google News RSS search queries.
GOOGLE_NEWS_QUERIES: dict[str, str] = {
    '"data annotation" OR "AI trainer" hiring remote': "jobs",
    '"remote software engineer" hiring': "jobs",
    "Federal Reserve OR FOMC OR Powell": "forex",
    "gold price XAU": "forex",
    "forex breaking news": "forex",
    "ECB OR \"Bank of Japan\" OR \"Bank of England\" rate decision": "forex",
}
GOOGLE_NEWS_LOCALE: str = os.getenv("GOOGLE_NEWS_LOCALE", "hl=en-US&gl=US&ceid=US:en")

# Direct RSS feeds (name -> (url, category)).
RSS_FEEDS: dict[str, tuple[str, str]] = {
    "ForexLive": ("https://www.forexlive.com/feed/news", "forex"),
    "FXStreet": ("https://www.fxstreet.com/rss/news", "forex"),
    "Investing.com Forex": ("https://www.investing.com/rss/news_1.rss", "forex"),
    "WeWorkRemotely Programming": (
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "jobs",
    ),
    "RemoteOK": ("https://remoteok.com/remote-jobs.rss", "jobs"),
}

# Google Trends daily trending searches RSS (geo code).
GOOGLE_TRENDS_GEOS: list[str] = ["US"]

# X/Twitter recent-search queries (X API v2 syntax).
X_QUERIES: dict[str, str] = {
    '("data annotation" OR "AI trainer" OR "data labeling") (hiring OR "apply") -is:retweet lang:en': "jobs",
    '("software engineer" OR "backend engineer") remote hiring -is:retweet lang:en': "jobs",
    '(FOMC OR Powell OR "rate decision" OR NFP OR XAUUSD OR "gold price") -is:retweet lang:en': "forex",
}
X_MAX_RESULTS_PER_QUERY: int = _env_int("X_MAX_RESULTS_PER_QUERY", 10)

