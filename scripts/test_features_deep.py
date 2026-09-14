"""Quick sanity test for features_deep (lock + values + timing)."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from core.otc_predict.features_deep import (
    DEEP_FEATURE_NAMES, build_deep_row, verify_deep_lock, DEEP_BLOCK_NAMES)
from core.otc_dataset import load_candles_from_db

DB = os.path.join(REPO, "signals.db")
assert os.path.exists(DB), f"signals.db not found at {DB} — run the app once"

# 1) names / blocks consistency
all_named = set(DEEP_FEATURE_NAMES)
for blk, names in DEEP_BLOCK_NAMES.items():
    for n in names:
        assert n in all_named, f"{n} missing from DEEP_FEATURE_NAMES"
assert len(DEEP_FEATURE_NAMES) == len(set(DEEP_FEATURE_NAMES)), "dup names"
print(f"deep features: {len(DEEP_FEATURE_NAMES)} "
      f"(time={len(DEEP_BLOCK_NAMES['time'])}, "
      f"serial={len(DEEP_BLOCK_NAMES['serial'])}, "
      f"regime={len(DEEP_BLOCK_NAMES['regime'])}, "
      f"inter={len(DEEP_BLOCK_NAMES['inter'])}, "
      f"micro={len(DEEP_BLOCK_NAMES['micro'])})")

# 2) build on real DB candles
cd = load_candles_from_db(DB)
candles = cd["AUDJPY_otc"]
w = candles[:50]
row = build_deep_row(w)
missing = [k for k in DEEP_FEATURE_NAMES if k not in row]
assert not missing, f"missing keys: {missing}"
extra = [k for k in row if k not in DEEP_FEATURE_NAMES]
assert not extra, f"extra keys: {extra}"
assert all(isinstance(v, float) for v in row.values())
print("sample values:", {k: row[k] for k in (
    "tod_sin", "hour_f", "is_weekend", "autocorr_lag1", "hurst_50",
    "bigram_entropy_50", "cond_up_after_up_50", "last3_pattern",
    "regime_trend_score", "micro_tick_z50")})

# 3) time features correct? candles[49] is 2880-1 min before last
import time as _t
t = candles[49]["time"]
st = _t.gmtime(t)
assert row["hour_f"] == float(st.tm_hour), (row["hour_f"], st.tm_hour)
print(f"time check ok: candle@{t} → utc hour {st.tm_hour} dow {st.tm_wday}")

# 4) timing
t0 = time.time()
for i in range(50, 150):
    build_deep_row(candles[i - 49: i + 1])
per = (time.time() - t0) / 100 * 1000
print(f"build_deep_row: {per:.2f} ms/row")

# 5) perturbation lock on a slice (future must not leak)
n, fut, self_ = verify_deep_lock(candles[:300], n_checks=30)
print(f"verify_deep_lock: checks={n} future_hits={fut} self_hits={self_}")
assert fut == 0, "LEAK: future candles changed deep features"
assert self_ > 0, "vacuous: self mutation changed nothing"

# 6) live-style window WITHOUT micro fields must not crash
bare = [{k: c[k] for k in ("time", "open", "high", "low", "close")}
        for c in candles[:50]]
row_bare = build_deep_row(bare)
assert row_bare["micro_tick_z50"] == 0.0
assert row_bare["micro_fight_rate_20"] == 0.0
print("bare-window (no micro) ok — neutral micro values")

print("\nALL DEEP-FEATURE CHECKS PASSED")
