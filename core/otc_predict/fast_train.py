"""core/otc_predict/fast_train.py — FAST BOOTSTRAP TRAINER (2026-09-12).

USER REQ (verbatim): "Model রান করার জন্য 14 দিন অপেক্ষা করতে হবে কেনো?
প্রত্যেক পেয়ার এ যেই কয়েকটি ক্যান্ডেল লাগে, সেই কয়েকটি হিস্টোরি ডেটা সহ
লাইভ ক্যান্ডেল আপডেট হবে, এবং খুব অল্প সময়ের মধ্যে মডেল ট্রেইন হবে, রান
হবে। 5/7 মিনিটের মধ্যে। এই জন্য হার্ড কোড ব্যবহার করেন।"

ANSWER — a boot-time daemon that does NOT wait 14 days of accumulation:

  1. TOP-UP  every OTC pair whose candle_micro history is shorter than the
     few candles the model actually needs gets the missing days pulled from
     the SAME Quotex platform (PART 1 same-source rule) with the LIVE
     session token, inserted with INSERT OR IGNORE (existing live rows —
     including their microstructure — are never overwritten).
  2. TRAIN   per-pair T+1/T+2 bundle via the same leak-proof walk-forward
     used by the full trainer (expanding folds, EMBARGO, Platt tail,
     shuffle probe) — just with a smaller, faster configuration.
  3. REGISTER immediately:
       VERIFIED     — beats every baseline by ≥1.5pp on unseen folds AND
                      logloss < ln(2) AND shuffle probe clean.
       PROVISIONAL  — shuffle probe clean but the edge is not proven yet:
                      the model RUNS (predictions display + are graded),
                      the PART-14 score tiers still guard emission, and the
                      UI badge shows "প্রোভিশনাল".
       (a model with a leakage signature is NEVER registered.)
  4. RE-TRAIN on the growing dataset every FAST_RETRAIN_SECS — live candles
     keep accumulating in candle_micro, so bundles improve on their own
     with zero downtime.

ALL knobs are HARDCODED below (user's explicit ask). The only kill switch
is QX_FAST_TRAIN=0.
"""

import asyncio               # MODEL-RUN-FIX-2: was only imported inside
                             # _fetch_pairs() — the module-level _fetch_batch()
                             # raised "NameError: name 'asyncio' is not
                             # defined" on EVERY pair → +0 candles forever
import math
import os
import random
import sqlite3
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.constants import ALLOWED_PAIRS_OTC
from core.otc_dataset import build_dataset, load_candles_from_db
from core.otc_predict.features_ext import (build_extended_row,
                                           EXTENDED_FEATURE_NAMES)
from core.otc_predict import models as _models
from core.otc_predict.models import (CANDIDATES, fit_candidate,
                                     platt_calibrate, ModelBundle,
                                     save_bundle)
from core.otc_predict.walk_forward import _folds, _fit_predict_fold, EMBARGO

__all__ = ["run_bootstrap", "bootstrap_status", "start_daemon",
           "fast_train_one", "ensure_history", "pair_states"]

# ── HARDCODED CONFIG (user req: "এই জন্য হার্ড কোড ব্যবহার করেন") ──────────
WINDOW = 50                     # PART 7 context window (spec: 20-50)
FAST_DAYS = 2                   # max history depth fetched per short pair
                                # (2d ≈ 2880 candles > the 2500 floor → the
                                # cold-start fetch finishes ~1/3 faster than
                                # the old 3-day pull; 10-min retry self-heals
                                # any short fetch)
FAST_MIN_CANDLES = 2500         # pair with ≥ this many micro candles skips fetch
                                # (fetch TARGET only — training NEVER blocks
                                # on it any more, see FAST_MIN_PAIR_ROWS)

# MODEL-RUN-FIX-2 (2026-09-12): the user waited 10 runs × 10 min with ZERO
# models because the old floor (2000 rows) can NEVER be met by live candles
# alone (~90-130 rows) while the asyncio bug killed every fetch. New
# hardcoded floors — the SAME leak-proof walk-forward now runs on the small
# live dataset the moment the server boots, exactly the user's ask:
# "কয়েকটি ক্যান্ডেল দিয়েই ৫-৭ মিনিটের মধ্যে মডেল ট্রেইন হবে, রান হবে".
# The gates (expanding folds, EMBARGO, shuffle probe, honest statuses) are
# untouched — only the VOLUME requirements moved into the small-data regime.
FAST_MIN_PAIR_ROWS = 80         # was 2000 — 80 rows ≈ 130+ live candles;
                                # below this even one honest walk-forward
                                # fold cannot exist
