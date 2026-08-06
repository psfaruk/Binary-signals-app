"""Module: DIVERGENCE — Price vs momentum divergence detection."""
from engines.base.types import ModuleResult, MarketContext

__all__ = ["analyze"]

_DIV_LOOKBACK = 12
_MIN_SWING_SEP = 3
_FRESH_SWING_IX = 5
_MOMENTUM_RATIO = 0.75  # bearish: last mom < prev * 0.75
_MOMENTUM_RATIO_BULL = 1.25
_MIN_MOM = 0.15


def analyze(candles, ctx: MarketContext) -> list:
    """Detect price/momentum divergences."""
    if len(candles) < _DIV_LOOKBACK:
        return []

    _div_cands = candles[-_DIV_LOOKBACK:]
    _sw_highs = []  # (idx, price, momentum)
    _sw_lows = []

    for i in range(1, len(_div_cands) - 1):
        _sh = _div_cands[i]["high"]
        _sl = _div_cands[i]["low"]
        _sr = _div_cands[i]["high"] - _div_cands[i]["low"]
        _sb = abs(_div_cands[i]["close"] - _div_cands[i]["open"])
        _mom = _sb / _sr if _sr > 0 else 0
        if _sh >= _div_cands[i - 1]["high"] and _sh >= _div_cands[i + 1]["high"]:
            _sw_highs.append((i, _sh, _mom))
        if _sl <= _div_cands[i - 1]["low"] and _sl <= _div_cands[i + 1]["low"]:
            _sw_lows.append((i, _sl, _mom))

    results = []

    # Bearish divergence: higher high but momentum weaker
    if len(_sw_highs) >= 2:
        _h_last = _sw_highs[-1]
        _h_prev = _sw_highs[-2]
        _h_sep = _h_last[0] - _h_prev[0]
        _h_fresh = (len(_div_cands) - 1 - _h_last[0]) <= _FRESH_SWING_IX
        if (_h_sep >= _MIN_SWING_SEP and _h_fresh
                and _h_last[1] > _h_prev[1]
                and _h_last[2] < _h_prev[2] * _MOMENTUM_RATIO
                and _h_prev[2] > _MIN_MOM):
            _div_mag = 2
            results.append(ModuleResult(
                module_name="divergence",
                direction="PUT",
                score=_div_mag,
                confidence=58,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="DIVERGENCE",
                reasons=[f"DIVERGENCE Bearish: higher high ({_h_prev[1]:.5g}->{_h_last[1]:.5g}), "
                         f"momentum {_h_prev[2]:.0%}->{_h_last[2]:.0%} -> PUT (x{_div_mag})"],
            ))

    # Bullish divergence: lower low but momentum stronger
    if len(_sw_lows) >= 2:
        _l_last = _sw_lows[-1]
        _l_prev = _sw_lows[-2]
        _l_sep = _l_last[0] - _l_prev[0]
        _l_fresh = (len(_div_cands) - 1 - _l_last[0]) <= _FRESH_SWING_IX
        if (_l_sep >= _MIN_SWING_SEP and _l_fresh
                and _l_last[1] < _l_prev[1]
                and _l_last[2] > _l_prev[2] * _MOMENTUM_RATIO_BULL
                and _l_prev[2] > _MIN_MOM):
            _div_mag = 2
            results.append(ModuleResult(
                module_name="divergence",
                direction="CALL",
                score=_div_mag,
                confidence=58,
                signal_type="REVERSAL",
                reliability="CANDLE",
                group="DIVERGENCE",
                reasons=[f"DIVERGENCE Bullish: lower low ({_l_prev[1]:.5g}->{_l_last[1]:.5g}), "
                         f"momentum {_l_prev[2]:.0%}->{_l_last[2]:.0%} -> CALL (x{_div_mag})"],
            ))

    return results
