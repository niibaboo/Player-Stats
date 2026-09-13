import os, json, time, math, hashlib
from datetime import date
from pathlib import Path
import requests

# --- CONFIG YOU EDIT ---
API_KEY = os.environ.get("THESTATSAPI_KEY", "PASTE_YOUR_KEY_HERE")
USE_ROLLING_FORM = False # False = 80 calls, True = 1000+ calls but more accurate
MIN_AVG_MINUTES = 45
BASE_URL = "https://api.thestatsapi.com/api/football"

OUTPUT_JSON = Path(__file__).parent /"docs"/ "player_stats_data.json"
# If using GitHub Pages, change to: Path(__file__).parent / "docs" / "player_stats_data.json"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_JSON.parent.mkdir(exist_ok=True)

CRITERIA_THRESHOLDS = {"shots_over_1.5":0.65,"sot_over_0.5":0.60,"to_be_carded":0.30,"goal_or_assist":0.55}
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# --- API + CACHE ---
def _cache_path(k): return CACHE_DIR / f"{hashlib.sha256(k.encode()).hexdigest()[:20]}.json"
def cached_get(path, params=None, ttl_hours=24):
    key = path + json.dumps(params or {}, sort_keys=True)
    cfile = _cache_path(key)
    if cfile.exists() and (time.time() - cfile.stat().st_mtime)/3600 < ttl_hours:
        return json.loads(cfile.read_text())
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    cfile.write_text(json.dumps(data))
    time.sleep(0.35)
    return data

def get_todays_fixtures():
    today = date.today().isoformat()
    print(f"Fixtures for {today}")
    d = cached_get("/matches", {"date_from":today,"date_to":today,"status":"scheduled","per_page":100}, ttl_hours=2)
    matches = d.get("data", d.get("matches", []))
    print(f"Found {len(matches)} fixtures")
    return matches

def get_team_squad(tid):
    d=cached_get(f"/teams/{tid}/players", ttl_hours=24*7)
    return d.get("data", d.get("players", []))
def get_player_profile(pid): return cached_get(f"/players/{pid}", ttl_hours=24)
def get_team_recent_matches(tid, lim=5):
    d=cached_get(f"/teams/{tid}/matches", {"status":"finished","per_page":lim,"order":"desc"}, ttl_hours=12)
    return d.get("data", d.get("matches", []))[:lim]
def get_match_player_stats(mid,pid):
    try:
        d=cached_get(f"/matches/{mid}/player-stats", ttl_hours=24*30)
        for row in d.get("data", d.get("player_stats", [])):
            if str(row.get("player_id"))==str(pid): return row
    except: pass
    return None

STAT_FIELDS={"shots":lambda r:r.get("shooting",{}).get("shots",r.get("shots",0)),"shots_on_target":lambda r:r.get("shooting",{}).get("shots_on_target",r.get("shots_on_target",0)),"cards":lambda r:r.get("discipline",{}).get("yellow_cards",0)+r.get("discipline",{}).get("red_cards",0),"goals":lambda r:r.get("shooting",{}).get("goals",r.get("goals",0)),"assists":lambda r:r.get("passing",{}).get("assists",r.get("assists",0))}
def per90(tot,m): return tot*90/m if m>0 else 0.0
def rolling_form(pid,tid):
    if not USE_ROLLING_FORM: return {"matches_used":0,"minutes_played":0,"per90":{k:0 for k in STAT_FIELDS}}
    totals={k:0.0 for k in STAT_FIELDS}; mins=0; used=0
    for match in get_team_recent_matches(tid):
        row=get_match_player_stats(match["id"],pid)
        if not row: continue
        m=row.get("minutes",0)
        if m<=0: continue
        mins+=m; used+=1
        for k,fn in STAT_FIELDS.items():
            try: totals[k]+=fn(row)
            except: pass
    return {"matches_used":used,"minutes_played":mins,"per90":{k:per90(totals[k],mins) for k in STAT_FIELDS}}
