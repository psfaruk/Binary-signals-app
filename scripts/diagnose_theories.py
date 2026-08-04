#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Theory Performance Diagnostic — pull last 8 hours from DB, identify weak theories.

Run on Railway (where signals.db has production data):
    python scripts/diagnose_theories.py
    python scripts/diagnose_theories.py --hours 8
    python scripts/diagnose_theories.py --disable-bad  # auto-disable theories < 40% WR

Or run locally if you have a copy of the production signals.db:
    DB_PATH=/path/to/signals.db python scripts/diagnose_theories.py
"""
import sys, os, time, json
from pathlib import Path

APP_DIR = Path(__file__).parent.parent
sys.path.append(str(APP_DIR))

import db

HOURS = int(sys.argv[sys.argv.index('--hours') + 1]) if '--hours' in sys.argv else 8
DISABLE_BAD = '--disable-bad' in sys.argv
BAD_THRESHOLD = 35  # win rate below this = bad theory

print("=" * 80)
print(f"THEORY PERFORMANCE DIAGNOSTIC — Last {HOURS} hours")
print("=" * 80)

# Initialize DB (creates tables if missing)
db.init()

cutoff = time.time() - HOURS * 3600

# ════════════════════════════════════════════════════════════════════════
# 1. Check if theory_votes table exists and has data
# ════════════════════════════════════════════════════════════════════════
conn = db._conn()
cur = conn.cursor()

try:
    total = cur.execute("SELECT COUNT(*) as n FROM theory_votes WHERE ts >= ?", (cutoff,)).fetchone()
    print(f"\nTheory vote records (last {HOURS}h): {total['n']}")
    
    if total['n'] == 0:
        print("\n⚠ No theory_votes data found. Possible reasons:")
        print("  1. theory_votes table was just created — no signals graded yet")
        print("  2. The app hasn't been redeployed with the theory_votes code")
        print("  3. DB_PATH is not pointing to the production database")
        print(f"\n  DB_PATH = {db.DB_PATH}")
        
        # Check signal_log as fallback
        sig_count = cur.execute("SELECT COUNT(*) as n FROM signal_log WHERE ts >= ?", (cutoff,)).fetchone()
        print(f"  signal_log rows (last {HOURS}h): {sig_count['n']}")
        
        if sig_count['n'] > 0:
            print("\n  signal_log HAS data but theory_votes is empty.")
            print("  → The theory_votes table was added in the latest commit.")
            print("  → Redeploy the app to start populating theory_votes.")
        conn.close()
        sys.exit(0)

    # ════════════════════════════════════════════════════════════════════════
    # 2. Per-theory win rate (last N hours)
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{'Theory':<40} {'Module':<18} {'Fired':>6} {'Correct':>8} {'Wrong':>6} {'Win%':>7} {'Verdict':>8}")
    print("-" * 100)
    
    rows = cur.execute("""
        SELECT module_name, theory_name, theory_group,
               COUNT(*) as total,
               SUM(CASE WHEN vote_correct=1 THEN 1 ELSE 0 END) as correct,
               SUM(CASE WHEN vote_correct=0 THEN 1 ELSE 0 END) as wrong
        FROM theory_votes
        WHERE ts >= ? AND vote_correct IS NOT NULL
        GROUP BY module_name, theory_name
        HAVING total >= 2
        ORDER BY 100.0 * SUM(CASE WHEN vote_correct=1 THEN 1 ELSE 0 END) / COUNT(*) ASC
    """, (cutoff,)).fetchall()
    
    bad_theories = []
    good_theories = []
    
    for r in rows:
        total = r['total']
        correct = r['correct'] or 0
        wrong = r['wrong'] or 0
        win_pct = (correct / total * 100) if total > 0 else 0
        
        if win_pct < BAD_THRESHOLD:
            verdict = "❌ BAD"
            bad_theories.append((r['module_name'], r['theory_name'], win_pct, total))
        elif win_pct < 45:
            verdict = "⚠️ WEAK"
            bad_theories.append((r['module_name'], r['theory_name'], win_pct, total))
        elif win_pct >= 55:
            verdict = "✅ GOOD"
            good_theories.append((r['module_name'], r['theory_name'], win_pct, total))
        else:
            verdict = "➖ OK"
        
        print(f"  {r['theory_name']:<40} {r['module_name']:<18} {total:>6} {correct:>8} {wrong:>6} {win_pct:>6.1f}% {verdict:>8}")
    
    # ════════════════════════════════════════════════════════════════════════
    # 3. Per-pair per-theory (worst combos)
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print(f"WORST PAIR+THEORY COMBOS (last {HOURS}h)")
    print(f"{'='*80}")
    
    pair_rows = cur.execute("""
        SELECT asset, module_name, theory_name,
               COUNT(*) as total,
               SUM(CASE WHEN vote_correct=1 THEN 1 ELSE 0 END) as correct
        FROM theory_votes
        WHERE ts >= ? AND vote_correct IS NOT NULL
        GROUP BY asset, module_name, theory_name
        HAVING total >= 3
        ORDER BY 100.0 * SUM(CASE WHEN vote_correct=1 THEN 1 ELSE 0 END) / COUNT(*) ASC
        LIMIT 20
    """, (cutoff,)).fetchall()
    
    print(f"\n{'Asset':<16} {'Theory':<35} {'Fired':>6} {'Win%':>7}")
    print("-" * 70)
    for r in pair_rows:
        win = (r['correct'] / r['total'] * 100) if r['total'] > 0 else 0
        print(f"  {r['asset']:<16} {r['theory_name']:<35} {r['total']:>6} {win:>6.1f}%")
    
    # ════════════════════════════════════════════════════════════════════════
    # 4. Summary
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"  Total theories with data: {len(rows)}")
    print(f"  Good theories (≥55% WR): {len(good_theories)}")
    print(f"  Weak theories (35-45% WR): {len([t for t in bad_theories if t[2] >= 35])}")
    print(f"  Bad theories (<35% WR): {len([t for t in bad_theories if t[2] < 35])}")
    
    if bad_theories:
        print(f"\n{'='*80}")
        print("THEORIES TO REVIEW / DISABLE")
        print(f"{'='*80}")
        for mod, theory, wr, n in bad_theories:
            print(f"  ❌ {mod:<18} / {theory:<35} WR={wr:.1f}% (n={n})")
    
    # ════════════════════════════════════════════════════════════════════════
    # 5. Direction bias analysis (why theories are wrong)
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("DIRECTION BIAS — WHY THEORIES ARE WRONG")
    print(f"{'='*80}")
    
    bias_rows = cur.execute("""
        SELECT module_name, theory_name, direction,
               COUNT(*) as total,
               SUM(CASE WHEN vote_correct=1 THEN 1 ELSE 0 END) as correct
        FROM theory_votes
        WHERE ts >= ? AND vote_correct IS NOT NULL
        GROUP BY module_name, theory_name, direction
        HAVING total >= 3
        ORDER BY module_name, theory_name, direction
    """, (cutoff,)).fetchall()
    
    print(f"\n{'Theory':<35} {'Dir':<5} {'Fired':>6} {'Win%':>7} {'Issue':>20}")
    print("-" * 80)
    
    # Group by theory to see CALL vs PUT bias
    from collections import defaultdict
    theory_dir = defaultdict(dict)
    for r in bias_rows:
        theory_dir[(r['module_name'], r['theory_name'])][r['direction']] = {
            'total': r['total'], 'correct': r['correct']
        }
    
    for (mod, theory), dirs in sorted(theory_dir.items()):
        call = dirs.get('CALL', {'total': 0, 'correct': 0})
        put = dirs.get('PUT', {'total': 0, 'correct': 0})
        call_wr = (call['correct']/call['total']*100) if call['total'] > 0 else None
        put_wr = (put['correct']/put['total']*100) if put['total'] > 0 else None
        
        issue = ""
        if call_wr is not None and put_wr is not None:
            gap = abs(call_wr - put_wr)
            if gap > 20:
                worse_dir = "CALL" if call_wr < put_wr else "PUT"
                issue = f"⚠ {worse_dir} biased ({gap:.0f}pp gap)"
        
        if call_wr is not None:
            print(f"  {theory:<35} CALL  {call['total']:>6} {call_wr:>6.1f}% {issue:>20}")
        if put_wr is not None:
            print(f"  {theory:<35} PUT   {put['total']:>6} {put_wr:>6.1f}%")
    
    # ════════════════════════════════════════════════════════════════════════
    # 6. Auto-disable bad theories (if --disable-bad flag)
    # ════════════════════════════════════════════════════════════════════════
    if DISABLE_BAD and bad_theories:
        really_bad = [t for t in bad_theories if t[2] < BAD_THRESHOLD]
        if really_bad:
            print(f"\n{'='*80}")
            print(f"AUTO-DISABLE: {len(really_bad)} theories below {BAD_THRESHOLD}% WR")
            print(f"{'='*80}")
            for mod, theory, wr, n in really_bad:
                print(f"  Would disable: {mod}/{theory} (WR={wr:.1f}%, n={n})")
            print("\n  To actually disable, edit the module source code and")
            print("  remove or comment out the theory's detection block.")
    
    conn.close()

except Exception as e:
    print(f"\nError: {e}")
    import traceback
    traceback.print_exc()
    conn.close()
