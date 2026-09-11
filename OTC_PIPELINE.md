# OTC Future Candle Prediction System — Data Pipeline (Phase 1 সিরিয়াল)

**ব্যাপার:** "OTC Future Candle Prediction System" (২০২৬-০৯-১১) — ১০-ফেজ প্ল্যানের
**প্রথম বাস্তব কাজ**: `OTC live data → historical data → candle builder →
prediction dataset`। ইউজারের নির্দেশ: **এই data pipeline ঠিক না হওয়া পর্যন্ত
AI model তৈরি করা উচিত না।** সেই শর্ত মেনে এই ডেলিভারিতে Phase 1 + 2 + 3 +
8 সম্পূর্ণ ও প্রমাণসহ তৈরি হয়েছে; Phase 4-7, 9-10 এর জন্য ইন্টারফেস প্রস্তুত।

---

## কী তৈরি হলো (Phase ম্যাপিং)

| Phase | ফাইল | অবস্থা |
|---|---|---|
| **1. OTC Data সংগ্রহ** | `feed.py` (একই প্ল্যাটফর্মের OTC tick feed → 1-min candle) + `candle_micro` টেবিল + Railway Volume + ১৫-মিনিট ব্যাকআপ + **৯০ দিন retention** | ✅ আগে থেকেই চালু; নতুন **audit** টুল দিয়ে গভীরতা দেখা যায় |
| **2. Candle Feature** | `core/otc_features.py` — ২৪টি ফিচার: body_size, upper/lower_wick, range, body/range, direction, prev-3 direction, mom_5, mom_10, streak, volatility (atr/vol_10/vol_20), hi20_pos, dist_support/resistance (ATR ইউনিট), ret_1 + same-feed microstructure (buy_pct, tick_count, is_fight) | ✅ নতুন |
| **3. Target তৈরি** | `core/otc_dataset.py` — প্রতি candle i-তে: input = i-49..i, target y1 = candle i+1 UP/DOWN, y2 = candle i+2 UP/DOWN (UP মানে close > open — অ্যাপের grading-এর সাথে এক) | ✅ নতুন |
| **8. Prediction Lock** | গঠনগতভাবে (structurally) এনফোর্সড: feature ইঞ্জিন শুধু অতীতের slice পায়; target জ্যামিতিকভাবে পরের ১ মিনিট হতে হয়; **perturbation test** প্রমাণ করে ভবিষ্যৎ candle mutate করলেও কোনো ফিচার বদলায় না | ✅ নতুন, টেস্টেড |
| 4 (XGBoost/LSTM) | `--seq-out` .npz (N × 50 × 5 raw window) LSTM/GRU-র জন্য রেডি; CSV ট্যাবুলার মডেলের জন্য রেডি | ⏳ পরের ধাপ (pipeline যাচাই ও data জমার পর) |
| 5-7, 9-10 | Price-action confirmation, confidence filter, live prediction, walk-forward backtest, paper trading | ⏳ পরের ধাপ |

## কীভাবে চালাবেন

```bash
# Phase-1 audit — কোন পেয়ারে কত দিনের ডেটা জমা হলো, gap কোথায়
python3 scripts/build_otc_dataset.py --db signals.db --audit

# Phase-8 lock proof — বাস্তব DB-র উপরেই perturbation টেস্ট
python3 scripts/build_otc_dataset.py --db signals.db --verify

# Dataset export (tabular CSV + LSTM sequence .npz)
python3 scripts/build_otc_dataset.py --db signals.db \
    --out data/otc_dataset.csv --seq-out data/otc_seq.npz

# সম্পূর্ণ integrity test suite (২২টি টেস্ট)
python3 scripts/test_otc_pipeline.py
```

## গুরুত্বপূর্ণ নিয়ম যা কোডে কাঠামোগতভাবে জোরদার

1. **Gap-free windows** — feed বাধা (reconnect/weekend) থাকলে পেয়ার সেখানে
   ভাগ হয়ে যায়; কোনো feature window বা target কখনো missing minute-এর
   উপর দিয়ে যায় না (`_gapfree_runs`)।
2. **Immediate-next targets** — প্রতিটি row-তে `t1_ctime − window_end_ctime == 60`
   এবং `t2 − t1 == 60` assert করা; অর্থাৎ T+1/T+2 সত্যিই পরের ১ মিনিট।
