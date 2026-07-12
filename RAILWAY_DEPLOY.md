# Railway Deployment Guide — বাংলা

## Railway-তে Deploy করার সম্পূর্ণ নিয়ম

═══════════════════════════════════════════════════════════════════════

### ধাপ ১: Railway অ্যাকাউন্ট তৈরি করুন

1. https://railway.app এ যান
2. "Login" ক্লিক করুন
3. GitHub দিয়ে login করুন (Authorize Railway ক্লিক করুন)

═══════════════════════════════════════════════════════════════════════

### ধাপ ২: New Project তৈরি করুন

1. Railway dashboard-এ "New Project" ক্লিক করুন
2. "Deploy from GitHub repo" সিলেক্ট করুন
3. `psfaruk/Binary-signals-app` সিলেক্ট করুন
4. "Deploy Now" ক্লিক করুন

═══════════════════════════════════════════════════════════════════════

### ধাপ ৩: Variables সেট করুন (সবচেয়ে গুরুত্বপূর্ণ)

Railway dashboard → আপনার project → "Variables" tab → "New Variable"

এই ৫টি variable যোগ করুন:

```
Name:  QX_EMAIL
Value: plybitai.com@gmail.com
```

```
Name:  QX_PASSWORD
Value: 56529050Fk/
```

```
Name:  QX_USE_RAW_WS
Value: 1
```

```
Name:  HEADLESS
Value: 1
```

```
Name:  AUTO_OPEN_BROWSER
Value: 0
```

═══════════════════════════════════════════════════════════════════════

### ধাপ ৪: Deploy শুরু হবে

- Railway স্বয়ংক্রিয়ভাবে build শুরু করবে
- "Deployments" tab-এ status দেখুন
- Build শেষ হতে ৫-১০ মিনিট লাগবে (Playwright browser download হবে)
- "Active" status দেখলে deploy সফল!

═══════════════════════════════════════════════════════════════════════

### ধাপ ৫: Public URL পান

1. Railway dashboard → আপনার project → "Settings" tab
2. "Networking" section → "Generate Domain" ক্লিক করুন
3. একটি URL পাবেন, যেমন:
   ```
   https://binary-signals-app-production.up.railway.app
   ```
4. এই URL দিয়ে যেকোনো ডিভাইস থেকে অ্যাক্সেস করতে পারবেন!

═══════════════════════════════════════════════════════════════════════

### ধাপ ৬: লগ চেক করুন

1. Railway dashboard → আপনার project → "Deployments" tab
2. সর্বশেষ deploy-এ ক্লিক করুন
3. "Deploy Logs" দেখুন
4. সফল হলে দেখবেন:
   ```
   [server] Railway environment detected
   [feed] browser-login: trying curl_cffi...
   [feed] browser-login ok — ssid=XXXX...
   [feed] connect -> ok=True  reason=connected
   [feed] pairs loaded: 38 forex pairs
   INFO:     Uvicorn running on http://0.0.0.0:PORT
   ```

═══════════════════════════════════════════════════════════════════════

## ⚠️ গুরুত্বপূর্ণ মনে রাখবেন

### ১. Railway-তে Cloudflare Block হতে পারে

Railway-এর IP datacenter IP — Cloudflare সেটা block করতে পারে।
যদি login fail করে, তাহলে:

**সমাধান ১:** Local PC থেকে session.json তৈরি করে Railway-এ আপলোড করুন
1. Local PC-তে অ্যাপ চালান → session.json তৈরি হবে
2. Railway → Variables → নতুন variable যোগ করুন:
   ```
   QX_TOKEN = <session.json থেকে token value paste করুন>
   ```

**সমাধান ২:** Railway-তে সবসময় নতুন token আনতে Playwright fallback কাজ করবে
(headless mode + stealth library দিয়ে)

### ২. Railway Free Plan সীমাবদ্ধতা

- মাসে $5 credit free (≈ 500 hours usage)
- কিছু সময় পরে app sleep হতে পারে
- সবসময় চালু রাখতে paid plan ($5/month) লাগবে

### ৩. Token Expire হলে

Railway-তে app চলতে থাকলে token auto-refresh হবে:
```
[feed] auto-relogin: doing fresh browser-login...
[feed] browser-login ok — ssid=XXXX...
```

যদি Cloudflare block করে, তাহলে local PC থেকে নতুন QX_TOKEN
Railway Variables-এ আপডেট করতে হবে।

═══════════════════════════════════════════════════════════════════════

## সমস্যা সমাধান

### সমস্যা: Build fail করছে

লগ দেখুন। সাধারণত:
- Playwright install fail → Dockerfile-এ system deps মিস করছে
- pip install fail → requirements.txt-এ কিছু missing

### সমস্যা: App crash করছে

Deploy logs দেখুন। সাধারণত:
- Quotex login fail → QX_EMAIL/QX_PASSWORD চেক করুন
- Port error → PORT env var Railway সেট করে দেয়

### সমস্যা: WebSocket connection fail

Railway-তে WebSocket support আছে, কিন্তু কিছু সময় লাগতে পারে।
১-২ মিনিট অপেক্ষা করুন।

═══════════════════════════════════════════════════════════════════════

## সংক্ষেপে

1. Railway-তে login করুন
2. GitHub repo সিলেক্ট করুন
3. Variables সেট করুন (QX_EMAIL, QX_PASSWORD, QX_USE_RAW_WS=1, HEADLESS=1)
4. Generate Domain ক্লিক করুন
5. URL দিয়ে অ্যাক্সেস করুন!

**Deploy time:** ৫-১০ মিনিট
**Cost:** Free plan ($5 credit/month)
**URL:** https://your-app-name.up.railway.app
