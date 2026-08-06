"""engines/base/direction_bias.py — Per-pair per-module direction lock."""

# (asset, module_name) -> "CALL" or "PUT"
# A module vote in the LOCKED direction passes through unchanged.
# A module vote in the OPPOSITE direction is suppressed (score=0).
DIRECTION_LOCK = {
}


def locked_direction(asset: str, module_name: str):
    """Return the locked direction for (asset, module) or None if no lock."""
    return DIRECTION_LOCK.get((asset, module_name))


def is_vote_allowed(asset: str, module_name: str, direction: str) -> bool:
    """Return True if the module's vote in `direction` is allowed."""
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
            f"(vote {direction} suppressed)")
