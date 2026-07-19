"""
core/stats.py — Shared module-stats computer.

Single source of truth for the per-module win-rate report. Previously
the same logic was duplicated 90% between:
  - server.py /api/stats endpoint        (returns JSON to frontend)
  - module_performance_report.py CLI     (prints formatted text)

Both had drifted out of sync — neither included `trend_follow` in their
MODULE_NAMES dict, so Real-engine signals were silently undercounted.

This module exposes one function `compute_module_stats(db_path)` that
both callers use. The single source of truth for module names lives in
`core/constants.MODULE_NAMES`.
"""
import json
import os
import sqlite3
from collections import defaultdict

from core.constants import MODULE_NAMES, MODULE_DISPLAY_NAMES


def compute_module_stats(db_path=None):
    """Compute per-module win-rate statistics from signal_log.

    Parses the `reasons` JSON array from each graded signal_log row and,
    for each module, counts how often its votes aligned with the final
    graded outcome.

    Args:
        db_path: path to signals.db. If None, uses db.DB_PATH.

    Returns:
        dict with:
            total_signals: int
            total_graded: int
            overall_win_pct: float
            total_correct: int
            total_wrong: int
            modules: list of per-module stat dicts (display_name, total,
                     correct, wrong, win_pct, call_*, put_*)
            pairs: dict[asset][module_key] = {display_name, total, correct,
                                              wrong, win_pct}
            error: str (only if DB missing)
            message: str (only if no signals logged yet)
    """
    if db_path is None:
        try:
            import db as _db
            db_path = _db.DB_PATH
        except ImportError:
            db_path = "signals.db"

    if not os.path.exists(db_path):
        return {"error": "signals.db not found", "db_path": db_path}

    # FIX (AUDIT-CORE #4, 2026-07-19): use db._conn() so we inherit WAL
    # mode + synchronous=NORMAL. Previously this opened a raw sqlite3
    # connection WITHOUT WAL, causing lock contention with the feed's
    # WAL writers — every /api/stats refresh could block writes for
    # 100ms+ on slow disks. Also wrap in try/finally so the connection
    # is closed even on exception (previously leaked on any error
    # between connect and conn.close()).
    try:
        import db as _db
        conn = _db._conn()
    except Exception:
        # Fallback: raw connect (no WAL) if db module isn't importable.
        conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        return _compute_module_stats_inner(cur, total_query=total)
    finally:
        conn.close()


def _compute_module_stats_inner(cur, total_query=None):
    """Inner stats computation — takes a cursor, returns the stats dict.
    Split out so the connection lifecycle can be managed separately.
    """
    total = cur.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
    if total == 0:
        return {"total_signals": 0, "message": "No signals logged yet"}

    # Per-module global stats
    module_stats = defaultdict(lambda: {
        "CALL": {"correct": 0, "wrong": 0},
        "PUT": {"correct": 0, "wrong": 0},
    })
    # Per-pair per-module stats
    pair_module_stats = defaultdict(lambda: defaultdict(lambda: {
        "CALL": {"correct": 0, "wrong": 0},
        "PUT": {"correct": 0, "wrong": 0},
    }))

    rows = cur.execute("""
        SELECT asset, signal, accuracy, reasons
        FROM signal_log
        WHERE signal IN ('CALL', 'PUT')
        ORDER BY ts DESC
        LIMIT ?
    """, (int(os.environ.get("STATS_MAX_ROWS", "5000")),)).fetchall()

    for row in rows:
        asset = row["asset"]
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        reasons_raw = row["reasons"] or "[]"
        try:
            reasons = json.loads(reasons_raw) if isinstance(reasons_raw, str) else reasons_raw
        except (ValueError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []

        for reason in reasons:
            reason_str = str(reason)
            if not reason_str.startswith("["):
                continue
            end_bracket = reason_str.find("]")
            if end_bracket == -1:
                continue
            module = reason_str[1:end_bracket].strip()
            if module not in MODULE_NAMES:
                continue
            reason_upper = reason_str.upper()
            if "PUT" in reason_upper or "BEAR" in reason_upper or "SELLER" in reason_upper:
                direction = "PUT"
            elif "CALL" in reason_upper or "BULL" in reason_upper or "BUYER" in reason_upper:
                direction = "CALL"
            else:
                continue

            # Attribution logic:
            # - Module vote matched final AND final was correct → module correct
            # - Module vote opposed final AND final was wrong → module correct
            #   (it was right, against the majority)
            # - Otherwise → module wrong
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

    # ── Per-module summary ────────────────────────────────────────────────
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
        win_pct = (total_c / total_all * 100) if total_all else 0
        call_total = call_c + call_w
        call_win = (call_c / call_total * 100) if call_total else 0
        put_total = put_c + put_w
        put_win = (put_c / put_total * 100) if put_total else 0
        modules_summary.append({
            "module": module_key,
            "display_name": display_name,
            "total": total_all,
            "correct": total_c,
            "wrong": total_w,
            "win_pct": round(win_pct, 1),
            "call_win_pct": round(call_win, 1),
            "put_win_pct": round(put_win, 1),
            "call_correct": call_c,
            "call_wrong": call_w,
            "put_correct": put_c,
            "put_wrong": put_w,
        })

    # ── Overall accuracy ──────────────────────────────────────────────────
    acc_rows = cur.execute("""
        SELECT accuracy, COUNT(*) as n
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY accuracy
    """).fetchall()
    total_correct = sum(r["n"] for r in acc_rows if r["accuracy"] == "correct")
    total_wrong = sum(r["n"] for r in acc_rows if r["accuracy"] == "wrong")
    total_graded = total_correct + total_wrong
    overall_win = (total_correct / total_graded * 100) if total_graded else 0

    # ── Per-pair breakdown ────────────────────────────────────────────────
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
                "win_pct": round(total_c / total_all * 100, 1) if total_all else 0,
            }
        if pair_modules:
            pairs_summary[asset] = pair_modules

    # Connection is closed by the caller (try/finally in compute_module_stats).

    return {
        "total_signals": total,
        "total_graded": total_graded,
        "overall_win_pct": round(overall_win, 1),
        "total_correct": total_correct,
        "total_wrong": total_wrong,
        "modules": modules_summary,
        "pairs": pairs_summary,
    }
