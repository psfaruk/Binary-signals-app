/** i18n — Bengali-first dictionary with English financial terms inline.
 *  Kept as a flat typed map: no framework, no runtime dependency; an
 *  `en` table can be added later without touching components. */

export type Str =
  | "brand" | "brandSub"
  | "nav.overview" | "nav.terminal" | "nav.history" | "nav.analytics"
  | "nav.models" | "nav.settings"
  | "market" | "market.otc" | "market.real"
  | "connect.live" | "connect.backoff" | "connect.closed" | "connect.failed" | "connect.idle" | "connect.stale"
  | "banner.research" | "banner.researchSub"
  | "price" | "spread" | "period" | "countdown" | "nextCandle"
  | "signal.title" | "signal.state" | "signal.direction" | "signal.confidence"
  | "signal.strength" | "signal.strategy" | "signal.regime" | "signal.reason"
  | "signal.target" | "signal.entry" | "signal.evidence" | "signal.opposing"
  | "signal.timingVerified" | "signal.modelValidated" | "signal.eligible"
  | "signal.call" | "signal.put" | "signal.noSignal" | "signal.research"
  | "feed.fresh" | "feed.staleSeconds" | "feed.lastTick" | "feed.health"
  | "overview.today" | "overview.feedHealth" | "overview.pairs"
  | "overview.recentResults" | "overview.dataFreshness" | "overview.truthState"
  | "overview.noInvented"
  | "history.search" | "history.pair" | "history.direction" | "history.result"
  | "history.all" | "history.win" | "history.loss" | "history.ungraded"
  | "history.empty" | "history.export" | "history.detail" | "history.page"
  | "analytics.winrate" | "analytics.callPerf" | "analytics.putPerf"
  | "analytics.pairPerf" | "analytics.hourPerf" | "analytics.daily"
  | "analytics.modelPerf" | "analytics.strategyPerf" | "analytics.sample"
  | "analytics.breakeven" | "analytics.researchOnly"
  | "models.available" | "models.status" | "models.version" | "models.training"
  | "models.validation" | "models.oos" | "models.lastTrained" | "models.errors"
  | "models.none"
  | "settings.appearance" | "settings.dark" | "settings.light"
  | "settings.chart" | "settings.sound" | "settings.refresh"
  | "settings.diagnostics" | "settings.language"
  | "common.loading" | "common.error" | "common.retry" | "common.unavailable"
  | "common.noData" | "common.close" | "common.apply" | "common.saved"
  | "common.statSignals" | "common.statWinrate" | "common.statSamples"
  | "terminal.chart" | "terminal.recentSignals" | "terminal.tickInfo";

