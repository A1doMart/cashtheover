#!/usr/bin/env python3
"""EdgeOS daily slate updater — MLB + NBA + NFL."""

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
MLB_SAVANT_PITCHER = "https://baseballsavant.mlb.com/leaderboard/expected_statistics?type=pitcher&year={year}&position=&team=&filterType=bip&min=0&csv=true"
MLB_SAVANT_TEAM    = "https://baseballsavant.mlb.com/leaderboard/expected_statistics?type=batter-team&year={year}&position=&team=&filterType=bip&min=0&csv=true"
NBA_TEAM_STATS_URL = "https://stats.nba.com/stats/leaguedashteamstats?Conference=&DateFrom=&DateTo=&Division=&GameScope=&GameSegment=&LastNGames=0&LeagueID=00&Location=&MeasureType=Advanced&Month=0&OpponentTeamID=0&Outcome=&PORound=0&PaceAdjust=N&PerMode=PerGame&Period=0&PlayerExperience=&PlayerPosition=&PlusMinus=N&Rank=N&Season={season}&SeasonSegment=&SeasonType=Regular+Season&ShotClockRange=&StarterBench=&TeamID=0&TwoWay=0&VsConference=&VsDivision="
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

PATCH_HELPERS = """
function currentSportRows(rows){return rows.filter(r=>activeSport==='mlb'?(!r.sport||r.sport==='mlb'):r.sport===activeSport);}
function currentDateLabel(){if(activeSport==='mlb')return typeof CURRENT_SLATE_DATE==='string'?CURRENT_SLATE_DATE:SLATE_DATE;if(activeSport==='nba')return'TODAY';return NFL_CURRENT_SLATE.length?'LIVE NFL SLATE':'OFFSEASON';}
function sportName(){return activeSport==='mlb'?'MLB':activeSport==='nba'?'NBA':'NFL';}
function thirdModelName(){return activeSport==='mlb'?'RUN LINE':'SPREAD';}
function thirdModelShort(){return activeSport==='mlb'?'RL':'ATS';}
function moneylineHomeEdge(model){if(model.homeEdge!=null)return model.homeEdge;return(model.homeWinProb||0.5)-(model.mktHome||0.5);}
function moneylineAwayEdge(model){if(model.awayEdge!=null)return model.awayEdge;return(model.awayWinProb||0.5)-(model.mktAway||0.5);}
function totalsLineForGame(game){return game.line??game.total??game.model_t?.line??'--';}
function totalsEdgeValue(model){return model.edgeRuns!=null?model.edgeRuns:model.totalEdge;}
function totalsStdValue(model){return model.sd!=null?model.sd:'--';}
function spreadMarketProb(model){return model.bestSide==='HOME'?(model.mktHomeRL??model.mktHome):(model.mktAwayRL??model.mktAway);}
function buildMetaRow(game){if(activeSport==='mlb'){const xA=game.away_xera!=null&&game.away_era!=null?(game.away_era-game.away_xera<-0.8?'<div class="xflag warn">ERA '+game.away_era.toFixed(2)+' vs xERA '+game.away_xera.toFixed(2)+' -- LUCKY</div>':game.away_era-game.away_xera>0.8?'<div class="xflag good">ERA '+game.away_era.toFixed(2)+' vs xERA '+game.away_xera.toFixed(2)+' -- UNLUCKY</div>':''):'';const xH=game.home_xera!=null&&game.home_era!=null?(game.home_era-game.home_xera<-0.8?'<div class="xflag warn">ERA '+game.home_era.toFixed(2)+' vs xERA '+game.home_xera.toFixed(2)+' -- LUCKY</div>':game.home_era-game.home_xera>0.8?'<div class="xflag good">ERA '+game.home_era.toFixed(2)+' vs xERA '+game.home_xera.toFixed(2)+' -- UNLUCKY</div>':''):'';return '<div class="prow"><div class="pp"><div class="ppname">'+(game.away_pitcher||'TBD')+'</div><div class="ppside">AWAY -- '+game.away+'</div><div class="ppstats">'+(game.away_era!=null?'<div class="ps"><span class="k">ERA </span><span class="v">'+game.away_era.toFixed(2)+'</span></div>':'')+(game.away_k9!=null?'<div class="ps"><span class="k">K/9 </span><span class="v">'+game.away_k9.toFixed(1)+'</span></div>':'')+(game.away_xera!=null?'<div class="ps"><span class="k">xERA </span><span class="v">'+game.away_xera.toFixed(2)+'</span></div>':'')+'</div>'+xA+'</div><div class="pp"><div class="ppname">'+(game.home_pitcher||'TBD')+'</div><div class="ppside">HOME -- '+game.home+'</div><div class="ppstats">'+(game.home_era!=null?'<div class="ps"><span class="k">ERA </span><span class="v">'+game.home_era.toFixed(2)+'</span></div>':'')+(game.home_k9!=null?'<div class="ps"><span class="k">K/9 </span><span class="v">'+game.home_k9.toFixed(1)+'</span></div>':'')+(game.home_xera!=null?'<div class="ps"><span class="k">xERA </span><span class="v">'+game.home_xera.toFixed(2)+'</span></div>':'')+'</div>'+xH+'</div></div>';}if(activeSport==='nba'){return '<div class="prow"><div class="pp"><div class="ppname">'+game.away+'</div><div class="ppside">AWAY</div><div class="ppstats">'+(game.away_offrtg!=null?'<div class="ps"><span class="k">ORtg </span><span class="v">'+game.away_offrtg+'</span></div>':'')+(game.away_defrtg!=null?'<div class="ps"><span class="k">DRtg </span><span class="v">'+game.away_defrtg+'</span></div>':'')+'</div></div><div class="pp"><div class="ppname">'+game.home+'</div><div class="ppside">HOME</div><div class="ppstats">'+(game.home_offrtg!=null?'<div class="ps"><span class="k">ORtg </span><span class="v">'+game.home_offrtg+'</span></div>':'')+(game.home_defrtg!=null?'<div class="ps"><span class="k">DRtg </span><span class="v">'+game.home_defrtg+'</span></div>':'')+'</div></div></div>';}return '<div class="prow"><div class="pp"><div class="ppname">'+game.away+'</div><div class="ppside">AWAY</div></div><div class="pp"><div class="ppname">'+game.home+'</div><div class="ppside">HOME</div></div></div>';}
"""

