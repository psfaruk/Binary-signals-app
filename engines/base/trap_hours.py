"""engines/base/trap_hours.py — Auto-skip trap hours."""

# Map: asset -> set of UTC hours where signals are suppressed.
TRAP_HOURS = {}


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
