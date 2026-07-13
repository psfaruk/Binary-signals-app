@echo off
chcp 65001 >nul
title Binary Signals App

REM ════════════════════════════════════════════════════════════════════
REM   Binary Signals App — One-Click Launcher
REM   শুধু এই ফাইলে double-click করুন, বাকিটা অ্যাপ নিজে করবে।
REM ════════════════════════════════════════════════════════════════════

cd /d "%~dp0"

echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║          Binary Signals App — শুরু হচ্ছে                       ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.

REM ── ধাপ ১: Python চেক ─────────────────────────────────────────────
echo [1/5] Python চেক হচ্ছে...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ❌ Python ইনস্টল নেই!
    echo.
    echo দয়া করে Python ইনস্টল করুন:
    echo   https://www.python.org/downloads/
    echo.
    echo ইনস্টল করার সময় "Add Python to PATH" অবশ্যই চেক করবেন।
    echo.
    pause
    exit /b 1
)
echo ✅ Python পাওয়া গেছে

REM ── ধাপ ২: Dependencies চেক/ইনস্টল ────────────────────────────────
echo [2/5] Dependencies চেক হচ্ছে...
python -c "import fastapi, uvicorn, websockets" >nul 2>&1
if errorlevel 1 (
    echo 📦 Dependencies ইনস্টল করা হচ্ছে... একটু সময় লাগবে...
    python -m pip install -r requirements.txt >nul 2>&1
    if errorlevel 1 (
        echo ❌ Dependencies ইনস্টল করতে সমস্যা। আবার চেষ্টা করা হচ্ছে...
        python -m pip install --user -r requirements.txt
    )
)
echo ✅ Dependencies প্রস্তুত

REM ── ধাপ ৩: session.json চেক ───────────────────────────────────────
echo [3/5] Quotex session চেক হচ্ছে...
if exist "session.json" (
    echo ✅ session.json পাওয়া গেছে — অটো-কানেক্ট হবে
    goto :START_SERVER
)

REM ── ধাপ ৪: .env চেক (email/password দিয়ে auto-login করবে) ────────
echo [4/5] session.json নেই — .env চেক হচ্ছে...
if exist ".env" (
    REM .env ফাইল আছে, কিন্তু তাতে কি আসল email/password আছে কিনা চেক করুন
    findstr /C:"QX_EMAIL=" /C:"QX_PASSWORD=" .env >nul 2>&1
    if not errorlevel 1 (
        REM QX_EMAIL এবং QX_PASSWORD পাওয়া গেছে — চেক করুন placeholder না
        findstr /C:"your_email" /C:"your_password" /C:"example.com" .env >nul 2>&1
        if errorlevel 1 (
            echo ✅ .env পাওয়া গেছে — অ্যাপ নিজে থেকে login করবে
            goto :START_SERVER
        ) else (
            echo ⚠️ .env ফাইলে placeholder আছে — নতুন করে তথ্য দিন
            goto :ASK_CREDENTIALS
        )
    ) else (
        echo ⚠️ .env ফাইলে QX_EMAIL/QX_PASSWORD নেই — নতুন করে তথ্য দিন
        goto :ASK_CREDENTIALS
    )
)

:ASK_CREDENTIALS
REM ── ধাপ ৫: প্রথমবার — credentials নিন ─────────────────────────────
echo [5/5] Quotex credentials দরকার
echo.
echo ┌──────────────────────────────────────────────────────────────┐
echo │  আপনার Quotex অ্যাকাউন্টের তথ্য দিন                            │
echo │  (এটা একবারই দিতে হবে, পরে অটো-কানেক্ট হবে)                   │
echo └──────────────────────────────────────────────────────────────┘
echo.

set /p QX_EMAIL="Quotex Email: "
if "%QX_EMAIL%"=="" (
    echo ❌ Email দিতে হবে।
    pause
    exit /b 1
)

set /p QX_PASSWORD="Quotex Password: "
if "%QX_PASSWORD%"=="" (
    echo ❌ Password দিতে হবে।
    pause
    exit /b 1
)

REM .env ফাইল তৈরি করুন
(
    echo QX_EMAIL=%QX_EMAIL%
    echo QX_PASSWORD=%QX_PASSWORD%
    echo QX_USE_RAW_WS=1
    echo PORT=8000
) > .env

echo.
echo ✅ .env ফাইল তৈরি হয়েছে
echo.

:START_SERVER
echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║  🚀 সার্ভার চালু হচ্ছে...                                       ║
echo ║  📊 ব্রাউজার নিজে থেকে খুলবে (৫ সেকেন্ড পরে)                    ║
echo ║  ⏹️  বন্ধ করতে: Ctrl+C অথবা এই window বন্ধ করুন                ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.

REM সার্ভার চালু করুন — browser নিজে থেকে খুলবে (server.py-এ implemented)
python server.py

REM সার্ভার বন্ধ হলে
echo.
echo সার্ভার বন্ধ হয়েছে।
pause
