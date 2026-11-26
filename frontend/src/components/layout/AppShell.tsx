/**
 * AppShell — desktop sidebar + mobile bottom nav + sticky topbar +
 * the RESEARCH honesty banner (user decision: prominent + chips).
 * Mobile is designed independently (bottom nav, safe areas), not
 * squeezed from desktop (directive Phase 3).
 */

import type { ReactNode } from "react";
import { useRoute, type RouteId } from "../../routes";
import { useApp } from "../../stores/AppStore";
import { t } from "../../i18n/dict";
import { ConnBadge } from "./ConnBadge";
import "./shell.css";

const NAV: { id: RouteId; label: string; icon: string }[] = [
  { id: "overview",  label: t("nav.overview"),  icon: "grid" },
  { id: "terminal",  label: t("nav.terminal"), icon: "chart" },
  { id: "history",   label: t("nav.history"),  icon: "clock" },
  { id: "analytics", label: t("nav.analytics"), icon: "bars" },
  { id: "models",    label: t("nav.models"),   icon: "cpu" },
  { id: "settings",  label: t("nav.settings"), icon: "gear" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const [route, navigate] = useRoute();
  const { state } = useApp();

  return (
    <div className="shell">
      {/* ── desktop sidebar (≥1024px) ─────────────────────────────── */}
      <aside className="shell-sidebar" aria-label="Main navigation">
        <div className="shell-brand">
          <div className="shell-logo" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"
                 strokeLinejoin="round">
              <path d="M3 12h4l3-9 4 18 3-9h4" />
            </svg>
          </div>
          <div>
            <h1 className="shell-title">{t("brand")}</h1>
            <span className="shell-sub">{t("brandSub")}</span>
          </div>
        </div>
        <nav className="shell-nav">
          {NAV.map((n) => (
            <button key={n.id} type="button"
              className={`nav-item ${route === n.id ? "active" : ""}`}
              onClick={() => navigate(n.id)}
              aria-current={route === n.id ? "page" : undefined}>
              <NavIcon name={n.icon} />
              <span className="nav-label">{n.label}</span>
            </button>
          ))}
        </nav>
        <div className="shell-sidefoot">
          <ConnBadge />
        </div>
      </aside>

      {/* ── mobile sticky topbar ──────────────────────────────────── */}
      <header className="shell-topbar">
        <div className="shell-topbar-row">
          <span className="topbar-title">{t("brand")}</span>
          <ConnBadge />
        </div>
      </header>

      {/* ── honesty banner: TRUTH LAYER state, always visible ─────── */}
      {state.truth && state.truth.actionable_signals_possible === false && (
        <div className="research-banner" role="status">
          <span className="chip research">RESEARCH ONLY</span>
          <div className="research-copy">
            <strong>{t("banner.research")}</strong>
            <span>{t("banner.researchSub")}</span>
          </div>
        </div>
      )}

      <main id="main-content" className="shell-main">{children}</main>

      {/* ── mobile bottom nav (safe-area aware) ───────────────────── */}
      <nav className="shell-bottomnav" aria-label="Mobile navigation">
        {NAV.map((n) => (
          <button key={n.id} type="button"
            className={`bnav-item ${route === n.id ? "active" : ""}`}
            onClick={() => navigate(n.id)}
            aria-current={route === n.id ? "page" : undefined}>
            <NavIcon name={n.icon} />
            <span className="bnav-label">{shortLabel(n.id)}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

/** bottom-nav labels stay ≤5 chars so nothing clips at 320px */
function shortLabel(id: RouteId): string {
  switch (id) {
    case "overview":  return "হোম";
    case "terminal":  return "টার্মিনাল";
    case "history":   return "হিস্টোরি";
    case "analytics": return "রেজাল্ট";
    case "models":    return "মডেল";
    case "settings":  return "সেটিংস";
  }
}

function NavIcon({ name }: { name: string }) {
  const p = { fill: "none", stroke: "currentColor", strokeWidth: 2,
             strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (name) {
    case "grid": return <svg width="18" height="18" viewBox="0 0 24 24" {...p}>
      <rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>;
    case "chart": return <svg width="18" height="18" viewBox="0 0 24 24" {...p}>
      <path d="M3 3v18h18" /><path d="M7 14l3-6 4 4 5-8" /></svg>;
    case "clock": return <svg width="18" height="18" viewBox="0 0 24 24" {...p}>
      <circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /></svg>;
    case "bars": return <svg width="18" height="18" viewBox="0 0 24 24" {...p}>
      <path d="M12 20V10" /><path d="M18 20V4" /><path d="M6 20v-4" /></svg>;
    case "cpu": return <svg width="18" height="18" viewBox="0 0 24 24" {...p}>
      <rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" />
      <path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3" /></svg>;
    default: return <svg width="18" height="18" viewBox="0 0 24 24" {...p}>
      <circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>;
  }
}
