/**
 * CandleChart — the Terminal's primary visual. Wraps TradingView
 * Lightweight Charts v4.1.3 (npm — the SAME version as the legacy
 * vendored copy, guaranteeing feature parity with the old UI).
 *
 * Marker honesty (directive Phase 2 / Phase 7):
 *   • CALL/PUT markers are drawn ONLY for eligible actionable signals,
 *     at the signal's OWN target candle time — never at the current
 *     candle, never at a guessed price.
 *   • Everything research-only renders as a neutral dot with the
 *     reason code in the marker title, so the chart itself can never
 *     be screenshotted as an actionable CALL/PUT that never existed.
 */

import { useEffect, useRef } from "react";
import {
  createChart, ColorType, CrosshairMode, type IChartApi,
  type ISeriesApi, type CandlestickData, Time,
} from "lightweight-charts";
import type { Candle, Prediction } from "../../services/websocket/protocol";
import { toSignalView } from "../../services/signals/states";

export interface CandleChartProps {
  candles: Candle[];
  prediction: Prediction | null;
  researchMode: boolean;
}

export function CandleChart({ candles, prediction, researchMode }: CandleChartProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  // create once
  useEffect(() => {
    if (!boxRef.current) return;
    const chart = createChart(boxRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "var(--ink-3)",
        fontFamily: "var(--font-ui, Inter, system-ui, sans-serif)",
      },
      grid: {
        vertLines: { color: "var(--bg-2)" },
        horzLines: { color: "var(--bg-2)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "var(--line-1)" },
      timeScale: { borderColor: "var(--line-1)", timeVisible: true, secondsVisible: false },
    });
    const series = chart.addCandlestickSeries({
      upColor: "var(--pos)", downColor: "var(--neg)",
      wickUpColor: "var(--pos)", wickDownColor: "var(--neg)",
      borderVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;

    const onResize = () => {
      if (!boxRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({
        width: boxRef.current.clientWidth,
        height: boxRef.current.clientHeight,
      });
    };
    onResize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(onResize);
    if (boxRef.current) ro.observe(boxRef.current);

    return () => {
      window.removeEventListener("resize", onResize);
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // data updates
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !candles.length) return;
    series.setData(candles.map(toBar));
  }, [candles]);

  // signal markers — derived from the truth-layer view, never raw
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    const view = toSignalView(prediction);

    // Only an ELIGIBLE actionable signal may place a CALL/PUT marker, at
    // the contract's target candle time. Everything else → neutral dot.
    if (!view.isActionable || !prediction?.target_candle_open_epoch) {
      series.setMarkers([]);
      return;
    }
    const target = prediction.target_candle_open_epoch as Time;
    series.setMarkers([
      {
        time: target,
        position: view.direction === "CALL" ? "belowBar" : "aboveBar",
        color: view.direction === "CALL" ? "var(--pos)" : "var(--neg)",
        shape: view.direction === "CALL" ? "arrowUp" : "arrowDown",
        text: `${view.direction}${researchMode ? " (RESEARCH)" : ""}`,
      },
    ]);
  }, [prediction, researchMode]);

  return <div ref={boxRef} className="chart-box" data-testid="candle-chart" />;
}

function toBar(c: Candle): CandlestickData {
  return { time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close };
}
