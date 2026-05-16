#!/usr/bin/env python3
"""Build data/baseball.db from the MLB Stats API (statsapi.mlb.com).

This replaces pybaseball entirely. The Stats API is MLB's official data
source: no scraping, no rate limits in practice, no IP blocks.

Usage:
  python scripts/build_db.py                          # current season
  python scripts/build_db.py --seasons 2024 2025 2026 # rebuild specified seasons

Trade-off: we don't pull FanGraphs-only metrics (wRC+, fWAR, FIP). Those
columns stay in the schema as NULL in case we wire in another source later.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "baseball.db"
CURRENT_SEASON = dt.datetime.now().year

API = "https://statsapi.mlb.com/api/v1"

TEAM_IDS = {
    "ARI": 109, "ATL": 144, "BAL": 110, "BOS": 111, "CHC": 112, "CHW": 145,
    "CIN": 113, "CLE": 114, "COL": 115, "DET": 116, "HOU": 117, "KCR": 118,
    "LAA": 108, "LAD": 119, "MIA": 146, "MIL": 158, "MIN": 142, "NYM": 121,
    "NYY": 147, "OAK": 133, "PHI": 143, "PIT": 134, "SDP": 135, "SEA": 136,
    "SFG": 137, "STL": 138, "TBR": 139, "TEX": 140, "TOR": 141, "WSN": 120,
}
TEAM_ID_TO_ABBR = {v: k for k, v in TEAM_IDS.items()}

# MLB division ID -> (league, division)
DIVISIONS = {
    200: ("AL", "West"),    201: ("AL", "East"),    202: ("AL", "Central"),
    203: ("NL", "West"),    204: ("NL", "East"),    205: ("NL", "Central"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standings (
    season INTEGER NOT NULL,
    league TEXT NOT NULL,
    division TEXT NOT NULL,
    team_full TEXT NOT NULL,
    w INTEGER, l INTEGER, w_l_pct REAL, gb TEXT,
    PRIMARY KEY (season, team_full)
);

CREATE TABLE IF NOT EXISTS team_schedule (
    season INTEGER NOT NULL,
    team_abbr TEXT NOT NULL,
    gm_num INTEGER NOT NULL,
    date TEXT,
    home_away TEXT,
    opp TEXT,
    w_l TEXT,
    r INTEGER,
    ra INTEGER,
    innings INTEGER,
    record TEXT,
    rank INTEGER,
    gb TEXT,
    win_pitcher TEXT,
    loss_pitcher TEXT,
    save_pitcher TEXT,
    time TEXT,
    d_n TEXT,
    attendance INTEGER,
    streak TEXT,
    PRIMARY KEY (season, team_abbr, gm_num)
);

CREATE TABLE IF NOT EXISTS team_batting (
    season INTEGER NOT NULL,
    team_abbr TEXT NOT NULL,
    g INTEGER, pa INTEGER, ab INTEGER, h INTEGER, hr INTEGER,
    rbi INTEGER, sb INTEGER, bb INTEGER, so INTEGER,
    avg REAL, obp REAL, slg REAL, ops REAL,
    wrc_plus INTEGER, war REAL,
    PRIMARY KEY (season, team_abbr)
);

CREATE TABLE IF NOT EXISTS team_pitching (
    season INTEGER NOT NULL,
    team_abbr TEXT NOT NULL,
    w INTEGER, l INTEGER, era REAL,
    g INTEGER, gs INTEGER, sv INTEGER,
    ip REAL, so INTEGER, bb INTEGER, hr INTEGER,
    whip REAL, fip REAL, k_9 REAL, bb_9 REAL, war REAL,
    PRIMARY KEY (season, team_abbr)
);

CREATE TABLE IF NOT EXISTS batters (
    season INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    name TEXT, team_abbr TEXT,
    g INTEGER, pa INTEGER, ab INTEGER, h INTEGER, hr INTEGER,
    rbi INTEGER, sb INTEGER, bb INTEGER, so INTEGER,
    avg REAL, obp REAL, slg REAL, ops REAL,
    wrc_plus INTEGER, war REAL,
    PRIMARY KEY (season, player_id)
);

CREATE TABLE IF NOT EXISTS pitchers (
    season INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    name TEXT, team_abbr TEXT,
    w INTEGER, l INTEGER, era REAL,
    g INTEGER, gs INTEGER, sv INTEGER,
    ip REAL, so INTEGER, bb INTEGER, hr INTEGER,
    whip REAL, fip REAL, k_9 REAL, bb_9 REAL, war REAL,
    PRIMARY KEY (season, player_id)
);

CREATE INDEX IF NOT EXISTS idx_batters_team   ON batters(season, team_abbr);
CREATE INDEX IF NOT EXISTS idx_pitchers_team  ON pitchers(season, team_abbr);
CREATE INDEX IF NOT EXISTS idx_schedule_team  ON team_schedule(season, team_abbr);
"""


