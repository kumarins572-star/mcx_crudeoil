import os
import time
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import requests
import pyotp
import pandas as pd
import pandas_ta_classic as ta
import telebot
from flask import Flask, jsonify, request, send_from_directory
from waitress import serve
import threading
from SmartApi import SmartConnect

# ==========================
# CONFIG
# ==========================
load_dotenv(Path(__file__).with_name(".env"))


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = required_env("BOT_TOKEN")
CHAT_ID = required_env("CHAT_ID")

ANGEL_API_KEY = required_env("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = required_env("ANGEL_CLIENT_CODE")
ANGEL_PIN = required_env("ANGEL_PIN")
ANGEL_TOTP_SECRET = required_env("ANGEL_TOTP_SECRET")

bot = telebot.TeleBot(BOT_TOKEN)
smart = SmartConnect(api_key=ANGEL_API_KEY)

app = Flask(__name__)
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
DASHBOARD_ORIGIN = os.getenv("DASHBOARD_ORIGIN", "*")

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = DASHBOARD_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route('/')
def home():
    index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "dist", "index.html")
    if os.path.isfile(index):
        return send_from_directory(os.path.dirname(index), "index.html")
    return "MCX CRUDEOIL MINI FIB/PIVOT BOT ACTIVE"

UNDERLYING_NAME = "CRUDEOILM"   # Crude Oil Mini symbol name on MCX
EXCHANGE_SEG_COM = "MCX"

INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ==========================
# AUTO-ORDER SAFETY SWITCH
# Defaults to OFF. Real orders only go out if AUTO_TRADE_ENABLED=true
# is explicitly set on the server.
# ==========================
AUTO_TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "false").strip().lower() == "true"
ORDER_LOTS = int(os.getenv("ORDER_LOTS", "1"))  # number of lots per trade

# Minimum available margin (₹) required before placing a trade. This is a
# rough safety check, not an exact margin calculator - verify against your
# broker's actual margin requirement for Crude Oil Mini options and adjust.
MIN_MARGIN_REQUIRED = float(os.getenv("MIN_MARGIN_REQUIRED", "5000"))

# AngelOne interval codes. Note: SmartAPI has no native 2-MINUTE or
# 4-HOUR interval - both are built by resampling native data.
TF_INTERVAL_MAP = {
    "1m": "ONE_MINUTE",
    "2m": "ONE_MINUTE",   # resampled to 2min
    "3m": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "1h": "ONE_HOUR",
    "4h": "ONE_HOUR",     # resampled to 4H
}
CONFIRM_TIMEFRAMES = ["10m", "1m", "2m", "3m", "5m", "15m", "1h", "4h"]
RESAMPLE_RULE = {"2m": "2min", "4h": "4H"}

# Supertrend(length, multiplier) per timeframe. Default is (7,3); the
# 10-minute timeframe specifically uses Supertrend(10,3).
SUPERTREND_PARAMS = {"10m": (10, 3)}
DEFAULT_SUPERTREND_PARAMS = (7, 3)

# ==========================
# MULTI-TIMEFRAME ROLE HIERARCHY (fixes multi-TF overtrading)
# All 8 timeframes are still tracked in state (for exit-monitoring of
# whatever is already active + status display), but ONLY entry_tf is
# allowed to OPEN a new trade, and only when it agrees with the setup
# timeframe, which itself must agree with the master (macro) timeframes.
# 1m/2m/3m/5m/1h/4h no longer independently start their own trades.
# ==========================
MASTER_TF = ["4h", "1h"]     # macro trend filter - ALL must agree
SETUP_TF = "15m"             # intermediate structure filter
ENTRY_TF = "10m"             # the ONLY timeframe allowed to open NEW trades
PRIORITY_TIMEFRAME = ENTRY_TF  # kept as an alias - used in messages/recovery

TARGET_TRADE_STEPS = 3

# When the trend is WEAK (see is_market_weak / is_trend_weakening), trades
# should not wait for the full fallback distance - they should complete on
# a small move instead: small target + small (tighter) SL.
WEAK_TREND_FALLBACK_POINTS = {1: 40, 2: 40, 3: 30}
WEAK_TREND_SL_ATR_MULTIPLIER = 0.5   # tighter SL when trend is weak

# ATR-DYNAMIC TARGET (replaces old fixed 150/70 point fallback): when the
# market is volatile, the fallback target distance now expands with it
# instead of staying pinned at a fixed point count.
TARGET_ATR_MULTIPLIER = float(os.getenv("TARGET_ATR_MULTIPLIER", "1.5"))
MIN_TARGET_POINTS = float(os.getenv("MIN_TARGET_POINTS", "40"))

def get_fallback_target_points(trade_no, atr, weak_market=False):
    if weak_market:
        return WEAK_TREND_FALLBACK_POINTS.get(trade_no, 30)
    if atr is None or pd.isna(atr):
        atr = 0
    return max(MIN_TARGET_POINTS, atr * TARGET_ATR_MULTIPLIER)

FIB_MATCH_PCT = 0.003

# ==========================
# GLOBAL SESSION WATCH TIMES (IST) - MCX Crude Oil
# Around these times, apply EXTRA STRICT confirmation: tighter Fib
# tolerance + stronger Call/Put OI skew requirement.
# ==========================
SESSION_WATCH_TIMES = [(11, 0), (13, 30), (17, 0), (19, 0), (21, 0)]  # (hour, minute) IST
SESSION_WATCH_WINDOW_MIN = 15
STRICT_FIB_MATCH_PCT = FIB_MATCH_PCT / 2
STRICT_OI_SKEW_RATIO = 1.2

def is_session_watch_window():
    now = datetime.datetime.now(IST)
    for h, m in SESSION_WATCH_TIMES:
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff_minutes = abs((now - target).total_seconds()) / 60
        if diff_minutes <= SESSION_WATCH_WINDOW_MIN:
            return True
    return False
FLIP_LOOKBACK = 10
FLIP_THRESHOLD = 3

# ==========================
# MCX MARKET HOURS
# MCX trades Mon-Fri, ~9:00 AM to 11:30 PM IST (23:55 on non-DST-linked
# days for some contracts). Using 9:00-23:30 as a safe window - adjust
# if your specific contract's session differs.
# ==========================
IST = ZoneInfo("Asia/Kolkata")
MCX_OPEN_HOUR, MCX_OPEN_MIN = 9, 0
MCX_CLOSE_HOUR, MCX_CLOSE_MIN = 23, 30

def is_market_open():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    open_t = now.replace(hour=MCX_OPEN_HOUR, minute=MCX_OPEN_MIN, second=0, microsecond=0)
    close_t = now.replace(hour=MCX_CLOSE_HOUR, minute=MCX_CLOSE_MIN, second=0, microsecond=0)
    return open_t <= now <= close_t


def close_all_open_trades_for_day():
    """
    Called once when the market transitions from open -> closed.
    Sends a DAY CLOSE / EXIT message for any timeframe with running
    trades (Trade 1/2/3, whichever are active), and resets state so
    nothing carries into the next day.
    """
    for tf, tf_state in state["timeframes"].items():
        if tf_state["trend"] is not None:
            old_trend = tf_state["trend"]
            for trade_no, trade in tf_state["trades"].items():
                if trade["active"]:
                    entry_line = f"📍 Entry was : {trade['entry_price']:.2f}\n" if trade["entry_price"] is not None else ""
                    msg = f"""
🌙 DAY CLOSE - EXIT  [{tf}] Trade {trade_no}
⚠️ Market closing, exiting running {old_trend} trade now (no overnight carry).

{entry_line}"""
                    try:
                        bot.send_message(CHAT_ID, msg)
                    except Exception as e:
                        print("Day-close message failed:", e)

        tf_state["trend"] = None
        tf_state["trades"] = {1: new_trade_slot(), 2: new_trade_slot(), 3: new_trade_slot()}
        tf_state["trade3_started"] = False
        tf_state["last_signal_candle_ts"] = None
        tf_state["recovered"] = False
        tf_state["st_history"] = []

# ==========================
# STATE
# ==========================
def new_trade_slot():
    return {
        "active": False,
        "entry_price": None,
        "target": None,
        "target_source": None,
        "sl": None,
        "max_price_reached": None,
        "trailing_active": False,
        # Option-side tracking (added so exits can be judged on actual
        # option premium P&L, not just the underlying move) - see
        # option_entry_price / get_option_ltp().
        "option_symbol": None,
        "option_token": None,
        "option_entry_price": None,
        "sl_order_id": None,
    }

# ==========================
# GLOBAL POSITION LIMIT (fixes 8-timeframe overtrading)
# Hard cap on how many trade slots can be active across ALL timeframes
# at once, regardless of how many timeframes/signals fire.
# ==========================
MAX_ACTIVE_TRADES_GLOBAL = int(os.getenv("MAX_ACTIVE_TRADES_GLOBAL", "2"))

def count_active_trades():
    count = 0
    for tf_state in state["timeframes"].values():
        for trade in tf_state["trades"].values():
            if trade["active"]:
                count += 1
    return count

state = {
    "paused": False,   # /pause and /resume commands toggle this - blocks NEW trade entries
                        # only; existing active trades still get tracked/exited for safety.
    "monthly_fib": None,
    "weekly_fib": None,
    "daily_fib": None,
    "session_5pm_fib": None,
    "session_5pm_date": None,   # date string, so 5PM snapshot is taken once per day
    "fib_updated": None,
    "instrument_master": None,
    "instrument_master_updated": None,
    "underlying_token": None,   # nearest-expiry FUTCOM token, used for price/indicators
    "underlying_symbol": None,
    "open_positions": {},        # tradingsymbol -> {qty, side, avg_price} - synced from broker
    "positions_synced_at": None,
    "dashboard": {
        "last_cycle": None,
        "market_open": None,
        "underlying_price": None,
        "master_trend": None,
        "setup_trend": None,
        "entry_trend": None,
        "cp_data": None,
        "last_error": None,
        "skip_reason": None,
    },
    "timeframes": {
        tf: {
            "trend": None,        # overall direction for this timeframe: BUY/SELL/None
            # Trade 1 & Trade 2 open TOGETHER at the same entry when a new
            # trend starts, and run fully independently of each other.
            # Trade 3 opens (same direction, new entry) only after BOTH
            # Trade 1 and Trade 2 have closed, and runs until its own
            # target/SL hits OR the trend flips - whichever first.
            "trades": {1: new_trade_slot(), 2: new_trade_slot(), 3: new_trade_slot()},
            "trade3_started": False,   # guards against opening Trade 3 twice
            "last_signal_candle_ts": None,  # duplicate-signal guard
            "recovered": False,    # True if this trade was recovered from broker on restart
            "st_history": []
        } for tf in CONFIRM_TIMEFRAMES
    }
}

