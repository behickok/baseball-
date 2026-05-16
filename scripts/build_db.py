#!/usr/bin/env python3
"""Build data/baseball.db from pybaseball.

Usage:
  python scripts/build_db.py                          # current season only (refresh)
  python scripts/build_db.py --seasons 2024 2025 2026 # rebuild specified seasons
  python scripts/build_db.py --skip-schedule          # skip per-team schedule fetch (faster)

This script is meant to run locally or in a GitHub Action; the deployed
Flask app never imports pybaseball.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import pybaseball as pyb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "baseball.db"
CURRENT_SEASON = dt.datetime.now().year

TEAMS = [
    "ARI", "ATL", "BAL", "BOS", "CHC", "CHW", "CIN", "CLE", "COL", "DET",
    "HOU", "KCR", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "OAK",
    "PHI", "PIT", "SDP", "SEA", "SFG", "STL", "TBR", "TEX", "TOR", "WSN",
]

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


def to_int(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return int(v)
    except (ValueError, TypeError):
        return None


def to_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def to_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v)


def col(row, name, default=None):
    """Get a column from a pandas Series-like row, treating NaN as missing."""
    if name not in row:
        return default
    v = row[name]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    return v


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def clear_season(conn: sqlite3.Connection, season: int) -> None:
    for table in ("standings", "team_schedule", "team_batting",
                  "team_pitching", "batters", "pitchers"):
        conn.execute(f"DELETE FROM {table} WHERE season = ?", (season,))


# --- pybaseball fetches -------------------------------------------------------
def fetch_standings(conn: sqlite3.Connection, season: int) -> None:
    info(f"standings {season}")
    divisions = pyb.standings(season)
    for df in divisions:
        # The first column is named after the division ("AL East", "NL West", ...)
        div_label = str(df.columns[0])
        parts = div_label.split(" ", 1)
        league = parts[0] if parts else ""
        division = parts[1] if len(parts) > 1 else ""
        team_col = df.columns[0]
        for _, row in df.iterrows():
            conn.execute(
                """INSERT OR REPLACE INTO standings
                   (season, league, division, team_full, w, l, w_l_pct, gb)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    season, league, division,
                    to_str(col(row, team_col)),
                    to_int(col(row, "W")),
                    to_int(col(row, "L")),
                    to_float(col(row, "W-L%")),
                    to_str(col(row, "GB")),
                ),
            )


def fetch_team_schedules(conn: sqlite3.Connection, season: int,
                         delay: float = 0.8) -> None:
    info(f"team schedules for {season}")
    for i, team in enumerate(TEAMS, 1):
        try:
            df = pyb.schedule_and_record(season, team)
        except Exception as exc:
            info(f"  {i:>2}/{len(TEAMS)} {team}: skipped ({exc.__class__.__name__})")
            time.sleep(delay)
            continue
        rows = 0
        for _, r in df.iterrows():
            gm_num = to_int(col(r, "Gm#"))
            if gm_num is None:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO team_schedule
                   (season, team_abbr, gm_num, date, home_away, opp, w_l,
                    r, ra, innings, record, rank, gb,
                    win_pitcher, loss_pitcher, save_pitcher,
                    time, d_n, attendance, streak)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    season, team, gm_num,
                    to_str(col(r, "Date")),
                    to_str(col(r, "Home_Away")) or "",
                    to_str(col(r, "Opp")),
                    to_str(col(r, "W/L")),
                    to_int(col(r, "R")),
                    to_int(col(r, "RA")),
                    to_int(col(r, "Inn")) or 9,
                    to_str(col(r, "W-L")),
                    to_int(col(r, "Rank")),
                    to_str(col(r, "GB")),
                    to_str(col(r, "Win")),
                    to_str(col(r, "Loss")),
                    to_str(col(r, "Save")),
                    to_str(col(r, "Time")),
                    to_str(col(r, "D/N")),
                    to_int(col(r, "Attendance")),
                    to_str(col(r, "Streak")),
                ),
            )
            rows += 1
        info(f"  {i:>2}/{len(TEAMS)} {team}: {rows} games")
        time.sleep(delay)


def fetch_team_batting(conn: sqlite3.Connection, season: int) -> None:
    info(f"team batting {season}")
    df = pyb.team_batting(season)
    for _, r in df.iterrows():
        team = to_str(col(r, "Team"))
        if not team:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO team_batting
               (season, team_abbr, g, pa, ab, h, hr, rbi, sb, bb, so,
                avg, obp, slg, ops, wrc_plus, war)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                season, team.upper(),
                to_int(col(r, "G")), to_int(col(r, "PA")), to_int(col(r, "AB")),
                to_int(col(r, "H")), to_int(col(r, "HR")), to_int(col(r, "RBI")),
                to_int(col(r, "SB")), to_int(col(r, "BB")), to_int(col(r, "SO")),
                to_float(col(r, "AVG")), to_float(col(r, "OBP")),
                to_float(col(r, "SLG")), to_float(col(r, "OPS")),
                to_int(col(r, "wRC+")), to_float(col(r, "WAR")),
            ),
        )


