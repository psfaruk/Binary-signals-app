"""
core/stats.py — Shared module-stats computer.

Single source of truth for the per-module win-rate report. Previously
the same logic was duplicated 90% between:
  - server.py /api/stats endpoint        (returns JSON to frontend)
  - module_performance_report.py CLI     (prints formatted text)

FIX (MODULE-PRUNE-2026-08-03): indicator / otc_pattern / trend_follow removed.
MODULE_NAMES dict, so Real-engine signals were silently undercounted.

This module exposes one function `compute_module_stats(db_path)` that
both callers use. The single source of truth for module names lives in
`core/constants.MODULE_NAMES`.

FIX (DEEP-AUDIT-2026-07-26 / F-08-03): extract shared reason-parsing +
direction-attribution helpers (`parse_reasons`, `parse_module_direction`)
so core/auto_tune.py can reuse them. Previously the same logic was
copy-pasted verbatim between stats.py and auto_tune.py — both files had
the same "FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-25)" comment block
and drifted in lockstep (A-04 problem 53).
"""
import json
import os
import sqlite3
from collections import defaultdict

from core.constants import (
    MODULE_NAMES,
    MODULE_DISPLAY_NAMES,
    STATS_MAX_ROWS,
)


def parse_reasons(reasons_raw):
    """Parse a signal_log ``reasons`` field (JSON string or list) into a list.

    Returns ``[]`` on any parse error or unexpected type so callers can
    iterate the result unconditionally. Previously every consumer
    reimplemented this same try/except isinstance check inline.
    """
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
    """Parse a ``[module_name] ... -> CALL|PUT`` reason string.

    Returns ``(module_name, direction)`` or ``(None, None)`` when the reason
    is malformed, the module is not in ``module_names``, or no direction can
    be inferred.

    Direction inference:
      1. Normalise ASCII arrows (``->``, ``=>``) to the Unicode ``→`` so
         reason strings emitted by older engines go through the same code
         path (A-04 problem 54).
      2. Take the tail (text after the final ``→``) and look for a CALL/PUT
         suffix or substring. CALL is checked first to avoid the legacy
         order-bias bug where reasons mentioning BOTH directions were
         misclassified as PUT (A-04 problem 53 / original LIVE-FIX-2-25).
      3. If the tail doesn't match, fall back to a CALL-first substring scan
         of the whole reason string.
    """
    if not reason_str.startswith("["):
        return None, None
    end_bracket = reason_str.find("]")
    if end_bracket == -1:
        return None, None
    module = reason_str[1:end_bracket].strip()
    if module not in module_names:
        return None, None
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-04): normalise ASCII arrows so
    # `->` and `=>` are routed through the same tail-based detection path
    # as the Unicode `→`. Without this, reasons using ASCII arrows fell
    # through to the legacy whole-string scan, which re-introduced the
    # order-bias bug the original LIVE-FIX-2-25 patch was meant to kill.
    normalized = reason_str.replace("->", "→").replace("=>", "→")
    tail = (
        normalized.rsplit("→", 1)[-1].strip().upper()
        if "→" in normalized
        else ""
    )
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-05): drop redundant `tail == "CALL"`
    # / `tail == "PUT"` clauses — `endswith` already covers the equality.
    if tail.endswith("CALL") or " CALL" in tail:
        direction = "CALL"
    elif tail.endswith("PUT") or " PUT" in tail:
        direction = "PUT"
    else:
        # Legacy fallback: CALL-first scan (fires when no arrow present
        # OR when the tail doesn't contain CALL/PUT — preserves the
        # original behaviour).
        reason_upper = reason_str.upper()
        if "CALL" in reason_upper or "BULL" in reason_upper or "BUYER" in reason_upper:
            direction = "CALL"
        elif "PUT" in reason_upper or "BEAR" in reason_upper or "SELLER" in reason_upper:
            direction = "PUT"
        else:
            return None, None
    return module, direction


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
            # FIX (DEEP-AUDIT-2026-07-26 / F-08-06): fall back to the
            # centralised `core.constants.DB_PATH` (which itself honours the
            # `DB_PATH` env var) instead of the bare relative "signals.db"
            # path. The old fallback opened/created the wrong DB whenever
            # the CWD was not the project root (A-04 problem 64).
            from core.constants import DB_PATH as _CONST_DB_PATH
            db_path = _CONST_DB_PATH

    if not os.path.exists(db_path):
        return {"error": "signals.db not found", "db_path": db_path}

    # FIX (AUDIT-CORE #4, 2026-07-19): use db._conn() so we inherit WAL
    # mode + synchronous=NORMAL. Previously this opened a raw sqlite3
    # connection WITHOUT WAL, causing lock contention with the feed's
    # WAL writers — every /api/stats refresh could block writes for
    # 100ms+ on slow disks. Also wrap in try/finally so the connection
    # is closed even on exception (previously leaked on any error
    # between connect and conn.close()).
    #
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-07): catch ImportError only — the
    # old broad `except Exception` swallowed genuine failures from
    # `db._conn()` (e.g.OperationalError on a locked DB) and silently
    # fell back to a non-WAL connection, causing lock contention with
    # the feed's WAL writers (A-04 problem 65).
    try:
        import db as _db
        conn = _db._conn()
    except ImportError:
        # Fallback: raw connect (no WAL) if db module isn't importable.
        conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        # NOTE: `_compute_module_stats_inner` no longer accepts the
        # `total_query` parameter — it was a dead arg from an earlier
        # refactor (A-04 problem 66) and the prior call here passed an
        # undefined `total` variable that would have raised NameError
        # the moment /api/stats was hit (FIX BUG-STATS-1, 2026-07-20).
        return _compute_module_stats_inner(cur)
    finally:
        conn.close()


