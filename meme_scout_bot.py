"""
Meme Scout Telegram Bot — Solana
Same screening rules as the meme coin scout, with market cap capped at $500K:
  - Market cap $150K–$500K
  - Pair at least 6 hours old
  - Liquidity at least $50K and at least 8% of market cap
  - 24h volume at least $300K and at least 1,000 trades in 24h
  - Not dumping: 6h change better than -25%, 1h change better than -20%
  - No RugCheck "danger" risks; skips stablecoins, tokenized stocks and copycats
Each coin is alerted only once.

Screen only — not trading advice. Always check rugcheck.xyz before buying.

Setup:
  1. pip install requests
  2. In Telegram, message @BotFather -> /newbot -> copy the token.
  3. Paste the token into BOT_TOKEN below (or set env var TG_BOT_TOKEN).
  4. Run:  python meme_scout_bot.py
     Then send /start to your bot in Telegram.
     The bot saves your chat ID automatically and starts alerting.
"""

import json
import math
import os
import time
from datetime import datetime, timezone

import requests

# ───────────────────────── SETTINGS ─────────────────────────
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("TG_CHAT_ID", "")      # filled in automatically on first /start

MAX_MCAP = 500_000          # market cap at or under this (USD)
MIN_MCAP = 150_000          # market cap at or above this (USD)
MIN_PAIR_AGE_HOURS = 6      # skip fresh sniper launches
MIN_LIQUIDITY = 50_000      # USD
MIN_LIQ_TO_MCAP = 0.08      # liquidity at least 8% of market cap
MIN_VOLUME_24H = 300_000    # USD
MIN_TXNS_24H = 1_000        # buys + sells in 24h (proxy for trader count)
MIN_H6_CHANGE = -25.0       # 6h change must be better than this (%)
MIN_H1_CHANGE = -20.0       # 1h change must be better than this (%)
MAX_ALERTS_PER_SCAN = 3     # top 3 new coins per scan, like the scout
USE_RUGCHECK = True         # drop coins with RugCheck "danger" risks
SCOUT_ALERTS_ON = True      # the regular scout-rule alerts above

# ── Breakout alerts: sideways range for a day or more, then a sharp break up ──
BREAKOUT_ALERTS_ON = True
BO_MAX_MCAP = 500_000       # market cap at or under this when the breakout is spotted
BO_MIN_MCAP = 30_000        # ignore dust
BO_MIN_LIQUIDITY = 10_000   # USD
BO_RANGE_HOURS = 24         # the range must last at least this many hours
BO_MAX_RANGE_WIDTH = 2.2    # range top ÷ range bottom (2.2 = top is at most 2.2x the bottom)
BO_MIN_MULTIPLE = 2.0       # market cap now ÷ middle of the range (2.0 = doubled)
BO_MIN_ABOVE_TOP = 1.25     # price now ÷ range top (1.25 = 25% above the top of the range)
BO_MIN_VOLUME_SPIKE = 3.0   # last 2h volume vs the range's normal hourly volume
BO_MAX_CHECKS = 10          # coins to inspect per scan (candle data has a rate limit)

HEARTBEAT_HOURS = 6         # GitHub mode: "still running" summary in Telegram this often (0 = off)

SCAN_EVERY_SECONDS = 300    # scan every 5 minutes
STATE_FILE = "meme_scout_bot_state.json"
# ────────────────────────────────────────────────────────────

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
HEADERS = {"User-Agent": "meme-scout-bot/1.0", "Accept": "application/json"}
SKIP_SYMBOLS = {"USDC", "USDT", "PYUSD", "USDS", "USDE", "DAI", "SOL", "WSOL", "JUP", "BONK", "WIF"}


# ───────────────────────── helpers ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"chat_id": "", "alerted": {}, "tg_offset": 0}


def save_state(state):
    data = dict(state)
    if os.getenv("RUN_ONCE") == "1":
        data.pop("chat_id", None)   # on GitHub the state file is public; chat ID stays in secrets
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_json(url, params=None, timeout=15):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 429:
            print("  rate limited, waiting 30s")
            time.sleep(30)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  request failed: {url} -> {e}")
        return None


