# Deployment Guide — Binary Signals App v2.0

> **PHASE-2 to PHASE-10 FIX (2026-08-13)** — This guide covers the new
> consolidated app with 4-tab navigation, API key system, and "every candle
> gets a signal" mode.

---

## ⚠️ নিরাপত্তা সতর্ততা (প্রথমে পড়ুন)

আপনি চ্যাটে যে GitHub Token পাঠিয়েছিলেন সেটি **exposed**। অবিলম্বে:

1. https://github.com/settings/tokens এ যান
2. পুরোনো টোকেনটি **Revoke** করুন
3. নতুন টোকেন তৈরি করুন (চ্যাটে পাঠাবেন না)
4. নতুন টোকেন নিরাপদে রাখুন — environment variable হিসেবে, চ্যাটে নয়

---

## 📋 পরিবর্তনসমূহ (এই ভার্সনে যা হয়েছে)

### Phase 2 — Signal Generation Fix
- ✅ `engines/base/blender.py`: "last-resort" fallback যোগ — প্রতি candle এ CALL/PUT নিশ্চিত
- ✅ `feed.py`: WEAK→NEUTRAL override env-gated (`QX_WEAK_NEUTRAL=0` ডিফল্ট)
- ✅ `engines/__init__.py`: BREAKEVEN_GATE, PAIR_HEALTH_GATE, TRAP_HOUR সব env-gated (ডিফল্ট বন্ধ)
- ✅ `blender.py`: PATTERN_GATE, WEAK_FILTER, LOW_CONF_SKIP সব ডিফল্ট বন্ধ
- ✅ Smoke test: 320/320 candle এ signal এসেছে (100% coverage)

### Phase 3 — API Key System
- ✅ `core/api_keys.py` — নতুন module (SQLite-backed, SHA-256 hashed keys)
- ✅ FastAPI middleware — প্রতিটি request classify করে (public_read / api_key_write / admin)
- ✅ Endpoints: `GET/POST /api/keys`, `DELETE /api/keys/{id}`, `GET /api/keys/verify`
- ✅ Public read default ON — যে কেউ URL দিয়ে signals দেখতে পারবে
- ✅ API key optional — programmatic clients এর জন্য

### Phase 4 + 5 — Navigation + HTML Consolidation
- ✅ নতুন `static/app.html` — একটি single-page app (3 টা duplicate HTML কে replace করে)
- ✅ 4 ট্যাব: **Home**, **Chart Signal**, **History**, **Setting**
- ✅ Mobile (<1024px): bottom navigation bar
- ✅ Desktop (≥1024px): left sidebar
- ✅ Legacy URLs (`/otc.html`, `/real.html`, `/alltime_otc.html`) → 302 redirect

### Phase 6 — Dead Code Cleanup
- ✅ `core/realtime_analyzer.py` ডিলিট (615 LoC, কখনো import হয়নি)
- ✅ 9টি ফাইল ডিলিট: পুরোনো HTML/CSS/JS duplicates + agent-panel.js
- ✅ ~3000+ লাইন dead code সরানো হয়েছে

### Phase 7 — Signal Share Section
- ✅ প্রতি pair এর জন্য signal, confidence, strength দেখায়
- ✅ NEUTRAL row ও visually distinct
- ✅ Filter (type, signal, search) যোগ করা হয়েছে
- ✅ Auto-refresh প্রতি 10 সেকেন্ডে

### Phase 8 — Backtest Verification
- ✅ `scripts/smoke_test_signals.py` তৈরি
- ✅ 100% signal coverage verify হয়েছে

### Phase 9 — Local Smoke Test
- ✅ সব endpoint test পাস করেছে
- ✅ API key creation, verification, snapshot save সব কাজ করছে

---

## 🚀 Railway Deployment Steps

### Step 1: Code কে আপনার GitHub এ push করুন