# ==========================
# LOGIN / SESSION
# ==========================
def login():
    totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
    data = smart.generateSession(ANGEL_CLIENT_CODE, ANGEL_PIN, totp)
    if not data.get("status"):
        raise Exception(f"AngelOne login failed: {data}")
    return data

# ==========================
# BROKER POSITION SYNC (HIGH PRIORITY)
# In-memory state resets on bot restart. This reconciles that state
# against AngelOne's actual open positions so Entry/Qty/Direction are
# recovered, and so duplicate orders are never placed for a symbol that
# already has an open position.
# ==========================
def fetch_broker_positions():
    """
    Returns a list of open (netqty != 0) CRUDEOILM option positions from
    AngelOne. NOTE: verify field names ('netqty', 'avgnetprice',
    'tradingsymbol', 'symboltoken') against your account's actual
    response via a standalone smart.position() test call.
    """
    resp = smart.position()
    if not resp or not resp.get("status"):
        raise Exception(f"position() failed: {resp}")

    data = resp.get("data") or []
    open_positions = []
    for p in data:
        try:
            netqty = int(p.get("netqty", 0) or 0)
        except Exception:
            netqty = 0
        symbol = p.get("tradingsymbol", "")
        if netqty == 0 or UNDERLYING_NAME not in symbol:
            continue
        avg_price = float(p.get("avgnetprice", 0) or p.get("avgnetprice", 0) or 0)
        side = "BUY" if symbol.endswith("CE") else ("SELL" if symbol.endswith("PE") else None)
        open_positions.append({
            "tradingsymbol": symbol,
            "symboltoken": p.get("symboltoken"),
            "qty": abs(netqty),
            "avg_price": avg_price,
            "side": side,   # BUY = underlying-BUY view (via CE), SELL = underlying-SELL view (via PE)
        })
    return open_positions


def sync_positions_from_broker(notify_new=False):
    """
    Refreshes state["open_positions"] from the broker. Called on startup
    (with notify_new=True to alert about anything already open) and every
    analyze() cycle (silently) to stay in sync and prevent duplicate orders.
    """
    try:
        positions = fetch_broker_positions()
    except Exception as e:
        print("Position sync failed:", e)
        return

    new_map = {p["tradingsymbol"]: p for p in positions}
    old_map = state["open_positions"]

    if notify_new and new_map:
        lines = [f"  • {p['tradingsymbol']} qty={p['qty']} avg={p['avg_price']:.2f}" for p in positions]
        try:
            bot.send_message(
                CHAT_ID,
                "🔄 RECOVERED OPEN POSITIONS on startup:\n" + "\n".join(lines) +
                "\n\nBot state has been resynced from broker."
            )
        except Exception as e:
            print("Recovery notify failed:", e)

    state["open_positions"] = new_map
    state["positions_synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Seed any recovered position into a timeframe state so live target/SL
    # tracking can resume - assigned to the priority timeframe if it has
    # no active trade, otherwise the first free timeframe. Target/SL are
    # unknown from the broker, so they're recomputed using the fallback
    # points (Trade 1 level) as a reasonable estimate; verify/adjust
    # manually after restart if precision matters.
    # NOTE: broker only tells us ONE recovered position, but the new model
    # runs Trade 1 & Trade 2 in parallel - recovery seeds Trade 1 only.
    # Trade 2 for that entry is NOT auto-recovered; check manually after
    # a restart if Trade 1 & 2 were both open before the bot went down.
    if notify_new:
        for symbol, p in new_map.items():
            if symbol in old_map:
                continue
            already_tracked = any(
                tf_state["trades"][1]["entry_price"] == p["avg_price"] and tf_state["trend"] == p["side"]
                for tf_state in state["timeframes"].values()
            )
            if already_tracked or p["side"] is None:
                continue

            target_tf = ENTRY_TF if state["timeframes"][ENTRY_TF]["trend"] is None else None
            if target_tf is None:
                for tf in CONFIRM_TIMEFRAMES:
                    if state["timeframes"][tf]["trend"] is None:
                        target_tf = tf
                        break
            if target_tf is None:
                continue

            entry = p["avg_price"]
            atr_estimate = None
            try:
                if state.get("underlying_token"):
                    _, atr_estimate = get_last_price_and_atr(state["underlying_token"], EXCHANGE_SEG_COM, target_tf)
            except Exception as e:
                print("Recovery ATR estimate failed:", e)
            fallback = get_fallback_target_points(1, atr_estimate)
            tf_state = state["timeframes"][target_tf]
            tf_state["trend"] = p["side"]
            trade1 = tf_state["trades"][1]
            trade1["active"] = True
            trade1["entry_price"] = entry
            trade1["target"] = entry + fallback if p["side"] == "BUY" else entry - fallback
            trade1["target_source"] = "recovered estimate (underlying pts - verify manually)"
            trade1["sl"] = entry - (fallback / 2) if p["side"] == "BUY" else entry + (fallback / 2)
            trade1["option_symbol"] = symbol
            trade1["option_token"] = p.get("symboltoken")
            trade1["option_entry_price"] = p["avg_price"]
            tf_state["recovered"] = True

            # ==========================
            # BROKER = SOURCE OF TRUTH FOR RECOVERY
            # The target/SL above is just a rough underlying-points
            # estimate for messaging/trailing. The real safety net is a
            # broker-side SL order - if this recovered position doesn't
            # already have one working at the broker, place it NOW so a
            # crash/restart never leaves a position unprotected.
            # ==========================
            try:
                already_protected = has_open_sl_order(symbol)
            except Exception as e:
                print("SL-order lookup failed during recovery:", e)
                already_protected = False
            if not already_protected:
                sl_order_id, trigger_price = place_sl_order(symbol, p.get("symboltoken"), p["qty"], p["avg_price"])
                if sl_order_id:
                    trade1["sl_order_id"] = sl_order_id
                    try:
                        bot.send_message(CHAT_ID, f"🛡️ Broker-side SL placed on recovery for {symbol} @ trigger {trigger_price:.2f}")
                    except Exception:
                        pass
                else:
                    try:
                        bot.send_message(CHAT_ID, f"⚠️ RECOVERY WARNING: could not place broker-side SL for {symbol} — SET SL MANUALLY NOW.")
                    except Exception:
                        pass


def check_duplicate_position(tradingsymbol):
    """Returns True if an open broker position already exists for this exact option symbol."""
    return tradingsymbol in state["open_positions"] and state["open_positions"][tradingsymbol]["qty"] > 0

# ==========================
# INSTRUMENT MASTER (cached, refreshed once a day)
# ==========================
def refresh_instrument_master_if_needed():
    today = time.strftime("%Y-%m-%d")
    if state["instrument_master_updated"] == today and state["instrument_master"] is not None:
        return
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
    resp.raise_for_status()
    state["instrument_master"] = resp.json()
    state["instrument_master_updated"] = today


def find_nearest_future(name=UNDERLYING_NAME, exch_seg=EXCHANGE_SEG_COM):
    """Nearest-expiry FUTCOM contract for the underlying, used for price/indicators."""
    refresh_instrument_master_if_needed()
    today = datetime.date.today()
    best = None
    for inst in state["instrument_master"]:
        if inst.get("name") != name or inst.get("exch_seg") != exch_seg:
            continue
        if inst.get("instrumenttype") != "FUTCOM":
            continue
        try:
            expiry = datetime.datetime.strptime(inst["expiry"], "%d%b%Y").date()
        except Exception:
            continue
        if expiry >= today and (best is None or expiry < best[0]):
            best = (expiry, inst)
    if not best:
        return None
    return best[1]


def find_options_for_nearest_expiry(name=UNDERLYING_NAME, exch_seg=EXCHANGE_SEG_COM):
    """
    Returns (expiry_date, [list of CE/PE instrument dicts]) for the
    nearest-expiry Crude Oil Mini option chain.
    NOTE: verify 'instrumenttype' value for MCX options against the
    instrument master before relying on this (commonly "OPTFUT").
    """
    refresh_instrument_master_if_needed()
    today = datetime.date.today()
    candidates = []
    for inst in state["instrument_master"]:
        if inst.get("name") != name or inst.get("exch_seg") != exch_seg:
            continue
        if inst.get("instrumenttype") not in ("OPTFUT", "OPTCOM"):
            continue
        try:
            expiry = datetime.datetime.strptime(inst["expiry"], "%d%b%Y").date()
        except Exception:
            continue
        if expiry >= today:
            candidates.append((expiry, inst))

    if not candidates:
        return None, []

    nearest_expiry = min(c[0] for c in candidates)
    chain = [c[1] for c in candidates if c[0] == nearest_expiry]
    return nearest_expiry, chain

# ==========================
# CANDLE DATA
# ==========================
def get_candles(token, exch_seg, interval, days_back=5):
    to_date = datetime.datetime.now()
    from_date = to_date - datetime.timedelta(days=days_back)
    params = {
        "exchange": exch_seg,
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M"),
    }
    resp = smart.getCandleData(params)
    if not resp.get("status"):
        raise Exception(f"getCandleData failed: {resp}")

    rows = resp["data"]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def get_candles_for_timeframe(token, exch_seg, tf):
    interval = TF_INTERVAL_MAP[tf]
    days_back = 5 if tf in ("1m", "2m", "3m", "5m") else 30
    df = get_candles(token, exch_seg, interval, days_back=days_back)

    if tf in RESAMPLE_RULE:
        rule = RESAMPLE_RULE[tf]
        df = df.set_index("timestamp").resample(rule).agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna().reset_index()

    return df


def get_daily_hlc(token, exch_seg, days):
    df = get_candles(token, exch_seg, "ONE_DAY", days_back=days + 5)
    window_df = df.iloc[:-1] if len(df) > 1 else df
    window_df = window_df.tail(days)
    H = window_df["high"].max()
    L = window_df["low"].min()
    C = window_df["close"].iloc[-1]
    return H, L, C

# ==========================
# REAL SUPPORT / RESISTANCE (swing-based, no fixed formula)
# Instead of a fixed pivot formula (R1/R2/R3, S1/S2/S3), this looks at
# ACTUAL past price action: where price repeatedly turned around.
# ==========================
SR_LOOKBACK_DAYS = 60
SR_SWING_WINDOW = 3            # candles on each side to confirm a swing high/low
SR_CLUSTER_TOLERANCE_PCT = 0.3  # % distance to merge nearby swing points into one level
SR_MAX_LEVELS = 3               # strongest resistance/support levels to keep, each side

def find_support_resistance(token, exch_seg):
    """
    Detects real support & resistance from price action:
      1. Pulls the last SR_LOOKBACK_DAYS daily candles.
      2. A candle's high is a "swing high" if it's the highest among
         SR_SWING_WINDOW candles on either side of it (a swing low is
         the mirror, on lows) - i.e. price actually turned around there.
      3. Nearby swing points (within SR_CLUSTER_TOLERANCE_PCT of each
         other) get merged into one level. How many swings land in a
         cluster = that level's strength (how many times price reacted
         there).
      4. Returns the SR_MAX_LEVELS strongest swing-high levels (RES_1
         being the strongest/most-touched) and swing-low levels (SUP_1
         onward) as a plain {name: price} dict.
    """
    df = get_candles(token, exch_seg, "ONE_DAY", days_back=SR_LOOKBACK_DAYS + 5)
    if len(df) < (2 * SR_SWING_WINDOW + 5):
        return {}

    highs, lows = df["high"].values, df["low"].values
    n = len(df)

    swing_highs, swing_lows = [], []
    for i in range(SR_SWING_WINDOW, n - SR_SWING_WINDOW):
        window_h = highs[i - SR_SWING_WINDOW: i + SR_SWING_WINDOW + 1]
        window_l = lows[i - SR_SWING_WINDOW: i + SR_SWING_WINDOW + 1]
        if highs[i] == window_h.max():
            swing_highs.append(highs[i])
        if lows[i] == window_l.min():
            swing_lows.append(lows[i])

    def cluster(points):
        clusters = []  # [running_avg_price, touch_count]
        for p in sorted(points):
            placed = False
            for c in clusters:
                if c[0] != 0 and abs(p - c[0]) / c[0] * 100 <= SR_CLUSTER_TOLERANCE_PCT:
                    c[0] = (c[0] * c[1] + p) / (c[1] + 1)
                    c[1] += 1
                    placed = True
                    break
            if not placed:
                clusters.append([p, 1])
        clusters.sort(key=lambda c: c[1], reverse=True)  # most-touched (strongest) first
        return clusters

    resistance = cluster(swing_highs)[:SR_MAX_LEVELS]
    support = cluster(swing_lows)[:SR_MAX_LEVELS]

    levels = {}
    for idx, (price, touches) in enumerate(resistance, start=1):
        levels[f"RES_{idx}"] = price
    for idx, (price, touches) in enumerate(support, start=1):
        levels[f"SUP_{idx}"] = price
    return levels

# ==========================
# PIVOT + FIBONACCI LEVELS
# ==========================
FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.382]  # 0.0% -> 138.2%

