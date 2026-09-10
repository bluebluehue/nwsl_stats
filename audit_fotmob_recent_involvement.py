#!/usr/bin/env python3
"""
Standalone FotMob NWSL recent-involvement audit.

PURPOSE
-------
Prove that FotMob can supply match-by-match player involvement for the 2026
NWSL season before we connect anything to the production fantasy model.

This script:
  1. Fetches a known NWSL matchDetails payload.
  2. Tries to discover all 2026 NWSL match IDs from FotMob's league endpoint.
  3. Falls back to recursively crawling matchDetails -> teamForm links if needed.
  4. Extracts match-level player stats and stable FotMob + Opta IDs.
  5. Builds each player's last N appearances (default: 4).
  6. Writes a transparent audit JSON. It DOES NOT calculate or change Decision,
     Form, Fixture, or any other production fantasy rating.

Output:
  fotmob_recent_involvement_audit.json

Example:
  python audit_fotmob_recent_involvement.py

Optional:
  python audit_fotmob_recent_involvement.py --max-matches 25
  python audit_fotmob_recent_involvement.py --last-appearances 4
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


BASE_URLS = (
    "https://www.fotmob.com/api/data",
    "https://www.fotmob.com/api",
)

NWSL_PARENT_LEAGUE_ID = 9134
NWSL_SEASON = "2026"
SEED_MATCH_ID = 5161617  # Gotham 1-1 Portland, Aug 28 2026 ET / Aug 29 UTC
OUTPUT_PATH = Path("fotmob_recent_involvement_audit.json")

REQUEST_DELAY_SECONDS = 0.18
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.fotmob.com/",
}

KEY_STATS = (
    "minutes_played",
    "rating_title",
    "goals",
    "assists",
    "expected_goals",
    "expected_goals_non_penalty",
    "expected_goals_on_target_variant",
    "expected_assists",
    "xg_and_xa",
    "total_shots",
    "ShotsOnTarget",
    "ShotsOffTarget",
    "blocked_shots",
    "chances_created",
    "big_chance_created_team_title",
    "big_chance_missed_title",
    "touches",
    "touches_opp_box",
    "dribbles_succeeded",
    "accurate_crosses",
    "passes_into_final_third",
    "defensive_actions",
    "matchstats.headers.tackles",
    "interceptions",
    "recoveries",
    "clearances",
    "shot_blocks",
    "ground_duels_won",
    "aerials_won",
    "duel_won",
    "duel_lost",
    "saves",
    "goals_conceded",
    "expected_goals_on_target_faced",
    "goals_prevented",
    "saves_inside_box",
)

ZERO_IF_MISSING_FOR_APPEARANCE = {
    "goals",
    "assists",
    "expected_goals",
    "expected_goals_non_penalty",
    "expected_goals_on_target_variant",
    "expected_assists",
    "xg_and_xa",
    "total_shots",
    "ShotsOnTarget",
    "ShotsOffTarget",
    "blocked_shots",
    "chances_created",
    "big_chance_created_team_title",
    "big_chance_missed_title",
    "touches_opp_box",
    "dribbles_succeeded",
    "accurate_crosses",
    "passes_into_final_third",
    "defensive_actions",
    "matchstats.headers.tackles",
    "interceptions",
    "recoveries",
    "clearances",
    "shot_blocks",
    "ground_duels_won",
    "aerials_won",
    "duel_won",
    "duel_lost",
    "saves",
    "goals_conceded",
    "saves_inside_box",
}

DISPLAY_NAMES = {
    "rating_title": "FotMob rating",
    "minutes_played": "Minutes",
    "expected_goals": "xG",
    "expected_goals_non_penalty": "npxG",
    "expected_goals_on_target_variant": "xGOT",
    "expected_assists": "xA",
    "xg_and_xa": "xG+xA",
    "ShotsOnTarget": "Shots on target",
    "ShotsOffTarget": "Shots off target",
    "touches_opp_box": "Opposition-box touches",
    "dribbles_succeeded": "Successful dribbles",
    "accurate_crosses": "Accurate crosses",
    "passes_into_final_third": "Passes into final third",
    "matchstats.headers.tackles": "Tackles",
    "shot_blocks": "Blocks",
    "expected_goals_on_target_faced": "xGOT faced",
    "goals_prevented": "Goals prevented",
}

SPOT_CHECK_PLAYERS = (
    "Samantha Kerr",
    "Jordynn Dudley",
    "Guro Reiten",
    "Jessie Fleming",
    "Ann-Katrin Berger",
    "Ashley Sanchez",
)


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FotMobClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.requests_made = 0

    def get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None

        for base in BASE_URLS:
            url = f"{base}/{path.lstrip('/')}"
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = self.session.get(
                        url,
                        params=params,
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    self.requests_made += 1

                    if response.status_code == 200:
                        time.sleep(REQUEST_DELAY_SECONDS)
                        return response.json()

                    if response.status_code in (403, 404):
                        # Try alternate /api/data vs /api route.
                        break

                    if response.status_code == 429 or response.status_code >= 500:
                        wait = min(8.0, 1.25 * (2 ** (attempt - 1)))
                        print(
                            f"  FotMob {response.status_code}; "
                            f"retrying in {wait:.1f}s..."
                        )
                        time.sleep(wait)
                        continue

                    response.raise_for_status()

                except (requests.RequestException, ValueError) as exc:
                    last_error = exc
                    if attempt < MAX_RETRIES:
                        time.sleep(min(8.0, 1.25 * (2 ** (attempt - 1))))
                    else:
                        break

        if last_error:
            raise RuntimeError(
                f"Unable to fetch FotMob path={path!r}, params={params}: {last_error}"
            )
        raise RuntimeError(
            f"Unable to fetch FotMob path={path!r}, params={params}"
        )

    def match_details(self, match_id: int | str) -> dict[str, Any]:
        return self.get_json("matchDetails", {"matchId": str(match_id)})

    def league(self) -> dict[str, Any]:
        return self.get_json(
            "leagues",
            {
                "id": NWSL_PARENT_LEAGUE_ID,
                "ccode3": "USA",
                "season": NWSL_SEASON,
            },
        )


def extract_match_id_from_link(link: Any) -> int | None:
    if not isinstance(link, str):
        return None
    # FotMob match links normally end with #5161617.
    match = re.search(r"#(\d+)(?:$|[?&])", link)
    if match:
        return int(match.group(1))
    return None


def recursively_find_match_ids(obj: Any) -> set[int]:
    """
    Generic league-payload parser.

    FotMob has changed league response shapes over time, so this deliberately
    searches recursively for objects that resemble fixtures/matches instead
    of relying on one brittle JSON path.
    """
    found: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            # Common match object shape: id + home/away + status/date.
            candidate_id = value.get("id")
            has_match_shape = (
                ("home" in value and "away" in value)
                or ("homeTeam" in value and "awayTeam" in value)
                or ("pageUrl" in value and "status" in value)
            )
            if has_match_shape:
                try:
                    if candidate_id is not None:
                        found.add(int(candidate_id))
                except (TypeError, ValueError):
                    pass

            for link_key in ("linkToMatch", "pageUrl", "matchUrl"):
                link_id = extract_match_id_from_link(value.get(link_key))
                if link_id:
                    found.add(link_id)

            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return found


def team_form_match_ids(details: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    team_form = (
        details.get("content", {})
        .get("matchFacts", {})
        .get("teamForm", [])
    )
    for side in team_form if isinstance(team_form, list) else []:
        for match in side if isinstance(side, list) else []:
            match_id = extract_match_id_from_link(match.get("linkToMatch"))
            if match_id:
                ids.add(match_id)
    return ids


def is_target_nwsl_2026(details: dict[str, Any]) -> bool:
    general = details.get("general", {})
    if not general:
        return False

    parent_league = general.get("parentLeagueId")
    league_name = str(general.get("leagueName") or "").strip().upper()
    date = str(
        general.get("matchTimeUTCDate")
        or general.get("matchTimeUTC")
        or ""
    )

    league_ok = (
        str(parent_league) == str(NWSL_PARENT_LEAGUE_ID)
        or league_name == "NWSL"
    )
    season_ok = date.startswith("2026-")
    return league_ok and season_ok


def match_is_finished(details: dict[str, Any]) -> bool:
    general = details.get("general", {})
    if general.get("finished") is True:
        return True
    status = details.get("header", {}).get("status", {})
    return status.get("finished") is True


def flatten_player_stats(player_obj: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten FotMob's grouped player stat cards using stable stat keys.
    Preserve fraction numerators and totals separately.
    """
    flat: dict[str, Any] = {}
    titles: dict[str, str] = {}

    for section in player_obj.get("stats", []) or []:
        stats = section.get("stats", {})
        if not isinstance(stats, dict):
            continue

        for title, descriptor in stats.items():
            if not isinstance(descriptor, dict):
                continue

            key = descriptor.get("key")
            stat = descriptor.get("stat") or {}
            if not key or not isinstance(stat, dict):
                continue

            titles[key] = title

            if "value" in stat:
                flat[key] = stat.get("value")
            elif key not in flat:
                flat[key] = None

            if "total" in stat:
                flat[f"{key}__total"] = stat.get("total")

    return {
        "values": flat,
        "titles": titles,
    }


