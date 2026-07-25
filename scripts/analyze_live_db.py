#!/usr/bin/env python3
"""
Download the signals.db from Railway and run a comprehensive analysis.

USAGE:
    # Set these env vars first (see scripts/setup_railway_token.py --list):
    export RAILWAY_API_TOKEN="railway-..."
    export RAILWAY_PROJECT_ID="prj-..."
    export RAILWAY_SERVICE_ID="svc-..."

    # Download DB + run analysis:
    python3 scripts/analyze_live_db.py

    # Or just download (no analysis):
    python3 scripts/analyze_live_db.py download

    # Or analyze an already-downloaded DB:
    python3 scripts/analyze_live_db.py analyze --db /path/to/signals.db

The script:
1. Connects to your Railway service via SSH (Railway plugin not required)
2. Runs `sqlite3 /data/signals.db .dump` inside the container
3. Reconstructs the DB locally
4. Runs 18 analysis queries on the local copy
5. Outputs a Markdown report with findings

ALTERNATIVE (if Railway SSH isn't available):
1. Open Railway dashboard → your service → Settings → "Volume Mount"
2. Note the volume mount path (typically /data)
3. Use `railway ssh` (Railway CLI) to enter the container
4. Run: sqlite3 /data/signals.db .dump > signals_dump.sql
5. Download signals_dump.sql
6. Reconstruct: sqlite3 /home/z/my-project/Binary-signals-app/signals.db < signals_dump.sql
7. Then: python3 scripts/analyze_live_db.py analyze --db signals.db
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "signals.db"


# ════════════════════════════════════════════════════════════════════════════
# DB DOWNLOAD (Railway SSH approach)
# ════════════════════════════════════════════════════════════════════════════

def cmd_download(args):
    """Download signals.db from Railway via SSH.

    Requires Railway CLI (https://railway.app/cli) installed and authenticated.
    Falls back to instructing the user how to download manually.
    """
    print("📥 Downloading signals.db from Railway...\n")

    # Check if railway CLI is available
    try:
        result = subprocess.run(
            ["railway", "version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            raise FileNotFoundError()
        print(f"  Railway CLI found: {result.stdout.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("❌ Railway CLI not found. Install it first:")
        print("   npm install -g @railway/cli")
        print("   railway login")
        print()
        print("Alternatively, download the DB manually:")
        print("  1. Open Railway dashboard → your service")
        print("  2. Click 'Settings' → 'Volumes'")
        print("  3. Note the mount path (usually /data)")
        print("  4. Open a shell in the container:")
        print("     railway shell  (or Railway dashboard → 'Shell' tab)")
        print("  5. Run inside the shell:")
        print("     sqlite3 /data/signals.db .dump > /tmp/dump.sql")
        print("     cat /tmp/dump.sql  # copy the output to your clipboard")
        print("  6. Save it locally as signals_dump.sql")
        print("  7. Reconstruct locally:")
        print("     sqlite3 signals.db < signals_dump.sql")
        print("  8. Run analysis:")
        print("     python3 scripts/analyze_live_db.py analyze --db signals.db")
        sys.exit(1)

    # Try railway ssh to dump the DB
    print("\n🔌 Connecting to Railway service via SSH...")
    print("   (this opens an interactive shell — type 'exit' when done)\n")

    db_path_in_container = args.db_path or "/data/signals.db"
    dump_script = f"sqlite3 {db_path_in_container} .dump"

    print(f"   Will run inside container: {dump_script}")
    print(f"   Saving to: {args.output or DEFAULT_DB_PATH}\n")

    # Use railway ssh with a command
    output_path = args.output or str(DEFAULT_DB_PATH)
    sql_dump_path = output_path + ".sql"

    try:
        with open(sql_dump_path, "w") as f:
            result = subprocess.run(
                ["railway", "ssh", "-c", dump_script],
                stdout=f, stderr=subprocess.PIPE, text=True, timeout=120
            )
            if result.returncode != 0:
                print(f"❌ Railway SSH failed: {result.stderr}")
                sys.exit(1)
        print(f"  ✅ SQL dump saved: {sql_dump_path}")

        # Reconstruct DB from dump
        if os.path.exists(output_path):
            os.remove(output_path)
        result = subprocess.run(
            ["sqlite3", output_path],
            stdin=open(sql_dump_path),
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"❌ sqlite3 reconstruct failed: {result.stderr}")
            sys.exit(1)
        print(f"  ✅ DB reconstructed: {output_path}")
        print(f"\n   DB size: {os.path.getsize(output_path) / 1024:.1f} KB")
        print(f"\n   Now run analysis:")
        print(f"   python3 scripts/analyze_live_db.py analyze --db {output_path}")

    except subprocess.TimeoutExpired:
        print("❌ Railway SSH timed out (120s). Try manually:")
        print(f"  railway ssh")
        print(f"  > sqlite3 {db_path_in_container} .dump > /tmp/dump.sql")
        print(f"  > exit")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Download failed: {e}")
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# DB ANALYSIS — 18 queries that surface prediction failures
# ════════════════════════════════════════════════════════════════════════════

def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f"❌ DB not found: {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _q(conn, sql, label, params=()):
    """Run a query and pretty-print the result."""
    cur = conn.cursor()
    rows = cur.execute(sql, params).fetchall()
    if not rows:
        print(f"\n## {label}\n(no rows)\n")
        return rows
    cols = [d[0] for d in cur.description]
    print(f"\n## {label}")
    print("| " + " | ".join(cols) + " |")
    print("|" + "|".join(["---"] * len(cols)) + "|")
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    print()
    return rows


def cmd_analyze(args):
    """Run 18 analysis queries against a local signals.db."""
    db_path = args.db or str(DEFAULT_DB_PATH)
    print(f"🔍 Analyzing DB: {db_path}")
    print(f"   File size: {os.path.getsize(db_path) / 1024:.1f} KB\n")

    conn = _connect(db_path)

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 1: Overview — what's in the DB?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 1: OVERVIEW")
    print("=" * 70)

    _q(conn, """
        SELECT
            COUNT(*) AS total_signals,
            COUNT(DISTINCT asset) AS unique_assets,
            MIN(datetime(ctime, 'unixepoch')) AS first_signal,
            MAX(datetime(ctime, 'unixepoch')) AS last_signal,
            (MAX(ctime) - MIN(ctime)) / 86400.0 AS days_live
        FROM signal_log
    """, "1.1 — Total signals collected")

    _q(conn, """
        SELECT
            COUNT(*) AS total_candles,
            COUNT(DISTINCT asset) AS unique_assets,
            SUM(tick_count) AS total_ticks
        FROM candle_micro
    """, "1.2 — Total candles captured")

    _q(conn, """
        SELECT category, COUNT(*) AS n,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM signal_log), 1) AS pct
        FROM signal_log
        GROUP BY category
    """, "1.3 — Signals by category (otc vs real)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 2: Overall Win Rate — how accurate is the engine?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 2: OVERALL WIN RATE")
    print("=" * 70)

    _q(conn, """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN accuracy='draw' THEN 1 ELSE 0 END) AS draws,
            SUM(CASE WHEN accuracy IS NULL OR accuracy='' THEN 1 ELSE 0 END) AS ungraded,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate_pct
        FROM signal_log
    """, "2.1 — Overall win rate (correct / wrong only)")

    _q(conn, """
        SELECT
            signal,
            COUNT(*) AS n,
            SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) AS losses,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate_pct
        FROM signal_log
        WHERE signal IN ('CALL', 'PUT')
        GROUP BY signal
    """, "2.2 — Win rate by direction (CALL vs PUT bias check)")

    _q(conn, """
        SELECT
            category,
            signal,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate_pct
        FROM signal_log
        WHERE signal IN ('CALL', 'PUT')
        GROUP BY category, signal
        ORDER BY category, signal
    """, "2.3 — Win rate by category × direction (asymmetry check)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 3: Per-Pair Performance — which pairs are bleeding money?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 3: PER-PAIR PERFORMANCE")
    print("=" * 70)

    _q(conn, """
        SELECT
            asset,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate,
            SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) AS losses
        FROM signal_log
        WHERE signal IN ('CALL', 'PUT')
        GROUP BY asset
        HAVING n >= 10
        ORDER BY win_rate ASC
        LIMIT 20
    """, "3.1 — Worst 20 pairs by win rate (min 10 signals)")

    _q(conn, """
        SELECT
            asset,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL', 'PUT')
        GROUP BY asset
        HAVING n >= 10
        ORDER BY win_rate DESC
        LIMIT 20
    """, "3.2 — Best 20 pairs by win rate")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 4: Per-Module Performance — which theories are wrong?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 4: PER-MODULE / THEORY PERFORMANCE")
    print("=" * 70)

    # Theories are JSON-encoded in `theories` field. Extract first theory code.
    _q(conn, """
        SELECT
            json_extract(value, '$.code') AS theory_code,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log, json_each(theories)
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY theory_code
        ORDER BY win_rate ASC
    """, "4.1 — Per-theory win rate (extracted from JSON)")

    _q(conn, """
        SELECT
            strength,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT')
        GROUP BY strength
        ORDER BY n DESC
    """, "4.2 — Win rate by strength tier (STRONG/MEDIUM/LOW)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 5: Confidence Calibration — does confidence match accuracy?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 5: CONFIDENCE CALIBRATION")
    print("=" * 70)

    _q(conn, """
        SELECT
            CASE
                WHEN confidence >= 90 THEN '90-100'
                WHEN confidence >= 80 THEN '80-89'
                WHEN confidence >= 70 THEN '70-79'
                WHEN confidence >= 60 THEN '60-69'
                WHEN confidence >= 50 THEN '50-59'
                WHEN confidence >= 40 THEN '40-49'
                WHEN confidence >= 30 THEN '30-39'
                ELSE '<30'
            END AS conf_bin,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS actual_win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY conf_bin
        ORDER BY conf_bin DESC
    """, "5.1 — Calibration: predicted confidence vs actual win rate")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 6: Time-of-Day Patterns — when is the engine wrong?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 6: TIME-OF-DAY PATTERNS")
    print("=" * 70)

    _q(conn, """
        SELECT
            CAST(strftime('%H', datetime(ctime, 'unixepoch')) AS INT) AS hour_utc,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY hour_utc
        ORDER BY hour_utc
    """, "6.1 — Win rate by hour of day (UTC)")

    _q(conn, """
        SELECT
            CAST(strftime('%w', datetime(ctime, 'unixepoch')) AS INT) AS day_of_week,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY day_of_week
        ORDER BY day_of_week
    """, "6.2 — Win rate by day of week (0=Sun, 6=Sat)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 7: Regime Analysis — which market regime is hardest?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 7: REGIME & ZONE ANALYSIS")
    print("=" * 70)

    _q(conn, """
        SELECT
            regime,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate,
            SUM(CASE WHEN signal='CALL' THEN 1 ELSE 0 END) AS call_count,
            SUM(CASE WHEN signal='PUT' THEN 1 ELSE 0 END) AS put_count
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
        GROUP BY regime
        ORDER BY win_rate ASC
    """, "7.1 — Win rate by regime (TREND_UP/DOWN/SIDEWAYS/VOLATILE/RANGE)")

    _q(conn, """
        SELECT
            zone,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong') AND zone IS NOT NULL AND zone != ''
        GROUP BY zone
        ORDER BY win_rate ASC
    """, "7.2 — Win rate by zone (LONDON/NY/TOKYO/SYDNEY/OVERLAP)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 8: Recent Performance — is the engine degrading?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 8: RECENT PERFORMANCE (DRIFT DETECTION)")
    print("=" * 70)

    _q(conn, """
        SELECT
            DATE(datetime(ctime, 'unixepoch')) AS day,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate,
            SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN accuracy='wrong' THEN 1 ELSE 0 END) AS losses
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
          AND ctime >= (strftime('%s','now') - 7*86400)
        GROUP BY day
        ORDER BY day DESC
    """, "8.1 — Daily win rate (last 7 days) — drift detection")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 9: Tag Analysis — which tag combinations work?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 9: TAG ANALYSIS")
    print("=" * 70)

    _q(conn, """
        SELECT
            tags,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
          AND tags IS NOT NULL AND tags != ''
        GROUP BY tags
        HAVING n >= 5
        ORDER BY win_rate ASC
        LIMIT 30
    """, "9.1 — Top 30 worst tag combinations (min 5 signals)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 10: Wrong Signal Analysis — what's going wrong?
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 10: WRONG SIGNALS DEEP DIVE")
    print("=" * 70)

    _q(conn, """
        SELECT
            asset,
            signal,
            confidence,
            strength,
            regime,
            reasons,
            postmortem
        FROM signal_log
        WHERE accuracy='wrong'
          AND signal IN ('CALL','PUT')
        ORDER BY confidence DESC
        LIMIT 10
    """, "10.1 — Top 10 wrong HIGH-confidence signals (worst false positives)")

    _q(conn, """
        SELECT
            SUBSTR(reasons, 1, 80) AS reason_preview,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN accuracy='correct' THEN 1 ELSE 0 END) /
                  NULLIF(SUM(CASE WHEN accuracy IN ('correct','wrong') THEN 1 ELSE 0 END), 0), 2) AS win_rate
        FROM signal_log
        WHERE signal IN ('CALL','PUT') AND accuracy IN ('correct','wrong')
          AND reasons IS NOT NULL AND reasons != ''
        GROUP BY SUBSTR(reasons, 1, 80)
        HAVING n >= 5
        ORDER BY win_rate ASC
        LIMIT 20
    """, "10.2 — Worst 20 reason patterns (min 5 signals)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 11: Candle Microstructure Patterns
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 11: CANDLE MICROSTRUCTURE PATTERNS")
    print("=" * 70)

    _q(conn, """
        SELECT
            pressure,
            COUNT(*) AS n,
            ROUND(AVG(buy_pct), 1) AS avg_buy_pct,
            ROUND(AVG(sell_pct), 1) AS avg_sell_pct,
            ROUND(AVG(tick_count), 1) AS avg_ticks
        FROM candle_micro
        WHERE pressure IS NOT NULL AND pressure != ''
        GROUP BY pressure
        ORDER BY n DESC
    """, "11.1 — Pressure distribution (BUY/SELL/NEUTRAL/FIGHT)")

    _q(conn, """
        SELECT
            reaction,
            COUNT(*) AS n
        FROM candle_micro
        WHERE reaction IS NOT NULL AND reaction != ''
        GROUP BY reaction
        ORDER BY n DESC
        LIMIT 20
    """, "11.2 — Most common candle reactions (top 20)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 12: Brain-learned Weights (if brain_predictions table exists)
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("SECTION 12: BRAIN-LEARNED WEIGHTS (if available)")
    print("=" * 70)

    # Check if brain tables exist
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"\nAvailable tables: {tables}\n")

    if "brain_predictions" in tables:
        _q(conn, """
            SELECT
                asset, module,
                COUNT(*) AS n,
                ROUND(AVG(weight), 3) AS avg_weight,
                ROUND(AVG(win_rate), 3) AS avg_win_rate
            FROM brain_predictions
            GROUP BY asset, module
            ORDER BY asset, avg_win_rate ASC
            LIMIT 30
        """, "12.1 — Per-asset per-module brain weights (worst first)")
    else:
        print("(brain_predictions table not found — skipping)\n")

    if "algorithm_changes" in tables:
        _q(conn, """
            SELECT
                asset,
                change_type,
                COUNT(*) AS n,
                MAX(datetime(ts, 'unixepoch')) AS last_seen
            FROM algorithm_changes
            GROUP BY asset, change_type
            ORDER BY n DESC
            LIMIT 20
        """, "12.2 — Algorithm changes detected per asset")
    else:
        print("(algorithm_changes table not found — skipping)\n")

    # ════════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ════════════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("ANALYSIS COMPLETE")
    print("=" * 70)
    print(f"\nDB analyzed: {db_path}")
    print(f"Save this output and share it for deeper analysis.")
    print(f"\nNext steps:")
    print(f"  1. Identify worst pairs / modules / regimes from the report")
    print(f"  2. Cross-reference with code audit findings (worklog.md)")
    print(f"  3. Prioritize fixes that target the BIGGEST losing segments")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Download and analyze the live signals.db from Railway"
    )
    sub = parser.add_subparsers(dest="command")

    p_download = sub.add_parser("download", help="Download signals.db from Railway")
    p_download.add_argument("--output", help="Output path for downloaded DB")
    p_download.add_argument("--db-path", default="/data/signals.db",
                            help="DB path inside the container (default: /data/signals.db)")
    p_download.set_defaults(func=cmd_download)

    p_analyze = sub.add_parser("analyze", help="Analyze a local signals.db")
    p_analyze.add_argument("--db", help="Path to signals.db (default: ./signals.db)")
    p_analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        print("\nQuick start:")
        print("  python3 scripts/analyze_live_db.py download   # get DB from Railway")
        print("  python3 scripts/analyze_live_db.py analyze    # run 18 queries on local DB")
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()
