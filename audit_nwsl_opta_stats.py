from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://api-sdp.nwslsoccer.com"

SEASON_ID = "nwsl::Football_Season::0b6761e4701749f593690c0f338da74c"

OUTPUT_PATH = Path("nwsl_opta_stats_audit.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 NWSL fantasy stats audit",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nwslsoccer.com/",
}


TARGET_CONCEPTS = {
    "shots_on_target": [
        "shots on target",
        "shot on target",
    ],
    "key_passes": [
        "key passes",
        "attempt assists",
    ],
    "successful_crosses": [
        "successful crosses",
        "accurate crosses",
        "crosses successful",
    ],
    "successful_dribbles": [
        "successful dribbles",
        "dribbles completed",
        "successful take ons",
        "successful take-ons",
    ],
    "tackles_won": [
        "tackles won",
        "successful tackles",
    ],
    "interceptions": [
        "interceptions",
        "interception",
    ],
    "clearances": [
        "clearances",
        "clearance",
    ],
    "blocks": [
        "blocks",
        "blocked shots",
        "blocked",
    ],
    "recoveries": [
        "recoveries",
        "ball recoveries",
        "recovery",
    ],
}


def normalize_text(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def fetch_players() -> list[dict[str, Any]]:
    url = (
        f"{BASE_URL}/v1/nwsl/football/seasons/"
        f"{SEASON_ID}/stats/players"
    )

    params = {
        "locale": "en-US",
        "category": "general",
        "role": "all",
        "direction": "desc",
        "page": 1,
        "pageNumElement": 400,
    }

    print("=" * 80)
    print("FETCHING NWSL OPTA PLAYER STATS")
    print("=" * 80)
    print(f"URL: {url}")
    print(f"PARAMS: {params}")

    response = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=60,
    )

    print(f"HTTP {response.status_code}")
    print(
        "CONTENT-TYPE:",
        response.headers.get("content-type"),
    )

    response.raise_for_status()

    payload = response.json()

    players = payload.get("players")

    if not isinstance(players, list):
        raise RuntimeError(
            "Response did not contain a players list."
        )

    print(f"Players returned: {len(players)}")

    return players


def extract_stats(player: dict[str, Any]) -> list[dict[str, Any]]:
    stats = player.get("stats")

    if isinstance(stats, list):
        return [
            row
            for row in stats
            if isinstance(row, dict)
        ]

    if isinstance(stats, dict):
        rows = []

        for key, value in stats.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("statsId", key)
            else:
                row = {
                    "statsId": key,
                    "statsLabel": key,
                    "statsValue": value,
                }

            rows.append(row)

        return rows

    return []


def player_name(player: dict[str, Any]) -> str:
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

    combined = f"{first} {last}".strip()

    if combined:
        return combined

    return str(
        player.get("playerId")
        or player.get("providerId")
        or "Unknown Player"
    )


def player_team(player: dict[str, Any]) -> str:
    team = player.get("team")

    if isinstance(team, dict):
        for key in (
            "acronymName",
            "acronym",
            "shortName",
            "officialName",
            "name",
        ):
            value = team.get(key)

            if value:
                return str(value)

    for key in (
        "teamAcronymName",
        "teamShortName",
        "teamName",
    ):
        value = player.get(key)

        if value:
            return str(value)

    return ""


def player_position(player: dict[str, Any]) -> str:
    for key in (
        "roleLabel",
        "position",
        "positionName",
        "skillName",
    ):
        value = player.get(key)

        if value:
            return str(value)

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


def build_stat_catalog(
    players: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    catalog = {}

    for player in players:
        for stat in extract_stats(player):
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
                or stats_id
            )

            stats_value = (
                stat.get("statsValue")
                if "statsValue" in stat
                else stat.get("value")
            )

            row = catalog.setdefault(
                stats_id,
                {
                    "statsId": stats_id,
                    "statsLabel": stats_label,
                    "seenForPlayers": 0,
                    "exampleValues": [],
                },
            )

            row["seenForPlayers"] += 1

            if (
                stats_value is not None
                and len(row["exampleValues"]) < 5
            ):
                row["exampleValues"].append(stats_value)

    return catalog