FAST_MAX_ROWS_PER_PAIR = 12000  # newest-rows cap — retrain stays bounded
FAST_POOL_MIN_ROWS = 400        # was 8000 — pooled global fallback now
                                # reachable from ~11 pairs × ~40+ rows
FAST_POOL_MAX_ROWS = 40000      # pooled cap — the global fit stays bounded
FAST_CANDIDATES = ("logreg", "rf")   # histgb off — speed (PART 10 family kept)
FAST_N_FOLDS = 3                # expanding walk-forward folds (auto-trims to
                                # 2 honest folds on small data, see
                                # _fast_folds below)
FAST_MIN_TEST_PRED = 40         # was 150 — pooled unseen predictions per
                                # horizon; 40 is the smallest sample that
                                # still makes the smoke gate meaningful
FAST_SMALL_N = 550              # below this row count the shared _folds()
                                # emits INVALID cuts (train_end > n) and
                                # _fit_predict_fold fits on an EMPTY train
                                # set — _fast_folds() handles this regime
FAST_SHUFFLE_MAX = 0.53         # HARD gate — leakage signature = never register
FAST_BASELINE_MARGIN_PP = 1.5   # VERIFIED bar (PROVISIONAL sits below it)
FAST_VAL_FRAC = 0.2             # Platt calibration tail
FAST_RETRAIN_SECS = 6 * 3600    # refresh bundles twice a day AFTER a good run
FAST_RETRY_SECS = 600           # MODEL-RUN-FIX: a run that registered NOTHING
                                # (fetch failure / short data / transient
                                # error) retries in 10 minutes instead of
                                # silently sleeping 6 hours — the user must
                                # never wait blind again
FAST_BOOT_DELAY_SECS = 20       # let the server/feed settle first
FAST_FETCH_BATCH = 2            # pairs per platform connect (kind batch)
FAST_FETCH_TIMEOUT = 300        # per-pair get_historical_candles timeout
FAST_SLEEP_BETWEEN = 2.0        # pause between fetch batches

_state = {
    "running": False, "runs": 0, "last_run": 0.0, "last_result": None,
    "last_error": None, "thread_started": False, "started_at": time.time(),
    # MODEL-RUN-FIX: persistent per-pair state so the UI can show — at any
    # moment, even between runs — exactly which model trained how much and
    # why a pair is still waiting (fetch error / too few rows / rejected…).
    "pairs": {},
    "next_run_at": 0.0,   # epoch the daemon will next attempt a run
}
_lock = threading.Lock()        # guards CONCURRENT RUNS (non-reentrant)
_pair_lock = threading.Lock()   # guards _state["pairs"] only — MUST be a
                                # separate lock: _record_pair is called from
                                # inside _run_bootstrap_inner while _lock is
                                # held (a shared lock would self-deadlock)
_wake = threading.Event()       # MODEL-RUN-FIX: token import / admin force
                                # can wake the sleeping daemon instantly
_force_requested = False


def _fast_folds(n):
    """Expanding walk-forward cuts for the SMALL-DATA regime.

    The shared _folds() (walk_forward.py) assumes n ≥ ~550: below that it
    returns cuts like (200, 130) — train_end BEYOND the dataset — which
    made every small pair end "rejected: no test rows". Same contract as
    the big regime: train strictly before test, EMBARGO gap (2 rows = the
    T+2 overlap), last fold reaches n. Returns [] when even one honest
    fold is impossible.
    """
    if n >= FAST_SMALL_N:
        return _folds(n, FAST_N_FOLDS, None)
    cuts = []
    t1 = int(n * 0.4)                       # fold 1: train on the first 40%
    if t1 >= 25 and (n - (t1 + EMBARGO)) >= 15:
        cuts.append((t1, int(n * 0.7)))
    t2 = int(n * 0.7)                       # fold 2: train on the first 70%
    if t2 >= 25 and (n - (t2 + EMBARGO)) >= 15 and \
            (not cuts or t2 > cuts[-1][0]):
        cuts.append((t2, n))
    if not cuts:                            # single-fold fallback: 60/40
        t1 = int(n * 0.6)
        if t1 >= 25 and (n - (t1 + EMBARGO)) >= 10:
            cuts.append((t1, n))
    return cuts


def _sklearn_ok():
    """Read DYNAMICALLY (tests + startup both patch/miss it differently)."""
    return bool(getattr(_models, "SKLEARN_OK", False))


def _blocked_reason():
    """Why training cannot run at all, or None."""
    if not _sklearn_ok():
        return ("scikit-learn/numpy missing — মডেল ট্রেইন অসম্ভব "
                "(pip install scikit-learn numpy)")
    return None


# ────────────────────────────── small helpers ─────────────────────────────

def _log(msg):
    print(f"[fast-train] {msg}", flush=True)


def _current_token():
    """The live session token — exactly what the feed itself uses."""
    tok = ""
    try:
        from core import token_store
        rec = token_store.load_token()
        if rec and rec.get("token"):
            tok = str(rec["token"]).strip()
    except Exception:
        pass
    return tok or os.environ.get("QX_TOKEN", "").strip()


def _db_path():
    from db import DB_PATH
    return DB_PATH


def _micro_counts():
    """{asset: n} — closed 1m candles currently stored per OTC pair."""
    conn = sqlite3.connect(_db_path(), timeout=30)
    try:
        rows = conn.execute(
            "SELECT asset, COUNT(*) FROM candle_micro WHERE period=60 "
            "GROUP BY asset").fetchall()
    finally:
        conn.close()
    return {a: int(n) for a, n in rows}


def notify_token_pushed():
    """Wake the daemon NOW after a fresh token import (MODEL-RUN-FIX).

    A dead token blocks the history top-up; the user pastes a new one in
    the UI → the feed reconnects in seconds → training must retry NOW,
    not on its next 10-min timer. Called from server._apply_token().
    """
    if _state["running"]:
        return False
    _state["next_run_at"] = time.time() + 2.0
    _wake.set()
    return True


def pair_states():
    """Persistent per-pair snapshot for the Models tab."""
    with _pair_lock:
        return {a: dict(s) for a, s in _state["pairs"].items()}


def _next_run_in():
    """Seconds until the daemon's next attempt (None if not scheduled)."""
    if _state["running"]:
        return 0.0
    if _state["next_run_at"]:
        return max(0.0, round(_state["next_run_at"] - time.time(), 1))
    return None


def bootstrap_status():
    """JSON-safe status for /api/prediction/bootstrap and the UI."""
    blocked = _blocked_reason()
    return {
        "enabled": os.environ.get("QX_FAST_TRAIN", "1") not in ("0", "false", "no"),
        "running": _state["running"],
        "runs": _state["runs"],
        "last_run_ago": (round(time.time() - _state["last_run"], 1)
                         if _state["last_run"] else None),
        "last_error": _state["last_error"],
        "started_at": _state["started_at"],
        "sklearn_ok": _sklearn_ok(),
        "blocked": blocked,
        "pairs": pair_states(),
        "next_run_in": _next_run_in(),
        "retrain_secs": FAST_RETRAIN_SECS,
        "retry_secs": FAST_RETRY_SECS,
        "config": {
            "fast_days": FAST_DAYS, "min_candles": FAST_MIN_CANDLES,
            "min_pair_rows": FAST_MIN_PAIR_ROWS,
            "max_rows_per_pair": FAST_MAX_ROWS_PER_PAIR,
            "pool_max_rows": FAST_POOL_MAX_ROWS,
            "candidates": FAST_CANDIDATES,
            "folds": FAST_N_FOLDS, "min_test_pred": FAST_MIN_TEST_PRED,
            "shuffle_max": FAST_SHUFFLE_MAX,
            "baseline_margin_pp": FAST_BASELINE_MARGIN_PP,
            "retrain_secs": FAST_RETRAIN_SECS,
        },
        "result": _state["last_result"],
    }


# ─────────────────────── phase 1: history top-up ──────────────────────────

def ensure_history(counts=None, log=None):
    """Top up candle_micro for pairs shorter than FAST_MIN_CANDLES.

    Fetches real 1m history from the SAME platform the live feed uses
    (pyquotex + the live session SSID), INSERT OR IGNORE so a live row that
    already carries microstructure is never overwritten. Returns a report.
    """
    log = log or _log
    counts = counts if counts is not None else _micro_counts()
    missing = [a for a in sorted(ALLOWED_PAIRS_OTC)
               if counts.get(a, 0) < FAST_MIN_CANDLES]
    if not missing:
        return {"fetched": False, "reason": "all pairs have enough candles",
                "pairs": [], "counts": counts}
    token = _current_token()
    if not token:
        return {"fetched": False, "reason": "no token — training on what "
                "candle_micro already has", "pairs": missing, "counts": counts}

    # adaptive depth: only the missing candles (+1 day of margin), capped
    plan = {}
    for a in missing:
        need = FAST_MIN_CANDLES - counts.get(a, 0)
        days = max(1, min(FAST_DAYS, int(math.ceil(need / 1440.0)) + 1))
        plan[a] = days

    report = {"fetched": True, "pairs": missing, "counts": counts,
              "plan": plan, "results": {}}
    try:
        res = _fetch_pairs(plan, log)
        report["results"] = res
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(f"history top-up failed: {type(exc).__name__}: {exc}")
    return report


