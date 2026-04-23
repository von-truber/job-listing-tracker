# Job Listing Tracker

An automated Python tool that monitors job listing URLs for availability changes and sends email alerts when a listing goes offline or is removed.

Built as a practical job search utility and as a portfolio demonstration of Python scripting, Google Sheets API integration, and GitHub Actions automation.

---

## What It Does

Most job postings do not include a closing date. This tool solves that problem by:

1. Reading a list of job listings from a Google Sheet (company, role, URL)
2. Visiting each URL on a daily schedule
3. Checking two signals to determine whether the listing is still active:
   - **HTTP status check:** a 404 or server error means the page is gone
   - **Keyword presence check:** confirms the job title and/or an apply button still appear in the page body
4. Writing the result (`Active`, `Inactive`, or `Error`) and a timestamp back to the sheet
5. Sending a Gmail alert summarising any listings that changed status since the last run

---

## Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3.11 |
| Sheet data source | Google Sheets API via `gspread` |
| HTTP checking | `requests` |
| Page parsing | `BeautifulSoup4` |
| Email alerts | `smtplib` (Gmail, SSL) |
| Scheduling | GitHub Actions (cron) |
| Credentials management | GitHub Repository Secrets |

---

## Repository Structure

```
job_tracker/
├── job_tracker.py               # Main script
├── config.py                    # Credentials and settings (not committed)
├── requirements.txt             # Python dependencies
├── .github/
│   └── workflows/
│       └── job_tracker.yml      # GitHub Actions workflow
└── README.md
```

---

## How It Works

```
GitHub Actions (daily cron)
        |
        v
  job_tracker.py
        |
        |-- gspread --> Google Sheet (read all rows)
        |
        |-- requests + BeautifulSoup --> check each URL
        |       |
        |       |-- 404 or no keywords found --> "Inactive"
        |       |-- page loads with signals   --> "Active"
        |       └-- connection failure        --> "Error"
        |
        |-- gspread --> Google Sheet (batch write Last Checked + Status)
        |
        └-- smtplib --> Gmail alert (only if any status changed)
```

---

## Google Sheet Structure

| Column | Header | Filled By |
|---|---|---|
| A | Company | You |
| B | Job Title | You |
| C | Job URL | You |
| D | Date Added | You |
| E | Last Checked | Script |
| F | Status | Script |
| G | Notes | You |

---

## Setup Guide

### 1. Clone the repository

```bash
git clone https://github.com/your-username/job-listing-tracker.git
cd job-listing-tracker
```

### 2. Create a Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Sheets API**
4. Navigate to **IAM & Admin > Service Accounts** and create a new service account
5. Under **Keys**, add a new JSON key and download the file
6. Rename the file to `credentials.json`

### 3. Share your Google Sheet with the service account

Copy the `client_email` value from `credentials.json` and share your Google Sheet with it (Viewer access is enough for reading; Editor access is required for the script to write back status columns).

### 4. Add GitHub repository secrets

In your repository: **Settings > Secrets and variables > Actions > New repository secret**

| Secret name | Value |
|---|---|
| `GOOGLE_CREDENTIALS_JSON` | The full contents of `credentials.json` |
| `SHEET_ID` | The ID from your sheet URL |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Your 16-character Gmail App Password |
| `ALERT_EMAIL` | Email address to receive alerts |

To generate a Gmail App Password: Google Account > Security > 2-Step Verification > App Passwords.

### 5. Enable the workflow

Push the repository to GitHub. The workflow runs automatically at 08:00 UTC daily. To trigger it manually, go to **Actions > Job Listing Tracker > Run workflow**.

---

## Running Locally (PythonAnywhere or any machine)

```bash
pip install -r requirements.txt
```

Fill in `config.py` with your credentials, then:

```bash
python job_tracker.py
```

Schedule it on PythonAnywhere via **Tasks > Add a new scheduled task** using the daily option.

---

## Example Alert Email

```
Subject: Job Tracker: 2 listing(s) changed status

Job Tracker detected 2 change(s) on 2025-04-23.

==================================================

Company   : Acme Corp
Role      : Support Engineer (EMEA)
URL       : https://acmecorp.com/careers/support-engineer-emea
Change    : Active -> Inactive
Checked   : 2025-04-23 08:04 UTC
--------------------------------------------------

Company   : Buildco
Role      : Technical Customer Success Manager
URL       : https://buildco.io/jobs/tcsm
Change    : Active -> Inactive
Checked   : 2025-04-23 08:04 UTC
--------------------------------------------------
```

---

## Author

Michael Osborne Teye  
Technical Support Engineer | GitHub: [von-truber](https://github.com/von-truber)
