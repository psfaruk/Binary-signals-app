"""core/otc_predict/models.py — T+1 / T+2 models (PART 10+11+13+22).

USER SPEC:

  PART 10 (Baseline Model): প্রথম model হিসেবে Logistic Regression baseline;
       এরপর Random Forest / XGBoost / LightGBM এর মধ্যে তুলনা। যে model
       unseen data-তে সবচেয়ে ভালো এবং stable ফল দেয় সেটি নির্বাচন।
  PART 11 (Deep Learning): প্রথম থেকেই LSTM/Transformer ব্যবহার করা হবে
       না — enough data হলে পরে পরীক্ষা করা যেতে পারে (npz sequence export
       already supports that). NOT implemented here by design.
  PART 13 (Probability Calibration): Model-এর "80%" output মানেই বাস্তবে
       80% win হবে—এটা ধরে নেওয়া যাবে না → calibration করা হবে।
  PART 22 (Pair-Specific Model): সব pair-এর জন্য একটি model সবসময় ভালো
       নাও হতে পারে → per-pair আলাদা training সমর্থিত (caller decides scope)。

XGBoost/LightGBM are NOT installed in this deployment; their
scikit-learn equivalents are used for the same gradient-boosting family:
  * HistGradientBoostingClassifier  (LightGBM-family, histogram based)
  * RandomForestClassifier          (bagging family)
  * LogisticRegression              (PART 10 required baseline)
The candidate chosen is the one with the best time-ordered validation
log-loss (selection happens in walk_forward.py; training here just fits).

Calibration (PART 13): manual Platt scaling — a 1-D logistic regression
fitted on the base model's log-odds over a TIME-ORDERED validation tail
(never the test fold). Version-proof (no private sklearn APIs).

Model artefacts: joblib bundle {version, feature_names, t1, t2, meta}
saved under MODELS_DIR (env QX_PREDICT_MODELS_DIR > DB directory >
repo ./models). The registry lives in db.model_registry (active flag),
so a redeploy never serves a stale unregistered file.
"""

import math
import os
import time

__all__ = ["SKLEARN_OK", "CANDIDATES", "fit_candidate", "platt_calibrate",
           "apply_platt", "ModelBundle", "models_dir", "save_bundle",
           "load_bundle"]

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (RandomForestClassifier,
                                  HistGradientBoostingClassifier)
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    import joblib
    SKLEARN_OK = True
except Exception:  # pragma: no cover — Railway slim image fallback
    SKLEARN_OK = False

SEED = 42


def _std_pipeline(clf):
    return Pipeline([("sc", StandardScaler()), ("clf", clf)])


def CANDIDATES():
    """Candidate estimators (PART 10 list mapped to available libs)."""
    if not SKLEARN_OK:
        return {}
    return {
        "logreg": lambda: _std_pipeline(LogisticRegression(
            C=0.2, max_iter=1000, random_state=SEED)),
        "rf": lambda: RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=20,
            random_state=SEED, n_jobs=1, class_weight=None),
        "histgb": lambda: HistGradientBoostingClassifier(
            max_iter=150, max_depth=3, learning_rate=0.05,
            min_samples_leaf=40, l2_regularization=1.0, random_state=SEED),
    }


def fit_candidate(name, X, y):
    """Fit one candidate on (X, y). Returns the fitted estimator."""
    cands = CANDIDATES()
    if name not in cands:
        raise ValueError(f"unknown candidate: {name}")
    return cands[name]().fit(X, y)


# ───────────────────────── PART 13: Platt calibration ─────────────────────

def platt_calibrate(base_model, X_val, y_val):
    """Fit Platt scaling on a time-ordered validation tail.

    Platt: p = sigmoid(A * logit(p_base) + B). Fitted with plain logistic
    regression on the 1-D logit feature. Returns (A, B) or None when the
    tail is too small / degenerate (caller then uses raw probabilities).
    """
    if not SKLEARN_OK:
        return None
    try:
        if len(X_val) < 200 or len(set(y_val)) < 2:
            return None
        p = base_model.predict_proba(X_val)[:, 1]
        p = np.clip(p, 1e-6, 1 - 1e-6)
        logit = np.log(p / (1 - p)).reshape(-1, 1)
        lr = LogisticRegression(C=1e6, max_iter=500)
        lr.fit(logit, np.asarray(y_val))
        return (float(lr.coef_[0][0]), float(lr.intercept_[0]))
    except Exception as exc:  # pragma: no cover
        print(f"[models] platt calibration skipped: {type(exc).__name__}: {exc}")
        return None


def apply_platt(prob, coefs):
    """Apply (A, B) Platt coefficients to a raw probability."""
    if not coefs:
        return prob
    a, b = coefs
    p = min(max(prob, 1e-6), 1 - 1e-6)
    logit = math.log(p / (1 - p))
    z = a * logit + b
    return 1.0 / (1.0 + math.exp(-z))


# ───────────────────────────── model bundle ───────────────────────────────

def models_dir():
    """Artefact directory: env > DB dir (Railway Volume) > repo ./models."""
    d = os.environ.get("QX_PREDICT_MODELS_DIR")
    if not d:
        from db import DB_PATH
        d = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "models")
    os.makedirs(d, exist_ok=True)
    return d


class ModelBundle:
    """Container for the pair of models (T+1 / T+2) + metadata."""

    def __init__(self, version, feature_names, t1, t2, meta):
        self.version = version
        self.feature_names = list(feature_names)
        self.t1 = t1          # {"model": fitted, "platt": (A,B)|None, "name": str}
        self.t2 = t2
        self.meta = meta or {}
        self.created_at = time.time()

    def predict_up(self, horizon, feat_row):
        """P(UP) for a 1-row feature dict; None when the model is missing."""
        slot = self.t1 if horizon == 1 else self.t2
        if slot is None or not SKLEARN_OK:
            return None
        import numpy as _np
        X = _np.array([[float(feat_row[k]) for k in self.feature_names]])
        p = float(slot["model"].predict_proba(X)[0, 1])
        return apply_platt(p, slot.get("platt"))

    def to_dict(self):
        return {"version": self.version, "feature_names": self.feature_names,
                "meta": self.meta, "created_at": self.created_at}


def save_bundle(bundle, path=None):
    if not SKLEARN_OK:
        raise RuntimeError("sklearn unavailable — cannot save bundle")
    path = path or os.path.join(
        models_dir(), f"otc_bundle_{bundle.version}.joblib")
    joblib.dump({"version": bundle.version,
                 "feature_names": bundle.feature_names,
                 "t1": bundle.t1, "t2": bundle.t2,
                 "meta": bundle.meta,
                 "created_at": bundle.created_at}, path)
    return path


def load_bundle(path):
    if not SKLEARN_OK:
        return None
    d = joblib.load(path)
    b = ModelBundle(d["version"], d["feature_names"], d["t1"], d["t2"],
                    d.get("meta"))
    b.created_at = d.get("created_at", 0)
    return b
