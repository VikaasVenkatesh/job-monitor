# Job Monitor

A tiny, free job-posting watcher. It checks the **Cisco** and **Cognizant**
career sites for active SWE / AI-engineer roles and reports only the **new**
postings it hasn't seen before.

Both `careers.cisco.com` and `careers.cognizant.com` run on the
[Phenom People](https://www.phenom.com/) platform, which serves results from a
JSON search endpoint. The script queries that endpoint for each company,
filters titles by keyword (entry-level by default), and diffs against a local
`seen_jobs.json` file so you only get alerted about roles you haven't seen yet.

## Run it locally

```bash
pip install -r requirements.txt
python job_monitor.py
```

The first run prints every relevant role it finds and writes `seen_jobs.json`.
Subsequent runs print only roles that are new since the last run.

## Run it on a schedule (free)

The included GitHub Actions workflow
([`.github/workflows/job-monitor.yml`](.github/workflows/job-monitor.yml))
runs the check on a weekday cron and commits the updated `seen_jobs.json` back
to the repo, so each run's output shows up in the Actions log. You can also
trigger it manually from the **Actions** tab (it has `workflow_dispatch`
enabled).

## Tuning

Edit the config block near the top of [`job_monitor.py`](job_monitor.py):

- `KEYWORDS` — title substrings that mark a role as relevant.
- `ENTRY_LEVEL_ONLY` / `ENTRY_LEVEL_HINTS` — set `ENTRY_LEVEL_ONLY = False` to
  see every matching role, not just entry-level ones.
- `COMPANIES` — the Phenom search hosts/endpoints to query.

### If a company returns nothing

Phenom occasionally tweaks its request payload. If a company stops returning
results, confirm the live request: open the careers site, open DevTools →
**Network**, filter for `widgets`, run a search, and copy the request shape
into `build_payload`.
