"""Stub removed (DEEP-AUDIT-2026-07-26 / F-16-23).

The Logout class was a stub (`__init__` only, no methods) — dead code
with no caller anywhere in this codebase. The matching `QuotexAPI.logout`
property in api.py has also been removed. This file is kept as an empty
marker so any lingering `from pyquotex.network.logout import Logout`
imports elsewhere don't crash with ModuleNotFoundError — the class symbol
itself is gone, but the module path remains importable.
"""