```bash
# প্রথমে পুরোনো token revoke করে নতুন token তৈরি করুন (নিরাপত্তা সতর্কতা দেখুন)
# তারপর:

cd /home/z/my-project/Binary-signals-app

# একটি নতুন branch তৈরি করুন
git checkout -b phase-2-fix-v2

# সব পরিবর্তন add করুন
git add -A

# Commit করুন
git commit -m "Phase 2-10 fix: signal generation, API keys, 4-tab nav, dead code cleanup

- Add last-resort fallback in blender.py (every candle gets CALL/PUT)
- Disable PATTERN_GATE, WEAK_FILTER, BREAKEVEN_GATE, PAIR_HEALTH_GATE by default
- Add API key system (core/api_keys.py + middleware + /api/keys endpoints)
- Consolidate otc.html/real.html/alltime_otc.html into single app.html
- Add 4-tab navigation (Home, Chart Signal, History, Setting)
- Mobile: bottom nav bar / Desktop: left sidebar
- Delete dead code: realtime_analyzer.py, agent-panel.js, duplicate HTML/CSS/JS
- Smoke test: 100% signal coverage (320/320 candles)
- Update railway.json with new env vars
"

# Push করুন (নতুন token ব্যবহার করুন)
git push https://github.com/psfaruk/Binary-signals-app.git phase-2-fix-v2:main
# বা যদি আপনার git remote already set থাকে:
# git push origin phase-2-fix-v2:main
```

### Step 2: Railway এ ডিপ্লয়

Railway স্বয়ংক্রিয়ভাবে আপনার `main` branch এ push হলে ডিপ্লয় শুরু করবে।

1. Railway Dashboard এ যান → আপনার Binary Signals service
2. **Settings → Volumes** এ যান → নিশ্চিত করুন `/app/data` mount করা আছে
3. **Variables** tab এ গিয়ে নিশ্চিত করুন:
   - `QX_TOKEN` সেট করা আছে (আপনার Quotex SSID)
   - `ADMIN_KEY` সেট করা আছে (API key management এর জন্য একটি শক্তিশালী secret)
   - নতুন env vars সব ডিফল্ট মানে আছে (railway.json এ সেট করা)
4. **Deploy** button চাপুন যদি auto-deploy না থাকে

### Step 3: Verify করুন ডিপ্লয় সফল

```bash
# আপনার Railway app URL দিয়ে replace করুন
APP_URL="https://your-app.up.railway.app"

# Health check
curl $APP_URL/healthz

# Public endpoint (যে কেউ দেখতে পারবে)
curl $APP_URL/api/share-signals | python3 -m json.tool | head -20

# Allowlist
curl $APP_URL/api/allowlist

# API key verify (no key — should return valid:false)
curl $APP_URL/api/keys/verify

# Create API key (replace YOUR_ADMIN_KEY)
curl -X POST $APP_URL/api/keys \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  -d '{"label":"my-bot","rate_limit_per_min":60}'

# Browser এ খুলুন
# $APP_URL/ — consolidated app with 4-tab navigation
# $APP_URL/?market=real — Real market
# $APP_URL/?market=alltime_otc — All-Time OTC
```

### Step 4: Browser এ verify

1. `$APP_URL/` খুলুন
2. বাম দিকে sidebar দেখুন (desktop) অথবা নিচে bottom nav (mobile)
3. ৪ ট্যাব আছে কিনা দেখুন: Home, Chart Signal, History, Setting
4. **Setting** ট্যাব খুলুন → API Keys section দেখুন
5. **Home** ট্যাব এ live signal summary দেখুন
6. **Chart Signal** ট্যাব এ chart + share signal table দেখুন
7. **History** ট্যাব এ signals / accuracy / backtest দেখুন

---

## 🔧 Environment Variables Reference

### Required
| Var | Description |
|-----|-------------|
| `QX_TOKEN` | Quotex SSID token (live data এর জন্য) |

