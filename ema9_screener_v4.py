"""
EMA9 Momentum Breakout Screener v4
- Crypto: Binance Futures, MEXC Futures, Binance Spot (default 1h)
- Gold XAUUSD (default 15m + 30m)
- Telegram alerts

Signal:
  BULL = green momentum candle jis ki body EMA9 ko break kare
         (open <= EMA9) aur body EMA9 ke UPAR close ho
  BEAR = red momentum candle jis ki body EMA9 ko break kare
         (open >= EMA9) aur body EMA9 ke NECHAY close ho

Install:  pip install requests
Run once: python ema9_screener_v4.py
Loop (apne PC par): python ema9_screener_v4.py --loop
Test Telegram:      python ema9_screener_v4.py --test
Last closed candle ignore freshness ke saath dekhna: python ema9_screener_v4.py --all
"""

import os
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

# ================= SETTINGS =================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")      # BotFather se mila token
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # tumhari chat id
# PC par chalana ho to seedha yahan likh sakte ho, masalan:
# TELEGRAM_TOKEN = "123456:ABC..."
# TELEGRAM_CHAT_ID = "123456789"

# Crypto: jo exchange nahi chahiye uski line hata do. Khali list [] = crypto band
EXCHANGES = [
    "BINANCE_FUTURES",
    "MEXC_FUTURES",
    "BINANCE_SPOT",
]
CRYPTO_TFS = ["1h"]            # options: "15m", "30m", "1h"

SCAN_GOLD = True               # XAUUSD scan on/off
GOLD_TFS = ["15m", "30m"]      # options: "15m", "30m", "1h"

EMA_LEN = 9
MIN_BODY_PCT = 60         # body kam az kam candle range ka itna % ho
BODY_MULT = 1.5           # body avg body se itni guna bari ho
AVG_BODY_LEN = 10         # average body kitni pichli candles ki
MIN_VOLUME_USDT = 5_000_000   # crypto 24h volume filter
CANDLES = 100
SHOW_BULL = True
SHOW_BEAR = True
MAX_TELEGRAM_ROWS = 40

# Signal sirf tab jab candle itne minute ke andar close hui ho (dobara alert na aaye)
FRESH_MINUTES = 14

# MEXC ki rate limit sakht hai, is liye kam workers
WORKERS = {"BINANCE_FUTURES": 10, "BINANCE_SPOT": 10, "MEXC_FUTURES": 4}
# ============================================

ALL_MODE = "--all" in sys.argv
FRESH_SECONDS = FRESH_MINUTES * 60

TF_SEC = {"15m": 900, "30m": 1800, "1h": 3600}
MEXC_INT = {"15m": "Min15", "30m": "Min30", "1h": "Min60"}
YAHOO_INT = {"15m": "15m", "30m": "30m", "1h": "60m"}

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "USDE", "XUSD", "PYUSD", "USD1"}
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ema9-screener)"})


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
                    continue
                last_err = f"{base} -> HTTP {r.status_code}"
                break
            except Exception as e:
                last_err = str(e)
                time.sleep(0.5)
    raise RuntimeError(last_err)


