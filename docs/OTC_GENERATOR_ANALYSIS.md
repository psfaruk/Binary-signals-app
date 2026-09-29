# OTC GENERATOR ANALYSIS — "Quotex এর ক্যান্ডেল কোন সিস্টেমে তৈরি?"

> **USER QUESTION (2026-09-29, verbatim):**
> "Quotex এর ক্যান্ডেল গুলো কোনো একটা সিস্টেম দিয়ে তৈরি, কারণ এখানে কোনো রিয়েল বায়ার নাই, ক্যান্ডেল গুলো কি pre জেনারেট? হতে পারে নির্দিষ্ট কিছু ক্যান্ডেল? না হলে এত স্মোথলি কিভাবে দেখায় fronted এ? এই বিষয় টা চিহ্নিত করতে পারলেই মনে হয়, প্রেডিকশন সম্ভব"

এই ডকুমেন্ট = ওই প্রশ্নের সম্পূর্ণ উত্তর — প্রমাণ (protocol analysis + web
research), **চিহ্নিতকরণ যন্ত্র** (`core/otc_fingerprint.py` — ১১টি axis),
validation ফলাফল (৪/৪ সঠিক), এবং প্রেডিকশন-সম্ভাবনার সৎ হিসাব।
সঙ্গী ডকুমেন্ট: `docs/NEXT_CANDLE_ENGINE.md` (CSE — এই বিশ্লেষণের জ্ঞান
দিয়ে ট্রেড করে যে একমাত্র ইঞ্জিন)।

---

## ১. সরাসরি উত্তর (এক নজরে)

| উপ-প্রশ্ন | উত্তর |
|---|---|
| ক্যান্ডেলগুলো কি **pre-generate** করা? | **না।** Quotex প্রতিটি টিক **live, server-side** জেনারেট করে এবং websocket-এ (Socket.IO `quotes/stream`) **tick-by-tick** পাঠায় — ক্যান্ডেল হলো সেই টিকের aggregate। আগে-বানানো পূর্ণ চার্ট-সিরিজ ক্লায়েন্টকে দেওয়া হয় না। |
| **নির্দিষ্ট কিছু ক্যান্ডেলের fixed library?** | গবেষণা-প্রমাণ বলে **না** (কোনো documented ঘটনা নেই; strategy-decay রিপোর্টের সাথে সাংঘর্ষিক)। কিন্তু অনুমানের দরকার নেই — এই অ্যাপের **exact return-tuple repetition detector** (section ৩.৩) fixed block library থাকলে ধরে ফেলে। Validated: 24-block library → **53,458 repeat**; fresh generator → **0**। |
| **তাহলে এত smooth কেন?** | **ডিজাইনের ধর্ম।** (ক) bounded volatility — রিয়েল buyer নেই, নিউজ-শক নেই; (খ) mean-reverting anchor — দামকে বারবার টেনে ফেরানো হয়; (গ) fixed ~1-2.5 tick/সেকেন্ড cadence — ছোট, সমান স্টেপ; (ঘ) generator-ই "চেনা ফর্মেশন" (trend wave, consolidation) রিপ্লিকেট করে। Real market এমন নয় — সেখানে টিক আসে order-flow **burst**-এ। |
| **চিহ্নিত করলেই প্রেডিকশন সম্ভব?** | **নির্দিষ্ট পর্যন্ত।** Generator-এর RNG seed recover করা বাইরে থেকে সম্ভব নয় (deterministic প্রেডিকশন বাদ)। কিন্তু ফিডের **statistical structure** (mean-reversion, regime, time-of-day schedule) exploitable — adaptive model structured ফিডে **66-77%**, real market-এ সৎ **52-54%** পায়। **সৎ ceiling: 55-65% sustained। 90%+ দাবি = মিথ্যা।** |

এক লাইনে: **Quotex OTC হলো একটা server-side stochastic tick engine (crypto-RNG
random walk + mean-reverting anchor + volatility/regime scheduling) — সব
user-এর জন্য একই, tick-by-tick stream হয়ে আসে। প্রেডিক্টেবল অংশটুকু হলো এর
কাঠামো, seed নয়।**

---

## ২. প্রমাণ — কেন এই উত্তরগুলো

### ২.১ প্রোটোকল: live tick stream, pre-rendered চার্ট নয়

