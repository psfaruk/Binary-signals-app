#!/bin/bash
# Binary Signals App — startup script
# ব্যবহার: ./run.sh

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
# FIX (BUG-5, 2026-07-18): .env.example now exists with empty fields, so
# we check if QX_TOKEN / QX_EMAIL / QX_PASSWORD are still empty rather
# than looking for a placeholder email string. Also accept USE_SIM=1 as
# a valid config (no creds needed in sim mode).
if ! grep -qE "^(QX_TOKEN|QX_EMAIL|QX_PASSWORD)=.+" .env && \
   ! grep -qE "^USE_SIM=1" .env; then
    echo ""
    echo "❌ .env ফাইলে কোনো Quotex credentials নেই!"
    echo ""
    echo "   .env ফাইল edit করুন এবং আপনার Quotex email + password দিন:"
    echo "   nano .env"
    echo ""
    echo "   অথবা যদি আপনার কাছে session token থাকে (browser থেকে কপি করা):"
    echo "   QX_TOKEN=abc123... লাইনটি uncomment করে token দিন"
    echo ""
    echo "   অথবা simulation mode এ চালাতে (credentials ছাড়া):"
    echo "   USE_SIM=1 লাইনটি uncomment করুন"
    exit 1
fi

# ── Python dependencies চেক ─────────────────────────────────────────────────
echo ""
echo "📦 Python dependencies চেক হচ্ছে..."
python3 -c "import fastapi, uvicorn, websockets" 2>/dev/null || {
    echo "❌ কিছু dependency missing। ইনস্টল করা হচ্ছে..."
    pip3 install -r requirements.txt
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
