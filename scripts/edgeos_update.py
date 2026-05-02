#!/usr/bin/env python3
"""EdgeOS daily slate updater — MLB + NBA + NFL.

Pulls today's games, stats, and odds for all three sports and injects
them into edgeos_combined.html so the site stays fresh every day.

Data sources (all free or covered by one Odds API key):
  MLB  : MLB Stats API + Baseball Savant xERA + The Odds API
  NBA  : NBA Stats API team ratings + The Odds API schedule/odds
  NFL  : The Odds API (offseason = empty slate; auto-populates in September)

Usage:
  python edgeos_update.py --template edgeos_combined.html --output edgeos_combined.html
  python edgeos_update.py --template edgeos_combined.html --output edgeos_combined.html --date 2026-09-07
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR  = SCRIPT_DIR / ".cache"

MLB_SCHEDULE_URL   = "https://statsapi.mlb.com/api/v1/schedule"
MLB_PERSON_STATS   = "https://statsapi.mlb.com/api/v1/people/{pid}/stats"
MLB_TEAM_STATS_URL = "https://statsapi.mlb.com/api/v1/teams/{tid}/stats"
MLB_SAVANT_PITCHER = (
    "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    "?type=pitcher&year={year}&position=&team=&filterType=bip&min=0&csv=true"
)
MLB_SAVANT_TEAM = (
    "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    "?type=batter-team&year={year}&position=&team=&filterType=bip&min=0&csv=true"
)
NBA_TEAM_STATS_URL = (
    "https://stats.nba.com/stats/leaguedashteamstats"
    "?Conference=&DateFrom=&DateTo=&Division=&GameScope=&GameSegment="
    "&LastNGames=0&LeagueID=00&Location=&MeasureType=Advanced&Month=0"
    "&OpponentTeamID=0&Outcome=&PORound=0&PaceAdjust=N&PerMode=PerGame"
    "&Period=0&PlayerExperience=&PlayerPosition=&PlusMinus=N&Rank=N"
    "&Season={season}&SeasonSegment=&SeasonType=Regular+Season"
    "&ShotClockRange=&StarterBench=&TeamID=0&TwoWay=0&VsConference=&VsDivision="
)
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}
ODDS_BASE   = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
ODDS_SPORTS = {"mlb": "baseball_mlb", "nba": "basketball_nba", "nfl": "americanfootball_nfl"}
MLB_LG = {"era": 4.05, "k9": 8.4, "wrc": 100, "bull_era": 4.05}
NBA_LG = {"offrtg": 116, "defrtg": 116, "pace": 98}


# ── Utilities ────────────────────────────────────────────────────────────────

def to_float(v: Any, default=None):
    try: return float(v)
    except: return default

def safe_get(session, url, params=None, headers=None, timeout=20, retries=2):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json() if "json" in r.headers.get("Content-Type","") else r.text
        except Exception as e:
            if attempt < retries:
                time.sleep(2)
                continue
            print(f"  [warn] {url[:70]} -> {e}")
            return None

def read_cache(path, max_hours=20):
    if path.exists() and (datetime.now().timestamp()-path.stat().st_mtime)/3600 < max_hours:
        return path.read_text(encoding="utf-8")
    return None

def write_cache(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def fetch_csv(session, url, cache_path, max_hours=20, extra_headers=None):
    cached = read_cache(cache_path, max_hours)
    if cached is None:
        hdrs = dict(session.headers)
        if extra_headers: hdrs.update(extra_headers)
        try:
            r = session.get(url, headers=hdrs, timeout=30); r.raise_for_status()
            cached = r.text; write_cache(cache_path, cached)
        except Exception as e:
            print(f"  [warn] CSV {url[:60]}: {e}"); return []
    return list(csv.DictReader(io.StringIO(cached.lstrip("\ufeff"))))


# ── Odds API ─────────────────────────────────────────────────────────────────

def fetch_odds(session, sport_key, api_key):
    if not api_key: return []
    data = safe_get(session, ODDS_BASE.format(sport=sport_key), params={
        "apiKey": api_key, "regions": "us",
        "markets": "h2h,spreads,totals", "oddsFormat": "american", "dateFormat": "iso",
    })
    return data if isinstance(data, list) else []

def normalize_name(n): return re.sub(r"[^a-z]", "", n.lower())

def match_odds(odds_games, home, away):
    hn, an = normalize_name(home), normalize_name(away)
    for og in odds_games:
        oh = normalize_name(og.get("home_team",""))
        oa = normalize_name(og.get("away_team",""))
        if (hn in oh or oh in hn) and (an in oa or oa in an):
            return parse_odds_game(og)
    return {}

def parse_odds_game(og):
    out = {}
    home = og.get("home_team","")
    bks = og.get("bookmakers", [])
    if not bks: return out
    bk = next((b for b in bks if "draftkings" in b.get("key","")), None) or \
         next((b for b in bks if "fanduel"    in b.get("key","")), None) or bks[0]
    markets = {m["key"]: m for m in bk.get("markets",[])}
    if "h2h" in markets:
        for o in markets["h2h"].get("outcomes",[]):
            if o["name"]==home: out["ml_home"]=int(o["price"])
            else: out["ml_away"]=int(o["price"])
    if "spreads" in markets:
        for o in markets["spreads"].get("outcomes",[]):
            if o["name"]==home: out["spread"]=float(o.get("point",0)); out["spread_home"]=int(o["price"])
            else: out["spread_away"]=int(o["price"])
    if "totals" in markets:
        for o in markets["totals"].get("outcomes",[]):
            if o["name"]=="Over":  out["total"]=float(o.get("point",0)); out["total_over_odds"]=int(o["price"])
            elif o["name"]=="Under": out["total_under_odds"]=int(o["price"])
    return out


# ── MLB ──────────────────────────────────────────────────────────────────────

def mlb_pitcher_stats(session, pid, year):
    data = safe_get(session, MLB_PERSON_STATS.format(pid=pid),
                    params={"stats":"season","group":"pitching","season":year})
    if not data: return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits", [])
    if not splits: return {}
    s = splits[0].get("stat",{})
    ip = float(s.get("inningsPitched",0) or 0)
    so = int(s.get("strikeOuts",0) or 0)
    return {"era": to_float(s.get("era")), "k9": round(so/ip*9,2) if ip>0 else None}

def mlb_team_bull(session, tid, year):
    data = safe_get(session, MLB_TEAM_STATS_URL.format(tid=tid),
                    params={"stats":"season","group":"pitching","season":year})
    if not data: return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits",[])
    rp = [sp for sp in splits if sp.get("position",{}).get("abbreviation")=="RP"]
    return {"bull_era": to_float((rp[0].get("stat",{}) if rp else {}).get("era"))}

def savant_pitcher_index(session, year):
    rows = fetch_csv(session, MLB_SAVANT_PITCHER.format(year=year),
                     CACHE_DIR/f"savant_p_{year}.csv")
    idx = {}
    for r in rows:
        name = (r.get("last_name, first_name") or r.get("last_name","")).strip().lower()
        pid  = str(r.get("player_id","")).strip()
        xera = to_float(r.get("xera"))
        if name: idx[name] = xera
        if pid:  idx[pid]  = xera
    return idx

def savant_team_index(session, year):
    rows = fetch_csv(session, MLB_SAVANT_TEAM.format(year=year),
                     CACHE_DIR/f"savant_t_{year}.csv")
    return {(r.get("team_name","")).strip().lower(): {
        "wrc": to_float(r.get("wrc_plus") or r.get("wrc")),
        "hit_xwoba": to_float(r.get("xwoba")),
    } for r in rows if r.get("team_name")}

def get_xera(savant_idx, probable, full_name):
    pid = str(probable.get("id",""))
    if pid and pid in savant_idx: return savant_idx[pid]
    last = (full_name.split()[-1] if full_name and full_name!="TBD" else "").lower()
    for k,v in savant_idx.items():
        if last and last in k: return v
    return None

def build_mlb_games(session, target_date, odds_games):
    year = target_date.year
    data = safe_get(session, MLB_SCHEDULE_URL, params={
        "sportId":1, "date": target_date.strftime("%Y-%m-%d"), "hydrate":"probablePitcher,team"})
    schedule = ((data.get("dates") or [{}])[0]).get("games",[]) if data else []
    if not schedule: print("  [MLB] No games."); return []

    sav_p = savant_pitcher_index(session, year)
    sav_t = savant_team_index(session, year)
    games = []

    for game in schedule:
        teams = game.get("teams",{})
        away_info = teams.get("away",{}).get("team",{})
        home_info = teams.get("home",{}).get("team",{})
        away_prob = teams.get("away",{}).get("probablePitcher") or {}
        home_prob = teams.get("home",{}).get("probablePitcher") or {}
        away_name = away_info.get("name",""); home_name = home_info.get("name","")
        away_p = away_prob.get("fullName") or "TBD"
        home_p = home_prob.get("fullName") or "TBD"

        away_s = mlb_pitcher_stats(session, away_prob["id"], year) if away_prob.get("id") else {}
        home_s = mlb_pitcher_stats(session, home_prob["id"], year) if home_prob.get("id") else {}
        away_team_bull = mlb_team_bull(session, away_info.get("id",0), year) if away_info.get("id") else {}
        home_team_bull = mlb_team_bull(session, home_info.get("id",0), year) if home_info.get("id") else {}

        away_st = sav_t.get(away_name.lower(),{}); home_st = sav_t.get(home_name.lower(),{})
        odds = match_odds(odds_games, home_name, away_name)
        line = odds.get("total") or 8.5

        g = {
            "home": home_name, "away": away_name,
            "home_pitcher": home_p, "away_pitcher": away_p,
            "home_era": home_s.get("era"), "away_era": away_s.get("era"),
            "home_k9": home_s.get("k9"),  "away_k9": away_s.get("k9"),
            "home_xera": get_xera(sav_p, home_prob, home_p),
            "away_xera": get_xera(sav_p, away_prob, away_p),
            "home_wrc": home_st.get("wrc") or MLB_LG["wrc"],
            "away_wrc": away_st.get("wrc") or MLB_LG["wrc"],
            "home_hit_xwoba": home_st.get("hit_xwoba"),
            "away_hit_xwoba": away_st.get("hit_xwoba"),
            "home_bull_era": home_team_bull.get("bull_era") or MLB_LG["bull_era"],
            "away_bull_era": away_team_bull.get("bull_era") or MLB_LG["bull_era"],
            "park": (game.get("venue") or {}).get("name"),
            "park_factor": 100, "line": line, "line_open": line,
            "wind": None, "wind_dir": "calm", "temp": None, "elevation": 0,
            "ml_home": odds.get("ml_home"), "ml_away": odds.get("ml_away"),
            "rl_home": None, "rl_away": None,
            "rest_home": 0, "rest_away": 0,
            "form_home": 0.5, "form_away": 0.5, "rd_home": 0, "rd_away": 0,
            "note": f"Auto-fetched {target_date}. xERA from Savant. Odds from The Odds API.",
        }
        games.append(g)
        print(f"  [MLB] {away_name} @ {home_name} | line {line} | {away_p}/{home_p}")
    return games


# ── NBA ──────────────────────────────────────────────────────────────────────

NBA_NAME_FIXES = {"LA Clippers": "LA Clippers", "Los Angeles Clippers": "LA Clippers"}

def nba_season(d: date) -> str:
    y = d.year
    return f"{y}-{str(y+1)[2:]}" if d.month >= 10 else f"{y-1}-{str(y)[2:]}"

def fetch_nba_ratings(session, season):
    cache = CACHE_DIR / f"nba_ratings_{season.replace('-','_')}.json"
    if cache.exists() and (datetime.now().timestamp()-cache.stat().st_mtime)/3600 < 6:
        try: return json.loads(cache.read_text())
        except: pass
    time.sleep(0.5)
    data = safe_get(session, NBA_TEAM_STATS_URL.format(season=season), headers=NBA_HEADERS, timeout=30)
    if not isinstance(data, dict): print("  [NBA] Could not fetch team ratings."); return {}
    rs = (data.get("resultSets") or [{}])[0]
    hdrs = {h:i for i,h in enumerate(rs.get("headers",[]))}
    ratings = {}
    for row in rs.get("rowSet",[]):
        name = NBA_NAME_FIXES.get(row[hdrs.get("TEAM_NAME",0)], row[hdrs.get("TEAM_NAME",0)])
        ratings[name] = {
            "offrtg": to_float(row[hdrs.get("OFF_RATING", hdrs.get("E_OFF_RATING",0))]),
            "defrtg": to_float(row[hdrs.get("DEF_RATING", hdrs.get("E_DEF_RATING",0))]),
            "pace":   to_float(row[hdrs.get("PACE",       hdrs.get("E_PACE",0))]),
        }
    if ratings: cache.parent.mkdir(parents=True,exist_ok=True); cache.write_text(json.dumps(ratings))
    return ratings

def build_nba_games(session, target_date, odds_games):
    if not odds_games: print("  [NBA] No odds data."); return []
    # NBA Stats API blocks cloud IPs — use league averages, real odds still work
    ratings = {}
    print("  [NBA] Using league averages for ratings (NBA Stats API blocks cloud servers)")
    games = []
    for og in odds_games:
        try:
            gd = datetime.fromisoformat(og.get("commence_time","").replace("Z","+00:00")).date()
            # Allow +/- 1 day window to handle UTC vs CT timezone differences
            if abs((gd - target_date).days) > 1: continue
        except: pass
        home = NBA_NAME_FIXES.get(og.get("home_team",""), og.get("home_team",""))
        away = NBA_NAME_FIXES.get(og.get("away_team",""), og.get("away_team",""))
        odds = parse_odds_game(og)
        hr = ratings.get(home, {}); ar = ratings.get(away, {})
        g = {
            "home": home, "away": away,
            "home_offrtg": hr.get("offrtg") or NBA_LG["offrtg"],
            "away_offrtg": ar.get("offrtg") or NBA_LG["offrtg"],
            "home_defrtg": hr.get("defrtg") or NBA_LG["defrtg"],
            "away_defrtg": ar.get("defrtg") or NBA_LG["defrtg"],
            "home_pace":   hr.get("pace")   or NBA_LG["pace"],
            "away_pace":   ar.get("pace")   or NBA_LG["pace"],
            "rest_home": 1, "rest_away": 1,
            "total": odds.get("total"), "total_open": odds.get("total"),
            "total_over_odds":  odds.get("total_over_odds")  or -110,
            "total_under_odds": odds.get("total_under_odds") or -110,
            "spread": odds.get("spread"), "spread_open": odds.get("spread"),
            "spread_home": odds.get("spread_home") or -110,
            "spread_away": odds.get("spread_away") or -110,
            "ml_home": odds.get("ml_home"), "ml_away": odds.get("ml_away"),
            "note": f"Auto-fetched {target_date}. Ratings: NBA Stats API. Odds: The Odds API.",
        }
        games.append(g)
        print(f"  [NBA] {away} @ {home} | total {g['total']} | spread {g['spread']}")
    return games


# ── NFL ──────────────────────────────────────────────────────────────────────

def build_nfl_games(session, target_date, odds_games):
    if not odds_games: print("  [NFL] No games (offseason or no key)."); return []
    games = []
    for og in odds_games:
        try:
            gd = datetime.fromisoformat(og.get("commence_time","").replace("Z","+00:00")).date()
            if abs((gd - target_date).days) > 4: continue
        except: pass
        home = og.get("home_team",""); away = og.get("away_team","")
        odds = parse_odds_game(og)
        g = {
            "home": home, "away": away,
            "home_off_epa": None, "away_off_epa": None,
            "home_def_epa": None, "away_def_epa": None,
            "rest_home": 7, "rest_away": 7,
            "total": odds.get("total"), "total_open": odds.get("total"),
            "total_over_odds":  odds.get("total_over_odds")  or -110,
            "total_under_odds": odds.get("total_under_odds") or -110,
            "spread": odds.get("spread"), "spread_open": odds.get("spread"),
            "spread_home": odds.get("spread_home") or -110,
            "spread_away": odds.get("spread_away") or -110,
            "ml_home": odds.get("ml_home"), "ml_away": odds.get("ml_away"),
            "outdoor": True, "wind": None, "temp": None,
            "note": f"Auto-fetched {target_date}. EPA stats unavailable — DQ will be lower.",
        }
        games.append(g)
        print(f"  [NFL] {away} @ {home} | total {g['total']} | spread {g['spread']}")
    return games


# ── HTML injection ───────────────────────────────────────────────────────────

def jsv(v):
    if v is None: return "null"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int, float)): return str(v)
    return "'" + str(v).replace("\\","\\\\").replace("'","\\'") + "'"

def games_js(games):
    if not games: return "[]"
    rows = ["  {" + ",".join(f"{k}:{jsv(v)}" for k,v in g.items()) + "}" for g in games]
    return "[\n" + ",\n".join(rows) + "\n]"

def inject_all(html, mlb, nba, nfl, d: date):
    date_iso   = d.strftime("%Y-%m-%d")
    date_label = d.strftime("%b %d %Y").upper()
    pill_label = d.strftime("%b %d").upper()

    def replace(pattern, block, label):
        nonlocal html
        h, n = re.subn(pattern, block, html, count=1, flags=re.S)
        if n: html = h; print(f"  Injected {label}")
        else: print(f"  [warn] Pattern not found: {label}")

    replace(r"const RAW_GAMES = \[.*?\];",
            f"const RAW_GAMES = {games_js(mlb)};", f"{len(mlb)} MLB games")
    replace(r"const NBA_RAW_GAMES = \[.*?\];",
            f"const NBA_RAW_GAMES = {games_js(nba)};", f"{len(nba)} NBA games")
    replace(r"const NFL_RAW_GAMES = \[.*?\];",
            f"const NFL_RAW_GAMES = {games_js(nfl)};", f"{len(nfl)} NFL games")

    html = re.sub(r"const SLATE_DATE = '[0-9-]+'",  f"const SLATE_DATE = '{date_iso}'", html, 1)
    html = re.sub(r'id="pill-text">[^<]*</span>', f'id="pill-text">{len(mlb)} GAMES · {pill_label}</span>', html, 1)
    html = re.sub(r'(<span id="footer-date">)[^<]*(</span>)', rf'\g<1>{date_label}\g<2>', html, 1)
    html = re.sub(r'(<sub id="brand-sub">)[^<]*(</sub>)',     rf'\g<1>v10+ML+RL | {date_iso}\g<2>', html, 1)
    return html


# ── Entry point ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--template", type=Path, required=True)
    p.add_argument("--output",   type=Path, required=True)
    p.add_argument("--date", dest="target_date")
    p.add_argument("--refresh-cache", action="store_true")
    return p.parse_args()

def main() -> int:
    load_dotenv()
    args = parse_args()
    target_date = (datetime.strptime(args.target_date, "%Y-%m-%d").date()
                   if args.target_date else date.today())

    print(f"\nEdgeOS slate updater | {target_date}")
    print("=" * 50)
    if not args.template.exists():
        print(f"ERROR: template not found: {args.template}"); return 1

    html = args.template.read_text(encoding="utf-8")
    session = requests.Session()
    session.headers.update({"User-Agent": "edgeos-updater/2.0"})

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("[warn] ODDS_API_KEY not set — odds fields will be null")

    print("\nFetching odds...")
    mlb_odds = fetch_odds(session, ODDS_SPORTS["mlb"], api_key)
    nba_odds = fetch_odds(session, ODDS_SPORTS["nba"], api_key)
    nfl_odds = fetch_odds(session, ODDS_SPORTS["nfl"], api_key)
    print(f"  MLB:{len(mlb_odds)} NBA:{len(nba_odds)} NFL:{len(nfl_odds)} games from Odds API")

    print("\nBuilding MLB slate...")
    mlb = build_mlb_games(session, target_date, mlb_odds)
    print("\nBuilding NBA slate...")
    nba = build_nba_games(session, target_date, nba_odds)
    print("\nBuilding NFL slate...")
    nfl = build_nfl_games(session, target_date, nfl_odds)

    print("\nFetching yesterday's scores...")
    yscores = fetch_yesterday_scores(session, target_date)
    print(f"  {len(yscores)} final scores found")

    print("\nInjecting into HTML...")
    html = inject_all(html, mlb, nba, nfl, target_date)
    html = auto_grade_picks(html, yscores, target_date)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"\n✓ {len(mlb)} MLB | {len(nba)} NBA | {len(nfl)} NFL -> {args.output}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
