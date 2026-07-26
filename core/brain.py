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
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

# FIX (DEEP-AUDIT-2026-07-26 / F-05-09, F-05-10): removed unused imports
# `contextmanager` (from contextlib) and `threading` (only used to construct
# the dead `_lock` global). Neither was ever referenced. The `_lock` was
# declared at module scope but never acquired/released, giving a false sense
# of thread safety on a module that opens brand-new sqlite3 connections per
# call. Either use it or remove it — we remove it.

# FIX (DEEP-AUDIT-2026-07-26 / F-05-68): DB_PATH default resolved via
# `os.path.dirname(__file__)` which may be empty/None when brain.py is loaded
# from a .pyc or zip. Use os.path.abspath to guarantee a concrete path.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..", "signals.db")),
)

# FIX (DEEP-AUDIT-2026-07-26 / F-05-69): connection timeout magic 10s → named
# module-level constant so it is greppable and tunable.
_DB_TIMEOUT_SEC = 10

# FIX (DEEP-AUDIT-2026-07-26 / F-05-36): documented module-level constant for
# the 24h insight dedup window (was already a constant but undocumented).
_INSIGHT_DEDUP_WINDOW_SEC = 24 * 3600  # 24 hours

# FIX (DEEP-AUDIT-2026-07-26 / F-05-38, F-05-39, F-05-42, F-05-43, F-05-44):
# Hoist recommendation / pattern thresholds out of function bodies into
# named module-level constants so they are tunable without editing the
# function body. Previously these were scattered magic numbers (0.60, 0.55,
# 0.45, 0.40, 0.15, 0.10, 0.55, 0.40, 0.45).
_WR_BOOST_STRONG = 0.60   # win rate above which module weight ×1.5
_WR_BOOST = 0.55          # win rate above which module weight ×1.3
_WR_DAMPEN = 0.45         # win rate below which module weight ×0.7
_WR_DAMPEN_SEVERE = 0.40  # win rate below which module weight ×0.5
_WR_DIRECTION_BIAS = 0.15  # |call_wr - put_wr| above which a bias note fires
_WR_LOW_REGIME = 0.45     # regime/session/HTF win rate below which we warn
_OVERCONFIDENCE_MARGIN = 0.10  # how far below bin midpoint = "overconfident"
_CONTRARIAN_WR_MIN = 0.55    # win rate above which a low-agreement module is contrarian
_CONTRARIAN_AGREE_MAX = 0.40 # agreement rate below which a high-WR module is contrarian
_CONFIDENCE_BIN_WIDTH = 10  # bucket size for confidence-calibration analysis
_MIN_LOSS_STREAK = 5       # consecutive losses required to log a LOSS_CLUSTER insight