def shotmap_summary(player_obj: dict[str, Any]) -> dict[str, Any]:
    shots = player_obj.get("shotmap", []) or []
    if not isinstance(shots, list):
        shots = []

    total_xg = 0.0
    total_xgot = 0.0
    open_play_xg = 0.0
    set_piece_xg = 0.0
    penalty_xg = 0.0
    shots_inside_box = 0
    shots_on_target = 0
    goals = 0

    for shot in shots:
        xg = safe_float(shot.get("expectedGoals")) or 0.0
        xgot = safe_float(shot.get("expectedGoalsOnTarget")) or 0.0
        situation = str(shot.get("situation") or "")
        total_xg += xg
        total_xgot += xgot

        if shot.get("isFromInsideBox"):
            shots_inside_box += 1
        if shot.get("isOnTarget") and not shot.get("isBlocked"):
            shots_on_target += 1
        if shot.get("eventType") == "Goal":
            goals += 1

        if situation == "Penalty":
            penalty_xg += xg
        elif situation in {"SetPiece", "FromCorner", "FreeKick"}:
            set_piece_xg += xg
        else:
            open_play_xg += xg

    return {
        "shot_count": len(shots),
        "shotmap_xg": round(total_xg, 4),
        "shotmap_xgot": round(total_xgot, 4),
        "open_play_xg": round(open_play_xg, 4),
        "set_piece_xg": round(set_piece_xg, 4),
        "penalty_xg": round(penalty_xg, 4),
        "shots_inside_box": shots_inside_box,
        "shots_on_target_from_shotmap": shots_on_target,
        "goals_from_shotmap": goals,
    }


