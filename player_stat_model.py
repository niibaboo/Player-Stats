"""
Player Stat Model
------------------
Player prop predictor covering shots / shots on target, cards, and
goals + assists. Sibling project to Corner Flag (football-data.org),
Blue Line (NHL) and Match IQ (TheStatsAPI match-level model).

Pulls per-player data from TheStatsAPI (api.thestatsapi.com):
  - season profile stats (appearances, goals, assists, minutes)
  - per-match player-stats rows for the player's team's last N
    finished matches (shots, shots on target, cards, minutes)

Blends a rolling "recent form" average (last 5-10 matches) with the
full-season average into a single projected rate per 90 minutes, then
uses a Poisson model to price over/under and yes/no prop lines.

On top of single-player lookup, the model can also pick its own pool
of players to check, two ways:
  - Watchlist scan: a fixed list of teams you set below (WATCHLIST).
  - Competition scan: every team with a fixture in a competition over
    the next N days (opt-in, since it burns a lot more API quota).

Either scan pulls the full squad for each team, drops fringe players
by expected minutes, builds a report for everyone left, then flags
players against your CRITERIA_THRESHOLDS and separately surfaces the
top-N highest-probability plays per market.

Usage:
    python3 player_stat_model.py

Set THESTATSAPI_KEY as an environment variable, or paste it into
API_KEY below. Results are appended to player_stats_data.json, which
the companion player_stat_model.html web app reads for live use.
"""

import os
import json
import time
import math
import hashlib
from datetime import date, timedelta
from pathlib import Path

import requests

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

API_KEY = os.environ.get("THESTATSAPI_KEY", "PASTE_YOUR_KEY_HERE")
BASE_URL = "https://api.thestatsapi.com/api/football"

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

OUTPUT_JSON = Path(__file__).parent / "player_stats_data.json"

# How much weight to give recent form vs full-season average.
# 0.6 means: 60% recent form, 40% season baseline.
ROLLING_WEIGHT = 0.6
ROLLING_MATCHES = 10  # pull up to this many recent finished matches

# Teams to scan by default (team names, resolved via search_team()).
# Edit this to your actual watchlist.
WATCHLIST = [
    "Arsenal",
    "Manchester City",
]

# Skip players who don't average at least this many minutes per
# appearance this season — filters out fringe/bench players so the
# scan doesn't waste quota (or your attention) on non-starters.
MIN_AVG_MINUTES = 60

# A player "meets criteria" if any of their prop probabilities clears
# the threshold below. Edit freely.
CRITERIA_THRESHOLDS = {
    "shots_over_1.5": 0.65,
    "sot_over_0.5": 0.60,
    "to_be_carded": 0.30,
    "goal_or_assist": 0.55,
}

# Regardless of thresholds, also surface the top N plays per market
# from whatever pool was scanned.
TOP_N_PER_MARKET = 5

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


# ----------------------------------------------------------------------
# Simple on-disk cache so re-runs don't burn API quota
# ----------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{h}.json"


def cached_get(path: str, params: dict | None = None, ttl_hours: int = 12) -> dict:
    """GET from TheStatsAPI with a simple file cache to control quota use."""
    cache_key = path + json.dumps(params or {}, sort_keys=True)
    cfile = _cache_path(cache_key)

    if cfile.exists():
        age_hours = (time.time() - cfile.stat().st_mtime) / 3600
        if age_hours < ttl_hours:
            return json.loads(cfile.read_text())

    resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    cfile.write_text(json.dumps(data))
    time.sleep(0.3)  # be polite to the free-tier rate limit
    return data


# ----------------------------------------------------------------------
# TheStatsAPI calls
# ----------------------------------------------------------------------

def search_player(name: str) -> dict:
    """Search for a player by name, return the first match."""
    data = cached_get("/players/search", {"query": name}, ttl_hours=24 * 7)
    results = data.get("data", data.get("players", []))
    if not results:
        raise ValueError(f"No player found for '{name}'")
    return results[0]


def get_player_profile(player_id: str) -> dict:
    """Season-level profile: appearances, goals, assists, minutes, etc."""
    return cached_get(f"/players/{player_id}", ttl_hours=24)


def get_team_recent_matches(team_id: str, limit: int = ROLLING_MATCHES) -> list[dict]:
    """Most recent finished matches for a team, newest first."""
    data = cached_get(
        f"/teams/{team_id}/matches",
        {"status": "finished", "per_page": limit, "order": "desc"},
        ttl_hours=6,
    )
    return data.get("data", data.get("matches", []))[:limit]


