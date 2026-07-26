"""Custom exception types raised by pyquotex public APIs.

FIX (DEEP-AUDIT-2026-07-26 / F-16-25): removed the dead
`QuotexTimeoutError` class — it was defined but never raised anywhere
in the codebase. Callers in `_api/history.py` and `_api/assets.py` catch
the stdlib `TimeoutError` / `asyncio.TimeoutError` directly.

Kept as a placeholder so future code can add custom exceptions here
without recreating the module.
"""
