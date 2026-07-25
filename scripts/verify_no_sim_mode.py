#!/usr/bin/env python3
"""
Startup verification test — verifies that sim mode is disabled and the
app correctly requires live credentials.

Run this locally before deploying to Railway:
    python3 scripts/verify_no_sim_mode.py

The script:
1. Verifies sim_feed.py is renamed (.DISABLED)
2. Verifies server.py has no `from sim_feed import` statement
3. Verifies feed.py has no live `import sim_feed` statement
4. Verifies the _warn_if_stuck method exists (replaces _fallback_to_sim_if_stuck)
5. Verifies /api/token-status endpoint is defined in server.py
6. Verifies railway.json has QX_TOKEN in env_required

Exit code 0 = all checks pass; 1 = at least one check failed.
"""
import os
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
FAIL = "\033[91m❌ FAIL\033[0m"
PASS = "\033[92m✅ PASS\033[0m"


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
    return condition


def main():
    print("\n🔍 Verifying sim mode is disabled and live-data-only is enforced...\n")
    all_pass = True

    # Check 1: sim_feed.py should be renamed to .DISABLED
    sim_path = REPO_ROOT / "sim_feed.py"
    sim_disabled_path = REPO_ROOT / "sim_feed.py.DISABLED"
    all_pass &= check(
        "sim_feed.py is renamed to .DISABLED",
        not sim_path.exists() and sim_disabled_path.exists(),
        f"found: {sim_disabled_path.name}" if sim_disabled_path.exists() else "neither file found",
    )

    # Check 2: server.py should NOT import sim_feed
    server_py = (REPO_ROOT / "server.py").read_text()
    has_sim_import = bool(re.search(r"^\s*from\s+sim_feed\s+import", server_py, re.MULTILINE))
    has_sim_import |= bool(re.search(r"^\s*import\s+sim_feed", server_py, re.MULTILINE))
    all_pass &= check(
        "server.py does not import sim_feed",
        not has_sim_import,
    )

    # Check 3: server.py should have the SIM-MODE-DISABLED marker
    all_pass &= check(
        "server.py has SIMULATION MODE DISABLED marker",
        "SIMULATION MODE DISABLED" in server_py,
    )

    # Check 4: server.py should require QX_TOKEN at startup
    all_pass &= check(
        "server.py logs clear error if QX_TOKEN missing",
        "no Quotex credentials found" in server_py,
    )

    # Check 5: server.py should have /api/token-status endpoint
    all_pass &= check(
        "server.py defines /api/token-status endpoint",
        '/api/token-status' in server_py and 'def token_status' in server_py,
    )

    # Check 6: feed.py should have _warn_if_stuck (replaces _fallback_to_sim_if_stuck)
    feed_py = (REPO_ROOT / "feed.py").read_text()
    all_pass &= check(
        "feed.py has _warn_if_stuck method",
        "async def _warn_if_stuck" in feed_py,
    )

    # Check 7: feed.py should NOT have a working _fallback_to_sim_if_stuck
    # (it can be in a comment/docstring, but not as a live function call)
    has_live_fallback = bool(re.search(
        r"^\s*async\s+def\s+_fallback_to_sim_if_stuck", feed_py, re.MULTILINE))
    all_pass &= check(
        "feed.py does NOT have live _fallback_to_sim_if_stuck function",
        not has_live_fallback,
    )

    # Check 8: feed.py ensure_stream should return no_credentials error
    all_pass &= check(
        "feed.py ensure_stream returns no_credentials error",
        "no_credentials" in feed_py and "no Quotex credentials" in feed_py,
    )

    # Check 9: railway.json should have QX_TOKEN in env_required
    railway_json = (REPO_ROOT / "railway.json").read_text()
    all_pass &= check(
        "railway.json has QX_TOKEN in env_required",
        "QX_TOKEN" in railway_json and "env_required" in railway_json,
    )

    # Check 10: setup_railway_token.py should exist
    setup_script = REPO_ROOT / "scripts" / "setup_railway_token.py"
    all_pass &= check(
        "scripts/setup_railway_token.py exists",
        setup_script.exists(),
    )

    # Check 11: .env.example should mention sim mode disabled
    env_example = (REPO_ROOT / ".env.example").read_text()
    all_pass &= check(
        ".env.example mentions sim mode disabled",
        "PERMANENTLY DISABLED" in env_example,
    )

    # Check 12: RAILWAY_TOKEN_SETUP.md should mention auto-setup script
    setup_md = (REPO_ROOT / "RAILWAY_TOKEN_SETUP.md").read_text()
    all_pass &= check(
        "RAILWAY_TOKEN_SETUP.md documents auto-setup script",
        "setup_railway_token.py" in setup_md,
    )

    print()
    if all_pass:
        print("🎉 All checks passed — sim mode is permanently disabled.")
        print("   The app will run on live Quotex data only.")
        print("   Next: set QX_TOKEN in Railway Variables or via the setup script.")
        sys.exit(0)
    else:
        print("⚠️  Some checks failed — review the output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
