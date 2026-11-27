/**
 * Overview — the honest dashboard. Market connection status, active
 * market, pairs, latest signal, today's count, VERIFIED performance
 * stats only, feed health, data freshness. No invented statistics —
 * every number comes from the live backend response or renders "—".
 */

import { useApp } from "../stores/AppStore";
import { t } from "../i18n/dict";
import { toSignalView } from "../services/signals/states";
import { api } from "../services/api/client";
import { useEffect, useState } from "react";
import type { WinrateResponse } from "../services/websocket/protocol";
import "./Overview.css";

export function OverviewPage() {
  const { state, refreshOverview } = useApp();
  const [win, setWin] = useState<WinrateResponse | null>(null);
  const [winErr, setWinErr] = useState<string | null>(null);

  useEffect(() => {
    void refreshOverview();
    api.winrate().then(setWin).catch(() => setWinErr("unavailable"));
  }, [refreshOverview]);

  const view = toSignalView(state.prediction);
  const overall = win?.overall;

  return (
    <div className="ovw">
      <div className="ovw-hero">
        <Stat label={t("common.statSignals")} value={state.signalsToday ?? "—"}
              note={t("overview.today")} />
        <Stat label={t("common.statWinrate")}
              value={overall?.total ? `${((overall.correct ?? 0) / overall.total * 100).toFixed(1)}%` : "—"}
              note={`${t("common.statSamples")}: ${overall?.total ?? "—"}`} />
        <Stat label={t("overview.pairs")}
              value={state.market === "otc" ? state.pairsOtc.length : state.pairsReal.length}
              note={state.market === "otc" ? t("market.otc") : t("market.real")} />
        <Stat label={t("overview.dataFreshness")}
              value={state.wsStale ? t("connect.stale") : t("feed.fresh")}
              note={`${t("feed.lastTick")}`} />
      </div>

      <p className="ovw-note muted">{t("overview.noInvented")}</p>
      {winErr && <p className="ovw-note muted">{t("common.unavailable")}: /api/winrate</p>}

      <div className="ovw-grid">
        <section className="panel">
          <div className="panel-head"><h3>{t("overview.truthState")}</h3></div>
          <div className="panel-body">
            <div className="kv"><span className="k">actionable_signals_possible</span>
              <span className="v mono">{String(state.truth?.actionable_signals_possible ?? "—")}</span></div>
            <div className="kv"><span className="k">policy_version</span>
              <span className="v mono">{str(state.truth?.policy_version)}</span></div>
            <div className="kv"><span className="k">db</span>
              <span className="v mono">{str(state.health?.db)}</span></div>
            <div className="kv"><span className="k">feed</span>
              <span className="v mono">{str(state.health?.feed)}</span></div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head"><h3>{t("signal.title")}</h3></div>
          <div className="panel-body">
            <div className="ovw-signal">
              <span className={`sig-big ${view.state.toLowerCase()}`}>
                {view.direction === "NO_SIGNAL" ? t("signal.noSignal") : view.direction}
              </span>
              {view.state === "RESEARCH_ONLY" && <span className="chip research">RESEARCH ONLY</span>}
            </div>
            <p className="muted">{view.reasonText}</p>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head"><h3>{t("overview.feedHealth")}</h3></div>
          <div className="panel-body">
            <div className="kv"><span className="k">connected</span>
              <span className="v mono">{str(state.health?.connected ?? state.conn)}</span></div>
            <div className="kv"><span className="k">ws</span>
              <span className="v mono">{state.conn}</span></div>
            <div className="kv"><span className="k">candles</span>
              <span className="v mono">{state.candles.length}</span></div>
          </div>
        </section>
      </div>
    </div>
  );
}

function Stat({ label, value, note }: { label: string; value: string | number; note?: string }) {
  return (
    <div className="panel ovw-stat">
      <span className="ovw-stat-label">{label}</span>
      <span className="ovw-stat-value" data-testid="ovw-value">{value}</span>
      {note && <span className="ovw-stat-note">{note}</span>}
    </div>
  );
}

function str(v: unknown): string {
  return typeof v === "string" && v ? v : "—";
}