def get_match_player_stats(match_id: str, player_id: str) -> dict | None:
    """This player's row from a single match's player-stats endpoint."""
    data = cached_get(f"/matches/{match_id}/player-stats", ttl_hours=24 * 30)
    rows = data.get("data", data.get("player_stats", []))
    for row in rows:
        if str(row.get("player_id")) == str(player_id):
            return row
    return None


def search_team(name: str) -> dict:
    """Search for a team by name, return the first match."""
    data = cached_get("/teams/search", {"query": name}, ttl_hours=24 * 30)
    results = data.get("data", data.get("teams", []))
    if not results:
        raise ValueError(f"No team found for '{name}'")
    return results[0]


def get_team_squad(team_id: str) -> list[dict]:
    """Full current squad for a team."""
    data = cached_get(f"/teams/{team_id}/players", ttl_hours=24 * 7)
    return data.get("data", data.get("players", []))


def get_upcoming_matches(competition_id: str, days_ahead: int = 7) -> list[dict]:
    """Scheduled matches for a competition over the next N days."""
    today = date.today()
    data = cached_get(
        "/matches",
        {
            "competition_id": competition_id,
            "date_from": today.isoformat(),
            "date_to": (today + timedelta(days=days_ahead)).isoformat(),
            "status": "scheduled",
            "per_page": 100,
        },
        ttl_hours=6,
    )
    return data.get("data", data.get("matches", []))


# ----------------------------------------------------------------------
# Stat extraction / blending
# ----------------------------------------------------------------------

STAT_FIELDS = {
    "shots": lambda row: row.get("shooting", {}).get("shots", 0),
    "shots_on_target": lambda row: row.get("shooting", {}).get("shots_on_target", 0),
    "cards": lambda row: (row.get("discipline", {}).get("yellow_cards", 0)
                           + row.get("discipline", {}).get("red_cards", 0)),
    "goals": lambda row: row.get("shooting", {}).get("goals", 0),
    "assists": lambda row: row.get("passing", {}).get("assists", 0),
}


def per90(total: float, minutes: float) -> float:
    if minutes <= 0:
        return 0.0
    return total * 90.0 / minutes


def rolling_form(player_id: str, team_id: str) -> dict:
    """Average per-90 rate across the player's last N finished matches."""
    matches = get_team_recent_matches(team_id)
    totals = {k: 0.0 for k in STAT_FIELDS}
    minutes_played = 0.0
    matches_used = 0

    for match in matches:
        row = get_match_player_stats(match["id"], player_id)
        if not row:
            continue
        mins = row.get("minutes", 0)
        if mins <= 0:
            continue
        minutes_played += mins
        matches_used += 1
        for stat, extractor in STAT_FIELDS.items():
            totals[stat] += extractor(row)

    return {
        "matches_used": matches_used,
        "minutes_played": minutes_played,
        "per90": {stat: per90(totals[stat], minutes_played) for stat in STAT_FIELDS},
    }


def season_baseline(profile: dict) -> dict:
    """Average per-90 rate from the player's season-long profile totals."""
    stats = profile.get("season_stats", profile.get("stats", {}))
    minutes = stats.get("minutes", 0)
    totals = {
        "shots": stats.get("shots", 0),
        "shots_on_target": stats.get("shots_on_target", 0),
        "cards": stats.get("yellow_cards", 0) + stats.get("red_cards", 0),
        "goals": stats.get("goals", 0),
        "assists": stats.get("assists", 0),
    }
    return {
        "minutes": minutes,
        "per90": {stat: per90(totals[stat], minutes) for stat in STAT_FIELDS},
    }


def blend(rolling: dict, season: dict, weight: float = ROLLING_WEIGHT) -> dict:
    """Weighted blend of rolling form and season baseline, per stat."""
    blended = {}
    for stat in STAT_FIELDS:
        r = rolling["per90"].get(stat, 0.0)
        s = season["per90"].get(stat, 0.0)
        # If we don't have enough recent-match data, lean on season instead.
        w = weight if rolling["matches_used"] >= 3 else 0.25
        blended[stat] = w * r + (1 - w) * s
    return blended


# ----------------------------------------------------------------------
# Poisson pricing for prop lines
# ----------------------------------------------------------------------