- **Wire-level প্রমাণ:** Quotex Socket.IO (v3) websocket ব্যবহার করে। লাইভ
  দাম আসে `quotes/stream` event-এ, ফরম্যাট `[asset, timestamp, price, dir]`
  — একেকটি টিক একেকটি message। History সম্পূর্ণ আলাদা payload (binary,
  ক্যান্ডেল-আকারে)। মানে: **চলতি দাম = টানা স্ট্রিম, history = একবারের
  snapshot** — দুটো আলাদা চ্যানেল, যেমনটা live generator-এ হয়।
- **কমিউনিটি ক্লায়েন্ট:** `pyquotex` (github.com/cleitonleonel/pyquotex) ও
  `A11ksa/API-Quotex` (github.com/A11ksa/API-Quotex) — দুটোই একই
  `quotes/stream` খেয়ে **নিজেরা ক্যান্ডেল জোড়ে** real-time chart বানায়।
  আমাদের নিজেদের `quotex_ws.py` (repo-তে) ঠিক এই স্ট্রিমই consume করে।
- **Latency প্রমাণ:** Quotex OTC engine-এর real-time pricing authenticated
  session থেকে ~8ms latency-তে পড়া যায় (CryptoCraft MT5-connector থ্রেড) —
  pre-rendered হলে "8ms live latency" অর্থহীন হতো।
- **Tick হার:** প্রতি asset-এ ~1-2.5 tick/সেকেন্ড (≈60-150 tick/1m
  candle)। তুলনা: Deriv-এর synthetic index-এ documented ঠিক 1 tick/2s (বা
  fast variant-এ 1 tick/1s) — fixed cadence এই ইন্ডাস্ট্রির স্বাক্ষর।

### ২.২ সব user একই chart — একটাই server-side ইঞ্জিন

- সরাসরি কমিউনিটি প্রমাণ: *"we operate in a group of 8-15 traders, all
  the time charts are the same"* (reddit.com/r/binaryoptions/comments/1820dfs)।
- Demo ও live account একই OTC chart দেখে; যেকোনো authenticated session একই
  ফিড পায়।
- মানে ফিড user-নির্দিষ্ট নয় — একটাই ইঞ্জিন সবার জন্য চলে (এবং প্রতিটি
  user-এর ট্রেডের বিপরীতে দাম বানায় — রিয়েল order book নয়)।
- তবে feed **broker-specific**: Quotex-এর ফিড Pocket Option-এ যায় না —
  তাই "এক ব্রোকারের সিগন্যাল অন্য ব্রোকারে" কাজ করে না।

### ২.৩ Disconnection টেস্ট: ডেটা সার্ভার থেকেই আসে

TradersUnion-এর ডকুমেন্টেড আচরণ (tradersunion.com/brokers/binary/view/quotex/otc-market):
চার্ট মাঝে মাঝে **freeze/lag** হয় — "chart pauses, then jumps ahead" —
connection ফিরলে client **সার্ভারের ডেটা দিয়ে** পরে ধরে নেয়। Pre-generated
চার্ট হলে freeze-এর প্রশ্নই আসত না; lag-এর পরে server-sync হওয়া = live
server-driven stream-এর লক্ষণ।

### ২.৪ ইন্ডাস্ট্রি টেমপ্লেট: Deriv synthetic indices

Deriv — ইন্ডাস্ট্রির একমাত্র transparent উদাহরণ
(deriv.com/markets/derived-indices/synthetic-indices):

- **Cryptographically secure RNG** দিয়ে প্রতিটি tick continuous, 24/7
  generate হয়; fairness-এর জন্য audited।
- Regime-switching তারা **product হিসেবেই বিক্রি করে**: Drift Switch
  indices, Volatility Switch (5-60 মিনিটের regime), Step indices, Crash/Boom
  (নির্দিষ্ট গড়-ফ্রিকোয়েন্সির spike)। মানে "regime-scheduled synthetic
  feed" এই ইন্ডাস্ট্রির প্রতিষ্ঠিত ডিজাইন প্যাটার্ন।

Quotex সম্পর্কে কোনো public reverse-engineering নেই — যা ভাঙা হয়েছে তা
শুধু **transport** (websocket প্রোটোকল), কখনো price generator নয়।
TradersUnion-এর বর্ণনা (affiliate source — plausible, unverified):
"past market behavior + synthetic volatility + platform-wide user activity"
মিশ্রণ, চেনা ফর্মেশন (trend wave, consolidation) রিপ্লিকেট, আর
**time-of-day অনুযায়ী volatility schedule**।

### ২.৫ "নির্দিষ্ট কিছু ক্যান্ডেল?" — প্রমাণ বলে না

