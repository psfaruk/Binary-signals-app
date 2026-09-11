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
