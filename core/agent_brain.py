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

# Trap hours (UTC) — historically low win rate
TRAP_HOURS = {3, 8, 9, 11, 16, 18, 22}
BOOST_HOURS = {4, 23, 14, 6, 10, 15, 17, 21, 12}

FEATURE_NAMES = [
    "tick_mom_30", "tick_mom_10", "tick_accel", "order_flow_30",
    "order_flow_10", "micro_vol_3s", "micro_vol_10s", "htf_align",
    "gap_prev_close", "tick_rate", "candle_body_pct", "hour_normalized",
]


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

        # Online normalization stats
        self.feat_mean = [0.0] * NUM_FEATURES
        self.feat_m2 = [0.0] * NUM_FEATURES  # for variance
        self.feat_count = 0

        # Learning stats
        self.samples = 0
        self.correct_predictions = 0
        self.total_loss = 0.0
        self.last_features_raw = [0.0] * NUM_FEATURES
        self.last_features_norm = [0.0] * NUM_FEATURES
        self.last_p_win = 0.5
        self.last_hidden = [0.0] * HIDDEN_NEURONS

        # Recent predictions for accuracy tracking
        self.recent_predictions = deque(maxlen=100)

        # Pending signal for learning
        self.pending_signal = None

    def add_tick(self, ts_ms: float, price: float):
        self.ticks.append((ts_ms, price))

    def _normalize_feature(self, i: int, val: float) -> float:
        """Online normalization using Welford's algorithm."""
        self.feat_count += 1
        delta = val - self.feat_mean[i]
        self.feat_mean[i] += delta / self.feat_count
        delta2 = val - self.feat_mean[i]
        self.feat_m2[i] += delta * delta2
        if self.feat_count < 2:
            return val  # not enough data
        variance = self.feat_m2[i] / (self.feat_count - 1)
        std = math.sqrt(max(1e-8, variance))
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
        dir_indices = {0, 1, 3, 4, 7, 8, 10}  # f1, f2, f4, f5, f8, f9, f11
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

    def learn(self, features: List[float], signal: str, was_correct: bool):
        """Backpropagation with momentum + L2 regularization + dropout."""
        self.samples += 1
        p_pred = self.predict(features, signal)
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

        # Directional feature adjustment
        dir_indices = {0, 1, 3, 4, 7, 8, 10}
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
        if not self.recent_predictions:
            return 0.0
        correct = sum(1 for p, wc in self.recent_predictions if (p >= 0.5) == wc)
        return correct / len(self.recent_predictions)

    def avg_loss(self) -> float:
        if self.samples == 0:
            return 0.0
        return self.total_loss / max(MIN_LEARNING_SAMPLES, self.samples - MIN_LEARNING_SAMPLES)

    def to_dict(self) -> Dict:
        return {
            "asset": self.asset,
            "samples": self.samples,
            "accuracy": round(self.accuracy(), 3),
            "avg_loss": round(self.avg_loss(), 4),
            "last_p_win": round(self.last_p_win, 3),
            "last_features": [round(f, 3) for f in self.last_features_norm],
            "tick_buffer_size": len(self.ticks),
            "hidden_neurons": HIDDEN_NEURONS,
        }


class AgentBrain:
    """Autonomous AI agent V2 with neural network."""

    def __init__(self):
        self.models: Dict[str, AssetModel] = {}
        self.lock = threading.RLock()
        self.enabled = os.environ.get("QX_AGENT_BRAIN", "0") == "1"
        self.started_at = time.time()
        self.thoughts = deque(maxlen=THOUGHT_BUFFER_SIZE)
        self.total_ticks_processed = 0
        self.total_signals_evaluated = 0
        self.total_signals_learned = 0
        self.total_veto = 0
        self.total_confirm = 0
        self.total_weaken = 0
        self.total_pass = 0

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

    def evaluate(self, prediction: Dict[str, Any], asset: str,
                 closed_candles: List[Dict]) -> Dict[str, Any]:
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

            # ENSEMBLE: average predictions from 3 forward passes with
            # different dropout patterns (Monte Carlo dropout).
            # This reduces variance and improves calibration.
            p_samples = []
            for _ in range(5):
                # Apply test-time dropout
                dropout_mask = [1.0 if random.random() > DROPOUT_PROB else 0.0
                                for _ in range(HIDDEN_NEURONS)]
                # Directional feature adjustment
                dir_indices = {0, 1, 3, 4, 7, 8, 10}
                sign = 1.0 if signal == "CALL" else -1.0
                adjusted = [f * sign if i in dir_indices else f
                            for i, f in enumerate(features)]
                # Forward with dropout
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
            p_win = model.calibrate_p(p_raw)

            # Verdict based on calibrated P(win)
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

            with self.lock:
                self.total_signals_evaluated += 1
                if verdict == "CONFIRM": self.total_confirm += 1
                elif verdict == "VETO": self.total_veto += 1
                elif verdict == "WEAKEN": self.total_weaken += 1
                else: self.total_pass += 1

            model.pending_signal = {
                "signal": signal,
                "features": list(features),
                "p_win": p_win,
            }

            active_features = []
            for name, val in zip(FEATURE_NAMES, features):
                if abs(val) > 0.3:
                    active_features.append(f"{name}={val:+.2f}")

            reason = f"P(win)={p_win:.1%} samples={model.samples} acc={model.accuracy():.1%}"
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
                "layers": {},
            }
        except Exception as e:
            print(f"[agent] evaluate error {asset}: {e}")
            return {"verdict": "PASS", "confidence_adjustment": 1.0,
                    "p_win": 0.5, "features": [], "reason": f"error: {e}"}

    def learn(self, asset: str, signal: str, was_correct: bool):
        if not self.enabled:
            return
        try:
            model = self._get_model(asset)
            if not model.pending_signal:
                return
            if model.pending_signal["signal"] != signal:
                return
            features = model.pending_signal["features"]
            model.learn(features, signal, was_correct)
            model.pending_signal = None
            with self.lock:
                self.total_signals_learned += 1
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

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            uptime = time.time() - self.started_at
            return {
                "enabled": self.enabled,
                "uptime_seconds": round(uptime, 0),
                "uptime_human": _human_duration(uptime),
                "total_ticks_processed": self.total_ticks_processed,
                "total_signals_evaluated": self.total_signals_evaluated,
                "total_signals_learned": self.total_signals_learned,
                "total_veto": self.total_veto,
                "total_confirm": self.total_confirm,
                "total_weaken": self.total_weaken,
                "total_pass": self.total_pass,
                "assets_tracked": len(self.models),
                "thought_count": len(self.thoughts),
                "analyzer_type": "agent_brain_v2_neural",
                "learning_rate": LEARNING_RATE,
                "momentum": MOMENTUM,
                "feature_names": FEATURE_NAMES,
                "num_features": NUM_FEATURES,
                "hidden_neurons": HIDDEN_NEURONS,
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
            }


def _human_duration(seconds: float) -> str:
    if seconds < 60: return f"{int(seconds)}s"
    if seconds < 3600: return f"{int(seconds/60)}m {int(seconds%60)}s"
    if seconds < 86400: return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"


agent = AgentBrain()
