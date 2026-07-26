"""Stub removed (DEEP-AUDIT-2026-07-26 / F-16-25).

The Resource class was a base class for HTTP-resource subclasses. After
the 2026-07-13 cleanup that removed trading/account/profile methods, no
subclass remains in the codebase. The class was never instantiated —
Login, Settings, etc. all extend Browser directly. This file is kept as
an empty marker for the network package's importability.
"""
