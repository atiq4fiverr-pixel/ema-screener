"""
EMA200 Rejection Screener (1h) - v2
- Crypto: MEXC Futures + Binance Spot  (Binance Futures GitHub par block hai, HTTP 451)
- Telegram alerts

Signal (1h candle, EMA200 se rejection):
  BULL = wick EMA200 ke NECHAY gayi, close EMA200 ke UPAR (support se bounce)
  BEAR = wick EMA200 ke UPAR gayi, close EMA200 ke NECHAY (resistance se reject)
  Saath mein rejection wick candle range ka kam az kam MIN_WICK_PCT % ho.

Run once: python ema200_screener.py
Loop:     python ema200_screener.py --loop
Test:     python ema200_screener.py --test
Purani candle bhi dekhni ho: python ema200_screener.py --all
"""

import os
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

# ================= SETTINGS =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

EXCHANGES = [
    "MEXC_FUTURES",
    "BINANCE_SPOT",
]
CRYPTO_TFS = ["1h"]

EMA_LEN = 200
CANDLES = 400                 # EMA200 ke liye kafi history
MIN_CANDLES = 250             # is se kam history wale (nayi listing) pairs skip

MIN_WICK_PCT = 35             # rejection wick candle range ka kam az kam itna %
BODY_STAYS_OUTSIDE = True     # True = body bhi EMA200 ki sahi taraf rahe (sirf wick ne touch kiya ho)
TOUCH_TOL_PCT = 0.3           # EMA200 ke itne % qareeb wali wick ko bhi touch maano
REQUIRE_CANDLE_COLOR = True   # BULL = green candle, BEAR = red candle

MIN_VOLUME_USDT = 1_000_000   # 24h volume filter (kam = zyada pairs)
SHOW_BULL = True
SHOW_BEAR = True
MAX_TELEGRAM_ROWS = 40

FRESH_MINUTES = 14            # candle itne minute ke andar band hui ho (dobara alert na aaye)

WORKERS = {"BINANCE_SPOT": 10, "MEXC_FUTURES": 4}
# ============================================

ALL_MODE = "--all" in sys.argv
FRESH_SECONDS = FRESH_MINUTES * 60

TF_SEC = {"15m": 900, "30m": 1800, "1h": 3600}
MEXC_INT = {"15m": "Min15", "30m": "Min30", "1h": "Min60"}

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "USDE", "XUSD", "PYUSD", "USD1"}
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
})


def http_get(bases, path, params=None):
    last_err = None
    for base in bases:
        for attempt in range(3):
            try:
                r = session.get(base + path, params=params, timeout=15)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (418, 429):
                    time.sleep(1.5 * (attempt + 1))
                    last_err = f"{base} -> HTTP {r.status_code}"
                    continue
                last_err = f"{base} -> HTTP {r.status_code}"
                break
            except Exception as e:
                last_err = str(e)
                time.sleep(0.5)
    raise RuntimeError(last_err)


