#!/usr/bin/env python3
"""
NWSL Decision architecture backtest v2

This audit answers four questions:

1. Can we map essentially all completed 2026 Fantasy NWSL games to FotMob?
2. How should true RECENT match-level involvement be measured?
3. How much do Form, Recent Opportunity/Involvement, and Fixture each predict
   the NEXT FANTASY GAMEWEEK?
4. What blend works best by fantasy position, including DGWs?

Nothing in this script changes the live website/model.

KEY FANTASY PRINCIPLE
---------------------
The recent involvement window is the club's PREVIOUS FOUR TEAM MATCHES.
Activity is measured per TEAM match, not merely per 90. Missing a match or
playing a short cameo therefore hurts the signal automatically.

A separate role score (recent minutes/team-match) is also tested. This prevents
a "2 goals per 90, but only 10 minutes every week" super-sub from looking like
a 90-minute starter.

TARGET
------
Next GAMEWEEK fantasy points, not merely next individual match points.
If a player has a DGW, game points are summed for the target GW and the fixture
rating keeps the production model's strong avg * sqrt(number_of_fixtures) DGW
boost.

NO LOOKAHEAD
------------
For each historical target GW:
- Form uses only prior player matches.
- Recent involvement uses only the prior four TEAM matches.
- ASA team strength uses only ASA games before the target GW.
- Same-GW percentiles are calculated from pre-GW information only.

Outputs:
  decision_architecture_backtest_v2.json
  decision_architecture_backtest_v2_samples.csv
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
from itertools import product
from pathlib import Path
from typing import Any

import requests


# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

FANTASY_API = "https://api.fantasynwsl.com/graphql"
FOTMOB_BASES = (
    "https://www.fotmob.com/api/data",
    "https://www.fotmob.com/api",
)
ASA_BASE_URL = "https://app.americansocceranalysis.com/api/v1"

NWSL_PARENT_LEAGUE_ID = 9134
NWSL_SEASON = "2026"
RECENT_TEAM_MATCH_WINDOW = 4

OUTPUT_JSON = Path("decision_architecture_backtest_v2.json")
OUTPUT_CSV = Path("decision_architecture_backtest_v2_samples.csv")

REQUEST_DELAY = 0.12
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

# Current production NWSL fixture-model controls.
RECENT_MATCH_COUNT = 6
RECENCY_DECAY = 0.82
SEASON_WEIGHT = 0.65
RECENT_WEIGHT = 0.35
ATTACK_XG_WEIGHT = 0.75
ATTACK_GOALS_WEIGHT = 0.25
DEFENSE_XGA_WEIGHT = 0.80
DEFENSE_GOALS_WEIGHT = 0.20
ATTACK_XG_FLOOR = 0.55
ATTACK_XG_CEILING = 2.25
DEFENSE_XG_BEST = 0.55
DEFENSE_XG_WORST = 2.25

# Current live Decision outer weights, retained only as a benchmark.
LIVE_DECISION_WEIGHTS = {
    "GK":  {"fixture": 0.85, "form": 0.15},
    "DEF": {"fixture": 0.65, "form": 0.35},
    "MID": {"fixture": 0.35, "form": 0.65},
    "FOR": {"fixture": 0.25, "form": 0.75},
}

# FotMob match-level stat keys.
STAT_KEYS = {
    "minutes": "minutes_played",
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
    "dribbles": "dribbles_succeeded",
    "crosses": "accurate_crosses",
    "final_third_passes": "passes_into_final_third",
    "tackles": "matchstats.headers.tackles",
    "interceptions": "interceptions",
    "recoveries": "recoveries",
    "clearances": "clearances",
    "blocks": "shot_blocks",
    "saves": "saves",
    "goals_conceded": "goals_conceded",
    "xgot_faced": "expected_goals_on_target_faced",
    "goals_prevented": "goals_prevented",
    "saves_inside_box": "saves_inside_box",
}

ZERO_IF_MISSING = {
    key for label, key in STAT_KEYS.items()
    if label not in {"xgot_faced", "goals_prevented"}
}

# Candidate recent ACTIVITY composites. These are tested, not assumed final.
COMPOSITES = {
    "DEF": {
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
    "MID": {
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
    "FOR": {
        "attack_core": {
            "xg": 0.30,
            "xa": 0.15,
            "sot": 0.20,
            "box_touches": 0.20,
            "chances": 0.15,
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

# Role share inside the "Recent Opportunity" signal.
# This is explicitly tested against activity-only in the output.
ROLE_BLEND = {
    "GK": 0.00,   # prior audit did not support a recent-GK activity layer
    "DEF": 0.40,
    "MID": 0.25,
    "FOR": 0.40,
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

TEAM_ALIASES = {
    # codes
    "LA": "LA", "BAY": "BAY", "BOS": "BOS", "CHI": "CHI",
    "DEN": "DEN", "GFC": "GFC", "HOU": "HOU", "KC": "KC",
    "LOU": "LOU", "NC": "NC", "ORL": "ORL", "POR": "POR",
    "SD": "SD", "SEA": "SEA", "UTA": "UTA", "WAS": "WAS",

    # full names / variants
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
    "NJ NY GOTHAM": "GFC",
    "HOUSTON DASH": "HOU",
    "KANSAS CITY CURRENT": "KC",
    "RACING LOUISVILLE": "LOU",
    "RACING LOUISVILLE FC": "LOU",
    "LOUISVILLE": "LOU",
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

ASA_TEAM_NAME_TO_CODE = {
    "Denver Summit FC": "DEN",
    "Bay FC": "BAY",
    "Houston Dash": "HOU",
    "Boston Legacy FC": "BOS",
    "Kansas City Current": "KC",
    "San Diego Wave FC": "SD",
    "Seattle Reign FC": "SEA",
    "Chicago Stars FC": "CHI",
    "Portland Thorns FC": "POR",
    "Orlando Pride": "ORL",
    "Washington Spirit": "WAS",
    "Utah Royals FC": "UTA",
    "Racing Louisville FC": "LOU",
    "Angel City FC": "LA",
    "NJ/NY Gotham FC": "GFC",
    "North Carolina Courage": "NC",
}


# ---------------------------------------------------------------------
# BASICS
# ---------------------------------------------------------------------

def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm_text(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_team(v: Any) -> str:
    n = norm_text(v)
    return TEAM_ALIASES.get(n, n)


def norm_player(v: Any) -> str:
    return norm_text(v)


def player_name_key(v: Any) -> tuple[str, str]:
    parts = norm_player(v).split()
    if not parts:
        return ("", "")
    return (parts[0][:1], parts[-1])


def parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        # ASA format
        try:
            return datetime.strptime(
                str(v).replace(" UTC", ""),
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return None


def rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg
        i = j
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
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
    less = sum(x < value for x in population)
    equal = sum(x == value for x in population)
    return 100.0 * (less + 0.5 * equal) / len(population)


def weighted_average(values: list[float], decay: float = RECENCY_DECAY) -> float:
    if not values:
        return 0.0
    weights = [decay ** i for i in range(len(values))]
    den = sum(weights)
    return sum(v*w for v, w in zip(values, weights)) / den if den else statistics.fmean(values)


# ---------------------------------------------------------------------
# CLIENT
# ---------------------------------------------------------------------

class Client:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.fotmob_requests = 0

    def fotmob_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last = None
        for base in FOTMOB_BASES:
            for attempt in range(RETRIES):
                try:
                    r = self.session.get(
                        f"{base}/{path}",
                        params=params,
                        timeout=TIMEOUT,
                    )
                    self.fotmob_requests += 1
                    if r.status_code == 200:
                        time.sleep(REQUEST_DELAY)
                        return r.json()
                    if r.status_code in (403, 404):
                        break
                    if r.status_code == 429 or r.status_code >= 500:
                        time.sleep(min(8, 1.25 * (2 ** attempt)))
                        continue
                    r.raise_for_status()
                except Exception as exc:
                    last = exc
                    time.sleep(min(8, 1.25 * (2 ** attempt)))
        raise RuntimeError(f"FotMob request failed {path}: {last}")

    def league(self) -> dict[str, Any]:
        return self.fotmob_json(
            "leagues",
            {"id": NWSL_PARENT_LEAGUE_ID, "ccode3": "USA", "season": NWSL_SEASON},
        )

    def match(self, mid: int) -> dict[str, Any]:
        return self.fotmob_json("matchDetails", {"matchId": mid})

    def fantasy(self) -> dict[str, Any]:
        q = FANTASY_QUERY
        r = self.session.post(
            FANTASY_API,
            json={"query": q},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"])
        return payload["data"]

    def asa_json(self, path: str) -> Any:
        r = self.session.get(f"{ASA_BASE_URL}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------
# FANTASY DATA
# ---------------------------------------------------------------------

FANTASY_QUERY = """
{
  games {
    id
    scheduledAt
    hasStarted
    stage { id }
    home {
      score
      party {
        __typename
        ... on Club { id name shortName }
      }
    }
    away {
      score
      party {
        __typename
        ... on Club { id name shortName }
      }
    }
  }
  players {
    slug
    firstName
    lastName
    club { id name shortName }
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
              ... on Club { id name shortName }
            }
          }
          away {
            score
            party {
              __typename
              ... on Club { id name shortName }
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
    }
  }
}
"""


def fantasy_game_info(g: dict[str, Any]) -> dict[str, Any] | None:
    dt = parse_dt(g.get("scheduledAt"))
    if not dt:
        return None
    hp = (g.get("home") or {}).get("party") or {}
    ap = (g.get("away") or {}).get("party") or {}
    return {
        "id": str(g.get("id")),
        "date": dt,
        "gw": str((g.get("stage") or {}).get("id") or ""),
        "home": norm_team(hp.get("id") or hp.get("shortName") or hp.get("name")),
        "away": norm_team(ap.get("id") or ap.get("shortName") or ap.get("name")),
        "home_score": safe_float((g.get("home") or {}).get("score")),
        "away_score": safe_float((g.get("away") or {}).get("score")),
        "has_started": g.get("hasStarted"),
    }


def fantasy_player_records(player: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for perf in player.get("performanceV2", []) or []:
        for gr in perf.get("games", []) or []:
            g = gr.get("game") or {}
            info = fantasy_game_info(g)
            if not info:
                continue
            got_bonus = any(
                c.get("contribution") == "Bonus" and (c.get("quantity") or 0) > 0
                for c in gr.get("contributions", []) or []
            )
            rows.append({
                **info,
                "points": safe_float(gr.get("points")) or 0.0,
                "got_bonus": got_bonus,
            })
    rows.sort(key=lambda x: x["date"])
    return rows


def calculate_form_rating(points: list[float], bonus_games: int) -> float:
    if not points:
        return 0.0
    n = len(points)
    ppg = sum(points) / n
    ppg_score = clamp((ppg / 8.0) * 100.0, 0, 100)
    consistency = 100.0 * sum(p >= 3 for p in points) / n
    bonus = 100.0 * bonus_games / n
    return round(0.50 * ppg_score + 0.35 * consistency + 0.15 * bonus)


def historical_form(
    player_records: list[dict[str, Any]],
    target_start: datetime,
    target_gw: int,
) -> float:
    prior = [r for r in player_records if r["date"] < target_start]
    recent = list(reversed(prior[-4:]))
    if not recent:
        return 0.0

    points = [r["points"] for r in recent]
    bonus_games = sum(r["got_bonus"] for r in recent)
    rating = calculate_form_rating(points, bonus_games)

    # Mirror the live recency penalty.
    prior_gws = [int(r["gw"]) for r in prior if str(r["gw"]).isdigit()]
    last_played_gw = max(prior_gws) if prior_gws else None
    latest_completed_gw = target_gw - 1
    if last_played_gw is not None:
        missed_gws = latest_completed_gw - last_played_gw
        if missed_gws == 2:
            rating = round(rating * 0.5)
        elif missed_gws >= 3:
            rating = 0.0
    return float(rating)


# ---------------------------------------------------------------------
# FOTMOB
# ---------------------------------------------------------------------

def recursively_find_match_ids(obj: Any) -> set[int]:
    ids = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            candidate = x.get("id")
            if candidate is not None and (
                ("home" in x and "away" in x)
                or ("homeTeam" in x and "awayTeam" in x)
            ):
                try:
                    ids.add(int(candidate))
                except Exception:
                    pass
            for key in ("linkToMatch", "pageUrl", "matchUrl"):
                val = x.get(key)
                if isinstance(val, str):
                    m = re.search(r"#(\d+)", val)
                    if m:
                        ids.add(int(m.group(1)))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return ids


def flatten_player_stats(p: dict[str, Any]) -> dict[str, float | None]:
    flat = {}
    for section in p.get("stats", []) or []:
        stats = section.get("stats")
        if not isinstance(stats, dict):
            continue
        for desc in stats.values():
            if not isinstance(desc, dict):
                continue
            key = desc.get("key")
            stat = desc.get("stat") or {}
            if key and isinstance(stat, dict):
                flat[key] = safe_float(stat.get("value"))
    return flat


def parse_fotmob_match(payload: dict[str, Any]) -> dict[str, Any] | None:
    g = payload.get("general", {}) or {}
    if (
        str(g.get("parentLeagueId")) != str(NWSL_PARENT_LEAGUE_ID)
        and str(g.get("leagueName") or "").upper() != "NWSL"
    ):
        return None

    dt = parse_dt(g.get("matchTimeUTCDate") or g.get("matchTimeUTC"))
    if not dt or dt.year != 2026 or not g.get("finished"):
        return None

    hs = safe_float(((payload.get("header") or {}).get("teams") or [{}])[0].get("score"))
    teams_header = (payload.get("header") or {}).get("teams") or []
    away_score = safe_float(teams_header[1].get("score")) if len(teams_header) > 1 else None

    players = {}
    for pid, p in ((payload.get("content") or {}).get("playerStats") or {}).items():
        flat = flatten_player_stats(p)
        mins = safe_float(flat.get(STAT_KEYS["minutes"])) or 0.0
        if mins <= 0:
            continue
        for key in ZERO_IF_MISSING:
            if flat.get(key) is None:
                flat[key] = 0.0
        players[str(pid)] = {
            "fotmob_id": p.get("id") or pid,
            "opta_id": p.get("optaId"),
            "name": p.get("name"),
            "name_norm": norm_player(p.get("name")),
            "name_key": player_name_key(p.get("name")),
            "team": norm_team(p.get("teamName")),
            "stats": flat,
        }

    return {
        "match_id": int(g["matchId"]),
        "date": dt,
        "home": norm_team((g.get("homeTeam") or {}).get("name")),
        "away": norm_team((g.get("awayTeam") or {}).get("name")),
        "home_score": hs,
        "away_score": away_score,
        "players": players,
    }


def fetch_fotmob_matches(client: Client) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ids = sorted(recursively_find_match_ids(client.league()))
    print(f"FotMob league endpoint found {len(ids)} candidate match IDs.")
    matches, errors = [], []

    for i, mid in enumerate(ids, 1):
        try:
            m = parse_fotmob_match(client.match(mid))
            if m:
                matches.append(m)
        except Exception as exc:
            errors.append({"match_id": mid, "error": str(exc)})
        if i % 20 == 0:
            print(
                f"  FotMob {i}/{len(ids)}; "
                f"{len(matches)} completed league matches retained."
            )
    matches.sort(key=lambda x: x["date"])
    return matches, errors


# ---------------------------------------------------------------------
# ROBUST FANTASY <-> FOTMOB MATCH MAPPING
# ---------------------------------------------------------------------

def map_fantasy_to_fotmob(
    fantasy_games: list[dict[str, Any]],
    fotmob: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, Any]]:
    """
    Multi-stage mapping.

    Stage 1: exact normalized home+away + close date
    Stage 2: exact unordered team pair + close date
    Stage 3: unique close datetime + exact scoreline
    Stage 4: unique nearest datetime within 3 hours

    Stages 3/4 exist specifically to survive provider team-name/code differences.
    Ambiguous matches are NEVER guessed.
    """
    fmap = {}
    methods = Counter()
    unresolved = []
    completed_infos = []

    for raw in fantasy_games:
        fg = fantasy_game_info(raw)
        if not fg or fg["has_started"] is not True:
            continue
        completed_infos.append(fg)

        # stage 1
        cands = []
        for fm in fotmob:
            hours = abs((fg["date"] - fm["date"]).total_seconds()) / 3600.0
            if hours <= 36 and fg["home"] == fm["home"] and fg["away"] == fm["away"]:
                cands.append((hours, fm))
        cands.sort(key=lambda x: x[0])
        if len(cands) == 1 or (len(cands) > 1 and cands[0][0] < cands[1][0]):
            fmap[fg["id"]] = cands[0][1]["match_id"]
            methods["exact_teams_date"] += 1
            continue

        # stage 2
        cands = []
        pair = {fg["home"], fg["away"]}
        for fm in fotmob:
            hours = abs((fg["date"] - fm["date"]).total_seconds()) / 3600.0
            if hours <= 36 and pair == {fm["home"], fm["away"]}:
                cands.append((hours, fm))
        cands.sort(key=lambda x: x[0])
        if len(cands) == 1 or (len(cands) > 1 and cands[0][0] < cands[1][0]):
            fmap[fg["id"]] = cands[0][1]["match_id"]
            methods["unordered_teams_date"] += 1
            continue

        # stage 3: date/time + scoreline
        cands = []
        for fm in fotmob:
            hours = abs((fg["date"] - fm["date"]).total_seconds()) / 3600.0
            score_same = (
                fg["home_score"] is not None
                and fg["away_score"] is not None
                and fm["home_score"] is not None
                and fm["away_score"] is not None
                and fg["home_score"] == fm["home_score"]
                and fg["away_score"] == fm["away_score"]
            )
            if hours <= 6 and score_same:
                cands.append((hours, fm))
        cands.sort(key=lambda x: x[0])
        if len(cands) == 1:
            fmap[fg["id"]] = cands[0][1]["match_id"]
            methods["datetime_score"] += 1
            continue

        # stage 4: a unique very-close kickoff is safe even if provider team
        # naming differs. If two NWSL matches are equally close, reject.
        cands = sorted(
            (
                abs((fg["date"] - fm["date"]).total_seconds()) / 3600.0,
                fm,
            )
            for fm in fotmob
            if abs((fg["date"] - fm["date"]).total_seconds()) / 3600.0 <= 3
        )
        if len(cands) == 1:
            fmap[fg["id"]] = cands[0][1]["match_id"]
            methods["unique_datetime"] += 1
            continue

        unresolved.append({
            "id": fg["id"],
            "gw": fg["gw"],
            "date": fg["date"].isoformat(),
            "home": fg["home"],
            "away": fg["away"],
            "score": [fg["home_score"], fg["away_score"]],
            "nearby": [
                {
                    "hours": round(hours, 3),
                    "match_id": fm["match_id"],
                    "home": fm["home"],
                    "away": fm["away"],
                    "score": [fm["home_score"], fm["away_score"]],
                }
                for hours, fm in cands[:5]
            ],
        })

    return fmap, {
        "completed_fantasy_games": len(completed_infos),
        "mapped": len(fmap),
        "methods": dict(methods),
        "unresolved": unresolved,
    }


# ---------------------------------------------------------------------
# RECENT INVOLVEMENT / ROLE
# ---------------------------------------------------------------------

def build_team_history(matches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out = defaultdict(list)
    for m in matches:
        out[m["home"]].append(m)
        out[m["away"]].append(m)
    for team in out:
        out[team].sort(key=lambda x: x["date"])
    return out


def find_player(match: dict[str, Any], fantasy_name: str, team: str) -> dict[str, Any] | None:
    exact, fallback = [], []
    nn = norm_player(fantasy_name)
    nk = player_name_key(fantasy_name)
    for p in match["players"].values():
        if p["team"] != team:
            continue
        if p["name_norm"] == nn:
            exact.append(p)
        elif p["name_key"] == nk:
            fallback.append(p)
    if len(exact) == 1:
        return exact[0]
    if not exact and len(fallback) == 1:
        return fallback[0]
    return None


def recent_features(
    fantasy_name: str,
    team: str,
    target_start: datetime,
    team_history: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    prior = [m for m in team_history.get(team, []) if m["date"] < target_start]
    window = prior[-RECENT_TEAM_MATCH_WINDOW:]
    if len(window) < RECENT_TEAM_MATCH_WINDOW:
        return None

    totals = defaultdict(float)
    minutes = 0.0
    appearances = 0
    sixty = 0
    seventyfive = 0
    ever_matched = False

    for m in window:
        p = find_player(m, fantasy_name, team)
        if not p:
            continue
        ever_matched = True
        appearances += 1
        mins = safe_float(p["stats"].get(STAT_KEYS["minutes"])) or 0.0
        minutes += mins
        sixty += mins >= 60
        seventyfive += mins >= 75

        for label, key in STAT_KEYS.items():
            if label == "minutes":
                continue
            value = safe_float(p["stats"].get(key))
            if value is not None:
                totals[label] += value

    return {
        "ever_matched": ever_matched,
        "appearances": appearances,
        "minutes": minutes,
        "minutes_per_team_match": minutes / RECENT_TEAM_MATCH_WINDOW,
        "appearance_rate": appearances / RECENT_TEAM_MATCH_WINDOW,
        "sixty_plus_rate": sixty / RECENT_TEAM_MATCH_WINDOW,
        "seventyfive_plus_rate": seventyfive / RECENT_TEAM_MATCH_WINDOW,
        "ptm": {
            k: totals[k] / RECENT_TEAM_MATCH_WINDOW
            for k in totals
        },
    }


# ---------------------------------------------------------------------
# HISTORICAL ASA FIXTURE MODEL
# ---------------------------------------------------------------------

def fetch_asa(client: Client) -> tuple[list[dict[str, Any]], dict[Any, str]]:
    teams = client.asa_json("/nwsl/teams")
    team_map = {}
    for t in teams:
        code = ASA_TEAM_NAME_TO_CODE.get(t.get("team_name"))
        if code:
            team_map[t.get("team_id")] = code

    all_games = client.asa_json("/nwsl/games/xgoals")
    season = [
        g for g in all_games
        if str(g.get("date_time_utc", "")).startswith("2026-")
    ]
    return season, team_map


def filter_asa_to_regular_season(
    asa_games: list[dict[str, Any]],
    team_map: dict[Any, str],
    fotmob_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    kept = []
    for ag in asa_games:
        ah = team_map.get(ag.get("home_team_id"))
        aa = team_map.get(ag.get("away_team_id"))
        ad = parse_dt(ag.get("date_time_utc"))
        if not ah or not aa or not ad:
            continue
        candidates = [
            fm for fm in fotmob_matches
            if fm["home"] == ah
            and fm["away"] == aa
            and abs((fm["date"] - ad).total_seconds()) <= 36 * 3600
        ]
        if candidates:
            kept.append(ag)
    return kept


def build_strength_as_of(
    asa_regular: list[dict[str, Any]],
    team_map: dict[Any, str],
    cutoff: datetime,
) -> tuple[dict[str, Any], dict[str, float]] | None:
    prior = []
    for g in asa_regular:
        d = parse_dt(g.get("date_time_utc"))
        if d and d < cutoff:
            prior.append(g)

    if not prior:
        return None

    team_games = defaultdict(list)
    all_home_xg, all_away_xg = [], []

    for g in prior:
        home = team_map.get(g.get("home_team_id"))
        away = team_map.get(g.get("away_team_id"))
        d = parse_dt(g.get("date_time_utc"))
        if not home or not away or not d:
            continue
        try:
            hxg = float(g.get("home_team_xgoals", 0) or 0)
            axg = float(g.get("away_team_xgoals", 0) or 0)
            hg = int(g.get("home_goals", 0) or 0)
            ag = int(g.get("away_goals", 0) or 0)
        except Exception:
            continue

        all_home_xg.append(hxg)
        all_away_xg.append(axg)

        team_games[home].append({
            "date": d, "xgf": hxg, "xga": axg, "gf": hg, "ga": ag
        })
        team_games[away].append({
            "date": d, "xgf": axg, "xga": hxg, "gf": ag, "ga": hg
        })

    strength = {}
    for team, rows in team_games.items():
        rows = sorted(rows, key=lambda x: x["date"], reverse=True)
        n = len(rows)
        if n == 0:
            continue

        sxg = sum(r["xgf"] for r in rows) / n
        sxga = sum(r["xga"] for r in rows) / n
        sgf = sum(r["gf"] for r in rows) / n
        sga = sum(r["ga"] for r in rows) / n

        recent = rows[:RECENT_MATCH_COUNT]
        rxg = weighted_average([r["xgf"] for r in recent])
        rxga = weighted_average([r["xga"] for r in recent])
        rgf = weighted_average([r["gf"] for r in recent])
        rga = weighted_average([r["ga"] for r in recent])

        season_attack = ATTACK_XG_WEIGHT*sxg + ATTACK_GOALS_WEIGHT*sgf
        recent_attack = ATTACK_XG_WEIGHT*rxg + ATTACK_GOALS_WEIGHT*rgf
        season_def = DEFENSE_XGA_WEIGHT*sxga + DEFENSE_GOALS_WEIGHT*sga
        recent_def = DEFENSE_XGA_WEIGHT*rxga + DEFENSE_GOALS_WEIGHT*rga

        strength[team] = {
            "attack": SEASON_WEIGHT*season_attack + RECENT_WEIGHT*recent_attack,
            "def_allowed": SEASON_WEIGHT*season_def + RECENT_WEIGHT*recent_def,
        }

    if len(strength) < 8:
        return None

    league_attack = statistics.fmean(s["attack"] for s in strength.values())
    league_def = statistics.fmean(s["def_allowed"] for s in strength.values())

    # Current production code derives league xG from team season xG means.
    team_sxg = []
    for rows in team_games.values():
        if rows:
            team_sxg.append(sum(r["xgf"] for r in rows) / len(rows))
    league_xg = statistics.fmean(team_sxg) if team_sxg else 1.45

    avg_h = statistics.fmean(all_home_xg) if all_home_xg else league_xg
    avg_a = statistics.fmean(all_away_xg) if all_away_xg else league_xg
    if avg_h > 0 and avg_a > 0:
        home_factor = clamp((avg_h / avg_a) ** 0.5, 0.90, 1.10)
    else:
        home_factor = 1.0

    ctx = {
        "league_attack": league_attack,
        "league_def": league_def,
        "league_xg": league_xg,
        "home_factor": home_factor,
        "away_factor": 1.0 / home_factor if home_factor else 1.0,
    }
    return strength, ctx


def projected_xg(
    attack_team: str,
    defense_team: str,
    is_home: bool,
    strength: dict[str, Any],
    ctx: dict[str, float],
) -> float | None:
    a = strength.get(attack_team)
    d = strength.get(defense_team)
    if not a or not d:
        return None
    if ctx["league_attack"] <= 0 or ctx["league_def"] <= 0:
        return None
    attack_idx = a["attack"] / ctx["league_attack"]
    weakness_idx = d["def_allowed"] / ctx["league_def"]
    matchup = max(0.01, attack_idx * weakness_idx) ** 0.5
    venue = ctx["home_factor"] if is_home else ctx["away_factor"]
    return clamp(ctx["league_xg"] * matchup * venue, 0.05, 4.0)


def single_fixture_rating(
    position: str,
    team: str,
    opp: str,
    is_home: bool,
    strength: dict[str, Any],
    ctx: dict[str, float],
) -> float:
    own_xg = projected_xg(team, opp, is_home, strength, ctx)
    opp_xg = projected_xg(opp, team, not is_home, strength, ctx)
    if own_xg is None or opp_xg is None:
        return 50.0

    attack = clamp(
        100.0 * (own_xg - ATTACK_XG_FLOOR)
        / (ATTACK_XG_CEILING - ATTACK_XG_FLOOR),
        0,
        100,
    )
    defense = clamp(
        100.0 * (DEFENSE_XG_WORST - opp_xg)
        / (DEFENSE_XG_WORST - DEFENSE_XG_BEST),
        0,
        100,
    )

    if position == "FOR":
        return attack
    if position == "MID":
        return 0.90*attack + 0.10*defense
    if position in {"DEF", "GK"}:
        return defense
    return 50.0


def combine_fixture_scores(scores: list[float]) -> float:
    if not scores:
        return 0.0
    avg = statistics.fmean(scores)
    if len(scores) == 1:
        return round(avg, 1)
    # Preserve the production model's strong DGW/TGW boost.
    return round(clamp(avg * math.sqrt(len(scores)), 0, 100), 1)


# ---------------------------------------------------------------------
# BUILD PLAYER-GW SAMPLES
# ---------------------------------------------------------------------

def make_gw_schedule(fantasy_games: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    out = defaultdict(list)
    for raw in fantasy_games:
        info = fantasy_game_info(raw)
        if not info or not info["gw"].isdigit():
            continue
        out[int(info["gw"])].append(info)
    for gw in out:
        out[gw].sort(key=lambda x: x["date"])
    return out


def player_target_gw_points(records: list[dict[str, Any]]) -> dict[int, float]:
    out = defaultdict(float)
    for r in records:
        if str(r["gw"]).isdigit():
            out[int(r["gw"])] += r["points"]
    return dict(out)


def build_samples(
    fantasy_players: list[dict[str, Any]],
    fantasy_games: list[dict[str, Any]],
    team_history: dict[str, list[dict[str, Any]]],
    asa_regular: list[dict[str, Any]],
    asa_team_map: dict[Any, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gw_schedule = make_gw_schedule(fantasy_games)
    completed_gws = sorted({
        gw for gw, games in gw_schedule.items()
        if games and any(g["has_started"] is True for g in games)
    })

    # Cache historical ASA strength once per GW.
    strength_cache = {}
    for gw in completed_gws:
        games = gw_schedule[gw]
        started = [g for g in games if g["has_started"] is True]
        if not started:
            continue
        cutoff = min(g["date"] for g in started)
        built = build_strength_as_of(asa_regular, asa_team_map, cutoff)
        if built:
            strength_cache[gw] = built

    samples = []
    status = Counter()
    unmatched_names = Counter()

    for p in fantasy_players:
        name = f"{p.get('firstName','')} {p.get('lastName','')}".strip()
        team = norm_team(
            (p.get("club") or {}).get("id")
            or (p.get("club") or {}).get("shortName")
            or (p.get("club") or {}).get("name")
        )
        pos = POSITION_MAP.get(norm_text(p.get("position")), norm_text(p.get("position")))
        records = fantasy_player_records(p)
        target_points = player_target_gw_points(records)

        for gw in completed_gws:
            if gw not in target_points:
                # We evaluate player-GWs where the player actually has a Fantasy
                # performance record. Recent role still distinguishes starters/subs.
                continue

            target_games = [
                g for g in gw_schedule[gw]
                if g["has_started"] is True
                and team in {g["home"], g["away"]}
            ]
            if not target_games:
                status["no_target_team_fixture"] += 1
                continue

            target_start = min(g["date"] for g in target_games)

            recent = recent_features(name, team, target_start, team_history)
            if recent is None:
                status["not_four_prior_team_matches"] += 1
                continue
            if not recent["ever_matched"]:
                unmatched_names[f"{name}|{team}"] += 1
                status["recent_name_unmatched"] += 1
                continue

            if gw not in strength_cache:
                status["no_historical_asa_strength"] += 1
                continue

            strength, ctx = strength_cache[gw]
            fixture_scores = []
            for fg in target_games:
                if fg["home"] == team:
                    opp, is_home = fg["away"], True
                else:
                    opp, is_home = fg["home"], False
                fixture_scores.append(
                    single_fixture_rating(pos, team, opp, is_home, strength, ctx)
                )
            fixture = combine_fixture_scores(fixture_scores)

            form = historical_form(records, target_start, gw)

            sample = {
                "name": name,
                "club": team,
                "position": pos,
                "gw": gw,
                "target_start": target_start.isoformat(),
                "target_points": target_points[gw],
                "fixture_count": len(target_games),
                "form_raw": form,
                "fixture_raw": fixture,
                "recent_appearances": recent["appearances"],
                "recent_minutes": recent["minutes"],
                "minutes_per_team_match": recent["minutes_per_team_match"],
                "appearance_rate": recent["appearance_rate"],
                "sixty_plus_rate": recent["sixty_plus_rate"],
                "seventyfive_plus_rate": recent["seventyfive_plus_rate"],
            }

            for k, v in recent["ptm"].items():
                sample[f"ptm_{k}"] = v

            samples.append(sample)
            status["sample"] += 1

    return samples, {
        "status_counts": dict(status),
        "unmatched_recent_names": unmatched_names.most_common(50),
        "asa_strength_gws": sorted(strength_cache),
    }


# ---------------------------------------------------------------------
# SAME-GW POSITION CALIBRATION + RECENT SIGNALS
# ---------------------------------------------------------------------

def add_same_gw_percentiles(samples: list[dict[str, Any]]) -> None:
    groups = defaultdict(list)
    for s in samples:
        groups[(s["gw"], s["position"])].append(s)

    activity_features = sorted({
        f"ptm_{feature}"
        for poscomps in COMPOSITES.values()
        for weights in poscomps.values()
        for feature in weights
    })

    for group in groups.values():
        fields = ["form_raw", "fixture_raw", "minutes_per_team_match"] + activity_features
        for field in fields:
            pop = [safe_float(s.get(field)) or 0.0 for s in group]
            for s, value in zip(group, pop):
                s[f"pct_{field}"] = percentile(value, pop)

        # WSL-style comparison calibration for Form and Fixture.
        for s in group:
            s["form_cmp"] = (
                0.70 * s["pct_form_raw"] + 0.30 * s["form_raw"]
            )
            s["fixture_cmp"] = (
                0.70 * s["pct_fixture_raw"] + 0.30 * s["fixture_raw"]
            )

    # Activity composite and role-aware opportunity signal.
    for s in samples:
        pos = s["position"]
        comps = COMPOSITES.get(pos, {})
        if not comps:
            continue

        # There is one primary candidate per position in this v2 audit.
        cname, weights = next(iter(comps.items()))
        activity = 0.0
        den = 0.0
        for feature, weight in weights.items():
            activity += weight * (safe_float(s.get(f"pct_ptm_{feature}")) or 50.0)
            den += weight
        activity = activity / den if den else 50.0

        role = safe_float(s.get("pct_minutes_per_team_match")) or 50.0
        role_weight = ROLE_BLEND.get(pos, 0.0)

        s["recent_activity"] = activity
        s["recent_role"] = role
        s["recent_opportunity"] = (
            (1.0 - role_weight) * activity + role_weight * role
        )
        s["recent_composite_name"] = cname
        s["recent_role_weight"] = role_weight

    # GK: activity is retained for audit, but opportunity is not used in the
    # recommended 3-way grid unless it proves itself.
    for s in samples:
        if s["position"] == "GK":
            s["recent_opportunity"] = s.get("recent_activity", 50.0)


# ---------------------------------------------------------------------
# EVALUATION
# ---------------------------------------------------------------------

def metric_result(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    pairs = [
        (safe_float(r.get(field)), safe_float(r.get("target_points")))
        for r in rows
    ]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    rho = spearman(xs, ys)

    top_avg = rest_avg = None
    if len(xs) >= 8:
        order = sorted(range(len(xs)), key=lambda i: xs[i], reverse=True)
        n = max(1, len(xs)//4)
        top = set(order[:n])
        a = [ys[i] for i in range(len(ys)) if i in top]
        b = [ys[i] for i in range(len(ys)) if i not in top]
        top_avg = statistics.fmean(a) if a else None
        rest_avg = statistics.fmean(b) if b else None

    return {
        "n": len(pairs),
        "rho": round(rho, 4) if rho is not None else None,
        "top_quartile_avg_points": round(top_avg, 3) if top_avg is not None else None,
        "rest_avg_points": round(rest_avg, 3) if rest_avg is not None else None,
        "top_quartile_lift": (
            round(top_avg-rest_avg, 3)
            if top_avg is not None and rest_avg is not None
            else None
        ),
    }


def add_weighted_score(
    rows: list[dict[str, Any]],
    out_field: str,
    fields: list[str],
    weights: list[float],
) -> None:
    for r in rows:
        r[out_field] = sum(
            (safe_float(r.get(f)) or 0.0) * w
            for f, w in zip(fields, weights)
        )


def grid_weights(n: int, step: float = 0.10) -> list[tuple[float, ...]]:
    units = round(1.0 / step)
    out = []
    if n == 2:
        for a in range(units+1):
            b = units-a
            out.append((a/units, b/units))
    elif n == 3:
        for a in range(units+1):
            for b in range(units+1-a):
                c = units-a-b
                out.append((a/units, b/units, c/units))
    return out


def optimize_blend(
    train: list[dict[str, Any]],
    holdout: list[dict[str, Any]],
    fields: list[str],
    label: str,
) -> dict[str, Any]:
    best = None
    for weights in grid_weights(len(fields), 0.10):
        vals = [
            sum((safe_float(r.get(f)) or 0.0)*w for f, w in zip(fields, weights))
            for r in train
        ]
        ys = [safe_float(r.get("target_points")) or 0.0 for r in train]
        rho = spearman(vals, ys)
        if rho is None:
            continue
        if best is None or rho > best["train_rho"]:
            best = {
                "weights": weights,
                "train_rho": rho,
            }

    if best is None:
        return {"label": label, "fields": fields, "weights": None}

    weights = best["weights"]
    train_vals = [
        sum((safe_float(r.get(f)) or 0.0)*w for f, w in zip(fields, weights))
        for r in train
    ]
    train_y = [safe_float(r.get("target_points")) or 0.0 for r in train]

    hold_vals = [
        sum((safe_float(r.get(f)) or 0.0)*w for f, w in zip(fields, weights))
        for r in holdout
    ]
    hold_y = [safe_float(r.get("target_points")) or 0.0 for r in holdout]

    return {
        "label": label,
        "fields": fields,
        "weights": {
            field: round(weight, 2)
            for field, weight in zip(fields, weights)
        },
        "train_rho": round(spearman(train_vals, train_y), 4),
        "holdout_rho": (
            round(spearman(hold_vals, hold_y), 4)
            if len(holdout) >= 3
            else None
        ),
        "train_n": len(train),
        "holdout_n": len(holdout),
    }


def evaluate_position(rows: list[dict[str, Any]], pos: str) -> dict[str, Any]:
    pr = [r for r in rows if r["position"] == pos]
    gws = sorted({r["gw"] for r in pr})
    split_idx = max(1, int(len(gws)*0.70))
    train_gws = set(gws[:split_idx])
    hold_gws = set(gws[split_idx:])
    train = [r for r in pr if r["gw"] in train_gws]
    hold = [r for r in pr if r["gw"] in hold_gws]

    # Benchmarks: raw and WSL-style comparison-calibrated.
    singles = {
        "form_raw": metric_result(pr, "form_raw"),
        "form_cmp": metric_result(pr, "form_cmp"),
        "fixture_raw": metric_result(pr, "fixture_raw"),
        "fixture_cmp": metric_result(pr, "fixture_cmp"),
        "recent_activity": metric_result(pr, "recent_activity"),
        "recent_role": metric_result(pr, "recent_role"),
        "recent_opportunity": metric_result(pr, "recent_opportunity"),
    }

    # Current live formula benchmark on RAW Form + Fixture.
    live = LIVE_DECISION_WEIGHTS.get(pos, {"form": 0.5, "fixture": 0.5})
    for r in pr:
        r["live_formula_recreated"] = (
            live["form"]*r["form_raw"] + live["fixture"]*r["fixture_raw"]
        )
    singles["live_formula_recreated"] = metric_result(pr, "live_formula_recreated")

    # Optimize on EARLY 70%, evaluate exact chosen weights on late 30%.
    blends = []

    for calibrated in (False, True):
        f = "form_cmp" if calibrated else "form_raw"
        x = "fixture_cmp" if calibrated else "fixture_raw"
        suffix = "comparison" if calibrated else "raw"

        blends.append(optimize_blend(
            train, hold,
            [f, "recent_opportunity"],
            f"Form + Recent Opportunity ({suffix})",
        ))
        blends.append(optimize_blend(
            train, hold,
            [f, x],
            f"Form + Fixture ({suffix})",
        ))
        blends.append(optimize_blend(
            train, hold,
            ["recent_opportunity", x],
            f"Recent Opportunity + Fixture ({suffix})",
        ))
        blends.append(optimize_blend(
            train, hold,
            [f, "recent_opportunity", x],
            f"Form + Recent Opportunity + Fixture ({suffix})",
        ))

    # For GK, also explicitly optimize Form+Fixture without forcing recent layer.
    return {
        "samples": len(pr),
        "gws": gws,
        "train_gws": sorted(train_gws),
        "holdout_gws": sorted(hold_gws),
        "train_n": len(train),
        "holdout_n": len(hold),
        "single_signals": singles,
        "optimized_blends": blends,
    }


def print_results(results: dict[str, Any]) -> None:
    print()
    print("=== DECISION ARCHITECTURE BACKTEST V2 ===")
    for pos in ("GK", "DEF", "MID", "FOR"):
        r = results[pos]
        print()
        print(
            f"{pos}: {r['samples']} player-GW samples | "
            f"train GWs {r['train_gws'][0]}-{r['train_gws'][-1]} | "
            f"holdout GWs {r['holdout_gws'][0]}-{r['holdout_gws'][-1]}"
        )
        print("  Single signals (all samples):")
        for key in (
            "form_raw", "form_cmp", "recent_activity", "recent_role",
            "recent_opportunity", "fixture_raw", "fixture_cmp",
            "live_formula_recreated",
        ):
            m = r["single_signals"][key]
            print(
                f"    {key}: rho={m['rho']} "
                f"topQ lift={m['top_quartile_lift']}"
            )

        print("  Train-optimized blends -> late-GW holdout:")
        for b in r["optimized_blends"]:
            print(
                f"    {b['label']}: weights={b['weights']} | "
                f"train={b.get('train_rho')} holdout={b.get('holdout_rho')}"
            )
    print()
    print("=== END DECISION ARCHITECTURE BACKTEST V2 ===")


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main() -> int:
    c = Client()

    print("=== NWSL DECISION ARCHITECTURE BACKTEST V2 ===")
    print("Target = next GAMEWEEK fantasy points.")
    print(
        "Recent activity = previous 4 TEAM matches, per-team-match, "
        "so super subs are naturally penalized."
    )
    print("DGW boost = existing avg * sqrt(fixture_count), preserved.")
    print("No production model files are changed.")
    print()

    fantasy = c.fantasy()
    fgames = fantasy.get("games", []) or []
    fplayers = fantasy.get("players", []) or []
    print(f"Fantasy API: {len(fgames)} games, {len(fplayers)} players.")

    fotmob, fot_errors = fetch_fotmob_matches(c)
    print(
        f"FotMob: {len(fotmob)} completed 2026 NWSL matches; "
        f"{len(fot_errors)} fetch errors."
    )

    fmap, map_diag = map_fantasy_to_fotmob(fgames, fotmob)
    print()
    print("=== FANTASY -> FOTMOB MAPPING ===")
    print(
        f"Completed fantasy games: {map_diag['completed_fantasy_games']} | "
        f"mapped: {map_diag['mapped']} | "
        f"unresolved: {len(map_diag['unresolved'])}"
    )
    print(f"Mapping methods: {map_diag['methods']}")
    if map_diag["unresolved"]:
        print("Unresolved examples:")
        for row in map_diag["unresolved"][:15]:
            print(
                f"  GW{row['gw']} {row['date']} "
                f"{row['home']} vs {row['away']} score={row['score']}"
            )
    print("=== END MAPPING ===")

    # The model itself uses FotMob chronology/team histories, so fmap is a
    # mapping-quality audit rather than a hard dependency for every sample.
    team_history = build_team_history(fotmob)

    asa_games, asa_team_map = fetch_asa(c)
    asa_regular = filter_asa_to_regular_season(
        asa_games, asa_team_map, fotmob
    )
    print(
        f"ASA: {len(asa_games)} 2026 records; "
        f"{len(asa_regular)} matched to regular-season FotMob games."
    )

    samples, sample_diag = build_samples(
        fplayers, fgames, team_history, asa_regular, asa_team_map
    )
    print(f"Historical player-GW samples built: {len(samples)}")

    add_same_gw_percentiles(samples)

    results = {
        pos: evaluate_position(samples, pos)
        for pos in ("GK", "DEF", "MID", "FOR")
    }
    print_results(results)

    # Useful current-player sanity checks: latest historical target sample.
    latest = {}
    for s in sorted(samples, key=lambda x: (x["gw"], x["target_start"])):
        latest[norm_player(s["name"])] = s

    spot_checks = {}
    for name in (
        "Ashley Sanchez",
        "Samantha Kerr",
        "Sam Kerr",
        "Jordynn Dudley",
        "Jessie Fleming",
    ):
        row = latest.get(norm_player(name))
        if row:
            spot_checks[name] = {
                k: v for k, v in row.items()
                if k in {
                    "name", "club", "position", "gw", "target_points",
                    "fixture_count", "form_raw", "form_cmp", "fixture_raw",
                    "fixture_cmp", "recent_minutes", "minutes_per_team_match",
                    "recent_activity", "recent_role", "recent_opportunity",
                }
                or k.startswith("ptm_")
            }

    payload = {
        "metadata": {
            "version": "nwsl-decision-architecture-backtest-v2",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "production_changed": False,
            "target": "next fantasy gameweek points",
            "recent_window": "previous 4 team matches",
            "super_sub_rule": (
                "Activity is per team match. Missed matches and cameo minutes "
                "therefore reduce the signal. A separate recent-role percentile "
                "is also blended into Recent Opportunity."
            ),
            "dgw_rule": "average fixture rating * sqrt(fixture count), capped 100",
            "comparison_calibration": (
                "WSL-style test: 70% same-GW position percentile + 30% raw score"
            ),
            "role_blend": ROLE_BLEND,
            "fotmob_completed_matches": len(fotmob),
            "fotmob_fetch_errors": len(fot_errors),
            "fotmob_requests": c.fotmob_requests,
            "asa_2026_records": len(asa_games),
            "asa_regular_season_records": len(asa_regular),
            "player_gw_samples": len(samples),
        },
        "mapping": map_diag,
        "sample_diagnostics": sample_diag,
        "candidate_recent_composites": COMPOSITES,
        "results": results,
        "spot_checks": spot_checks,
        "fotmob_fetch_errors": fot_errors,
    }

    OUTPUT_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # CSV for manual inspection.
    if samples:
        fields = sorted({k for s in samples for k in s})
        preferred = [
            "name", "club", "position", "gw", "target_points",
            "fixture_count", "form_raw", "form_cmp",
            "recent_minutes", "minutes_per_team_match",
            "recent_activity", "recent_role", "recent_opportunity",
            "fixture_raw", "fixture_cmp",
        ]
        columns = preferred + [f for f in fields if f not in preferred]
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            for s in samples:
                w.writerow(s)

    print()
    print(f"Saved: {OUTPUT_JSON}")
    print(f"Saved: {OUTPUT_CSV}")
    print("=== END NWSL DECISION ARCHITECTURE BACKTEST V2 ===")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise
