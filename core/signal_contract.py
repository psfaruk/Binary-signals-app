"""PHASE-1 TRUTH LAYER — canonical signal contract (2026-10)
=================================================================
Governing principle (Phase 1 directive, verbatim):

  NO VERIFIED DATA + NO VALIDATED MODEL + NO CORRECT TARGET TIMING
  = NO ACTIONABLE DIRECTIONAL SIGNAL.

Every prediction that leaves the emission boundary (feed._run_eoc
wrapper) passes through THIS module's deterministic eligibility
policy.  A prediction that fails any requirement is converted to
NO_SIGNAL with a stable machine-readable reason code, while the
engine's diagnostic preference is preserved as RESEARCH-ONLY fields
(never rendered as an actionable signal, never graded into the
win-rate ledger).

Labels
------
  CALL       eligible upward/green prediction
  PUT        eligible downward/red prediction
  NO_SIGNAL  no actionable directional prediction
  STALE      internal/UI data-freshness state (NOT a prediction)
  ERROR      internal processing failure (NOT a prediction)

Reason codes (stable, machine-readable)
---------------------------------------
  DATA_UNAVAILABLE            no prediction/data available at all
  DATA_INVALID                required candle data missing/malformed
  DATA_STALE                  feed freshness beyond documented threshold
  INSUFFICIENT_HISTORY        fewer closed candles than the engine minimum
  FEATURE_ERROR               feature generation failed
  MODEL_UNAVAILABLE           model artifact cannot be loaded
  MODEL_OUTPUT_INVALID        non-finite / out-of-range / schema-mismatch
  MODEL_NOT_VALIDATED         no valid OOS evidence in the registry
  BELOW_ELIGIBILITY_THRESHOLD policy threshold not met / engine abstained
  TARGET_CANDLE_INVALID       target candle identity/time invalid
  TIMING_UNVERIFIED           broker server-time sync not established
  INTERNAL_ERROR              unexpected internal failure (fail-closed)
  DATA_SOURCE_UNAUTHORIZED    (addition beyond the required 12) the live
                              Quotex channel is a reverse-engineered,
                              unauthorized client (Phase 0 RC-03) — fail
                              closed until an authorized source exists

Fail-closed defaults (Phase 0 evidence, not new measurements)
-------------------------------------------------------------
  * data_source_authorization = "unauthorized" (RC-03) — the repo's own
    docs describe TLS-spoofing / cookie-replay / DevTools-token access;
    a prior account block is self-documented.
  * No validation registry file exists → no model is validated
    (real-OTC accuracy measured 49.92–50.11%, production_ready=false —
    repo's own committed reports).
  * Timing verification only exists on the raw-WS backend
    (quotex_ws._server_time_synced); production default (pyquotex,
    QX_USE_RAW_WS=0) has no timesync → TIMING_UNVERIFIED.

These defaults are NOT env-overridable: the only path to actionable
signals is a signed validation registry entry (core/model_validation.json)
plus an authorized data source — see docs/PHASE1_TRUTH_LAYER.md.

No secrets, tokens, cookies or raw market payloads are stored or
serialized by this module.  Contract fields are whitelisted.
=================================================================
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── policy identity ────────────────────────────────────────────────────
POLICY_VERSION = "1.0.0"
CONTRACT_SCHEMA_VERSION = "phase1_truth_v1"

# ── reason codes ───────────────────────────────────────────────────────
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
DATA_INVALID = "DATA_INVALID"
DATA_STALE = "DATA_STALE"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
FEATURE_ERROR = "FEATURE_ERROR"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
MODEL_NOT_VALIDATED = "MODEL_NOT_VALIDATED"
BELOW_ELIGIBILITY_THRESHOLD = "BELOW_ELIGIBILITY_THRESHOLD"
TARGET_CANDLE_INVALID = "TARGET_CANDLE_INVALID"
TIMING_UNVERIFIED = "TIMING_UNVERIFIED"
INTERNAL_ERROR = "INTERNAL_ERROR"
DATA_SOURCE_UNAUTHORIZED = "DATA_SOURCE_UNAUTHORIZED"

ALL_REASON_CODES = (
    DATA_UNAVAILABLE, DATA_INVALID, DATA_STALE, INSUFFICIENT_HISTORY,
    FEATURE_ERROR, MODEL_UNAVAILABLE, MODEL_OUTPUT_INVALID,
    MODEL_NOT_VALIDATED, BELOW_ELIGIBILITY_THRESHOLD,
    TARGET_CANDLE_INVALID, TIMING_UNVERIFIED, INTERNAL_ERROR,
    DATA_SOURCE_UNAUTHORIZED,
)

# ── signal statuses ────────────────────────────────────────────────────
STATUS_ELIGIBLE = "eligible"
STATUS_REJECTED = "rejected"
STATUS_STALE = "stale"
STATUS_ERROR = "error"

# ── directions ─────────────────────────────────────────────────────────
CALL = "CALL"
PUT = "PUT"
NO_SIGNAL = "NO_SIGNAL"

# ── data source identity (Phase 0 RC-03: fail-closed) ─────────────────
DATA_SOURCE_ID = "quotex_ws_unofficial"
DATA_SOURCE_AUTHORIZATION = "unauthorized"
AUTHORIZED_STATUSES = ("authorized", "explicitly_approved")

# ── engine minimums (documented, not invented) ────────────────────────
# CSE v1 requires >= 64 closed candles before it speaks (feed.py CSE
# gate `len(closed) >= 64`, core/next_candle.py warmup).  This is the
# engine's OWN minimum — reused here, not a new threshold.
MIN_CLOSED_CANDLES = 64

# Feed freshness threshold: the repo's own documented stale definition
# (feed.py STALE_SECS = 90s, the re-arm/stale-frame threshold).  Derived
# from the documented OTC tick-gap behavior (TIMER_GRACE=7s, gaps 5-10s),
# not an arbitrary number.
DEFAULT_FRESHNESS_SECS = 90.0

# Validation registry (absent → nothing validated — fail closed)
_HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATION_REGISTRY_PATH = os.path.join(_HERE, "model_validation.json")

# CSE v1 artifact (global prior) — provenance target (Phase 0 RC-04)
CSE_WEIGHTS_PATH = os.path.join(_HERE, "next_candle_weights.json")
CSE_MODEL_ID = "cse_v1"
CSE_MODEL_VERSION = "next_candle_v1"
CSE_FEATURE_SCHEMA = "cse_v1_35feat"

# Documented provenance for the shipped prior.  The ARTIFACT itself has
# no metadata (keys: w,b,mu,sd,names — Phase 0 verified).  The training
# source below is DOCUMENTED in docs/NEXT_CANDLE_ENGINE.md (repo's own
# words: "৪টি রিয়েল মার্কেটে (BTC/ETH/XRP/BNB 1m, ~36k ক্যান্ডেল)
# pooled-trained").  We label it as documented-external, NOT embedded —
# and the domain (crypto) does not match the live domain (OTC).
CSE_DOCUMENTED_TRAINING_SOURCE = (
    "binance_btc_eth_xrp_bnb_1m (~36k candles, documented in "
    "docs/NEXT_CANDLE_ENGINE.md; NOT embedded in the artifact)")
CSE_DOCUMENTED_ASSET_DOMAIN = "crypto"

_LOCK = threading.Lock()
_PROV_CACHE: Dict[str, Dict[str, Any]] = {}


# ── time helpers (timezone-aware UTC) ─────────────────────────────────
# Credential-shaped key patterns — DEFENSE IN DEPTH: stripped from
# every truth-layer output so a misbehaving engine can never leak a
# token/cookie through a serialized signal response.  (Engines do not
# have credentials today; this makes that guarantee structural.)
_SECRET_KEY_PATTERNS = (
    "qx_token", "token", "ssid", "password", "passwd", "cookie",
    "cookies", "cf_clearance", "cf_bm", "authorization", "session_id",
    "session", "api_key", "secret", "bearer", "credentials",
)


def _strip_secret_like(d: Dict[str, Any]) -> Dict[str, Any]:
    """Remove credential-shaped keys (recursive, shallow values kept)."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        kl = str(k).strip().lower()
        if any(p == kl or kl.endswith("_" + p) or kl == p + "s"
               for p in _SECRET_KEY_PATTERNS):
            continue
        out[k] = v
    return out


