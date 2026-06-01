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
import datetime
import json
import logging
import logging.handlers
import os
import pathlib
import sys
import time
import urllib.parse
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
# The supplier login flow starts on the WSP (web supplier portal) landing page,
# where the user must first click "Doorgaan als leverancier" (Continue as
# supplier) before the credentials form is shown.
LOGIN_URL = f"{BASE_URL}/wsp/nl"
ERROR_URL_PREFIX = f"{BASE_URL}/wsp/login?error="
SUPPLIER_INSIGHT_URL = "https://portal.bol.com/supplier/insight/"

# Text shown on the button that reveals the credentials form on the WSP
# landing page. Matched case-insensitively against button text.
CONTINUE_AS_SUPPLIER_TEXT = "Doorgaan als leverancier"

CREDENTIALS_FILE = pathlib.Path(__file__).parent / "credentials.txt"

# Selenium wait timeouts (seconds)
PAGE_LOAD_TIMEOUT = 30
ELEMENT_WAIT_TIMEOUT = 20
DOWNLOAD_POLL_INTERVAL = 2
DOWNLOAD_MAX_WAIT = 120

# The report-generation flow on the insight page is a multi-step wizard:
#   1. Click "Selectie maken" to open the report-selection dialog.
#   2. Tick the four required report checkboxes.
#   3. Click "Download gereed maken" to start server-side generation.
#   4. Wait (up to ~5 min) until a dark-blue "Downloaden" button appears.
#   5. Click "Downloaden" to fetch the resulting ZIP file.
SELECTIE_MAKEN_TEXT = "Selectie maken"
DOWNLOAD_GEREED_MAKEN_TEXT = "Download gereed maken"
DOWNLOADEN_TEXT = "Downloaden"

# Report labels (as shown next to their checkboxes) that must be selected.
REQUIRED_REPORT_LABELS = [
    "Commerciële rapportage: Publishers",
    "Product visits en conversie benchmark rapportage",
    "Product visits en conversie rapportage",
    "Search terms analysis - top 5 per product",
]

# How long to wait for the server to finish generating the report bundle and
# reveal the "Downloaden" button (seconds). Generation can take ~5 minutes.
REPORT_GENERATION_MAX_WAIT = 600  # 10 minutes
REPORT_GENERATION_POLL_INTERVAL = 5

# Rotating log file configuration
LOG_FILE = pathlib.Path(__file__).parent / "logs" / "scraper.log"
LOG_MAX_BYTES = 1_000_000  # 1 MB per file before rotation
LOG_BACKUP_COUNT = 5       # keep the 5 most recent rotated files

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# XPath helpers
# ---------------------------------------------------------------------------

# The BOL supplier portal is built with the "Puik" Angular design system, so
# clickable controls are custom web components such as <puik-button> rather
# than native <button>/<a> elements. Any text-based lookup must include these.
_CLICKABLE_ELEMENTS = (
    "self::button",
    "self::a",
    "self::puik-button",
    "@role='button'",
)


def _clickable_text_xpath(text: str) -> str:
    """
    Build an XPath that matches a clickable element (native or Puik custom
    element) whose visible text contains *text*, matched case-insensitively.
    """
    lowered = text.lower()
    element_predicate = " or ".join(_CLICKABLE_ELEMENTS)
    return (
        f"//*[{element_predicate}]"
        "[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"'{lowered}')]"
    )


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


def _extract_login_error(driver: webdriver.Chrome, current_url: str) -> str:
    """
    Determine the reason a login attempt failed.

    BOL.com communicates failures in two ways:
    1. A query parameter on the redirect URL, e.g. ``login?error=invalid_csrf``
       or ``login?error=bad_credentials``. This is the most reliable signal.
    2. An inline error element rendered on the page.

    This helper checks the URL first, then falls back to the DOM, and finally
    dumps the page source for inspection so an "Unknown login error" still
    leaves a debugging trail.
    """
    # 1. Parse the ?error= query parameter from the URL.
    parsed = urllib.parse.urlparse(current_url)
    query = urllib.parse.parse_qs(parsed.query)
    error_code = query.get("error", [None])[0]
    if error_code:
        log.error("Login error code from URL: %s", error_code)
        return f"server returned error '{error_code}' (URL: {current_url})"

    # 2. Fall back to an inline error element on the page.
    try:
        error_el = driver.find_element(
            By.CSS_SELECTOR, "[class*='error'], [data-testid*='error']"
        )
        text = error_el.text.strip()
        if text:
            return text
    except NoSuchElementException:
        pass

    # 3. Nothing found – dump the page source so the failure is debuggable.
    try:
        debug_path = LOG_FILE.parent / "login_error_page.html"
        debug_path.write_text(driver.page_source, encoding="utf-8")
        log.error("No error message found. Page source saved to %s", debug_path)
        return f"Unknown login error (page source saved to {debug_path})"
    except OSError:
        return "Unknown login error (could not save page source)"