def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def prob_over(lam: float, line: float) -> float:
    """P(stat > line), e.g. shots over 2.5."""
    threshold = math.floor(line) + 1
    return 1 - sum(poisson_pmf(k, lam) for k in range(threshold))


def prob_at_least_one(lam: float) -> float:
    """P(stat >= 1), e.g. 'to be carded', 'to score or assist'."""
    return 1 - poisson_pmf(0, lam)


# ----------------------------------------------------------------------
# Building a player report
# ----------------------------------------------------------------------

def estimate_expected_minutes(profile: dict) -> float:
    """Average minutes per appearance this season — a starter proxy."""
    stats = profile.get("season_stats", profile.get("stats", {}))
    apps = stats.get("appearances", 0) or stats.get("matches_played", 0)
    minutes = stats.get("minutes", 0)
    if apps <= 0:
        return 0.0
    return minutes / apps


def build_report(player: dict, profile: dict, team_id: str, expected_minutes: float) -> dict:
    player_id = player["id"]
    rolling = rolling_form(player_id, team_id)
    season = season_baseline(profile)
    blended_per90 = blend(rolling, season)

    # Scale each per-90 rate to the minutes we expect this player on the pitch
    projected = {stat: rate * expected_minutes / 90.0 for stat, rate in blended_per90.items()}

    return {
        "player_id": player_id,
        "player_name": player.get("name", "Unknown"),
        "team_id": team_id,
        "expected_minutes": round(expected_minutes, 1),
        "rolling_matches_used": rolling["matches_used"],
        "projected_per_match": projected,
        "props": {
            "shots_over_1.5": round(prob_over(projected["shots"], 1.5), 3),
            "shots_over_2.5": round(prob_over(projected["shots"], 2.5), 3),
            "sot_over_0.5": round(prob_over(projected["shots_on_target"], 0.5), 3),
            "sot_over_1.5": round(prob_over(projected["shots_on_target"], 1.5), 3),
            "to_be_carded": round(prob_at_least_one(projected["cards"]), 3),
            "to_score": round(prob_at_least_one(projected["goals"]), 3),
            "to_assist": round(prob_at_least_one(projected["assists"]), 3),
            "goal_or_assist": round(
                prob_at_least_one(projected["goals"] + projected["assists"]), 3
            ),
        },
    }


def build_player_report(name: str, expected_minutes: float = 90.0) -> dict:
    """Single-player lookup by name (the original manual flow)."""
    player = search_player(name)
    team_id = player.get("team_id") or player.get("team", {}).get("id")
    profile = get_player_profile(player["id"])
    return build_report(player, profile, team_id, expected_minutes)


# ----------------------------------------------------------------------
# Team / squad scanning — lets the model pick its own player pool
# ----------------------------------------------------------------------

def scan_team(team_id: str, min_avg_minutes: float = MIN_AVG_MINUTES) -> list[dict]:
    """Build reports for every regular starter in a team's squad."""
    squad = get_team_squad(team_id)
    reports = []
    for player in squad:
        try:
            profile = get_player_profile(player["id"])
        except Exception:
            continue
        avg_minutes = estimate_expected_minutes(profile)
        if avg_minutes < min_avg_minutes:
            continue  # fringe/bench player — skip to save quota + noise
        try:
            report = build_report(player, profile, team_id, expected_minutes=min(avg_minutes, 90))
        except Exception:
            continue
        reports.append(report)
    return reports


def run_watchlist_scan(team_names: list[str] = WATCHLIST) -> list[dict]:
    """Scan every team on the fixed watchlist."""
    all_reports = []
    for name in team_names:
        try:
            team = search_team(name)
        except Exception as exc:
            print(f"  Skipping '{name}': {exc}")
            continue
        print(f"Scanning {team.get('name', name)}...")
        all_reports.extend(scan_team(team["id"]))
    return all_reports


def run_competition_scan(competition_id: str, days_ahead: int = 7) -> list[dict]:
    """Scan every team with a fixture in a competition over the next N days."""
    matches = get_upcoming_matches(competition_id, days_ahead)
    team_ids = set()
    for m in matches:
        home = m.get("home_team_id") or m.get("home_team", {}).get("id")
        away = m.get("away_team_id") or m.get("away_team", {}).get("id")
        team_ids.update({home, away} - {None})

    print(f"{len(matches)} fixtures found, {len(team_ids)} teams to scan.")
    all_reports = []
    for team_id in team_ids:
        all_reports.extend(scan_team(team_id))
    return all_reports


