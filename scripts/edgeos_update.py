#!/usr/bin/env python3
"""EdgeOS daily slate updater — MLB + NBA + NFL.

Architecture: each sport is fully decoupled with its own build, inject,
and grade pipeline. A failure in one sport never affects another.

Data sources:
  MLB  : MLB Stats API + Baseball Savant xERA + The Odds API
  NBA  : Hardcoded 2025-26 ratings + The Odds API schedule/odds + ESPN injuries
  NFL  : The Odds API (offseason = empty; auto-populates in September)

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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

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
ODDS_BASE   = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
ODDS_SCORES = "https://api.the-odds-api.com/v4/sports/{sport}/scores/"
ODDS_SPORTS = {"mlb": "baseball_mlb", "nba": "basketball_nba", "nfl": "americanfootball_nfl"}
ESPN_NBA_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"

MLB_LG = {"era": 4.05, "k9": 8.4, "wrc": 100, "bull_era": 4.05}
NBA_LG = {"offrtg": 116, "defrtg": 116, "pace": 98}

INJURY_IMPACT = {
    "out":          {"offrtg": -4.0, "defrtg": +2.0},
    "doubtful":     {"offrtg": -2.5, "defrtg": +1.5},
    "questionable": {"offrtg": -1.5, "defrtg": +1.0},
}
STAR_PPG_THRESHOLD = 18.0

NBA_NAME_FIXES = {"Los Angeles Clippers": "LA Clippers"}

# Hardcoded 2025-26 regular season ratings (NBA Stats API blocks cloud servers)
NBA_TEAM_RATINGS: Dict[str, Dict[str, float]] = {
    "Oklahoma City Thunder":  {"offrtg": 118.9, "defrtg": 107.3, "pace": 99.1},
    "Boston Celtics":         {"offrtg": 120.5, "defrtg": 112.6, "pace": 97.8},
    "Cleveland Cavaliers":    {"offrtg": 117.8, "defrtg": 109.4, "pace": 95.2},
    "New York Knicks":        {"offrtg": 117.4, "defrtg": 112.7, "pace": 96.8},
    "Indiana Pacers":         {"offrtg": 119.2, "defrtg": 116.1, "pace": 101.3},
    "Miami Heat":             {"offrtg": 118.6, "defrtg": 113.0, "pace": 97.4},
    "Milwaukee Bucks":        {"offrtg": 116.8, "defrtg": 115.2, "pace": 98.6},
    "Detroit Pistons":        {"offrtg": 117.1, "defrtg": 109.6, "pace": 98.9},
    "Atlanta Hawks":          {"offrtg": 118.4, "defrtg": 117.8, "pace": 100.2},
    "Chicago Bulls":          {"offrtg": 114.9, "defrtg": 116.4, "pace": 98.1},
    "Philadelphia 76ers":     {"offrtg": 113.8, "defrtg": 116.3, "pace": 96.1},
    "Toronto Raptors":        {"offrtg": 114.2, "defrtg": 113.3, "pace": 97.6},
    "Charlotte Hornets":      {"offrtg": 113.6, "defrtg": 118.2, "pace": 99.4},
    "Washington Wizards":     {"offrtg": 110.4, "defrtg": 121.3, "pace": 98.7},
    "Brooklyn Nets":          {"offrtg": 109.8, "defrtg": 120.6, "pace": 97.3},
    "Denver Nuggets":         {"offrtg": 119.8, "defrtg": 114.2, "pace": 98.4},
    "Minnesota Timberwolves": {"offrtg": 118.2, "defrtg": 113.4, "pace": 97.9},
    "San Antonio Spurs":      {"offrtg": 119.4, "defrtg": 111.5, "pace": 99.8},
    "Los Angeles Lakers":     {"offrtg": 117.6, "defrtg": 114.8, "pace": 98.2},
    "Golden State Warriors":  {"offrtg": 116.4, "defrtg": 114.9, "pace": 99.6},
    "Houston Rockets":        {"offrtg": 114.8, "defrtg": 113.4, "pace": 97.8},
    "Los Angeles Clippers":   {"offrtg": 116.2, "defrtg": 115.6, "pace": 98.3},
    "LA Clippers":            {"offrtg": 116.2, "defrtg": 115.6, "pace": 98.3},
    "Phoenix Suns":           {"offrtg": 115.6, "defrtg": 117.4, "pace": 99.1},
    "Dallas Mavericks":       {"offrtg": 118.8, "defrtg": 116.2, "pace": 97.6},
    "Sacramento Kings":       {"offrtg": 117.4, "defrtg": 116.8, "pace": 100.4},
    "New Orleans Pelicans":   {"offrtg": 113.2, "defrtg": 116.4, "pace": 97.2},
    "Memphis Grizzlies":      {"offrtg": 115.8, "defrtg": 115.2, "pace": 98.8},
    "Portland Trail Blazers": {"offrtg": 111.4, "defrtg": 116.6, "pace": 100.1},
    "Utah Jazz":              {"offrtg": 110.8, "defrtg": 119.4, "pace": 98.6},
    "Orlando Magic":          {"offrtg": 114.6, "defrtg": 110.8, "pace": 96.4},
}


# ══════════════════════════════════════════════════════════════════════════════
# SPORT SLATE RESULT — independent failure boundary per sport
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SportSlateResult:
    sport: str
    games: Optional[List[Dict[str, Any]]]  # None = preserve existing HTML slate
    status: str                             # "ok" | "empty" | "preserve" | "error"
    warnings: List[str] = field(default_factory=list)
    preserve_existing: bool = False         # True = skip injection, keep what's in HTML

    def log(self) -> None:
        n = len(self.games) if self.games is not None else "preserved"
        print(f"  [{self.sport.upper()}] status={self.status} games={n}")
        for w in self.warnings:
            print(f"    [warn] {w}")


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def to_float(v: Any, default=None) -> Optional[float]:
    try: return float(v)
    except: return default

def safe_get(session, url, params=None, headers=None, timeout=20, retries=2):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json() if "json" in r.headers.get("Content-Type", "") else r.text
        except Exception as e:
            if attempt < retries:
                time.sleep(2)
                continue
            print(f"  [warn] {url[:70]} -> {e}")
            return None

def read_cache(path: Path, max_hours: int = 20) -> Optional[str]:
    if path.exists() and (datetime.now().timestamp() - path.stat().st_mtime) / 3600 < max_hours:
        return path.read_text(encoding="utf-8")
    return None

def write_cache(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def fetch_csv(session, url: str, cache_path: Path, max_hours: int = 20) -> List[Dict]:
    cached = read_cache(cache_path, max_hours)
    if cached is None:
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            cached = r.text
            write_cache(cache_path, cached)
        except Exception as e:
            print(f"  [warn] CSV {url[:60]}: {e}")
            return []
    return list(csv.DictReader(io.StringIO(cached.lstrip("\ufeff"))))

def jsv(v: Any) -> str:
    if v is None: return "null"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int, float)): return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"

def games_js(games: List[Dict]) -> str:
    if not games: return "[]"
    rows = ["  {" + ",".join(f"{k}:{jsv(v)}" for k, v in g.items()) + "}" for g in games]
    return "[\n" + ",\n".join(rows) + "\n]"

def replace_in_html(html: str, pattern: str, block: str, label: str) -> str:
    updated, n = re.subn(pattern, block, html, count=1, flags=re.S)
    if n:
        print(f"  Injected {label}")
        return updated
    print(f"  [warn] Pattern not found: {label}")
    return html


# ══════════════════════════════════════════════════════════════════════════════
# ODDS API — shared
# ══════════════════════════════════════════════════════════════════════════════

def fetch_odds(session, sport_key: str, api_key: Optional[str]) -> List[Dict]:
    if not api_key: return []
    data = safe_get(session, ODDS_BASE.format(sport=sport_key), params={
        "apiKey": api_key, "regions": "us",
        "markets": "h2h,spreads,totals", "oddsFormat": "american", "dateFormat": "iso",
    })
    return data if isinstance(data, list) else []

def fetch_scores(session, sport_key: str, api_key: Optional[str], days_from: int = 1) -> List[Dict]:
    if not api_key: return []
    data = safe_get(session, ODDS_SCORES.format(sport=sport_key), params={
        "apiKey": api_key, "daysFrom": days_from, "dateFormat": "iso"
    })
    return data if isinstance(data, list) else []

def normalize_name(n: str) -> str:
    return re.sub(r"[^a-z]", "", n.lower())

def match_odds(odds_games: List[Dict], home: str, away: str) -> Dict:
    hn, an = normalize_name(home), normalize_name(away)
    for og in odds_games:
        oh = normalize_name(og.get("home_team", ""))
        oa = normalize_name(og.get("away_team", ""))
        if (hn in oh or oh in hn) and (an in oa or oa in an):
            return parse_odds_game(og)
    return {}

def parse_odds_game(og: Dict) -> Dict:
    out: Dict[str, Any] = {}
    home = og.get("home_team", "")
    bks = og.get("bookmakers", [])
    if not bks: return out
    bk = (next((b for b in bks if "draftkings" in b.get("key", "")), None) or
          next((b for b in bks if "fanduel"    in b.get("key", "")), None) or bks[0])
    markets = {m["key"]: m for m in bk.get("markets", [])}
    if "h2h" in markets:
        for o in markets["h2h"].get("outcomes", []):
            try:
                if o["name"] == home: out["ml_home"] = int(o["price"])
                else:                 out["ml_away"] = int(o["price"])
            except (KeyError, ValueError): pass
    if "spreads" in markets:
        for o in markets["spreads"].get("outcomes", []):
            try:
                pt = float(o.get("point", 0))
                if o["name"] == home:
                    out["spread"] = pt
                    out["spread_home"] = int(o["price"])
                    # MLB run line: exactly ±1.5
                    if abs(abs(pt) - 1.5) < 0.01:
                        out["rl_home"] = int(o["price"])
                else:
                    out["spread_away"] = int(o["price"])
                    if abs(abs(pt) - 1.5) < 0.01:
                        out["rl_away"] = int(o["price"])
            except (KeyError, ValueError): pass
    if "totals" in markets:
        for o in markets["totals"].get("outcomes", []):
            try:
                if o["name"] == "Over":
                    out["total"] = float(o.get("point", 0))
                    out["total_over_odds"] = int(o["price"])
                elif o["name"] == "Under":
                    out["total_under_odds"] = int(o["price"])
            except (KeyError, ValueError): pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
# MLB — build / inject / grade
# ══════════════════════════════════════════════════════════════════════════════

def mlb_pitcher_stats(session, pid: int, year: int) -> Dict:
    data = safe_get(session, MLB_PERSON_STATS.format(pid=pid),
                    params={"stats": "season", "group": "pitching", "season": year})
    if not data: return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits", [])
    if not splits: return {}
    s = splits[0].get("stat", {})
    ip = float(s.get("inningsPitched", 0) or 0)
    so = int(s.get("strikeOuts", 0) or 0)
    return {"era": to_float(s.get("era")), "k9": round(so / ip * 9, 2) if ip > 0 else None}

def mlb_team_bull(session, tid: int, year: int) -> Dict:
    data = safe_get(session, MLB_TEAM_STATS_URL.format(tid=tid),
                    params={"stats": "season", "group": "pitching", "season": year})
    if not data: return {}
    splits = ((data.get("stats") or [{}])[0]).get("splits", [])
    rp = [sp for sp in splits if sp.get("position", {}).get("abbreviation") == "RP"]
    return {"bull_era": to_float((rp[0].get("stat", {}) if rp else {}).get("era"))}

def savant_pitcher_index(session, year: int) -> Dict:
    rows = fetch_csv(session, MLB_SAVANT_PITCHER.format(year=year),
                     CACHE_DIR / f"savant_p_{year}.csv")
    idx: Dict[str, Any] = {}
    for r in rows:
        name = (r.get("last_name, first_name") or r.get("last_name", "")).strip().lower()
        pid  = str(r.get("player_id", "")).strip()
        xera = to_float(r.get("xera"))
        if name: idx[name] = xera
        if pid:  idx[pid]  = xera
    return idx

def savant_team_index(session, year: int) -> Dict:
    rows = fetch_csv(session, MLB_SAVANT_TEAM.format(year=year),
                     CACHE_DIR / f"savant_t_{year}.csv")
    return {
        r.get("team_name", "").strip().lower(): {
            "wrc": to_float(r.get("wrc_plus") or r.get("wrc")),
            "hit_xwoba": to_float(r.get("xwoba")),
        }
        for r in rows if r.get("team_name")
    }

def get_xera(savant_idx: Dict, probable: Dict, full_name: str) -> Optional[float]:
    pid = str(probable.get("id", ""))
    if pid and pid in savant_idx: return savant_idx[pid]
    last = (full_name.split()[-1] if full_name and full_name != "TBD" else "").lower()
    for k, v in savant_idx.items():
        if last and last in k: return v
    return None

def build_mlb(session, target_date: date, odds_games: List[Dict]) -> SportSlateResult:
    year = target_date.year
    warnings: List[str] = []
    try:
        data = safe_get(session, MLB_SCHEDULE_URL, params={
            "sportId": 1, "date": target_date.strftime("%Y-%m-%d"),
            "hydrate": "probablePitcher,team"
        })
        schedule = ((data.get("dates") or [{}])[0]).get("games", []) if data else []
    except Exception as e:
        return SportSlateResult("mlb", [], "error", [f"Schedule fetch failed: {e}"])

    if not schedule:
        return SportSlateResult("mlb", [], "empty", ["No MLB games today"])

    sav_p = savant_pitcher_index(session, year)
    sav_t = savant_team_index(session, year)
    games = []

    for game in schedule:
        try:
            teams = game.get("teams", {})
            away_info = teams.get("away", {}).get("team", {})
            home_info = teams.get("home", {}).get("team", {})
            away_prob = teams.get("away", {}).get("probablePitcher") or {}
            home_prob = teams.get("home", {}).get("probablePitcher") or {}
            away_name = away_info.get("name", "")
            home_name = home_info.get("name", "")
            away_p = away_prob.get("fullName") or "TBD"
            home_p = home_prob.get("fullName") or "TBD"

            away_s = mlb_pitcher_stats(session, away_prob["id"], year) if away_prob.get("id") else {}
            home_s = mlb_pitcher_stats(session, home_prob["id"], year) if home_prob.get("id") else {}
            away_bull = mlb_team_bull(session, away_info.get("id", 0), year) if away_info.get("id") else {}
            home_bull = mlb_team_bull(session, home_info.get("id", 0), year) if home_info.get("id") else {}
            away_st = sav_t.get(away_name.lower(), {})
            home_st = sav_t.get(home_name.lower(), {})
            odds = match_odds(odds_games, home_name, away_name)
            line = odds.get("total") or 8.5

            g = {
                "home": home_name, "away": away_name,
                "home_pitcher": home_p, "away_pitcher": away_p,
                "home_era": home_s.get("era"), "away_era": away_s.get("era"),
                "home_k9":  home_s.get("k9"),  "away_k9":  away_s.get("k9"),
                "home_xera": get_xera(sav_p, home_prob, home_p),
                "away_xera": get_xera(sav_p, away_prob, away_p),
                "home_wrc": home_st.get("wrc") or MLB_LG["wrc"],
                "away_wrc": away_st.get("wrc") or MLB_LG["wrc"],
                "home_hit_xwoba": home_st.get("hit_xwoba"),
                "away_hit_xwoba": away_st.get("hit_xwoba"),
                "home_bull_era": home_bull.get("bull_era") or MLB_LG["bull_era"],
                "away_bull_era": away_bull.get("bull_era") or MLB_LG["bull_era"],
                "park": (game.get("venue") or {}).get("name"),
                "park_factor": 100, "line": line, "line_open": line,
                "wind": None, "wind_dir": "calm", "temp": None, "elevation": 0,
                "ml_home": odds.get("ml_home"), "ml_away": odds.get("ml_away"),
                "rl_home": odds.get("rl_home"), "rl_away": odds.get("rl_away"),
                "rest_home": 0, "rest_away": 0,
                "form_home": 0.5, "form_away": 0.5, "rd_home": 0, "rd_away": 0,
                "note": f"Auto-fetched {target_date}. xERA from Savant. Odds from The Odds API.",
            }
            games.append(g)
            print(f"  [MLB] {away_name} @ {home_name} | line {line} | {away_p}/{home_p}")
        except Exception as e:
            warnings.append(f"Game build error: {e}")

    return SportSlateResult("mlb", games, "ok", warnings)

def inject_mlb(html: str, result: SportSlateResult, d: date) -> str:
    if result.preserve_existing or result.games is None:
        print("  [MLB] Preserving existing slate")
        return html
    games = result.games or []
    html = replace_in_html(html, r"const RAW_GAMES = \[.*?\];",
                           f"const RAW_GAMES = {games_js(games)};",
                           f"{len(games)} MLB games")
    # Update pill text and date markers
    pill = d.strftime("%b %d").upper()
    html = re.sub(r'id="pill-text">[^<]*</span>',
                  f'id="pill-text">{len(games)} GAMES · {pill}</span>', html, 1)
    return html

def fetch_mlb_scores(session, target_date: date) -> Dict[str, Dict]:
    """Fetch yesterday's final MLB scores from MLB Stats API."""
    yesterday = target_date - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    data = safe_get(session, MLB_SCHEDULE_URL, params={
        "sportId": 1, "date": date_str, "hydrate": "linescore,team"
    })
    if not data: return {}
    games = ((data.get("dates") or [{}])[0]).get("games", [])
    scores: Dict[str, Dict] = {}
    for g in games:
        if g.get("status", {}).get("abstractGameState") != "Final": continue
        home = g.get("teams", {}).get("home", {})
        away = g.get("teams", {}).get("away", {})
        hn = home.get("team", {}).get("name", "")
        an = away.get("team", {}).get("name", "")
        hs = home.get("score")
        aws = away.get("score")
        if hs is not None and aws is not None:
            scores[an + "@" + hn] = {
                "home": hn, "away": an,
                "home_score": hs, "away_score": aws,
                "total": hs + aws, "date": date_str
            }
            print(f"  [MLB Score] {an} @ {hn}: {aws}-{hs} (total {hs + aws})")
    return scores

