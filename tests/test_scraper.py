"""
Tests for scraper.py
=====================

Test strategy
-------------
* Unit tests mock the Selenium WebDriver and all network/file I/O so they are
  fast, offline, and reproducible.
* CSRF-specific tests verify that:
    - the token is correctly extracted from __NEXT_DATA__
    - the scraper raises clearly when the token is missing / malformed
    - a token mismatch (simulated error redirect) causes RuntimeError
* Login tests cover: happy path, wrong credentials, account locked,
  missing environment variable, missing credentials file.
* Download tests cover: happy path, missing link, download timeout.

Run
---
    pytest tests/ -v
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import time
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest
from selenium.common.exceptions import WebDriverException

import scraper


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

VALID_CSRF_TOKEN = "D-i4VkR2LPt85SbP6D4xV_x_0nZmhLX_4g5AIQHERne"
VALID_NEXT_DATA = json.dumps(
    {
        "props": {
            "pageProps": {
                "data": {
                    "csrf": {
                        "token": VALID_CSRF_TOKEN,
                        "parameterName": "_csrf",
                        "headerName": "X-CSRF-TOKEN",
                    }
                }
            }
        }
    }
)


def _make_driver_mock(
    current_url: str = "https://portal.bol.com/supplier/insight/",
    next_data_json: str = VALID_NEXT_DATA,
    hidden_input_values: list[str] | None = None,
    anchors: list[dict] | None = None,
    buttons: list[dict] | None = None,
) -> MagicMock:
    """
    Build a fully configured mock WebDriver.

    Parameters
    ----------
    current_url:
        The URL the driver reports after navigation / login.
    next_data_json:
        The textContent of the __NEXT_DATA__ <script> element.
    hidden_input_values:
        Values returned by the hidden input elements (for CSRF injection).
    anchors:
        List of dicts with keys 'href' and 'text' representing <a> elements.
    buttons:
        List of dicts with keys 'text', 'aria_label' representing <button>s.
    """
    driver = MagicMock()

    # current_url property
    type(driver).current_url = PropertyMock(return_value=current_url)

    # __NEXT_DATA__ element
    next_data_el = MagicMock()
    next_data_el.get_attribute.return_value = next_data_json

    # Hidden CSRF inputs
    hidden_vals = hidden_input_values or [VALID_CSRF_TOKEN, "10406995640141692"]
    hidden_input_mocks = []
    for val in hidden_vals:
        inp = MagicMock()
        inp.get_attribute.return_value = val
        hidden_input_mocks.append(inp)

    # Email / password fields
    email_field = MagicMock()
    password_field = MagicMock()

    # Submit button
    submit_btn = MagicMock()

    # find_element dispatch
    def find_element_side_effect(by, value):
        if value == "__NEXT_DATA__":
            return next_data_el
        if value == "j_username":
            return email_field
        if value == "j_password":
            return password_field
        if value == "submit":
            return submit_btn
        return MagicMock()

    driver.find_element.side_effect = find_element_side_effect

    # find_elements dispatch
    anchor_mocks = []
    for a in (anchors or []):
        m = MagicMock()
        m.get_attribute.return_value = a.get("href", "")
        type(m).text = PropertyMock(return_value=a.get("text", ""))
        anchor_mocks.append(m)

    button_mocks = []
    for b in (buttons or []):
        m = MagicMock()
        type(m).text = PropertyMock(return_value=b.get("text", ""))
        m.get_attribute.return_value = b.get("aria_label", "")
        button_mocks.append(m)

    def find_elements_side_effect(by, value):
        if "hidden" in value or "aria-hidden" in value:
            return hidden_input_mocks
        if value == "a":
            return anchor_mocks
        if value == "button":
            return button_mocks
        if value == "body":
            return [MagicMock()]
        return []

    driver.find_elements.side_effect = find_elements_side_effect

    # page_source property
    type(driver).page_source = PropertyMock(return_value="<html>mocked</html>")

    return driver


# ---------------------------------------------------------------------------
# Tests: read_email
# ---------------------------------------------------------------------------

class TestReadEmail:
    def test_reads_first_non_empty_line(self, tmp_path):
        f = tmp_path / "credentials.txt"
        f.write_text("CB-Admin@lannoomeulenhoff.nl\n", encoding="utf-8")
        assert scraper.read_email(f) == "CB-Admin@lannoomeulenhoff.nl"

    def test_skips_leading_blank_lines(self, tmp_path):
        f = tmp_path / "credentials.txt"
        f.write_text("\n\nuser@example.com\n", encoding="utf-8")
        assert scraper.read_email(f) == "user@example.com"

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "credentials.txt"
        f.write_text("  user@example.com  \n", encoding="utf-8")
        assert scraper.read_email(f) == "user@example.com"

    def test_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="credentials.txt not found"):
            scraper.read_email(tmp_path / "nonexistent.txt")

    def test_raises_on_empty_file(self, tmp_path):
        f = tmp_path / "credentials.txt"
        f.write_text("   \n\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            scraper.read_email(f)


# ---------------------------------------------------------------------------
# Tests: read_password
# ---------------------------------------------------------------------------

class TestReadPassword:
    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("BOL_PSWD", "secret123")
        assert scraper.read_password() == "secret123"

    def test_raises_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("BOL_PSWD", raising=False)
        with pytest.raises(EnvironmentError, match="BOL_PSWD"):
            scraper.read_password()

    def test_raises_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("BOL_PSWD", "")
        with pytest.raises(EnvironmentError, match="BOL_PSWD"):
            scraper.read_password()


# ---------------------------------------------------------------------------
# Tests: extract_csrf_token
# ---------------------------------------------------------------------------

class TestExtractCsrfToken:
    """These tests directly verify CSRF token extraction logic."""

    def test_extracts_valid_token(self):
        driver = _make_driver_mock(next_data_json=VALID_NEXT_DATA)
        token = scraper.extract_csrf_token(driver)
        assert token == VALID_CSRF_TOKEN

    def test_raises_when_element_missing(self):
        driver = MagicMock()
        from selenium.common.exceptions import NoSuchElementException
        driver.find_element.side_effect = NoSuchElementException("not found")
        with pytest.raises(RuntimeError, match="CSRF token"):
            scraper.extract_csrf_token(driver)

    def test_raises_on_malformed_json(self):
        driver = _make_driver_mock(next_data_json="not-valid-json{{{")
        with pytest.raises(RuntimeError, match="CSRF token"):
            scraper.extract_csrf_token(driver)

    def test_raises_when_csrf_key_absent(self):
        data = json.dumps({"props": {"pageProps": {"data": {}}}})
        driver = _make_driver_mock(next_data_json=data)
        with pytest.raises(RuntimeError, match="CSRF token"):
            scraper.extract_csrf_token(driver)

    def test_raises_when_token_value_missing(self):
        data = json.dumps(
            {"props": {"pageProps": {"data": {"csrf": {"parameterName": "_csrf"}}}}}
        )
        driver = _make_driver_mock(next_data_json=data)
        with pytest.raises(RuntimeError, match="CSRF token"):
            scraper.extract_csrf_token(driver)

    def test_token_is_string_and_non_empty(self):
        driver = _make_driver_mock()
        token = scraper.extract_csrf_token(driver)
        assert isinstance(token, str)
        assert len(token) > 0


# ---------------------------------------------------------------------------
# Tests: login (CSRF flow + credential validation)
# ---------------------------------------------------------------------------

class TestLogin:
    """
    Login tests use a patched WebDriverWait so that wait.until() calls
    return immediately without real network activity.
    """

    def _patch_wait(self, driver_mock):
        """Return a context manager that patches WebDriverWait."""
        wait_mock = MagicMock()
        # until() returns the email field mock from find_element
        wait_mock.until.return_value = driver_mock.find_element(
            "by", "j_username"
        )
        return patch("scraper.WebDriverWait", return_value=wait_mock)

    def test_successful_login(self):
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/insight/"
        )
        with self._patch_wait(driver):
            # Should not raise
            scraper.login(driver, "user@example.com", "password123")

        driver.get.assert_called_once_with(scraper.LOGIN_URL)
        # Verify credentials were entered
        email_field = driver.find_element("by", "j_username")
        email_field.send_keys.assert_called_with("user@example.com")

    def test_csrf_token_injected_into_hidden_field(self):
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/insight/"
        )
        with self._patch_wait(driver):
            scraper.login(driver, "user@example.com", "password123")

        # execute_script must have been called to inject the CSRF token
        assert driver.execute_script.called, "CSRF token was not injected via execute_script"
        call_args = driver.execute_script.call_args
        assert VALID_CSRF_TOKEN in str(call_args), (
            "execute_script was called but did not include the CSRF token"
        )

    def test_login_fails_on_error_url(self):
        driver = _make_driver_mock(
            current_url=f"{scraper.ERROR_URL_PREFIX}wrong_password"
        )
        with self._patch_wait(driver):
            with pytest.raises(RuntimeError, match="Login failed"):
                scraper.login(driver, "user@example.com", "wrongpass")

    def test_login_fails_when_url_contains_login(self):
        driver = _make_driver_mock(
            current_url="https://login.bol.com/login?error=blocked"
        )
        with self._patch_wait(driver):
            with pytest.raises(RuntimeError, match="Login failed"):
                scraper.login(driver, "user@example.com", "wrongpass")

    def test_login_navigates_to_login_url(self):
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/"
        )
        with self._patch_wait(driver):
            scraper.login(driver, "user@example.com", "password123")

        driver.get.assert_any_call(scraper.LOGIN_URL)

    def test_submit_button_clicked(self):
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/"
        )
        with self._patch_wait(driver):
            scraper.login(driver, "user@example.com", "password123")

        submit = driver.find_element("by", "submit")
        submit.click.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: report-selection helpers
# ---------------------------------------------------------------------------

class TestClickByText:
    def test_clicks_found_element(self):
        driver = MagicMock()
        element = MagicMock()
        wait = MagicMock()
        wait.until.return_value = element
        with patch("scraper.WebDriverWait", return_value=wait):
            scraper._click_by_text(driver, "Selectie maken", wait)
        element.click.assert_called_once()

    def test_raises_when_not_found(self):
        from selenium.common.exceptions import TimeoutException
        driver = MagicMock()
        wait = MagicMock()
        wait.until.side_effect = TimeoutException("nope")
        with pytest.raises(RuntimeError, match="Selectie maken"):
            scraper._click_by_text(driver, "Selectie maken", wait)

    def test_falls_back_to_js_click(self):
        from selenium.common.exceptions import WebDriverException
        driver = MagicMock()
        element = MagicMock()
        element.click.side_effect = WebDriverException("intercepted")
        wait = MagicMock()
        wait.until.return_value = element
        scraper._click_by_text(driver, "Downloaden", wait)
        driver.execute_script.assert_called_once()


class TestSelectReportCheckbox:
    """
    Tests for _select_reports_via_angular (called via _select_report_checkbox).

    execute_script call order inside _select_reports_via_angular:
      call 0 – diagnostic probe  (always; returns a JSON string or raises)
      call 1 – Angular form patchValue (js_angular)
      call 2 – shadow-DOM click fallback (js_click), only if call 1 didn't succeed
    """

    def _make_wait(self):
        """Return a wait mock whose .until() succeeds (option-group present)."""
        wait = MagicMock()
        wait.until.return_value = MagicMock()
        return wait

    def _probe_result(self):
        """Minimal probe JSON that won't raise."""
        return '{"hostFound":false}'

    # ------------------------------------------------------------------
    # Primary path: Angular form patchValue succeeds
    # ------------------------------------------------------------------

    def test_succeeds_via_angular_form(self):
        """Angular strategy returns 'ok:1' → success, no shadow-DOM call."""
        driver = MagicMock()
        # call 0 = probe, call 1 = angular ok
        driver.execute_script.side_effect = [self._probe_result(), "ok:1"]
        wait = self._make_wait()

        scraper._select_report_checkbox(driver, "Search terms analysis", wait)
        assert driver.execute_script.call_count == 2

    def test_succeeds_via_angular_form_multiple_labels(self):
        """_select_reports_via_angular with 2 labels → ok:2."""
        driver = MagicMock()
        driver.execute_script.side_effect = [self._probe_result(), "ok:2"]
        wait = self._make_wait()

        scraper._select_reports_via_angular(
            driver, ["Publishers", "Search terms analysis"], wait
        )
        assert driver.execute_script.call_count == 2

    # ------------------------------------------------------------------
    # Fallback path: Angular fails → shadow-DOM click
    # ------------------------------------------------------------------

    def test_falls_back_to_shadow_dom_click(self):
        """Angular strategy returns an error → shadow-DOM click is tried."""
        driver = MagicMock()
        # call 0 = probe, call 1 = angular err, call 2 = shadow ok
        driver.execute_script.side_effect = [
            self._probe_result(), "err:no-options", "ok:publishers"
        ]
        wait = self._make_wait()

        scraper._select_report_checkbox(driver, "Publishers", wait)
        assert driver.execute_script.call_count == 3

    def test_raises_when_both_strategies_fail(self):
        """Both Angular and shadow-DOM strategies fail → RuntimeError."""
        driver = MagicMock()
        driver.execute_script.side_effect = [
            self._probe_result(), "err:no-options", "err:none-clicked"
        ]
        wait = self._make_wait()

        with pytest.raises(RuntimeError, match="Publishers"):
            scraper._select_report_checkbox(driver, "Publishers", wait)

    # ------------------------------------------------------------------
    # Wait timeout → RuntimeError before any JS runs
    # ------------------------------------------------------------------

    def test_raises_when_option_group_missing(self):
        """TimeoutException waiting for puik-form-option-group → RuntimeError."""
        from selenium.common.exceptions import TimeoutException
        driver = MagicMock()
        wait = MagicMock()
        wait.until.side_effect = TimeoutException("missing")
        with pytest.raises(RuntimeError, match="option group"):
            scraper._select_report_checkbox(driver, "Some report", wait)

    # ------------------------------------------------------------------
    # Probe failure is non-fatal
    # ------------------------------------------------------------------

    def test_probe_exception_is_non_fatal(self):
        """If the diagnostic probe raises, the main strategy still runs."""
        driver = MagicMock()
        # probe raises, angular returns ok
        driver.execute_script.side_effect = [Exception("probe error"), "ok:1"]
        wait = self._make_wait()

        scraper._select_report_checkbox(driver, "Search terms analysis", wait)
        assert driver.execute_script.call_count == 2

    def test_raises_when_label_missing_in_both(self):
        """Angular and shadow-DOM both return errors → RuntimeError mentions label."""
        driver = MagicMock()
        driver.execute_script.side_effect = [
            self._probe_result(), "err:no-match", "err:none-clicked"
        ]
        wait = self._make_wait()

        with pytest.raises(RuntimeError, match="Missing report"):
            scraper._select_report_checkbox(driver, "Missing report", wait)