def _click_continue_as_supplier(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """
    On the WSP landing page (``/wsp/nl``) click the "Doorgaan als leverancier"
    (Continue as supplier) button to reveal the credentials form.

    The button is located case-insensitively by its visible text using XPath so
    we are resilient to surrounding markup changes. If the credentials form is
    already present (e.g. BOL skipped the landing step), this is a no-op.
    """
    # If the credentials form is already on the page, there is nothing to click.
    if driver.find_elements(By.NAME, "j_username"):
        log.debug("Credentials form already present; skipping landing step.")
        return

    log.info("Clicking '%s'…", CONTINUE_AS_SUPPLIER_TEXT)
    xpath = _clickable_text_xpath(CONTINUE_AS_SUPPLIER_TEXT)
    try:
        button = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
    except TimeoutException as exc:
        raise RuntimeError(
            f"Could not find the '{CONTINUE_AS_SUPPLIER_TEXT}' button on "
            f"{LOGIN_URL}. The landing-page layout may have changed."
        ) from exc

    button.click()


def login(
    driver: webdriver.Chrome,
    email: str,
    password: str,
) -> None:
    """
    Perform the full login flow on login.bol.com.

    Steps
    -----
    1. Navigate to the WSP landing page (Selenium receives the session cookie
       and the embedded CSRF token simultaneously – no separate HTTP request
       needed).
    2. Click "Doorgaan als leverancier" to reveal the credentials form.
    3. Extract the live CSRF token from __NEXT_DATA__.
    4. Fill in credentials and submit the form (the CSRF token travels as a
       hidden field value that the browser sends automatically).
    5. Detect success by checking that the URL no longer starts with ERROR_URL_PREFIX
       and that the page is not still the login page.

    Raises
    ------
    RuntimeError
        On login failure (wrong credentials, CSRF rejection, account locked, etc.)
    """
    log.info("Navigating to login page…")
    driver.get(LOGIN_URL)

    wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

    # The supplier login is a two-step flow: first the WSP landing page, then
    # the credentials form. Click "Doorgaan als leverancier" to reveal the form.
    _click_continue_as_supplier(driver, wait)

    # Wait for the email field to be *interactable* (visible + enabled), not just
    # present in the DOM. On this Next.js page the form HTML exists before the
    # client-side JS hydrates; submitting too early causes the CSRF token to be
    # stale/missing and the server silently redirects back to ?error=.
    email_field = wait.until(
        EC.element_to_be_clickable((By.NAME, "j_username"))
    )

    # Also ensure the __NEXT_DATA__ blob carrying the CSRF token is present
    # before we read it, confirming hydration has produced a usable token.
    wait.until(EC.presence_of_element_located((By.ID, "__NEXT_DATA__")))

    # Extract the CSRF token *after* the page has loaded so it is valid
    csrf_token = extract_csrf_token(driver)
    log.info("CSRF token obtained (length=%d).", len(csrf_token))

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
    url_before_submit = driver.current_url
    submit_button = driver.find_element(By.ID, "submit")
    submit_button.click()

    # Wait for the post-submit navigation. On success BOL redirects away from
    # the login.bol.com host (to the portal); on failure it redirects back to
    # a /wsp/login?error=... URL on the same host. Wait until the URL leaves
    # the credentials page we just submitted from.
    try:
        wait.until(EC.url_changes(url_before_submit))
    except TimeoutException:
        pass  # URL may not change if login fails with an inline error

    current_url = driver.current_url
    log.info("Post-login URL: %s", current_url)

    # Failure signals: an explicit ?error= redirect, or still being parked on
    # the login.bol.com host instead of having reached the portal.
    if current_url.startswith(ERROR_URL_PREFIX) or current_url.startswith(BASE_URL):
        error_msg = _extract_login_error(driver, current_url)
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


def _find_clickable_by_text(
    driver: webdriver.Chrome,
    text: str,
    wait: WebDriverWait,
) -> "object":
    """
    Find and return a clickable element (button / link / role=button) whose
    visible text contains *text*, matched case-insensitively.

    Raises RuntimeError (via TimeoutException) if not found within the wait.
    """
    xpath = _clickable_text_xpath(text)
    return wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))