def auto_grade_mlb(html: str, scores: Dict, target_date: date) -> str:
    """Inject JS that auto-grades yesterday's MLB totals and ML picks."""
    if not scores: return html
    yesterday = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    js = _grade_script(scores, yesterday, ["edgeos-v10-today", "edgeos-ml-v1-backtest"])
    return _inject_script(html, js, f"MLB grade ({len(scores)} scores)")

def auto_grade_mlb_rl(html: str, scores: Dict, target_date: date) -> str:
    """Inject JS that auto-grades yesterday's MLB run line picks."""
    if not scores: return html
    yesterday = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    js = _rl_grade_script(scores, yesterday, "edgeos-rl-v1-backtest")
    return _inject_script(html, js, f"MLB RL grade ({len(scores)} scores)")


# ══════════════════════════════════════════════════════════════════════════════
# NBA — build / inject / grade
# ══════════════════════════════════════════════════════════════════════════════

def nba_season(d: date) -> str:
    y = d.year
    return f"{y}-{str(y + 1)[2:]}" if d.month >= 10 else f"{y - 1}-{str(y)[2:]}"

def fetch_nba_injuries(session) -> Dict[str, List[Dict]]:
    data = safe_get(session, ESPN_NBA_INJURIES, timeout=10)
    if not isinstance(data, dict): return {}
    injuries: Dict[str, List[Dict]] = {}
    for team_entry in (data.get("injuries") or []):
        team_name = team_entry.get("team", {}).get("displayName", "")
        for injury in (team_entry.get("injuries") or []):
            athlete = injury.get("athlete", {})
            status = (injury.get("status") or "").lower()
            stats = athlete.get("statistics") or []
            ppg = to_float(stats[0].get("value") if stats else None, 0)
            if status in INJURY_IMPACT and ppg >= STAR_PPG_THRESHOLD:
                injuries.setdefault(team_name, []).append({
                    "name": athlete.get("displayName", ""),
                    "status": status, "ppg": ppg,
                })
                print(f"  [Injury] {athlete.get('displayName', '')} ({team_name}): {status} ({ppg} ppg)")
    return injuries

