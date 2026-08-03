# scripts package marker.
#
# FIX (DEEP-AUDIT-2026-07-26 / F-19-26): add `scripts/__init__.py` so the
# directory is recognised as a Python package (A-10 PROBLEM 48). Scripts
# still use the `sys.path.insert(...)` parent-app trick to import `feed.py`,
# `core.*`, `engines.*` — that's intentional because the repo root is not a
# package itself. This file only enables `scripts._helpers` and similar
# intra-package imports.