def fmt_usd(x):
    x = x or 0
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:,.0f}"


def pair_age_hours(p):
    created = p.get("pairCreatedAt")
    return (time.time() * 1000 - created) / 3_600_000 if created else 0


def age_text(p):
    h = pair_age_hours(p)
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}d"


# ───────────────────────── Telegram ─────────────────────────
def tg_send(chat_id, text):
    try:
        requests.post(
            f"{TG}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        print(f"  telegram send failed: {e}")


def tg_poll_commands(state, last_scan_info):
    """Reads new Telegram messages: /start, /status, /alert, /alerts, /remove, /help."""
    data = get_json(f"{TG}/getUpdates",
                    params={"offset": state.get("tg_offset", 0), "timeout": 0})
    if not data or not data.get("ok"):
        return
    changed = False
    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        changed = True
        msg = upd.get("message") or {}
        chat = msg.get("chat", {})
        text = (msg.get("text") or "").strip()
        if not chat:
            continue
        cid = str(chat["id"])
        if not state.get("chat_id"):
            state["chat_id"] = cid
            tg_send(cid, "✅ Meme Scout Bot connected.\n"
                         f"Screening Solana memes at {fmt_usd(MIN_MCAP)}–{fmt_usd(MAX_MCAP)} market cap "
                         f"every {SCAN_EVERY_SECONDS // 60} min.\nSend /help to see commands.")
            print(f"  chat ID saved: {cid}")
            continue
        if cid != state["chat_id"]:
            continue   # ignore anyone else who finds the bot
        handle_command(state, text, last_scan_info)
    if changed:
        save_state(state)


HELP_TEXT = (
    "🔔 <b>Price alerts</b>\n"
    "<code>/alert ADDRESS 500k</code> — alert when market cap reaches $500K\n"
    "<code>/alert ADDRESS 500k 1m 150k</code> — several targets at once\n"
    "<code>/alert ADDRESS above 2m</code> / <code>below 100k</code> — set the direction yourself\n"
    "<code>/alert ADDRESS 0.00012</code> — use a price instead of market cap (any value under $1)\n"
    "ADDRESS can be the contract address or a DexScreener link.\n\n"
    "<code>/alerts</code> — list your alerts\n"
    "<code>/remove 2</code> — remove alert #2 · <code>/remove all</code>\n"
    "<code>/status</code> — is the bot running\n\n"
    "Each alert fires once, then is removed."
)


def handle_command(state, text, last_scan_info):
    words = text.split()
    if not words:
        return
    cmd = words[0].lower().split("@")[0]
    chat = state["chat_id"]
    if cmd in ("/help", "/start"):
        tg_send(chat, HELP_TEXT)
    elif cmd == "/status":
        tg_send(chat, f"🟢 Running.\nLast scan: {last_scan_info.get('text', 'see the 6-hour summary')}\n"
                      f"Price alerts set: {len(state.get('price_alerts', []))}")
    elif cmd == "/alert":
        add_price_alerts(state, words[1:])
    elif cmd == "/alerts":
        list_price_alerts(state)
    elif cmd == "/remove":
        remove_price_alerts(state, words[1:])
    else:
        tg_send(chat, "I didn't get that. Send /help to see the commands.")


# ───────────────────────── scanning ─────────────────────────
def collect_candidate_addresses():
    """Gather Solana token addresses from trending/new feeds."""
    addrs = set()
    for path, pages in (("networks/solana/trending_pools", 5),
                        ("networks/solana/new_pools", 2)):
        for page in range(1, pages + 1):
            d = get_json(f"{GECKO}/{path}", params={"page": page})
            for pool in (d or {}).get("data", []):
                tid = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
                if tid.startswith("solana_"):
                    addrs.add(tid.split("_", 1)[1])
            time.sleep(2.1)  # stay under GeckoTerminal's 30 calls/min
    for path in ("token-profiles/latest/v1", "token-boosts/latest/v1", "token-boosts/top/v1"):
        for item in get_json(f"{DEX}/{path}") or []:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                addrs.add(item["tokenAddress"])
    return list(addrs)


def fetch_best_pairs(addresses):
    """Batch-look-up pairs on DexScreener (30 per call); keep the most liquid pair per token."""
    best = {}
    for i in range(0, len(addresses), 30):
        chunk = addresses[i:i + 30]
        for p in get_json(f"{DEX}/tokens/v1/solana/{','.join(chunk)}") or []:
            addr = p.get("baseToken", {}).get("address")
            if addr not in chunk:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if addr not in best or liq > ((best[addr].get("liquidity") or {}).get("usd") or 0):
                best[addr] = p
        time.sleep(0.3)
    return best


def passes(p):
    bt = p.get("baseToken", {})
    sym = (bt.get("symbol") or "").upper()
    name = (bt.get("name") or "").lower()
    if sym in SKIP_SYMBOLS or "xstock" in name or "tokenized" in name:
        return False
    mcap = p.get("marketCap") or p.get("fdv") or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol24 = (p.get("volume") or {}).get("h24") or 0
    t24 = (p.get("txns") or {}).get("h24") or {}
    txns24 = (t24.get("buys") or 0) + (t24.get("sells") or 0)
    pc = p.get("priceChange") or {}
    return (MIN_MCAP <= mcap <= MAX_MCAP
            and pair_age_hours(p) >= MIN_PAIR_AGE_HOURS
            and liq >= MIN_LIQUIDITY
            and liq >= MIN_LIQ_TO_MCAP * mcap
            and vol24 >= MIN_VOLUME_24H
            and txns24 >= MIN_TXNS_24H
            and (pc.get("h6") or 0) > MIN_H6_CHANGE
            and (pc.get("h1") or 0) > MIN_H1_CHANGE)


def drop_copycats(pairs):
    """If several coins share a name/ticker, keep only the one with the most volume."""
    best = {}
    for p in pairs:
        bt = p.get("baseToken", {})
        key = (bt.get("symbol") or "").upper()
        vol = (p.get("volume") or {}).get("h24") or 0
        if key not in best or vol > ((best[key].get("volume") or {}).get("h24") or 0):
            best[key] = p
    return list(best.values())


def score(p):
    """Rank by healthy 6h momentum plus volume, like the scout."""
    h6 = (p.get("priceChange") or {}).get("h6") or 0
    vol = (p.get("volume") or {}).get("h24") or 0
    return min(h6, 300) / 100 + vol / 1_000_000


def rugcheck(addr):
    """Returns (ok, summary_text). ok=False if any 'danger' risk."""
    d = get_json(f"{RUGCHECK}/{addr}/report/summary")
    if not d:
        return True, "RugCheck: unavailable"
    risks = d.get("risks") or []
    if any((r.get("level") or "").lower() == "danger" for r in risks):
        return False, ""
    warns = [r.get("name") for r in risks if (r.get("level") or "").lower() == "warn"]
    lp = d.get("lpLockedPct")
    parts = [f"RugCheck score {d.get('score_normalised', '?')}/10"]
    if lp is not None:
        parts.append(f"LP locked {lp:.0f}%")
    text = " · ".join(parts)
    if warns:
        text += f"\n⚠️ {', '.join(warns[:3])}"
    return True, text


def alert_text(p, rc_text):
    bt = p.get("baseToken", {})
    pc = p.get("priceChange") or {}
    t1 = (p.get("txns") or {}).get("h1") or {}
    t24 = (p.get("txns") or {}).get("h24") or {}
    info = p.get("info") or {}
    has_x = any(s.get("type") == "twitter" for s in info.get("socials") or [])
    has_site = bool(info.get("websites"))
    addr = bt.get("address", "")
    concern = ""
    if (t1.get("sells") or 0) > (t1.get("buys") or 0):
        concern = "\nNote: more sellers than buyers in the last hour."
    return (
        f"🆕 <b>{bt.get('name', '?')} (${bt.get('symbol', '?')})</b>\n"
        f"MC {fmt_usd(p.get('marketCap') or p.get('fdv'))} · "
        f"Liq {fmt_usd((p.get('liquidity') or {}).get('usd'))} · Age {age_text(p)}\n"
        f"1h {pc.get('h1', 0):+.0f}% · 6h {pc.get('h6', 0):+.0f}% · 24h {pc.get('h24', 0):+.0f}%\n"
        f"Vol 24h {fmt_usd((p.get('volume') or {}).get('h24'))}\n"
        f"Buys/sells 1h: {t1.get('buys', 0)}/{t1.get('sells', 0)} · "
        f"24h: {t24.get('buys', 0)}/{t24.get('sells', 0)}\n"
        f"X: {'yes' if has_x else 'no'} · Website: {'yes' if has_site else 'no'}\n"
        f"{rc_text}{concern}\n"
        f"<code>{addr}</code>\n"
        f"<a href=\"{p.get('url', '')}\">DexScreener</a> · "
        f"<a href=\"https://rugcheck.xyz/tokens/{addr}\">RugCheck</a>\n"
        f"<i>Screen only, not advice — check rugcheck.xyz before buying.</i>"
    )


# ───────────────────────── price alerts ─────────────────────────
MAX_PRICE_ALERTS = 30


def fmt_price(p):
    p = float(p or 0)
    if p <= 0:
        return "$0"
    if p >= 1:
        return f"${p:,.2f}"
    decimals = min(12, max(2, -int(math.floor(math.log10(p))) + 3))
    return f"${p:.{decimals}f}"


def best_pair_for_token(addr):
    best = None
    for p in get_json(f"{DEX}/tokens/v1/solana/{addr}") or []:
        if p.get("baseToken", {}).get("address") != addr:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if best is None or liq > ((best.get("liquidity") or {}).get("usd") or 0):
            best = p
    return best


def resolve_token(text):
    """Accepts a contract address or a DexScreener link; returns the token's best pair or None."""
    x = text.strip().rstrip("/").split("?")[0].split("/")[-1]
    if not (30 <= len(x) <= 50):
        return None
    pair = best_pair_for_token(x)
    if pair:
        return pair
    d = get_json(f"{DEX}/latest/dex/pairs/solana/{x}") or {}
    found = d.get("pair") or (d.get("pairs") or [None])[0]
    if found and found.get("baseToken", {}).get("address"):
        return best_pair_for_token(found["baseToken"]["address"]) or found
    return None


def parse_amount(s):
    """'500k' -> (500000, True), '0.00012' -> (0.00012, False). Second value = has k/m/b suffix."""
    s = s.lower().replace("$", "").replace(",", "")
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}
    suffix = s[-1:] if s[-1:] in mult else ""
    try:
        v = float(s[:-1] if suffix else s)
    except ValueError:
        return None, False
    return (v * mult[suffix] if suffix else v), bool(suffix)


