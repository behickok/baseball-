# ⚾ Baseball Dashboard

Flask + htmx dashboard for MLB stats. Pick a default team, dig into any other team on the fly. Backed by a **pre-built SQLite database** that's refreshed nightly by a GitHub Action — the deployed app has zero runtime scraping dependencies, so it fits cleanly on Vercel.

## Architecture

```
┌──────────────────────┐    nightly cron     ┌──────────────────────┐
│  GitHub Action       │ ───────────────────▶│ scripts/build_db.py  │
│  refresh-data.yml    │                     │ (pybaseball, pandas) │
└──────────────────────┘                     └──────────┬───────────┘
            │                                            │ writes
            │ commits + pushes                           ▼
            │                                  ┌──────────────────┐
            ▼                                  │ data/baseball.db │
   ┌────────────────┐    auto-deploy          └─────────┬────────┘
   │ GitHub default │ ───────────────────────────────▶  │ bundled
   │ branch         │                                    │ into the
   └────────────────┘                                    ▼ function
                                              ┌────────────────────┐
                                              │  Vercel Python fn  │
                                              │  Flask + sqlite3   │
                                              └────────────────────┘
```

- **`app.py`** — Flask app. Reads only from SQLite via stdlib `sqlite3`. No pandas, no pybaseball.
- **`scripts/build_db.py`** — Run locally or in CI. Hits the **MLB Stats API** (`statsapi.mlb.com`) and writes `data/baseball.db`. Just `requests` + stdlib — no scraping, no IP blocks.
- **`.github/workflows/refresh-data.yml`** — Cron at 11:15 UTC daily + manual trigger. Re-runs the build script for the current season and commits the new `.db` if anything changed. Vercel redeploys on push.
- **`api/index.py`** + **`vercel.json`** — Vercel serverless entry point.

## Features

- Default team persisted as a cookie (star button next to the team picker).
- Season picker (whatever seasons are in the DB show up).
- Tabs with htmx in-place swaps: Overview, Schedule, Team Batting, Team Pitching, Standings, Batting/Pitching Leaders.
- Footer shows the DB's last-built timestamp so you can tell how fresh it is.

## Local development

```bash
# install runtime + build deps
pip install -r requirements.txt -r requirements-build.txt

# seed the DB (current season only; ~2 minutes)
python scripts/build_db.py

# or seed multiple seasons
python scripts/build_db.py --seasons 2024 2025 2026

# run
python app.py        # http://localhost:5000
```

## Deploying to Vercel

1. Push this repo to GitHub.
2. Import the repo into Vercel. Framework preset: **Other**. It picks up `vercel.json` automatically.
3. No env vars are required.
4. Make sure `data/baseball.db` has been built and committed at least once before the first deploy — otherwise the app will render the "no data loaded yet" panel.

After the initial deploy, the GitHub Action keeps the DB fresh:
- Daily cron commits any changes to the default branch.
- Vercel's Git integration redeploys on every push.
- The Action can also be triggered manually from the Actions tab (with optional `seasons` input to rebuild historical years).

## Why SQLite and not DuckDB?

- Stdlib (zero runtime deps, smaller Vercel bundle).
- The queries here are point lookups by `(season, team_abbr)`, not analytics. DuckDB's columnar engine would be overkill.
- Works out of the box on Vercel's read-only filesystem.

## Why MLB Stats API and not FanGraphs / Baseball-Reference?

The first iteration used pybaseball, which scrapes Baseball-Reference and FanGraphs. Both increasingly block automated traffic — even GitHub Actions IPs get 403s from FanGraphs. The MLB Stats API is MLB's own JSON API, free, unauthenticated, and reliable.

Trade-off: no FanGraphs-only metrics (wRC+, fWAR, FIP). All the standard rate stats (AVG/OBP/SLG/OPS, ERA/WHIP/K-9/BB-9) match exactly.

## Why not Cloudflare?

Cloudflare Workers don't run real CPython well (Pyodide-only, no pandas/lxml). To target Cloudflare, the runtime would need to be rewritten in JS using D1 (their hosted SQLite). The build script and schema would carry over unchanged.
