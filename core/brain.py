"""
core/brain.py — Self-learning brain for the Binary Signals App.

This module turns the DB into a "brain" that:
1. Records EVERY detail of each prediction (full context)
2. Auto-analyzes WHY each prediction won/lost
3. Identifies patterns across losses and wins
4. Writes solutions/insights back to DB
5. Generates actionable recommendations for the engine

Tables created:
  - brain_predictions: full context of every prediction
  - brain_module_votes: per-module vote for each prediction
  - brain_patterns: discovered patterns (what leads to wins/losses)
  - brain_insights: auto-generated solutions and recommendations
  - brain_learning: per-pair per-module learned weights (for adaptation)

The brain runs analysis after every N graded signals, finding:
  - "When module X votes CALL in regime Y, win rate is Z%"
  - "Pair X loses 60% when confidence > 70% in RANGE regime"
  - "Module X contradicts module Y 80% of the time — Y is more often right"
  - "Time-of-day pattern: losses cluster at hour H"
"""
import json
import sqlite3
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "signals.db"))
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════════════════════════════════
#  SCHEMA — brain tables
# ═══════════════════════════════════════════════════════════════════════════

def init_brain():
    """Create brain tables. Called on app startup after db.init()."""
    conn = _conn()
    cur = conn.cursor()
    try:
        # ── brain_predictions: full context of every prediction ──
        cur.execute("""CREATE TABLE IF NOT EXISTS brain_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            asset TEXT, period INT, ctime INT,
            category TEXT,
            signal TEXT, confidence REAL, strength TEXT, score INT,
            actual TEXT, accuracy TEXT,
            regime TEXT, regime_strength REAL, htf_trend TEXT,
            vol_pct REAL, atr REAL,
            ema9 REAL, ema21 REAL,
            streak INT, streak_dir INT, streak_rarity REAL,
            close_percentile REAL, z_body REAL,
            near_level TEXT, level_action TEXT,
            tick_count INT, buy_pct REAL, sell_pct REAL, pressure TEXT,
            net_move REAL, tick_speed_accel REAL,
            orderflow_imbalance INT, vap_migration TEXT,
            v_shape TEXT, momentum_shift TEXT, last_react TEXT,
            htf_aligned INT, regime_aligned INT,
            call_groups INT, put_groups INT, total_groups INT,
            net_margin REAL,
            signal_type TEXT,
            session_hour INT,
            session_name TEXT,
            reasons_json TEXT,
            modules_json TEXT
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bp_asset_period ON brain_predictions(asset, period)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bp_accuracy ON brain_predictions(accuracy)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bp_regime ON brain_predictions(regime, accuracy)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bp_asset_regime ON brain_predictions(asset, regime, accuracy)")
        # FIX (AUDIT-CORE #51, 2026-07-21): UNIQUE index on (asset, period, ctime)
        # so INSERT OR REPLACE in record_prediction() dedupes correctly. First
        # dedupe any existing duplicates so the index creation succeeds.
        try:
            cur.execute("""
                DELETE FROM brain_predictions WHERE id IN (
                    SELECT b1.id FROM brain_predictions b1
                    WHERE EXISTS (
                        SELECT 1 FROM brain_predictions b2
                        WHERE b2.asset = b1.asset
                          AND b2.period = b1.period
                          AND b2.ctime  = b1.ctime
                          AND b2.id     > b1.id
                    )
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_bp_asset_period_ctime
                ON brain_predictions(asset, period, ctime)
            """)
        except Exception as _e:
            print(f"[brain] could not create UNIQUE index on brain_predictions: {_e}")

        # ── brain_module_votes: per-module vote for each prediction ──
        cur.execute("""CREATE TABLE IF NOT EXISTS brain_module_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INT,
            ts REAL,
            asset TEXT, period INT,
            module_name TEXT,
            direction TEXT, score INT, confidence REAL,
            signal_type TEXT, reliability TEXT, signal_group TEXT,
            reason TEXT,
            actual TEXT, prediction_accuracy TEXT,
            module_correct INT,
            regime TEXT, htf_trend TEXT,
            FOREIGN KEY (prediction_id) REFERENCES brain_predictions(id)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bmv_module ON brain_module_votes(module_name)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bmv_asset_module ON brain_module_votes(asset, module_name)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bmv_module_regime ON brain_module_votes(module_name, regime)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bmv_module_correct ON brain_module_votes(module_name, module_correct)")

        # ── brain_patterns: discovered patterns ──
        cur.execute("""CREATE TABLE IF NOT EXISTS brain_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            pattern_type TEXT,
            description TEXT,
            condition_json TEXT,
            win_rate REAL,
            total INT, correct INT, wrong INT,
            impact TEXT,
            confidence REAL,
            action TEXT,
            applies_to TEXT
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bp_pattern_type ON brain_patterns(pattern_type)")

        # ── brain_insights: auto-generated solutions ──
        cur.execute("""CREATE TABLE IF NOT EXISTS brain_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            insight_type TEXT,
            title TEXT,
            description TEXT,
            evidence_json TEXT,
            recommendation TEXT,
            priority TEXT,
            status TEXT DEFAULT 'active',
            applies_to TEXT,
            confidence REAL,
            auto_generated INT DEFAULT 1
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bi_status ON brain_insights(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bi_priority ON brain_insights(priority, status)")
        # FIX (AUDIT-CORE #59, 2026-07-21): index on (insight_type, applies_to, title)
        # so insert_insight can dedupe against recent identical insights. Without
        # dedup, every analyze_and_learn call (every 50 graded signals) inserted
        # new rows for the same patterns, causing brain_insights to grow
        # unbounded with near-duplicate entries.
        try:
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_bi_dedup
                          ON brain_insights(insight_type, applies_to, title, ts DESC)""")
        except Exception:
            pass

        # ── brain_learning: learned weights per pair per module ──
        cur.execute("""CREATE TABLE IF NOT EXISTS brain_learning (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            asset TEXT, period INT,
            module_name TEXT,
            total INT, correct INT, wrong INT,
            win_rate REAL,
            call_total INT, call_correct INT,
            put_total INT, put_correct INT,
            recommended_weight REAL,
            recommended_score_adjustment REAL,
            notes TEXT
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bl_asset_module ON brain_learning(asset, module_name)")

        conn.commit()
        print("[brain] brain tables initialized")
    except Exception as e:
        print(f"[brain] init error: {e}")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
#  RECORD — save full prediction context
# ═══════════════════════════════════════════════════════════════════════════

def record_prediction(prediction: dict, asset: str, period: int,
                      ctime: int, actual: str, accuracy: str,
                      closed_candle: dict, micro: dict = None):
    """Record a complete prediction with full context.

    Called after each candle is graded. Saves:
    - Full prediction details (signal, confidence, strength, score)
    - Market context (regime, HTF, EMA, ATR, vol)
    - Microstructure (tick data, orderflow, VAP, etc.)
    - Per-module votes (what each module said and why)
    - Result (actual direction, win/loss)
    """
    conn = _conn()
    cur = conn.cursor()
    try:
        # Extract context from prediction
        regime = prediction.get("regime", {})
        reasons = prediction.get("reasons", [])
        modules = prediction.get("modules", {})

        # Extract microstructure data
        micro = micro or {}
        orderflow = micro.get("orderflow") or {}
        vap = micro.get("vap_migration") or {}
        last_vel = micro.get("last_velocity") or {}
        tick_speed = micro.get("tick_speed") or {}

        # Determine alignment
        signal = prediction.get("signal", "NEUTRAL")
        htf_trend = prediction.get("htf_trend", "SIDEWAYS")
        htf_aligned = 1 if (
            (htf_trend == "UPTREND" and signal == "CALL") or
            (htf_trend == "DOWNTREND" and signal == "PUT")
        ) else 0

        regime_name = regime.get("regime", "RANGE")
        regime_aligned = 1 if (
            (regime_name == "TREND_UP" and signal == "CALL") or
            (regime_name == "TREND_DOWN" and signal == "PUT")
        ) else 0

        # Session detection
        dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
        hour = dt.hour
        if 0 <= hour < 7: session = "ASIAN"
        elif 7 <= hour < 13: session = "LONDON"
        elif 13 <= hour < 17: session = "OVERLAP"
        elif 17 <= hour < 21: session = "NY"
        else: session = "LATE_NY"

        # Compute net margin
        score = prediction.get("score", 0)
        net_margin = abs(score) / 10.0 if score else 0  # rough estimate

        # Insert brain_predictions — using dict-based insert to avoid column mismatch
        pred_data = {
            "ts": time.time(),
            "asset": asset,
            "period": period,
            "ctime": ctime,
            "category": "otc" if asset.endswith("_otc") else "real",
            "signal": signal,
            "confidence": prediction.get("confidence", 0),
            "strength": prediction.get("strength", "NEUTRAL"),
            "score": prediction.get("score", 0),
            "actual": actual,
            "accuracy": accuracy,
            "regime": regime_name,
            "regime_strength": regime.get("trend_strength", 0),
            "htf_trend": htf_trend,
            "vol_pct": regime.get("volatility_pct", 1.0),
            "atr": 0,
            "ema9": regime.get("ema9", 0),
            "ema21": regime.get("ema21", 0),
            "streak": 0,
            "streak_dir": 0,
            "streak_rarity": 0,
            "close_percentile": 0,
            "z_body": 0,
            "near_level": "",
            "level_action": "",
            "tick_count": micro.get("tick_count", 0),
            "buy_pct": micro.get("buy_pct", 50),
            "sell_pct": micro.get("sell_pct", 50),
            "pressure": micro.get("pressure", "FIGHT"),
            "net_move": micro.get("net", 0),
            "tick_speed_accel": last_vel.get("accel", 1.0),
            "orderflow_imbalance": orderflow.get("imbalance", 0),
            "vap_migration": vap.get("dir", "FLAT"),
            "v_shape": micro.get("v_shape"),
            "momentum_shift": micro.get("momentum_shift"),
            "last_react": micro.get("last_react"),
            "htf_aligned": htf_aligned,
            "regime_aligned": regime_aligned,
            "call_groups": prediction.get("agree", 0),
            "put_groups": prediction.get("total", 0) - prediction.get("agree", 0),
            "total_groups": prediction.get("total", 0),
            "net_margin": net_margin,
            "signal_type": "CONTINUATION" if "continuation" in " ".join(reasons).lower() else "REVERSAL",
            "session_hour": hour,
            "session_name": session,
            "reasons_json": json.dumps(reasons),
            "modules_json": json.dumps(modules),
        }

        columns = ", ".join(pred_data.keys())
        placeholders = ", ".join(["?"] * len(pred_data))
        # FIX (AUDIT-CORE #51, 2026-07-21): use INSERT OR REPLACE to prevent
        # duplicate brain_predictions rows for the same (asset, period, ctime).
        # Previously a watchdog restart or double-EOC would insert duplicates,
        # which the brain's analyze_and_learn then double-counted in its
        # win-rate stats, producing wrong weight recommendations. The
        # brain_predictions table needs a UNIQUE(asset, period, ctime) index
        # for this to work — see init_brain().
        cur.execute(f"INSERT OR REPLACE INTO brain_predictions ({columns}) VALUES ({placeholders})",
                    list(pred_data.values()))
        pred_id = cur.lastrowid

        # Insert brain_module_votes
        for mname, mdata in modules.items():
            mdir = mdata.get("direction", "NEUTRAL")
            if mdir == "NEUTRAL":
                continue
            # Find the reason for this module
            m_reason = ""
            for r in reasons:
                r_str = str(r)
                if r_str.startswith(f"[{mname}]"):
                    m_reason = r_str
                    break

            # Determine if this module was correct
            actual_up = actual == "UP"
            pred_up = mdir == "CALL"
            module_correct = 1 if (pred_up == actual_up) else 0

            vote_data = {
                "prediction_id": pred_id,
                "ts": time.time(),
                "asset": asset,
                "period": period,
                "module_name": mname,
                "direction": mdir,
                "score": mdata.get("score", 0),
                "confidence": mdata.get("confidence", 0),
                "signal_type": "",
                "reliability": "",
                "signal_group": "",
                "reason": m_reason,
                "actual": actual,
                "prediction_accuracy": accuracy,
                "module_correct": module_correct,
                "regime": regime_name,
                "htf_trend": htf_trend,
            }
            v_cols = ", ".join(vote_data.keys())
            v_ph = ", ".join(["?"] * len(vote_data))
            cur.execute(f"INSERT INTO brain_module_votes ({v_cols}) VALUES ({v_ph})",
                       list(vote_data.values()))

        conn.commit()
    except Exception as e:
        print(f"[brain] record error: {e}")
    finally:
        conn.close()


# FIX (AUDIT-CORE #59, 2026-07-21): helper that dedupes brain_insights
# inserts. Checks if an active insight with the same (insight_type, title)
# already exists within the last 24h. If so, UPDATE its description /
# recommendation / priority / confidence (so the insight reflects the
# latest evidence) and skip the INSERT. This prevents brain_insights from
# growing unbounded with near-duplicate rows on every analyze_and_learn call.
_INSIGHT_DEDUP_WINDOW_SEC = 24 * 3600  # 24 hours

def _insert_insight_dedup(cur, insight_type, title, description,
                          recommendation, priority, confidence,
                          applies_to=None):
    """Insert a brain_insights row, deduping against recent identical insights.

    If an active insight with the same (insight_type, title) exists and was
    inserted within the last 24h, UPDATE its description / recommendation /
    priority / confidence in place instead of inserting a new row. This keeps
    brain_insights bounded — one row per (insight_type, title) per 24h window.
    """
    now = time.time()
    cutoff = now - _INSIGHT_DEDUP_WINDOW_SEC
    try:
        existing = cur.execute("""SELECT id FROM brain_insights
            WHERE insight_type = ? AND title = ? AND ts >= ?
            ORDER BY ts DESC LIMIT 1""",
            (insight_type, title, cutoff)).fetchone()
    except Exception:
        existing = None
    if existing:
        try:
            cur.execute("""UPDATE brain_insights
                SET ts = ?, description = ?, recommendation = ?,
                    priority = ?, confidence = ?, applies_to = ?, status = 'active'
                WHERE id = ?""",
                (now, description, recommendation, priority, confidence,
                 applies_to, existing["id"]))
        except Exception as _e:
            print(f"[brain] insight update failed: {_e}")
        return
    try:
        cur.execute("""INSERT INTO brain_insights (
            ts, insight_type, title, description,
            recommendation, priority, status, applies_to, confidence, auto_generated
        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, 1)""",
            (now, insight_type, title, description,
             recommendation, priority, applies_to, confidence))
    except Exception as _e:
        print(f"[brain] insight insert failed: {_e}")


# ═══════════════════════════════════════════════════════════════════════════
#  ANALYZE — find patterns and generate insights
# ═══════════════════════════════════════════════════════════════════════════

def analyze_and_learn(min_samples: int = 20):
    """Run brain analysis — find patterns, generate insights.

    Called periodically (every ~50 graded signals). Finds:
    1. Per-module win rates per pair (with recommendations)
    2. Regime-specific patterns (which conditions lead to losses)
    3. Session patterns (time-of-day win/loss clusters)
    4. Confidence calibration (which confidence levels are overconfident)
    5. Signal agreement patterns (when modules agree, are they right?)
    6. HTF alignment patterns
    """
    conn = _conn()
    cur = conn.cursor()
    try:
        _analyze_module_performance(cur, min_samples)
        _analyze_regime_patterns(cur, min_samples)
        _analyze_session_patterns(cur, min_samples)
        _analyze_confidence_calibration(cur, min_samples)
        _analyze_signal_agreement(cur, min_samples)
        _analyze_htf_patterns(cur, min_samples)
        _analyze_loss_clusters(cur)
        conn.commit()
        print("[brain] analysis complete")
    except Exception as e:
        print(f"[brain] analyze error: {e}")
    finally:
        conn.close()


def _analyze_module_performance(cur, min_samples):
    """Per-module per-pair win rates with recommendations."""
    rows = cur.execute("""SELECT asset, module_name,
        COUNT(*) as total,
        SUM(module_correct) as correct,
        SUM(CASE WHEN direction='CALL' THEN 1 ELSE 0 END) as call_total,
        SUM(CASE WHEN direction='CALL' AND module_correct=1 THEN 1 ELSE 0 END) as call_correct,
        SUM(CASE WHEN direction='PUT' THEN 1 ELSE 0 END) as put_total,
        SUM(CASE WHEN direction='PUT' AND module_correct=1 THEN 1 ELSE 0 END) as put_correct
        FROM brain_module_votes
        WHERE module_correct IS NOT NULL
        GROUP BY asset, module_name
        HAVING total >= ?""", (min_samples,)).fetchall()

    for row in rows:
        wr = row["correct"] / row["total"] if row["total"] > 0 else 0
        call_wr = row["call_correct"] / row["call_total"] if row["call_total"] > 0 else 0
        put_wr = row["put_correct"] / row["put_total"] if row["put_total"] > 0 else 0

        # Recommended weight adjustment.
        # FIX (AUDIT-CORE #17, 2026-07-21): the previous if/elif chain had
        # `elif wr > 0.60` AFTER `elif wr > 0.55`, which made the 0.60
        # branch UNREACHABLE — any wr > 0.60 also satisfies wr > 0.55, so
        # the BOOST_STRONG (×1.5) tier never fired. Reordered so the
        # higher threshold is checked first. Excellent modules (>60% win
        # rate) now correctly get ×1.5 instead of ×1.3.
        if wr > 0.60:
            rec_weight = 1.5
            action = "BOOST_STRONG"
            priority = "HIGH"
            notes = f"Module excellent ({wr:.0%}). Recommend weight ×1.5."
        elif wr > 0.55:
            rec_weight = 1.3
            action = "BOOST"
            priority = "MEDIUM"
            notes = f"Module overperforming ({wr:.0%}). Recommend weight ×1.3."
        elif wr < 0.40:
            rec_weight = 0.5
            action = "DAMPEN_SEVERE"
            priority = "HIGH"
            notes = f"Module performing badly ({wr:.0%}). Recommend weight ×0.5."
        elif wr < 0.45:
            rec_weight = 0.7
            action = "DAMPEN"
            priority = "MEDIUM"
            notes = f"Module underperforming ({wr:.0%}). Recommend weight ×0.7."
        else:
            rec_weight = 1.0
            action = "NORMAL"
            priority = "LOW"
            notes = f"Module normal ({wr:.0%})."

        # Direction bias note
        if abs(call_wr - put_wr) > 0.15:
            bias = "CALL" if call_wr > put_wr else "PUT"
            notes += f" {bias} bias: CALL={call_wr:.0%}, PUT={put_wr:.0%}."

        # Upsert brain_learning
        cur.execute("""INSERT OR REPLACE INTO brain_learning (
            ts, asset, period, module_name,
            total, correct, wrong,
            win_rate, call_total, call_correct,
            put_total, put_correct,
            recommended_weight, recommended_score_adjustment,
            notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), row["asset"], 60, row["module_name"],
             row["total"], row["correct"], row["total"] - row["correct"],
             wr, row["call_total"], row["call_correct"],
             row["put_total"], row["put_correct"],
             rec_weight, rec_weight - 1.0, notes))

        # Generate insight for severe cases
        if wr < 0.40 or wr > 0.60:
            insight_type = "MODULE_PERFORMANCE"
            title = f"{row['module_name']} on {row['asset']}: {wr:.0%} win rate"
            desc = f"Module {row['module_name']} has {wr:.0%} win rate on {row['asset']} "
            desc += f"({row['correct']}/{row['total']} correct). "
            desc += f"CALL: {call_wr:.0%} ({row['call_correct']}/{row['call_total']}), "
            desc += f"PUT: {put_wr:.0%} ({row['put_correct']}/{row['put_total']}). "
            desc += f"Recommendation: {action} (weight ×{rec_weight})."

            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, insight_type, title, desc,
                f"Set {row['module_name']} weight to {rec_weight} for {row['asset']}",
                priority, wr, applies_to=row['asset'])


def _analyze_regime_patterns(cur, min_samples):
    """Find regime-specific patterns."""
    rows = cur.execute("""SELECT regime, accuracy,
        COUNT(*) as total
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        GROUP BY regime, accuracy""").fetchall()

    regime_totals = defaultdict(lambda: {"correct": 0, "wrong": 0})
    for row in rows:
        regime_totals[row["regime"]][row["accuracy"]] = row["total"]

    for regime, counts in regime_totals.items():
        total = counts["correct"] + counts["wrong"]
        if total < min_samples:
            continue
        wr = counts["correct"] / total
        if wr < 0.45:
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'REGIME_PATTERN',
                f"Low accuracy in {regime}: {wr:.0%}",
                f"Engine wins only {wr:.0%} in {regime} regime ({counts['correct']}/{total}). "
                f"This regime is problematic — consider skipping signals or inverting logic.",
                f"Apply confidence penalty in {regime} regime",
                'HIGH', wr, applies_to=regime)


def _analyze_session_patterns(cur, min_samples):
    """Find time-of-day patterns."""
    rows = cur.execute("""SELECT session_name, session_hour,
        COUNT(*) as total,
        SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) as correct
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        GROUP BY session_name
        HAVING total >= ?""", (min_samples,)).fetchall()

    for row in rows:
        wr = row["correct"] / row["total"] if row["total"] > 0 else 0
        if wr < 0.45:
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'SESSION_PATTERN',
                f"Low accuracy in {row['session_name']} session: {wr:.0%}",
                f"During {row['session_name']} session, win rate is only {wr:.0%} "
                f"({row['correct']}/{row['total']}). Market behavior may be different.",
                f"Reduce confidence in {row['session_name']} session or skip signals",
                'MEDIUM', wr, applies_to=row['session_name'])


def _analyze_confidence_calibration(cur, min_samples):
    """Find overconfident/underconfident bins."""
    rows = cur.execute("""SELECT
        CAST(confidence/10 AS INT)*10 as conf_bin,
        COUNT(*) as total,
        SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) as correct
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        GROUP BY conf_bin
        HAVING total >= ?""", (min_samples,)).fetchall()

    for row in rows:
        wr = row["correct"] / row["total"] if row["total"] > 0 else 0
        bin_mid = row["conf_bin"] + 5
        if wr < bin_mid / 100.0 - 0.10:
            # Significantly overconfident
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'CONFIDENCE_CALIBRATION',
                f"Overconfident at {row['conf_bin']}-{row['conf_bin']+9}%: actual {wr:.0%}",
                f"Predictions with confidence {row['conf_bin']}-{row['conf_bin']+9}% "
                f"only win {wr:.0%} ({row['correct']}/{row['total']}). "
                f"Expected ~{bin_mid}%. Engine is overconfident in this range.",
                f"Cap confidence at {row['conf_bin']-10}% for this bin",
                'HIGH', wr, applies_to=f"conf_{row['conf_bin']}")


def _analyze_signal_agreement(cur, min_samples):
    """When modules agree vs disagree, who's right?"""
    rows = cur.execute("""SELECT
        module_name,
        SUM(CASE WHEN direction = (
            SELECT signal FROM brain_predictions bp
            WHERE bp.id = brain_module_votes.prediction_id
        ) THEN 1 ELSE 0 END) as agreed_with_final,
        SUM(CASE WHEN module_correct = 1 THEN 1 ELSE 0 END) as module_correct_count,
        COUNT(*) as total
        FROM brain_module_votes
        WHERE module_correct IS NOT NULL
        GROUP BY module_name
        HAVING total >= ?""", (min_samples,)).fetchall()

    for row in rows:
        if row["total"] < min_samples:
            continue
        wr = row["module_correct_count"] / row["total"]
        agree_rate = row["agreed_with_final"] / row["total"]

        # If module has high win rate but low agreement, it's a contrarian indicator
        if wr > 0.55 and agree_rate < 0.40:
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'CONTRARIAN_MODULE',
                f"{row['module_name']} is a contrarian indicator",
                f"Module {row['module_name']} has {wr:.0%} win rate but only "
                f"agrees with final {agree_rate:.0%} of the time. "
                f"When it disagrees, it's often RIGHT. Consider boosting its vote.",
                f"Increase {row['module_name']} weight when it disagrees with majority",
                'HIGH', wr, applies_to=row['module_name'])


def _analyze_htf_patterns(cur, min_samples):
    """HTF alignment patterns."""
    rows = cur.execute("""SELECT htf_trend, htf_aligned, accuracy,
        COUNT(*) as total
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        GROUP BY htf_trend, htf_aligned, accuracy""").fetchall()

    htf_stats = defaultdict(lambda: {"correct": 0, "wrong": 0})
    for row in rows:
        key = f"{row['htf_trend']}_{'aligned' if row['htf_aligned'] else 'counter'}"
        htf_stats[key][row["accuracy"]] = row["total"]

    for key, counts in htf_stats.items():
        total = counts["correct"] + counts["wrong"]
        if total < min_samples:
            continue
        wr = counts["correct"] / total
        if wr < 0.45:
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'HTF_PATTERN',
                f"Low accuracy when HTF {key}: {wr:.0%}",
                f"When HTF trend is {key}, win rate is only {wr:.0%} "
                f"({counts['correct']}/{total}).",
                f"Reduce confidence or skip when HTF is {key}",
                'MEDIUM', wr, applies_to=key)


def _analyze_loss_clusters(cur):
    """Find clusters of consecutive losses per pair."""
    rows = cur.execute("""SELECT asset, ctime, accuracy
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        ORDER BY asset, ctime""").fetchall()

    # Find consecutive loss streaks
    current_streak = 0
    max_streak = 0
    streak_start = None
    current_asset = None

    for row in rows:
        if row["asset"] != current_asset:
            current_asset = row["asset"]
            current_streak = 0
            max_streak = 0

        if row["accuracy"] == "wrong":
            if current_streak == 0:
                streak_start = row["ctime"]
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            if current_streak >= 5:
                # Record loss cluster
                # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
                _insert_insight_dedup(cur, 'LOSS_CLUSTER',
                    f"{current_asset}: {current_streak} consecutive losses",
                    f"Pair {current_asset} had {current_streak} consecutive losses "
                    f"starting at {datetime.fromtimestamp(streak_start).isoformat()}. "
                    f"This may indicate a regime shift or broker pattern change.",
                    f"Skip {current_asset} for 30 minutes or reduce confidence",
                    'HIGH', 0.0, applies_to=current_asset)
            current_streak = 0


# ═══════════════════════════════════════════════════════════════════════════
#  QUERY — retrieve brain data for API
# ═══════════════════════════════════════════════════════════════════════════

def get_insights(active_only: bool = True, limit: int = 50):
    """Get brain insights (recommendations)."""
    conn = _conn()
    try:
        query = "SELECT * FROM brain_insights"
        if active_only:
            query += " WHERE status = 'active'"
        query += " ORDER BY ts DESC LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_learning(asset: str = None, limit: int = 100):
    """Get learned weights per pair per module."""
    conn = _conn()
    try:
        if asset:
            rows = conn.execute(
                "SELECT * FROM brain_learning WHERE asset = ? ORDER BY win_rate DESC LIMIT ?",
                (asset, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM brain_learning ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# FIX (DEAD-CODE-2026-07-21): removed get_patterns() — never called by any
# endpoint or internal code. The /api/brain/insights endpoint uses
# get_insights() instead.


def get_brain_summary():
    """Get a summary of brain activity."""
    conn = _conn()
    try:
        total_preds = conn.execute(
            "SELECT COUNT(*) FROM brain_predictions").fetchone()[0]
        total_correct = conn.execute(
            "SELECT COUNT(*) FROM brain_predictions WHERE accuracy='correct'").fetchone()[0]
        total_wrong = conn.execute(
            "SELECT COUNT(*) FROM brain_predictions WHERE accuracy='wrong'").fetchone()[0]
        total_insights = conn.execute(
            "SELECT COUNT(*) FROM brain_insights WHERE status='active'").fetchone()[0]
        total_learning = conn.execute(
            "SELECT COUNT(*) FROM brain_learning").fetchone()[0]

        acc = total_correct / (total_correct + total_wrong) * 100 if (total_correct + total_wrong) > 0 else 0

        return {
            "total_predictions": total_preds,
            "correct": total_correct,
            "wrong": total_wrong,
            "accuracy": round(acc, 2),
            "active_insights": total_insights,
            "learned_weights": total_learning,
            "brain_active": total_preds > 0,
        }
    finally:
        conn.close()
