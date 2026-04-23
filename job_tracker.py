"""
job_tracker.py
--------------
Reads job listings from a Google Sheet, checks whether each listing
is still active, writes status back to the sheet, and sends a Gmail
alert for any listings that have changed status since the last run.

Detection strategy:
  - For known job board platforms (Workable, Ashby, Breezy HR, Concentrix),
    the script queries each platform's background JSON API directly.
    This bypasses the JavaScript-rendering problem that causes `requests`
    to return empty pages on modern Single Page Application job boards.
  - For all other URLs (standard company career pages), the script falls
    back to a two-signal HTML check: HTTP status code + keyword presence.

Schedule this script via PythonAnywhere's task scheduler (daily recommended).
"""

import re
import time
from datetime import datetime

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    ALERT_EMAIL,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    SERVICE_ACCOUNT_FILE,
    SHEET_ID,
)

# ---------------------------------------------------------------------------
# Column positions (0-based, matching sheet order)
# A=0  B=1  C=2  D=3  E=4  F=5  G=6
# ---------------------------------------------------------------------------
COL_COMPANY       = 0
COL_TITLE         = 1
COL_URL           = 2
COL_DATE_ADDED    = 3
COL_LAST_CHECKED  = 4   # written by this script
COL_STATUS        = 5   # written by this script
COL_NOTES         = 6

# Keywords that suggest an apply button or form is still present
APPLY_KEYWORDS = [
    "apply now",
    "apply for this",
    "apply today",
    "submit application",
    "apply to this job",
    "apply to this position",
]

# Mimic a real browser so servers do not block the request
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Seconds to wait between URL requests (be polite to servers)
REQUEST_DELAY = 2


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def get_sheet():
    """Authenticate with the Google Sheets API and return the first sheet."""
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform(url: str) -> str:
    """
    Identify which job board platform a URL belongs to.
    Returns a short string key used to route to the correct checker.
    """
    if "apply.workable.com" in url:
        return "workable"
    if "jobs.ashbyhq.com" in url:
        return "ashby"
    if ".breezy.hr" in url:
        return "breezy"
    if "jobs.concentrix.com" in url:
        return "concentrix"
    return "generic"


# ---------------------------------------------------------------------------
# Platform-specific API checkers
# ---------------------------------------------------------------------------

def check_workable(url: str) -> str:
    """
    Workable exposes a public REST API for individual job listings.
    URL pattern : https://apply.workable.com/{company}/j/{job_id}/
    API endpoint: https://apply.workable.com/api/v3/accounts/{company}/jobs/{job_id}
    The response includes a 'state' field: 'published' = live, anything else = closed.
    """
    try:
        match = re.search(r"apply\.workable\.com/([^/]+)/j/([^/?]+)", url)
        if not match:
            print("  Could not parse Workable URL.")
            return "Error"

        company, job_id = match.group(1), match.group(2)
        api_url = f"https://apply.workable.com/api/v3/accounts/{company}/jobs/{job_id}"

        r = requests.get(api_url, headers=REQUEST_HEADERS, timeout=15)

        if r.status_code == 404:
            return "Inactive"
        if r.status_code != 200:
            return "Error"

        data  = r.json()
        state = data.get("state", "").lower()

        if state == "published":
            return "Active"
        if state in ("closed", "archived", "draft", ""):
            return "Inactive"

        # Unknown state value: treat conservatively as Active to avoid false alarms
        return "Active"

    except Exception as exc:
        print(f"  Workable check error: {exc}")
        return "Error"