### Optional (Auth)
| Var | Default | Description |
|-----|---------|-------------|
| `ADMIN_KEY` | (unset) | Admin endpoints এর জন্য শক্তিশালী secret. না থাকলে admin endpoints 503 দেয় |
| `QX_PUBLIC_READ` | `1` | `1` = anyone URL দিয়ে read করতে পারবে; `0` = API key লাগবে |

### Signal Generation (Phase 2)
| Var | Default | Description |
|-----|---------|-------------|
| `QX_PATTERN_GATE` | `0` | `1` = pattern module না ফায়ার করলে NEUTRAL |
| `QX_ALLOW_WEAK_SIGNALS` | `1` | `0` = WEAK signals কে NEUTRAL করে |
| `QX_BREAKEVEN_GATE` | `0` | `1` = pair এর WR < breakeven হলে NEUTRAL |
| `QX_PAIR_HEALTH_GATE` | `0` | `1` = ৮ লজে পুরো pair বন্ধ |
| `QX_LOW_CONF_SKIP_OTC` | `0` | `< N` confidence হলে NEUTRAL (OTC) |
| `QX_LOW_CONF_SKIP_REAL` | `0` | `< N` confidence হলে NEUTRAL (Real) |
| `QX_TRAP_HOUR` | `0` | `1` = trap hour এ NEUTRAL |
| `QX_CHOP_GUARD` | `0` | `1` = ৩ বার wrong হলে NEUTRAL |
| `QX_WEAK_NEUTRAL` | `0` | `1` = feed.py তে WEAK→NEUTRAL |
| `QX_LOSS_COOLDOWN` | `0` | `1` = loss streak এ cooldown |
| `QX_PAIR_PENALTY_NEUTRAL` | `0` | `1` = pair penalty < 25 হলে NEUTRAL |
| `QX_NO_FALLBACK` | `0` | `1` = smart-fallback বন্ধ |
| `QX_WEAK_BOOST` | `1` | WEAK signals কে boost করে MEDIUM এ |
| `QX_TIERED_FILTER` | `0` | `1` = strict tiered filter (৯৭% suppress করে) |

### API Key System (Phase 3)
API key গুলো SQLite এ stored। `signals.db` এ `api_keys` table এ।
- Format: `qxa_` + 32 hex chars
- Transmission: `Authorization: Bearer qxa_...` header অথবা `?api_key=qxa_...` query param
- Public endpoints (কোনো key লাগে না): `/api/share-signals`, `/api/pairs`, `/api/history/*`, `/api/stats`, `/api/keys/verify`
- Key-required: `POST /api/share-signals/save`
- Admin (X-Admin-Key বা X-App-Pin): `/api/keys`, `/api/set-token`, `/api/session/*`

---

## 🧪 Smoke Test (লোকালে)

```bash
cd /home/z/my-project/Binary-signals-app

# ১. Signal generation smoke test
.venv/bin/python scripts/smoke_test_signals.py --candles 100

# ২. Server শুরু করুন (QX_TOKEN ছাড়া)
QX_TOKEN="" QX_SKIP_DOTENV=1 PORT=8765 AUTO_OPEN_BROWSER=0 \
  .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8765

# ৩. অন্য terminal এ endpoints test করুন
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/api/share-signals
curl http://127.0.0.1:8765/api/keys/verify
```

---

## 📁 নতুন ফাইল স্ট্রাকচার