# FIX (DEEP-AUDIT-2026-07-26 / F-05-07): rolling window for the
# `_analyze_module_performance` query. Without this, the brain weighted a
# module's recommendations by its entire history — modules whose behavior
# changed months ago kept their stale weights. Now restricted to last N days.
_MODULE_PERF_LOOKBACK_DAYS = 14
_MODULE_PERF_LOOKBACK_SEC = _MODULE_PERF_LOOKBACK_DAYS * 24 * 3600


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT_SEC)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as _e:
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-66): bare `except: pass` swallowed
        # PRAGMA failures (e.g., read-only filesystem, permission denied). At
        # least log so the operator can detect a misconfigured DB path.
        print(f"[brain] PRAGMA setup warning: {_e}")
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
        # FIX (DEEP-AUDIT-2026-07-26 / A-03-CRIT-4):
        # The previous dedup deleted duplicate brain_predictions rows but left
        # their brain_module_votes rows orphaned. Those orphan votes still
        # appeared in JOIN-based analysis queries (e.g. _analyze_module_performance
        # joins brain_module_votes on prediction_id), distorting win-rate
        # calculations for modules whose old (incorrect) votes were never cleared.
        # Now: after deleting dup brain_predictions rows, also delete orphaned
        # brain_module_votes rows whose prediction_id no longer exists.
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
            # FIX (A-03-CRIT-4): clean up orphaned votes from the rows we just deleted.
            #
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-99): the orphan-votes DELETE
            # was INSIDE the same try block as the CREATE UNIQUE INDEX below.
            # On a fresh DB where `brain_module_votes` doesn't exist yet (it's
            # created later in this function), the DELETE throws "no such
            # table", which triggers the except and SKIPS the CREATE UNIQUE
            # INDEX. That means the UNIQUE(asset, period, ctime) index was
            # silently NOT created on every fresh DB — defeating the entire
            # purpose of the dedup block (record_prediction's INSERT OR REPLACE
            # needs this index to dedupe correctly). Now: wrap the orphan
            # DELETE in its own try/except so a missing brain_module_votes
            # table doesn't prevent the UNIQUE INDEX from being created.
            try:
                cur.execute("""
                    DELETE FROM brain_module_votes
                    WHERE prediction_id NOT IN (
                        SELECT id FROM brain_predictions
                    )
                """)
            except Exception as _oe:
                # brain_module_votes table may not exist yet on a fresh DB
                # — safe to ignore; it gets created below.
                print(f"[brain] orphan-vote cleanup skipped: {_oe}")
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
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-67): the bare `except: pass`
        # swallowed index-creation failures. If the UNIQUE index fails to
        # create (e.g., existing duplicates the dedup DELETE couldn't reach),
        # downstream `INSERT OR REPLACE` becomes a no-op (no conflict to
        # resolve) and rows accumulate. Now: log so the operator can detect
        # the schema drift before it silently breaks dedup.
        try:
            cur.execute("""CREATE INDEX IF NOT EXISTS ix_bi_dedup
                          ON brain_insights(insight_type, applies_to, title, ts DESC)""")
        except Exception as _e:
            print(f"[brain] could not create ix_bi_dedup index: {_e}")

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
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-08): add a UNIQUE constraint
        # on (asset, module_name, period) so the `INSERT OR REPLACE` below
        # actually upserts (updates in place) instead of accumulating a new
        # row per analyze_and_learn call (~every 50 graded signals). Without
        # this constraint, `INSERT OR REPLACE` is just `INSERT`, and the
        # brain_learning table grew ~100x faster than intended — bloating
        # the DB and causing stale weights to coexist with new ones. We
        # dedupe any existing duplicates first so the index creation succeeds.
        try:
            cur.execute("""
                DELETE FROM brain_learning WHERE id IN (
                    SELECT a1.id FROM brain_learning a1
                    WHERE EXISTS (
                        SELECT 1 FROM brain_learning a2
                        WHERE a2.asset = a1.asset
                          AND a2.module_name = a1.module_name
                          AND a2.period = a1.period
                          AND a2.id > a1.id
                    )
                )
            """)
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_bl_asset_module_period
                ON brain_learning(asset, module_name, period)""")
        except Exception as _e:
            print(f"[brain] could not create UNIQUE index on brain_learning: {_e}")

        conn.commit()
        print("[brain] brain tables initialized")
    except Exception as e:
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-20): the old code printed the
        # exception and returned NORMALLY — so the server believed brain was
        # initialized when in reality NO tables existed. Subsequent
        # record_prediction calls failed individually (also silently) and the
        # operator had no way to tell. Now: log the error AND re-raise so
        # the caller (server.py lifespan) can detect the failure and refuse
        # to start. Defensive catch-alls that swallow startup errors are
        # exactly how production systems end up running with a broken DB.
        print(f"[brain] init error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
#  RECORD — save full prediction context
# ═══════════════════════════════════════════════════════════════════════════

# FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-07 + AUDIT-LIVE-1-02): derive
# signal_type from per-module `signal_type` metadata (passed in the modules
# dict) rather than substring-matching reason text. Reason strings routinely
# contain BOTH "continuation" and "reversal" (e.g. blender's metadata
# "_REGIME: TREND_UP → continuation ×1.3, reversal ×0.8") which corrupted
# the old substring check, tagging nearly every TREND_UP prediction as
# "CONTINUATION". This helper takes the majority signal_type across modules
# that voted in the final direction; defaults to REVERSAL if no signal_type
# metadata is present (preserving old behavior for legacy callers).
def _derive_signal_type(modules: dict, reasons: list) -> str:
    """Return 'CONTINUATION' or 'REVERSAL' based on per-module metadata.

    Counts each module's `signal_type` field (if present). If most voting
    modules tag themselves as continuation, return 'CONTINUATION'. Otherwise
    return 'REVERSAL'. Falls back to the old substring check when no module
    carries a `signal_type` field (backward compat with legacy callers).
    """
    try:
        cont = 0
        rev = 0
        has_meta = False
        if isinstance(modules, dict):
            for _name, mdata in modules.items():
                if not isinstance(mdata, dict):
                    continue
                st = mdata.get("signal_type")
                if not st:
                    continue
                has_meta = True
                st_upper = str(st).upper()
                if "CONTINUATION" in st_upper:
                    cont += 1
                elif "REVERSAL" in st_upper:
                    rev += 1
        if has_meta:
            return "CONTINUATION" if cont >= rev else "REVERSAL"
    except Exception:
        pass
    # Legacy fallback: substring scan (only used if no per-module metadata).
    return "CONTINUATION" if "continuation" in " ".join(reasons).lower() else "REVERSAL"


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

        # Determine alignment.
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-32, F-05-70): the original code
        # only matched exact strings "UPTREND"/"DOWNTREND" and "TREND_UP"/
        # "TREND_DOWN". Engines also emit "STRONG_UP"/"STRONG_DOWN" and
        # lowercase variants — those were silently classified as NOT aligned.
        # Now: substring match so "STRONG_UPTREND" / "uptrend" both align.
        signal = prediction.get("signal", "NEUTRAL")
        htf_trend = prediction.get("htf_trend", "SIDEWAYS") or "SIDEWAYS"
        htf_trend_u = str(htf_trend).upper()
        htf_up = "UP" in htf_trend_u or "BULL" in htf_trend_u
        htf_dn = "DOWN" in htf_trend_u or "BEAR" in htf_trend_u
        htf_aligned = 1 if (
            (htf_up and signal == "CALL") or
            (htf_dn and signal == "PUT")
        ) else 0

        regime_name = regime.get("regime", "RANGE") or "RANGE"
        regime_name_u = str(regime_name).upper()
        reg_up = "TREND_UP" in regime_name_u or regime_name_u.endswith("_UP")
        reg_dn = "TREND_DOWN" in regime_name_u or regime_name_u.endswith("_DOWN")
        regime_aligned = 1 if (
            (reg_up and signal == "CALL") or
            (reg_dn and signal == "PUT")
        ) else 0

        # Session detection
        dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
        hour = dt.hour
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-14): rename 21-24 UTC bucket
        # from "LATE_NY" to "ASIA_OPEN". NY session closes at 21:00-22:00 UTC;
        # 21-24 UTC is actually the Sydney/Tokyo pre-open (Asian session start).
        # The old label misled session-pattern insights ("skip LATE_NY" when the
        # real issue is the Asia pre-open).
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-31): `0 <= hour` is always True
        # (hour is 0-23 from dt.hour). The dead `0 <=` half of the chained
        # comparison was confusing. Simplified to `hour < 7`.
        if hour < 7: session = "ASIAN"
        elif hour < 13: session = "LONDON"
        elif hour < 17: session = "OVERLAP"
        elif hour < 21: session = "NY"
        else: session = "ASIA_OPEN"

        # Compute net margin
        #
        # FIX (AUDIT-DEEP-A3, 2026-07-23): the previous `if score else 0`
        # is a truthy check — for `score=0` (NEUTRAL predictions or a
        # perfect tie), Python treats 0 as falsy and the else branch
        # returns 0 anyway. The bug was SEMANTIC not functional: the
        # expression `abs(score) / 10.0` already produces 0.0 when
        # score=0, so the if/else is redundant. Worse, `score=None`
        # (defensive: prediction.get("score", 0) returns 0, but if a
        # caller passed a dict with explicit None) would crash on
        # abs(None). Now explicit `is None` check handles None cleanly.
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-30, F-05-71): reuse the guarded
        # `score` local in pred_data instead of calling
        # `prediction.get("score", 0)` again. The old code fetched `score`
        # TWICE — once with default None (guarded to 0) and once with
        # default 0 in pred_data — which meant a caller passing
        # `score=None` explicitly would write None into the DB column
        # instead of 0. Now: single source of truth, always 0 for None.
        score = prediction.get("score")
        if score is None:
            score = 0
        try:
            net_margin = abs(score) / 10.0
        except (TypeError, ValueError):
            net_margin = 0

        # Brain-predictions stat columns (streak / z_body / close_percentile).
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-19): these were previously
        # hardcoded to 0. The caller may include a `stats` sub-dict in the
        # prediction payload (engines/base/blender threads `ctx.stats` in);
        # if so we read from it. Otherwise fall back to 0 (backward compatible
        # with older callers that don't pass stats).
        _stats = prediction.get("stats") or {}
        _streak = _stats.get("current_streak", 0) if isinstance(_stats, dict) else 0
        _streak_dir = _stats.get("streak_direction", 0) if isinstance(_stats, dict) else 0
        _streak_rarity = _stats.get("streak_rarity", 0) if isinstance(_stats, dict) else 0
        _close_pctile = _stats.get("close_percentile", 0) if isinstance(_stats, dict) else 0
        _z_body = _stats.get("z_body", 0) if isinstance(_stats, dict) else 0

        # FIX (DEEP-AUDIT-2026-07-26 / F-05-74, F-05-27): the `agree`/`total`
        # values were fetched up to 3 separate times via repeated
        # `prediction.get("agree", 0)` / `prediction.get("total", 0)` calls
        # embedded in two hard-to-read nested ternaries for `call_groups`
        # vs `put_groups`. Now: fetched ONCE into locals, and the assignment
        # is an explicit if/else so the semantic ("agree = votes for final
        # signal direction") is clear.
        agree_votes = prediction.get("agree", 0) or 0
        total_votes = prediction.get("total", 0) or 0
        disagree_votes = max(0, total_votes - agree_votes)
        if signal == "CALL":
            call_groups = agree_votes
            put_groups = disagree_votes
        else:
            # Includes NEUTRAL / PUT — `agree` is votes for final direction.
            call_groups = disagree_votes
            put_groups = agree_votes

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
            "score": score,
            "actual": actual,
            "accuracy": accuracy,
            "regime": regime_name,
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-72): document the magic
            # defaults — trend_strength=0 (no trend) and volatility_pct=1.0
            # (average volatility). When the regime dict is missing keys,
            # these defaults are intentional "neutral" placeholders so the
            # brain doesn't bias stats when the caller forgot to populate
            # the regime dict.
            "regime_strength": regime.get("trend_strength", 0),
            "htf_trend": htf_trend,
            "vol_pct": regime.get("volatility_pct", 1.0),
            "atr": prediction.get("atr", 0),
            "ema9": regime.get("ema9", 0),
            "ema21": regime.get("ema21", 0),
            "streak": _streak,
            "streak_dir": _streak_dir,
            "streak_rarity": _streak_rarity,
            "close_percentile": _close_pctile,
            "z_body": _z_body,
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-73): `near_level` /
            # `level_action` are kept as empty strings because removing
            # them would require a schema migration. They are reserved for
            # future use when the caller threads key-level context in.
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
            "call_groups": call_groups,
            "put_groups": put_groups,
            "total_groups": total_votes,
            "net_margin": net_margin,
            "signal_type": _derive_signal_type(modules, reasons),
            "session_hour": hour,
            "session_name": session,
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-75): add `default=str` so a
            # non-JSON-serializable value (e.g., datetime) doesn't crash
            # record_prediction. Previously the entire record failed.
            "reasons_json": json.dumps(reasons, default=str),
            "modules_json": json.dumps(modules, default=str),
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
        #
        # FIX (P1-ISSUE-052, 2026-07-22): INSERT OR REPLACE deletes the old
        # row (including its id) before inserting a new one. This orphans
        # brain_module_votes rows that referenced the old prediction_id.
        # Over time, brain_module_votes accumulates orphaned rows that
        # distort the analysis queries. Now: explicitly delete old votes
        # for the same (asset, period, ctime) BEFORE the REPLACE, so the
        # new prediction_id gets fresh votes.
        try:
            old_pred = cur.execute(
                "SELECT id FROM brain_predictions WHERE asset=? AND period=? AND ctime=?",
                (asset, period, ctime)).fetchone()
            if old_pred:
                cur.execute("DELETE FROM brain_module_votes WHERE prediction_id=?",
                            (old_pred["id"],))
        except Exception as _e:
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-21, F-05-34): the old comment
            # "table may not exist on first run" is misleading — init_brain()
            # runs on startup and creates all tables BEFORE record_prediction
            # can ever be called. So this except catches REAL bugs (schema
            # drift, locked DB, permission denied) which the silent pass was
            # hiding. Now: log so the operator can detect the failure.
            print(f"[brain] pre-replace vote cleanup failed: {_e}")
        cur.execute(f"INSERT OR REPLACE INTO brain_predictions ({columns}) VALUES ({placeholders})",
                    list(pred_data.values()))
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-12): `cur.lastrowid` semantics for
        # `INSERT OR REPLACE` are version-dependent — older Python builds
        # returned the PREVIOUS row's lastrowid, newer ones return the new
        # row's. Relying on it meant votes were sometimes written against
        # the wrong prediction_id on certain Python builds, corrupting
        # brain_module_votes JOINs. Now: explicitly SELECT the id by the
        # UNIQUE (asset, period, ctime) tuple so we get the right one
        # regardless of Python/sqlite3 version.
        row = cur.execute(
            "SELECT id FROM brain_predictions WHERE asset=? AND period=? AND ctime=?",
            (asset, period, ctime)).fetchone()
        pred_id = row["id"] if row else cur.lastrowid

        # Insert brain_module_votes
        for mname, mdata in modules.items():
            mdir = mdata.get("direction", "NEUTRAL")
            if mdir == "NEUTRAL":
                continue
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-11): `module_correct` previously
            # treated ANY non-"UP" actual value (FLAT, NEUTRAL, DOJI, DOWN) as
            # DOWN — so a flat candle caused CALL votes to be marked wrong
            # and PUT votes right, biasing brain_learning toward PUT during
            # flat periods. Now: skip the vote row entirely when the actual
            # direction is not CALL/PUT (i.e., not UP/DOWN). The
            # _analyze_module_performance query already filters
            # `WHERE module_correct IS NOT NULL`, so skipped votes don't
            # contaminate the win-rate stats.
            if actual not in ("UP", "DOWN"):
                continue

            # Find the reason(s) for this module.
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-33, F-05-35): the old lookup
            # was (a) case-sensitive — `mname="RSI"` wouldn't match reason
            # text `"[rsi] ..."` — and (b) first-match-only — if a module
            # emitted MULTIPLE reasons, only the first was recorded, so the
            # brain couldn't analyze secondary reasons. Now: case-insensitive
            # match, and concatenate ALL matching reasons with " | ".
            m_reason_parts = []
            mname_lower = str(mname).lower()
            mname_prefix = f"[{mname_lower}]"
            for r in reasons:
                r_str = str(r)
                if r_str.lower().startswith(mname_prefix):
                    m_reason_parts.append(r_str)
            m_reason = " | ".join(m_reason_parts) if m_reason_parts else ""

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
                # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-LIVE-1-02): populate
                # signal_type/reliability/signal_group from per-module metadata
                # so the brain can analyze per-(module, signal_type) win rates.
                # Previously these were empty strings — the brain couldn't tell
                # continuation votes from reversal votes.
                "signal_type": str(mdata.get("signal_type", "") or ""),
                "reliability": str(mdata.get("reliability", "") or ""),
                "signal_group": str(mdata.get("signal_group", "") or ""),
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-13, F-05-76): the old code
        # closed the connection in `finally` WITHOUT rolling back the
        # transaction. On exception (e.g., a partial insert of
        # brain_predictions row but half-failed brain_module_votes
        # inserts), the partial inserts were COMMITTED implicitly when
        # the connection closed — leaving the DB in an inconsistent
        # state. Now: explicit rollback. Also log the exception so the
        # caller (feed.py) can detect the failure (the old `print(...)`
        # swallowed it silently and the engine kept churning as if the
        # prediction was recorded).
        print(f"[brain] record error: {e}")
        try:
            conn.rollback()
        except Exception as _rb_err:
            print(f"[brain] record rollback failed: {_rb_err}")
    finally:
        conn.close()


# FIX (AUDIT-CORE #59, 2026-07-21): helper that dedupes brain_insights
# inserts. Checks if an active insight with the same (insight_type, title)
# already exists within the last 24h. If so, UPDATE its description /
# recommendation / priority / confidence (so the insight reflects the
# latest evidence) and skip the INSERT. This prevents brain_insights from
# growing unbounded with near-duplicate rows on every analyze_and_learn call.
#
# FIX (DEEP-AUDIT-2026-07-26 / F-05-08): the original dedup keyed on the
# FULL title string, but every insight title embeds a dynamic value
# (win-rate %, streak count, regime name). As those values changed between
# analyzer runs, the title string changed, the dedup never matched, and
# brain_insights accumulated near-duplicate rows — exactly the bug the
# helper was meant to prevent. Now: callers pass an additional `dedup_title`
# (a stable form of the title with the variable numbers stripped). When
# `dedup_title` is provided we dedup against it instead of the display
# title, while still inserting the dynamic title for human display.
# `_normalize_insight_title` produces a default stable key when a caller
# does not pass `dedup_title` explicitly (regex-strips percentages, "N
# consecutive", and ISO timestamps).

# Constant _INSIGHT_DEDUP_WINDOW_SEC is defined at the top of the module
# alongside the other tuning constants (F-05-36).

# Regex patterns for stripping variable numeric tokens out of insight titles.
# We compile once at module import (cheap, only done once).
_INSIGHT_PCT_RE = re.compile(r"\s*\d+(?:\.\d+)?%")
_INSIGHT_STREAK_RE = re.compile(r":\s*\d+\s+consecutive")
_INSIGHT_ISO_TS_RE = re.compile(r"\s+at\s+\d{4}-\d{2}-\d{2}T[\d:.\-+Z]+")


def _normalize_insight_title(title: str) -> str:
    """Return a stable dedup key for an insight title.

    Strips variable numeric / timestamp tokens so that titles like
    "RSI on EURUSD: 50% win rate" and "RSI on EURUSD: 60% win rate" map to
    the same dedup key "RSI on EURUSD: win rate". The dynamic numbers are
    preserved in the insight `description` so the API consumer still sees
    the latest values.
    """
    if not title:
        return ""
    s = _INSIGHT_ISO_TS_RE.sub("", title)
    s = _INSIGHT_STREAK_RE.sub(": consecutive", s)
    s = _INSIGHT_PCT_RE.sub("", s)
    # Collapse the double spaces left behind by stripping percentages.
    s = re.sub(r"\s{2,}", " ", s).strip()
    # Trailing "win rate" with leading colon — normalize.
    s = s.replace(": win rate", " win rate").replace(":  win rate", " win rate")
    return s


def _insert_insight_dedup(cur, insight_type, title, description,
                          recommendation, priority, confidence,
                          applies_to=None, dedup_title=None):
    """Insert a brain_insights row, deduping against recent identical insights.

    If an active insight with the same (insight_type, dedup_title) exists and
    was inserted within the last 24h, UPDATE its description / recommendation /
    priority / confidence (and display `title`) in place instead of inserting
    a new row. This keeps brain_insights bounded — one row per
    (insight_type, dedup_title) per 24h window.

    `dedup_title` is a stable form of the title with variable numbers stripped
    (see `_normalize_insight_title`). When omitted, the title is normalized
    automatically.
    """
    now = time.time()
    cutoff = now - _INSIGHT_DEDUP_WINDOW_SEC
    dedup_key = dedup_title if dedup_title is not None else _normalize_insight_title(title)
    try:
        existing = cur.execute("""SELECT id FROM brain_insights
            WHERE insight_type = ? AND title = ? AND ts >= ?
            ORDER BY ts DESC LIMIT 1""",
            (insight_type, dedup_key, cutoff)).fetchone()
    except Exception as _e:
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-21): silent except-pass hid
        # schema-drift bugs. Log so we can detect a broken brain_insights
        # table before it silently drops every insight.
        print(f"[brain] dedup SELECT failed ({insight_type}): {_e}")
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): store the STABLE dedup_key
        # as the title (not the dynamic display title) so subsequent dedup
        # SELECTs match. The dynamic numbers (win-rate %, streak count) are
        # preserved in `description` for human readers.
        cur.execute("""INSERT INTO brain_insights (
            ts, insight_type, title, description,
            recommendation, priority, status, applies_to, confidence, auto_generated
        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, 1)""",
            (now, insight_type, dedup_key, description,
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
    # FIX (DEEP-AUDIT-2026-07-26 / F-05-04, F-05-37): the original code wrapped
    # ALL six `_analyze_*` calls in a single try/except. A single bad query
    # (schema drift, locked DB, malformed row) silently disabled the brain's
    # entire learning loop — `_analyze_loss_clusters` and the rest never ran
    # again until the operator noticed stale insights. Now: each analyzer is
    # wrapped in its own try/except so a failure in one doesn't block the
    # others. We track per-analyzer success so the final log line accurately
    # reports partial completion.
    analyzers = [
        ("module_performance", lambda: _analyze_module_performance(cur, min_samples)),
        ("regime_patterns",    lambda: _analyze_regime_patterns(cur, min_samples)),
        ("session_patterns",   lambda: _analyze_session_patterns(cur, min_samples)),
        ("confidence_calibration", lambda: _analyze_confidence_calibration(cur, min_samples)),
        ("signal_agreement",   lambda: _analyze_signal_agreement(cur, min_samples)),
        ("htf_patterns",       lambda: _analyze_htf_patterns(cur, min_samples)),
        ("loss_clusters",      lambda: _analyze_loss_clusters(cur)),
    ]
    succeeded = []
    failed = []
    try:
        for name, fn in analyzers:
            try:
                fn()
                succeeded.append(name)
            except Exception as _e:
                # FIX (DEEP-AUDIT-2026-07-26 / F-05-21, F-05-37): log the
                # per-analyzer exception so the operator can detect which
                # analyzer failed (the old code printed only the first one
                # then aborted the rest).
                print(f"[brain] analyzer '{name}' failed: {_e}")
                failed.append(name)
        try:
            conn.commit()
        except Exception as _e:
            print(f"[brain] analyze commit failed: {_e}")
            conn.rollback()
        # FIX (F-05-37): print an honest summary that distinguishes full vs
        # partial success. The old "analysis complete" message was misleading
        # when only some analyzers ran.
        if failed:
            print(f"[brain] analysis partial: ok={succeeded}, failed={failed}")
        else:
            print("[brain] analysis complete")
    except Exception as e:
        # Defensive: a non-analyzer exception (e.g., conn.cursor()) should
        # still be logged and rolled back. Re-raise so the caller knows.
        print(f"[brain] analyze error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _analyze_module_performance(cur, min_samples):
    """Per-module per-pair win rates with recommendations."""
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-09): GROUP BY period as well as
    # (asset, module_name). Previously the query mixed win rates across all
    # periods for a pair (60s, 300s, etc.) and stored the blended result
    # under period=60. Now each (asset, module, period) tuple gets its own
    # row, so multi-period pairs are tuned correctly.
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-05-07): add a rolling-window time filter
    # `WHERE ts >= ?` so module recommendations reflect RECENT behavior. The
    # original query used the ENTIRE brain_module_votes history — a module
    # whose win rate shifted months ago kept its stale weights. Now restricted
    # to last _MODULE_PERF_LOOKBACK_DAYS (14 days). Pass `0` lookback via the
    # SQL parameter to keep backward-compat for any row inserted before the
    # ts column was populated (it is REAL so always populated).
    perf_cutoff = time.time() - _MODULE_PERF_LOOKBACK_SEC
    rows = cur.execute("""SELECT asset, period, module_name,
        COUNT(*) as total,
        SUM(module_correct) as correct,
        SUM(CASE WHEN direction='CALL' THEN 1 ELSE 0 END) as call_total,
        SUM(CASE WHEN direction='CALL' AND module_correct=1 THEN 1 ELSE 0 END) as call_correct,
        SUM(CASE WHEN direction='PUT' THEN 1 ELSE 0 END) as put_total,
        SUM(CASE WHEN direction='PUT' AND module_correct=1 THEN 1 ELSE 0 END) as put_correct
        FROM brain_module_votes
        WHERE module_correct IS NOT NULL AND ts >= ?
        GROUP BY asset, period, module_name
        HAVING total >= ?""", (perf_cutoff, min_samples)).fetchall()

    for row in rows:
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-77): the SQL `HAVING total >= ?`
        # (where `min_samples` defaults to 20) already guarantees total > 0.
        # The `if row["total"] > 0 else 0` defensive check was dead. Removed.
        wr = row["correct"] / row["total"]
        call_wr = row["call_correct"] / row["call_total"] if row["call_total"] > 0 else 0
        put_wr = row["put_correct"] / row["put_total"] if row["put_total"] > 0 else 0

        # Recommended weight adjustment.
        # FIX (AUDIT-CORE #17, 2026-07-21): the previous if/elif chain had
        # `elif wr > 0.60` AFTER `elif wr > 0.55`, which made the 0.60
        # branch UNREACHABLE — any wr > 0.60 also satisfies wr > 0.55, so
        # the BOOST_STRONG (×1.5) tier never fired. Reordered so the
        # higher threshold is checked first. Excellent modules (>60% win
        # rate) now correctly get ×1.5 instead of ×1.3.
        #
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-38): hoist 0.60/0.55/0.45/0.40
        # thresholds to module-level constants so they are tunable in one
        # place. Behavior unchanged.
        if wr > _WR_BOOST_STRONG:
            rec_weight = 1.5
            action = "BOOST_STRONG"
            priority = "HIGH"
            notes = f"Module excellent ({wr:.0%}). Recommend weight ×1.5."
        elif wr > _WR_BOOST:
            rec_weight = 1.3
            action = "BOOST"
            priority = "MEDIUM"
            notes = f"Module overperforming ({wr:.0%}). Recommend weight ×1.3."
        elif wr < _WR_DAMPEN_SEVERE:
            rec_weight = 0.5
            action = "DAMPEN_SEVERE"
            priority = "HIGH"
            notes = f"Module performing badly ({wr:.0%}). Recommend weight ×0.5."
        elif wr < _WR_DAMPEN:
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-39): magic 0.15 → _WR_DIRECTION_BIAS
        # constant. Also guard against the spurious-bias case where only one
        # direction was traded (call_total=0 → call_wr=0, then |0 - put_wr|
        # can exceed 0.15 spuriously). Now require BOTH directions to have
        # samples before emitting a bias note.
        if (row["call_total"] > 0 and row["put_total"] > 0 and
                abs(call_wr - put_wr) > _WR_DIRECTION_BIAS):
            bias = "CALL" if call_wr > put_wr else "PUT"
            notes += f" {bias} bias: CALL={call_wr:.0%}, PUT={put_wr:.0%}."

        # Upsert brain_learning
        # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-08 + AUDIT-2-09): use
        # ON CONFLICT upsert against the new UNIQUE(asset, module_name,
        # period) index, and pass the actual `period` from the row (was
        # hardcoded to 60). The old `INSERT OR REPLACE` was a no-op because
        # no UNIQUE constraint existed, so rows accumulated ~100x. Now we
        # update in place.
        try:
            cur.execute("""INSERT INTO brain_learning (
                ts, asset, period, module_name,
                total, correct, wrong,
                win_rate, call_total, call_correct,
                put_total, put_correct,
                recommended_weight, recommended_score_adjustment,
                notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset, module_name, period) DO UPDATE SET
                ts=excluded.ts, total=excluded.total, correct=excluded.correct,
                wrong=excluded.wrong, win_rate=excluded.win_rate,
                call_total=excluded.call_total, call_correct=excluded.call_correct,
                put_total=excluded.put_total, put_correct=excluded.put_correct,
                recommended_weight=excluded.recommended_weight,
                recommended_score_adjustment=excluded.recommended_score_adjustment,
                notes=excluded.notes""",
                (time.time(), row["asset"], row["period"], row["module_name"],
                 row["total"], row["correct"], row["total"] - row["correct"],
                 wr, row["call_total"], row["call_correct"],
                 row["put_total"], row["put_correct"],
                 rec_weight, rec_weight - 1.0, notes))
        except Exception as _e:
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-28, F-05-40, F-05-21): the
            # old `except Exception:` was a bare silent fallback to
            # `INSERT OR REPLACE`. When the ON CONFLICT clause failed (older
            # SQLite, missing UNIQUE index, NULL in conflict column), the
            # silent fallback DELETE+re-INSERTed the brain_learning row on
            # every cycle, churning the autoincrement id and breaking any
            # external reference. Now: log the exception so the operator
            # can detect the schema drift; the INSERT OR REPLACE fallback
            # is preserved only for genuine older-SQLite compatibility.
            print(f"[brain] brain_learning ON CONFLICT failed ({row['asset']}/{row['module_name']}): {_e}")
            cur.execute("""INSERT OR REPLACE INTO brain_learning (
                ts, asset, period, module_name,
                total, correct, wrong,
                win_rate, call_total, call_correct,
                put_total, put_correct,
                recommended_weight, recommended_score_adjustment,
                notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), row["asset"], row["period"], row["module_name"],
                 row["total"], row["correct"], row["total"] - row["correct"],
                 wr, row["call_total"], row["call_correct"],
                 row["put_total"], row["put_correct"],
                 rec_weight, rec_weight - 1.0, notes))

        # Generate insight for severe cases
        if wr < _WR_DAMPEN_SEVERE or wr > _WR_BOOST_STRONG:
            insight_type = "MODULE_PERFORMANCE"
            title = f"{row['module_name']} on {row['asset']}: {wr:.0%} win rate"
            desc = f"Module {row['module_name']} has {wr:.0%} win rate on {row['asset']} "
            desc += f"({row['correct']}/{row['total']} correct). "
            desc += f"CALL: {call_wr:.0%} ({row['call_correct']}/{row['call_total']}), "
            desc += f"PUT: {put_wr:.0%} ({row['put_correct']}/{row['put_total']}). "
            desc += f"Recommendation: {action} (weight ×{rec_weight})."

            # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): pass a stable dedup_title
            # so the insight dedups correctly across runs as `wr` changes.
            # Display `title` carries the latest numbers; `dedup_title` is the
            # stable key the brain_insights.title column stores.
            stable_title = f"{row['module_name']} on {row['asset']} win rate"
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, insight_type, title, desc,
                f"Set {row['module_name']} weight to {rec_weight} for {row['asset']}",
                priority, wr, applies_to=row['asset'],
                dedup_title=stable_title)


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
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-44): magic 0.45 → _WR_LOW_REGIME.
        if wr < _WR_LOW_REGIME:
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): pass a stable dedup_title
            # so the insight dedups across runs as `wr` changes.
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'REGIME_PATTERN',
                f"Low accuracy in {regime}: {wr:.0%}",
                f"Engine wins only {wr:.0%} in {regime} regime ({counts['correct']}/{total}). "
                f"This regime is problematic — consider skipping signals or inverting logic.",
                f"Apply confidence penalty in {regime} regime",
                'HIGH', wr, applies_to=regime,
                dedup_title=f"Low accuracy in {regime}")


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
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-77): HAVING total >= min_samples
        # already guarantees total > 0; the `if row["total"] > 0 else 0` was
        # a dead defensive check. Removed.
        wr = row["correct"] / row["total"]
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-44): magic 0.45 → _WR_LOW_REGIME.
        if wr < _WR_LOW_REGIME:
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): pass a stable dedup_title
            # so the insight dedups across runs as `wr` changes.
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'SESSION_PATTERN',
                f"Low accuracy in {row['session_name']} session: {wr:.0%}",
                f"During {row['session_name']} session, win rate is only {wr:.0%} "
                f"({row['correct']}/{row['total']}). Market behavior may be different.",
                f"Reduce confidence in {row['session_name']} session or skip signals",
                'MEDIUM', wr, applies_to=row['session_name'],
                dedup_title=f"Low accuracy in {row['session_name']} session")


