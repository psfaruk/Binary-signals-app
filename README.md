# Binary Signals App

Real-time binary options signal generator for Quotex broker pairs. Uses two
separate prediction engines (one for OTC markets, one for Real markets)
with a shared 6-module analysis pipeline.

> **Python:** 3.10–3.12 (3.13 not yet tested — `distutils` removal + asyncio
> changes may break dependencies). See `requirements.txt`.

## Architecture

```
                     ┌──────────────────────────────────────┐
                     │            server.py                 │
                     │  FastAPI + WebSocket (uvicorn)       │
                     └──┬───────────────────────────────┬───┘
                        │                               │
            (QX_TOKEN)  │                               │ (QX_EMAIL+QX_PASSWORD,
                        │                               │  local dev only)
                        ▼                               ▼
              ┌──────────────────┐            ┌──────────────────┐
              │  pyquotex (WS)   │            │     feed.py      │
              │  (vendored)      │            │  (real Quotex)   │
              └────────┬─────────┘            └────────┬─────────┘
                       │                               │
                       └───────────┬───────────────────┘
                                   │
                                   ▼
                       ┌───────────────────────────┐
                       │     engines/ (router)     │
                       │  auto-detects category    │
                       │  from asset name suffix   │
                       └─────┬─────────────────┬───┘
                             │                 │
                  _otc suffix│                 │ no _otc suffix
                             ▼                 ▼
                  ┌──────────────────┐  ┌──────────────────┐
                  │  engines/otc/    │  │  engines/real/   │
                  │  config.py       │  │  config.py       │
                  │  (mean-reversion)│  │  (trend-follow)  │
                  └────────┬─────────┘  └────────┬─────────┘
                           │                     │
                           └──────────┬──────────┘
                                      │
                                      ▼
                          ┌───────────────────────────┐
                          │    engines/base/          │
                          │  (shared blender,         │
                          │   context, types,         │
                          │   5 shared modules)       │
                          └──────────┬────────────────┘
                                     │
                                     ▼
                          ┌───────────────────────────┐
                          │      core/                │
                          │  analysis.py (regime,     │
                          │   patterns, ATR, EMA,     │
                          │   key levels, stats)      │
                          │  microstructure.py        │
                          │  constants.py (MODULE_NAMES)│
                          │  stats.py (per-module     │
                          │   win-rate report)        │
                          └───────────────────────────┘
```

> **NOTE (FIX DEEP-AUDIT-2026-07-26 / F-19-28):** `sim_feed.py` was
> permanently disabled on 2026-07-25 — renamed to `sim_feed.py.DISABLED`.
> There is NO simulation mode. `QX_TOKEN` is REQUIRED. The previous
> architecture diagram mentioned `USE_SIM=1`; that path no longer exists.

### OTC vs Real Engine Separation

The app uses **two completely separate prediction engines**:

- **OTC engine** (`engines/otc/`): for broker-generated OTC pairs (asset
  names ending in `_otc`, e.g. `EURUSD_otc`). Tuned for mean-reversion
  behavior — the 6th module is `otc_pattern` (detects streak reversals,
  z-score extremes, alternation patterns). Payout floor: 85%.

- **Real engine** (`engines/real/`): for live exchange pairs (no `_otc`
  suffix, e.g. `EURUSD`). Tuned for trend-following — the 6th module is
  `trend_follow` (detects momentum continuation, EMA alignment, HH/HL
  structure, ATR expansion). Payout floor: 70%.

Both engines share:
- The blender algorithm (`engines/base/blender.py`)
- The market context computer (`engines/base/context.py`)
- 5 of 6 modules: `candle_reaction`, `running_tick`, `pattern`,
  `indicator`, `key_level`

Engine selection is automatic based on asset name suffix, and enforced
server-side: a subscribe request with `category="real"` but
`asset="EURUSD_otc"` is rejected with an error.

## File Structure

