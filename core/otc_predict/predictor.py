"""core/otc_predict/predictor.py — live prediction engine (PART 15+16+24+27).

USER SPEC (PART 15 — Signal Timing + PART 16 — Freeze + PART 27 — Live
Architecture):

    20:45 candle শেষ হওয়ার মুহূর্তে (EOC, কোনো নতুন tick ছাড়াই) system
    candle-১০০-পর্যন্ত CLOSED data দিয়ে প্রেডিকশন করে:
        T+1 → candle 101   |   T+2 → candle 102
    Signal তৈরি হওয়ার সাথে সাথেই FREEZE (PART 16): otc_predictions টেবিলে
    INSERT OR IGNORE — পরে কখনো বদলানো যাবে না। ফল আসে candle close-এ
    (settle), কেউ না, কিছুই এডিট করতে পারে না।

    PART 27 flow (implemented here):
        Candle close → Feature Engineering → [T+1 Model ∥ T+2 Model]
        → Price Action → Signal Filter → FREEZE → (broadcast via feed)
        → Result Tracker (settle on later closes)

    PART 24 quality gates — any failure ⇒ NO SIGNAL (still tracked):
        data_complete / no_gap / model_loaded / vol_acceptable / no_conflict

HONEST STATES (সবচেয়ে গুরুত্বপূর্ণ নীতি): model না থাকলে / data gap হলে /
window ছোট হলে → status সহ NO SIGNAL — কখনো বানানো signal নয়।

This module NEVER raises into the feed: on_candle_closed() swallows and
logs every failure (the live feed's health outranks predictions).
"""

import json
import os
import threading
import time

from core.otc_predict.features_ext import (build_extended_row,
                                           EXTENDED_FEATURE_NAMES,
                                           MIN_WINDOW_EXT)
from core.otc_predict.regime import detect_regime
from core.otc_predict.price_action import price_action_confirm
from core.otc_predict.signal_filter import score_signal

__all__ = ["on_candle_closed", "engine_enabled", "PRED_WINDOW",
           "runtime_status"]

PRED_WINDOW = int(os.environ.get("QX_PRED_WINDOW", "50"))  # PART 7: 20-50

_engine_on = os.environ.get("QX_PREDICT", "1") not in ("0", "false", "no")

# ── model cache (reload-aware, PART 25 continuous learning friendly) ──────
# Per-registry-entry cache: key "<name>:<version>" → ModelBundle.
_cache = {"bundles": {}, "reg": {}, "checked_at": 0.0}
_TTL = float(os.environ.get("QX_PREDICT_MODELS_TTL", "300"))

# ── PREDICT-FLOW-FIX (2026-09-12): live runtime telemetry ────────────────
# The user waited an hour with 11 models registered and ZERO predictions
# and the UI could only show "—" — every failure in this module used to be
# a print() that nobody sees. Now every candle close records what happened
# (window short? no model? bundle load error? frozen? settled?) and the
# মডেল tab shows it in plain Bengali.
_runtime = {
    "started_at": time.time(),
    "closes_seen": 0,      # candle closes handed to on_candle_closed
    "predicted": 0,        # closes that produced a T+1/T+2 payload
    "frozen": 0,           # NEW prediction rows actually written
    "no_model": 0,         # closes with no usable bundle for the pair
    "window_short": 0,     # closes skipped: stream has < PRED_WINDOW candles
    "errors": 0,           # unexpected exceptions in the predict path
    "settle_graded": 0,    # rows graded at exact-close (settle_target)
    "settle_history": 0,   # rows graded from reloaded history (restart-proof)
    "registry_rows": 0,    # active registry rows at last refresh
    "bundles_loaded": 0,   # distinct bundles successfully loaded this run
    "last_error": None,
    "last_close_at": 0.0,
    "per_asset": {},       # asset → live counters + last status/reason
}
_runtime_lock = threading.Lock()


def _note(asset, **fields):
    """Merge fields into per-asset runtime state (bounded, JSON-safe)."""
    with _runtime_lock:
        st = _runtime["per_asset"].setdefault(
            asset, {"closes": 0, "frozen": 0, "last_status": None,
                    "last_reason": None, "last_error": None,
                    "last_at": 0.0})
        st.update(fields)
        st["last_at"] = time.time()


def runtime_status():
    """JSON-safe snapshot for /api/prediction/overview (মডেল tab)."""
    with _runtime_lock:
        per = {a: dict(s) for a, s in _runtime["per_asset"].items()}
    snap = {k: v for k, v in _runtime.items() if k != "per_asset"}
    snap["per_asset"] = per
    snap["uptime_secs"] = round(time.time() - _runtime["started_at"], 1)
    snap["engine_enabled"] = bool(_engine_on)
    snap["pred_window"] = PRED_WINDOW
    return snap


def engine_enabled():
    return _engine_on


def _get_bundle(asset=None):
    """Active ModelBundle for this pair (PART 22) or None.

    Lookup order: registry row named `asset` (per-pair model) → row named
    "global" (pooled model). Re-checks the registry on TTL so a newly
    registered bundle is picked up without a redeploy.

    PREDICT-FLOW-FIX: a registry READ FAILURE no longer wipes the cache —
    the previous registry keeps serving (stale-but-good) for the next TTL
    window. The old code cached `{}` on a transient sqlite lock (the fast-
    train daemon writes the registry from another thread every few minutes)
    and silently predicted NOTHING for up to 5 minutes — every such candle
    close is a prediction that can never be graded later.
    """
    now = time.time()
    if now - _cache["checked_at"] >= _TTL or _cache["checked_at"] <= 0:
        try:
            from core.otc_predict.tracker import active_models
            reg = active_models()
            _cache["reg"] = reg
            with _runtime_lock:
                _runtime["registry_rows"] = len(reg)
        except Exception as exc:
            print(f"[predictor] registry read failed (keeping stale): "
                  f"{type(exc).__name__}: {exc}")
            with _runtime_lock:
                _runtime["last_error"] = (
                    f"registry read: {type(exc).__name__}: {exc}")
        _cache["checked_at"] = now
    reg = _cache.get("reg") or {}
    row = reg.get(asset) if asset else None
    if row is None:
        row = reg.get("global")
    if not row:
        return None
    key = f"{row['name']}:{row['version']}"
    cached = _cache["bundles"].get(key)
    if cached is not None:
        return cached
    try:
        from core.otc_predict.models import SKLEARN_OK, load_bundle
        if not SKLEARN_OK:
            with _runtime_lock:
                _runtime["last_error"] = "sklearn unavailable at bundle load"
            return None
        import os as _os
        path = row["path"]
        if not path or not _os.path.exists(path):
            print(f"[predictor] registered bundle missing on disk: {path}")
            if asset:
                _note(asset, last_status="bundle_missing",
                      last_reason=f"bundle file missing: {path}")
            return None
        bundle = load_bundle(path)
        _cache["bundles"][key] = bundle
        with _runtime_lock:
            _runtime["bundles_loaded"] += 1
        print(f"[predictor] loaded model bundle {row['version']} "
              f"({row['name']})")
        return bundle
    except Exception as exc:
        print(f"[predictor] bundle load failed: {type(exc).__name__}: {exc}")
        if asset:
            _note(asset, last_status="bundle_load_failed",
                  last_error=f"{type(exc).__name__}: {exc}")
        return None


def _quality_gates(window, period, bundle, reg_info, pa):
    """PART 24 minimum conditions. Values are booleans keyed by rule."""
    w = PRED_WINDOW
    data_complete = len(window) >= w
    # gap check across the FEATURE window tail (contiguous minutes)
    no_gap = data_complete and all(
        window[j]["time"] - window[j - 1]["time"] == period
        for j in range(1, len(window)))
    model_loaded = bundle is not None
    vol_acceptable = not reg_info.get("extreme_vol", False)
    no_conflict = pa.get("against_count", 0) < 3
    return {"data_complete": data_complete, "no_gap": no_gap,
            "model_loaded": model_loaded, "vol_acceptable": vol_acceptable,
            "no_conflict": no_conflict}


def on_candle_closed(asset, period, candles, closed_candle, micro):
    """Called by feed the moment a candle closes (before new-candle ticks).

    1. Settles every frozen prediction whose target_time == closed time,
       THEN grades any older prediction whose close was missed while the
       app was down/redeploying — the reloaded stream history still holds
       those REAL candles (PREDICT-FLOW-FIX, restart-proof settlement).
    2. If the engine is enabled and models exist: predicts T+1/T+2 from
       CLOSED candles only, applies PART 24 gates + PART 14 score, freezes
       both rows, returns the WS payload (None when nothing to broadcast).

    Every branch records runtime telemetry — the মডেল tab can always answer
    "কেন প্রেডিকশন শূন্য?" in plain words instead of a silent "—".
    """
    if not _engine_on:
        return None

    with _runtime_lock:
        _runtime["closes_seen"] += 1
        _runtime["last_close_at"] = time.time()
    if asset:
        with _runtime_lock:
            st = _runtime["per_asset"].setdefault(
                asset, {"closes": 0, "frozen": 0, "last_status": None,
                        "last_reason": None, "last_error": None,
                        "last_at": 0.0})
            st["closes"] = st.get("closes", 0) + 1

    # 1a) settlement first — the closed candle IS some earlier T+1/T+2 target
    try:
        from core.otc_predict.tracker import settle_target
        n = settle_target(asset, period, closed_candle["time"],
                          closed_candle["open"], closed_candle["close"])
        if n:
            with _runtime_lock:
                _runtime["settle_graded"] += n
    except Exception as exc:
        print(f"[predictor] settle failed {asset}: {type(exc).__name__}: {exc}")
        _note(asset, last_error=f"settle: {type(exc).__name__}: {exc}")

    # 1b) restart-proof settlement — grade predictions whose target candle
    # closed while the app was down; the stream history window still holds
    # those real candles. Cheap: dict lookup over ≤400-500 closed candles.
    try:
        from core.otc_predict.tracker import settle_from_history
        n = settle_from_history(asset, period, list(candles))
        if n:
            with _runtime_lock:
                _runtime["settle_history"] += n
            print(f"[predictor] {asset}: settled {n} missed-close "
                  f"prediction(s) from reloaded history")
    except Exception as exc:
        print(f"[predictor] settle-from-history failed {asset}: "
              f"{type(exc).__name__}: {exc}")

    # 2) live prediction for the NEXT two candles
    try:
        from core.otc_predict.tracker import insert_prediction
        bundle = _get_bundle(asset)
        window = list(candles[-PRED_WINDOW:])
        if len(window) < PRED_WINDOW or window[-1]["time"] != closed_candle["time"]:
            with _runtime_lock:
                _runtime["window_short"] += 1
            _note(asset, last_status="window_short",
                  last_reason=f"স্ট্রিমে {len(window)} ক্যান্ডেল — "
                              f"{PRED_WINDOW} লাগবে")
            return None  # not enough history yet — honest no-op

        status = "ok" if bundle else "no_model"
        payload = {"asset": asset, "period": period,
                   "signal_time": closed_candle["time"],
                   "model_version": bundle.version if bundle else None,
                   # FAST-TRAIN (2026-09-12): "verified" | "provisional" —
                   # the UI shows a প্রোভিশনাল badge for unproven models.
                   "model_status": (bundle.meta.get("status", "verified")
                                    if bundle else None),
                   "status": status, "locked": True,
                   "t1": None, "t2": None}

        if bundle is None:
            payload["reason"] = "no_model_registered"
            with _runtime_lock:
                _runtime["no_model"] += 1
            _note(asset, last_status="no_model",
                  last_reason="রেজিস্ট্রিতে সক্রিয় মডেল নেই")
            return payload

        feats = build_extended_row(window, micro=micro)

        frozen_here = 0
        for horizon, key in ((1, "t1"), (2, "t2")):
            # horizon-scoped pass: PA first, then the PART 24 gates with the
            # REAL per-horizon conflict state (not a placeholder)
            prob = bundle.predict_up(horizon, feats)
            direction_up = prob is not None and prob >= 0.5
            pa = price_action_confirm(window, direction_up, features=feats)
            reg_info = detect_regime(window)
            quality = _quality_gates(window, period, bundle, reg_info, pa)
            filt = score_signal(
                prob if prob is not None else 0.5, direction_up,
                pa, reg_info, quality)
            filt["probability"] = round(prob, 4) if prob is not None else 0.5
            filt["status"] = "ok" if prob is not None else "model_missing"
            target_time = closed_candle["time"] + horizon * period
            inserted = insert_prediction(
                asset=asset, period=period,
                signal_time=closed_candle["time"], target_time=target_time,
                horizon=horizon, prediction=filt["prediction"],
                probability=filt["probability"], tier=filt["tier"],
                score=filt["score"], emit=filt["emit"],
                components=filt["components"], regime=filt["regime"],
                pa_agreed=filt["pa_agreed"], quality=quality,
                reason=filt["reason"], model_version=bundle.version,
                # PART 17 audit trail: full features frozen for emitted
                # signals only (bounded storage); tracked-only rows skip it.
                feature_json=(feats if filt["emit"] else None),
                close_i=closed_candle["close"])
            if inserted:
                frozen_here += 1
            payload[key] = {
                "target_time": target_time,
                "prediction": filt["prediction"],
                "probability": filt["probability"],
                "tier": filt["tier"], "score": filt["score"],
                "emit": filt["emit"], "reason": filt["reason"],
                "regime": filt["regime"],
                "pa_agreed": filt["pa_agreed"],
                "frozen_new": bool(inserted),
                # frozen_new=False ⇒ the UNIQUE freeze key already held a row
                # (replay/late callback) and the original prediction stands —
                # PART 16 doing its job.
            }
        payload["quality"] = quality
        with _runtime_lock:
            _runtime["predicted"] += 1
            _runtime["frozen"] += frozen_here
            st = _runtime["per_asset"].setdefault(
                asset, {"closes": 0, "frozen": 0, "last_status": None,
                        "last_reason": None, "last_error": None,
                        "last_at": 0.0})
            st["frozen"] = st.get("frozen", 0) + frozen_here
            st["last_status"] = "ok"
            st["last_reason"] = None
            st["last_error"] = None
            st["last_at"] = time.time()
        return payload
    except Exception as exc:
        print(f"[predictor] predict failed {asset}: "
              f"{type(exc).__name__}: {exc}")
        with _runtime_lock:
            _runtime["errors"] += 1
            _runtime["last_error"] = f"{asset}: {type(exc).__name__}: {exc}"
        _note(asset, last_status="error",
              last_error=f"{type(exc).__name__}: {exc}")
        return None


def describe_status(asset=None):
    """For /api/prediction endpoints: engine + registry + bootstrap state.

    `asset` resolves the SAME model the live predictor would use for that
    pair (per-pair → global fallback), so the UI card shows the honest
    status of the model actually behind the pair's predictions.
    """
    bundle = _get_bundle(asset)
    try:
        from core.otc_predict.models import SKLEARN_OK
    except Exception:
        SKLEARN_OK = False

    # FAST-TRAIN (2026-09-12): resolve the active registry row's recorded
    # status (verified/provisional) + the bootstrap daemon state, so the UI
    # can show an honest "মডেল ট্রেইন হচ্ছে…" instead of a dead end.
    model_status = None
    trained_rows = None
    if bundle is not None:
        model_status = bundle.meta.get("status", "verified")
        trained_rows = bundle.meta.get("trained_rows")
    else:
        reg = _cache.get("reg") or {}
        row = reg.get(asset) if (asset and asset in reg) else reg.get("global")
        if row:
            try:
                m = json.loads(row.get("metrics") or "{}")
                model_status = m.get("status")
                trained_rows = m.get("rows")
            except Exception:
                pass

    fast = {}
    try:
        from core.otc_predict import fast_train
        st = fast_train.bootstrap_status()
        fast = {"enabled": st.get("enabled"), "running": st.get("running"),
                "runs": st.get("runs"), "last_run_ago": st.get("last_run_ago"),
                "last_error": st.get("last_error"),
                # MODEL-RUN-FIX: surface the hard blocker + next attempt so
                # the UI can say WHY nothing trained (e.g. sklearn missing).
                "blocked": st.get("blocked"),
                "sklearn_ok": st.get("sklearn_ok"),
                "next_run_in": st.get("next_run_in"),
                "retry_secs": st.get("retry_secs")}
        res = st.get("result") or {}
        if res:
            fast["pairs_registered"] = res.get("pairs_registered")
    except Exception:
        fast = {}

    return {"engine_enabled": _engine_on,
            "sklearn_ok": bool(SKLEARN_OK),
            "model_version": bundle.version if bundle else None,
            "model_status": model_status,
            "trained_rows": trained_rows,
            "fast_train": fast,
            "window": PRED_WINDOW}
