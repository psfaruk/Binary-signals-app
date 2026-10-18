"""Phase C4 — baseline predictors.

Every baseline is evaluated on EXACTLY the same eligible observations as
the predictor under test (same candles, same exclusions, same tie policy).
No baseline is assumed profitable or useful — they are reported so the
predictor's value can be judged against trivial references.

Baselines:

* ``majority_direction`` — the majority class of the TRAIN region only
  (computing it on the test region would leak the answer). Ties in the
  majority count -> "CALL" is NOT silently chosen: the function returns
  None and the caller records an explicit tie note.
* ``previous_candle_direction`` — continuation: predicts the target candle
  will close in the same direction as the immediately preceding candle.
  Uses no fitted parameters (nothing to leak).
* always-GREEN / always-RED — diagnostic constants.
* ``PrespecifiedLogreg`` — the production-independent research baseline:
  a fixed, pre-specified logistic regression on 6 simple candle features,
  L2=1.0, full-batch gradient descent, deterministic (no randomness, no
  tuning). Standardization statistics are fitted on the TRAIN split ONLY
  via core.eval_clean.leakage.SplitScaler. The feature list is frozen in
  RESEARCH_BASELINE_FEATURES; changing it requires a new experiment ID,
  not an edit.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .integrity import ValidCandle
from .leakage import SplitScaler

CALL = "CALL"
PUT = "PUT"

RESEARCH_BASELINE_FEATURES: Tuple[str, ...] = (
    "body_ratio",       # (close-open)/range of source candle
    "upper_wick_ratio",  # (high-max(o,c))/range
    "lower_wick_ratio",  # (min(o,c)-low)/range
    "ret1",             # close/open - 1 of source candle
    "ret2",             # close[t-2]/open[t-2] - 1 (2 candles back)
    "range_atr_ratio",  # source range / mean range of last 14 candles
)


def majority_direction(train_candles: Sequence[ValidCandle]) -> Optional[str]:
    """Majority class computed on TRAIN outcomes only. None on an exact
    tie (recorded, not silently broken)."""
    g = sum(1 for c in train_candles if c.outcome == "GREEN")
    r = sum(1 for c in train_candles if c.outcome == "RED")
    if g > r:
        return CALL
    if r > g:
        return PUT
    return None


def previous_candle_direction(source: ValidCandle) -> str:
    """Continuation baseline: predicts the target closes in the same
    direction as the source candle (the candle immediately before the
    target). FLAT source -> PUT is NOT silently chosen: returns None."""
    if source.outcome == "GREEN":
        return CALL
    if source.outcome == "RED":
        return PUT
    return None  # type: ignore[return-value]


def always_green() -> str:
    return CALL


def always_red() -> str:
    return PUT


def _source_window(candles: List[ValidCandle], i: int) -> List[ValidCandle]:
    """Candles strictly before index i (the evidence window for predicting
    candle i). No future access by construction."""
    return candles[max(0, i - 16):i]


def research_features(candles: List[ValidCandle], i: int) -> List[float]:
    """Compute the 6 pre-specified features for predicting candle ``i``
    from candles before ``i``. All evidence is candles[<i]; the evidence
    timestamp is candle[i-1].t + period (its close) == candle[i].t (the
    boundary — legitimate)."""
    if i < 2:
        raise ValueError("need at least 2 prior candles")
    src = candles[i - 1]
    rng = src.high - src.low
    if rng <= 0:
        rng = 1e-9
    body = (src.close - src.open) / rng
    upper = (src.high - max(src.open, src.close)) / rng
    lower = (min(src.open, src.close) - src.low) / rng
    ret1 = src.close / src.open - 1.0
    prev = candles[i - 2]
    ret2 = prev.close / prev.open - 1.0
    window = _source_window(candles, i)
    w = [c.high - c.low for c in window if c.high - c.low > 0]
    mean_rng = (sum(w) / len(w)) if w else rng
    rat = (src.high - src.low) / mean_rng if mean_rng > 0 else 0.0
    return [body, upper, lower, ret1, ret2, rat]


def research_label(candles: List[ValidCandle], i: int) -> Optional[int]:
    """1 = target candle closes GREEN, 0 = RED, None = FLAT (tie policy:
    FLAT rows are dropped from training, consistent with the grading
    convention)."""
    oc = candles[i].outcome
    if oc == "GREEN":
        return 1
    if oc == "RED":
        return 0
    return None


class PrespecifiedLogreg:
    """Deterministic logistic regression (no sklearn dependency).

    fit(): standardizes with TRAIN-only statistics, runs fixed full-batch
    gradient descent (LR=0.1, 400 iters, L2=1.0 — pre-specified, never
    tuned against evaluation data). predict_proba(): probability of GREEN.
    """

    LR = 0.1
    ITERS = 400
    L2 = 1.0

    def __init__(self) -> None:
        self._scaler = SplitScaler()
        self._w: List[float] = []
        self._b = 0.0
        self._fitted = False
        self.n_train = 0

    def fit(self, X: Sequence[Sequence[float]], y: Sequence[int]) -> "PrespecifiedLogreg":
        if len(X) != len(y):
            raise ValueError("X/y length mismatch")
        if not X:
            raise ValueError("empty training matrix")
        self._scaler = SplitScaler().fit(list(X))
        Xs = self._scaler.transform(list(X))
        width = len(Xs[0])
        self._w = [0.0] * width
        self._b = 0.0
        n = len(Xs)
        for _ in range(self.ITERS):
            gw = [0.0] * width
            gb = 0.0
            for xs, yi in zip(Xs, y):
                z = self._b + sum(wj * xj for wj, xj in zip(self._w, xs))
                p = 1.0 / (1.0 + pow(2.718281828459045, -z))
                err = p - float(yi)
                gb += err
                for j in range(width):
                    gw[j] += err * xs[j]
            self._b -= self.LR * gb / n
            for j in range(width):
                self._w[j] -= self.LR * (gw[j] / n + self.L2 * self._w[j] / n)
        self._fitted = True
        self.n_train = n
        return self

    def predict_proba_green(self, x: Sequence[float]) -> float:
        if not self._fitted:
            raise RuntimeError("PrespecifiedLogreg.predict before fit")
        xs = self._scaler.transform([list(x)])[0]
        z = self._b + sum(wj * xj for wj, xj in zip(self._w, xs))
        return 1.0 / (1.0 + pow(2.718281828459045, -z))

    def predict(self, x: Sequence[float]) -> str:
        return CALL if self.predict_proba_green(x) >= 0.5 else PUT