def _analyze_confidence_calibration(cur, min_samples):
    """Find overconfident/underconfident bins."""
    # FIX (DEEP-AUDIT-2026-07-26 / F-05-41): magic 10 (bucket width) →
    # _CONFIDENCE_BIN_WIDTH constant so it is greppable / tunable.
    rows = cur.execute(f"""SELECT
        CAST(confidence/{_CONFIDENCE_BIN_WIDTH} AS INT)*{_CONFIDENCE_BIN_WIDTH} as conf_bin,
        COUNT(*) as total,
        SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) as correct
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        GROUP BY conf_bin
        HAVING total >= ?""", (min_samples,)).fetchall()

    for row in rows:
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-77): HAVING total >= min_samples
        # already guarantees total > 0; defensive guard removed.
        wr = row["correct"] / row["total"]
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-41): clamp the displayed bin label
        # to 0-100 range — for `confidence=100` the bucket "100-109 %" is an
        # impossible range (confidence is 0-100). The original code emitted
        # the impossible label, which was confusing in the API/UI.
        bin_lo = max(0, min(90, row["conf_bin"]))
        bin_hi = min(100, bin_lo + _CONFIDENCE_BIN_WIDTH - 1)
        bin_mid = bin_lo + _CONFIDENCE_BIN_WIDTH // 2
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-15, F-05-42): the original check
        # `if wr < bin_mid / 100.0 - 0.10` could NEVER fire for the lowest
        # bin (conf_bin=0 → bin_mid=5 → threshold = -0.05 → wr always >= 0).
        # So a 0%-win-rate lowest-confidence bucket was never flagged as
        # overconfident. Now: use `_OVERCONFIDENCE_MARGIN` constant and only
        # fire when the threshold is non-negative (any bin whose mid >= 10%
        # can be overconfident; the 0-9 % bin is excluded because it's
        # trivially always "right" — a 0 % confidence prediction winning
        # 5 % of the time is fine, not overconfident).
        threshold = (bin_mid / 100.0) - _OVERCONFIDENCE_MARGIN
        if threshold > 0 and wr < threshold:
            # Significantly overconfident
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-14): magic "Cap at -10 %" —
            # when `conf_bin=0` the recommendation was "Cap confidence at
            # -10 %", a nonsensical negative number. Clamp to >= 0.
            cap_pct = max(0, bin_lo - _CONFIDENCE_BIN_WIDTH)
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): pass a stable dedup_title
            # so the insight dedups across runs as `wr` changes.
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'CONFIDENCE_CALIBRATION',
                f"Overconfident at {bin_lo}-{bin_hi}%: actual {wr:.0%}",
                f"Predictions with confidence {bin_lo}-{bin_hi}% "
                f"only win {wr:.0%} ({row['correct']}/{row['total']}). "
                f"Expected ~{bin_mid}%. Engine is overconfident in this range.",
                f"Cap confidence at {cap_pct}% for this bin",
                'HIGH', wr, applies_to=f"conf_{bin_lo}",
                dedup_title=f"Overconfident at {bin_lo}-{bin_hi}%")