def add_price_alerts(state, args):
    chat = state["chat_id"]
    if len(args) < 2:
        tg_send(chat, "Use: <code>/alert ADDRESS 500k</code>\nSend /help for more examples.")
        return
    pair = resolve_token(args[0])
    if not pair:
        tg_send(chat, "❌ I couldn't find that coin on Solana. Check the address or DexScreener link.")
        return
    alerts = state.setdefault("price_alerts", [])
    bt = pair["baseToken"]
    price = float(pair.get("priceUsd") or 0)
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    direction, lines = None, []
    for w in args[1:]:
        lw = w.lower()
        if lw in ("above", ">", "over", "up"):
            direction = "above"; continue
        if lw in ("below", "<", "under", "down"):
            direction = "below"; continue
        if lw in ("mc", "mcap", "price"):
            continue
        value, has_suffix = parse_amount(w)
        if value is None or value <= 0:
            lines.append(f"❌ '{w}' isn't a number I understand")
            continue
        if has_suffix or value >= 1000:
            kind, current = "mc", mcap
        elif value < 1:
            kind, current = "price", price
        else:
            lines.append(f"❌ '{w}': write market cap like 500k / 1.2m, or price like 0.00012")
            continue
        if not current:
            lines.append("❌ No live price for this coin right now")
            continue
        d = direction or ("above" if value > current else "below")
        if len(alerts) >= MAX_PRICE_ALERTS:
            lines.append(f"❌ You already have {MAX_PRICE_ALERTS} alerts. Remove some with /remove")
            break
        aid = max([a["id"] for a in alerts], default=0) + 1
        alerts.append({"id": aid, "token": bt["address"], "pair": pair.get("pairAddress", ""),
                       "name": bt.get("name", "?"), "symbol": bt.get("symbol", "?"),
                       "kind": kind, "dir": d, "target": value,
                       "set_at": time.time(), "last_check": time.time()})
        shown = fmt_usd(value) if kind == "mc" else fmt_price(value)
        lines.append(f"✅ #{aid}: {'market cap' if kind == 'mc' else 'price'} {d} {shown}")
    tg_send(chat, f"🔔 <b>{bt.get('name', '?')} (${bt.get('symbol', '?')})</b>\n"
                  f"Now: MC {fmt_usd(mcap)} · price {fmt_price(price)}\n" + "\n".join(lines) +
                  "\n<i>Checked every scan (about every 5–15 min).</i>")