def ema(values, length):
    # pehli 'length' candles ke SMA se seed (TradingView jaisa behtar match)
    if len(values) < length:
        return [values[0]] * len(values)
    k = 2 / (length + 1)
    seed = sum(values[:length]) / length
    out = [seed] * length
    for v in values[length:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def keep_closed(candles, tf_sec):
    now = time.time()
    return [x for x in candles if x[0] + tf_sec <= now + 2]


# ---------------- TELEGRAM ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("(Telegram token/chat id set nahi hai, sirf screen par dikha raha hun)")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram error:", e)


def build_messages(results):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    for r in results[:MAX_TELEGRAM_ROWS]:
        if r["side"] == "BULL":
            icon, txt = "🟢", "BULLISH rejection (EMA200 se bounce)"
        else:
            icon, txt = "🔴", "BEARISH rejection (EMA200 se reject)"
        lines.append(
            f"{icon} {r['symbol']} [{r['tf']}] ({r['ex']})\n"
            f"   {txt}\n"
            f"   Close {r['close']:.6g} | EMA200 {r['ema']:.6g}\n"
            f"   Wick {r['wick_pct']:.0f}% | EMA se doori {r['dist_pct']:+.2f}%"
        )
    header = f"EMA200 Rejection 1h ({stamp})\nMatches: {len(results)}\n"
    msgs, cur = [], header
    for ln in lines:
        if len(cur) + len(ln) + 2 > 3800:
            msgs.append(cur)
            cur = ""
        cur += "\n" + ln + "\n"
    if len(results) > MAX_TELEGRAM_ROWS:
        cur += f"\n... aur {len(results) - MAX_TELEGRAM_ROWS} signals (screen par dekho)"
    msgs.append(cur)
    return msgs


# ---------------- BINANCE SPOT ----------------
SPOT_BASES = ["https://api.binance.com", "https://data-api.binance.vision"]


def binance_spot_symbols():
    data = http_get(SPOT_BASES, "/api/v3/ticker/24hr")
    out = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLES or any(b in s for b in BAD_PARTS):
            continue
        vol = float(t["quoteVolume"])
        if vol < MIN_VOLUME_USDT:
            continue
        out.append((s, vol))
    return out


def binance_spot_candles(sym, tf):
    k = http_get(SPOT_BASES, "/api/v3/klines", {"symbol": sym, "interval": tf, "limit": CANDLES})
    candles = [(x[0] / 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in k]
    return keep_closed(candles, TF_SEC[tf])


# ---------------- MEXC FUTURES ----------------
MEXC_BASES = ["https://contract.mexc.com"]


def mexc_fut_symbols():
    data = http_get(MEXC_BASES, "/api/v1/contract/ticker")["data"]
    out = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("_USDT"):
            continue
        if s[:-5] in STABLES:
            continue
        vol = float(t.get("amount24") or 0)
        if vol < MIN_VOLUME_USDT:
            continue
        out.append((s, vol))
    return out


def mexc_fut_candles(sym, tf):
    time.sleep(0.15)
    tfs = TF_SEC[tf]
    now = int(time.time())
    start = now - CANDLES * tfs
    d = http_get(
        MEXC_BASES,
        f"/api/v1/contract/kline/{sym}",
        {"interval": MEXC_INT[tf], "start": start, "end": now},
    )["data"]
    candles = list(zip(d["time"], map(float, d["open"]), map(float, d["high"]),
                       map(float, d["low"]), map(float, d["close"])))
    return keep_closed(candles, tfs)


EX = {
    "BINANCE_SPOT": ("Binance Spot", binance_spot_symbols, binance_spot_candles),
    "MEXC_FUTURES": ("MEXC Fut", mexc_fut_symbols, mexc_fut_candles),
}


# ---------------- CORE LOGIC ----------------
def analyze(label, sym, tf, candles):
    if len(candles) < MIN_CANDLES:
        return None

    tfs = TF_SEC[tf]
    if not ALL_MODE:
        closed_ago = time.time() - (candles[-1][0] + tfs)
        if closed_ago > FRESH_SECONDS:
            return None

    closes = [x[4] for x in candles]
    e = ema(closes, EMA_LEN)[-1]

    _t, o, h, l, c = candles[-1]
    rng = h - l
    if rng <= 0:
        return None

    tol = e * TOUCH_TOL_PCT / 100
    body_top, body_bot = max(o, c), min(o, c)

    # BULL: neeche wick gayi, upar close (support se bounce)
    bull = False
    lower_wick = body_bot - l
    if SHOW_BULL and l <= e + tol and c > e:
        ok = lower_wick / rng * 100 >= MIN_WICK_PCT
        if BODY_STAYS_OUTSIDE:
            ok = ok and body_bot > e
        if REQUIRE_CANDLE_COLOR:
            ok = ok and c > o
        bull = ok

    # BEAR: upar wick gayi, neeche close (resistance se reject)
    bear = False
    upper_wick = h - body_top
    if SHOW_BEAR and h >= e - tol and c < e:
        ok = upper_wick / rng * 100 >= MIN_WICK_PCT
        if BODY_STAYS_OUTSIDE:
            ok = ok and body_top < e
        if REQUIRE_CANDLE_COLOR:
            ok = ok and c < o
        bear = ok

    if not (bull or bear):
        return None

    wick = lower_wick if bull else upper_wick
    return {
        "ex": label,
        "symbol": sym.replace("_", ""),
        "tf": tf,
        "side": "BULL" if bull else "BEAR",
        "close": c,
        "ema": e,
        "wick_pct": wick / rng * 100,
        "dist_pct": (c - e) / e * 100,
    }


def scan_exchange(key, tf, sym_cache):
    label, sym_fn, candle_fn = EX[key]
    if key not in sym_cache:
        try:
            sym_cache[key] = sym_fn()
        except Exception as e:
            print(f"[{label}] pairs list nahi mili: {e}")
            sym_cache[key] = []
        print(f"[{label}] Pairs: {len(sym_cache[key])}")
    syms = sym_cache[key]

    def job(item):
        sym, _vol = item
        try:
            return analyze(label, sym, tf, candle_fn(sym, tf))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=WORKERS.get(key, 6)) as ex:
        return [r for r in ex.map(job, syms) if r]


def scan():
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Scanning ...")
    now = time.time()
    results = []

    sym_cache = {}
    for tf in CRYPTO_TFS:
        if not ALL_MODE and now % TF_SEC[tf] > FRESH_SECONDS:
            print(f"[Crypto {tf}] abhi nayi candle close nahi hui, skip")
            continue
        for key in EXCHANGES:
            results += scan_exchange(key, tf, sym_cache)

    if not results:
        print("\nKoi signal nahi mila.")
        return

    results.sort(key=lambda r: r["wick_pct"], reverse=True)
    print(f"\nSignals: {len(results)}")
    print("-" * 88)
    print(f"{'EXCHANGE':<16}{'PAIR':<14}{'TF':<5}{'SIDE':<6}{'CLOSE':>12}{'EMA200':>12}{'WICK%':>7}{'DIST%':>8}")
    print("-" * 88)
    for r in results:
        print(
            f"{r['ex']:<16}{r['symbol']:<14}{r['tf']:<5}{r['side']:<6}{r['close']:>12.6g}{r['ema']:>12.6g}"
            f"{r['wick_pct']:>7.0f}{r['dist_pct']:>8.2f}"
        )

    for msg in build_messages(results):
        send_telegram(msg)


def wait_next_quarter():
    now = time.time()
    nxt = (int(now // 900) + 1) * 900 + 20
    time.sleep(max(1, nxt - now))


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_telegram("Test message: EMA200 screener Telegram se connect hai.")
        print("Test message bhej diya (agar token/chat id sahi hain).")
    elif "--loop" in sys.argv:
        while True:
            try:
                scan()
            except Exception as e:
                print("Error:", e)
            wait_next_quarter()
    else:
        scan()
