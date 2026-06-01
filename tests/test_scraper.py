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

import json
import os
import pathlib
import time
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest

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
# Tests: find_search_terms_report_link
# ---------------------------------------------------------------------------

class TestFindSearchTermsReportLink:
    def _patched_wait(self):
        wait_mock = MagicMock()
        wait_mock.until.return_value = None
        return patch("scraper.WebDriverWait", return_value=wait_mock)

    def test_finds_xlsx_href(self):
        driver = _make_driver_mock(
            anchors=[{"href": "https://portal.bol.com/reports/search_terms.xlsx", "text": ""}]
        )
        with self._patched_wait():
            link = scraper.find_search_terms_report_link(driver)
        assert link == "https://portal.bol.com/reports/search_terms.xlsx"

    def test_finds_download_href(self):
        driver = _make_driver_mock(
            anchors=[{"href": "https://portal.bol.com/download/report", "text": ""}]
        )
        with self._patched_wait():
            link = scraper.find_search_terms_report_link(driver)
        assert link == "https://portal.bol.com/download/report"

    def test_finds_by_zoektermen_text(self):
        driver = _make_driver_mock(
            anchors=[{"href": "https://portal.bol.com/some/path", "text": "Zoektermen analyse"}]
        )
        with self._patched_wait():
            link = scraper.find_search_terms_report_link(driver)
        assert link == "https://portal.bol.com/some/path"

    def test_finds_download_button_and_clicks_it(self):
        driver = _make_driver_mock(
            anchors=[],
            buttons=[{"text": "Download zoektermen", "aria_label": ""}],
        )
        with self._patched_wait():
            with patch("scraper.time.sleep"):
                link = scraper.find_search_terms_report_link(driver)
        assert link == "__button_clicked__"

    def test_returns_none_when_nothing_found(self):
        driver = _make_driver_mock(anchors=[], buttons=[])
        with self._patched_wait():
            link = scraper.find_search_terms_report_link(driver)
        assert link is None


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
                with pytest.raises(TimeoutError, match="No XLSX file"):
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
# Tests: download_search_terms_report
# ---------------------------------------------------------------------------

class TestDownloadSearchTermsReport:
    def test_navigates_and_downloads(self, tmp_path):
        driver = _make_driver_mock(
            anchors=[{"href": "https://portal.bol.com/reports/zoektermen.xlsx", "text": ""}]
        )
        xlsx_file = tmp_path / "zoektermen.xlsx"

        with patch("scraper.WebDriverWait", return_value=MagicMock(until=MagicMock())):
            with patch("scraper.wait_for_download", return_value=xlsx_file):
                result = scraper.download_search_terms_report(driver, tmp_path)

        assert result == xlsx_file
        driver.get.assert_any_call(scraper.SUPPLIER_INSIGHT_URL)

    def test_raises_when_link_not_found(self, tmp_path):
        driver = _make_driver_mock(anchors=[], buttons=[])

        with patch("scraper.WebDriverWait", return_value=MagicMock(until=MagicMock())):
            with pytest.raises(RuntimeError, match="not found on the insight page"):
                scraper.download_search_terms_report(driver, tmp_path)

        # debug page should have been written
        assert (tmp_path / "debug_page.html").exists()


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
