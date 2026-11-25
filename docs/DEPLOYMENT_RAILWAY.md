# Railway Deployment Guide (রেলওয়ে ডিপ্লয় গাইড) — 2026-11-26

> এই গাইড `main` branch-এর জন্য: `Dockerfile` + `railway.toml` + `Procfile`
> + `run_service.sh` — সব একসাথে একটা **long-running Railway worker**
> হিসেবে অ্যাপটি চালাবে।
>
> **গুরুত্বপূর্ণ সত্য:** এই অ্যাপ Vercel serverless-এর জন্য তৈরি নয় —
> `quotex_ws.py` একটা **persistent WebSocket** ধরে রাখে, প্রতি ক্যান্ডেলে
> timer চলে, আর SQLite-এর জন্য Volume লাগে। Serverless ফাংশন রিকোয়েস্টের
> মাঝে ফ্রিজ হয়ে যায় — ফিড মরে যায়। Railway-ই সঠিক প্ল্যাটফর্ম।

---

## ০. কেন Railway (এক নজরে)

| প্রয়োজন | Vercel serverless | Railway worker |
|---|---|---|
| Quotex WebSocket (২৪/৭ সংযোগ) | ❌ ফাংশন ফ্রিজ হয় | ✅ প্রসেস চলতে থাকে |
| প্রতি-মিনিটের candle timer | ❌ | ✅ |
| SQLite + persistent Volume | ❌ | ✅ `/app/data` mount |
| WebSocket UI broadcast (`/ws`) | ❌ | ✅ |
| Token minter (আলাদা role) | ❌ | ✅ `QX_SERVICE_ROLE=minter` |

---

## ১. Prerequisites

1. [Railway](https://railway.app) অ্যাকাউন্ট (Hobby plan-এ Volume সহ সীমিত
   ব্যবহার পাওয়া যায়; সিরিয়াস ব্যবহারে Pro)।
2. এই repo আপনার GitHub-এ fork/push করা (`main` branch)।
3. একটা বৈধ Quotex SSID token (নিচের minter সেকশন দেখুন)।

---

## ২. Deploy করা (Dashboard দিয়ে — সবচেয়ে সহল)

1. **Railway Dashboard → New Project → Deploy from GitHub repo**
   → `Binary-signals-app` সিলেক্ট করুন।
2. Railway নিজে থেকেই `railway.toml` পড়ে **Dockerfile builder** ব্যবহার
   করবে — build command হাতে লিখতে হবে না।
3. প্রথম deploy শেষ হলে **Settings → Networking → Generate Domain**
   চাপুন → `https://<your-app>.up.railway.app` পাবেন।

CLI দিয়ে (ঐচ্ছিক):

```bash
npm i -g @railway/cli
railway login
railway init          # নতুন প্রজেক্ট
railway up            # এই repo থেকে deploy
railway domain        # পাবলিক URL
```

---

## ৩. Volume সেটআপ (একবারই — না করলে ডেটা মুছে যাবে!)

> **সতর্কতা:** Railway container **ephemeral** — Volume ছাড়া প্রতিটি
> redeploy-এ `signals.db` মুছে যায় (2026-09-09 এর ঘটনা: ১৪k+ সিগন্যাল
> রো + ৭৯k learned votes হারিয়েছিল)।

1. Service → **Settings → Volumes → New Volume**
2. **Mount path: `/app/data`** (হুবহু এটাই — `db.py` এই পাথ
   auto-detect করে)
3. Size: 1 GB যথেষ্ট (Volume বেশি ভরাট হলে `QX_RETENTION_*`
   ভেরিয়েবল দিয়ে পুরনো রো ছাঁটার নিয়ম `.env.example`-এ আছে)।

যাচাই: deploy-এর পর
`curl https://<your-app>.up.railway.app/api/latency` — সাড়া এলে সার্ভার
বেঁচে আছে; লগে `DB_PATH=/app/data/signals.db` এর মতো লাইন খুঁজুন।

---

## ৪. Environment Variables (Variables tab)

ন্যূনতম যেটা লাগবেই:

| Variable | মান | কেন |
|---|---|---|
| `QX_TOKEN` | Quotex SSID | লাইভ ফিড (নিচের minter বিকল্প দেখুন) |
| `ADMIN_KEY` | শক্তিশালী র‍্যান্ডম secret | token-push/admin API গার্ড (Phase-J) |

