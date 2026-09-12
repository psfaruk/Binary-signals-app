#!/usr/bin/env python3
"""scripts/e2e_future_candle.py — FUTURE-CANDLE end-to-end (AUDIT 2026-09-13).

USER COMPLAINT (verbatim): "মডেল গুলো ফিউচার ক্যান্ডেল দেখানোর কথা কিন্তু
দেখাচ্ছে না কেনো আসলে কোথায় সমস্যা।"

This E2E proves the full fix chain end-to-end on a seeded temp DB:

  PHASE A — geometry unit checks (core/otc_predict/geometry.py)
      * CALL/PUT candle internal consistency (high>=max(o,c), low<=min(o,c))
      * conviction scaling: higher probability -> larger expected body
      * atr_from_candles > 0 on synthetic windows (incl. degenerate input)

  PHASE B — live WS payload (predictor.on_candle_closed with a REAL trained
             bundle registered in the model registry)
      * t1.candle / t2.candle exist with target times exactly
        closed_time + h*period
      * candle direction matches the frozen prediction (CALL => close>open)
      * per-horizon quality gates present (P6 fix: t1's no longer clobbered)
      * freeze actually wrote rows (PART 16) with the same directions

  PHASE C — REST path over HTTP (boots the real server)
      * GET /api/prediction/<asset> rows carry `candle` geometry
        anchored at the frozen close_i
      * GET /app serves common.js containing the model-ghost drawing code
        (setModelGhostCandles / drawModelGhostCandles / modelPredCandles)
        — the frontend half of the fix is deployed, not just the backend

Run: python3 scripts/e2e_future_candle.py   (QX_FAST_TRAIN=0 for determinism)
"""
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="future_candle_e2e_")
os.environ["DB_PATH"] = os.path.join(_TMP, "e2e.db")
os.environ["QX_PREDICT_MODELS_DIR"] = os.path.join(_TMP, "models")
os.environ["QX_FAST_TRAIN"] = "0"
PORT = 8841
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _synthetic_candles(n=320, seed=42, start=None, p0=1.1000):
    """Deterministic random-walk 1-minute candles (seeded)."""
    rng = random.Random(seed)
    t0 = int(start if start is not None else (time.time() // 60) * 60 - n * 60)
    out = []
    px = p0
    for i in range(n):
        o = px
        drift = rng.uniform(-1, 1) * 0.0008
        c = o + drift
        h = max(o, c) + abs(rng.gauss(0, 0.0004))
        l = min(o, c) - abs(rng.gauss(0, 0.0004))
        out.append({"time": t0 + i * 60, "open": round(o, 6),
                    "high": round(h, 6), "low": round(l, 6),
                    "close": round(c, 6)})
        px = c
    return out


# ═══════════════════ PHASE A — geometry unit checks ═══════════════════
print("── PHASE A: geometry module ──")
from core.otc_predict.geometry import future_candle, atr_from_candles  # noqa: E402

candles_a = _synthetic_candles(80)
atr_a = atr_from_candles(candles_a)
check("atr_from_candles positive on synthetic window", atr_a > 0, str(atr_a))
check("atr_from_candles survives empty window",
      atr_from_candles([]) == 0.0)
check("atr_from_candles degenerate flat window falls back > 0",
      atr_from_candles([{"open": 1.0, "high": 1.0, "low": 1.0,
                         "close": 1.0}]) > 0)

last_a = candles_a[-1]
c_up = future_candle(last_a["close"], atr_a, True, 0.62, last_a["time"] + 60)
c_dn = future_candle(last_a["close"], atr_a, False, 0.58, last_a["time"] + 60)
check("CALL candle close above open", c_up["close"] > c_up["open"])
check("PUT candle close below open", c_dn["close"] < c_dn["open"])
for nm, c in (("CALL", c_up), ("PUT", c_dn)):
    ok = (c["high"] >= max(c["open"], c["close"])
          and c["low"] <= min(c["open"], c["close"])
          and all(c[k] > 0 for k in ("open", "high", "low", "close"))
          and c["time"] > 0)
    check(f"{nm} candle internally consistent + positive", ok, str(c))

c_hi = future_candle(last_a["close"], atr_a, True, 0.90, last_a["time"] + 60)
c_lo = future_candle(last_a["close"], atr_a, True, 0.50, last_a["time"] + 60)
check("conviction scales the expected body (0.90 > 0.50)",
      abs(c_hi["close"] - c_hi["open"]) > abs(c_lo["close"] - c_lo["open"]))
# 50% coin-flip still draws a small honest body, never zero
check("coin-flip probability still draws a visible body",
      abs(c_lo["close"] - c_lo["open"]) > 0)

# ═══════════════════ PHASE B — live WS payload ═══════════════════
print("── PHASE B: predictor.on_candle_closed payload ──")
import db as _db                                                    # noqa: E402
_db.init()

from core.otc_predict import models as _models                     # noqa: E402
from core.otc_predict import predictor as _predictor               # noqa: E402
from core.otc_predict.tracker import register_model, insert_prediction  # noqa: E402

# Train a tiny real bundle on synthetic windows (leak-safe: past-only rows)
train_candles = _synthetic_candles(300, seed=7)
from core.otc_dataset import build_dataset                         # noqa: E402
from core.otc_predict.features_ext import (build_extended_row,     # noqa: E402
                                           EXTENDED_FEATURE_NAMES)
rows, stats = build_dataset({"TESTPAIR_otc": train_candles},
                            window=50, feature_fn=build_extended_row)
check("dataset rows built for training", len(rows) >= 100, str(len(rows)))

import numpy as _np                                                # noqa: E402
X = _np.array([[r[k] for k in EXTENDED_FEATURE_NAMES] for r in rows])
y1 = _np.array([1 if r["y1_up"] else 0 for r in rows])
y2 = _np.array([1 if r["y2_up"] else 0 for r in rows])
split = int(len(X) * 0.8)
m1 = _models.fit_candidate("logreg", X[:split], y1[:split])
m2 = _models.fit_candidate("logreg", X[:split], y2[:split])
coefs = _models.platt_calibrate(m1, X[split:], y1[split:])
bundle = _models.ModelBundle(
    "vfc-e2e-1", EXTENDED_FEATURE_NAMES,
    {"model": m1, "platt": coefs, "name": "logreg"},
    {"model": m2, "platt": coefs, "name": "logreg"},
    {"status": "provisional", "rows": len(rows)})
path = _models.save_bundle(bundle)
register_model(name="TESTPAIR_otc", version="vfc-e2e-1", scope="pair",
               asset="TESTPAIR_otc",
               metrics={"status": "provisional", "rows": len(rows)},
               path=path, activate=True)

# make the predictor use it immediately (bypass TTL)
_predictor._cache["reg"] = {"TESTPAIR_otc": {
    "name": "TESTPAIR_otc", "version": "vfc-e2e-1", "path": path}}
_predictor._cache["checked_at"] = time.time()
_predictor._cache["bundles"] = {}

# live window: 50 CLOSED candles ending exactly at the just-closed one
live_candles = _synthetic_candles(120, seed=11)
window = live_candles[-50:]
closed = window[-1]
micro = {"buy_pct": 55, "sell_pct": 45, "tick_count": 120, "is_fight": False}
payload = _predictor.on_candle_closed("TESTPAIR_otc", 60, window,
                                      closed, micro)
check("payload returned", payload is not None and payload.get("status") == "ok",
      str(payload and payload.get("status")))
t1 = (payload or {}).get("t1") or {}
t2 = (payload or {}).get("t2") or {}
check("t1.candle geometry present", isinstance(t1.get("candle"), dict),
      str(t1))
check("t2.candle geometry present", isinstance(t2.get("candle"), dict),
      str(t2))
check("t1 target time = closed + 60",
      t1.get("target_time") == closed["time"] + 60)
check("t2 target time = closed + 120",
      t2.get("target_time") == closed["time"] + 120)
check("t1.candle time matches target_time",
      t1.get("candle", {}).get("time") == t1.get("target_time"))
check("t2.candle time matches target_time",
      t2.get("candle", {}).get("time") == t2.get("target_time"))


def _dir_ok(slot):
    c = slot.get("candle") or {}
    if not c:
        return False
    if slot.get("prediction") == "CALL":
        return c["close"] > c["open"]
    if slot.get("prediction") == "PUT":
        return c["close"] < c["open"]
    return False


check("t1 candle direction matches frozen prediction", _dir_ok(t1), str(t1))
check("t2 candle direction matches frozen prediction", _dir_ok(t2), str(t2))
check("t1 carries its own quality gates (P6 fix)",
      isinstance(t1.get("quality"), dict)
      and "no_gap" in t1.get("quality", {}))
check("t2 carries its own quality gates (P6 fix)",
      isinstance(t2.get("quality"), dict)
      and "no_gap" in t2.get("quality", {}))
check("top-level quality mirrors t1 (backward compat)",
      payload.get("quality") == t1.get("quality"))

# candle anchored at the closed candle's close price
check("t1 candle opens at closed candle close",
      abs((t1.get("candle") or {}).get("open", 0) - closed["close"]) < 1e-6,
      f"{(t1.get('candle') or {}).get('open')} vs {closed['close']}")
check("t2 candle opens at closed candle close",
      abs((t2.get("candle") or {}).get("open", 0) - closed["close"]) < 1e-6)

# freeze actually happened (PART 16) — same directions in the DB
from core.otc_predict.tracker import latest_predictions             # noqa: E402
frozen = latest_predictions("TESTPAIR_otc", 4)
sig_rows = [r for r in frozen if r["signal_time"] == closed["time"]]
check("frozen rows written for this close", len(sig_rows) == 2,
      str(len(sig_rows)))
if len(sig_rows) == 2:
    dirs = {r["horizon"]: r["prediction"] for r in sig_rows}
    check("frozen t1 direction == payload t1",
          dirs.get(1) == t1.get("prediction"))
    check("frozen t2 direction == payload t2",
          dirs.get(2) == t2.get("prediction"))

# ═══════════════════ PHASE C — REST + served frontend ═══════════════════
print("── PHASE C: REST payload + served frontend ──")
env = dict(os.environ)
env.update({"PORT": str(PORT), "HEADLESS": "1",
            "AUTO_OPEN_BROWSER": "0", "QX_TOKEN": ""})
proc = subprocess.Popen(
    [sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    up = False
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/prediction/overview",
                                        timeout=2) as r:
                json.loads(r.read().decode())
            up = True
            break
        except Exception:
            time.sleep(0.5)
    check("server booted", up)

    with urllib.request.urlopen(
            BASE + "/api/prediction/TESTPAIR_otc", timeout=10) as r:
        card = json.loads(r.read().decode())
    cur = card.get("current") or []
    check("REST card current group has both horizons", len(cur) == 2,
          str(len(cur)))
    if len(cur) == 2:
        r1, r2 = cur[0], cur[1]
        check("REST t1 row carries candle geometry",
              isinstance(r1.get("candle"), dict), str(r1))
        check("REST t2 row carries candle geometry",
              isinstance(r2.get("candle"), dict), str(r2))
        # anchored at the frozen close_i (geometry base from the DB row)
        from core.otc_predict.tracker import latest_predictions as _lp
        fz = [x for x in _lp("TESTPAIR_otc", 4)
              if x["signal_time"] == closed["time"]]
        fz1 = next((x for x in fz if x["horizon"] == 1), None)
        if fz1:
            check("REST t1 candle anchored at frozen close_i",
                  abs(r1["candle"]["open"] - (fz1["close_i"] or 0)) < 1e-6,
                  f"{r1['candle']['open']} vs {fz1['close_i']}")
        check("REST t1 candle time == target_time",
              r1.get("candle", {}).get("time") == r1.get("target_time"))

    # the frontend half of the fix must be SERVED (not just backend)
    with urllib.request.urlopen(BASE + "/static/js/common.js",
                                timeout=10) as r:
        js = r.read().decode()
    for needle, label in (
            ("setModelGhostCandles", "common.js defines setModelGhostCandles"),
            ("drawModelGhostCandles", "common.js defines drawModelGhostCandles"),
            ("modelPredCandles", "common.js keeps modelPredCandles state"),
    ):
        check(label, needle in js)

    with urllib.request.urlopen(BASE + "/app", timeout=10) as r:
        html = r.read().decode()
    check("/app still serves the pred card", "pred-card" in html)

    # ═══════════ PHASE D — real browser chart drawing (Playwright) ═══════
    # Boots headless Chromium, loads /app, injects an exact 'otc_pred' WS
    # frame through the REAL handleMsg() path (window.__feedOtcPred), and
    # verifies the model's T+1/T+2 future candles actually reach the
    # chart's ghost series + the card hint appears. This closes the loop
    # the user complained about — models showing future candles ON CHART.
    try:
        from playwright.sync_api import sync_playwright
        _PW_OK = True
    except Exception:
        _PW_OK = False
        print("  [SKIP] playwright not installed — browser phase skipped")
    if _PW_OK:
        print("── PHASE D: real browser chart drawing ──")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(BASE + "/app", wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_function(
                    "window.__modelPred && window.__feedOtcPred",
                    timeout=15000)
            except Exception:
                pass
            ready = page.evaluate(
                "() => !!(window.__modelPred && window.__feedOtcPred)")
            check("browser app booted with debug hooks", ready)
            if ready:
                # /app boots the 'otc' category → currentAsset = EURUSD_otc.
                # onOtcPred drops frames for other pairs (defensive filter),
                # so the injected frame must carry THAT asset — the geometry
                # (t1/t2 candle OHLC from PHASE B) is what we verify.
                cur_asset = page.evaluate("() => window.__modelPred().currentAsset")
                check("browser booted on a pair (otc default)",
                      bool(cur_asset), str(cur_asset))
                frame = {
                    "asset": cur_asset,
                    "period": 60,
                    "status": "ok",
                    "model_version": "vfc-e2e-1",
                    "model_status": "provisional",
                    "signal_time": closed["time"],
                    "locked": True,
                    "t1": t1, "t2": t2,
                }
                page.evaluate("fr => window.__feedOtcPred(fr)", frame)
                st = page.evaluate("() => window.__modelPred()")
                check("browser stored model ghost candles",
                      st["asset"] == cur_asset and len(st["candles"]) == 2,
                      json.dumps(st))
                check("browser ghost candles carry the payload geometry",
                      len(st["candles"]) == 2
                      and st["candles"][0]["time"] == t1["target_time"]
                      and st["candles"][1]["time"] == t2["target_time"])
                check("ghost series actually painted (drawn payload)",
                      len(st["drawn"]) == 2, json.dumps(st.get("drawn")))
                if len(st["drawn"]) == 2:
                    d1, d2 = st["drawn"]
                    check("drawn T+1 keeps its direction",
                          (d1["close"] > d1["open"]) ==
                          (t1["prediction"] == "CALL"))
                    check("drawn T+2 keeps its direction",
                          (d2["close"] > d2["open"]) ==
                          (t2["prediction"] == "CALL"))
                # the card must tell the user the candles are on the chart
                card_txt = page.evaluate(
                    "() => document.getElementById('pred-card') ?"
                    " document.getElementById('pred-card').textContent : ''")
                check("pred card shows the chart hint",
                      "ফিউচার ক্যান্ডেল" in card_txt and "চার্টে" in card_txt,
                      card_txt[:120])
                check("pred card shows NEXT CANDLE row", "NEXT CANDLE" in card_txt)
                check("no page errors during drawing", not errors,
                      str(errors[:3]))
            browser.close()

    print(f"\n══ E2E RESULT: {PASS} PASS, {FAIL} FAIL ══")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
