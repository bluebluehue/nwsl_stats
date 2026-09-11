#!/usr/bin/env python3
"""Apply validated NWSL Decision v5 to transformed_data.json.

Uses the last four TEAM matches from FotMob to build a role-aware Recent
Opportunity Rating, then blends Form + Recent Opportunity + existing ASA
Next Fixture Rating with position-specific rolling-validation weights.

This intentionally does NOT alter the existing Form or Fixture models.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DATA_PATH = Path("transformed_data.json")
FOTMOB_BASES = ("https://www.fotmob.com/api/data", "https://www.fotmob.com/api")
NWSL_PARENT_LEAGUE_ID = 9134
NWSL_SEASON = "2026"
RECENT_TEAM_MATCH_WINDOW = 4
REQUEST_DELAY = 0.10
TIMEOUT = 30
RETRIES = 4

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.fotmob.com/",
}

STAT_KEYS = {
    "minutes": "minutes_played",
    "xg": "expected_goals",
    "xa": "expected_assists",
    "sot": "ShotsOnTarget",
    "box_touches": "touches_opp_box",
    "chances": "chances_created",
    "dribbles": "dribbles_succeeded",
    "crosses": "accurate_crosses",
    "tackles": "matchstats.headers.tackles",
    "interceptions": "interceptions",
    "recoveries": "recoveries",
    "clearances": "clearances",
    "blocks": "shot_blocks",
    "saves": "saves",
    "xgot_faced": "expected_goals_on_target_faced",
    "goals_prevented": "goals_prevented",
    "saves_inside_box": "saves_inside_box",
}
ZERO_IF_MISSING = {
    key for label, key in STAT_KEYS.items()
    if label not in {"xgot_faced", "goals_prevented"}
}

COMPOSITES = {
    "GK": {
        "saves": 0.35, "xgot_faced": 0.25,
        "goals_prevented": 0.25, "saves_inside_box": 0.15,
    },
    "DEF": {
        "xg": 0.08, "xa": 0.08, "box_touches": 0.08, "chances": 0.08,
        "tackles": 0.14, "interceptions": 0.14, "recoveries": 0.14,
        "clearances": 0.13, "blocks": 0.13,
    },
    "MID": {
        "xg": 0.15, "xa": 0.15, "sot": 0.10, "box_touches": 0.10,
        "chances": 0.15, "tackles": 0.08, "interceptions": 0.07,
        "recoveries": 0.10, "dribbles": 0.05, "crosses": 0.05,
    },
    "FOR": {
        "xg": 0.30, "xa": 0.15, "sot": 0.20,
        "box_touches": 0.20, "chances": 0.15,
    },
}
ROLE_BLEND = {"GK": 0.00, "DEF": 0.40, "MID": 0.25, "FOR": 0.40}

# Rolling-validation production choice.
DECISION_WEIGHTS_V5 = {
    "GK":  {"form": 0.00, "recent": 0.20, "fixture": 0.80},
    "DEF": {"form": 0.15, "recent": 0.20, "fixture": 0.65},
    "MID": {"form": 0.15, "recent": 0.40, "fixture": 0.45},
    "FOR": {"form": 0.15, "recent": 0.50, "fixture": 0.35},
}

TEAM_ALIASES = {
    "LA":"LA","BAY":"BAY","BOS":"BOS","CHI":"CHI","DEN":"DEN","GFC":"GFC",
    "HOU":"HOU","KC":"KC","LOU":"LOU","NC":"NC","ORL":"ORL","POR":"POR",
    "SD":"SD","SEA":"SEA","UTA":"UTA","WAS":"WAS",
    "ANGEL CITY":"LA","ANGEL CITY FC":"LA","BAY FC":"BAY",
    "BOSTON LEGACY":"BOS","BOSTON LEGACY FC":"BOS",
    "CHICAGO STARS":"CHI","CHICAGO STARS FC":"CHI",
    "DENVER SUMMIT":"DEN","DENVER SUMMIT FC":"DEN",
    "GOTHAM":"GFC","GOTHAM FC":"GFC","NJ NY GOTHAM":"GFC","NJ NY GOTHAM FC":"GFC",
    "HOUSTON DASH":"HOU","KANSAS CITY CURRENT":"KC",
    "RACING LOUISVILLE":"LOU","RACING LOUISVILLE FC":"LOU","LOUISVILLE":"LOU",
    "NORTH CAROLINA COURAGE":"NC","NC COURAGE":"NC","ORLANDO PRIDE":"ORL",
    "PORTLAND THORNS":"POR","PORTLAND THORNS FC":"POR",
    "SAN DIEGO WAVE":"SD","SAN DIEGO WAVE FC":"SD",
    "SEATTLE REIGN":"SEA","SEATTLE REIGN FC":"SEA",
    "UTAH ROYALS":"UTA","UTAH ROYALS FC":"UTA","WASHINGTON SPIRIT":"WAS",
}


def safe_float(v: Any) -> float | None:
    if v is None or v == "": return None
    try: return float(v)
    except (TypeError, ValueError): return None


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def norm_text(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_team(v: Any) -> str:
    n = norm_text(v)
    return TEAM_ALIASES.get(n, n)


def player_key(v: Any) -> tuple[str, str]:
    p = norm_text(v).split()
    return (p[0][:1], p[-1]) if p else ("", "")


def parse_dt(v: Any) -> datetime | None:
    if not v: return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def percentile(value: float, population: list[float]) -> float:
    if not population: return 50.0
    less = sum(x < value for x in population)
    equal = sum(x == value for x in population)
    return 100.0 * (less + 0.5 * equal) / len(population)


def recursively_find_match_ids(obj: Any) -> set[int]:
    ids: set[int] = set()
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            candidate = x.get("id")
            if candidate is not None and (("home" in x and "away" in x) or ("homeTeam" in x and "awayTeam" in x)):
                try: ids.add(int(candidate))
                except Exception: pass
            for key in ("linkToMatch", "pageUrl", "matchUrl"):
                val = x.get(key)
                if isinstance(val, str):
                    m = re.search(r"#(\d+)", val)
                    if m: ids.add(int(m.group(1)))
            for val in x.values(): walk(val)
        elif isinstance(x, list):
            for val in x: walk(val)
    walk(obj)
    return ids


class FotMobClient:
    def __init__(self) -> None:
        self.s = requests.Session(); self.s.headers.update(HEADERS)
    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last = None
        for base in FOTMOB_BASES:
            for attempt in range(RETRIES):
                try:
                    r = self.s.get(f"{base}/{path}", params=params, timeout=TIMEOUT)
                    if r.status_code == 200:
                        time.sleep(REQUEST_DELAY); return r.json()
                    if r.status_code in (403, 404): break
                    if r.status_code == 429 or r.status_code >= 500:
                        time.sleep(min(8, 1.25 * (2 ** attempt))); continue
                    r.raise_for_status()
                except Exception as exc:
                    last = exc; time.sleep(min(8, 1.25 * (2 ** attempt)))
        raise RuntimeError(f"FotMob request failed {path}: {last}")


def flatten_player_stats(p: dict[str, Any]) -> dict[str, float | None]:
    flat: dict[str, float | None] = {}
    for section in p.get("stats", []) or []:
        stats = section.get("stats")
        if not isinstance(stats, dict): continue
        for desc in stats.values():
            if not isinstance(desc, dict): continue
            key = desc.get("key"); stat = desc.get("stat") or {}
            if key and isinstance(stat, dict): flat[key] = safe_float(stat.get("value"))
    return flat


def parse_match(payload: dict[str, Any]) -> dict[str, Any] | None:
    g = payload.get("general", {}) or {}
    if str(g.get("parentLeagueId")) != str(NWSL_PARENT_LEAGUE_ID) and str(g.get("leagueName") or "").upper() != "NWSL":
        return None
    dt = parse_dt(g.get("matchTimeUTCDate") or g.get("matchTimeUTC"))
    if not dt or dt.year != 2026 or not g.get("finished"): return None
    players = []
    for pid, p in ((payload.get("content") or {}).get("playerStats") or {}).items():
        flat = flatten_player_stats(p)
        mins = safe_float(flat.get(STAT_KEYS["minutes"])) or 0.0
        if mins <= 0: continue
        for key in ZERO_IF_MISSING:
            if flat.get(key) is None: flat[key] = 0.0
        players.append({
            "name": p.get("name"), "name_norm": norm_text(p.get("name")),
            "name_key": player_key(p.get("name")), "team": norm_team(p.get("teamName")),
            "opta_id": str(p.get("optaId") or ""), "stats": flat,
        })
    return {
        "match_id": int(g["matchId"]), "date": dt,
        "home": norm_team((g.get("homeTeam") or {}).get("name")),
        "away": norm_team((g.get("awayTeam") or {}).get("name")), "players": players,
    }


def fetch_matches() -> list[dict[str, Any]]:
    c = FotMobClient()
    league = c.get("leagues", {"id": NWSL_PARENT_LEAGUE_ID, "ccode3": "USA", "season": NWSL_SEASON})
    ids = sorted(recursively_find_match_ids(league))
    print(f"FotMob candidate matches: {len(ids)}")
    out = []
    for i, mid in enumerate(ids, 1):
        try:
            m = parse_match(c.get("matchDetails", {"matchId": mid}))
            if m: out.append(m)
        except Exception as exc:
            print(f"WARNING match {mid}: {exc}")
        if i % 40 == 0: print(f"  {i}/{len(ids)} checked; {len(out)} completed NWSL matches retained")
    out.sort(key=lambda m: m["date"])
    return out


def find_player(match: dict[str, Any], fantasy: dict[str, Any]) -> dict[str, Any] | None:
    team = norm_team(fantasy.get("Club")); name = fantasy.get("Name") or ""
    # Opta bridge first when available.
    opta = str(fantasy.get("Opta Player ID") or "")
    opta_tail = opta.split(":")[-1] if opta else ""
    if opta_tail:
        hits = [p for p in match["players"] if p["team"] == team and p["opta_id"] == opta_tail]
        if len(hits) == 1: return hits[0]
    nn, nk = norm_text(name), player_key(name)
    exact = [p for p in match["players"] if p["team"] == team and p["name_norm"] == nn]
    if len(exact) == 1: return exact[0]
    fallback = [p for p in match["players"] if p["team"] == team and p["name_key"] == nk]
    return fallback[0] if len(fallback) == 1 else None


def recent_raw(player: dict[str, Any], team_history: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    team = norm_team(player.get("Club"))
    window = team_history.get(team, [])[-RECENT_TEAM_MATCH_WINDOW:]
    if len(window) < RECENT_TEAM_MATCH_WINDOW: return None
    totals = defaultdict(float); minutes = 0.0; apps = 0
    for m in window:
        p = find_player(m, player)
        if not p: continue
        apps += 1
        mins = safe_float(p["stats"].get(STAT_KEYS["minutes"])) or 0.0
        minutes += mins
        for label, key in STAT_KEYS.items():
            if label == "minutes": continue
            val = safe_float(p["stats"].get(key))
            if val is not None: totals[label] += val
    return {
        "apps": apps, "minutes": minutes,
        "minutes_per_team_match": minutes / RECENT_TEAM_MATCH_WINDOW,
        "ptm": {k: totals[k] / RECENT_TEAM_MATCH_WINDOW for k in totals},
    }


def main() -> None:
    if not DATA_PATH.exists(): raise SystemExit("transformed_data.json not found")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    players = data.get("players", [])
    matches = fetch_matches()
    print(f"Completed 2026 NWSL matches parsed: {len(matches)}")

    team_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in matches:
        team_history[m["home"]].append(m); team_history[m["away"]].append(m)
    for t in team_history: team_history[t].sort(key=lambda m: m["date"])

    rows = []
    for p in players:
        pos = str(p.get("Position") or "").upper()
        if pos not in COMPOSITES: continue
        r = recent_raw(p, team_history)
        if r is None: continue
        row = {"player": p, "pos": pos, **r}
        for feature in COMPOSITES[pos]: row[f"ptm_{feature}"] = r["ptm"].get(feature, 0.0)
        rows.append(row)

    # Calibrate against current players in the same fantasy position who have
    # appeared at least once in the four-team-match window. Non-appearing players
    # receive Recent Opportunity = 0 rather than distorting the active population.
    for pos in COMPOSITES:
        group = [r for r in rows if r["pos"] == pos and r["apps"] > 0]
        minute_pop = [r["minutes_per_team_match"] for r in group]
        feature_pops = {
            f: [r[f"ptm_{f}"] for r in group] for f in COMPOSITES[pos]
        }
        for r in [x for x in rows if x["pos"] == pos]:
            p = r["player"]
            if r["apps"] <= 0:
                activity = role = opportunity = 0.0
            else:
                activity = sum(
                    w * percentile(r[f"ptm_{f}"], feature_pops[f])
                    for f, w in COMPOSITES[pos].items()
                )
                role = percentile(r["minutes_per_team_match"], minute_pop)
                rw = ROLE_BLEND[pos]
                opportunity = (1.0 - rw) * activity + rw * role
            p["Recent Opportunity Rating"] = round(clamp(opportunity, 0, 100), 1)
            p["Recent Activity Rating"] = round(clamp(activity, 0, 100), 1)
            p["Recent Role Rating"] = round(clamp(role, 0, 100), 1)
            p["Recent 4 Team Matches Apps"] = r["apps"]
            p["Recent 4 Team Matches Minutes"] = round(r["minutes"], 1)
            p["Recent Minutes Per Team Match"] = round(r["minutes_per_team_match"], 1)

    # Players whose team/history could not be scored get zero recent opportunity.
    scored_ids = {id(r["player"]) for r in rows}
    for p in players:
        if id(p) not in scored_ids:
            p["Recent Opportunity Rating"] = 0.0
            p["Recent Activity Rating"] = 0.0
            p["Recent Role Rating"] = 0.0
            p["Recent 4 Team Matches Apps"] = 0
            p["Recent 4 Team Matches Minutes"] = 0.0
            p["Recent Minutes Per Team Match"] = 0.0

    for p in players:
        pos = str(p.get("Position") or "").upper()
        w = DECISION_WEIGHTS_V5.get(pos, {"form": .25, "recent": .25, "fixture": .50})
        form = safe_float(p.get("Form Rating")) or 0.0
        recent = safe_float(p.get("Recent Opportunity Rating")) or 0.0
        fixture = safe_float(p.get("Next Fixture Rating")) or 0.0
        p["Decision Rating"] = round(clamp(w["form"]*form + w["recent"]*recent + w["fixture"]*fixture, 0, 100), 1)

    meta = data.setdefault("metadata", {})
    meta["decision_rating"] = {
        "version": "v5-rolling-validated-recent-opportunity",
        "weights": DECISION_WEIGHTS_V5,
        "uses": ["Form Rating", "Recent Opportunity Rating", "Next Fixture Rating"],
        "recent_window": "previous 4 team matches",
        "recent_calibration": "same-position current-player percentile",
        "fotmob_completed_matches": len(matches),
        "applied_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    DATA_PATH.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")

    print("\n=== DECISION V5 APPLIED ===")
    print(f"Players: {len(players)} | recent rows: {len(rows)}")
    for pos in ("GK", "DEF", "MID", "FOR"):
        eligible = [p for p in players if str(p.get("Position") or "").upper() == pos]
        ranked = sorted(eligible, key=lambda p: safe_float(p.get("Decision Rating")) or -1, reverse=True)[:8]
        print(f"\n{pos} top 8:")
        for p in ranked:
            print(f"  {p.get('Name')} ({p.get('Club')}): Decision {p.get('Decision Rating')} | Form {p.get('Form Rating')} | Recent {p.get('Recent Opportunity Rating')} | Fixture {p.get('Next Fixture Rating')} | mins/club-match {p.get('Recent Minutes Per Team Match')}")

if __name__ == "__main__":
    main()
