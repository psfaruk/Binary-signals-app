"""NEXT-CANDLE ENGINE — "Candle Sequence Engine" (CSE v1)
=================================================================
USER DIRECTIVE (2026-09-29, verbatim intent):

  "যখন একটি ক্যান্ডেল শুরু হবে 0 সেকেন্ড এ, ওই শুরু হওয়া ক্যান্ডেল টি red
  হবে নাকি গ্রিন হবে এটার প্রেডিকশন... আগের ক্যান্ডেল বা আগের মার্কেট চার্ট
  এনালাইসিস করবে, শুরু হওয়া নতুন ক্যান্ডেল টি প্রেডিকশন করবে।"

  ONE strategy is enough (user: "একটা স্ট্রাটেজি সঠিক হলে একটাই যথেষ্ট") —
  this module IS that one strategy.  It replaces the old signal chain
  (module theories → ML fallback → body-fade) as THE source of every
  candle's CALL/PUT.

WHAT IT DOES
------------
At the exact open (0s) of every NEW candle, predict the color of THAT
candle (close > open = GREEN/CALL, close < open = RED/PUT) from the
anatomy + sequence of the PREVIOUS closed candles only:

  * candle anatomy        — body sizes, wick rejection, close location
  * sequence structure    — color streaks, 2/3-candle color patterns,
                            empirical pattern-transition probabilities
                            ("কোন ক্যান্ডেলের পরে কোন ক্যান্ডেল আসে")
  * market context        — range position, stretch, EMA trend tilt,
                            5-minute higher-timeframe trend, volatility
                            regime, hour-of-day

MODEL: one regularized logistic regression over 34 features.
  * GLOBAL PRIOR — weights pooled-trained on real market data
    (BTC/ETH/XRP/BNB 1m, walk-forward-validated) — ships as
    core/next_candle_weights.json.  Sane prediction from candle #1.
  * LOCAL ADAPTATION — the same model refit ONLINE on this asset's own
    recent candles (trailing 5000 rows, refit every 50).  Research
    showed frozen weights score 35-40% on a mean-reverting feed while
    the ADAPTED model scores 66-71% — the engine LEARNS which candle
    follows which ON THIS FEED.
  * BLEND — p = w·p_local + (1-w)·p_global,  w = n/(n+3000).
  * CALIBRATION — displayed probability shrunk by the measured pooled
    calibration slope (0.636), so on-screen 60% means a real ~60%.

BACKTEST (scripts/backtest_next_candle.py — runs THIS engine end-to-end):
  real 1m crypto walk-forward : 50.8–53.6% per symbol (pooled ~52%,
                                p < 1e-6 vs coin flip; beats every
                                baseline: continuation / reversal /
                                streak-fade / 2nd-order Markov)
  synthetic mean-reverting   : 66–71%  (structure captured)
  synthetic regime-trend     : ~69%
  pure random walk (GBM)     : ~50%   (NO fake edge — honest)
No look-ahead anywhere: features at candle i use candles ≤ i only.
=================================================================
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

try:  # numpy is in requirements.txt; degrade gracefully without it
    import numpy as np
except Exception:  # pragma: no cover
    np = None

_EPS = 1e-12
_HERE = os.path.dirname(os.path.abspath(__file__))
_WEIGHTS_FILE = os.path.join(_HERE, "next_candle_weights.json")

# ── tunables (env-overridable; research defaults) ────────────────
BLEND_K = float(os.environ.get("QX_NC_BLEND_K", "3000"))     # local/global blend
FIT_MIN_ROWS = int(os.environ.get("QX_NC_FIT_MIN", "300"))   # local fit threshold
TRAIN_WINDOW = int(os.environ.get("QX_NC_WINDOW", "5000"))   # trailing rows
REFIT_EVERY = int(os.environ.get("QX_NC_REFIT", "50"))       # rows between refits
BULK_REFIT_EVERY = int(os.environ.get("QX_NC_BULK_REFIT", "1000"))
CALIB_SLOPE = 0.636          # measured pooled calibration slope (research)
L2 = 1.0
GD_ITERS = 500
GD_LR = 0.3

FEATURE_NAMES = [
    "last_color", "streak", "body1", "body2", "body3", "body_decay",
    "press3", "press10_z", "wick_imb1", "wick_imb3", "clv1", "range_pos",
    "ret5", "ret20", "ret60", "ema_gap", "same2", "atr_ratio",
    "abs_body1", "abs_body2", "range1_atr", "stretch50", "htf5_gap",
    "hour_sin", "hour_cos", "patt2_prob", "patt3_prob",
    "pat_ggg", "pat_ggr", "pat_grg", "pat_grr",
    "pat_rgg", "pat_rgr", "pat_rrg", "pat_rrr",
]
N_FEATURES = len(FEATURE_NAMES)
_PATTERNS = ("ggg", "ggr", "grg", "grr", "rgg", "rgr", "rrg", "rrr")

FACTOR_LABELS = {
    "last_color": "শেষ ক্যান্ডেলের রঙ",
    "streak": "একটানা কালার-স্ট্রিক",
    "body1": "শেষ বডি (মোমেন্টাম)",
    "body2": "আগের বডি",
    "body3": "৩-আগের বডি",
    "body_decay": "বডি-গতি পরিবর্তন",
    "press3": "৩-ক্যান্ডেল নেট চাপ",
    "press10_z": "১০-ক্যান্ডেল চাপ (z)",
    "wick_imb1": "উইক রিজেকশন (শেষ)",
    "wick_imb3": "উইক রিজেকশন (৩টি)",
    "clv1": "ক্লোজ অবস্থান (ক্যান্ডেলের কোথায়)",
    "range_pos": "২০-রেঞ্জে অবস্থান",
    "ret5": "৫-ক্যান্ডেল রিটার্ন",
    "ret20": "২০-ক্যান্ডেল রিটার্ন",
    "ret60": "৬০-ক্যান্ডেল রিটার্ন",
    "ema_gap": "EMA9−21 ট্রেন্ড টিল্ট",
    "same2": "শেষ দুটি একই রঙ?",
    "atr_ratio": "ভোলাটিলিটি রেজিম",
    "abs_body1": "শেষ বডির সাইজ",
    "abs_body2": "আগের বডির সাইজ",
    "range1_atr": "শেষ ক্যান্ডেলের রেঞ্জ",
    "stretch50": "৫০-ক্যান্ডেল স্ট্রেচ",
    "htf5_gap": "৫-মিনিট ট্রেন্ড (HTF)",
    "hour_sin": "দিনের সময় (sin)",
    "hour_cos": "দিনের সময় (cos)",
    "patt2_prob": "২-ক্যান্ডেল প্যাটার্ন-স্ট্যাট",
    "patt3_prob": "৩-ক্যান্ডেল প্যাটার্ন-স্ট্যাট",
    "pat_ggg": "প্যাটার্ন 🟢🟢🟢", "pat_ggr": "প্যাটার্ন 🟢🟢🔴",
    "pat_grg": "প্যাটার্ন 🟢🔴🟢", "pat_grr": "প্যাটার্ন 🟢🔴🔴",
    "pat_rgg": "প্যাটার্ন 🔴🟢🟢", "pat_rgr": "প্যাটার্ন 🔴🟢🔴",
    "pat_rrg": "প্যাটার্ন 🔴🔴🟢", "pat_rrr": "প্যাটার্ন 🔴🔴🔴",
}


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


# ────────────────────────── model ──────────────────────────
class _Logistic:
    """Standardized-feature logistic regression, L2, full-batch GD.
    Identical math to the research prototype (/research/cse.py)."""

    __slots__ = ("w", "b", "mu", "sd")

    def __init__(self):
        self.w = None
        self.b = 0.0
        self.mu = None
        self.sd = None

    def fit(self, X, y_pm) -> "_Logistic":
        if np is None:
            return self
        X = np.asarray(X, dtype=np.float64)
        t = (np.asarray(y_pm) > 0).astype(np.float64)
        m, d = X.shape
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd < 1e-9] = 1.0
        Z = (X - self.mu) / self.sd
        w = np.zeros(d)
        b = 0.0
        vw = np.zeros(d)
        vb = 0.0
        beta = 0.9
        for _ in range(GD_ITERS):
            p = 1.0 / (1.0 + np.exp(-(Z @ w + b)))
            g = p - t
            gw = (Z.T @ g) / m + (L2 / m) * w
            gb = g.mean()
            vw = beta * vw + (1 - beta) * gw
            vb = beta * vb + (1 - beta) * gb
            w -= GD_LR * vw
            b -= GD_LR * vb
        self.w = w
        self.b = float(b)
        return self

    def predict_proba_row(self, x: List[float]) -> Optional[float]:
        if self.w is None:
            return None
        try:
            if np is not None:
                z = (np.asarray(x, dtype=np.float64) - self.mu) / self.sd
                return float(1.0 / (1.0 + math.exp(
                    -float(z @ self.w + self.b))))
            # pure-python path (global prior without numpy)
            s = self.b
            for j in range(N_FEATURES):
                sd = self.sd[j] if abs(self.sd[j]) > 1e-9 else 1.0
                s += self.w[j] * ((x[j] - self.mu[j]) / sd)
            return 1.0 / (1.0 + math.exp(-s))
        except (OverflowError, ValueError):
            return 0.5


def _load_global_model() -> Optional[_Logistic]:
    try:
        with open(_WEIGHTS_FILE) as f:
            d = json.load(f)
        if list(d.get("names") or []) != FEATURE_NAMES:
            print("[next-candle] global weights feature-order mismatch — "
                  "prior disabled")
            return None
        m = _Logistic()
        m.w = [float(v) for v in d["w"]]
        m.b = float(d["b"])
        m.mu = [float(v) for v in d["mu"]]
        m.sd = [float(v) for v in d["sd"]]
        return m
    except FileNotFoundError:
        print("[next-candle] no global weights file — running local-only")
        return None
    except Exception as exc:
        print(f"[next-candle] global weights load failed: {exc}")
        return None


_GLOBAL_MODEL = _load_global_model()


# ────────────────────────── engine ──────────────────────────
class NextCandleEngine:
    """ONE strategy: next-candle color at its 0-second open.

    feed.py wiring:
      engine.ingest(candle)     → call for EVERY closed candle (idempotent
                                  by time; bulk-replays broker history at
                                  stream start).  Returns the prediction
                                  payload for the candle that FOLLOWS.
      engine.current()          → payload _run_eoc turns into the signal.
    O(1) per candle (rolling deques + running state).
    """

    def __init__(self, asset: str, period: int):
        self.asset = asset
        self.period = int(period)
        self.lock = threading.Lock()

        # ── rolling candle state ──────────────────────────────
        self.last_time = -1
        self.prev_close: Optional[float] = None
        self.tr14: Deque[float] = deque(maxlen=14)
        self.tr5: Deque[float] = deque(maxlen=5)
        self.tr50: Deque[float] = deque(maxlen=50)
        self.closes: Deque[float] = deque(maxlen=61)
        self.bodies3: Deque[float] = deque(maxlen=3)
        self.bodies10: Deque[float] = deque(maxlen=10)
        self.press10_hist: Deque[float] = deque(maxlen=200)
        self.hi20: Deque[float] = deque(maxlen=20)
        self.lo20: Deque[float] = deque(maxlen=20)
        self.c50: Deque[float] = deque(maxlen=50)
        self.upw3: Deque[float] = deque(maxlen=3)
        self.dnw3: Deque[float] = deque(maxlen=3)
        self.rng3: Deque[float] = deque(maxlen=3)
        self.ema9: Optional[float] = None
        self.ema21: Optional[float] = None
        # HTF 5m
        self._htf_bucket_ms: Optional[int] = None
        self._htf_prev_close: Optional[float] = None
        self._htf_e9: Optional[float] = None
        self._htf_e21: Optional[float] = None
        self._htf_c21: Deque[float] = deque(maxlen=21)
        self._htf_count = 0
        # colors / sequence (actual colors drive streak/same2; carried
        # colors — doji inherits previous — drive the patterns)
        self.streak = 0.0
        self._prev_actual = 0
        self.carried3: Deque[int] = deque(maxlen=3)
        self._prev_carried = 0
        self._carried_hist: Deque[int] = deque(maxlen=4)
        self.cnt2: Dict[Tuple[int, int], List[int]] = {}
        self.cnt3: Dict[Tuple[int, int, int], List[int]] = {}
        # last-candle anatomy (for clv / last_color / same2)
        self._last_clv = 0.0
        self._last_color = 0

        # ── training buffer ───────────────────────────────────
        self._pending_x: Optional[List[float]] = None
        self._train_x: List[List[float]] = []
        self._train_y: List[int] = []
        self._rows_since_fit = REFIT_EVERY + 1   # fit as soon as possible
        self.local_model: Optional[_Logistic] = None
        self.n_train = 0

        # ── prediction & grading ──────────────────────────────
        self._current: Optional[dict] = None
        self.stats = {"total": 0, "correct": 0, "skipped_doji": 0,
                      "cur_wrong": 0, "max_wrong": 0,
                      "last20": deque(maxlen=20)}

    # ────────────────────────── ingest ───────────────────────
    def ingest(self, candle: dict, *, bulk: bool = False) -> Optional[dict]:
        """Process ONE closed candle (idempotent by candle time).
        Returns the prediction payload for the FOLLOWING candle."""
        try:
            t = int(candle.get("time") or candle.get("t") or 0)
            o = float(candle.get("open") if candle.get("open") is not None
                      else candle.get("o"))
            h = float(candle.get("high") if candle.get("high") is not None
                      else candle.get("h"))
            lo = float(candle.get("low") if candle.get("low") is not None
                       else candle.get("l"))
            c = float(candle.get("close") if candle.get("close") is not None
                      else candle.get("c"))
        except (TypeError, ValueError, KeyError):
            return None
        if t <= self.last_time:
            return None  # duplicate / out-of-order — already processed

        with self.lock:
            # 1) grade the prediction made for THIS candle (it was built at
            #    the previous ingest; target_time must match exactly)
            self._grade(t, o, c)

            # 2) resolve the pending training row (target = this color)
            body = c - o
            color = 1 if body > 0 else (-1 if body < 0 else 0)
            if self._pending_x is not None and color != 0:
                self._train_x.append(self._pending_x)
                self._train_y.append(color)
                if len(self._train_x) > TRAIN_WINDOW:
                    cut = len(self._train_x) - TRAIN_WINDOW
                    del self._train_x[:cut]
                    del self._train_y[:cut]
                self._rows_since_fit += 1
            self._pending_x = None

            # 3) rolling state update with this candle
            self._update_state(t, o, h, lo, c, body, color)

            # 4) refit when due (bulk replay: sparser)
            due = BULK_REFIT_EVERY if bulk else REFIT_EVERY
            if (np is not None and self._rows_since_fit >= due
                    and len(self._train_x) >= FIT_MIN_ROWS):
                self._fit_local()

            # 5) feature row → prediction for the NEXT candle
            self._current = self._build_prediction(t, c)
            return self._current

    def ingest_replace(self, candles: List[dict]) -> Optional[dict]:
        """RESET all state and replay `candles` from scratch.

        Used by the deep history seed: the base seed already ingested the
        last ~200 candles, and the deep window OVERLAPS those timestamps —
        plain ingest would skip everything (idempotency) and never build
        the deeper training window.  One coherent replay of the deep
        window is strictly better (it is a superset in time).  Replay
        re-populates the grading stats over the deep window too.
        """
        lock = self.lock
        asset, period = self.asset, self.period
        self.__init__(asset, period)
        self.lock = lock
        return self.ingest_many(candles)

    def ingest_many(self, candles: List[dict]) -> Optional[dict]:
        """Bulk replay (broker history seed).  Sparse refits during replay,
        one final fit + payload rebuild at the end so the current
        prediction reflects the freshly fitted local model."""
        payload = None
        last_tc: Optional[Tuple[int, float]] = None
        for c in candles or []:
            p = self.ingest(c, bulk=True)
            if p is not None:
                payload = p
        if self.last_time > 0 and self.prev_close is not None:
            last_tc = (self.last_time, self.prev_close)
        if np is not None and len(self._train_x) >= FIT_MIN_ROWS:
            with self.lock:
                self._fit_local()
                if last_tc is not None:
                    self._current = self._build_prediction(*last_tc)
                    payload = self._current
        return payload

    # ────────────────────────── state ────────────────────────
    def _update_state(self, t: int, o: float, h: float, lo: float,
                      c: float, body: float, color: int) -> None:
        # true range (uses previous close)
        if self.prev_close is not None:
            tr = max(h - lo, abs(h - self.prev_close),
                     abs(lo - self.prev_close))
        else:
            tr = h - lo
        self.tr14.append(tr)
        self.tr5.append(tr)
        self.tr50.append(tr)

        # HTF 5m bucket: crossing a 5m boundary ⇒ the PREVIOUS 1m close
        # completed a 5m candle  (times are SECONDS — repo/Quotex convention)
        if self._htf_bucket_ms is None:
            self._htf_bucket_ms = t - (t % 300)
        elif (t - self._htf_bucket_ms >= 300
                  and self._htf_prev_close is not None):
            fc = self._htf_prev_close
            self._htf_e9 = fc if self._htf_e9 is None else \
                0.2 * fc + 0.8 * self._htf_e9
            self._htf_e21 = fc if self._htf_e21 is None else \
                (2.0 / 22.0) * fc + (1.0 - 2.0 / 22.0) * self._htf_e21
            self._htf_c21.append(fc)
            self._htf_count += 1
            while t - self._htf_bucket_ms >= 300:
                self._htf_bucket_ms += 300
        self._htf_prev_close = c

        self.prev_close = c
        self.closes.append(c)
        self.c50.append(c)

        rng = max(h - lo, 0.0)
        self.bodies3.append(body)
        self.bodies10.append(body)
        if len(self.bodies10) == 10:
            self.press10_hist.append(sum(self.bodies10))

        self.hi20.append(h)
        self.lo20.append(lo)
        self.upw3.append(h - max(o, c))
        self.dnw3.append(min(o, c) - lo)
        self.rng3.append(rng)

        self.ema9 = c if self.ema9 is None else 0.2 * c + 0.8 * self.ema9
        self.ema21 = c if self.ema21 is None else \
            (2.0 / 22.0) * c + (1.0 - 2.0 / 22.0) * self.ema21

        # last-candle anatomy
        self._last_clv = (2.0 * c - h - lo) / rng if rng > _EPS else 0.0
        self._last_color = color

        # carried color (doji inherits previous) — drives patterns
        carried = color if color != 0 else self._prev_carried
        self._prev_carried = carried

        # streak over ACTUAL colors (doji resets, research parity)
        if color != 0 and color == self._prev_actual:
            self.streak = self.streak + color
        elif color != 0:
            self.streak = float(color)
        else:
            self.streak = 0.0
        self._prev_actual = color

        # pattern-transition counts (causal): the outcome for the pattern
        # ending at the PREVIOUS candle is THIS candle's actual color.
        if color != 0:
            if len(self.carried3) >= 2:
                # carried3 still holds [c_{i-2}, c_{i-1}] here (pre-append)
                c_im1 = self.carried3[-1]
                c_im2 = self.carried3[-2]
                d = self.cnt2.setdefault((c_im2, c_im1), [0, 0])
                if color > 0:
                    d[0] += 1
                else:
                    d[1] += 1
            if len(self._carried_hist) >= 3:
                # hist holds [c_{i-3}, c_{i-2}, c_{i-1}] (pre-append)
                c_im1 = self._carried_hist[-1]
                c_im2 = self._carried_hist[-2]
                c_im3 = self._carried_hist[-3]
                d3 = self.cnt3.setdefault((c_im3, c_im2, c_im1), [0, 0])
                if color > 0:
                    d3[0] += 1
                else:
                    d3[1] += 1

        self.carried3.append(carried)
        self._carried_hist.append(carried)

    # ────────────────────────── features ─────────────────────
    def _feature_row(self, t: int, c: float) -> Optional[List[float]]:
        if (len(self.closes) < 61 or len(self.tr14) < 14
                or len(self.tr50) < 50 or len(self.bodies3) < 3
                or len(self.carried3) < 3):
            return None
        atr14 = sum(self.tr14) / 14.0
        atr = max(atr14, abs(c) * 1e-9, _EPS)
        atr5 = sum(self.tr5) / 5.0
        atr50 = sum(self.tr50) / 50.0

        b1 = _clip(self.bodies3[-1] / atr, -4, 4)
        b2 = _clip(self.bodies3[-2] / atr, -4, 4)
        b3 = _clip(self.bodies3[-3] / atr, -4, 4)

        press3 = _clip((sum(self.bodies3) / 3.0) / atr, -4, 4)
        p10z = 0.0
        if len(self.press10_hist) >= 200:
            ph = list(self.press10_hist)          # exactly 200 values
            m = sum(ph) / len(ph)
            sd = math.sqrt(sum((v - m) ** 2 for v in ph) / len(ph))
            if sd > _EPS:
                p10z = _clip((ph[-1] - m) / sd, -3, 3)

        r_last = self.rng3[-1]
        uw1 = self.upw3[-1] / r_last if r_last > _EPS else 0.0
        lw1 = self.dnw3[-1] / r_last if r_last > _EPS else 0.0
        su, sl, sr = sum(self.upw3), sum(self.dnw3), sum(self.rng3)
        w3 = (su - sl) / sr if sr > _EPS else 0.0

        hi = max(self.hi20)
        lo20v = min(self.lo20)
        rw = hi - lo20v
        rp = (c - lo20v) / rw - 0.5 if rw > _EPS else 0.0

        ret5 = _clip((c - self.closes[-6]) / atr, -6, 6)
        ret20 = _clip((c - self.closes[-21]) / atr, -6, 6)
        ret60 = _clip((c - self.closes[-61]) / atr, -8, 8)
        eg = _clip((self.ema9 - self.ema21) / atr, -4, 4)
        arat = _clip((atr5 / atr50) - 1.0, -0.8, 4.0) if atr50 > _EPS else 0.0

        st50 = _clip((c - (sum(self.c50) / len(self.c50))) / atr, -6, 6) \
            if len(self.c50) == 50 else 0.0

        htf = 0.0
        if self._htf_count >= 25 and self._htf_e9 is not None:
            cl = list(self._htf_c21)
            m = sum(cl) / len(cl)
            sd = math.sqrt(sum((v - m) ** 2 for v in cl) / len(cl))
            if sd > _EPS:
                htf = _clip((self._htf_e9 - self._htf_e21) / sd, -4, 4)

        hod = (t % 86400) / 86400.0     # t is in SECONDS (repo convention)
        hsin = math.sin(2.0 * math.pi * hod)
        hcos = math.cos(2.0 * math.pi * hod)

        c3 = list(self.carried3)                  # [c_{i-2}, c_{i-1}, c_i]
        pat = "".join("g" if v > 0 else "r" for v in c3)
        pat_oh = [0.0] * 8
        if pat in _PATTERNS:
            pat_oh[_PATTERNS.index(pat)] = 1.0

        # empirical transition probabilities for the pattern ending NOW
        k2 = (c3[-2], c3[-1])
        g, r_ = self.cnt2.get(k2, [0, 0])
        p2 = (g + 30.0) / (g + r_ + 60.0) - 0.5
        k3 = (c3[0], c3[1], c3[2])
        g3, r3 = self.cnt3.get(k3, [0, 0])
        base = p2 + 0.5
        p3 = (g3 + base * 150.0) / (g3 + r3 + 150.0) - 0.5

        # same2 over ACTUAL colors (doji → 0, research parity): the last
        # two actual colors are equal ⟺ |streak| ≥ 2 with a non-doji last
        if self._last_color == 0:
            same2 = 0.0
        else:
            same2 = 1.0 if abs(self.streak) >= 2 else -1.0

        return [
            float(self._last_color), _clip(self.streak, -8, 8),
            b1, b2, b3, _clip(b1 - b2, -6, 6),
            press3, p10z,
            _clip(uw1 - lw1, -1, 1), _clip(w3, -1, 1),
            _clip(self._last_clv, -1, 1), _clip(rp, -0.5, 0.5),
            ret5, ret20, ret60, eg, same2, arat,
            abs(b1), abs(b2), _clip(r_last / atr, 0.0, 5.0), st50, htf,
            hsin, hcos,
            _clip(p2, -0.35, 0.35), _clip(p3, -0.35, 0.35),
            *pat_oh,
        ]

    # ────────────────────────── prediction ───────────────────
    def _build_prediction(self, t: int, c: float) -> Optional[dict]:
        x = self._feature_row(t, c)
        if x is None:
            return None
        self._pending_x = x

        p_local = None
        if self.local_model is not None and np is not None:
            p_local = self.local_model.predict_proba_row(x)
        p_global = None
        if _GLOBAL_MODEL is not None:
            p_global = _GLOBAL_MODEL.predict_proba_row(x)

        n = len(self._train_x)
        if p_local is not None and p_global is not None:
            w = n / (n + BLEND_K)
            p_raw = w * p_local + (1.0 - w) * p_global
            state = "LOCAL" if w >= 0.7 else "BLENDED"
        elif p_local is not None:
            p_raw, state = p_local, "LOCAL"
        elif p_global is not None:
            p_raw, state = p_global, "PRIOR"
        else:
            return None
        w_blend = n / (n + BLEND_K) if p_local is not None else 0.0

        p_cal = 0.5 + (p_raw - 0.5) * CALIB_SLOPE
        direction = "CALL" if p_cal >= 0.5 else "PUT"
        conf = 50.0 + abs(p_cal - 0.5) * 100.0

        # factor contributions (standardized w·z, signed toward GREEN)
        factors = []
        mf = self.local_model if (self.local_model is not None
                                  and self.local_model.w is not None) \
            else _GLOBAL_MODEL
        if mf is not None and mf.w is not None:
            for j, name in enumerate(FEATURE_NAMES):
                sd = mf.sd[j] if abs(mf.sd[j]) > 1e-9 else 1.0
                z = (x[j] - mf.mu[j]) / sd
                factors.append({
                    "name": name,
                    "label": FACTOR_LABELS.get(name, name),
                    "value": round(float(x[j]), 4),
                    "contribution": round(float(mf.w[j] * z), 5),
                })
            factors.sort(key=lambda f: abs(f["contribution"]), reverse=True)
            factors = factors[:6]

        c3 = list(self.carried3)
        pat = "".join("g" if v > 0 else "r" for v in c3) if len(c3) == 3 else ""
        k2 = (c3[-2], c3[-1]) if len(c3) >= 2 else None
        g2 = self.cnt2.get(k2, [0, 0]) if k2 else [0, 0]
        k3 = tuple(c3) if len(c3) == 3 else None
        g3c = self.cnt3.get(k3, [0, 0]) if k3 else [0, 0]

        return {
            "engine": "CSE",
            "version": "next_candle_v1",
            "target_time": t + self.period,   # SECONDS (repo convention)
            "direction": direction,
            "p_green": round(p_cal, 4),
            "p_green_raw": round(p_raw, 4),
            "confidence": int(round(conf)),
            "pattern": pat,
            "pattern_emoji": "".join(
                "🟢" if v > 0 else "🔴" for v in c3) if len(c3) == 3 else "",
            "streak": int(self.streak),
            "state": state,
            "n_train": n,
            "blend_w": round(w_blend, 3),
            "p_local": round(p_local, 4) if p_local is not None else None,
            "p_global": round(p_global, 4) if p_global is not None else None,
            "factors": factors,
            "pattern_stats": {
                "p2_n": g2[0] + g2[1],
                "p2_green_rate": round(g2[0] / (g2[0] + g2[1]), 3)
                if (g2[0] + g2[1]) > 0 else None,
                "p3_n": g3c[0] + g3c[1],
                "p3_green_rate": round(g3c[0] / (g3c[0] + g3c[1]), 3)
                if (g3c[0] + g3c[1]) > 0 else None,
            },
            "stats": self.stats_public(),
            "ts": time.time(),
        }

    def current(self) -> Optional[dict]:
        return self._current

    def stats_public(self) -> dict:
        s = self.stats
        last20 = list(s["last20"])
        return {
            "total": s["total"],
            "correct": s["correct"],
            "accuracy": round(s["correct"] / s["total"], 4)
            if s["total"] else None,
            "last20_accuracy": round(
                sum(1 for v in last20 if v) / len(last20), 4)
            if last20 else None,
            "current_wrong_streak": s["cur_wrong"],
            "max_wrong_streak": s["max_wrong"],
            "skipped_doji": s["skipped_doji"],
        }

    # ────────────────────────── grading ──────────────────────
    def _grade(self, t: int, o: float, c: float) -> None:
        pred = self._current
        if not pred or pred.get("direction") not in ("CALL", "PUT"):
            return
        if int(pred.get("target_time") or 0) != t:
            return  # prediction was for a different (missed) candle
        if c == o:
            self.stats["skipped_doji"] += 1
            return
        actual_green = c > o
        ok = (pred["direction"] == "CALL") == actual_green
        self.stats["total"] += 1
        if ok:
            self.stats["correct"] += 1
            self.stats["cur_wrong"] = 0
        else:
            self.stats["cur_wrong"] += 1
            self.stats["max_wrong"] = max(self.stats["max_wrong"],
                                          self.stats["cur_wrong"])
        self.stats["last20"].append(ok)

    # ────────────────────────── local fit ─────────────────────
    def _fit_local(self) -> None:
        try:
            m = _Logistic()
            m.fit(self._train_x, self._train_y)
            self.local_model = m
            self.n_train = len(self._train_x)
            self._rows_since_fit = 0
        except Exception as exc:
            print(f"[next-candle] local fit failed for {self.asset}: {exc}")


# ──────────────────── module-level registry ────────────────────
_ENGINES: Dict[Tuple[str, int], NextCandleEngine] = {}
_ENGINES_LOCK = threading.Lock()


def get_engine(asset: str, period: int) -> NextCandleEngine:
    key = (asset, int(period))
    with _ENGINES_LOCK:
        eng = _ENGINES.get(key)
        if eng is None:
            eng = NextCandleEngine(asset, int(period))
            _ENGINES[key] = eng
        return eng


def enabled() -> bool:
    """The ONE-strategy engine is THE signal source unless explicitly
    disabled (QX_NEXT_CANDLE=0 → legacy chain rollback)."""
    return os.environ.get("QX_NEXT_CANDLE", "1") == "1"
