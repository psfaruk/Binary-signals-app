# Deep Analysis: Running Candle থেকে Prediction — কী দেখা হচ্ছে আর কী দেখা উচিত?

**Date:** 2026-07-10
**Question (Bangla):** "Ekti choloman candle deke porer candle predict kore. Amar prosno — ei running candle e ki ki dekhe, ar ki ki dekha uchit? Deep analyse korar jnno, ar koto droto dekha uchit?"

---

## 📌 সংক্ষিপ্ত উত্তর

আপনার কোড এই মুহূর্তে রানিং ক্যান্ডেল থেকে **৮টি জিনিস** দেখছে (ভাল কভারেজ), কিন্তু **৬টি গুরুত্বপূর্ণ জিনিস মিস করছে** যা প্রেডিকশন নির্ভুলতা উল্লেখযোগ্যভাবে বাড়াতে পারে। রিফ্রেশ রেট এখন **প্রতি ৩০ টিক** — এটা অনেক ধীর; আদর্শ হলো **প্রতি ৫ টিক বা প্রতি ১-২ সেকেন্ডে**।

নিচে পুরো বিশ্লেষণ।

---

## 🔍 এখন কী কী দেখা হচ্ছে (current state)

`_build_micro()` ফাংশনটি (analyze_eoc.py লাইন ১৭৬-৩৪৭) রানিং ক্যান্ডেলের টিকস থেকে ৮টি জিনিস বের করে:

| # | কী দেখে | ভ্যারিয়েবল | কী সিগন্যাল দেয় | গুরুত্ব |
|---|---------|-------------|------------------|--------|
| 1 | **Tick-weighted buyer/seller pressure** | `buy_pct`, `sell_pct` | বড় মুভমেন্টের টিকস বেশি ওজন পায় → আসল প্রেসার দিক | ⭐⭐⭐⭐⭐ |
| 2 | **Midpoint crosses (fight zone)** | `is_fight`, `crosses` | ≥৪ বার midpoint cross = buyer/seller লড়াই = uncertainty | ⭐⭐⭐ |
| 3 | **Volume profile (hold price)** | `hold_price`, `hold_visits` | কোথায় দাম বেশি সময় ছিল — support/resistance | ⭐⭐⭐ |
| 4 | **Phase momentum (early/mid/late thirds)** | `phases` (UP/DOWN/FLAT × ৩) | ক্যান্ডেলের ৩ ভাগের দিক পরিবর্তন | ⭐⭐⭐⭐ |
| 5 | **Reaction (extreme → reverse)** | `reaction` (BUYER/SELLER) | high/low ছুঁয়ে ফিরে এল = rejection | ⭐⭐⭐⭐⭐ |
| 6 | **Final-tick exhaustion/recovery** | `last_react` (EXHAUST/RECOVERY) | শেষ অংশে মোমেন্টাম মরছে কি ফিরছে | ⭐⭐⭐⭐ |
| 7 | **Tick speed / acceleration** | `tick_speed.accel` (first half vs second half speed) | মোমেন্টাম বাড়ছে কমছে | ⭐⭐⭐ |
| 8 | **Momentum shift (late direction change)** | `momentum_shift` (BULL_SHIFT/BEAR_SHIFT) | শেষ তৃতীয়াংশে দিক পরিবর্তন | ⭐⭐⭐⭐ |

আর `feed.py` এর `_analyze_microstructure()` আরও ৩টি অ্যাড-অন দেখে:
- `count_buy_pct` (টিক কাউন্ট ভিত্তিক)
- `vol_count_diverge` (volume-weighted vs count-weighted আপস)
- `round` (রাউন্ড-নাম্বার প্রক্সিমিটি)

**মোট: ~১১টি ফিচার দেখছে।**

---

## ❌ যা দেখা হচ্ছে না — কিন্তু দেখা উচিত

এই ৬টি জিনিস বর্তমান কোডে নেই, যা real-time prediction এর জন্য critical:

### ১. ⏱️ **Time-decay weighting** (টিকস কখন এসেছে)
**এখন:** সব টিক সমান ওজনে।
**কেন দরকার:** শেষ ১০ সেকেন্ডের টিক প্রথম ১০ সেকেন্ডের চেয়ে অনেক বেশি গুরুত্বপূর্ণ (close-এর কাছে প্রেসার = পরের ক্যান্ডেলের দিক)। 60-সেকেন্ডের ক্যান্ডেলে শেষ ১৫ সেকেন্ডের ওজন ৩-৫ গুণ বেশি হওয়া উচিত।

**লজিক:**
```python
weight = 1.0 + (tick_index / total_ticks) * 4.0  # প্রথম টিক=1.0, শেষ টিক=5.0
weighted_buy_vol += delta * weight
```

