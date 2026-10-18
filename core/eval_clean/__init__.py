"""Clean evaluation package (Phase C).

Builds the deterministic, leakage-guarded backtest evaluation pipeline:

  integrity  — candle/prediction validation + quarantine + exclusion ledger (C1)
  leakage    — evidence-cutoff guards + adversarial leakage detectors (C2)
  splits     — chronological, embargoed, walk-forward splitting (C3)
  baselines  — majority / previous-candle / always-GREEN / always-RED /
               pre-specified research logreg (C4)
  metrics    — coverage-aware metrics, Wilson CIs, Brier, calibration (C5)
  breakeven  — payout-aware break-even calculator (C6)

This package is RESEARCH ONLY: it never emits trading advice, never contacts
a broker, and never mutates production ledgers. It fulfills (and supersedes
in scope) the `core/eval_protocol.py` module planned in
docs/recovery/REBUILD_PLAN.md §Phase C before the 2026-10-18 loss.
"""

from .integrity import (  # noqa: F401
    CandleCheckPolicy,
    CandleIntegrityReport,
    validate_candle_series,
    validate_predictions,
    PredictionIntegrityReport,
    GREEN,
    RED,
    FLAT,
)
from .leakage import (  # noqa: F401
    EvidenceLedger,
    SplitScaler,
    verify_split_independence,
    detect_label_echo,
    future_perturbation_unchanged,
)
from .splits import (  # noqa: F401
    SplitSpec,
    SplitResult,
    chronological_split,
    walk_forward_folds,
)
from .baselines import (  # noqa: F401
    majority_direction,
    previous_candle_direction,
    PrespecifiedLogreg,
    RESEARCH_BASELINE_FEATURES,
)
from .metrics import (  # noqa: F401
    wilson_ci,
    binary_metrics,
    calibration_table,
)
from .breakeven import (  # noqa: F401
    break_even_win_rate,
    breakeven_report,
)

__all__ = [
    "CandleCheckPolicy", "CandleIntegrityReport", "validate_candle_series",
    "validate_predictions", "PredictionIntegrityReport",
    "EvidenceLedger", "SplitScaler", "verify_split_independence",
    "detect_label_echo", "future_perturbation_unchanged",
    "SplitSpec", "SplitResult", "chronological_split", "walk_forward_folds",
    "majority_direction", "previous_candle_direction", "PrespecifiedLogreg",
    "RESEARCH_BASELINE_FEATURES",
    "wilson_ci", "binary_metrics", "calibration_table",
    "break_even_win_rate", "breakeven_report",
    "GREEN", "RED", "FLAT",
]
