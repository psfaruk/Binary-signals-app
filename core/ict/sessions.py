"""Trading-session tagging and ICT kill-zone windows for Phase L4.

RESEARCH DISCLAIMER: session boundaries are a research convention;
validity on OTC feeds is unverified (OTC feeds are 24/7 synthetic — the
notion of a London "cash session" may have no meaning there). On
exchange feeds the sessions are real but still approximate. No predictive
claim is made: kill zones are a documented ICT research convention, not a
verified edge.

Conventions (all UTC, integer epoch seconds — no datetime objects in the
hot path so detectors stay fast and purely arithmetic):

  London cash 08:00-16:30 local  -> winter 08:00-16:30 UTC / summer 07:00-15:30 UTC
  New York 09:30-16:00 local     -> winter 14:30-21:00 UTC / summer 13:30-20:00 UTC
  Asia (research convention)     -> 15:00-24:00 UTC, the literal Tokyo cash
                                    session 00:00-09:00 JST (JST = UTC+9),
                                    which ends exactly at UTC midnight;
                                    half-open [15:00, 24:00) — no wrap.

  Overlap resolution order when several sessions are active:
  london > newyork > asia.

  Kill zones (ICT-style research convention): first 3h of each session —
  London KZ 08:00-11:00 (winter) / 07:00-10:00 (summer) UTC; NY KZ
  14:30-17:30 (winter) / 13:30-16:30 (summer) UTC. All windows are
  half-open [start, end). The kill_zone field is INDEPENDENT of the
  session-priority resolution: e.g. in winter 14:30-16:30 the session is
  "london" (priority) while the kill zone is "ny_kz" (the NY session's
  first 3h are running).

  DST approximation (documented, integer-exact):
    London DST: last Sunday of March 01:00 UTC -> last Sunday of October 01:00 UTC
    NY DST:     second Sunday of March 07:00 UTC -> first Sunday of November 06:00 UTC
  dst_mode "auto" applies these rules; "never"/"always" force both flags.
  Weekends are NOT modelled (crypto trades 24/7; FX weekend gaps are out
  of scope for this research module).
"""
from __future__ import annotations

from typing import Sequence

from .types import (
    Candle,
    ContractError,
    Detection,
    validate_candles,
    validate_detections,
)

__all__ = ["session_at", "tag_sessions", "detect_session_events"]

NOTE = "session boundaries are a research convention; validity on OTC feeds is unverified"

_DST_MODES = ("auto", "never", "always")

# session window edges, seconds-of-day (all half-open [start, end))
_ASIA_OPEN = 15 * 3600   # 15:00 UTC (Tokyo 00:00 JST)
_ASIA_CLOSE = 24 * 3600  # 24:00 UTC == UTC midnight (Tokyo 09:00 JST)
_LON_WINTER = (8 * 3600, 16 * 3600 + 1800)  # 08:00-16:30
_LON_SUMMER = (7 * 3600, 15 * 3600 + 1800)  # 07:00-15:30
_NY_WINTER = (14 * 3600 + 1800, 21 * 3600)  # 14:30-21:00
_NY_SUMMER = (13 * 3600 + 1800, 20 * 3600)  # 13:30-20:00
_KZ_HOURS = 3 * 3600


# ---------------------------------------------------------------------------
# integer civil calendar (Howard Hinnant's algorithms; days since 1970-01-01)
# ---------------------------------------------------------------------------
def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z: int) -> tuple[int, int, int]:
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + (1 if m <= 2 else 0), m, d)