def check_ashby(url: str) -> str:
    """
    Ashby exposes a public job board API that returns all open listings for a company.
    URL pattern : https://jobs.ashbyhq.com/{company}/{job_id}
    API endpoint: https://api.ashbyhq.com/posting-api/job-board/{company}
    The response contains a 'jobPostings' list; we check whether our job_id is in it.
    A missing job_id means the listing has been removed or closed.
    """
    try:
        match = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([^/?]+)", url)
        if not match:
            print("  Could not parse Ashby URL.")
            return "Error"

        company, job_id = match.group(1), match.group(2)
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"

        r = requests.get(api_url, headers=REQUEST_HEADERS, timeout=15)

        if r.status_code == 404:
            return "Inactive"
        if r.status_code != 200:
            return "Error"

        data     = r.json()
        postings = data.get("jobPostings", [])
        live_ids = [p.get("id", "").lower() for p in postings]

        if job_id.lower() in live_ids:
            return "Active"

        return "Inactive"

    except Exception as exc:
        print(f"  Ashby check error: {exc}")
        return "Error"


def check_breezy(url: str) -> str:
    """
    Breezy HR exposes a /json endpoint that lists all open positions for a company.
    URL pattern : https://{company}.breezy.hr/p/{position-id}-{slug}/apply
    API endpoint: https://{company}.breezy.hr/json
    The position ID is the leading 12-character hex string in the URL path segment.
    We check whether that ID appears in the live positions list.
    """
    try:
        match = re.search(r"https?://([^.]+)\.breezy\.hr/p/([^/]+)", url)
        if not match:
            print("  Could not parse Breezy HR URL.")
            return "Error"

        company       = match.group(1)
        position_slug = match.group(2)

        # The position ID is the leading hex segment, e.g. "4ae72963384f" from
        # "4ae72963384f-cpt-10850-admin-data-entry-support"
        id_match = re.match(r"([0-9a-f]+)", position_slug, re.IGNORECASE)
        if not id_match:
            print("  Could not extract Breezy position ID from URL.")
            return "Error"

        position_id = id_match.group(1).lower()
        api_url     = f"https://{company}.breezy.hr/json"

        r = requests.get(api_url, headers=REQUEST_HEADERS, timeout=15)

        if r.status_code == 404:
            return "Inactive"
        if r.status_code != 200:
            return "Error"

        positions = r.json()

        for pos in positions:
            if position_id in pos.get("_id", "").lower():
                return "Active"
            if position_id in pos.get("friendly_id", "").lower():
                return "Active"

        return "Inactive"

    except Exception as exc:
        print(f"  Breezy HR check error: {exc}")
        return "Error"


def check_concentrix(url: str) -> str:
    """
    Concentrix uses a custom careers platform.
    URL pattern: https://jobs.concentrix.com/job/?id={job_id}
    We attempt their internal API first, then fall back to scraping
    the page for 'no longer available' or similar closed-role signals.
    """
    try:
        id_match = re.search(r"id=([^&]+)", url)
        if not id_match:
            print("  Could not extract Concentrix job ID from URL.")
            return "Error"

        job_id = id_match.group(1)

        # Attempt internal API endpoint (used by their front-end SPA)
        api_url = f"https://jobs.concentrix.com/api/jobs/{job_id}"
        r = requests.get(api_url, headers=REQUEST_HEADERS, timeout=15)

        if r.status_code == 200:
            return "Active"
        if r.status_code == 404:
            return "Inactive"

        # API did not give a clean answer: fall back to scraping the listing page
        r2 = requests.get(url, headers=REQUEST_HEADERS, timeout=15)

        if r2.status_code == 404:
            return "Inactive"
        if r2.status_code >= 400:
            return "Error"

        page_lower = r2.text.lower()

        closed_signals = [
            "job not found",
            "position not found",
            "no longer available",
            "this job has expired",
            "job has been filled",
            "posting has been removed",
        ]

        if any(signal in page_lower for signal in closed_signals):
            return "Inactive"

        if job_id.lower() in page_lower:
            return "Active"

        return "Error"

    except Exception as exc:
        print(f"  Concentrix check error: {exc}")
        return "Error"


# ---------------------------------------------------------------------------
# Generic checker (fallback for standard company career pages)
# ---------------------------------------------------------------------------

