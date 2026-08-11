"""
job_tracker.py
--------------
Reads opportunity listings from a Google Sheet, checks whether each listing
URL is still active, writes status back to the sheet, and sends a Gmail
alert for any listings that have changed status since the last run.

Sheet column layout (1-based):
  1  Name of poster      — read-only, ignored by this script
  2  Type of opportunity — read-only, used for logging and alert emails
  3  Platform            — read-only, ignored (internal posting platform)
  4  Company             — read-only, used for logging and alert emails
  5  Date of posting     — read-only, never written by this script
  6  Actual Link         — read-only, the URL this script checks
  7  Last Checked        — written by this script
  8  Status              — written by this script
  9  Notes               — written by this script

Detection strategy:
  - Workable: fetches the listing page directly. 404 = Inactive. 200 with
    no closed-role signal phrases = Active. Title matching removed because
    the sheet has no job title column.
  - Ashby: queries the public job board API and checks whether the job ID
    is present in the list of open postings.
  - Breezy HR: queries the company public /json endpoint and checks
    whether the position ID is present in the open positions list.
  - Concentrix: HTTP status only. Job content is JavaScript-rendered so
    text-based signals are unreliable. 200 = Active, 404 = Inactive.
  - Generic: HTTP status + apply keyword presence in page body.
    Title matching removed because the sheet has no job title column.
"""

import re
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
import requests
from bs4 import BeautifulSoup
from config import (
    ALERT_EMAIL,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    SERVICE_ACCOUNT_FILE,
    SHEET_ID,
)
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Column positions (0-based)
# A=0  B=1  C=2  D=3  E=4  F=5  G=6  H=7  I=8
# ---------------------------------------------------------------------------
COL_POSTER       = 0   # Name of poster      — not used
COL_TYPE         = 1   # Type of opportunity — used for logging
COL_PLATFORM     = 2   # Internal platform   — not used
COL_COMPANY      = 3   # Company name
COL_DATE_POSTED  = 4   # Date of posting     — read-only, never written
COL_URL          = 5   # Actual Link
COL_LAST_CHECKED = 6   # Written by this script → column G
COL_STATUS       = 7   # Written by this script → column H
COL_NOTES        = 8   # Written by this script → column I

# Pad every row to this width so column index lookups never raise IndexError
MIN_COLS = 9

# Phrases injected server-side when a listing is explicitly closed.
# Used by the Workable checker.
CLOSED_SIGNALS = [
    "job not found",
    "position not found",
    "no longer available",
    "this job has expired",
    "job has been filled",
    "posting has been removed",
    "this position has been filled",
    "no longer accepting",
    "this job is not available",
    "sorry, this job",
]

# Keywords that suggest an apply button or form is still present.
# Used by the generic checker as the primary activity signal.
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
# Google Sheets helper
# ---------------------------------------------------------------------------