def apply_injury_adjustments(games: List[Dict], injuries: Dict) -> List[Dict]:
    if not injuries: return games
    adjusted = []
    for g in games:
        g = dict(g)
        for side in ("home", "away"):
            team = g.get(side, "")
            team_injuries = injuries.get(team, [])
            if not team_injuries: continue
            off_adj = sum(INJURY_IMPACT.get(i["status"], {}).get("offrtg", 0) for i in team_injuries)
            def_adj = sum(INJURY_IMPACT.get(i["status"], {}).get("defrtg", 0) for i in team_injuries)
            notes = [f"{i['name']} {i['status']}" for i in team_injuries]
            if off_adj or def_adj:
                g[f"{side}_offrtg"] = round((g.get(f"{side}_offrtg") or NBA_LG["offrtg"]) + off_adj, 1)
                g[f"{side}_defrtg"] = round((g.get(f"{side}_defrtg") or NBA_LG["defrtg"]) + def_adj, 1)
                g["note"] = g.get("note", "") + f" INJURIES: {'; '.join(notes)}."
                print(f"  [Injury adj] {team}: ORtg {off_adj:+.1f} DRtg {def_adj:+.1f}")
        adjusted.append(g)
    return adjusted

def build_nba(session, target_date: date, odds_games: List[Dict]) -> SportSlateResult:
    if not odds_games:
        return SportSlateResult(
            "nba", None, "preserve",
            ["No NBA odds from API — preserving existing slate"],
            preserve_existing=True
        )

    ratings = NBA_TEAM_RATINGS
    print("  [NBA] Using 2025-26 regular season ratings")

    try:
        injuries = fetch_nba_injuries(session)
    except Exception as e:
        injuries = {}
        print(f"  [NBA] Injury fetch failed: {e}")

    games = []
    for og in odds_games:
        try:
            gd = datetime.fromisoformat(og.get("commence_time", "").replace("Z", "+00:00")).date()
            if abs((gd - target_date).days) > 1: continue
        except Exception:
            pass

        home = NBA_NAME_FIXES.get(og.get("home_team", ""), og.get("home_team", ""))
        away = NBA_NAME_FIXES.get(og.get("away_team", ""), og.get("away_team", ""))
        odds = parse_odds_game(og)
        hr = ratings.get(home, {})
        ar = ratings.get(away, {})

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
            "note": f"Auto-fetched {target_date}. Ratings: 2025-26 season. Odds: The Odds API.",
        }
        games.append(g)
        print(f"  [NBA] {away} @ {home} | total {g['total']} | spread {g['spread']}")

    games = apply_injury_adjustments(games, injuries)
    status = "ok" if games else "empty"
    return SportSlateResult("nba", games, status)

