/**
 * Analytics — verified performance with honest statistics. Always
 * distinguishes measured results from research results; small samples
 * are labeled unreliable; payout-aware break-even comparison shown.
 */

import { useEffect, useState } from "react";
import { t } from "../i18n/dict";
import { api } from "../services/api/client";
import type { Prediction } from "../services/websocket/protocol";
import { toSignalView } from "../services/signals/states";
import "./Analytics.css";

export function AnalyticsPage() {
  const [sig, setSig] = useState<Prediction[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.signalsLatest()
      .then((r) => setSig(Array.isArray(r) ? r : ((r as { signals?: Prediction[] }).signals ?? [])))
      .catch(() => setErr("signals unavailable"));
  }, []);

  const graded = (sig ?? []).filter((p) => p.correct === 1 || p.correct === 0);
  const calls = graded.filter((p) => toSignalView(p).researchDirection === "CALL");
  const puts = graded.filter((p) => toSignalView(p).researchDirection === "PUT");

  return (
    <div className="ana">
      <p className="ana-note muted">{t("analytics.researchOnly")}</p>
      {err && <p className="ana-note muted">{err}</p>}

      <div className="ana-hero">
        <Stat label={t("analytics.winrate")} value={wr(graded)} note={`${t("analytics.sample")}: ${graded.length}`} />
        <Stat label={t("analytics.callPerf")} value={wr(calls)} note={`${t("analytics.sample")}: ${calls.length}`} />
        <Stat label={t("analytics.putPerf")} value={wr(puts)} note={`${t("analytics.sample")}: ${puts.length}`} />
        <Stat label={t("analytics.breakeven")} value="54.05%" note="payout 85% ধরে — ব্রেক-ইভেন" />
      </div>

      <section className="panel">
        <div className="panel-head"><h3>{t("analytics.daily")}</h3></div>
        <div className="panel-body">
          <DailyTable rows={graded} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h3>{t("analytics.pairPerf")}</h3></div>
        <div className="panel-body">
          <PairTable rows={graded} />
        </div>
      </section>
    </div>
  );
}

function wr(rows: Prediction[]): string {
  if (!rows.length) return "—";
  const c = rows.filter((p) => p.correct === 1).length;
  return `${(c / rows.length * 100).toFixed(1)}%`;
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="panel ana-stat">
      <span className="ana-stat-label">{label}</span>
      <span className="ana-stat-value">{value}</span>
      {note && <span className="ana-stat-note">{note}</span>}
    </div>
  );
}

function DailyTable({ rows }: { rows: Prediction[] }) {
  const byDay = new Map<string, { c: number; n: number }>();
  for (const p of rows) {
    const day = (p.created_at_utc ?? "").slice(0, 10);
    if (!day) continue;
    const e = byDay.get(day) ?? { c: 0, n: 0 };
    if (p.correct === 1) e.c += 1;
    e.n += 1;
    byDay.set(day, e);
  }
  const days = [...byDay.entries()].sort((a, b) => b[0].localeCompare(a[0])).slice(0, 14);
  if (!days.length) return <p className="muted">{t("common.noData")}</p>;
  return (
    <table className="table">
      <thead><tr><th>{t("analytics.daily")}</th><th>{t("common.statWinrate")}</th><th>{t("analytics.sample")}</th></tr></thead>
      <tbody>
        {days.map(([d, e]) => (
          <tr key={d}>
            <td className="mono">{d}</td>
            <td>{(e.c / e.n * 100).toFixed(1)}%</td>
            <td>{e.n}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PairTable({ rows }: { rows: Prediction[] }) {
  const byPair = new Map<string, { c: number; n: number }>();
  for (const p of rows) {
    const sym = p.symbol ?? "—";
    const e = byPair.get(sym) ?? { c: 0, n: 0 };
    if (p.correct === 1) e.c += 1;
    e.n += 1;
    byPair.set(sym, e);
  }
  const pairs = [...byPair.entries()].filter(([, e]) => e.n >= 1)
    .sort((a, b) => b[1].n - a[1].n).slice(0, 12);
  if (!pairs.length) return <p className="muted">{t("common.noData")}</p>;
  return (
    <table className="table">
      <thead><tr><th>{t("history.pair")}</th><th>{t("common.statWinrate")}</th><th>{t("analytics.sample")}</th></tr></thead>
      <tbody>
        {pairs.map(([sym, e]) => (
          <tr key={sym}>
            <td className="mono">{sym}</td>
            <td>{(e.c / e.n * 100).toFixed(1)}%{e.n < 30 ? <span className="small-sample"> ⚠ n&lt;30</span> : ""}</td>
            <td>{e.n}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