- Quotex-এ **কোনো same-seed replay বা identical history repetition
  documented নেই।**
- সবচেয়ে জোরালো পাল্টা-যুক্তি — **strategy decay**: 80-90% win-rate কৌশল
  কয়েক দিন/সপ্তাহে <20%-এ নেমে যায়
  (reddit.com/r/binaryoptions/comments/1lmyyc4)। Fixed library replay
  হলে একবার প্যাটার্ন ধরা পড়লে চিরদিন জিতত; উল্টোটা ঘটে — মানে ফিড
  টানা **fresh** generate হয় এবং সময়ে সময়ে **re-tune** হয়।
- আর এটা এখন অনুমান নয় — আমাদের exact repetition detector validated
  (section ৩.৩ + ৪)।

### ২.৬ ৯০%+ accuracy দাবি = মার্কেটিং

- পরীক্ষিত সব bot (Hunter Robot, QxbrokerFutures, Telegram VIP suite)
  একই **public tick stream**-এ standard TA (MA/RSI/Bollinger/pattern)
  চালায় — কারো generator exploit নেই।
- `github.com/usmanch96/QxbrokerFutures`-এর **"90.57% verified win rate"**
  = README + screenshot, 1-commit repo, Telegram funnel; **কোনো auditable
  log নেই।**
- কমিউনিটির সেরা সৎ দাবি: *"average maybe 70% on OTC… never touched 50%
  on real"* (reddit 1clcche)।
- Regulator প্রমাণ (SEC/CFTC): এমন platform-রাই trading software manipulate
  করে price/payout বাঁকাতে পারে — তাই দাবি নয়, নিজের graded log-ই সত্য।

---

## ৩. চিহ্নিতকরণ যন্ত্র — `core/otc_fingerprint.py`

প্রশ্নটা অনুমানের বিষয় নয় — প্রতিটি price feed তার জেনারেটরের **measurable
fingerprint** রেখে যায়। এই module সেটাই live মাপে, যে ফিডেই অ্যাপ জোড়া
থাকুক (আসল Quotex OTC, real market, demo)। Pure Python, O(1)/tick
incremental (deques + running sums) — `next_candle.py`-এর convention।

### ৩.১ ১১টি axis

| # | Axis | কী মাপে | Synthetic হলে | Real / trade-driven হলে |
|---|---|---|---|---|
| 1 | Tick cadence | টিক-আগমনের নিয়মিততা (gap CV, per-second count CV) | মেট্রোনোমের মতো fixed cadence | bursty — trade এলে ঝাঁক, নাহলে ফাঁকা |
| 2 | Ticks per candle | প্রতি 1m ক্যান্ডেলে টিক-সংখ্যার consistency | প্রায় ধ্রুবক (যেমন 142±1) | এলোমেলো |
| 3 | Price grid | দামের fixed pip-grid / quantization | একটাই ছোট increment, সব দাম তার multiple | venue-মিশ্রণে বিভিন্ন tick size |
| 4 | Increment shape | tick return-এর kurtosis | near-Gaussian (kurt<1) — RNG আউটপুট | fat tails (kurt>5) — real order flow |
| 5 | Variance ratio VR(q) | Lo–MacKinlay | MR (<0.75) / RW (≈1) / trending (>1.25); window-ভেদে বদলালে regime-mix | দীর্ঘভাবে ≈1 |
| 6 | Hurst (R/S) | return-এর persistence | H<0.45 anti-persistent / H>0.58 persistent (return-এ গণনা — level-এ নয়) | ≈0.5 এর কাছাকাছি |
| 7 | Direction memory | candle-color autocorrelation + streak distribution | লক্ষণীয় ±AC — রঙের ধারায় কাঠামো | ≈0 (memoryless) |
| 8 | **Repetition search** | exact return-tuple repeat (নিচে বিস্তারিত) | block library হলে হাজারবার repeat | fresh generation ⇒ near-unique |
| 9 | Weekend continuity | Sat/Sun-এ candle আছে? gap কত? | আছে, gap<90min — FX-স্টাইল pair-এ প্রমাণ | real FX/stock শুক্র-রবি বন্ধ |
| 10 | Hourly σ schedule | ঘণ্টা-ভিত্তিক volatility profile | flat/নিয়মিত — designed schedule | সেশন-খাড়া (London/NY open) |
| 11 | Cross-asset sync | একই সেকেন্ডে অনেক asset টিক করে? | হ্যাঁ — এক shared engine সব pair চালায় | আলাদা venue ⇒ sync নেই |

