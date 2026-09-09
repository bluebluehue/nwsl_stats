from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import requests


BASE_URL = "https://api-sdp.nwslsoccer.com"
API_PREFIX = "/v1/nwsl/football"
LOCALE = "en-US"

OUTPUT_PATH = Path("nwsl_opta_stats_audit.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 NWSL fantasy stats audit",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nwslsoccer.com/",
}


# ---------------------------------------------------------------------------
# Stats we specifically hope to find for the WSL-style involvement model.
#
# We are NOT assuming these are the exact NWSL statsId values yet.
# The audit searches both statsId and statsLabel for these concepts.
# ---------------------------------------------------------------------------

TARGET_CONCEPTS = {
    "shots_on_target": [
        "shot on target",
        "shots on target",
        "shots-on-target",
        "shot-on-target",
    ],
    "key_passes": [
        "key pass",
        "key passes",
        "key-pass",
        "key-passes",
    ],
    "successful_crosses": [
        "successful cross",
        "successful crosses",
        "crosses successful",
        "accurate crosses",
    ],
    "successful_dribbles": [
        "successful dribble",
        "successful dribbles",
        "dribbles completed",
        "dribble success",
    ],
    "tackles_won": [
        "tackles won",
        "tackle won",
        "successful tackles",
    ],
    "interceptions": [
        "interception",
        "interceptions",
    ],
    "clearances": [
        "clearance",
        "clearances",
    ],
    "blocks": [
        "block",
        "blocks",
        "blocked shots",
    ],
    "recoveries": [
        "recovery",
        "recoveries",
        "ball recoveries",
    ],
}


def get_json(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"

    merged = {"locale": LOCALE}
    if params:
        merged.update(params)

    print(f"GET {url}")
    print(f"params={merged}")

    response = requests.get(
        url,
        params=merged,
        headers=HEADERS,
        timeout=30,
    )

    print(f"HTTP {response.status_code}")

    response.raise_for_status()

    return response.json()


def normalize_text(value) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def extract_stat_rows(player: dict) -> list[dict]:
    """
    The SDP feed normally stores Opta metrics in player['stats'].

    Keep this defensive in case the exact wrapper shape differs.
    """

    stats = player.get("stats")

    if isinstance(stats, list):
        return [s for s in stats if isinstance(s, dict)]

    if isinstance(stats, dict):
        rows = []

        for key, value in stats.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("statsId", key)
                rows.append(row)
            else:
                rows.append({
                    "statsId": key,
                    "statsLabel": key,
                    "statsValue": value,
                })

        return rows

    return []


def player_name(player: dict) -> str:
    for key in (
        "displayName",
        "mediaName",
        "fullName",
        "shortName",
        "shirtName",
    ):
        value = player.get(key)
        if value:
            return str(value)

    first = (
        player.get("mediaFirstName")
        or player.get("firstName")
        or ""
    )

    last = (
        player.get("mediaLastName")
        or player.get("lastName")
        or ""
    )

    name = f"{first} {last}".strip()

    return name or str(player.get("playerId") or "Unknown Player")


def player_team(player: dict) -> str:
    team = player.get("team")

    if isinstance(team, dict):
        for key in (
            "acronym",
            "acronymName",
            "shortName",
            "name",
            "officialName",
        ):
            if team.get(key):
                return str(team[key])

    for key in (
        "teamAcronymName",
        "teamShortName",
        "teamName",
    ):
        if player.get(key):
            return str(player[key])

    return ""


def player_position(player: dict) -> str:
    for key in (
        "roleLabel",
        "position",
        "positionName",
        "skillName",
    ):
        if player.get(key):
            return str(player[key])

    role = player.get("role")

    role_map = {
        1: "GK",
        2: "DEF",
        3: "MID",
        4: "FOR",
        "1": "GK",
        "2": "DEF",
        "3": "MID",
        "4": "FOR",
    }

    return role_map.get(role, str(role or ""))


def search_target_concepts(
    stat_catalog: dict[str, dict],
) -> dict[str, list[dict]]:
    results = {}

    for concept, phrases in TARGET_CONCEPTS.items():
        matches = []

        for stats_id, row in stat_catalog.items():
            stats_label = row.get("statsLabel")

            combined = normalize_text(
                f"{stats_id} {stats_label or ''}"
            )

            if any(
                normalize_text(phrase) in combined
                for phrase in phrases
            ):
                matches.append({
                    "statsId": stats_id,
                    "statsLabel": stats_label,
                    "exampleValue": row.get("exampleValue"),
                })

        results[concept] = matches

    return results


def find_season_ids_from_public_pages() -> list[str]:
    """
    Try to discover SDP Football_Season IDs from public NWSL pages.

    This is intentionally permissive because the public site's HTML/JS can
    change. If this does not work, the workflow accepts NWSL_SEASON_ID as a
    manual override.
    """

    urls = [
        "https://www.nwslsoccer.com/standings",
        "https://www.nwslsoccer.com/stats",
        "https://www.nwslsoccer.com/schedule",
    ]

    found = set()

    pattern = re.compile(
        r"nwsl(?::|%3A%3A)Football_Season(?::|%3A%3A)"
        r"([0-9a-fA-F]{20,})"
    )

    for url in urls:
        try:
            print(f"Trying season discovery from {url}")

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=30,
            )

            print(f"  HTTP {response.status_code}")

            if response.status_code != 200:
                continue

            text = response.text

            for match in pattern.finditer(text):
                uuid_part = match.group(1)

                found.add(
                    f"nwsl::Football_Season::{uuid_part}"
                )

        except Exception as exc:
            print(f"  discovery warning: {exc}")

    return sorted(found)