def _year_of(ts: int) -> int:
    return _civil_from_days(ts // 86400)[0]


def _dow(days: int) -> int:
    """Day of week for an epoch day index: Monday=0 .. Sunday=6."""
    return (days + 3) % 7


def _last_sunday_of_march(y: int) -> int:
    d31 = _days_from_civil(y, 3, 31)
    return d31 - ((_dow(d31) + 1) % 7)


def _last_sunday_of_october(y: int) -> int:
    d31 = _days_from_civil(y, 10, 31)
    return d31 - ((_dow(d31) + 1) % 7)


def _nth_sunday_of_month(y: int, m: int, n: int) -> int:
    d1 = _days_from_civil(y, m, 1)
    first_sunday = d1 + ((6 - _dow(d1)) % 7)
    return first_sunday + (n - 1) * 7


def _london_dst_window(y: int) -> tuple[int, int]:
    """[start, end) epochs of UK DST approximation for year y."""
    start = _last_sunday_of_march(y) * 86400 + 3600  # 01:00 UTC
    end = _last_sunday_of_october(y) * 86400 + 3600  # 01:00 UTC
    return start, end


def _ny_dst_window(y: int) -> tuple[int, int]:
    """[start, end) epochs of US DST approximation for year y."""
    start = _nth_sunday_of_month(y, 3, 2) * 86400 + 7 * 3600  # 2nd Sun Mar 07:00 UTC
    end = _nth_sunday_of_month(y, 11, 1) * 86400 + 6 * 3600  # 1st Sun Nov 06:00 UTC
    return start, end


def _in_london_dst(ts: int) -> bool:
    start, end = _london_dst_window(_year_of(ts))
    return start <= ts < end


def _in_ny_dst(ts: int) -> bool:
    start, end = _ny_dst_window(_year_of(ts))
    return start <= ts < end


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def session_at(ts: int, dst_mode: str = "auto") -> dict:
    """Classify one unix-seconds timestamp into session + kill zone.

    Returns {"session": "asia"|"london"|"newyork"|"off",
             "kill_zone": "london_kz"|"ny_kz"|None,
             "dst_london": bool, "dst_ny": bool, "note": str}.
    """
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise ContractError(f"ts must be int (unix seconds), got {type(ts).__name__}")
    if dst_mode not in _DST_MODES:
        raise ContractError(f"dst_mode must be one of {_DST_MODES}, got {dst_mode!r}")
    if dst_mode == "always":
        dst_london = dst_ny = True
    elif dst_mode == "never":
        dst_london = dst_ny = False
    else:
        dst_london = _in_london_dst(ts)
        dst_ny = _in_ny_dst(ts)

    tod = ts % 86400
    lon_open, lon_close = _LON_SUMMER if dst_london else _LON_WINTER
    ny_open, ny_close = _NY_SUMMER if dst_ny else _NY_WINTER

    in_london = lon_open <= tod < lon_close
    in_ny = ny_open <= tod < ny_close
    in_asia = _ASIA_OPEN <= tod < _ASIA_CLOSE

    if in_london:
        session = "london"
    elif in_ny:
        session = "newyork"
    elif in_asia:
        session = "asia"
    else:
        session = "off"

    kill_zone = None
    if lon_open <= tod < lon_open + _KZ_HOURS:
        kill_zone = "london_kz"
    elif ny_open <= tod < ny_open + _KZ_HOURS:
        kill_zone = "ny_kz"

    return {
        "session": session,
        "kill_zone": kill_zone,
        "dst_london": dst_london,
        "dst_ny": dst_ny,
        "note": NOTE,
    }


# Every second-of-day at which a session edge or kill-zone edge can occur
# under EITHER dst regime, plus the Asia open (the Asia close IS midnight,
# i.e. the next day's base, which is added separately in _next_state_change).
_EDGE_OFFSETS = tuple(
    sorted(
        {
            _LON_WINTER[0], _LON_WINTER[1], _LON_WINTER[0] + _KZ_HOURS,
            _LON_SUMMER[0], _LON_SUMMER[1], _LON_SUMMER[0] + _KZ_HOURS,
            _NY_WINTER[0], _NY_WINTER[1], _NY_WINTER[0] + _KZ_HOURS,
            _NY_SUMMER[0], _NY_SUMMER[1], _NY_SUMMER[0] + _KZ_HOURS,
            _ASIA_OPEN,
        }
    )
)


def _next_state_change(ts: int) -> int:
    """Smallest t > ts at which the auto-DST (session, kill_zone) state can
    change. Sound over-approximation (pure integer arithmetic):
      * every session/KZ edge offset of today, tomorrow and the day after
        under BOTH dst regimes (the flags are constant between DST
        transitions, so the active edges are a subset of these),
      * the next one or two midnights (the Asia window [15:00, 24:00) ends
        exactly at UTC midnight, so the state can flip at the day rollover),
      * the next London and NY DST transitions (this year's or next year's).
    Used only to skip candles whose state provably cannot have changed."""
    day = ts // 86400
    cands = [d * 86400 + off for d in (day, day + 1, day + 2) for off in _EDGE_OFFSETS]
    cands += [(day + 1) * 86400, (day + 2) * 86400]  # midnights (Asia close)
    y = _year_of(ts)
    for window in (_london_dst_window, _ny_dst_window):
        for yy in (y, y + 1):
            s, e = window(yy)
            if s > ts:
                cands.append(s)
            if e > ts:
                cands.append(e)
    return min(c for c in cands if c > ts)


def _iter_tags(candles: Sequence[Candle]):
    """Yield (session, kill_zone) per candle, recomputing via session_at()
    only when a candle's ts reaches the next possible state change.
    Provably identical to calling session_at() on every candle (the state
    is constant on [ts, _next_state_change(ts)) by construction)."""
    state = None
    next_change = -1
    for c in candles:
        if state is None or c.ts >= next_change:
            st = session_at(c.ts)
            state = (st["session"], st["kill_zone"])
            next_change = _next_state_change(c.ts)
        yield state


def tag_sessions(candles: Sequence[Candle]) -> list[dict]:
    """Per-candle session tags aligned to index:
    [{"session": ..., "kill_zone": ...}, ...] via session_at(candle.ts).
    """
    validate_candles(candles)
    return [{"session": s, "kill_zone": k} for s, k in _iter_tags(candles)]


def detect_session_events(candles: Sequence[Candle]) -> list[Detection]:
    """Session/kill-zone boundary events between consecutive candles.

    The session and kill_zone components are compared INDEPENDENTLY against
    the previous candle's tags. At the first candle where a component
    CHANGED, emit (for that component only — an unchanged session never
    re-emits open/close just because the kill zone flipped, and vice versa):
      * kind "session_close"  for the previous session (unless it was "off")
      * kind "session_open"   for the new session (unless it is "off")
      * kind "kill_zone_close" for the previous kill zone (if any)
      * kind "kill_zone_open"  for the new kill zone (if any)
    confirm_index = the candle where the change occurs; side "neutral";
    meta {"session": <session or kill-zone name>, "event": <kind>}.

    The first candle of the series emits nothing (no predecessor to
    compare with — the session may have opened before the data started).
    Sparse candles only reveal the boundaries actually observed.
    """
    validate_candles(candles)
    events: list[Detection] = []
    prev: tuple[str, str | None] | None = None

    def _event(i: int, kind: str, name: str) -> Detection:
        return Detection(
            kind=kind,
            start_index=i,
            confirm_index=i,
            ts=candles[i].ts,
            side="neutral",
            meta={"session": name, "event": kind},
        )

    for i, state in enumerate(_iter_tags(candles)):
        if prev is not None:
            if state[0] != prev[0]:
                if prev[0] != "off":
                    events.append(_event(i, "session_close", prev[0]))
                if state[0] != "off":
                    events.append(_event(i, "session_open", state[0]))
            if state[1] != prev[1]:
                if prev[1] is not None:
                    events.append(_event(i, "kill_zone_close", prev[1]))
                if state[1] is not None:
                    events.append(_event(i, "kill_zone_open", state[1]))
        prev = state
    validate_detections(events, candles)
    return events
