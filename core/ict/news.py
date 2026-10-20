"""Economic-calendar import and news-window risk policy for Phase L4.

NO NETWORK — this module never fetches anything. Calendars are
owner-supplied local files (.json or .csv) whose bytes are hashed
(sha256) into every imported event for provenance. The bundled sample
``scripts/data/news_sample_calendar.json`` is a SYNTHETIC TEST FIXTURE
ONLY (fake events with self-labeling "SYNTHETIC-TEST" titles) — it is not
real calendar data and must never be presented as such.

gate_for_news() is a RISK-POLICY helper, not a prediction: callers use it
to emit NO_SIGNAL (refrain from signalling) around scheduled high-impact
news. Whether news windows actually matter for a given asset/feed is an
open research question (explicitly flagged in the Phase-L master plan) —
no claim is made here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .types import ContractError

__all__ = ["NewsEvent", "import_calendar", "minutes_to_event", "gate_for_news"]

_IMPACTS = ("high", "medium", "low")
_EPOCH_MS_THRESHOLD = 100_000_000_000  # above this an epoch is read as milliseconds


@dataclass(frozen=True)
class NewsEvent:
    """One calendar event. ts = unix seconds (UTC).

    source_file / source_sha256 record the exact file bytes the event was
    imported from (provenance; the hash is over the whole file).
    """

    ts: int
    currency: str
    impact: str
    title: str
    source_file: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.ts, int) or isinstance(self.ts, bool):
            raise ContractError(f"ts must be int (unix seconds), got {type(self.ts).__name__}")
        if self.ts <= 0:
            raise ContractError(f"ts must be > 0, got {self.ts}")
        for name in ("currency", "impact", "title"):
            v = getattr(self, name)
            if not isinstance(v, str):
                raise ContractError(f"{name} must be str, got {type(v).__name__}")
        # normalise whitespace (documented; content otherwise untouched)
        object.__setattr__(self, "currency", self.currency.strip())
        object.__setattr__(self, "title", self.title.strip())
        if not self.currency:
            raise ContractError("currency must be non-empty")
        if self.impact not in _IMPACTS:
            raise ContractError(f"impact must be one of {_IMPACTS}, got {self.impact!r}")
        if not self.title:
            raise ContractError("title must be non-empty")
        if not self.source_file:
            raise ContractError("source_file must be non-empty")
        if not self.source_sha256:
            raise ContractError("source_sha256 must be non-empty")


def _parse_epoch(value, where: str) -> int:
    """Epoch seconds-or-milliseconds (numeric only) -> unix seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{where}: ts must be a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        # JSON also accepts NaN/Infinity literals — reject them explicitly
        raise ContractError(f"{where}: ts must be finite, got {value!r}")
    ts = int(value)  # sub-second truncation (documented)
    if ts > _EPOCH_MS_THRESHOLD:
        ts //= 1000
    return ts


def _parse_iso_utc(value, where: str) -> int:
    """ISO timestamp with EXPLICIT UTC marker 'Z' or '+00:00' only."""
    if not isinstance(value, str):
        raise ContractError(f"{where}: time must be an ISO string, got {value!r}")
    s = value.strip()
    if s.endswith("Z"):
        normalised = s[:-1] + "+00:00"
    elif s.endswith("+00:00"):
        normalised = s
    else:
        raise ContractError(
            f"{where}: ISO time must carry an explicit UTC 'Z' or '+00:00' suffix, got {value!r}"
        )
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ContractError(f"{where}: unparseable ISO time {value!r}: {exc}") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ContractError(f"{where}: time must be UTC, got {value!r}")
    return int(dt.timestamp())