def fetch_players_for_season(
    season_id: str,
    page_size: int = 500,
) -> dict:
    path = (
        f"{API_PREFIX}/seasons/"
        f"{quote(season_id, safe='')}/stats/players"
    )

    order_candidates = [
        "goals",
        "appearances",
        "minutes-played",
        "total-points",
    ]

    last_error = None

    for order_by in order_candidates:
        try:
            print()
            print(
                f"Trying season {season_id} "
                f"with orderBy={order_by}"
            )

            payload = get_json(
                path,
                {
                    "orderBy": order_by,
                    "direction": "desc",
                    "page": 1,
                    "pageNumElement": page_size,
                },
            )

            players = payload.get("players")

            if isinstance(players, list) and players:
                print(
                    f"SUCCESS: {len(players)} players "
                    f"using orderBy={order_by}"
                )

                return {
                    "season_id": season_id,
                    "order_by": order_by,
                    "payload": payload,
                    "players": players,
                }

        except Exception as exc:
            last_error = str(exc)
            print(f"  failed: {exc}")

    raise RuntimeError(
        f"Could not retrieve players for {season_id}. "
        f"Last error: {last_error}"
    )


def build_audit(players: list[dict], season_id: str) -> dict:
    stat_catalog = {}

    player_examples = []

    position_examples = {}

    stats_per_player = []

    raw_top_level_keys = set()

    for player in players:
        raw_top_level_keys.update(player.keys())

        stats = extract_stat_rows(player)

        stats_per_player.append(len(stats))

        pos = player_position(player)

        if pos and pos not in position_examples:
            position_examples[pos] = {
                "name": player_name(player),
                "team": player_team(player),
                "position": pos,
                "stats_count": len(stats),
                "sample_stats": stats[:25],
            }

        for stat in stats:
            stats_id = str(
                stat.get("statsId")
                or stat.get("statId")
                or stat.get("id")
                or ""
            )

            if not stats_id:
                continue

            stats_label = (
                stat.get("statsLabel")
                or stat.get("statLabel")
                or stat.get("label")
            )

            stats_value = (
                stat.get("statsValue")
                if "statsValue" in stat
                else stat.get("value")
            )

            existing = stat_catalog.setdefault(
                stats_id,
                {
                    "statsId": stats_id,
                    "statsLabel": stats_label,
                    "exampleValue": stats_value,
                    "seenForPlayers": 0,
                },
            )

            existing["seenForPlayers"] += 1

            if (
                not existing.get("statsLabel")
                and stats_label
            ):
                existing["statsLabel"] = stats_label

        if len(player_examples) < 8:
            player_examples.append({
                "name": player_name(player),
                "team": player_team(player),
                "position": pos,
                "playerId": player.get("playerId"),
                "providerId": player.get("providerId"),
                "stats_count": len(stats),
            })

    target_matches = search_target_concepts(stat_catalog)

    all_targets_found = all(
        bool(matches)
        for matches in target_matches.values()
    )

    sorted_catalog = sorted(
        stat_catalog.values(),
        key=lambda row: (
            normalize_text(row.get("statsLabel")),
            normalize_text(row.get("statsId")),
        ),
    )

    stats_counts = stats_per_player or [0]

    return {
        "metadata": {
            "source": "NWSL public SDP / Opta API",
            "base_url": BASE_URL,
            "season_id": season_id,
            "player_count": len(players),
            "unique_stat_count": len(stat_catalog),
            "minimum_stats_per_player": min(stats_counts),
            "maximum_stats_per_player": max(stats_counts),
            "average_stats_per_player": round(
                sum(stats_counts) / len(stats_counts),
                2,
            ),
            "all_target_concepts_found": all_targets_found,
        },
        "target_involvement_concepts": target_matches,
        "position_examples": position_examples,
        "player_examples": player_examples,
        "player_top_level_keys": sorted(raw_top_level_keys),
        "all_available_stats": sorted_catalog,
    }


