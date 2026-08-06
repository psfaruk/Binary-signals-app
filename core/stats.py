"""core/stats.py — Shared module-stats computer."""
import json
import os
import sqlite3
from collections import defaultdict

from core.constants import (
    MODULE_NAMES,
    MODULE_DISPLAY_NAMES,
    STATS_MAX_ROWS,
    ALLOWED_PAIRS,
    allowlist_sql_filter,
    compute_win_rate,
)

def parse_reasons(reasons_raw):
    """Parse a signal_log ``reasons`` field (JSON string or list) into a list."""
    try:
        reasons = (
            json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        )
    except (ValueError, TypeError):
        return []
    if not isinstance(reasons, list):
        return []
    return reasons
def parse_module_direction(reason_str, module_names=MODULE_NAMES):
    """Parse a ``[module_name] ... -> CALL|PUT`` reason string."""
    if not reason_str.startswith("["):
        return None, None
    end_bracket = reason_str.find("]")
    if end_bracket == -1:
        return None, None
    module = reason_str[1:end_bracket].strip()
    if module not in module_names:
        return None, None
    normalized = reason_str.replace("->", "→").replace("=>", "→")
    tail = (
        normalized.rsplit("→", 1)[-1].strip().upper()
        if "→" in normalized
        else ""
    )
    if tail.endswith("CALL") or " CALL" in tail:
        direction = "CALL"
    elif tail.endswith("PUT") or " PUT" in tail:
        direction = "PUT"
    else:
        reason_upper = reason_str.upper()
        if "CALL" in reason_upper or "BULL" in reason_upper or "BUYER" in reason_upper:
            direction = "CALL"
        elif "PUT" in reason_upper or "BEAR" in reason_upper or "SELLER" in reason_upper:
            direction = "PUT"
        else:
            return None, None
    return module, direction
def compute_module_stats(db_path=None):
    """Compute per-module win-rate statistics from signal_log."""
    if db_path is None:
        try:
            import db as _db
            db_path = _db.DB_PATH
        except ImportError:
            from core.constants import DB_PATH as _CONST_DB_PATH
            db_path = _CONST_DB_PATH
    if not os.path.exists(db_path):
        return {"error": "signals.db not found", "db_path": db_path}
    try:
        import db as _db
        conn = _db._conn()
    except ImportError:
        conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        return _compute_module_stats_inner(cur)
    finally:
        conn.close()
