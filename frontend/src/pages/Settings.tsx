/**
 * Settings — every control ACTUALLY works (directive Phase 2: no
 * decorative switches). Each group below is wired to the store or a
 * real browser capability; system diagnostics fetches real endpoints.
 */

import { useEffect, useState } from "react";
import { useApp } from "../stores/AppStore";
import { t } from "../i18n/dict";
import { api } from "../services/api/client";
import "./Settings.css";

export function SettingsPage() {
  const { state, saveSettings, ws, select } = useApp();
  const [diag, setDiag] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    api.dataQuality().then((d) => setDiag(d as Record<string, unknown>)).catch(() => setDiag(null));
  }, []);

  return (
    <div className="set">
      <section className="panel">
        <div className="panel-head"><h3>{t("settings.appearance")}</h3></div>
        <div className="panel-body">
          <Toggle label={t("settings.dark")} checked={state.settings.theme === "dark"}
            onChange={(v) => saveSettings({ theme: v ? "dark" : "light" })} />
          <div className="set-row">
            <span className="set-label">{t("settings.language")}</span>
            <span className="muted">বাংলা (BN) — ফিন্যান্সিয়াল টার্ম ইংরেজি</span>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h3>{t("settings.refresh")}</h3></div>
        <div className="panel-body">
          <div className="set-row">
            <span className="set-label">{t("settings.refresh")}</span>
            <select value={String(state.settings.refreshMs)} onChange={(e) =>
              saveSettings({ refreshMs: Number(e.target.value) })}>
              <option value="5000">5s</option><option value="10000">10s</option>
              <option value="30000">30s</option><option value="60000">60s</option>
            </select>
          </div>
          <Toggle label={t("settings.sound")} checked={state.settings.sound}
            onChange={(v) => saveSettings({ sound: v })} />
          <Toggle label={t("countdown")} checked={state.settings.showCountdown}
            onChange={(v) => saveSettings({ showCountdown: v })} />
          <div className="set-row">
            <span className="set-label">WS</span>
            <button type="button" className="btn small"
              onClick={() => { ws.disconnect(); ws.connect(); }}>
              {t("common.retry")}
            </button>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h3>{t("market")}</h3></div>
        <div className="panel-body">
          <div className="set-row">
            <span className="set-label">{t("period")}</span>
            <select value={String(state.period)} onChange={(e) =>
              select(state.market, state.asset, Number(e.target.value))}>
              {[5, 15, 30, 60, 120, 300].map((p) => <option key={p} value={p}>{p}s</option>)}
            </select>
          </div>
          <div className="set-row">
            <span className="set-label">{t("history.pair")}</span>
            <span className="mono">{state.asset}</span>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><h3>{t("settings.diagnostics")}</h3></div>
        <div className="panel-body">
          {diag === null && <p className="muted">{t("common.loading")}</p>}
          {diag && Object.entries(diag).slice(0, 12).map(([k, v]) => (
            <div className="kv" key={k}>
              <span className="k">{k}</span>
              <span className="v mono">{typeof v === "object" ? JSON.stringify(v).slice(0, 60) : String(v)}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function Toggle({ label, checked, onChange }:
  { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="set-row">
      <span className="set-label" id={`lbl-${label}`}>{label}</span>
      <button type="button" role="switch" aria-checked={checked}
        aria-label={label}
        className={`toggle ${checked ? "on" : ""}`}
        onClick={() => onChange(!checked)}>
        <span className="toggle-knob" />
      </button>
    </div>
  );
}
