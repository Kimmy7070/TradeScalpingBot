#!/usr/bin/env python
# File: scalp_bot.py

import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ──────────  1. LOAD ENV & CONFIGURATION  ──────────

load_dotenv()  # loads T212_API_KEY and T212_ENV from .env

API_KEY = os.getenv("T212_API_KEY", "").strip()
ENV     = os.getenv("T212_ENV", "demo").strip().lower()

if not API_KEY:
    raise RuntimeError("Missing T212_API_KEY in environment/.env")

# Choose base URL based on ENV
if ENV == "live":
    BASE_URL = "https://api.trading212.com"
else:
    BASE_URL = "https://demo.trading212.com"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

# ──────── CONFIGURATION ────────
#  Instrument & quantity
SYMBOL        = "RHM"       # Ticker you want to scalp
LOT_QTY       = 0.77        # Fractional shares per cycle

#  ATR & volatility
ATR_PERIOD    = 14          # ATR length (in 1 min bars)
MIN_ATR_EUR   = 2.00        # Skip trading if ATR < €1.00 (too quiet)

#  Entry / exit thresholds (multiples of ATR)
SELL_ATR_K    = 1.0         # If red candle’s drop ≥ 1.0×ATR → SELL
STOP_ATR_K    = 1.0         # After buy, STOP = entry_price – 1.0×ATR
TRAIL_ATR_K   = 1.0         # Trailing stop: mid – 1.0×ATR if > old_stop

#  Mode flags
MODE_BASIC    = True        # “Basic Mode 1” = sell-red-candle & buy-back same qty
MODE_COMPOUND = False       # “Mode 2” = deploy ALL cash at market on dip
# (Set MODE_COMPOUND=True to use Mode 2; if both True, Mode 2 takes precedence)

#  Poll intervals
POLL_BAR_SEC    = 60        # Check for new 1 min bar every 60 s
POLL_PRICE_SEC  = 5         # Check prices every 5 s during buy/stop loops

#  Risk management
DAILY_LOSS_LIMIT = 20.0     # If you lose ≥ €20 in one day, halt until midnight

#  Edge-case thresholds
MAX_SPREAD_EUR   = 0.20     # Skip bar if ask-bid > €0.20 (low liquidity)
GAP_FAILSAFE_MULT = 0.995   # If mid ≤ 0.995×stop → failsafe
MIN_PRICE_MOVE   = 0.005    # Only move trailing if at least €0.005
# ───── END CONFIGURATION ─────

def _url(path: str) -> str:
    """Build full endpoint URL."""
    return f"{BASE_URL}{path}"