def import_calendar(path: str) -> list[NewsEvent]:
    """Import an owner-supplied calendar file (loader — the ONLY place in
    the core/ict package allowed to touch the filesystem, by design).

    .json : a top-level LIST of rows, each {"ts" (epoch s or ms) OR "time"
            (ISO UTC with explicit Z/+00:00 — anything else is rejected),
            "currency", "impact", "title"}. Exactly one of ts/time per row.
    .csv  : header exactly ``ts,time_utc,currency,impact,title``; per row
            the ts column takes precedence when non-empty, else time_utc
            (ISO UTC), else the row is malformed. Blank lines are skipped.

    Every event records source_file and the sha256 of the file bytes.
    Exact duplicates (same ts + currency + title) are dropped, keeping the
    first occurrence (import order is preserved). Malformed rows raise
    ContractError with the row number. No network access of any kind.
    """
    if not isinstance(path, str) or not path:
        raise ContractError(f"path must be a non-empty string, got {path!r}")
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    if suffix not in ("json", "csv"):
        # validated BEFORE touching the filesystem: an unsupported format is
        # a caller contract violation regardless of whether the file exists
        raise ContractError(f"unsupported calendar format: {path!r} (expected .json or .csv)")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ContractError(f"calendar file unreadable: {path!r}: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()

    if suffix == "json":
        rows = _rows_from_json(raw)
    else:
        rows = _rows_from_csv(raw)

    events: list[NewsEvent] = []
    seen: set[tuple[int, str, str]] = set()
    for idx, row in enumerate(rows):
        where = f"row {idx}"
        try:
            ev = NewsEvent(
                ts=row["ts"],
                currency=row["currency"],
                impact=row["impact"],
                title=row["title"],
                source_file=path,
                source_sha256=digest,
            )
        except ContractError as exc:
            raise ContractError(f"{where}: {exc}") from exc
        key = (ev.ts, ev.currency, ev.title)
        if key in seen:
            continue  # exact duplicate (ts + currency + title)
        seen.add(key)
        events.append(ev)
    return events


def _rows_from_json(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"calendar JSON unparseable: {exc}") from exc
    if not isinstance(data, list):
        raise ContractError("calendar JSON must be a top-level list of event rows")
    rows: list[dict] = []
    for idx, row in enumerate(data):
        where = f"row {idx}"
        if not isinstance(row, dict):
            raise ContractError(f"{where}: must be an object, got {type(row).__name__}")
        if "ts" in row and "time" in row:
            raise ContractError(f"{where}: has both 'ts' and 'time' — provide exactly one")
        if "ts" in row:
            ts = _parse_epoch(row["ts"], where)
        elif "time" in row:
            ts = _parse_iso_utc(row["time"], where)
        else:
            raise ContractError(f"{where}: needs 'ts' (epoch) or 'time' (ISO UTC)")
        impact = row.get("impact")
        if isinstance(impact, str):
            impact = impact.strip().lower()  # documented normalisation
        rows.append(
            {
                "ts": ts,
                "currency": row.get("currency"),
                "impact": impact,
                "title": row.get("title"),
            }
        )
    return rows


def _rows_from_csv(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError(f"calendar CSV not utf-8: {exc}") from exc
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    expected = ["ts", "time_utc", "currency", "impact", "title"]
    if header != expected:
        raise ContractError(f"calendar CSV header must be {expected}, got {header!r}")
    rows: list[dict] = []
    for line_no, fields in enumerate(reader, start=2):
        if not fields or all(not f.strip() for f in fields):
            continue  # blank line
        where = f"line {line_no}"
        if len(fields) != 5:
            raise ContractError(f"{where}: expected 5 fields, got {len(fields)}")
        ts_field, time_field = fields[0].strip(), fields[1].strip()
        if ts_field:
            try:
                ts = _parse_epoch(float(ts_field), where)
            except ValueError as exc:
                raise ContractError(f"{where}: non-numeric ts {ts_field!r}") from exc
        elif time_field:
            ts = _parse_iso_utc(time_field, where)
        else:
            raise ContractError(f"{where}: needs ts or time_utc")
        impact = fields[3].strip().lower()
        rows.append(
            {
                "ts": ts,
                "currency": fields[2].strip(),
                "impact": impact,
                "title": fields[4].strip(),
            }
        )
    return rows


def minutes_to_event(
    ts: int,
    events: Sequence[NewsEvent],
    impact: str = "high",
) -> tuple[int, str] | None:
    """(absolute minutes, title) of the NEAREST event whose impact is an
    EXACT match for ``impact`` — "high" matches high-impact events only
    (no escalation to include other levels; deliberate simple contract).
    Minutes = floor(|event_ts - ts| / 60). Ties break deterministically by
    (absolute distance, event ts, title). None if no matching events.
    """
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise ContractError(f"ts must be int (unix seconds), got {type(ts).__name__}")
    if impact not in _IMPACTS:
        raise ContractError(f"impact must be one of {_IMPACTS}, got {impact!r}")
    best_key: tuple[int, int, str] | None = None
    best_title: str | None = None
    for ev in events:
        if ev.impact != impact:
            continue
        diff = abs(ev.ts - ts)
        key = (diff, ev.ts, ev.title)
        if best_key is None or key < best_key:
            best_key = key
            best_title = ev.title
    if best_key is None:
        return None
    return (best_key[0] // 60, best_title)


def gate_for_news(
    ts: int,
    events: Sequence[NewsEvent],
    window_min: int = 30,
    impacts: tuple[str, ...] = ("high",),
) -> bool:
    """True iff ts is within window_min minutes (INCLUSIVE) of any event
    whose impact is in ``impacts``.

    RISK-POLICY helper: callers should use True to suppress signalling
    (emit NO_SIGNAL) around scheduled news. It says NOTHING about price
    direction or volatility — it is a standing policy hook, not a signal.
    """
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise ContractError(f"ts must be int (unix seconds), got {type(ts).__name__}")
    if not isinstance(window_min, int) or isinstance(window_min, bool) or window_min < 0:
        raise ContractError(f"window_min must be int >= 0, got {window_min!r}")
    for imp in impacts:
        if imp not in _IMPACTS:
            raise ContractError(f"impacts entries must be one of {_IMPACTS}, got {imp!r}")
    window_s = window_min * 60
    for ev in events:
        if ev.impact in impacts and abs(ev.ts - ts) <= window_s:
            return True
    return False