def parse_match(details: dict[str, Any]) -> dict[str, Any]:
    general = details.get("general", {})
    home = general.get("homeTeam", {}) or {}
    away = general.get("awayTeam", {}) or {}
    player_stats = details.get("content", {}).get("playerStats", {}) or {}

    appearances = []
    bench_or_unused = []

    for player_id_key, player_obj in player_stats.items():
        if not isinstance(player_obj, dict):
            continue

        flattened = flatten_player_stats(player_obj)
        values = flattened["values"]
        minutes = safe_float(values.get("minutes_played"))

        player_record = {
            "fotmob_player_id": player_obj.get("id") or player_id_key,
            "opta_id": player_obj.get("optaId"),
            "name": player_obj.get("name"),
            "team_id": player_obj.get("teamId"),
            "team_name": player_obj.get("teamName"),
            "is_goalkeeper": bool(player_obj.get("isGoalkeeper")),
            "usual_position": player_obj.get("usualPosition"),
            "position_id": player_obj.get("positionId"),
            "shirt_number": player_obj.get("shirtNumber"),
            "stats": values,
            "stat_titles": flattened["titles"],
            "shotmap_summary": shotmap_summary(player_obj),
        }

        if player_obj.get("stats") and minutes is not None and minutes > 0:
            appearances.append(player_record)
        else:
            bench_or_unused.append(
                {
                    "fotmob_player_id": player_record["fotmob_player_id"],
                    "opta_id": player_record["opta_id"],
                    "name": player_record["name"],
                    "team_id": player_record["team_id"],
                    "team_name": player_record["team_name"],
                    "is_goalkeeper": player_record["is_goalkeeper"],
                }
            )

    return {
        "match_id": int(general.get("matchId")),
        "date_utc": general.get("matchTimeUTCDate") or general.get("matchTimeUTC"),
        "match_name": general.get("matchName"),
        "league_name": general.get("leagueName"),
        "parent_league_id": general.get("parentLeagueId"),
        "home_team": {
            "id": home.get("id"),
            "name": home.get("name"),
        },
        "away_team": {
            "id": away.get("id"),
            "name": away.get("name"),
        },
        "finished": match_is_finished(details),
        "appearances": appearances,
        "bench_or_unused": bench_or_unused,
    }