class TestWaitForDownloadReadyButton:
    def test_returns_button_when_ready(self):
        driver = MagicMock()
        button = MagicMock()
        button.is_displayed.return_value = True
        button.is_enabled.return_value = True
        driver.find_elements.return_value = [button]
        with patch("scraper.time.sleep"):
            result = scraper.wait_for_download_ready_button(driver, timeout=30)
        assert result is button

    def test_raises_timeout_when_never_ready(self):
        driver = MagicMock()
        driver.find_elements.return_value = []
        with patch("scraper.time.sleep"):
            with patch("scraper.time.time", side_effect=[0, 1, 200]):
                with pytest.raises(TimeoutError, match="Downloaden"):
                    scraper.wait_for_download_ready_button(driver, timeout=5)


# ---------------------------------------------------------------------------
# Tests: wait_for_download
# ---------------------------------------------------------------------------

class TestWaitForDownload:
    def test_returns_path_when_file_appears(self, tmp_path):
        xlsx = tmp_path / "report.xlsx"
        # Simulate the file appearing after the first poll by writing it
        # before the second iteration using a fake sleep that writes the file.
        call_count = 0

        def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                xlsx.write_bytes(b"PK")  # minimal ZIP/XLSX magic bytes

        with patch("scraper.time.sleep", side_effect=fake_sleep):
            result = scraper.wait_for_download(tmp_path, timeout=30)

        assert result == xlsx

    def test_raises_timeout_when_no_file(self, tmp_path):
        with patch("scraper.time.sleep"):
            with patch("scraper.time.time", side_effect=[0, 1, 200]):  # instant timeout
                with pytest.raises(TimeoutError, match="No download"):
                    scraper.wait_for_download(tmp_path, timeout=5)

    def test_ignores_crdownload_partial_files(self, tmp_path):
        partial = tmp_path / "report.xlsx.crdownload"
        partial.write_bytes(b"partial")

        # Make sure it's not picked up as complete
        tick = 0

        def fake_time():
            nonlocal tick
            tick += 1
            return tick

        with patch("scraper.time.sleep"):
            with patch("scraper.time.time", side_effect=list(range(200))):
                with pytest.raises(TimeoutError):
                    scraper.wait_for_download(tmp_path, timeout=5)


