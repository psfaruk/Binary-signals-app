"""Phase C5 — coverage-aware evaluation metrics.

Principles:

* Coverage is reported alongside accuracy ALWAYS. A predictor that answers
  on 3% of candles must never be compared against one that answers on
  100% without the coverage number visible next to it.
* Every accuracy carries a Wilson 95% confidence interval. Small samples
  look small.
* Brier score is computed ONLY when every directional prediction carries a
  genuine probability; otherwise it is ``None`` with an explanatory note.
* Calibration is EVALUATED (bucketed reliability), never assumed. Engine
  confidence scores are reported as engine-internal scores; if they are
  presented as probabilities anywhere, the calibration table is the
  evidence about whether that presentation is justified.
* FLAT (tie) outcomes are excluded from direction metrics and counted —
  the documented tie policy.

``binary_metrics`` input rows are the ACCEPTED prediction rows from
``core.eval_clean.integrity.validate_predictions`` (direction, outcome,
prob, correct) — optionally mixed with NO_SIGNAL rows (direction=None) so
coverage reflects the full observation stream.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


def wilson_ci(successes: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    """Wilson score interval for a binomial proportion.

    Returns [lo, hi] or None when n == 0 (no dishonest 0±0 interval).
    """
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError("successes must be in [0, n]")
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return [max(0.0, (centre - margin) / denom),
            min(1.0, (centre + margin) / denom)]


def binary_metrics(rows: Sequence[Dict[str, Any]], *,
                   label: str = "") -> Dict[str, Any]:
    """Compute the full metric block for one arm over one observation set.

    Row contract (from integrity.validate_predictions, plus NO_SIGNAL rows):
      direction: "CALL" | "PUT" | None (None = NO_SIGNAL / abstention)
      outcome:   "GREEN" | "RED" | "FLAT"
      prob:      float in (0,1) or None
      correct:   bool (directional rows only)
    """
    n_total = len(rows)
    no_signal = [r for r in rows if r.get("direction") is None]
    directional = [r for r in rows if r.get("direction") in ("CALL", "PUT")]
    flats = [r for r in directional if r.get("outcome") == "FLAT"]
    graded = [r for r in directional if r.get("outcome") in ("GREEN", "RED")]

    n_call = sum(1 for r in directional if r["direction"] == "CALL")
    n_put = sum(1 for r in directional if r["direction"] == "PUT")

    g_call = sum(1 for r in graded
                 if r["outcome"] == "GREEN" and r["direction"] == "CALL")
    g_put = sum(1 for r in graded
                if r["outcome"] == "GREEN" and r["direction"] == "PUT")
    r_call = sum(1 for r in graded
                 if r["outcome"] == "RED" and r["direction"] == "CALL")
    r_put = sum(1 for r in graded
                if r["outcome"] == "RED" and r["direction"] == "PUT")
    f_call = sum(1 for r in flats if r["direction"] == "CALL")
    f_put = sum(1 for r in flats if r["direction"] == "PUT")

    n_green = g_call + g_put
    n_red = r_call + r_put
    correct = g_call + r_put

    accuracy = correct / len(graded) if graded else None
    acc_ci = wilson_ci(correct, len(graded)) if graded else None

    call_precision = g_call / n_call if n_call else None
    put_precision = r_put / n_put if n_put else None
    green_recall = g_call / n_green if n_green else None
    red_recall = r_put / n_red if n_red else None
    balanced = None
    if green_recall is not None and red_recall is not None:
        balanced = (green_recall + red_recall) / 2.0

    coverage = len(directional) / n_total if n_total else None

    probs = [r.get("prob") for r in graded]
    have_probs = bool(probs) and all(
        isinstance(p, (int, float)) and math.isfinite(p) and 0.0 < p < 1.0
        for p in probs)
    brier = None
    brier_note = None
    if graded and have_probs:
        # probability of GREEN; CALL wins iff GREEN
        brier = sum(
            (float(r["prob"]) - (1.0 if r["outcome"] == "GREEN" else 0.0)) ** 2
            for r in graded) / len(graded)
    elif graded:
        brier_note = ("not computed: predictions do not carry genuine "
                      "probabilities in (0,1) for every graded row")

    return {
        "label": label,
        "n_total": n_total,
        "n_directional": len(directional),
        "n_no_signal": len(no_signal),
        "n_tie_excluded": len(flats),
        "coverage": coverage,
        "call_count": n_call,
        "put_count": n_put,
        "graded": len(graded),
        "correct": correct,
        "accuracy": accuracy,
        "accuracy_ci95": acc_ci,
        "balanced_accuracy": balanced,
        "call_precision": call_precision,
        "put_precision": put_precision,
        "green_recall": green_recall,
        "red_recall": red_recall,
        "confusion_matrix": {
            "green_call": g_call, "green_put": g_put,
            "red_call": r_call, "red_put": r_put,
            "flat_call": f_call, "flat_put": f_put,
        },
        "brier_score": brier,
        "brier_note": brier_note,
    }


def calibration_table(rows: Sequence[Dict[str, Any]], *,
                      n_buckets: int = 10,
                      direction_prob: str = "green") -> Optional[List[Dict[str, Any]]]:
    """Bucketed reliability for probability of GREEN.

    Returns None when no graded row carries a probability. Buckets are
    [i/n, (i+1)/n); each row reports n, mean predicted probability of
    GREEN, and the observed GREEN rate. A perfectly calibrated predictor
    has mean_pred ~= observed_rate in every bucket.
    """
    graded = [r for r in rows
              if r.get("outcome") in ("GREEN", "RED")
              and isinstance(r.get("prob"), (int, float))
              and 0.0 < float(r["prob"]) < 1.0]
    if not graded:
        return None
    buckets: List[List[Dict[str, Any]]] = [[] for _ in range(n_buckets)]
    for r in graded:
        p = float(r["prob"])
        b = min(n_buckets - 1, int(p * n_buckets))
        buckets[b].append(r)
    table = []
    for i, b in enumerate(buckets):
        if not b:
            table.append({"bucket": f"[{i/n_buckets:.1f},{(i+1)/n_buckets:.1f})",
                          "n": 0, "mean_pred": None, "observed_green_rate": None})
            continue
        mean_pred = sum(float(r["prob"]) for r in b) / len(b)
        obs = sum(1.0 for r in b if r["outcome"] == "GREEN") / len(b)
        table.append({
            "bucket": f"[{i/n_buckets:.1f},{(i+1)/n_buckets:.1f})",
            "n": len(b), "mean_pred": round(mean_pred, 4),
            "observed_green_rate": round(obs, 4),
        })
    return table


def period_breakdown(rows: Sequence[Dict[str, Any]],
                     buckets: Dict[str, Any],
                     key_fn=lambda r: r.get("target_t")) -> Dict[str, Any]:
    """Split rows into named time buckets (buckets: name -> (lo, hi) target_t
    bounds, inclusive lo, exclusive hi) and compute binary_metrics per
    bucket. Used for 'performance by time period' reporting."""
    out: Dict[str, Any] = {}
    for name, (lo, hi) in buckets.items():
        sub = [r for r in rows if lo <= (key_fn(r) or 0) < hi]
        out[name] = binary_metrics(sub, label=name)
    return out