def _analyze_signal_agreement(cur, min_samples):
    """When modules agree vs disagree, who's right?"""
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-12): replace the O(n²) correlated
    # subquery (one SELECT per brain_module_votes row) with a LEFT JOIN on
    # brain_predictions. With 100k votes, the old query ran ~100k subqueries;
    # the JOIN version is O(n log n) and completes in milliseconds.
    rows = cur.execute("""SELECT
        bmv.module_name,
        SUM(CASE WHEN bmv.direction = bp.signal THEN 1 ELSE 0 END) as agreed_with_final,
        SUM(CASE WHEN bmv.module_correct = 1 THEN 1 ELSE 0 END) as module_correct_count,
        COUNT(*) as total
        FROM brain_module_votes bmv
        LEFT JOIN brain_predictions bp ON bp.id = bmv.prediction_id
        WHERE bmv.module_correct IS NOT NULL
        GROUP BY bmv.module_name
        HAVING total >= ?""", (min_samples,)).fetchall()

    for row in rows:
        if row["total"] < min_samples:
            continue
        wr = row["module_correct_count"] / row["total"]
        agree_rate = row["agreed_with_final"] / row["total"]

        # If module has high win rate but low agreement, it's a contrarian indicator
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-43): magic 0.55/0.40 → named
        # constants _CONTRARIAN_WR_MIN / _CONTRARIAN_AGREE_MAX.
        if wr > _CONTRARIAN_WR_MIN and agree_rate < _CONTRARIAN_AGREE_MAX:
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): pass a stable dedup_title.
            # The original title was already stable (no embedded numbers), but
            # be explicit so future edits adding numbers don't break dedup.
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'CONTRARIAN_MODULE',
                f"{row['module_name']} is a contrarian indicator",
                f"Module {row['module_name']} has {wr:.0%} win rate but only "
                f"agrees with final {agree_rate:.0%} of the time. "
                f"When it disagrees, it's often RIGHT. Consider boosting its vote.",
                f"Increase {row['module_name']} weight when it disagrees with majority",
                'HIGH', wr, applies_to=row['module_name'],
                dedup_title=f"{row['module_name']} contrarian indicator")


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
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-44): magic 0.45 → _WR_LOW_REGIME.
        if wr < _WR_LOW_REGIME:
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-08): pass a stable dedup_title
            # so the insight dedups across runs as `wr` changes.
            # FIX (AUDIT-CORE #59, 2026-07-21): route through dedup helper.
            _insert_insight_dedup(cur, 'HTF_PATTERN',
                f"Low accuracy when HTF {key}: {wr:.0%}",
                f"When HTF trend is {key}, win rate is only {wr:.0%} "
                f"({counts['correct']}/{total}).",
                f"Reduce confidence or skip when HTF is {key}",
                'MEDIUM', wr, applies_to=key,
                dedup_title=f"Low accuracy when HTF {key}")


