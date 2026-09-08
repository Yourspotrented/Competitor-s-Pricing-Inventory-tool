# Competitors Data Finder

Scans SpotHero and ParkWhiz for our active ReachPro events and flags where
competitor inventory is **limited** or **sold out**. When a competitor is
running low on a lot near an event we sell, that's a demand signal — we can
likely raise our ReachPro price on that event before we sell out too cheap.

Results are shown on a dashboard, sorted sold-out-first.

## Why this exists

Instead of manually checking SpotHero/ParkWhiz per event, this scans all of
our active listings and surfaces the ones worth a price review.

## What "scarcity" means per platform

- **SpotHero** exposes an exact remaining count in its API
  (`availability.available_spaces` out of `facility.common.inventory.default_quantity`).
  We use that directly — e.g. "5 of 13 spots left" — and mark it `limited` when
  ≤15% of capacity remains, `sold_out` at 0.
- **ParkWhiz** does **not** expose an exact count via its API — only a 3-state
  status per lot: `available` / `limited` / `sold_out`. We use that status
  as-is. (If ParkWhiz's own site visually shows a specific "X left" number,
  that's rendered client-side from something not present in the API response
  we checked.)

## Project structure

```
backend/
  main.py                    FastAPI app — dashboard + scan endpoints
  orchestrator.py            Pulls our listings from ReachPro, runs scarcity checks, persists
  reachpro.py                ReachPro API client (copied from listing_availability_checker)
  database.py                SQLite model: one row per (event, platform) scarcity reading
  checkers/
    scarcity_check.py        SpotHero + ParkWhiz scarcity checkers (adapted from
                              listing_availability_checker/backend/checkers/platform_check.py,
                              extended to extract spots_left/capacity/status)
  static/index.html           Dashboard UI (single page, no build step)
```

This reuses the matching/geocoding approach from
`~/Desktop/listing_availability_checker/backend/checkers/platform_check.py`
(same repo that powers the Repricing Tool's on-demand platform-check), but
adds the scarcity fields that tool didn't need, and runs across *all* active
listings rather than one at a time on demand.

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in REACHPRO_COOKIE
```

## Run

```bash
cd backend
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** for the dashboard.

## Running a scan

- From the dashboard: click **Run Scan** (runs in the background, polls for progress).
- From the API: `POST /api/scan` (optional query params: `region`, `event_limit`, `events_per_region`).
- From the CLI:
  ```bash
  python orchestrator.py --event-limit 50   # quick test run
  python orchestrator.py                     # full scan, all regions
  ```

A scan can take a while since it checks every active listing across both
platforms. Use `--event-limit` for a quick test.

## Checking a single event on demand

```
GET /api/check-one?event_name=...&event_date=YYYY-MM-DD&event_venue=...&our_section=...
```

Bypasses ReachPro and the database — useful for testing.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/scan` | Start a background scan across our active listings |
| `GET` | `/api/scan/status` | Progress of the current/last scan |
| `GET` | `/api/check-one` | On-demand scarcity check for one event |
| `GET` | `/api/dashboard` | Latest scarcity reading per event×platform, most-scarce first |
| `GET` | `/api/dashboard/summary` | Counts by scarcity level |

## Not yet built

- **Scheduling** — scans are manual/on-demand for now. A cron/scheduler to
  run this automatically (e.g. hourly) is a follow-up.
- **Team notifications** (Slack/Teams) — dashboard-first was the priority;
  push notifications are a later phase.
- **Radius-based grouping / price-spike detection** — an earlier version of
  this brief described monitoring price *increases* within a 0.10-mile radius
  for the Events team. That's a different signal (price trend vs. inventory
  scarcity) and isn't built here — flag if that's still wanted as a separate
  feature.

## Lysted listings (sold-out-at-source alerts)

The listing team exports their inventory from Lysted as a CSV and uploads it
on the **Lysted Listings** tab (or `POST /api/lysted/upload`). The tool then
checks each live listing's lot against SpotHero and ParkWhiz — the same
scarcity check the ReachPro flow uses, just fed from the CSV instead of the
API — every 3 hours, and posts a Teams alert (`LYSTED_TEAMS_WEBHOOK_URL`)
naming any listing whose parking has **just** sold out at source, so it can
be deactivated on Lysted before someone buys a pass we can't fulfil.

The brief, the export's shape, and the assumptions the build makes are kept
in the team's internal notes (not published in this repo).
