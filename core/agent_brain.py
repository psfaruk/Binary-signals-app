"""
core/agent_brain.py — AUTONOMOUS AI AGENT WITH ONLINE LEARNING (PROD-AGENT-2026-08-06)

WHAT THIS IS
============
A real, self-learning AI agent that:

  1. AUTONOMOUSLY ingests EVERY tick from the market (not just at signal time)
  2. Computes 8 features in real-time from tick data
  3. Uses a logistic regression model (single-layer neural network) to estimate
     P(win) for any CALL/PUT signal
  4. LEARNS online — after each signal resolves, updates its weights via
     gradient descent based on whether the prediction was right
  5. Maintains SEPARATE models per asset (EURUSD_otc ≠ USDJPY_otc)
  6. Streams its "thoughts" to a live queue the UI can read

This is NOT rule-based. It's a genuine ML model with learned weights that
adapt over time based on what actually works.

ARCHITECTURE
============
[Quotex WS] → feed.py tick loop → agent.process_tick(asset, price, ts)
                                         ↓
                                   tick buffer (last 1000 ticks)
                                         ↓
                                   feature extraction (8 features)
                                         ↓
                                   [wait for signal]
                                         ↓
agent.evaluate(signal, asset) ← feed.py at EOC
         ↓
   P(win) = sigmoid(w · features + bias)
         ↓
   verdict: P>=0.62 → CONFIRM, P<=0.45 → VETO, else PASS
         ↓
[signal resolves]
         ↓
agent.learn(asset, was_correct, features) ← feed.py after grading
         ↓
   w -= learning_rate * (predicted_prob - actual) * features
         ↓
   model improves over time

8 FEATURES (all normalized to roughly [-1, 1])
==============================================
  f1: tick_momentum      — velocity of last 30 ticks (ATR/sec)
  f2: tick_acceleration  — acceleration (2nd derivative)
  f3: order_flow         — uptick ratio (0=all down, 1=all up)
  f4: micro_volatility   — recent price range / ATR
  f5: htf_alignment      — 5-min vs 20-min EMA separation
  f6: gap_vs_prev_close  — current price vs prev close (ATR units)
  f7: tick_rate          — ticks per second (broker behavior)
  f8: candle_position    — where in current candle (0=open, 1=close)

USAGE
=====
  from core.agent_brain import agent
  agent.process_tick("EURUSD_otc", 1.0850, time.time())  # every tick
  result = agent.evaluate({"signal":"CALL","confidence":55}, "EURUSD_otc")  # at EOC
  agent.learn("EURUSD_otc", "CALL", True, features)  # after grading

ENABLE
======
  QX_AGENT_BRAIN=1 env var turns on the agent.
"""
import os
import time
import math
import json
import threading
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
from collections import deque, defaultdict


# ── Configuration ────────────────────────────────────────────────────────────

TICK_BUFFER_SIZE = 1000          # per-asset tick buffer
THOUGHT_BUFFER_SIZE = 200        # recent "thoughts" for UI
LEARNING_RATE = 0.05             # gradient descent step size
MIN_LEARNING_SAMPLES = 5         # start learning after this many graded signals
SIGNIFICANT_FEATURE_THRESHOLD = 0.3  # feature values above this are "active"

# Verdict thresholds (on P(win))
P_CONFIRM = 0.62   # P >= 0.62 → CONFIRM (×1.15 boost)
P_VETO = 0.45      # P <= 0.45 → VETO (kill signal)
P_WEAKEN = 0.50    # 0.45 < P < 0.50 → WEAKEN (×0.6)
# 0.50 <= P < 0.62 → PASS (no change)

# Feature names (for UI display + DB storage)
FEATURE_NAMES = [
    "tick_momentum",
    "tick_acceleration",
    "order_flow",
    "micro_volatility",
    "htf_alignment",
    "gap_vs_prev_close",
    "tick_rate",
    "candle_position",
]


# ── Per-asset model state ───────────────────────────────────────────────────

