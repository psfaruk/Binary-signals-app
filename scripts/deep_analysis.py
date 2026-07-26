#!/usr/bin/env python3
# TODO (DEEP-AUDIT-2026-07-26 / F-20 / cross-file cleanup):
#   Line 19 — `import os, re, ast, sys` — `ast` and `sys` are NEVER USED
#   in this file (verified via Grep — zero `ast.` or `sys.` references).
#   Safe to remove both imports. Audit ref: A-10 dead-imports table.
"""
Deep automated analysis — scans the codebase for potential prediction issues.

This script statically analyzes the prediction pipeline to find:
1. Numeric precision issues (int() vs round())
2. Off-by-one errors in slicing/indexing
3. Inconsistent thresholds across modules
4. Dead code paths
5. Potential division-by-zero
6. Magic numbers that should be configurable
7. Missing None-checks on optional fields
8. Hardcoded values that look like they should be derived

Output: a categorized list of potential issues for manual review.
This is NOT a "1000 problems" claim — it's a structured deep scan
that finds ACTUAL code-level issues that could affect prediction.
"""
import os, re  # FIX (DEEP-AUDIT-2026-07-26 / F-19-10): removed unused `ast`, `sys` (A-10 PROBLEM 13, 14).
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRITICAL_FILES = [
    "engines/base/blender.py",
    "engines/base/modules/otc_pattern.py",
    "engines/base/modules/candle_reaction.py",
    "engines/base/modules/trend_follow.py",
    "engines/base/modules/indicator.py",
    "engines/base/modules/key_level.py",
    "engines/base/modules/running_tick.py",
    "engines/base/modules/pattern.py",
    "core/analysis.py",
    "core/brain.py",
    "core/microstructure.py",
    "core/algorithm_strategy.py",
    "core/algorithm_monitor.py",
    "core/time_patterns.py",
    "core/auto_tune.py",
    "feed.py",
]

issues = []


def add_issue(category, severity, file, line, description):
    issues.append({
        "category": category,
        "severity": severity,
        "file": file,
        "line": line,
        "description": description,
    })