def check_generic(url: str, job_title: str) -> str:
    """
    Fallback for URLs that are not on a known job board platform.
    Uses two signals:
      1. HTTP status code: 404 or server error means the page is gone.
      2. Keyword presence: job title or apply button must appear in page body.
    """
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

        if r.status_code == 404:
            return "Inactive"
        if r.status_code >= 400:
            return "Error"

        soup      = BeautifulSoup(r.text, "html.parser")
        page_text = soup.get_text(separator=" ").lower()

        title_present = job_title.lower() in page_text
        apply_present = any(kw in page_text for kw in APPLY_KEYWORDS)

        if not title_present and not apply_present:
            return "Inactive"

        return "Active"

    except requests.exceptions.Timeout:
        return "Error"
    except requests.exceptions.ConnectionError:
        return "Error"
    except Exception as exc:
        print(f"  Unexpected error checking {url}: {exc}")
        return "Error"


# ---------------------------------------------------------------------------
# Main dispatcher: routes each URL to the correct checker
# ---------------------------------------------------------------------------

def check_listing(url: str, job_title: str) -> str:
    """
    Detect the platform and route to the appropriate checker.
    This is the single entry point called by the main runner.
    """
    platform = detect_platform(url)

    if platform == "workable":
        return check_workable(url)
    elif platform == "ashby":
        return check_ashby(url)
    elif platform == "breezy":
        return check_breezy(url)
    elif platform == "concentrix":
        return check_concentrix(url)
    else:
        return check_generic(url, job_title)


# ---------------------------------------------------------------------------
# Email alert
# ---------------------------------------------------------------------------

def send_alert(changes: list) -> None:
    """Send a Gmail alert listing every status change detected."""
    if not changes:
        return

    subject = f"Job Tracker: {len(changes)} listing(s) changed status"

    lines = [
        f"Job Tracker detected {len(changes)} change(s) on "
        f"{datetime.utcnow().strftime('%Y-%m-%d')}.\n",
        "=" * 50,
    ]

    for item in changes:
        lines += [
            f"\nCompany   : {item['company']}",
            f"Role      : {item['title']}",
            f"URL       : {item['url']}",
            f"Change    : {item['old_status']} -> {item['new_status']}",
            f"Checked   : {item['checked_at']}",
            "-" * 50,
        ]

    body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = ALERT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, ALERT_EMAIL, msg.as_string())

    print(f"Alert email sent for {len(changes)} change(s).")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run() -> None:
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}] Job Tracker started.")

    sheet    = get_sheet()
    all_rows = sheet.get_all_values()

    if len(all_rows) < 2:
        print("Sheet has no data rows. Add some listings and run again.")
        return

    data_rows = all_rows[1:]   # skip header row
    changes   = []
    updates   = []             # batched sheet writes

    for i, row in enumerate(data_rows):
        # Pad row so we never get an IndexError on sparse sheets
        row = row + [""] * (7 - len(row))

        company    = row[COL_COMPANY].strip()
        title      = row[COL_TITLE].strip()
        url        = row[COL_URL].strip()
        old_status = row[COL_STATUS].strip()

        if not url:
            continue  # skip rows with no URL

        platform = detect_platform(url)
        print(f"  Checking [{platform}]: {company} — {title}")

        new_status = check_listing(url, title)
        checked_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        # Sheet row number is i + 2 (1-based index, plus the header row)
        sheet_row = i + 2
        updates.append({
            "range":  f"E{sheet_row}:F{sheet_row}",
            "values": [[checked_at, new_status]],
        })

        # Only flag a change if there was a previous status to compare against
        if old_status and new_status != old_status:
            changes.append({
                "company":    company,
                "title":      title,
                "url":        url,
                "old_status": old_status,
                "new_status": new_status,
                "checked_at": checked_at,
            })

        time.sleep(REQUEST_DELAY)

    # Write all Last Checked and Status cells in a single batch call
    if updates:
        sheet.batch_update(updates)
        print(f"\nSheet updated: {len(updates)} row(s) written.")

    print(f"Run complete. {len(changes)} status change(s) detected.")

    if changes:
        send_alert(changes)


if __name__ == "__main__":
    run()