def get_sheet():
    """Authenticate with the Google Sheets and Drive APIs and return sheet1."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
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
# Platform-specific checkers
# All checkers return a (status, notes) tuple.
#   status: "Active" | "Inactive" | "Error"
#   notes:  short string written to column I, empty string when not needed
# ---------------------------------------------------------------------------


def check_workable(url: str) -> tuple[str, str]:
    """
    Fetches the Workable listing page directly.

    Title matching has been removed because the sheet has no job title column.
    Detection relies on HTTP status code and closed-role signal phrases.

    Active:   200 response with no closed-role signal in the page text.
    Inactive: 404 response, or 200 with a closed-role signal present.
    Error:    Network failure or unexpected HTTP status.
    """
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

        if r.status_code == 404:
            return "Inactive", "404 response"
        if r.status_code >= 400:
            return "Error", f"HTTP {r.status_code}"

        raw_lower = r.text.lower()
        for signal in CLOSED_SIGNALS:
            if signal in raw_lower:
                return "Inactive", f"Closed signal detected: {signal!r}"

        return "Active", ""

    except requests.exceptions.Timeout:
        return "Error", "Request timed out"
    except requests.exceptions.ConnectionError:
        return "Error", "Connection error"
    except Exception as exc:
        print(f"  Workable check error: {exc}")
        return "Error", str(exc)[:80]


def check_ashby(url: str) -> tuple[str, str]:
    """
    Queries the Ashby public job board API.

    URL pattern : https://jobs.ashbyhq.com/{company}/{job_id}
    API endpoint: https://api.ashbyhq.com/posting-api/job-board/{company}

    The response contains a jobPostings list. If the job ID from the URL
    is present in that list the listing is still open.
    """
    try:
        match = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([^/?]+)", url)
        if not match:
            return "Error", "Could not parse Ashby URL"

        company, job_id = match.group(1), match.group(2)
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
        r = requests.get(api_url, headers=REQUEST_HEADERS, timeout=15)

        if r.status_code == 404:
            return "Inactive", "404 response"
        if r.status_code != 200:
            return "Error", f"HTTP {r.status_code}"

        data = r.json()
        live_ids = [p.get("id", "").lower() for p in data.get("jobPostings", [])]

        if job_id.lower() in live_ids:
            return "Active", ""
        return "Inactive", "Job ID not found in open postings"

    except Exception as exc:
        print(f"  Ashby check error: {exc}")
        return "Error", str(exc)[:80]


def check_breezy(url: str) -> tuple[str, str]:
    """
    Queries the Breezy HR public /json endpoint.

    URL pattern : https://{company}.breezy.hr/p/{position-id}-{slug}
    API endpoint: https://{company}.breezy.hr/json

    The position ID is the leading hex string in the URL path segment.
    We check whether that ID appears in the live positions list.
    """
    try:
        match = re.search(r"https?://([^.]+)\.breezy\.hr/p/([^/]+)", url)
        if not match:
            return "Error", "Could not parse Breezy HR URL"

        company = match.group(1)
        position_slug = match.group(2)

        id_match = re.match(r"([0-9a-f]+)", position_slug, re.IGNORECASE)
        if not id_match:
            return "Error", "Could not extract Breezy position ID"

        position_id = id_match.group(1).lower()
        api_url = f"https://{company}.breezy.hr/json"
        r = requests.get(api_url, headers=REQUEST_HEADERS, timeout=15)

        if r.status_code == 404:
            return "Inactive", "404 response"
        if r.status_code != 200:
            return "Error", f"HTTP {r.status_code}"

        for pos in r.json():
            if position_id in pos.get("_id", "").lower():
                return "Active", ""
            if position_id in pos.get("friendly_id", "").lower():
                return "Active", ""

        return "Inactive", "Position ID not found in open listings"

    except Exception as exc:
        print(f"  Breezy HR check error: {exc}")
        return "Error", str(exc)[:80]


def check_concentrix(url: str) -> tuple[str, str]:
    """
    Fetches the Concentrix listing page and reads the HTTP status code only.

    Concentrix job content is JavaScript-rendered, so text-based signal
    checks are unreliable. The HTTP status code is the only dependable signal.

    Active:   200 response.
    Inactive: 404 response.
    Error:    Network failure or unexpected HTTP status.
    """
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

        if r.status_code == 200:
            return "Active", ""
        if r.status_code == 404:
            return "Inactive", "404 response"
        return "Error", f"HTTP {r.status_code}"

    except requests.exceptions.Timeout:
        return "Error", "Request timed out"
    except requests.exceptions.ConnectionError:
        return "Error", "Connection error"
    except Exception as exc:
        print(f"  Concentrix check error: {exc}")
        return "Error", str(exc)[:80]


def check_generic(url: str) -> tuple[str, str]:
    """
    Fallback checker for URLs not on a known job board platform.

    Title matching has been removed because the sheet has no job title column.
    Detection relies on HTTP status and apply keyword presence in the page body.

    Active:   200 response with at least one apply keyword found in the page.
    Inactive: 404 response, HTTP error, or 200 with no apply keywords found.
    Error:    Network failure or unexpected HTTP status.
    """
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True)

        if r.status_code == 404:
            return "Inactive", "404 response"
        if r.status_code >= 400:
            return "Error", f"HTTP {r.status_code}"

        soup = BeautifulSoup(r.text, "html.parser")
        page_text = soup.get_text(separator=" ").lower()

        for kw in APPLY_KEYWORDS:
            if kw in page_text:
                return "Active", ""

        return "Inactive", "No apply signal found on page"

    except requests.exceptions.Timeout:
        return "Error", "Request timed out"
    except requests.exceptions.ConnectionError:
        return "Error", "Connection error"
    except Exception as exc:
        print(f"  Unexpected error checking {url}: {exc}")
        return "Error", str(exc)[:80]


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------


def check_listing(url: str) -> tuple[str, str]:
    """
    Detect the platform and route to the appropriate checker.
    Returns (status, notes). This is the single entry point called by run().
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
        return check_generic(url)