def ema(values, length):
    k = 2 / (length + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def keep_closed(candles, tf_sec):
    """candles = [(open_time_sec, o, h, l, c)]; abhi ban rahi candle hata do"""
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
        icon = "🟢" if r["side"] == "BULL" else "🔴"
        lines.append(
            f"{icon} {r['symbol']} [{r['tf']}] {r['side']} ({r['ex']})\n"
            f"   Close {r['close']:.6g} | EMA9 {r['ema9']:.6g}\n"
            f"   Body {r['body_pct']:.0f}% | {r['mult']:.1f}x avg | {r['chg_pct']:+.2f}%"
        )
    header = f"EMA9 Momentum Break ({stamp})\nMatches: {len(results)}\n"
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


# ---------------- BINANCE ----------------
def binance_symbols(base_urls, path):
    data = http_get(base_urls, path)
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


def binance_klines(base_urls, path, symbol, tf):
    k = http_get(base_urls, path, {"symbol": symbol, "interval": tf, "limit": CANDLES})
    candles = [(x[0] / 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in k]
    return keep_closed(candles, TF_SEC[tf])


SPOT_BASES = ["https://api.binance.com", "https://data-api.binance.vision"]
FUT_BASES = ["https://fapi.binance.com"]


def binance_spot_symbols():
    return binance_symbols(SPOT_BASES, "/api/v3/ticker/24hr")


def binance_spot_candles(sym, tf):
    return binance_klines(SPOT_BASES, "/api/v3/klines", sym, tf)


def binance_fut_symbols():
    return binance_symbols(FUT_BASES, "/fapi/v1/ticker/24hr")


def binance_fut_candles(sym, tf):
    return binance_klines(FUT_BASES, "/fapi/v1/klines", sym, tf)


# ---------------- MEXC FUTURES ----------------
MEXC_BASES = ["https://contract.mexc.com"]


def mexc_fut_symbols():
    data = http_get(MEXC_BASES, "/api/v1/contract/ticker")["data"]
    out = []
    for t in data:
        s = t["symbol"]  # e.g. BTC_USDT
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
    "BINANCE_FUTURES": ("Binance Fut", binance_fut_symbols, binance_fut_candles),
    "MEXC_FUTURES": ("MEXC Fut", mexc_fut_symbols, mexc_fut_candles),
}


# ---------------- GOLD (XAUUSD) ----------------
def yahoo_gold_candles(tf):
    d = http_get(
        ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"],
        "/v8/finance/chart/XAUUSD=X",
        {"interval": YAHOO_INT[tf], "range": "5d"},
    )
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    candles = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append((t, float(o), float(h), float(l), float(c)))
    return keep_closed(candles, TF_SEC[tf])


def gold_candles(tf):
    """Pehle Yahoo XAUUSD=X, na chale to Binance Futures XAUUSDT"""
    try:
        c = yahoo_gold_candles(tf)
        if len(c) >= AVG_BODY_LEN + EMA_LEN + 2:
            return c, "Yahoo"
    except Exception as e:
        print(f"[Gold {tf}] Yahoo fail: {e}")
    try:
        return binance_fut_candles("XAUUSDT", tf), "Binance XAUUSDT"
    except Exception as e:
        print(f"[Gold {tf}] Binance XAUUSDT fail: {e}")
    return [], ""


# ---------------- CORE LOGIC ----------------
def analyze(label, sym, tf, candles):
    if len(candles) < AVG_BODY_LEN + EMA_LEN + 2:
        return None

    tfs = TF_SEC[tf]
    if not ALL_MODE:
        closed_ago = time.time() - (candles[-1][0] + tfs)
        if closed_ago > FRESH_SECONDS:
            return None  # purani candle, alert pehle aa chuka hoga

    opens = [x[1] for x in candles]
    highs = [x[2] for x in candles]
    lows = [x[3] for x in candles]
    closes = [x[4] for x in candles]
    e = ema(closes, EMA_LEN)

    i = len(candles) - 1
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    rng = h - l
    if rng <= 0:
        return None
    body = abs(c - o)
    body_pct = body / rng * 100

    prev_bodies = [abs(closes[j] - opens[j]) for j in range(i - AVG_BODY_LEN, i)]
    avg_body = sum(prev_bodies) / len(prev_bodies)
    if avg_body <= 0:
        return None
    mult = body / avg_body

    if body_pct < MIN_BODY_PCT or mult < BODY_MULT:
        return None

    # body EMA9 ko break kare aur ek taraf close ho
    bull = SHOW_BULL and c > o and o <= e[i] < c
    bear = SHOW_BEAR and c < o and c < e[i] <= o
    if not (bull or bear):
        return None

    return {
        "ex": label,
        "symbol": sym.replace("_", ""),
        "tf": tf,
        "side": "BULL" if bull else "BEAR",
        "close": c,
        "ema9": e[i],
        "body_pct": body_pct,
        "mult": mult,
        "chg_pct": (c - o) / o * 100,
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

    # Gold
    if SCAN_GOLD:
        for tf in GOLD_TFS:
            if not ALL_MODE and now % TF_SEC[tf] > FRESH_SECONDS:
                continue
            candles, src = gold_candles(tf)
            r = analyze(src or "Gold", "XAUUSD", tf, candles)
            print(f"[XAUUSD {tf}] source: {src or 'none'} | signal: {'YES' if r else 'no'}")
            if r:
                results.append(r)

    # Crypto
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

    results.sort(key=lambda r: r["mult"], reverse=True)
    print(f"\nSignals: {len(results)}")
    print("-" * 92)
    print(f"{'EXCHANGE':<16}{'PAIR':<12}{'TF':<5}{'SIDE':<6}{'CLOSE':>12}{'EMA9':>12}{'BODY%':>7}{'xAVG':>6}{'CHG%':>7}")
    print("-" * 92)
    for r in results:
        print(
            f"{r['ex']:<16}{r['symbol']:<12}{r['tf']:<5}{r['side']:<6}{r['close']:>12.6g}{r['ema9']:>12.6g}"
            f"{r['body_pct']:>7.0f}{r['mult']:>6.1f}{r['chg_pct']:>7.2f}"
        )

    for msg in build_messages(results):
        send_telegram(msg)


def wait_next_quarter():
    now = time.time()
    nxt = (int(now // 900) + 1) * 900 + 20  # har 15 min candle close ke 20 sec baad
    time.sleep(max(1, nxt - now))


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_telegram("Test message: EMA9 screener Telegram se connect hai.")
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