প্রায়ই দরকার হয়:

| Variable | ডিফল্ট | নোট |
|---|---|---|
| `DB_PATH` | auto (`/app/data`) | Volume mount থাকলে ছুঁবেন না |
| `QX_AUTO_REFRESH` | `1` | ~৬ ঘণ্টা পর টোকেন auto-refresh |
| `QX_PAYOUT_FLOOR_OTC` | `85` | পেআউট অনুমান (break-even 54.05%) |
| `QX_WEAK_NEUTRAL` | `0` | `1` করলে WEAK সিগন্যাল NEUTRAL হবে |
| `QX_DEMO_FEED` | `0` | `1` = লোকাল সিন্থেটিক টিক (ব্যানার-সহ) |

বাকি সব ভেরিয়েবলের ব্যাখ্যা `.env.example`-এ — কপি করে Variables-এ
বসালেই হবে। `.env` ফাইল কখনো repo-তে commit করবেন না।

---

## ৫. Token Minter (geo-block সমাধান — ঐচ্ছিক কিন্তু সুপারিশকৃত)

Quotex **দেশ অনুযায়ী** geo-block করে (US datacenter থেকে trade পেজ
ব্লক, কিন্তু WebSocket ব্লক নয়)। সমাধান — একই repo থেকে দ্বিতীয়
service:

1. একই প্রজেক্টে **New Service → GitHub repo (same repo)**
2. তার Variables-এ:
   - `QX_SERVICE_ROLE=minter`
   - `QX_EMAIL` / `QX_PASSWORD` (বা seed `QX_TOKEN`)
   - `APP_URL=https://<main-app>.up.railway.app` (টোকেন এখানে push করবে)
3. Main service-এ `ADMIN_KEY` সেট থাকতে ভুলবেন না — minter সেটা দিয়েই
   authenticated push করে।

---

## ৬. Deploy-এর পর যাচাই (ক্রমানুসারে)

```bash
APP="https://<your-app>.up.railway.app"

curl $APP/healthz                          # সার্ভার বেঁচে আছে?
curl $APP/api/latency                      # ফিড latency
curl "$APP/api/live-prediction/EURUSD_otc" # রানিং ক্যান্ডেল প্রেডিকশন
                                           # + ensemble_rolling (নতুন!)
```

`ensemble_rolling` ব্লকে পাবেন — all-theory ensemble-এর **নিজে-নিজে
মাপা** accuracy: প্রতিটি theory-voice-র rolling accuracy, বর্তমান
weight, আর tier-ভিত্তিক direction accuracy (LOCKED/STRONG/MEDIUM/WEAK)।
এগুলোই আসল সত্য — প্রতিশ্রুতি নয়, মাপা সংখ্যা।

---

## ৭. খরচ ও সীমা

- Railway Volume: প্ল্যানভেদে ১–৫ GB — `signals.db` বড় হলে retention
  ভেরিয়েবল (`QX_RETENTION_DAYS` ইত্যাদি) ছোট করুন।
- প্রতি redeploy-এ নতুন container — Volume-এর বাইরে কিছু টিকবে না
  (`session.json` টোকেন-স্টোর Volume পাথে রাখতে চাইলে `QX_TOKEN_DIR`
  দেখুন)।

---

## ৮. সৎ সতর্কতা (এই repo-র নিয়ম, ভাঙা যাবে না)

1. **এটি একটি research application।** Phase-L final verdict: প্রতিটি
   measured arm break-even (54.05%) এর নিচে — লাইভ সিগন্যাল টুল হিসেবে
   validated নয়।
2. **Ensemble-এর LOCKED-tier direction accuracy ≥ ৯০% মাপা হয়েছে**
   (`scripts/ensemble_verification_report.json`, Wilson CI সহ) — কিন্তু
   সেটা *ক্যান্ডেলের দিক বলার* accuracy, **entry win-rate নয়**।
   Fair random walk-এ entry WR ≈ ৫০% (EV ঋণাত্মক) — এই সত্যটাই
   UI/API-তে আলাদা করে দেখানো হয়।
3. আসল সংখ্যা দেখুন আপনার নিজের ফিডে: `/api/live-prediction/<asset>` →
   `ensemble_rolling` — প্রতি ক্যান্ডেলে self-grade হয়, প্রথম ঘণ্টাতেই
   জমা শুরু।