def inject_nba(html: str, result: SportSlateResult) -> str:
    if result.preserve_existing or result.games is None:
        print("  [NBA] Preserving existing slate")
        return html
    return replace_in_html(html, r"const NBA_RAW_GAMES = \[.*?\];",
                           f"const NBA_RAW_GAMES = {games_js(result.games)};",
                           f"{len(result.games)} NBA games")

def fetch_nba_scores(session, api_key: Optional[str], target_date: date) -> Dict[str, Dict]:
    raw = fetch_scores(session, ODDS_SPORTS["nba"], api_key)
    scores: Dict[str, Dict] = {}
    yesterday = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    for g in raw:
        if not g.get("completed"): continue
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        hs = aw = None
        for t in (g.get("scores") or []):
            if t.get("name") == home: hs = to_float(t.get("score"))
            elif t.get("name") == away: aw = to_float(t.get("score"))
        if hs is not None and aw is not None:
            scores[away + "@" + home] = {
                "home": home, "away": away,
                "home_score": hs, "away_score": aw,
                "total": hs + aw, "date": yesterday
            }
            print(f"  [NBA Score] {away} @ {home}: {int(aw)}-{int(hs)} (total {int(hs + aw)})")
    return scores

def auto_grade_nba(html: str, scores: Dict, target_date: date) -> str:
    """Inject JS that auto-grades yesterday's NBA totals, ML, and spread picks."""
    if not scores: return html
    yesterday = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    js = _grade_script(scores, yesterday, ["edgeos-v10-today", "edgeos-ml-v1-backtest"])
    html = _inject_script(html, js, f"NBA ML/totals grade ({len(scores)} scores)")
    js_rl = _rl_grade_script(scores, yesterday, "edgeos-rl-v1-backtest")
    return _inject_script(html, js_rl, f"NBA spread grade ({len(scores)} scores)")