# ---------------------------------------------------------------------------
# Tests: _parse_weeks
# ---------------------------------------------------------------------------

class TestParseWeeks:
    def test_single_week(self):
        assert scraper._parse_weeks("18") == [18]

    def test_multiple_weeks_sorted_deduplicated(self):
        assert scraper._parse_weeks("20,18,19,18") == [18, 19, 20]

    def test_strips_spaces(self):
        assert scraper._parse_weeks(" 18 , 19 ") == [18, 19]

    def test_raises_on_non_integer(self):
        with pytest.raises(argparse.ArgumentTypeError, match="valid week number"):
            scraper._parse_weeks("18,foo")

    def test_raises_on_empty_string(self):
        with pytest.raises(argparse.ArgumentTypeError, match="[Aa]t least one"):
            scraper._parse_weeks("  ")


# ---------------------------------------------------------------------------
# Tests: _weeks_to_date_range
# ---------------------------------------------------------------------------

class TestWeeksToDateRange:
    def test_single_week(self):
        # ISO week 20 of 2026: Mon 2026-05-11 → Sun 2026-05-17
        start, end = scraper._weeks_to_date_range([20], year=2026)
        assert start == datetime.date(2026, 5, 11)
        assert end   == datetime.date(2026, 5, 17)
        assert start.weekday() == 0  # Monday
        assert end.weekday()   == 6  # Sunday

    def test_contiguous_weeks(self):
        # Weeks 18–20 of 2026: Mon 2026-04-27 → Sun 2026-05-17
        start, end = scraper._weeks_to_date_range([18, 19, 20], year=2026)
        assert start == datetime.date(2026, 4, 27)
        assert end   == datetime.date(2026, 5, 17)

    def test_non_consecutive_weeks_span(self):
        # Weeks 18 and 21: gaps are included in the span
        start, end = scraper._weeks_to_date_range([18, 21], year=2026)
        assert start == datetime.date(2026, 4, 27)  # Mon of week 18
        assert end   == datetime.date(2026, 5, 24)  # Sun of week 21

    def test_order_of_input_does_not_matter(self):
        a = scraper._weeks_to_date_range([20, 18, 19], year=2026)
        b = scraper._weeks_to_date_range([18, 19, 20], year=2026)
        assert a == b

    def test_defaults_to_current_year(self):
        today = datetime.date.today()
        current_year = today.isocalendar()[0]
        start, end = scraper._weeks_to_date_range([1])
        assert start == datetime.date.fromisocalendar(current_year, 1, 1)
        assert end   == datetime.date.fromisocalendar(current_year, 1, 7)

    def test_raises_on_empty_list(self):
        with pytest.raises(ValueError, match="[Aa]t least one"):
            scraper._weeks_to_date_range([])

    def test_raises_on_week_zero(self):
        with pytest.raises(ValueError, match="out of range"):
            scraper._weeks_to_date_range([0], year=2026)

    def test_raises_on_week_54(self):
        with pytest.raises(ValueError, match="out of range"):
            scraper._weeks_to_date_range([54], year=2026)

    def test_raises_on_week_53_in_short_year(self):
        # 2021 only has 52 ISO weeks; week 53 does not exist
        with pytest.raises(ValueError, match="does not exist"):
            scraper._weeks_to_date_range([53], year=2021)


