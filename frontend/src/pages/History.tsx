/**
 * History — searchable, filterable, paginated signal history.
 * Historical rows render exactly what the backend recorded; the UI
 * NEVER modifies a historical signal's fields (directive Phase 7.5).
 * Filters: pair search, direction, result, market (otc/real), date.
 */

import { useEffect, useMemo, useState } from "react";
import { useApp } from "../stores/AppStore";
import { t } from "../i18n/dict";
import { toSignalView, outcomeBadge } from "../services/signals/states";
import type { Prediction } from "../services/websocket/protocol";
import "./History.css";

const PAGE = 25;

type DirFilter = "all" | "CALL" | "PUT" | "NO_SIGNAL";
type ResFilter = "all" | "win" | "loss" | "ungraded";

export function HistoryPage() {
  const { state, ws } = useApp();
  const [rows, setRows] = useState<Prediction[] | null>(null);
  const [pairQ, setPairQ] = useState("");
  const [dirF, setDirF] = useState<DirFilter>("all");
  const [resF, setResF] = useState<ResFilter>("all");
  const [mktF, setMktF] = useState<"all" | "otc" | "real">("all");
  const [dateF, setDateF] = useState("");
  const [page, setPage] = useState(1);

  // Load history via the WS `signals` request (asset=ALL, the backend's
  // own cross-pair history path — same wire the legacy UI uses).
  useEffect(() => {
    setRows(null);
    const send = () => ws.send({ type: "signals", asset: "ALL", period: state.period, limit: 200 });
    if (ws.getState() !== "open") { ws.connect(); setTimeout(send, 1200); }
    else send();
    const onFrame = (f: { type: string; signals?: Prediction[] }) => {
      if (f.type === "signals" && Array.isArray(f.signals)) setRows(f.signals);
    };
    ws.bind({ onFrame });
    return () => ws.bind({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.period]);

  const filtered = useMemo(() => {
    if (!rows) return [];
    return rows.filter((p) => {
      if (pairQ && !(p.symbol ?? "").toLowerCase().includes(pairQ.toLowerCase())) return false;
      const v = toSignalView(p);
      if (dirF !== "all") {
        const d = v.researchDirection ?? v.direction;
        if (d !== dirF) return false;
      }
      if (mktF !== "all") {
        const isOtc = (p.symbol ?? "").endsWith("_otc");
        if (mktF === "otc" && !isOtc) return false;
        if (mktF === "real" && isOtc) return false;
      }
      const oc = outcomeBadge(p);
      if (resF !== "all" && oc !== resF) return false;
      if (dateF && p.created_at_utc && !p.created_at_utc.startsWith(dateF)) return false;
      return true;
    });
  }, [rows, pairQ, dirF, resF, mktF, dateF]);

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE));
  const cur = Math.min(page, pages);
  const slice = filtered.slice((cur - 1) * PAGE, cur * PAGE);

  return (
    <div className="hist">
      <div className="panel hist-filters">
        <input className="hist-search" placeholder={t("history.search")} value={pairQ}
          onChange={(e) => { setPairQ(e.target.value); setPage(1); }} />
        <select value={dirF} onChange={(e) => { setDirF(e.target.value as DirFilter); setPage(1); }}>
          <option value="all">{t("history.direction")}: {t("history.all")}</option>
          <option value="CALL">CALL</option>
          <option value="PUT">PUT</option>
          <option value="NO_SIGNAL">NO_SIGNAL</option>
        </select>
        <select value={resF} onChange={(e) => { setResF(e.target.value as ResFilter); setPage(1); }}>
          <option value="all">{t("history.result")}: {t("history.all")}</option>
          <option value="win">{t("history.win")}</option>
          <option value="loss">{t("history.loss")}</option>
          <option value="ungraded">{t("history.ungraded")}</option>
        </select>
        <select value={mktF} onChange={(e) => { setMktF(e.target.value as "otc" | "real" | "all"); setPage(1); }}>
          <option value="all">{t("market")}: {t("history.all")}</option>
          <option value="otc">{t("market.otc")}</option>
          <option value="real">{t("market.real")}</option>
        </select>
        <input type="date" value={dateF} onChange={(e) => { setDateF(e.target.value); setPage(1); }} />
        <button type="button" className="btn small" onClick={() => exportCsv(filtered)}>
          {t("history.export")}
        </button>
      </div>

      <section className="panel">
        <div className="panel-head"><h3>{t("nav.history")}</h3>
          <span className="muted mono">{filtered.length}</span></div>
        <div className="panel-body hist-table-wrap">
          {rows === null && <div className="skeleton" style={{ height: 200 }} />}
          {rows?.length === 0 && <p className="muted">{t("common.noData")}</p>}
          {slice.length === 0 && rows?.length !== 0 && rows !== null &&
            <p className="muted">{t("history.empty")}</p>}
          {slice.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>{t("history.pair")}</th><th>{t("history.direction")}</th>
                  <th>{t("signal.state")}</th><th>{t("signal.reason")}</th>
                  <th>{t("history.result")}</th><th>{t("signal.entry")}</th>
                </tr>
              </thead>
              <tbody>
                {slice.map((p) => {
                  const v = toSignalView(p);
                  const oc = outcomeBadge(p);
                  return (
                    <tr key={p.signal_id ?? `${p.symbol}-${p.ctime}`}>
                      <td className="mono">{p.symbol ?? "—"}</td>
                      <td>{v.researchDirection ?? v.direction ?? "—"}</td>
                      <td><span className={`chip ${v.state === "RESEARCH_ONLY" ? "research" : "neutral"}`}>{v.state}</span></td>
                      <td className="mono reason-cell">{v.reasonCode ?? "—"}</td>
                      <td>{oc === "win" ? <span className="chip pos">{t("history.win")}</span>
                          : oc === "loss" ? <span className="chip neg">{t("history.loss")}</span>
                          : <span className="chip neutral">{t("history.ungraded")}</span>}</td>
                      <td className="mono nowrap">{p.created_at_utc ?? "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <div className="hist-pager">
        <button type="button" className="btn small" disabled={cur <= 1}
          onClick={() => setPage(cur - 1)}>‹</button>
        <span className="muted">{t("history.page")} {cur}/{pages}</span>
        <button type="button" className="btn small" disabled={cur >= pages}
          onClick={() => setPage(cur + 1)}>›</button>
      </div>
    </div>
  );
}

function exportCsv(rows: Prediction[]): void {
  const head = "symbol,direction,state,reason_code,result,created_at_utc,signal_id\n";
  const body = rows.map((p) => {
    const v = toSignalView(p);
    const oc = outcomeBadge(p);
    const esc = (s: unknown) => `"${String(s ?? "").replace(/"/g, '""')}"`;
    return [p.symbol, v.researchDirection ?? v.direction, v.state,
            v.reasonCode ?? "", oc, p.created_at_utc ?? "", p.signal_id ?? ""]
      .map(esc).join(",");
  }).join("\n");
  const blob = new Blob([head + body], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "signal-history.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}
