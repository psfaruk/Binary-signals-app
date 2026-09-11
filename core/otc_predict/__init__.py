"""core/otc_predict — OTC Future Candle Prediction Engine (PART 10-27).

USER SPEC (2026-09-11, 30-part "OTC Future Candle Prediction Engine"):

    মূল লক্ষ্য: একটি নির্দিষ্ট OTC pair-এর live price feed থেকে বর্তমান
    candle চলাকালীন পরবর্তী T+1 ও T+2 candle-এর সম্ভাব্য direction নির্ণয়।
    "Future candle দেখা" নয় → "Future candle-এর সম্ভাব্য direction
    statistically predict করা"।

    সবচেয়ে গুরুত্বপূর্ণ component: সঠিক OTC live data + leakage-free
    historical dataset + rigorous walk-forward testing।

MODULE MAP (spec PART 26 proposed structure → this package)
------------------------------------------------------------
    backend/data/tick_collector.py + candle_builder.py  → feed.py +
        candle_micro (already live, PIPELINE PHASE 1, commit b03d1d4)
    backend/data/data_validator.py    → predictor._quality_gates
    backend/features/*                → otc_predict.features_ext
    backend/models/t1_model.py + t2_model.py + model_loader.py
                                      → otc_predict.models (+ model_registry)
    backend/models/predictor.py       → otc_predict.predictor
    backend/strategy/price_action.py  → otc_predict.price_action
    backend/strategy/signal_filter.py + confidence.py
                                      → otc_predict.signal_filter (+ regime)
    backend/backtest/backtester.py + walk_forward.py
                                      → otc_predict.walk_forward +
                                        scripts/backtest_otc_predictor.py
    backend/database/models.py + repository.py
                                      → db.py (otc_predictions +
                                        model_registry) + otc_predict.tracker
    backend/api/signals.py            → server.py /api/prediction* endpoints
    frontend signal.js + websocket.js → static/js/common.js otc_pred handler

DESIGN CONSTANTS
----------------
* The prediction layer is ADDITIVE: the existing every-candle confluence
  signal engine is untouched. This module produces the separate
  NEXT CANDLE / 2ND CANDLE probability card (PART 30 UI).
* FREEZE (PART 16): every prediction is INSERT-ed once at creation into
  otc_predictions with UNIQUE(asset, period, target_time, horizon);
  re-prediction / late editing is structurally impossible. Only the
  settlement columns (actual_result / win_loss / settled_at) are ever
  filled afterwards — by candle-close events, never by humans.
* NO look-ahead (PART 19): features are computed from CLOSED candles up to
  the prediction-time candle only; the feature engine physically receives
  no future slice (same API design as core/otc_features.py, proven by
  perturbation tests).
* HONEST STATES (PART 24): no model registered / data gap / window too
  short / extreme volatility → NO SIGNAL with the reason recorded —
  never a fabricated signal.
* Thresholds (PART 14) are DEFAULTS to be tuned by walk-forward evidence,
  not fixed truth — they are env-tunable and reported per tier in every
  backtest.
"""

__all__ = ["features_ext", "regime", "price_action", "signal_filter",
           "models", "tracker", "predictor", "walk_forward"]
