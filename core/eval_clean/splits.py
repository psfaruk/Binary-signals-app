"""Phase C3 — chronological, embargoed, walk-forward data splitting.

Rules implemented here:

* Splits are CHRONOLOGICAL and NON-RANDOM. Rows keep temporal order; the
  only free parameters are the split fractions and the embargo, both fixed
  in code BEFORE the holdout is evaluated (see
  scripts/backtest_clean_eval.py: the method is frozen in this module and
  in the committed protocol doc).
* No overlapping label windows contaminate splits: an ``embargo_periods``
  gap is enforced between the train region and the evaluation region so
  that the train labels have fully settled before evaluation features
  begin (for a T+1 label the embargo is >= 1 period; the protocol uses 1
  unless a longer horizon is evaluated).
* Cutoffs (candle indices + UTC timestamps) are recorded and reported.
* ``verify`` re-checks every produced split with
  ``core.eval_clean.leakage.verify_split_independence`` so a regression in
  the split logic fails loudly instead of silently contaminating results.

The final holdout is the LAST ``test_frac`` of each asset's post-warmup
region. It is reported ONCE by the pre-committed evaluation method; the
selection region exists precisely so exploratory comparisons never need to
touch the holdout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from .integrity import ValidCandle
from .leakage import verify_split_independence


@dataclass
class SplitSpec:
    """Pre-committed split geometry (frozen before holdout evaluation)."""

    warmup: int = 600              # candles per asset excluded from metrics
    train_frac: float = 0.50       # of post-warmup candles
    selection_frac: float = 0.25   # of post-warmup candles
    test_frac: float = 0.25        # of post-warmup candles (holdout)
    embargo_periods: int = 1       # gap between train and evaluation regions
    method: str = "chronological"

    def __post_init__(self) -> None:
        total = self.train_frac + self.selection_frac + self.test_frac
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"split fractions must sum to 1.0 (got {total})")
        if self.warmup < 0 or self.embargo_periods < 0:
            raise ValueError("warmup/embargo must be non-negative")
        for name in ("train_frac", "selection_frac", "test_frac"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass
class AssetSplit:
    asset: str
    warmup: List[ValidCandle] = field(default_factory=list)
    train: List[ValidCandle] = field(default_factory=list)
    selection: List[ValidCandle] = field(default_factory=list)
    test: List[ValidCandle] = field(default_factory=list)
    cutoffs_utc: Dict[str, Any] = field(default_factory=dict)
    embargo_candles: List[ValidCandle] = field(default_factory=list)
    # candles inside the embargo gap (not in any region)

    def as_observation(self, region: str) -> List[Dict[str, Any]]:
        """Region rows as prediction observations for split verification."""
        rows = []
        for c in getattr(self, region):
            rows.append({"asset": c.asset, "target_t": c.t})
        return rows


@dataclass
class SplitResult:
    spec: SplitSpec
    by_asset: Dict[str, AssetSplit] = field(default_factory=dict)

    def cutoffs_report(self) -> Dict[str, Any]:
        return {a: s.cutoffs_utc for a, s in self.by_asset.items()}

    def verify(self, period: int = 60) -> Dict[str, Any]:
        """Re-verify: no overlap, embargo honored, no duplicates across
        train vs selection/test. Raises LeakageError on violation."""
        checks = {}
        for a, s in self.by_asset.items():
            for eval_region in ("selection", "test"):
                checks[f"{a}:train_vs_{eval_region}"] = verify_split_independence(
                    s.as_observation("train"),
                    s.as_observation(eval_region),
                    embargo_periods=self.spec.embargo_periods,
                    period=period,
                )
            checks[f"{a}:selection_vs_test"] = verify_split_independence(
                s.as_observation("selection"),
                s.as_observation("test"),
                embargo_periods=0,  # selection labels are not training data
                period=period,
            )
        return checks


def _utc(t: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def chronological_split(
    candles_by_asset: Dict[str, Sequence[ValidCandle]],
    spec: SplitSpec | None = None,
) -> SplitResult:
    """Chronological 3-way split (train / selection / holdout test) after a
    warmup region, with an embargo gap before the evaluation regions.

    Boundaries are per-asset candle indices; the exact UTC cutoffs are
    recorded in ``AssetSplit.cutoffs_utc`` for the report. The embargo
    candles are removed from the START of the selection region (the
    selection region begins ``embargo_periods`` candles after the train
    region ends).
    """
    sp = spec or SplitSpec()
    res = SplitResult(spec=sp)
    for asset, candles in candles_by_asset.items():
        cl = list(candles)
        if len(cl) <= sp.warmup + 3:
            raise ValueError(
                f"asset {asset}: {len(cl)} candles is not enough for a "
                f"3-way split with warmup={sp.warmup}")
        post = len(cl) - sp.warmup
        n_train = int(post * sp.train_frac)
        n_selection = int(post * sp.selection_frac)
        # ensure at least 1 candle per region and room for embargo
        n_train = max(1, min(n_train, post - 2))
        n_selection = max(1, min(n_selection, post - n_train - 1))
        s = AssetSplit(asset=asset)
        s.warmup = cl[:sp.warmup]
        s.train = cl[sp.warmup:sp.warmup + n_train]
        emb_start = sp.warmup + n_train
        s.embargo_candles = cl[emb_start:emb_start + sp.embargo_periods]
        sel_start = emb_start + sp.embargo_periods
        s.selection = cl[sel_start:sel_start + n_selection]
        s.test = cl[sel_start + n_selection:]
        if not s.test:
            raise ValueError(f"asset {asset}: empty holdout")
        s.cutoffs_utc = {
            "warmup_end_index": sp.warmup,
            "warmup_end_utc": _utc(s.warmup[-1].t) if s.warmup else None,
            "train_start_index": sp.warmup,
            "train_start_utc": _utc(s.train[0].t),
            "train_end_index": sp.warmup + n_train - 1,
            "train_end_utc": _utc(s.train[-1].t),
            "embargo_candles": len(s.embargo_candles),
            "embargo_end_utc": _utc(s.embargo_candles[-1].t)
            if s.embargo_candles else None,
            "selection_start_utc": _utc(s.selection[0].t),
            "selection_end_utc": _utc(s.selection[-1].t),
            "test_start_index": sel_start + n_selection,
            "test_start_utc": _utc(s.test[0].t),
            "test_end_utc": _utc(s.test[-1].t),
            "n_train": len(s.train), "n_selection": len(s.selection),
            "n_test": len(s.test), "n_warmup": len(s.warmup),
        }
        res.by_asset[asset] = s
    return res


@dataclass
class Fold:
    index: int
    train: List[ValidCandle]
    test: List[ValidCandle]
    embargo: List[ValidCandle]
    train_end_utc: str
    test_start_utc: str
    test_end_utc: str


def walk_forward_folds(
    candles: Sequence[ValidCandle],
    n_folds: int = 4,
    embargo_periods: int = 1,
    period: int = 60,
) -> List[Fold]:
    """Expanding-window walk-forward folds over a single asset's candles.

    Fold i (of n): train = all candles before the fold's test window minus
    embargo; test = the i-th slice of the tail region. Temporal order is
    preserved; the first ``min_train`` candles are always train-only.
    """
    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    n = len(candles)
    min_train = max(200, n // (2 * n_folds))
    tail = n - min_train
    if tail < n_folds:
        raise ValueError(
            f"not enough candles ({n}) for {n_folds} walk-forward folds")
    fold_size = tail // n_folds
    folds: List[Fold] = []
    for i in range(n_folds):
        test_start = min_train + i * fold_size
        test_end = test_start + fold_size if i < n_folds - 1 else n
        emb_start = test_start - embargo_periods
        train = list(candles[:max(0, emb_start)])
        embargo = list(candles[max(0, emb_start):test_start])
        test = list(candles[test_start:test_end])
        folds.append(Fold(
            index=i, train=train, test=test, embargo=embargo,
            train_end_utc=_utc(train[-1].t) if train else None,
            test_start_utc=_utc(test[0].t), test_end_utc=_utc(test[-1].t),
        ))
    return folds