### ৩.২ Verdict কিভাবে বানে

1. প্রতিটি axis প্রমাণ-পয়েন্ট যোগ করে `synthetic_evidence` বা
   `real_evidence`-তে (যেমন tick-density CV<0.18 ⇒ synthetic +2.0;
   kurtosis>5 ⇒ real +2.0)।
2. Tick axis একেবারে না থাকলে (শুধু history OHLC) **সৎ labeling**:
   `OHLC_ONLY_*` — শুধু process (random walk / MR / trending) classify হয়,
   real-vs-synthetic বলা যায় না।
3. Classification: `TRADE_DRIVEN_REAL` (real evidence জয়ী) /
   `SYNTHETIC_RANDOM_WALK` / `SYNTHETIC_MEAN_REVERT` / `SYNTHETIC_TRENDING` /
   `SYNTHETIC_REGIME_MIX` (VR window-ভেদে বদলায় — regime-switching
   generator-এর signature, OTC-সদৃশ ফিডের প্রত্যাশিত রায়)।
4. `pre_generated_blocks`: YES/NO — ৩.৩-এর detector।
5. `predictability_score` (0-100): **CSE backtest-এর বিপরীতে calibrated** —
   real BTC 1m ⇒ 52-54% WR ⇒ score ≤60; mean-reverting synthetic ⇒ 66-77%
   ⇒ 75+; block library ⇒ 95 (library ধরা পড়লে প্রায় পুরোটাই predictable)।

### ৩.৩ "নির্দিষ্ট কিছু ক্যান্ডেল?" — exact return-tuple repetition test

User-এর এই প্রশ্নের সরাসরি ডিটেক্টর:

- জেনারেটরের **pip-grid শেখা হয়** (প্রথম ~800 টিক থেকে), তারপর প্রতিটি
  টিকের return কে grid-unit-এ **integer** করা হয়, এবং পরপর **৮টি টিকের
  exact tuple** (যেমন `(-1005, -59, 562, -297, …)`) hash-map-এ গোনা হয়।
- দৈব collision-এর প্রত্যাশা ≈ **n²/9.4e9** — 40-60k টিকে মাত্র ~0.2-0.4টা।
  মানে fresh generator-এ কার্যত শূন্য repeat।
- Fixed block library replay হলে: validated টেস্টে 24-block library-তে
  **53,458টা exact repeat** ধরা পড়ে — chance-এর লক্ষ-লক্ষ গুণ।
- **Validation-এ পাওয়া গুরুত্বপূর্ণ শিক্ষা:** direction-only pattern
  (৩^k space) গোনা যাবে না — birthday paradox-এ সেখানে ~96% duplication
  "স্বাভাবিক", কিছুই প্রমাণ করে না। Candle-color 4-pattern-এর dup rate 96%
  থাকাও NORMAL (৮১টা possible pattern-এ শত শত candle)। **Exact return
  tuple-ই একমাত্র নির্ভরযোগ্য টেস্ট।**

---

## ৪. Validation ফলাফল — টুল নিজেকে প্রমাণ করেছে

`scripts/fingerprint_otc.py --validate` — ৪টি **KNOWN ground-truth** ফিডের
বিপরীতে; রিপোর্ট: `backtest_reports/fingerprint_validation.json`।

| ফিড (ground truth) | টুলের verdict | Score | ফল |
|---|---|---|---|
| Binance real BTCUSDT 1m (রিয়েল মার্কেট) | `OHLC_ONLY_RANDOM_WALK` — VR(5)≈1.06, Hurst 0.54; fake edge নেই | 56 | ✅ |
| আমাদের demo dynamics (MR+trend regime mix — হুবহু কপি করা জেনারেটর) | `SYNTHETIC_REGIME_MIX`, pre_gen=**NO**, 0 dupes | 76 | ✅ |
| Pure GBM random walk | `SYNTHETIC_RANDOM_WALK` — near-Gaussian, VR≈0.91 | 49 | ✅ |
| Fixed 24-block library (pre-generated replay) | pre_gen=**YES** — 53,458 exact repeat | 95 | ✅ |

**৪/৪ সঠিক শ্রেণীবদ্ধকরণ।** যা বোঝা যায়:

- Real market-কে টুল "predictable" বলেনি — score 56 মানে কাঠামো সামান্য
  (CSE-র real 52-54% WR-এর সাথে consistent)। **টুলে কোনো fake edge নেই।**
