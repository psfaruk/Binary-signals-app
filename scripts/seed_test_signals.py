#!/usr/bin/env python3
"""Seed signals.db with synthetic graded signals to verify the Result tab
master-detail UI end-to-end (winrate list, pair drill-in, logs, live card).
Rows are tagged with a marker so they can be wiped after the test."""
import sqlite3
import time
import random
import sys

DB = '/home/z/my-project/Binary-signals-app/signals.db'
MARKER = 'SEEDTEST'  # stored in reasons so we can delete exactly these rows

PAIRS = [
    ('EURUSD_otc', 'otc'), ('GBPUSD_otc', 'otc'), ('USDJPY_otc', 'otc'),
    ('AUDCAD_otc', 'otc'), ('NZDJPY_otc', 'otc'), ('EURJPY_otc', 'otc'),
    ('USDBRL_otc', 'otc'), ('USDINR_otc', 'otc'), ('USDPKR_otc', 'otc'),
    ('USDBDT_otc', 'otc'), ('EURUSD', 'real'),
]

def main():
    random.seed(20260907)
    db = sqlite3.connect(DB)
    cur = db.cursor()
    # Detect the actual signal_log schema first.
    cols = [r[1] for r in cur.execute('PRAGMA table_info(signal_log)')]
    print('signal_log columns:', cols)
    now = int(time.time())
    # 6 hours of 60s candles → 360 candles per pair
    n_rows = 0
    for asset, cat in PAIRS:
        # Give each pair a distinct call/put bias so the UI shows variety.
        call_win_p = random.uniform(0.35, 0.75)
        put_win_p = random.uniform(0.35, 0.75)
        for i in range(360):
            ctime = now - (360 - i) * 60
            ctime = ctime - (ctime % 60)
            sig = random.choice(['CALL', 'PUT'])
            # outcome: correct/wrong/draw/pending mix; older ones graded
            if i >= 356:
                acc = 'pending'
            else:
                r = random.random()
                base = call_win_p if sig == 'CALL' else put_win_p
                if r < 0.04:
                    acc = 'draw'
                elif r < base:
                    acc = 'correct'
                else:
                    acc = 'wrong'
            score = random.randint(-6, 6)
            strength = random.choice(['STRONG', 'MEDIUM', 'WEAK'])
            conf = round(random.uniform(50, 92), 1)
            regime = random.choice(['TREND_UP', 'TREND_DOWN', 'RANGE', 'VOLATILE'])
            row = {
                'asset': asset, 'period': 60, 'ctime': ctime,
                'signal': sig, 'accuracy': acc, 'score': score,
                'strength': strength, 'confidence': conf,
                'regime': regime, 'category': cat,
                'reasons': MARKER,   # wipe-marker: --wipe deletes exactly these rows
            }
            # Build INSERT dynamically from intersection with real columns.
            keys = [k for k in row if k in cols]
            placeholders = ','.join('?' for _ in keys)
            sql = f'INSERT INTO signal_log ({",".join(keys)}) VALUES ({placeholders})'
            vals = [row[k] if not isinstance(row[k], dict) else str(row[k]) for k in keys]
            # tag marker if a reasons/details column exists
            if 'reasons' in keys:
                vals[keys.index('reasons')] = MARKER
            cur.execute(sql, vals)
            n_rows += 1
    db.commit()
    print('inserted', n_rows, 'seed rows')
    db.close()

if __name__ == '__main__':
    if '--wipe' in sys.argv:
        db = sqlite3.connect(DB)
        cur = db.cursor()
        cols = [r[1] for r in cur.execute('PRAGMA table_info(signal_log)')]
        if 'reasons' in cols:
            n = cur.execute("DELETE FROM signal_log WHERE reasons='SEEDTEST'").rowcount
        else:
            n = cur.execute('DELETE FROM signal_log').rowcount
        db.commit()
        print('wiped', n, 'rows')
        db.close()
    else:
        main()
