"""Fetch final NBA regular-season scores for 2026-27 and merge them into scores.json.

Run by .github/workflows/update-scores.yml. Uses only the Python standard library.

Sources, in order:
  1. NBA's public season schedule feed (one request, includes final scores).
  2. ESPN's public scoreboard feed, one request per day (fallback).

Existing scores are never deleted, so a failed fetch can't wipe the file.
The file is rewritten only when a score is added or changed.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

SEASON = "2026-27"
SEASON_START = dt.date(2026, 10, 20)
SEASON_END = dt.date(2027, 4, 11)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scores.json")

NBA_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}&limit=50"

TRICODES = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL", "MEM",
            "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"}
NICKNAMES = {"Hawks": "ATL", "Celtics": "BOS", "Nets": "BKN", "Hornets": "CHA", "Bulls": "CHI", "Cavaliers": "CLE",
             "Mavericks": "DAL", "Nuggets": "DEN", "Pistons": "DET", "Warriors": "GSW", "Rockets": "HOU",
             "Pacers": "IND", "Clippers": "LAC", "Lakers": "LAL", "Grizzlies": "MEM", "Heat": "MIA", "Bucks": "MIL",
             "Timberwolves": "MIN", "Pelicans": "NOP", "Knicks": "NYK", "Thunder": "OKC", "Magic": "ORL",
             "76ers": "PHI", "Suns": "PHX", "Trail Blazers": "POR", "Kings": "SAC", "Spurs": "SAS",
             "Raptors": "TOR", "Jazz": "UTA", "Wizards": "WAS"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nba.com/",
}


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def overtime(text):
    """'Final/OT' -> 'OT', 'Final/2OT' -> '2OT', 'Final' -> ''."""
    m = re.search(r"(\d*)\s*OT", text or "", re.I)
    return (m.group(1) + "OT") if m else ""


def in_season(day):
    return SEASON_START <= day <= SEASON_END + dt.timedelta(days=7)


def parse_nba(data):
    """Final regular-season games from NBA's scheduleLeagueV2 feed."""
    sched = data.get("leagueSchedule") or {}
    if sched.get("seasonYear") != SEASON:
        raise ValueError(f"NBA feed is for season {sched.get('seasonYear')!r}, not {SEASON}")
    out = []
    for gd in sched.get("gameDates", []):
        for g in gd.get("games", []):
            if str(g.get("gameId", ""))[:3] != "002":      # 002 = regular season (includes NBA Cup games)
                continue
            if int(g.get("gameStatus") or 0) != 3:          # 3 = final
                continue
            home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
            h, a = home.get("teamTricode"), away.get("teamTricode")
            if h not in TRICODES or a not in TRICODES:
                continue
            day = (g.get("gameDateEst") or "")[:10]
            if not day:
                continue
            out.append({"d": day, "a": a, "h": h, "as": int(away.get("score") or 0), "hs": int(home.get("score") or 0),
                        "ot": overtime(g.get("gameStatusText"))})
    return out


def parse_espn(data):
    """Final regular-season games from one day of ESPN's scoreboard feed."""
    out = []
    for ev in data.get("events", []):
        if (ev.get("season") or {}).get("type") != 2:
            continue
        comp = (ev.get("competitions") or [{}])[0]
        status = (comp.get("status") or ev.get("status") or {}).get("type") or {}
        if not status.get("completed"):
            continue
        teams = {}
        for c in comp.get("competitors", []):
            code = NICKNAMES.get((c.get("team") or {}).get("name", ""))
            if not code:
                break
            teams[c.get("homeAway")] = (code, int(float(c.get("score") or 0)))
        if set(teams) != {"home", "away"}:
            continue
        # Game dates are kept in US Eastern time. Every NBA tip-off is between noon and
        # 11 pm Eastern, so UTC minus 6 hours always lands on the Eastern calendar date.
        start = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        day = (start - dt.timedelta(hours=6)).date().isoformat()
        out.append({"d": day, "a": teams["away"][0], "h": teams["home"][0], "as": teams["away"][1],
                    "hs": teams["home"][1], "ot": overtime(status.get("detail") or status.get("shortDetail"))})
    return out


def fetch_espn(existing_days, today):
    first = SEASON_START if not existing_days else max(SEASON_START, today - dt.timedelta(days=4))
    games, day = [], first
    while day <= min(today, SEASON_END + dt.timedelta(days=7)):
        try:
            games += parse_espn(get_json(ESPN_URL.format(d=day.strftime("%Y%m%d"))))
        except Exception as e:                                  # keep going; other days may work
            print(f"  ESPN {day}: {e}")
        day += dt.timedelta(days=1)
    return games


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    try:
        with open(OUT) as f:
            current = json.load(f)
    except (FileNotFoundError, ValueError):
        current = {"season": SEASON, "updated": None, "games": []}
    known = {f'{g["d"]}|{g["a"]}|{g["h"]}': g for g in current.get("games", [])}

    found, source = [], None
    try:
        found, source = parse_nba(get_json(NBA_URL)), "NBA"
    except Exception as e:
        print(f"NBA feed unavailable ({e}); trying ESPN")
    if source is None:
        found, source = fetch_espn({g["d"] for g in known.values()}, today), "ESPN"

    changed = 0
    for g in found:
        key = f'{g["d"]}|{g["a"]}|{g["h"]}'
        if known.get(key) != g:
            known[key] = g
            changed += 1

    print(f"Source: {source}. Final games seen: {len(found)}. New or changed: {changed}. Total stored: {len(known)}.")
    if changed or not os.path.exists(OUT):
        current = {"season": SEASON,
                   "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                   "source": source,
                   "games": sorted(known.values(), key=lambda g: (g["d"], g["h"]))}
        with open(OUT, "w") as f:
            json.dump(current, f, separators=(",", ":"))
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