def _fetch_pairs(plan, log):
    """One platform connect per FAST_FETCH_BATCH pairs (kindness rules that
    scripts/fetch_otc_history.py proved on real data)."""
    import asyncio

    async def _run():
        out = {}
        items = list(plan.items())
        for k in range(0, len(items), FAST_FETCH_BATCH):
            batch = dict(items[k:k + FAST_FETCH_BATCH])
            if k:
                await asyncio.sleep(FAST_SLEEP_BETWEEN)
            try:
                res = await _fetch_batch(batch)
                out.update(res)
            except Exception as exc:
                for a in batch:
                    out[a] = {"status": "error",
                              "error": f"{type(exc).__name__}: {exc}"}
        return out

    return asyncio.run(_run())


async def _fetch_batch(plan):
    """Fetch a small batch of pairs and INSERT OR IGNORE into candle_micro."""
    token = _current_token()
    from pyquotex.stable_api import Quotex
    from pyquotex.network.login import Login
    from pyquotex.types import ReconnectPolicy

    host = os.environ.get("QX_HOST", "market-qx.trade")
    Login.base_url = host
    Login.https_base_url = f"https://{host}"
    client = Quotex(
        email="", password="", host=host, lang="en",
        root_path="/tmp/qx_fast_train",
        reconnect_policy=ReconnectPolicy(
            enabled=False, max_attempts=0, base_delay=2.0,
            max_delay=30.0, stale_timeout=45.0))
    client.set_session(
        user_agent=os.environ.get("QX_UA", "").strip() or
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) "
        "Gecko/20100101 Firefox/119.0",
        ssid=token)
    ok, reason = False, ""
    try:
        ok, reason = await client.connect()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
    if not ok:
        try:
            await client.close()
        except Exception:
            pass
        return {a: {"status": "connect_failed", "reason": str(reason)}
                for a in plan}

    out = {}
    conn = sqlite3.connect(_db_path(), timeout=60)
    try:
        for asset, days in plan.items():
            try:
                raw = await asyncio.wait_for(
                    client.get_historical_candles(
                        asset, amount_of_seconds=days * 86400,
                        period=60, max_workers=1),
                    timeout=FAST_FETCH_TIMEOUT)
            except Exception as exc:
                out[asset] = {"status": "error",
                              "error": f"{type(exc).__name__}: {exc}"}
                continue
            norm = []
            if isinstance(raw, dict):
                for key in ("candles", "data", "history"):
                    if key in raw:
                        raw = raw[key]
                        break
            for c in raw or []:
                try:
                    if not all(kk in c for kk in
                               ("open", "high", "low", "close")):
                        continue
                    t = int(c.get("time", c.get("from", 0)))
                    if t <= 0:
                        continue
                    norm.append((asset, 60, t, float(c["open"]),
                                 float(c["high"]), float(c["low"]),
                                 float(c["close"])))
                except Exception:
                    continue
            # INSERT OR IGNORE: a live candle_micro row (with microstructure)
            # must NEVER be replaced by a bare history row.
            before = conn.execute(
                "SELECT COUNT(*) FROM candle_micro WHERE asset=? AND period=60",
                (asset,)).fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO candle_micro"
                "(asset, period, ctime, open, high, low, close) "
                "VALUES (?,?,?,?,?,?,?)", norm)
            conn.commit()
            after = conn.execute(
                "SELECT COUNT(*) FROM candle_micro WHERE asset=? AND period=60",
                (asset,)).fetchone()[0]
            out[asset] = {"status": "ok", "rows": len(norm),
                          "added": after - before, "total": after,
                          "days": days}
    finally:
        conn.close()
        try:
            await client.close()
        except Exception:
            pass
    return out


# ─────────────────── phase 2: fast walk-forward train ─────────────────────