def season_baseline(p):
    s=p.get("season_stats", p.get("stats",{})); mins=s.get("minutes",0)
    totals={"shots":s.get("shots",0),"shots_on_target":s.get("shots_on_target",0),"cards":s.get("yellow_cards",0)+s.get("red_cards",0),"goals":s.get("goals",0),"assists":s.get("assists",0)}
    return {"minutes":mins,"per90":{k:per90(totals[k],mins) for k in STAT_FIELDS}}
def blend(roll,seas):
    out={}
    for k in STAT_FIELDS:
        r=roll["per90"].get(k,0); s=seas["per90"].get(k,0)
        w=0 if roll["matches_used"]==0 else 0.25 if roll["matches_used"]<3 else 0.6
        if not USE_ROLLING_FORM: w=0
        val=w*r+(1-w)*s
        if val==0 and s>0: val=s
        out[k]=val
    return out
def poisson_pmf(k,lam): return math.exp(-lam)*lam**k/math.factorial(k) if lam>0 else (1.0 if k==0 else 0.0)
def prob_over(lam,line): return 1-sum(poisson_pmf(k,lam) for k in range(math.floor(line)+1))
def prob_at_least_one(lam): return 1-poisson_pmf(0,lam)
def estimate_minutes(p):
    s=p.get("season_stats", p.get("stats",{})); apps=s.get("appearances",0) or s.get("matches_played",0)
    return s.get("minutes",0)/apps if apps>0 else 0

if __name__=="__main__":
    if API_KEY=="PASTE_YOUR_KEY_HERE": raise SystemExit("Set THESTATSAPI_KEY")
    fixtures=get_todays_fixtures()
    team_map={}
    for m in fixtures:
        for key in ["home_team","away_team"]:
            t=m.get(key,{});
            if t.get("id"): team_map[str(t["id"])]=t.get("name","")
        if m.get("home_team_id"): team_map[str(m["home_team_id"])]=m.get("home_team_name","")
        if m.get("away_team_id"): team_map[str(m["away_team_id"])]=m.get("away_team_name","")

    if not team_map: team_map={"Arsenal":"Arsenal","Manchester City":"Manchester City"}

    all_reports=[]
    for tid,tname in team_map.items():
        print(f"\nScanning {tname}...")
        try:
            squad=get_team_squad(tid)
            for player in squad:
                try: profile=get_player_profile(player["id"])
                except: continue
                avg=estimate_minutes(profile)
                if avg<MIN_AVG_MINUTES: continue
                roll=rolling_form(player["id"], tid)
                seas=season_baseline(profile)
                blended=blend(roll,seas)
                proj={k:v*min(avg,90)/90 for k,v in blended.items()}
                rep={"player_id":player["id"],"player_name":player.get("name","Unknown"),"team_id":tid,"team_name":tname,"expected_minutes":round(min(avg,90),1),"rolling_matches_used":roll["matches_used"],"projected_per_match":proj,"props":{"shots_over_1.5":round(prob_over(proj["shots"],1.5),3),"shots_over_2.5":round(prob_over(proj["shots"],2.5),3),"sot_over_0.5":round(prob_over(proj["shots_on_target"],0.5),3),"sot_over_1.5":round(prob_over(proj["shots_on_target"],1.5),3),"to_be_carded":round(prob_at_least_one(proj["cards"]),3),"to_score":round(prob_at_least_one(proj["goals"]),3),"to_assist":round(prob_at_least_one(proj["assists"]),3),"goal_or_assist":round(prob_at_least_one(proj["goals"]+proj["assists"]),3)}}
                hits=[p for p,th in CRITERIA_THRESHOLDS.items() if rep["props"].get(p,0)>=th]
                if hits: rep["criteria_hit"]=hits
                all_reports.append(rep)
        except Exception as e: print(f"fail {tname}: {e}")

    output={"generated_at":date.today().isoformat(),"total_players":len(all_reports),"teams":sorted(list(set(r["team_name"] for r in all_reports))),"players":{r["player_name"]:r for r in all_reports}}
    OUTPUT_JSON.write_text(json.dumps(output, indent=2))
    print(f"\nDONE: {len(all_reports)} players, {len(output['teams'])} teams -> {OUTPUT_JSON}")
