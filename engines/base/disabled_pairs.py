"""engines/base/disabled_pairs.py — Disable pairs with chronically low win rate.

USER FIX #8 (2026-08-03): pairs with < 45% aggregate win rate are
DISABLED entirely — the engine returns NEUTRAL with reason="pair_disabled"
for these assets, regardless of what the modules vote.

Live brain data (/api/stats pairs) shows these pairs have negative
expected value over 200+ graded signals — the engine's per-module
dampening cannot rescue them, so the cleanest fix is to suppress them
entirely. The user can still SEE the chart and watch the price, but no
signal will be emitted.

Regeneration: re-run /home/z/my-project/scripts/generate_calibration.py
after ~1000 more signals accumulate; it refreshes this list from the
latest /api/stats pairs endpoint.
"""

# Set of asset names with chronically low (< 45%) win rate.
# Source: live /api/stats on Railway production, 2026-08-03.
# Threshold: aggregate win rate < 45% AND >= 50 graded signals.
DISABLED_PAIRS = frozenset({
    "USDCOP_otc",    # 44.9% win (n=205) — 5 of 6 modules dampened, still losing
    "BRLUSD_otc",    # 45.1% win (n=266) — worst pair, all modules dampened
    "USDBDT_otc",    # 45.9% win (n=209) — otc_pattern + indicator disabled
    "GBPUSD_otc",    # 33.3% win (n=24)  — small sample but extremely poor
    # USDMXN_otc (25% n=5) and USDCHF (0% n=6) are below threshold but have
    # too few samples to be confidently disabled — left active for now.
})


def is_pair_disabled(asset: str) -> bool:
    """Return True if the asset is in the disabled-pairs blocklist."""
    return asset in DISABLED_PAIRS


def disabled_reason(asset: str) -> str:
    """Return a human-readable reason for suppression, or empty string."""
    if is_pair_disabled(asset):
        return (f"_PAIR_DISABLED: {asset} is in the disabled-pairs blocklist "
                f"(chronically < 45% win rate over 200+ graded signals). "
                f"Signal suppressed to prevent negative-EV trades.")
    return ""
