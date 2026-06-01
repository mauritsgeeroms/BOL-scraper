# BOL.com Supplier Insight Scraper

Automates the bulk-export flow on the BOL.com supplier portal. Logs in, opens the selection dialog, ticks the required report checkboxes, optionally sets a date range, starts generation, and downloads the resulting ZIP file.

## Requirements

- Python 3.11+
- Google Chrome (the matching ChromeDriver is downloaded automatically)

```
pip install -r requirements.txt
```

## Configuration

**Email** — put your supplier portal e-mail address in `credentials.txt` (first non-blank line):

```
your-email@example.com
```

**Password** — set the environment variable `BOL_PSWD`:

```bash
# Linux / macOS
export BOL_PSWD="your_password"

# Windows CMD
set BOL_PSWD=your_password

# Windows PowerShell
$env:BOL_PSWD = "your_password"
```

## Usage

```
python scraper.py [options]
```

| Option | Default | Description |
|---|---|---|
| `--output-dir PATH` | `./reports` | Directory where the ZIP is saved |
| `--weeks N[,N…]` | *(portal default)* | ISO week numbers to download, e.g. `20` or `18,19,20` |
| `--headed` | *(headless)* | Show the Chrome window (useful for debugging) |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

### Examples

Download the most recent data (portal picks the date range):

```
python scraper.py
```

Download a specific week:

```
python scraper.py --weeks 20
```

Download several weeks at once (the scraper spans from Monday of the earliest week to Sunday of the latest):

```
python scraper.py --weeks 18,19,20
```

Show the browser window while running:

```
python scraper.py --weeks 20 --headed
```

Save to a custom directory:

```
python scraper.py --weeks 20 --output-dir C:\data\bol-reports
```

## Week numbers

`--weeks` accepts one or more comma-separated ISO week numbers for the **current calendar year**. Non-consecutive weeks (e.g. `--weeks 18,21`) are allowed — the scraper uses the Monday of the earliest week as the start date and the Sunday of the latest week as the end date, covering all weeks in between.

The portal only provides data for completed (Mon–Sun) weeks, so very recent weeks may not be available yet.

## Changing which reports are downloaded

The set of reports that get selected in the dialog is controlled by `REQUIRED_REPORT_LABELS` at the top of `scraper.py`:

```python
REQUIRED_REPORT_LABELS = [
    "Commerciële rapportage: Publishers",
    "Product visits en conversie benchmark rapportage",
    "Product visits en conversie rapportage",
    "Search terms analysis - top 5 per product",
]
```

Each string is matched **case-insensitively as a substring** against the report labels shown in the portal dialog. To add or remove a report:

1. Run the scraper with `--headed` to open the browser.
2. Click *Selectie maken* to open the dialog and note the exact label text of the report you want.
3. Add the label (or a unique substring of it) to `REQUIRED_REPORT_LABELS`, or remove an existing entry.

The full list of available reports can also be seen in the `allOptionLabels` field of the JS probe logged at `INFO` level during each run.

## Output

The downloaded file is a ZIP archive containing one XLSX per selected report. It is saved to `--output-dir` with the filename assigned by the portal (a UUID). The path is printed to stdout on success:

```
Report downloaded: reports\4076d7a5-8496-4db8-beb8-e2d05a633182.zip
```

## Logs

Logs are written to `logs/scraper.log` (rotating, max 1 MB, 5 backups kept) and to the console. Use `--log-level DEBUG` to see every Selenium request and the JS probe output, which is helpful when the portal layout changes.

## Running the tests

```
pytest tests/ -v
```
