"""
Player Stat Model V5 LITE - fixes 429 + 0 starters
"""

import os
import json
import time
import math
import hashlib
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

API_KEY = os.environ.get("THESTATSAPI_KEY", "PASTE_YOUR_KEY_HERE")
BASE_URL = "https://api.thestatsapi.com/api/football"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_JSON = Path(__file__).parent / "player_stats_data.json"

ROLLING_WEIGHT = 0.6
ROLLING_MATCHES = 3 # was 10 -> now 3 to save 70% quota
USE_ROLLING = False # set True after your quota resets on 15/09

WATCHLIST = ["Arsenal"] # keep 1 team until 15/09

MIN_AVG_MINUTES = 0 # was 30 - was filtering everyone to 0

CRITERIA_THRESHOLDS = {
    "shots_over_1.5": 0.65,
    "sot_over_0.5": 0.60,
    "to_be_carded": 0.30,
    "goal_or_assist": 0.55,
}

TOP_N_PER_MARKET = 5
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{h}.json"

def cached_get(path: str, params: dict | None = None, ttl_hours: int = 12) -> dict:
    cache_key = path + json.dumps(params or {}, sort_keys=True)
    cfile = _cache_path(cache_key)
    # ALWAYS use cache if exists - this is how we stay under 10k
    if cfile.exists():
        try:
            return json.loads(cfile.read_text())
        except:
            pass

    for attempt in range(3):
        resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=25)
        if resp.status_code == 429:
            print(f" 429 on {path}, waiting 20s... (quota saver)")
            time.sleep(20)
            continue
        if resp.status_code == 400:
            print(f" 400 on {path} {params} -> {resp.text[:300]}")
        try:
            resp.raise_for_status()
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(5)
            continue
        data = resp.json()
        cfile.write_text(json.dumps(data))
        time.sleep(2.2) # 2.2 sec = 27 req/min = under 30/min limit
        return data
    raise Exception(f"Rate limit giving up on {path}")

def search_player(name: str) -> dict:
    data = cached_get("/players", {"search": name}, ttl_hours=24 * 7)
    results = data.get("data", data.get("players", []))
    if not results:
        raise ValueError(f"No player found for '{name}'")
    return results[0]

def get_player_profile(player_id: str) -> dict:
    return cached_get(f"/players/{player_id}", ttl_hours=24*30)

def get_team_recent_matches(team_id: str, limit: int = ROLLING_MATCHES) -> list[dict]:
    data = cached_get(f"/teams/{team_id}/matches", {"status": "finished", "per_page": limit, "order": "desc"}, ttl_hours=24)
    return data.get("data", data.get("matches", []))[:limit]

def get_match_player_stats(match_id: str, player_id: str) -> dict | None:
    try:
        data = cached_get(f"/matches/{match_id}/player-stats", ttl_hours=24 * 90)
    except:
        return None
    rows = data.get("data", data.get("player_stats", []))
    for row in rows:
        if str(row.get("player_id")) == str(player_id):
            return row
    return None

def search_team(name: str) -> dict:
    data = cached_get("/teams", {"search": name}, ttl_hours=24 * 30)
    results = data.get("data", data.get("teams", []))
    if not results:
        print(f" Trying full team list for '{name}'...")
        all_teams = cached_get("/teams", {"per_page": 100}, ttl_hours=24*30)
        all_results = all_teams.get("data", all_teams.get("teams", []))
        for t in all_results:
            if name.lower() in t.get("name","").lower():
                return t
        raise ValueError(f"No team found for '{name}'")
    return results[0]

def get_team_squad(team_id: str) -> list[dict]:
    data = cached_get(f"/teams/{team_id}/players", ttl_hours=24 * 7)
    return data.get("data", data.get("players", []))

def get_upcoming_matches(competition_id: str, days_ahead: int = 7) -> list[dict]:
    today = date.today()
    data = cached_get("/matches", {"competition_id": competition_id, "date_from": today.isoformat(), "date_to": (today + timedelta(days=days_ahead)).isoformat(), "status": "scheduled", "per_page": 100}, ttl_hours=6)
    return data.get("data", data.get("matches", []))

