/** ConnBadge — live connection-state chip (never fakes "live"). */

import { useApp } from "../../stores/AppStore";
import { t } from "../../i18n/dict";
import type { ConnState } from "../../services/websocket/manager";

const LABEL: Record<ConnState, { bn: string; cls: string }> = {
  idle:     { bn: t("connect.idle"),     cls: "neutral" },
  connecting:{ bn: t("connect.backoff"), cls: "warn" },
  open:     { bn: t("connect.live"),    cls: "pos" },
  backoff:  { bn: t("connect.backoff"), cls: "warn" },
  closed:   { bn: t("connect.closed"),  cls: "neutral" },
  failed:   { bn: t("connect.failed"),  cls: "neg" },
};

export function ConnBadge() {
  const { state } = useApp();
  const l = LABEL[state.conn] ?? LABEL.idle!;
  const cls = state.wsStale && state.conn === "open" ? "warn" : l.cls;
  return (
    <span className={`conn-badge chip ${cls}`} data-testid="conn-badge">
      <span className="conn-dot" aria-hidden="true" />
      {state.wsStale && state.conn === "open" ? t("connect.stale") : l.bn}
    </span>
  );
}