# ══════════════════════════════════════════════════════════════════════════════
# NFL — build / inject / grade
# ══════════════════════════════════════════════════════════════════════════════

def build_nfl(session, target_date: date, odds_games: List[Dict]) -> SportSlateResult:
    if not odds_games:
        return SportSlateResult("nfl", [], "empty", ["NFL offseason or no API data"])

    games = []
    for og in odds_games:
        try:
            gd = datetime.fromisoformat(og.get("commence_time", "").replace("Z", "+00:00")).date()
            if abs((gd - target_date).days) > 4: continue
        except Exception:
            pass

        home = og.get("home_team", "")
        away = og.get("away_team", "")
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
            "note": f"Auto-fetched {target_date}. EPA unavailable — DQ will be lower.",
        }
        games.append(g)
        print(f"  [NFL] {away} @ {home} | total {g['total']} | spread {g['spread']}")

    status = "ok" if games else "empty"
    return SportSlateResult("nfl", games, status)

def inject_nfl(html: str, result: SportSlateResult) -> str:
    if result.preserve_existing or result.games is None:
        print("  [NFL] Preserving existing slate")
        return html
    return replace_in_html(html, r"const NFL_RAW_GAMES = \[.*?\];",
                           f"const NFL_RAW_GAMES = {games_js(result.games)};",
                           f"{len(result.games)} NFL games")