### ২. 📊 **Order-flow imbalance** (টিক সাইজের ডিস্ট্রিবিউশন)
**এখন:** শুধু sum করা হয়।
**কেন দরকার:** একটি বড় বায়ার টিক বনাম ১০টি ছোট সেলার টিক — sum একই হতে পারে কিন্তু অর্থ ভিন্ন। বড় টিক = institutional/profit-taking move, ছোট টিক = retail noise। টিক সাইজের std-dev বের করা উচিত, আর largest 3 ticks এর দিক আলাদা দেখা উচিত।

### ৩. 🎯 **Speed of last 5 ticks** (micro-momentum)
**এখন:** first half vs second half speed দেখা হয় (২ ভাগ)।
**কেন দরকার:** পরের ক্যান্ডেলের দিক নির্ধারণে সবচেয়ে গুরুত্বপূর্ণ হলো **শেষ ৫-১০ টিকের velocity**। ২ ভাগের বদলে last-5 / last-10 / last-20 টিকের গতি আলাদা ট্র্যাক করা উচিত।

**লজিক:**
```python
last5_speed = abs(ticks[-1] - ticks[-5]) / 5
last10_speed = abs(ticks[-1] - ticks[-10]) / 10
# Acceleration of the last 5 vs the 5 before that
accel = last5_speed / last10_speed if last10_speed > 0 else 1
```

### ৪. 📈 **Rejection wick formation** (লাইভ wick building)
**এখন:** শুধু closed candle এর wick দেখা হয় (REV theory)।
**কেন দরকার:** রানিং ক্যান্ডেলে যদি দেখা যায় high ছুঁয়ে দাম নামছে (live upper wick বাড়ছে) — সেটা এখনই PUT signal দেয়, closed candle এর জন্য অপেক্ষা করতে হবে না। live wick-to-body ratio ট্র্যাক করা উচিত।

**লজিক:**
```python
live_upper_wick = high - max(open, current)
live_lower_wick = min(open, current) - low
live_body = abs(current - open)
# If upper_wick > 2*body AND price dropping → live rejection → PUT bias
```

### ৫. 🔄 **Consecutive tick streak** (run-length encoding)
**এখন:** শুধু up_count/dn_count আছে।
**কেন দরকার:** ৫-টানা up-tick এর পর ৫-টানা down-tick = strong reversal signal (V-shape)। কিন্তু শুধু count দিয়ে এটা ধরা যায় না। সবচেয়ে দীর্ঘ same-direction streak এবং তার পরের streak এর দিক ট্র্যাক করা উচিত।

**লজিক:**
```python
streaks = []
cur_dir, cur_len = 0, 0
for i in range(1, len(ticks)):
    d = 1 if ticks[i] > ticks[i-1] else -1 if ticks[i] < ticks[i-1] else 0
    if d == cur_dir and d != 0:
        cur_len += 1
    else:
        if cur_len > 0:
            streaks.append((cur_dir, cur_len))
        cur_dir, cur_len = d, 1
# Last 2 streaks: if [UP, 5] → [DOWN, 3] = bull-to-bear reversal
```

### ৬. 🌊 **Volume-at-price acceleration** (VAP পরিবর্তন)
**এখন:** শুধু hold_price (একটি bin) দেখা হয়।
**কেন দরকার:** রানিং ক্যান্ডেলে যদি দেখা যায় hold_price উপরের দিকে সরে যাচ্ছে (volume profile migrating up) = uptrend building; নিচে সরে যাচ্ছে = downtrend। প্রতি ১০ সেকেন্ডে hold_price কোথায় সেটা ট্র্যাক করা উচিত।

---

## ⚡ কত দ্রুত দেখা উচিত? (Refresh rate)

### বর্তমান: **প্রতি ৩০ টিক** (LIVE theory re-eval)
`feed.py` লাইন ১৬৯৬:
```python
len(stream.ticks) - stream._live_reeval_ticks >= 30
```

### সমস্যা:
- ৬০-সেকেন্ড ক্যান্ডেলে ~২০০-৫০০ টিক আসে (OTC sparse: ~১০০-১৫০)
- ৩০ টিকে একবার = পুরো ক্যান্ডেলে মাত্র ৩-৫ বার re-eval
- শেষ ১০ সেকেন্ডের গুরুত্বপূর্ণ মুভমেন্ট মিস হয়

### সুপারিশ: **৩-স্তরের রিফ্রেশ**

| স্তর | টিক সংখ্যা | সময় (approx) | কী করবে |
|------|-----------|---------------|---------|
| **Micro-refresh** | প্রতি ৫ টিক | ~১-২ সেকেন্ড | last-5 speed, consecutive streak, live wick update |
| **Mid-refresh** | প্রতি ১৫ টিক | ~৫ সেকেন্ড | phase momentum, momentum shift, VAP migration |
| **Full re-eval** | প্রতি ৩০ টিক | ~১০-১৫ সেকেন্ড | পুরো analyze_eoc re-run + strength gate |

