"""
First-time setup script — interactive .env creator.

Run: python setup.py
Asks for Quotex email + password, writes them to .env file with proper
formatting (no Notepad .txt suffix issue, no encoding problems).

Also validates that the credentials work BEFORE saving, so the user
doesn't end up with a broken .env file.
"""
import os
import sys
import getpass
from pathlib import Path


ENV_PATH = Path(__file__).parent / ".env"

ENV_TEMPLATE = """\
# ── Quotex Account Credentials ───────────────────────────────────────────────
QX_EMAIL={email}
QX_PASSWORD={password}

# ── Backend Selection ────────────────────────────────────────────────────────
# QX_USE_RAW_WS=1 (DEFAULT, recommended) → raw Socket.IO v3 WebSocket client
QX_USE_RAW_WS=1

# ── Server ───────────────────────────────────────────────────────────────────
PORT=8000

# ── Optional: Session Token (auto-filled by the app after first login) ──────
# The app saves the working ssid here after a successful login, so subsequent
# restarts skip the email/password login flow and connect faster.
# QX_TOKEN=auto_filled_after_first_login
"""


def main():
    print()
    print("═" * 65)
    print("  Binary Signals App — First-time Setup")
    print("═" * 65)
    print()

    # ── Check if .env already exists ──────────────────────────────────────
    if ENV_PATH.exists():
        print("📄 Existing .env file found. Current contents:")
        print("─" * 65)
        # Print contents but mask the password
        try:
            content = ENV_PATH.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("QX_PASSWORD="):
                    print("QX_PASSWORD=******** (hidden)")
                elif line.startswith("QX_TOKEN=") and len(line) > 20:
                    print(f"{line[:25]}... (hidden)")
                else:
                    print(line)
        except Exception as e:
            print(f"  (could not read: {e})")
        print("─" * 65)
        print()
        ans = input("Re-create .env with new credentials? (y/N): ").strip().lower()
        if ans != "y":
            print("Setup cancelled. Existing .env kept.")
            return
        print()

    # ── Ask for credentials ───────────────────────────────────────────────
    print("📝 আপনার Quotex অ্যাকাউন্টের তথ্য দিন:")
    print()
    email = input("  Email: ").strip()
    if not email:
        print("❌ Email দিতে হবে। Setup বাতিল।")
        return

    # Use getpass so password isn't shown on screen
    password = getpass.getpass("  Password (input hidden): ").strip()
    if not password:
        print("❌ Password দিতে হবে। Setup বাতিল।")
        return

    print()

    # ── Validate credentials work ──────────────────────────────────────────
    print("🔍 Credentials যাচাই হচ্ছে (Quotex-এ login চেষ্টা করা হচ্ছে)...")
    print("   এতে ১০-৩০ সেকেন্ড সময় লাগতে পারে।")
    print()

    try:
        # Set env vars for the validation call
        os.environ["QX_EMAIL"] = email
        os.environ["QX_PASSWORD"] = password
        from qx_login import fetch_session
        import asyncio

        async def _validate():
            return await asyncio.wait_for(
                asyncio.to_thread(
                    fetch_session, email, password, "market-qx.trade",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
                timeout=60)

        result = asyncio.run(_validate())
    except ImportError as e:
        print(f"⚠️  curl_cffi ইনস্টল নেই: {e}")
        print("   প্রথমে: pip install curl_cffi")
        print("   তারপর আবার: python setup.py")
        return
    except Exception as e:
        print(f"⚠️  Login validation error: {e}")
        print("   .env ফাইল তৈরি করা হবে তবে হতে পারে credentials ভুল।")
        result = {}

    if not result.get("ssid"):
        error = result.get("error", "unknown error")
        print()
        print(f"❌ Login ব্যর্থ: {error}")
        print()
        if "PIN" in error or "keep_code" in error:
            print("🔧 Quotex নতুন device-এ PIN verify করতে চায়।")
            print("   ১. ব্রাউজারে https://market-qx.trade এ login করুন")
            print("   ২. ইমেইল চেক করে PIN দিন")
            print("   ৩. 'Remember this device' চেক করুন")
            print("   ৪. Logout করুন")
            print("   ৫. আবার: python setup.py")
            return
        if "rejected" in error.lower() or "HTTP 403" in error:
            print("🔧 Cloudflare blocking। Token দিয়ে চেষ্টা করুন:")
            print("   ১. ব্রাউজারে login করুন")
            print("   ২. F12 → Application → Cookies → 'session' কপি করুন")
            print("   ৩. .env ফাইলে: QX_TOKEN=<এখানে paste>")
            return
        ans = input(".env ফাইল তবুও তৈরি করব? (y/N): ").strip().lower()
        if ans != "y":
            print("Setup বাতিল।")
            return
    else:
        print("✅ Login সফল!")
        ssid = result["ssid"]
        print(f"   ssid = {ssid[:12]}... (length {len(ssid)})")

    # ── Write .env file ────────────────────────────────────────────────────
    print()
    print("📝 .env ফাইল তৈরি হচ্ছে...")

    try:
        ENV_PATH.write_text(
            ENV_TEMPLATE.format(email=email, password=password),
            encoding="utf-8")
        print(f"✅ .env ফাইল তৈরি হয়েছে: {ENV_PATH}")
    except Exception as e:
        print(f"❌ .env ফাইল তৈরিতে ত্রুটি: {e}")
        return

    # ── Save the working token too (so first server.py run is instant) ────
    if result.get("ssid"):
        try:
            with open(ENV_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n# Auto-saved working token (login সফল হওয়ার পর):\n")
                f.write(f"QX_TOKEN={result['ssid']}\n")
            print(f"✅ Working token-ও .env-এ সেভ করা হয়েছে")
        except Exception:
            pass

    print()
    print("═" * 65)
    print("  ✅ Setup সম্পূর্ণ!")
    print("═" * 65)
    print()
    print("এখন সার্ভার চালু করুন:")
    print()
    print("  python server.py")
    print()
    print("ব্রাউজারে: http://localhost:8000")
    print()


if __name__ == "__main__":
    main()