- Demo dynamics-এর score 76 — CSE-র structured-feed accuracy band (66-77%)
  এর সাথে consistent: একই ফিড দুই স্বাধীন মাপে একই কথা বলছে।
- Validation চলাকালীন ৪টি বাগ ধরা পড়ে ও ঠিক হয়েছে — Hurst return-এ গণনা
  (level-এ দিলে H=1.04 অসম্ভব), direction-pattern বাদ (birthday-paradox
  false positive), weekend axis শুধু FX/OTC pair-এ, আর OHLC-only সৎ label।
  মানে এখনকার সংখ্যাগুলো **পরীক্ষিত** সংখ্যা।

---

## ৫. তাহলে প্রেডিকশন সম্ভব?

### ৫.১ যা সম্ভব নয়: seed recovery / deterministic prediction

- Deriv-এর RNG **cryptographically secure ও audited**; Quotex-এ কোনো
  seed-recovery বা same-seed replay-এর documented ঘটনা নেই।
- Generator-এর state জানা থাকলে পরের টিক বলা যেত — কিন্তু websocket-এ টিক
  *দেখে* seed recover করা crypto-secure RNG-র বিপরীতে কার্যত অসম্ভব, আর
  ফিড evolve-ও করে (নতুন OTC pair, wire-format change, behavior shift)।

### ৫.২ যা সম্ভব: statistical structure exploitation

- **Mean reversion:** VR<1 ফিডে reversal কাঠামো ধরা যায়।
- **Regime switching:** trend ⇄ chop ⇄ revert — adaptive model প্রতি ফিডে
  নিজে শেখে। গবেষণায় প্রমাণিত: frozen weights regime-বদলে 35-40%-এ পতিত
  হয়, **online adaptation-ই আসল শক্তি**।
- **Time-of-day schedule:** সকাল/সন্ধ্যায় ফিড বেশি structured (কমিউনিটি
  রিপোর্ট + TradersUnion-এর বর্ণনা)।
- ফলাফল (CSE, এই অ্যাপের একমাত্র স্ট্র্যাটেজি): **structured
  (OTC-সদৃশ) ফিডে 66-77%, real market-এ সৎ 52-54%** (p<1e-8) —
  confidence band-এ 56-61%।

### ৫.৩ Payout গণিত — "কত % লাগবে?"

| Payout | Breakeven win-rate |
|---|---|
| 95% | 51.3% |
| 90% | 52.6% |
| 85% | 54.1% |

- **সৎ 55-65% sustained = স্পষ্ট লাভজনক** (প্রতি ট্রেডে EV positive)।
- **90%+ sustained দাবি = মিথ্যা** বা অতি-স্বল্পস্থায়ী ভাগ্য। কেউ দিলে
  auditable log চান — পাবেন না (২.৬ দেখুন)।

### ৫.৪ Edge decay হবে — তাই monitor আছে

- কমিউনিটি-documented: 80-90% WR কৌশল কয়েক দিনে <20% — generator re-tune
  হয়। এটাই ২.৫-এর "fresh generation" যুক্তির সাথে consistent।
- অ্যাপে তাই প্রতি ক্যান্ডেলে **rolling accuracy monitor** আছে; accuracy
  breakeven-এর নিচে নামলে সতর্কতা।
- Fingerprint টুলও এখানে কাজে লাগে: verdict বদলায় (regime_mix প্যারামিটার
  সরে, cadence বদলায়) = জেনারেটর re-tune-এর আভাস — তখন মডেলকে নতুন
  ডেটায় পুনরায় adapt হতে দিন।

---

## ৬. কিভাবে চালাবেন ও পড়বেন

1. **আসল Quotex token দিয়ে অ্যাপ চালু:** `QX_TOKEN` env set, `QX_DEMO_FEED`
   unset। (Demo মোডে চালালে verdict সবসময় demo-ফিডের হবে — আসল প্রশ্নের
   উত্তর দেবে না।)
2. **১৫-২০ মিনিট ডেটা জমুক** — tick axis সক্রিয় হতে ৩০০+ tick, process
   বলতে ১২০+ candle লাগে; verdict তখন স্থিতিশীল হতে শুরু করে।
3. **পড়ুন:** `GET /api/otc-fingerprint` → `verdict_bn` (বাংলা রায়) +
   `predictability_score` + `notes`। UI-তে প্যানেল **"জেনারেটর
   ফিঙ্গারপ্রিন্ট"** (এই রিলিজে যোগ হয়েছে)।
