#!/usr/bin/env python3
# scalp_bot.py
# A minimal “scalping” bot for Trading 212 (demo or live).
# ----------------------------------------------------------------------------
# Requirements: pip install python‐dotenv requests
# ----------------------------------------------------------------------------

import os
import sys
import time
import logging
import argparse
import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# SETUP: Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="▶▶▶ %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES (AND SANITIZE API KEY)
# ----------------------------------------------------------------------------
# 1. Attempt to load a .env file from the current working directory.
load_dotenv()  

# 2. Read T212_API_KEY, strip whitespace, then force‐sanitize to Latin-1/ASCII.
_raw_key = os.getenv("T212_API_KEY", "").strip()
if not _raw_key:
    logger.error("T212_API_KEY is not set. Please add it to your .env file.")
    sys.exit(1)

# Remove any non-Latin-1 characters (e.g. “…”). Trading 212 HTTP headers must be Latin-1.
T212_API_KEY = _raw_key.encode("utf-8", "ignore").decode("latin-1", "ignore")
if T212_API_KEY != _raw_key:
    logger.warning("Your T212_API_KEY contained non-ASCII characters; they have been stripped out.")
if not T212_API_KEY:
    logger.error("After sanitization, T212_API_KEY is empty. Please double-check your .env.")
    sys.exit(1)

# 3. Read T212_ENV, and print exactly what was read (for debugging).
_raw_env = os.getenv("T212_ENV", "").strip()
logger.debug(f"T212_ENV (raw from environment) = {repr(_raw_env)}")

# If nothing was in the environment, default to "demo"
if not _raw_env:
    logger.debug("T212_ENV was empty; defaulting to 'demo'.")
    _raw_env = "demo"

T212_ENV = _raw_env.lower()
if T212_ENV not in ("demo", "live"):
    logger.error("T212_ENV must be either 'demo' or 'live' (case-insensitive).")
    sys.exit(1)

# Determine the correct BASE_URL
if T212_ENV == "live":
    BASE_URL = "https://api.trading212.com"
else:
    BASE_URL = "https://demo.trading212.com"

logger.debug(f"T212_ENV (normalized) = '{T212_ENV}'")
logger.debug(f"BASE_URL = '{BASE_URL}'")

# 4. Build a HEADERS dict that only contains Latin-1/ASCII
HEADERS = {
    "Authorization": f"Bearer {T212_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ----------------------------------------------------------------------------
# DEFAULTS (can also be overridden via .env)
# ----------------------------------------------------------------------------
DEFAULT_SYMBOL      = os.getenv("SYMBOL", "AAPL").strip().upper()
DEFAULT_SIZE        = float(os.getenv("SIZE", "1.0"))
DEFAULT_ASSET_TYPE  = os.getenv("ASSET_TYPE", "EQUITY").strip().upper()

# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------
def safe_sleep(seconds: float):
    """Sleep for the given number of seconds, with a debug log."""
    logger.debug(f"Sleeping for {seconds} second(s)…")
    time.sleep(seconds)

# ----------------------------------------------------------------------------
# SEARCH INSTRUMENT
# ----------------------------------------------------------------------------
def search_instrument(symbol: str, asset_type: str = "EQUITY") -> dict:
    """
    POST /api/v1/instruments/search
      payload: {"query": symbol, "assetTypes": [asset_type]}
    Returns the first matching instrument JSON.
    Raises RuntimeError if HTTP ≠200 or no hits.
    """
    url = f"{BASE_URL}/api/v1/instruments/search"
    payload = {"query": symbol, "assetTypes": [asset_type]}

    logger.debug(f"search_instrument: POST {url} with payload={payload}")
    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=10)
    except Exception as e:
        raise RuntimeError(f"search_instrument(): Request failed: {e}")

    logger.debug(f"→ HTTP {r.status_code} | Response: {r.text}")
    if r.status_code != 200:
        raise RuntimeError(f"search_instrument(): HTTP {r.status_code} – {r.text}")

    data = r.json()
    hits = data.get("instruments", [])
    if not hits:
        raise RuntimeError(f"No instruments found matching '{symbol}' (type={asset_type}).")

    return hits[0]