def _click_by_text(driver: webdriver.Chrome, text: str, wait: WebDriverWait) -> None:
    """Click the first clickable element whose visible text contains *text*."""
    log.info("Clicking '%s'…", text)
    try:
        element = _find_clickable_by_text(driver, text, wait)
    except TimeoutException as exc:
        raise RuntimeError(
            f"Could not find a clickable '{text}' element. "
            "The insight-page layout may have changed."
        ) from exc
    # Use a JS click as a fallback-safe approach (avoids overlay interception).
    try:
        element.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)


def _select_reports_via_angular(
    driver: webdriver.Chrome,
    labels: list[str],
    wait: WebDriverWait,
) -> None:
    """
    Select the report checkboxes for every label in *labels* by manipulating
    Angular's reactive form directly from JavaScript.

    Background
    ----------
    The portal renders all report options inside a single
    ``<puik-form-option-group id="bulkSelectionReports">`` Stencil.js web
    component. Angular passes the option objects via property binding, but the
    component only populates its shadow DOM lazily — by the time Selenium's
    ``page_source`` is captured the shadow DOM is still empty, so neither XPath
    nor CSS selectors can reach the individual checkboxes.

    Strategy (tried in order until one succeeds)
    --------------------------------------------
    1. **Angular form patchValue** – reads the ``.options`` JS property on the
       ``<puik-form-option-group>`` element (populated synchronously by Angular
       before the component hydrates), filters it to the required labels, then
       finds the Angular ``FormGroup`` that owns the ``reports`` control via
       ``ng.getContext()`` / ``__ngContext__`` and calls
       ``formGroup.get('reports').setValue(selectedValues)``.

    2. **Shadow-DOM click** – as a last resort, recursively pierces shadow roots
       to locate ``<input type="checkbox">`` elements whose associated label
       text matches, then clicks them one by one.

    Raises ``RuntimeError`` if neither strategy manages to select all required
    labels.
    """
    lowered_targets = [lbl.lower() for lbl in labels]

    # ------------------------------------------------------------------
    # Wait for the option-group element to appear in the light DOM.
    # ------------------------------------------------------------------
    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "puik-form-option-group#bulkSelectionReports,"
                 "puik-form-option-group")
            )
        )
    except TimeoutException as exc:
        raise RuntimeError(
            "Could not find the report-selection option group in the dialog. "
            "The page layout may have changed."
        ) from exc

    # ------------------------------------------------------------------
    # Strategy 1: manipulate Angular's FormGroup directly via JavaScript.
    # ------------------------------------------------------------------
    # The JS snippet:
    # a) Reads element.options (the array Angular bound to the component).
    # b) Filters to the required labels (case-insensitive substring match).
    # c) Finds the Angular FormGroup by walking __ngContext__ on the <form>.
    # d) Calls setValue() on the 'reports' control and marks it dirty so
    #    Angular's validators / change detection pick up the new value.
    # Returns a JSON string: "ok:<n>" on success, or an error description.
    js_angular = """
        var targets = arguments[0];   // array of lower-cased label substrings

        // ---- a) Get the option objects from the Stencil element property ----
        var host = document.querySelector('puik-form-option-group#bulkSelectionReports')
                || document.querySelector('puik-form-option-group');
        if (!host) return 'err:no-host';

        var allOptions = host.options;
        if (!allOptions || !allOptions.length) return 'err:no-options';

        var selected = [];
        for (var i = 0; i < allOptions.length; i++) {
            var opt = allOptions[i];
            var lbl = ((opt.label || opt.name || opt.title || '') + '').toLowerCase();
            for (var j = 0; j < targets.length; j++) {
                if (lbl.indexOf(targets[j]) !== -1) {
                    selected.push(opt.value !== undefined ? opt.value : opt);
                    break;
                }
            }
        }
        if (selected.length === 0) return 'err:no-match';

        // ---- b) Find the Angular FormGroup via ng.getContext() ----
        // The <form> is inside the Stencil shadow DOM so __ngContext__ is empty.
        // Angular 14+ exposes ng.getContext() which works on the component host.
        var formGroup = null;
        if (typeof ng !== 'undefined' && ng.getContext) {
            // Try the component host first, then the form element itself.
            var candidates = [
                document.querySelector('app-bulk-export-selection-editor'),
                document.querySelector('app-bulk-export-selection-editor form'),
            ];
            for (var ci = 0; ci < candidates.length; ci++) {
                if (!candidates[ci]) continue;
                var comp = ng.getContext(candidates[ci]);
                if (comp) {
                    // Component exposes .form (FormGroup) directly.
                    if (comp.form && comp.form.controls && comp.form.controls.reports) {
                        formGroup = comp.form; break;
                    }
                    // Or it may be nested under another property.
                    var keys = Object.keys(comp);
                    for (var ki = 0; ki < keys.length; ki++) {
                        var v = comp[keys[ki]];
                        if (v && typeof v === 'object' && v.controls && v.controls.reports) {
                            formGroup = v; break;
                        }
                    }
                    if (formGroup) break;
                }
            }
        }
        // Fallback: walk __ngContext__ on the host element (Angular lView array).
        if (!formGroup) {
            var hostEl = document.querySelector('app-bulk-export-selection-editor');
            var ctx = hostEl && hostEl.__ngContext__;
            if (ctx) {
                for (var k = 0; k < ctx.length; k++) {
                    var item = ctx[k];
                    if (item && typeof item === 'object' && !Array.isArray(item)
                            && item.controls && item.controls.reports) {
                        formGroup = item; break;
                    }
                }
            }
        }
        // ---- b) Set the value directly on the Stencil element and fire
        //         the puikChange event that Angular's CVA is listening to ----
        // Merge with any already-selected values to avoid deselecting things.
        var existing = Array.isArray(host.value) ? host.value : [];
        var merged = existing.slice();
        for (var m = 0; m < selected.length; m++) {
            var v = selected[m];
            var alreadyIn = false;
            for (var n = 0; n < merged.length; n++) {
                if (JSON.stringify(merged[n]) === JSON.stringify(v)) {
                    alreadyIn = true; break;
                }
            }
            if (!alreadyIn) merged.push(v);
        }

        // Write back to the element property so the component's internal
        // state stays consistent, then dispatch puikChange so Angular's
        // ControlValueAccessor callback fires.
        host.value = merged;
        host.dispatchEvent(new CustomEvent('puikChange', {
            detail: merged,
            bubbles: true,
            composed: true
        }));

        return 'ok:' + merged.length;
    """

    # ------------------------------------------------------------------
    # Diagnostic probe – logged at DEBUG level to aid selector development.
    # Captures: first option object shape, __ngContext__ key types, shadow DOM.
    # ------------------------------------------------------------------
    js_probe = """
        var info = {};
        var host = document.querySelector('puik-form-option-group#bulkSelectionReports')
                || document.querySelector('puik-form-option-group');
        info.hostFound = !!host;
        if (host) {
            info.hostTagName   = host.tagName;
            info.hostClassName = host.className;
            // Raw .options property (Angular property binding)
            var opts = host.options;
            info.optionsType   = typeof opts;
            info.optionsIsArray = Array.isArray(opts);
            info.optionsLength = opts ? opts.length : null;
            // Dump first option object in full so we can see its shape
            if (opts && opts.length > 0) {
                try { info.firstOption = JSON.parse(JSON.stringify(opts[0])); }
                catch(e) { info.firstOptionKeys = Object.keys(opts[0]); }
            }
            // All option labels
            info.allOptionLabels = opts ? Array.prototype.map.call(opts, function(o) {
                return o.label || o.name || o.title || Object.keys(o).join(',');
            }) : null;
            // Shadow root status
            info.hasShadowRoot = !!host.shadowRoot;
            if (host.shadowRoot) {
                info.shadowChildCount = host.shadowRoot.childElementCount;
                var inputs = host.shadowRoot.querySelectorAll('input[type=checkbox]');
                info.shadowCheckboxCount = inputs.length;
            }
        }
        // Form element + __ngContext__ shape
        var formEl = document.querySelector('app-bulk-export-selection-editor form');
        info.formFound = !!formEl;
        if (formEl && formEl.__ngContext__) {
            var ctx = formEl.__ngContext__;
            info.ctxLength = ctx.length;
            info.ctxTypes  = Array.prototype.map.call(ctx, function(x) {
                if (x === null) return 'null';
                var t = typeof x;
                if (t === 'object') {
                    if (Array.isArray(x)) return 'Array(' + x.length + ')';
                    var keys = Object.keys(x);
                    if (x.controls) return 'FormGroup{' + Object.keys(x.controls).join(',') + '}';
                    return 'Object{' + keys.slice(0,5).join(',') + (keys.length > 5 ? '...' : '') + '}';
                }
                return t;
            });
        } else if (formEl) {
            info.ngCtxMissing = true;
        }
        // Inspect what events Angular has attached to the option-group element
        // (Angular stores event listeners in __ngContext__ or via getListeners)
        if (typeof ng !== 'undefined' && ng.getListeners && host) {
            try {
                var listeners = ng.getListeners(host);
                info.ngListeners = listeners ? listeners.map(function(l) {
                    return l.name || l.type || String(l);
                }) : [];
            } catch(e) { info.ngListenersErr = String(e); }
        }
        // Also check what properties the element exposes (look for 'value')
        if (host) {
            info.hostHasValue = 'value' in host;
            info.hostValue = host.value;
            // Try reading all own enumerable properties starting with known names
            var propNames = ['value', 'values', 'selectedValues', 'checked', 'selected'];
            info.hostProps = {};
            for (var pi = 0; pi < propNames.length; pi++) {
                if (propNames[pi] in host) {
                    try { info.hostProps[propNames[pi]] = JSON.parse(JSON.stringify(host[propNames[pi]])); }
                    catch(e) { info.hostProps[propNames[pi]] = typeof host[propNames[pi]]; }
                }
            }
        }
        return JSON.stringify(info);
    """
    try:
        probe_result = driver.execute_script(js_probe)
        log.info("JS probe result: %s", probe_result)
    except Exception as exc:  # noqa: BLE001
        log.warning("JS probe failed: %s", exc)

    result = driver.execute_script(js_angular, lowered_targets)
    log.info("Angular form patchValue result: %s", result)

    if isinstance(result, str) and result.startswith("ok:"):
        log.info(
            "Selected %s report(s) via Angular form: %s",
            result.split(":")[1],
            labels,
        )
        return

    # ------------------------------------------------------------------
    # Strategy 2: shadow-DOM click fallback.
    # ------------------------------------------------------------------
    log.debug(
        "Angular form strategy failed (%s); trying shadow-DOM click fallback.",
        result,
    )
    js_click = """
        var targets = arguments[0];   // array of lower-cased label substrings
        var clicked = [];
        var errors = [];

        function searchAndClick(root) {
            var inputs = root.querySelectorAll('input[type="checkbox"]');
            for (var i = 0; i < inputs.length; i++) {
                var inp = inputs[i];
                var labelEl = null;
                if (inp.id) labelEl = root.querySelector('label[for="' + inp.id + '"]');
                if (!labelEl) labelEl = inp.closest('label');
                var text = (labelEl ? labelEl.textContent
                            : (inp.parentElement ? inp.parentElement.textContent : '')).toLowerCase();
                for (var j = 0; j < targets.length; j++) {
                    if (text.indexOf(targets[j]) !== -1) {
                        if (!inp.checked) inp.click();
                        clicked.push(targets[j]);
                        break;
                    }
                }
            }
            // Recurse into nested shadow roots
            var all = root.querySelectorAll('*');
            for (var k = 0; k < all.length; k++) {
                if (all[k].shadowRoot) searchAndClick(all[k].shadowRoot);
            }
        }

        var host = document.querySelector('puik-form-option-group#bulkSelectionReports')
                || document.querySelector('puik-form-option-group');
        if (!host) return 'err:no-host';
        searchAndClick(host.shadowRoot || host);
        return clicked.length > 0 ? 'ok:' + clicked.join(',') : 'err:none-clicked';
    """

    result2 = driver.execute_script(js_click, lowered_targets)
    log.info("Shadow-DOM click result: %s", result2)

    if isinstance(result2, str) and result2.startswith("ok:"):
        log.info("Selected report(s) via shadow-DOM click: %s", result2)
        return

    raise RuntimeError(
        f"Could not select the required reports {labels} in the selection "
        f"dialog. Angular strategy: {result!r}. Shadow-DOM strategy: "
        f"{result2!r}. The page structure may have changed significantly."
    )