def fetch_team_pitching(conn: sqlite3.Connection, season: int) -> None:
    info(f"team pitching {season}")
    df = pyb.team_pitching(season)
    for _, r in df.iterrows():
        team = to_str(col(r, "Team"))
        if not team:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO team_pitching
               (season, team_abbr, w, l, era, g, gs, sv, ip, so, bb, hr,
                whip, fip, k_9, bb_9, war)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                season, team.upper(),
                to_int(col(r, "W")), to_int(col(r, "L")),
                to_float(col(r, "ERA")),
                to_int(col(r, "G")), to_int(col(r, "GS")), to_int(col(r, "SV")),
                to_float(col(r, "IP")), to_int(col(r, "SO")),
                to_int(col(r, "BB")), to_int(col(r, "HR")),
                to_float(col(r, "WHIP")), to_float(col(r, "FIP")),
                to_float(col(r, "K/9")), to_float(col(r, "BB/9")),
                to_float(col(r, "WAR")),
            ),
        )


def fetch_batters(conn: sqlite3.Connection, season: int) -> None:
    info(f"batters {season}")
    df = pyb.batting_stats(season, qual=30)
    for _, r in df.iterrows():
        pid = to_int(col(r, "IDfg"))
        if pid is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO batters
               (season, player_id, name, team_abbr,
                g, pa, ab, h, hr, rbi, sb, bb, so,
                avg, obp, slg, ops, wrc_plus, war)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                season, pid,
                to_str(col(r, "Name")),
                (to_str(col(r, "Team")) or "").upper(),
                to_int(col(r, "G")), to_int(col(r, "PA")), to_int(col(r, "AB")),
                to_int(col(r, "H")), to_int(col(r, "HR")), to_int(col(r, "RBI")),
                to_int(col(r, "SB")), to_int(col(r, "BB")), to_int(col(r, "SO")),
                to_float(col(r, "AVG")), to_float(col(r, "OBP")),
                to_float(col(r, "SLG")), to_float(col(r, "OPS")),
                to_int(col(r, "wRC+")), to_float(col(r, "WAR")),
            ),
        )


def fetch_pitchers(conn: sqlite3.Connection, season: int) -> None:
    info(f"pitchers {season}")
    df = pyb.pitching_stats(season, qual=10)
    for _, r in df.iterrows():
        pid = to_int(col(r, "IDfg"))
        if pid is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO pitchers
               (season, player_id, name, team_abbr,
                w, l, era, g, gs, sv, ip, so, bb, hr,
                whip, fip, k_9, bb_9, war)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                season, pid,
                to_str(col(r, "Name")),
                (to_str(col(r, "Team")) or "").upper(),
                to_int(col(r, "W")), to_int(col(r, "L")),
                to_float(col(r, "ERA")),
                to_int(col(r, "G")), to_int(col(r, "GS")), to_int(col(r, "SV")),
                to_float(col(r, "IP")), to_int(col(r, "SO")),
                to_int(col(r, "BB")), to_int(col(r, "HR")),
                to_float(col(r, "WHIP")), to_float(col(r, "FIP")),
                to_float(col(r, "K/9")), to_float(col(r, "BB/9")),
                to_float(col(r, "WAR")),
            ),
        )


# --- top-level driver ---------------------------------------------------------
def build(seasons: list[int], skip_schedule: bool = False) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_schema(conn)
        for season in seasons:
            info(f"=== season {season} ===")
            clear_season(conn, season)

            for fn in (fetch_standings, fetch_team_batting, fetch_team_pitching,
                       fetch_batters, fetch_pitchers):
                try:
                    fn(conn, season)
                    conn.commit()
                except Exception as exc:
                    info(f"  {fn.__name__} failed: {exc.__class__.__name__}: {exc}")

            if not skip_schedule:
                try:
                    fetch_team_schedules(conn, season)
                    conn.commit()
                except Exception as exc:
                    info(f"  fetch_team_schedules failed: {exc.__class__.__name__}: {exc}")

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
    ap.add_argument("--skip-schedule", action="store_true",
                    help="Skip per-team schedule fetch (it's slow: 30 requests).")
    args = ap.parse_args(argv)

    try:
        pyb.cache.enable()
    except Exception:
        pass

    build(args.seasons, skip_schedule=args.skip_schedule)
    return 0


if __name__ == "__main__":
    sys.exit(main())