```
Binary-signals-app/
├── server.py                    # FastAPI + WebSocket entry
├── db.py                        # SQLite persistence (signal_log, candle_micro)
├── feed.py                      # Real Quotex feed (multi-asset)
├── quotex_ws.py                 # Raw WebSocket Quotex client (alt backend)
├── module_performance_report.py # CLI per-module win-rate report
├── requirements.txt
├── railway.json                 # Railway deployment config
├── run.sh                       # Linux/Mac launcher (chmod +x first)
├── start.bat                    # Windows launcher
├── install.bat                  # Windows installer
├── .env.example                 # Environment variable template
│
├── core/                        # Shared analysis library
│   ├── constants.py             # MODULE_NAMES (single source of truth)
│   ├── analysis.py              # Regime, patterns, ATR, EMA, key levels
│   ├── microstructure.py        # Tick-level microstructure builder
│   └── stats.py                 # Per-module win-rate computer
│
├── engines/                     # Prediction engines
│   ├── __init__.py              # Category router
│   ├── base/                    # Shared engine code
│   │   ├── types.py             # ModuleResult, MarketContext dataclasses
│   │   ├── context.py           # compute_context()
│   │   ├── blender.py           # Smart blender + BlenderConfig
│   │   ├── per_pair.py          # PairWeightAdapter (generic)
│   │   └── modules/             # All 7 modules (5 shared + otc_pattern + trend_follow)
│   ├── otc/                     # OTC engine (thin wrapper)
│   │   ├── __init__.py          # predict() routes to base blender with OTC config
│   │   └── config.py            # PAIR_CONFIGS_OTC, RELIABILITY_OTC, module_6=otc_pattern
│   └── real/                    # Real engine (thin wrapper)
│       ├── __init__.py          # predict() routes to base blender with Real config
│       └── config.py            # PAIR_CONFIGS_REAL, RELIABILITY_REAL, module_6=trend_follow
│
├── scripts/                    # Maintenance & verification scripts
│   ├── __init__.py              # Package marker
│   ├── _helpers.py              # Shared test/candle helpers (NEW)
│   ├── deep_analysis.py         # Static analysis scanner
│   ├── backtest_fixes.py        # Verify the 13 prediction-bug fixes
│   ├── backtest_remaining.py    # Verify the A1-A10 LOW/MEDIUM fixes
│   ├── backtest_weak_neutral.py  # Verify WEAK→NEUTRAL Options A/B
│   ├── setup_railway_token.py   # Push QX_TOKEN to Railway via GraphQL API
│   ├── verify_no_sim_mode.py    # Verify sim mode is permanently disabled
│   └── verify_live_audit_fixes.py # Verify the 12 live-audit fixes
│
├── static/                      # Frontend
│   ├── index.html               # Router (redirects to real.html or otc.html)
│   ├── real.html                # Real Market page (green accent)
│   ├── otc.html                 # OTC Market page (yellow accent)
│   ├── css/
│   │   ├── common.css           # Shared dark theme, responsive
│   │   ├── real.css             # Real-specific overrides
│   │   └── otc.css              # OTC-specific overrides
│   ├── js/
│   │   ├── common.js            # Shared WS + chart + signal logic
│   │   ├── real.js              # initApp('real')
│   │   └── otc.js               # initApp('otc')
│   └── lightweight-charts.js    # TradingView library (vendored)
│
└── pyquotex/                    # Vendored Quotex broker API (with TLS fixes)
```

> **REMOVED (FIX DEEP-AUDIT-2026-07-26 / F-19-29):** `sim_feed.py` was
> listed here previously as "Simulated feed (for dev / no creds)". That
> file is now `sim_feed.py.DISABLED` and is no longer importable. There
> is no simulation mode (A-10 PROBLEM 4).

## Setup (Local Dev)

### Prerequisites
- Python 3.10–3.12 (3.13 not yet tested)
- pip
- Git

### Install
```bash
git clone https://github.com/psfaruk/Binary-signals-app.git
cd Binary-signals-app
python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env and fill in your Quotex credentials:
#   - QX_TOKEN (REQUIRED — copy from browser DevTools, see RAILWAY_TOKEN_SETUP.md)
#   - OR QX_EMAIL + QX_PASSWORD (LOCAL DEV ONLY — Cloudflare blocks this on Railway)
#
# FIX (DEEP-AUDIT-2026-07-26 / F-19-30): removed USE_SIM=1 mention — sim
# mode is permanently disabled (A-10 PROBLEM 5). There is NO simulation
# fallback; QX_TOKEN is required for live data.
```

### Run
```bash
chmod +x run.sh  # Linux/Mac — one time only
./run.sh         # Linux/Mac
# OR
start.bat        # Windows
# OR
python3 server.py
```

Open http://localhost:8000 in your browser.

## Railway Deployment

