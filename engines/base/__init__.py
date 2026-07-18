"""
engines/base/__init__.py — Shared base engine code.

The OTC and Real engines are SEPARATE (each has its own config, weight
adapter, and 6th module), but they SHARE the blender algorithm, the
context computer, the types, and 5 of 6 modules.

That shared code lives here. Each engine package (`engines/otc/`,
`engines/real/`) is a thin wrapper that wires up a `BlenderConfig` and
exposes a `predict()` function.
"""
from engines.base.types import ModuleResult, MarketContext
from engines.base.context import compute_context
from engines.base.blender import predict, BlenderConfig  # noqa: F401
from engines.base.per_pair import PairWeightAdapter