3. **Doji exclusion** — close == open হলে (perfect tie) সেই target row বাদ,
   সংখ্যা হিসেবে রিপোর্ট হয় (কাউন্টিং লুকানো হয় না)।
4. **কোনো interpolation নেই** — অনুপস্থিত candle কখনো বানানো হয় না।

## পরীক্ষার ফল (এই ডেলিভারিতে)

- `scripts/test_otc_pipeline.py` → **22 PASS / 0 FAIL**
  (perturbation lock, manual tamper case, target correctness, window
  invariance, doji exclusion, ctime chain, tick→candle builder, gap splitting)
- Synthetic E2E: 800 candles × 2 seeds → 1,498 rows × 32 cols CSV +
  (1498, 50, 5) sequence npz, lock 2/2 CLEAN
- Seeded-DB E2E (2 pairs × 2,600 candles): audit → completeness 100%,
  verify → 2/2 CLEAN, dataset 5,098 rows

## এখন কী হবে (acceptance path)

1. **Railway-তে এই build deploy হলে** `candle_micro` প্রতি মিনিটে জমতে থাকবে
   (Volume লাগানো থাকলে; Volume ছাড়া deploy-এ ডেটা মুছে যায় — আগের
   PERSISTENCE-FIX দেখুন)।
2. প্রতিদিন `--audit` চালিয়ে দেখুন পেয়ারপ্রতি span বাড়ছে — লক্ষ্য
   **পেয়ারপ্রতি ≥ ১৪ দিন** (২ সপ্তাহ); retention ৯০ দিন, তাই ৩ সপ্তাহ পর্যন্ত
   নিরাপদে জমবে।
3. ডেটা জমা হলে ধাপে ধাপে: Phase 4 (LightGBM baseline → walk-forward),
   Phase 6 threshold ক্যালিব্রেশন, Phase 5 confirmation, Phase 7 live
   integration — প্রতিটি ধাপে Phase 9-এর walk-forward শর্ত আগে, পরে live।

## সততার নোট

সিনথেটিক random-walk ডেটায় কোনো মডেলই ৫৫%-এর উপরে যেতে পারবে না — এটাই
প্রত্যাশিত (edge না থাকলে edge নেই)। পাইপলাইনের কাজ হলো বাস্তব OTC ডেটায়
সৎভাবে মাপা; আগের অডিটে (AUDIT_2026-09-11.md) পাওয়া candle-color
persistence এজ-টাইপ প্যাটার্ন এই ফিচার সেটে ধরা পড়বে — বাস্তব ডেটা জমার
পরেই walk-forward এ তার প্রমাণ হবে।

---

# PHASE 4 COMPLETE — বাস্তব ডেটায় সৎ বিচার (2026-09-11, OTC-PREDICT-ENGINE)

## যা তৈরি হলো (PART 6-29 ইঞ্জিন)

- `core/otc_predict/` — features_ext (PART 6+7: ৪৪ ফিচার), regime (PART 23),
  price_action (PART 12), signal_filter (PART 14+24), models (PART 10+13:
  logreg/rf/histgb + Platt calibration), walk_forward (PART 18+19),
  predictor (PART 15+16+27), tracker (PART 16+17+21+25 — freeze +
  settlement + model registry)
- `db.py` — `otc_predictions` (UNIQUE freeze key) + `model_registry` টেবিল
- `feed.py` — candle-close হলেই prediction freeze + settle + WS broadcast
- `server.py` — `/api/prediction/{asset}`, `/api/prediction-analytics`,
  `/api/prediction/models`, `/api/prediction/reload-models`
- UI — "ভবিষ্যৎ ক্যান্ডেল প্রেডিকশন" কার্ড (PART 30): NEXT CANDLE / 2ND
  CANDLE / Signal Quality / 🔒 প্রেডিকশন লক — frozen rows থেকেই রেন্ডার

## বাস্তব ডেটা ব্যাকটেস্ট (QX টোকেন দিয়ে)

- `scripts/fetch_otc_history.py` → **১২ pair × ১০ দিন × ১ মিনিট =
  ১,৭২,৭৬৯টি আসল OTC ক্যান্ডেল, gap = ০** (data/otc_history.db, gitignored)