def list_price_alerts(state):
    alerts = state.get("price_alerts", [])
    if not alerts:
        tg_send(state["chat_id"], "No price alerts set. Example: <code>/alert ADDRESS 500k</code>")
        return
    rows = []
    for a in alerts:
        shown = fmt_usd(a["target"]) if a["kind"] == "mc" else fmt_price(a["target"])
        rows.append(f"#{a['id']} {a['name']} (${a['symbol']}) — "
                    f"{'MC' if a['kind'] == 'mc' else 'price'} {a['dir']} {shown}")
    tg_send(state["chat_id"], "🔔 <b>Your price alerts</b>\n" + "\n".join(rows) +
            "\n\nRemove one with <code>/remove 2</code>")


def remove_price_alerts(state, args):
    alerts = state.get("price_alerts", [])
    if not args:
        tg_send(state["chat_id"], "Use <code>/remove 2</code> or <code>/remove all</code>. See /alerts.")
        return
    if args[0].lower() == "all":
        state["price_alerts"] = []
        tg_send(state["chat_id"], f"🗑 Removed all {len(alerts)} alerts.")
        return
    ids = {int(x.strip("#,")) for x in args if x.strip("#,").isdigit()}
    keep = [a for a in alerts if a["id"] not in ids]
    removed = len(alerts) - len(keep)
    state["price_alerts"] = keep
    tg_send(state["chat_id"], f"🗑 Removed {removed} alert(s)." if removed else
            "No alert with that number. Send /alerts to see the numbers.")