def fetch_nfl_scores(session, api_key: Optional[str], target_date: date) -> Dict[str, Dict]:
    raw = fetch_scores(session, ODDS_SPORTS["nfl"], api_key)
    scores: Dict[str, Dict] = {}
    for g in raw:
        if not g.get("completed"): continue
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        hs = aw = None
        for t in (g.get("scores") or []):
            if t.get("name") == home: hs = to_float(t.get("score"))
            elif t.get("name") == away: aw = to_float(t.get("score"))
        if hs is not None and aw is not None:
            scores[away + "@" + home] = {
                "home": home, "away": away,
                "home_score": hs, "away_score": aw,
                "total": hs + aw
            }
            print(f"  [NFL Score] {away} @ {home}: {int(aw)}-{int(hs)} (total {int(hs + aw)})")
    return scores

def auto_grade_nfl(html: str, scores: Dict, target_date: date) -> str:
    """Inject JS that auto-grades NFL totals, ML, and spread picks."""
    if not scores: return html
    yesterday = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    js = _grade_script(scores, yesterday, ["edgeos-v10-today", "edgeos-ml-v1-backtest"])
    html = _inject_script(html, js, f"NFL ML/totals grade ({len(scores)} scores)")
    js_rl = _rl_grade_script(scores, yesterday, "edgeos-rl-v1-backtest")
    return _inject_script(html, js_rl, f"NFL spread grade ({len(scores)} scores)")


