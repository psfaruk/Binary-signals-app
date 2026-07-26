#!/bin/bash
# Binary Signals App — startup script
# ব্যবহার: ./run.sh
#
# FIX (DEEP-AUDIT-2026-07-26 / F-19-41): removed USE_SIM=1 acceptance —
#   sim mode is permanently disabled (A-10 PROBLEM 28). Now requires
#   QX_TOKEN or QX_EMAIL+QX_PASSWORD.
# FIX (DEEP-AUDIT-2026-07-26 / F-19-42): broadened dependency check from
#   3 imports (fastapi, uvicorn, websockets) to all 8 required packages
#   (A-10 PROBLEM 29).
# FIX (DEEP-AUDIT-2026-07-26 / F-19-43): use `python3 -m pip` instead of
#   bare `pip3` (A-10 PROBLEM 30).

set -e
cd "$(dirname "$0")"

echo "═══════════════════════════════════════════════════════════════"
echo "  Binary Signals App — শুরু হচ্ছে"
echo "═══════════════════════════════════════════════════════════════"

# ── .env ফাইল চেক ─────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo ""
    echo "❌ .env ফাইল নেই!"
    echo ""
    echo "📄 .env.example থেকে .env তৈরি করুন:"
    echo "   cp .env.example .env"
    echo "   তারপর .env ফাইলে আপনার Quotex email + password দিন"
    echo ""
    echo "এখন .env তৈরি করা হচ্ছে..."
    cp .env.example .env
    echo ""
    echo "✅ .env তৈরি হয়েছে। এখন ফাইলটি edit করে আপনার Quotex email + password দিন:"
    echo "   nano .env"
    echo "   অথবা যেকোনো text editor দিয়ে"
    echo ""
    echo "তারপর আবার এই script চালান: ./run.sh"
    exit 1
fi

# ── Credentials চেক ─────────────────────────────────────────────────────────
# FIX (DEEP-AUDIT-2026-07-26 / F-19-41): USE_SIM=1 is no longer accepted.
# Sim mode was permanently disabled on 2026-07-25 — QX_TOKEN (or
# QX_EMAIL+QX_PASSWORD) is REQUIRED.
if ! grep -qE "^(QX_TOKEN|QX_EMAIL|QX_PASSWORD)=.+" .env; then
    echo ""
    echo "❌ .env ফাইলে কোনো Quotex credentials নেই!"
    echo ""
    echo "   .env ফাইল edit করুন এবং আপনার Quotex email + password দিন:"
    echo "   nano .env"
    echo ""
    echo "   অথবা যদি আপনার কাছে session token থাকে (browser থেকে কপি করা):"
    echo "   QX_TOKEN=abc123... লাইনটি uncomment করে token দিন"
    echo ""
    echo "   ⚠️  USE_SIM=1 আর কাজ করে না — sim mode স্থায়ীভাবে disabled করা হয়েছে।"
    echo "   QX_TOKEN বা QX_EMAIL+QX_PASSWORD দিতেই হবে।"
    exit 1
fi

# ── Python dependencies চেক ─────────────────────────────────────────────────
# FIX (DEEP-AUDIT-2026-07-26 / F-19-42): broadened import check to cover
# all 8 required runtime packages (A-10 PROBLEM 29).
echo ""
echo "📦 Python dependencies চেক হচ্ছে..."
python3 -c "
import fastapi, uvicorn, websockets, httpx, bs4, certifi, fake_useragent, typing_extensions
" 2>/dev/null || {
    echo "❌ কিছু dependency missing। ইনস্টল করা হচ্ছে..."
    # FIX (DEEP-AUDIT-2026-07-26 / F-19-43): use `python3 -m pip` instead of
    # bare `pip3` — `pip3` is deprecated on Debian/Ubuntu with Python 3.11+
    # and may not exist on macOS without explicit install (A-10 PROBLEM 30).
    python3 -m pip install -r requirements.txt
}

# ── সার্ভার শুরু ─────────────────────────────────────────────────────────
echo ""
echo "🚀 সার্ভার শুরু হচ্ছে..."
echo ""
echo "📊 ব্রাউজারে খুলুন: http://localhost:8000"
echo ""
echo "⏹️  বন্ধ করতে: Ctrl+C"
echo ""

python3 server.py
