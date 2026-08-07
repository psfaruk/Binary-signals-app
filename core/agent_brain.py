"""
core/agent_brain.py — AUTONOMOUS AI AGENT V2 (PROD-AGENT-V2-2026-08-06)

UPGRADES FROM V1
================
V1 had bugs:
  - gap_vs_prev_close always 0 (timestamp reconstruction was wrong)
  - tick_rate always 0 (only computed at signal time, not from buffer)
  - candle_position always 1 (signals evaluated at candle end)
  - Weights stayed tiny (±0.16) — learning rate too small for the loss
  - P(win) calibration was poor (P=0.65 → actual 40% win)
  - Verdict accuracy flat at 47-51% (no edge)

V2 FIXES + UPGRADES:
  1. 12 features (up from 8) — added 4 new high-signal features
  2. Hidden layer (8 neurons) — true neural network, not just logistic regression
  3. Higher learning rate (0.15) with momentum (0.9) for faster convergence
  4. L2 regularization to prevent overfitting
  5. Feature normalization with running mean/std (online)
  6. Better verdict thresholds based on calibrated P(win)
  7. Confidence boost for STRONG signals (P>=0.70)
  8. Per-direction models (CALL model + PUT model per asset)
  9. Looks at BOTH current candle ticks AND previous candle ticks
  10. Time-of-day feature (some hours are traps, some are gold)

NEW FEATURES (12 total):
  f1:  tick_momentum_30     — velocity of last 30 ticks (ATR/sec)
  f2:  tick_momentum_10     — velocity of last 10 ticks (recent, faster)
  f3:  tick_acceleration    — 2nd derivative
  f4:  order_flow_30        — uptick ratio (last 30 ticks)
  f5:  order_flow_10        — uptick ratio (last 10 ticks, recent)
  f6:  micro_volatility_3s  — 3-sec price range / ATR
  f7:  micro_volatility_10s — 10-sec price range / ATR
  f8:  htf_alignment        — 5-min vs 20-min EMA separation
  f9:  gap_vs_prev_close    — current price vs prev close (FIXED)
  f10: tick_rate            — ticks/sec from buffer (FIXED)
  f11: candle_body_pct      — last candle body / range (momentum)
  f12: hour_utc_normalized  — time of day (trap hours = negative)

ARCHITECTURE
============
  Input: 12 features
  Hidden: 8 neurons (ReLU activation)
  Output: 1 neuron (sigmoid → P(win))

  Forward:  h = ReLU(W1 · x + b1)
            p = sigmoid(W2 · h + b2)
  Backward: standard backprop with cross-entropy loss
"""
import os
import time
import math
import random
import threading
from typing import Optional, Dict, Any, List, Tuple
from collections import deque, defaultdict


# ── Configuration ────────────────────────────────────────────────────────────

TICK_BUFFER_SIZE = 1000
THOUGHT_BUFFER_SIZE = 200
# Rolling window of graded decisions used to measure the model's own edge.
OOS_WINDOW = 300
LEARNING_RATE = 0.03          # lower = more stable learning
MOMENTUM = 0.85
L2_REGULARIZATION = 0.01      # higher = less overfitting
DROPOUT_PROB = 0.2            # 20% dropout during training
MIN_LEARNING_SAMPLES = 10     # collect more data before learning
HIDDEN_NEURONS = 8
NUM_FEATURES = 12

# Verdict thresholds (calibrated for actual signal distribution)
# Most signals hover around 50% — only extreme P(win) is meaningful
P_STRONG_CONFIRM = 0.72   # P >= 0.72 → CONFIRM (×1.2) — very high confidence only
P_CONFIRM = 0.62          # 0.62-0.72 → CONFIRM (×1.1)
P_PASS_HIGH = 0.55        # 0.55-0.62 → PASS (slight lean correct)
P_PASS_LOW = 0.45         # 0.45-0.55 → PASS (neutral)
P_WEAKEN = 0.38           # 0.38-0.45 → WEAKEN (×0.5)
                          # P < 0.38 → VETO (kill)

# ── Authority gate (AGENT-GATE-2026-08-06) ──────────────────────────────────
# The agent does NOT get to override the engine just because an env var says
# so. It has to demonstrate, out-of-sample, that its P(win) actually ranks
# winners above losers. Measured by AUC over the last OOS_WINDOW graded
# decisions for that asset.
#
# Why this exists: a walk-forward backtest over 2,569 real graded signals from
# the production DB (scripts/backtest_agent.py) measured the V2 net's AUC at
# 0.5015 (starved tick feed, i.e. as deployed) and 0.5087 (tick feed fixed) —
# both statistically indistinguishable from 0.50 = no information. Despite
# that, QX_AGENT_FINAL=1 was live and the agent had flipped 117 signals and
# neutralised 248 in its first 47 minutes, on a randomly-initialised net that
# had seen ~20 samples per asset. The gate makes authority something the model
# earns from measured performance rather than something a deploy toggle grants.
AGENT_GATE_ENABLED = os.environ.get("QX_AGENT_GATE", "1") == "1"
# Minimum graded decisions for that asset before any authority is considered.
GATE_MIN_SAMPLES = int(os.environ.get("QX_AGENT_GATE_MIN_SAMPLES", "150"))
# The gate tests the LOWER 95% confidence bound of AUC, not the point
# estimate. At n=150 the standard error of AUC is ~0.048, so a raw "AUC >=
# 0.55" rule passes roughly 1-in-8 edgeless assets by luck — with 24 assets
# being tested that is ~3 false authorisations per run, which is exactly what
# a naive threshold produced here. Requiring the lower bound to clear 0.52
# means the asset has to be measurably better than a coin flip, not just
# numerically ahead of it on the day.
GATE_MIN_AUC = float(os.environ.get("QX_AGENT_GATE_MIN_AUC", "0.52"))
# Flush learned weights to the DB every N learn() calls.
SAVE_EVERY_N_LEARNS = int(os.environ.get("QX_AGENT_SAVE_EVERY", "20"))

# Trap hours (UTC) — historically low win rate
TRAP_HOURS = {3, 8, 9, 11, 16, 18, 22}
BOOST_HOURS = {4, 23, 14, 6, 10, 15, 17, 21, 12}

FEATURE_NAMES = [
    "tick_mom_30", "tick_mom_10", "tick_accel", "order_flow_30",
    "order_flow_10", "micro_vol_3s", "micro_vol_10s", "htf_align",
    "gap_prev_close", "tick_rate", "candle_body_pct", "hour_normalized",
]

# Indices of features whose SIGN encodes direction. These get multiplied by
# -1 when scoring a PUT, so one network can score both directions.
# f1 mom30, f2 mom10, f3 accel, f4 flow30, f5 flow10, f8 htf, f9 gap, f11 body.
_DIR_INDICES = frozenset({0, 1, 2, 3, 4, 7, 8, 10})


def _relu(x: float) -> float:
    return max(0.0, x)

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-15, min(15, x))))

