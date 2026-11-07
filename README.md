# Agent Titus (Swarm Node 01)

Autonomous 24/7 intelligence-gathering agent that monitors **Reddit**, **RSS / Forex & Job feeds**, **Google News**, **Google Trends**, and **X/Twitter**, filters for high-signal opportunities, and dispatches instant alerts to **Telegram**.

### Phase 1 Target Domains
1. **Remote Job Hunting (AI Data Labeling & Software Engineering):** Early-stage postings across `r/forhire`, `r/remotework`, `r/DataAnnotationTech`, WeWorkRemotely, RemoteOK, Google News, and X (Outlier, Scale AI, Turing, Mercor, RLHF, Python/Backend roles) while filtering out `[For Hire]` noise.
2. **Macro & Forex / Gold (`XAUUSD`) Intelligence:** Central bank policy shifts (Fed/FOMC, ECB, BoJ, BoE), tier-1 economic releases (NFP, CPI, PMI), and geopolitical catalysts driving Gold and major currency pairs.

---

## Project Structure

```text
Agent-Titus/
├── config.py                     # Env config, weighted keywords, feeds & subreddits
├── main.py                       # CLI & 24/7 fault-tolerant polling orchestrator
├── modules/
│   ├── common.py                 # Item dataclass, HTML cleaner, retry/rate-limit HTTP client
│   ├── scraper_x.py              # X API v2 recent search + Nitter RSS fallback
│   ├── scraper_reddit.py         # Subreddit /new Atom scanner with footer stripping
│   ├── scraper_news.py           # Direct RSS, Google News RSS & Google Trends RSS
│   └── filter.py                 # Word-boundary scoring, age check & atomic SeenStore
├── notifier/
│   └── telegram.py               # Telegram Bot API MarkdownV2 dispatcher + fallback
├── data/
│   └── seen_items.json           # Persistent deduplication cache (created at runtime)
├── deploy/
│   └── agent-titus.service       # systemd unit for headless Linux VM (UTM / VPS)
├── .github/workflows/
│   └── agent-titus.yml           # Serverless GitHub Actions cron alternative
└── tests/
    └── test_agent_titus.py       # Unit tests for scoring, dedup, and formatting
```

---

## Quick Start

### 1. Create Virtual Environment & Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Credentials

```bash
cp .env.example .env
```

Edit `.env` and set:
- `TELEGRAM_BOT_TOKEN`: Create a bot via `@BotFather` on Telegram.
- `TELEGRAM_CHAT_ID`: Your personal chat ID or target channel ID.
- *(Optional)* `X_BEARER_TOKEN` (X API v2 Basic+) or `X_NITTER_BASE_URL` (RSS instance).

### 3. Verify & Run

```bash
# Run unit tests
python -m unittest discover -s tests -v

# Test live scraping + filtering in console without sending Telegram messages
python main.py --once --dry-run

# Verify your Telegram Bot Token & Chat ID
python main.py --test-telegram

# Start the 24/7 continuous daemon
python main.py
```

> **Note on First Run:** By default (`SEED_ON_FIRST_RUN=true`), the first live run seeds `data/seen_items.json` with currently visible posts so your Telegram isn't flooded with historical items. Subsequent cycles (every 5 minutes) alert only on brand-new matches. Pass `--no-seed` if you want immediate alerts on the first cycle.

---

## 24/7 Deployment on UTM Linux VM (`systemd`)

1. Copy the project to `/opt/agent-titus` inside your Debian/Ubuntu UTM VM, create `.venv`, install `requirements.txt`, and configure `/opt/agent-titus/.env`.
2. Install and enable the `systemd` service:

```bash
sudo cp deploy/agent-titus.service /etc/systemd/system/agent-titus.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-titus
sudo journalctl -u agent-titus -f
```

# Agent-Titus
