import os
import time
from datetime import datetime, timedelta
from functools import wraps

import pandas as pd
from flask import Flask, render_template, request, make_response, abort

import pybaseball as pyb

# Enable pybaseball's on-disk cache so repeated queries are fast.
try:
    pyb.cache.enable()
except Exception:
    pass

app = Flask(__name__)

DEFAULT_TEAM_COOKIE = "default_team"
CURRENT_SEASON = datetime.now().year

# (abbrev, full name, league, division)
# Abbrevs match the codes pybaseball.schedule_and_record / team_batting expect.
MLB_TEAMS = [
    ("ARI", "Arizona Diamondbacks",      "NL", "West"),
    ("ATL", "Atlanta Braves",            "NL", "East"),
    ("BAL", "Baltimore Orioles",         "AL", "East"),
    ("BOS", "Boston Red Sox",            "AL", "East"),
    ("CHC", "Chicago Cubs",              "NL", "Central"),
    ("CHW", "Chicago White Sox",         "AL", "Central"),
    ("CIN", "Cincinnati Reds",           "NL", "Central"),
    ("CLE", "Cleveland Guardians",       "AL", "Central"),
    ("COL", "Colorado Rockies",          "NL", "West"),
    ("DET", "Detroit Tigers",            "AL", "Central"),
    ("HOU", "Houston Astros",            "AL", "West"),
    ("KCR", "Kansas City Royals",        "AL", "Central"),
    ("LAA", "Los Angeles Angels",        "AL", "West"),
    ("LAD", "Los Angeles Dodgers",       "NL", "West"),
    ("MIA", "Miami Marlins",             "NL", "East"),
    ("MIL", "Milwaukee Brewers",         "NL", "Central"),
    ("MIN", "Minnesota Twins",           "AL", "Central"),
    ("NYM", "New York Mets",             "NL", "East"),
    ("NYY", "New York Yankees",          "AL", "East"),
    ("OAK", "Oakland Athletics",         "AL", "West"),
    ("PHI", "Philadelphia Phillies",     "NL", "East"),
    ("PIT", "Pittsburgh Pirates",        "NL", "Central"),
    ("SDP", "San Diego Padres",          "NL", "West"),
    ("SEA", "Seattle Mariners",          "AL", "West"),
    ("SFG", "San Francisco Giants",      "NL", "West"),
    ("STL", "St. Louis Cardinals",       "NL", "Central"),
    ("TBR", "Tampa Bay Rays",            "AL", "East"),
    ("TEX", "Texas Rangers",             "AL", "West"),
    ("TOR", "Toronto Blue Jays",         "AL", "East"),
    ("WSN", "Washington Nationals",      "NL", "East"),
]
TEAM_MAP = {abbr: {"name": name, "league": lg, "division": div}
            for abbr, name, lg, div in MLB_TEAMS}

# fangraphs uses different codes for a couple teams
FANGRAPHS_TEAM_MAP = {
    "CHW": "CHW", "KCR": "KCR", "SDP": "SDP", "SFG": "SFG", "TBR": "TBR", "WSN": "WSN",
}


# --- tiny in-memory TTL cache --------------------------------------------------
_CACHE: dict = {}

def memoize(ttl_seconds: int = 300):
    """Tiny cache to keep the UI snappy and avoid hammering the data sources."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            now = time.time()
            hit = _CACHE.get(key)
            if hit and now - hit[0] < ttl_seconds:
                return hit[1]
            value = fn(*args, **kwargs)
            _CACHE[key] = (now, value)
            return value
        return wrapper
    return deco


# --- data fetchers ------------------------------------------------------------
@memoize(ttl_seconds=600)
def get_standings(season: int):
    """Returns a list of (division_label, dataframe) for the season."""
    return pyb.standings(season)


@memoize(ttl_seconds=600)
def get_team_schedule(team: str, season: int) -> pd.DataFrame:
    return pyb.schedule_and_record(season, team)


@memoize(ttl_seconds=900)
def get_team_batting(season: int) -> pd.DataFrame:
    return pyb.team_batting(season)


@memoize(ttl_seconds=900)
def get_team_pitching(season: int) -> pd.DataFrame:
    return pyb.team_pitching(season)


@memoize(ttl_seconds=900)
def get_batting_leaders(season: int) -> pd.DataFrame:
    return pyb.batting_stats(season, qual=50)


@memoize(ttl_seconds=900)
def get_pitching_leaders(season: int) -> pd.DataFrame:
    return pyb.pitching_stats(season, qual=20)


# --- helpers ------------------------------------------------------------------
def default_team() -> str:
    t = request.cookies.get(DEFAULT_TEAM_COOKIE)
    if t and t in TEAM_MAP:
        return t
    return "NYY"


def selected_team() -> str:
    """Team currently being viewed - query arg wins, then cookie, then default."""
    t = request.args.get("team")
    if t and t in TEAM_MAP:
        return t
    return default_team()


def safe_call(fn, *args, **kwargs):
    """Run a pybaseball call and surface errors as a (None, message) tuple
    instead of 500ing the whole page."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def render_or_error(template: str, error: str | None, **ctx):
    if error:
        return render_template("partials/_error.html", error=error)
    return render_template(template, **ctx)


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
        season=CURRENT_SEASON,
    )