1. Push this repo to GitHub.
2. Go to [railway.app](https://railway.app) → New Project → Deploy from
   GitHub repo.
3. Set these variables in Railway's Variables tab:
   - `QX_TOKEN` (REQUIRED — Quotex ssid token from your browser's DevTools)
   - `QX_USE_RAW_WS=0` (default — uses vendored pyquotex with Firefox TLS)
   - `QX_PAYOUT_FLOOR_REAL=70`
   - `QX_PAYOUT_FLOOR_OTC=85`
   - `AUTO_OPEN_BROWSER=0`, `HEADLESS=1` (Railway has no GUI)
   - `USE_SIM=0` (optional — sim mode is permanently disabled anyway)
4. Railway auto-detects `railway.json` and deploys.
5. Healthcheck at `/healthz` returns `{"ok": true}` when the server is up.

**Note:** Cloudflare blocks Quotex login from datacenter IPs (Railway
included). For production use, set `QX_TOKEN` directly (copied from your
browser's devtools after logging into Quotex manually). There is NO
simulation mode — the app requires live Quotex credentials.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serves `static/index.html` (router) |
| `/static/real.html` | GET | Real Market page |
| `/static/otc.html` | GET | OTC Market page |
| `/healthz` | GET | Railway healthcheck |
| `/api/pairs` | GET | Returns `{real_pairs, otc_pairs, payout_floor_real, payout_floor_otc, ...}` |
| `/api/pairs/{real\|otc}` | GET | Returns pairs for one category |
| `/api/status` | GET | Connection + stream status |
| `/api/history/{asset}/{period}` | GET | Candle history + last prediction |
| `/api/debug` | GET | Diagnostic info (env vars, streams) |
| `/api/stats` | GET | Per-module win-rate report |
| `/api/signals/{asset}/{period}` | GET | Recent signal history |
| `/api/signals/{asset}/{period}/{ctime}` | GET | Single signal detail |
| `/api/token-status` | GET | QX_TOKEN presence + Quotex connection state |
| `/api/set-token` | GET | Runtime token refresh (query: `?token=NEW`) |
| `/api/set-token` | POST | Runtime token refresh (JSON body `{"token":"..."}`) |
| `/api/brain` | GET | Brain summary (recent predictions aggregate) |
| `/api/brain/insights` | GET | Brain learning insights (per-asset patterns) |
| `/api/brain/learning` | GET | Brain learning log (recent updates) |
| `/api/brain/analyze` | GET | On-demand brain analysis for an asset |
| `/api/patterns` | GET | All detected chart patterns |
| `/api/patterns/{asset}` | GET | Patterns for a specific asset |
| `/api/patterns/refresh` | POST | Force pattern re-detection |
| `/api/strategies` | GET | All per-asset strategy assignments |
| `/api/current-strategy` | GET | Currently active strategy (per asset) |
| `/api/auto-tune` | GET | Auto-tune status + last run |
| `/api/auto-tune/apply` | POST | Trigger an auto-tune cycle |
| `/api/algorithm-changes` | GET | Algorithm change log (all assets) |
| `/api/algorithm-changes/{asset}` | GET | Algorithm changes for one asset |
| `/ws` | WS | WebSocket (subscribe, pairs, status, signals) |

<!-- FIX (DEEP-AUDIT-2026-07-26 / F-19-31): added 15 missing endpoints
     above (A-10 PROBLEM 6). Previously only 11 of 26 routes were
     documented, including the security-critical `/api/set-token` (GET+POST)
     and `/api/token-status`. -->

## WebSocket Protocol

### Client → Server
```json
{"type": "subscribe", "asset": "EURUSD_otc", "period": 60, "category": "otc"}
{"type": "pairs"}
{"type": "status"}
{"type": "signals", "asset": "EURUSD_otc", "period": 60}
```

### Server → Client
```json
{"type": "snapshot", "asset": "...", "period": 60, "candles": [...], "prediction": {...}}
{"type": "tick", "asset": "...", "period": 60, "candle": {...}, "running_conf": "...", "micro": {...}, "prediction": {...}}
{"type": "eoc", "asset": "...", "period": 60, "candles": [...], "prediction": {...}, "accuracy": "correct"|"wrong"|"draw"|null}
{"type": "pairs", "real_pairs": [...], "otc_pairs": [...], "payout_floor_real": 70, "payout_floor_otc": 85, ...}
{"type": "status", "connected": true, "streams": {...}}
{"type": "signals", "signals": [...]}
{"type": "error", "error": "..."}
```

## Module Statistics

Run `python module_performance_report.py` to see per-module win rates
from `signals.db`. Also available via `/api/stats` as JSON.

## Tech Stack

- **Backend:** Python 3.10–3.12, FastAPI (`>=0.110.0,<0.115`), uvicorn[standard], websockets
- **HTTP client:** httpx (async, HTTP/2)
- **HTML parsing:** beautifulsoup4 (Quotex HTML scraping)
- **TLS / certs:** certifi (bundled CA roots), Firefox TLS cipher suite (vendored in `pyquotex/network/ssl_utils.py`)
- **Typing:** typing_extensions (real dep of `pyquotex/network/navigator.py`)
- **Browser fingerprinting:** fake-useragent (rotating User-Agent)
- **Env config:** python-dotenv
- **Database:** SQLite (WAL mode)
- **Frontend:** Vanilla JS, LightweightCharts v4.1.3, CSS Grid/Flexbox
- **Deployment:** Railway (NIXPACKS builder)
- **Broker API:** Vendored pyquotex (with Firefox TLS cipher suite to
  bypass Cloudflare bot detection)

See `requirements.txt` for the full pinned list.

## Scripts

The `scripts/` folder contains maintenance and verification tools:

| Script | Purpose |
|---|---|
| `scripts/backtest_fixes.py` | Verify the 13 prediction-bug fixes (BUG-01..BUG-13). Run: `python scripts/backtest_fixes.py` |
| `scripts/backtest_remaining.py` | Verify the A1-A10 LOW/MEDIUM audit fixes. Run: `python scripts/backtest_remaining.py` |
| `scripts/backtest_weak_neutral.py` | Verify WEAK→NEUTRAL Options A+B (EOC + LIVE). Run: `python scripts/backtest_weak_neutral.py` |
| `scripts/deep_analysis.py` | Static analysis scanner — finds potential numeric precision / IndexError / division-by-zero issues. Run: `python scripts/deep_analysis.py` |
| `scripts/setup_railway_token.py` | Push QX_TOKEN to Railway Variables via GraphQL API. Run: `python3 scripts/setup_railway_token.py set --token "..."` |
| `scripts/verify_no_sim_mode.py` | Verify sim mode is permanently disabled and live-data-only is enforced. Run: `python3 scripts/verify_no_sim_mode.py` |
| `scripts/verify_live_audit_fixes.py` | Verify all 12 live-audit fixes are applied. Run: `python3 scripts/verify_live_audit_fixes.py` |

## Troubleshooting

### "no Quotex credentials" error
The server starts but stream subscriptions return this error if `QX_TOKEN`
is missing. Sim mode is permanently disabled.
- **Fix:** Set `QX_TOKEN` in Railway Variables, `.env`, or via `/api/set-token`.
- See `RAILWAY_TOKEN_SETUP.md` for the full token-refresh workflow.

### `/api/debug` shows `connected: true` but no candles flow
This was the "silent tick death" bug (AUDIT-1-18) — already fixed. The
WebSocket now closes on `authorization/reject`, triggering a clean
reconnect. If you still see this on an old deploy, refresh the token via
`POST /api/set-token` or force a redeploy.

### `ModuleNotFoundError: No module named 'sim_feed'`
You're running an old script that still imports `sim_feed`. Update to the
latest `scripts/` (the import was removed in the 2026-07-26 audit).

### `IndexError` or `KeyError` from `feed.py` during startup
Check `signals.db` integrity: `sqlite3 signals.db "PRAGMA integrity_check;"`.
If the DB is corrupt, stop the server, delete `signals.db` (and
`signals.db-shm` / `signals.db-wal`), and restart — the DB will be
re-created on startup.

### Browser doesn't auto-open
On Railway there's no GUI. Locally, if the browser doesn't open
automatically, navigate to http://localhost:8000 manually.

## Contributing

1. Fork the repo and create a feature branch.
2. Run the backtest suite before pushing:
   ```bash
   python scripts/backtest_fixes.py
   python scripts/backtest_remaining.py
   python scripts/verify_live_audit_fixes.py
   python scripts/verify_no_sim_mode.py
   ```
   All tests must pass (exit code 0). `verify_live_audit_fixes.py` may
   exit 2 if FIX #8/#9 are deferred — that's expected.
3. Do NOT commit your `.env`, `session.json`, or `signals.db`.
4. Keep `scripts/` and `core/` imports clean — run
   `python scripts/deep_analysis.py` and address any new MEDIUM-severity
   findings.

## License

See the original repository at https://github.com/psfaruk/Binary-signals-app.

<!-- FIX (DEEP-AUDIT-2026-07-26 / F-19-32): added Scripts section, Troubleshooting
     section, Contributing section, full Tech Stack list, and Python version
     upper bound. A-10 PROBLEMS 68, 69, 70, 83, 88, 89, 106. -->