STAT_FIELDS = {
    "shots": lambda row: row.get("shooting", {}).get("shots", 0),
    "shots_on_target": lambda row: row.get("shooting", {}).get("shots_on_target", 0),
    "cards": lambda row: (row.get("discipline", {}).get("yellow_cards", 0) + row.get("discipline", {}).get("red_cards", 0)),
    "goals": lambda row: row.get("shooting", {}).get("goals", 0),
    "assists": lambda row: row.get("passing", {}).get("assists", 0),
}

def per90(total: float, minutes: float) -> float:
    return 0.0 if minutes <=0 else total * 90.0 / minutes

def rolling_form(player_id: str, team_id: str) -> dict:
    if not USE_ROLLING:
        return {"matches_used": 0, "minutes_played": 0, "per90": {k:0 for k in STAT_FIELDS}}
    matches = get_team_recent_matches(team_id)
    totals = {k: 0.0 for k in STAT_FIELDS}
    minutes_played = 0.0
    matches_used = 0
    for match in matches:
        row = get_match_player_stats(match["id"], player_id)
        if not row: continue
        mins = row.get("minutes", 0)
        if mins <=0: continue
        minutes_played += mins
        matches_used += 1
        for stat, extractor in STAT_FIELDS.items():
            totals[stat] += extractor(row)
    return {"matches_used": matches_used, "minutes_played": minutes_played, "per90": {stat: per90(totals[stat], minutes_played) for stat in STAT_FIELDS}}

def season_baseline(profile: dict) -> dict:
    stats = profile.get("season_stats", profile.get("stats", {}))
    minutes = stats.get("minutes", 0) or stats.get("time_played",0) or 0
    totals = {"shots": stats.get("shots",0), "shots_on_target": stats.get("shots_on_target",0), "cards": stats.get("yellow_cards",0)+stats.get("red_cards",0), "goals": stats.get("goals",0), "assists": stats.get("assists",0)}
    return {"minutes": minutes, "per90": {stat: per90(totals[stat], minutes if minutes>0 else 1) for stat in STAT_FIELDS}, "raw": stats}

def blend(rolling: dict, season: dict, weight: float = ROLLING_WEIGHT) -> dict:
    blended = {}
    for stat in STAT_FIELDS:
        r = rolling["per90"].get(stat, 0.0)
        s = season["per90"].get(stat, 0.0)
        w = weight if rolling["matches_used"] >=2 else 0.1
        blended[stat] = w * r + (1-w)*s if USE_ROLLING else s
    return blended

def poisson_pmf(k: int, lam: float) -> float:
    if lam <=0:
        return 1.0 if k==0 else 0.0
    return math.exp(-lam)*lam**k/math.factorial(k)

def prob_over(lam: float, line: float) -> float:
    threshold = math.floor(line)+1
    return 1 - sum(poisson_pmf(k, lam) for k in range(threshold))

def prob_at_least_one(lam: float) -> float:
    return 1 - poisson_pmf(0, lam)

def estimate_expected_minutes(profile: dict) -> float:
    stats = profile.get("season_stats", profile.get("stats", {})) or profile.get("stats",{}) or {}
    minutes = stats.get("minutes", 0) or stats.get("time_played",0) or 0
    apps = stats.get("appearances",0) or stats.get("matches_played",0) or stats.get("games",0) or 0
    if minutes and apps:
        return minutes / apps
    if apps >= 5:
        return 75.0
    if apps >= 1:
        return 45.0
    return 70.0 # default for unknown

def build_report(player: dict, profile: dict, team_id: str, expected_minutes: float) -> dict:
    player_id = player["id"]
    rolling = rolling_form(player_id, team_id)
    season = season_baseline(profile)
    blended_per90 = blend(rolling, season)
    projected = {stat: rate * expected_minutes / 90.0 for stat, rate in blended_per90.items()}
    return {"player_id": player_id, "player_name": player.get("name","Unknown"), "team_id": team_id, "expected_minutes": round(expected_minutes,1), "rolling_matches_used": rolling["matches_used"], "projected_per_match": projected, "props": {"shots_over_1.5": round(prob_over(projected["shots"],1.5),3), "shots_over_2.5": round(prob_over(projected["shots"],2.5),3), "sot_over_0.5": round(prob_over(projected["shots_on_target"],0.5),3), "sot_over_1.5": round(prob_over(projected["shots_on_target"],1.5),3), "to_be_carded": round(prob_at_least_one(projected["cards"]),3), "to_score": round(prob_at_least_one(projected["goals"]),3), "to_assist": round(prob_at_least_one(projected["assists"]),3), "goal_or_assist": round(prob_at_least_one(projected["goals"]+projected["assists"]),3)}}