- `scripts/backtest_otc_predictor.py` → ১,৬৭,১২৫টি leak-free row;
  walk-forward (expanding, EMBARGO=2) × ৩ candidate × T+1/T+2
- `scripts/analyze_otc_edges.py` — conditional-edge scan

### ফলাফল (সত্যি কথা)

| প্রশ্ন (PART 29) | উত্তর |
|---|---|
| মডেল কি unseen ডেটায় baseline-কে হারায়? | **না** — best candidate (RF) T+1 acc **৫০.১১%**, T+2 **৪৯.৯৮%**, logloss ≈ ln2 |
| লিকেজ আছে? | **নেই** — shuffle-probe ৫০.১%; perturbation lock CLEAN; ১০০% NO-SIGNAL সৎ আচরণ |
| Emitted WR breakeven (৫৪.০৫%)-এর উপরে? | **না** — গেট সব আটকে দিয়েছে (২,২২,১৯২ decision-এ **০টি emit** — score সর্বোচ্চ ৫৩ < ৬০) |
| Candle-color persistence এজ? | **নেই** — pooled follow-rate **৪৯.১৩%** (CI95 ৪৮.৮৯–৪৯.৩৬), কোনো ঘণ্টায় >±1.4% নেই |

**রায়:** ১০ দিনের 1m OTC ডেটায় breakeven-উর্ধ্ব কোনো সৎ edge নেই — মডেল
নিজেই সেটা বুঝে প্রায় সব ক্ষেত্রে NO TRADE দিচ্ছে (এটাই সিস্টেমের সঠিক
আচরণ)। `scripts/train_otc_model.py`-এর PART-29 gate জালিয়াতি মডেলকে
registry-তে ঢুকতে দেবে না; production-এ যতদিন না কোনো bundle gate PASS
করে, ততদিন কার্ডে সৎ "মডেল প্রস্তুত নয়" দেখাবে।

## চালানোর নিয়ম (production)

```bash
# বাস্তব ডেটা আনুন (QX_TOKEN লাগবে)
QX_TOKEN=... python3 scripts/fetch_otc_history.py --days 10
# ব্যাকটেস্ট (resumable ফেজ)
python3 scripts/backtest_otc_predictor.py --phase dataset
python3 scripts/backtest_otc_predictor.py --phase models
python3 scripts/backtest_otc_predictor.py --phase signals --model rf
python3 scripts/backtest_otc_predictor.py --phase report
# ট্রেন + গেট + রেজিস্টার (শুধুমাত্র gate PASS হলেই register হবে)
python3 scripts/train_otc_model.py --db data/signals.db --register
# টেস্ট
python3 scripts/test_otc_predict.py    # 42 PASS / 0 FAIL
```

## পরের ধাপ

1. পেয়ারপ্রতি **≥ ১৪ দিন** ডেটা জমুক (Railway Volume লাগানো আছে) —
   তারপর সপ্তাহ-প্রতি এই ব্যাকটেস্ট রিপিট করুন; যেদিন কোনো pair/সেট
   breakeven+margin পার করবে, `--register` সেদিনই মডেল লাইভ করবে।
2. Microstructure ফিচারগুলো (buy_pct/tick_count) ইতিহাসে নেই বলে
   ট্রেনিংয়ে constant — production `candle_micro` জমলে সেগুলো সক্রিয়
   হবে (PART 1-এর সম্পূর্ণ সুবিধা)।
3. Threshold টিউনিং (PART 14): বর্তমান ৮০/৭০/৬০ লাইনে emit হতে হলে
   calibrated P(UP) ≥ ~৭২% দরকার (perfect PA তে) — এটা walk-forward
   evidence দিয়েই কমানো হবে, আগভাঙা নয়।

## FAST-TRAIN (2026-09-12) — ১৪ দিন অপেক্ষা নয়, ৫–৭ মিনিটে মডেল রান

ইউজারের প্রশ্ন: "Model রান করার জন্য 14 দিন অপেক্ষা করতে হবে কেনো? … খুব
অল্প সময়ের মধ্যে মডেল ট্রেইন হবে, রান হবে। 5/7 মিনিটের মধ্যে। এই জন্য হার্ড
কোড ব্যবহার করেন।"

