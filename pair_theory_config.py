"""
Per-pair theory configuration — auto-generated from DB theory_votes data.

Each pair gets ONLY theories with >=50% win rate (min 3 votes).
Theories below 50% are muted for that specific pair.

This is DATA-DRIVEN: the config is regenerated from actual signal history.
As more signals accumulate, the config auto-updates via _refresh_theory_mutes.

Generated: 2026-07-13
Total pairs: 16
"""

# All available theories (for reference)
ALL_THEORIES = [
    "CON", "REV", "RUN", "TRAP", "GAP", "RNG", "MICRO", "MEAN",
    "VELOCITY", "LIVE_WICK", "ORDERFLOW", "HOLD",
    "CONTINUITY", "HISTORY", "OB", "SWEEP", "STRUCT", "MST", "ZZ",
]

# Per-pair configuration: which theories are ENABLED (>=50% win rate)
# Any theory NOT in the enabled list is effectively muted for that pair.
# If a pair is not listed here, ALL theories run (default behavior).
PAIR_THEORY_CONFIG = {
    "AUDNZD_otc": {
        "enabled": ["ORDERFLOW", "HISTORY", "RUN", "LIVE_WICK", "MEAN", "MICRO", "MOMENTUM"],
    },
    "BRLUSD_otc": {
        "enabled": ["REV", "GAP", "LIVE_WICK", "MEAN", "OB", "VELOCITY", "HISTORY", "MOMENTUM", "SWEEP"],
    },
    "CADCHF_otc": {
        "enabled": ["HISTORY", "REV", "RNG", "CON", "ZZ", "CONTINUITY", "RUN", "MICRO", "LIVE_WICK", "STRUCT", "TRAP"],
    },
    "EURJPY": {
        "enabled": ["ZZ", "ORDERFLOW", "MOMENTUM", "MEAN", "OB", "RUN", "TRAP"],
    },
    "EURNZD_otc": {
        "enabled": ["MOMENTUM", "ORDERFLOW"],
    },
    "EURUSD_otc": {
        "enabled": ["ORDERFLOW", "VELOCITY", "RUN", "CON", "MICRO", "MST"],
    },
    "GBPNZD_otc": {
        "enabled": ["MOMENTUM", "HOLD", "STRUCT", "ZZ", "CON"],
    },
    "NZDCAD_otc": {
        "enabled": ["REV", "GAP", "ZZ", "CONTINUITY", "OB", "VELOCITY", "HISTORY", "HOLD", "ORDERFLOW", "CON", "MOMENTUM"],
    },
    "NZDCHF_otc": {
        "enabled": ["ORDERFLOW", "RNG", "SWEEP", "MEAN", "CONTINUITY", "VELOCITY", "OB", "REV"],
    },
    "NZDJPY_otc": {
        "enabled": ["LIVE_WICK", "RNG", "MICRO", "CONTINUITY", "HOLD", "VELOCITY", "CON", "MOMENTUM"],
    },
    "NZDUSD_otc": {
        "enabled": ["REV", "RNG", "LIVE_WICK", "MOMENTUM", "CONTINUITY", "ZZ", "ORDERFLOW", "RUN", "TRAP"],
    },
    "USDARS_otc": {
        "enabled": ["HISTORY", "HOLD", "MEAN", "CONTINUITY", "ORDERFLOW"],
    },
    "USDBDT_otc": {
        "enabled": ["LIVE_WICK", "MEAN", "MOMENTUM", "MICRO", "SWEEP", "REV", "CONTINUITY", "VELOCITY"],
    },
    "USDCOP_otc": {
        "enabled": ["SWEEP", "MEAN", "MICRO", "STRUCT", "HOLD", "MOMENTUM", "ZZ"],
    },
    "USDDZD_otc": {
        "enabled": ["OB", "VELOCITY", "HOLD", "STRUCT", "ORDERFLOW", "CONTINUITY", "RUN", "REV"],
    },
    "USDMXN_otc": {
        "enabled": ["MST", "CON", "SWEEP", "HOLD", "HISTORY"],
    },
    "USDPKR_otc": {
        "enabled": ["LIVE_WICK", "SWEEP", "MOMENTUM", "TRAP", "RUN", "MICRO", "REV"],
    },
}


def get_muted_theories(asset: str) -> set:
    """Return the set of theories to MUTE for a given asset.

    If the asset is in PAIR_THEORY_CONFIG, mute everything NOT in enabled.
    If the asset is NOT in config, return empty set (all theories run).
    """
    config = PAIR_THEORY_CONFIG.get(asset)
    if not config:
        return set()   # no config → all theories enabled

    enabled = set(config.get("enabled", []))
    # Mute = all theories NOT in enabled
    muted = set(ALL_THEORIES) - enabled
    # Also add deleted theories (FVG, LAST, SHIFT, MOMENTUM) to muted
    # so they don't accidentally get re-enabled
    muted.update({"FVG", "LAST", "SHIFT"})
    return muted


def get_enabled_theories(asset: str) -> list:
    """Return the list of theories to ENABLE for a given asset."""
    config = PAIR_THEORY_CONFIG.get(asset)
    if not config:
        return list(ALL_THEORIES)   # no config → all enabled
    return config.get("enabled", ALL_THEORIES)