# ---------------------------------------------------------------------------
# Tests: _validate_date_range
# ---------------------------------------------------------------------------

class TestValidateDateRange:
    def test_valid_monday_to_sunday(self):
        # 2026-05-18 is a Monday, 2026-05-24 is a Sunday
        scraper._validate_date_range(
            datetime.date(2026, 5, 18),
            datetime.date(2026, 5, 24),
        )  # should not raise

    def test_valid_multi_week(self):
        # Mon 2026-05-11 → Sun 2026-05-24
        scraper._validate_date_range(
            datetime.date(2026, 5, 11),
            datetime.date(2026, 5, 24),
        )

    def test_raises_when_start_not_monday(self):
        with pytest.raises(ValueError, match="Monday"):
            scraper._validate_date_range(
                datetime.date(2026, 5, 19),  # Tuesday
                datetime.date(2026, 5, 24),
            )

    def test_raises_when_end_not_sunday(self):
        with pytest.raises(ValueError, match="Sunday"):
            scraper._validate_date_range(
                datetime.date(2026, 5, 18),
                datetime.date(2026, 5, 23),  # Saturday
            )

    def test_raises_when_start_after_end(self):
        with pytest.raises(ValueError, match="before"):
            scraper._validate_date_range(
                datetime.date(2026, 5, 25),  # Monday after the Sunday
                datetime.date(2026, 5, 24),
            )