class AssetModel:
    """Per-asset learned model + tick buffer."""

    def __init__(self, asset: str):
        self.asset = asset
        # Logistic regression weights (8 features + bias)
        # Initialized to zero — agent starts "knowing nothing" and learns
        self.weights = [0.0] * 8
        self.bias = 0.0
        # Tick buffer: [(timestamp_ms, price), ...]
        self.ticks = deque(maxlen=TICK_BUFFER_SIZE)
        # Learning stats
        self.samples = 0
        self.correct_predictions = 0
        self.total_loss = 0.0
        # Last computed features (for UI)
        self.last_features = [0.0] * 8
        self.last_p_win = 0.5
        # Recent predictions for this asset (for accuracy tracking)
        self.recent_predictions = deque(maxlen=50)  # [(p_win, was_correct), ...]
        # Last signal context (for learning when outcome arrives)
        self.pending_signal = None  # {"signal": "CALL", "features": [...], "p_win": float}

    def add_tick(self, ts_ms: float, price: float):
        self.ticks.append((ts_ms, price))

    def extract_features(self, closed_candles: List[Dict]) -> List[float]:
        """Extract 8 features from current tick buffer + closed candles."""
        if len(self.ticks) < 10:
            self.last_features = [0.0] * 8
            return self.last_features

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

        # f1: tick_momentum — velocity over last 30 ticks (ATR/sec)
        recent_30 = ticks[-30:] if len(ticks) >= 30 else ticks
        dt_sec = (recent_30[-1][0] - recent_30[0][0]) / 1000.0
        if dt_sec > 0:
            velocity = (recent_30[-1][1] - recent_30[0][1]) / dt_sec / atr
            f1 = max(-1.0, min(1.0, velocity * 2.0))  # scale + clamp
        else:
            f1 = 0.0

        # f2: tick_acceleration — 2nd derivative (first half vs second half velocity)
        mid = len(recent_30) // 2
        if mid > 0:
            dt1 = (recent_30[mid][0] - recent_30[0][0]) / 1000.0
            dt2 = (recent_30[-1][0] - recent_30[mid][0]) / 1000.0
            v1 = (recent_30[mid][1] - recent_30[0][1]) / max(0.001, dt1) / atr if dt1 > 0 else 0
            v2 = (recent_30[-1][1] - recent_30[mid][1]) / max(0.001, dt2) / atr if dt2 > 0 else 0
            accel = (v2 - v1) * 2.0
            f2 = max(-1.0, min(1.0, accel))
        else:
            f2 = 0.0

        # f3: order_flow — uptick ratio over last 30 ticks
        up = down = 0
        for i in range(1, len(recent_30)):
            if recent_30[i][1] > recent_30[i-1][1]:
                up += 1
            elif recent_30[i][1] < recent_30[i-1][1]:
                down += 1
        total = up + down
        f3 = (up / total - 0.5) * 2.0 if total > 0 else 0.0  # [-1, 1]

        # f4: micro_volatility — price range over last 3 sec / ATR
        cutoff = now_ms - 3000
        recent_3s = [p for t, p in ticks if t >= cutoff]
        if len(recent_3s) >= 2:
            rng = max(recent_3s) - min(recent_3s)
            f4 = max(-1.0, min(1.0, (rng / atr - 0.3) * 1.5))
        else:
            f4 = 0.0

        # f5: htf_alignment — 5-min vs 20-min EMA proxy
        if len(closed_candles) >= 20:
            last5_avg = sum(c["close"] for c in closed_candles[-5:]) / 5
            last20_avg = sum(c["close"] for c in closed_candles[-20:]) / 20
            sep = (last5_avg - last20_avg) / max(1e-9, last20_avg)
            f5 = max(-1.0, min(1.0, sep * 100))  # scale percent to [-1,1]
        else:
            f5 = 0.0

        # f6: gap_vs_prev_close — current price vs prev close (ATR units)
        if closed_candles and atr > 0:
            prev_close = closed_candles[-1]["close"]
            cur_price = ticks[-1][1]
            gap = (cur_price - prev_close) / atr
            f6 = max(-1.0, min(1.0, gap * 2.0))
        else:
            f6 = 0.0

        # f7: tick_rate — ticks per second (broker behavior)
        cutoff_5s = now_ms - 5000
        recent_5s_count = sum(1 for t, _ in ticks if t >= cutoff_5s)
        rate = recent_5s_count / 5.0
        # Normal 5-10/sec. Below 2 = slow, above 20 = burst
        if rate < 2:
            f7 = -1.0  # slow (suspicious)
        elif rate > 20:
            f7 = -0.5  # burst (risky)
        elif 5 <= rate <= 10:
            f7 = 0.5   # ideal
        else:
            f7 = 0.0   # acceptable

        # f8: candle_position — where in current candle (0=open, 1=close)
        # We don't have candle open time here precisely; estimate from tick buffer
        # by looking at oldest tick in last 60 seconds
        cutoff_60s = now_ms - 60000
        first_tick_in_min = next((t for t, _ in ticks if t >= cutoff_60s), None)
        if first_tick_in_min and now_ms > first_tick_in_min:
            pos = (now_ms - first_tick_in_min) / 60000.0
            f8 = max(-1.0, min(1.0, (pos - 0.5) * 2.0))  # 0 at open, 1 at close
        else:
            f8 = 0.0

        features = [f1, f2, f3, f4, f5, f6, f7, f8]
        self.last_features = features
        return features

    def predict(self, features: List[float], signal: str) -> float:
        """Logistic regression: P(win) = sigmoid(w·f + b).

        For PUT signals, we flip the sign of directional features because
        a "bullish" feature that helps CALL would hurt PUT.
        """
        # Directional features: f1 (momentum), f3 (order_flow), f5 (htf), f6 (gap)
        # For PUT, these should be negated
        dir_indices = {0, 2, 4, 5}  # f1, f3, f5, f6
        sign = 1.0 if signal == "CALL" else -1.0
        adjusted = []
        for i, f in enumerate(features):
            if i in dir_indices:
                adjusted.append(f * sign)
            else:
                adjusted.append(f)
        z = sum(w * f for w, f in zip(self.weights, adjusted)) + self.bias
        p = 1.0 / (1.0 + math.exp(-max(-10, min(10, z))))
        self.last_p_win = p
        return p

    def learn(self, features: List[float], signal: str, was_correct: bool):
        """Online gradient descent update.

        Loss = binary cross-entropy: -[y log(p) + (1-y) log(1-p)]
        Gradient: dL/dw_j = (p - y) * f_j
        Update: w_j -= lr * (p - y) * f_j
        """
        # Always count the sample
        self.samples += 1
        self.recent_predictions.append((self.predict(features, signal), was_correct))
        if was_correct:
            self.correct_predictions += 1
        # Don't update weights until we have enough samples
        if self.samples < MIN_LEARNING_SAMPLES:
            return

        p = self.predict(features, signal)
        y = 1.0 if was_correct else 0.0
        error = p - y

        # Directional feature adjustment (same as predict)
        dir_indices = {0, 2, 4, 5}
        sign = 1.0 if signal == "CALL" else -1.0
        adjusted = []
        for i, f in enumerate(features):
            if i in dir_indices:
                adjusted.append(f * sign)
            else:
                adjusted.append(f)

        # Gradient descent
        for i in range(len(self.weights)):
            self.weights[i] -= LEARNING_RATE * error * adjusted[i]
        self.bias -= LEARNING_RATE * error

        # Loss for monitoring
        loss = -(y * math.log(max(1e-10, p)) + (1-y) * math.log(max(1e-10, 1-p)))
        self.total_loss += loss

    def accuracy(self) -> float:
        if not self.recent_predictions:
            return 0.0
        # Accuracy = fraction where p >= 0.5 matched was_correct
        correct = sum(1 for p, wc in self.recent_predictions
                      if (p >= 0.5) == wc)
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
            "weights": [round(w, 3) for w in self.weights],
            "bias": round(self.bias, 3),
            "last_features": [round(f, 3) for f in self.last_features],
            "last_p_win": round(self.last_p_win, 3),
            "tick_buffer_size": len(self.ticks),
        }