def compute_fib_pivot_levels(H, L, C):
    """
    Classic Fibonacci Pivot Points (distinct from the retracement series
    below). Anchored on a single Pivot Point (PP), with Resistance/Support
    levels spaced out using the 38.2% / 61.8% / 100% Fibonacci ratios of
    the day's range:

        PP = (H + L + C) / 3
        R1 = PP + 0.382*(H-L)     S1 = PP - 0.382*(H-L)
        R2 = PP + 0.618*(H-L)     S2 = PP - 0.618*(H-L)
        R3 = PP + 1.000*(H-L)     S3 = PP - 1.000*(H-L)
    """
    diff = H - L
    PP = (H + L + C) / 3
    return {
        "R1": PP + 0.382 * diff, "R2": PP + 0.618 * diff, "R3": PP + 1.000 * diff,
        "S1": PP - 0.382 * diff, "S2": PP - 0.618 * diff, "S3": PP - 1.000 * diff,
    }


def compute_fib_levels(H, L, C):
    """
    Combines two distinct Fibonacci-based level sets off the same
    High/Low/Close range:
      1. FIBONACCI RETRACEMENT series (UP_x.x / DN_x.x, 0% -> 138.2%)
         - UP_x.x : anchored at the Low (0%) going up through the High
                    (100%) and beyond - resistance/targets for BUY.
         - DN_x.x : anchored at the High (0%) going down through the Low
                    (100%) and beyond - support/targets for SELL.
      2. FIBONACCI PIVOT LEVELS (PP, R1-R3, S1-S3) via
         compute_fib_pivot_levels() - the classic pivot-point formula.
    Both sets land in the same dict, so all existing matching/target
    logic (nearest_level_above/below, price_matches_fib_level) sees them
    together automatically.
    """
    diff = H - L
    PP = (H + L + C) / 3
    levels = {"PP": PP, "H": H, "L": L, "C": C}
    for r in FIB_RATIOS:
        label = f"{r * 100:.1f}"
        levels[f"UP_{label}"] = L + diff * r
        levels[f"DN_{label}"] = H - diff * r
    levels.update(compute_fib_pivot_levels(H, L, C))
    return levels


def refresh_fib_levels_if_needed(token, exch_seg):
    today = time.strftime("%Y-%m-%d")
    if state["fib_updated"] == today and state["monthly_fib"] is not None:
        return
    H, L, C = get_daily_hlc(token, exch_seg, 30)
    state["monthly_fib"] = compute_fib_levels(H, L, C)
    H, L, C = get_daily_hlc(token, exch_seg, 7)
    state["weekly_fib"] = compute_fib_levels(H, L, C)
    # Daily pivot uses YESTERDAY's High/Low/Close (single most recent
    # fully-closed daily candle) as the trend range base.
    H, L, C = get_daily_hlc(token, exch_seg, 1)
    state["daily_fib"] = compute_fib_levels(H, L, C)
    # Merge in REAL swing-based support/resistance (RES_1.., SUP_1..)
    # alongside the Fibonacci series, so target/match logic sees both.
    try:
        state["daily_fib"].update(find_support_resistance(token, exch_seg))
    except Exception as e:
        print("Support/resistance detection failed:", e)
    state["fib_updated"] = today


def get_5pm_session_fib_levels(token, exch_seg):
    """
    Takes High/Low/Close of TODAY from market open up to 5:00 PM IST,
    and derives a Pivot/Fib table from that range.
    """
    now_ist = datetime.datetime.now(IST)
    session_end = now_ist.replace(hour=17, minute=0, second=0, microsecond=0)

    df = get_candles(token, exch_seg, "FIVE_MINUTE", days_back=1)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

    today_str = now_ist.strftime("%Y-%m-%d")
    window_df = df[(df["timestamp"].dt.strftime("%Y-%m-%d") == today_str) & (df["timestamp"] <= session_end)]
    if len(window_df) == 0:
        return None

    H, L, C = window_df["high"].max(), window_df["low"].min(), window_df["close"].iloc[-1]
    return compute_fib_levels(H, L, C)


def refresh_5pm_session_fib_if_needed(token, exch_seg):
    """Takes the 5PM snapshot once per day, as soon as time reaches 5PM IST."""
    now_ist = datetime.datetime.now(IST)
    today = now_ist.strftime("%Y-%m-%d")
    if now_ist.hour < 17:
        return
    if state["session_5pm_date"] == today:
        return
    try:
        levels = get_5pm_session_fib_levels(token, exch_seg)
    except Exception as e:
        print("5PM session fib fetch error:", e)
        return
    if levels:
        state["session_5pm_fib"] = levels
        state["session_5pm_date"] = today


def nearest_level_above(levels, price):
    candidates = [v for k, v in levels.items() if k not in ("H", "L", "C") and v > price]
    return min(candidates) if candidates else levels["R3"]


def nearest_level_below(levels, price):
    candidates = [v for k, v in levels.items() if k not in ("H", "L", "C") and v < price]
    return max(candidates) if candidates else levels["S3"]


def price_matches_fib_level(monthly_levels, weekly_levels, daily_levels, session_5pm_levels, price, tolerance_pct=FIB_MATCH_PCT):
    windows = [("monthly", monthly_levels), ("weekly", weekly_levels), ("daily", daily_levels)]
    if session_5pm_levels:
        windows.append(("session5pm", session_5pm_levels))
    for window_name, levels in windows:
        for name, level_val in levels.items():
            if name in ("H", "L", "C"):
                continue
            if level_val == 0:
                continue
            if abs(price - level_val) / level_val <= tolerance_pct:
                return True, window_name, name, level_val
    return False, None, None, None

# ==========================
# CALL / PUT OI + VOLUME (MCX Crude Oil Mini options chain)
# Uses getMarketData (mode FULL) per token to read live OI/volume.
# ==========================
# ==========================
# ATM ZONE FILTER for OI/Volume
# Only strikes within ATM_STRIKE_RANGE steps of the current ATM strike
# feed into the Call/Put OI-skew decision, instead of the whole chain
# (which lets a huge far-OTM OI number distort the signal).
# NOTE: verify CRUDEOIL_STRIKE_STEP (₹ points between consecutive
# strikes) against the live option chain - it can change with the
# underlying's price level; 50 is a typical MCX Crude Oil Mini step but
# is not guaranteed.
# ==========================
ATM_STRIKE_RANGE = int(os.getenv("ATM_STRIKE_RANGE", "2"))
CRUDEOIL_STRIKE_STEP = float(os.getenv("CRUDEOIL_STRIKE_STEP", "50"))


def _strike_val(c):
    try:
        return float(c["strike"]) / 100.0
    except Exception:
        return None


def filter_chain_to_atm_zone(chain, underlying_price, strike_step=CRUDEOIL_STRIKE_STEP, strike_range=ATM_STRIKE_RANGE):
    if not chain or underlying_price is None:
        return chain
    atm_strike = round(underlying_price / strike_step) * strike_step
    band = strike_range * strike_step
    zone = []
    for c in chain:
        sv = _strike_val(c)
        if sv is None:
            continue
        if abs(sv - atm_strike) <= band:
            zone.append(c)
    return zone or chain  # never return an empty chain - fall back to full chain


def get_call_put_data(underlying_price=None):
    expiry, chain = find_options_for_nearest_expiry()
    if not chain:
        return None

    if underlying_price is not None:
        chain = filter_chain_to_atm_zone(chain, underlying_price)

    ce_tokens = [c["token"] for c in chain if c.get("symbol", "").endswith("CE")]
    pe_tokens = [c["token"] for c in chain if c.get("symbol", "").endswith("PE")]

    def sum_oi_vol(tokens):
        if not tokens:
            return 0.0, 0.0
        total_oi, total_vol = 0.0, 0.0
        # SmartAPI market data calls are typically batched (<=50 tokens per call)
        for i in range(0, len(tokens), 50):
            batch = tokens[i:i + 50]
            resp = smart.getMarketData(mode="FULL", exchangeTokens={EXCHANGE_SEG_COM: batch})
            if not resp.get("status"):
                continue
            for item in resp["data"].get("fetched", []):
                total_oi += float(item.get("opnInterest", 0) or 0)
                total_vol += float(item.get("tradeVolume", 0) or 0)
        return total_oi, total_vol

    call_oi, call_vol = sum_oi_vol(ce_tokens)
    put_oi, put_vol = sum_oi_vol(pe_tokens)

    return {
        "expiry": str(expiry),
        "call_oi": call_oi,
        "put_oi": put_oi,
        "call_volume": call_vol,
        "put_volume": put_vol,
    }