def _select_report_checkbox(
    driver: webdriver.Chrome,
    label: str,
    wait: WebDriverWait,
) -> None:
    """
    Thin wrapper kept for backward compatibility.
    Delegates to :func:`_select_reports_via_angular` for a single label.
    """
    _select_reports_via_angular(driver, [label], wait)


def _set_date_range(
    driver: webdriver.Chrome,
    start_date: datetime.date,
    end_date: datetime.date,
    wait: WebDriverWait,
) -> None:
    """
    Set the date-range picker in the bulk-export selection dialog.

    The ``<puik-form-date id="bulkSelectionDateRange">`` Stencil component
    uses the same CVA pattern as ``<puik-form-option-group>``: its ``.value``
    JS property holds ``{start: <Date>, end: <Date>}`` and Angular listens for
    a ``puikChange`` CustomEvent carrying the new value as ``event.detail``.

    Important: the component only accepts native JS ``Date`` *objects* — it
    rejects plain ISO strings (resets value to ``{start: null, end: null}``).
    We therefore pass year/month/day as integers and construct the Date objects
    inside the JS snippet.

    The portal only allows complete (Mon–Sun) weeks, so the caller is
    responsible for passing valid week boundaries.

    Parameters
    ----------
    start_date:
        The Monday that starts the desired reporting week range.
    end_date:
        The Sunday that ends the desired reporting week range.
    """
    log.info("Setting date range: %s → %s", start_date, end_date)

    # Pass date components as integers; JS month is 0-indexed.
    js = """
        var sy = arguments[0], sm = arguments[1], sd = arguments[2];
        var ey = arguments[3], em = arguments[4], ed = arguments[5];
        var el = document.querySelector('puik-form-date#bulkSelectionDateRange')
                 || document.querySelector('puik-form-date');
        if (!el) return 'err:no-element';

        // Construct native Date objects (month is 0-indexed in JS).
        var startDate = new Date(sy, sm - 1, sd, 0, 0, 0, 0);
        var endDate   = new Date(ey, em - 1, ed, 23, 59, 59, 999);
        var value = {start: startDate, end: endDate};

        el.value = value;
        el.dispatchEvent(new CustomEvent('puikChange', {
            detail: value,
            bubbles: true,
            composed: true
        }));

        // Verify the component accepted the value (not reset to null).
        var v = el.value;
        if (!v || v.start === null || v.start === undefined) return 'err:rejected';
        return 'ok';
    """

    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "puik-form-date#bulkSelectionDateRange,"
                 "puik-form-date")
            )
        )
    except TimeoutException as exc:
        raise RuntimeError(
            "Could not find the date-range picker in the selection dialog."
        ) from exc

    result = driver.execute_script(
        js,
        start_date.year, start_date.month, start_date.day,
        end_date.year,   end_date.month,   end_date.day,
    )
    if result != "ok":
        raise RuntimeError(
            f"Failed to set date range ({start_date} – {end_date}): {result}"
        )
    log.debug("Date range set: %s → %s", start_date, end_date)


