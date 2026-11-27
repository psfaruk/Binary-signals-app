# Deployment Guide — Binary Signals App on Vercel (2026-11)

> **VERCEL-DEPLOY** — Railway সম্পূর্ণ বাদ (`railway.json` ডিলিট)। Vercel কনফিগারেশন
> যোগ হয়েছে: `vercel.json` + `api/index.py` (মূল app) + `api/cron/refresh.py`
> (live signal burst worker) + `api/requirements.txt` (slim) + `core/supabase_pull.py`
> (Supabase → SQLite history read-back)।

---

## ⚠️ নিরাপত্তা সতর্কতা (প্রথমে পড়ুন)

আপনি চ্যাটে যে GitHub Token পাঠিয়েছেন সেটি **exposed**। অবিলম্বে:

1. https://github.com/settings/tokens এ যান
2. পুরোনো টোকেনটি **Revoke** করুন
3. নতুন টোকেন তৈরি করুন — আর কখনো চ্যাটে পাঠাবেন না
4. Quotex SSID-ও সংবেদনশীল — সবসময় environment variable এ, কখনো code/chat এ নয়

---

## 🧠 Vercel-এ এই অ্যাপ যেভাবে কাজ করে (জরুরি — পড়ুন)

Vercel একটি **serverless** প্ল্যাটফর্ম। এই অ্যাপের হৃদয় হলো Quotex-এর সাথে একটি
**persistent WebSocket feed** (tick স্ট্রিম → সিগন্যাল জেনারেশন → গ্রেডিং)।
Serverless-এ **কোনো চলমান প্রসেস থাকে না** — তাই আর্কিটেকচারটি এভাবে ভাগ করা হয়েছে:

| অংশ | Vercel-এ কী হয় |
|---|---|
| **UI (`static/app.html` + CSS/JS)** | ✅ স্বাভাবিক সার্ভ হয় (`api/index.py` মাধ্যমে) |
| **REST API (signals, history, winrate, stats, keys…)** | ✅ কাজ করে — প্রতি request-এ serverless function |
| **লাইভ Quotex feed + real-time WebSocket push** | ⚠️ **Burst mode** — Vercel Cron `api/cron/refresh` কে ডাকলে সেখানে সত্যিকারের feed `QX_CRON_BUDGET` সেকেন্ড (ডিফল্ট 240) চালু থাকে, সিগন্যাল তৈরি হয়, Supabase-এ সেভ হয় |
| **Durable ডাটা (signal history)** | ✅ **Supabase** (ফ্রি Postgres) — প্রতিটি burst এর পর `signal_log`, `otc_predictions`, `model_registry` সেখানে mirror হয়; UI-র ফাংশন সেগুলো ফিরিয়ে পড়ে |
| **SQLite** | প্রতি serverless instance-এ `/tmp` এ একটি ephemeral ক্যাশ — Supabase-ই একমাত্র স্থায়ী স্টোর |

**সীমাবদ্ধতা (সৎ কথা):**
- Vercel-এর সার্ভারলেস ফাংশন **WebSocket connection গ্রহণ করে না** — অ্যাপের `/ws` রুট সেখানে কাজ করবে না। তবে UI-তে আগে থেকেই ১০-সেকেন্ড REST polling fallback আছে (`refreshShareSignals`) — তাই সিগন্যাল টেবিল, হিস্ট্রি, win-rate সব **polling-এ** আপডেট হয়।
- Cron burst-এর বাইরের সময়ে নতুন candle ক্লোজ হলে সেটি পরের burst-এ ধরা পড়বে। **Hobby plan-এ** cron দিনে ১ বার + ৬০ সেকেন্ড duration → বার্স্ট কার্যত ডেমো-মাত্র; **Pro plan-এ** প্রতি ১৫ মিনিটে cron + ৩০০–৮০০ সেকেন্ড duration সেট করলে মানসম্মত কভারেজ পাবেন।
- যদি ২৪/7 টানা টিক-লেভেল স্ট্রিমিং দরকার হয় — সেটি Vercel-এর ডিজাইনেই অসম্ভব; সেক্ষেত্রে `run_service.sh` (uvicorn) যেকোনো persistent host (VPS/fly.io/Render) এ চালান।

---

## 🚀 Deploy করার ধাপ (৫ মিনিট)

### Step 1 — Vercel এ Project import করুন

1. https://vercel.com → **Add New → Project**
2. GitHub account কানেক্ট করে **psfaruk/Binary-signals-app** রিপো সিলেক্ট করুন
3. Settings এ নিশ্চিত করুন:
   - **Framework Preset:** `Other`
   - **Build Command:** (খালি — vercel.json এর `builds` সব হ্যান্ডেল করে)
   - **Output Directory:** (খালি)
4. Environment Variables এ নিচের Step 2 সেট করুন
5. **Deploy** চাপুন

`vercel.json` এ দুটি Python function ডিফাইন করা:
- `api/index.py` — পুরো ফাস্টএপিআই অ্যাপ + static UI (সব route এখানে যায়)
- `api/cron/refresh.py` — burst signal worker (`/api/cron/refresh`)