export const bn: Record<Str, string> = {
  "brand": "বাইনারি সিগন্যাল",
  "brandSub": "টার্মিনাল v2",
  "nav.overview": "ওভারভিউ",
  "nav.terminal": "টার্মিনাল",
  "nav.history": "সিগন্যাল হিস্টোরি",
  "nav.analytics": "রেজাল্ট ও অ্যানালিটিক্স",
  "nav.models": "মডেল",
  "nav.settings": "সেটিংস",
  "market": "মার্কেট",
  "market.otc": "OTC",
  "market.real": "Real",
  "connect.live": "লাইভ",
  "connect.backoff": "পুনঃসংযোগ…",
  "connect.closed": "বন্ধ",
  "connect.failed": "সংযোগ ব্যর্থ",
  "connect.idle": "নিষ্ক্রিয়",
  "connect.stale": "পুরোনো ডেটা",
  "banner.research": "গবেষণা মোড — কোনো অ্যাকশনেবল সিগন্যাল নেই",
  "banner.researchSub":
    "ডেটা সোর্স অননুমোদিত ও মডেল অ-যাচাইকৃত; সব প্রেডিকশন RESEARCH-ONLY। " +
    "এগুলো ট্রেড নির্দেশনা নয়।",
  "price": "মূল্য",
  "spread": "স্প্রেড",
  "period": "পিরিয়ড",
  "countdown": "কাউন্টডাউন",
  "nextCandle": "পরবর্তী ক্যান্ডেল",
  "signal.title": "সিগন্যাল ইন্টেলিজেন্স",
  "signal.state": "স্টেট",
  "signal.direction": "দিক",
  "signal.confidence": "কনফিডেন্স",
  "signal.strength": "শক্তি",
  "signal.strategy": "স্ট্র্যাটেজি",
  "signal.regime": "মার্কেট রেজিম",
  "signal.reason": "বিরত থাকার কারণ",
  "signal.target": "টার্গেট ক্যান্ডেল",
  "signal.entry": "এন্ট্রি টাইম",
  "signal.evidence": "সপোর্টিং এভিডেন্স",
  "signal.opposing": "অপোজিং এভিডেন্স",
  "signal.timingVerified": "টাইমিং যাচাই",
  "signal.modelValidated": "মডেল যাচাই",
  "signal.eligible": "এলিজিবিলিটি",
  "signal.call": "CALL",
  "signal.put": "PUT",
  "signal.noSignal": "NO_SIGNAL",
  "signal.research": "RESEARCH",
  "feed.fresh": "ফ্রেশ",
  "feed.staleSeconds": "সেকেন্ড আগের ডেটা",
  "feed.lastTick": "শেষ টিক",
  "feed.health": "ফিড হেলথ",
  "overview.today": "আজকের সিগন্যাল",
  "overview.feedHealth": "ফিড হেলথ",
  "overview.pairs": "অ্যাক্টিভ পেয়ার",
  "overview.recentResults": "সাম্প্রতিক রেজাল্ট",
  "overview.dataFreshness": "ডেটা ফ্রেশনেস",
  "overview.truthState": "ট্রুথ লেয়ার",
  "overview.noInvented":
    "কোনো বানানো পরিসংখ্যান নেই — সব সংখ্যা ভেরিফায়েড সোর্স থেকে।",
  "history.search": "পেয়ার খুঁজুন…",
  "history.pair": "পেয়ার",
  "history.direction": "দিক",
  "history.result": "ফলাফল",
  "history.all": "সব",
  "history.win": "উইন",
  "history.loss": "লস",
  "history.ungraded": "মূল্যায়ন বাকি",
  "history.empty": "এই ফিল্টারে কোনো সিগন্যাল নেই",
  "history.export": "এক্সপোর্ট (CSV)",
  "history.detail": "সিগন্যাল বিস্তারিত",
  "history.page": "পেজ",
  "analytics.winrate": "ওভারাল ভেরিফায়েড উইনরেট",
  "analytics.callPerf": "CALL পারফরম্যান্স",
  "analytics.putPerf": "PUT পারফরম্যান্স",
  "analytics.pairPerf": "পেয়ার-ওয়াইজ",
  "analytics.hourPerf": "আওয়ার-ওয়াইজ",
  "analytics.daily": "ডেইলি",
  "analytics.modelPerf": "মডেল পারফরম্যান্স",
  "analytics.strategyPerf": "স্ট্র্যাটেজি পারফরম্যান্স",
  "analytics.sample": "স্যাম্পল সাইজ",
  "analytics.breakeven": "ব্রেক-ইভেন তুলনা (payout-aware)",
  "analytics.researchOnly":
    "সব পারফরম্যান্স সংখ্যা রিসার্চ-গ্রেড — স্যাম্পল ছোট হলে কোনো পরিসংখ্যানই নির্ভরযোগ্য নয়।",
  "models.available": "উপলব্ধ মডেল",
  "models.status": "স্টেটাস",
  "models.version": "ভার্সন",
  "models.training": "ট্রেনিং",
  "models.validation": "ভ্যালিডেশন",
  "models.oos": "Out-of-Sample",
  "models.lastTrained": "শেষ ট্রেনিং",
  "models.errors": "মডেল ত্রুটি",
  "models.none": "কোনো যাচাইকৃত মডেল নেই — Truth Layer অনুযায়ী সঠিক",
  "settings.appearance": "অ্যাপিয়ারেন্স",
  "settings.dark": "ডার্ক",
  "settings.light": "লাইট",
  "settings.chart": "চার্ট",
  "settings.sound": "সাউন্ড",
  "settings.refresh": "ডেটা রিফ্রেশ",
  "settings.diagnostics": "সিস্টেম ডায়াগনস্টিক্স",
  "settings.language": "ভাষা",
  "common.loading": "লোড হচ্ছে…",
  "common.error": "ত্রুটি",
  "common.retry": "আবার চেষ্টা",
  "common.unavailable": "অনুপলব্ধ",
  "common.noData": "ডেটা নেই",
  "common.close": "বন্ধ",
  "common.apply": "প্রয়োগ",
  "common.saved": "সেভ হয়েছে",
  "common.statSignals": "সিগন্যাল",
  "common.statWinrate": "উইনরেট",
  "common.statSamples": "নমুনা",
  "terminal.chart": "লাইভ চার্ট",
  "terminal.recentSignals": "সাম্প্রতিক সিগন্যাল",
  "terminal.tickInfo": "টিক তথ্য",
};

export function t(key: Str): string { return bn[key]; }
