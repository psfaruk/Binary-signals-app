/**
 * SignalPanel — the Signal Intelligence Panel (directive Phase 3).
 * Every value renders from the truth-layer contract; unavailable values
 * render as "—" with the unavailable style — NEVER a fabricated number.
 */

import { useApp } from "../../stores/AppStore";
import { t } from "../../i18n/dict";
import { toSignalView, confidenceLabel } from "../../services/signals/states";
import type { UiSignalState } from "../../services/signals/states";
import "./SignalPanel.css";

export function SignalPanel() {
  const { state } = useApp();
  const view = toSignalView(state.prediction);

  return (
    <section className="panel sig-panel" data-testid="signal-panel" aria-label={t("signal.title")}>
      <div className="panel-head">
        <h3>{t("signal.title")}</h3>
        <StateBadge state={view.state} />
      </div>

      <div className="panel-body">
        {/* the big direction block */}
        <div className={`sig-hero ${view.state.toLowerCase()}`}>
          <span className="sig-dir" data-testid="sig-direction">
            {view.direction === "CALL" ? t("signal.call")
             : view.direction === "PUT" ? t("signal.put")
             : view.state === "RESEARCH_ONLY" && view.researchDirection
               ? `${view.researchDirection} → ${t("signal.noSignal")}`
               : t("signal.noSignal")}
          </span>
          <span className="chip research">{t("signal.research")}</span>
        </div>

        <div className="sig-kvs">
          <Row k={t("signal.confidence")} v={confidenceLabel(view.confidence)}
               unavailable={view.confidence === null} />
          {view.state === "RESEARCH_ONLY" && (
            <Row k={t("signal.confidence")} v={`${view.researchDirection ?? "—"} ${
              confidenceLabel(view.researchConfidence)}`}
              note="research" />
          )}
          <Row k={t("signal.strength")} v={view.isActionable
            ? (state.prediction?.strength ?? "—") : "NO_SIGNAL"} />
          <Row k={t("signal.strategy")} v={str(state.prediction?.research_strategy
            ?? state.prediction?.strategy)} />
          <Row k={t("signal.regime")} v={str(state.prediction?.regime)} />
          <Row k={t("signal.timingVerified")}
               v={state.prediction?.reason_code === "TIMING_UNVERIFIED" ? "✗" : "✗"} />
          <Row k={t("signal.modelValidated")}
               v={state.prediction?.validation_status === "validated" ? "✓" : "✗"} />
          <Row k={t("signal.eligible")} v={view.isActionable ? "✓" : "✗"} />
        </div>

        {/* abstention reason — always present for non-actionable states */}
        {view.reasonCode && (
          <div className="sig-reason" data-testid="sig-reason">
            <span className="mono reason-code">{view.reasonCode}</span>
            <p>{view.reasonText}</p>
          </div>
        )}

        {/* evidence — supporting vs opposing, only what the frame carries */}
        {Array.isArray(state.prediction?.reasons) &&
          state.prediction.reasons.length > 0 && (
          <details className="sig-evidence">
            <summary>{t("signal.evidence")} ({state.prediction.reasons.length})</summary>
            <ul>
              {state.prediction.reasons.slice(0, 12).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </details>
        )}

        <div className="sig-target mono">
          {state.prediction?.target_candle_open_time_utc
            ? `${t("signal.target")}: ${state.prediction.target_candle_open_time_utc}`
            : `${t("signal.target")}: —`}
        </div>
      </div>
    </section>
  );
}

function Row({ k, v, unavailable, note }:
  { k: string; v: string; unavailable?: boolean; note?: string }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className={`v ${unavailable ? "unavail" : ""}`}>{v}{note ? ` (${note})` : ""}</span>
    </div>
  );
}

function str(v: unknown): string {
  return typeof v === "string" && v ? v : "—";
}

/** state chip: 1:1 with backend contract states */
export function StateBadge({ state }: { state: UiSignalState }) {
  const cls: Record<UiSignalState, string> = {
    CALL: "pos", PUT: "neg", NO_SIGNAL: "neutral",
    STALE: "warn", ERROR: "neg", RESEARCH_ONLY: "research",
  };
  const label: Record<UiSignalState, string> = {
    CALL: "CALL", PUT: "PUT", NO_SIGNAL: "NO_SIGNAL",
    STALE: "STALE", ERROR: "ERROR", RESEARCH_ONLY: "RESEARCH_ONLY",
  };
  return <span className={`chip ${cls[state]}`}>{label[state]}</span>;
}
