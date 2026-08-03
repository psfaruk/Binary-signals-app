"""engines/base/trap_hours.py — Auto-skip trap hours.

Generated from live Railway brain data (/api/quotex-algo-detect).
A trap hour is an (asset, hour_utc) where the pair historically loses
>= 65% of trades with >= 5 samples. The engine router checks this map
BEFORE running the prediction — if (asset, current_utc_hour) is here,
the signal is suppressed (returns NEUTRAL with reason="trap_hour").

Regenerate with: python /home/z/my-project/scripts/generate_calibration.py
"""

# Map: asset -> set of UTC hours where signals are suppressed.
# Format: { "EURUSD_otc": {0, 17, 19}, ... }
TRAP_HOURS = {
    "AUDUSD_otc": frozenset({0}),
    "BRLUSD_otc": frozenset({2, 18}),
    "EURUSD_otc": frozenset({1, 17, 18, 20}),
    "USDARS_otc": frozenset({1, 19}),
    "USDBDT_otc": frozenset({20}),
    "USDCHF": frozenset({2}),
    "USDCHF_otc": frozenset({0, 1}),
    "USDCOP_otc": frozenset({21}),
    "USDDZD_otc": frozenset({19}),
    "USDIDR_otc": frozenset({18}),
    "USDJPY_otc": frozenset({1, 19, 23}),
    "USDPKR_otc": frozenset({1}),
}


def is_trap_hour(asset: str, hour_utc: int) -> bool:
    """Return True if (asset, hour_utc) is in the trap-hours suppression map."""
    hours = TRAP_HOURS.get(asset)
    if not hours:
        return False
    return hour_utc in hours


def trap_reason(asset: str, hour_utc: int) -> str:
    """Return a human-readable reason for suppression (or empty string)."""
    if is_trap_hour(asset, hour_utc):
        return f"trap hour {hour_utc}:00 UTC for {asset} - historically loses >=65% of trades here"
    return ""