```
Binary-signals-app/
├── server.py                    # FastAPI app (+ API key middleware + endpoints)
├── feed.py                      # Quotex feed (WEAK→NEUTRAL env-gated)
├── db.py                        # SQLite layer (unchanged)
├── railway.json                 # Updated env vars (Phase 2-3)
├── run_service.sh
├── requirements.txt
├── core/
│   ├── api_keys.py              # NEW — API key system
│   ├── brain.py                 # Cleaned
│   ├── signal_verifier.py       # Kept (status readers used by /api/verifier/*)
│   ├── agent_brain.py           # Kept (singleton + status readers used)
│   └── ...
├── engines/
│   ├── __init__.py              # BREAKEVEN/PAIR_HEALTH/TRAP_HOUR env-gated
│   ├── base/
│   │   ├── blender.py           # + last-resort fallback
│   │   └── ...
│   ├── otc/
│   └── real/
├── static/
│   ├── app.html                 # NEW — consolidated single-page app
│   ├── css/
│   │   ├── common.css           # Existing (cleaned references)
│   │   ├── token-panel.css      # Existing
│   │   └── app-nav.css          # NEW — bottom nav + sidebar styling
│   ├── js/
│   │   ├── common.js            # Existing
│   │   ├── app-nav.js           # NEW — 4-tab navigation + settings
│   │   ├── api-keys.js          # NEW — API key UI helpers
│   │   └── token-panel.js       # Existing
│   └── lightweight-charts.js
├── scripts/
│   ├── smoke_test_signals.py    # NEW — every-candle signal verification
│   ├── backtest_agent.py        # Existing
│   └── live_backtest.py         # Existing
└── (deleted)
    ├── static/otc.html
    ├── static/real.html
    ├── static/alltime_otc.html
    ├── static/index.html
    ├── static/css/otc.css
    ├── static/css/real.css
    ├── static/js/otc.js
    ├── static/js/real.js
    ├── static/js/agent-panel.js
    └── core/realtime_analyzer.py
```

---

## ❓ সাধারণ সমস্যা ও সমাধান

### Q: Signal Share table এ সব row "—" দেখাচ্ছে
**A:** Quotex token টি expired বা invalid। Token panel এ গিয়ে নতুন token import করুন। `/api/token-status` check করুন।

### Q: "admin endpoint — provide X-App-Pin or X-Admin-Key" error
**A:** API key management এর জন্য `ADMIN_KEY` env var Railway এ সেট করুন। অথবা Token panel এ PIN claim করুন।

### Q: Bottom nav দেখাচ্ছে না (mobile)
**A:** Screen width 1024px এর কম আছে কিনা দেখুন। Desktop এ sidebar দেখাবে। Mobile এ bottom nav।

### Q: পুরোনো URL /otc.html কাজ করছে না
**A:** পুরোনো URLs এখন redirect করে `/` এ। Bookmarks আপডেট করুন।

### Q: Signal গুলো WEAK strength দেখাচ্ছে
**A:** এটি স্বাভাবিক। Last-resort fallback সব candle এ signal দেয় কিন্তু WEAK strength এ। STRONG signals এর জন্য pattern + multi-module consensus দরকার। Setting এ "Show WEAK signals" toggle off করলে শুধু MEDIUM/STRONG দেখাবে।

---

## 🎯 Success Criteria (সব পূরণ হয়েছে)

- [x] প্রতি candle এ CALL বা PUT signal আসছে (smoke test এ 100% coverage)
- [x] Signal Share table এ সব pair এর জন্য signal দৃশ্যমান
- [x] Mobile এ bottom navigation, Desktop এ sidebar
- [x] ৪ ট্যাব: Home, Chart Signal, History, Setting
- [x] API key system কাজ করছে (Settings এ manage করা যায়)
- [x] Public URL দিয়ে যে কেউ signals দেখতে পারবে
- [x] Dead code সরানো হয়েছে (~3000+ লাইন)
- [x] Backtest এ signal count বৃদ্ধি verify হয়েছে
- [x] Local smoke test পাস

---

## ⚠️ গুরুত্বপূর্ণ নোট

1. **GitHub Token:** চ্যাটে দেওয়া টোকেন revoke করে নতুন নিন।
2. **Quotex Token:** লাইভ ডাটার জন্য `QX_TOKEN` প্রয়োজন। ~২৪ ঘন্টায় expire হয়।
3. **Railway Volume:** `/app/data` mount করা থাকতে হবে, নাহলে signals.db মুছে যাবে।
4. **ADMIN_KEY:** API key management এর জন্য একটি শক্তিশালী secret সেট করুন।
