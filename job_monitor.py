#!/usr/bin/env python3
"""
job_monitor.py
Checks Cisco and Cognizant career sites for active SWE / AI-engineer roles
and reports only NEW postings since the last run.

Both careers.cisco.com and careers.cognizant.com run on the Phenom People
platform, which serves results from a JSON search endpoint (the "widgets"
or "search-results" API). This script queries that endpoint for each
company, filters by keyword, and diffs against a local seen-jobs file so
you only get alerted about postings you haven't seen before.

Free to run:
  - Locally:        python job_monitor.py
  - On a schedule:  free GitHub Actions cron (see job-monitor.yml)

Dependencies (all free / open source):
  pip install aiohttp
"""

import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Keywords that mark a role as relevant. Case-insensitive substring match
# against the job title. Tune freely.
KEYWORDS = [
    "software engineer",
    "ai engineer",
    "machine learning",
    "ml engineer",
    "associate software",
    "new grad",
    "early in career",
    "graduate",
    "developer",
]

# Only surface roles that look entry-level. Set to False to see everything.
ENTRY_LEVEL_ONLY = True
ENTRY_LEVEL_HINTS = [
    "new grad", "new graduate", "early in career", "entry", "associate",
    "graduate", "university", "campus", "jr", "junior", "0-1", "i ",
]

# Only surface roles based in the United States. The Phenom feed returns
# global postings even with country="us", so we filter per-job below.
US_ONLY = True
US_MARKERS = ["united states", "united states of america", "u.s.", "usa"]

SEEN_FILE = "seen_jobs.json"

# ---------------------------------------------------------------------------
# PHENOM SEARCH ENDPOINTS
#
# These are the standard Phenom People search hosts for each company. The
# request shape below matches Phenom's "refineSearch" widget call. If a
# company tweaks their payload, do the 30-second confirm step in the README
# (DevTools -> Network -> filter "widgets" -> copy the request).
# ---------------------------------------------------------------------------

COMPANIES = {
    "Cisco": {
        "host": "https://careers.cisco.com",
        "endpoint": "https://careers.cisco.com/widgets",
        "country": "us",
    },
    # NOTE: careers.cognizant.com sits behind Cloudflare bot-protection, which
    # returns HTTP 403 to any scripted/datacenter request (including GitHub
    # Actions runners). The payload below is the correct Phenom shape, so this
    # works from a real browser session, but in CI it will be blocked and the
    # script skips it gracefully (Cisco still runs). To actually cover a second
    # company, swap this for a Workday/Greenhouse/Lever-backed careers site.
    "Cognizant": {
        "host": "https://careers.cognizant.com",
        "endpoint": "https://careers.cognizant.com/widgets",
        "country": "us",
    },
}


def build_payload(keyword: str, country: str, start: int = 0, size: int = 50) -> dict:
    """Standard Phenom 'refineSearch' search payload."""
    return {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": country,
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "sortBy": "Most recent",
        "subsearch": "",
        "from": start,
        "jobs": True,
        "counts": True,
        "all_fields": ["country", "state", "city", "category", "type"],
        "size": size,
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "pageId": "page",
        "siteType": "external",
        "keywords": keyword,
        "global": True,
        "selected_fields": {},
        "locationData": {},
    }


HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


# ---------------------------------------------------------------------------
# CORE
# ---------------------------------------------------------------------------

def is_us(job: dict) -> bool:
    """
    True if the posting is US-based. Phenom gives a per-job `country` plus a
    `multi_location` list (a single req can span several cities), so we treat a
    job as US if any of those signals point to the United States.
    """
    haystacks = [
        job.get("country", ""),
        job.get("cityStateCountry", ""),
        job.get("location", ""),
    ]
    haystacks += job.get("multi_location") or []
    blob = " ".join(str(h) for h in haystacks).lower()
    return any(m in blob for m in US_MARKERS)


def is_relevant(title: str) -> bool:
    t = title.lower()
    if not any(k in t for k in KEYWORDS):
        return False
    if ENTRY_LEVEL_ONLY and not any(h in t for h in ENTRY_LEVEL_HINTS):
        return False
    return True


def parse_jobs(company: str, data: dict) -> list[dict]:
    """
    Pull jobs out of a Phenom response. Phenom nests the list under
    refineSearch -> data -> jobs, but layouts vary slightly, so we search
    defensively for the jobs array.
    """
    jobs = []

    def find_jobs(node):
        if isinstance(node, dict):
            if "jobs" in node and isinstance(node["jobs"], list):
                return node["jobs"]
            for v in node.values():
                found = find_jobs(v)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = find_jobs(item)
                if found:
                    return found
        return None

    raw = find_jobs(data) or []
    for j in raw:
        title = j.get("title") or j.get("name") or ""
        if not title:
            continue
        job_id = str(j.get("jobId") or j.get("id") or j.get("jobSeqNo") or title)
        url = j.get("applyUrl") or j.get("canonicalUrl") or j.get("url") or ""
        location = j.get("cityState") or j.get("location") or j.get("city") or ""
        jobs.append({
            "company": company,
            "id": f"{company}:{job_id}",
            "title": title.strip(),
            "location": location,
            "url": url,
            # Location signals retained so we can filter US-only roles.
            "country": j.get("country", ""),
            "cityStateCountry": j.get("cityStateCountry", ""),
            "multi_location": j.get("multi_location") or [],
        })
    return jobs


async def fetch_company(session: aiohttp.ClientSession, company: str, cfg: dict) -> list[dict]:
    collected: dict[str, dict] = {}
    for keyword in set(KEYWORDS):
        payload = build_payload(keyword, cfg["country"])
        try:
            async with session.post(cfg["endpoint"], json=payload, headers=HEADERS, timeout=30) as resp:
                if resp.status != 200:
                    print(f"  [{company}] '{keyword}' -> HTTP {resp.status}", file=sys.stderr)
                    continue
                data = await resp.json(content_type=None)
        except Exception as e:
            print(f"  [{company}] '{keyword}' -> error: {e}", file=sys.stderr)
            continue

        for job in parse_jobs(company, data):
            if not is_relevant(job["title"]):
                continue
            if US_ONLY and not is_us(job):
                continue
            collected[job["id"]] = job

    return list(collected.values())


def load_seen() -> set[str]:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(ids: set[str]) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


async def main() -> None:
    print(f"Job check at {datetime.now().isoformat(timespec='seconds')}\n")
    seen = load_seen()

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[
            fetch_company(session, company, cfg)
            for company, cfg in COMPANIES.items()
        ])

    all_jobs = [job for company_jobs in results for job in company_jobs]
    new_jobs = [j for j in all_jobs if j["id"] not in seen]

    if not all_jobs:
        print("No jobs returned. The Phenom payload may need confirming "
              "(see the DevTools step in the README).")
        return

    if new_jobs:
        print(f"{len(new_jobs)} NEW role(s):\n")
        for j in sorted(new_jobs, key=lambda x: (x["company"], x["title"])):
            print(f"  [{j['company']}] {j['title']}")
            if j["location"]:
                print(f"       {j['location']}")
            if j["url"]:
                print(f"       {j['url']}")
            print()
        # notify_hook(new_jobs)   # plug in email / Slack here if you want
    else:
        print(f"No new roles. ({len(all_jobs)} relevant roles still open.)")

    save_seen(seen | {j["id"] for j in all_jobs})


if __name__ == "__main__":
    asyncio.run(main())
