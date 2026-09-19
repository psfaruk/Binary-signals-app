"""MODEL-STATUS-STRIP visual verification (USER-2026-09-19 "মডেল কি চলে?").

Boots the real server against a seeded temp DB replicating EXACTLY the
production state the user screenshotted:
  - provisional model registered for USDBDT_otc (REAL loadable bundle)
  - frozen T+1/T+2 rows, emit=False, reason=edge_guard:model_not_verified
Then checks (HTTP + DOM assertions) that:
  1. REST card rows now carry `reason` (server.py fix — reload-proof)
  2. engine reports model_version + provisional status
  3. the pred card shows the "মডেল চলছে" engine strip + wait note
  4. NO TRADE reason says মডেল চলছে ✓ (not "not trained")
  5. footer says "ML সিগন্যাল কোয়ালিটি" + "প্রেডিকশন সিলড"
Screenshots the card at 390px and desktop for eyeball confirmation.
"""
import json
import os
import sys
import tempfile
import time
import urllib.request

REPO = "/home/z/my-project/Binary-signals-app"
sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="mstat_e2e_")
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


import numpy as np  # noqa: E402
import db as _db  # noqa: E402
_db.init()
from core.otc_predict.tracker import insert_prediction, register_model  # noqa: E402
from core.otc_predict.fast_train import UNIFIED_FEATURE_NAMES  # noqa: E402
from core.otc_predict.models import ModelBundle, save_bundle  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402

now = int(time.time())
sig_t = now - 120
# REAL loadable bundle → engine.model_version resolves like production
_X = np.random.rand(200, len(UNIFIED_FEATURE_NAMES))
_y = (np.random.rand(200) > 0.5).astype(int)
_rf = RandomForestClassifier(n_estimators=8, random_state=0).fit(_X, _y)
_slot = {"model": _rf, "platt": None, "name": "rf"}
_b = ModelBundle("v20260919-0733", UNIFIED_FEATURE_NAMES, _slot, dict(_slot),
                 {"status": "provisional", "trained_rows": 1428,
                  "trainer": "fast-unified"})
_path = save_bundle(_b, os.path.join(_TMP, "v933.joblib"))
register_model(name="USDBDT_otc", version="v20260919-0733", scope="pair",
               asset="USDBDT_otc",
               metrics={"status": "provisional", "rows": 1428},
               path=_path, activate=True)
# exactly the frozen production shape: NO TRADE because model_not_verified
insert_prediction(asset="USDBDT_otc", period=60, signal_time=sig_t,
                  target_time=sig_t + 60, horizon=1, prediction="CALL",
                  probability=0.5031, tier="NO_SIGNAL", score=44, emit=False,
                  reason="edge_guard:model_not_verified",
                  model_version="v20260919-0733")
insert_prediction(asset="USDBDT_otc", period=60, signal_time=sig_t,
                  target_time=sig_t + 120, horizon=2, prediction="CALL",
                  probability=0.5288, tier="NO_SIGNAL", score=46, emit=False,
                  reason="edge_guard:model_not_verified",
                  model_version="v20260919-0733")

import subprocess  # noqa: E402
env = dict(os.environ)
env.update({"PORT": str(PORT), "HEADLESS": "1",
            "AUTO_OPEN_BROWSER": "0", "QX_TOKEN": ""})
proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")],
                         cwd=REPO, env=env,
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

    with urllib.request.urlopen(BASE + "/api/prediction/USDBDT_otc",
                                timeout=5) as r:
        card = json.loads(r.read().decode())
    eng = card.get("engine") or {}
    cur = card.get("current") or []
    check("engine reports model_version (real bundle loads)",
          eng.get("model_version") == "v20260919-0733", str(eng.get("model_version")))
    check("engine reports provisional", eng.get("model_status") == "provisional")
    check("REST rows now carry reason (reload-proof fix)",
          len(cur) == 2 and all("model_not_verified" in (e.get("reason") or "")
                                for e in cur),
          str([e.get("reason") for e in cur]))

    # ── DOM-level render assertions (real page, real CSS) ───────────────
    import asyncio

    async def dom_checks():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            for w, h, tag in ((390, 844, "mobile390"), (1280, 900, "desktop")):
                page = await browser.new_page(viewport={"width": w, "height": h})
                await page.goto(BASE + "/app", wait_until="networkidle")
                await page.evaluate("""() => {
                    const sel = document.getElementById('pair-select');
                    if(sel){
                        for(const o of sel.options){
                            if(o.value === 'USDBDT_otc'){ sel.value = o.value;
                                sel.dispatchEvent(new Event('change')); break; }
                        }
                    }
                }""")
                await page.wait_for_timeout(3500)  # REST card fetch
                html = await page.evaluate(
                    "() => (document.getElementById('pred-card')||{}).innerHTML || ''")
                check(f"[{tag}] engine strip says মডেল চলছে",
                      "মডেল চলছে" in html and "pred-engine-status" in html)
                check(f"[{tag}] strip carries provisional wait note",
                      "ভেরিফায়েড নয়" in html and "সিগন্যাল বন্ধ (নিরাপত্তা)" in html)
                check(f"[{tag}] NO TRADE reason says মডেল চলছে ✓",
                      "মডেল চলছে ✓" in html)
                check(f"[{tag}] old ambiguous text gone",
                      "verified না হওয়া পর্যন্ত সিগন্যাল নেই" not in html)
                check(f"[{tag}] footer is ML সিগন্যাল কোয়ালিটি",
                      "ML সিগন্যাল কোয়ালিটি" in html)
                check(f"[{tag}] footer says প্রেডিকশন সিলড (not লক)",
                      "প্রেডিকশন সিলড" in html and "প্রেডিকশন লক" not in html)
                check(f"[{tag}] version chip rendered",
                      "v20260919-0733" in html)
                if tag == "mobile390":
                    ow = await page.evaluate(
                        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    check("[mobile390] zero horizontal overflow", ow <= 1, f"ow={ow}")
                el = await page.query_selector("#pred-card")
                if el:
                    await el.screenshot(path=f"/home/z/my-project/scripts/mstat_{tag}.png")
                await page.close()
            await browser.close()

    asyncio.run(dom_checks())
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print(f"\n══ VISUAL E2E RESULT: {PASS} PASS, {FAIL} FAIL ══")
sys.exit(1 if FAIL else 0)