def fast_train_one(rows, seed=13):
    """Train + gate ONE asset's rows with the fast configuration.

    Returns (report, bundle_or_None, status) with status in
    {"verified", "provisional", "rejected"}.
    """
    import numpy as np
    rng = random.Random(seed)
    cand_names = [c for c in FAST_CANDIDATES if c in CANDIDATES()]
    if not cand_names or not _sklearn_ok():
        return {"error": _blocked_reason() or "sklearn unavailable"}, \
            None, "rejected"

    n = len(rows)
    # Guard: the first expanding fold must leave a real train split after
    # the Platt val tail — below this, folds fit on empty/near-empty sets.
    # The caller normally filters, this is the structural guarantee for
    # direct calls. (MODEL-RUN-FIX-2: floor is now 80, not 2000 — small
    # live datasets train immediately instead of never.)
    if n < FAST_MIN_PAIR_ROWS:
        return {"error": f"n={n} < FAST_MIN_PAIR_ROWS={FAST_MIN_PAIR_ROWS}",
                "rows": n}, None, "rejected"

    cuts = _fast_folds(n)
    if not cuts:
        return {"error": f"not enough rows for folds (n={n})"}, None, "rejected"

    pooled = {h: {c: {"p": [], "y": []} for c in cand_names}
              for h in ("y1_up", "y2_up")}
    base = {h: {"always_call": [0, 0], "prev_dir": [0, 0], "rev_dir": [0, 0]}
            for h in ("y1_up", "y2_up")}

    for (tr_end, te_end) in cuts:
        for h in ("y1_up", "y2_up"):
            for i in range(tr_end + EMBARGO, te_end):
                r = rows[i]
                y = 1 if r[h] else 0
                cur_dir = 1 if r.get("direction", 0) > 0 else 0
                b = base[h]
                b["always_call"][1] += 1
                b["always_call"][0] += y
                b["prev_dir"][1] += 1
                b["prev_dir"][0] += (y == cur_dir)
                b["rev_dir"][1] += 1
                b["rev_dir"][0] += (y == (1 - cur_dir))
            for name in cand_names:
                res = _fit_predict_fold(rows, EXTENDED_FEATURE_NAMES, h,
                                        [name], tr_end, te_end)
                _, p, y, _ = res[0]
                pooled[h][name]["p"].extend(float(x) for x in p)
                pooled[h][name]["y"].extend(int(v) for v in y)

    def _stats(p, y):
        if not p:
            return None
        acc = sum(1 for pi, yi in zip(p, y)
                  if (pi >= 0.5) == (yi == 1)) / len(p)
        ll = 0.0
        for pi, yi in zip(p, y):
            pi = min(max(pi, 1e-6), 1 - 1e-6)
            ll += math.log(pi) if yi else math.log(1 - pi)
        return {"n": len(p), "acc": round(acc, 4),
                "logloss": round(-ll / len(p), 4)}

    report = {"rows": n, "folds": len(cuts), "candidates": {}, "gate": {}}
    best_overall = None
    for h in ("y1_up", "y2_up"):
        stats = {c: _stats(v["p"], v["y"]) for c, v in pooled[h].items()}
        stats = {c: s for c, s in stats.items() if s}
        report["candidates"][h] = stats
        if not stats:
            continue
        cname, cs = min(stats.items(), key=lambda kv: kv[1]["logloss"])
        ys = pooled[h][cname]["y"][:]
        rng.shuffle(ys)
        ps = pooled[h][cname]["p"]
        shuf_acc = sum(1 for pi, yi in zip(ps, ys)
                       if (pi >= 0.5) == (yi == 1)) / len(ps)
        bl = base[h]
        bl_rates = {
            "always_call": 100 * bl["always_call"][0] / bl["always_call"][1],
            "prev_dir": 100 * bl["prev_dir"][0] / bl["prev_dir"][1],
            "rev_dir": 100 * bl["rev_dir"][0] / bl["rev_dir"][1],
        }
        acc_pp = 100 * cs["acc"]
        best_base = max(bl_rates.values())
        hard = {
            "no_leakage": shuf_acc < FAST_SHUFFLE_MAX,
            "enough_test_rows": cs["n"] >= FAST_MIN_TEST_PRED,
        }
        verified = {
            "beats_baseline": acc_pp > best_base + FAST_BASELINE_MARGIN_PP,
            "logloss_below_coinflip": cs["logloss"] < math.log(2),
        }
        report["gate"][h] = {
            "selected": cname, **cs,
            "acc_pct": round(acc_pp, 2),
            "baselines": {k: round(v, 2) for k, v in bl_rates.items()},
            "shuffle_acc": round(shuf_acc, 4),
            "hard": hard, "verified": verified,
        }
        if best_overall is None or cs["logloss"] < best_overall[1]:
            best_overall = (h, cs["logloss"], cname)

    if not report["gate"]:
        return report, None, "rejected"

    # HARD gates first (leakage = never registered, PART 19)
    all_hard_ok = all(g["hard"]["no_leakage"] and g["hard"]["enough_test_rows"]
                      for g in report["gate"].values())
    if not all_hard_ok:
        report["status"] = "rejected"
        return report, None, "rejected"

    all_verified = all(g["verified"]["beats_baseline"] and
                       g["verified"]["logloss_below_coinflip"]
                       for g in report["gate"].values())
    status = "verified" if all_verified else "provisional"
    report["status"] = status

    # production bundle: retrain the winning candidate on ALL rows
    version = time.strftime("v%Y%m%d-%H%M", time.gmtime())
    meta = {"trained_rows": n, "status": status, "trainer": "fast",
            "walk_forward": report["gate"]}
    bundle = ModelBundle(version, EXTENDED_FEATURE_NAMES, None, None, meta)
    Xall = np.array([[r[k] for k in EXTENDED_FEATURE_NAMES] for r in rows])
    for h, slot in (("y1_up", "t1"), ("y2_up", "t2")):
        yall = np.array([1 if r[h] else 0 for r in rows])
        # MODEL-RUN-FIX-2: small-data val tail — max(200, …) sliced the
        # production fit set to EMPTY for n<1000 (Xall[:-200] on 130 rows)
        # and crashed the whole bundle build.
        if len(Xall) >= 1000:
            val_n = max(200, int(len(Xall) * FAST_VAL_FRAC))
        else:
            val_n = max(10, int(len(Xall) * FAST_VAL_FRAC))
        model = fit_candidate(best_overall[2], Xall[:-val_n], yall[:-val_n])
        coefs = platt_calibrate(model, Xall[-val_n:], yall[-val_n:])
        setattr(bundle, slot, {"model": model, "platt": coefs,
                               "name": best_overall[2]})
    return report, bundle, status


# ─────────────────────── phase 3: the bootstrap run ───────────────────────

def run_bootstrap(force=False):
    """Full sequence: top-up history → dataset → per-pair fast train →
    register. Safe to call from a thread; never raises into the caller."""
    global _force_requested
    if _state["running"]:
        if not force:
            return {"started": False, "reason": "already_running",
                    "status": bootstrap_status()}
        # a forced run while busy: request a re-run right after this one
        _force_requested = True
        return {"started": False, "reason": "busy_force_queued",
                "status": bootstrap_status()}

    if not _lock.acquire(blocking=False):
        return {"started": False, "reason": "locked",
                "status": bootstrap_status()}
    _state["running"] = True
    _force_requested = False
    t0 = time.time()
    summary = {}
    try:
        summary = _run_bootstrap_inner()
        _state["runs"] += 1
        _state["last_run"] = time.time()
        _state["last_result"] = summary
        _state["last_error"] = None
    except Exception as exc:
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        summary = {"error": _state["last_error"]}
        _log(f"bootstrap failed: {_state['last_error']}")
    finally:
        _state["running"] = False
        summary["secs"] = round(time.time() - t0, 1)
        _lock.release()
    if _force_requested:
        # run the queued forced pass (e.g. admin pressed force mid-run)
        threading.Thread(target=run_bootstrap, kwargs={"force": False},
                         daemon=True).start()
    return {"started": True, "status": bootstrap_status(), "summary": summary}


def _record_pair(asset, **fields):
    """Merge fields into the persistent per-pair state (MODEL-RUN-FIX)."""
    with _pair_lock:
        st = _state["pairs"].setdefault(asset, {})
        st.update(fields)
        st["updated_at"] = time.time()


def _run_bootstrap_inner():
    _log("bootstrap run starting (hardcoded fast config)")
    blocked = _blocked_reason()
    if blocked:
        # MODEL-RUN-FIX: never fail silently again — the missing-deps case
        # is announced loudly AND recorded per-pair so the UI shows exactly
        # why no model can train. Fetch still runs: data accumulation is
        # useful the moment the deps land (redeploy).
        _log(f"FATAL: {blocked} — training phase will be skipped; "
             f"history top-up still runs so data keeps accumulating")
    counts = _micro_counts()
    for a in sorted(ALLOWED_PAIRS_OTC):
        _record_pair(a, candles=counts.get(a, 0))
    _log("candle_micro counts: " + ", ".join(
        f"{a.split('_')[0]}={n}" for a, n in sorted(counts.items())))

    # ── phase 1 — TRAIN on what candle_micro ALREADY has ────────────────
    # MODEL-RUN-FIX-2 REORDER: the (slow) platform history pull used to run
    # FIRST and training sat behind it — with 11 short pairs the fetch alone
    # eats the whole 5-7 min budget before a single model exists. Now the
    # small real dataset trains and registers IMMEDIATELY (the user sees
    # models + predictions within the first minute), and the fetch runs
    # after; the next 10-min run consolidates on the bigger merged data.
    candles_by_asset = {a: cs for a, cs in
                        load_candles_from_db(_db_path()).items()
                        if a in set(ALLOWED_PAIRS_OTC)}
    registered = []
    details = {}
    pooled_rows = []
    dstats = {"rows": 0, "dropped_doji_t1": 0, "dropped_doji_t2": 0}
    if not candles_by_asset:
        _log("no candle data at all — nothing to train on yet")
        for a in sorted(ALLOWED_PAIRS_OTC):
            _record_pair(a, status="no_data",
                         reason="candle_micro খালি — ফিড/টোকেন চেক করুন")
    else:
        rows, dstats = build_dataset(candles_by_asset, window=WINDOW,
                                     micro=True,
                                     feature_fn=build_extended_row)
        _log(f"dataset: {dstats['rows']} rows from "
             f"{len(candles_by_asset)} pairs "
             f"(doji t1={dstats['dropped_doji_t1']} "
             f"t2={dstats['dropped_doji_t2']})")

        by_asset = {}
        for r in rows:
            by_asset.setdefault(r["asset"], []).append(r)
        # newest-rows caps keep the retrain time bounded as history
        # accumulates (90d retention could otherwise push one RF fit
        # into minutes)
        for a in list(by_asset):
            if len(by_asset[a]) > FAST_MAX_ROWS_PER_PAIR:
                by_asset[a] = by_asset[a][-FAST_MAX_ROWS_PER_PAIR:]
        pooled_rows = [r for a in sorted(by_asset) for r in by_asset[a]]

        def _gate_summary(gate, h):
            g = gate.get(h) or {}
            if not g:
                return None
            return {"acc": g.get("acc_pct"),
                    "baseline": (g.get("baselines") or {}).get("prev_dir"),
                    "shuffle": g.get("shuffle_acc"),
                    "model": g.get("selected"),
                    "test_n": g.get("n")}

        for asset, arows in sorted(by_asset.items()):
            if blocked:
                details[asset] = {"status": "blocked", "rows": len(arows),
                                  "reason": blocked}
                _record_pair(asset, status="blocked", rows=len(arows),
                             reason=blocked)
                continue
            if len(arows) < FAST_MIN_PAIR_ROWS:
                details[asset] = {"status": "skipped",
                                  "rows": len(arows),
                                  "reason": f"< {FAST_MIN_PAIR_ROWS} rows"}
                _record_pair(asset, status="skipped", rows=len(arows),
                             reason=f"ডেটা কম: {len(arows)} rows < "
                                    f"{FAST_MIN_PAIR_ROWS}")
                continue
            report, bundle, status = fast_train_one(arows)
            gate = report.get("gate", {})
            details[asset] = {"status": status, "rows": len(arows),
                              "gate": gate}
            _record_pair(asset, status=status, rows=len(arows),
                         reason=report.get("error"),
                         t1=_gate_summary(gate, "y1_up"),
                         t2=_gate_summary(gate, "y2_up"),
                         error=report.get("error"))
            if bundle is None:
                continue
            path = save_bundle(bundle)
            from core.otc_predict.tracker import register_model
            register_model(asset, bundle.version, "pair", asset,
                           {"status": status, "rows": len(arows),
                            "walk_forward": report.get("gate", {})},
                           path, activate=True)
            registered.append(asset)
            _record_pair(asset, version=bundle.version,
                         registered_at=time.time())
            _log(f"{asset}: {status} bundle {bundle.version} registered "
                 f"({len(arows)} rows)")

        # pooled global fallback when NO per-pair bundle registered
        if not registered and len(pooled_rows) >= FAST_POOL_MIN_ROWS:
            if len(pooled_rows) > FAST_POOL_MAX_ROWS:
                pooled_rows = pooled_rows[-FAST_POOL_MAX_ROWS:]
            report, bundle, status = fast_train_one(pooled_rows, seed=17)
            details["__global__"] = {"status": status,
                                     "rows": len(pooled_rows)}
            _record_pair("__global__", status=status,
                         rows=len(pooled_rows),
                         reason=report.get("error"))
            if bundle is not None:
                path = save_bundle(bundle)
                from core.otc_predict.tracker import register_model
                register_model("global", bundle.version, "global", "",
                               {"status": status,
                                "rows": len(pooled_rows),
                                "walk_forward": report.get("gate", {})},
                               path, activate=True)
                registered.append("global")
                _record_pair("__global__", version=bundle.version,
                             registered_at=time.time())
                _log(f"global: {status} bundle {bundle.version} registered "
                     f"({len(pooled_rows)} pooled rows)")

    # ── phase 2 — history top-up AFTER registration (slow, background) ──
    fetch = ensure_history(counts)
    for a, res in (fetch.get("results") or {}).items():
        if res.get("status") == "ok":
            _record_pair(a, fetch={"added": res.get("added", 0),
                                   "total": res.get("total", 0)},
                         fetch_error=None)
        else:
            _record_pair(a, fetch_error=res.get(
                "reason") or res.get("error") or res.get("status"))
    counts = _micro_counts()
    for a in sorted(ALLOWED_PAIRS_OTC):
        _record_pair(a, candles=counts.get(a, 0))
    fetch_added = sum(
        int((res or {}).get("added", 0))
        for res in (fetch.get("results") or {}).values()
        if isinstance(res, dict))

    # predictor picks up new registry rows on its TTL; nudge it now
    try:
        from core.otc_predict import predictor
        predictor._cache["checked_at"] = 0.0
    except Exception:
        pass

    _log(f"bootstrap done: registered={registered or 'NONE'} "
         f"({round(time.time() - _state['started_at'], 0)}s since start)")
    return {"fetch": fetch, "fetch_added": fetch_added,
            "pairs_registered": registered,
            "details": details, "dataset_rows": dstats["rows"],
            "dataset_pairs": len(candles_by_asset)}


