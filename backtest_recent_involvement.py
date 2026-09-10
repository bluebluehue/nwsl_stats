#!/usr/bin/env python3
"""
Historical NWSL Recent Involvement backtest.

GOAL
----
Test whether recent match-level FotMob activity predicts NEXT-MATCH fantasy
production, while correctly penalizing "super subs" and players who are not
actually playing starter-level minutes.

This is an AUDIT ONLY. It does not modify get_data.py, transformed_data.json,
Form Rating, Fixture Rating, Decision Rating, or the website.

IMPORTANT DESIGN CHOICE
-----------------------
The recent window is the player's club's previous FOUR TEAM MATCHES, not the
player's previous four appearances.

That means:
- 4 x 90-minute starter: 360 recent minutes
- 4 x 25-minute super-sub: ~100 recent minutes
- player who missed 3 of 4: only the minutes/actions from the one appearance

For fantasy usefulness, the primary activity variants are measured PER TEAM
MATCH, not only per 90. A player producing 2 goals/90 in 10-minute cameos does
not get treated like a 90-minute starter producing at the same rate.

Outputs
-------
recent_involvement_backtest.json
recent_involvement_backtest_samples.csv

Dependencies
------------
requests only (stdlib otherwise)
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


FOTMOB_BASES = (
    "https://www.fotmob.com/api/data",
    "https://www.fotmob.com/api",
)
FANTASY_API = "https://api.fantasynwsl.com/graphql"

NWSL_PARENT_LEAGUE_ID = 9134
NWSL_SEASON = "2026"
RECENT_TEAM_MATCH_WINDOW = 4

OUTPUT_JSON = Path("recent_involvement_backtest.json")
OUTPUT_CSV = Path("recent_involvement_backtest_samples.csv")

REQUEST_DELAY = 0.15
TIMEOUT = 30
RETRIES = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.fotmob.com/",
}

# Canonical stat keys from FotMob matchDetails -> content -> playerStats
STAT_KEYS = {
    "minutes": "minutes_played",
    "rating": "rating_title",
    "goals": "goals",
    "assists": "assists",
    "xg": "expected_goals",
    "npxg": "expected_goals_non_penalty",
    "xgot": "expected_goals_on_target_variant",
    "xa": "expected_assists",
    "shots": "total_shots",
    "sot": "ShotsOnTarget",
    "box_touches": "touches_opp_box",
    "chances": "chances_created",
    "big_chances_created": "big_chance_created_team_title",
    "big_chances_missed": "big_chance_missed_title",
    "dribbles": "dribbles_succeeded",
    "crosses": "accurate_crosses",
    "final_third_passes": "passes_into_final_third",
    "def_actions": "defensive_actions",
    "tackles": "matchstats.headers.tackles",
    "interceptions": "interceptions",
    "recoveries": "recoveries",
    "clearances": "clearances",
    "blocks": "shot_blocks",
    "ground_duels": "ground_duels_won",
    "aerial_duels": "aerials_won",
    "saves": "saves",
    "goals_conceded": "goals_conceded",
    "xgot_faced": "expected_goals_on_target_faced",
    "goals_prevented": "goals_prevented",
    "saves_inside_box": "saves_inside_box",
}

# For an appearing player, FotMob often omits a counting stat entirely when 0.
ZERO_IF_MISSING = {
    k for k in STAT_KEYS.values()
    if k not in {"rating_title", "expected_goals_on_target_faced", "goals_prevented"}
}

POSITION_MAP = {
    "GOALKEEPER": "GK",
    "DEFENDER": "DEF",
    "MIDFIELDER": "MID",
    "FORWARD": "FOR",
    "GK": "GK",
    "DEF": "DEF",
    "MID": "MID",
    "FOR": "FOR",
    "FWD": "FOR",
}

# Team-name aliases used only for match mapping.
TEAM_ALIASES = {
    "ANGEL CITY": "LA",
    "ANGEL CITY FC": "LA",
    "BAY FC": "BAY",
    "BOSTON LEGACY": "BOS",
    "BOSTON LEGACY FC": "BOS",
    "CHICAGO STARS": "CHI",
    "CHICAGO STARS FC": "CHI",
    "DENVER SUMMIT": "DEN",
    "DENVER SUMMIT FC": "DEN",
    "GOTHAM": "GFC",
    "GOTHAM FC": "GFC",
    "NJ NY GOTHAM FC": "GFC",
    "HOUSTON DASH": "HOU",
    "KANSAS CITY CURRENT": "KC",
    "LOUISVILLE": "LOU",
    "RACING LOUISVILLE": "LOU",
    "RACING LOUISVILLE FC": "LOU",
    "NORTH CAROLINA COURAGE": "NC",
    "NC COURAGE": "NC",
    "ORLANDO PRIDE": "ORL",
    "PORTLAND THORNS": "POR",
    "PORTLAND THORNS FC": "POR",
    "SAN DIEGO WAVE": "SD",
    "SAN DIEGO WAVE FC": "SD",
    "SEATTLE REIGN": "SEA",
    "SEATTLE REIGN FC": "SEA",
    "UTAH ROYALS": "UTA",
    "UTAH ROYALS FC": "UTA",
    "WASHINGTON SPIRIT": "WAS",
}

# Candidate activity composites. These are deliberately transparent rather
# than "final weights"; the backtest compares them side-by-side.
COMPOSITES = {
    "FOR": {
        "attack_core": {
            "xg": 0.30,
            "xa": 0.15,
            "sot": 0.20,
            "box_touches": 0.20,
            "chances": 0.15,
        },
        "attack_broad": {
            "xg": 0.25,
            "xa": 0.15,
            "sot": 0.15,
            "box_touches": 0.15,
            "chances": 0.15,
            "dribbles": 0.10,
            "crosses": 0.05,
        },
    },
    "MID": {
        "attack_core": {
            "xg": 0.20,
            "xa": 0.20,
            "sot": 0.15,
            "box_touches": 0.15,
            "chances": 0.20,
            "dribbles": 0.10,
        },
        "two_way": {
            "xg": 0.15,
            "xa": 0.15,
            "sot": 0.10,
            "box_touches": 0.10,
            "chances": 0.15,
            "tackles": 0.08,
            "interceptions": 0.07,
            "recoveries": 0.10,
            "dribbles": 0.05,
            "crosses": 0.05,
        },
    },
    "DEF": {
        "defense_core": {
            "tackles": 0.20,
            "interceptions": 0.20,
            "recoveries": 0.20,
            "clearances": 0.20,
            "blocks": 0.20,
        },
        "fantasy_two_way": {
            "xg": 0.08,
            "xa": 0.08,
            "box_touches": 0.08,
            "chances": 0.08,
            "tackles": 0.14,
            "interceptions": 0.14,
            "recoveries": 0.14,
            "clearances": 0.13,
            "blocks": 0.13,
        },
    },
    "GK": {
        "keeper_activity": {
            "saves": 0.35,
            "xgot_faced": 0.25,
            "goals_prevented": 0.25,
            "saves_inside_box": 0.15,
        },
    },
}


def norm_text(value: Any) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_team(value: Any) -> str:
    n = norm_text(value)
    return TEAM_ALIASES.get(n, n)


def norm_player_name(value: Any) -> str:
    return norm_text(value)


def player_match_key(name: str) -> tuple[str, str]:
    parts = norm_player_name(name).split()
    if not parts:
        return ("", "")
    return (parts[0][:1], parts[-1])


def safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def rankdata(values: list[float]) -> list[float]:
    """Average ranks for ties, 1-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        i = j
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(x*x for x in dx) * sum(y*y for y in dy))
    if den == 0:
        return None
    return sum(a*b for a, b in zip(dx, dy)) / den


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    return pearson(rankdata(xs), rankdata(ys))


