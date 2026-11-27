/** market controls: market switch, pair select, price ticker, countdown */

import { useApp } from "../../stores/AppStore";
import { t } from "../../i18n/dict";
import { pairLabel } from "../../services/websocket/protocol";
import "./market.css";

export function MarketSwitch() {
  const { state, select } = useApp();
  return (
    <div className="mkt-switch" role="tablist" aria-label={t("market")}>
      {(["otc", "real"] as const).map((m) => (
        <button key={m} type="button" role="tab" aria-selected={state.market === m}
          className={`mkt-btn ${state.market === m ? "on" : ""}`}
          onClick={() => {
            if (m === state.market) return;
            const pairs = m === "otc" ? state.pairsOtc : state.pairsReal;
            const first = pairs[0]?.asset ?? (m === "otc" ? "EURUSD_otc" : "BTCUSDT");
            select(m, first, state.period);
          }}>
          {m === "otc" ? t("market.otc") : t("market.real")}
        </button>
      ))}
    </div>
  );
}

export function PairSelect() {
  const { state, select } = useApp();
  const pairs = state.market === "otc" ? state.pairsOtc : state.pairsReal;
  const list = pairs.length ? pairs : [{ asset: state.asset }];
  return (
    <label className="pair-select">
      <span className="visually-hidden">{t("history.pair")}</span>
      <select value={state.asset}
        onChange={(e) => select(state.market, e.target.value, state.period)}>
        {list.map((p) => <option key={p.asset} value={p.asset}>{pairLabel(p)}</option>)}
      </select>
    </label>
  );
}

export function PriceTicker() {
  const { state } = useApp();
  const v = state.lastPrice;
  return (
    <div className="price-ticker" data-testid="price-ticker">
      <span className="price-label">{t("price")}</span>
      <span className="price-value mono">
        {v === null ? "—" : formatPrice(v)}
      </span>
    </div>
  );
}

export function Countdown() {
  const { state } = useApp();
  const { period } = state;
  return <CountdownInner openAt={state.candleOpenAt} periodSec={period} />;
}

function CountdownInner({ openAt, periodSec }: { openAt: number | null; periodSec: number }) {
  const [, force] = useTick(250);
  void force;
  const left = openAt === null ? null :
    Math.max(0, Math.ceil(openAt + periodSec - Date.now() / 1000));
  return (
    <div className="candle-countdown" data-testid="countdown">
      <span className="cd-label">{t("countdown")}</span>
      <span className={`cd-value mono ${left !== null && left <= 5 ? "ending" : ""}`}>
        {left === null ? "—" : `${left}s`}
      </span>
    </div>
  );
}

import { useEffect, useState } from "react";
function useTick(ms: number): [number, () => void] {
  const [n, setN] = useState(0);
  useEffect(() => { const id = setInterval(() => setN((x) => x + 1), ms); return () => clearInterval(id); }, [ms]);
  return [n, () => setN((x) => x + 1)];
}

export function formatPrice(v: number): string {
  const s = v >= 1000 ? v.toFixed(2) : v >= 10 ? v.toFixed(3) : v.toFixed(5);
  return s;
}