# ══════════════════════════════════════════════════════════════════════════════
# GRADING SCRIPT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _grade_script(scores: Dict, yesterday: str, storage_keys: List[str]) -> str:
    """Build JS that grades totals + ML picks for the given storage keys."""
    calls = "\n".join(
        f"  grade('{k}', '{('totals' if 'today' in k else 'ml')}');"
        for k in storage_keys
    )
    return (
        "<script>\n(function(){\n"
        f"  var scores={json.dumps(scores)};\n"
        f"  var yesterday='{yesterday}';\n"
        "  function grade(key,type){\n"
        "    try{\n"
        "      var rows=JSON.parse(localStorage.getItem(key)||'[]');\n"
        "      var changed=false;\n"
        "      rows.forEach(function(r){\n"
        "        if(r.result||r.date!==yesterday)return;\n"
        "        var sc=scores[r.away+'@'+r.home];\n"
        "        if(!sc)return;\n"
        "        if(type==='totals'){\n"
        "          var pick=(r.pick||'').toUpperCase();\n"
        "          var line=parseFloat(r.line||0);\n"
        "          var total=sc.total;\n"
        "          if(total===line)r.result='PUSH';\n"
        "          else if(pick==='OVER')r.result=total>line?'HIT':'MISS';\n"
        "          else if(pick==='UNDER')r.result=total<line?'HIT':'MISS';\n"
        "          r.actual=total;\n"
        "        }else{\n"
        "          var side=(r.pick_side||'').toUpperCase();\n"
        "          var hs=sc.home_score,as=sc.away_score;\n"
        "          if(side==='HOME')r.result=hs>as?'WIN':'LOSS';\n"
        "          else if(side==='AWAY')r.result=as>hs?'WIN':'LOSS';\n"
        "        }\n"
        "        if(r.result)changed=true;\n"
        "      });\n"
        "      if(changed)localStorage.setItem(key,JSON.stringify(rows));\n"
        "    }catch(e){}\n"
        "  }\n"
        f"{calls}\n"
        "})();\n</script>"
    )

def _rl_grade_script(scores: Dict, yesterday: str, storage_key: str) -> str:
    """Build JS that grades RL/spread picks."""
    return (
        "<script>\n(function(){\n"
        f"  var scores={json.dumps(scores)};\n"
        f"  var yesterday='{yesterday}';\n"
        "  try{\n"
        f"    var rows=JSON.parse(localStorage.getItem('{storage_key}')||'[]');\n"
        "    var changed=false;\n"
        "    rows.forEach(function(r){\n"
        "      if(r.result||r.date!==yesterday)return;\n"
        "      var sc=scores[r.away+'@'+r.home];\n"
        "      if(!sc)return;\n"
        "      var mg=sc.home_score-sc.away_score;\n"
        "      var pk=(r.pick||'');\n"
        "      if(pk.indexOf('+1.5')>=0||pk.indexOf('+7.5')>=0){\n"
        "        r.result=Math.abs(mg)<=Math.abs(parseFloat(pk.match(/[+-][0-9.]+/)||[1.5])||1.5)?'COVER':'MISS';\n"
        "      }else if(pk.indexOf('-1.5')>=0||pk.indexOf('-7.5')>=0){\n"
        "        r.result=mg>=2?'COVER':'MISS';\n"
        "      }else{\n"
        "        var sd=(r.pick_side||'').toUpperCase();\n"
        "        if(sd==='HOME')r.result=mg>=2?'COVER':'MISS';\n"
        "        else if(sd==='AWAY')r.result=mg<=-2?'COVER':'MISS';\n"
        "      }\n"
        "      if(r.result)changed=true;\n"
        "    });\n"
        f"    if(changed)localStorage.setItem('{storage_key}',JSON.stringify(rows));\n"
        "  }catch(e){}\n"
        "})();\n</script>"
    )