def scan_team(team_id: str, min_avg_minutes: float = MIN_AVG_MINUTES) -> list[dict]:
    squad = get_team_squad(team_id)
    print(f" Squad size: {len(squad)} players")
    reports=[]
    for player in squad:
        try:
            profile = get_player_profile(player["id"])
        except Exception as e:
            print(f" Skip {player.get('name')}: {e}")
            continue
        avg_minutes = estimate_expected_minutes(profile)
        if avg_minutes < min_avg_minutes:
            continue
        try:
            report = build_report(player, profile, team_id, expected_minutes=min(avg_minutes,90))
        except Exception as e:
            print(f" Skip build {player.get('name')}: {e}")
            continue
        reports.append(report)
    print(f" -> {len(reports)} starters after filter (min {min_avg_minutes} mins)")
    return reports

def run_watchlist_scan(team_names: list[str] = WATCHLIST) -> list[dict]:
    all_reports=[]
    for name in team_names:
        try:
            team = search_team(name)
        except Exception as exc:
            print(f" Skipping '{name}': {exc}")
            continue
        print(f"Scanning {team.get('name',name)}... id={team['id']}")
        all_reports.extend(scan_team(team["id"]))
    return all_reports

def apply_criteria(reports: list[dict], thresholds: dict = CRITERIA_THRESHOLDS) -> list[dict]:
    qualifying=[]
    for report in reports:
        hits=[prop for prop, threshold in thresholds.items() if report["props"].get(prop,0)>=threshold]
        if hits:
            report["criteria_hit"]=hits
            qualifying.append(report)
    qualifying.sort(key=lambda r: max(r["props"][p] for p in r["criteria_hit"]), reverse=True)
    return qualifying

def top_n_by_market(reports: list[dict], top_n: int = TOP_N_PER_MARKET) -> dict:
    if not reports:
        return {}
    markets = reports[0]["props"].keys()
    ranked={}
    for market in markets:
        ranked[market]=sorted(reports, key=lambda r: r["props"].get(market,0), reverse=True)[:top_n]
    return ranked

def _load_output() -> dict:
    if OUTPUT_JSON.exists():
        try:
            return json.loads(OUTPUT_JSON.read_text())
        except:
            return {}
    return {}

def save_scan(reports: list[dict], qualifying: list[dict], ranked: dict) -> None:
    data=_load_output()
    data.setdefault("players",{})
    for report in reports:
        data["players"][report["player_name"]]=report
    data["screener"]={"generated_at": date.today().isoformat(), "qualifying": [r["player_name"] for r in qualifying], "top_by_market": {market: [r["player_name"] for r in reps] for market, reps in ranked.items()}}
    OUTPUT_JSON.write_text(json.dumps(data, indent=2))
    print(f"Saved {len(reports)} player reports + screener to {OUTPUT_JSON}")

def run_scan(reports: list[dict]) -> None:
    if not reports:
        print("No qualifying players found - saving empty to keep Pages alive")
        save_scan([], [], {})
        return
    qualifying = apply_criteria(reports)
    ranked = top_n_by_market(reports)
    print(f"\n{len(reports)} players scanned, {len(qualifying)} met a threshold.\n")
    for r in qualifying[:10]:
        hits=", ".join(f"{p} ({r['props'][p]*100:.0f}%)" for p in r["criteria_hit"])
        print(f" {r['player_name']:<24} {hits}")
    save_scan(reports, qualifying, ranked)

if __name__ == "__main__":
    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("Set THESTATSAPI_KEY env var")
        raise SystemExit(1)
    if os.getenv("GITHUB_ACTIONS") or not sys.stdin.isatty():
        print("CI detected - auto-running watchlist scan (mode 2)")
        choice="2"
    else:
        choice="2"
    if choice=="2":
        run_scan(run_watchlist_scan())