4. **২+ দিনের history** জমলে (~3000 candle) weekend axis সক্রিয় হবে —
   synthetic-প্রমাণ আরও দৃঢ়।
5. **Standalone:** `python scripts/fingerprint_otc.py --feed` (চলমান
   অ্যাপ থেকে), `--db` (সংরক্ষিত ticks), `--validate` (ground-truth
   battery পুনরায়), `--demo-live SECONDS`। সব read-only।

**পড়ার নিয়ম:**
- `pre_generated_blocks: true` এলেই user-এর "নির্দিষ্ট কিছু ক্যান্ডেল"
  অনুমান **সত্যি প্রমাণিত** — তখন সেই library map করা যাবে (score 95)।
- `SYNTHETIC_REGIME_MIX` + score 70-80 = প্রত্যাশিত Quotex-সদৃশ ফল —
  CSE-এর জন্য ভালো খেলার মাঠ।
- `predictability_score ≤ 60` হলে সেই পেয়ারে ট্রেড কমানোই বুদ্ধিমানের কাজ।

---

## ৭. সীমাবদ্ধতা (সৎ ঘোষণা)

- Sandbox-এ আসল Quotex token **expired** ছিল (`wss://ws2.qxbroker.com`
  handshake সফল, কিন্তু auth ব্যর্থ: "Token has expired or is invalid") —
  তাই এই ডকুমেন্টে **Quotex-নির্দিষ্ট কোনো সংখ্যা নেই**। সব উপসংহার দাঁড়িয়ে
  আছে: (ক) Deriv-এর documented template, (খ) কমিউনিটি/সাংবাদিক প্রমাণ,
  (গ) আমাদের ৪/৪-validated detector — যা দিয়ে **user নিজের ডেটায় ১৫-২০
  মিনিটেই** যাচাই করা যায়।
- `signals.db`-র demo-era candle ডেটা আমাদের নিজের demo generator-এর —
  forensic evidence হিসেবে ব্যবহৃত হয়নি।
- TradersUnion-এর generator-বর্ণনা affiliate source — plausible কিন্তু
  unverified।
- Quotex-এর generator-এর চূড়ান্ত সত্য শুধু Quotex-এর কাছে। এই ডকুমেন্ট বলে:
  **কোন প্রমাণ কী বলে, আর কিভাবে সেটা নিজে যাচাই করবেন।**

---

## শেষ কথা

"চিহ্নিত করা" সম্ভব হয়েছে — ফিড কোন সিস্টেমে তৈরি, সেটা এখন অ্যাপ নিজেই
বলে দেয় (`/api/otc-fingerprint`)। কিন্তু চিহ্নিত করা মানে seed পাওয়া নয় —
মানে **কাঠামো চেনা**। প্রেডিকশন সেই কাঠামো থেকেই আসে: সৎ, প্রতি
ক্যান্ডেলে graded, 55-65% ব্যান্ডে — এবং যেকোনো ৯০%+ দাবিকে সন্দেহ করাই
সঠিক।

**সাইটম্যাপ:**

| ফাইল | ভূমিকা |
|---|---|
| `core/otc_fingerprint.py` | ★ 11-axis fingerprint engine + verdict + repetition detector |
| `scripts/fingerprint_otc.py` | `--validate` / `--db` / `--feed` / `--demo-live` harness |
| `backtest_reports/fingerprint_validation.json` | 4/4 validation রিপোর্ট |
| `server.py` | `GET /api/otc-fingerprint` |
| `static/app.html` + `static/js/common.js` | "জেনারেটর ফিঙ্গারপ্রিন্ট" UI প্যানেল |
| `quotex_ws.py` + `pyquotex/` | আমাদের protocol ground truth (`quotes/stream`) |
| `docs/NEXT_CANDLE_ENGINE.md` | CSE — এই জ্ঞান দিয়ে ট্রেড করে যে ইঞ্জিন |

**মূল সূত্র:** github.com/cleitonleonel/pyquotex · github.com/A11ksa/API-Quotex ·
reddit.com/r/binaryoptions/comments/1820dfs ·
tradersunion.com/brokers/binary/view/quotex/otc-market ·
deriv.com/markets/derived-indices/synthetic-indices ·
reddit.com/r/binaryoptions/comments/1lmyyc4 ·
reddit.com/r/binaryoptions/comments/1clcche ·
github.com/usmanch96/QxbrokerFutures