def _inject_script(html: str, script: str, label: str) -> str:
    if "</body>" in html:
        html = html.replace("</body>", script + "\n</body>", 1)
        print(f"  Injected grading: {label}")
    return html


# ══════════════════════════════════════════════════════════════════════════════
# SHARED METADATA INJECTION
# ══════════════════════════════════════════════════════════════════════════════

def inject_metadata(html: str, d: date, mlb_count: int) -> str:
    date_iso   = d.strftime("%Y-%m-%d")
    date_label = d.strftime("%b %d %Y").upper()
    html = re.sub(r"const SLATE_DATE = '[0-9-]+'",
                  f"const SLATE_DATE = '{date_iso}'", html, 1)
    html = re.sub(r'(<span id="footer-date">)[^<]*(</span>)',
                  rf'\g<1>{date_label}\g<2>', html, 1)
    html = re.sub(r'(<sub id="brand-sub">)[^<]*(</sub>)',
                  rf'\g<1>v10+ML+RL | {date_iso}\g<2>', html, 1)
    return html


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
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
    print("=" * 55)

    if not args.template.exists():
        print(f"ERROR: template not found: {args.template}")
        return 1

    html = args.template.read_text(encoding="utf-8")
    session = requests.Session()
    session.headers.update({"User-Agent": "edgeos-updater/2.0"})

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("[warn] ODDS_API_KEY not set — odds fields will be null")

    # ── Fetch odds (shared, one call per sport) ────────────────────────────
    print("\nFetching odds...")
    mlb_odds = fetch_odds(session, ODDS_SPORTS["mlb"], api_key)
    nba_odds = fetch_odds(session, ODDS_SPORTS["nba"], api_key)
    nfl_odds = fetch_odds(session, ODDS_SPORTS["nfl"], api_key)
    print(f"  MLB:{len(mlb_odds)} NBA:{len(nba_odds)} NFL:{len(nfl_odds)} games from Odds API")

    # ── Build each sport independently ────────────────────────────────────
    print("\nBuilding MLB slate...")
    mlb_result = build_mlb(session, target_date, mlb_odds)
    mlb_result.log()

    print("\nBuilding NBA slate...")
    nba_result = build_nba(session, target_date, nba_odds)
    nba_result.log()

    print("\nBuilding NFL slate...")
    nfl_result = build_nfl(session, target_date, nfl_odds)
    nfl_result.log()

    # ── Fetch yesterday's scores independently ─────────────────────────────
    print("\nFetching yesterday's scores...")
    mlb_scores = fetch_mlb_scores(session, target_date)
    nba_scores  = fetch_nba_scores(session, api_key, target_date)
    nfl_scores  = fetch_nfl_scores(session, api_key, target_date)
    print(f"  {len(mlb_scores)} MLB | {len(nba_scores)} NBA | {len(nfl_scores)} NFL scores")

    # ── Inject each sport independently ────────────────────────────────────
    print("\nInjecting into HTML...")
    html = inject_mlb(html, mlb_result, target_date)
    html = inject_nba(html, nba_result)
    html = inject_nfl(html, nfl_result)
    html = inject_metadata(html, target_date, len(mlb_result.games or []))

    # ── Auto-grade each sport independently ────────────────────────────────
    html = auto_grade_mlb(html, mlb_scores, target_date)
    html = auto_grade_mlb_rl(html, mlb_scores, target_date)
    html = auto_grade_nba(html, nba_scores, target_date)
    html = auto_grade_nfl(html, nfl_scores, target_date)

    # ── Write output ───────────────────────────────────────────────────────
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")

    mlb_n = len(mlb_result.games or [])
    nba_n = len(nba_result.games) if nba_result.games is not None else "preserved"
    nfl_n = len(nfl_result.games or [])
    print(f"\n✓ {mlb_n} MLB | {nba_n} NBA | {nfl_n} NFL -> {args.output}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