def date_sort_key(value: Any) -> str:
    return str(value or "")


def appearance_with_match_context(
    parsed_match: dict[str, Any],
    appearance: dict[str, Any],
) -> dict[str, Any]:
    team_id = str(appearance.get("team_id"))
    home_id = str(parsed_match["home_team"].get("id"))
    away_id = str(parsed_match["away_team"].get("id"))

    if team_id == home_id:
        opponent = parsed_match["away_team"]
        venue = "H"
    elif team_id == away_id:
        opponent = parsed_match["home_team"]
        venue = "A"
    else:
        opponent = {"id": None, "name": None}
        venue = "?"

    return {
        "match_id": parsed_match["match_id"],
        "date_utc": parsed_match["date_utc"],
        "opponent": opponent.get("name"),
        "opponent_id": opponent.get("id"),
        "venue": venue,
        "team_name": appearance.get("team_name"),
        "minutes": appearance.get("stats", {}).get("minutes_played"),
        "stats": appearance.get("stats", {}),
        "shotmap_summary": appearance.get("shotmap_summary", {}),
    }


def summarize_recent_appearances(
    appearances: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Summarize the recent appearance window.

    Important FotMob schema rule:
    many event/counting stats are omitted entirely when the player recorded zero.
    For a player who DID appear, omission of those known counting/activity stats
    therefore means 0, not "unknown". We zero-fill those fields here.

    Metrics where absence can genuinely mean unavailable (for example FotMob
    rating or xGOT-faced) remain nullable.
    """
    totals: dict[str, float] = defaultdict(float)
    available_counts: Counter[str] = Counter()
    explicit_counts: Counter[str] = Counter()
    total_minutes = 0.0

    for app in appearances:
        stats = app.get("stats", {})
        minutes = safe_float(stats.get("minutes_played")) or 0.0
        total_minutes += minutes

        for key in KEY_STATS:
            if key == "minutes_played":
                continue

            raw = stats.get(key)
            value = safe_float(raw)

            if value is None and key in ZERO_IF_MISSING_FOR_APPEARANCE:
                value = 0.0
                available_counts[key] += 1
            elif value is not None:
                available_counts[key] += 1
                explicit_counts[key] += 1

            if value is not None:
                totals[key] += value

    per90: dict[str, float] = {}
    if total_minutes > 0:
        for key, total in totals.items():
            if key in {
                "rating_title",
                "goals_conceded",
                "goals_prevented",
                "expected_goals_on_target_faced",
            }:
                continue
            per90[key] = round(total * 90.0 / total_minutes, 3)

    averages: dict[str, float] = {}
    for key, count in available_counts.items():
        if count:
            averages[key] = round(totals[key] / count, 3)

    return {
        "appearances": len(appearances),
        "minutes": round(total_minutes, 1),
        "totals": {k: round(v, 3) for k, v in totals.items()},
        "averages_when_available": averages,
        "per90_over_minutes": per90,
        "stat_availability_counts": dict(available_counts),
        "stat_explicit_presence_counts": dict(explicit_counts),
        "zero_fill_rule": (
            "For known event/counting stats, a missing FotMob key for a player "
            "who appeared is treated as zero activity."
        ),
    }

def build_player_histories(
    matches: list[dict[str, Any]],
    last_n: int,
) -> dict[str, Any]:
    by_player: dict[str, dict[str, Any]] = {}

    for match in matches:
        for app in match["appearances"]:
            player_id = str(app["fotmob_player_id"])
            record = by_player.setdefault(
                player_id,
                {
                    "fotmob_player_id": app["fotmob_player_id"],
                    "opta_id": app.get("opta_id"),
                    "name": app.get("name"),
                    "is_goalkeeper": app.get("is_goalkeeper"),
                    "usual_position": app.get("usual_position"),
                    "all_appearances": [],
                },
            )
            # Preserve an Opta ID if one appears later.
            if not record.get("opta_id") and app.get("opta_id"):
                record["opta_id"] = app.get("opta_id")

            record["all_appearances"].append(
                appearance_with_match_context(match, app)
            )

    for record in by_player.values():
        record["all_appearances"].sort(
            key=lambda x: date_sort_key(x.get("date_utc")),
            reverse=True,
        )
        recent = record["all_appearances"][:last_n]
        record["recent_appearances"] = recent
        record["recent_summary"] = summarize_recent_appearances(recent)
        record["season_appearances_found"] = len(record["all_appearances"])

        # Keep output manageable: we only need the last N match rows for this audit.
        del record["all_appearances"]

    return by_player


def build_stat_inventory(matches: list[dict[str, Any]]) -> dict[str, Any]:
    player_appearance_count = 0
    key_counts: Counter[str] = Counter()
    title_by_key: dict[str, str] = {}
    positions_by_key: dict[str, Counter[str]] = defaultdict(Counter)

    for match in matches:
        for app in match["appearances"]:
            player_appearance_count += 1
            pos = "GK" if app.get("is_goalkeeper") else str(
                app.get("usual_position")
            )
            for key, value in app.get("stats", {}).items():
                if key.endswith("__total"):
                    continue
                if value is not None:
                    key_counts[key] += 1
                    positions_by_key[key][pos] += 1
            title_by_key.update(app.get("stat_titles", {}))

    rows = []
    for key, count in key_counts.most_common():
        rows.append(
            {
                "key": key,
                "title": title_by_key.get(key) or DISPLAY_NAMES.get(key) or key,
                "appearances_with_stat": count,
                "coverage_pct_of_player_appearances": (
                    round(100.0 * count / player_appearance_count, 1)
                    if player_appearance_count
                    else 0.0
                ),
                "position_presence": dict(positions_by_key[key]),
            }
        )

    return {
        "player_appearances": player_appearance_count,
        "distinct_stat_keys": len(key_counts),
        "stats": rows,
    }


def discover_match_ids(
    client: FotMobClient,
    max_matches: int | None,
) -> tuple[list[int], dict[str, Any], dict[int, dict[str, Any]]]:
    """
    Discover matches, preferring the league endpoint.

    Returns:
      match_ids, discovery diagnostics, preloaded matchDetails cache
    """
    diagnostics: dict[str, Any] = {
        "league_endpoint_success": False,
        "league_ids_found": 0,
        "fallback_crawl_used": False,
    }
    preloaded: dict[int, dict[str, Any]] = {}

    discovered: set[int] = {SEED_MATCH_ID}

    try:
        league_payload = client.league()
        league_ids = recursively_find_match_ids(league_payload)
        diagnostics["league_endpoint_success"] = True
        diagnostics["league_ids_found"] = len(league_ids)
        discovered.update(league_ids)
        print(f"League endpoint exposed {len(league_ids)} candidate match IDs.")
    except Exception as exc:
        diagnostics["league_endpoint_error"] = str(exc)
        print(f"League endpoint discovery failed: {exc}")

    # If league discovery looks incomplete, recursively crawl teamForm links
    # starting from the known-good seed match.
    if len(discovered) < 100:
        diagnostics["fallback_crawl_used"] = True
        print(
            "League discovery looks incomplete; crawling historical teamForm "
            "links from the known match..."
        )
        queue: deque[int] = deque([SEED_MATCH_ID])
        crawled: set[int] = set()

        while queue:
            match_id = queue.popleft()
            if match_id in crawled:
                continue
            if max_matches is not None and len(crawled) >= max_matches:
                break

            try:
                details = client.match_details(match_id)
            except Exception as exc:
                diagnostics.setdefault("crawl_errors", []).append(
                    {"match_id": match_id, "error": str(exc)}
                )
                crawled.add(match_id)
                continue

            crawled.add(match_id)
            preloaded[match_id] = details

            if not is_target_nwsl_2026(details):
                continue

            linked = team_form_match_ids(details)
            for linked_id in linked:
                if linked_id not in crawled:
                    discovered.add(linked_id)
                    queue.append(linked_id)

            if len(crawled) % 20 == 0:
                print(
                    f"  Crawled {len(crawled)} matches; "
                    f"{len(discovered)} candidate IDs discovered..."
                )

        diagnostics["crawl_match_details_fetched"] = len(crawled)
        diagnostics["crawl_candidate_ids_found"] = len(discovered)

    ids = sorted(discovered)
    if max_matches is not None:
        # Always retain the seed, then cap remaining IDs.
        ids = [SEED_MATCH_ID] + [x for x in ids if x != SEED_MATCH_ID]
        ids = ids[:max_matches]

    return ids, diagnostics, preloaded


def run(max_matches: int | None, last_n: int) -> dict[str, Any]:
    client = FotMobClient()

    print("=== FOTMOB NWSL RECENT INVOLVEMENT AUDIT ===")
    print(f"Known seed match: {SEED_MATCH_ID}")
    print("This does NOT modify the fantasy Decision model.")
    print()

    # First prove the exact matchDetails payload still works.
    seed = client.match_details(SEED_MATCH_ID)
    if not is_target_nwsl_2026(seed):
        raise RuntimeError(
            "Known seed match did not return the expected 2026 NWSL payload."
        )

    seed_player_stats = seed.get("content", {}).get("playerStats", {}) or {}
    seed_player_count = len(seed_player_stats)
    seed_active_count = sum(
        1
        for p in seed_player_stats.values()
        if p.get("stats")
    )
    print(
        f"Seed payload OK: {seed_player_count} listed players; "
        f"{seed_active_count} players with match stats."
    )
    print(
        "Seed content keys: "
        + ", ".join(sorted((seed.get("content", {}) or {}).keys()))
    )

    match_ids, discovery, preloaded = discover_match_ids(
        client,
        max_matches=max_matches,
    )
    preloaded[SEED_MATCH_ID] = seed

    print(f"Candidate match IDs to inspect: {len(match_ids)}")
    print()

    parsed_matches: list[dict[str, Any]] = []
    rejected = []
    fetch_errors = []

    for idx, match_id in enumerate(match_ids, start=1):
        try:
            details = preloaded.get(match_id)
            if details is None:
                details = client.match_details(match_id)

            if not is_target_nwsl_2026(details):
                rejected.append(match_id)
                continue
            if not match_is_finished(details):
                rejected.append(match_id)
                continue

            parsed = parse_match(details)
            parsed_matches.append(parsed)

            if idx % 20 == 0 or idx == len(match_ids):
                print(
                    f"  Processed {idx}/{len(match_ids)} candidates; "
                    f"{len(parsed_matches)} completed 2026 NWSL matches retained."
                )

        except Exception as exc:
            fetch_errors.append(
                {
                    "match_id": match_id,
                    "error": str(exc),
                }
            )

    parsed_matches.sort(
        key=lambda x: date_sort_key(x.get("date_utc"))
    )

    histories = build_player_histories(parsed_matches, last_n=last_n)
    inventory = build_stat_inventory(parsed_matches)

    # Player ID bridge diagnostics.
    with_opta = sum(1 for p in histories.values() if p.get("opta_id"))
    without_opta = len(histories) - with_opta

    # Named spot checks for immediate human inspection.
    spot_checks = {}
    for wanted in SPOT_CHECK_PLAYERS:
        matches_for_name = [
            p for p in histories.values()
            if str(p.get("name") or "").casefold() == wanted.casefold()
        ]
        if matches_for_name:
            spot_checks[wanted] = matches_for_name[0]

    # Compact console output.
    print()
    print("=== AUDIT SUMMARY ===")
    print(f"Completed NWSL matches parsed: {len(parsed_matches)}")
    print(f"Unique appearing players: {len(histories)}")
    print(f"Players with Opta ID bridge: {with_opta}")
    print(f"Players without Opta ID bridge: {without_opta}")
    print(f"Distinct match-level stat keys: {inventory['distinct_stat_keys']}")
    print(f"Fetch errors: {len(fetch_errors)}")
    print()

    print("High-value raw-key presence:")
    print(
        "  NOTE: for known counting/activity stats, a missing key for an "
        "appearing player is interpreted as ZERO in recent summaries."
    )
    inventory_by_key = {
        row["key"]: row for row in inventory["stats"]
    }
    for key in KEY_STATS:
        row = inventory_by_key.get(key)
        if row:
            print(
                f"  {key}: {row['appearances_with_stat']} appearances "
                f"({row['coverage_pct_of_player_appearances']}%)"
            )

    print()
    print("Named player spot checks:")
    for name, player in spot_checks.items():
        summary = player.get("recent_summary", {})
        totals = summary.get("totals", {})
        print(
            f"  {name}: {summary.get('appearances')} recent apps, "
            f"{summary.get('minutes')} min | "
            f"xG={totals.get('expected_goals')} "
            f"xA={totals.get('expected_assists')} "
            f"SOT={totals.get('ShotsOnTarget')} "
            f"box touches={totals.get('touches_opp_box')} "
            f"chances={totals.get('chances_created')} "
            f"tackles={totals.get('matchstats.headers.tackles')} "
            f"recoveries={totals.get('recoveries')}"
        )

    payload = {
        "metadata": {
            "generated_at_utc": iso_now(),
            "version": "fotmob-nwsl-recent-involvement-audit-v1.1-zero-aware",
            "purpose": (
                "Validate match-by-match recent player involvement from FotMob "
                "before any production fantasy-model integration."
            ),
            "changes_production_model": False,
            "season": NWSL_SEASON,
            "parent_league_id": NWSL_PARENT_LEAGUE_ID,
            "seed_match_id": SEED_MATCH_ID,
            "last_appearances_window": last_n,
            "requests_made": client.requests_made,
            "max_matches_argument": max_matches,
            "discovery": discovery,
            "candidate_match_ids": len(match_ids),
            "completed_nwsl_matches_parsed": len(parsed_matches),
            "rejected_candidate_ids": len(rejected),
            "fetch_error_count": len(fetch_errors),
            "unique_appearing_players": len(histories),
            "players_with_opta_id": with_opta,
            "players_without_opta_id": without_opta,
        },
        "stat_inventory": inventory,
        "spot_checks": spot_checks,
        "players": histories,
        "matches": [
            {
                "match_id": m["match_id"],
                "date_utc": m["date_utc"],
                "match_name": m["match_name"],
                "home_team": m["home_team"],
                "away_team": m["away_team"],
                "player_appearances": len(m["appearances"]),
                "bench_or_unused": len(m["bench_or_unused"]),
            }
            for m in parsed_matches
        ],
        "fetch_errors": fetch_errors,
        "rejected_candidate_match_ids": rejected,
    }

    OUTPUT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print()
    print(f"Saved audit to {OUTPUT_PATH}")
    print("=== END FOTMOB NWSL RECENT INVOLVEMENT AUDIT ===")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit FotMob match-by-match NWSL player involvement."
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=None,
        help=(
            "Optional cap for testing. Omit to inspect all discovered completed "
            "2026 NWSL matches."
        ),
    )
    parser.add_argument(
        "--last-appearances",
        type=int,
        default=4,
        help="Recent player appearance window to preserve in the audit (default: 4).",
    )
    args = parser.parse_args()

    try:
        run(
            max_matches=args.max_matches,
            last_n=max(1, args.last_appearances),
        )
        return 0
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