# ── Main Agent Brain singleton ──────────────────────────────────────────────

class AgentBrain:
    """Autonomous AI agent that processes every tick and learns from outcomes."""

    def __init__(self):
        self.models: Dict[str, AssetModel] = {}
        self.lock = threading.RLock()
        self.enabled = os.environ.get("QX_AGENT_BRAIN", "0") == "1"
        self.started_at = time.time()
        # Thought stream for UI
        self.thoughts = deque(maxlen=THOUGHT_BUFFER_SIZE)
        # Global stats
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
        """Add a thought to the stream (visible in UI)."""
        thought = {
            "ts": time.time(),
            "type": thought_type,  # "tick", "evaluate", "learn", "feature"
            "asset": asset,
            "message": message[:200],
            "data": data or {},
        }
        with self.lock:
            self.thoughts.appendleft(thought)

    # ── Called on EVERY tick from feed.py ───────────────────────────────

    def process_tick(self, asset: str, price: float, ts_ms: float):
        """Ingest a tick. Called from feed.py tick loop for every tick.

        This makes the agent autonomous — it sees ALL market data,
        not just at signal time.
        """
        if not self.enabled:
            return
        try:
            model = self._get_model(asset)
            model.add_tick(ts_ms, price)
            self.total_ticks_processed += 1
            # Log significant ticks (every 50th tick per asset to avoid spam)
            if self.total_ticks_processed % 50 == 0:
                self._add_thought("tick", asset,
                    f"tick #{self.total_ticks_processed}: {asset} @ {price:.5f}",
                    {"price": price, "ts": ts_ms})
        except Exception as e:
            print(f"[agent] process_tick error {asset}: {e}")

    # ── Called at EOC to evaluate a signal ──────────────────────────────

    def evaluate(self, prediction: Dict[str, Any], asset: str,
                 closed_candles: List[Dict]) -> Dict[str, Any]:
        """Evaluate a CALL/PUT signal. Returns P(win) + verdict."""
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
            p_win = model.predict(features, signal)

            # Verdict based on P(win)
            if p_win >= P_CONFIRM:
                verdict = "CONFIRM"
                conf_mult = 1.15
            elif p_win <= P_VETO:
                verdict = "VETO"
                conf_mult = 0.0
            elif p_win < P_WEAKEN:
                verdict = "WEAKEN"
                conf_mult = 0.6
            else:
                verdict = "PASS"
                conf_mult = 1.0

            # Update stats
            with self.lock:
                self.total_signals_evaluated += 1
                if verdict == "CONFIRM": self.total_confirm += 1
                elif verdict == "VETO": self.total_veto += 1
                elif verdict == "WEAKEN": self.total_weaken += 1
                else: self.total_pass += 1

            # Store pending signal for learning when outcome arrives
            model.pending_signal = {
                "signal": signal,
                "features": list(features),
                "p_win": p_win,
            }

            # Build feature summary for reason
            active_features = []
            for i, (name, val) in enumerate(zip(FEATURE_NAMES, features)):
                if abs(val) > SIGNIFICANT_FEATURE_THRESHOLD:
                    active_features.append(f"{name}={val:+.2f}")

            reason = f"P(win)={p_win:.1%} samples={model.samples} acc={model.accuracy():.1%}"
            if active_features:
                reason += " | active: " + ", ".join(active_features[:4])

            # Visible log
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
                "layers": {},  # compat with verifier UI
            }
        except Exception as e:
            print(f"[agent] evaluate error {asset}: {e}")
            return {"verdict": "PASS", "confidence_adjustment": 1.0,
                    "p_win": 0.5, "features": [], "reason": f"error: {e}"}

    # ── Called after signal resolves to LEARN ───────────────────────────

    def learn(self, asset: str, signal: str, was_correct: bool):
        """Learn from outcome. Updates weights via gradient descent."""
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

    # ── Status / visibility for UI ──────────────────────────────────────

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
                "analyzer_type": "agent_brain_v1",
                "learning_rate": LEARNING_RATE,
                "feature_names": FEATURE_NAMES,
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
        """Get live feature values for an asset (for real-time UI)."""
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
                     "active": abs(val) > SIGNIFICANT_FEATURE_THRESHOLD}
                    for name, val in zip(FEATURE_NAMES, model.last_features)
                ],
                "tick_count": len(model.ticks),
                "last_p_win": round(model.last_p_win, 3),
                "samples": model.samples,
                "accuracy": round(model.accuracy(), 3),
                "weights": [round(w, 3) for w in model.weights],
            }


def _human_duration(seconds: float) -> str:
    if seconds < 60: return f"{int(seconds)}s"
    if seconds < 3600: return f"{int(seconds/60)}m {int(seconds%60)}s"
    if seconds < 86400: return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"


# ── Singleton ───────────────────────────────────────────────────────────────

agent = AgentBrain()