# ---------------------------------------------------------------------------
# Email alert
# ---------------------------------------------------------------------------


def send_alert(changes: list) -> None:
    """Send a Gmail alert listing every status change detected in this run."""
    if not changes:
        return

    # FIX: normalise ALERT_EMAIL to a list regardless of how it is stored in
    # config.py. The GitHub secret may be a comma-separated string
    # ("a@gmail.com,b@gmail.com") or a Python list. Both are handled here.
    if isinstance(ALERT_EMAIL, list):
        recipients = [e.strip() for e in ALERT_EMAIL if e.strip()]
    else:
        recipients = [e.strip() for e in str(ALERT_EMAIL).split(",") if e.strip()]

    if not recipients:
        print("No valid recipients in ALERT_EMAIL. Skipping email.")
        return

    subject = f"Opportunity Tracker: {len(changes)} listing(s) changed status"

    lines = [
        f"Opportunity Tracker detected {len(changes)} change(s) on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.\n",
        "=" * 50,
    ]

    for item in changes:
        lines += [
            f"\nCompany   : {item['company']}",
            f"Type      : {item['opp_type']}",
            f"URL       : {item['url']}",
            f"Change    : {item['old_status']} -> {item['new_status']}",
            f"Checked   : {item['checked_at']}",
        ]
        if item.get("notes"):
            lines.append(f"Notes     : {item['notes']}")
        lines.append("-" * 50)

    body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())

    print(f"Alert email sent to {len(recipients)} recipient(s) for {len(changes)} change(s).")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run() -> None:
    print(
        f"\n[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] "
        "Opportunity Tracker started."
    )

    sheet = get_sheet()
    all_rows = sheet.get_all_values()

    if len(all_rows) < 2:
        print("Sheet has no data rows. Add some listings and run again.")
        return

    data_rows = all_rows[1:]  # skip header row
    changes = []
    updates = []  # batched sheet writes collected before a single API call
    skipped_invalid = 0

    for i, row in enumerate(data_rows):
        # Pad short rows so column index lookups never raise IndexError
        row = row + [""] * (MIN_COLS - len(row))

        opp_type   = row[COL_TYPE].strip()
        company    = row[COL_COMPANY].strip()
        url        = row[COL_URL].strip()
        old_status = row[COL_STATUS].strip()

        if not url:
            continue  # skip rows with no URL at all

        # FIX: skip rows where the URL column contains non-URL content such as
        # WordPress admin edit links ("Edit: Title — Admin | Site") or plain
        # opportunity titles. These appear in the sheet when interns paste a
        # page title or admin link instead of the actual external URL.
        if not url.startswith(("http://", "https://")):
            print(f"  Skipped [invalid URL]: {company} — {opp_type} ({url[:60]})")
            skipped_invalid += 1
            sheet_row = i + 2
            checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            updates.append(
                {
                    "range": f"G{sheet_row}:I{sheet_row}",
                    "values": [[checked_at, "Error", "Column F does not contain a valid URL"]],
                }
            )
            continue

        platform = detect_platform(url)
        print(f"  Checking [{platform}]: {company} — {opp_type}")

        new_status, notes = check_listing(url)
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Sheet row number is i + 2 (1-based index plus the header row)
        sheet_row = i + 2
        updates.append(
            {
                "range": f"G{sheet_row}:I{sheet_row}",  # Last Checked : Notes
                "values": [[checked_at, new_status, notes]],
            }
        )

        # Only flag a change if there was a previous status to compare against
        if old_status and new_status != old_status:
            changes.append(
                {
                    "company": company,
                    "opp_type": opp_type,
                    "url": url,
                    "old_status": old_status,
                    "new_status": new_status,
                    "notes": notes,
                    "checked_at": checked_at,
                }
            )

        time.sleep(REQUEST_DELAY)

    # Write all Last Checked, Status, and Notes cells in a single batch call
    if updates:
        sheet.batch_update(updates)
        print(f"\nSheet updated: {len(updates)} row(s) written.")

    if skipped_invalid:
        print(f"Skipped {skipped_invalid} row(s) with invalid URLs in column F.")

    print(f"Run complete. {len(changes)} status change(s) detected.")

    if changes:
        send_alert(changes)


if __name__ == "__main__":
    run()