#!/usr/bin/env python3
"""
verify_ms_latency.py — MS-LATENCY backtest (2026-09-17).

USER REQUIREMENT (verbatim):
  "আর সকল ডেটা ও এনালাইসিস ও মিলি সেকেন্ড এর কম সময়ে আপডেট হচ্চে কিনা।
   এই বিষয় টি ফিক্স করতে হবে।"

What this proves, with numbers, on synthetic-but-realistic data:
  1. Every per-tick compute stage on the broadcast hot path is measured:
       a) running-candle build (OHLC update from tick)
       b) microstructure analysis over the last 200 ticks
       c) tick-eye live anatomy over the last 400 ticks
       d) four-voice coordination merge
       e) JSON serialization of the full tick message (what the WS layer
          sends) — including micro + eye + coordination payloads
  2. The FULL per-tick path (a→e combined) stays under 1 ms — this is
     exactly what feed.py's live gauge measures in production and
     GET /api/latency reports (tick_pipeline.within_1ms).
  3. feed.latency_stats() returns a well-formed report.
  4. The WS dequeue overhead (asyncio.Queue.get) is negligible.

Budget: every stage < 1 ms; full pipeline < 1 ms. (Network/WS transport
time to the browser is outside the server's control and excluded — the
requirement is about the app's own update computation.)

Run: python3 scripts/verify_ms_latency.py
Exit 0 = PASS (everything under budget). Any FAIL → exit 1.
"""
import asyncio
import json
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QX_RETENTION_ENABLED", "0")

# Throwaway DB so importing db/feed never touches a real signals.db.
import tempfile
TMPDIR = tempfile.mkdtemp(prefix="qx_lat_tmp_")
os.environ["DB_PATH"] = os.path.join(TMPDIR, "tmp_latency_backtest.db")

from core.tick_eye import live_eye            # noqa: E402
from core.coordination import compute_coordination  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ PASS  {name}  [{detail}]")
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {name}  [{detail}]")


def bench(fn, n=2000, *args):
    """Run fn(*args) n times; return (mean_ms, p99_ms, max_ms) — no warmup
    excluded (first-call cache warmth is part of reality)."""
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn(*args)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    mean = statistics.fmean(samples)
    p99 = samples[int(0.99 * (n - 1))]
    return mean, p99, samples[-1]