**কেন ৩ স্তরে?**
- Micro-refresh সস্তা (fast in-memory calculation), শেষ মুহূর্তের signal ধরে
- Full re-eval ব্যয়বহুল (DB query, theory blend), বেশি বার চালালে CPU/DB খাবে
- ৩৮টি always-on stream × প্রতি সেকেন্ডে full re-eval = ডেটাবেস ক্র্যাশ

### বিশেষ ক্ষেত্রে: **শেষ ১০ সেকেন্ডে আরও দ্রুত**
ক্যান্ডেলের শেষ ১০ সেকেন্ডে (যখন `time_to_close < 10s`) প্রতি **২-৩ টিকে** একবার re-eval করা উচিত — কারণ এই সময়ের টিকস পরের ক্যান্ডেলের দিক সবচেয়ে বেশি নির্ধারণ করে।

```python
time_to_close = (stream.candle_open_time + stream.period) - time.time()
if time_to_close < 10:
    reeval_interval = 2  # শেষ ১০ সেকেন্ডে প্রতি ২ টিকে
elif time_to_close < 30:
    reeval_interval = 10
else:
    reeval_interval = 30
```

---

## 🎯 প্রায়োরিটি অনুযায়ী ফিক্স তালিকা

যদি আমি কোড আপডেট করি, এই ক্রমে করব:

### Priority 1 — দ্রুত ফল আসবে (high ROI, low risk)
1. **Last-5-tick velocity tracking** — নতুন ফিচার, ~২০ লাইন কোড
2. **Refresh rate প্রতি ৩০ → প্রতি ১৫ টিক** + **শেষ ১০ সেকেন্ডে প্রতি ৩ টিকে** — এক লাইনের পরিবর্তন
3. **Consecutive tick streak** — নতুন ফিচার, ~২৫ লাইন

### Priority 2 — মাঝারি ফল (medium ROI, medium risk)
4. **Live wick formation tracker** — নতুন ফিচার, ~৩০ লাইন
5. **Time-decay weighting in `_build_micro`** — modify existing, ~১৫ লাইন
6. **Volume-at-price migration** — নতুন ফিচার, ~৩০ লাইন

### Priority 3 — পরীক্ষামূলক (high potential, high risk)
7. **Order-flow imbalance (tick size distribution)** — জটিল, কিন্তু OTC market এ খুব powerful
8. **Adaptive refresh rate** (ক্যান্ডেলের phase অনুযায়ী) — উন্নত

---

## 📊 কোথায় কী পরিবর্তন করতে হবে — ফাইল ম্যাপ

| পরিবর্তন | ফাইল | ফাংশন |
|---------|------|-------|
| Time-decay weighting | `analyze_eoc.py` | `_build_micro()` |
| Last-5/10 velocity | `analyze_eoc.py` | `_build_micro()` + `_theory_run()` |
| Live wick tracker | `analyze_eoc.py` | নতুন `_theory_live_wick()` |
| Consecutive streak | `analyze_eoc.py` | `_build_micro()` + `_theory_run()` |
| VAP migration | `analyze_eoc.py` | `_build_micro()` |
| Refresh rate (30→15/3) | `feed.py` | `_stream_loop()` লাইন ১৬৯৬ |
| Adaptive refresh | `feed.py` | নতুন helper |
| Refresh rate (sim) | `sim_feed.py` | সমান পরিবর্তন |

---

## 💡 একটি গুরুত্বপূর্ণ সতর্কতা

রানিং ক্যান্ডেলের ডেটা যত বেশি গভীরে দেখবেন, **overfitting ঝুঁকি** তত বাড়বে। 60-সেকেন্ডের ক্যান্ডেলে টিকস noise-heavy — তাই প্রতিটি নতুন ফিচার যোগ করার পর:
1. কমপক্ষে ১০০ ক্যান্ডেল লাইভ টেস্ট করুন
2. `signal_log` টেবিলে accuracy ট্র্যাক করুন
3. যে ফিচার accuracy বাড়ায় না সেটা সরিয়ে ফেলুন

অটো-ফ্লিপ সেফটি নেট (আমরা আগে যোগ করেছি) এই ঝুঁকি কিছুটা কমায় — কারণ ভুল থিওরি থাকলেও accuracy <40% হলে flip হবে।

---

## 🤔 আপনার সিদ্ধান্ত

চাইলে আমি **Priority 1** (৩টি ফিক্স) এখনই কোডে ইমপ্লিমেন্ট করতে পারি — এতে রিফ্রেশ রেট ৩ গুণ বাড়বে এবং দুটি নতুন high-value ফিচার (last-5 velocity + consecutive streak) যোগ হবে। Priority 2/3 এরপর ধাপে ধাপে যোগ করা যাবে।

বলুন কোনটা করব?
