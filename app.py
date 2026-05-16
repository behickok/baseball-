"""Baseball dashboard — Flask app backed by a pre-built SQLite database.

The DB at ``data/baseball.db`` is populated by ``scripts/build_db.py``
(which runs locally or in the GitHub Action). At runtime we only need
``flask`` and the stdlib ``sqlite3`` module — no pandas, no pybaseball.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, g, make_response, render_template, request

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BASEBALL_DB", ROOT / "data" / "baseball.db"))

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)

DEFAULT_TEAM_COOKIE = "default_team"

# (abbrev, full name, league, division)
MLB_TEAMS = [
    ("ARI", "Arizona Diamondbacks",  "NL", "West"),
    ("ATL", "Atlanta Braves",        "NL", "East"),
    ("BAL", "Baltimore Orioles",     "AL", "East"),
    ("BOS", "Boston Red Sox",        "AL", "East"),
    ("CHC", "Chicago Cubs",          "NL", "Central"),
    ("CHW", "Chicago White Sox",     "AL", "Central"),
    ("CIN", "Cincinnati Reds",       "NL", "Central"),
    ("CLE", "Cleveland Guardians",   "AL", "Central"),
    ("COL", "Colorado Rockies",      "NL", "West"),
    ("DET", "Detroit Tigers",        "AL", "Central"),
    ("HOU", "Houston Astros",        "AL", "West"),
    ("KCR", "Kansas City Royals",    "AL", "Central"),
    ("LAA", "Los Angeles Angels",    "AL", "West"),
    ("LAD", "Los Angeles Dodgers",   "NL", "West"),
    ("MIA", "Miami Marlins",         "NL", "East"),
    ("MIL", "Milwaukee Brewers",     "NL", "Central"),
    ("MIN", "Minnesota Twins",       "AL", "Central"),
    ("NYM", "New York Mets",         "NL", "East"),
    ("NYY", "New York Yankees",      "AL", "East"),
    ("OAK", "Oakland Athletics",     "AL", "West"),
    ("PHI", "Philadelphia Phillies", "NL", "East"),
    ("PIT", "Pittsburgh Pirates",    "NL", "Central"),
    ("SDP", "San Diego Padres",      "NL", "West"),
    ("SEA", "Seattle Mariners",      "AL", "West"),
    ("SFG", "San Francisco Giants",  "NL", "West"),
    ("STL", "St. Louis Cardinals",   "NL", "Central"),
    ("TBR", "Tampa Bay Rays",        "AL", "East"),
    ("TEX", "Texas Rangers",         "AL", "West"),
    ("TOR", "Toronto Blue Jays",     "AL", "East"),
    ("WSN", "Washington Nationals",  "NL", "East"),
]
TEAM_MAP = {abbr: {"name": name, "league": lg, "division": div}
            for abbr, name, lg, div in MLB_TEAMS}


# --- DB plumbing --------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        # check_same_thread=False is fine: each request has its own connection.
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        g._db = db
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    cur = get_db().execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def query_one(sql: str, params: tuple = ()) -> dict | None:
    cur = get_db().execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def db_exists() -> bool:
    return DB_PATH.exists() and DB_PATH.stat().st_size > 0


# --- helpers ------------------------------------------------------------------
def available_seasons() -> list[int]:
    if not db_exists():
        return [datetime.now().year]
    rows = query(
        "SELECT DISTINCT season FROM standings "
        "UNION SELECT DISTINCT season FROM team_batting "
        "UNION SELECT DISTINCT season FROM batters "
        "ORDER BY season DESC"
    )
    seasons = [r["season"] for r in rows if r["season"] is not None]
    return seasons or [datetime.now().year]


def default_season() -> int:
    return available_seasons()[0]


def default_team() -> str:
    t = request.cookies.get(DEFAULT_TEAM_COOKIE)
    if t in TEAM_MAP:
        return t
    return "NYY"


def selected_team() -> str:
    t = request.args.get("team")
    if t in TEAM_MAP:
        return t
    return default_team()


def selected_season() -> int:
    raw = request.args.get("season")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default_season()


def derive_streak(played: list[dict]) -> str:
    """Walk recent games and produce 'W3' / 'L2' from the trailing run."""
    if not played:
        return "—"
    last = played[-1].get("w_l") or ""
    if not last:
        return "—"
    kind = "W" if last.startswith("W") else "L" if last.startswith("L") else None
    if not kind:
        return "—"
    run = 0
    for game in reversed(played):
        wl = (game.get("w_l") or "")
        if wl.startswith(kind):
            run += 1
        else:
            break
    return f"{kind}{run}"


def last_build() -> str | None:
    if not db_exists():
        return None
    row = query_one("SELECT value FROM meta WHERE key = 'last_build_utc'")
    return row["value"] if row else None


# --- routes -------------------------------------------------------------------
@app.route("/")
def index():
    team = selected_team()
    return render_template(
        "index.html",
        teams=MLB_TEAMS,
        team_map=TEAM_MAP,
        selected=team,
        default=default_team(),
        season=selected_season(),
        seasons=available_seasons(),
        last_build=last_build(),
        db_missing=not db_exists(),
    )


@app.route("/set-default")
def set_default():
    team = request.args.get("team")
    if team not in TEAM_MAP:
        abort(400)
    html = render_template(
        "partials/_default_badge.html",
        team=team,
        team_map=TEAM_MAP,
    )
    resp = make_response(html)
    resp.set_cookie(DEFAULT_TEAM_COOKIE, team,
                    max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.route("/team/overview")
def team_overview():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    team = selected_team()
    season = selected_season()

    sched = query(
        "SELECT * FROM team_schedule WHERE season = ? AND team_abbr = ? "
        "ORDER BY gm_num",
        (season, team),
    )
    played = [s for s in sched if s.get("w_l")]
    upcoming = [s for s in sched if not s.get("w_l")]

    wins = sum(1 for s in played if (s.get("w_l") or "").startswith("W"))
    losses = sum(1 for s in played if (s.get("w_l") or "").startswith("L"))
    rs = sum(s.get("r") or 0 for s in played)
    ra = sum(s.get("ra") or 0 for s in played)
    pct = f"{wins / (wins + losses):.3f}".lstrip("0") if (wins + losses) else "—"

    record = {
        "W": wins, "L": losses, "pct": pct,
        "rs": rs, "ra": ra,
        "streak": derive_streak(played),
    }

    team_bat_row = query_one(
        "SELECT * FROM team_batting WHERE season = ? AND team_abbr = ?",
        (season, team),
    )
    team_pit_row = query_one(
        "SELECT * FROM team_pitching WHERE season = ? AND team_abbr = ?",
        (season, team),
    )

    return render_template(
        "partials/_overview.html",
        team=team,
        team_info=TEAM_MAP[team],
        season=season,
        record=record,
        recent=played[-10:],
        next_games=upcoming[:5],
        team_bat_row=team_bat_row,
        team_pit_row=team_pit_row,
    )


@app.route("/team/schedule")
def team_schedule():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    team = selected_team()
    season = selected_season()
    games = query(
        "SELECT * FROM team_schedule WHERE season = ? AND team_abbr = ? "
        "ORDER BY gm_num",
        (season, team),
    )
    return render_template(
        "partials/_schedule.html",
        team=team, team_info=TEAM_MAP[team], season=season, games=games,
    )


@app.route("/team/batting")
def team_batting():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    team = selected_team()
    season = selected_season()
    players = query(
        "SELECT name, g, pa, ab, h, hr, rbi, sb, bb, so, avg, obp, slg, ops "
        "FROM batters WHERE season = ? AND team_abbr = ? "
        "ORDER BY ops DESC NULLS LAST, pa DESC NULLS LAST",
        (season, team),
    )
    return render_template(
        "partials/_batting.html",
        team=team, team_info=TEAM_MAP[team], season=season, players=players,
    )


@app.route("/team/pitching")
def team_pitching():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    team = selected_team()
    season = selected_season()
    players = query(
        "SELECT name, w, l, era, g, gs, sv, ip, so, bb, hr, whip, k_9, bb_9 "
        "FROM pitchers WHERE season = ? AND team_abbr = ? "
        "ORDER BY ip DESC NULLS LAST, era ASC NULLS LAST",
        (season, team),
    )
    return render_template(
        "partials/_pitching.html",
        team=team, team_info=TEAM_MAP[team], season=season, players=players,
    )


@app.route("/standings")
def standings():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    season = selected_season()
    rows = query(
        "SELECT league, division, team_full, w, l, w_l_pct, gb "
        "FROM standings WHERE season = ? "
        "ORDER BY league, division, w_l_pct DESC NULLS LAST",
        (season,),
    )
    by_div: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_div.setdefault((r["league"], r["division"]), []).append({
            "Team": r["team_full"],
            "W": r["w"],
            "L": r["l"],
            "PCT": r["w_l_pct"],
            "GB": r["gb"],
        })
    divisions = [
        {"label": f"{lg} {div}".strip(),
         "cols": ["Team", "W", "L", "PCT", "GB"],
         "rows": rows_}
        for (lg, div), rows_ in sorted(by_div.items())
    ]
    return render_template(
        "partials/_standings.html",
        season=season, divisions=divisions,
        default=default_team(), team_map=TEAM_MAP,
    )


@app.route("/leaders/batting")
def leaders_batting():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    season = selected_season()
    rows = query(
        "SELECT name, team_abbr, g, pa, hr, rbi, sb, avg, obp, slg, ops "
        "FROM batters WHERE season = ? AND pa >= 50 "
        "ORDER BY ops DESC NULLS LAST LIMIT 25",
        (season,),
    )
    cols = [
        ("name", "Name"), ("team_abbr", "Team"), ("g", "G"), ("pa", "PA"),
        ("hr", "HR"), ("rbi", "RBI"), ("sb", "SB"),
        ("avg", "AVG"), ("obp", "OBP"), ("slg", "SLG"), ("ops", "OPS"),
    ]
    return render_template(
        "partials/_leaders.html",
        season=season, title="OPS leaders", cols=cols, rows=rows,
    )


@app.route("/leaders/pitching")
def leaders_pitching():
    if not db_exists():
        return render_template("partials/_empty_db.html")
    season = selected_season()
    rows = query(
        "SELECT name, team_abbr, w, l, era, g, gs, ip, so, whip, k_9 "
        "FROM pitchers WHERE season = ? AND ip >= 20 "
        "ORDER BY era ASC NULLS LAST LIMIT 25",
        (season,),
    )
    cols = [
        ("name", "Name"), ("team_abbr", "Team"),
        ("w", "W"), ("l", "L"), ("era", "ERA"),
        ("g", "G"), ("gs", "GS"), ("ip", "IP"), ("so", "SO"),
        ("whip", "WHIP"), ("k_9", "K/9"),
    ]
    return render_template(
        "partials/_leaders.html",
        season=season, title="ERA leaders (20+ IP)", cols=cols, rows=rows,
    )


@app.template_filter("fmt")
def fmt(value):
    """Display formatter — handles None and tidies floats."""
    if value is None:
        return "—"
    if isinstance(value, float):
        if 0 < value < 1:
            return f"{value:.3f}".lstrip("0")
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    return value


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