def wait_for_download(
    download_dir: pathlib.Path,
    timeout: int = DOWNLOAD_MAX_WAIT,
    patterns: tuple[str, ...] = ("*.zip", "*.xlsx"),
) -> pathlib.Path:
    """
    Block until a new download (matching *patterns*) appears in *download_dir*.

    The selected reports are delivered as a single ZIP, so ``*.zip`` is checked
    first, with ``*.xlsx`` kept as a fallback.

    Returns the path to the downloaded file.
    Raises TimeoutError if no file appears within *timeout* seconds.
    """
    log.info("Waiting for download in %s…", download_dir)

    def _matching() -> set[pathlib.Path]:
        found: set[pathlib.Path] = set()
        for pat in patterns:
            found.update(download_dir.glob(pat))
        return found

    deadline = time.time() + timeout
    seen_before: set[pathlib.Path] = _matching()

    while time.time() < deadline:
        new_files = _matching() - seen_before
        # Filter out Chrome's partial-download temp files.
        complete = {f for f in new_files if not f.name.endswith(".crdownload")}
        if complete:
            newest = max(complete, key=lambda p: p.stat().st_mtime)
            log.info("Download complete: %s", newest)
            return newest
        time.sleep(DOWNLOAD_POLL_INTERVAL)

    raise TimeoutError(
        f"No download matching {patterns} appeared in {download_dir} "
        f"within {timeout} seconds."
    )


