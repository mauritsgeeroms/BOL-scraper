"""
BOL.com Supplier Insight Scraper
=================================
Downloads the "Search terms analysis - top 5 per product" XLSX report
from the BOL.com supplier portal.

Authentication
--------------
- Email     : read from credentials.txt (first line)
- Password  : read from the environment variable BOL_PSWD

Usage
-----
    export BOL_PSWD="your_password_here"   # Linux/macOS
    set BOL_PSWD=your_password_here        # Windows CMD

    python scraper.py [--output-dir ./reports] [--headed]

The XLSX file is saved to the output directory with a timestamped name.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import pathlib
import sys
import time
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://login.bol.com"
LOGIN_URL = f"{BASE_URL}/login?client_id=gatekeeper"
ERROR_URL_PREFIX = f"{BASE_URL}/login?error="
SUPPLIER_INSIGHT_URL = "https://portal.bol.com/supplier/insight/"

CREDENTIALS_FILE = pathlib.Path(__file__).parent / "credentials.txt"

# Selenium wait timeouts (seconds)
PAGE_LOAD_TIMEOUT = 30
ELEMENT_WAIT_TIMEOUT = 20
DOWNLOAD_POLL_INTERVAL = 2
DOWNLOAD_MAX_WAIT = 120

# The report title fragment used to identify the correct download link / tab
REPORT_TITLE_FRAGMENT = "zoektermen"  # Dutch: "search terms"

# Rotating log file configuration
LOG_FILE = pathlib.Path(__file__).parent / "logs" / "scraper.log"
LOG_MAX_BYTES = 1_000_000  # 1 MB per file before rotation
LOG_BACKUP_COUNT = 5       # keep the 5 most recent rotated files

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def read_email(credentials_path: pathlib.Path = CREDENTIALS_FILE) -> str:
    """Return the email address from the first non-empty line of credentials.txt."""
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"credentials.txt not found at {credentials_path}. "
            "Create it with the BOL.com email on the first line."
        )
    lines = credentials_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    raise ValueError("credentials.txt is empty – add the email address on line 1.")


def read_password() -> str:
    """Return the password from the BOL_PSWD environment variable."""
    password = os.environ.get("BOL_PSWD", "")
    if not password:
        raise EnvironmentError(
            "Environment variable BOL_PSWD is not set or empty. "
            "Set it before running: export BOL_PSWD='yourpassword'"
        )
    return password


# ---------------------------------------------------------------------------
# WebDriver factory
# ---------------------------------------------------------------------------

def build_driver(
    headless: bool = True,
    download_dir: Optional[pathlib.Path] = None,
) -> webdriver.Chrome:
    """
    Build and return a configured Chrome WebDriver.

    Parameters
    ----------
    headless:
        Run Chrome without a visible window.
    download_dir:
        Directory where Chrome should save downloaded files automatically.
        Defaults to a ``downloads`` sub-folder next to this script.
    """
    if download_dir is None:
        download_dir = pathlib.Path(__file__).parent / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    # Disable the 'Save password?' bubble
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
    )
    # Suppress automation infobar
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


# ---------------------------------------------------------------------------
# CSRF helper
# ---------------------------------------------------------------------------

def extract_csrf_token(driver: webdriver.Chrome) -> str:
    """
    Extract the CSRF token from the page's __NEXT_DATA__ JSON blob.

    BOL.com embeds the current CSRF token in a <script id="__NEXT_DATA__"> tag.
    Selenium has already rendered the page, so the token is live and valid.
    """
    try:
        next_data_element = driver.find_element(By.ID, "__NEXT_DATA__")
        data = json.loads(next_data_element.get_attribute("textContent"))
        token: str = (
            data["props"]["pageProps"]["data"]["csrf"]["token"]
        )
        log.debug("CSRF token extracted: %s…", token[:10])
        return token
    except (NoSuchElementException, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Could not extract CSRF token from __NEXT_DATA__. "
            "The page structure may have changed."
        ) from exc


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(
    driver: webdriver.Chrome,
    email: str,
    password: str,
) -> None:
    """
    Perform the full login flow on login.bol.com.

    Steps
    -----
    1. Navigate to the login page (Selenium receives the session cookie and
       the embedded CSRF token simultaneously – no separate HTTP request needed).
    2. Extract the live CSRF token from __NEXT_DATA__.
    3. Fill in credentials and submit the form (the CSRF token travels as a
       hidden field value that the browser sends automatically).
    4. Detect success by checking that the URL no longer starts with ERROR_URL_PREFIX
       and that the page is not still the login page.

    Raises
    ------
    RuntimeError
        On login failure (wrong credentials, CSRF rejection, account locked, etc.)
    """
    log.info("Navigating to login page…")
    driver.get(LOGIN_URL)

    wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

    # Wait for the email field to be present (page is fully rendered)
    email_field = wait.until(
        EC.presence_of_element_located((By.NAME, "j_username"))
    )

    # Extract the CSRF token *after* the page has loaded so it is valid
    csrf_token = extract_csrf_token(driver)

    # Inject CSRF token into the hidden field (the form already has one but
    # we overwrite it to be sure we're using the freshly-read value)
    hidden_inputs = driver.find_elements(
        By.CSS_SELECTOR, "input[aria-hidden='true'][hidden]"
    )
    csrf_injected = False
    for hidden in hidden_inputs:
        val = hidden.get_attribute("value") or ""
        # The CSRF token hidden field has the same value as the one in __NEXT_DATA__
        if len(val) > 20 and not val.isdigit():
            driver.execute_script(
                "arguments[0].removeAttribute('readOnly'); arguments[0].value = arguments[1];",
                hidden,
                csrf_token,
            )
            csrf_injected = True
            log.debug("CSRF token injected into hidden field.")
            break

    if not csrf_injected:
        log.warning("Could not locate the CSRF hidden field; submitting without explicit injection.")

    # Fill in credentials
    password_field = driver.find_element(By.NAME, "j_password")
    email_field.clear()
    email_field.send_keys(email)
    password_field.clear()
    password_field.send_keys(password)

    log.info("Submitting login form…")
    submit_button = driver.find_element(By.ID, "submit")
    submit_button.click()

    # Wait for navigation away from the login page
    try:
        wait.until(EC.url_changes(LOGIN_URL))
    except TimeoutException:
        pass  # URL may not change if login fails with an inline error

    current_url = driver.current_url
    log.debug("Post-login URL: %s", current_url)

    if current_url.startswith(ERROR_URL_PREFIX) or "login" in current_url:
        # Try to grab the error message for a helpful exception
        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, "[class*='error'], [data-testid*='error']"
            )
            error_msg = error_el.text.strip()
        except NoSuchElementException:
            error_msg = "Unknown login error"
        raise RuntimeError(f"Login failed: {error_msg}")

    log.info("Login successful. Current URL: %s", current_url)


# ---------------------------------------------------------------------------
# Report download
# ---------------------------------------------------------------------------

def navigate_to_insight(driver: webdriver.Chrome) -> None:
    """Navigate to the supplier insight page."""
    log.info("Navigating to supplier insight: %s", SUPPLIER_INSIGHT_URL)
    driver.get(SUPPLIER_INSIGHT_URL)
    WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )


def find_search_terms_report_link(driver: webdriver.Chrome) -> Optional[str]:
    """
    Scan the page for an XLSX download link or button related to search-terms
    analysis. Returns the href/URL string, or None if not found.

    Strategy
    --------
    1. Look for <a> tags whose text or href contains "zoekterm" (Dutch for
       "search term") or whose href ends with ".xlsx".
    2. Look for download buttons whose aria-label / text matches.
    """
    wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)
    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "a[href*='.xlsx'], a[href*='download'], button[data-download]")
            )
        )
    except TimeoutException:
        log.debug("No obvious download anchors found with primary selector.")

    # Gather all anchors on the page
    anchors = driver.find_elements(By.TAG_NAME, "a")
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        text = anchor.text.lower()
        if (
            ".xlsx" in href.lower()
            or "download" in href.lower()
            or REPORT_TITLE_FRAGMENT in text
            or REPORT_TITLE_FRAGMENT in href.lower()
        ):
            log.debug("Candidate download link found: %s (text=%r)", href, text)
            return href

    # Fallback: look for buttons
    buttons = driver.find_elements(By.TAG_NAME, "button")
    for btn in buttons:
        text = btn.text.lower()
        aria = (btn.get_attribute("aria-label") or "").lower()
        if REPORT_TITLE_FRAGMENT in text or "download" in text or REPORT_TITLE_FRAGMENT in aria:
            log.debug("Candidate download button found (text=%r)", btn.text)
            btn.click()
            time.sleep(2)
            # After click, the file may start downloading; return sentinel
            return "__button_clicked__"

    return None


def wait_for_download(download_dir: pathlib.Path, timeout: int = DOWNLOAD_MAX_WAIT) -> pathlib.Path:
    """
    Block until a new XLSX file appears in *download_dir*.

    Returns the path to the downloaded file.
    Raises TimeoutError if no file appears within *timeout* seconds.
    """
    log.info("Waiting for XLSX download in %s…", download_dir)
    deadline = time.time() + timeout
    seen_before: set[pathlib.Path] = set(download_dir.glob("*.xlsx"))

    while time.time() < deadline:
        current = set(download_dir.glob("*.xlsx"))
        new_files = current - seen_before
        # Also filter out Chrome's partial-download temp files
        complete = {f for f in new_files if not f.name.endswith(".crdownload")}
        if complete:
            newest = max(complete, key=lambda p: p.stat().st_mtime)
            log.info("Download complete: %s", newest)
            return newest
        time.sleep(DOWNLOAD_POLL_INTERVAL)

    raise TimeoutError(
        f"No XLSX file appeared in {download_dir} within {timeout} seconds."
    )


def download_search_terms_report(
    driver: webdriver.Chrome,
    download_dir: pathlib.Path,
) -> pathlib.Path:
    """
    Full workflow: navigate to insight, find and trigger the search-terms
    XLSX download, wait for completion, return the file path.
    """
    navigate_to_insight(driver)
    link = find_search_terms_report_link(driver)

    if link is None:
        # Dump current page source for debugging
        log.warning(
            "Could not find a search-terms download link. "
            "Saving page source for inspection…"
        )
        debug_path = download_dir / "debug_page.html"
        debug_path.write_text(driver.page_source, encoding="utf-8")
        raise RuntimeError(
            "Search-terms report link not found on the insight page. "
            f"Page source saved to {debug_path}"
        )

    if link != "__button_clicked__":
        log.info("Triggering download via link: %s", link)
        driver.get(link)

    return wait_for_download(download_dir)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    output_dir: pathlib.Path,
    headless: bool = True,
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> pathlib.Path:
    """
    Orchestrate the full scrape: login → navigate → download → return path.

    Parameters
    ----------
    output_dir:
        Where to save the downloaded XLSX.
    headless:
        Whether to run Chrome in headless mode.
    email:
        Override the email (useful in tests). Defaults to credentials.txt.
    password:
        Override the password (useful in tests). Defaults to BOL_PSWD env var.

    Returns
    -------
    pathlib.Path
        The path of the downloaded XLSX file.
    """
    if email is None:
        email = read_email()
    if password is None:
        password = read_password()

    output_dir = pathlib.Path(output_dir)
    driver = build_driver(headless=headless, download_dir=output_dir)
    try:
        login(driver, email, password)
        report_path = download_search_terms_report(driver, output_dir)
    finally:
        driver.quit()

    return report_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape the BOL.com 'Search terms – top 5 per product' XLSX report."
    )
    parser.add_argument(
        "--output-dir",
        default="./reports",
        help="Directory to save the downloaded XLSX (default: ./reports)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chrome browser window (disables headless mode).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def _configure_logging(level: int, log_file: pathlib.Path = LOG_FILE) -> None:
    """
    Configure root logging with both a console handler and a rotating file handler.

    Parameters
    ----------
    level:
        The logging level (e.g. ``logging.INFO``).
    log_file:
        Path to the log file. Its parent directory is created if needed.
        Files rotate at ``LOG_MAX_BYTES`` keeping ``LOG_BACKUP_COUNT`` backups.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if this is called more than once.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(console_handler)
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _configure_logging(getattr(logging, args.log_level))
    try:
        report = run(
            output_dir=pathlib.Path(args.output_dir),
            headless=not args.headed,
        )
        print(f"Report downloaded: {report}")
    except (RuntimeError, EnvironmentError, FileNotFoundError, TimeoutError) as exc:
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