def info(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


SESSION = requests.Session()
SESSION.headers["User-Agent"] = (
    "baseball-dashboard/1.0 (+https://github.com/behickok/baseball-)"
)


def get(path: str, **params) -> dict:
    url = f"{API}{path}"
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def to_int(v):
    if v in (None, "", "-", "-.--"):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def to_float(v):
    if v in (None, "", "-", "-.--"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def clear_season(conn: sqlite3.Connection, season: int) -> None:
    for table in ("standings", "team_schedule", "team_batting",
                  "team_pitching", "batters", "pitchers"):
        conn.execute(f"DELETE FROM {table} WHERE season = ?", (season,))


# --- standings ---------------------------------------------------------------
def fetch_standings(conn: sqlite3.Connection, season: int) -> None:
    info(f"standings {season}")
    data = get("/standings", leagueId="103,104", season=season,
               standingsTypes="regularSeason")
    inserted = 0
    for record in data.get("records", []):
        div_id = (record.get("division") or {}).get("id")
        if div_id not in DIVISIONS:
            continue
        league, division = DIVISIONS[div_id]
        for tr in record.get("teamRecords", []):
            team = tr.get("team", {})
            team_full = team.get("name")
            conn.execute(
                """INSERT OR REPLACE INTO standings
                   (season, league, division, team_full, w, l, w_l_pct, gb)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (season, league, division, team_full,
                 to_int(tr.get("wins")), to_int(tr.get("losses")),
                 to_float(tr.get("winningPercentage")),
                 (tr.get("gamesBack") or "—")),
            )
            inserted += 1
    info(f"  inserted {inserted} standings rows")


# --- schedule ---------------------------------------------------------------
def fetch_schedule(conn: sqlite3.Connection, season: int) -> None:
    """One API call, split into per-team perspectives."""
    info(f"schedule {season}")
    data = get(
        "/schedule",
        sportId=1,
        startDate=f"{season}-01-01",
        endDate=f"{season}-12-31",
        gameType="R",
        hydrate="linescore",
    )

    per_team: dict[str, list[dict]] = {abbr: [] for abbr in TEAM_IDS}

    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            status = (game.get("status") or {}).get("detailedState", "")
            if status in ("Postponed", "Cancelled"):
                continue
            home = game.get("teams", {}).get("home", {})
            away = game.get("teams", {}).get("away", {})
            home_id = (home.get("team") or {}).get("id")
            away_id = (away.get("team") or {}).get("id")
            home_abbr = TEAM_ID_TO_ABBR.get(home_id)
            away_abbr = TEAM_ID_TO_ABBR.get(away_id)
            if not home_abbr or not away_abbr:
                continue

            is_final = status in ("Final", "Game Over", "Completed Early")
            home_score = to_int(home.get("score")) if is_final else None
            away_score = to_int(away.get("score")) if is_final else None

            linescore = game.get("linescore") or {}
            innings = to_int(linescore.get("currentInning")) if is_final else None

            game_date = (game.get("gameDate") or "")[:10]
            game_time = (game.get("gameDate") or "")[11:16]

            day_night = "D"
            if game.get("dayNight") == "night":
                day_night = "N"

            for perspective, opp, is_home, our, theirs in [
                (home_abbr, away_abbr, True,  home_score, away_score),
                (away_abbr, home_abbr, False, away_score, home_score),
            ]:
                wl = None
                if our is not None and theirs is not None and our != theirs:
                    wl = "W" if our > theirs else "L"
                per_team[perspective].append({
                    "date": game_date,
                    "home_away": "" if is_home else "@",
                    "opp": opp,
                    "w_l": wl,
                    "r": our,
                    "ra": theirs,
                    "innings": innings,
                    "time": game_time,
                    "d_n": day_night,
                })

    total = 0
    for team, games in per_team.items():
        # Sort by date so gm_num matches chronological order.
        games.sort(key=lambda g: (g["date"], g["time"]))
        wins = losses = 0
        streak_kind: str | None = None
        streak_len = 0
        for n, g in enumerate(games, 1):
            if g["w_l"] == "W":
                wins += 1
                streak_kind, streak_len = ("W", streak_len + 1 if streak_kind == "W" else 1)
            elif g["w_l"] == "L":
                losses += 1
                streak_kind, streak_len = ("L", streak_len + 1 if streak_kind == "L" else 1)
            record_str = f"{wins}-{losses}" if (wins + losses) else None
            streak_str = f"{streak_kind}{streak_len}" if streak_kind else None
            conn.execute(
                """INSERT OR REPLACE INTO team_schedule
                   (season, team_abbr, gm_num, date, home_away, opp, w_l,
                    r, ra, innings, record, rank, gb,
                    win_pitcher, loss_pitcher, save_pitcher,
                    time, d_n, attendance, streak)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (season, team, n, g["date"], g["home_away"], g["opp"], g["w_l"],
                 g["r"], g["ra"], g["innings"] or 9,
                 record_str, None, None,
                 None, None, None,
                 g["time"], g["d_n"], None, streak_str),
            )
            total += 1
    info(f"  inserted {total} schedule rows across {len(per_team)} teams")


# --- team stats -------------------------------------------------------------
def _stat_row(stats_list, group_name):
    """Pull the stat dict for a given group from a /teams/{id}/stats response."""
    for s in stats_list or []:
        if (s.get("group") or {}).get("displayName") == group_name and s.get("splits"):
            return s["splits"][0].get("stat") or {}
    return {}


def fetch_team_batting_and_pitching(conn: sqlite3.Connection, season: int) -> None:
    info(f"team batting & pitching {season}")
    bat_n = pit_n = 0
    for abbr, team_id in TEAM_IDS.items():
        try:
            data = get(f"/teams/{team_id}/stats",
                       stats="season", season=season,
                       group="hitting,pitching", sportIds=1)
        except Exception as exc:
            info(f"  {abbr}: failed ({exc.__class__.__name__})")
            continue
        stats = data.get("stats", [])

        h = _stat_row(stats, "hitting")
        if h:
            conn.execute(
                """INSERT OR REPLACE INTO team_batting
                   (season, team_abbr, g, pa, ab, h, hr, rbi, sb, bb, so,
                    avg, obp, slg, ops, wrc_plus, war)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (season, abbr,
                 to_int(h.get("gamesPlayed")), to_int(h.get("plateAppearances")),
                 to_int(h.get("atBats")), to_int(h.get("hits")),
                 to_int(h.get("homeRuns")), to_int(h.get("rbi")),
                 to_int(h.get("stolenBases")), to_int(h.get("baseOnBalls")),
                 to_int(h.get("strikeOuts")),
                 to_float(h.get("avg")), to_float(h.get("obp")),
                 to_float(h.get("slg")), to_float(h.get("ops")),
                 None, None),
            )
            bat_n += 1

        p = _stat_row(stats, "pitching")
        if p:
            conn.execute(
                """INSERT OR REPLACE INTO team_pitching
                   (season, team_abbr, w, l, era, g, gs, sv, ip, so, bb, hr,
                    whip, fip, k_9, bb_9, war)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (season, abbr,
                 to_int(p.get("wins")), to_int(p.get("losses")),
                 to_float(p.get("era")),
                 to_int(p.get("gamesPlayed")), to_int(p.get("gamesStarted")),
                 to_int(p.get("saves")),
                 to_float(p.get("inningsPitched")),
                 to_int(p.get("strikeOuts")), to_int(p.get("baseOnBalls")),
                 to_int(p.get("homeRuns")),
                 to_float(p.get("whip")), None,
                 to_float(p.get("strikeoutsPer9Inn")),
                 to_float(p.get("walksPer9Inn")),
                 None),
            )
            pit_n += 1
        time.sleep(0.05)
    info(f"  inserted {bat_n} team batting / {pit_n} team pitching rows")


# --- player stats -----------------------------------------------------------
def _iter_splits(group: str, season: int):
    """Stream player rows from the league-wide /stats endpoint, paginated."""
    offset = 0
    page_size = 500
    while True:
        data = get(
            "/stats",
            stats="season",
            season=season,
            group=group,
            sportIds=1,
            playerPool="All",
            limit=page_size,
            offset=offset,
        )
        splits: list = []
        for s in data.get("stats", []):
            splits.extend(s.get("splits", []))
        if not splits:
            return
        for sp in splits:
            yield sp
        if len(splits) < page_size:
            return
        offset += page_size


def fetch_batters(conn: sqlite3.Connection, season: int) -> None:
    info(f"batters {season}")
    inserted = 0
    for sp in _iter_splits("hitting", season):
        pid = to_int((sp.get("player") or {}).get("id"))
        if pid is None:
            continue
        name = (sp.get("player") or {}).get("fullName")
        team_id = (sp.get("team") or {}).get("id")
        team_abbr = TEAM_ID_TO_ABBR.get(team_id, "")
        st = sp.get("stat") or {}
        pa = to_int(st.get("plateAppearances")) or 0
        if pa < 10:  # filter out incidental position-player appearances
            continue
        conn.execute(
            """INSERT OR REPLACE INTO batters
               (season, player_id, name, team_abbr,
                g, pa, ab, h, hr, rbi, sb, bb, so,
                avg, obp, slg, ops, wrc_plus, war)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (season, pid, name, team_abbr,
             to_int(st.get("gamesPlayed")), pa,
             to_int(st.get("atBats")), to_int(st.get("hits")),
             to_int(st.get("homeRuns")), to_int(st.get("rbi")),
             to_int(st.get("stolenBases")), to_int(st.get("baseOnBalls")),
             to_int(st.get("strikeOuts")),
             to_float(st.get("avg")), to_float(st.get("obp")),
             to_float(st.get("slg")), to_float(st.get("ops")),
             None, None),
        )
        inserted += 1
    info(f"  inserted {inserted} batter rows")


def fetch_pitchers(conn: sqlite3.Connection, season: int) -> None:
    info(f"pitchers {season}")
    inserted = 0
    for sp in _iter_splits("pitching", season):
        pid = to_int((sp.get("player") or {}).get("id"))
        if pid is None:
            continue
        name = (sp.get("player") or {}).get("fullName")
        team_id = (sp.get("team") or {}).get("id")
        team_abbr = TEAM_ID_TO_ABBR.get(team_id, "")
        st = sp.get("stat") or {}
        ip = to_float(st.get("inningsPitched")) or 0
        if ip < 1:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO pitchers
               (season, player_id, name, team_abbr,
                w, l, era, g, gs, sv, ip, so, bb, hr,
                whip, fip, k_9, bb_9, war)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (season, pid, name, team_abbr,
             to_int(st.get("wins")), to_int(st.get("losses")),
             to_float(st.get("era")),
             to_int(st.get("gamesPlayed")), to_int(st.get("gamesStarted")),
             to_int(st.get("saves")),
             ip, to_int(st.get("strikeOuts")),
             to_int(st.get("baseOnBalls")), to_int(st.get("homeRuns")),
             to_float(st.get("whip")), None,
             to_float(st.get("strikeoutsPer9Inn")),
             to_float(st.get("walksPer9Inn")),
             None),
        )
        inserted += 1
    info(f"  inserted {inserted} pitcher rows")


# --- driver ------------------------------------------------------------------
def build(seasons: list[int]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_schema(conn)
        for season in seasons:
            info(f"=== season {season} ===")
            clear_season(conn, season)
            for fn in (fetch_standings, fetch_schedule,
                       fetch_team_batting_and_pitching,
                       fetch_batters, fetch_pitchers):
                try:
                    fn(conn, season)
                    conn.commit()
                except Exception as exc:
                    info(f"  {fn.__name__} failed: {exc.__class__.__name__}: {exc}")

        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_build_utc', ?)",
            (dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('seasons', ?)",
            (",".join(str(s) for s in seasons),),
        )
        conn.commit()
        info(f"wrote {DB_PATH}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seasons", nargs="+", type=int,
                    default=[CURRENT_SEASON],
                    help="Seasons to (re)build. Default: current year only.")
    args = ap.parse_args(argv)
    build(args.seasons)
    return 0


if __name__ == "__main__":
    sys.exit(main())