def wait_for_download_ready_button(
    driver: webdriver.Chrome,
    timeout: int = REPORT_GENERATION_MAX_WAIT,
) -> "object":
    """
    Poll the page until the dark-blue "Downloaden" button appears (the server
    has finished generating the report bundle), then return that element.

    Report generation can take several minutes, so this polls slowly with its
    own deadline rather than relying on a single WebDriverWait.
    """
    log.info(
        "Waiting up to %d seconds for the '%s' button to appear…",
        timeout,
        DOWNLOADEN_TEXT,
    )
    xpath = _clickable_text_xpath(DOWNLOADEN_TEXT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        elements = driver.find_elements(By.XPATH, xpath)
        for el in elements:
            try:
                if el.is_displayed() and el.is_enabled():
                    log.info("'%s' button is ready.", DOWNLOADEN_TEXT)
                    return el
            except WebDriverException:
                continue
        time.sleep(REPORT_GENERATION_POLL_INTERVAL)

    raise TimeoutError(
        f"The '{DOWNLOADEN_TEXT}' button did not appear within {timeout} "
        "seconds. Report generation may have failed or taken too long."
    )


def download_search_terms_report(
    driver: webdriver.Chrome,
    download_dir: pathlib.Path,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
) -> pathlib.Path:
    """
    Full report-download workflow on the supplier insight page:

    1. Navigate to the insight page.
    2. Click "Selectie maken" to open the report-selection dialog.
    3. Tick the four required report checkboxes.
    4. Optionally set the date range (start_date – end_date).
    5. Click "Download gereed maken" to start server-side generation.
    6. Wait (up to ~10 min) for the dark-blue "Downloaden" button.
    7. Click "Downloaden" and wait for the ZIP to land in *download_dir*.

    Parameters
    ----------
    start_date, end_date:
        Optional week boundaries for the reporting period.  Both must be
        supplied together; if omitted the portal's current default is kept.
        The portal only accepts completed (Mon–Sun) week ranges.

    Returns the path to the downloaded ZIP file.
    """
    if (start_date is None) != (end_date is None):
        raise ValueError("Provide both start_date and end_date, or neither.")

    navigate_to_insight(driver)
    wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

    try:
        # Step 2: open the selection dialog.
        _click_by_text(driver, SELECTIE_MAKEN_TEXT, wait)

        # The dialog is a Puik modal that renders its option-group only once it
        # opens. Wait for the modal to become visible before selecting reports.
        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     "puik-modal[ng-reflect-is-open='true'], puik-side-modal, "
                     "puik-form-option-group, puik-form-checkbox")
                )
            )
        except TimeoutException:
            log.debug("Selection modal did not report open state; continuing.")

        # Step 3: tick each required report checkbox.
        for label in REQUIRED_REPORT_LABELS:
            _select_report_checkbox(driver, label, wait)

        # Step 4 (optional): set date range.
        if start_date is not None and end_date is not None:
            _set_date_range(driver, start_date, end_date, wait)

        # Step 5: kick off report generation.
        _click_by_text(driver, DOWNLOAD_GEREED_MAKEN_TEXT, wait)
    except RuntimeError:
        # Save the page so selectors can be refined against the real DOM.
        debug_path = download_dir / "debug_insight_page.html"
        try:
            debug_path.write_text(driver.page_source, encoding="utf-8")
            log.error("Selection step failed. Page source saved to %s", debug_path)
        except OSError:
            log.error("Selection step failed and page source could not be saved.")
        raise

    # Step 5: wait for the server to finish and reveal the Downloaden button.
    download_button = wait_for_download_ready_button(driver)

    # Step 6: trigger the download and wait for the ZIP file.
    log.info("Clicking '%s' to start the file download…", DOWNLOADEN_TEXT)
    try:
        download_button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", download_button)

    return wait_for_download(download_dir)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    output_dir: pathlib.Path,
    headless: bool = True,
    email: Optional[str] = None,
    password: Optional[str] = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
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
    start_date, end_date:
        Optional week boundaries for the reporting period (``datetime.date``).
        Both must be supplied together; if omitted the portal default is used.
        The portal only accepts completed (Mon–Sun) week ranges.

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
        report_path = download_search_terms_report(
            driver, output_dir,
            start_date=start_date,
            end_date=end_date,
        )
    finally:
        driver.quit()

    return report_path


