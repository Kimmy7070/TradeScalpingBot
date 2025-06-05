#!/usr/bin/env python3
# scalp_bot.py
# A minimal “scalping” bot for Trading 212 (demo or live),
# now using cloudscraper to bypass Cloudflare’s JS challenge.
# ----------------------------------------------------------------------------
# Dependencies:
#   pip install cloudscraper python-dotenv
# ----------------------------------------------------------------------------

import os
import sys
import time
import logging
import argparse
import cloudscraper
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
# LOAD ENVIRONMENT (override any system‐level vars with .env)
# ----------------------------------------------------------------------------
load_dotenv(override=True)

# ----------------------------------------------------------------------------
# SANITY: show CWD & files so you can confirm .env is actually being loaded
# ----------------------------------------------------------------------------
cwd = os.getcwd()
logger.debug(f"Current working directory: {cwd!r}")
logger.debug(f"Files in CWD: {os.listdir(cwd)}")

# ----------------------------------------------------------------------------
# READ & SANITIZE T212_API_KEY
# ----------------------------------------------------------------------------
_raw_key = os.getenv("T212_API_KEY", "")
logger.debug(f"Raw T212_API_KEY from env: {repr(_raw_key)}")

_raw_key = _raw_key.strip()
if not _raw_key:
    logger.error("T212_API_KEY is not set or is empty. Please add it to your .env file.")
    sys.exit(1)

# Strip out any non-Latin-1 characters (e.g. stray “…”)
T212_API_KEY = _raw_key.encode("utf-8", "ignore").decode("latin-1", "ignore")
if T212_API_KEY != _raw_key:
    logger.warning("Your T212_API_KEY contained non-ASCII characters; they have been stripped out.")
if not T212_API_KEY:
    logger.error("After sanitization, T212_API_KEY is empty. Please double-check your .env.")
    sys.exit(1)

# ----------------------------------------------------------------------------
# READ & VALIDATE T212_ENV (“demo” or “live”)
# ----------------------------------------------------------------------------
_raw_env = os.getenv("T212_ENV", "")
logger.debug(f"Raw T212_ENV from env: {repr(_raw_env)}")

_raw_env = _raw_env.strip()
if not _raw_env:
    logger.debug("T212_ENV was empty—defaulting to 'demo'.")
    _raw_env = "demo"

T212_ENV = _raw_env.lower()
if T212_ENV not in ("demo", "live"):
    logger.error("T212_ENV must be exactly 'demo' or 'live' (case-insensitive).")
    sys.exit(1)

# Base URL depends on environment
if T212_ENV == "live":
    BASE_URL = "https://api.trading212.com"
else:
    BASE_URL = "https://demo.trading212.com"

logger.debug(f"T212_ENV (normalized) = {repr(T212_ENV)}")
logger.debug(f"BASE_URL = {repr(BASE_URL)}")

# ----------------------------------------------------------------------------
# CREATE CLOUDSCRAPER CLIENT (handles Cloudflare’s JS challenge automatically)
# ----------------------------------------------------------------------------
scraper = cloudscraper.create_scraper(
    # Tell cloudscraper to reuse typical browser headers
    browser={
        "custom": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/114.0.0.0 Safari/537.36"
    }
)

# ----------------------------------------------------------------------------
# BUILD HEADERS (merge into cloudscraper instance so all requests include them)
# ----------------------------------------------------------------------------
COMMON_HEADERS = {
    # 1) Trading 212 Bearer token (must be Latin-1/ASCII only)
    "Authorization": f"Bearer {T212_API_KEY}",

    # 2) Standard API headers
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",

    # 3) Browser/Cloudflare headers
    "Host": f"{T212_ENV}.trading212.com",
    "Origin": f"https://{T212_ENV}.trading212.com",
    "Referer": f"https://{T212_ENV}.trading212.com/",
    "Connection": "keep-alive",
}

scraper.headers.update(COMMON_HEADERS)

# ----------------------------------------------------------------------------
# FALLBACKS (pull from .env or use defaults)
# ----------------------------------------------------------------------------
DEFAULT_SYMBOL     = os.getenv("SYMBOL", "AAPL").strip().upper()
DEFAULT_SIZE       = float(os.getenv("SIZE", "1.0"))
DEFAULT_ASSET_TYPE = os.getenv("ASSET_TYPE", "EQUITY").strip().upper()

# ----------------------------------------------------------------------------
# UTILITY: Sleep with debug log
# ----------------------------------------------------------------------------
def safe_sleep(seconds: float):
    logger.debug(f"Sleeping for {seconds} second(s)…")
    time.sleep(seconds)

