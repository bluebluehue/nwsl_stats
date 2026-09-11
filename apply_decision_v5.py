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
    "SEATTLE REIGN":"SEA","SEATTLE REIGN FC":"SEA","OL REIGN":"SEA",
    "CHICAGO RED STARS":"CHI",
    "KANSAS CITY":"KC",
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

    # FotMob's website/API can expose women's teams with a trailing "(W)"
    # or "Women" marker.  That marker is not part of Fantasy NWSL club names.
    for suffix in (" W", " WOMEN"):
        if n.endswith(suffix):
            n = n[:-len(suffix)].strip()

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


def _candidate_names(fantasy: dict[str, Any]) -> list[str]:
    """
    Names available from the Fantasy/Opta side.

    "Underlying Involvement Opta Name" is especially useful for abbreviated
    Opta names such as "S. Menti".
    """
    values = [
        fantasy.get("Name"),
        fantasy.get("Underlying Involvement Opta Name"),
        fantasy.get("Short Name"),
    ]
    out = []
    seen = set()
    for value in values:
        s = str(value or "").strip()
        if not s or s.lower() == "nan":
            continue
        n = norm_text(s)
        if n and n not in seen:
            seen.add(n)
            out.append(s)
    return out


def _first_name_unique_match(players: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """
    Handles provider short forms such as:
      "Marta Vieira da Silva Veiga" -> "Marta"
      "Angelina Alonso Costantino" -> "Angelina"
      "Lorena da Silva Leite" -> "Lorena"

    Only used when that first name identifies exactly one listed player.
    """
    parts = norm_text(name).split()
    if not parts:
        return None
    first = parts[0]
    hits = []
    for p in players:
        pp = p["name_norm"].split()
        if pp and pp[0] == first:
            hits.append(p)
    return hits[0] if len(hits) == 1 else None


def _shared_name_unique_match(players: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """
    Conservative fallback for compound-name differences such as
    James-Turner vs James. Requires the same first initial and at least one
    shared non-first token, and the result must be unique.
    """
    parts = norm_text(name).split()
    if not parts:
        return None

    first_initial = parts[0][:1]
    tail_tokens = set(parts[1:])
    if not first_initial or not tail_tokens:
        return None

    hits = []
    for p in players:
        pp = p["name_norm"].split()
        if not pp or pp[0][:1] != first_initial:
            continue
        if tail_tokens.intersection(pp[1:]):
            hits.append(p)

    return hits[0] if len(hits) == 1 else None


def find_player(
    match: dict[str, Any],
    fantasy: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """
    Robust Fantasy -> FotMob player bridge.

    Important: the caller is already looking only at matches involving the
    fantasy player's club. Team-aware matching is preferred, but a unique
    full-name/name-key match across the match is allowed as a fallback so a
    provider team-name formatting difference cannot silently turn an entire
    club into zero recent minutes.
    """
    team = norm_team(fantasy.get("Club"))
    names = _candidate_names(fantasy)

    # Numeric FotMob Opta IDs are useful when available. Fantasy's provider ID
    # is often a non-numeric Opta entity key, so do not assume the formats match.
    opta = str(fantasy.get("Opta Player ID") or "")
    opta_tail = opta.split(":")[-1] if opta else ""
    if opta_tail.isdigit():
        hits = [p for p in match["players"] if p["opta_id"] == opta_tail]
        if len(hits) == 1:
            return hits[0], "opta_id"

    same_team = [p for p in match["players"] if p["team"] == team]
    pools = [
        ("team", same_team),
        ("match", match["players"]),
    ]

    # 1) Exact normalized name.
    for scope, pool in pools:
        for raw_name in names:
            nn = norm_text(raw_name)
            hits = [p for p in pool if p["name_norm"] == nn]
            if len(hits) == 1:
                return hits[0], f"{scope}_exact"

    # 2) First initial + final surname token.
    for scope, pool in pools:
        for raw_name in names:
            nk = player_key(raw_name)
            hits = [p for p in pool if p["name_key"] == nk]
            if len(hits) == 1:
                return hits[0], f"{scope}_name_key"

    # 3) Unique first-name bridge for long legal names vs single-name FotMob.
    for scope, pool in pools:
        for raw_name in names:
            hit = _first_name_unique_match(pool, raw_name)
            if hit is not None:
                return hit, f"{scope}_first_name"

    # 4) Conservative compound-name fallback.
    for scope, pool in pools:
        for raw_name in names:
            hit = _shared_name_unique_match(pool, raw_name)
            if hit is not None:
                return hit, f"{scope}_shared_name"

    return None, "unmatched"


def recent_raw(player: dict[str, Any], team_history: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    team = norm_team(player.get("Club"))
    window = team_history.get(team, [])[-RECENT_TEAM_MATCH_WINDOW:]
    if len(window) < RECENT_TEAM_MATCH_WINDOW:
        return None

    totals = defaultdict(float)
    minutes = 0.0
    apps = 0
    methods = Counter()

    for m in window:
        p, method = find_player(m, player)
        if not p:
            continue

        methods[method] += 1
        apps += 1
        mins = safe_float(p["stats"].get(STAT_KEYS["minutes"])) or 0.0
        minutes += mins

        for label, key in STAT_KEYS.items():
            if label == "minutes":
                continue
            val = safe_float(p["stats"].get(key))
            if val is not None:
                totals[label] += val

    return {
        "apps": apps,
        "minutes": minutes,
        "minutes_per_team_match": minutes / RECENT_TEAM_MATCH_WINDOW,
        "ptm": {k: totals[k] / RECENT_TEAM_MATCH_WINDOW for k in totals},
        "match_methods": dict(methods),
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
            p["Recent Match Methods"] = r.get("match_methods", {})
            p["Recent Match Status"] = "matched" if r["apps"] > 0 else "no_recent_match"

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
            p["Recent Match Methods"] = {}
            p["Recent Match Status"] = "team_history_unavailable"

    # -----------------------------------------------------------------
    # MATCHING SANITY AUDIT
    # -----------------------------------------------------------------
    # This deliberately catches the failure mode that prompted v5.1:
    # an entire club receiving zero recent minutes because provider team
    # names differed (e.g. a trailing "(W)").
    print("\n=== RECENT MATCHING SANITY AUDIT ===")

    hard_fail_teams = []
    for club in sorted({str(p.get("Club") or "") for p in players if p.get("Club")}):
        club_players = [p for p in players if str(p.get("Club") or "") == club]
        established = [
            p for p in club_players
            if (safe_float(p.get("Total Games Played")) or 0) >= 4
        ]
        matched = [
            p for p in club_players
            if (safe_float(p.get("Recent 4 Team Matches Apps")) or 0) > 0
        ]
        print(
            f"  {club}: {len(matched)} players matched in recent window "
            f"/ {len(club_players)} rostered"
        )
        if len(established) >= 5 and len(matched) == 0:
            hard_fail_teams.append(club)

    suspicious = []
    for p in players:
        recent_fantasy = safe_float(p.get("Total Over 4 Gameweeks")) or 0.0
        recent_apps = safe_float(p.get("Recent 4 Team Matches Apps")) or 0.0
        if recent_fantasy > 0 and recent_apps == 0:
            suspicious.append(p)

    suspicious.sort(
        key=lambda p: safe_float(p.get("Total Over 4 Gameweeks")) or 0.0,
        reverse=True,
    )

    print(
        f"Players with >0 fantasy points over 4 GWs but 0 FotMob apps "
        f"in last 4 team matches: {len(suspicious)}"
    )
    for p in suspicious[:30]:
        print(
            f"  CHECK {p.get('Name')} ({p.get('Club')}): "
            f"4GW fantasy pts={p.get('Total Over 4 Gameweeks')} | "
            f"season apps={p.get('Total Games Played')}"
        )

    if hard_fail_teams:
        raise RuntimeError(
            "Recent matching failed for entire established club roster(s): "
            + ", ".join(hard_fail_teams)
            + ". Refusing to overwrite Decision Rating."
        )

    # Explicit diagnostic for the player who exposed the original bug.
    menti = next(
        (p for p in players if norm_text(p.get("Name")) == "SALLY MENTI"),
        None,
    )
    if menti:
        print(
            "SALLY MENTI CHECK: "
            f"apps={menti.get('Recent 4 Team Matches Apps')} | "
            f"minutes={menti.get('Recent 4 Team Matches Minutes')} | "
            f"mins/team-match={menti.get('Recent Minutes Per Team Match')} | "
            f"methods={menti.get('Recent Match Methods')}"
        )
        if (safe_float(menti.get("Recent 4 Team Matches Apps")) or 0) == 0:
            raise RuntimeError(
                "Sally Menti still has 0 recent FotMob appearances. "
                "Refusing to overwrite Decision Rating."
            )

    for p in players:
        pos = str(p.get("Position") or "").upper()
        w = DECISION_WEIGHTS_V5.get(pos, {"form": .25, "recent": .25, "fixture": .50})
        form = safe_float(p.get("Form Rating")) or 0.0
        recent = safe_float(p.get("Recent Opportunity Rating")) or 0.0
        fixture = safe_float(p.get("Next Fixture Rating")) or 0.0
        p["Decision Rating"] = round(clamp(w["form"]*form + w["recent"]*recent + w["fixture"]*fixture, 0, 100), 1)

    meta = data.setdefault("metadata", {})
    meta["decision_rating"] = {
        "version": "v5.1-rolling-validated-recent-opportunity-matching-fix",
        "weights": DECISION_WEIGHTS_V5,
        "uses": ["Form Rating", "Recent Opportunity Rating", "Next Fixture Rating"],
        "recent_window": "previous 4 team matches",
        "recent_calibration": "same-position current-player percentile",
        "fotmob_completed_matches": len(matches),
        "applied_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    DATA_PATH.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")

    print("\n=== DECISION V5.1 APPLIED ===")
    print(f"Players: {len(players)} | recent rows: {len(rows)}")
    for pos in ("GK", "DEF", "MID", "FOR"):
        eligible = [p for p in players if str(p.get("Position") or "").upper() == pos]
        ranked = sorted(eligible, key=lambda p: safe_float(p.get("Decision Rating")) or -1, reverse=True)[:8]
        print(f"\n{pos} top 8:")
        for p in ranked:
            print(f"  {p.get('Name')} ({p.get('Club')}): Decision {p.get('Decision Rating')} | Form {p.get('Form Rating')} | Recent {p.get('Recent Opportunity Rating')} | Fixture {p.get('Next Fixture Rating')} | mins/club-match {p.get('Recent Minutes Per Team Match')}")

if __name__ == "__main__":
    main()
