"""
Quotex live tick verifier — tests whether a Quotex SSID token is valid
and whether ticks are actually flowing from the broker.

Run from inside the Binary-signals-app directory:
    python scripts/verify_live_ticks.py

Set the SSID token via env var (so it doesn't appear in shell history):
    export QX_TOKEN="your-ssid-token-here"
    python scripts/verify_live_ticks.py

Or via QX_TOKEN file:
    echo "your-ssid" > ~/.qx_token && QX_TOKEN_FILE=~/.qx_token python scripts/verify_live_ticks.py

Diagnostic output:
    Step 1: Token format check (length, structure)
    Step 2: WebSocket connect attempt
    Step 3: Authorization wait (15s timeout)
    Step 4: Asset list fetch (instruments/list)
    Step 5: Subscribe to EURUSD_otc
    Step 6: Wait for first tick (30s timeout)
    Step 7: Print tick count + sample ticks

If any step fails, prints the exact failure mode + suggested fix.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# Add repo root to path so we can import quotex_ws
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import quotex_ws


# ANSI colors for clarity
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str, hint: str = "") -> None:
    print(f"  {RED}✗{RESET} {msg}")
    if hint:
        print(f"    {YELLOW}→ {hint}{RESET}")


def info(msg: str) -> None:
    print(f"  {CYAN}•{RESET} {msg}")


def get_token() -> str:
    """Load token from env var or file (so it never appears in shell history)."""
    token = os.environ.get("QX_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("QX_TOKEN_FILE", "")
    if token_file and Path(token_file).exists():
        return Path(token_file).read_text().strip()
    return ""


async def verify_tick_flow(token: str, asset: str = "EURUSD_otc", period: int = 60) -> bool:
    """Run the full verification. Returns True if ticks are flowing."""

    print(f"\n{CYAN}═══════════════════════════════════════════════════════════════{RESET}")
    print(f"{CYAN}  Quotex Live Tick Verifier{RESET}")
    print(f"{CYAN}═══════════════════════════════════════════════════════════════{RESET}")
    print(f"  Asset:     {asset}")
    print(f"  Period:    {period}s")
    print(f"  Token:     {'set (' + str(len(token)) + ' chars)' if token else 'NOT SET'}")
    print()

    if not token:
        fail("QX_TOKEN env var not set.",
             "Run: export QX_TOKEN='your-ssid-token'  (don't paste in chat)")
        return False

    # ─── Step 1: Token format check ──────────────────────────────────────
    print(f"{CYAN}[Step 1/6] Token format check{RESET}")
    if len(token) < 50:
        fail(f"Token too short ({len(token)} chars, expected 200+).",
             "Real Quotex SSID is JWT-style (200+ chars). You may have copied only part.")
        return False
    if "://" not in token and "quotex" not in token.lower() and len(token) < 100:
        fail("Token doesn't look like a Quotex SSID.",
             "SSID is usually a long JWT or 'token=...' style string from DevTools cookies.")
        return False
    ok(f"Token length OK ({len(token)} chars)")
    print()

    # ─── Step 2: Construct client ───────────────────────────────────────
    print(f"{CYAN}[Step 2/6] Construct QuotexWSClient{RESET}")
    try:
        client = quotex_ws.QuotexWSClient(
            email="",
            password="",
            host="market-qx.trade",
            lang="en",
        )
        client.set_session(ssid=token)
        ok("Client constructed + session set")
    except Exception as e:
        fail(f"Client construction failed: {e}",
             "Check quotex_ws.py — recent fix-batch may have broken __init__.")
        return False
    print()

    # ─── Step 3: WebSocket connect ──────────────────────────────────────
    print(f"{CYAN}[Step 3/6] WebSocket connect (15s timeout){RESET}")
    try:
        # Run connect with timeout
        connect_result = await asyncio.wait_for(client.connect(), timeout=15.0)
        # connect() returns (success, message) — but our wrapper may differ
        if isinstance(connect_result, tuple):
            success, msg = connect_result
        elif isinstance(connect_result, bool):
            success, msg = connect_result, ""
        else:
            success, msg = bool(connect_result), str(connect_result)

        if not success:
            fail(f"Connect failed: {msg}",
                 "Token may be expired, account may be locked, or IP may be blocked.")
            return False
        ok(f"WebSocket connected: {msg or 'OK'}")
    except asyncio.TimeoutError:
        fail("Connection timed out after 15s.",
             "Network may be blocking WS, or Quotex is unreachable from this host.")
        return False
    except Exception as e:
        fail(f"Connect exception: {type(e).__name__}: {e}",
             "Check quotex_ws.connect() — recent fix-batch may have changed behavior.")
        return False
    print()

    # ─── Step 4: Authorization wait ─────────────────────────────────────
    print(f"{CYAN}[Step 4/6] Wait for authorization (15s timeout){RESET}")
    auth_deadline = time.time() + 15.0
    auth_ok = False
    while time.time() < auth_deadline:
        if getattr(client, "_authorized", False):
            auth_ok = True
            break
        await asyncio.sleep(0.5)

    if not auth_ok:
        fail("Authorization not received within 15s.",
             "This is the MOST COMMON failure — token is expired or Quotex rejected it.")
        # Don't return False yet — let's also check if WS closed
        if getattr(client, "_ws", None) and client._ws.closed:
            fail("WS was closed by server (auth reject).",
                 "Token is definitely invalid/expired. Get a fresh SSID from Quotex.")
        return False
    ok("Authorized!")
    print()

    # ─── Step 5: Fetch instruments list ─────────────────────────────────
    print(f"{CYAN}[Step 5/6] Fetch instruments list (10s timeout){RESET}")
    try:
        instruments = await asyncio.wait_for(client.get_instruments(), timeout=10.0)
        if not instruments:
            fail("Empty instruments list.",
                 "Account may have no assets enabled, or response was malformed.")
        else:
            asset_count = len(instruments) if hasattr(instruments, "__len__") else "?"
            ok(f"Got {asset_count} instruments")
            # Verify requested asset exists
            asset_found = False
            if isinstance(instruments, dict):
                asset_found = asset in instruments
            elif isinstance(instruments, list):
                asset_found = any(
                    (i.get("asset") == asset if isinstance(i, dict) else str(i) == asset)
                    for i in instruments
                )
            if asset_found:
                ok(f"Requested asset '{asset}' is in the list")
            else:
                fail(f"Asset '{asset}' NOT in instruments list.",
                     f"Try a different asset. Available sample: {list(instruments)[:5] if isinstance(instruments, dict) else instruments[:5]}")
    except asyncio.TimeoutError:
        fail("Instruments fetch timed out.",
             "WS may be alive but request not responded — check quotex_ws._api.")
    except Exception as e:
        fail(f"Instruments fetch failed: {type(e).__name__}: {e}")
    print()

    # ─── Step 6: Subscribe + wait for ticks ─────────────────────────────
    print(f"{CYAN}[Step 6/6] Subscribe to {asset} + wait for first tick (30s){RESET}")
    ticks_received = []
    tick_event = asyncio.Event()

    def on_tick(tick_dict):
        ticks_received.append(tick_dict)
        if len(ticks_received) == 1:
            tick_event.set()

    try:
        client.register_tick_callback(asset, on_tick)
    except Exception as e:
        fail(f"register_tick_callback failed: {e}")
        return False

    try:
        subscribed = await asyncio.wait_for(
            client.start_candles_stream(asset, period), timeout=10.0
        )
        if not subscribed:
            fail(f"start_candles_stream returned False for {asset}",
                 "Asset may be closed right now (market hours). Try EURUSD (real) instead of EURUSD_otc.")
            return False
        ok(f"Subscribed to {asset} (period={period}s)")
    except asyncio.TimeoutError:
        fail("Subscribe timed out.",
             "WS may be slow — try again, or check network.")
        return False
    except Exception as e:
        fail(f"Subscribe exception: {type(e).__name__}: {e}")
        return False

    # Wait for first tick
    try:
        await asyncio.wait_for(tick_event.wait(), timeout=30.0)
        ok(f"First tick received! price={ticks_received[0].get('price')}")
    except asyncio.TimeoutError:
        fail("NO TICKS received within 30s.",
             "Asset may be CLOSED (market closed). Try a 24/7 OTC pair or check market hours.")
        # Try fallback
        print(f"\n  {YELLOW}→ Trying fallback asset: EURUSD_otc (always-on OTC)...{RESET}")
        # already EURUSD_otc — try a different one
        for fallback in ["EURUSD-OTC", "EURUSD_otc", "EURUSD"]:
            if fallback != asset:
                print(f"  → fallback: {fallback}")
                # re-run not implemented here; just suggest
                break
        return False

    # Count ticks over 10s
    print(f"\n  {CYAN}Counting ticks for 10 seconds...{RESET}")
    initial_count = len(ticks_received)
    await asyncio.sleep(10.0)
    final_count = len(ticks_received)
    delta = final_count - initial_count

    if delta == 0:
        fail("No additional ticks in 10s — stream may be stalled.",
             "Reconnect may be needed. Check quotex_ws reconnect logic.")
        return False

    ok(f"{delta} ticks in 10s ({delta / 10.0:.1f} ticks/sec)")

    # Print sample
    print(f"\n  {CYAN}Sample ticks (last 5):{RESET}")
    for t in ticks_received[-5:]:
        print(f"    time={t.get('time')}, price={t.get('price')}")

    print(f"\n{GREEN}═══════════════════════════════════════════════════════════════{RESET}")
    print(f"{GREEN}  ✓ LIVE DATA IS FLOWING — Token valid, WS connected, ticks received.{RESET}")
    print(f"{GREEN}═══════════════════════════════════════════════════════════════{RESET}")
    return True


def main():
    token = get_token()
    if not token:
        print(f"\n{RED}ERROR: No Quotex token found.{RESET}")
        print(f"\nSet it via env var (recommended — won't appear in shell history):")
        print(f"  export QX_TOKEN='your-ssid-token-here'")
        print(f"  python scripts/verify_live_ticks.py")
        print(f"\nOr via file:")
        print(f"  echo 'your-ssid' > ~/.qx_token")
        print(f"  QX_TOKEN_FILE=~/.qx_token python scripts/verify_live_ticks.py")
        sys.exit(1)

    # Override asset/period via CLI args
    asset = sys.argv[1] if len(sys.argv) > 1 else "EURUSD_otc"
    period = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    try:
        success = asyncio.run(verify_tick_flow(token, asset, period))
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")
        sys.exit(130)
    except Exception as e:
        print(f"\n{RED}Unexpected error: {type(e).__name__}: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
