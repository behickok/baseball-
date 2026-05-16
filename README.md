# ⚾ Baseball Dashboard

A self-hosted MLB stats dashboard built with **Flask + htmx + pybaseball**. Pick a default team you care about most; switch to any other team on the fly.

## Features

- **Default team** persisted as a cookie (star button next to the team picker). Survives reloads.
- **Team picker** for all 30 MLB teams.
- Tabs (no page reload — htmx swaps the panel):
  - **Overview** — record, run differential, streak, team OPS/ERA, last 10, next 5
  - **Schedule** — full season schedule with results
  - **Team Batting** — qualified batters with AVG/OBP/SLG/OPS/wRC+/WAR
  - **Team Pitching** — pitchers with ERA/WHIP/FIP/K-9/WAR
  - **Standings** — all six divisions
  - **Batting / Pitching leaders** — league top 25 by WAR
- pybaseball's on-disk cache + in-process TTL cache keep things fast.
- Errors are caught per panel so one bad upstream call doesn't crash the page.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:5000>.

Set `PORT` to override the port, e.g. `PORT=8000 python app.py`.

## Notes

- Data is pulled live from Baseball-Reference and FanGraphs via [pybaseball](https://github.com/jldbc/pybaseball). The very first request for any season is slow while it scrapes; after that it's cached.
- Season defaults to the current calendar year. If you're poking around in the off-season, the leader/team stat panels for the current year may be empty until opening day.
- Default team is stored in a `default_team` cookie for one year.
