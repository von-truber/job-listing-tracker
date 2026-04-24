"""
diagnose.py
-----------
Temporary diagnostic script. Run this once via GitHub Actions to see
exactly what each platform's API returns on that network.
Remove this file from the repository after diagnosis is complete.
"""

import requests
import json

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

tests = [
    {
        "label": "Workable v3 API (Constructor - Regional Manager)",
        "url": "https://apply.workable.com/api/v3/accounts/constructor-1/jobs/651B82F614",
    },
    {
        "label": "Workable widget API (Constructor - all jobs)",
        "url": "https://apply.workable.com/api/v1/widget/accounts/constructor-1/jobs?details=true",
    },
    {
        "label": "Workable page direct fetch (Constructor - Regional Manager)",
        "url": "https://apply.workable.com/constructor-1/j/651B82F614/",
    },
    {
        "label": "Concentrix R-ID page fetch",
        "url": "https://jobs.concentrix.com/job/?id=R1724252",
    },
    {
        "label": "Concentrix numeric API (known working)",
        "url": "https://jobs.concentrix.com/api/jobs/2390497519",
    },
]

for test in tests:
    print(f"\n{'='*60}")
    print(f"TEST : {test['label']}")
    print(f"URL  : {test['url']}")
    try:
        r = requests.get(test["url"], headers=headers, timeout=15, allow_redirects=True)
        print(f"HTTP : {r.status_code}")
        print(f"SIZE : {len(r.text)} chars")

        # Try to parse as JSON first
        try:
            data = r.json()
            # Print first 600 chars of formatted JSON
            print(f"JSON : {json.dumps(data, indent=2)[:600]}")
        except Exception:
            # Not JSON: print first 600 chars of raw text
            sample = r.text[:600].replace("\n", " ").strip()
            print(f"TEXT : {sample}")

    except Exception as exc:
        print(f"ERR  : {exc}")

print(f"\n{'='*60}")
print("Diagnosis complete.")
