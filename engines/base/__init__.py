"""
engines/base/__init__.py — Shared base engine code.

The OTC and Real engines are SEPARATE (each has its own config, weight
adapter, and 6th module), but they SHARE the blender algorithm, the
context computer, the types, and 5 of 6 modules.

That shared code lives here. Each engine package (`engines/otc/`,
`engines/real/`) is a thin wrapper that wires up a `BlenderConfig` and
exposes a `predict()` function.
"""
# FIX (DEEP-AUDIT-2026-07-26 / F-03-09): added explicit `__all__` (PROBLEM 71)
# and uniform `# noqa: F401` markers on every re-export import (PROBLEM 72)
# so the re-export intent is unambiguous and linting is consistent.
from engines.base.types import ModuleResult, MarketContext  # noqa: F401
from engines.base.context import compute_context  # noqa: F401
from engines.base.blender import predict, BlenderConfig  # noqa: F401
from engines.base.per_pair import PairWeightAdapter  # noqa: F401

__all__ = [
    "ModuleResult",
    "MarketContext",
    "compute_context",
    "predict",
    "BlenderConfig",
    "PairWeightAdapter",
]
