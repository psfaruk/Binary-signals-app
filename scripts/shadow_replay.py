#!/usr/bin/env python3
"""PHASE-H — paper/shadow testing via REPLAY (no broker orders, ever).

NO authorized live data source exists (the standing blocker — see
docs/DATA_SOURCE_BLOCKER.md / the Phase D audit), so genuine live
shadow recording is IMPOSSIBLE today. Per the directive, this runner
performs REPLAY-BASED evaluation and labels every record mode='replay'
— never 'live_shadow' — with the dataset provenance stamped on every
row. When (and only when) an authorized source is integrated can the
same ledger record live_shadow rows; the recording path is built and
tested either way.

What it does:
  1. Replays the authorized Binance 1m datasets through the REAL
     production engine (subprocess-isolated CSE replay — live parity).
  2. Records EVERY prediction into the shadow_predictions ledger with
     the full directive field set: prediction id, asset/timeframe,
     candle identity, prediction cutoff, data provenance (dataset
     sha256 + classification), input-data quality status (the Phase-E
     gate verdict evaluated at prediction time), the gated signal
     (NO_SIGNAL under current policy — recorded honestly, not skipped),
     the research direction diagnostic, strategy/model/feature
     versions, the internal confidence score (labeled internal_score —
     calibration NOT established), settlement status, outcome once the
     replayed candle closes, correction history, error/exclusion
     reasons.
  3. Settles outcomes from the replayed candles through the honest
     convention (GREEN/RED/FLAT; FLAT recorded, never silently graded).
  4. Emits a machine-readable report: counts by signal/reason, the
     research-direction diagnostic accuracy (clearly labeled as
     research diagnostics — NOT an actionable-signal accuracy), and the
     explicit replay (not live-shadow) labeling.

NO orders. NO broker contact. NO credentials. NO production-numerics
changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.eval_clean.integrity import (  # noqa: E402
    validate_candle_series, outcome_of, GREEN, RED, FLAT)
from core import data_quality as _dq  # noqa: E402

BINANCE_ASSETS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT")
BINANCE_DIR = os.path.join(_REPO, "scripts", "data", "binance")
STRATEGY = "next_candle_v1"
MODEL_VERSION = "next_candle_v1"
FEATURE_VERSION = "cse_v1_35feat"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_binance_1m(path: str, asset: str) -> list:
    with open(path) as f:
        raw = json.load(f)
    return [{"asset": asset, "period": 60, "t": int(r["t"]) // 1000,
             "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
             "c": float(r["c"]), "v": float(r.get("v", 0.0) or 0.0)}
            for r in raw]


def _engine_predictions(asset: str):
    import subprocess
    import tempfile
    out_file = tempfile.mktemp(prefix=f"phase_h_eng_{asset}_",
                               suffix=".json")
    cmd = [sys.executable,
           os.path.join(_REPO, "scripts", "backtest_clean_eval.py"),
           "--internal-worker", "--arm", "cse_v1", "--asset", asset,
           "--source", "binance", "--seed", "7",
           "--out-file", out_file]
    proc = subprocess.run(cmd, cwd=_REPO, capture_output=True, text=True,
                          timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"engine worker failed: {proc.stderr[-400:]}")
    with open(out_file) as f:
        preds = json.load(f)
    os.unlink(out_file)
    return {p["target_t"]: p for p in preds}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="sqlite path for the shadow ledger (default: "
                         "temp DB — the repository DB is never touched)")
    ap.add_argument("--out-dir", default=os.path.join(_REPO, "scripts",
                                                      "out"))
    ap.add_argument("--assets", default=",".join(BINANCE_ASSETS))
    args = ap.parse_args()

    import tempfile
    if args.db:
        os.environ["DB_PATH"] = args.db
    else:
        _tmp = tempfile.mkdtemp(prefix="phase_h_shadow_")
        os.environ["DB_PATH"] = os.path.join(_tmp, "shadow.db")
    os.environ["QX_RETENTION_ENABLED"] = "0"

    import db as _db
    _db.init()

    started = dt.datetime.now(dt.timezone.utc)
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    datasets = {}
    raw = {}
    for a in assets:
        path = os.path.join(BINANCE_DIR, f"{a}_1m.json")
        raw[a] = load_binance_1m(path, a)
        datasets[a] = {
            "dataset_id": f"binance_1m_{a}",
            "sha256": _sha256(path),
            "classification":
                "authorized_historical_public_exchange (REPLAY INPUT — "
                "NOT a live feed; NOT OTC)",
        }

    report = validate_candle_series([r for a in raw for r in raw[a]])
    accepted = {}
    for c in report.accepted:
        accepted.setdefault(c.asset, []).append(c)
    for a in accepted:
        accepted[a].sort(key=lambda c: c.t)

    provenance_label = "REPLAY (no live source available — mode is " \
                       "replay by construction, never live_shadow)"
    recorded = settled = 0
    no_signal_rows = research_rows = 0
    research_correct = 0
    flat_outcomes = 0
    excluded = {}

    for a in assets:
        candles = accepted.get(a, [])
        if len(candles) < 100:
            continue
        preds = _engine_predictions(a)
        for i in range(1, len(candles)):
            target = candles[i]
            source = candles[i - 1]
            pid = f"replay/{a}/60/{target.t}"
            q = _dq.assess(
                source_id=f"binance_replay:{a}",
                source_authorized=True,     # the DATASET is authorized
                last_observation_wall=float(target.t),
                now=float(target.t),
                closed_candles=[{"open": c.open, "high": c.high,
                                 "low": c.low, "close": c.close}
                                for c in candles[max(0, i - 64):i]],
                clock_sync_status="N/A_HISTORICAL",
                clock_uncertainty_secs=None,
                missing_intervals=0)
            p = preds.get(target.t)
            direction = p["direction"] if p and p.get("direction") \
                else None
            row = {
                "prediction_id": pid,
                "mode": "replay",
                "asset": a,
                "period": 60,
                "candle_identity": f"{a}:60:{target.t}",
                "target_candle_open_epoch": float(target.t),
                "prediction_cutoff_epoch": float(target.t),
                "data_provenance": json.dumps(
                    {**datasets[a], "mode_note": provenance_label}),
                "input_quality_status": json.dumps(
                    {"verdict": q["verdict"], "reasons": q["reasons"]}),
                # The honest gated signal under current policy:
                "signal": "NO_SIGNAL",
                "research_direction": direction,
                "strategy": STRATEGY,
                "model_version": MODEL_VERSION,
                "feature_version": FEATURE_VERSION,
                "confidence_score": (float(p["prob"])
                                     if p and p.get("prob") is not None
                                     else None),
                "confidence_kind": "internal_score",
                "error_reason": None,
            }
            if _db.shadow_record(row):
                recorded += 1
            if direction is None:
                no_signal_rows += 1
            else:
                research_rows += 1
            # settle from the replayed candle (honest convention)
            oc = outcome_of(target.open, target.close)
            if oc == FLAT:
                flat_outcomes += 1
            correct = None
            if direction is not None and oc != FLAT:
                correct = (direction == "CALL") == (oc == GREEN)
                if correct:
                    research_correct += 1
            if _db.shadow_settle(pid, oc, correct):
                settled += 1

    counts = _db.shadow_counts()
    summary = {
        "report_version": "phase_h_shadow_v1",
        "generated_utc": started.isoformat(),
        "mode": "replay",
        "mode_note": provenance_label,
        "why_replay": ("no authorized live data source exists — the "
                       "vendored pyquotex / raw-WS channels are "
                       "unauthorized reverse-engineered access; genuine "
                       "live_shadow requires an authorized provider "
                       "(see docs/DATA_SOURCE_BLOCKER.md path)"),
        "no_broker_orders": True,
        "datasets": datasets,
        "integrity": report.summary(),
        "ledger_counts": counts,
        "recorded": recorded,
        "settled": settled,
        "no_signal_rows": no_signal_rows,
        "research_direction_rows": research_rows,
        "research_direction_correct": research_correct,
        "research_direction_accuracy": (
            round(research_correct / research_rows, 4)
            if research_rows else None),
        "flat_outcomes_recorded": flat_outcomes,
        "research_accuracy_label": (
            "RESEARCH DIAGNOSTIC ONLY — the engine's ungated preference "
            "evaluated on replay data; NOT an actionable-signal accuracy "
            "(the gated signal is NO_SIGNAL on every row by policy)"),
        "fields_recorded_per_prediction": [
            "prediction_id", "mode", "asset/period", "candle_identity",
            "prediction_cutoff_epoch", "data_provenance",
            "input_quality_status", "signal", "research_direction",
            "strategy/model_version/feature_version",
            "confidence_score + confidence_kind (internal_score)",
            "settlement_status", "outcome", "outcome_correct",
            "correction_history", "error_reason"],
    }

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%S") + f"_{os.getpid()}"
    out_path = os.path.join(args.out_dir, f"phase_h_shadow_{stamp}.json")
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(json.dumps({
        "out_path": os.path.relpath(out_path, _REPO),
        "mode": "replay",
        "recorded": recorded,
        "settled": settled,
        "no_signal_rows": no_signal_rows,
        "research_direction_accuracy": summary[
            "research_direction_accuracy"],
        "ledger_counts": counts,
    }, indent=1))
    print(f"REPORT WRITTEN: {out_path}")


if __name__ == "__main__":
    main()