def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class AssetModel:
    """Per-asset neural network with online learning."""

    def __init__(self, asset: str):
        self.asset = asset
        # Neural network weights
        # W1: [HIDDEN_NEURONS][NUM_FEATURES], b1: [HIDDEN_NEURONS]
        # W2: [HIDDEN_NEURONS], b2: scalar
        # Xavier initialization
        limit1 = math.sqrt(6.0 / (NUM_FEATURES + HIDDEN_NEURONS))
        self.W1 = [[random.uniform(-limit1, limit1) for _ in range(NUM_FEATURES)]
                   for _ in range(HIDDEN_NEURONS)]
        self.b1 = [0.0] * HIDDEN_NEURONS
        limit2 = math.sqrt(6.0 / (HIDDEN_NEURONS + 1))
        self.W2 = [random.uniform(-limit2, limit2) for _ in range(HIDDEN_NEURONS)]
        self.b2 = 0.0

        # Momentum (for gradient descent with momentum)
        self.vW1 = [[0.0] * NUM_FEATURES for _ in range(HIDDEN_NEURONS)]
        self.vb1 = [0.0] * HIDDEN_NEURONS
        self.vW2 = [0.0] * HIDDEN_NEURONS
        self.vb2 = 0.0

        # Tick buffer
        self.ticks = deque(maxlen=TICK_BUFFER_SIZE)

        # Online normalization stats.
        # BUG-FIX (AGENT-NORM-2026-08-06): feat_count used to be a single
        # shared int incremented once per FEATURE, so after one 12-feature
        # extraction it read 12 instead of 1. Every feature's Welford
        # denominator was therefore inflated by ~12x and mixed with the other
        # features' update counts, making every computed std meaningless — and
        # std is the divisor that produces the normalised input the network
        # actually sees. Now one independent counter per feature.
        self.feat_mean = [0.0] * NUM_FEATURES
        self.feat_m2 = [0.0] * NUM_FEATURES  # for variance
        self.feat_count = [0] * NUM_FEATURES

        # Learning stats
        self.samples = 0
        self.correct_predictions = 0
        self.total_loss = 0.0
        self.last_features_raw = [0.0] * NUM_FEATURES
        self.last_features_norm = [0.0] * NUM_FEATURES
        self.last_p_win = 0.5
        self.last_hidden = [0.0] * HIDDEN_NEURONS

        # Recent predictions for accuracy tracking.
        # Entries are (p_win, was_correct) TUPLES — not dicts. Anything reading
        # this deque must unpack, never call .get() on an element.
        self.recent_predictions = deque(maxlen=OOS_WINDOW)

        # Per-asset verdict counters. get_status()'s per_pair block read these
        # via getattr(..., 0) before they existed, so every per-pair verdict
        # count in the UI was a hardcoded zero presented as live data.
        self.veto_count = 0
        self.confirm_count = 0
        self.weaken_count = 0
        self.pass_count = 0
        self.flip_count = 0
        self.neutral_count = 0
        self.last_decision_at = 0.0   # epoch seconds of this asset's last verdict

        # Pending signals awaiting their outcome, keyed by (period, ctime).
        # BUG-FIX (AGENT-PENDING-2026-08-06): this used to be a single slot per
        # asset. Two streams on the same asset (e.g. 60s and 120s) overwrote
        # each other's pending features, so the net trained feature vector A
        # against outcome B. Keyed storage keeps each decision paired with its
        # own outcome.
        self.pending: Dict[tuple, Dict] = {}

    def add_tick(self, ts_ms: float, price: float):
        self.ticks.append((ts_ms, price))

    def _normalize_feature(self, i: int, val: float, update: bool = True) -> float:
        """Scale-normalise feature `i` by its running standard deviation.

        Deliberately scale-only (val/std), NOT a full z-score: subtracting the
        mean would destroy the sign symmetry that forward() relies on when it
        flips directional features for PUT evaluation.
        """
        if update:
            self.feat_count[i] += 1
            delta = val - self.feat_mean[i]
            self.feat_mean[i] += delta / self.feat_count[i]
            self.feat_m2[i] += delta * (val - self.feat_mean[i])
        n = self.feat_count[i]
        if n < 2:
            return val  # not enough data
        variance = self.feat_m2[i] / (n - 1)
        std = math.sqrt(max(1e-12, variance))
        return val / max(0.1, std)  # don't divide by tiny std

    def extract_features(self, closed_candles: List[Dict]) -> List[float]:
        """Extract 12 features from tick buffer + closed candles."""
        if len(self.ticks) < 10:
            self.last_features_raw = [0.0] * NUM_FEATURES
            return [0.0] * NUM_FEATURES

        ticks = list(self.ticks)
        now_ms = ticks[-1][0]

        # ATR from closed candles
        atr = 0.0001
        if len(closed_candles) >= 2:
            trs = []
            recent = closed_candles[-20:] if len(closed_candles) >= 20 else closed_candles
            for i in range(1, len(recent)):
                c, prev = recent[i], recent[i-1]
                tr = max(c["high"]-c["low"], abs(c["high"]-prev["close"]), abs(c["low"]-prev["close"]))
                trs.append(tr)
            atr = sum(trs)/len(trs) if trs else 0.0001

        # f1: tick_momentum_30 — velocity over last 30 ticks (ATR/sec)
        recent_30 = ticks[-30:] if len(ticks) >= 30 else ticks
        dt_30 = (recent_30[-1][0] - recent_30[0][0]) / 1000.0
        f1 = (recent_30[-1][1] - recent_30[0][1]) / max(0.001, dt_30) / atr * 2.0 if dt_30 > 0 else 0.0
        f1 = _clamp(f1)

        # f2: tick_momentum_10 — velocity over last 10 ticks (faster signal)
        recent_10 = ticks[-10:] if len(ticks) >= 10 else ticks
        dt_10 = (recent_10[-1][0] - recent_10[0][0]) / 1000.0
        f2 = (recent_10[-1][1] - recent_10[0][1]) / max(0.001, dt_10) / atr * 2.0 if dt_10 > 0 else 0.0
        f2 = _clamp(f2)

        # f3: tick_acceleration — 2nd derivative
        mid = len(recent_30) // 2
        if mid > 0:
            dt1 = (recent_30[mid][0] - recent_30[0][0]) / 1000.0
            dt2 = (recent_30[-1][0] - recent_30[mid][0]) / 1000.0
            v1 = (recent_30[mid][1] - recent_30[0][1]) / max(0.001, dt1) / atr if dt1 > 0 else 0
            v2 = (recent_30[-1][1] - recent_30[mid][1]) / max(0.001, dt2) / atr if dt2 > 0 else 0
            f3 = _clamp((v2 - v1) * 2.0)
        else:
            f3 = 0.0

        # f4: order_flow_30 — uptick ratio over last 30 ticks
        up = down = 0
        for i in range(1, len(recent_30)):
            if recent_30[i][1] > recent_30[i-1][1]: up += 1
            elif recent_30[i][1] < recent_30[i-1][1]: down += 1
        total = up + down
        f4 = (up / total - 0.5) * 2.0 if total > 0 else 0.0

        # f5: order_flow_10 — uptick ratio over last 10 ticks (recent)
        up10 = down10 = 0
        for i in range(1, len(recent_10)):
            if recent_10[i][1] > recent_10[i-1][1]: up10 += 1
            elif recent_10[i][1] < recent_10[i-1][1]: down10 += 1
        total10 = up10 + down10
        f5 = (up10 / total10 - 0.5) * 2.0 if total10 > 0 else 0.0

        # f6: micro_volatility_3s — 3-sec price range / ATR
        cutoff_3s = now_ms - 3000
        recent_3s = [p for t, p in ticks if t >= cutoff_3s]
        if len(recent_3s) >= 2:
            f6 = _clamp((max(recent_3s) - min(recent_3s)) / atr * 1.5 - 0.3)
        else:
            f6 = 0.0

        # f7: micro_volatility_10s — 10-sec price range / ATR
        cutoff_10s = now_ms - 10000
        recent_10s = [p for t, p in ticks if t >= cutoff_10s]
        if len(recent_10s) >= 2:
            f7 = _clamp((max(recent_10s) - min(recent_10s)) / atr - 0.3)
        else:
            f7 = 0.0

        # f8: htf_alignment — 5-min vs 20-min EMA separation
        if len(closed_candles) >= 20:
            last5_avg = sum(c["close"] for c in closed_candles[-5:]) / 5
            last20_avg = sum(c["close"] for c in closed_candles[-20:]) / 20
            sep = (last5_avg - last20_avg) / max(1e-9, last20_avg)
            f8 = _clamp(sep * 100)
        else:
            f8 = 0.0

        # f9: gap_vs_prev_close — current price vs prev close (FIXED)
        if closed_candles and atr > 0:
            prev_close = closed_candles[-1]["close"]
            cur_price = ticks[-1][1]
            gap = (cur_price - prev_close) / atr
            f9 = _clamp(gap * 2.0)
        else:
            f9 = 0.0

        # f10: tick_rate — ticks/sec from last 5 sec of buffer (FIXED)
        cutoff_5s = now_ms - 5000
        recent_5s_count = sum(1 for t, _ in ticks if t >= cutoff_5s)
        rate = recent_5s_count / 5.0
        if rate < 2:
            f10 = -1.0
        elif rate > 20:
            f10 = -0.5
        elif 5 <= rate <= 10:
            f10 = 0.5
        else:
            f10 = 0.0

        # f11: candle_body_pct — last candle body / range (momentum)
        if closed_candles:
            last = closed_candles[-1]
            rng = max(1e-9, last["high"] - last["low"])
            body = last["close"] - last["open"]
            f11 = _clamp(body / rng * 2.0)
        else:
            f11 = 0.0

        # f12: hour_utc_normalized — trap hours negative, boost hours positive
        try:
            hour = int((now_ms / 1000) % 86400 // 3600)
        except:
            hour = 12
        if hour in TRAP_HOURS:
            f12 = -1.0
        elif hour in BOOST_HOURS:
            f12 = 0.5
        else:
            f12 = 0.0

        features_raw = [f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12]
        self.last_features_raw = features_raw

        # Normalize features (online)
        features_norm = []
        for i, val in enumerate(features_raw):
            features_norm.append(_clamp(self._normalize_feature(i, val)))
        self.last_features_norm = features_norm
        return features_norm

    def forward(self, features: List[float], signal: str) -> Tuple[float, List[float]]:
        """Forward pass through neural network.

        For PUT signals, flip directional features (f1, f2, f4, f5, f8, f9, f11).
        Returns (p_win, hidden_activations).
        """
        # Directional feature adjustment for PUT
        # BUG-FIX (AGENT-DIR-2026-08-06): index 2 (f3 tick_acceleration) is a
        # signed directional quantity but was missing from this set, so PUT
        # evaluations kept the CALL-oriented sign for it. Added.
        dir_indices = _DIR_INDICES  # f1,f2,f3,f4,f5,f8,f9,f11
        sign = 1.0 if signal == "CALL" else -1.0
        adjusted = []
        for i, f in enumerate(features):
            if i in dir_indices:
                adjusted.append(f * sign)
            else:
                adjusted.append(f)

        # Hidden layer: ReLU
        hidden = [0.0] * HIDDEN_NEURONS
        for j in range(HIDDEN_NEURONS):
            z = self.b1[j]
            for i in range(NUM_FEATURES):
                z += self.W1[j][i] * adjusted[i]
            hidden[j] = _relu(z)

        # Output layer: sigmoid
        z_out = self.b2
        for j in range(HIDDEN_NEURONS):
            z_out += self.W2[j] * hidden[j]
        p = _sigmoid(z_out)

        self.last_hidden = hidden
        self.last_p_win = p
        return p, hidden

    def predict(self, features: List[float], signal: str) -> float:
        p, _ = self.forward(features, signal)
        return p

    def learn(self, features: List[float], signal: str, was_correct: bool,
              p_at_decision: Optional[float] = None):
        """Backpropagation with momentum + L2 regularization + dropout.

        `p_at_decision` is the calibrated P(win) the agent actually published
        when it made the call. Recording that — rather than re-running a
        forward pass here — is what makes recent_predictions a genuinely
        out-of-sample record, which edge_auc()/the authority gate depend on.
        """
        self.samples += 1
        p_pred = (p_at_decision if p_at_decision is not None
                  else self.predict(features, signal))
        self.recent_predictions.append((p_pred, was_correct))
        if was_correct:
            self.correct_predictions += 1

        if self.samples < MIN_LEARNING_SAMPLES:
            return

        # Re-do forward pass with DROPOUT to get hidden activations
        # Dropout: randomly disable 20% of hidden neurons during training
        # This prevents overfitting and improves calibration
        dropout_mask = [1.0 if random.random() > DROPOUT_PROB else 0.0
                        for _ in range(HIDDEN_NEURONS)]

        # Directional feature adjustment (see _DIR_INDICES note in forward()).
        dir_indices = _DIR_INDICES
        sign = 1.0 if signal == "CALL" else -1.0
        adjusted = []
        for i, f in enumerate(features):
            if i in dir_indices:
                adjusted.append(f * sign)
            else:
                adjusted.append(f)

        # Forward pass with dropout
        hidden = [0.0] * HIDDEN_NEURONS
        for j in range(HIDDEN_NEURONS):
            if dropout_mask[j] == 0.0:
                continue  # dropped out
            z = self.b1[j]
            for i in range(NUM_FEATURES):
                z += self.W1[j][i] * adjusted[i]
            hidden[j] = _relu(z)

        # Output (scale by 1/(1-dropout) to compensate for dropped neurons)
        scale = 1.0 / (1.0 - DROPOUT_PROB)
        z_out = self.b2
        for j in range(HIDDEN_NEURONS):
            z_out += self.W2[j] * hidden[j] * scale
        p = _sigmoid(z_out)

        y = 1.0 if was_correct else 0.0
        error = p - y

        # Backpropagation
        dW2 = [error * hidden[j] * scale + L2_REGULARIZATION * self.W2[j]
               for j in range(HIDDEN_NEURONS)]
        db2 = error

        dW1 = [[0.0] * NUM_FEATURES for _ in range(HIDDEN_NEURONS)]
        db1 = [0.0] * HIDDEN_NEURONS
        for j in range(HIDDEN_NEURONS):
            if dropout_mask[j] == 0.0:
                continue  # no gradient for dropped neurons
            if hidden[j] > 0:  # ReLU derivative
                grad_h = error * self.W2[j] * scale
                db1[j] = grad_h
                for i in range(NUM_FEATURES):
                    dW1[j][i] = grad_h * adjusted[i] + L2_REGULARIZATION * self.W1[j][i]

        # Update with momentum
        for j in range(HIDDEN_NEURONS):
            self.vW2[j] = MOMENTUM * self.vW2[j] - LEARNING_RATE * dW2[j]
            self.W2[j] += self.vW2[j]
            self.vb1[j] = MOMENTUM * self.vb1[j] - LEARNING_RATE * db1[j]
            self.b1[j] += self.vb1[j]
            for i in range(NUM_FEATURES):
                self.vW1[j][i] = MOMENTUM * self.vW1[j][i] - LEARNING_RATE * dW1[j][i]
                self.W1[j][i] += self.vW1[j][i]
        self.vb2 = MOMENTUM * self.vb2 - LEARNING_RATE * db2
        self.b2 += self.vb2

        # Loss tracking
        loss = -(y * math.log(max(1e-10, p)) + (1-y) * math.log(max(1e-10, 1-p)))
        self.total_loss += loss

    def calibrate_p(self, p: float) -> float:
        """Apply Platt-style calibration to raw P(win).

        Shrinks extreme predictions toward 0.5 to reduce overconfidence.
        Calibrated = 0.5 + (p - 0.5) * shrinkage_factor
        where shrinkage decreases as we have more samples (more confident).
        """
        if self.samples < 20:
            # Not enough data — shrink heavily toward 0.5
            return 0.5 + (p - 0.5) * 0.3
        elif self.samples < 50:
            return 0.5 + (p - 0.5) * 0.5
        elif self.samples < 100:
            return 0.5 + (p - 0.5) * 0.7
        else:
            return 0.5 + (p - 0.5) * 0.85

    def accuracy(self) -> float:
        """Calibration accuracy: how often P>=0.5 matched the realised outcome.

        NOTE this is NOT a win rate — a confident 'this will lose' that does
        lose counts as accurate. Use edge_auc() to judge whether the model can
        actually rank winners above losers.
        """
        if not self.recent_predictions:
            return 0.0
        correct = sum(1 for p, wc in self.recent_predictions if (p >= 0.5) == wc)
        return correct / len(self.recent_predictions)

    def edge_auc(self) -> Optional[float]:
        """Out-of-sample AUC of P(win) over the rolling decision window.

        0.50 = the model's score carries no information about whether the
        trade won. This is the number the authority gate is built on, because
        it is the one thing that distinguishes a model with an edge from a
        random number generator with good self-esteem.
        """
        rows = list(self.recent_predictions)
        pos = [p for p, ok in rows if ok]
        neg = [p for p, ok in rows if not ok]
        if len(pos) < 10 or len(neg) < 10:
            return None
        # Mann-Whitney U with tie-aware mid-ranks.
        ordered = sorted(rows, key=lambda r: r[0])
        rank_sum_pos = 0.0
        i = 0
        while i < len(ordered):
            j = i
            while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
                j += 1
            mid_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                if ordered[k][1]:
                    rank_sum_pos += mid_rank
            i = j + 1
        n1, n0 = len(pos), len(neg)
        return (rank_sum_pos - n1 * (n1 + 1) / 2.0) / (n1 * n0)

    def edge_auc_lower(self) -> Optional[float]:
        """Lower 95% confidence bound on edge_auc (Hanley-McNeil standard error).

        A point estimate alone is not evidence: at n=150 the SE of AUC is
        ~0.048, so an edgeless model clears a raw "AUC >= 0.55" test often
        enough that testing 24 assets produces several passes by chance.
        """
        a = self.edge_auc()
        if a is None:
            return None
        rows = self.recent_predictions
        n1 = sum(1 for _, ok in rows if ok)
        n0 = len(rows) - n1
        if n1 < 2 or n0 < 2:
            return None
        q1 = a / (2.0 - a)
        q2 = 2.0 * a * a / (1.0 + a)
        var = (a * (1 - a)
               + (n1 - 1) * (q1 - a * a)
               + (n0 - 1) * (q2 - a * a)) / (n1 * n0)
        return a - 1.96 * math.sqrt(max(0.0, var))

    def has_authority(self) -> bool:
        """May this model override the engine's signal?"""
        if not AGENT_GATE_ENABLED:
            return True
        if len(self.recent_predictions) < GATE_MIN_SAMPLES:
            return False
        lo = self.edge_auc_lower()
        return lo is not None and lo >= GATE_MIN_AUC

    def gate_reason(self) -> str:
        if not AGENT_GATE_ENABLED:
            return "gate disabled (QX_AGENT_GATE=0)"
        n = len(self.recent_predictions)
        if n < GATE_MIN_SAMPLES:
            return f"gated: {n}/{GATE_MIN_SAMPLES} graded decisions"
        a = self.edge_auc()
        lo = self.edge_auc_lower()
        if a is None or lo is None:
            return "gated: not enough win/loss variety yet"
        if lo < GATE_MIN_AUC:
            return (f"gated: AUC {a:.3f} (95% lower bound {lo:.3f}) "
                    f"< {GATE_MIN_AUC:.2f} — no measured edge")
        return f"authorised: AUC {a:.3f} (lower bound {lo:.3f}) over n={n}"

    def record_verdict(self, verdict: str) -> None:
        """Tally one decision against this asset. Caller holds the agent lock."""
        if verdict == "CONFIRM": self.confirm_count += 1
        elif verdict == "VETO": self.veto_count += 1
        elif verdict == "WEAKEN": self.weaken_count += 1
        elif verdict == "FLIP": self.flip_count += 1
        elif verdict == "NEUTRAL": self.neutral_count += 1
        else: self.pass_count += 1
        self.last_decision_at = time.time()

    def avg_loss(self) -> float:
        if self.samples <= MIN_LEARNING_SAMPLES:
            return 0.0
        return self.total_loss / (self.samples - MIN_LEARNING_SAMPLES)

    def to_dict(self) -> Dict:
        a = self.edge_auc()
        return {
            "asset": self.asset,
            "samples": self.samples,
            "accuracy": round(self.accuracy(), 3),
            "avg_loss": round(self.avg_loss(), 4),
            "last_p_win": round(self.last_p_win, 3),
            "last_features": [round(f, 3) for f in self.last_features_norm],
            "tick_buffer_size": len(self.ticks),
            "hidden_neurons": HIDDEN_NEURONS,
            # Mean absolute input weight per feature — what the UI's
            # "Weights" column has always tried to render. to_dict() never
            # exposed a `weights` key, so that column was permanently blank.
            "weights": [
                round(sum(abs(self.W1[j][i]) for j in range(HIDDEN_NEURONS))
                      / HIDDEN_NEURONS, 3)
                for i in range(NUM_FEATURES)
            ],
            "graded_decisions": len(self.recent_predictions),
            "edge_auc": round(a, 4) if a is not None else None,
            "edge_auc_lower": (round(self.edge_auc_lower(), 4)
                               if self.edge_auc_lower() is not None else None),
            "has_authority": self.has_authority(),
            "gate_reason": self.gate_reason(),
            "pending": len(self.pending),
        }

    # ── Persistence ─────────────────────────────────────────────────────────
    # Without this every Railway redeploy threw away all learned weights and
    # normalisation statistics, and the agent restarted from random Xavier
    # init — while still being allowed (in FINAL mode) to flip live signals.

    def state_dict(self) -> Dict:
        return {
            "v": 2,
            "asset": self.asset,
            "W1": self.W1, "b1": self.b1, "W2": self.W2, "b2": self.b2,
            "feat_mean": self.feat_mean, "feat_m2": self.feat_m2,
            "feat_count": self.feat_count,
            "samples": self.samples,
            "correct_predictions": self.correct_predictions,
            "total_loss": self.total_loss,
            "recent": [[p, 1 if ok else 0] for p, ok in self.recent_predictions],
        }

    def load_state(self, d: Dict) -> None:
        def _shape_ok(v, rows, cols):
            return (isinstance(v, list) and len(v) == rows
                    and all(isinstance(r, list) and len(r) == cols for r in v))
        if not _shape_ok(d.get("W1"), HIDDEN_NEURONS, NUM_FEATURES):
            raise ValueError("W1 shape mismatch — refusing to load stale model")
        if not (isinstance(d.get("W2"), list) and len(d["W2"]) == HIDDEN_NEURONS):
            raise ValueError("W2 shape mismatch — refusing to load stale model")
        self.W1 = [[float(x) for x in row] for row in d["W1"]]
        self.b1 = [float(x) for x in d.get("b1", self.b1)]
        self.W2 = [float(x) for x in d["W2"]]
        self.b2 = float(d.get("b2", 0.0))
        fm = d.get("feat_mean")
        if isinstance(fm, list) and len(fm) == NUM_FEATURES:
            self.feat_mean = [float(x) for x in fm]
        fm2 = d.get("feat_m2")
        if isinstance(fm2, list) and len(fm2) == NUM_FEATURES:
            self.feat_m2 = [float(x) for x in fm2]
        fc = d.get("feat_count")
        if isinstance(fc, list) and len(fc) == NUM_FEATURES:
            self.feat_count = [int(x) for x in fc]
        self.samples = int(d.get("samples", 0))
        self.correct_predictions = int(d.get("correct_predictions", 0))
        self.total_loss = float(d.get("total_loss", 0.0))
        self.recent_predictions = deque(
            ((float(p), bool(ok)) for p, ok in d.get("recent", [])),
            maxlen=OOS_WINDOW)


class AgentBrain:
    """Autonomous AI agent V2 with neural network.

    Three modes:
      - OBSERVE (default): agent processes ticks, evaluates signals, learns,
        BUT does NOT modify signals. Dashboard shows everything live.
      - ACTIVE (QX_AGENT_BRAIN=1): agent can VETO/WEAKEN/CONFIRM the engine's
        signal. Engine's direction is preserved unless agent disagrees strongly.
      - FINAL (QX_AGENT_FINAL=1): AGENT MAKES THE FINAL DECISION.
        Agent evaluates both CALL and PUT, picks whichever has higher P(win).
        Can FLIP the engine's signal (CALL→PUT or PUT→CALL) if opposite
        direction has higher P(win). Engine's signal becomes just a suggestion.
    """

    def __init__(self):
        self.models: Dict[str, AssetModel] = {}
        self.lock = threading.RLock()
        # Three modes (priority: FINAL > ACTIVE > OBSERVE)
        self.final_mode = os.environ.get("QX_AGENT_FINAL", "0") == "1"
        self.active_mode = self.final_mode or os.environ.get("QX_AGENT_BRAIN", "0") == "1"
        self.started_at = time.time()
        self.thoughts = deque(maxlen=THOUGHT_BUFFER_SIZE)
        self.total_ticks_processed = 0
        self.total_signals_evaluated = 0
        self.total_signals_learned = 0
        self.total_veto = 0
        self.total_confirm = 0
        self.total_weaken = 0
        self.total_pass = 0
        # Final mode stats
        self.total_flips = 0       # engine said X, agent flipped to Y
        self.total_kept = 0        # agent kept engine's direction
        self.total_neutralized = 0 # agent said NEUTRAL (no edge either way)
        # Decisions the authority gate demoted back to OBSERVE.
        self.total_gated = 0
        self._learns_since_save = 0
        self._last_save = 0.0
        self.load_state()

    @property
    def enabled(self):
        """Always enabled for observation. Use active_mode/final_mode for control."""
        return True

    def _get_model(self, asset: str) -> AssetModel:
        with self.lock:
            if asset not in self.models:
                self.models[asset] = AssetModel(asset)
            return self.models[asset]

    def _add_thought(self, thought_type: str, asset: str, message: str, data: Dict = None):
        thought = {
            "ts": time.time(),
            "type": thought_type,
            "asset": asset,
            "message": message[:200],
            "data": data or {},
        }
        with self.lock:
            self.thoughts.appendleft(thought)

    def process_tick(self, asset: str, price: float, ts_ms: float):
        # Always process ticks (even in observe mode) so the agent learns
        if not self.enabled:
            return
        try:
            model = self._get_model(asset)
            model.add_tick(ts_ms, price)
            self.total_ticks_processed += 1
            if self.total_ticks_processed % 100 == 0:
                self._add_thought("tick", asset,
                    f"tick #{self.total_ticks_processed}: {asset} @ {price:.5f}",
                    {"price": price, "ts": ts_ms})
        except Exception as e:
            print(f"[agent] process_tick error {asset}: {e}")

    def _forward_ensemble(self, model, features: List[float], signal: str) -> float:
        """Monte Carlo dropout ensemble (5 passes). Returns calibrated P(win)."""
        p_samples = []
        for _ in range(5):
            dropout_mask = [1.0 if random.random() > DROPOUT_PROB else 0.0
                            for _ in range(HIDDEN_NEURONS)]
            dir_indices = _DIR_INDICES
            sign = 1.0 if signal == "CALL" else -1.0
            adjusted = [f * sign if i in dir_indices else f
                        for i, f in enumerate(features)]
            hidden = [0.0] * HIDDEN_NEURONS
            for j in range(HIDDEN_NEURONS):
                if dropout_mask[j] == 0.0:
                    continue
                z = model.b1[j]
                for i in range(NUM_FEATURES):
                    z += model.W1[j][i] * adjusted[i]
                hidden[j] = _relu(z)
            scale = 1.0 / (1.0 - DROPOUT_PROB)
            z_out = model.b2
            for j in range(HIDDEN_NEURONS):
                z_out += model.W2[j] * hidden[j] * scale
            p_samples.append(_sigmoid(z_out))
        p_raw = sum(p_samples) / len(p_samples)
        return model.calibrate_p(p_raw)

    def evaluate(self, prediction: Dict[str, Any], asset: str,
                 closed_candles: List[Dict],
                 period: int = 0, ctime: int = 0) -> Dict[str, Any]:
        """Evaluate a signal. Behavior depends on mode:
           - OBSERVE: returns PASS (no modification), records P(win)
           - ACTIVE: returns VETO/WEAKEN/CONFIRM based on P(win)
           - FINAL: agent makes the FINAL decision, can FLIP signal direction

        ACTIVE and FINAL additionally require the per-asset authority gate to
        pass (see AGENT_GATE_ENABLED). A model with no measured edge always
        falls back to OBSERVE no matter how the env vars are set.
        """
        if not self.enabled:
            return {"verdict": "PASS", "confidence_adjustment": 1.0,
                    "p_win": 0.5, "features": [], "reason": "agent disabled"}

        signal = prediction.get("signal")
        if signal not in ("CALL", "PUT"):
            return {"verdict": "PASS", "confidence_adjustment": 1.0,
                    "p_win": 0.5, "features": [], "reason": "not CALL/PUT"}

        try:
            model = self._get_model(asset)
            features = model.extract_features(closed_candles)
            pkey = (int(period or 0), int(ctime or 0))

            authorised = model.has_authority()
            gate_note = model.gate_reason()
            if not authorised:
                with self.lock:
                    self.total_gated += 1

            # ── FINAL MODE: Agent makes the decision ─────────────────────
            # Evaluate P(win) for BOTH directions, pick the better one.
            if self.final_mode and authorised:
                p_call = self._forward_ensemble(model, features, "CALL")
                p_put = self._forward_ensemble(model, features, "PUT")

                # Decision: pick direction with higher P(win)
                # But only if there's a meaningful edge (>= 0.52)
                # Otherwise → NEUTRAL (no trade)
                FINAL_EDGE_THRESHOLD = 0.52
                NEUTRAL_THRESHOLD = 0.50

                if p_call >= FINAL_EDGE_THRESHOLD and p_call > p_put:
                    final_signal = "CALL"
                    final_p = p_call
                    verdict = "CONFIRM" if signal == "CALL" else "FLIP"
                elif p_put >= FINAL_EDGE_THRESHOLD and p_put > p_call:
                    final_signal = "PUT"
                    final_p = p_put
                    verdict = "CONFIRM" if signal == "PUT" else "FLIP"
                elif p_call < NEUTRAL_THRESHOLD and p_put < NEUTRAL_THRESHOLD:
                    # No edge either way — NEUTRAL
                    final_signal = "NEUTRAL"
                    final_p = max(p_call, p_put)
                    verdict = "VETO"
                else:
                    # Marginal — keep engine's signal but low confidence
                    final_signal = signal
                    final_p = p_call if signal == "CALL" else p_put
                    verdict = "PASS"

                flipped = (verdict == "FLIP")
                neutralized = (final_signal == "NEUTRAL")

                with self.lock:
                    self.total_signals_evaluated += 1
                    if flipped:
                        self.total_flips += 1
                    elif neutralized:
                        self.total_neutralized += 1
                    else:
                        self.total_kept += 1
                    if verdict == "CONFIRM": self.total_confirm += 1
                    elif verdict == "VETO": self.total_veto += 1
                    # BUG-FIX: FLIP used to be counted as a CONFIRM here, so
                    # the dashboard's "✅ CONFIRM" tile silently included every
                    # direction flip.
                    else: self.total_pass += 1
                    model.record_verdict(
                        "FLIP" if flipped else
                        ("NEUTRAL" if neutralized else verdict))

                # Only park a pending entry if an outcome can ever arrive.
                # A NEUTRALised candle is never graded, so storing one just
                # fills the dict with entries nothing will ever consume.
                if final_signal in ("CALL", "PUT"):
                    model.pending[pkey] = {
                        "signal": final_signal,  # learn from FINAL decision
                        "features": list(features),
                        "p_win": final_p,
                        "ts": time.time(),
                    }
                self._prune_pending(model)

                action = "FLIPPED" if flipped else ("NEUTRALIZED" if neutralized else "KEPT")
                reason = (f"FINAL: {action} {signal}→{final_signal} "
                          f"P(call)={p_call:.1%} P(put)={p_put:.1%} "
                          f"samples={model.samples} acc={model.accuracy():.1%}")

                emoji = "🔄" if flipped else ("🚫" if neutralized else "✅")
                print(f"[agent] {emoji} FINAL {action} {asset} {signal}→{final_signal} "
                      f"P(call)={p_call:.1%} P(put)={p_put:.1%}")

                self._add_thought("evaluate", asset,
                    f"FINAL {action}: {signal}→{final_signal} "
                    f"P(call)={p_call:.1%} P(put)={p_put:.1%}",
                    {"verdict": verdict, "p_win": final_p,
                     "p_call": p_call, "p_put": p_put,
                     "features": features, "signal": final_signal,
                     "engine_signal": signal, "flipped": flipped,
                     "neutralized": neutralized})

                # Return verdict that feed.py can apply
                if neutralized:
                    return {
                        "verdict": "VETO",
                        "confidence_adjustment": 0.0,
                        "p_win": final_p,
                        "p_call": p_call, "p_put": p_put,
                        "final_signal": "NEUTRAL",
                        "flipped": False,
                        "features": features,
                        "feature_names": FEATURE_NAMES,
                        "reason": reason,
                        "layers": {},
                    }
                elif flipped:
                    return {
                        "verdict": "FLIP",
                        "confidence_adjustment": 1.0,
                        "p_win": final_p,
                        "p_call": p_call, "p_put": p_put,
                        "final_signal": final_signal,
                        "flipped": True,
                        "features": features,
                        "feature_names": FEATURE_NAMES,
                        "reason": reason,
                        "layers": {},
                    }
                else:
                    return {
                        "verdict": "CONFIRM" if verdict == "CONFIRM" else "PASS",
                        "confidence_adjustment": 1.1 if verdict == "CONFIRM" else 1.0,
                        "p_win": final_p,
                        "p_call": p_call, "p_put": p_put,
                        "final_signal": final_signal,
                        "flipped": False,
                        "features": features,
                        "feature_names": FEATURE_NAMES,
                        "reason": reason,
                        "layers": {},
                    }

            # ── ACTIVE / OBSERVE MODE: Evaluate engine's signal ──────────
            p_win = self._forward_ensemble(model, features, signal)

            if p_win >= P_STRONG_CONFIRM:
                verdict = "CONFIRM"
                conf_mult = 1.2
            elif p_win >= P_CONFIRM:
                verdict = "CONFIRM"
                conf_mult = 1.1
            elif p_win < P_WEAKEN:
                verdict = "VETO"
                conf_mult = 0.0
            elif p_win < P_PASS_LOW:
                verdict = "WEAKEN"
                conf_mult = 0.5
            else:
                verdict = "PASS"
                conf_mult = 1.0

            # In observe mode — or when the model has not earned authority —
            # score the signal but do not modify it.
            if not self.active_mode or not authorised:
                verdict = "PASS"
                conf_mult = 1.0

            with self.lock:
                self.total_signals_evaluated += 1
                if verdict == "CONFIRM": self.total_confirm += 1
                elif verdict == "VETO": self.total_veto += 1
                elif verdict == "WEAKEN": self.total_weaken += 1
                else: self.total_pass += 1
                model.record_verdict(verdict)

            model.pending[pkey] = {
                "signal": signal,
                "features": list(features),
                "p_win": p_win,
                "ts": time.time(),
            }
            self._prune_pending(model)

            active_features = []
            for name, val in zip(FEATURE_NAMES, features):
                if abs(val) > 0.3:
                    active_features.append(f"{name}={val:+.2f}")

            reason = (f"P(win)={p_win:.1%} samples={model.samples} "
                      f"acc={model.accuracy():.1%} | {gate_note}")
            if active_features:
                reason += " | active: " + ", ".join(active_features[:5])

            emoji = {"CONFIRM": "✅", "VETO": "🚫", "WEAKEN": "⚠️", "PASS": "➖"}[verdict]
            print(f"[agent] {emoji} {verdict} {asset} {signal} P={p_win:.1%} "
                  f"samples={model.samples} acc={model.accuracy():.1%}")

            self._add_thought("evaluate", asset,
                f"{verdict} {signal} P(win)={p_win:.1%} (samples={model.samples})",
                {"verdict": verdict, "p_win": p_win, "features": features,
                 "signal": signal})

            return {
                "verdict": verdict,
                "confidence_adjustment": conf_mult,
                "p_win": p_win,
                "features": features,
                "feature_names": FEATURE_NAMES,
                "reason": reason,
                "authorised": authorised,
                "gate_reason": gate_note,
                "layers": {},
            }
        except Exception as e:
            print(f"[agent] evaluate error {asset}: {e}")
            return {"verdict": "PASS", "confidence_adjustment": 1.0,
                    "p_win": 0.5, "features": [], "reason": f"error: {e}"}

    @staticmethod
    def _prune_pending(model: AssetModel, max_age: float = 3600.0,
                       max_entries: int = 32) -> None:
        """Drop decisions whose outcome never arrived (stream died, restart)."""
        if len(model.pending) <= max_entries:
            cutoff = time.time() - max_age
            stale = [k for k, v in model.pending.items()
                     if v.get("ts", 0) < cutoff]
        else:
            stale = sorted(model.pending,
                           key=lambda k: model.pending[k].get("ts", 0)
                           )[:len(model.pending) - max_entries]
        for k in stale:
            model.pending.pop(k, None)

    def learn(self, asset: str, signal: str, was_correct: bool,
              period: int = 0, ctime: int = 0):
        if not self.enabled:
            return
        try:
            model = self._get_model(asset)
            pkey = (int(period or 0), int(ctime or 0))
            entry = model.pending.pop(pkey, None)
            if entry is None:
                # No exact match (older caller, or the key was never supplied).
                # Fall back to the oldest pending decision that agrees on
                # direction — never silently train on a mismatched pair.
                cands = [k for k, v in model.pending.items()
                         if v["signal"] == signal]
                if not cands:
                    return
                pkey = min(cands, key=lambda k: model.pending[k].get("ts", 0))
                entry = model.pending.pop(pkey)
            if entry["signal"] != signal:
                return
            model.learn(entry["features"], signal, was_correct,
                        p_at_decision=entry.get("p_win"))
            with self.lock:
                self.total_signals_learned += 1
                self._learns_since_save += 1
                due = self._learns_since_save >= SAVE_EVERY_N_LEARNS
            if due:
                self.save_state()
            self._add_thought("learn", asset,
                f"learned: {signal} {'✓' if was_correct else '✗'} "
                f"→ samples={model.samples} acc={model.accuracy():.1%} "
                f"loss={model.avg_loss():.3f}",
                {"was_correct": was_correct, "samples": model.samples,
                 "accuracy": model.accuracy()})
            print(f"[agent] 🧠 learned {asset} {signal} {'✓' if was_correct else '✗'} "
                  f"samples={model.samples} acc={model.accuracy():.1%}")
        except Exception as e:
            print(f"[agent] learn error {asset}: {e}")

    # ── Persistence ─────────────────────────────────────────────────────────

    def save_state(self) -> int:
        """Persist every asset model to the DB. Returns the number saved.

        Before this existed the agent restarted from random Xavier weights on
        every redeploy while still being allowed to override live signals.
        """
        try:
            import json as _json
            import db as _db
            with self.lock:
                snapshot = [(a, m.state_dict()) for a, m in self.models.items()]
                self._learns_since_save = 0
            if not snapshot:
                return 0
            with _db._write_cursor() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS agent_models (
                    asset TEXT PRIMARY KEY,
                    version INT,
                    samples INT,
                    state_json TEXT,
                    ts REAL)""")
                for asset, sd in snapshot:
                    c.execute(
                        "INSERT OR REPLACE INTO agent_models "
                        "(asset, version, samples, state_json, ts) "
                        "VALUES (?,?,?,?,?)",
                        (asset, 2, sd["samples"], _json.dumps(sd), time.time()))
            self._last_save = time.time()
            return len(snapshot)
        except Exception as e:
            print(f"[agent] save_state failed: {e}")
            return 0

    def load_state(self) -> int:
        """Restore asset models saved by a previous process. Returns count."""
        try:
            import json as _json
            import db as _db
            with _db._read_cursor() as c:
                try:
                    rows = c.execute(
                        "SELECT asset, state_json FROM agent_models").fetchall()
                except Exception:
                    return 0   # table not created yet — first ever boot
            n = 0
            for r in rows:
                try:
                    sd = _json.loads(r["state_json"])
                    m = AssetModel(r["asset"])
                    m.load_state(sd)
                    self.models[r["asset"]] = m
                    n += 1
                except Exception as e:
                    print(f"[agent] skipped stale model {r['asset']}: {e}")
            if n:
                print(f"[agent] 💾 restored {n} learned model(s) from DB")
            return n
        except Exception as e:
            print(f"[agent] load_state failed: {e}")
            return 0

    def authority_summary(self) -> Dict[str, Any]:
        """How many assets have actually earned the right to override."""
        with self.lock:
            models = list(self.models.values())
        authorised = [m.asset for m in models if m.has_authority()]
        aucs = [m.edge_auc() for m in models]
        aucs = [a for a in aucs if a is not None]
        return {
            "gate_enabled": AGENT_GATE_ENABLED,
            "gate_min_samples": GATE_MIN_SAMPLES,
            "gate_min_auc": GATE_MIN_AUC,
            "assets_authorised": len(authorised),
            "assets_total": len(models),
            "authorised_assets": authorised,
            "median_edge_auc": (round(sorted(aucs)[len(aucs) // 2], 4)
                                if aucs else None),
        }

    def get_status(self) -> Dict[str, Any]:
        auth = self.authority_summary()
        with self.lock:
            uptime = time.time() - self.started_at
            n_auth = auth["assets_authorised"]
            if self.final_mode and n_auth:
                mode_str = f"FINAL (agent decides on {n_auth} authorised asset(s))"
            elif self.active_mode and n_auth:
                mode_str = f"ACTIVE (modifying signals on {n_auth} asset(s))"
            elif self.final_mode or self.active_mode:
                mode_str = ("OBSERVE — requested "
                            + ("FINAL" if self.final_mode else "ACTIVE")
                            + ", but no asset has passed the edge gate yet")
            else:
                mode_str = "OBSERVE (learning, not modifying)"
            # FIX (PER-PAIR-AGENT-2026-08-07 / A-04 #1, A-13 #1): expose
            # per-pair stats so the UI can show agent data for the currently-
            # selected pair instead of only global aggregates. Previously the
            # Agent tab showed "Ticks Processed: 50000" globally — meaningless
            # when the user is looking at EURUSD_otc. Now `per_pair[asset]`
            # gives ticks/samples/AUC/verdicts/authority for that specific pair.
            per_pair: Dict[str, Dict[str, Any]] = {}
            for asset, m in self.models.items():
                auc = m.edge_auc()
                per_pair[asset] = {
                    "asset": asset,
                    "ticks_processed": len(m.ticks),
                    "samples": len(m.recent_predictions),
                    "edge_auc": round(auc, 4) if auc is not None else None,
                    "has_authority": m.has_authority(),
                    # BUG-FIX (2026-08-07): these five read getattr(m, ...) for
                    # attributes AssetModel never defined, so every per-pair
                    # count was a fake 0. They are real counters now.
                    "veto": m.veto_count,
                    "confirm": m.confirm_count,
                    "weaken": m.weaken_count,
                    "pass_count": m.pass_count,
                    "flips": m.flip_count,
                    "neutralized": m.neutral_count,
                    # BUG-FIX (2026-08-07): recent_predictions holds
                    # (p_win, was_correct) tuples, so `.get("p_win")` raised
                    # AttributeError: 'tuple' object has no attribute 'get'.
                    # That killed get_status() outright, which is why
                    # /api/agent/status, /live and /models all returned an
                    # error payload and the whole Agent tab rendered empty
                    # while the agent was in fact running and learning.
                    "last_pwin": round(m.last_p_win, 4),
                    "last_graded_pwin": (round(m.recent_predictions[-1][0], 4)
                                         if m.recent_predictions else None),
                    "last_decision_at": (m.last_decision_at
                                         if m.last_decision_at else None),
                }
            return {
                "enabled": self.enabled,
                "active_mode": self.active_mode,
                "final_mode": self.final_mode,
                "mode": mode_str,
                "uptime_seconds": round(uptime, 0),
                "uptime_human": _human_duration(uptime),
                "total_ticks_processed": self.total_ticks_processed,
                "total_signals_evaluated": self.total_signals_evaluated,
                "total_signals_learned": self.total_signals_learned,
                "total_veto": self.total_veto,
                "total_confirm": self.total_confirm,
                "total_weaken": self.total_weaken,
                "total_pass": self.total_pass,
                "total_flips": self.total_flips,
                "total_kept": self.total_kept,
                "total_neutralized": self.total_neutralized,
                "total_gated": self.total_gated,
                "assets_tracked": len(self.models),
                # Aliases the Agent tab's meta labels read. Without these the
                # "N models" / "N decisions" chips never populated.
                "models_count": len(self.models),
                "total_decisions": self.total_signals_evaluated,
                "thought_count": len(self.thoughts),
                "analyzer_type": "agent_brain_v2_neural",
                "learning_rate": LEARNING_RATE,
                "momentum": MOMENTUM,
                "feature_names": FEATURE_NAMES,
                "num_features": NUM_FEATURES,
                "hidden_neurons": HIDDEN_NEURONS,
                "authority": auth,
                # NEW: per-pair breakdown — lets the UI show agent state for
                # the currently-selected pair (resolves "agent computes all
                # pairs together" complaint).
                "per_pair": per_pair,
                "per_pair_count": len(per_pair),
                "last_saved_ago_sec": (round(time.time() - self._last_save)
                                       if self._last_save else None),
            }

    def get_live_feed(self, limit: int = 50) -> List[Dict]:
        with self.lock:
            return list(self.thoughts)[:limit]

    def get_models(self) -> List[Dict]:
        with self.lock:
            return [m.to_dict() for m in self.models.values()]

    def get_model(self, asset: str) -> Optional[Dict]:
        with self.lock:
            m = self.models.get(asset)
            return m.to_dict() if m else None

    def get_current_features(self, asset: str) -> Dict:
        if not self.enabled:
            return {"enabled": False, "features": []}
        with self.lock:
            model = self.models.get(asset)
            if not model or len(model.ticks) < 10:
                return {"enabled": True, "asset": asset,
                        "features": [], "tick_count": len(model.ticks) if model else 0}
            return {
                "enabled": True,
                "asset": asset,
                "features": [
                    {"name": name, "value": round(val, 3),
                     "active": abs(val) > 0.3}
                    for name, val in zip(FEATURE_NAMES, model.last_features_norm)
                ],
                "features_raw": [
                    {"name": name, "value": round(val, 3)}
                    for name, val in zip(FEATURE_NAMES, model.last_features_raw)
                ],
                "tick_count": len(model.ticks),
                "last_p_win": round(model.last_p_win, 3),
                "samples": model.samples,
                "accuracy": round(model.accuracy(), 3),
                "edge_auc": (round(model.edge_auc(), 4)
                             if model.edge_auc() is not None else None),
                "has_authority": model.has_authority(),
                "gate_reason": model.gate_reason(),
            }


def _human_duration(seconds: float) -> str:
    if seconds < 60: return f"{int(seconds)}s"
    if seconds < 3600: return f"{int(seconds/60)}m {int(seconds%60)}s"
    if seconds < 86400: return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"


agent = AgentBrain()
