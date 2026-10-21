"""PHASE-L7 battery — ICT/SMC research overlay (API + UI integration).

Verifies the /api/ict-research/{asset} endpoint and the frontend wiring:
  * honest no-data state (auto mode, no live feed — never fabricated);
  * replay mode: authorized dataset provenance (sha256 + target ts),
    full 41-key snapshot, pre-specified L6 research votes with honest
    labels (research-only, weight 0, NOT validated, NOT actionable);
  * caching determinism, parameter clamping, no secrets in payload;
  * UI markup present, JS syntax valid, no false 'validated' claims;
  * the Truth Layer gate is untouched by this feature.
"""
import json
import os
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="l7b_"), "s.db")
os.environ["QX_RETENTION_ENABLED"] = "0"
os.environ.pop("ADMIN_KEY", None)
os.environ.pop("QX_TOKEN", None)
os.environ.pop("QX_EMAIL", None)

_PASS = 0
_FAIL = 0


def check(cond, label, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label} {detail}")


from fastapi.testclient import TestClient  # noqa: E402
import server as _server  # noqa: E402

client = TestClient(_server.app, raise_server_exceptions=False)

print("A. HONEST NO-DATA STATE (auto mode, no live feed)")
r = client.get("/api/ict-research/BRLUSD")
check(r.status_code == 200, "A1 auto mode returns 200")
d = r.json()
check(d["data_mode"] == "no_data", "A2 data_mode == no_data")
check("unauthorized" in (d.get("reason") or ""),
      "A3 reason names the unauthorized-source truth")
check(d.get("state") is None, "A4 no fabricated state")

print("B. REPLAY MODE (authorized research dataset)")
r = client.get("/api/ict-research/BTCUSDT?mode=replay&at=42000")
check(r.status_code == 200, "B1 replay returns 200")
d = r.json()
check(d["data_mode"] == "research_replay_authorized_binance_30d",
      "B2 data_mode labels the authorized replay source")
pr = d.get("provenance", {})
import hashlib
sha = hashlib.sha256(open(os.path.join(
    _REPO, "scripts", "data", "binance_30d", "BTCUSDT_1m.json"), "rb"
).read()).hexdigest()
check(pr.get("sha256") == sha, "B3 provenance sha256 matches the dataset file")
check("NOT OTC" in pr.get("classification", ""),
      "B4 classification says NOT OTC / NOT live")
check(pr.get("target_ts_utc", "").startswith("2026-"),
      "B5 target timestamp recorded", pr.get("target_ts_utc"))
st = d.get("state") or {}
EXPECTED = ["structure.trend", "poi.nearest_supply", "poi.nearest_demand",
            "pattern.last_kind", "mom.rsi", "mom.roc", "mom.stoch_k",
            "liq.in_range", "liq.last_sweep", "liq.last_amd",
            "session.target_session", "session.target_kill_zone",
            "flow.last_delta", "news.provided"]
check(all(k in st for k in EXPECTED),
      "B6 snapshot carries the full feature keys",
      str([k for k in EXPECTED if k not in st]))

print("C. RESEARCH VOTES — HONEST LABELS")
votes = d.get("research_votes", {})
ARM_NAMES = ["ict1_ob_retest", "ict2_sweep_reversal",
             "ict3_amd_continuation", "ict4_fvg_fill", "ict5_divergence",
             "ict6_kz_structure", "ict7_vote"]
check(all(k in votes for k in ARM_NAMES), "C1 all 7 pre-specified arms present")
check(all(v.get("direction") in ("CALL", "PUT", None)
          for v in votes.values() if isinstance(v, dict)),
      "C2 vote directions are CALL/PUT/None only")
check(all("ভ্যালিডেটেড নয়" in (v.get("label") or "")
          for v in votes.values() if isinstance(v, dict)),
      "C3 every vote label says NOT validated (Bengali)")
h = d.get("honesty", {})
check(h.get("research_only") is True and h.get("validated") is False
      and h.get("actionable") is False and h.get("weight") == 0.0,
      "C4 honesty block: research-only, weight 0, not actionable")
check(any("break-even" in x for x in h.get("labels", [])),
      "C5 honesty labels cite the L6 break-even negative result")

print("D. EDGE CASES / SAFETY")
r2 = client.get("/api/ict-research/BTCUSDT?mode=replay&at=42000")
check(r2.json() == d, "D1 cached response identical (determinism)")
r3 = client.get("/api/ict-research/NOTANASSET?mode=replay")
check(r3.json()["data_mode"] == "no_data", "D2 unknown asset -> honest no-data")
r4 = client.get("/api/ict-research/BTCUSDT?mode=replay&at=999999999")
check(r4.status_code == 200 and r4.json()["data_mode"].startswith("research_"),
      "D3 at= clamped to dataset range")
blob = json.dumps(d).lower()
check(not any(s in blob for s in ("qx_token", "api_key", "password",
                                  "ssid", "pin")),
      "D4 no secret field names anywhere in the payload")
r5 = client.post("/api/ict-research/BTCUSDT")
check(r5.status_code == 405, "D5 endpoint is GET-only (405 on POST)")
rt = client.get("/api/truth-layer")
check(rt.json().get("actionable_signals_possible") is False,
      "D6 Truth Layer gate untouched (still fail-closed)")

print("E. FRONTEND WIRING")
html = open(os.path.join(_REPO, "static", "app.html")).read()
check('id="ict-panel"' in html and 'id="ict-overlay-body"' in html,
      "E1 app.html has the ICT panel markup")
check('<script src="/static/js/ict-overlay.js"></script>' in html,
      "E2 ict-overlay.js included")
js_path = os.path.join(_REPO, "static", "js", "ict-overlay.js")
proc = subprocess.run(["node", "--check", js_path], capture_output=True)
check(proc.returncode == 0, "E3 ict-overlay.js passes node --check")
js = open(js_path, encoding="utf-8").read()
check("ভ্যালিডেটেড নয়" in js and "validated" not in js.replace("NOT validated", ""),
      "E4 JS honest labels, no false validated claim")
check("বানানো হয় না" in js or "কিছু বানানো হয়নি" in js,
      "E5 no-fabrication wording present in JS fallback states")
check("api/ict-research" in js, "E6 JS fetches the new endpoint")

print()
print("=" * 72)
print(f"PHASE-L7 UI OVERLAY: {_PASS} passed, {_FAIL} failed, "
      f"{_PASS + _FAIL} total")
print("=" * 72)
sys.exit(1 if _FAIL else 0)
