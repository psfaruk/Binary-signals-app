"""
Cloudflare bypass login via real browser (Playwright).

When curl_cffi gets HTTP 403 from Cloudflare, this module opens a REAL
Chrome browser (Cloudflare can't tell it from a human user), navigates
to Quotex, logs in with email/password, and extracts the session token
+ cookies — exactly what qx_login.fetch_session() returns.

Usage:
    from browser_login import fetch_session_browser
    result = fetch_session_browser(email, password, host, user_agent)

Returns: {"ssid": str, "cookies": str} on success, {"error": str} on failure.

The browser is launched HEADLESS by default. If Cloudflare still blocks,
set HEADLESS=0 env var to show the browser window (user can solve any
CAPTCHA manually, then the script continues automatically).
"""
import asyncio
import json
import os
import time
from typing import Optional


def fetch_session_browser(email: str, password: str,
                          host: str = "market-qx.trade",
                          user_agent: str | None = None,
                          headless: bool | None = None) -> dict:
    """
    Open a real Chrome browser, log into Quotex, extract ssid + cookies.

    Synchronous — run in a thread from async code (like qx_login.fetch_session).
    Returns {"ssid", "cookies"} on success, {"error": reason} on failure.
    """
    if not email or not password:
        return {"error": "email/password not provided"}

    # Headless mode: default True (faster), but Cloudflare sometimes
    # requires a visible browser. Set HEADLESS=0 to show the window.
    if headless is None:
        headless = os.environ.get("HEADLESS", "1") == "1"

    # ── Railway/memory guard (2026-07-12) ──────────────────────────────────
    # Playwright Chromium needs ~500MB RAM. On Railway's free 512MB plan,
    # launching it can OOM-crash the whole container. If QX_TOKEN is set
    # (user provided a manual token), skip Playwright entirely — the
    # WebSocket connection itself uses <50MB and runs fine on free tier.
    if os.environ.get("QX_TOKEN", "").strip():
        return {"error": "QX_TOKEN is set — skipping browser login "
                         "(use the token directly via WebSocket). "
                         "Unset QX_TOKEN to force browser login."}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "playwright not installed — pip install playwright && playwright install chromium"}

    base_url = f"https://{host}"
    login_url = f"{base_url}/en/sign-in/"
    trade_url = f"{base_url}/en/trade"

    try:
        with sync_playwright() as p:
            # Launch Chromium with stealth settings to bypass Cloudflare
            # Using channel="chrome" if available (more realistic than chromium)
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process,VizDisplayCompositor",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--disable-extensions",
                "--disable-default-apps",
                "--no-first-run",
                "--password-store=basic",
                "--use-mock-keychain",
            ]

            try:
                browser = p.chromium.launch(
                    headless=headless,
                    channel="chrome",  # Try real Chrome first
                    args=launch_args
                )
            except Exception:
                # Fall back to bundled chromium
                browser = p.chromium.launch(
                    headless=headless,
                    args=launch_args
                )

            context = browser.new_context(
                user_agent=user_agent or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                device_scale_factor=1,
                has_touch=False,
                is_mobile=False,
                java_script_enabled=True,
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )

            # Apply playwright-stealth if available (better Cloudflare bypass)
            try:
                from playwright_stealth import Stealth
                stealth = Stealth()
                stealth.apply_stealth_sync(context)
                print("[browser_login] playwright-stealth applied")
            except ImportError:
                print("[browser_login] playwright-stealth not installed, using manual stealth")
                # Manual stealth script as fallback
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [{name: 'PDF Viewer', filename: 'internal-pdf-viewer'}]
                    });
                    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
                    window.chrome = { runtime: {} };
                """)
            except Exception as exc:
                print(f"[browser_login] stealth apply error: {exc}")

            page = context.new_page()

            # ── Step 1: Navigate to login page ─────────────────────────────
            try:
                # First navigate to base URL to establish session
                page.goto(base_url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(5)  # Wait for Cloudflare challenge

                # Now navigate to login page
                page.goto(login_url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(5)  # Wait for any Cloudflare challenge
            except Exception as exc:
                return {"error": f"failed to load login page: {exc}"}

            # Check if Cloudflare challenge is present
            content = page.content()
            if "challenge" in content.lower() or "cf-browser-verification" in content.lower():
                # Wait longer for Cloudflare to clear
                print("[browser_login] Cloudflare challenge detected, waiting 15s...")
                time.sleep(15)
                page.reload(timeout=30000)
                time.sleep(5)

            # Check if we're on the login page or got redirected
            current_url = page.url
            if "trade" in current_url:
                # Already logged in — go straight to extraction
                return _extract_session(page, context, browser, email)

            # ── Step 2: Fill login form ────────────────────────────────────
            try:
                # Quotex login form fields — try multiple selectors
                email_input = page.wait_for_selector(
                    'input[name="email"], input[type="email"], input#email, #email',
                    timeout=15000)
                email_input.fill(email)

                password_input = page.wait_for_selector(
                    'input[name="password"], input[type="password"], input#password, #password',
                    timeout=10000)
                password_input.fill(password)

                # Submit the form
                submit_btn = page.query_selector(
                    'button[type="submit"], input[type="submit"], button.login-btn, button:has-text("Sign in")')
                if submit_btn:
                    submit_btn.click()
                else:
                    # Press Enter as fallback
                    password_input.press("Enter")

            except Exception as exc:
                # Take screenshot for debugging (if possible)
                try:
                    page.screenshot(path="/tmp/login_failed.png")
                    print(f"[browser_login] screenshot saved: /tmp/login_failed.png")
                    print(f"[browser_login] page title: {page.title()}")
                    print(f"[browser_login] page URL: {page.url}")
                except Exception:
                    pass
                return {"error": f"login form fill failed: {exc}"}

            # ── Step 3: Wait for redirect to /trade (success) ──────────────
            try:
                page.wait_for_url("**/trade**", timeout=20000)
            except Exception:
                # Check for error messages (wrong credentials, PIN required, etc.)
                content = page.content()
                if "keep_code" in content or "PIN" in content:
                    browser.close()
                    return {"error": "Quotex emailed you a PIN code (new device check). "
                                     "Log in once from a browser on this machine, "
                                     "or solve the PIN in the open window."}
                if "credentials" in content.lower() or "password" in content.lower():
                    browser.close()
                    return {"error": "wrong email or password"}
                # Still not on /trade — wait more
                time.sleep(5)

            # Final check
            if "trade" not in page.url:
                browser.close()
                return {"error": f"login did not redirect to /trade (landed on {page.url})"}

            # ── Step 4: Extract ssid + cookies ─────────────────────────────
            return _extract_session(page, context, browser, email)

    except Exception as exc:
        return {"error": f"browser login error: {exc}"}


def _extract_session(page, context, browser, email: str) -> dict:
    """Extract ssid (token) and cookies from the logged-in browser session."""
    try:
        # Method 1: Parse window.settings.token from the trade page
        ssid = None
        try:
            settings_json = page.evaluate("""
                () => {
                    try {
                        return JSON.stringify(window.settings || {});
                    } catch(e) { return null; }
                }
            """)
            if settings_json:
                settings = json.loads(settings_json)
                ssid = settings.get("token")
        except Exception:
            pass

        # Method 2: If window.settings didn't work, try reading cookies
        if not ssid:
            cookies = context.cookies()
            for c in cookies:
                if c.get("name") == "session":
                    ssid = c.get("value")
                    break

        if not ssid:
            browser.close()
            return {"error": "could not extract session token from page"}

        # Build cookie string for WebSocket headers
        all_cookies = context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)

        browser.close()
        return {"ssid": ssid, "cookies": cookie_str, "email": email}

    except Exception as exc:
        try:
            browser.close()
        except Exception:
            pass
        return {"error": f"session extraction failed: {exc}"}


def fetch_session_browser_async(email: str, password: str,
                                 host: str = "market-qx.trade",
                                 user_agent: str | None = None) -> dict:
    """Async wrapper — runs the sync function in a thread."""
    import asyncio
    return asyncio.to_thread(
        fetch_session_browser, email, password, host, user_agent)


if __name__ == "__main__":
    # Test mode: run from command line
    import sys
    if len(sys.argv) < 3:
        print("Usage: python browser_login.py <email> <password>")
        sys.exit(1)
    email = sys.argv[1]
    password = sys.argv[2]
    print(f"Logging into Quotex as {email}...")
    result = fetch_session_browser(email, password, headless=False)
    if result.get("ssid"):
        print(f"✅ Login OK — ssid={result['ssid'][:16]}...")
        print(f"   Cookies: {len(result.get('cookies', ''))} chars")
    else:
        print(f"❌ Login failed: {result.get('error')}")
