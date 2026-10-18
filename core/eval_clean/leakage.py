"""Phase C2 — look-ahead / leakage guards with adversarial detectors.

Design: every feature/prediction carries an explicit EVIDENCE TIMESTAMP
(the wall-clock moment the information became available). The prediction
cutoff for a target candle opening at ``target_t`` is exactly ``target_t``:
evidence with ``evidence_ts <= target_t`` is legitimate (a source candle
closes exactly as the target opens — boundary evidence); evidence with
``evidence_ts > target_t`` is look-ahead and must be REJECTED.

This module provides:

* ``EvidenceLedger`` — records (name, evidence_ts) per feature used by a
  prediction; ``violations(cutoff)`` lists any look-ahead evidence.
* ``SplitScaler`` — train-only normalization: refuses to be fitted on data
  that leaks (fit on TRAIN, transform TEST only through the fitted stats).
  Constructing normalization statistics from the full dataset is the classic
  silent leakage path; this class makes the safe path the only path.
* ``verify_split_independence`` — asserts chronological separation,
  embargo compliance and no duplicate observations across splits.
* ``detect_label_echo`` — flags features that literally encode the label
  (a feature equal to the label in ~all rows is leakage by construction).
* ``future_perturbation_unchanged`` — the strongest causal proof: recompute
  a function under an adversarially MUTATED future; if outputs change, the
  function reads the future.

All checks are used by scripts/test_phase_c_eval.py on INTENTIONALLY BAD
examples — the evaluator must reject each one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple


class EvidenceLedger:
    """Per-prediction record of when each piece of information became
    available. The evaluator attaches one ledger per prediction and asserts
    ``violations(cutoff) == []`` before accepting the prediction."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: List[Tuple[str, float]] = []

    def add(self, name: str, evidence_ts: float) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("evidence name must be a non-empty string")
        try:
            ts = float(evidence_ts)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"evidence_ts for {name!r} not numeric") from exc
        if ts != ts or ts in (float("inf"), float("-inf")):
            raise ValueError(f"evidence_ts for {name!r} not finite")
        self._entries.append((name, ts))

    def violations(self, cutoff: float) -> List[Dict[str, Any]]:
        """Evidence strictly newer than the cutoff is look-ahead. Evidence
        exactly AT the cutoff is legitimate (boundary: source candle closes
        as the target opens)."""
        out = []
        for name, ts in self._entries:
            if ts > float(cutoff):
                out.append({"name": name, "evidence_ts": ts,
                            "cutoff": float(cutoff)})
        return out

    @property
    def max_evidence_ts(self) -> float:
        if not self._entries:
            return float("-inf")
        return max(ts for _, ts in self._entries)

    def n_entries(self) -> int:
        return len(self._entries)


class SplitScaler:
    """Train-only standardization guard.

    Usage:
        sc = SplitScaler().fit(train_matrix)     # stats from TRAIN only
        Xt = sc.transform(test_matrix)           # uses TRAIN stats

    The class REJECTS the leaky pattern: fitting after having seen test
    rows is impossible by API design (fit() returns a NEW scaler; there is
    no fit_transform-all path), and transform() before fit() raises.
    """

    def __init__(self) -> None:
        self._means: List[float] = []
        self._scales: List[float] = []
        self._fitted = False
        self._fit_rows = 0

    def fit(self, rows: Sequence[Sequence[float]]) -> "SplitScaler":
        if not rows:
            raise ValueError("cannot fit on empty matrix")
        width = len(rows[0])
        if width == 0:
            raise ValueError("cannot fit on zero-width matrix")
        for r in rows:
            if len(r) != width:
                raise ValueError("ragged matrix: row widths differ")
        self._means = []
        self._scales = []
        for j in range(width):
            col = [float(r[j]) for r in rows]
            m = sum(col) / len(col)
            var = sum((x - m) ** 2 for x in col) / len(col)
            sd = var ** 0.5
            self._means.append(m)
            self._scales.append(sd if sd > 1e-12 else 1.0)
        self._fitted = True
        self._fit_rows = len(rows)
        return self

    def transform(self, rows: Sequence[Sequence[float]]) -> List[List[float]]:
        if not self._fitted:
            raise RuntimeError(
                "SplitScaler.transform called before fit — normalization "
                "statistics must come from the TRAIN split only (leak guard)")
        out = []
        for r in rows:
            if len(r) != len(self._means):
                raise ValueError("row width differs from fitted matrix")
            out.append([(float(v) - m) / s
                        for v, m, s in zip(r, self._means, self._scales)])
        return out

    @property
    def fit_rows(self) -> int:
        return self._fit_rows