def _compute_module_stats_inner(cur):
    """Inner stats computation — takes a cursor, returns the stats dict.
    Split out so the connection lifecycle can be managed separately.

    FIX (DEEP-AUDIT-2026-07-26 / F-08-08): drop the dead `total_query`
    parameter — it was accepted but never read (A-04 problem 66).
    """
    # FIX (DEEP-AUDIT-2026-07-26 / F-08-09): hoist the `STATS_MAX_ROWS`
    # env-var lookup to module load (see `STATS_MAX_ROWS` import above).
    # The old code re-parsed the env var on every /api/stats call
    # (A-04 problem 67).
    _stats_max_rows = STATS_MAX_ROWS
    # Total + accuracy: count only CALL/PUT rows with a graded accuracy.
    # We use a subquery to derive the cutoff timestamp (the ts of the
    # Nth-most-recent graded signal) so the same window is applied to both
    # queries. Falls back to COUNT(*) if the subquery fails (older schemas).
    try:
        total_row = cur.execute("""
            SELECT COUNT(*) as n FROM signal_log
            WHERE signal IN ('CALL','PUT')
              AND accuracy IN ('correct','wrong')
              AND ts >= COALESCE((
                  SELECT MIN(ts) FROM (
                      SELECT ts FROM signal_log
                      WHERE signal IN ('CALL','PUT')
                        AND accuracy IN ('correct','wrong')
                      ORDER BY ts DESC LIMIT ?
                  )
              ), 0)
        """, (_stats_max_rows,)).fetchone()
        total = total_row[0] if total_row else 0
    except sqlite3.OperationalError:
        # Fallback: ctime column (older schema) or simple count.
        try:
            total_row = cur.execute("""
                SELECT COUNT(*) as n FROM signal_log
                WHERE signal IN ('CALL','PUT')
                  AND accuracy IN ('correct','wrong')
                  AND ctime >= COALESCE((
                      SELECT MIN(ctime) FROM (
                          SELECT ctime FROM signal_log
                          WHERE signal IN ('CALL','PUT')
                            AND accuracy IN ('correct','wrong')
                          ORDER BY ctime DESC LIMIT ?
                      )
                  ), 0)
            """, (_stats_max_rows,)).fetchone()
            total = total_row[0] if total_row else 0
        except sqlite3.OperationalError:
            # FIX (DEEP-AUDIT-2026-07-26 / F-08-10): the bare COUNT(*)
            # fallback previously counted ALL rows (including ungraded
            # and non-CALL/PUT), inflating `total_signals` and breaking
            # the consistency guarantee. Apply the same CALL/PUT + graded
            # filter here (A-04 problem 69).
            total = cur.execute(
                "SELECT COUNT(*) FROM signal_log "
                "WHERE signal IN ('CALL','PUT') "
                "AND accuracy IN ('correct','wrong')"
            ).fetchone()[0]
    if total == 0:
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-11): return the FULL schema
        # (zero/empty values) on the no-data path so callers that index
        # `result["total_graded"]` or `result["modules"]` don't crash
        # (A-04 problem 9). The `message` key is retained for callers
        # that check for it (e.g. module_performance_report.py).
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

    # FIX (DEEP-AUDIT-2026-07-26 / F-08-12): the per-module query was
    # missing the `accuracy IN ('correct','wrong')` filter and had no
    # `ctime` fallback for older schemas (A-04 problem 10). The filter
    # is critical because ungraded rows would otherwise be fetched,
    # then silently skipped inside the attribution loop (the `if
    # accuracy == "correct"/"wrong"` branches never match None). The
    # ctime fallback mirrors the total-count query above.
    try:
        rows = cur.execute("""
            SELECT asset, signal, accuracy, reasons
            FROM signal_log
            WHERE signal IN ('CALL', 'PUT')
              AND accuracy IN ('correct', 'wrong')
            ORDER BY ts DESC
            LIMIT ?
        """, (_stats_max_rows,)).fetchall()
    except sqlite3.OperationalError:
        rows = cur.execute("""
            SELECT asset, signal, accuracy, reasons
            FROM signal_log
            WHERE signal IN ('CALL', 'PUT')
              AND accuracy IN ('correct', 'wrong')
            ORDER BY ctime DESC
            LIMIT ?
        """, (_stats_max_rows,)).fetchall()

    for row in rows:
        asset = row["asset"]
        final_signal = row["signal"]
        accuracy = row["accuracy"]
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-13): use the shared
        # `parse_reasons` helper (defined above) instead of inlining the
        # same try/except isinstance block (A-04 problem 53).
        reasons = parse_reasons(row["reasons"] or "[]")

        for reason in reasons:
            reason_str = str(reason)
            # FIX (DEEP-AUDIT-2026-07-26 / F-08-14): use the shared
            # `parse_module_direction` helper. The old code had a
            # verbatim copy of the same parsing logic that lives in
            # core/auto_tune.py — both files carried the same
            # LIVE-FIX-2-25 comment block and drifted in lockstep
            # (A-04 problem 53). Normalising arrows here also fixes
            # the ASCII-arrow blind spot (A-04 problem 54).
            module, direction = parse_module_direction(reason_str, MODULE_NAMES)
            if module is None or direction is None:
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
        # FIX (DEEP-AUDIT-2026-07-26 / F-08-15): use `None` instead of 0 when
        # there's no data, so the frontend can distinguish "no data" from
        # "0% win rate" (catastrophic). Returning 0 for both masked
        # catastrophically-broken modules as "no_data" (A-04 problem 70).
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
            # Preserve numeric rounding for the non-None path; None passes
            # through `round(None, 1)` unchanged.
            "win_pct": round(win_pct, 1) if win_pct is not None else None,
            "call_win_pct": round(call_win, 1) if call_win is not None else None,
            "put_win_pct": round(put_win, 1) if put_win is not None else None,
            "call_correct": call_c,
            "call_wrong": call_w,
            "put_correct": put_c,
            "put_wrong": put_w,
        })

    # ── Overall accuracy ──────────────────────────────────────────────────
    # FIX (LIVE-FIX-BATCH-2026-07-25 / AUDIT-2-29): apply the same window as the
    # per-module query (LIMIT STATS_MAX_ROWS) so overall_win_pct is consistent
    # with the per-module win rates. The old code counted ALL rows here, which
    # disagreed with the LIMIT-ed per-module breakdown.
    try:
        acc_rows = cur.execute("""
            SELECT accuracy, COUNT(*) as n
            FROM signal_log
            WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
              AND ts >= COALESCE((
                  SELECT MIN(ts) FROM (
                      SELECT ts FROM signal_log
                      WHERE signal IN ('CALL','PUT')
                        AND accuracy IN ('correct','wrong')
                      ORDER BY ts DESC LIMIT ?
                  )
              ), 0)
            GROUP BY accuracy
        """, (_stats_max_rows,)).fetchall()
    except sqlite3.OperationalError:
        try:
            acc_rows = cur.execute("""
                SELECT accuracy, COUNT(*) as n
                FROM signal_log
                WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
                  AND ctime >= COALESCE((
                      SELECT MIN(ctime) FROM (
                          SELECT ctime FROM signal_log
                          WHERE signal IN ('CALL','PUT')
                            AND accuracy IN ('correct','wrong')
                          ORDER BY ctime DESC LIMIT ?
                      )
                  ), 0)
                GROUP BY accuracy
            """, (_stats_max_rows,)).fetchall()
        except sqlite3.OperationalError:
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
            # FIX (DEEP-AUDIT-2026-07-26 / F-08-16): drop the redundant
            # `if total_all else 0` ternary — the preceding `if total_all
            # == 0: continue` already guarantees total_all > 0 here
            # (A-04 problem 71).
            pair_modules[module_key] = {
                "display_name": display_name,
                "total": total_all,
                "correct": total_c,
                "wrong": total_w,
                "win_pct": round(total_c / total_all * 100, 1),
            }
        if pair_modules:
            pairs_summary[asset] = pair_modules

    # Connection is closed by the caller (try/finally in compute_module_stats).

    # FIX (DEEP-AUDIT-2026-07-26 / F-08-17): add `total_graded_signals` as an
    # alias for `total_signals` to make the field's actual semantics
    # (count of CALL/PUT graded rows in the sample window) explicit. The
    # legacy `total_signals` key is retained for backward compatibility —
    # both `module_performance_report.py` and the frontend read it
    # (A-04 problem 72).
    return {
        "total_signals": total,
        "total_graded_signals": total,
        "total_graded": total_graded,
        "overall_win_pct": round(overall_win, 1),
        "total_correct": total_correct,
        "total_wrong": total_wrong,
        "modules": modules_summary,
        "pairs": pairs_summary,
    }
