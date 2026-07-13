from typing import Any

from pyquotex.utils.services import group_by_period


def process_candles_v2(
        history: dict[str, Any],
        asset: str,
        data: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Process and merge historical + realtime candles with deduplication."""
    if not history or not isinstance(history, dict):
        return data if data else []

    candles_data = history.get(asset, {})
    candles = candles_data.get("candles", [])[1:] if candles_data else []

    # Combine candles and realtime data
    combined = candles + (data if data else [])

    # Deduplicate by time to prevent same candle from being added
    # multiple times
    if combined:
        candle_dict = {
            c.get('time'): c for c in combined
            if isinstance(c, dict) and 'time' in c
        }
        return list(candle_dict.values()) if candle_dict else []

    return combined


def calculate_candles(
        history: list[Any] | dict[str, Any],
        period: int
) -> list[dict[str, Any]]:
    """Calculate candles from tick history."""
    if isinstance(history, dict):
        history = history.get("history", history.get("candles", []))

    if not isinstance(history, list) or not history:
        return []

    grouped = group_by_period(history, period)
    if grouped is None:
        return []

    candles = []
    for minute, ticks in grouped.items():
        open_price = ticks[0][1]
        close_price = ticks[-1][1]
        high_price = max(tick[1] for tick in ticks)
        low_price = min(tick[1] for tick in ticks)
        num_ticks = len(ticks)
        candle = {
            'time': minute * period,
            'open': open_price,
            'close': close_price,
            'high': high_price,
            'low': low_price,
            'ticks': num_ticks
        }
        candles.append(candle)
    candles = candles[:-1]

    return candles


def merge_candles(candles_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Efficiently merge candles using dict comprehension."""
    if not candles_data:
        return []

    # Use dict to eliminate duplicates by time, then convert back to
    # sorted list
    candle_dict = {
        c['time']: c for c in candles_data
        if isinstance(c, dict) and 'time' in c
    }
    return sorted(
        candle_dict.values(), key=lambda x: x['time']
    ) if candle_dict else []