### Step 2 — Environment Variables (Vercel → Settings → Environment Variables)

| Var | মান | কেন |
|---|---|---|
| `QX_TOKEN` | আপনার Quotex SSID (`quotex-token.eyJ...`) | **আবশ্যক** — লাইভ ডাটা। ~২৪ ঘণ্টায় expire হয়; নতুন টোকেন বসিয়ে Redeploy করুন |
| `ADMIN_KEY` | শক্তিশালী random string | অ্যাডমিন endpoint গার্ড (না দিলে 503) |
| `CRON_SECRET` | শক্তিশালী random string | `/api/cron/refresh` কে আটকে রাখে — **অবশ্যই সেট করুন** |
| `SUPABASE_URL` | `https://xxxx.supabase.co` | durable signal store (দৃঢ়ভাবে recommended) |
| `SUPABASE_ANON_KEY` | anon public key | সাথে |
| `AUTO_OPEN_BROWSER` | `0` | সার্ভারলেসে ব্রাউজার নয় |
| `QX_PUBLIC_READ` | `1` | যে কেউ URL দিয়ে সিগন্যাল দেখবে (Phase-3 নিয়ম) |

Engine tuning (ঐচ্ছিক — আগের railway.json এর মানগুলো):
`QX_PAYOUT_FLOOR_OTC=85`, `QX_PAYOUT_FLOOR_REAL=70`, `QX_HISTORY_GATE=1`,
`QX_PAIR_HEALTH_GATE=1`, `QX_MIN_AGREE_CLUSTERS=3`, `QX_MIN_CONFLUENCE_CONF=65`,
`QX_RETENTION_ENABLED=1` (serverless-এ ডাটা Supabase-এ যায় বলে মূল্যহীন, তবু নিরাপদ),
`QX_CRON_BUDGET=240`

Telegram alert (ঐচ্ছিক): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

### Step 3 — Supabase একবার সেটআপ করুন (durable history)

1. https://supabase.com → New project (ফ্রি প্ল্যান যথেষ্ট) → region: Singapore
2. **SQL Editor** এ নিচের SQL চালান:

```sql
CREATE TABLE IF NOT EXISTS signal_log (
  id bigint PRIMARY KEY,
  asset text, period integer, ctime bigint,
  signal text, score integer, confidence double precision,
  theories text, actual text, accuracy text,
  strength text, agree integer,
  right_codes text, wrong_codes text, reasons text,
  a_open double precision, a_close double precision,
  regime text, zone text, tags text, postmortem text,
  category text, ts double precision, total integer,
  signal_quality text, strategy text
);
CREATE TABLE IF NOT EXISTS otc_predictions (
  id bigint PRIMARY KEY,
  asset text, period integer, signal_time bigint, target_time bigint,
  horizon integer, prediction text, probability double precision,
  tier text, score double precision, emit text, components text,
  regime text, pa_agreed text, quality double precision, reason text,
  model_version text, feature_json text, close_i double precision,
  created_at text, actual_open double precision, actual_close double precision,
  actual_result text, win_loss text, settled_at text
);
CREATE TABLE IF NOT EXISTS model_registry (
  id bigint PRIMARY KEY,
  name text, version text, scope text, asset text, trained_at text,
  metrics text, path text, active integer, created_at text
);
CREATE TABLE IF NOT EXISTS candle_micro (
  asset text, period integer, ctime bigint,
  open double precision, high double precision, low double precision,
  close double precision, buy_pct double precision, sell_pct double precision,
  pressure double precision, is_fight integer, crosses integer,
  hold_price double precision, hold_visits integer, phases text,
  reaction text, net double precision, tick_count integer,
  last_react double precision, round_near double precision,
  round_str double precision, gap_pct double precision, gap_type text,
  key_levels text, ticks_json text,
  PRIMARY KEY (asset, period, ctime)
);
```

3. Project Settings → API → **Project URL** ও **anon public key** কপি করে
   Vercel-এর `SUPABASE_URL` / `SUPABASE_ANON_KEY` এ বসান

**সার্কুলার ফ্লো:** cron burst → সিগন্যাল SQLite-এ → `sync_once()` Supabase-এ mirror →
পরের burst/API ফাংশন `supabase_pull.pull()` করে ইতিহাস ফিরে পায় → history-gate ও
pair-health শেখা তথ্য দিয়ে সিগন্যাল দেয়।

### Step 4 — Cron সেট করুন

`vercel.json` এ ডিফল্ট cron: **দিনে একবার ০৬:০০ UTC** (`0 6 * * *`) — সব প্ল্যানে
deploy সফল হবে এমন নিরাপদ ডিফল্ট।

- **Pro plan:** নিয়মিত কভারেজের জন্য বদলে দিন:
  ```json
  "crons": [{ "path": "/api/cron/refresh", "schedule": "*/15 * * * *" }]
  ```
  এবং Vercel Dashboard → Functions → `api/cron/refresh` → **maxDuration 300–800s**
  (Fluid Compute চালু থাকতে হবে), `QX_CRON_BUDGET` এ লিখুন যেমন `540`।