`core/otc_predict/fast_train.py` — সার্ভার চালু হওয়ার ~২০ সেকেন্ড পরে একটি
ব্যাকগ্রাউন্ড ডেমন স্বয়ংক্রিয়ভাবে চলে (kill switch: `QX_FAST_TRAIN=0`),
সব কনফিগ **হার্ডকোডড**:

| কনফিগ | মান | মানে |
|---|---|---|
| FAST_DAYS | ৩ | কোন পেয়ারের candle_micro ছোট হলে সেই পেয়ারের জন্য এই দিনের আসল হিস্ট্রি একই প্ল্যাটফর্ম থেকে আনা হয় |
| FAST_MIN_CANDLES | ২৫০০ | এর বেশি ক্যান্ডেল থাকলে fetch হয় না |
| FAST_MIN_PAIR_ROWS | ২০০০ | এর কম রো = ট্রেইন হবে না (ছোট স্যাম্পল নিষেধ) |
| FAST_CANDIDATES | logreg+rf | দ্রুততম পরিবার (histgb বন্ধ) |
| FAST_SHUFFLE_MAX | ০.৫৩ | **কঠোর গেট** — leakage সন্দেহে রেজিস্টারই হবে না |
| FAST_BASELINE_MARGIN_PP | ১.৫ | VERIFIED হওয়ার বার |
| FAST_RETRAIN_SECS | ৬ ঘণ্টা | লাইভ ডেটা জমার সাথে সাথে বান্ডল নিজে নিজে উন্নত হয় |

### সততার নিয়ম (PART 29 অপরিবর্তিত)

* **VERIFIED** — unseen walk-forward এ সব baseline কে ≥১.৫pp হারিয়েছে +
  logloss < ln2 + shuffle clean।
* **PROVISIONAL** — leakage নেই কিন্তু edge প্রমাণিত নয়। মডেল **চলবে**
  (প্রতি ক্যান্ডেলে T+1/T+2 প্রেডিকশন দেখাবে, ফলাফল ট্র্যাক হবে), তবে UI
  কার্ডে হলুদ **"প্রোভিশনাল"** ব্যাজ থাকবে এবং PART-14 স্কোর টিয়ার
  (<60 = NO SIGNAL, emit ≥ GOOD) আগের মতোই সিগন্যাল রক্ষা করবে।
* প্রমাণ (আসল OTC ডেটা, ১১ পেয়ার × ৩ দিন, `scripts/backtest_fast_train_real.py`):
  bootstrap ৯৫ সেকেন্ড; ১১টি পেয়ারই সৎ provisional (acc ৪৯.২–৫১.৯%,
  baseline পার হয়নি, shuffle ৪৮৯–৫১৫ সব clean); **৩,২৮৯ লাইভ-পাথ
  সিদ্ধান্ত → ০ emitted** — কয়েন-ফ্লিপ মডেল ভুয়া সিগন্যাল দিতে পারে না।

### এন্ডপয়েন্ট ও UI

* `GET /api/prediction/bootstrap` — ডেমন স্টেট + শেষ রানের ফলাফল।
* `POST /api/prediction/bootstrap` — এখনই ট্রেইন চালান (non-blocking)।
* কার্ডে মডেল না থাকলে: "স্বয়ংক্রিয় ফাস্ট-ট্রেইন ৫–৭ মিনিটের মধ্যে মডেল
  বানাবে" — আর কোনো ১৪ দিনের অপেক্ষা নেই।

### TOKEN-NAG-FIX (একই রিলিজ)

টোকেন পেস্ট করার উইন্ডো আর প্রতি রিলোডে খোলে না — খোলে শুধু (ক) টোকেন
expire/reject হলে, (খ) কোনো টোকেন না থাকলে (ট্যাব-প্রতি একবার), (গ) টোকেন
সংরক্ষিত থাকলেও ৩ মিনিটের বেশি ডেটা না আসলে (৩০ মিনিটে সর্বোচ্চ একবার)।
ইউজার বন্ধ করলে ৩০ মিনিট স্মরণ থাকে; নতুন টোকেন দিলে স্মরণ মুছে যায়।