# ---------------------------------------------------------------------------
# Tests: _set_date_range
# ---------------------------------------------------------------------------

class TestSetDateRange:
    def _make_wait(self):
        wait = MagicMock()
        wait.until.return_value = MagicMock()
        return wait

    def test_dispatches_puik_change_event(self):
        """execute_script is called with 6 date-component integers and returns 'ok'."""
        driver = MagicMock()
        driver.execute_script.return_value = "ok"
        wait = self._make_wait()

        scraper._set_date_range(
            driver,
            datetime.date(2026, 5, 18),
            datetime.date(2026, 5, 24),
            wait,
        )
        driver.execute_script.assert_called_once()
        # Args: js_snippet, sy, sm, sd, ey, em, ed
        call_args = driver.execute_script.call_args[0]
        assert call_args[1] == 2026 and call_args[2] == 5  and call_args[3] == 18  # start
        assert call_args[4] == 2026 and call_args[5] == 5  and call_args[6] == 24  # end

    def test_raises_when_element_missing(self):
        """execute_script returns an error string → RuntimeError."""
        driver = MagicMock()
        driver.execute_script.return_value = "err:no-element"
        wait = self._make_wait()

        with pytest.raises(RuntimeError, match="date range"):
            scraper._set_date_range(
                driver,
                datetime.date(2026, 5, 18),
                datetime.date(2026, 5, 24),
                wait,
            )

    def test_raises_when_date_picker_not_found_in_dom(self):
        """TimeoutException waiting for puik-form-date → RuntimeError."""
        from selenium.common.exceptions import TimeoutException
        driver = MagicMock()
        wait = MagicMock()
        wait.until.side_effect = TimeoutException("missing")

        with pytest.raises(RuntimeError, match="date-range picker"):
            scraper._set_date_range(
                driver,
                datetime.date(2026, 5, 18),
                datetime.date(2026, 5, 24),
                wait,
            )