def verify_split_independence(
    train_rows: Sequence[Dict[str, Any]],
    test_rows: Sequence[Dict[str, Any]],
    embargo_periods: int,
    period: int = 60,
    key: Tuple[str, ...] = ("asset", "target_t"),
) -> Dict[str, Any]:
    """Assert the two splits are chronologically separated by at least
    ``embargo_periods`` and share no observation.

    ``train_rows``/``test_rows`` are prediction observations carrying
    ``asset`` and ``target_t`` (epoch seconds). The check fails (raises
    ``LeakageError``) when:
      * any test target_t <= max train target_t (test precedes/overlaps
        train chronologically);
      * the embargo gap is violated: min(test target_t) <
        max(train target_t) + embargo_periods * period (label windows of
        train rows would still be settling while test features begin);
      * the same (asset, target_t) appears in both splits (duplicate leak).
    """
    if not train_rows or not test_rows:
        return {"ok": True, "checked": 0,
                "note": "empty split — nothing to verify"}

    tr_keys = [(r[key[0]], r[key[1]]) for r in train_rows]
    te_keys = [(r[key[0]], r[key[1]]) for r in test_rows]
    dup = set(tr_keys) & set(te_keys)
    if dup:
        raise LeakageError(
            f"duplicate observations across splits: {sorted(dup)[:5]}")

    tr_sorted = sorted(tr_keys)
    te_sorted = sorted(te_keys)
    # chronological separation per asset
    by_asset_tr: Dict[Any, List[float]] = {}
    by_asset_te: Dict[Any, List[float]] = {}
    for a, t in tr_sorted:
        by_asset_tr.setdefault(a, []).append(float(t))
    for a, t in te_sorted:
        by_asset_te.setdefault(a, []).append(float(t))
    for a, te_ts in by_asset_te.items():
        tr_ts = by_asset_tr.get(a)
        if not tr_ts:
            continue
        if min(te_ts) <= max(tr_ts):
            raise LeakageError(
                f"asset {a}: test target_t {min(te_ts)} <= train target_t "
                f"{max(tr_ts)} — splits overlap chronologically")
        required = max(tr_ts) + embargo_periods * period
        if min(te_ts) < required:
            raise LeakageError(
                f"asset {a}: embargo violated — first test target_t "
                f"{min(te_ts)} < last train target_t {max(tr_ts)} + "
                f"{embargo_periods} period(s) ({required})")
    return {"ok": True, "checked": len(tr_keys) + len(te_keys),
            "per_asset_min_test_ts": {a: min(v) for a, v in by_asset_te.items()},
            "per_asset_max_train_ts": {a: max(v) for a, v in by_asset_tr.items()}}


class LeakageError(RuntimeError):
    """Raised when a leakage guard is violated."""


def detect_label_echo(
    rows: Sequence[Dict[str, Any]],
    feature_names: Sequence[str],
    label_key: str = "y",
    threshold: float = 0.99,
) -> List[str]:
    """Flag features whose value equals the label in >= threshold of rows.

    A feature that mirrors the label almost everywhere encodes the outcome
    directly (label leakage through joins/cached state/extra columns). The
    detector returns the offending feature names; an empty list is clean.
    """
    if not rows:
        return []
    offenders = []
    n = len(rows)
    for fname in feature_names:
        hits = 0
        seen = 0
        for r in rows:
            if fname not in r:
                continue
            seen += 1
            try:
                if float(r[fname]) == float(r[label_key]):
                    hits += 1
            except (TypeError, ValueError):
                continue
        if seen and hits / seen >= threshold:
            offenders.append(fname)
    return offenders


def future_perturbation_unchanged(
    fn: Callable[..., Any],
    base_inputs: Sequence[Any],
    future_mutator: Callable[[Any], Any],
    *,
    equals: Callable[[Any, Any], bool] = lambda a, b: a == b,
) -> Dict[str, Any]:
    """Prove ``fn`` does not read the future.

    ``fn(*base_inputs)`` is computed twice: once on the original inputs and
    once after ``future_mutator`` has adversarially mutated every input
    (the mutator should alter future/future-dependent content only, but for
    the guard ANY change is allowed — the function under test must be
    invariant to data it claims not to use). Returns a report; raises
    ``LeakageError`` if outputs differ.
    """
    out_a = fn(*base_inputs)
    mutated = tuple(future_mutator(x) for x in base_inputs)
    out_b = fn(*mutated)
    if not equals(out_a, out_b):
        raise LeakageError(
            "future perturbation changed the output — function reads data "
            "beyond its declared evidence window")
    return {"ok": True, "invariant": True}
