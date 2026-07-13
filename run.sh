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
if grep -q "your_email@example.com" .env; then
    echo ""
    echo "❌ .env ফাইলে এখনও ডিফল্ট email আছে!"
    echo ""
    echo "   .env ফাইল edit করুন এবং আপনার Quotex email + password দিন:"
    echo "   nano .env"
    echo ""
    echo "   অথবা যদি আপনার কাছে session token থাকে (browser থেকে কপি করা):"
    echo "   QX_TOKEN=abc123... লাইনটি uncomment করে token দিন"
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
