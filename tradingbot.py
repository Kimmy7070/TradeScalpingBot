#time wated: 3 days
#!/usr/bin/env python3
# scalp_bot.py
# A minimal Trading 212 scalping bot (demo or live) that uses Selenium to bypass Cloudflare.
# ----------------------------------------------------------------------------
# Dependencies (install once):
#   pip install selenium webdriver-manager python-dotenv requests
# ----------------------------------------------------------------------------

import os
import sys
import time
import json
import types
import logging
import argparse
import requests
from packaging.version import Version as _PackagingVersion
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv

# ─── Import distutils.version.LooseVersion for Python 3.12+ ──────────────────

class LooseVersionShim:
    """
    A thin wrapper around packaging.version.Version that exposes both:
      - .version (tuple of release numbers) 
      - .vstring (string form) 
    so that undetected_chromedriver (which expects distutils.version.LooseVersion)
    can use .version and .vstring without errors.
    """
    def __init__(self, v):
        # If v is already a PackagingVersion, use it; otherwise parse from string
        if isinstance(v, _PackagingVersion):
            self._v = v
        else:
            self._v = _PackagingVersion(v)
        # .version: a tuple of ints (or strings) representing each release segment
        self.version = tuple(self._v.release)
        # .vstring: the original version string (e.g. "114.0.5735.110")
        self.vstring = str(self._v)

    def __str__(self):
        return self.vstring

    def __repr__(self):
        return f"LooseVersionShim('{self.vstring}')"

# ─── Monkey‐patch distutils.version.LooseVersion for Python 3.12+ ─────────────

distutils_version = types.SimpleNamespace(LooseVersion=LooseVersionShim)
sys.modules["distutils"] = types.SimpleNamespace(version=distutils_version)
sys.modules["distutils.version"] = distutils_version

# ─── Now it’s safe to import undetected_chromedriver ───────────────────────────

import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options

# ----------------------------------------------------------------------------
# SETUP: Logging
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="▶▶▶ %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# LOAD ENVIRONMENT (override any system vars with .env)
# ----------------------------------------------------------------------------
load_dotenv(override=True)

# ----------------------------------------------------------------------------
# SANITY: print CWD & files to confirm .env is in the same folder
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

# Strip out any non-Latin-1 characters (e.g. “…”)
T212_API_KEY = _raw_key.encode("utf-8", "ignore").decode("latin-1", "ignore")
if T212_API_KEY != _raw_key:
    logger.warning("Your T212_API_KEY contained non-ASCII/strange characters; they have been stripped out.")
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
# STEP 1: Use Selenium to “solve” Cloudflare and grab cookies
# ----------------------------------------------------------------------------

BASE_URL = "https://demo.trading212.com"
T212_API_KEY = os.getenv("T212_API_KEY")
T212_ENV = os.getenv("T212_ENV", "demo").lower()


def fetch_cloudflare_cookies():
    logging.debug("Entering fetch_cloudflare_cookies()")
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={os.getcwd()}/chrome_profile")
    logging.debug(f"Chrome profile path: {os.getcwd()}/chrome_profile")

    logging.debug("Starting undetected chromedriver... this may take a moment")
    driver = uc.Chrome(options=options)

    try:
        logging.debug(f"Navigating to {BASE_URL}")
        driver.get(BASE_URL)
        logging.info("Make sure you log in to Trading212 in the Chrome window now...")
        input("▶▶▶ Press ENTER once you’re logged in and see your Practice portfolio…")

        logging.debug("Fetching cookies from Selenium session...")
        cf_cookies = driver.get_cookies()
        logging.info(f"Retrieved {len(cf_cookies)} cookies from Chrome.")
        return cf_cookies

    finally:
        try:
            driver.quit()
            logging.debug("Chrome driver quit successfully.")
        except Exception as e:
            logging.error(f"Error quitting Chrome driver: {e}")


def build_api_session(cf_cookies):
    logging.debug("Entering build_api_session()")
    session = requests.Session()

    # Import cookies
    for c in cf_cookies:
        logging.debug(f"Adding cookie: {c['name']}={c['value']} (domain={c['domain']}, path={c['path']}, secure={c.get('secure', False)})")
        rest_dict = {"SameSite": c.get("sameSite", None) or None, "HttpOnly": c.get("httpOnly", False)}
        try:
            session.cookies.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", None),
                path=c.get("path", "/"),
                secure=c.get("secure", False),
                rest=rest_dict
            )
        except Exception as e:
            logging.error(f"Failed to set cookie {c['name']}: {e}")

    # Prepare headers
    logging.debug("Clearing existing headers and setting Chrome-like headers for initial GET...")
    session.headers.clear()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        'Sec-CH-UA': '"Chromium";v="115", "Google Chrome";v="115", ";Not A Brand";v="99"',
        'Sec-CH-UA-Mobile': '?0',
        'Sec-CH-UA-Platform': '"Windows"',
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Upgrade-Insecure-Requests": "1",
    })

    logging.debug(f"Headers for initial GET: {session.headers}")

    logging.debug(f"Performing initial GET to {BASE_URL}")
    resp = session.get(BASE_URL, allow_redirects=True)
    logging.debug(f"Initial GET response status: {resp.status_code}")
    logging.debug(f"Initial GET response headers: {resp.headers}")
    # Print a snippet of the body, if not 200
    if resp.status_code != 200:
        snippet = resp.content[:200] if resp.content else b"<no body>"
        logging.error(f"Initial GET body snippet: {snippet}")
        raise RuntimeError("Failed to clear Cloudflare challenge on initial GET.")

    logging.info("Cloudflare challenge cleared—status 200.")
    return session


def main():
    logging.debug("Entering main()")
    try:
        cf_cookies = fetch_cloudflare_cookies()
        logging.debug(f"Cloudflare cookies fetched: {len(cf_cookies)}")
    except Exception as e:
        logging.error(f"Error in fetch_cloudflare_cookies(): {e}")
        sys.exit(1)

    try:
        session = build_api_session(cf_cookies)
        logging.debug("Requests session built successfully.")
    except Exception as e:
        logging.error(f"Error in build_api_session(): {e}")
        sys.exit(1)

    logging.info("Ready to make authenticated API requests!")
    # ... continue with trading logic, e.g., session.get(...) etc.


if __name__ == "__main__":
    main()


# ----------------------------------------------------------------------------
# STEP 3: Now write the helper functions that use `session` for the Trading 212 REST calls
# ----------------------------------------------------------------------------
def search_instrument(session: requests.Session, symbol: str, asset_type: str = "EQUITY") -> dict:
    """
    POST {BASE_URL}/rest/v2/instruments/search
      JSON = {"query": symbol, "assetTypes": [asset_type]}
    Returns the first matching instrument JSON (raises if non-200).
    """
    url = f"{BASE_URL}/rest/v2/instruments/search"
    payload = {"query": symbol, "assetTypes": [asset_type]}

    logger.debug(f"search_instrument: POST {url} with payload={payload}")
    try:
        r = session.post(url, json=payload, timeout=15)
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

def get_market_quote(session: requests.Session, instrument_id: str) -> dict:
    """
    GET {BASE_URL}/api/v1/quotes/instrument/{instrument_id}
    Returns {"bid": float, "ask": float, "last": float, …} (raises if non-200).
    """
    url = f"{BASE_URL}/api/v1/quotes/instrument/{instrument_id}"
    logger.debug(f"get_market_quote: GET {url}")
    try:
        r = session.get(url, timeout=15)
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

def place_market_order(session: requests.Session, instrument_id: str, side: str, size: float, currency: str = None) -> dict:
    """
    POST {BASE_URL}/api/v1/orders
      JSON = {
        "instrumentId": instrument_id,
        "orderType": "MARKET",
        "side": side,
        "quantity": size,
        (optional) "currency": currency
      }
    Returns order JSON on HTTP 200/201 (raises otherwise).
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
        r = session.post(url, json=payload, timeout=15)
    except Exception as e:
        raise RuntimeError(f"place_market_order(): Request failed: {e}")

    logger.debug(f"→ HTTP {r.status_code} | Response: {r.text}")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"place_market_order(): HTTP {r.status_code} – {r.text}")

    return r.json()

# ----------------------------------------------------------------------------
# STEP 4: The main “scalp_cycle” logic (unchanged thresholds, etc.)
# ----------------------------------------------------------------------------
def scalp_cycle(session: requests.Session, symbol: str, size: float, asset_type: str):
    """
    1) search_instrument → get instrumentId
    2) get_market_quote → get bid/ask/last
    3) if ask ≤ 0.998×last → buy; elif bid ≥ 1.002×last → sell; else do nothing.
    """
    inst = search_instrument(session, symbol, asset_type)
    inst_id = inst.get("instrumentId")
    name    = inst.get("symbol", symbol)
    logger.info(f"Found instrument: {name} (instrumentId={inst_id})")

    quote = get_market_quote(session, inst_id)
    bid   = quote["bid"]
    ask   = quote["ask"]
    last  = quote["last"]
    logger.info(f"Market quote for {symbol}: bid={bid:.4f}, ask={ask:.4f}, last={last:.4f}")

    target_buy  = last * 0.998
    target_sell = last * 1.002
    logger.debug(f"Target buy @ {target_buy:.4f}, Target sell @ {target_sell:.4f}")

    if ask <= target_buy:
        logger.info(f"→ PLACING BUY MARKET ORDER @ size={size}")
        result = place_market_order(session, inst_id, side="BUY", size=size)
        logger.info(f"BUY RESULT: {result}")
    elif bid >= target_sell:
        logger.info(f"→ PLACING SELL MARKET ORDER @ size={size}")
        result = place_market_order(session, inst_id, side="SELL", size=size)
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

    # ─── Use Selenium+Chrome to solve Cloudflare’s JS challenge and grab cookies ───
    cf_cookies = fetch_cloudflare_cookies()

    # ─── Build a normal requests.Session() with those cookies + browser headers ───
    session = build_api_session(cf_cookies)

    # ─── Enter the scalp loop ──────────────────────────────────────────────────
    try:
        while True:
            scalp_cycle(session, symbol, size, atype)
            safe_sleep(interval)
    except KeyboardInterrupt:
        logger.info("Interrupted by user; shutting down.")
    except Exception as e:
        logger.exception(f"Fatal error in main loop: {e}")
        sys.exit(1)