def to_float(v, default=None):
    try: return float(v)
    except: return default

def safe_get(session, url, params=None, headers=None, timeout=20):
    try:
        r = session.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json() if "json" in r.headers.get("Content-Type","") else r.text
    except Exception as e:
        print(f"  [warn] {url[:70]} -> {e}")
        return None

def read_cache(path, max_hours=20):
    if path.exists() and (datetime.now().timestamp()-path.stat().st_mtime)/3600 < max_hours:
        return path.read_text(encoding="utf-8")
    return None

def write_cache(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def fetch_csv(session, url, cache_path, max_hours=20):
    cached = read_cache(cache_path, max_hours)
    if cached is None:
        try:
            r = session.get(url, timeout=30); r.raise_for_status()
            cached = r.text; write_cache(cache_path, cached)
        except Exception as e:
            print(f"  [warn] CSV {url[:60]}: {e}"); return []
    return list(csv.DictReader(io.StringIO(cached.lstrip("\ufeff"))))

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
    bk = next((b for b in bks if "draftkings" in b.get("key","")), None) or bks[0]
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
            if o["name"]=="Over": out["total"]=float(o.get("point",0)); out["total_over_odds"]=int(o["price"])
            elif o["name"]=="Under": out["total_under_odds"]=int(o["price"])
    return out

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
    rows = fetch_csv(session, MLB_SAVANT_PITCHER.format(year=year), CACHE_DIR/f"savant_p_{year}.csv")
    idx = {}
    for r in rows:
        name = (r.get("last_name, first_name") or r.get("last_name","")).strip().lower()
        pid = str(r.get("player_id","")).strip()
        xera = to_float(r.get("xera"))
        if name: idx[name] = xera
        if pid: idx[pid] = xera
    return idx

def savant_team_index(session, year):
    rows = fetch_csv(session, MLB_SAVANT_TEAM.format(year=year), CACHE_DIR/f"savant_t_{year}.csv")
    return {r.get("team_name","").strip().lower(): {
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
        away_p = away_prob.get(
