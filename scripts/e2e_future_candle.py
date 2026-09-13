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
# EDGE-GUARD (2026-09-13): a PROVISIONAL model is display-only — no
# emission, NO ghost candle. The old test asserted candles here; the new
# honesty contract is candle=None unless the slot actually emits.
check("provisional t1 emits nothing", t1.get("emit") is False, str(t1.get("emit")))
check("provisional t2 emits nothing", t2.get("emit") is False, str(t2.get("emit")))
check("provisional t1 carries NO ghost candle",
      t1.get("candle") is None, str(t1.get("candle")))
check("provisional t2 carries NO ghost candle",
      t2.get("candle") is None, str(t2.get("candle")))
check("provisional reason names the edge gate",
      "model_not_verified" in (t1.get("reason") or ""), str(t1.get("reason")))
check("t2 reason names the disabled second horizon",
      "t2_emit_disabled" in (t2.get("reason") or ""), str(t2.get("reason")))
check("t1 target time = closed + 60",
      t1.get("target_time") == closed["time"] + 60)
check("t2 target time = closed + 120",
      t2.get("target_time") == closed["time"] + 120)
check("t1 target time still broadcast (card countdown)",
      isinstance(t1.get("target_time"), int))


def _dir_ok(slot):
    c = slot.get("candle") or {}
    if not c:
        return False
    if slot.get("prediction") == "CALL":
        return c["close"] > c["open"]
    if slot.get("prediction") == "PUT":
        return c["close"] < c["open"]
    return False
check("t1 carries its own quality gates (P6 fix)",
      isinstance(t1.get("quality"), dict)
      and "no_gap" in t1.get("quality", {}))
check("t2 carries its own quality gates (P6 fix)",
      isinstance(t2.get("quality"), dict)
      and "no_gap" in t2.get("quality", {}))
check("top-level quality mirrors t1 (backward compat)",
      payload.get("quality") == t1.get("quality"))

# ── PHASE B2 — VERIFIED bundle + full-agreement voices → t1 EMITS ────
print("── PHASE B2: verified model, voices agree — emitted ghost candle ──")
# NOTE: the generator's default t0 depends on n — generating 121 candles
# with the default start would land closed2 at the SAME minute as closed
# (key collision on the freeze table, INSERT OR IGNORE silently keeps the
# old provisional rows). Anchor the second series to the first one's grid
# so closed2 is exactly one minute after closed.
live_candles2 = _synthetic_candles(121, seed=11,
                                    start=live_candles[0]["time"] + 60)
window2 = live_candles2[-50:]
closed2 = window2[-1]
_verified = _models.ModelBundle(
    "vfc-e2e-2", EXTENDED_FEATURE_NAMES,
    {"model": m1, "platt": coefs, "name": "logreg"},
    {"model": m2, "platt": coefs, "name": "logreg"},
    {"status": "verified", "rows": len(rows)})
_verified.predict_up = lambda horizon, feat_row: 0.90
_predictor._cache["reg"] = {"TESTPAIR_otc": {
    "name": "TESTPAIR_otc", "version": "vfc-e2e-2", "path": path}}
_predictor._cache["bundles"] = {"TESTPAIR_otc:vfc-e2e-2": _verified}
_predictor._cache["checked_at"] = time.time()

_orig_sv = _predictor.strategy_votes
_orig_ll = _predictor.live_lookup
_orig_pa = _predictor.price_action_confirm
_orig_dr = _predictor.detect_regime
_predictor.strategy_votes = lambda w, ticks=None: ({}, {
    "direction": "CALL", "net": 1.0, "agree_count": 5,
    "against_count": 0, "voters": 5})
_predictor.live_lookup = lambda a, p, c: {
    "p_up_t1": 0.9, "n_t1": 500, "level_t1": "L2",
    "p_up_t2": 0.9, "n_t2": 500, "level_t2": "L2"}
_predictor.price_action_confirm = lambda w, d, features=None, regime=None: {
    "pa_score": 1.0, "agreed": True, "against_count": 0, "veto": None,
    "components": {"trend": 1.0, "momentum": 1.0, "level": 1.0,
                   "rejection": 1.0, "structure": 1.0}}
_predictor.detect_regime = lambda w: {
    "regime": "RANGING", "trend_score": 0.1, "vol_state": "normal",
    "extreme_vol": False}
try:
    payload2 = _predictor.on_candle_closed("TESTPAIR_otc", 60, window2,
                                           closed2, micro)
finally:
    _predictor.strategy_votes = _orig_sv
    _predictor.live_lookup = _orig_ll
    _predictor.price_action_confirm = _orig_pa
    _predictor.detect_regime = _orig_dr

v1 = (payload2 or {}).get("t1") or {}
v2 = (payload2 or {}).get("t2") or {}
check("verified+agreed t1 EMITS", v1.get("emit") is True, str(v1.get("emit")))
check("emitted t1 carries ghost candle geometry",
      isinstance(v1.get("candle"), dict), str(v1.get("candle")))
check("emitted t1 candle direction matches prediction (CALL)",
      _dir_ok(v1), str(v1))