- **Hobby plan:** দৈনিক cron + ৬০s burst। চাইলে যেকোনো সময় ম্যানুয়ালি চালান
  (SECURITY-FIX C-1, 2026-11-27: সিক্রেট এখন Authorization হেডারেই লাগবে —
  query string-এ সিক্রেট পাঠানো নিষেধ, সেটা access log-এ লিক হয়):
  ```bash
  curl -H "Authorization: Bearer $CRON_SECRET" \
       "https://YOUR-APP.vercel.app/api/cron/refresh"
  ```
  (Vercel Cron নিজে `Authorization: Bearer <CRON_SECRET>` পাঠালে আটো পাস করবে; `CRON_SECRET` না সেট করলে endpoint এখন **503 fail-closed** — আগের ফেইল-ওপেন আচরণ বন্ধ।)

### Step 5 — Verify করুন

```bash
APP="https://YOUR-APP.vercel.app"

curl $APP/healthz | python3 -m json.tool          # {"ok": true, ...}
curl $APP/api/share-signals | python3 -m json.tool  # সিগন্যাল তালিকা
curl $APP/api/keys/verify                          # {"valid": false}
curl "$APP/api/cron/refresh" -H "Authorization: Bearer $CRON_SECRET"  # burst চালু করে ফল দেখায়
```

Browser-এ `$APP/` খুলুন → ৪ ট্যাব UI (Home / Chart Signal / History / Setting)
লোড হবে; সিগন্যাল ১০-সেকেন্ড polling-এ আপডেট হবে (WebSocket সংযোগ Vercel-এ
হবে না — UI নিজেই polling-এ ফিরে যায়)।

---

## 🔧 কাজ-করা/না-করা চূড়ান্ত তালিকা

| ফিচার | Vercel |
|---|---|
| UI, ৪-ট্যাব, সিগন্যাল টেবিল, হিস্ট্রি, win-rate, API key | ✅ |
| Public read (`QX_PUBLIC_READ=1`) | ✅ |
| লাইভ সিগন্যাল জেনারেশন | ✅ কিন্তু burst-মুখী (cron প্রতি চালনে `QX_CRON_BUDGET` সেকেন্ড) |
| Signal history দীর্ঘদিন টিকে থাকা | ✅ Supabase-এ (setup করলে) |
| ML model training (scikit-learn) | ❌ serverless-এ বন্ধ (numpy/sklearn ইনস্টল করা হয় না; মডেল ইনস্ট্যান্স জুড়ে টিকেও না) — confluence/history-gate ইঞ্জিন স্বাভাবিক চলে |
| Real-time WebSocket push | ❌ (Vercel সীমাবদ্ধতা) — polling fallback ব্যবহৃত |
| `/api/set-token` দিয়ে রানটাইমে টোকেন | ❌ (ephemeral FS) — `QX_TOKEN` env var-ই রাস্তা |

---

## 💻 Local dev (অপরিবর্তিত)

```bash
cd Binary-signals-app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # পূর্ণ manifest (numpy/sklearn সহ)
cp .env.example .env                       # QX_TOKEN দিন
./run.sh                                   # অথবা: uvicorn server:app --port 8000
```

Serverless-মোড লোকালে পরীক্ষা করতে:
```bash
QX_DISABLE_FEED=1 DB_PATH=/tmp/x.db AUTO_OPEN_BROWSER=0 QX_SKIP_DOTENV=1 \
  uvicorn server:app --port 8765
```

---

## ❓ FAQ

**Q: Deploy এ "maxDuration/memory limit" বা cron error এলো**
A: প্ল্যান-সীমা। Hobby-তে function duration 60s পর্যন্ত — cron বারবার চাইলে Pro
দরকার; vercel.json এ cron schedule `0 6 * * *` এ ফেরত আনু।

**Q: সিগন্যাল "—" দেখাচ্ছে / burst এ error আসছে**
A: `QX_TOKEN` expire হয়েছে সম্ভবত। Vercel → env var আপডেট → Redeploy। বার্স্ট
রেসপন্সের `synced_to_supabase` ফিল্ড দেখে নিশ্চিত হন Supabase কানেক্টেড।

**Q: কেউ বারবার cron URL ডেকে কস্ট বাড়াতে পারবে?**
A: `CRON_SECRET` সেট থাকলে পারবে না — সেটাই ডিফল্ট নির্দেশ।

**Q: Function region কোথায় চলে?**
A: ডিফল্ট Vercel region। Quotex কিছু দেশ ব্লক করে — দরকার হলে Pro-তে
`"regions": ["sin1"]` (Singapore) যোগ করুন vercel.json এ।

**Q: আগের Railway কনফিগ?**
A: মুছে ফেলা হয়েছে (`railway.json`)। ঐ সব env-নোট এখন এই ডকুমেন্টে ও
`DEPLOYMENT_V2.md` (ইতিহাস হিসেবে) সংরক্ষিত।
