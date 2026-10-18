"""Phase C6 — payout-aware break-even calculator.

The simplified binary payout model (explicitly documented):

* stake 1 unit per trade;
* a WIN returns net profit ``r`` units (r > 0) — the "payout" quoted by a
  platform (e.g. 0.85 means +0.85 per 1 staked);
* a LOSS costs the 1 unit staked;
* a TIE may be refunded (tie_outcome="refund", default) or lost
  ("loss") — which one applies is a BROKER RULE that we have NOT
  verified. The calculator requires the caller to state it explicitly.

For the refund case with tie rate ``tau``:

    EV = p * r - (1 - p - tau) + 0 * tau = 0
    =>  break_even_win_rate = (1 - tau) / (1 + r)

With tau = 0 this reduces to the familiar ``1 / (1 + r)`` — e.g. r = 0.85
gives ~0.5405 (54.05%). That number is a THEORETICAL arithmetic identity,
NOT a guaranteed Quotex rate, and accuracy above break-even is NOT
guaranteed profitability (spreads, varying payouts, slippage on entry,
strike-vs-open grading differences and platform rules all sit between
this formula and any real outcome).
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def break_even_win_rate(net_payout_per_unit: float,
                        tie_rate: float = 0.0,
                        tie_outcome: str = "refund") -> float:
    """Minimum directional win rate at which EV = 0 under the documented
    simplified model. Fails closed on invalid inputs."""
    r = float(net_payout_per_unit)
    if not (r > 0) or r == float("inf") or r != r:
        raise ValueError(
            f"net_payout_per_unit must be a positive finite number, got {r!r}")
    tau = float(tie_rate)
    if not (0.0 <= tau < 1.0) or tau != tau:
        raise ValueError(
            f"tie_rate must be in [0, 1), got {tau!r}")
    if tie_outcome == "refund":
        return (1.0 - tau) / (1.0 + r)
    if tie_outcome == "loss":
        # EV = p*r - (1-p) = 0  ->  p = 1/(1+r); ties already cost like losses
        return 1.0 / (1.0 + r)
    raise ValueError(
        f"tie_outcome must be 'refund' or 'loss', got {tie_outcome!r}")


def breakeven_report(net_payout_per_unit: Optional[float] = None,
                     measured_tie_rate: Optional[float] = None) -> Dict[str, Any]:
    """Transparent, self-documenting break-even block for reports.

    ``net_payout_per_unit`` MUST be supplied explicitly by the caller —
    nothing is hardcoded as a guaranteed platform rate. ``measured_tie_rate``
    is an optional OBSERVED statistic from the evaluated dataset (share of
    FLAT outcomes); it is an input, never something this function invents.
    """
    if net_payout_per_unit is None:
        raise ValueError(
            "net_payout_per_unit must be supplied explicitly — no default "
            "payout is assumed or hardcoded")
    tie = float(measured_tie_rate) if measured_tie_rate is not None else 0.0
    be_refund = break_even_win_rate(net_payout_per_unit, tie, "refund")
    be_loss = break_even_win_rate(net_payout_per_unit, tie, "loss")
    return {
        "formula_refund": "(1 - tie_rate) / (1 + r)",
        "formula_loss": "1 / (1 + r)",
        "net_payout_per_unit_supplied": net_payout_per_unit,
        "tie_rate_used": tie,
        "tie_rate_source": ("measured FLAT share of the evaluated dataset"
                            if measured_tie_rate is not None
                            else "0.0 (not measured; conservative default)"),
        "break_even_win_rate_refund_ties": round(be_refund, 6),
        "break_even_win_rate_ties_are_losses": round(be_loss, 6),
        "disclaimer": (
            "Theoretical arithmetic under a simplified model. NOT a "
            "guaranteed platform rate; payout was explicitly supplied, not "
            "hardcoded. Accuracy above break-even is not guaranteed "
            "profitability: entry timing, strike-vs-open grading, varying "
            "payouts and platform tie rules are not modelled here. Broker "
            "tie rules are UNVERIFIED."),
    }