def utc_iso(ts: Optional[float]) -> Optional[str]:
    """Epoch seconds → ISO-8601 UTC string.  None/invalid → None."""
    try:
        if ts is None:
            return None
        ts_f = float(ts)
        if not math.isfinite(ts_f) or ts_f <= 0:
            return None
        return datetime.fromtimestamp(ts_f, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


# ── artifact provenance (Task D) ───────────────────────────────────────
def inspect_artifact(path: str,
                     expected_feature_names: Optional[List[str]] = None,
                     model_id: str = CSE_MODEL_ID,
                     model_version: str = CSE_MODEL_VERSION,
                     feature_schema: str = CSE_FEATURE_SCHEMA,
                     documented_training_source: Optional[str] = None,
                     documented_asset_domain: Optional[str] = None
                     ) -> Dict[str, Any]:
    """Cryptographic + structural provenance for a model artifact.

    Missing metadata stays UNKNOWN — never invented.  The CSE weights
    file has no embedded metadata, so training source / dates come only
    from repo DOCS and are labelled as such.
    """
    rec: Dict[str, Any] = {
        "model_id": model_id,
        "model_version": model_version,
        "artifact_path": os.path.basename(path) if path else None,
        "artifact_sha256": "UNKNOWN",
        "artifact_size": None,
        "feature_schema_version": feature_schema,
        "feature_names": [],
        "feature_count": None,
        "schema_ok": False,
        "load_ok": False,
        "training_dataset": documented_training_source or "UNKNOWN",
        "training_dataset_source": ("documented_external"
                                    if documented_training_source
                                    else "UNKNOWN"),
        "asset_domain": documented_asset_domain or "UNKNOWN",
        "trained_at_utc": "UNKNOWN",
        "validation_method": "UNKNOWN",
        "validation_result_ref": "UNKNOWN",
        "approved_operational_status": "NOT_APPROVED",
    }
    try:
        if not path or not os.path.isfile(path):
            return rec
        rec["artifact_size"] = os.path.getsize(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        rec["artifact_sha256"] = h.hexdigest()
        # structural check: JSON artifact with a `names` list of features
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            names = data.get("names") if isinstance(data, dict) else None
            if isinstance(names, list) and names:
                rec["feature_names"] = [str(n) for n in names]
                rec["feature_count"] = len(names)
                rec["load_ok"] = True
                if expected_feature_names is not None:
                    rec["schema_ok"] = (
                        [str(n) for n in expected_feature_names]
                        == rec["feature_names"])
                else:
                    rec["schema_ok"] = True
        except (ValueError, OSError):
            # binary/unreadable artifacts: hash-only provenance
            rec["load_ok"] = False
    except OSError:
        pass
    return rec


def cse_provenance() -> Dict[str, Any]:
    """Provenance record for the shipped CSE v1 global prior."""
    key = f"cse:{CSE_WEIGHTS_PATH}"
    mtime = 0.0
    try:
        mtime = os.path.getmtime(CSE_WEIGHTS_PATH) if os.path.isfile(
            CSE_WEIGHTS_PATH) else 0.0
    except OSError:
        mtime = 0.0
    with _LOCK:
        cached = _PROV_CACHE.get(key)
        if cached and cached.get("_mtime") == mtime:
            return {k: v for k, v in cached.items() if k != "_mtime"}
        try:
            from core import next_candle as _nc
            expected = list(getattr(_nc, "FEATURE_NAMES", []) or [])
        except Exception:
            expected = None
        rec = inspect_artifact(
            CSE_WEIGHTS_PATH,
            expected_feature_names=expected,
            model_id=CSE_MODEL_ID,
            model_version=CSE_MODEL_VERSION,
            feature_schema=CSE_FEATURE_SCHEMA,
            documented_training_source=CSE_DOCUMENTED_TRAINING_SOURCE,
            documented_asset_domain=CSE_DOCUMENTED_ASSET_DOMAIN)
        rec["_mtime"] = mtime
        _PROV_CACHE[key] = rec
        return {k: v for k, v in rec.items() if k != "_mtime"}


# ── validation registry (Task E: the ONLY path to actionable) ──────────
def load_validation_registry(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the model validation registry.

    File absent / malformed → empty registry → NOTHING is validated
    (fail closed).  Schema (docs/PHASE1_TRUTH_LAYER.md):

      {"registry_version": "1.0",
       "approved": [{"model_id": ..., "artifact_sha256": ...,
                     "asset_domain": "otc",
                     "feature_schema_version": ...,
                     "oos_evidence": {"method": ..., "balanced_accuracy":
                     ..., "n_predictions": ..., "report_path": ...}}]}
    """
    p = path or VALIDATION_REGISTRY_PATH
    out: Dict[str, Any] = {
        "registry_path": os.path.basename(p),
        "exists": False,
        "registry_version": None,
        "approved": [],
        "error": None,
    }
    try:
        if not os.path.isfile(p):
            return out
        out["exists"] = True
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(
                data.get("approved"), list):
            out["error"] = "malformed registry (expected dict with 'approved' list)"
            return out
        out["registry_version"] = str(data.get("registry_version") or "UNKNOWN")
        for entry in data["approved"]:
            if isinstance(entry, dict) and entry.get("model_id"):
                out["approved"].append(entry)
    except (OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _registry_allows(prov: Dict[str, Any],
                     registry: Dict[str, Any],
                     live_domain: str) -> Tuple[bool, str]:
    """Deterministic registry check for one artifact.

    Returns (allowed, reason_code).  An entry matches only when the
    artifact hash, feature schema AND asset domain (must cover the live
    domain) all agree, and the OOS evidence meets the registry's own
    stated minimum (evidence thresholds live in the registry entry,
    NOT hardcoded here — we never invent a threshold).
    """
    if not registry.get("approved"):
        return False, MODEL_NOT_VALIDATED
    sha = prov.get("artifact_sha256")
    for entry in registry["approved"]:
        if not isinstance(entry, dict):
            continue
        if entry.get("model_id") != prov.get("model_id"):
            continue
        if entry.get("artifact_sha256") and sha and \
                entry["artifact_sha256"] != sha:
            continue  # hash mismatch → this entry is NOT this artifact
        domains = entry.get("asset_domain")
        if isinstance(domains, str):
            domains = [domains]
        if not isinstance(domains, list) or (
                live_domain and live_domain not in domains):
            continue  # domain mismatch (e.g. crypto prior on OTC feed)
        ev = entry.get("oos_evidence") or {}
        if not isinstance(ev, dict) or not ev.get("method"):
            continue  # no documented validation method → not validated
        return True, ""
    return False, MODEL_NOT_VALIDATED


# ── the canonical contract (Task C) ────────────────────────────────────
def build_contract_fields(asset: str,
                          period: int,
                          source_close_ts: Optional[float],
                          target_open_ts: Optional[float],
                          created_ts: Optional[float] = None,
                          prov: Optional[Dict[str, Any]] = None,
                          reason_code: Optional[str] = None,
                          status: str = STATUS_REJECTED,
                          validation_status: str = "unvalidated",
                          data_freshness: str = "unknown",
                          signal_id: Optional[str] = None,
                          trace_id: Optional[str] = None
                          ) -> Dict[str, Any]:
    """Canonical, whitelisted provenance fields for one prediction.

    Backward-compatible ADDITIVE fields on the existing result dict —
    existing consumers keep working (see docs for the full contract).
    """
    now = created_ts if created_ts is not None else time.time()
    sig_id = signal_id or uuid.uuid4().hex
    prov = prov or {}
    return {
        "signal_id": sig_id,
        "trace_id": trace_id or sig_id,
        "symbol": asset,
        "truth_status": status,
        "reason_code": reason_code,
        "eligibility_policy_version": POLICY_VERSION,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "created_at_utc": utc_iso(now),
        "created_at_epoch": round(float(now), 3),
        "source_candle_close_time_utc": utc_iso(source_close_ts),
        "source_candle_close_epoch": (round(float(source_close_ts), 3)
                                      if _finite(source_close_ts)
                                      else None),
        "target_candle_open_time_utc": utc_iso(target_open_ts),
        "target_candle_open_epoch": (round(float(target_open_ts), 3)
                                     if _finite(target_open_ts)
                                     else None),
        "target_candle_close_time_utc": utc_iso(
            target_open_ts + period if _finite(target_open_ts)
            and isinstance(period, (int, float)) else None),
        "model_id": prov.get("model_id") or "UNKNOWN",
        "model_version": prov.get("model_version") or "UNKNOWN",
        "model_artifact_hash": prov.get("artifact_sha256") or "UNKNOWN",
        "feature_schema_version": (prov.get("feature_schema_version")
                                   or "UNKNOWN"),
        "data_source_id": DATA_SOURCE_ID,
        "data_source_authorization": DATA_SOURCE_AUTHORIZATION,
        "data_freshness_status": data_freshness,
        "validation_status": validation_status,
    }


# ── the deterministic eligibility policy (Task E) ─────────────────────
def evaluate_eligibility(ctx: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Evaluate the ONE shared eligibility policy.

    ctx keys (all optional; missing → fail closed on that check):
      raw_result          dict | None   — engine output (signal CALL/PUT…)
      candles             list[dict]    — closed candles (last = source)
      last_tick_wall      float         — wall time of last real tick
      now                 float         — evaluation clock (default now)
      freshness_secs      float         — default DEFAULT_FRESHNESS_SECS
      timing_verified     bool          — server-time sync established?
      model_prov          dict          — artifact provenance record
      live_domain         str           — "otc"/"real"/"crypto"
      registry            dict          — validation registry (loaded)
      engine_feature_error bool         — feature generation failed flag
      engine_model_missing bool         — engine/artifact unavailable flag

    Deterministic: same ctx → same decision.  Checks run in a fixed
    order (data → model output → target → timing → source → registry),
    and the FIRST failure's reason code is reported.
    """
    checks: Dict[str, Any] = {}
    raw = ctx.get("raw_result")
    now = float(ctx.get("now") or time.time())

    # 1 — data availability
    checks["data_available"] = isinstance(raw, dict)
    if not checks["data_available"]:
        return False, DATA_UNAVAILABLE, checks

    # 2 — engine abstention / below threshold (NEUTRAL / NO_SIGNAL /
    #     non-directional output is an honest abstention, not an error)
    direction = raw.get("signal")
    checks["directional_candidate"] = direction in (CALL, PUT)
    if not checks["directional_candidate"]:
        return False, BELOW_ELIGIBILITY_THRESHOLD, checks

    # 3 — candle data validity (source candle finite OHLC)
    candles = ctx.get("candles") or []
    last = candles[-1] if isinstance(candles, list) and candles else None
    checks["candles_present"] = last is not None
    if not checks["candles_present"]:
        return False, DATA_INVALID, checks
    try:
        ohlc_ok = all(_finite(last.get(k)) for k in ("open", "high",
                                                     "low", "close"))
    except Exception:
        ohlc_ok = False
    checks["source_candle_valid"] = bool(ohlc_ok)
    if not checks["source_candle_valid"]:
        return False, DATA_INVALID, checks

    # 4 — sufficient history (engine's own documented minimum)
    checks["history_sufficient"] = len(candles) >= MIN_CLOSED_CANDLES
    if not checks["history_sufficient"]:
        return False, INSUFFICIENT_HISTORY, checks

    # 5 — feed freshness (documented stale threshold)
    freshness = float(ctx.get("freshness_secs") or DEFAULT_FRESHNESS_SECS)
    last_tick = ctx.get("last_tick_wall")
    age = (now - float(last_tick)) if _finite(last_tick) else None
    checks["data_fresh"] = age is not None and 0 <= age <= freshness
    checks["last_tick_age_secs"] = (round(age, 1) if age is not None
                                    else None)
    if not checks["data_fresh"]:
        return False, DATA_STALE, checks

    # 6 — feature generation
    if ctx.get("engine_feature_error"):
        checks["features_ok"] = False
        return False, FEATURE_ERROR, checks
    checks["features_ok"] = True

    # 7 — model availability
    if ctx.get("engine_model_missing"):
        checks["model_available"] = False
        return False, MODEL_UNAVAILABLE, checks
    prov = ctx.get("model_prov") or {}
    checks["model_available"] = bool(prov.get("load_ok"))
    if not checks["model_available"]:
        return False, MODEL_UNAVAILABLE, checks

    # 8 — model output validity (finite, in-range, schema ok)
    p = raw.get("ml_probability")
    if p is None:
        p = raw.get("p_green")
    prob_ok = p is None or (_finite(p) and 0.0 <= float(p) <= 1.0)
    conf = raw.get("confidence")
    conf_ok = conf is None or (_finite(conf) and 0 <= float(conf) <= 100)
    schema_ok = bool(prov.get("schema_ok"))
    checks["model_output_valid"] = bool(prob_ok and conf_ok and schema_ok)
    if not checks["model_output_valid"]:
        return False, MODEL_OUTPUT_INVALID, checks

    # 9 — target candle identity (must be the candle AFTER the source)
    period = ctx.get("period") or 60
    target_open = ctx.get("target_open_ts")
    src_time = last.get("time")
    target_ok = False
    if _finite(target_open) and _finite(src_time):
        target_ok = (float(target_open)
                     == float(src_time) + float(period)
                     and float(target_open) > float(src_time))
    checks["target_candle_valid"] = target_ok
    if not target_ok:
        return False, TARGET_CANDLE_INVALID, checks

    # 10 — timing verification (broker server-time sync)
    checks["timing_verified"] = bool(ctx.get("timing_verified"))
    if not checks["timing_verified"]:
        return False, TIMING_UNVERIFIED, checks

    # 11 — data source authorization (Phase 0 RC-03 — fail closed)
    auth = ctx.get("data_source_authorization") or DATA_SOURCE_AUTHORIZATION
    checks["data_source_authorized"] = auth in AUTHORIZED_STATUSES
    if not checks["data_source_authorized"]:
        return False, DATA_SOURCE_UNAUTHORIZED, checks

    # 12 — model validation (registry with real OOS evidence)
    registry = ctx.get("registry")
    if not isinstance(registry, dict):
        registry = load_validation_registry()
    live_domain = ctx.get("live_domain") or "otc"
    allowed, why = _registry_allows(prov, registry, live_domain)
    checks["model_validated"] = allowed
    if not allowed:
        return False, why or MODEL_NOT_VALIDATED, checks

    return True, "", checks


# ── the shared emission boundary gate (Task B) ─────────────────────────
def apply_truth_layer(raw: Optional[Dict[str, Any]],
                      asset: str,
                      period: int,
                      candles: Optional[List[Dict[str, Any]]] = None,
                      last_tick_wall: Optional[float] = None,
                      timing_verified: bool = False,
                      model_prov: Optional[Dict[str, Any]] = None,
                      live_domain: str = "otc",
                      registry: Optional[Dict[str, Any]] = None,
                      now: Optional[float] = None,
                      freshness_secs: Optional[float] = None,
                      internal_error: bool = False,
                      engine_feature_error: bool = False,
                      engine_model_missing: bool = False,
                      engine_reject_reason: Optional[str] = None,
                      data_source_authorization: Optional[str] = None
                      ) -> Dict[str, Any]:
    """Apply the truth layer to ONE emission-boundary result.

    * eligible  → CALL/PUT stands, canonical provenance attached.
    * rejected  → signal becomes NO_SIGNAL; the engine's preference is
                  preserved under research_* fields (offline evaluation
                  only); measured confidence is NOT attached to the
                  NO_SIGNAL (UI shows unavailable, never a fake 50).
    * exception upstream (internal_error=True) → NO_SIGNAL/INTERNAL_ERROR
                  — a generic exception can NEVER produce CALL/PUT.
    * engine_reject_reason → the producing chain itself already decided
                  it cannot speak (e.g. zero-tick fabricated candle);
                  the given reason code is honored verbatim.

    The returned dict ALWAYS carries the canonical contract fields.
    """
    now = float(now if now is not None else time.time())
    candles = candles if isinstance(candles, list) else []
    last = candles[-1] if candles else None
    src_close_ts = (float(last.get("time")) if last and _finite(
        last.get("time")) else None)
    target_open_ts = (src_close_ts + period
                      if src_close_ts is not None else None)
    prov = model_prov if isinstance(model_prov, dict) else cse_provenance()
    reg = registry if isinstance(registry, dict) else \
        load_validation_registry()

    if internal_error:
        eligible, reason, checks = False, INTERNAL_ERROR, \
            {"internal_error": True}
    elif engine_reject_reason:
        eligible, reason, checks = False, engine_reject_reason, \
            {"engine_reject": engine_reject_reason}
    else:
        ctx = {
            "raw_result": raw,
            "candles": candles,
            "last_tick_wall": last_tick_wall,
            "now": now,
            "freshness_secs": freshness_secs or DEFAULT_FRESHNESS_SECS,
            "timing_verified": timing_verified,
            "model_prov": prov,
            "live_domain": live_domain,
            "registry": reg,
            "engine_feature_error": engine_feature_error,
            "engine_model_missing": engine_model_missing,
            "period": period,
            "target_open_ts": target_open_ts,
            "data_source_authorization": data_source_authorization,
        }
        eligible, reason, checks = evaluate_eligibility(ctx)

    status = STATUS_ELIGIBLE if eligible else (
        STATUS_ERROR if reason == INTERNAL_ERROR else (
            STATUS_STALE if reason == DATA_STALE else STATUS_REJECTED))

    base = dict(raw) if isinstance(raw, dict) else {}
    # PHASE-1 defense in depth: credential-shaped keys never survive the
    # emission boundary (test: no secrets in serialized responses).
    base = _strip_secret_like(base)
    contract = build_contract_fields(
        asset=asset, period=period,
        source_close_ts=src_close_ts,
        target_open_ts=target_open_ts,
        created_ts=now,
        prov=prov,
        reason_code=reason,
        status=status,
        validation_status=("validated" if eligible else "unvalidated"),
        data_freshness=("fresh" if checks.get("data_fresh")
                        else ("stale" if reason == DATA_STALE
                              else "unknown")))

    out: Dict[str, Any] = {}
    if eligible:
        out.update(base)
        out["signal"] = base.get("signal")
        out["confidence"] = base.get("confidence")
        out.update(contract)
        out["eligibility_checks"] = checks
        out["research_only"] = False
        return out

    # rejected / stale / error → NO_SIGNAL (fail closed)
    out.update(base)
    out["signal"] = NO_SIGNAL
    # preserve the engine's DIAGNOSTIC preference as research-only
    if base.get("signal") in (CALL, PUT):
        out["research_direction"] = base.get("signal")
        out["research_confidence"] = base.get("confidence")
        out["research_strategy"] = (base.get("strategy")
                                    or base.get("signal_source"))
        out["research_reason"] = (
            f"engine preferred {base.get('signal')} but the Phase-1 "
            f"eligibility policy rejected it ({reason}) — research-only, "
            f"not an actionable signal")
    else:
        out.setdefault("research_direction", None)
        out.setdefault("research_confidence", None)
        out.setdefault("research_strategy", base.get("strategy"))
    # a NO_SIGNAL must not carry a measured-looking confidence
    out["confidence"] = None
    out["strength"] = "NO_SIGNAL"
    out["truth_status"] = contract["truth_status"]
    out["reason_code"] = reason
    out.update({k: v for k, v in contract.items()
                if k not in ("truth_status", "reason_code")})
    out["eligibility_checks"] = checks
    out["research_only"] = True
    return out


def truth_layer_state() -> Dict[str, Any]:
    """Public, secret-free snapshot for /api/truth-layer and docs."""
    reg = load_validation_registry()
    prov = cse_provenance()
    return {
        "policy_version": POLICY_VERSION,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "data_source_id": DATA_SOURCE_ID,
        "data_source_authorization": DATA_SOURCE_AUTHORIZATION,
        "actionable_signals_possible": bool(
            reg.get("approved")
            and DATA_SOURCE_AUTHORIZATION in AUTHORIZED_STATUSES),
        "validation_registry": {
            "exists": reg.get("exists"),
            "entries": len(reg.get("approved") or []),
            "error": reg.get("error"),
        },
        "cse_provenance": prov,
        "reason_codes": list(ALL_REASON_CODES),
        "note": (
            "Fail-closed by default: unauthorized data source (Phase 0 "
            "RC-03), no validation registry, and timing unverified on "
            "the default backend mean every directional output is "
            "NO_SIGNAL (research-only). See docs/PHASE1_TRUTH_LAYER.md."),
    }
