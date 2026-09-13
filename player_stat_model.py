import os, json, time, math, hashlib
from datetime import date, timedelta
from pathlib import Path
import requests

API_KEY = os.environ.get("THESTATSAPI_KEY", "PASTE_KEY")
BASE_URL = "https://api.thestatsapi.com/api/football"
OUTPUT_JSON = Path(__file__).parent / "docs" / "player_stats_data.json"
OUTPUT_JSON.parent.mkdir(exist_ok=True)
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

USE_ROLLING = False # keep False to save quota
MIN_MINUTES = 45
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def _cache(k): return CACHE_DIR / f"{hashlib.sha256(k.encode()).hexdigest()[:20]}.json"
def cached_get(path, params=None, ttl=12):
    key = path + json.dumps(params or {}, sort_keys=True)
    f = _cache(key)
    if f.exists() and (time.time() - f.stat().st_mtime)/3600 < ttl:
        return json.loads(f.read_text())
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=25)
    r.raise_for_status()
    j = r.json()
    f.write_text(json.dumps(j))
    time.sleep(0.4)
    return j

def get_todays_teams():
    today = date.today().isoformat()
    # Get ALL statuses, not just scheduled - your 5:09pm games are already live
    try:
        d = cached_get("/matches", {"date_from":today,"date_to":today,"per_page":100}, ttl=1)
        matches = d.get("data", d.get("matches", []))
    except:
        matches = []

    teams = {}
    for m in matches:
        for k in ["home_team","away_team"]:
            t = m.get(k, {})
            if t.get("id"): teams[str(t["id"])] = t.get("name","")
        if m.get("home_team_id"): teams[str(m["home_team_id"])] = m.get("home_team_name","")
        if m.get("away_team_id"): teams[str(m["away_team_id"])] = m.get("away_team_name","")

    # FALLBACK if no fixtures today (Sunday evening = no games) -> scan top teams
    if not teams:
        print("No fixtures today, using fallback top teams")
        teams = {"43":"Manchester City","14":"Arsenal","49":"Liverpool","50":"Chelsea","33":"Manchester United","40":"Tottenham","39":"Newcastle","61":"Barcelona","18":"Real Madrid"}
    print(f"Teams to scan: {list(teams.values())[:10]}")
    return teams

def get_squad(tid):
    try:
        d=cached_get(f"/teams/{tid}/players", ttl=24*7)
        return d.get("data", d.get("players", []))
    except: return []

def get_profile(pid):
    return cached_get(f"/players/{pid}", ttl=24)

def season_baseline(p):
    # FIX: TheStatsAPI has stats in different places
    s = p.get("season_stats") or p.get("stats") or p.get("data",{}).get("stats") or {}
    if isinstance(s, list) and s: s = s[0]
    mins = s.get("minutes") or s.get("minutes_played") or 0
    apps = s.get("appearances") or s.get("matches_played") or 1
    # try all possible keys
    shots = s.get("shots") or s.get("total_shots") or 0
    sot = s.get("shots_on_target") or s.get("shots_on_goal") or 0
    yellows = s.get("yellow_cards") or s.get("yellow") or 0
    reds = s.get("red_cards") or s.get("red") or 0
    goals = s.get("goals") or 0
    assists = s.get("assists") or 0
    # if still 0, try nested
    if shots==0 and "shooting" in p: shots = p["shooting"].get("shots",0)

    per90 = lambda v: v*90/max(mins,1) if mins else 0
    return {"minutes":mins,"apps":apps,"per90":{"shots":per90(shots),"shots_on_target":per90(sot),"cards":per90(yellows+reds),"goals":per90(goals),"assists":per90(assists)},"raw":s}

def prob_over(lam, line):
    if lam<=0: return 0.0
    tot=0
    for k in range(int(line)+1):
        tot+= math.exp(-lam)*lam**k/math.factorial(k)
    return 1-tot

if __name__=="__main__":
    if "PASTE" in API_KEY: raise SystemExit("Set API KEY")
    team_map = get_todays_teams()
    all_players=[]
    for tid,tname in team_map.items():
        print(f"Scanning {tname}")
        squad=get_squad(tid)
        for pl in squad[:25]: # only first 25 per team to save quota
            try:
                prof=get_profile(pl["id"])
                base=season_baseline(prof)
                if base["minutes"]/max(base["apps"],1) < MIN_MINUTES: continue
                # projected = per90 * expected 90
                avg_mins = base["minutes"]/max(base["apps"],1)
                proj = {k: v*min(avg_mins,90)/90 for k,v in base["per90"].items()}

                # FIX duplicate stats - now using real per90
                props={
                    "shots_over_1.5": round(prob_over(proj["shots"],1.5),3),
                    "sot_over_0.5": round(prob_over(proj["shots_on_target"],0.5),3),
                    "to_be_carded": round(1-math.exp(-proj["cards"]) if proj["cards"]>0 else 0,3),
                    "goal_or_assist": round(1-math.exp(-(proj["goals"]+proj["assists"])),3),
                    "to_score": round(1-math.exp(-proj["goals"]),3)
                }
                # Fix undefined - always 0 when USE_ROLLING=False
                rec={
                    "player_id":pl["id"],"player_name":pl.get("name","Unknown"),
                    "team_id":tid,"team_name":tname,
                    "expected_minutes":round(min(avg_mins,90),1),
                    "matches_used": 0, # FIX for undefined
                    "rolling_matches_used":0,
                    "projected_per_match":proj,
                    "props":props
                }
                if props["shots_over_1.5"]>0.15 or props["goal_or_assist"]>0.2:
                    all_players.append(rec)
            except Exception as e:
                print(f"skip {pl.get('name')}: {e}")
                continue

    output={"generated_at":date.today().isoformat(),"total_players":len(all_players),"teams":sorted(list(set(r["team_name"] for r in all_players))),"players":{r["player_name"]:r for r in all_players}}
    OUTPUT_JSON.write_text(json.dumps(output, indent=2))
    print(f"DONE: {len(all_players)} players, teams={output['teams'][:5]} -> {OUTPUT_JSON}")
