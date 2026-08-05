#!/usr/bin/env python3
"""Fetch real 60s candle history from Quotex for every pair, save to JSON.

Uses the SAME backend the production app uses (vendored pyquotex, Firefox TLS).

    python fetch_history.py <TOKEN> <OUTDIR> [hours]
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")
PAIRS_URL = "https://binary-signals-app-production.up.railway.app/api/pairs"


def log(*a):
    print(*a, flush=True)


def load_pairs():
    import urllib.request
    with urllib.request.urlopen(PAIRS_URL, timeout=20) as r:
        d = json.loads(r.read().decode())
    return [(p["asset"], p["category"])
            for p in d.get("real_pairs", []) + d.get("otc_pairs", [])]


def make_client(root):
    from pyquotex.stable_api import Quotex
    from pyquotex.types import ReconnectPolicy
    from pyquotex.network.login import Login
    host = "market-qx.trade"
    Login.base_url = host
    Login.https_base_url = f"https://{host}"
    return Quotex(email="", password="", host=host, lang="en",
                  root_path=root,
                  reconnect_policy=ReconnectPolicy(
                      enabled=True, max_attempts=0, base_delay=2.0,
                      max_delay=30.0, stale_timeout=45.0))


def norm(raw):
    out = {}
    for c in raw or []:
        try:
            if isinstance(c, dict):
                t = c.get("time", c.get("from"))
                if t is None:
                    continue
                out[int(t)] = {"time": int(t), "open": float(c["open"]),
                               "high": float(c["high"]), "low": float(c["low"]),
                               "close": float(c["close"])}
            elif isinstance(c, (list, tuple)) and len(c) >= 5 and c[0]:
                out[int(c[0])] = {"time": int(c[0]), "open": float(c[1]),
                                  "high": float(c[2]), "low": float(c[3]),
                                  "close": float(c[4])}
        except (TypeError, ValueError, KeyError):
            continue
    return out


async def fetch_pair(client, asset, hours, period=60):
    """Use pyquotex's chunked historical fetcher (offset is in SECONDS and the
    raw rows are [time, open, close, high, low] — their parser handles it)."""
    try:
        raw = await asyncio.wait_for(
            client.get_historical_candles(
                asset, amount_of_seconds=hours * 3600,
                period=period, max_workers=4),
            timeout=180)
    except Exception as e:
        log(f"    [{asset}] error: {type(e).__name__}: {e}")
        return []
    got = {}
    for c in raw or []:
        try:
            t = int(c["time"])
            got[t] = {"time": t, "open": float(c["open"]),
                      "high": float(c["high"]), "low": float(c["low"]),
                      "close": float(c["close"])}
        except (TypeError, ValueError, KeyError):
            continue
    return [got[t] for t in sorted(got)]


async def main():
    token, outdir = sys.argv[1], sys.argv[2]
    hours = int(sys.argv[3]) if len(sys.argv) > 3 else 24
    os.makedirs(outdir, exist_ok=True)

    client = make_client(os.getcwd())
    client.set_session(user_agent=UA, ssid=token)
    ok, reason = await asyncio.wait_for(client.connect(), timeout=60)
    log(f"connect ok={ok} reason={reason}")
    if not ok:
        sys.exit(2)

    pairs = load_pairs()
    log(f"{len(pairs)} pairs, target {hours}h of 60s candles each")
    summary = {}
    try:
        for asset, cat in pairs:
            t0 = time.time()
            cs = await fetch_pair(client, asset, hours)
            summary[asset] = {"category": cat, "candles": len(cs),
                              "first": cs[0]["time"] if cs else None,
                              "last": cs[-1]["time"] if cs else None}
            with open(os.path.join(outdir, f"{asset}.json"), "w") as f:
                json.dump({"asset": asset, "category": cat, "period": 60,
                           "candles": cs}, f)
            log(f"  {asset:14s} {cat:4s} {len(cs):6d} candles "
                f"({time.time() - t0:.1f}s)")
    finally:
        with open(os.path.join(outdir, "_summary.json"), "w") as f:
            json.dump(summary, f, indent=1)
        try:
            await client.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