def percentile(value: float, population: list[float]) -> float:
    if not population:
        return 50.0
    less = sum(1 for x in population if x < value)
    equal = sum(1 for x in population if x == value)
    # midrank percentile, 0..100
    return round(100.0 * (less + 0.5 * equal) / len(population), 3)


class Client:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.requests = 0

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_exc = None
        for base in FOTMOB_BASES:
            for attempt in range(RETRIES):
                try:
                    r = self.session.get(
                        f"{base}/{path}",
                        params=params,
                        timeout=TIMEOUT,
                    )
                    self.requests += 1
                    if r.status_code == 200:
                        time.sleep(REQUEST_DELAY)
                        return r.json()
                    if r.status_code in (403, 404):
                        break
                    if r.status_code == 429 or r.status_code >= 500:
                        time.sleep(min(8, 1.3 * (2 ** attempt)))
                        continue
                    r.raise_for_status()
                except Exception as exc:
                    last_exc = exc
                    time.sleep(min(8, 1.3 * (2 ** attempt)))
        raise RuntimeError(f"FotMob request failed {path}: {last_exc}")

    def league(self) -> dict[str, Any]:
        return self._get_json(
            "leagues",
            {"id": NWSL_PARENT_LEAGUE_ID, "ccode3": "USA", "season": NWSL_SEASON},
        )

    def match(self, match_id: int) -> dict[str, Any]:
        return self._get_json("matchDetails", {"matchId": match_id})

    def fantasy_graphql(self, query: str) -> dict[str, Any]:
        r = self.session.post(FANTASY_API, json={"query": query}, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"])
        return payload["data"]


def recursively_find_match_ids(obj: Any) -> set[int]:
    found: set[int] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "id" in x and (
                ("home" in x and "away" in x)
                or ("homeTeam" in x and "awayTeam" in x)
            ):
                try:
                    found.add(int(x["id"]))
                except Exception:
                    pass
            for key in ("linkToMatch", "pageUrl", "matchUrl"):
                value = x.get(key)
                if isinstance(value, str):
                    m = re.search(r"#(\d+)", value)
                    if m:
                        found.add(int(m.group(1)))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


def flatten_player_stats(player: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for section in player.get("stats", []) or []:
        stats = section.get("stats")
        if not isinstance(stats, dict):
            continue
        for descriptor in stats.values():
            if not isinstance(descriptor, dict):
                continue
            key = descriptor.get("key")
            stat = descriptor.get("stat") or {}
            if not key or not isinstance(stat, dict):
                continue
            out[key] = safe_float(stat.get("value"))
    return out


def parse_fotmob_match(payload: dict[str, Any]) -> dict[str, Any] | None:
    g = payload.get("general", {}) or {}
    if str(g.get("parentLeagueId")) != str(NWSL_PARENT_LEAGUE_ID):
        if str(g.get("leagueName") or "").upper() != "NWSL":
            return None
    dt = parse_dt(g.get("matchTimeUTCDate") or g.get("matchTimeUTC"))
    if not dt or dt.year != 2026 or not g.get("finished"):
        return None

    content = payload.get("content", {}) or {}
    pstats = content.get("playerStats", {}) or {}
    players = {}

    for pid, p in pstats.items():
        flat = flatten_player_stats(p)
        mins = safe_float(flat.get(STAT_KEYS["minutes"])) or 0.0
        if mins <= 0:
            continue

        # Zero-fill known counting stats.
        for key in ZERO_IF_MISSING:
            if flat.get(key) is None:
                flat[key] = 0.0

        players[str(pid)] = {
            "fotmob_id": p.get("id") or pid,
            "opta_id": p.get("optaId"),
            "name": p.get("name"),
            "name_norm": norm_player_name(p.get("name")),
            "name_key": player_match_key(p.get("name")),
            "team": norm_team(p.get("teamName")),
            "is_gk": bool(p.get("isGoalkeeper")),
            "stats": flat,
        }

    return {
        "match_id": int(g["matchId"]),
        "date": dt,
        "date_iso": dt.isoformat(),
        "home": norm_team((g.get("homeTeam") or {}).get("name")),
        "away": norm_team((g.get("awayTeam") or {}).get("name")),
        "players": players,
    }


FANTASY_QUERY = """
{
  games {
    id
    scheduledAt
    hasStarted
    stage { id }
    home {
      party {
        __typename
        ... on Club { id name shortName }
      }
      score
    }
    away {
      party {
        __typename
        ... on Club { id name shortName }
      }
      score
    }
  }
  players {
    slug
    firstName
    lastName
    club { id shortName }
    position
    performanceV2 {
      games {
        game {
          id
          scheduledAt
          stage { id }
          home {
            score
            party {
              __typename
              ... on Club { id shortName name }
            }
          }
          away {
            score
            party {
              __typename
              ... on Club { id shortName name }
            }
          }
        }
        points
        contributions {
          contribution
          quantity
          individualPoints
        }
      }
      extras {
        contributions {
          contribution
          quantity
          individualPoints
        }
      }
    }
  }
}
"""


def fantasy_game_info(game: dict[str, Any]) -> dict[str, Any] | None:
    dt = parse_dt(game.get("scheduledAt"))
    if not dt:
        return None
    home_party = (game.get("home") or {}).get("party") or {}
    away_party = (game.get("away") or {}).get("party") or {}
    return {
        "id": str(game.get("id")),
        "date": dt,
        "date_iso": dt.isoformat(),
        "gw": str((game.get("stage") or {}).get("id") or ""),
        "home": norm_team(home_party.get("shortName") or home_party.get("name") or home_party.get("id")),
        "away": norm_team(away_party.get("shortName") or away_party.get("name") or away_party.get("id")),
    }


def map_fantasy_to_fotmob_games(
    fantasy_games: list[dict[str, Any]],
    fotmob_matches: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """
    Match by normalized home/away teams and date within 36 hours.
    """
    mapping: dict[str, int] = {}
    diagnostics = []

    for fg_raw in fantasy_games:
        fg = fantasy_game_info(fg_raw)
        if not fg:
            continue
        candidates = []
        for fm in fotmob_matches:
            if fg["home"] != fm["home"] or fg["away"] != fm["away"]:
                continue
            hours = abs((fg["date"] - fm["date"]).total_seconds()) / 3600
            if hours <= 36:
                candidates.append((hours, fm["match_id"]))
        candidates.sort()
        if candidates:
            mapping[fg["id"]] = candidates[0][1]
        else:
            diagnostics.append({
                "fantasy_game_id": fg["id"],
                "date": fg["date_iso"],
                "home": fg["home"],
                "away": fg["away"],
            })
    return mapping, diagnostics


def build_team_match_history(fotmob_matches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_team: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in fotmob_matches:
        by_team[m["home"]].append(m)
        by_team[m["away"]].append(m)
    for team in by_team:
        by_team[team].sort(key=lambda m: m["date"])
    return by_team


def find_player_in_match(
    match: dict[str, Any],
    fantasy_name: str,
    team: str,
) -> dict[str, Any] | None:
    exact = []
    fallback = []
    nname = norm_player_name(fantasy_name)
    nkey = player_match_key(fantasy_name)

    for p in match["players"].values():
        if p["team"] != team:
            continue
        if p["name_norm"] == nname:
            exact.append(p)
        elif p["name_key"] == nkey:
            fallback.append(p)

    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    if len(fallback) == 1:
        return fallback[0]
    return None


def prior_team_matches(
    team_history: list[dict[str, Any]],
    target_date: datetime,
    n: int,
) -> list[dict[str, Any]]:
    prior = [m for m in team_history if m["date"] < target_date]
    return prior[-n:]


def recent_features(
    fantasy_name: str,
    team: str,
    target_date: datetime,
    team_history: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    window = prior_team_matches(team_history.get(team, []), target_date, RECENT_TEAM_MATCH_WINDOW)
    if len(window) < RECENT_TEAM_MATCH_WINDOW:
        return None

    totals = defaultdict(float)
    appearances = 0
    mins = 0.0
    sixty_plus = 0
    seventyfive_plus = 0
    matched_name_examples = []

    for m in window:
        p = find_player_in_match(m, fantasy_name, team)
        if not p:
            # Zero minutes and zero actions for a missed team match.
            continue

        appearances += 1
        matched_name_examples.append(p["name"])
        pm = safe_float(p["stats"].get(STAT_KEYS["minutes"])) or 0.0
        mins += pm
        if pm >= 60:
            sixty_plus += 1
        if pm >= 75:
            seventyfive_plus += 1

        for label, key in STAT_KEYS.items():
            if label in ("minutes", "rating"):
                continue
            value = safe_float(p["stats"].get(key))
            if value is not None:
                totals[label] += value

    # Per TEAM match is intentionally primary. It naturally penalizes substitutes,
    # missed matches, and unstable roles.
    per_team_match = {
        k: totals[k] / RECENT_TEAM_MATCH_WINDOW
        for k in totals
    }

    # Per 90 is retained for comparison/audit only.
    per90 = {
        k: (totals[k] * 90.0 / mins if mins > 0 else 0.0)
        for k in totals
    }

    return {
        "recent_team_matches": RECENT_TEAM_MATCH_WINDOW,
        "appearances": appearances,
        "appearance_rate": appearances / RECENT_TEAM_MATCH_WINDOW,
        "minutes": mins,
        "minutes_per_team_match": mins / RECENT_TEAM_MATCH_WINDOW,
        "minute_exposure": min(1.0, mins / (90.0 * RECENT_TEAM_MATCH_WINDOW)),
        "sixty_plus_rate": sixty_plus / RECENT_TEAM_MATCH_WINDOW,
        "seventyfive_plus_rate": seventyfive_plus / RECENT_TEAM_MATCH_WINDOW,
        "per_team_match": per_team_match,
        "per90": per90,
        "matched_name_examples": sorted(set(matched_name_examples)),
        "window_match_ids": [m["match_id"] for m in window],
        "window_dates": [m["date_iso"] for m in window],
    }


def total_game_points(game_record: dict[str, Any]) -> float:
    # The per-game points field is our main historical target.
    return safe_float(game_record.get("points")) or 0.0


def collect_samples(
    fantasy_players: list[dict[str, Any]],
    fantasy_games: list[dict[str, Any]],
    game_map: dict[str, int],
    fotmob_by_id: dict[int, dict[str, Any]],
    team_history: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    samples = []
    match_status = Counter()
    unmatched_players = Counter()

    for fp in fantasy_players:
        name = f"{fp.get('firstName','')} {fp.get('lastName','')}".strip()
        pos = POSITION_MAP.get(norm_text(fp.get("position")), norm_text(fp.get("position")))
        team = norm_team((fp.get("club") or {}).get("shortName") or (fp.get("club") or {}).get("id"))

        for perf in fp.get("performanceV2", []) or []:
            for gr in perf.get("games", []) or []:
                g = gr.get("game") or {}
                gid = str(g.get("id"))
                if gid not in game_map:
                    match_status["fantasy_game_not_mapped"] += 1
                    continue

                fm = fotmob_by_id.get(game_map[gid])
                if not fm:
                    match_status["fotmob_game_missing"] += 1
                    continue

                target_date = fm["date"]

                feats = recent_features(name, team, target_date, team_history)
                if feats is None:
                    match_status["not_four_prior_team_matches"] += 1
                    continue

                # Confirm that the player's name can be associated with FotMob at
                # least somewhere in the recent window OR target match. We keep
                # 0-minute recent windows if target match identifies the player.
                target_player = find_player_in_match(fm, name, team)
                if not feats["matched_name_examples"] and target_player is None:
                    unmatched_players[f"{name}|{team}"] += 1
                    match_status["player_name_unmatched"] += 1
                    continue

                target_points = total_game_points(gr)
                gw = str((g.get("stage") or {}).get("id") or "")
                samples.append({
                    "name": name,
                    "club": team,
                    "position": pos,
                    "gw": gw,
                    "target_game_id": gid,
                    "target_fotmob_match_id": fm["match_id"],
                    "target_date": fm["date_iso"],
                    "target_points": target_points,
                    **{f"role_{k}": feats[k] for k in (
                        "appearances", "appearance_rate", "minutes",
                        "minutes_per_team_match", "minute_exposure",
                        "sixty_plus_rate", "seventyfive_plus_rate",
                    )},
                    **{f"ptm_{k}": v for k, v in feats["per_team_match"].items()},
                    **{f"p90_{k}": v for k, v in feats["per90"].items()},
                })
                match_status["sample"] += 1

    return samples, {
        "status_counts": dict(match_status),
        "most_common_unmatched_players": unmatched_players.most_common(40),
    }


def add_percentile_features(samples: list[dict[str, Any]]) -> None:
    """
    Convert raw activity metrics to same-GW, same-position percentiles.
    This avoids comparing a raw xG scale directly to a raw tackles scale.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for s in samples:
        groups[(s["gw"], s["position"])].append(s)

    labels = set()
    for s in samples:
        for key in s:
            if key.startswith("ptm_") or key.startswith("p90_"):
                labels.add(key)

    for group in groups.values():
        for key in labels:
            pop = [safe_float(s.get(key)) or 0.0 for s in group]
            for s, value in zip(group, pop):
                s[f"pct_{key}"] = percentile(value, pop)

        # Role percentiles are useful independently.
        for key in (
            "role_minutes_per_team_match",
            "role_appearance_rate",
            "role_sixty_plus_rate",
            "role_seventyfive_plus_rate",
        ):
            pop = [safe_float(s.get(key)) or 0.0 for s in group]
            for s, value in zip(group, pop):
                s[f"pct_{key}"] = percentile(value, pop)


def composite_score(
    sample: dict[str, Any],
    weights: dict[str, float],
    mode: str,
) -> float:
    total = 0.0
    denom = 0.0
    prefix = "pct_ptm_" if mode == "per_team_match" else "pct_p90_"
    for feature, weight in weights.items():
        value = safe_float(sample.get(prefix + feature))
        if value is None:
            value = 50.0
        total += weight * value
        denom += weight
    return total / denom if denom else 50.0


def add_composites(samples: list[dict[str, Any]]) -> None:
    for s in samples:
        pos = s["position"]
        for name, weights in COMPOSITES.get(pos, {}).items():
            ptm = composite_score(s, weights, "per_team_match")
            p90 = composite_score(s, weights, "per90")

            s[f"score_{name}_ptm"] = round(ptm, 3)
            s[f"score_{name}_p90"] = round(p90, 3)

            # A third explicit "super-sub penalty" variant:
            # per90 activity shrunk toward neutral 50 by recent minute exposure.
            exposure = safe_float(s.get("role_minute_exposure")) or 0.0
            s[f"score_{name}_p90_exposure"] = round(
                50.0 + exposure * (p90 - 50.0),
                3,
            )


def metric_result(samples: list[dict[str, Any]], field: str) -> dict[str, Any]:
    pairs = [
        (safe_float(s.get(field)), safe_float(s.get("target_points")))
        for s in samples
    ]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    rho = spearman(xs, ys)

    # Top-quartile vs rest gives intuitive fantasy usefulness.
    top_avg = None
    rest_avg = None
    if len(xs) >= 8:
        ordered = sorted(range(len(xs)), key=lambda i: xs[i], reverse=True)
        top_n = max(1, len(xs) // 4)
        top_idx = set(ordered[:top_n])
        top_pts = [ys[i] for i in range(len(ys)) if i in top_idx]
        rest_pts = [ys[i] for i in range(len(ys)) if i not in top_idx]
        top_avg = statistics.fmean(top_pts) if top_pts else None
        rest_avg = statistics.fmean(rest_pts) if rest_pts else None

    return {
        "samples": len(pairs),
        "spearman_next_match_points": round(rho, 4) if rho is not None else None,
        "top_quartile_avg_next_points": round(top_avg, 3) if top_avg is not None else None,
        "rest_avg_next_points": round(rest_avg, 3) if rest_avg is not None else None,
        "top_quartile_lift": (
            round(top_avg - rest_avg, 3)
            if top_avg is not None and rest_avg is not None
            else None
        ),
    }


def evaluate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    results: dict[str, Any] = {}

    for pos in ("GK", "DEF", "MID", "FOR"):
        ps = [s for s in samples if s["position"] == pos]
        pos_result: dict[str, Any] = {"sample_count": len(ps)}

        # Role alone: does recent playing time predict next-match points?
        pos_result["role_signals"] = {
            key: metric_result(ps, key)
            for key in (
                "role_minutes_per_team_match",
                "role_appearance_rate",
                "role_sixty_plus_rate",
                "role_seventyfive_plus_rate",
            )
        }

        # Individual activity features in both modes.
        feature_results = {}
        relevant = set()
        for comp in COMPOSITES.get(pos, {}).values():
            relevant.update(comp.keys())

        for feature in sorted(relevant):
            feature_results[feature] = {
                "per_team_match": metric_result(ps, f"ptm_{feature}"),
                "per90": metric_result(ps, f"p90_{feature}"),
            }
        pos_result["individual_features"] = feature_results

        composites = {}
        for comp_name in COMPOSITES.get(pos, {}):
            composites[comp_name] = {
                "per_team_match": metric_result(ps, f"score_{comp_name}_ptm"),
                "per90": metric_result(ps, f"score_{comp_name}_p90"),
                "per90_exposure_adjusted": metric_result(
                    ps, f"score_{comp_name}_p90_exposure"
                ),
            }
        pos_result["composites"] = composites

        # Chronological holdout: latest 30% of GWs, if enough distinct GWs.
        numeric_gws = sorted({
            int(s["gw"]) for s in ps if str(s["gw"]).isdigit()
        })
        if len(numeric_gws) >= 6:
            split_idx = max(1, int(len(numeric_gws) * 0.70))
            holdout_gws = set(numeric_gws[split_idx:])
            holdout = [s for s in ps if str(s["gw"]).isdigit() and int(s["gw"]) in holdout_gws]
            holdout_res = {}
            for comp_name in COMPOSITES.get(pos, {}):
                holdout_res[comp_name] = {
                    "per_team_match": metric_result(
                        holdout, f"score_{comp_name}_ptm"
                    ),
                    "per90": metric_result(
                        holdout, f"score_{comp_name}_p90"
                    ),
                    "per90_exposure_adjusted": metric_result(
                        holdout, f"score_{comp_name}_p90_exposure"
                    ),
                }
            pos_result["chronological_holdout"] = {
                "holdout_gws": sorted(holdout_gws),
                "sample_count": len(holdout),
                "composites": holdout_res,
            }

        results[pos] = pos_result

    return results


def fetch_all_fotmob(client: Client) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    league = client.league()
    ids = sorted(recursively_find_match_ids(league))
    print(f"FotMob league endpoint found {len(ids)} candidate match IDs.")

    matches = []
    errors = []
    for i, mid in enumerate(ids, 1):
        try:
            parsed = parse_fotmob_match(client.match(mid))
            if parsed:
                matches.append(parsed)
        except Exception as exc:
            errors.append({"match_id": mid, "error": str(exc)})

        if i % 20 == 0:
            print(
                f"  FotMob {i}/{len(ids)} candidates; "
                f"{len(matches)} completed 2026 NWSL matches retained."
            )

    matches.sort(key=lambda m: m["date"])
    return matches, errors


def write_csv(samples: list[dict[str, Any]]) -> None:
    if not samples:
        return
    keys = sorted({k for s in samples for k in s.keys()})
    preferred = [
        "name", "club", "position", "gw", "target_date",
        "target_points", "role_minutes_per_team_match",
        "role_appearance_rate", "role_sixty_plus_rate",
        "role_seventyfive_plus_rate",
    ]
    fields = preferred + [k for k in keys if k not in preferred]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in samples:
            w.writerow(s)


def print_key_results(results: dict[str, Any]) -> None:
    print()
    print("=== RECENT INVOLVEMENT BACKTEST SUMMARY ===")
    for pos in ("GK", "DEF", "MID", "FOR"):
        r = results[pos]
        print()
        print(f"{pos}: {r['sample_count']} historical player-match samples")
        role = r["role_signals"]["role_minutes_per_team_match"]
        print(
            "  Recent minutes/team-match -> next fantasy points: "
            f"rho={role['spearman_next_match_points']} "
            f"topQ lift={role['top_quartile_lift']}"
        )
        for cname, modes in r["composites"].items():
            a = modes["per_team_match"]
            b = modes["per90"]
            c = modes["per90_exposure_adjusted"]
            print(
                f"  {cname}: "
                f"PER-TEAM-MATCH rho={a['spearman_next_match_points']} "
                f"lift={a['top_quartile_lift']} | "
                f"PER90 rho={b['spearman_next_match_points']} "
                f"lift={b['top_quartile_lift']} | "
                f"PER90+EXPOSURE rho={c['spearman_next_match_points']} "
                f"lift={c['top_quartile_lift']}"
            )
        hold = r.get("chronological_holdout")
        if hold:
            print(
                f"  Holdout GWs {hold['holdout_gws'][0]}-"
                f"{hold['holdout_gws'][-1]} ({hold['sample_count']} samples):"
            )
            for cname, modes in hold["composites"].items():
                a = modes["per_team_match"]
                b = modes["per90"]
                c = modes["per90_exposure_adjusted"]
                print(
                    f"    {cname}: PTM={a['spearman_next_match_points']} | "
                    f"P90={b['spearman_next_match_points']} | "
                    f"P90+EXP={c['spearman_next_match_points']}"
                )
    print("=== END RECENT INVOLVEMENT BACKTEST SUMMARY ===")


def main() -> int:
    client = Client()

    print("=== NWSL HISTORICAL RECENT INVOLVEMENT BACKTEST ===")
    print(
        f"Recent window = previous {RECENT_TEAM_MATCH_WINDOW} TEAM matches "
        "(not previous player appearances)."
    )
    print(
        "Primary test = per-team-match activity, so super subs and missed matches "
        "are naturally penalized."
    )
    print("This script does NOT modify the live fantasy model.")
    print()

    fantasy = client.fantasy_graphql(FANTASY_QUERY)
    fantasy_games = fantasy.get("games", []) or []
    fantasy_players = fantasy.get("players", []) or []
    print(
        f"Fantasy API: {len(fantasy_games)} games, "
        f"{len(fantasy_players)} players."
    )

    fotmob_matches, fetch_errors = fetch_all_fotmob(client)
    print(
        f"FotMob completed 2026 NWSL matches parsed: {len(fotmob_matches)}; "
        f"fetch errors: {len(fetch_errors)}"
    )

    game_map, unmapped_games = map_fantasy_to_fotmob_games(
        fantasy_games, fotmob_matches
    )
    print(
        f"Fantasy->FotMob completed game mappings: {len(game_map)}; "
        f"unmapped fantasy games: {len(unmapped_games)}"
    )

    fm_by_id = {m["match_id"]: m for m in fotmob_matches}
    team_history = build_team_match_history(fotmob_matches)

    samples, sample_diag = collect_samples(
        fantasy_players,
        fantasy_games,
        game_map,
        fm_by_id,
        team_history,
    )
    print(f"Historical player-match samples built: {len(samples)}")

    add_percentile_features(samples)
    add_composites(samples)
    results = evaluate(samples)
    print_key_results(results)

    # Current-player sanity checks from latest available sample per player.
    latest_by_name = {}
    for s in sorted(samples, key=lambda x: x["target_date"]):
        latest_by_name[norm_player_name(s["name"])] = s

    spot_names = [
        "Ashley Sanchez",
        "Samantha Kerr",
        "Jordynn Dudley",
        "Jessie Fleming",
    ]
    spot_checks = {}
    for name in spot_names:
        s = latest_by_name.get(norm_player_name(name))
        if s:
            spot_checks[name] = {
                k: v for k, v in s.items()
                if (
                    k.startswith("role_")
                    or k.startswith("ptm_")
                    or k.startswith("p90_")
                    or k.startswith("score_")
                    or k in {
                        "name", "club", "position", "gw",
                        "target_date", "target_points"
                    }
                )
            }

    payload = {
        "metadata": {
            "version": "recent-involvement-backtest-v1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "production_model_changed": False,
            "season": 2026,
            "recent_window": (
                f"previous {RECENT_TEAM_MATCH_WINDOW} team matches"
            ),
            "super_sub_treatment": (
                "Primary activity rates are per TEAM match, not per 90. "
                "Missed matches count as zero minutes/actions. Per90 and "
                "per90-with-minute-exposure are retained for direct comparison."
            ),
            "fotmob_completed_matches": len(fotmob_matches),
            "fotmob_requests": client.requests,
            "fotmob_fetch_errors": len(fetch_errors),
            "fantasy_games": len(fantasy_games),
            "fantasy_players": len(fantasy_players),
            "fantasy_to_fotmob_game_mappings": len(game_map),
            "historical_samples": len(samples),
        },
        "mapping_diagnostics": {
            "unmapped_fantasy_games": unmapped_games,
            **sample_diag,
        },
        "candidate_composites": COMPOSITES,
        "results": results,
        "spot_checks": spot_checks,
        "fotmob_fetch_errors": fetch_errors,
    }

    OUTPUT_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_csv(samples)

    print()
    print(f"Saved: {OUTPUT_JSON}")
    print(f"Saved: {OUTPUT_CSV}")
    print("=== END NWSL HISTORICAL RECENT INVOLVEMENT BACKTEST ===")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise
