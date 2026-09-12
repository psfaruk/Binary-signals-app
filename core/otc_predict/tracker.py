"""core/otc_predict/tracker.py — prediction freeze + result tracking
(PART 16 Freeze + PART 17 Result Tracking + PART 20 Paper Trading +
PART 21 Performance Dashboard).

USER SPEC:

  PART 16: Signal তৈরি হওয়ার পরে prediction পরিবর্তন করা যাবে না।
           20:45:30 Prediction = UP 76% → LOCKED … পরে signal edit করে
           historical accuracy বাড়ানোর সুযোগ থাকবে না।
  PART 17: প্রতিটি prediction database-এ prediction_id / pair / signal_time /
           target_time / prediction / probability / model_version /
           actual_result / win_loss রাখতে হবে।
  PART 20: Real money signal দেওয়ার আগে 100 → 500 → 1000+ signals track।
  PART 21: Total Predictions / Signals / Wins / Losses / Win Rate /
           No Signal % / Average Confidence / T+1 Accuracy / T+2 Accuracy /
           Best Pair / Worst Pair / Best Time / Worst Time。

FREEZE MECHANISM (structural, not procedural):
  insert_prediction() uses INSERT OR IGNORE on UNIQUE(asset, period,
  target_time, horizon). The FIRST row written for a (pair, target candle,
  horizon) is permanent; any later write for the same key is silently
  dropped — nobody, including future code paths, can rewrite history.
  Settlement (settle_target) only ever fills actual_result / actual_open /
  actual_close / win_loss / settled_at on rows whose settlement columns are
  still NULL.
"""

import json
import time

from db import _cursor

__all__ = ["insert_prediction", "settle_target", "latest_predictions",
           "prediction_analytics", "register_model", "active_models",
           "prediction_count"]


def insert_prediction(*, asset, period, signal_time, target_time, horizon,
                      prediction, probability, tier, score, emit,
                      components=None, regime="", pa_agreed=0, quality=None,
                      reason="", model_version="", feature_json=None,
                      close_i=None):
    """FREEZE one prediction. Returns True when a new row was written.

    Re-writing the same (asset, period, target_time, horizon) returns False
    and changes nothing — PART 16 enforced by the UNIQUE key.
    """
    with _cursor() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO otc_predictions
               (asset, period, signal_time, target_time, horizon,
                prediction, probability, tier, score, emit,
                components, regime, pa_agreed, quality, reason,
                model_version, feature_json, close_i, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (asset, int(period), int(signal_time), int(target_time),
             int(horizon), prediction, float(probability), tier, int(score),
             1 if emit else 0,
             json.dumps(components or {}), regime, 1 if pa_agreed else 0,
             json.dumps(quality or {}), reason, model_version,
             json.dumps(feature_json) if feature_json is not None else None,
             close_i, time.time()))
        return cur.rowcount > 0


def settle_target(asset, period, target_ctime, open_, close_):
    """Fill in the actual result for every frozen prediction whose target
    candle just closed (PART 17). Never touches settled rows.

    UP/DRAW/DOWN follows the app's own grading (close vs open). Win/loss:
    CALL wins iff UP; PUT wins iff DOWN; DRAW settles as draw.
    Returns the number of rows settled.
    """
    if close_ > open_:
        actual = "UP"
    elif close_ < open_:
        actual = "DOWN"
    else:
        actual = "DRAW"
    now = time.time()
    n = 0
    with _cursor() as c:
        rows = c.execute(
            """SELECT id, prediction FROM otc_predictions
               WHERE asset=? AND period=? AND target_time=?
                 AND settled_at IS NULL""",
            (asset, int(period), int(target_ctime))).fetchall()
        for r in rows:
            pred = r["prediction"]
            if actual == "DRAW":
                wl = "draw"
            elif (pred == "CALL" and actual == "UP") or \
                 (pred == "PUT" and actual == "DOWN"):
                wl = "win"
            else:
                wl = "loss"
            c.execute(
                """UPDATE otc_predictions
                   SET actual_result=?, actual_open=?, actual_close=?,
                       win_loss=?, settled_at=?
                   WHERE id=? AND settled_at IS NULL""",
                (actual, float(open_), float(close_), wl, now, r["id"]))
            n += 1
    return n


