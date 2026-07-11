@echo off
chcp 65001 >nul
REM Binary Signals App — Windows startup script
REM ব্যবহার: double-click করুন অথবা Command Prompt-এ: start.bat

cd /d "%~dp0"

echo.
echo ================================================================
echo   Binary Signals App — শুরু হচ্ছে
echo ================================================================
echo.

REM ── .env ফাইল চেক ─────────────────────────────────────────────────
if not exist ".env" (
    echo ❌ .env ফাইল নেই!
    echo.
    echo প্রথমবার setup করতে চালান:
    echo.
    echo    python setup.py
    echo.
    pause
    exit /b 1
)

REM ── Python dependencies চেক ────────────────────────────────────────
python -c "import fastapi, uvicorn, websockets, ntplib, curl_cffi" 2>nul
if errorlevel 1 (
    echo.
    echo 📦 Python dependencies missing। ইনস্টল করা হচ্ছে...
    echo.
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ❌ pip install ব্যর্থ। যদি "pip" না থাকে, চেষ্টা করুন:
        echo    python -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

REM ── সার্ভার শুরু ──────────────────────────────────────────────────
echo.
echo 🚀 সার্ভার শুরু হচ্ছে...
echo.
echo 📊 ব্রাউজারে খুলুন: http://localhost:8000
echo.
echo ⏹️  বন্ধ করতে: Ctrl+C
echo.

python server.py

pause