def _weeks_to_date_range(
    week_numbers: list[int],
    year: int | None = None,
) -> tuple[datetime.date, datetime.date]:
    """
    Convert a list of ISO week numbers to a (start_date, end_date) range.

    Uses the Monday of the earliest week as *start_date* and the Sunday of
    the latest week as *end_date*.  Non-consecutive weeks (gaps) are allowed —
    the resulting range spans everything between the first and last week.

    Parameters
    ----------
    week_numbers:
        One or more ISO week numbers (1–53).
    year:
        The ISO year to use.  Defaults to the current year.

    Returns
    -------
    (start_date, end_date)
        A (Monday, Sunday) pair suitable for passing to ``_set_date_range``.

    Raises
    ------
    ValueError
        If *week_numbers* is empty or contains values outside 1–53.
    """
    if not week_numbers:
        raise ValueError("At least one week number must be provided.")

    if year is None:
        year = datetime.date.today().isocalendar()[0]

    for w in week_numbers:
        if not (1 <= w <= 53):
            raise ValueError(
                f"Week number {w} is out of range (must be 1–53)."
            )
        # Verify the week actually exists in this year (week 53 doesn't always).
        # fromisocalendar raises for some years, but silently wraps for others,
        # so we catch both cases with a try/except + round-trip check.
        try:
            probe = datetime.date.fromisocalendar(year, w, 1)
        except ValueError:
            raise ValueError(f"Week {w} does not exist in year {year}.")
        if probe.isocalendar()[1] != w:
            raise ValueError(f"Week {w} does not exist in year {year}.")

    first_week = min(week_numbers)
    last_week  = max(week_numbers)
    start_date = datetime.date.fromisocalendar(year, first_week, 1)  # Monday
    end_date   = datetime.date.fromisocalendar(year, last_week,  7)  # Sunday
    return start_date, end_date