def _flush_loss_cluster(cur, asset, streak_count, streak_start_ts, ongoing=False):
    """Emit a LOSS_CLUSTER insight for a completed (or ongoing) loss streak.

    FIX (DEEP-AUDIT-2026-07-26 / F-05-45): the loss-cluster INSERT block was
    triplicated (asset-boundary flush, mid-streak flush, post-loop flush).
    Three copies = three places to update when the schema changes. Extracted
    to a single helper called from all three sites.

    FIX (DEEP-AUDIT-2026-07-26 / F-05-08): use a STABLE dedup_title that
    does NOT embed the streak count, so successive runs (with different
    streak counts) dedup correctly. The dynamic count is preserved in the
    description.

    FIX (DEEP-AUDIT-2026-07-26 / F-05-45): magic 5 (minimum streak) →
    _MIN_LOSS_STREAK constant.
    """
    suffix = " (ongoing)" if ongoing else ""
    # Guard against the streak_start being None (shouldn't happen because
    # current_streak >= _MIN_LOSS_STREAK implies at least one "wrong" row
    # was seen, but defensive).
    if streak_start_ts is None:
        iso_start = "unknown"
    else:
        iso_start = datetime.fromtimestamp(streak_start_ts, tz=timezone.utc).isoformat()
    _insert_insight_dedup(
        cur, 'LOSS_CLUSTER',
        f"{asset}: {streak_count} consecutive losses{suffix}",
        f"Pair {asset} had {streak_count} consecutive losses "
        f"starting at {iso_start}. "
        f"This may indicate a regime shift or broker pattern change.",
        f"Skip {asset} for 30 minutes or reduce confidence",
        'HIGH', 0.0, applies_to=asset,
        dedup_title=f"{asset} consecutive losses")