def price_range_since(pool, since_ts):
    """Highest high and lowest low (USD price) from 5-minute candles since since_ts."""
    if not pool:
        return None, None
    minutes = max(5, (time.time() - since_ts) / 60)
    d = get_json(f"{GECKO}/networks/solana/pools/{pool}/ohlcv/minute",
                 params={"aggregate": 5, "limit": min(200, int(minutes // 5) + 2),
                         "currency": "usd", "token": "base"})
    rows = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    rows = [r for r in rows if len(r) >= 6 and r[0] >= since_ts - 300]
    if not rows:
        return None, None
    return max(float(r[2]) for r in rows), min(float(r[3]) for r in rows)


def check_price_alerts(state):
    """Fires alerts whose target was touched since the last check (uses candle highs/lows,
    so a quick spike between scans still counts). Returns number fired."""
    alerts = state.get("price_alerts", [])
    if not alerts:
        return 0
    pairs = fetch_best_pairs(list({a["token"] for a in alerts}))
    keep, fired = [], 0
    for a in alerts:
        p = pairs.get(a["token"])
        if not p:
            keep.append(a)
            continue
        price = float(p.get("priceUsd") or 0)
        mcap = p.get("marketCap") or p.get("fdv") or 0
        if price <= 0:
            keep.append(a)
            continue
        hi, lo = price_range_since(a.get("pair") or p.get("pairAddress"), a["last_check"])
        time.sleep(2.1)
        hi = max(hi or price, price)
        lo = min(lo or price, price)
        to_mc = mcap / price if mcap else 0
        if a["kind"] == "mc":
            hi, lo = hi * to_mc, lo * to_mc
        hit = hi >= a["target"] if a["dir"] == "above" else lo <= a["target"]
        a["last_check"] = time.time()
        if not hit:
            keep.append(a)
            continue
        fired += 1
        pc = p.get("priceChange") or {}
        addr = a["token"]
        shown = fmt_usd(a["target"]) if a["kind"] == "mc" else fmt_price(a["target"])
        arrow = "📈" if a["dir"] == "above" else "📉"
        tg_send(state["chat_id"],
                f"🔔{arrow} <b>PRICE ALERT: {a['name']} (${a['symbol']})</b>\n"
                f"{'Market cap' if a['kind'] == 'mc' else 'Price'} went {a['dir']} {shown}\n"
                f"Now: MC <b>{fmt_usd(mcap)}</b> · price {fmt_price(price)}\n"
                f"5m {pc.get('m5', 0):+.0f}% · 1h {pc.get('h1', 0):+.0f}% · 24h {pc.get('h24', 0):+.0f}%\n"
                f"<code>{addr}</code>\n"
                f"<a href=\"{p.get('url', '')}\">DexScreener</a> · "
                f"<a href=\"https://rugcheck.xyz/tokens/{addr}\">RugCheck</a>\n"
                f"<i>Alert #{a['id']} done. Set a new one with /alert.</i>")
    state["price_alerts"] = keep
    return fired


# ───────────────────────── breakout detection ─────────────────────────
def might_be_breakout(p):
    """Cheap first filter from DexScreener data, before fetching candles."""
    mcap = p.get("marketCap") or p.get("fdv") or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0
    pc = p.get("priceChange") or {}
    sym = (p.get("baseToken", {}).get("symbol") or "").upper()
    return (sym not in SKIP_SYMBOLS
            and BO_MIN_MCAP <= mcap <= BO_MAX_MCAP
            and liq >= BO_MIN_LIQUIDITY
            and pair_age_hours(p) >= BO_RANGE_HOURS + 3
            and ((pc.get("h1") or 0) >= 30 or (pc.get("h6") or 0) >= 60))


def hourly_candles(pool_address):
    """Newest-first list of (ts, open, high, low, close, volume) in USD."""
    d = get_json(f"{GECKO}/networks/solana/pools/{pool_address}/ohlcv/hour",
                 params={"aggregate": 1, "limit": 72, "currency": "usd",
                         "token": "base", "include_empty_intervals": "true"})
    rows = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    rows = [r for r in rows if len(r) >= 6]
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def find_breakout(p, candles):
    """
    Looks for: at least BO_RANGE_HOURS of sideways trading, then a break above it.
    The last 2 hourly candles are the breakout; the range is the hours before that.
    Returns a dict with the details, or None.
    """
    if len(candles) < BO_RANGE_HOURS + 2:
        return None
    price_now = float(p.get("priceUsd") or 0)
    mcap_now = p.get("marketCap") or p.get("fdv") or 0
    if price_now <= 0 or mcap_now <= 0:
        return None
    to_mcap = mcap_now / price_now

    recent = candles[:2]
    base = candles[2:2 + BO_RANGE_HOURS]
    closes = sorted(float(c[4]) for c in base)
    # ignore the most extreme 10% of hours so one stray wick doesn't break the range
    cut = max(1, len(closes) // 10)
    bottom, top = closes[cut], closes[-1 - cut]
    if bottom <= 0:
        return None
    width = top / bottom
    middle = closes[len(closes) // 2]
    base_vol = sum(float(c[5]) for c in base) / len(base)
    recent_vol = sum(float(c[5]) for c in recent) / len(recent)
    spike = recent_vol / base_vol if base_vol > 0 else 99

    # how long the range really lasted: walk back while closes stay inside it
    hours_in_range = 0
    for c in candles[2:]:
        if bottom / 1.15 <= float(c[4]) <= top * 1.15:
            hours_in_range += 1
        else:
            break

    ok = (width <= BO_MAX_RANGE_WIDTH
          and price_now >= top * BO_MIN_ABOVE_TOP
          and price_now >= middle * BO_MIN_MULTIPLE
          and spike >= BO_MIN_VOLUME_SPIKE)
    if not ok:
        return None
    return {
        "range_low": bottom * to_mcap,
        "range_high": top * to_mcap,
        "range_mid": middle * to_mcap,
        "hours": hours_in_range,
        "multiple": price_now / middle,
        "spike": spike,
    }


def breakout_text(p, bo, rc_text):
    bt = p.get("baseToken", {})
    pc = p.get("priceChange") or {}
    t1 = (p.get("txns") or {}).get("h1") or {}
    addr = bt.get("address", "")
    days = bo["hours"] / 24
    span = f"{days:.1f} days" if days >= 1 else f"{bo['hours']}h"
    return (
        f"📈 <b>BREAKOUT: {bt.get('name', '?')} (${bt.get('symbol', '?')})</b>\n"
        f"Range for {span}: {fmt_usd(bo['range_low'])}–{fmt_usd(bo['range_high'])}\n"
        f"MC now: <b>{fmt_usd(p.get('marketCap') or p.get('fdv'))}</b> "
        f"({bo['multiple']:.1f}x the range middle)\n"
        f"Volume: {bo['spike']:.0f}x normal · 1h {pc.get('h1', 0):+.0f}% · 5m {pc.get('m5', 0):+.0f}%\n"
        f"Liq {fmt_usd((p.get('liquidity') or {}).get('usd'))} · "
        f"Buys/sells 1h: {t1.get('buys', 0)}/{t1.get('sells', 0)} · Age {age_text(p)}\n"
        f"{rc_text}\n"
        f"<code>{addr}</code>\n"
        f"<a href=\"{p.get('url', '')}\">DexScreener</a> · "
        f"<a href=\"https://rugcheck.xyz/tokens/{addr}\">RugCheck</a>\n"
        f"<i>Screen only, not advice — breakouts can fail fast.</i>"
    )


def scan_breakouts(state, pairs):
    """Returns number of breakout alerts sent."""
    done = state.setdefault("breakout_alerted", {})
    pool = [p for a, p in pairs.items() if a not in done and might_be_breakout(p)]
    pool.sort(key=lambda p: (p.get("priceChange") or {}).get("h1") or 0, reverse=True)
    sent = 0
    for p in pool[:BO_MAX_CHECKS]:
        addr = p["baseToken"]["address"]
        candles = hourly_candles(p.get("pairAddress", ""))
        time.sleep(2.1)
        bo = find_breakout(p, candles)
        if not bo:
            continue
        done[addr] = time.time()
        ok, rc_text = rugcheck(addr) if USE_RUGCHECK else (True, "")
        if not ok:
            print(f"  skipped breakout {p['baseToken'].get('symbol')} (RugCheck danger)")
            continue
        tg_send(state["chat_id"], breakout_text(p, bo, rc_text))
        sent += 1
        print(f"  breakout alert {p['baseToken'].get('symbol')} {addr}")
    return sent


def scan(state, last_scan_info):
    addrs = collect_candidate_addresses()
    pairs = fetch_best_pairs(addrs)
    bo_sent = scan_breakouts(state, pairs) if BREAKOUT_ALERTS_ON else 0
    fresh = []
    if SCOUT_ALERTS_ON:
        fresh = [p for a, p in pairs.items() if a not in state["alerted"] and passes(p)]
        fresh = sorted(drop_copycats(fresh), key=score, reverse=True)

    sent = 0
    for p in fresh:
        if sent >= MAX_ALERTS_PER_SCAN:
            break
        addr = p["baseToken"]["address"]
        ok, rc_text = rugcheck(addr) if USE_RUGCHECK else (True, "")
        state["alerted"][addr] = time.time()   # never re-check or re-alert this coin
        if not ok:
            print(f"  skipped {p['baseToken'].get('symbol')} (RugCheck danger)")
            continue
        tg_send(state["chat_id"], alert_text(p, rc_text))
        sent += 1
        print(f"  alerted {p['baseToken'].get('symbol')} {addr}")
    save_state(state)

    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    last_scan_info["text"] = (f"{stamp}, {len(pairs)} coins checked, "
                              f"{sent} scout + {bo_sent} breakout alerts")
    print(f"[{stamp}] scanned {len(pairs)} tokens, {len(fresh)} passed, "
          f"{sent} scout alerts, {bo_sent} breakout alerts")
    return len(pairs), sent, bo_sent


def heartbeat(state, checked, scout_sent, bo_sent):
    """Counts scans and every HEARTBEAT_HOURS sends a short 'still running' summary."""
    if not HEARTBEAT_HOURS:
        return
    hb = state.setdefault("heartbeat", {"last": 0, "scans": 0, "checked": 0,
                                        "scout": 0, "breakout": 0, "empty": 0})
    hb["scans"] += 1
    hb["checked"] += checked
    hb["scout"] += scout_sent
    hb["breakout"] += bo_sent
    if checked == 0:
        hb["empty"] += 1
    now = time.time()
    if now - hb["last"] < HEARTBEAT_HOURS * 3600:
        return
    first = hb["last"] == 0
    period = "since the update" if first else f"in the last {HEARTBEAT_HOURS}h"
    warn = ""
    if hb["empty"] and hb["empty"] >= hb["scans"] / 2:
        warn = "\n⚠️ Many scans got no market data (sites may be limiting requests)."
    tg_send(state["chat_id"],
            f"🟢 <b>Bot running</b> — {period}:\n"
            f"Scans: {hb['scans']} · Coins checked: {hb['checked']:,}\n"
            f"Alerts: {hb['scout']} scout · {hb['breakout']} breakout{warn}\n"
            f"Next update in about {HEARTBEAT_HOURS}h.")
    state["heartbeat"] = {"last": now, "scans": 0, "checked": 0,
                          "scout": 0, "breakout": 0, "empty": 0}


def run_once():
    """One scan and exit — used by GitHub Actions, which runs this every few minutes."""
    state = load_state()
    state.setdefault("alerted", {})
    state["chat_id"] = CHAT_ID
    if not state.get("welcomed"):
        tg_send(CHAT_ID, "✅ Meme Scout Bot is live.\n"
                         f"Screening Solana memes at {fmt_usd(MIN_MCAP)}–{fmt_usd(MAX_MCAP)} market cap "
                         "every few minutes. New coins will appear here.")
        state["welcomed"] = True
    tg_poll_commands(state, {})          # read /alert, /alerts, /remove sent since last run
    check_price_alerts(state)
    checked, scout_sent, bo_sent = scan(state, {})
    heartbeat(state, checked, scout_sent, bo_sent)
    save_state(state)


def ask_for_token():
    """On a phone, it's easier to paste the token when asked than to edit the file."""
    global BOT_TOKEN, TG
    cfg = "meme_scout_bot_token.txt"
    if "PASTE_YOUR" in BOT_TOKEN:
        try:
            with open(cfg) as f:
                BOT_TOKEN = f.read().strip()
        except FileNotFoundError:
            BOT_TOKEN = input("Paste your bot token from BotFather, then press return: ").strip()
            with open(cfg, "w") as f:
                f.write(BOT_TOKEN)
    TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
    me = get_json(f"{TG}/getMe")
    if not me or not me.get("ok"):
        print("That token didn't work. Run the bot again and paste the full token.")
        if os.path.exists(cfg):
            os.remove(cfg)
        return False
    print(f"Connected to @{me['result'].get('username')}")
    return True


def main():
    if not ask_for_token():
        return
    if os.getenv("RUN_ONCE") == "1":
        if not CHAT_ID:
            print("Set TG_CHAT_ID for RUN_ONCE mode.")
            return
        run_once()
        return
    state = load_state()
    state.setdefault("alerted", {})
    if CHAT_ID:
        state["chat_id"] = CHAT_ID
    print("Meme Scout Bot running. Ctrl+C to stop.")
    if not state.get("chat_id"):
        print("Send /start to your bot in Telegram to connect it.")

    last_scan_info = {}
    next_scan = 0
    while True:
        tg_poll_commands(state, last_scan_info)
        if state.get("chat_id") and time.time() >= next_scan:
            try:
                check_price_alerts(state)
                scan(state, last_scan_info)
            except Exception as e:
                print(f"  scan error: {e}")
            next_scan = time.time() + SCAN_EVERY_SECONDS
        time.sleep(5)


if __name__ == "__main__":
    main()
