"""engines/base/disabled_pairs.py — Pairs with chronically low win rate."""

# Pairs with chronically low win rate that get a confidence penalty.
PENALIZED_PAIRS = {
}

# Backward-compat: empty set — no pair is fully blocked anymore.
DISABLED_PAIRS = frozenset()


def is_pair_disabled(asset: str) -> bool:
    """Return True if the asset is fully blocked. Always False now."""
    return False


def disabled_reason(asset: str) -> str:
    """Return a human-readable reason for full block. Always empty now."""
    return ""


def pair_penalty(asset: str) -> float:
    """Return the confidence multiplier for a penalized pair (0.5-1.0)."""
    return PENALIZED_PAIRS.get(asset, 1.0)


def penalty_reason(asset: str) -> str:
    """Return a human-readable reason for the penalty, or empty string."""
    mult = PENALIZED_PAIRS.get(asset)
    if mult is None or mult == 1.0:
        return ""
    return (f"_PAIR_PENALTY: {asset} has chronically low win rate "
            f"-> confidence x{mult:.2f} (live data shows < 45% win)")
