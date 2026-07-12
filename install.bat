@echo off
chcp 65001 >nul
title Binary Signals App — Installer

REM ════════════════════════════════════════════════════════════════════
REM   Binary Signals App — One-Click Installer
REM   এই ফাইলে double-click করলে সব কিছু নিজে থেকে হবে:
REM     1. App download
REM     2. Dependencies install
REM     3. Quotex credentials নেওয়া
REM     4. সার্ভার চালু
REM ════════════════════════════════════════════════════════════════════

cd /d "%~dp0"

echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║       Binary Signals App — Installer                         ║
echo ║       এক ক্লিকে সব কিছু হবে                                   ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.

REM ── Python চেক ─────────────────────────────────────────────────────
echo [1/4] Python চেক হচ্ছে...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ❌ Python ইনস্টল নেই!
    echo.
    echo দয়া করে প্রথমে Python ইনস্টল করুন:
    echo.
    echo   ১. https://www.python.org/downloads/ এ যান
    echo   ২. "Download Python" ক্লিক করুন
    echo   ৩. ইনস্টল করার সময় "Add Python to PATH" অবশ্যই চেক করবেন ⚠️
    echo   ৪. Install ক্লিক করুন
    echo.
    echo ইনস্টল হলে আবার এই ফাইলে double-click করুন।
    echo.
    pause
    exit /b 1
)
echo ✅ Python পাওয়া গেছে

REM ── Git চেক (app download করার জন্য) ──────────────────────────────
echo [2/4] Git চেক হচ্ছে...
git --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ❌ Git ইনস্টল নেই!
    echo.
    echo দয়া করে Git ইনস্টল করুন:
    echo.
    echo   ১. https://git-scm.com/download/win এ যান
    echo   ২. "Click here to download" ক্লিক করুন
    echo   ৩. ইনস্টল করুন (সব default অপশনে Next ক্লিক করুন)
    echo.
    echo ইনস্টল হলে আবার এই ফাইলে double-click করুন।
    echo.
    pause
    exit /b 1
)
echo ✅ Git পাওয়া গেছে

REM ── App download (যদি এখনও download করা হয়নি) ────────────────────
echo [3/4] App download হচ্ছে...
if exist "feed.py" (
    echo ✅ App আগে থেকেই আছে
    goto :UPDATE_APP
)

echo 📥 GitHub থেকে download হচ্ছে...
git clone https://github.com/psfaruk/Binary-signals-app.git temp_app
if errorlevel 1 (
    echo ❌ Download ব্যর্থ। ইন্টারনেট সংযোগ চেক করুন।
    pause
    exit /b 1
)

REM temp_app এর সব ফাইল current folder-এ নিয়ে আসুন
xcopy temp_app\* . /E /I /H /Y >nul
rmdir /S /Q temp_app
echo ✅ App download সম্পূর্ণ

:UPDATE_APP
echo 🔄 App আপডেট হচ্ছে (যদি নতুন version থাকে)...
git pull >nul 2>&1
echo ✅ App আপ-টু-ডেট

REM ── Dependencies ইনস্টল ───────────────────────────────────────────
echo [4/4] Python dependencies ইনস্টল হচ্ছে...
echo কয়েক সেকেন্ড সময় লাগবে...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ⚠️ কিছু dependency install করতে সমস্যা, আবার চেষ্টা করা হচ্ছে...
    python -m pip install --user -r requirements.txt
)
echo ✅ Python dependencies ইনস্টল সম্পূর্ণ

echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║  ✅ ইনস্টলেশন সম্পূর্ণ!                                        ║
echo ║                                                              ║
echo ║  এখন start.bat চালু করুন:                                     ║
echo ║    শুধু start.bat ফাইলে double-click করুন                      ║
echo ║                                                              ║
echo ║  প্রথমবার আপনার Quotex email + password দিতে হবে              ║
echo ║  এরপর থেকে অটো-কানেক্ট হবে                                    ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.
echo এখন start.bat চালু করতে যেকোনো key চাপুন...
pause

REM সরাসরি start.bat চালু করুন
start "" "%~dp0start.bat"
exit
