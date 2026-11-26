/**
 * Terminal — the live trading terminal (directive Phase 2, layout:
 * top command bar / dominant chart / signal intelligence panel /
 * recent-signals strip; mobile stacks them with the chart on top).
 */

import { useApp } from "../stores/AppStore";
import { t } from "../i18n/dict";
import { CandleChart } from "../components/charts/CandleChart";
import { SignalPanel } from "../components/signals/SignalPanel";
import { MarketSwitch, PairSelect, PriceTicker, Countdown } from "../components/market/market";
import { toSignalView, outcomeBadge } from "../services/signals/states";
import type { Prediction } from "../services/websocket/protocol";
import { useEffect, useState } from "react";
import { api } from "../services/api/client";
import "../components/charts/chart.css";

export function TerminalPage() {
  const { state } = useApp();
  return (
    <div className="terminal-grid">
      {/* ── command bar + chart ─────────────────────────────────────── */}
      <section className="panel term-main">
        <div className="term-bar">
          <MarketSwitch />
          <PairSelect />
          <div className="spacer" />
          <PriceTicker />
          <Countdown />
        </div>
        <div className="chart-frame">
          <CandleChart candles={state.candles} prediction={state.prediction}
                       researchMode={state.truth?.actionable_signals_possible === false} />
        </div>
      </section>

      {/* ── signal intelligence panel ───────────────────────────────── */}
      <SignalPanel />

      {/* ── recent signals strip (bottom) ────────────────────────────── */}
      <RecentSignals asset={state.asset} period={state.period} />
    </div>
  );
}

function RecentSignals({ asset, period }: { asset: string; period: number }) {
  const [rows, setRows] = useState<Prediction[] | null>(null);
  useEffect(() => {
    let alive = true;
    api.signalsLatest()
      .then((r) => {
        if (!alive) return;
        const list = Array.isArray(r) ? r
          : Array.isArray((r as { signals?: Prediction[] }).signals)
            ? (r as { signals: Prediction[] }).signals : [];
        setRows(list.filter((p) => p.symbol === undefined || p.symbol === asset));
      })
      .catch(() => { if (alive) setRows([]); });
    return () => { alive = false; };
  }, [asset, period]);

  return (
    <section className="panel term-history">
      <div className="panel-head"><h3>{t("terminal.recentSignals")}</h3></div>
      <div className="panel-body">
        {rows === null && <SkeletonRows />}
        {rows?.length === 0 && <p className="muted">{t("common.noData")}</p>}
        {rows && rows.length > 0 && (
          <table className="table">
            <thead>
              <tr><th>{t("history.pair")}</th><th>{t("history.direction")}</th>
                  <th>{t("signal.state")}</th><th>{t("history.result")}</th>
                  <th>{t("signal.reason")}</th></tr>
            </thead>
            <tbody>
              {rows.slice(0, 8).map((p) => {
                const v = toSignalView(p);
                const oc = outcomeBadge(p);
                return (
                  <tr key={p.signal_id ?? p.ctime ?? Math.random()}>
                    <td className="mono">{p.symbol ?? asset}</td>
                    <td>{v.researchDirection ?? v.direction}</td>
                    <td><span className={`chip ${v.state === "RESEARCH_ONLY" ? "research" : "neutral"}`}>
                      {v.state}</span></td>
                    <td>{oc === "win" ? <span className="chip pos">{t("history.win")}</span>
                        : oc === "loss" ? <span className="chip neg">{t("history.loss")}</span>
                        : <span className="chip neutral">{t("history.ungraded")}</span>}</td>
                    <td className="mono reason-cell">{v.reasonCode ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function SkeletonRows() {
  return <div className="skel-rows">{[0, 1, 2].map((i) =>
    <div key={i} className="skeleton" style={{ height: 22, marginBottom: 8 }} />)}</div>;
}