# ----------------------------------------------------------------------------
# INITIALIZE CLOUDFLARE SESSION (GET homepage to get cf_clearance cookie)
# ----------------------------------------------------------------------------
def init_cloudflare_session():
    homepage = f"{BASE_URL}/"
    logger.debug(f"Initializing Cloudflare session via GET {homepage}")
    try:
        r = scraper.get(homepage, timeout=15)
    except Exception as e:
        logger.error(f"Could not reach {homepage}: {e}")
        sys.exit(1)

    logger.debug(f"→ HTTP {r.status_code} on {homepage}")
    if r.status_code not in (200, 302):
        # 302 sometimes happens if Cloudflare immediately redirects to login
        logger.warning(f"Got HTTP {r.status_code} while fetching homepage; you may still be blocked.")
    # After this GET, cloudscraper automatically stores the cf_clearance cookie if the JS challenge is passed.

# ----------------------------------------------------------------------------
# SEARCH INSTRUMENT (using /rest/v2 for both demo & live)
# ----------------------------------------------------------------------------
def search_instrument(symbol: str, asset_type: str = "EQUITY") -> dict:
    """
    POST {BASE_URL}/rest/v2/instruments/search
      JSON: {"query": symbol, "assetTypes": [asset_type]}
    Using the same cloudscraper client (with cookies), so Cloudflare won’t block.
    Returns first matching instrument JSON or raises on error.
    """
    url = f"{BASE_URL}/rest/v2/instruments/search"
    payload = {"query": symbol, "assetTypes": [asset_type]}

    logger.debug(f"search_instrument: POST {url} with payload={payload}")
    try:
        r = scraper.post(url, json=payload, timeout=15)
    except Exception as e:
        raise RuntimeError(f"search_instrument(): Request failed: {e}")

    logger.debug(f"→ HTTP {r.status_code} | Response: {r.text}")
    if r.status_code != 200:
        raise RuntimeError(f"search_instrument(): HTTP {r.status_code} – {r.text}")

    data = r.json()
    hits = data.get("instruments", [])
    if not hits:
        raise RuntimeError(f"No instruments found matching '{symbol}' (assetType={asset_type}).")

    return hits[0]

# ----------------------------------------------------------------------------
# GET MARKET QUOTE
# ----------------------------------------------------------------------------
def get_market_quote(instrument_id: str) -> dict:
    """
    GET {BASE_URL}/api/v1/quotes/instrument/{instrument_id}
    Returns {"bid": float, "ask": float, "last": float, …} or raises on error.
    """
    url = f"{BASE_URL}/api/v1/quotes/instrument/{instrument_id}"
    logger.debug(f"get_market_quote: GET {url}")
    try:
        r = scraper.get(url, timeout=15)
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
    POST {BASE_URL}/api/v1/orders
      JSON: {
        "instrumentId": instrument_id,
        "orderType": "MARKET",
        "side": side,
        "quantity": size
        (optional) "currency": ...
      }
    Returns order JSON on HTTP 200/201 or raises on error.
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
        r = scraper.post(url, json=payload, timeout=15)
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
    1) Search instrument (via /rest/v2/instruments/search)
    2) Get current bid/ask/last (via /api/v1/quotes/…)
    3) If ask ≤ 0.998×last → BUY; elif bid ≥ 1.002×last → SELL; otherwise → skip.
    """
    inst = search_instrument(symbol, asset_type)
    inst_id = inst.get("instrumentId")
    name    = inst.get("symbol", symbol)
    logger.info(f"Found instrument: {name} (instrumentId={inst_id})")

    quote = get_market_quote(inst_id)
    bid   = quote["bid"]
    ask   = quote["ask"]
    last  = quote["last"]
    logger.info(f"Market quote for {symbol}: bid={bid:.4f}, ask={ask:.4f}, last={last:.4f}")

    target_buy  = last * 0.998
    target_sell = last * 1.002
    logger.debug(f"Target buy @ {target_buy:.4f}, Target sell @ {target_sell:.4f}")

    if ask <= target_buy:
        logger.info(f"→ PLACING BUY MARKET ORDER @ size={size}")
        result = place_market_order(inst_id, side="BUY", size=size)
        logger.info(f"BUY RESULT: {result}")
    elif bid >= target_sell:
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
        help="Seconds between scalp cycles (default: 5s)"
    )
    args = parser.parse_args()

    symbol   = args.symbol.upper().strip()
    size     = args.size
    atype    = args.asset_type.upper().strip()
    interval = args.interval

    logger.info(f"Starting scalp_bot (Mode={atype}), symbol={symbol}, size={size}, interval={interval}s")

    # 1) First, “wake up” Cloudflare by visiting the homepage:
    init_cloudflare_session()

    # 2) Then enter the scalping loop:
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