@app.route("/set-default")
def set_default():
    """htmx target: persists the cookie and re-renders the star + label."""
    team = request.args.get("team")
    if team not in TEAM_MAP:
        abort(400)
    html = render_template(
        "partials/_default_badge.html",
        team=team,
        team_map=TEAM_MAP,
    )
    resp = make_response(html)
    # 1 year
    resp.set_cookie(DEFAULT_TEAM_COOKIE, team, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.route("/team/overview")
def team_overview():
    team = selected_team()
    season = int(request.args.get("season", CURRENT_SEASON))

    sched, sched_err = safe_call(get_team_schedule, team, season)
    batting, bat_err = safe_call(get_team_batting, season)
    pitching, pit_err = safe_call(get_team_pitching, season)

    # last 10 results
    recent = []
    record = {"W": 0, "L": 0, "pct": "—", "rs": 0, "ra": 0, "streak": "—"}
    next_games = []
    if sched is not None and not sched.empty:
        played = sched.dropna(subset=["W/L"]) if "W/L" in sched.columns else sched.iloc[0:0]
        wins = (played["W/L"].astype(str).str.startswith("W")).sum() if not played.empty else 0
        losses = (played["W/L"].astype(str).str.startswith("L")).sum() if not played.empty else 0
        record["W"] = int(wins)
        record["L"] = int(losses)
        if wins + losses:
            record["pct"] = f"{wins / (wins + losses):.3f}".lstrip("0")
        if "R" in played.columns and "RA" in played.columns:
            record["rs"] = int(pd.to_numeric(played["R"], errors="coerce").sum())
            record["ra"] = int(pd.to_numeric(played["RA"], errors="coerce").sum())
        if "Streak" in played.columns and not played.empty:
            last = played.iloc[-1]["Streak"]
            if isinstance(last, str) and last:
                # baseball-reference uses "+++" / "---"; convert to W3 / L3
                if set(last) == {"+"}:
                    record["streak"] = f"W{len(last)}"
                elif set(last) == {"-"}:
                    record["streak"] = f"L{len(last)}"
                else:
                    record["streak"] = last

        recent = played.tail(10).to_dict("records")

        upcoming = sched[sched["W/L"].isna()] if "W/L" in sched.columns else sched
        next_games = upcoming.head(5).to_dict("records")

    # Team's own row from team batting / pitching
    team_bat_row = None
    team_pit_row = None
    if batting is not None and "Team" in batting.columns:
        match = batting[batting["Team"].astype(str).str.upper() == team]
        if not match.empty:
            team_bat_row = match.iloc[0].to_dict()
    if pitching is not None and "Team" in pitching.columns:
        match = pitching[pitching["Team"].astype(str).str.upper() == team]
        if not match.empty:
            team_pit_row = match.iloc[0].to_dict()

    return render_template(
        "partials/_overview.html",
        team=team,
        team_info=TEAM_MAP[team],
        season=season,
        record=record,
        recent=recent,
        next_games=next_games,
        team_bat_row=team_bat_row,
        team_pit_row=team_pit_row,
        sched_err=sched_err,
        bat_err=bat_err,
        pit_err=pit_err,
    )


@app.route("/team/schedule")
def team_schedule():
    team = selected_team()
    season = int(request.args.get("season", CURRENT_SEASON))
    sched, err = safe_call(get_team_schedule, team, season)
    games = sched.to_dict("records") if sched is not None else []
    return render_or_error(
        "partials/_schedule.html",
        err,
        team=team,
        team_info=TEAM_MAP[team],
        season=season,
        games=games,
    )


@app.route("/team/batting")
def team_batting():
    team = selected_team()
    season = int(request.args.get("season", CURRENT_SEASON))
    leaders, err = safe_call(get_batting_leaders, season)
    if err:
        return render_template("partials/_error.html", error=err)

    team_players = []
    if leaders is not None and "Team" in leaders.columns:
        df = leaders[leaders["Team"].astype(str).str.upper() == team]
        cols = [c for c in ["Name", "G", "PA", "AB", "H", "HR", "RBI", "SB", "BB", "SO",
                            "AVG", "OBP", "SLG", "OPS", "wRC+", "WAR"] if c in df.columns]
        df = df[cols].sort_values("WAR" if "WAR" in cols else cols[0], ascending=False)
        team_players = df.to_dict("records")

    return render_template(
        "partials/_batting.html",
        team=team,
        team_info=TEAM_MAP[team],
        season=season,
        players=team_players,
    )


@app.route("/team/pitching")
def team_pitching():
    team = selected_team()
    season = int(request.args.get("season", CURRENT_SEASON))
    leaders, err = safe_call(get_pitching_leaders, season)
    if err:
        return render_template("partials/_error.html", error=err)

    team_players = []
    if leaders is not None and "Team" in leaders.columns:
        df = leaders[leaders["Team"].astype(str).str.upper() == team]
        cols = [c for c in ["Name", "W", "L", "ERA", "G", "GS", "SV", "IP", "SO", "BB",
                            "HR", "WHIP", "FIP", "K/9", "BB/9", "WAR"] if c in df.columns]
        df = df[cols].sort_values("WAR" if "WAR" in cols else cols[0], ascending=False)
        team_players = df.to_dict("records")

    return render_template(
        "partials/_pitching.html",
        team=team,
        team_info=TEAM_MAP[team],
        season=season,
        players=team_players,
    )


@app.route("/standings")
def standings():
    season = int(request.args.get("season", CURRENT_SEASON))
    data, err = safe_call(get_standings, season)
    if err:
        return render_template("partials/_error.html", error=err)

    divisions = []
    for df in data:
        # pybaseball gives a small df with a name in df.columns[0] like "AL East"
        label = df.columns[0]
        rows = df.to_dict("records")
        divisions.append({"label": label, "rows": rows, "cols": list(df.columns)})
    return render_template(
        "partials/_standings.html",
        season=season,
        divisions=divisions,
        default=default_team(),
        team_map=TEAM_MAP,
    )


@app.route("/leaders/batting")
def leaders_batting():
    season = int(request.args.get("season", CURRENT_SEASON))
    df, err = safe_call(get_batting_leaders, season)
    if err:
        return render_template("partials/_error.html", error=err)

    cols = [c for c in ["Name", "Team", "G", "PA", "HR", "RBI", "SB",
                        "AVG", "OBP", "SLG", "OPS", "wRC+", "WAR"] if c in df.columns]
    df = df[cols].sort_values("WAR" if "WAR" in cols else cols[0], ascending=False).head(25)
    return render_template(
        "partials/_leaders.html",
        season=season,
        title="Batting leaders",
        cols=cols,
        rows=df.to_dict("records"),
    )


@app.route("/leaders/pitching")
def leaders_pitching():
    season = int(request.args.get("season", CURRENT_SEASON))
    df, err = safe_call(get_pitching_leaders, season)
    if err:
        return render_template("partials/_error.html", error=err)

    cols = [c for c in ["Name", "Team", "W", "L", "ERA", "G", "GS", "IP", "SO",
                        "WHIP", "FIP", "K/9", "WAR"] if c in df.columns]
    df = df[cols].sort_values("WAR" if "WAR" in cols else cols[0], ascending=False).head(25)
    return render_template(
        "partials/_leaders.html",
        season=season,
        title="Pitching leaders",
        cols=cols,
        rows=df.to_dict("records"),
    )


@app.template_filter("fmt")
def fmt(value):
    """Format numbers nicely for table cells."""
    if value is None:
        return "—"
    if isinstance(value, float):
        if pd.isna(value):
            return "—"
        # rate stats stay 3 decimals, others get 2
        if 0 < value < 1:
            return f"{value:.3f}".lstrip("0")
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    return value


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
