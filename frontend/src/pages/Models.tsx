/**
 * Models — available models, status, version, training/validation state,
 * out-of-sample performance, last training time, provenance. The backend
 * reports honestly (most fields null/unvalidated today) and this page
 * renders that honestly — no fabricated training results.
 */

import { useEffect, useState } from "react";
import { t } from "../i18n/dict";
import { api } from "../services/api/client";
import type { ModelsRow } from "../services/websocket/protocol";
import { useApp } from "../stores/AppStore";

export function ModelsPage() {
  const [rows, setRows] = useState<ModelsRow[] | null>(null);
  const [overview, setOverview] = useState<Record<string, unknown> | null>(null);
  const { state } = useApp();

  useEffect(() => {
    api.models()
      .then((r) => setRows(Array.isArray(r) ? r : ((r as { models?: ModelsRow[] }).models ?? [])))
      .catch(() => setRows([]));
    api.predictionOverview()
      .then((r) => setOverview(r as Record<string, unknown>))
      .catch(() => setOverview(null));
  }, []);

  return (
    <div className="mods">
      <section className="panel">
        <div className="panel-head"><h3>{t("models.available")}</h3></div>
        <div className="panel-body">
          {rows === null && <div className="skeleton" style={{ height: 120 }} />}
          {rows?.length === 0 && (
            <div className="mods-none">
              <p>{t("models.none")}</p>
              <span className="chip neutral">Truth Layer: unvalidated</span>
            </div>
          )}
          {rows && rows.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>{t("models.available")}</th><th>{t("models.version")}</th>
                  <th>{t("models.status")}</th><th>{t("models.validation")}</th>
                  <th>{t("models.lastTrained")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((m, i) => (
                  <tr key={m.model_id ?? i}>
                    <td className="mono">{m.model_id ?? "—"}</td>
                    <td className="mono">{m.version ?? "—"}</td>
                    <td>{m.status ?? "—"}{m.active === false ? "" : m.active ? " (active)" : ""}</td>
                    <td>{m.status === "validated" || m.oos ? "✓" : "✗"}</td>
                    <td className="nowrap">{m.trained_at ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h3>Registry</h3></div>
        <div className="panel-body">
          <div className="kv"><span className="k">actionable_signals_possible</span>
            <span className="v mono">{String(state.truth?.actionable_signals_possible ?? "—")}</span></div>
          <div className="kv"><span className="k">validation_registry</span>
            <span className="v mono">core/model_validation.json — absent → unvalidated</span></div>
          <div className="kv"><span className="k">data_source_authorization</span>
            <span className="v mono">unauthorized</span></div>
          {overview && Object.entries(overview).slice(0, 10).map(([k, v]) => (
            <div className="kv" key={k}><span className="k">{k}</span>
              <span className="v mono">{String(v)}</span></div>
          ))}
        </div>
      </section>
    </div>
  );
}