def print_summary(audit: dict) -> None:
    meta = audit["metadata"]

    print()
    print("=" * 72)
    print("NWSL OPTA PLAYER-STATS AUDIT")
    print("=" * 72)

    print(f"Season ID: {meta['season_id']}")
    print(f"Players: {meta['player_count']}")
    print(
        "Unique Opta stats: "
        f"{meta['unique_stat_count']}"
    )

    print(
        "Stats/player: "
        f"min {meta['minimum_stats_per_player']} | "
        f"avg {meta['average_stats_per_player']} | "
        f"max {meta['maximum_stats_per_player']}"
    )

    print()
    print("TARGET INVOLVEMENT FIELDS")
    print("-" * 72)

    targets = audit["target_involvement_concepts"]

    for concept, matches in targets.items():
        marker = "✅" if matches else "❌"

        print(f"{marker} {concept}")

        for match in matches[:10]:
            print(
                f"    {match['statsId']} "
                f"| {match.get('statsLabel')} "
                f"| example={match.get('exampleValue')}"
            )

    print()

    if meta["all_target_concepts_found"]:
        print(
            "🎉 ALL NINE WSL-STYLE INVOLVEMENT "
            "CONCEPTS HAVE CANDIDATE MATCHES."
        )
    else:
        print(
            "Some target concepts were not matched automatically."
        )
        print(
            "That does NOT necessarily mean the stats are absent."
        )
        print(
            "Inspect nwsl_opta_stats_audit.json for the "
            "complete statsId catalog."
        )

    print()
    print("Example players by position:")

    for pos, row in audit["position_examples"].items():
        print(
            f"  {pos}: {row['name']} "
            f"({row['team']}) — "
            f"{row['stats_count']} stats"
        )

    print("=" * 72)


def main() -> int:
    import os

    manual_season = os.getenv(
        "NWSL_SEASON_ID",
        "",
    ).strip()

    candidate_seasons = []

    if manual_season:
        print(
            "Using NWSL_SEASON_ID environment override:"
        )
        print(manual_season)

        candidate_seasons = [manual_season]

    else:
        candidate_seasons = (
            find_season_ids_from_public_pages()
        )

        print()
        print(
            f"Discovered {len(candidate_seasons)} "
            f"candidate season IDs from public pages."
        )

        for season in candidate_seasons:
            print(f"  {season}")

    if not candidate_seasons:
        print()
        print("ERROR: No NWSL SDP season ID was discovered.")
        print()
        print(
            "This does not mean the Opta API failed. "
            "It only means the season ID was not embedded "
            "in the public page HTML."
        )
        print()
        print(
            "Set a repository/action variable named "
            "NWSL_SEASON_ID to the current SDP season ID "
            "and rerun."
        )

        return 1

    successful = None

    for season_id in candidate_seasons:
        try:
            successful = fetch_players_for_season(
                season_id
            )

            if successful:
                break

        except Exception as exc:
            print(
                f"Season candidate failed: "
                f"{season_id}: {exc}"
            )

    if not successful:
        print()
        print(
            "ERROR: Found season IDs, but none returned "
            "a usable player stats payload."
        )

        return 1

    audit = build_audit(
        successful["players"],
        successful["season_id"],
    )

    audit["metadata"]["order_by"] = (
        successful["order_by"]
    )

    OUTPUT_PATH.write_text(
        json.dumps(
            audit,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print_summary(audit)

    print()
    print(f"Wrote {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