def match_target_concepts(
    catalog: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    results = {}

    for concept, phrases in TARGET_CONCEPTS.items():
        matches = []

        for row in catalog.values():
            stats_id = row.get("statsId")
            stats_label = row.get("statsLabel")

            searchable = normalize_text(
                f"{stats_id} {stats_label}"
            )

            matched_phrases = []

            for phrase in phrases:
                normalized_phrase = normalize_text(phrase)

                if normalized_phrase in searchable:
                    matched_phrases.append(phrase)

            if matched_phrases:
                matches.append(
                    {
                        "statsId": stats_id,
                        "statsLabel": stats_label,
                        "seenForPlayers": row.get(
                            "seenForPlayers"
                        ),
                        "exampleValues": row.get(
                            "exampleValues"
                        ),
                        "matchedPhrases": matched_phrases,
                    }
                )

        results[concept] = sorted(
            matches,
            key=lambda x: (
                normalize_text(x.get("statsLabel")),
                normalize_text(x.get("statsId")),
            ),
        )

    return results


def get_representative_players(
    players: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    examples = {}

    for player in players:
        position = player_position(player)

        normalized_position = normalize_text(position)

        if "goal" in normalized_position:
            bucket = "GK"
        elif "def" in normalized_position:
            bucket = "DEF"
        elif "mid" in normalized_position:
            bucket = "MID"
        elif (
            "for" in normalized_position
            or "striker" in normalized_position
            or "forward" in normalized_position
        ):
            bucket = "FOR"
        else:
            continue

        if bucket in examples:
            continue

        stats = extract_stats(player)

        examples[bucket] = {
            "name": player_name(player),
            "team": player_team(player),
            "positionRaw": position,
            "playerId": player.get("playerId"),
            "providerId": player.get("providerId"),
            "statsCount": len(stats),
            "sampleStats": stats[:40],
        }

        if len(examples) == 4:
            break

    return examples


def build_player_target_values(
    players: list[dict[str, Any]],
    target_matches: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    target_ids = {}

    for concept, matches in target_matches.items():
        target_ids[concept] = {
            str(match.get("statsId"))
            for match in matches
        }

    sample_rows = []

    for player in players:
        stats_lookup = {}

        for stat in extract_stats(player):
            stats_id = str(
                stat.get("statsId")
                or stat.get("statId")
                or stat.get("id")
                or ""
            )

            if stats_id:
                stats_lookup[stats_id] = stat

        values = {}

        any_found = False

        for concept, ids in target_ids.items():
            concept_values = []

            for stats_id in ids:
                stat = stats_lookup.get(stats_id)

                if not stat:
                    continue

                value = (
                    stat.get("statsValue")
                    if "statsValue" in stat
                    else stat.get("value")
                )

                concept_values.append(
                    {
                        "statsId": stats_id,
                        "statsLabel": (
                            stat.get("statsLabel")
                            or stat.get("statLabel")
                            or stat.get("label")
                        ),
                        "value": value,
                    }
                )

            if concept_values:
                any_found = True

            values[concept] = concept_values

        if any_found:
            sample_rows.append(
                {
                    "name": player_name(player),
                    "team": player_team(player),
                    "position": player_position(player),
                    "playerId": player.get("playerId"),
                    "providerId": player.get(
                        "providerId"
                    ),
                    "targetValues": values,
                }
            )

        if len(sample_rows) >= 25:
            break

    return sample_rows


def build_audit(
    players: list[dict[str, Any]],
) -> dict[str, Any]:
    catalog = build_stat_catalog(players)

    targets = match_target_concepts(catalog)

    stat_counts = [
        len(extract_stats(player))
        for player in players
    ]

    if not stat_counts:
        stat_counts = [0]

    sorted_catalog = sorted(
        catalog.values(),
        key=lambda row: (
            normalize_text(row.get("statsLabel")),
            normalize_text(row.get("statsId")),
        ),
    )

    return {
        "metadata": {
            "source": "NWSL public SDP / Opta API",
            "seasonId": SEASON_ID,
            "endpoint": (
                f"{BASE_URL}/v1/nwsl/football/"
                f"seasons/{SEASON_ID}/stats/players"
            ),
            "playerCount": len(players),
            "uniqueStatCount": len(catalog),
            "minimumStatsPerPlayer": min(stat_counts),
            "maximumStatsPerPlayer": max(stat_counts),
            "averageStatsPerPlayer": round(
                sum(stat_counts) / len(stat_counts),
                2,
            ),
            "allNineTargetConceptsMatched": all(
                bool(matches)
                for matches in targets.values()
            ),
        },
        "targetInvolvementConcepts": targets,
        "representativePlayers": (
            get_representative_players(players)
        ),
        "samplePlayerTargetValues": (
            build_player_target_values(
                players,
                targets,
            )
        ),
        "allAvailableStats": sorted_catalog,
    }


def print_summary(
    audit: dict[str, Any],
) -> None:
    metadata = audit["metadata"]

    print()
    print("=" * 80)
    print("NWSL OPTA STATS AUDIT")
    print("=" * 80)

    print(
        "Season ID:",
        metadata["seasonId"],
    )

    print(
        "Players:",
        metadata["playerCount"],
    )

    print(
        "Unique Opta stat fields:",
        metadata["uniqueStatCount"],
    )

    print(
        "Stats/player:",
        f"min={metadata['minimumStatsPerPlayer']}",
        f"avg={metadata['averageStatsPerPlayer']}",
        f"max={metadata['maximumStatsPerPlayer']}",
    )

    print()
    print("WSL-STYLE INVOLVEMENT TARGETS")
    print("-" * 80)

    for concept, matches in (
        audit["targetInvolvementConcepts"].items()
    ):
        marker = "YES" if matches else "NO"

        print(f"{marker:3}  {concept}")

        for match in matches[:10]:
            print(
                "     ",
                match.get("statsId"),
                "|",
                match.get("statsLabel"),
                "| examples:",
                match.get("exampleValues"),
            )

    print()
    print(
        "All nine concepts matched:",
        metadata[
            "allNineTargetConceptsMatched"
        ],
    )

    print()
    print(
        "Representative players by position:"
    )

    for position, row in (
        audit["representativePlayers"].items()
    ):
        print(
            f"  {position}: "
            f"{row.get('name')} "
            f"({row.get('team')}) "
            f"- {row.get('statsCount')} stats"
        )

    print("=" * 80)


def main() -> None:
    players = fetch_players()

    audit = build_audit(players)

    OUTPUT_PATH.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print_summary(audit)

    print()
    print(
        f"Wrote {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