def latest_predictions(asset, limit=20, emit_only=False):
    """Newest frozen predictions for a pair (UI card + drill-in)."""
    q = ("SELECT * FROM otc_predictions WHERE asset=? AND period=60"
         + (" AND emit=1" if emit_only else "")
         + " ORDER BY signal_time DESC LIMIT ?")
    with _cursor() as c:
        rows = c.execute(q, (asset, int(limit))).fetchall()
    return [dict(r) for r in rows]


def prediction_count():
    with _cursor() as c:
        return c.execute("SELECT COUNT(*) FROM otc_predictions").fetchone()[0]


def prediction_analytics(days=None):
    """PART 21 dashboard metrics (settled rows only)."""
    cutoff = (time.time() - days * 86400) if days else 0
    with _cursor() as c:
        rows = c.execute(
            """SELECT asset, horizon, emit, tier, probability, win_loss,
                      signal_time, score, model_version
               FROM otc_predictions
               WHERE settled_at IS NOT NULL AND signal_time > ?""",
            (cutoff,)).fetchall()

    def _blank():
        return {"n": 0, "wins": 0, "losses": 0, "draws": 0,
                "dir_n": 0, "dir_wins": 0, "dir_losses": 0, "dir_draws": 0}

    out = {
        "generated_at": time.time(),
        "total_predictions": len(rows),
        "total_signals": sum(1 for r in rows if r["emit"]),
        "wins": sum(1 for r in rows if r["emit"] and r["win_loss"] == "win"),
        "losses": sum(1 for r in rows if r["emit"] and r["win_loss"] == "loss"),
        "no_signal_pct": 0.0,
        "avg_confidence": 0.0,
        "t1": _blank(), "t2": _blank(),
        "per_pair": {}, "per_tier": {}, "per_hour": {},
        "model_versions": {},
        # MODEL-RUN-FIX: directional accuracy over ALL frozen predictions
        # (emit হোক বা না হোক) — provisional মডেলের আসল "রেজাল্ট" এটাই।
        "dir_total": 0, "dir_wins": 0, "dir_losses": 0, "dir_draws": 0,
    }
    conf_sum = conf_n = 0
    hour_stats = {}
    for r in rows:
        slot = out["t1"] if r["horizon"] == 1 else out["t2"]
        slot["n"] += 1
        # directional accuracy counts EVERY settled row (emit or not)
        slot["dir_n"] += 1
        slot["dir_" + (r["win_loss"] or "draw")] += 1
        out["dir_total"] += 1
        out["dir_" + (r["win_loss"] or "draw")] += 1
        if r["emit"]:
            slot["wins" if r["win_loss"] == "win" else
                 "losses" if r["win_loss"] == "loss" else "draws"] += 1
        conf_sum += r["probability"] or 0
        conf_n += 1

        pp = out["per_pair"].setdefault(
            r["asset"], {"n": 0, "emit": 0, "wins": 0, "losses": 0,
                         "dir_n": 0, "dir_wins": 0, "dir_losses": 0})
        pp["n"] += 1
        pp["dir_n"] += 1
        if r["win_loss"] == "win":
            pp["dir_wins"] += 1
        elif r["win_loss"] == "loss":
            pp["dir_losses"] += 1
        if r["emit"]:
            pp["emit"] += 1
            pp["wins" if r["win_loss"] == "win" else
               "losses" if r["win_loss"] == "loss" else "draws"] += 1
            if r["win_loss"] in ("win", "loss"):
                h = time.strftime("%H", time.gmtime(r["signal_time"]))
                hs = hour_stats.setdefault(h, {"wins": 0, "losses": 0})
                hs[r["win_loss"]] += 1

        tier = out["per_tier"].setdefault(
            r["tier"] or "?", {"n": 0, "emit": 0, "wins": 0, "losses": 0})
        tier["n"] += 1
        if r["emit"]:
            tier["emit"] += 1
            tier["wins" if r["win_loss"] == "win" else
                 "losses" if r["win_loss"] == "loss" else "draws"] += 1

        mv = r["model_version"] or "?"
        out["model_versions"][mv] = out["model_versions"].get(mv, 0) + 1

    if out["total_signals"] > 0:
        dec = out["wins"] + out["losses"]
        out["win_rate"] = round(100.0 * out["wins"] / dec, 2) if dec else None
    else:
        out["win_rate"] = None
    if conf_n:
        out["avg_confidence"] = round(conf_sum / conf_n, 4)
    tracked = [r for r in rows if r["emit"]]
    out["no_signal_pct"] = round(
        100.0 * (len(rows) - len(tracked)) / len(rows), 1) if rows else 0.0

    for k in ("t1", "t2"):
        dec = out[k]["wins"] + out[k]["losses"]
        out[k]["win_rate"] = round(100.0 * out[k]["wins"] / dec, 2) if dec else None
        # MODEL-RUN-FIX: all-prediction directional accuracy per horizon
        ddec = out[k]["dir_wins"] + out[k]["dir_losses"]
        out[k]["dir_win_rate"] = (round(100.0 * out[k]["dir_wins"] / ddec, 2)
                                  if ddec else None)
    # top-level directional accuracy (ALL settled predictions)
    ddec = out["dir_wins"] + out["dir_losses"]
    out["dir_win_rate"] = (round(100.0 * out["dir_wins"] / ddec, 2)
                           if ddec else None)
    for a, v in out["per_pair"].items():
        ddec = v["dir_wins"] + v["dir_losses"]
        v["dir_win_rate"] = (round(100.0 * v["dir_wins"] / ddec, 2)
                             if ddec else None)

    def _wr(d):
        dec = d["wins"] + d["losses"]
        return (100.0 * d["wins"] / dec) if dec else -1.0

    pair_rows = [(a, v, _wr(v)) for a, v in out["per_pair"].items()
                 if v["wins"] + v["losses"] >= 5]
    if pair_rows:
        best = max(pair_rows, key=lambda x: x[2])
        worst = min(pair_rows, key=lambda x: x[2])
        out["best_pair"] = {"asset": best[0], "win_rate": round(best[2], 2),
                            "n": best[1]["wins"] + best[1]["losses"]}
        out["worst_pair"] = {"asset": worst[0], "win_rate": round(worst[2], 2),
                             "n": worst[1]["wins"] + worst[1]["losses"]}
    if hour_stats:
        hrows = [(h, 100.0 * v["wins"] / (v["wins"] + v["losses"]), v)
                 for h, v in hour_stats.items()
                 if v["wins"] + v["losses"] >= 5]
        if hrows:
            bh = max(hrows, key=lambda x: x[1])
            wh = min(hrows, key=lambda x: x[1])
            out["best_time"] = {"hour_utc": bh[0], "win_rate": round(bh[1], 2)}
            out["worst_time"] = {"hour_utc": wh[0], "win_rate": round(wh[1], 2)}
    return out


# ─────────────────────────── model registry (PART 25/28) ──────────────────

def register_model(name, version, scope, asset, metrics, path, activate=True):
    """Insert (or update) a trained bundle and optionally make it active."""
    now = time.time()
    with _cursor() as c:
        c.execute(
            """INSERT INTO model_registry
               (name, version, scope, asset, trained_at, metrics, path,
                active, created_at)
               VALUES (?,?,?,?,?,?,?,0,?)
               ON CONFLICT(name, version) DO UPDATE SET
                 metrics=excluded.metrics, path=excluded.path,
                 trained_at=excluded.trained_at""",
            (name, version, scope, asset, now,
             json.dumps(metrics or {}), path, now))
        if activate:
            c.execute("UPDATE model_registry SET active=0 WHERE name=?", (name,))
            c.execute("UPDATE model_registry SET active=1 "
                      "WHERE name=? AND version=?", (name, version))
    return version


def active_models():
    """All active registry rows: {name: row_dict}."""
    with _cursor() as c:
        rows = c.execute(
            "SELECT * FROM model_registry WHERE active=1").fetchall()
    return {r["name"]: dict(r) for r in rows}