# ─────────────────────────── the daemon thread ────────────────────────────

def _next_sleep_secs(last_summary, last_error):
    """MODEL-RUN-FIX: adaptive cadence.

    A run that registered at least one model → normal 6h refresh.
    A run that registered NOTHING (fetch failed, data short, transient
    error, blocked deps) → retry in 10 minutes so a deploy that starts
    working self-heals within the user's patience window instead of
    silently sleeping 6 hours.
    MODEL-RUN-FIX-2: a run that trained on the small live dataset AND
    just landed fresh history also retries in 10 minutes — the next run
    consolidates the bundles on the much bigger merged dataset.
    """
    if last_error:
        return FAST_RETRY_SECS
    s = last_summary or {}
    if s.get("pairs_registered"):
        if s.get("fetch_added"):
            return FAST_RETRY_SECS      # consolidation retrain soon
        return FAST_RETRAIN_SECS
    return FAST_RETRY_SECS


def _daemon():
    time.sleep(FAST_BOOT_DELAY_SECS)
    if os.environ.get("QX_FAST_TRAIN", "1") in ("0", "false", "no"):
        _log("disabled by QX_FAST_TRAIN=0 — daemon exiting")
        return
    blocked = _blocked_reason()
    if blocked:
        _log(f"WARNING: {blocked}")
    _log(f"daemon armed: first run in {FAST_BOOT_DELAY_SECS}s, then every "
         f"{FAST_RETRAIN_SECS // 3600}h (or {FAST_RETRY_SECS // 60}min "
         f"retry after an empty run — hardcoded fast config)")
    while True:
        try:
            res = run_bootstrap()
            summary = res.get("summary") or {}
            if res.get("started"):
                _log(f"run #{_state['runs']}: registered="
                     f"{summary.get('pairs_registered')} in "
                     f"{summary.get('secs')}s")
            # schedule the next attempt from THIS run's outcome (also
            # covers the force-run race: a forced pass finishing here
            # simply reschedules — the daemon is the only sleeper)
            sleep_for = _next_sleep_secs(summary, _state["last_error"])
            _state["next_run_at"] = time.time() + sleep_for
            _log(f"next run in {sleep_for // 60:.0f}min")
        except Exception as exc:   # the daemon must never die
            _log(f"daemon loop error: {type(exc).__name__}: {exc}")
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            _state["next_run_at"] = time.time() + FAST_RETRY_SECS
        nxt = _state.get("next_run_at") or (time.time() + FAST_RETRY_SECS)
        # sleep until the next slot — but wake instantly when a fresh token
        # (or an admin nudge) arrives
        _wake.wait(timeout=max(1.0, nxt - time.time()))
        _wake.clear()


def start_daemon():
    """Start the background trainer once (idempotent)."""
    if _state["thread_started"]:
        return
    _state["thread_started"] = True
    threading.Thread(target=_daemon, name="otc-fast-train",
                     daemon=True).start()
