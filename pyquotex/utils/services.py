from collections import defaultdict
from typing import Any


def group_by_period(
        data: list[list[Any]],
        period: int
) -> dict[int, list[list[Any]]]:
    """Group tick data by timeframe period."""
    grouped = defaultdict(list)
    for tick in data:
        timestamp = int(tick[0])
        timeframe = int(timestamp // period)
        grouped[timeframe].append(tick)
    return dict(grouped)