# ----------------------------------------------------------------------------
# GET MARKET QUOTE
# ----------------------------------------------------------------------------
def get_market_quote(instrument_id: str) -> dict:
    """
    GET /api/v1/quotes/instrument/{instrumentId}
    Returns a dict with at least {"bid": float, "ask": float, "last": float, "timestamp": …}.
    """
    url = f"{BASE_URL}/api/v1/quotes/instrument/{instrument_id}"
    logger.debug(f"get_market_quote: GET {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
    except Exception as e:
        raise RuntimeError(f"get_market_quote(): Request failed: {e}")

    logger.debug(f"→ HTTP {r.status_code} | Response: {r.text}")
    if r.status_code != 200:
        raise RuntimeError(f"get_market_quote(): HTTP {r.status_code} – {r.text}")

    d = r.json()
    return {
        "bid": float(d.get("bid", 0)),
        "ask": float(d.get("ask", 0)),
        "last": float(d.get("last", 0)),
        "timestamp": d.get("timestamp")
    }

# ----------------------------------------------------------------------------
# PLACE MARKET ORDER
# ----------------------------------------------------------------------------
def place_market_order(instrument_id: str, side: str, size: float, currency: str = None) -> dict:
    """
    POST /api/v1/orders
      payload: {
        "instrumentId": str,
        "orderType": "MARKET",
        "side": "BUY" or "SELL",
        "quantity": float,
        (optional) "currency": "USD"
      }
    Returns the JSON response on success (HTTP 200 or 201). Raises otherwise.
    """
    url = f"{BASE_URL}/api/v1/orders"
    payload = {
        "instrumentId": instrument_id,
        "orderType": "MARKET",
        "side": side,
        "quantity": size
    }
    if currency:
        payload["currency"] = currency

    logger.debug(f"place_market_order: POST {url} with payload={payload}")
    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=10)
    except Exception as e:
        raise RuntimeError(f"place_market_order(): Request failed: {e}")

    logger.debug(f"→ HTTP {r.status_code} | Response: {r.text}")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"place_market_order(): HTTP {r.status_code} – {r.text}")

    return r.json()

# ----------------------------------------------------------------------------
# MAIN SCALP CYCLE
# ----------------------------------------------------------------------------
def scalp_cycle(symbol: str, size: float, asset_type: str):
    """
    1) Search instrument by symbol
    2) Get market quote
    3) If “ask” ≤ target_buy_price → place BUY MARKET order
       If “bid” ≥ target_sell_price → place SELL MARKET order
       Else → no action.
    """
    # 1) Search
    inst = search_instrument(symbol, asset_type)
    inst_id = inst.get("instrumentId")
    name    = inst.get("symbol", symbol)
    logger.info(f"Found instrument: {name} (instrumentId={inst_id})")

    # 2) Quote
    quote = get_market_quote(inst_id)
    bid   = quote["bid"]
    ask   = quote["ask"]
    last  = quote["last"]
    logger.info(f"Market quote for {symbol}: bid={bid:.4f}, ask={ask:.4f}, last={last:.4f}")

    # 3) Simple threshold logic (customize as needed)
    target_buy_price  = last * 0.998  # 0.2% below last
    target_sell_price = last * 1.002  # 0.2% above last
    logger.debug(f"Target buy @ {target_buy_price:.4f}, Target sell @ {target_sell_price:.4f}")

    if ask <= target_buy_price:
        logger.info(f"→ PLACING BUY MARKET ORDER @ size={size}")
        result = place_market_order(inst_id, side="BUY", size=size)
        logger.info(f"BUY RESULT: {result}")
    elif bid >= target_sell_price:
        logger.info(f"→ PLACING SELL MARKET ORDER @ size={size}")
        result = place_market_order(inst_id, side="SELL", size=size)
        logger.info(f"SELL RESULT: {result}")
    else:
        logger.info("No scalp opportunity right now. Skipping order.")

# ----------------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Simple Trading 212 Scalping Bot")
    parser.add_argument(
        "--symbol", "-s",
        default=DEFAULT_SYMBOL,
        help=f"Ticker symbol to scalp (default: {DEFAULT_SYMBOL})"
    )
    parser.add_argument(
        "--size", "-z",
        type=float,
        default=DEFAULT_SIZE,
        help=f"Size/quantity per order (default: {DEFAULT_SIZE})"
    )
    parser.add_argument(
        "--asset-type", "-t",
        default=DEFAULT_ASSET_TYPE,
        help=f"Asset type to search (default: {DEFAULT_ASSET_TYPE})"
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=5.0,
        help="Time in seconds to wait between scalp cycles (default: 5s)"
    )
    args = parser.parse_args()

    symbol   = args.symbol.upper().strip()
    size     = args.size
    atype    = args.asset_type.upper().strip()
    interval = args.interval

    logger.info(f"Starting scalp_bot (Mode={atype}), symbol={symbol}, size={size}, interval={interval}s")
    try:
        while True:
            scalp_cycle(symbol, size, atype)
            safe_sleep(interval)
    except KeyboardInterrupt:
        logger.info("Interrupted by user; shutting down.")
    except Exception as e:
        logger.exception(f"Fatal error in main loop: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
