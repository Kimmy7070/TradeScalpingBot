import os
import sys
import time
import logging
import undetected_chromedriver as uc
import requests

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# -----------------------------------------------------------------------------
# CONFIGURATION AND LOGGING
# -----------------------------------------------------------------------------

# Set up logger to print debug statements to console
logging.basicConfig(
    level=logging.DEBUG,
    format="▶▶▶ %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Base URL for Trading 212 (demo or live, depending on T212_ENV)
RAW_ENV = os.getenv("T212_ENV", "demo")
T212_ENV = RAW_ENV.strip().lower()
if T212_ENV not in ("demo", "live"):
    logger.error(f"Invalid T212_ENV (`{RAW_ENV}`); must be 'demo' or 'live'.")
    sys.exit(1)

BASE_URL = "https://demo.trading212.com" if T212_ENV == "demo" else "https://live.trading212.com"

# API key (not used in this snippet, but loaded for completeness)
T212_API_KEY = os.getenv("T212_API_KEY", "").strip()
if not T212_API_KEY:
    logger.warning("No T212_API_KEY found in environment; certain API calls may fail.")

# -----------------------------------------------------------------------------
# STEP 1: FETCH COOKIES VIA UNDETECTED CHROMEDRIVER
# -----------------------------------------------------------------------------

def fetch_cloudflare_cookies() -> list:
    """
    Launch a headless browser (undetected_chromedriver) to open BASE_URL and
    wait for user to log in manually. Once the user presses ENTER, retrieve
    all cookies from the browser and return them as a list of dicts.
    """
    logger.info("Starting fetch_cloudflare_cookies()")
    logger.debug(f"Launching Chrome to open {BASE_URL}…")

    # Launch undetected_chromedriver with user profile so login persists
    chrome_options = uc.ChromeOptions()
    # You can comment out headless if you actually want to see it—by default we let it show
    # chrome_options.headless = True
    chrome_options.add_argument(f"--user-data-dir={os.path.abspath('chrome_profile')}")
    chrome = uc.Chrome(options=chrome_options)

    try:
        logger.debug(f"Navigating to {BASE_URL}")
        chrome.get(BASE_URL)

        logger.info("────────── WAIT FOR MANUAL LOGIN ──────────")
        logger.info("  • In the Chrome window, log in to your Trading 212 account.")
        logger.info("  • Switch to your Practice/Demo account if needed.")
        logger.info("  • Once you see your Practice portfolio, come back here ")
        logger.info("    and press ENTER in this terminal. ▶▶▶")
        input("▶▶▶ Press ENTER once you’re fully logged in… ")

        # After ENTER, fetch cookies
        webdriver_cookies = chrome.get_cookies()
        logger.info(f"Chrome closed; retrieved {len(webdriver_cookies)} cookies.")
        for idx, c in enumerate(webdriver_cookies, start=1):
            logger.debug(f"  Cookie #{idx}: name={c.get('name')}  domain={c.get('domain')}  path={c.get('path')}  secure={c.get('secure')}")

        return webdriver_cookies

    except Exception as e:
        logger.error(f"Error in fetch_cloudflare_cookies(): {e}")
        raise

    finally:
        try:
            chrome.quit()
        except Exception as e:
            logger.warning(f"Exception when quitting Chrome driver: {e}")


# -----------------------------------------------------------------------------
# STEP 2: BUILD A REQUESTS SESSION AND ATTACH COOKIES
# -----------------------------------------------------------------------------

def build_api_session(cf_cookies: list) -> requests.Session:
    """
    Given a list of cookies (as dicts) from fetch_cloudflare_cookies(),
    construct a requests.Session, populate all cookie fields, then perform
    an initial GET to clear the Cloudflare challenge. Return the prepared session.
    """
    logger.info("Entering build_api_session()")
    session = requests.Session()

    # Attach each cookie to the session
    for idx, c in enumerate(cf_cookies, start=1):
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain", "")
        path = c.get("path", "/")
        secure = c.get("secure", False)

        # The requests `create_cookie` API does NOT accept 'httpOnly' or sameSite 
        # directly; we strip those out here.
        cookie_params = {
            "domain": domain,
            "path": path,
            "secure": secure,
            # omit httpOnly; requests will set default HttpOnly = False
        }

        # Log what we’re adding
        logger.debug(
            f"Adding cookie to session: #{idx} name={name} value={value[:8]}… "
            f"domain={domain} path={path} secure={secure}"
        )

        session.cookies.set(name=name, value=value, **cookie_params)

    # Now attempt an initial GET to BASE_URL in order to “clear” Cloudflare
    logger.debug("About to perform initial GET to clear Cloudflare challenge…")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }
    logger.debug(f"Initial GET headers:\n  {headers}")

    try:
        resp = session.get(BASE_URL, headers=headers, timeout=20)
    except Exception as e:
        logger.error(f"Exception during initial GET: {e}")
        raise

    logger.debug(f"Initial GET returned status code: {resp.status_code}")
    if resp.status_code != 200:
        snippet = resp.content[:200]
        logger.error(f"Initial GET body snippet: {snippet!r}")
        raise RuntimeError("Failed to clear Cloudflare challenge on initial GET.")

    logger.info("Cloudflare challenge cleared (initial GET returned 200).")
    return session


# -----------------------------------------------------------------------------
# STEP 3: MAIN ENTRYPOINT
# -----------------------------------------------------------------------------

def main():
    logger.info("Starting main()")
    try:
        # 1) Fetch cookies by manual login
        cf_cookies = fetch_cloudflare_cookies()
        logger.info(f"Fetched {len(cf_cookies)} cookies from Chrome.")

        # 2) Build an API session with those cookies
        session = build_api_session(cf_cookies)
        logger.info("Successfully built API session with Cloudflare cookies.")

        # At this point, `session` can be used for subsequent Trading212 API calls.
        # (For demonstration, just do a sample GET on /portfolio or /history.)
        portfolio_url = BASE_URL + "/api/portfolio"  # example endpoint
        logger.debug(f"Performing sample GET to {portfolio_url}")
        sample_resp = session.get(portfolio_url, timeout=15)
        logger.info(f"Sample GET to /api/portfolio status: {sample_resp.status_code}")
        if sample_resp.status_code == 200:
            logger.debug(f"Sample response JSON: {sample_resp.json()}")
        else:
            logger.error(f"Sample request failed: HTTP {sample_resp.status_code}")

    except Exception as e:
        logger.error(f"Exception in main(): {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
