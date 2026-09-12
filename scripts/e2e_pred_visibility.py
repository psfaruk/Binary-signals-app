#!/usr/bin/env python3
"""scripts/e2e_pred_visibility.py — PRED-VISIBILITY end-to-end (2026-09-12).

Boots the REAL server against a seeded temp DB (registry rows + frozen
T+1/T+2 predictions, one emitted+graded, one tracked-only) and verifies
over HTTP that the user's question "প্রেডিকশন T+1/T+2 কোথায় দেখানো হচ্ছে?
কোন পেয়ার এ প্রেডিকশন দিচ্ছে?" is now answerable:

  1. GET /api/prediction/overview → `live` array lists the active pair
     with its frozen T+1/T+2 slots (direction/probability/graded result).
  2. GET /api/prediction/<asset> → engine.registered_assets names the
     pairs WITH models; engine.has_predictions flags frozen rows.
  3. GET /app serves the মডেল tab's "সব পেয়ারের লাইভ প্রেডিকশন" table
     (mdl-live-tbody) and the panel JS loads (200).

Run: python3 scripts/e2e_pred_visibility.py   (no token needed — the
fast-train daemon is disabled via QX_FAST_TRAIN=0 for determinism)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="pred_vis_e2e_")
os.environ["DB_PATH"] = os.path.join(_TMP, "e2e.db")
os.environ["QX_PREDICT_MODELS_DIR"] = os.path.join(_TMP, "models")
os.environ["QX_FAST_TRAIN"] = "0"
PORT = 8831
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


import db as _db                                        # noqa: E402
_db.init()
from core.otc_predict.tracker import (insert_prediction, settle_target,  # noqa: E402
                                      register_model, live_predictions)
now = int(time.time())
sig_t = now - 120
# pair model with frozen + graded predictions (exactly the live shape)
register_model(name="USDBDT_otc", version="ve2e1", scope="pair",
               asset="USDBDT_otc",
               metrics={"status": "provisional", "rows": 2800},
               path="/nonexistent/ve2e1.joblib", activate=True)
insert_prediction(asset="USDBDT_otc", period=60, signal_time=sig_t,
                  target_time=sig_t + 60, horizon=1, prediction="CALL",
                  probability=0.71, tier="GOOD", score=72, emit=True,
                  model_version="ve2e1")
insert_prediction(asset="USDBDT_otc", period=60, signal_time=sig_t,
                  target_time=sig_t + 120, horizon=2, prediction="PUT",
                  probability=0.58, tier="WATCH", score=61, emit=False,
                  model_version="ve2e1")
settle_target("USDBDT_otc", 60, sig_t + 60, open_=1.0, close_=1.0021)
live = live_predictions()
check("in-process live_predictions lists the pair with t1/t2",
      any(e["asset"] == "USDBDT_otc" and e["t1"] and e["t2"]
          for e in live))

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

    with urllib.request.urlopen(BASE + "/api/prediction/overview",
                                timeout=10) as r:
        ov = json.loads(r.read().decode())
    lv = ov.get("live")
    check("overview.live is an array", isinstance(lv, list),
          str(type(lv)))
    ent = next((e for e in lv if e.get("asset") == "USDBDT_otc"), None)
    check("overview.live lists USDBDT_otc with frozen t1",
          ent is not None and ent["t1"] is not None
          and ent["t1"]["prediction"] == "CALL"
          and ent["t1"]["win_loss"] == "win")
    check("overview.live carries t2 tracked-only slot",
          ent is not None and ent["t2"] is not None
          and ent["t2"]["emit"] is False
          and ent["t2"]["win_loss"] is None)
    check("overview.live carries model status",
          ent is not None and ent["model_version"] == "ve2e1"
          and ent["model_status"] == "provisional")

    with urllib.request.urlopen(
            BASE + "/api/prediction/USDBDT_otc", timeout=10) as r:
        card = json.loads(r.read().decode())
    eng = card.get("engine") or {}
    check("card engine names registered assets",
          "USDBDT_otc" in (eng.get("registered_assets") or []))
    check("card engine has_predictions=True",
          eng.get("has_predictions") is True)
    check("card current group has both horizons",
          len(card.get("current") or []) == 2)

    with urllib.request.urlopen(BASE + "/app", timeout=10) as r:
        html = r.read().decode()
    check("/app serves the all-pairs live prediction table",
          "mdl-live-tbody" in html and "সব পেয়ারের লাইভ প্রেডিকশন" in html)
    with urllib.request.urlopen(BASE + "/static/js/models-panel.js",
                                timeout=10) as r:
        check("models-panel.js served", r.status == 200)

    print(f"\n══ E2E RESULT: {PASS} PASS, {FAIL} FAIL ══")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
