import os, json, time, math, hashlib
from datetime import date
from pathlib import Path
import requests

API_KEY = os.environ.get("THESTATSAPI_KEY", "").strip()
BASE_URL = "https://api.thestatsapi.com/api/football"
OUTPUT_JSON = Path(__file__).parent / "docs" / "player_stats_data.json"
OUTPUT_JSON.parent.mkdir(exist_ok=True)
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def _cache(k): return CACHE_DIR / f"{hashlib.sha256(k.encode()).hexdigest()[:20]}.json"

def cached_get(path, params=None, ttl=6):
    key = path + json.dumps(params or {}, sort_keys=True)
    f = _cache(key)
    if f.exists() and (time.time() - f.stat().st_mtime)/3600 < ttl:
        try:
            return json.loads(f.read_text())
        except: pass
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    f.write_text(json.dumps(j))
    time.sleep(0.35)
    return j

def get_todays_teams():
    today = date.today().isoformat()
    try:
        d = cached_get("/matches", {"date_from": today, "date_to": today, "per_page": 100}, ttl=1)
        matches = d.get("data", [])
    except Exception as e:
        print(f"matches today error: {e}")
        matches = []

    teams = {}
    for m in matches:
        ht = m.get("home_team", {}) or {}
        at = m.get("away_team", {}) or {}
        if ht.get("id"): teams[ht["id"]] = ht.get("name", ht["id"])
        if at.get("id"): teams[at["id"]] = at.get("name", at["id"])

    if not teams:
        print("No fixtures today - using fallback (Semenyo IS Man City now, Jan 2026)")
        teams = {
            "tm_3046": "Manchester City",
            "tm_3040": "Arsenal",
            "tm_3043": "Liverpool",
            "tm_3039": "Bournemouth"
        }
    print(f"Teams to scan: {teams}")
    return teams

def get_squad(tid):
    try:
        d = cached_get(f"/teams/{tid}/players", ttl=24*7)
        return d.get("data", [])
    except Exception as e:
        print(f"squad {tid} error {e}")
        return []

def get_player_profile(pid):
    try:
        d = cached_get(f"/players/{pid}", ttl=24)
        return d.get("data", d)
    except Exception as e:
        print(f"profile {pid} error {e}")
        return {}

def get_player_last_matches(pid, limit=10):
    try:
        d = cached_get(f"/players/{pid}/matches", {"per_page": limit}, ttl=6)
        return d.get("data", [])
    except:
        return []

def calc_from_matches(matches):
    if not matches: return None
    total = {"shots":0, "sot":0, "cards":0, "goals":0, "assists":0, "mins":0, "count":0}
    for m in matches:
        stats = m.get("stats", {}) or m.get("player_stats", {}) or {}
        total["shots"] += stats.get("shots", 0) or stats.get("total_shots",0) or 0
        total["sot"] += stats.get("shots_on_target", 0) or 0
        total["cards"] += (stats.get("yellow_cards",0) or 0) + (stats.get("red_cards",0) or 0)
        total["goals"] += stats.get("goals",0) or 0
        total["assists"] += stats.get("assists",0) or 0
        total["mins"] += stats.get("minutes",0) or stats.get("minutes_played",0) or 0
        total["count"] += 1
    if total["count"]==0: return None
    # If all zero, API trial doesn't give player match stats - return None
    if total["shots"]==0 and total["goals"]==0 and total["mins"]==0:
        return None
    return total

def season_parse(raw):
    s = raw.get("stats") or raw.get("season_stats") or {}
    if isinstance(s, list): s = s[0] if s else {}
    if isinstance(s, dict) and "stats" in s: s = s["stats"]

    mins = s.get("minutes") or s.get("minutes_played") or raw.get("minutes") or 0
    apps = s.get("appearances") or s.get("matches_played") or s.get("games") or 0
    shots = s.get("shots") or s.get("total_shots") or 0
    sot = s.get("shots_on_target") or 0
    cards = (s.get("yellow_cards") or 0) + (s.get("red_cards") or 0)
    goals = s.get("goals") or 0
    assists = s.get("assists") or 0

    if mins==0 and shots==0 and goals==0:
        return None

    return {"mins":mins,"apps":max(apps,1),"shots":shots,"sot":sot,"cards":cards,"goals":goals,"assists":assists}

def prob_over(lam, line):
    if lam < 0.02: return 0.0
    tot = 0.0
    for k in range(int(line)+1):
        tot += math.exp(-lam) * (lam**k) / math.factorial(k)
    return 1.0 - tot

if __name__ == "__main__":
    if not API_KEY or "PASTE" in API_KEY:
        raise SystemExit("Set THESTATSAPI_KEY env")

    team_map = get_todays_teams()
    all_players = []

    for tid, tname in list(team_map.items())[:4]:
        squad = get_squad(tid)
        print(f"--- {tname} ({tid}) : {len(squad)} players")
        for pl in squad[:20]:
            pid = pl.get("id")
            pname = pl.get("name") or pl.get("full_name") or "Unknown"
            if not pid: continue

            prof = get_player_profile(pid)
            # Try season first
            parsed = season_parse(prof)

            used_matches = 0
            if parsed:
                avg_mins = parsed["mins"] / max(parsed["apps"],1)
                per90 = {k: parsed[k]*90/max(parsed["mins"],1) for k in ["shots","sot","cards","goals","assists"]}
                used_matches = parsed["apps"]
            else:
                # Try last 10 matches
                last = get_player_last_matches(pid, 10)
                calc = calc_from_matches(last)
                if not calc:
                    print(f" SKIP {pname} - API returns no stats (trial key limit)")
                    continue
                avg_mins = calc["mins"]/max(calc["count"],1)
                per90 = {
                    "shots": calc["shots"]/max(calc["count"],1),
                    "sot": calc["sot"]/max(calc["count"],1),
                    "cards": calc["cards"]/max(calc["count"],1),
                    "goals": calc["goals"]/max(calc["count"],1),
                    "assists": calc["assists"]/max(calc["count"],1),
                }
                used_matches = calc["count"]

            if avg_mins < 25: continue

            proj = {k: v*min(avg_mins,90)/90 for k,v in per90.items()}
            # Ensure distinct values - not same 0.75 for all
            props = {
                "shots_over_1.5": round(prob_over(proj["shots"], 1.5), 3),
                "sot_over_0.5": round(prob_over(proj["sot"], 0.5), 3),
                "to_be_carded": round(1-math.exp(-proj["cards"]), 3) if proj["cards"]>0 else 0.0,
                "goal_or_assist": round(1-math.exp(-(proj["goals"]+proj["assists"])), 3),
                "to_score": round(1-math.exp(-proj["goals"]), 3)
            }

            # Only keep if has some signal
            if max(props.values()) < 0.05: continue

            all_players.append({
                "player_id": pid,
                "player_name": pname,
                "team_id": tid,
                "team_name": tname,
                "expected_minutes": round(min(avg_mins,90),1),
                "season_minutes": int(parsed["mins"] if parsed else calc["mins"]),
                "matches_used": used_matches,
                "projected_per_match": {k: round(v,3) for k,v in proj.items()},
                "props": props
            })
            print(f" OK {pname}: {proj} -> {props}")

    output = {
        "generated_at": date.today().isoformat(),
        "total_players": len(all_players),
        "teams": sorted(list(set(p["team_name"] for p in all_players))),
        "players": {p["player_name"]: p for p in all_players}
    }
    OUTPUT_JSON.write_text(json.dumps(output, indent=2))
    print(f"\nDONE: {len(all_players)} players, teams={output['teams']}")
    print(f"Saved to {OUTPUT_JSON}")