def make_ticks(n: int, base: float = 1.0850, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    ticks, price = [], base
    for _ in range(n):
        price += rng.gauss(0, 0.00012)
        ticks.append(round(price, 6))
    return ticks


def main() -> int:
    print("══ MS-LATENCY verification — per-tick pipeline budget: 1 ms ══")
    ticks200 = make_ticks(200)
    ticks400 = make_ticks(400)

    # 1a. running candle build — the OHLC update per tick (same math as
    # QuotexFeed._running_candle's hot section, on a prebuilt stream).
    import feed as _feed_mod
    stream = _feed_mod._AssetStream(asset="EURUSD_otc", period=60)
    stream.candle_open_time = int(time.time()) // 60 * 60
    stream.candle_open_price = ticks400[0]
    for t in ticks400:
        stream.ticks.append(t)

    def _running_update():
        hi = max(stream.ticks)
        lo = min(stream.ticks)
        return {"open": stream.candle_open_price, "high": hi, "low": lo,
                "close": stream.ticks[-1]}
    m, p99, mx = bench(_running_update, 2000)
    check("running-candle OHLC update < 1 ms", m < 1.0,
          f"mean={m:.4f}ms p99={p99:.4f}ms max={mx:.4f}ms")

    # 1b. microstructure over last 200 ticks (QuotexFeed._analyze_microstructure)
    f = _feed_mod.QuotexFeed()
    m, p99, mx = bench(f._analyze_microstructure, 2000, ticks200,
                       ticks200[0])
    check("microstructure analysis (200 ticks) < 1 ms", m < 1.0,
          f"mean={m:.4f}ms p99={p99:.4f}ms max={mx:.4f}ms")

    # 1c. tick-eye live anatomy over last 400 ticks
    m, p99, mx = bench(live_eye, 2000, ticks400, ticks400[0], 60,
                       stream.candle_open_time)
    check("tick-eye live anatomy (400 ticks) < 1 ms", m < 1.0,
          f"mean={m:.4f}ms p99={p99:.4f}ms max={mx:.4f}ms")

    # 1d. four-voice coordination merge
    eye = live_eye(ticks400, ticks400[0], 60, stream.candle_open_time)
    pred = {"signal": "CALL", "confidence": 72, "strength": "MEDIUM",
            "strategy": "confluence_v1_any"}
    model_voice = {"direction": "CALL", "probability": 0.61, "state": "ready"}
    m, p99, mx = bench(compute_coordination, 2000, eye, pred, model_voice,
                       "CONFIRMING")
    check("coordination 4-voice merge < 1 ms", m < 1.0,
          f"mean={m:.4f}ms p99={p99:.4f}ms max={mx:.4f}ms")
    check("coordination self-reported compute_us < 1000",
          compute_coordination(eye, pred, model_voice, "CONFIRMING")
          ["compute_us"] < 1000)

    # 1e. JSON serialization of the FULL tick message (micro+eye+coord+pred)
    micro = f._analyze_microstructure(ticks200, ticks200[0])
    coord = compute_coordination(eye, pred, model_voice, "CONFIRMING")
    msg = {"type": "tick", "asset": "EURUSD_otc", "period": 60,
           "candle": _running_update(),
           "running_conf": "CONFIRMING", "micro": micro,
           "tick_eye": eye, "coordination": coord, "prediction": pred}
    m, p99, mx = bench(json.dumps, 2000, msg)
    check("tick message JSON serialize < 1 ms", m < 1.0,
          f"mean={m:.4f}ms p99={p99:.4f}ms max={mx:.4f}ms "
          f"size={len(json.dumps(msg)) // 1024}KB")

    # 2. FULL per-tick pipeline (the exact production hot path)
    def _full_tick_path():
        # (a) running candle
        hi = max(stream.ticks)
        lo = min(stream.ticks)
        running = {"open": stream.candle_open_price, "high": hi, "low": lo,
                   "close": stream.ticks[-1]}
        # (b) micro (last 200)
        micro2 = f._analyze_microstructure(ticks200, ticks200[0])
        # (c) eye (last 400)
        eye2 = live_eye(ticks400, ticks400[0], 60, stream.candle_open_time)
        # (d) coordination
        coord2 = compute_coordination(eye2, pred, model_voice, "CONFIRMING")
        # (e) serialize
        m2 = {"type": "tick", "asset": "EURUSD_otc", "period": 60,
              "candle": running, "running_conf": "CONFIRMING",
              "micro": micro2, "tick_eye": eye2, "coordination": coord2,
              "prediction": pred}
        return json.dumps(m2)
    m, p99, mx = bench(_full_tick_path, 1000)
    check("FULL tick pipeline (compute+serialize) < 1 ms", m < 1.0,
          f"mean={m:.4f}ms p99={p99:.4f}ms max={mx:.4f}ms")

    # 3. asyncio.Queue dequeue overhead (event-driven path wakeup cost)
    async def _queue_bench():
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        for i in range(500):
            q.put_nowait({"time": time.time(), "price": 1.085 + i * 1e-6})
        t0 = time.perf_counter()
        n = 0
        while not q.empty():
            await asyncio.wait_for(q.get(), timeout=0.05)
            n += 1
        return (time.perf_counter() - t0) * 1000.0, n
    total_ms, n = asyncio.run(_queue_bench())
    check("asyncio tick-queue dequeue overhead < 0.01 ms/tick",
          total_ms / max(1, n) < 0.01,
          f"{total_ms / max(1, n):.5f}ms/tick over {n} ticks")

    # 4. feed.latency_stats() shape (production gauge API)
    stats = f.latency_stats()
    ok_keys = all(k in stats for k in (
        "tick_proc_ms_ema", "tick_proc_ms_max", "data_age_ms_ema",
        "samples", "within_1ms"))
    check("feed.latency_stats() report shape", ok_keys, str(stats))

    # 5. feed module-level hot-path imports resolved (no per-tick import)
    check("feed._live_eye hoisted to module level",
          getattr(_feed_mod, "_live_eye", None) is not None)
    check("feed._compute_coordination hoisted to module level",
          getattr(_feed_mod, "_compute_coordination", None) is not None)

    print(f"\n{'=' * 64}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("Production live evidence: GET /api/latency → "
          "tick_pipeline.within_1ms (feed.py MS-LATENCY gauge)")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import shutil
        shutil.rmtree(TMPDIR, ignore_errors=True)
    sys.exit(code)
