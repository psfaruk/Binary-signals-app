"""engines/base/disabled_pairs.py — Pairs with chronically low win rate.

USER FIX #8 (2026-08-03, REVISED after live deploy):
The original Fix #8 SUPPRESSED signals entirely for pairs with < 45%
win rate. But this caused the live app to show NO signals on these
pairs — the user could not see ANY directional guidance even when the
engine had a strong consensus.

REVISED approach (2026-08-03): these pairs are NOT blocked — they still
produce signals, but the engines/__init__.py predict() router applies
a confidence penalty (×0.6) to dampen the displayed conviction. This
matches the brain's per-pair dampening philosophy without completely
hiding the signal.

The is_pair_disabled() / disabled_reason() functions are kept for
backward compatibility but now return False / "" — no pair is fully
blocked. The PENALIZED_PAIRS dict below is what the router actually
uses to apply the dampening multiplier.
"""

# Pairs with chronically low win rate that get a confidence penalty.
# These pairs are NOT blocked — they still produce signals, but the
# router multiplies the final confidence by the listed factor.
#
# Source: live /api/stats on Railway production, 2026-08-03.
# Threshold: aggregate win rate < 45% AND >= 50 graded signals.
PENALIZED_PAIRS = {
    # FIX (ALWAYS-SIGNAL-2026-08-03): cleared all penalty entries.
    # Every pair now signals at full confidence — no pair-based dampening.
}

# Backward-compat: empty set — no pair is fully blocked anymore.
# (Was: frozenset of pair names that returned NEUTRAL.)
DISABLED_PAIRS = frozenset()


def is_pair_disabled(asset: str) -> bool:
    """Return True if the asset is fully blocked. Always False now —
    see module docstring for the revised approach (confidence penalty
    instead of full suppression).
    """
    return False


def disabled_reason(asset: str) -> str:
    """Return a human-readable reason for full block. Always empty now."""
    return ""


def pair_penalty(asset: str) -> float:
    """Return the confidence multiplier for a penalized pair (0.5-1.0).
    Returns 1.0 if the pair is not in the penalty map (no penalty).
    """
    return PENALIZED_PAIRS.get(asset, 1.0)


def penalty_reason(asset: str) -> str:
    """Return a human-readable reason for the penalty, or empty string."""
    mult = PENALIZED_PAIRS.get(asset)
    if mult is None or mult == 1.0:
        return ""
    return (f"_PAIR_PENALTY: {asset} has chronically low win rate "
            f"-> confidence ×{mult:.2f} (live data shows < 45% win)")