check("emitted t1 candle opens at closed candle close",
      abs((v1.get("candle") or {}).get("open", 0) - closed2["close"]) < 1e-6)
check("emitted t1 candle time matches target_time",
      v1.get("candle", {}).get("time") == v1.get("target_time"))
check("t2 still display-only (second horizon disabled)",
      v2.get("emit") is False and v2.get("candle") is None, str(v2.get("emit")))
# freeze actually happened (PART 16) — same directions in the DB
from core.otc_predict.tracker import latest_predictions             # noqa: E402
frozen2 = latest_predictions("TESTPAIR_otc", 6)
sig2 = [r for r in frozen2 if r["signal_time"] == closed2["time"]]
emit_rows = [r for r in sig2 if r["emit"]]
check("emitted row frozen with emit=1", len(emit_rows) == 1,
      str([(r['horizon'], r['emit']) for r in sig2]))

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
    # The newest frozen snapshot is the Phase-B2 one: t1 emitted (candle
    # present), t2 display-only (candle None) — the REST path must mirror
    # the WS payload exactly (EDGE-GUARD parity).
    if len(cur) == 2:
        r1, r2 = cur[0], cur[1]
        check("REST emitted t1 row carries candle geometry",
              r1.get("emit") and isinstance(r1.get("candle"), dict),
              str(r1.get("emit")) + " " + str(r1.get("candle")))
        check("REST non-emitted t2 row carries NO candle",
              (not r2.get("emit")) and r2.get("candle") is None,
              str(r2.get("emit")) + " " + str(r2.get("candle")))
        if r1.get("candle"):
            from core.otc_predict.tracker import latest_predictions as _lp
            fz = [x for x in _lp("TESTPAIR_otc", 8)
                  if x["signal_time"] == r1.get("signal_time")]
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
                # so the injected frame must carry THAT asset.
                # EDGE-GUARD contract: t1 = EMITTED slot WITH geometry (must
                # draw), t2 = display-only (must NOT draw) — and a third
                # rogue slot (emit=false but geometry present) verifies the
                # frontend refuses non-emit geometry even if a bug sends it.
                cur_asset = page.evaluate("() => window.__modelPred().currentAsset")
                check("browser booted on a pair (otc default)",
                      bool(cur_asset), str(cur_asset))
                from core.otc_predict.geometry import future_candle as _fc
                from core.otc_predict.geometry import atr_from_candles as _atr
                _a = _atr(window2)
                emit_candle = _fc(closed2["close"], _a, True, 0.9,
                                  closed2["time"] + 60)
                rogue_candle = _fc(closed2["close"], _a, False, 0.55,
                                   closed2["time"] + 120)
                frame = {
                    "asset": cur_asset,
                    "period": 60,
                    "status": "ok",
                    "model_version": "vfc-e2e-2",
                    "model_status": "verified",
                    "signal_time": closed2["time"],
                    "locked": True,
                    "t1": {"target_time": closed2["time"] + 60,
                           "prediction": "CALL", "probability": 0.9,
                           "tier": "HIGH", "score": 92, "emit": True,
                           "reason": "tier:HIGH",
                           "candle": emit_candle},
                    "t2": {"target_time": closed2["time"] + 120,
                           "prediction": "PUT", "probability": 0.55,
                           "tier": "WATCH", "score": 62, "emit": False,
                           "reason": "edge_guard:t2_emit_disabled",
                           "candle": None},
                }
                page.evaluate("fr => window.__feedOtcPred(fr)", frame)
                st = page.evaluate("() => window.__modelPred()")
                check("browser stored ONLY the emitted ghost candle",
                      st["asset"] == cur_asset and len(st["candles"]) == 1,
                      json.dumps(st))
                if len(st["candles"]) == 1:
                    check("stored ghost carries the emitted geometry",
                          st["candles"][0]["time"] == closed2["time"] + 60)
                check("ghost series painted exactly the emitted candle",
                      len(st["drawn"]) == 1
                      and st["drawn"][0]["time"] == closed2["time"] + 60,
                      json.dumps(st.get("drawn")))
                if len(st["drawn"]) == 1:
                    d1 = st["drawn"][0]
                    check("drawn T+1 keeps its direction (CALL up)",
                          d1["close"] > d1["open"])
                # rogue frame: emit=false + geometry — frontend must refuse
                rogue_frame = {
                    "asset": cur_asset, "period": 60, "status": "ok",
                    "model_version": "vfc-e2e-2",
                    "model_status": "verified",
                    "signal_time": closed2["time"],
                    "locked": True,
                    "t1": {"target_time": closed2["time"] + 60,
                           "prediction": "PUT", "probability": 0.55,
                           "tier": "WATCH", "score": 62, "emit": False,
                           "reason": "edge_guard:prob_below_band",
                           "candle": rogue_candle},
                    "t2": None,
                }
                page.evaluate("fr => window.__feedOtcPred(fr)", rogue_frame)
                st2 = page.evaluate("() => window.__modelPred()")
                check("frontend REFUSES non-emit geometry (rogue slot)",
                      len(st2["candles"]) == 0 and len(st2["drawn"]) == 0,
                      json.dumps({"candles": st2["candles"],
                                  "drawn": st2["drawn"]}))
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