def search_instrument(symbol: str, asset_type: str="EQUITY") -> str:
    """Return instrumentId for symbol."""
    r = requests.post(
        _url("/equity/instrument/search"),
        headers=HEADERS,
        json={"symbol": symbol, "assetType": asset_type}
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        raise RuntimeError(f"No instrument found for '{symbol}'")
    # Prefer exact symbol match
    for inst in results:
        if inst.get("symbol") == symbol and inst.get("assetType") == asset_type:
            return inst["id"]
    return results[0]["id"]

def get_historical_bars(inst_id: str, minutes: int) -> pd.DataFrame:
    """
    Fetch last `minutes` of 1-min bars. Returns DataFrame with [timestamp, open, high, low, close].
    """
    now_ms   = int(time.time() * 1000)
    start_ms = int((time.time() - 60*minutes) * 1000)
    r = requests.get(
        _url("/equity/historicalBars"),
        headers=HEADERS,
        params={
            "instrumentId": inst_id,
            "resolution": "1m",
            "from": start_ms,
            "to": now_ms
        }
    )
    r.raise_for_status()
    bars = r.json()  # list of dicts
    df = pd.DataFrame(bars)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df[["timestamp","open","high","low","close"]]

def compute_atr(df: pd.DataFrame, period: int) -> float:
    """
    Compute ATR(period) on df (oldest→newest). Returns latest ATR as float.
    """
    df2 = df.copy().reset_index(drop=True)
    df2["prev_close"] = df2["close"].shift(1)
    df2["tr1"] = df2["high"] - df2["low"]
    df2["tr2"] = (df2["high"] - df2["prev_close"]).abs()
    df2["tr3"] = (df2["low"]  - df2["prev_close"]).abs()
    df2["TR"]  = df2[["tr1","tr2","tr3"]].max(axis=1)
    atr = df2["TR"].rolling(window=period, min_periods=period).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else 0.0

def get_last_price(inst_id: str) -> dict:
    """Return {'bid': float, 'ask': float} for inst_id."""
    r = requests.post(
        _url("/equity/price"),
        headers=HEADERS,
        json={"instrumentId": inst_id}
    )
    r.raise_for_status()
    resp = r.json()
    return {"bid": float(resp["bid"]), "ask": float(resp["ask"]) }

def place_market_order(inst_id: str, qty: float) -> str:
    """Place MARKET BUY (or SELL) of qty shares. Returns orderId."""
    r = requests.post(
        _url("/equity/order"),
        headers=HEADERS,
        json={"instrumentId": inst_id, "quantity": qty, "orderType": "MARKET"}
    )
    r.raise_for_status()
    return r.json()["orderId"]

def place_limit_order(inst_id: str, qty: float, price: float) -> str:
    """Place LIMIT order (buy or sell) @ price. Returns orderId."""
    r = requests.post(
        _url("/equity/order"),
        headers=HEADERS,
        json={
            "instrumentId": inst_id,
            "quantity": qty,
            "orderType": "LIMIT",
            "limitPrice": round(price, 2)
        }
    )
    r.raise_for_status()
    return r.json()["orderId"]

def place_stop_order(inst_id: str, qty: float, stop_price: float) -> str:
    """Place STOP (sell) @ stop_price. Returns orderId."""
    r = requests.post(
        _url("/equity/order"),
        headers=HEADERS,
        json={
            "instrumentId": inst_id,
            "quantity": qty,
            "orderType": "STOP",
            "stopPrice": round(stop_price, 2)
        }
    )
    r.raise_for_status()
    return r.json()["orderId"]

def get_order_status(order_id: str) -> dict:
    """Fetch status of order_id. Returns JSON with fields 'status','avgPrice',etc."""
    r = requests.get(_url(f"/equity/order/{order_id}"), headers=HEADERS)
    r.raise_for_status()
    return r.json()

def cancel_order(order_id: str):
    """Cancel an existing LIMIT or STOP order."""
    r = requests.post(_url(f"/equity/order/{order_id}/cancel"), headers=HEADERS)
    r.raise_for_status()

def get_open_position(inst_id: str) -> dict:
    """
    Return your open position for inst_id, e.g.
    {'instrumentId':..., 'quantity':0.77, 'avgEntryPrice':123.45, ...}, or {} if none.
    """
    r = requests.get(_url("/equity/positions"), headers=HEADERS)
    r.raise_for_status()
    for pos in r.json():
        if pos["instrumentId"] == inst_id:
            return pos
    return {}

def get_cash_balance() -> float:
    """Return available EUR balance as float."""
    r = requests.get(_url("/equity/accounts"), headers=HEADERS)
    r.raise_for_status()
    for acct in r.json().get("cash", []):
        if acct["currency"] == "EUR":
            return float(acct["available"])
    return 0.0

def request_withdrawal(amount: float):
    """Withdraw amount EUR back to your bank."""
    r = requests.post(
        _url("/withdrawal"),
        headers=HEADERS,
        json={"amount": round(amount, 2), "currency": "EUR"}
    )
    r.raise_for_status()

# ────────  MAIN BOT LOGIC  ────────

def main():
    print(f"[{datetime.now()}] Starting scalp_bot (Mode={'COMPOUND' if MODE_COMPOUND else 'BASIC'})")
    inst_id = search_instrument(SYMBOL, asset_type="EQUITY")
    print(f"  → {SYMBOL} instrumentId = {inst_id}")

    current_day = datetime.now().date()
    daily_pnl = 0.0
    last_bar_time = None

    while True:
        now = datetime.now()

        # 1) Reset daily P&L at midnight
        if now.date() != current_day:
            current_day = now.date()
            daily_pnl = 0.0
            print(f"[{now}] → New day. Reset daily P&L.")

        # 2) Check daily loss limit
        if daily_pnl <= -DAILY_LOSS_LIMIT:
            secs_to_mid = ((datetime(now.year, now.month, now.day) + timedelta(days=1)) - now).seconds + 5
            print(f"[{now}] ⚠️ Daily P&L (€{daily_pnl:.2f}) ≤ −{DAILY_LOSS_LIMIT:.2f}. Sleeping {secs_to_mid}s.")
            time.sleep(secs_to_mid)
            continue

        # 3) Fetch recent bars (ATR_PERIOD+2)
        try:
            df = get_historical_bars(inst_id, minutes=ATR_PERIOD+2)
        except Exception as e:
            print(f"[{now}] ❌ Error fetching bars: {e}. Sleeping {POLL_BAR_SEC}s.")
            time.sleep(POLL_BAR_SEC)
            continue

        if df.empty or len(df) < ATR_PERIOD + 1:
            print(f"[{now}] Not enough bars ({len(df)}); waiting {POLL_BAR_SEC}s.")
            time.sleep(POLL_BAR_SEC)
            continue

        latest_bar = df.iloc[-1]
        bar_time = latest_bar["timestamp"]
        if last_bar_time is not None and bar_time <= last_bar_time:
            # No new closed bar yet
            time.sleep(POLL_BAR_SEC)
            continue

        # New 1-min bar detected
        last_bar_time = bar_time
        print(f"\n[{bar_time}] Bar closed: O={latest_bar['open']} H={latest_bar['high']} L={latest_bar['low']} C={latest_bar['close']}")

        # 4) Compute ATR on prior ATR_PERIOD bars
        atr_df = df.iloc[-(ATR_PERIOD+1):-1]
        atr = compute_atr(atr_df, period=ATR_PERIOD)
        print(f"  → ATR({ATR_PERIOD}) = €{atr:.2f}")

        if atr < MIN_ATR_EUR:
            print(f"  ⚠️ ATR (€{atr:.2f}) < MIN_ATR (€{MIN_ATR_EUR:.2f}); skipping bar.")
            time.sleep(POLL_BAR_SEC)
            continue

        pos = get_open_position(inst_id)
        qty = pos.get("quantity", 0.0)

        # 5) Check liquidity: skip if spread too wide
        px = get_last_price(inst_id)
        spread = px["ask"] - px["bid"]
        if spread > MAX_SPREAD_EUR:
            print(f"  ⚠️ Spread (€{spread:.2f}) > MAX_SPREAD (€{MAX_SPREAD_EUR:.2f}); skipping bar.")
            time.sleep(POLL_BAR_SEC)
            continue

        # ── MODE_COMPOUND HANDLER ──
        if MODE_COMPOUND and not pos:
            cash = get_cash_balance()
            est_qty = cash / px["ask"] if px["ask"]>0 else 0
            if est_qty * px["ask"] >= MIN_ATR_EUR:
                print(f"  ↪ MODE_COMPOUND: Market-BUY ~{est_qty:.4f} shares (all cash).")
                buy_id = place_market_order(inst_id, est_qty)
                entry_price = None
                while True:
                    status = get_order_status(buy_id)
                    st = status["status"]
                    if st == "FILLED":
                        entry_price = status.get("avgPrice")
                        print(f"    ✓ Compound BUY filled @ €{entry_price:.2f}")
                        break
                    elif st in ("CANCELLED","REJECTED","EXPIRED"):
                        print(f"    ⚠️ Compound BUY {st}; aborting.")
                        entry_price = None
                        break
                    time.sleep(POLL_PRICE_SEC)
                if entry_price is not None:
                    initial_stop = entry_price - STOP_ATR_K * atr
                    stop_id = place_stop_order(inst_id, est_qty, initial_stop)
                    print(f"    🛡️ COMPOUND STOP @ €{initial_stop:.2f}")
                    pos2 = {"quantity": est_qty, "entryPrice": entry_price, "stopOrderId": stop_id, "stopPrice": initial_stop}
                    handle_trailing_and_failsafe(inst_id, pos2, atr, daily_pnl)
                continue
            else:
                print(f"  ⚠️ MODE_COMPOUND: Not enough cash (€{cash:.2f}) to buy ≥ €{MIN_ATR_EUR:.2f} ATR.")

        # ── MODE_BASIC HANDLER ──
        if MODE_BASIC and not pos:
            open_p  = latest_bar["open"]
            close_p = latest_bar["close"]
            drop = open_p - close_p
            if close_p < open_p and drop >= SELL_ATR_K * atr:
                print(f"  🔻 Red candle drop (€{drop:.2f}) ≥ {SELL_ATR_K}×ATR → attempt SELL")
                # If we had qty, market sell; but since pos=={}, skip actual sell on first run
                if qty > 0:
                    sell_id = place_market_order(inst_id, qty)
                    # Wait for fill
                    sale_price = None
                    while True:
                        status = get_order_status(sell_id)
                        st = status["status"]
                        if st == "FILLED":
                            sale_price = status.get("avgPrice")
                            print(f"    ✓ Sold {qty} @ €{sale_price:.2f}")
                            break
                        elif st in ("CANCELLED","REJECTED","EXPIRED"):
                            print(f"    ⚠️ Sell {st}; aborting cycle.")
                            sale_price = None
                            break
                        time.sleep(POLL_PRICE_SEC)
                    if sale_price is not None:
                        handle_buyback(inst_id, qty, sale_price, atr, daily_pnl)
                else:
                    print("    ⚠️ No position to sell at first run.")
            else:
                print("  🔹 No BASIC entry condition met.")

        # ── TRAILING-STOP ON EXISTING POSITION ──
        if pos:
            entry_price = pos["avgEntryPrice"]
            stop_price  = pos.get("stopPrice", None)
            if stop_price is None:
                stop_price = entry_price - STOP_ATR_K * atr
                stop_id = place_stop_order(inst_id, qty, stop_price)
                pos["stopOrderId"] = stop_id
                pos["stopPrice"]   = stop_price
                print(f"  🛡️ Initial STOP @ €{stop_price:.2f}")
            handle_trailing_and_failsafe(inst_id, pos, atr, daily_pnl)

        time.sleep(POLL_BAR_SEC)


def handle_buyback(inst_id: str, quantity: float, sale_price: float, atr: float, daily_pnl_ref: float):
    """
    After a market SELL @ sale_price, place a LIMIT BUY-BACK @ sale_price
    to exit mild dips. Once buy fills, place ATR-based stop & trailing.
    """
    print(f"  → Placing LIMIT buy-back: {quantity} @ €{sale_price:.2f}")
    buy_id = place_limit_order(inst_id, quantity, sale_price)

    entry_price = None
    while True:
        status = get_order_status(buy_id)
        st = status["status"]
        if st == "FILLED":
            entry_price = status.get("avgPrice")
            print(f"    ✓ Buy-back filled @ €{entry_price:.2f}")
            break
        elif st in ("CANCELLED","REJECTED","EXPIRED"):
            print(f"    ⚠️ Buy-back {st}; giving up.")
            return
        time.sleep(POLL_PRICE_SEC)

    initial_stop = entry_price - STOP_ATR_K * atr
    stop_id = place_stop_order(inst_id, quantity, initial_stop)
    print(f"    🛡️ Placed STOP-LOSS @ €{initial_stop:.2f}")
    pos2 = {"quantity": quantity, "entryPrice": entry_price, "stopOrderId": stop_id, "stopPrice": initial_stop}
    handle_trailing_and_failsafe(inst_id, pos2, atr, daily_pnl_ref)


def handle_trailing_and_failsafe(inst_id: str, pos: dict, atr: float, daily_pnl_ref: float):
    """
    Maintain a trailing stop and failsafe. Exits when stop fills or failsafe triggers.
    Updates daily_pnl_ref upon exit.
    """
    entry_price = pos["entryPrice"]
    quantity    = pos["quantity"]
    stop_price  = pos["stopPrice"]
    stop_id     = pos["stopOrderId"]

    while True:
        px = get_last_price(inst_id)
        mid = (px["bid"] + px["ask"]) / 2

        # 1) Trailing stop: move up if (mid - TRAIL_ATR_K*ATR) > old_stop + MIN_PRICE_MOVE
        new_stop = mid - TRAIL_ATR_K * atr
        if new_stop > stop_price + MIN_PRICE_MOVE:
            print(f"      ↗️ Moving stop: {stop_price:.2f} → {new_stop:.2f}")
            try:
                cancel_order(stop_id)
            except:
                pass
            stop_price = new_stop
            stop_id = place_stop_order(inst_id, quantity, stop_price)
            pos["stopOrderId"] = stop_id
            pos["stopPrice"]   = stop_price

        # 2) Failsafe: if mid ≤ GAP_FAILSAFE_MULT*stop_price
        if mid <= GAP_FAILSAFE_MULT * stop_price:
            print(f"      🚨 Failsafe: mid (€{mid:.2f}) ≤ {GAP_FAILSAFE_MULT}×stop (€{stop_price:.2f}); market-sell")
            try:
                cancel_order(stop_id)
            except:
                pass
            sell_id = place_market_order(inst_id, quantity)
            while True:
                status2 = get_order_status(sell_id)
                st2 = status2["status"]
                if st2 == "FILLED":
                    exit_price = status2.get("avgPrice")
                    pnl = round((exit_price - entry_price) * quantity, 2)
                    daily_pnl_ref += pnl
                    print(f"        ✖️ Forced sell @ €{exit_price:.2f} → P/L=€{pnl:.2f} | Daily P&L=€{daily_pnl_ref:.2f}")
                    return
                time.sleep(POLL_PRICE_SEC)

        # 3) If STOP triggers normally
        status = get_order_status(stop_id)
        if status["status"] == "FILLED":
            exit_price = status.get("avgPrice")
            pnl = round((exit_price - entry_price) * quantity, 2)
            daily_pnl_ref += pnl
            print(f"      🔴 STOP hit @ €{exit_price:.2f} → P/L=€{pnl:.2f} | Daily P&L=€{daily_pnl_ref:.2f}")
            return

        time.sleep(POLL_PRICE_SEC)

if __name__ == "__main__":
    main()
