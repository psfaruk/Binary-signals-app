# 🚂 Railway Deployment & Token Setup Guide

> **Question this answers:** "Configure the token on Railway so it auto-setups."

---

## 🎯 Short Answer (TL;DR)

**Sim mode is now permanently disabled.** The app requires live Quotex credentials. Three ways to provision the token on Railway:

| Method | How | When to Use |
|---|---|---|
| **1. Railway Variables** (manual) | Set `QX_TOKEN` in Railway dashboard | Initial deploy (one-time) |
| **2. `setup_railway_token.py` script** (semi-auto) | Run locally → pushes to Railway via API | Token refresh (no dashboard clicks) |
| **3. `/api/set-token` URL** | Open a URL in your browser | Runtime refresh (no redeploy) |

> ⚠️ **Important**: There is NO fully-automatic token extraction. Cloudflare blocks headless login from Railway's datacenter IPs. The token MUST come from a real browser session. The "auto-setup" here means: after the one-time Railway API token setup, you can push new Quotex tokens with a single command — no dashboard clicks, no manual redeploy.

---

## 📋 Prerequisites

1. A Quotex account (free or funded)
2. A Railway account (https://railway.app)
3. The repo forked/pushed to your GitHub
4. Python 3.10+ on your local machine (for the auto-setup script)

---

## 🚀 Step-by-Step: First-Time Railway Setup

### Step 1 — Deploy the repo to Railway

1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo**
2. Select your fork of `Binary-signals-app`
3. Railway auto-detects `railway.json` and starts building with NIXPACKS
4. Wait for the first deploy to finish (2-3 minutes)
5. The deploy will FAIL the healthcheck — that's expected, because `QX_TOKEN` is not set yet

### Step 2 — Get your Quotex token

The Quotex `ssid` token is required because Cloudflare blocks username/password login from Railway's datacenter IPs. You must extract it from a logged-in browser session:

1. Open https://quotex.com in Chrome/Firefox
2. Log in normally
3. Open DevTools (F12) → **Network** tab
4. Reload the page (Ctrl+R)
5. In the Network filter, type `authorization`
6. Click on the request that appears → **Payload** tab
7. Copy the value of the `"session"` field — it will look like:
   ```
   "quotex-token.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
   ```
   (the part inside the quotes, without the quotes)
8. Save this token somewhere safe — it expires in ~24 hours

### Step 3 — Set the token in Railway (Method 1 — manual)

For the FIRST deploy only, use the Railway dashboard:

1. In Railway dashboard → your project → **Variables** tab
2. Click **New Variable**
3. Key: `QX_TOKEN`
4. Value: paste the token from Step 2
5. Click **Add** — Railway will auto-redeploy

### Step 4 — Verify it's working

1. Wait for the redeploy to finish
2. Open your app URL (Railway dashboard → **Open** button)
3. Open `/api/debug` in a new tab:
   ```
   https://your-app.up.railway.app/api/debug
   ```
4. Look for:
   ```json
   {
     "sim_mode": false,
     "connected": true,
     ...
   }
   ```
5. If `sim_mode: false` and `connected: true`, you're live! 🎉
6. If `sim_mode: true`, the token didn't take effect — check the logs

---

## 🔄 Method 2 — Auto-Setup Script (semi-automatic, RECOMMENDED for refreshes)

After the first deploy, use the included script to push new tokens without
touching the Railway dashboard. This is the closest thing to "auto-setup"
possible given Cloudflare's restrictions.

### Step 2a — One-time script setup

1. **Get a Railway API token:**
   - Go to https://railway.app/account/tokens
   - Click **New Token** → name it "qx-token-sync"
   - Copy the token (starts with "railway-...")

2. **Find your project + service IDs:**
   ```bash
   cd Binary-signals-app
   export RAILWAY_API_TOKEN="railway-..."
   python3 scripts/setup_railway_token.py --list
   ```
   This prints all your projects, services, and environments with their IDs.

3. **Set environment variables:**
   Add these to your `~/.bashrc` or `~/.zshrc`:
   ```bash
   export RAILWAY_API_TOKEN="railway-..."
   export RAILWAY_PROJECT_ID="prj-..."        # from --list output
   export RAILWAY_SERVICE_ID="svc-..."        # from --list output
   export RAILWAY_ENVIRONMENT_ID="env-..."    # from --list output (optional)
   ```

### Step 2b — Push a new token (whenever it expires)

```bash
cd Binary-signals-app
python3 scripts/setup_railway_token.py set --token "quotex-token.eyJhbGc..."
```

That's it. The script:
1. Validates the token format
2. Pushes it to Railway Variables via the GraphQL API
3. Railway auto-redeploys within ~30 seconds
4. The app picks up the new token on startup

### Step 2c — Verify

```bash
# Verify the variable was set:
python3 scripts/setup_railway_token.py get

# Or check via the app:
curl https://your-app.up.railway.app/api/debug | python3 -m json.tool
```

---

## 🌐 Method 3 — Runtime Token Refresh (no redeploy)

When you don't want to wait for a redeploy, refresh the token at runtime:

### Option A — Browser URL (simplest)

Open this URL in any browser:
```
https://your-app.up.railway.app/api/set-token?token=YOUR_NEW_TOKEN_HERE
```

JSON response:
```json
{
  "ok": true,
  "message": "Token set (quotex-t...XXXX). Real feed reconnecting...",
  "timestamp": 1753449600.0,
  "sim_mode_disabled_permanently": true,
  "next_step": "Wait 5-10 seconds, then check /api/debug to confirm connected:true"
}
```

### Option B — POST request

```bash
curl -X POST https://your-app.up.railway.app/api/set-token \
     -H "Content-Type: application/json" \
     -d '{"token":"YOUR_NEW_TOKEN_HERE"}'
```

### Option C — Python script

```python
import requests
resp = requests.post(
    "https://your-app.up.railway.app/api/set-token",
    json={"token": "YOUR_NEW_TOKEN_HERE"}
)
print(resp.json())
```

> ⚠️ Note: tokens set via Method 3 are persisted to `session.json` on disk, but Railway's filesystem is ephemeral and is reset on every redeploy. For tokens that survive redeployments, use Method 1 or Method 2.

---

## ⚙️ Recommended Railway Variables

Set these in the Railway **Variables** tab for production use:

| Variable | Value | Purpose |
|---|---|---|
| `QX_TOKEN` | (your token) | **REQUIRED** — Quotex auth (sim mode disabled) |
| `QX_USE_RAW_WS` | `0` | Use vendored pyquotex (Firefox TLS bypass) |
| `USE_SIM` | `0` | Ignored (sim mode permanently disabled) — set for clarity |
| `AUTO_OPEN_BROWSER` | `0` | Railway has no GUI |
| `HEADLESS` | `1` | Headless browser (if used) |
| `QX_PAYOUT_FLOOR_REAL` | `70` | Min payout for real pairs |
| `QX_PAYOUT_FLOOR_OTC` | `85` | Min payout for OTC pairs |

---

## 🕒 Market Hours Behavior

The app handles market hours correctly:

| Market | Hours | App Behavior |
|---|---|---|
| **OTC pairs** (e.g., `EURUSD_otc`) | 24/7/365 | Always live — always available |
| **Real pairs** (e.g., `EURUSD`) | Mon 00:00 UTC → Fri 22:00 UTC | Available during forex hours |
| **Real pairs** (weekends) | Fri 22:00 UTC → Sun 22:00 UTC | Filtered out — not shown in dropdown |

The frontend shows a clear "Market Closed" indicator on weekends for real pairs. OTC pairs are always available regardless of day/time.

---

## 🩺 Troubleshooting

### Problem: `/api/debug` shows `sim_mode: true`

**Impossible** — sim mode is permanently disabled. If you see this, you're running an OLD deploy. Force a fresh redeploy:
```bash
# Via Railway CLI (if installed):
railway up

# Or via Railway dashboard → Settings → Redeploy
```

### Problem: App refuses to start — "no Quotex credentials"

This is the new behavior. The server starts (so `/api/set-token` is reachable) but all stream subscriptions return:
```json
{
  "ok": false,
  "status": "no_credentials",
  "error": "no Quotex credentials — sim mode is disabled. Set QX_TOKEN...",
  "action": "set_token"
}
```

**Fix**: Set `QX_TOKEN` via Method 1, 2, or 3 above.

### Problem: `/api/debug` shows `connected: true` but no candles flow

This was the "silent tick death" bug (AUDIT-1-18) — already fixed. The WebSocket now closes on `authorization/reject`, triggering a clean reconnect. If you still see this on an old deploy, refresh the token via Method 3.

### Problem: Token keeps expiring every 24 hours

Quotex tokens are short-lived by design. The most practical workflow:

1. **Manual refresh (simplest)**: every ~20 hours, extract a fresh token from your browser (Step 2 above) and run:
   ```bash
   python3 scripts/setup_railway_token.py set --token "new-token..."
   ```

2. **Cron job on your local machine** (semi-auto):
   ```bash
   # Crontab — refresh token every 12 hours (you must update token.txt manually)
   0 */12 * * * /usr/bin/python3 /path/to/Binary-signals-app/scripts/setup_railway_token.py set --token "$(cat /path/to/token.txt)"
   ```

3. **Browser extension** (advanced, fully auto): build a Chrome/Firefox extension that:
   - Detects when you're on quotex.com
   - Auto-extracts the `session` token from the page
   - POSTs it to your app's `/api/set-token` endpoint
   - This requires custom development — see the extension skeleton below.

> ⚠️ There is NO truly automatic token rotation from Railway alone. Cloudflare's bot protection makes headless login impossible from datacenter IPs. The token MUST originate from a real browser session.

---

## 🔧 Browser Extension Skeleton (advanced, fully-auto refresh)

For users who want true automation, here's a minimal Firefox/Chrome extension that auto-syncs the token:

**`manifest.json`:**
```json
{
  "manifest_version": 3,
  "name": "Quotex Token Sync",
  "version": "1.0",
  "permissions": ["webRequest", "storage"],
  "host_permissions": ["https://quotex.com/*", "https://*.up.railway.app/*"],
  "background": { "scripts": ["background.js"] }
}
```

**`background.js`:**
```javascript
// Listen for authorization requests on quotex.com
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (!details.requestBody) return;
    const raw = details.requestBody.raw?.[0]?.bytes;
    if (!raw) return;
    const text = new TextDecoder().decode(raw);
    try {
      const payload = JSON.parse(text);
      if (payload.session) {
        // Token found! Push to Railway app.
        fetch("https://your-app.up.railway.app/api/set-token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: payload.session })
        });
        console.log("[qx-sync] token pushed to Railway app");
      }
    } catch (e) { /* not an auth request, ignore */ }
  },
  { urls: ["https://quotex.com/*"] },
  ["requestBody"]
);
```

Install this extension in your browser. Whenever you log into Quotex, the extension automatically extracts the token and pushes it to your Railway app. No manual steps required.

---

## 🔐 Security Notes

1. **Never commit your real `.env` file** to Git. The `.gitignore` should exclude it.
2. **The `/api/set-token` endpoint is UNPROTECTED** — anyone with the URL can set the token. For a personal app this is fine. For multi-user, add an auth header check.
3. **Rotate the token if you suspect it leaked.** Quotex tokens can be invalidated by logging out of all sessions from your Quotex account page.
4. **Railway API token**: keep `RAILWAY_API_TOKEN` private. Anyone with it can modify your Railway project's variables.
5. **Audit logs**: Railway keeps 7 days of deploy logs. Check them if the app behaves unexpectedly.

---

## ✅ Pre-deployment Checklist

- [ ] `railway.json` specifies NIXPACKS builder (✓ present)
- [ ] `QX_TOKEN` set in Railway Variables (REQUIRED — sim mode disabled)
- [ ] `USE_SIM=0` set (or omitted — sim is ignored either way)
- [ ] `AUTO_OPEN_BROWSER=0` set (Railway has no GUI)
- [ ] `QX_USE_RAW_WS=0` set (use vendored pyquotex)
- [ ] `/api/set-token` endpoint tested with a valid token (✓ fixed in audit)
- [ ] `/api/debug` shows `sim_mode: false` and `connected: true`
- [ ] Candles flow visible in the chart within 60 seconds
- [ ] `scripts/setup_railway_token.py` configured locally for future refreshes

---

## 📞 Summary of Audit Fixes Applied to Token Flow + Sim Mode

| Bug ID / Change | File | Fix |
|---|---|---|
| AUDIT-1-01 | `server.py:11,304` | Added `Request` import + type annotation on POST endpoint |
| AUDIT-1-02 | `quotex_ws.py:404-419` | New `save_token_only(token)` method |
| AUDIT-1-02 | `server.py:363` | Use `save_token_only` instead of broken `save_session_json(token)` |
| AUDIT-1-18 | `quotex_ws.py:630-648` | Close WS on `authorization/reject` → triggers clean reconnect |
| SIM-DISABLE | `server.py:33-100` | Removed sim fallback import; require QX_TOKEN; log clear error if missing |
| SIM-DISABLE | `feed.py:1097-1150` | Replaced `_fallback_to_sim_if_stuck` with `_warn_if_stuck` (no sim) |
| SIM-DISABLE | `feed.py:1014-1041` | `ensure_stream` returns clear error if no credentials (no silent sim) |
| SIM-DISABLE | `feed.py:801-807` | `available_pairs` no longer routes to sim delegate |
| SIM-DISABLE | `feed.py:1173-1217` | `_aggressive_reconnect` clears any stray sim delegate |
| SIM-DISABLE | `sim_feed.py` | Renamed to `sim_feed.py.DISABLED` — no longer importable |
| AUTO-SETUP | `scripts/setup_railway_token.py` | NEW — push tokens to Railway via GraphQL API |
| AUTO-SETUP | `railway.json` | Added `env_required.QX_TOKEN` + default env vars |

These changes together ensure:
- **No silent sim fallback** — the app ALWAYS runs on live data
- **Token persistence** — `save_token_only` correctly persists to `session.json`
- **Clean token expiry** — WS closes on auth-reject, triggers reconnect
- **Auto-setup workflow** — push tokens via API script, no dashboard clicks
- **Loud failure** — if no token, server starts but streams return clear error
