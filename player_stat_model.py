"""
Player Stat Model V5.2 LITE - Fixed squad endpoint + quota saver
"""
import os
import json
import time
import math
import hashlib
from datetime import date
from pathlib import Path
import requests

API_KEY = os.environ.get("THESTATSAPI_KEY", "PASTE_YOUR_KEY_HERE")
BASE_URL = "https://api.thestatsapi.com/api/football"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_JSON = Path(__file__).parent / "docs" / "player_stats_data.json"
OUTPUT_JSON.parent.mkdir(exist_ok=True)

WATCHLIST = ["Arsenal FC", "Manchester City"] # start with 2 to save quota
MIN_AVG_MINUTES = 30 # skip bench - saves 40% calls

CRITERIA_THRESHOLDS = {
    "shots_over_1.5": 0.10,
    "sot_over_0.5": 0.10,
    "to_be_carded": 0.05,
    "goal_or_assist": 0.10,
}

TOP_N_PER_MARKET = 5
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{h}.json"

def cached_get(path: str, params: dict | None = None, ttl_hours: int = 12) -> dict:
    cache_key = path + json.dumps(params or {}, sort_keys=True)
    cfile = _cache_path(cache_key)
    if cfile.exists():
        try:
            return json.loads(cfile.read_text())
        except:
            pass
    for attempt in range(3):
        resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=25)
        if resp.status_code == 429:
            print(f" Rate limit, sleeping 20s...")
            time.sleep(20)
            continue
        if resp.status_code == 404:
            raise FileNotFoundError(f"404 for {path}")
        try:
            resp.raise_for_status()
        except:
            if attempt == 2:
                raise
            time.sleep(3)
            continue
        data = resp.json()
        cfile.write_text(json.dumps(data))
        time.sleep(2.2)
        return data
    raise Exception(f"Failed {path}")

def search_team(name: str) -> dict:
    print(f" Searching team '{name}'...")
    data = cached_get("/teams", {"search": name}, ttl_hours=24 * 30)
    results = data.get("data", data.get("teams", []))
    # filter out women and pick exact match
    mens = [t for t in results if "women" not in t.get("name","").lower()]
    if mens:
        # prefer exact name match
        for t in mens:
            if name.lower() in t.get("name","").lower():
                print(f" Found: {t.get('name')} id={t.get('id')}")
                return t
        return mens[0]
    if results:
        return results[0]
    raise ValueError(f"No team for '{name}' - got {len(results)} results")

def get_team_squad(team_id: str) -> list[dict]:
    # FIXED: try multiple possible endpoints
    for path in [f"/teams/{team_id}/squad", f"/teams/{team_id}/players", f"/teams/{team_id}"]:
        try:
            data = cached_get(path, ttl_hours=24*7)
            # data can be list or dict
            raw = data.get("data", data)
            # case 1: data is team object with squad field
            if isinstance(raw, dict):
                if "squad" in raw and isinstance(raw["squad"], list):
                    squad = raw["squad"]
                elif "players" in raw and isinstance(raw["players"], list):
                    squad = raw["players"]
                else:
                    squad = [raw] if "id" in raw else []
            else:
                squad = raw

            # flatten if grouped by position: [{"position":"GK","players":[...]},...]
            if squad and isinstance(squad[0], dict) and "players" in squad[0]:
                flat = []
                for group in squad:
                    flat.extend(group.get("players", []))
                squad = flat

            if squad and len(squad) > 0:
                print(f" Squad from {path}: {len(squad)} players")
                return squad
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f" try {path} failed: {e}")
            continue
    print(f" WARNING: No squad found for {team_id}")
    return []

def get_player_profile(player_id: str) -> dict:
    return cached_get(f"/players/{player_id}", ttl_hours=24*30)

def per90(total: float, minutes: float) -> float:
    return 0.0 if minutes <=0 else total * 90.0 / minutes

def season_baseline(profile: dict) -> dict:
    stats = profile.get("season_stats", profile.get("stats", {})) or {}
    minutes = stats.get("minutes", 0) or stats.get("time_played",0) or 0
    totals = {"shots": stats.get("shots",0), "shots_on_target": stats.get("shots_on_target",0), "cards": stats.get("yellow_cards",0)+stats.get("red_cards",0), "goals": stats.get("goals",0), "assists": stats.get("assists",0)}
    per90_dict = {stat: per90(totals[stat], minutes if minutes>0 else 1) for stat in totals}
    if minutes == 0 or sum(totals.values()) == 0:
        per90_dict = {"shots": 1.5, "shots_on_target": 0.6, "cards": 0.25, "goals": 0.25, "assists": 0.15}
        minutes = 600
    return {"minutes": minutes, "per90": per90_dict}

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
    stats = profile.get("season_stats", profile.get("stats", {})) or {}
    minutes = stats.get("minutes", 0) or 0
    apps = stats.get("appearances",0) or stats.get("matches_played",0) or 0
    if minutes and apps:
        return minutes / apps
    return 75.0 if apps >=5 else 45.0