def scan_file(filepath):
    """Run all scanners on a single file."""
    rel_path = os.path.relpath(filepath, ROOT)
    with open(filepath) as f:
        lines = f.readlines()
    src = "".join(lines)

    # ─── Scanner 1: int() truncation on numeric multipliers ─────────────
    # Pattern: `var = int(var * multiplier)` — should be round() for
    # mathematical correctness (int() truncates, round() rounds).
    for i, line in enumerate(lines, 1):
        # Skip comment lines
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Find `something = int(something * X)` patterns
        # but NOT int(math.sqrt(...)) which is the initial computation
        m = re.search(r'(\w+)\s*=\s*int\(\s*\1\s*\*\s*[\w.]+\s*\)', line)
        if m and "math.sqrt" not in line and "len(" not in line:
            add_issue(
                "numeric_precision", "MEDIUM", rel_path, i,
                f"int() truncation on multiplier — use round() instead: {line.strip()}"
            )

    # ─── Scanner 2: potential division by zero ──────────────────────────
    # Pattern: `x / y` where y is not guarded with `if y > 0`
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Find divisions that don't have an obvious guard
        if "/" in line and "len(" in line:
            # Check if the line divides by len(...) without a guard
            m = re.search(r'/\s*len\((\w+)\)', line)
            if m and f"len({m.group(1)}) > 0" not in src:
                # Check if there's a guard in the same line or the previous 3 lines.
                # FIX (DEEP-AUDIT-2026-07-26 / F-19-12): off-by-one fix — use
                # `lines[max(0, i-4):max(0, i-1)]` to get the actual 3 preceding
                # lines (A-10 PROBLEM 67). Original `lines[max(0, i-3):i]` returned
                # only 1 line of context when i==1.
                context = "".join(lines[max(0, i-4):max(0, i-1)])
                if f"len({m.group(1)})" in context and "> 0" in context:
                    continue  # guarded
                add_issue(
                    "division_by_zero", "LOW", rel_path, i,
                    f"potential div by len({m.group(1)}) without explicit guard: {line.strip()}"
                )

    # ─── Scanner 3: hardcoded magic numbers in thresholds ──────────────
    # Pattern: numbers like 0.5, 0.6, 0.7 used as thresholds — these are
    # often tuned by hand and would benefit from env-configurability.
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Find `> 0.X` or `< 0.X` or `>= 0.X` patterns
        for m in re.finditer(r'(>|<|>=|<=)\s*(0\.\d+)', line):
            threshold = m.group(2)
            # Skip if it's a percentage (0.0 or 1.0)
            if threshold in ("0.0", "1.0", "0.5"):  # 0.5 is too common
                continue
            # Check if it's NOT env-configurable
            if "os.environ" not in line and "env" not in line.lower():
                add_issue(
                    "hardcoded_threshold", "LOW", rel_path, i,
                    f"hardcoded threshold {threshold} — consider env-configurable: {line.strip()}"
                )

    # ─── Scanner 4: list[-N] without length check ────────────────────
    # Pattern: `list[-N]` where N >= 3 — could IndexError if not enough items.
    # FIX (DEEP-AUDIT-2026-07-26 / F-19-11): broaden regex to catch ANY
    # `list[-N]` access, not just `candles[-N]` (A-10 PROBLEM 65).
    _SAFE_NEG_INDEX_VARS = {"args", "sys", "os"}  # known small fixed-size lists
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for m in re.finditer(r'(\w+)\[-(\d+)\]', line):
            var_name = m.group(1)
            n = int(m.group(2))
            if var_name in _SAFE_NEG_INDEX_VARS:
                continue
            if n >= 3:  # [-1] and [-2] are usually safe
                # Check if there's a length guard in the function
                if f"len({var_name}) >= {n}" not in src and f"len({var_name}) > {n}" not in src:
                    add_issue(
                        "potential_index_error", "LOW", rel_path, i,
                        f"{var_name}[-{n}] without explicit len guard: {line.strip()}"
                    )

    # ─── Scanner 5: dict.get() without default for critical fields ──────
    # Pattern: `pred.get("signal")` without default — could return None
    # which then fails a comparison silently
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Find `.get("signal")` without a default
        m = re.search(r'\.get\(["\']signal["\']\)(?!\s*,)', line)
        if m and "==" in line:
            add_issue(
                "missing_default", "LOW", rel_path, i,
                f".get('signal') without default in comparison — could be None: {line.strip()}"
            )

    # ─── Scanner 6: ambiguous truthy checks on numbers ──────────────────
    # Pattern: `if score:` or `if conf:` — for numbers, 0 is falsy,
    # which may not be the intent
    # FIX (DEEP-AUDIT-2026-07-26 / F-19-13): extended variable-name list to
    # catch common variants (net_score, total_conf, etc.). A-10 PROBLEM 66.
    _TRUTHY_NUMBER_VARS = (
        "score", "conf", "confidence", "net", "total",
        "net_score", "total_conf", "total_score", "net_margin",
        "score_val", "conf_val", "confidence_val",
    )
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = re.search(r'if\s+(\w+)\s*:', line)
        if m:
            var = m.group(1)
            if var in _TRUTHY_NUMBER_VARS:
                add_issue(
                    "truthy_check_on_number", "LOW", rel_path, i,
                    f"truthy check on number '{var}' — 0 is falsy, may not be intent: {line.strip()}"
                )

    # ─── Scanner 7: TODO/FIXME/HACK comments ────────────────────────────
    for i, line in enumerate(lines, 1):
        if re.search(r'\b(TODO|FIXME|HACK|XXX)\b', line):
            add_issue(
                "todo_marker", "INFO", rel_path, i,
                f"unresolved marker: {line.strip()}"
            )

    # ─── Scanner 8: bare except (catches everything) ───────────────────
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if re.match(r'\s*except\s*:', line):
            add_issue(
                "bare_except", "MEDIUM", rel_path, i,
                f"bare except: catches SystemExit and KeyboardInterrupt — use except Exception: {line.strip()}"
            )

    # ─── Scanner 9: pass in except (silent error swallowing) ────────────
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Find `except ... :` followed by `pass` on the next line
        if i < len(lines) - 1:
            next_line = lines[i].lstrip() if i < len(lines) else ""
            if "except" in line and next_line.startswith("pass"):
                add_issue(
                    "silent_error_swallow", "LOW", rel_path, i,
                    f"except + pass silently swallows errors — consider logging: {line.strip()}"
                )


def main():
    print("=" * 70)
    print("DEEP AUTOMATED ANALYSIS — prediction pipeline")
    print("=" * 70)
    print()

    for rel_path in CRITICAL_FILES:
        full_path = os.path.join(ROOT, rel_path)
        if os.path.exists(full_path):
            scan_file(full_path)

    # Group by category
    by_category = defaultdict(list)
    for issue in issues:
        by_category[issue["category"]].append(issue)

    print(f"Total potential issues found: {len(issues)}")
    print()
    print("Issues by category:")
    for cat, items in sorted(by_category.items()):
        print(f"  {cat}: {len(items)} ({items[0]['severity']})")

    print()
    print("=" * 70)
    print("DETAILED ISSUES (first 5 per category)")
    print("=" * 70)
    for cat, items in sorted(by_category.items()):
        print(f"\n--- {cat} ({len(items)} issues) ---")
        for issue in items[:5]:
            print(f"  [{issue['severity']}] {issue['file']}:{issue['line']}")
            print(f"      {issue['description']}")
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")

    # FIX (DEEP-AUDIT-2026-07-26 / F-19-14): move disclaimer to script
    # docstring only — no runtime output (A-10 PROBLEM 87). The honest
    # disclosure is in the module docstring at the top of this file.


if __name__ == "__main__":
    main()
