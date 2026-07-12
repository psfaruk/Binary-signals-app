#!/usr/bin/env python3
"""
Diagnose why candle data might not match the Quotex broker.

Run this on your deployment server to see which feed is actually being
used. If pyquotex is missing, the app silently falls back to sim_feed
(random-walk simulated data) — that's why candles never match.

Usage:
    python check_feed.py
"""
import sys
import os

print("=" * 60)
print("  FEED DIAGNOSTIC")
print("=" * 60)
print()

# 1. Check pyquotex installation
print("[1/4] Checking pyquotex installation...")
try:
    import pyquotex
    from pyquotex.stable_api import Quotex
    print(f"  ✓ pyquotex is installed at: {pyquotex.__file__}")
    print(f"  ✓ Quotex class imports successfully")
    has_pyquotex = True
except ImportError as e:
    print(f"  ✗ pyquotex is NOT installed: {e}")
    print(f"    This is the ROOT CAUSE of candle mismatch.")
    print(f"    Without pyquotex, the app uses sim_feed.py (random-walk")
    print(f"    simulated data) which will NEVER match the real broker.")
    has_pyquotex = False

print()

# 2. Check what feed the server will actually use
print("[2/4] Checking which feed server.py will load...")
use_sim = os.environ.get("USE_SIM") == "1"
if use_sim:
    print(f"  ✗ USE_SIM=1 is set — app is FORCED to use simulated feed")
    print(f"    Unset USE_SIM to allow real feed: unset USE_SIM")
elif not has_pyquotex:
    print(f"  ✗ App will fall back to sim_feed.py (random-walk data)")
    print(f"    This is why candles don't match the broker!")
else:
    print(f"  ✓ Real Quotex feed (feed.py) will be used")

print()

# 3. Check auth credentials
print("[3/4] Checking Quotex auth credentials...")
email = os.environ.get("QX_EMAIL", "").strip()
password = os.environ.get("QX_PASSWORD", "").strip()
token = os.environ.get("QX_TOKEN", "").strip()
ua = os.environ.get("QX_UA", "").strip()

if email and password:
    print(f"  ✓ QX_EMAIL is set ({email[:3]}***@{email.split('@')[-1] if '@' in email else '?'})")
    print(f"  ✓ QX_PASSWORD is set ({'*' * len(password)})")
    print(f"    Will use email/password login flow")
elif token:
    print(f"  ✓ QX_TOKEN is set ({token[:8]}...)")
    print(f"    Will use session token (SSID) login flow")
else:
    print(f"  ✗ No auth credentials found!")
    print(f"    Need ONE of:")
    print(f"      QX_EMAIL + QX_PASSWORD  (login flow, recommended)")
    print(f"      QX_TOKEN                (session token, faster but expires)")
    print(f"    Optional: QX_UA (custom User-Agent)")

print()

# 4. Check critical Quotex API methods
print("[4/4] Checking pyquotex API surface (if installed)...")
if has_pyquotex:
    required = [
        "get_realtime_price",
        "get_instruments",
        "start_candles_stream",
        "stop_candles_stream",
        "set_session",
        "get_payout_by_asset",
        "get_historical_candles",
        "get_candles",
        "connect",
        "close",
    ]
    missing = [m for m in required if not hasattr(Quotex, m)]
    if missing:
        print(f"  ✗ Missing methods: {missing}")
        print(f"    Your pyquotex version is too old or too new.")
        print(f"    Install from: https://github.com/cleitonleonel/pyquotex")
    else:
        print(f"  ✓ All {len(required)} required API methods present")
        print(f"  ✓ feed.py should work correctly with this pyquotex version")

print()
print("=" * 60)
print("  DIAGNOSIS")
print("=" * 60)
if not has_pyquotex:
    print()
    print("  ❌ ROOT CAUSE FOUND: pyquotex is not installed")
    print()
    print("  FIX:")
    print("    pip install \"git+https://github.com/cleitonleonel/pyquotex.git\"")
    print()
    print("  Then restart your server. Candle data should immediately")
    print("  start matching the Quotex broker (after login completes).")
elif use_sim:
    print()
    print("  ❌ ROOT CAUSE FOUND: USE_SIM=1 is forcing simulated feed")
    print()
    print("  FIX:")
    print("    unset USE_SIM")
    print("    # or remove USE_SIM from your .env / deployment config")
elif not (email and password) and not token:
    print()
    print("  ❌ ROOT CAUSE FOUND: No Quotex auth credentials")
    print()
    print("  FIX:")
    print("    export QX_EMAIL='your_email@example.com'")
    print("    export QX_PASSWORD='your_password'")
    print("    # OR")
    print("    export QX_TOKEN='your_ssid_token'")
else:
    print()
    print("  ✓ All checks passed. If candles still don't match, possible causes:")
    print("    1. Slow tick polling — candles close late (timer-close fallback)")
    print("    2. Stale session token — QX_TOKEN may have expired, clear it")
    print("    3. Network issues with qxbroker.com — try market-qx.trade mirror")
    print("    4. Quotex account region — payouts/pairs differ by region")
print()