def _parse_weeks(value: str) -> list[int]:
    """
    Parse a comma-separated list of week numbers (e.g. ``"18,19,20"``) into
    a sorted list of ints.  Used as an argparse ``type`` converter.
    """
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(
            "Provide at least one week number, e.g. --weeks 18 or --weeks 18,19,20"
        )
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"'{p}' is not a valid week number. Expected integers, e.g. 18,19,20"
            )
    return sorted(set(result))


def _validate_date_range(
    start: datetime.date,
    end: datetime.date,
) -> None:
    """
    Raise ``ValueError`` if the date range is not a valid Mon–Sun aligned range.

    The portal only accepts week-aligned ranges.  We check:
    - start is a Monday (weekday == 0)
    - end   is a Sunday  (weekday == 6)
    - start <= end
    """
    if start.weekday() != 0:
        raise ValueError(
            f"start_date {start} is not a Monday "
            f"(weekday={start.weekday()}; Monday=0)."
        )
    if end.weekday() != 6:
        raise ValueError(
            f"end_date {end} is not a Sunday "
            f"(weekday={end.weekday()}; Sunday=6)."
        )
    if start > end:
        raise ValueError(
            f"start_date {start} must be on or before end_date {end}."
        )


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
    parser.add_argument(
        "--weeks",
        metavar="N[,N…]",
        type=_parse_weeks,
        default=None,
        help=(
            "Comma-separated ISO week numbers to download, e.g. --weeks 18 or "
            "--weeks 18,19,20. Uses the current year. The scraper spans from "
            "Monday of the earliest week to Sunday of the latest."
        ),
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

    start_date = end_date = None
    if args.weeks is not None:
        try:
            start_date, end_date = _weeks_to_date_range(args.weeks)
        except ValueError as exc:
            log.error("%s", exc)
            sys.exit(1)
        log.info(
            "Weeks %s → %s – %s",
            ",".join(str(w) for w in args.weeks),
            start_date,
            end_date,
        )

    try:
        report = run(
            output_dir=pathlib.Path(args.output_dir),
            headless=not args.headed,
            start_date=start_date,
            end_date=end_date,
        )
        print(f"Report downloaded: {report}")
    except (RuntimeError, EnvironmentError, FileNotFoundError, TimeoutError) as exc:
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
