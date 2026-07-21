# Binary Signals App

Real-time binary options signal generator for Quotex broker pairs. Uses two
separate prediction engines (one for OTC markets, one for Real markets)
with a shared 6-module analysis pipeline.

## Architecture

```
                     ┌──────────────────────────────────────┐
                     │            server.py                 │
                     │  FastAPI + WebSocket (uvicorn)       │
                     └──┬───────────────────────────────┬───┘
                        │                               │
            (USE_SIM=1) │                               │ (real Quotex creds)
                        ▼                               ▼
              ┌──────────────────┐            ┌──────────────────┐
              │   sim_feed.py    │            │     feed.py      │
              │  (simulated)     │            │  (real Quotex)   │
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
├── sim_feed.py                  # Simulated feed (for dev / no creds)
├── quotex_ws.py                 # Raw WebSocket Quotex client (alt backend)
├── module_performance_report.py # CLI per-module win-rate report
├── requirements.txt
├── railway.json                 # Railway deployment config
├── run.sh                       # Linux/Mac launcher
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

## Setup (Local Dev)

### Prerequisites
- Python 3.10+
- pip

### Install
```bash
git clone https://github.com/psfaruk/Binary-signals-app.git
cd Binary-signals-app
pip install -r requirements.txt
cp .env.example .env
# Edit .env and fill in your Quotex credentials (QX_TOKEN or QX_EMAIL+QX_PASSWORD)
# OR set USE_SIM=1 to run in simulation mode (no creds needed)
```

### Run
```bash
./run.sh         # Linux/Mac
# OR
start.bat        # Windows
# OR
python server.py
```

Open http://localhost:8000 in your browser.

## Railway Deployment

1. Push this repo to GitHub.
2. Go to [railway.app](https://railway.app) → New Project → Deploy from
   GitHub repo.
3. Set these variables in Railway's Variables tab:
   - `QX_TOKEN` or `QX_EMAIL` + `QX_PASSWORD` (real Quotex creds)
   - OR `USE_SIM=1` (simulation mode)
   - `QX_USE_RAW_WS=0` (default — uses vendored pyquotex with Firefox TLS)
   - `QX_PAYOUT_FLOOR_REAL=70`
   - `QX_PAYOUT_FLOOR_OTC=85`
4. Railway auto-detects `railway.json` and deploys.
5. Healthcheck at `/healthz` returns `{"ok": true}` when the server is up.

**Note:** Cloudflare blocks Quotex login from datacenter IPs (Railway
included). For production use, set `QX_TOKEN` directly (copied from your
browser's devtools after logging into Quotex manually), OR run with
`USE_SIM=1` for demos.

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
| `/ws` | WS | WebSocket (subscribe, pairs, status, signals) |

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

- **Backend:** Python 3.10+, FastAPI, uvicorn, websockets
- **Database:** SQLite (WAL mode)
- **Frontend:** Vanilla JS, LightweightCharts v4.1.3, CSS Grid/Flexbox
- **Deployment:** Railway (NIXPACKS builder)
- **Broker API:** Vendored pyquotex (with Firefox TLS cipher suite to
  bypass Cloudflare bot detection)

## License

See the original repository at https://github.com/psfaruk/Binary-signals-app