# ---------------------------------------------------------------------------
# Tests: download_search_terms_report
# ---------------------------------------------------------------------------

class TestDownloadSearchTermsReport:
    def test_runs_full_wizard_and_downloads(self, tmp_path):
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/insight/"
        )
        zip_file = tmp_path / "reports.zip"
        download_button = MagicMock()

        with patch("scraper.WebDriverWait", return_value=MagicMock(until=MagicMock())):
            with patch("scraper.navigate_to_insight"):
                with patch("scraper._click_by_text") as mock_click:
                    with patch("scraper._select_report_checkbox") as mock_select:
                        with patch("scraper._set_date_range") as mock_date:
                            with patch(
                                "scraper.wait_for_download_ready_button",
                                return_value=download_button,
                            ):
                                with patch(
                                    "scraper.wait_for_download", return_value=zip_file
                                ):
                                    result = scraper.download_search_terms_report(
                                        driver, tmp_path
                                    )

        assert result == zip_file
        # Selectie maken + Download gereed maken => two _click_by_text calls.
        assert mock_click.call_count == 2
        # One checkbox per required report.
        assert mock_select.call_count == len(scraper.REQUIRED_REPORT_LABELS)
        # No date range set when omitted.
        mock_date.assert_not_called()
        download_button.click.assert_called_once()

    def test_sets_date_range_when_provided(self, tmp_path):
        """When start/end dates are passed, _set_date_range is called once."""
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/insight/"
        )
        zip_file = tmp_path / "reports.zip"
        start = datetime.date(2026, 5, 18)
        end   = datetime.date(2026, 5, 24)

        with patch("scraper.WebDriverWait", return_value=MagicMock(until=MagicMock())):
            with patch("scraper.navigate_to_insight"):
                with patch("scraper._click_by_text"):
                    with patch("scraper._select_report_checkbox"):
                        with patch("scraper._set_date_range") as mock_date:
                            with patch(
                                "scraper.wait_for_download_ready_button",
                                return_value=MagicMock(),
                            ):
                                with patch(
                                    "scraper.wait_for_download", return_value=zip_file
                                ):
                                    scraper.download_search_terms_report(
                                        driver, tmp_path,
                                        start_date=start, end_date=end,
                                    )

        mock_date.assert_called_once()
        call_kwargs = mock_date.call_args
        assert call_kwargs[0][1] == start
        assert call_kwargs[0][2] == end

    def test_raises_when_only_start_date_given(self, tmp_path):
        """Providing start_date without end_date raises ValueError immediately."""
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/insight/"
        )
        with pytest.raises(ValueError, match="both"):
            scraper.download_search_terms_report(
                driver, tmp_path,
                start_date=datetime.date(2026, 5, 18),
            )

    def test_saves_debug_page_when_selection_fails(self, tmp_path):
        driver = _make_driver_mock(
            current_url="https://portal.bol.com/supplier/insight/"
        )

        with patch("scraper.WebDriverWait", return_value=MagicMock(until=MagicMock())):
            with patch("scraper.navigate_to_insight"):
                with patch(
                    "scraper._click_by_text",
                    side_effect=RuntimeError("Could not find 'Selectie maken'"),
                ):
                    with pytest.raises(RuntimeError, match="Selectie maken"):
                        scraper.download_search_terms_report(driver, tmp_path)

        assert (tmp_path / "debug_insight_page.html").exists()