def build_report(player: dict, profile: dict, team_id: str, team_name: str, expected_minutes: float) -> dict:
    season = season_baseline(profile)
    blended = season["per90"]
    projected = {stat: rate * expected_minutes / 90.0 for stat, rate in blended.items()}
    return {
        "player_id": player["id"],
        "player_name": player.get("name","Unknown"),
        "team_id": team_id,
        "team_name": team_name,
        "expected_minutes": round(expected_minutes,1),
        "projected_per_match": projected,
        "props": {
            "shots_over_1.5": round(prob_over(projected["shots"],1.5),3),
            "sot_over_0.5": round(prob_over(projected["shots_on_target"],0.5),3),
            "to_be_carded": round(prob_at_least_one(projected["cards"]),3),
            "goal_or_assist": round(prob_at_least_one(projected["goals"]+projected["assists"]),3)
        }
    }

def scan_team(team_id: str, team_name: str, min_avg_minutes: float = MIN_AVG_MINUTES) -> list[dict]:
    squad = get_team_squad(team_id)
    if not squad:
        print(f" No squad for {team_name} ({team_id}) - skipping")
        return []
    print(f" Squad {team_name}: {len(squad)} players")
    reports=[]
    for player in squad:
        player["team_name"] = team_name
        try:
            profile = get_player_profile(player["id"])
        except Exception:
            continue
        avg_minutes = estimate_expected_minutes(profile)
        if avg_minutes < min_avg_minutes:
            continue
        try:
            report = build_report(player, profile, team_id, team_name, expected_minutes=min(avg_minutes,90))
        except:
            continue
        reports.append(report)
    return reports

def run_watchlist_scan(team_names: list[str] = WATCHLIST) -> list[dict]:
    all_reports=[]
    for name in team_names:
        try:
            team = search_team(name)
        except Exception as exc:
            print(f" Skip '{name}': {exc}")
            continue
        all_reports.extend(scan_team(team["id"], team.get("name", name)))
    return all_reports

def apply_criteria(reports: list[dict], thresholds: dict = CRITERIA_THRESHOLDS) -> list[dict]:
    qualifying=[]
    for report in reports:
        hits=[prop for prop, thr in thresholds.items() if report["props"].get(prop,0)>=thr]
        if hits:
            report["criteria_hit"]=hits
            qualifying.append(report)
    qualifying.sort(key=lambda r: max(r["props"][p] for p in r["criteria_hit"]), reverse=True)
    return qualifying

def top_n_by_market(reports: list[dict], top_n: int = TOP_N_PER_MARKET) -> dict:
    if not reports: return {}
    markets = reports[0]["props"].keys()
    return {m: sorted(reports, key=lambda r: r["props"].get(m,0), reverse=True)[:top_n] for m in markets}

def _load_output() -> dict:
    if OUTPUT_JSON.exists():
        try: return json.loads(OUTPUT_JSON.read_text())
        except: return {}
    return {}

def save_scan(reports: list[dict], qualifying: list[dict], ranked: dict) -> None:
    data=_load_output()
    data["players"]={}
    for report in reports:
        data["players"][report["player_name"]]=report
    data["screener"]={"generated_at": date.today().isoformat(), "qualifying": [r["player_name"] for r in qualifying], "top_by_market": {m: [r["player_name"] for r in reps] for m, reps in ranked.items()}}
    OUTPUT_JSON.write_text(json.dumps(data, indent=2))
    print(f"Saved {len(reports)} reports to {OUTPUT_JSON}")

def run_scan(reports: list[dict]) -> None:
    if not reports:
        save_scan([], [], {})
        return
    qualifying = apply_criteria(reports)
    ranked = top_n_by_market(reports)
    print(f"\n{len(reports)} scanned, {len(qualifying)} met threshold.\n")
    save_scan(reports, qualifying, ranked)

if __name__ == "__main__":
    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("Set THESTATSAPI_KEY")
        raise SystemExit(1)
    print("Scanning teams:", WATCHLIST)
    run_scan(run_watchlist_scan())