def option_skew_confirms(signal, cp_data, strict=False):
    if not cp_data:
        return False

    put_oi = cp_data["put_oi"] or 1e-9
    call_oi = cp_data["call_oi"] or 1e-9
    put_vol = cp_data["put_volume"] or 1e-9
    call_vol = cp_data["call_volume"] or 1e-9

    if signal == "BUY":
        if strict:
            return call_oi > put_oi * STRICT_OI_SKEW_RATIO and call_vol > put_vol * STRICT_OI_SKEW_RATIO
        return call_oi > put_oi and call_vol > put_vol
    else:
        if strict:
            return put_oi > call_oi * STRICT_OI_SKEW_RATIO and put_vol > call_vol * STRICT_OI_SKEW_RATIO
        return put_oi > call_oi and put_vol > call_vol

# ==========================
# SIDEWAYS / CHOPPY FILTER
# ==========================
def update_st_history_and_check_sideways(tf_state, current_dir):
    hist = tf_state["st_history"]
    hist.append(current_dir)
    if len(hist) > FLIP_LOOKBACK:
        hist.pop(0)
    flips = sum(1 for i in range(1, len(hist)) if hist[i] != hist[i - 1])
    return flips >= FLIP_THRESHOLD

# ==========================
# AUTO ORDER PLACEMENT (MCX option, via AngelOne SmartAPI)
# ==========================
# ==========================
# ACCOUNT BALANCE / MARGIN CHECK
# ==========================
def get_available_balance():
    """
    Returns available margin/cash (₹) using AngelOne's RMS limit API.
    NOTE: verify the exact field name in your account's response - common
    field is 'availablecash', but confirm via a standalone test call.
    """
    resp = smart.rmsLimit()
    if not resp.get("status"):
        raise Exception(f"rmsLimit failed: {resp}")
    data = resp.get("data", {})
    return float(data.get("availablecash", 0) or 0)


def pick_atm_option(chain, signal, underlying_price):
    """Picks the nearest at-the-money CE (for BUY) or PE (for SELL)."""
    suffix = "CE" if signal == "BUY" else "PE"
    filtered = [c for c in chain if c.get("symbol", "").endswith(suffix) and c.get("strike")]
    if not filtered:
        return None
    # strike stored as paise-like integer in some feeds; adjust divisor if needed
    def strike_val(c):
        try:
            return float(c["strike"]) / 100.0
        except Exception:
            return float("inf")
    return min(filtered, key=lambda c: abs(strike_val(c) - underlying_price))


SL_PREMIUM_BUFFER_PCT = float(os.getenv("SL_PREMIUM_BUFFER_PCT", "0.30"))  # broker-side SL: % below entry premium
LIMIT_ORDER_SLIPPAGE_PCT = float(os.getenv("LIMIT_ORDER_SLIPPAGE_PCT", "0.5"))
LIMIT_ORDER_WAIT_SECONDS = int(os.getenv("LIMIT_ORDER_WAIT_SECONDS", "6"))
LIMIT_ORDER_POLL_INTERVAL = int(os.getenv("LIMIT_ORDER_POLL_INTERVAL", "2"))


def get_option_ltp(token):
    """Live last-traded-price for one option token. Used for option-premium
    P&L tracking and for pricing limit/SL orders. NOTE: verify the exact
    response field name ('ltp') against a standalone smart.getMarketData
    test call for your account."""
    resp = smart.getMarketData(mode="LTP", exchangeTokens={EXCHANGE_SEG_COM: [str(token)]})
    if not resp.get("status"):
        raise Exception(f"getMarketData(LTP) failed: {resp}")
    fetched = resp.get("data", {}).get("fetched", [])
    if not fetched:
        raise Exception("No LTP data returned for token")
    return float(fetched[0]["ltp"])


def calculate_safe_limit_price(ltp, side="BUY", slippage_pct=LIMIT_ORDER_SLIPPAGE_PCT):
    """Bounded limit price instead of an unbounded MARKET order. BUY (our
    only entry side) pays a small cushion ABOVE ltp so the order still
    fills readily; SELL (exits/SL) prices a small cushion BELOW."""
    cushion = ltp * (slippage_pct / 100.0)
    return round(ltp + cushion, 1) if side == "BUY" else round(ltp - cushion, 1)


def has_open_sl_order(tradingsymbol):
    """True if a pending STOPLOSS order already exists at the broker for
    this exact option symbol. NOTE: verify orderBook()'s exact 'status'
    strings and 'ordertype' naming against your account."""
    resp = smart.orderBook()
    if not resp.get("status"):
        return False
    for o in (resp.get("data") or []):
        if o.get("tradingsymbol") != tradingsymbol:
            continue
        if not str(o.get("ordertype", "")).upper().startswith("STOPLOSS"):
            continue
        if str(o.get("status", "")).lower() in ("open", "trigger pending", "pending", "after market order req received"):
            return True
    return False


def place_sl_order(tradingsymbol, symboltoken, qty, entry_premium):
    """
    Places a broker-side STOPLOSS-MARKET SELL order so the position is
    protected even if this bot's process is down (crash, server restart,
    internet loss) - a Python-only SL (checked from check_active_trade_exit)
    is not enough on its own. NOTE: verify 'STOPLOSS_MARKET' ordertype and
    'triggerprice' field naming against your account via a standalone
    smart.placeOrder() test before relying on this live.
    """
    if not AUTO_TRADE_ENABLED:
        return None, None
    trigger_price = round(entry_premium * (1 - SL_PREMIUM_BUFFER_PCT), 1)
    order_params = {
        "variety": "STOPLOSS",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": "SELL",
        "exchange": EXCHANGE_SEG_COM,
        "ordertype": "STOPLOSS_MARKET",
        "producttype": "CARRYFORWARD",
        "duration": "DAY",
        "triggerprice": str(trigger_price),
        "price": "0",
        "quantity": str(qty),
    }
    try:
        sl_order_id = smart.placeOrder(order_params)
        return sl_order_id, trigger_price
    except Exception as e:
        print("SL order placement error:", e)
        return None, trigger_price


def emergency_exit_position(tradingsymbol, symboltoken, qty):
    """Immediate flatten of a position that has NO broker-side SL
    protecting it - used only when the SL order itself failed to place,
    so a trade is never left completely unprotected."""
    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": "SELL",
        "exchange": EXCHANGE_SEG_COM,
        "ordertype": "MARKET",
        "producttype": "CARRYFORWARD",
        "duration": "DAY",
        "price": "0",
        "quantity": str(qty),
    }
    try:
        smart.placeOrder(order_params)
        bot.send_message(CHAT_ID, f"🚨 EMERGENCY EXIT — {tradingsymbol}: broker-side SL failed to place, flattening immediately (no unprotected position allowed).")
    except Exception as e:
        print("EMERGENCY EXIT FAILED:", e)
        try:
            bot.send_message(CHAT_ID, f"🆘 CRITICAL: emergency exit ALSO failed for {tradingsymbol}. Manual intervention required NOW. Error: {e}")
        except Exception:
            pass


def place_limit_order(tradingsymbol, symboltoken, transactiontype, qty, limit_price,
                       wait_seconds=LIMIT_ORDER_WAIT_SECONDS, poll_interval=LIMIT_ORDER_POLL_INTERVAL):
    """
    LIMIT order instead of a raw MARKET order, to avoid unbounded slippage
    on a thin option book. Polls briefly for a fill; if still unfilled
    after wait_seconds, cancels it (caller decides what to do next).
    NOTE: verify smart.placeOrder's order-status lookup / cancelOrder
    signatures against your account - 'individual_order_details' below is
    a placeholder for whichever status-check call your SmartAPI version
    exposes.
    """
    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transactiontype,
        "exchange": EXCHANGE_SEG_COM,
        "ordertype": "LIMIT",
        "producttype": "CARRYFORWARD",
        "duration": "DAY",
        "price": str(limit_price),
        "quantity": str(qty),
    }
    try:
        order_id = smart.placeOrder(order_params)
    except Exception as e:
        print("Limit order placement error:", e)
        return None, False

    filled = False
    waited = 0
    while waited < wait_seconds:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            status_resp = smart.individual_order_details(order_id)
            status_val = str(status_resp.get("data", {}).get("status", "")).lower()
            if status_val in ("complete", "executed"):
                filled = True
                break
        except Exception as e:
            print("Order status check failed:", e)

    if not filled:
        try:
            smart.cancelOrder(order_id, "NORMAL")
        except Exception as e:
            print("Cancel unfilled limit order failed:", e)

    return order_id, filled


def place_market_order(tradingsymbol, symboltoken, signal, lots, lot_size):
    if not AUTO_TRADE_ENABLED:
        print(f"[AUTO_TRADE OFF] Would place {signal} {lots} lot(s) {tradingsymbol} (skipped)")
        return None, None

    # ==========================
    # DUPLICATE ORDER PREVENTION
    # If broker already shows an open position for this exact option
    # symbol, don't place another entry order for it.
    # ==========================
    if check_duplicate_position(tradingsymbol):
        print(f"Duplicate order blocked: open position already exists for {tradingsymbol}")
        try:
            bot.send_message(CHAT_ID, f"🚫 DUPLICATE BLOCKED — {tradingsymbol} already has an open position.")
        except Exception:
            pass
        return None, None

    # ==========================
    # PROACTIVE BALANCE CHECK
    # Checked fresh every time (not cached) - so once you top up funds,
    # the very next cycle (within ~60s) will see it and resume trading
    # automatically. No restart needed.
    # ==========================
    try:
        available = get_available_balance()
    except Exception as e:
        print("Balance check failed:", e)
        try:
            bot.send_message(CHAT_ID, f"⚠️ Could not verify balance, skipping order: {e}")
        except Exception:
            pass
        return None, None

    if available < MIN_MARGIN_REQUIRED:
        print(f"Insufficient balance: ₹{available:.0f} available, ₹{MIN_MARGIN_REQUIRED:.0f} required")
        try:
            bot.send_message(
                CHAT_ID,
                f"⚠️ INSUFFICIENT BALANCE — Trade skipped\n"
                f"Available: ₹{available:.0f} | Required: ₹{MIN_MARGIN_REQUIRED:.0f}\n"
                f"Add funds — bot will auto-resume next cycle once balance is enough."
            )
        except Exception:
            pass
        return None, None

    qty = lots * lot_size

    # ==========================
    # LIMIT ENTRY (never a raw MARKET order) - price a small, bounded
    # cushion above LTP so it still fills readily without unbounded
    # slippage on a thin option book.
    # ==========================
    try:
        ltp = get_option_ltp(symboltoken)
    except Exception as e:
        print("LTP fetch for entry failed:", e)
        try:
            bot.send_message(CHAT_ID, f"⚠️ Could not fetch LTP for {tradingsymbol}, entry skipped: {e}")
        except Exception:
            pass
        return None, None

    limit_price = calculate_safe_limit_price(ltp, side="BUY")

    try:
        order_id, filled = place_limit_order(tradingsymbol, symboltoken, "BUY", qty, limit_price)
    except Exception as e:
        print("Order placement error:", e)
        try:
            bot.send_message(CHAT_ID, f"⚠️ ORDER FAILED [{tradingsymbol} {signal}]: {e}")
        except Exception:
            pass
        return None, None

    if not order_id:
        return None, None

    # Best-known entry premium: LTP at the fill-check moment if filled,
    # otherwise fall back to the limit price used - sync_positions_from_broker()
    # will overwrite this with the broker's actual avg_price shortly after.
    option_entry_price = ltp if filled else limit_price

    # Optimistically record this position locally; the next
    # sync_positions_from_broker() call will reconcile it with the
    # broker's actual confirmed state.
    state["open_positions"][tradingsymbol] = {
        "tradingsymbol": tradingsymbol, "symboltoken": symboltoken,
        "qty": qty, "avg_price": option_entry_price, "side": signal,
    }

    # ==========================
    # BROKER-SIDE SL (Python SL alone is not enough - a crash/restart/
    # internet drop must not leave the position unprotected). If the SL
    # order itself fails to place, flatten immediately rather than run
    # unprotected.
    # ==========================
    sl_order_id, trigger_price = place_sl_order(tradingsymbol, symboltoken, qty, option_entry_price)
    if not sl_order_id:
        try:
            bot.send_message(CHAT_ID, f"⚠️ Broker-side SL FAILED for {tradingsymbol} — emergency-exiting position.")
        except Exception:
            pass
        emergency_exit_position(tradingsymbol, symboltoken, qty)
        return None, None

    return order_id, {
        "option_entry_price": option_entry_price,
        "option_symbol": tradingsymbol,
        "option_token": symboltoken,
        "sl_order_id": sl_order_id,
        "sl_trigger_price": trigger_price,
        "filled": filled,
    }