# ---------------------------------------------------------------------------
# Tests: run (integration-level, all I/O mocked)
# ---------------------------------------------------------------------------

class TestRun:
    def test_full_run_calls_login_and_download(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOL_PSWD", "testpassword")

        fake_report = tmp_path / "report.xlsx"
        fake_report.write_bytes(b"PK")

        with patch("scraper.build_driver") as mock_build:
            mock_driver = _make_driver_mock(
                current_url="https://portal.bol.com/supplier/"
            )
            mock_build.return_value = mock_driver

            with patch("scraper.login") as mock_login:
                with patch(
                    "scraper.download_search_terms_report", return_value=fake_report
                ) as mock_dl:
                    result = scraper.run(
                        output_dir=tmp_path,
                        headless=True,
                        email="CB-Admin@lannoomeulenhoff.nl",
                        password="testpassword",
                    )

        mock_login.assert_called_once_with(
            mock_driver, "CB-Admin@lannoomeulenhoff.nl", "testpassword"
        )
        mock_dl.assert_called_once()
        mock_driver.quit.assert_called_once()
        assert result == fake_report

    def test_driver_quit_called_even_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOL_PSWD", "testpassword")

        with patch("scraper.build_driver") as mock_build:
            mock_driver = MagicMock()
            mock_build.return_value = mock_driver

            with patch("scraper.login", side_effect=RuntimeError("login error")):
                with pytest.raises(RuntimeError, match="login error"):
                    scraper.run(
                        output_dir=tmp_path,
                        email="test@test.com",
                        password="pw",
                    )

        mock_driver.quit.assert_called_once()