def _analyze_loss_clusters(cur):
    """Find clusters of consecutive losses per pair."""
    rows = cur.execute("""SELECT asset, ctime, accuracy
        FROM brain_predictions
        WHERE accuracy IN ('correct', 'wrong')
        ORDER BY asset, ctime""").fetchall()

    # Find consecutive loss streaks
    current_streak = 0
    streak_start = None
    current_asset = None

    for row in rows:
        if row["asset"] != current_asset:
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-10): flush any in-progress
            # streak when switching assets. Previously a loss streak at the END
            # of one asset's data was never logged because the loop just reset
            # the counter — the most recent (most actionable) loss clusters
            # were systematically missed.
            if current_asset is not None and current_streak >= _MIN_LOSS_STREAK:
                # FIX (F-05-45): use the extracted helper.
                _flush_loss_cluster(cur, current_asset, current_streak, streak_start)
            current_asset = row["asset"]
            current_streak = 0
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-46): streak_start was NOT
            # reset on asset change. If the new asset's first row was
            # `correct` (so streak_start would never be reassigned for this
            # asset), the next loss streak on the new asset would inherit the
            # OLD asset's streak_start timestamp. Doesn't crash (current_streak
            # was reset to 0), but the recorded timestamp was wrong. Now reset.
            streak_start = None
            # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-11): removed the dead
            # `max_streak` tracking — it was never read after the loop.

        if row["accuracy"] == "wrong":
            if current_streak == 0:
                streak_start = row["ctime"]
            current_streak += 1
        else:
            if current_streak >= _MIN_LOSS_STREAK:
                # Record loss cluster
                # FIX (F-05-45): use the extracted helper.
                # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-13): use UTC-aware
                # datetime to match the rest of brain.py (record_prediction uses
                # tz=timezone.utc). The naive datetime previously displayed in
                # local time, mismatching other brain timestamps.
                _flush_loss_cluster(cur, current_asset, current_streak, streak_start)
            current_streak = 0
            # FIX (DEEP-AUDIT-2026-07-26 / F-05-46): reset streak_start when
            # the streak ends, so the next streak starts with a fresh timestamp.
            streak_start = None

    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-10): post-loop check for a
    # streak that's still in progress at the end of the dataset. The previous
    # code only logged a LOSS_CLUSTER when a `correct` prediction ENDED the
    # streak — so a pair whose last 7 predictions were all wrong was never
    # flagged. Now we check after the loop.
    if current_asset is not None and current_streak >= _MIN_LOSS_STREAK:
        # FIX (F-05-45): use the extracted helper, mark as ongoing.
        _flush_loss_cluster(cur, current_asset, current_streak, streak_start, ongoing=True)


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