# ==========================
# PER-TIMEFRAME SIGNAL CHECK
# ==========================
def get_last_price(token, exch_seg, tf):
    """Lightweight fetch of just the latest close price for a timeframe."""
    df = get_candles_for_timeframe(token, exch_seg, tf)
    if len(df) == 0:
        return None
    return df.iloc[-1]["close"]


TRAILING_BREAKEVEN_TRIGGER = 50   # points profit -> move SL to entry (zero loss)
TRAILING_STEP_POINTS = 30         # after breakeven, trail SL up every extra 30 points

def _process_single_trade_exit(tf, trade_no, trade, tf_state, signal, price):
    """
    Runs trailing-SL + target/SL hit check for ONE trade slot (1, 2, or 3).
    Mutates `trade` in place. Returns True if the trade closed this cycle.
    """
    if not trade["active"]:
        return False

    entry = trade["entry_price"]

    if trade["max_price_reached"] is None:
        trade["max_price_reached"] = entry

    if signal == "BUY":
        if price > trade["max_price_reached"]:
            trade["max_price_reached"] = price
        profit_points = trade["max_price_reached"] - entry
    else:
        if price < trade["max_price_reached"]:
            trade["max_price_reached"] = price
        profit_points = entry - trade["max_price_reached"]

    if profit_points >= TRAILING_BREAKEVEN_TRIGGER:
        if not trade["trailing_active"]:
            trade["sl"] = entry
            trade["trailing_active"] = True
        extra_points = profit_points - TRAILING_BREAKEVEN_TRIGGER
        steps = int(extra_points // TRAILING_STEP_POINTS)
        if steps > 0:
            new_sl = entry + steps * TRAILING_STEP_POINTS if signal == "BUY" else entry - steps * TRAILING_STEP_POINTS
            if (signal == "BUY" and new_sl > trade["sl"]) or (signal == "SELL" and new_sl < trade["sl"]):
                trade["sl"] = new_sl

    target, sl = trade["target"], trade["sl"]
    if signal == "BUY":
        target_hit = price >= target
        sl_hit = (sl is not None) and (price <= sl)
    else:
        target_hit = price <= target
        sl_hit = (sl is not None) and (price >= sl)

    option_pnl_line = _option_pnl_line(trade)

    if target_hit:
        points = (price - entry) if signal == "BUY" else (entry - price)
        bot.send_message(CHAT_ID, f"""
🎯 TARGET HIT - EXIT  [{tf}] Trade {trade_no}
{signal} trade closed at target ({trade.get('target_source', 'target')}).

📍 Entry : {entry:.2f}
📍 Exit  : {price:.2f}
📈 Points (underlying) : {points:+.2f}
{option_pnl_line}
""")
        tf_state["trades"][trade_no] = new_trade_slot()
        return True

    if sl_hit:
        points = (price - entry) if signal == "BUY" else (entry - price)
        tag = "🟢 BREAKEVEN/TRAILING" if trade["trailing_active"] else "🛑"
        bot.send_message(CHAT_ID, f"""
{tag} SL HIT - EXIT  [{tf}] Trade {trade_no}
{signal} trade closed at stop loss.

📍 Entry : {entry:.2f}
📍 Exit  : {price:.2f}
📈 Points (underlying) : {points:+.2f}
{option_pnl_line}
""")
        tf_state["trades"][trade_no] = new_trade_slot()
        return True

    return False


def _option_pnl_line(trade):
    """Best-effort option-premium P&L string for exit messages - the
    underlying target/SL above is still what drives the exit decision,
    this is additional visibility into the ACTUAL option P&L (which can
    diverge from underlying points due to theta/IV)."""
    token = trade.get("option_token")
    entry_premium = trade.get("option_entry_price")
    if not token or entry_premium is None:
        return ""
    try:
        option_ltp = get_option_ltp(token)
    except Exception as e:
        print("Option LTP fetch for P&L line failed:", e)
        return ""
    pnl_per_unit = option_ltp - entry_premium  # we only ever BUY options, so this is always long-side P&L
    return f"💵 Option Premium : {entry_premium:.2f} → {option_ltp:.2f}  (P&L/unit: {pnl_per_unit:+.2f})"


def check_active_trade_exit(tf, tf_state, token, exch_seg):
    """
    Live-tracks Trade 1, Trade 2, and Trade 3 on this timeframe
    independently and sends an EXIT message the moment price crosses
    that trade's own target or SL. If Trade 1 & Trade 2 have both
    closed and Trade 3 hasn't started yet, it does NOT start here -
    that needs fresh fib data, so it's handled separately in analyze()
    via maybe_start_trade3().
    """
    if tf_state["trend"] is None:
        return

    active_nos = [n for n, t in tf_state["trades"].items() if t["active"]]
    if not active_nos:
        return

    try:
        price = get_last_price(token, exch_seg, tf)
    except Exception as e:
        print(f"Exit-check price fetch error [{tf}]:", e)
        return

    if price is None:
        return

    signal = tf_state["trend"]

    # If ALL trades close this cycle and none are left running / pending
    # trade3, the overall trend for this timeframe is done.
    for trade_no in active_nos:
        _process_single_trade_exit(tf, trade_no, tf_state["trades"][trade_no], tf_state, signal, price)

    any_active = any(t["active"] for t in tf_state["trades"].values())
    if not any_active and tf_state["trade3_started"]:
        # Trade 3 (the last leg) has also closed - this trend cycle is over.
        tf_state["trend"] = None
        tf_state["trade3_started"] = False
        tf_state["recovered"] = False


def get_trend_direction(token, exch_seg, tf):
    """Current Supertrend direction ('BUY'/'SELL') for a timeframe, or
    None if there isn't enough data. Used to build the master/setup
    trend filter, independent of any specific entry signal."""
    df = get_candles_for_timeframe(token, exch_seg, tf)
    if len(df) < 20:
        return None
    st_length, st_mult = SUPERTREND_PARAMS.get(tf, DEFAULT_SUPERTREND_PARAMS)
    st_df = ta.supertrend(df["high"], df["low"], df["close"], length=st_length, multiplier=st_mult)
    df = pd.concat([df, st_df], axis=1)
    st_cols = [c for c in df.columns if "supertd" in c.lower()]
    if not st_cols:
        return None
    last_dir = df.iloc[-1][st_cols[0]]
    if pd.isna(last_dir):
        return None
    return "BUY" if last_dir == 1 else "SELL"


def get_master_trend(token, exch_seg):
    """Master (macro) trend: ALL of MASTER_TF must agree on direction,
    otherwise returns None (no aligned trend -> no new entries)."""
    dirs = set()
    for mtf in MASTER_TF:
        try:
            d = get_trend_direction(token, exch_seg, mtf)
        except Exception as e:
            print(f"Master trend fetch error [{mtf}]:", e)
            d = None
        if d is None:
            return None
        dirs.add(d)
    return dirs.pop() if len(dirs) == 1 else None


def check_timeframe_signal(token, exch_seg, tf, monthly_fib, weekly_fib, daily_fib, session_5pm_fib, cp_data, tf_state, master_trend=None):
    df = get_candles_for_timeframe(token, exch_seg, tf)
    if len(df) < 20:
        return None

    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    st_length, st_mult = SUPERTREND_PARAMS.get(tf, DEFAULT_SUPERTREND_PARAMS)
    st_df = ta.supertrend(df["high"], df["low"], df["close"], length=st_length, multiplier=st_mult)
    df = pd.concat([df, st_df], axis=1)

    st_cols = [c for c in df.columns if "supertd" in c.lower()]
    if not st_cols:
        return None
    st_col = st_cols[0]

    last = df.iloc[-1]
    candle_ts = str(last["timestamp"])
    rsi, atr, price, candle_open, candle_dir = (
        last["rsi"], last["atr"], last["close"], last["open"], last[st_col]
    )

    if pd.isna(rsi) or pd.isna(atr) or pd.isna(candle_dir):
        return None

    is_sideways = update_st_history_and_check_sideways(tf_state, candle_dir)
    if is_sideways:
        return {"sideways": True}

    bullish_candle = price > candle_open
    bearish_candle = price < candle_open

    signal = None
    if candle_dir == 1 and rsi > 55 and bullish_candle:
        signal = "BUY"
    elif candle_dir == -1 and rsi < 45 and bearish_candle:
        signal = "SELL"

    if not signal:
        return None

    # MASTER TREND GATE: on the entry timeframe, never take a signal that
    # goes against the aligned master/setup trend - no more relaxed
    # tolerance override for this timeframe either (that was letting weak
    # 10m-only setups trigger against the bigger trend).
    if tf == ENTRY_TF and master_trend is not None and signal != master_trend:
        return None

    strict_mode = is_session_watch_window()
    tolerance = STRICT_FIB_MATCH_PCT if strict_mode else FIB_MATCH_PCT

    matched, window_name, level_name, level_val = price_matches_fib_level(
        monthly_fib, weekly_fib, daily_fib, session_5pm_fib, price, tolerance_pct=tolerance
    )
    if not matched:
        return None

    if not option_skew_confirms(signal, cp_data, strict=strict_mode):
        return None

    return {
        "sideways": False, "signal": signal, "price": price, "atr": atr, "rsi": rsi,
        "window_name": window_name, "level_name": level_name, "level_val": level_val,
        "cp_data": cp_data, "strict_mode": strict_mode, "candle_ts": candle_ts,
    }

# ==========================
# SEND TRADE MESSAGE + AUTO ORDER
# ==========================
MARKET_WEAK_ATR_RATIO = 0.7    # current ATR below this fraction of recent avg ATR = quiet
MARKET_WEAK_RSI_BAND = (45, 55)  # RSI inside this band = no clear momentum
MARKET_WEAK_BODY_RATIO = 0.3   # candle body below this fraction of ATR = small move

def is_market_weak(token, exch_seg, tf):
    """
    True only when ALL THREE show a quiet/choppy market together:
      1. Current ATR is well below its recent (20-bar) average
      2. RSI is sitting in the neutral 45-55 band (no clear momentum)
      3. The latest candle's body is small relative to ATR
    Used to decide whether Trade 1 & 2 should open WITHOUT a hard SL
    (ride to target or trend-flip instead) since a normal ATR-based SL
    is too tight to survive noise in a quiet market.
    """
    df = get_candles_for_timeframe(token, exch_seg, tf)
    if len(df) < 20:
        return False

    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    last = df.iloc[-1]
    rsi_now, atr_now, price, candle_open = last["rsi"], last["atr"], last["close"], last["open"]
    if pd.isna(rsi_now) or pd.isna(atr_now):
        return False

    atr_avg = df["atr"].tail(20).mean()
    if pd.isna(atr_avg) or atr_avg == 0:
        return False

    atr_weak = atr_now < (atr_avg * MARKET_WEAK_ATR_RATIO)
    rsi_weak = MARKET_WEAK_RSI_BAND[0] <= rsi_now <= MARKET_WEAK_RSI_BAND[1]
    body = abs(price - candle_open)
    body_weak = (body < (atr_now * MARKET_WEAK_BODY_RATIO)) if atr_now else False

    return atr_weak and rsi_weak and body_weak


MAX_SL_TO_TARGET_RATIO = 0.5   # SL distance can never exceed this fraction of target distance

def _open_trade(tf_state, trade_no, signal, price, atr, fib_used, window_name, uncapped,
                 sl_atr_multiplier=None, weak_market=False):
    """Computes target/SL from current price and activates one trade slot.
    SL is ALWAYS ATR-based - every trade (1, 2, 3) has a stop loss,
    no exceptions. sl_atr_multiplier widens/narrows the initial SL distance
    (e.g. 1.5 for Trade 2 riding an uncapped/strong-trend move, so a
    normal wiggle doesn't stop it out early) - the existing trailing-SL
    logic still follows price up from there regardless of this width.

    When weak_market is True (see is_market_weak): the fallback target
    distance shrinks to WEAK_TREND_FALLBACK_POINTS instead of the normal
    150/70, and - unless the caller explicitly passed sl_atr_multiplier -
    the SL also tightens to WEAK_TREND_SL_ATR_MULTIPLIER, so the trade can
    complete on a small move instead of waiting for a big fallback swing.

    RISK:REWARD SAFETY CAP: ATR is computed independently of the target
    distance, so on a volatile candle the raw ATR-based SL distance can
    end up LARGER than the target distance - i.e. a "small SL, big
    profit" trade could silently become the opposite. After the normal
    SL is computed, its distance from entry is capped to at most
    MAX_SL_TO_TARGET_RATIO of the target distance, so every trade always
    risks less than it targets."""
    if sl_atr_multiplier is None:
        sl_atr_multiplier = WEAK_TREND_SL_ATR_MULTIPLIER if weak_market else 1.0

    fallback_points = get_fallback_target_points(trade_no, atr, weak_market=weak_market)

    if signal == "BUY":
        fib_target = nearest_level_above(fib_used, price)
        if uncapped:
            target, target_source = fib_target, f"{window_name} fib (uncapped)"
        else:
            fallback_target = price + fallback_points
            if (fib_target - price) <= fallback_points:
                target, target_source = fib_target, f"{window_name} fib"
            else:
                target, target_source = fallback_target, f"fallback {fallback_points}pts"
        sl = price - (atr * sl_atr_multiplier)
        target_distance = target - price
        sl_distance = price - sl
        max_sl_distance = target_distance * MAX_SL_TO_TARGET_RATIO
        if target_distance > 0 and sl_distance > max_sl_distance:
            sl = price - max_sl_distance
    else:
        fib_target = nearest_level_below(fib_used, price)
        if uncapped:
            target, target_source = fib_target, f"{window_name} fib (uncapped)"
        else:
            fallback_target = price - fallback_points
            if (price - fib_target) <= fallback_points:
                target, target_source = fib_target, f"{window_name} fib"
            else:
                target, target_source = fallback_target, f"fallback {fallback_points}pts"
        sl = price + (atr * sl_atr_multiplier)
        target_distance = price - target
        sl_distance = sl - price
        max_sl_distance = target_distance * MAX_SL_TO_TARGET_RATIO
        if target_distance > 0 and sl_distance > max_sl_distance:
            sl = price + max_sl_distance

    trade = tf_state["trades"][trade_no]
    trade["active"] = True
    trade["entry_price"] = price
    trade["target"] = target
    trade["target_source"] = target_source
    trade["sl"] = sl
    trade["max_price_reached"] = None
    trade["trailing_active"] = False
    return target, target_source, sl


def _place_auto_order(option_chain, signal, underlying_price):
    """Returns (order_line, order_meta). order_meta is a dict with
    option_entry_price/option_symbol/option_token/sl_order_id when a live
    order + broker-side SL were both placed successfully, else None -
    callers should copy order_meta's fields into the relevant trade
    slot(s) so exit-tracking can use real option premium, not just the
    underlying's move."""
    order_line = "🔒 Auto-trade OFF (signal only)"
    chosen = pick_atm_option(option_chain, signal, underlying_price)
    if not chosen:
        return order_line, None

    lot_size = int(chosen.get("lotsize", 1))
    order_id, order_meta = place_market_order(chosen["symbol"], chosen["token"], signal, ORDER_LOTS, lot_size)
    if order_id and order_meta:
        order_line = (
            f"✅ LIVE ORDER PLACED : {chosen['symbol']} x {ORDER_LOTS} lot(s)\n"
            f"🛡️ Broker-side SL @ {order_meta['sl_trigger_price']:.2f}"
        )
    else:
        order_line += f"\n(Nearest option: {chosen['symbol']})"
    return order_line, order_meta


def _apply_order_meta_to_trades(tf_state, trade_nos, order_meta):
    """Copies option_entry_price/symbol/token/sl_order_id from a placed
    order into one or more trade slots that share that same underlying
    option position."""
    if not order_meta:
        return
    for trade_no in trade_nos:
        trade = tf_state["trades"][trade_no]
        trade["option_entry_price"] = order_meta["option_entry_price"]
        trade["option_symbol"] = order_meta["option_symbol"]
        trade["option_token"] = order_meta["option_token"]
        trade["sl_order_id"] = order_meta["sl_order_id"]


def process_timeframe_trade(tf, result, monthly_fib, weekly_fib, daily_fib, session_5pm_fib, tf_state, option_chain, underlying_price, token, exch_seg):
    """
    Handles a fresh validated signal for this timeframe:
      - No existing trend -> starts Trade 1 & Trade 2 TOGETHER at the
        same entry, running fully independently of each other.
          * Market strong: Trade 1 = 150pt-capped (or nearer fib),
            Trade 2 = uncapped fib (rides as far as the trend goes).
          * Market weak/choppy (is_market_weak): BOTH Trade 1 & Trade 2
            use the small capped target instead - no uncapped chase
            when there's no real chance of a big move.
        Every trade ALWAYS has an ATR-based SL - no exceptions.
      - Existing trend, opposite signal (flip) -> force-closes whatever
        of Trade 1/2/3 is still active with a REVERSED message, then
        starts a new Trade 1 & Trade 2 pair in the new direction.
      - Existing trend, same signal -> no-op (Trade 1/2/3 already
        govern the position; repeat signals don't open anything new).
    Trade 3 is NOT opened here - see maybe_start_trade3(), which opens
    it once Trade 1 & Trade 2 have both closed.
    This same logic applies symmetrically whether the trend is BUY or SELL.
    """
    signal, price, atr, rsi, cp = result["signal"], result["price"], result["atr"], result["rsi"], result["cp_data"]
    fib_map = {"monthly": monthly_fib, "weekly": weekly_fib, "daily": daily_fib, "session5pm": session_5pm_fib}
    fib_used = fib_map[result["window_name"]]

    candle_ts = result.get("candle_ts")
    if candle_ts is not None and tf_state.get("last_signal_candle_ts") == candle_ts and tf_state["trend"] == signal:
        return  # already acted on this exact candle for this timeframe+direction

    is_flip = tf_state["trend"] is not None and tf_state["trend"] != signal
    is_fresh = tf_state["trend"] is None

    if not (is_flip or is_fresh):
        tf_state["last_signal_candle_ts"] = candle_ts
        return  # already trending this direction - Trade 1/2/3 already active

    # GLOBAL POSITION LIMIT: never open a new Trade 1/2 pair if it would
    # push total active trades (across all timeframes) past the cap.
    if count_active_trades() >= MAX_ACTIVE_TRADES_GLOBAL and not is_flip:
        print(f"Global position limit reached ({MAX_ACTIVE_TRADES_GLOBAL}) - skipping new entry on {tf}")
        tf_state["last_signal_candle_ts"] = candle_ts
        return

    if is_flip:
        old_trend = tf_state["trend"]
        for trade_no, trade in tf_state["trades"].items():
            if trade["active"]:
                old_entry = trade["entry_price"]
                points_moved = (price - old_entry) if old_trend == "BUY" else (old_entry - price)
                result_word = "PROFIT" if points_moved > 0 else ("LOSS" if points_moved < 0 else "FLAT")
                bot.send_message(CHAT_ID, f"""
🔄 CRUDEOILM {old_trend} TREND REVERSED  [{tf}] Trade {trade_no}
⚠️ EXIT old {old_trend} trade now (trend flipping)

📍 Old Entry : {old_entry:.2f}
📍 Exit Price : {price:.2f}
📈 Points Moved : {points_moved:+.2f} ({result_word})
""")
        tf_state["trades"] = {1: new_trade_slot(), 2: new_trade_slot(), 3: new_trade_slot()}
        tf_state["trade3_started"] = False

    # Fresh trend start (either brand new, or right after a flip):
    # open Trade 1 & Trade 2 TOGETHER at this entry.
    tf_state["trend"] = signal
    tf_state["fib_window"] = result["window_name"]

    try:
        weak_market = is_market_weak(token, exch_seg, tf)
    except Exception as e:
        print(f"Market-weak check error [{tf}]:", e)
        weak_market = False

    # Trade 1 is always capped. Trade 2 rides uncapped ONLY when the
    # market is genuinely strong - in a weak/choppy market it takes the
    # same small capped target as Trade 1 instead of chasing a far fib.
    # When uncapped (strong trend, longer chase), Trade 2 also gets a
    # LOOSER initial SL (1.5x ATR instead of 1x) so normal wiggle on the
    # way to a distant fib target doesn't stop it out early - the
    # existing trailing-SL logic still locks in profit as it moves.
    t2_uncapped = not weak_market
    t1_target, t1_src, t1_sl = _open_trade(
        tf_state, 1, signal, price, atr, fib_used, result["window_name"],
        uncapped=False, weak_market=weak_market
    )
    t2_target, t2_src, t2_sl = _open_trade(
        tf_state, 2, signal, price, atr, fib_used, result["window_name"],
        uncapped=t2_uncapped, sl_atr_multiplier=(1.5 if t2_uncapped else None),
        weak_market=weak_market
    )

    oi_line = (f"📞 Call OI/Vol : {cp['call_oi']:.0f} / {cp['call_volume']:.0f}\n"
               f"📉 Put OI/Vol  : {cp['put_oi']:.0f} / {cp['put_volume']:.0f}  (exp {cp['expiry']})")

    # AUTO ORDER: one live broker order for this signal. Trade 1 & Trade 2
    # are both tracked here purely for target/SL/Telegram purposes on this
    # SAME underlying option position (placing 2 live orders on the same
    # ATM contract would be blocked as a duplicate position).
    order_line, order_meta = _place_auto_order(option_chain, signal, underlying_price)
    _apply_order_meta_to_trades(tf_state, [1, 2], order_meta)

    session_line = "🌐 SESSION WATCH (strict confirm)" if result.get("strict_mode") else ""
    priority_line = f"⭐ PRIORITY TIMEFRAME ({PRIORITY_TIMEFRAME})" if tf == PRIORITY_TIMEFRAME else ""
    weak_line = (
        f"⚠️ Market weak/choppy - small target ({WEAK_TREND_FALLBACK_POINTS[1]}pts) "
        f"+ tight SL ({WEAK_TREND_SL_ATR_MULTIPLIER}x ATR) on both trades, no 150pt fallback wait"
        if weak_market else ""
    )

    msg = f"""
🎯 CRUDEOILM {signal} TRADE 1 & TRADE 2 STARTED  [{tf}]
{session_line}
{priority_line}
{weak_line}

💰 Entry (underlying) : {price:.2f}

🅰️ Trade 1 Target ({t1_src}) : {t1_target:.2f}
🅰️ Trade 1 SL (ATR) : {t1_sl:.2f}

🅱️ Trade 2 Target ({t2_src}) : {t2_target:.2f}
🅱️ Trade 2 SL ({"1.5x ATR" if t2_uncapped else "ATR"}) : {t2_sl:.2f}

📊 RSI : {rsi:.2f}
📐 Matched Level : {result['window_name']}-{result['level_name']} ({result['level_val']:.2f})
{oi_line}
{order_line}
"""
    bot.send_message(CHAT_ID, msg)
    tf_state["last_signal_candle_ts"] = candle_ts


def get_last_price_and_atr(token, exch_seg, tf):
    """Lightweight fetch of the latest close + ATR(14) for a timeframe."""
    df = get_candles_for_timeframe(token, exch_seg, tf)
    if len(df) == 0:
        return None, None
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    last = df.iloc[-1]
    atr = last["atr"]
    if pd.isna(atr):
        return last["close"], None
    return last["close"], atr


TREND_WEAKENING_ST_PROXIMITY_PCT = 0.15  # Supertrend line within this % of price counts as "close" (early warning)

def is_trend_weakening(token, exch_seg, tf, signal):
    """
    True only when ALL THREE early-warning signs show up together:
      1. Supertrend line has come very close to price (within
         TREND_WEAKENING_ST_PROXIMITY_PCT%)
      2. RSI is reversing direction (falling during a BUY trend,
         rising during a SELL trend)
      3. The latest candle closed against the trend direction
         (bearish candle during BUY, bullish during SELL)
    Used to gate Trade 3 - it should only start once the trend shows
    real, combined signs of nearing its end, not immediately after
    Trade 1 & 2 close, and not on just one weak signal alone.
    """
    df = get_candles_for_timeframe(token, exch_seg, tf)
    if len(df) < 20:
        return False

    df["rsi"] = ta.rsi(df["close"], length=14)
    st_length, st_mult = SUPERTREND_PARAMS.get(tf, DEFAULT_SUPERTREND_PARAMS)
    st_df = ta.supertrend(df["high"], df["low"], df["close"], length=st_length, multiplier=st_mult)
    df = pd.concat([df, st_df], axis=1)

    line_cols = [c for c in df.columns if c.lower().startswith("supert_")]
    if not line_cols:
        return False
    line_col = line_cols[0]

    last, prev = df.iloc[-1], df.iloc[-2]
    price, st_line, candle_open = last["close"], last[line_col], last["open"]
    rsi_now, rsi_prev = last["rsi"], prev["rsi"]
    if pd.isna(price) or pd.isna(st_line) or pd.isna(candle_open) or pd.isna(rsi_now) or pd.isna(rsi_prev):
        return False

    proximity_pct = abs(price - st_line) / price * 100
    close_to_flip = proximity_pct <= TREND_WEAKENING_ST_PROXIMITY_PCT

    rsi_reversing = (rsi_now < rsi_prev) if signal == "BUY" else (rsi_now > rsi_prev)

    opposite_candle = (price < candle_open) if signal == "BUY" else (price > candle_open)

    # All three must agree - a single weak signal alone isn't enough.
    return close_to_flip and rsi_reversing and opposite_candle


def maybe_start_trade3(tf, tf_state, monthly_fib, weekly_fib, daily_fib, session_5pm_fib, option_chain, token, exch_seg):
    """
    Starts Trade 3 - same direction as Trade 1 & 2, fresh entry - once
    Trade 1 & Trade 2 have BOTH closed, the trend shows an early sign of
    weakening (see is_trend_weakening), AND price has pulled back to a
    nearby Fib/Pivot level (a small correction) rather than entering
    immediately at whatever price weakening was first detected at.
    Runs every cycle so it can fire as soon as all conditions hold.
    If the trend flips before Trade 1 & 2 both close, process_timeframe_trade()
    will have already reset trend/trades and this simply won't fire for
    the old direction.
    """
    if tf_state["trend"] is None or tf_state["trade3_started"]:
        return
    t1, t2 = tf_state["trades"][1], tf_state["trades"][2]
    if t1["active"] or t2["active"]:
        return  # not both closed yet

    signal = tf_state["trend"]

    try:
        if not is_trend_weakening(token, exch_seg, tf, signal):
            return  # trend still going strong - wait
    except Exception as e:
        print(f"Trend-weakening check error [{tf}]:", e)
        return

    try:
        price, atr = get_last_price_and_atr(token, exch_seg, tf)
    except Exception as e:
        print(f"Trade3-start price fetch error [{tf}]:", e)
        return
    if price is None or atr is None:
        return

    # ==========================
    # FIB PULLBACK CONFIRMATION
    # Weakening alone isn't enough - wait for price to actually pull back
    # to a nearby Fib/Pivot level (a small correction) before entering,
    # rather than chasing the entry at the exact weakening moment.
    # ==========================
    matched, window_name, level_name, level_val = price_matches_fib_level(
        monthly_fib, weekly_fib, daily_fib, session_5pm_fib, price
    )
    if not matched:
        return  # weakening confirmed, but still waiting for a fib pullback level

    fib_map = {"monthly": monthly_fib, "weekly": weekly_fib, "daily": daily_fib, "session5pm": session_5pm_fib}
    fib_used = fib_map[window_name]

    target, target_source, sl = _open_trade(
        tf_state, 3, signal, price, atr, fib_used, window_name,
        uncapped=False, weak_market=True
    )
    tf_state["trade3_started"] = True

    order_line, order_meta = _place_auto_order(option_chain, signal, price)
    _apply_order_meta_to_trades(tf_state, [3], order_meta)

    msg = f"""
⚠️ CRUDEOILM {signal} FINAL TRADE (Trade 3)  [{tf}]
Trade 1 & Trade 2 both closed, trend weakening confirmed, price pulled back to a fib level - entering final leg.

📐 Pullback Level : {window_name}-{level_name} ({level_val:.2f})
💰 Entry (underlying) : {price:.2f}
🎯 Target ({target_source}) : {target:.2f}
🛑 SL (ATR) : {sl:.2f}
{order_line}
"""
    bot.send_message(CHAT_ID, msg)

# ==========================
# MAIN ANALYZE
# ==========================
def analyze():
    fut = find_nearest_future()
    if not fut:
        print("No nearest future contract found")
        state["dashboard"]["skip_reason"] = "No nearest future contract found"
        state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return

    token, exch_seg = fut["token"], EXCHANGE_SEG_COM
    state["underlying_token"] = token
    state["underlying_symbol"] = fut.get("symbol") or fut.get("tradingsymbol")

    # Refresh broker positions every cycle - keeps duplicate-order
    # prevention accurate without waiting for a restart.
    sync_positions_from_broker(notify_new=False)

    # ==========================
    # LIVE EXIT TRACKING - runs every cycle regardless of fib/OI data
    # availability, so active trades are never left unmonitored.
    # ==========================
    for tf in CONFIRM_TIMEFRAMES:
        tf_state = state["timeframes"][tf]
        try:
            check_active_trade_exit(tf, tf_state, token, exch_seg)
        except Exception as e:
            print(f"Exit-check error [{tf}]:", e)

    # ==========================
    # /pause command: stop opening NEW trades, but exit tracking above
    # still runs every cycle so any already-open trade is never left
    # unmonitored.
    # ==========================
    if state["paused"]:
        state["dashboard"]["skip_reason"] = "paused"
        state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return

    try:
        refresh_fib_levels_if_needed(token, exch_seg)
        refresh_5pm_session_fib_if_needed(token, exch_seg)
    except Exception as e:
        print("Fib fetch error:", e)
        state["dashboard"]["last_error"] = f"Fib fetch error: {e}"
        return

    monthly_fib, weekly_fib = state["monthly_fib"], state["weekly_fib"]
    daily_fib = state["daily_fib"]
    session_5pm_fib = state["session_5pm_fib"]

    try:
        atm_ref_price = get_last_price(token, exch_seg, ENTRY_TF)
    except Exception as e:
        print("ATM reference price fetch error:", e)
        atm_ref_price = None

    try:
        cp_data = get_call_put_data(atm_ref_price)
    except Exception as e:
        print("Call/Put OI fetch error:", e)
        cp_data = None

    state["dashboard"]["cp_data"] = cp_data
    if atm_ref_price is not None:
        state["dashboard"]["underlying_price"] = float(atm_ref_price)

    if cp_data is None:
        print("Skipping new-signal check: no options OI/volume data available")
        state["dashboard"]["skip_reason"] = "no options OI/volume data"
        state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return

    _, option_chain = find_options_for_nearest_expiry()

    # ==========================
    # MASTER -> SETUP -> ENTRY TIMEFRAME HIERARCHY (fixes overtrading)
    # Only ENTRY_TF ("10m") can open a NEW trade, and only when all three
    # layers agree on direction:
    #   MASTER_TF (4h + 1h, must all agree) -> SETUP_TF (15m) -> ENTRY_TF
    # If master/setup don't agree, skip new-signal scanning entirely this
    # cycle (existing trades on any timeframe are still exit-tracked above
    # regardless of this gate).
    # ==========================
    try:
        master_trend = get_master_trend(token, exch_seg)
    except Exception as e:
        print("Master trend fetch error:", e)
        master_trend = None

    try:
        setup_trend = get_trend_direction(token, exch_seg, SETUP_TF) if master_trend else None
    except Exception as e:
        print("Setup trend fetch error:", e)
        setup_trend = None

    state["dashboard"]["master_trend"] = master_trend
    state["dashboard"]["setup_trend"] = setup_trend
    try:
        state["dashboard"]["entry_trend"] = get_trend_direction(token, exch_seg, ENTRY_TF)
    except Exception:
        state["dashboard"]["entry_trend"] = None

    if master_trend is None or setup_trend is None or setup_trend != master_trend:
        state["dashboard"]["skip_reason"] = "master/setup timeframe not aligned"
        state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return  # no aligned macro trend - don't scan for new entries this cycle

    tf = ENTRY_TF
    tf_state = state["timeframes"][tf]

    # Trade 3 doesn't need a fresh signal - it starts as soon as
    # Trade 1 & Trade 2 have both closed, same direction.
    try:
        maybe_start_trade3(tf, tf_state, monthly_fib, weekly_fib, daily_fib, session_5pm_fib, option_chain, token, exch_seg)
    except Exception as e:
        print(f"Trade3-start error [{tf}]:", e)

    try:
        result = check_timeframe_signal(token, exch_seg, tf, monthly_fib, weekly_fib, daily_fib, session_5pm_fib, cp_data, tf_state, master_trend)
    except Exception as e:
        print(f"Timeframe {tf} error:", e)
        return

    if not result or result.get("sideways"):
        state["dashboard"]["skip_reason"] = "sideways" if result and result.get("sideways") else "no entry signal"
        state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return

    underlying_price = result["price"]
    state["dashboard"]["underlying_price"] = float(underlying_price)
    process_timeframe_trade(tf, result, monthly_fib, weekly_fib, daily_fib, session_5pm_fib, tf_state, option_chain, underlying_price, token, exch_seg)
    state["dashboard"]["skip_reason"] = None
    state["dashboard"]["last_error"] = None
    state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")

# ==========================
# LOOP
# ==========================
def run():
    was_open = None
    while True:
        try:
            now_open = is_market_open()

            if was_open is True and now_open is False:
                close_all_open_trades_for_day()

            state["dashboard"]["market_open"] = now_open
            if now_open:
                analyze()
            else:
                print("Market closed - skipping cycle")
                state["dashboard"]["skip_reason"] = "market closed"
                state["dashboard"]["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")

            was_open = now_open
        except Exception as e:
            print("Error:", e)
            # session may have expired - try logging in again next cycle
            try:
                login()
            except Exception as e2:
                print("Re-login failed:", e2)
        time.sleep(60)

# ==========================
# SELF-HEALING WRAPPER
# run() already never dies from an analyze()/login problem (it has its
# own try/except + re-login every cycle). This wrapper is the outer
# safety net for the rare case something escapes even that (or
# run_telegram_polling(), which has no inner loop of its own) - instead
# of that thread silently dying and the bot going quiet forever, it's
# auto-restarted after a short pause and you get notified on Telegram.
# ==========================
def run_forever(target, name):
    while True:
        try:
            target()
        except Exception as e:
            print(f"{name} crashed, restarting in 10s: {e}")
            try:
                bot.send_message(CHAT_ID, f"⚠️ {name} crashed and restarted automatically.\nReason: {e}")
            except Exception:
                pass
        time.sleep(10)

# ==========================
# TELEGRAM COMMANDS (/status, /pause, /resume, /balance)
# Only responds to messages from CHAT_ID - anyone else messaging this
# bot is ignored, so a stranger can never pause/resume your trading or
# see your balance even if they find the bot's username.
# ==========================
def _is_authorized(message):
    return str(message.chat.id) == str(CHAT_ID)


@bot.message_handler(commands=["status"])
def handle_status(message):
    if not _is_authorized(message):
        return
    lines = [f"📊 STATUS — {'⏸️ PAUSED' if state['paused'] else '▶️ RUNNING'}"]
    any_active = False
    for tf in CONFIRM_TIMEFRAMES:
        tf_state = state["timeframes"][tf]
        if tf_state["trend"] is None:
            continue
        active_trades = {n: t for n, t in tf_state["trades"].items() if t["active"]}
        if not active_trades:
            continue
        any_active = True
        lines.append(f"\n[{tf}] Trend: {tf_state['trend']}")
        for trade_no, t in active_trades.items():
            lines.append(
                f"  Trade{trade_no} — Entry:{t['entry_price']:.2f} "
                f"Target:{t['target']:.2f} SL:{t['sl']:.2f}"
            )
    if not any_active:
        lines.append("\nNo active trades right now.")
    bot.send_message(CHAT_ID, "\n".join(lines))


@bot.message_handler(commands=["pause"])
def handle_pause(message):
    if not _is_authorized(message):
        return
    state["paused"] = True
    bot.send_message(CHAT_ID, "⏸️ PAUSED — no new trades will open. Existing active trades are still tracked and will exit normally on target/SL. Send /resume to continue.")


@bot.message_handler(commands=["resume"])
def handle_resume(message):
    if not _is_authorized(message):
        return
    state["paused"] = False
    bot.send_message(CHAT_ID, "▶️ RESUMED — new trades can open again.")


@bot.message_handler(commands=["balance"])
def handle_balance(message):
    if not _is_authorized(message):
        return
    try:
        available = get_available_balance()
        bot.send_message(CHAT_ID, f"💰 Available Margin : ₹{available:.0f}")
    except Exception as e:
        bot.send_message(CHAT_ID, f"⚠️ Could not fetch balance: {e}")


def run_telegram_polling():
    bot.infinity_polling(skip_pending=True)

# ==========================
# REACT DASHBOARD API
# The trading engine stays in Python. React reads this JSON and can
# pause/resume. Broker keys never leave the server.
# ==========================
def _json_safe(obj):
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        if pd.isna(obj):
            return None
        return float(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return str(obj)
    try:
        if pd.isna(obj):
            return None
    except Exception:
        pass
    if hasattr(obj, "item"):
        try:
            return _json_safe(obj.item())
        except Exception:
            pass
    return str(obj)


def _dashboard_authorized():
    if not DASHBOARD_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {DASHBOARD_TOKEN}"


def _status_payload():
    timeframes = {}
    for tf, tf_state in state["timeframes"].items():
        trades = {}
        for n, t in tf_state["trades"].items():
            trades[str(n)] = {
                "active": t["active"],
                "entry_price": t["entry_price"],
                "target": t["target"],
                "target_source": t["target_source"],
                "sl": t["sl"],
                "trailing_active": t["trailing_active"],
                "option_symbol": t.get("option_symbol"),
                "option_entry_price": t.get("option_entry_price"),
            }
        timeframes[tf] = {
            "trend": tf_state["trend"],
            "trade3_started": tf_state["trade3_started"],
            "recovered": tf_state["recovered"],
            "trades": trades,
        }
    return {
        "paused": state["paused"],
        "auto_trade_enabled": AUTO_TRADE_ENABLED,
        "market_open": is_market_open() if state["dashboard"]["market_open"] is None else state["dashboard"]["market_open"],
        "session_watch": is_session_watch_window(),
        "underlying_symbol": state.get("underlying_symbol"),
        "fib_updated": state.get("fib_updated"),
        "positions_synced_at": state.get("positions_synced_at"),
        "open_positions": list(state.get("open_positions", {}).values()),
        "monthly_fib": state.get("monthly_fib"),
        "weekly_fib": state.get("weekly_fib"),
        "daily_fib": state.get("daily_fib"),
        "session_5pm_fib": state.get("session_5pm_fib"),
        "timeframes": timeframes,
        "active_trade_count": count_active_trades(),
        "max_active_trades": MAX_ACTIVE_TRADES_GLOBAL,
        "entry_tf": ENTRY_TF,
        "setup_tf": SETUP_TF,
        "master_tf": MASTER_TF,
        "dashboard": state.get("dashboard"),
    }


@app.route("/api/status", methods=["GET", "OPTIONS"])
def api_status():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _dashboard_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_json_safe(_status_payload()))


@app.route("/api/pause", methods=["POST", "OPTIONS"])
def api_pause():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _dashboard_authorized():
        return jsonify({"error": "unauthorized"}), 401
    state["paused"] = True
    return jsonify({"ok": True, "paused": True})


@app.route("/api/resume", methods=["POST", "OPTIONS"])
def api_resume():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _dashboard_authorized():
        return jsonify({"error": "unauthorized"}), 401
    state["paused"] = False
    return jsonify({"ok": True, "paused": False})


@app.route("/api/balance", methods=["GET", "OPTIONS"])
def api_balance():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _dashboard_authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        available = get_available_balance()
        return jsonify({"available": available})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


DASHBOARD_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "dist")


@app.route("/assets/<path:asset>")
def serve_dashboard_assets(asset):
    return send_from_directory(os.path.join(DASHBOARD_DIST, "assets"), asset)


# ==========================
# START
# ==========================
if __name__ == "__main__":
    try:
        login()
        sync_positions_from_broker(notify_new=True)
        bot.send_message(CHAT_ID, "🚀 MCX CRUDEOIL MINI FIB/PIVOT BOT STARTED\nCommands: /status /pause /resume /balance")
    except Exception as e:
        print("Startup failed:", e)

    threading.Thread(target=run_forever, args=(run, "Trading loop"), daemon=True).start()
    threading.Thread(target=run_forever, args=(run_telegram_polling, "Telegram polling"), daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    serve(app, host="0.0.0.0", port=port)
