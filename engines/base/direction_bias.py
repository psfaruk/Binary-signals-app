"""engines/base/direction_bias.py — Per-pair per-module direction lock.

USER FIX #7 (2026-08-03): exploit Quotex's fixed direction biases.

Live brain data (/api/brain/learning) shows 18 pair+module combinations
where CALL and PUT win rates differ by >=15 percentage points. Quotex's
broker algorithm has a structural bias at specific (asset, module)
combos — it consistently favors one direction.

This module enforces that bias: when a (asset, module_name) is in the
DIRECTION_LOCK map, the module's vote is filtered to ONLY count in the
locked direction. A vote in the opposite direction is suppressed
(score -> 0) before the blender combines it with other modules.

Map generated from /api/brain/learning via /home/z/my-project/scripts/generate_calibration.py.
Entries are only included when both CALL and PUT have >=8 samples AND
the win-rate gap is >=15 percentage points.

Regeneration: re-run the calibration script after ~1000 more signals
accumulate; it will refresh this map from the latest live brain data.
"""

# (asset, module_name) -> "CALL" or "PUT"
# A module vote in the LOCKED direction passes through unchanged.
# A module vote in the OPPOSITE direction is suppressed (score=0).
#
# Source: live /api/brain/learning on Railway production, 2026-08-03.
# Only includes pair+module combos with >=8 samples in BOTH CALL and PUT
# AND a win-rate gap >=15 percentage points.
DIRECTION_LOCK = {
    # CALL-biased (PUT suppressed)
    ("USDPKR_otc",  "running_tick"):    "CALL",   # CALL 54% vs PUT 33%
    ("USDCHF_otc",  "pattern"):         "CALL",   # CALL 65% vs PUT 50%
    ("EURUSD_otc",  "candle_reaction"): "CALL",   # CALL 56% vs PUT 41%
    # PUT-biased (CALL suppressed)
    ("USDJPY_otc",  "key_level"):       "PUT",    # CALL 41% vs PUT 61%
    ("USDCOP_otc",  "running_tick"):    "PUT",    # CALL 34% vs PUT 52%
    ("USDCOP_otc",  "key_level"):       "PUT",    # CALL 33% vs PUT 60%
    ("AUDUSD_otc",  "running_tick"):    "PUT",    # CALL 46% vs PUT 64%
    ("AUDUSD_otc",  "pattern"):         "PUT",    # CALL 36% vs PUT 52%
    ("AUDUSD_otc",  "candle_reaction"): "PUT",    # CALL 47% vs PUT 64%
    # FIX (MODULE-PRUNE-2026-08-03): removed 9 entries that referenced
    # deleted modules (indicator, otc_pattern). Kept only entries for the
    # 4 surviving modules: candle_reaction, running_tick, pattern, key_level.
}


def locked_direction(asset: str, module_name: str):
    """Return the locked direction for (asset, module) or None if no lock.

    Returns:
        "CALL", "PUT", or None (no lock — module can vote either way).
    """
    return DIRECTION_LOCK.get((asset, module_name))


def is_vote_allowed(asset: str, module_name: str, direction: str) -> bool:
    """Return True if the module's vote in `direction` is allowed.

    If (asset, module) is not in the lock map, all directions are allowed.
    If it IS in the lock map, only the locked direction is allowed.
    """
    lock = DIRECTION_LOCK.get((asset, module_name))
    if lock is None:
        return True
    return direction == lock


def suppression_reason(asset: str, module_name: str, direction: str) -> str:
    """Return a human-readable reason for suppression, or empty string."""
    lock = DIRECTION_LOCK.get((asset, module_name))
    if lock is None or direction == lock:
        return ""
    return (f"_DIR_LOCK: {module_name} on {asset} is locked to {lock} "
            f"(vote {direction} suppressed — live data shows {direction} "
            f"loses consistently on this combo)")