# FIX (DEEP-AUDIT-2026-07-26 / F-05-78): removed stale comment block that
# documented the deletion of `get_patterns()`. The function was deleted in
# a prior audit; the comment itself is now dead. Just remove the comment.


def get_brain_summary():
    """Get a summary of brain activity."""
    conn = _conn()
    try:
        # FIX (DEEP-AUDIT-2026-07-26 / F-05-47): the original code fired 5
        # separate `COUNT(*)` queries (one per metric). Each opened its own
        # implicit transaction. Combined into a single SELECT with CASE-WHEN
        # aggregation so we hit the DB once. With 100k+ predictions, this is
        # ~5x faster under load and avoids intermittent "database is locked"
        # errors when the brain is also recording predictions concurrently.
        row = conn.execute("""SELECT
            COUNT(*) AS total_preds,
            SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS total_correct,
            SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) AS total_wrong
            FROM brain_predictions""").fetchone()
        total_preds = row["total_preds"] if row else 0
        total_correct = row["total_correct"] if row else 0
        total_wrong = row["total_wrong"] if row else 0
        total_insights = conn.execute(
            "SELECT COUNT(*) FROM brain_insights WHERE status='active'").fetchone()[0]
        total_learning = conn.execute(
            "SELECT COUNT(*) FROM brain_learning").fetchone()[0]

        # FIX (DEEP-AUDIT-2026-07-26 / F-05-79): compute the sum once into a
        # local instead of evaluating `(total_correct + total_wrong)` twice.
        graded_total = total_correct + total_wrong
        acc = (total_correct / graded_total * 100) if graded_total > 0 else 0

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