def _compute_module_stats_inner(cur):
    """Inner stats computation — takes a cursor, returns the stats dict."""
    _stats_max_rows = STATS_MAX_ROWS
    try:
        allow_frag, allow_params = allowlist_sql_filter(column="asset")
        total_row = cur.execute(f"""
            SELECT COUNT(*) as n FROM signal_log
            WHERE signal IN ('CALL','PUT')
              AND accuracy IN ('correct','wrong')
              AND ts >= COALESCE((
                  SELECT MIN(ts) FROM (
                      SELECT ts FROM signal_log
                      WHERE signal IN ('CALL','PUT')
                        AND accuracy IN ('correct','wrong')
                        {allow_frag}
                      ORDER BY ts DESC LIMIT ?
                  )
              ), 0)
              {allow_frag}
        """, allow_params + (_stats_max_rows,) + allow_params).fetchone()
        total = total_row[0] if total_row else 0
    except sqlite3.OperationalError:
        try:
            allow_frag, allow_params = allowlist_sql_filter(column="asset")
            total_row = cur.execute(f"""
                SELECT COUNT(*) as n FROM signal_log
                WHERE signal IN ('CALL','PUT')
                  AND accuracy IN ('correct','wrong')
                  AND ctime >= COALESCE((
                      SELECT MIN(ctime) FROM (
                          SELECT ctime FROM signal_log
                          WHERE signal IN ('CALL','PUT')
                            AND accuracy IN ('correct','wrong')
                            {allow_frag}
                          ORDER BY ctime DESC LIMIT ?
                      )
                  ), 0)
                  {allow_frag}
            """, allow_params + (_stats_max_rows,) + allow_params).fetchone()
            total = total_row[0] if total_row else 0
        except sqlite3.OperationalError:
            allow_frag, allow_params = allowlist_sql_filter(column="asset")
            total = cur.execute(
                f"SELECT COUNT(*) FROM signal_log "
                f"WHERE signal IN ('CALL','PUT') "
                f"AND accuracy IN ('correct','wrong')"
                f" {allow_frag}",
                allow_params
            ).fetchone()[0]
    if total == 0:
        return {
            "total_signals": 0,
            "total_graded": 0,
            "overall_win_pct": 0,
            "total_correct": 0,
            "total_wrong": 0,
            "modules": [],
            "pairs": {},
            "message": "No signals logged yet",
        }
    module_stats = defaultdict(lambda: {
        "CALL": {"correct": 0, "wrong": 0},
        "PUT": {"correct": 0, "wrong": 0},
    })
    pair_module_stats = defaultdict(lambda: defaultdict(lambda: {
        "CALL": {"correct": 0, "wrong": 0},
        "PUT": {"correct": 0, "wrong": 0},
    }))
    try:
        allow_frag, allow_params = allowlist_sql_filter(column="asset")
        rows = cur.execute(f"""
            SELECT asset, signal, accuracy, reasons
            FROM signal_log
            WHERE signal IN ('CALL', 'PUT')
              AND accuracy IN ('correct', 'wrong')
              {allow_frag}
            ORDER BY ts DESC
            LIMIT ?
        """, allow_params + (_stats_max_rows,)).fetchall()
    except sqlite3.OperationalError:
        allow_frag, allow_params = allowlist_sql_filter(column="asset")
        rows = cur.execute(f"""
            SELECT asset, signal, accuracy, reasons
            FROM signal_log
            WHERE signal IN ('CALL', 'PUT')
              AND accuracy IN ('correct', 'wrong')
              {allow_frag}
            ORDER BY ctime DESC
            LIMIT ?
        """, allow_params + (_stats_max_rows,)).fetchall()
    for row in rows:
        asset = row["asset"]
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        reasons = parse_reasons(row["reasons"] or "[]")
        for reason in reasons:
            reason_str = str(reason)
            module, direction = parse_module_direction(reason_str, MODULE_NAMES)
            if module is None or direction is None:
                continue
            if direction == final_signal:
                if accuracy == "correct":
                    module_stats[module][direction]["correct"] += 1
                    pair_module_stats[asset][module][direction]["correct"] += 1
                elif accuracy == "wrong":
                    module_stats[module][direction]["wrong"] += 1
                    pair_module_stats[asset][module][direction]["wrong"] += 1
            else:
                if accuracy == "wrong":
                    module_stats[module][direction]["correct"] += 1
                    pair_module_stats[asset][module][direction]["correct"] += 1
                elif accuracy == "correct":
                    module_stats[module][direction]["wrong"] += 1
                    pair_module_stats[asset][module][direction]["wrong"] += 1
    modules_summary = []
    for module_key in MODULE_NAMES:
        display_name = MODULE_DISPLAY_NAMES.get(module_key, module_key)
        stats = module_stats.get(module_key, {})
        call_c = stats.get("CALL", {}).get("correct", 0)
        call_w = stats.get("CALL", {}).get("wrong", 0)
        put_c = stats.get("PUT", {}).get("correct", 0)
        put_w = stats.get("PUT", {}).get("wrong", 0)
        total_c = call_c + put_c
        total_w = call_w + put_w
        total_all = total_c + total_w
        win_pct = (total_c / total_all * 100) if total_all else None
        call_total = call_c + call_w
        call_win = (call_c / call_total * 100) if call_total else None
        put_total = put_c + put_w
        put_win = (put_c / put_total * 100) if put_total else None
        modules_summary.append({
            "module": module_key,
            "display_name": display_name,
            "total": total_all,
            "correct": total_c,
            "wrong": total_w,
            "win_pct": round(win_pct, 1) if win_pct is not None else None,
            "call_win_pct": round(call_win, 1) if call_win is not None else None,
            "put_win_pct": round(put_win, 1) if put_win is not None else None,
            "call_correct": call_c,
            "call_wrong": call_w,
            "put_correct": put_c,
            "put_wrong": put_w,
        })
    try:
        allow_frag, allow_params = allowlist_sql_filter(column="asset")
        acc_rows = cur.execute(f"""
            SELECT accuracy, COUNT(*) as n
            FROM signal_log
            WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
              AND ts >= COALESCE((
                  SELECT MIN(ts) FROM (
                      SELECT ts FROM signal_log
                      WHERE signal IN ('CALL','PUT')
                        AND accuracy IN ('correct','wrong')
                        {allow_frag}
                      ORDER BY ts DESC LIMIT ?
                  )
              ), 0)
              {allow_frag}
            GROUP BY accuracy
        """, allow_params + (_stats_max_rows,) + allow_params).fetchall()
    except sqlite3.OperationalError:
        try:
            allow_frag, allow_params = allowlist_sql_filter(column="asset")
            acc_rows = cur.execute(f"""
                SELECT accuracy, COUNT(*) as n
                FROM signal_log
                WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
                  AND ctime >= COALESCE((
                      SELECT MIN(ctime) FROM (
                          SELECT ctime FROM signal_log
                          WHERE signal IN ('CALL','PUT')
                            AND accuracy IN ('correct','wrong')
                            {allow_frag}
                          ORDER BY ctime DESC LIMIT ?
                      )
                  ), 0)
                  {allow_frag}
                GROUP BY accuracy
            """, allow_params + (_stats_max_rows,) + allow_params).fetchall()
        except sqlite3.OperationalError:
            allow_frag, allow_params = allowlist_sql_filter(column="asset")
            acc_rows = cur.execute(f"""
                SELECT accuracy, COUNT(*) as n
                FROM signal_log
                WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
                {allow_frag}
                GROUP BY accuracy
            """, allow_params).fetchall()
    total_correct = sum(r["n"] for r in acc_rows if r["accuracy"] == "correct")
    total_wrong = sum(r["n"] for r in acc_rows if r["accuracy"] == "wrong")
    total_graded = total_correct + total_wrong
    overall_win = (total_correct / total_graded * 100) if total_graded else 0
    # ── Per-pair SIGNAL-level win rate ──────────────────────────────────────
    # FIX (PAIR-ALLOWLIST-2026-08-07): filter to the 15-pair allowlist so
    # removed pairs (EURUSD_otc, USDCHF_otc, USDJPY_otc, USDARS_otc, etc.)
    # don't resurface in the win-rate dropdown. Uses
    # core.constants.allowlist_sql_filter() for parameterised SQL.
    pair_signal_stats = {}
    try:
        allow_frag, allow_params = allowlist_sql_filter(column="asset")
        sig_rows = cur.execute(f"""
            SELECT asset,
                   SUM(accuracy = 'correct') AS correct,
                   SUM(accuracy = 'wrong')   AS wrong,
                   SUM(accuracy = 'draw')    AS draws,
                   COUNT(*)                  AS total,
                   MAX(ts)                   AS last_ts
            FROM signal_log
            WHERE signal IN ('CALL','PUT')
              AND accuracy IN ('correct','wrong','draw')
              {allow_frag}
            GROUP BY asset
        """, allow_params).fetchall()
        for r in sig_rows:
            c_, w_ = (r["correct"] or 0), (r["wrong"] or 0)
            graded = c_ + w_
            win_pct = compute_win_rate(c_, w_)
            pair_signal_stats[r["asset"]] = {
                "correct": c_,
                "wrong": w_,
                "draws": r["draws"] or 0,
                "graded": graded,
                "total": r["total"] or 0,
                "win_pct": round(win_pct, 1) if win_pct is not None else None,
                "last_ts": r["last_ts"] or 0,
            }
    except sqlite3.Error as e:
        print(f"[stats] per-pair signal stats failed: {e}")

    pairs_summary = {}
    for asset, pair_data in pair_module_stats.items():
        pair_modules = {}
        for module_key in MODULE_NAMES:
            display_name = MODULE_DISPLAY_NAMES.get(module_key, module_key)
            stats = pair_data.get(module_key)
            if not stats:
                continue
            call_c = stats["CALL"]["correct"]
            call_w = stats["CALL"]["wrong"]
            put_c = stats["PUT"]["correct"]
            put_w = stats["PUT"]["wrong"]
            total_c = call_c + put_c
            total_w = call_w + put_w
            total_all = total_c + total_w
            if total_all == 0:
                continue
            pair_modules[module_key] = {
                "display_name": display_name,
                "total": total_all,
                "correct": total_c,
                "wrong": total_w,
                "win_pct": round(total_c / total_all * 100, 1),
            }
        if pair_modules:
            pairs_summary[asset] = pair_modules
    return {
        "total_signals": total,
        "total_graded_signals": total,
        "total_graded": total_graded,
        "overall_win_pct": round(overall_win, 1),
        "total_correct": total_correct,
        "total_wrong": total_wrong,
        "modules": modules_summary,
        # `pairs` = per-pair MODULE-VOTE breakdown (many rows per signal).
        "pairs": pairs_summary,
        # `pair_signals` = per-pair SIGNAL-level counts. Use this for any
        # user-facing "win rate for this pair" number — see the note above.
        "pair_signals": pair_signal_stats,
    }