# ----------------------------------------------------------------------
# Criteria matching + ranking
# ----------------------------------------------------------------------

def apply_criteria(reports: list[dict], thresholds: dict = CRITERIA_THRESHOLDS) -> list[dict]:
    """Flag which threshold(s) each report clears; return only the hits."""
    qualifying = []
    for report in reports:
        hits = [prop for prop, threshold in thresholds.items()
                if report["props"].get(prop, 0) >= threshold]
        if hits:
            report["criteria_hit"] = hits
            qualifying.append(report)
    qualifying.sort(key=lambda r: max(r["props"][p] for p in r["criteria_hit"]), reverse=True)
    return qualifying


def top_n_by_market(reports: list[dict], top_n: int = TOP_N_PER_MARKET) -> dict:
    """Best N reports per market, regardless of whether they hit a threshold."""
    markets = reports[0]["props"].keys() if reports else CRITERIA_THRESHOLDS.keys()
    ranked = {}
    for market in markets:
        ranked[market] = sorted(
            reports, key=lambda r: r["props"].get(market, 0), reverse=True
        )[:top_n]
    return ranked


# ----------------------------------------------------------------------
# Saving results
# ----------------------------------------------------------------------

def save_report(report: dict) -> None:
    data = _load_output()
    data.setdefault("players", {})[report["player_name"]] = report
    OUTPUT_JSON.write_text(json.dumps(data, indent=2))
    print(f"Saved {report['player_name']} to {OUTPUT_JSON}")


def save_scan(reports: list[dict], qualifying: list[dict], ranked: dict) -> None:
    data = _load_output()
    data.setdefault("players", {})
    for report in reports:
        data["players"][report["player_name"]] = report
    data["screener"] = {
        "generated_at": date.today().isoformat(),
        "qualifying": [r["player_name"] for r in qualifying],
        "top_by_market": {
            market: [r["player_name"] for r in reps] for market, reps in ranked.items()
        },
    }
    OUTPUT_JSON.write_text(json.dumps(data, indent=2))
    print(f"Saved {len(reports)} player reports + screener results to {OUTPUT_JSON}")


def _load_output() -> dict:
    if OUTPUT_JSON.exists():
        return json.loads(OUTPUT_JSON.read_text())
    return {}


def print_report(report: dict) -> None:
    print(f"\n{report['player_name']}  "
          f"(last {report['rolling_matches_used']} matches used for form)")
    for prop, prob in report["props"].items():
        print(f"  {prop:<16} {prob * 100:5.1f}%")


def run_scan(reports: list[dict]) -> None:
    if not reports:
        print("No qualifying players found.")
        return
    qualifying = apply_criteria(reports)
    ranked = top_n_by_market(reports)

    print(f"\n{len(reports)} players scanned, {len(qualifying)} met a threshold.\n")
    print("=== Meets criteria ===")
    for r in qualifying:
        hits = ", ".join(f"{p} ({r['props'][p]*100:.0f}%)" for p in r["criteria_hit"])
        print(f"  {r['player_name']:<24} {hits}")

    print("\n=== Top plays by market ===")
    for market, reps in ranked.items():
        print(f"\n  {market}:")
        for r in reps:
            print(f"    {r['player_name']:<24} {r['props'][market]*100:5.1f}%")

    save_scan(reports, qualifying, ranked)


if __name__ == "__main__":
    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("Set THESTATSAPI_KEY env var or edit API_KEY in this file first.")
        raise SystemExit(1)

    print("Player Stat Model")
    print("  1) Look up a single player")
    print("  2) Scan the watchlist")
    print("  3) Scan a competition's upcoming fixtures")
    choice = input("Choose a mode [1/2/3]: ").strip()

    if choice == "2":
        run_scan(run_watchlist_scan())

    elif choice == "3":
        competition_id = input("Competition ID: ").strip()
        days = input("Days ahead (default 7): ").strip()
        days_ahead = int(days) if days else 7
        run_scan(run_competition_scan(competition_id, days_ahead))

    else:
        print("Type a player name (blank to quit)")
        while True:
            name = input("\nPlayer name: ").strip()
            if not name:
                break
            try:
                report = build_player_report(name)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user directly
                print(f"  Could not build report: {exc}")
                continue
            print_report(report)
            save_report(report)
