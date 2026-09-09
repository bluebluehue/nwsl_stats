from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import requests


# =============================================================================
# Configuration
# =============================================================================

BASE_URL = "https://api-sdp.nwslsoccer.com"

# Confirmed working season-level player-stat season.
PLAYER_STATS_SEASON_ID = (
    "nwsl::Football_Season::0b6761e4701749f593690c0f338da74c"
)

OUTPUT_PATH = Path("nwsl_involvement.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 NWSL fantasy involvement fetcher",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nwslsoccer.com/",
}

PLAYER_STATS_URL = (
    f"{BASE_URL}/v1/nwsl/football/"
    f"seasons/{PLAYER_STATS_SEASON_ID}/stats/players"
)


# =============================================================================
# Involvement components
# =============================================================================
#
# These are the nine concepts we verified in the Opta audit.
#
# IMPORTANT:
# This file deliberately keeps the raw components separate from the final
# involvement score. That lets us inspect/recalibrate the weights later
# without having to re-discover the data.
#
# Attack:
#   shots on target
#   key passes
#   successful crosses
#   successful dribbles
#
# Defense:
#   tackles won
#   interceptions
#   clearances
#   blocks
#   recoveries
#
# The aliases are intentionally fairly strict. We do not want to silently
# substitute a vaguely similar Opta field.
# =============================================================================

STAT_ALIASES = {
    "shots_on_target": [
        "shots on target",
        "shots on goal",
    ],
    "key_passes": [
        "key passes (attempt assists)",
        "key passes",
    ],
    "successful_crosses": [
        "successful crosses",
        "accurate crosses",
    ],
    "successful_dribbles": [
        "successful dribbles",
        "successful take ons",
        "successful take-ons",
    ],
    "tackles_won": [
        "tackles won",
        "successful tackles",
    ],
    "interceptions": [
        "interceptions",
    ],
    "clearances": [
        "clearances",
    ],
    "blocks": [
        "blocks",
        "blocked shots",
    ],
    "recoveries": [
        "recoveries",
        "ball recoveries",
    ],
}


# =============================================================================
# Position-specific component weights
# =============================================================================
#
# These are INITIAL transparent weights, not sacred model coefficients.
#
# The important thing in v1 is:
#   1. capture the correct Opta data,
#   2. normalize it fairly,
#   3. expose every component,
#   4. inspect the rankings,
#   5. then backtest/recalibrate.
#
# They sum to 1.0 within each position.
# =============================================================================

POSITION_WEIGHTS = {
    "GK": {
        "shots_on_target": 0.00,
        "key_passes": 0.00,
        "successful_crosses": 0.00,
        "successful_dribbles": 0.00,
        "tackles_won": 0.05,
        "interceptions": 0.05,
        "clearances": 0.15,
        "blocks": 0.05,
        "recoveries": 0.70,
    },

    "DEF": {
        "shots_on_target": 0.08,
        "key_passes": 0.10,
        "successful_crosses": 0.08,
        "successful_dribbles": 0.05,
        "tackles_won": 0.17,
        "interceptions": 0.15,
        "clearances": 0.15,
        "blocks": 0.10,
        "recoveries": 0.12,
    },

    "MID": {
        "shots_on_target": 0.15,
        "key_passes": 0.20,
        "successful_crosses": 0.10,
        "successful_dribbles": 0.12,
        "tackles_won": 0.10,
        "interceptions": 0.08,
        "clearances": 0.03,
        "blocks": 0.02,
        "recoveries": 0.20,
    },

    "FOR": {
        "shots_on_target": 0.30,
        "key_passes": 0.20,
        "successful_crosses": 0.10,
        "successful_dribbles": 0.15,
        "tackles_won": 0.05,
        "interceptions": 0.03,
        "clearances": 0.01,
        "blocks": 0.01,
        "recoveries": 0.15,
    },
}


# =============================================================================
# Utility
# =============================================================================

def normalize_text(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def safe_float(value: Any) -> float | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return float(value)

    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)

        return None

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(",", "")

    if text.endswith("%"):
        text = text[:-1]

    try:
        number = float(text)

        if math.isfinite(number):
            return number

    except ValueError:
        pass

    return None


def clamp(
    value: float,
    low: float,
    high: float,
) -> float:
    return max(
        low,
        min(high, value),
    )


# =============================================================================
# Fetch player stats
# =============================================================================

def fetch_players() -> list[dict[str, Any]]:
    params = {
        "locale": "en-US",
        "category": "general",
        "role": "all",
        "direction": "desc",
        "page": 1,
        "pageNumElement": 400,
    }

    print("=" * 88)
    print("FETCHING NWSL OPTA PLAYER STATS")
    print("=" * 88)
    print(PLAYER_STATS_URL)
    print(params)

    response = requests.get(
        PLAYER_STATS_URL,
        params=params,
        headers=HEADERS,
        timeout=90,
    )

    print("HTTP:", response.status_code)

    response.raise_for_status()

    payload = response.json()

    if isinstance(payload, dict):
        players = payload.get("players")
    else:
        players = None

    if not isinstance(players, list):
        raise RuntimeError(
            "NWSL Opta response did not contain a players list."
        )

    players = [
        row
        for row in players
        if isinstance(row, dict)
    ]

    print(
        "Players returned:",
        len(players),
    )

    return players


# =============================================================================
# Player metadata
# =============================================================================

def player_name(
    player: dict[str, Any],
) -> str:
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

    if name:
        return name

    return str(
        player.get("playerId")
        or player.get("providerId")
        or "Unknown Player"
    )


def player_team(
    player: dict[str, Any],
) -> dict[str, Any]:
    team = player.get("team")

    if isinstance(team, dict):
        return {
            "id": (
                team.get("teamId")
                or team.get("id")
            ),
            "providerId": (
                team.get("providerId")
            ),
            "name": (
                team.get("officialName")
                or team.get("name")
                or team.get("shortName")
            ),
            "shortName": (
                team.get("shortName")
                or team.get("acronymName")
                or team.get("acronym")
            ),
        }

    return {
        "id": player.get("teamId"),
        "providerId": (
            player.get("teamProviderId")
        ),
        "name": player.get("teamName"),
        "shortName": (
            player.get("teamShortName")
            or player.get("teamAcronymName")
        ),
    }


def normalize_position(
    player: dict[str, Any],
) -> str:
    candidates = [
        player.get("roleLabel"),
        player.get("position"),
        player.get("positionName"),
        player.get("skillName"),
    ]

    for candidate in candidates:
        text = normalize_text(candidate)

        if not text:
            continue

        if (
            "goalkeeper" in text
            or text == "gk"
        ):
            return "GK"

        if (
            "defender" in text
            or text == "def"
            or "defence" in text
            or "defense" in text
        ):
            return "DEF"

        if (
            "midfielder" in text
            or text == "mid"
        ):
            return "MID"

        if (
            "forward" in text
            or "striker" in text
            or text in {
                "for",
                "fwd",
            }
        ):
            return "FOR"

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

    return role_map.get(
        role,
        "UNK",
    )


# =============================================================================
# Opta stat extraction
# =============================================================================

def extract_stats(
    player: dict[str, Any],
) -> list[dict[str, Any]]:
    stats = player.get("stats")

    if isinstance(stats, list):
        return [
            row
            for row in stats
            if isinstance(row, dict)
        ]

    if isinstance(stats, dict):
        output = []

        for key, value in stats.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault(
                    "statsId",
                    key,
                )

            else:
                row = {
                    "statsId": key,
                    "statsLabel": key,
                    "statsValue": value,
                }

            output.append(row)

        return output

    return []


def build_stat_lookup(
    player: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    lookup = {}

    for stat in extract_stats(player):
        stat_id = str(
            stat.get("statsId")
            or stat.get("statId")
            or stat.get("id")
            or ""
        )

        stat_label = str(
            stat.get("statsLabel")
            or stat.get("statLabel")
            or stat.get("label")
            or stat_id
        )

        if not stat_id and not stat_label:
            continue

        searchable = normalize_text(
            f"{stat_id} {stat_label}"
        )

        lookup[searchable] = stat

    return lookup


def find_stat(
    player: dict[str, Any],
    aliases: list[str],
) -> dict[str, Any] | None:
    stats = extract_stats(player)

    normalized_aliases = [
        normalize_text(alias)
        for alias in aliases
    ]

    # First pass: exact label / ID match.
    for stat in stats:
        stat_id = normalize_text(
            stat.get("statsId")
        )

        stat_label = normalize_text(
            stat.get("statsLabel")
            or stat.get("statLabel")
            or stat.get("label")
        )

        for alias in normalized_aliases:
            if (
                stat_id == alias
                or stat_label == alias
            ):
                return stat

    # Second pass: phrase match.
    for stat in stats:
        searchable = normalize_text(
            f"{stat.get('statsId', '')} "
            f"{stat.get('statsLabel', '')} "
            f"{stat.get('statLabel', '')} "
            f"{stat.get('label', '')}"
        )

        for alias in normalized_aliases:
            if alias and alias in searchable:
                return stat

    return None


def stat_value(
    stat: dict[str, Any] | None,
) -> float | None:
    if not stat:
        return None

    if "statsValue" in stat:
        return safe_float(
            stat.get("statsValue")
        )

    return safe_float(
        stat.get("value")
    )


# =============================================================================
# Supporting stats
# =============================================================================
#
# We want minutes / appearances if Opta exposes them so that raw season totals
# become rates rather than simply rewarding whoever has played the most.
# =============================================================================

SUPPORTING_STAT_ALIASES = {
    "minutes": [
        "minutes played",
        "total minutes played",
        "minutes",
    ],
    "appearances": [
        "appearances",
        "total appearances",
    ],
    "starts": [
        "starts",
        "total starts",
    ],
    "sub_on": [
        "total sub on",
        "substitute on",
        "sub on",
    ],
    "sub_off": [
        "total sub off",
        "substitute off",
        "sub off",
    ],
    "goals": [
        "goals",
        "total goals",
    ],
    "assists": [
        "assists",
        "total assists",
    ],
    "shots": [
        "shots",
        "total shots",
    ],
    "xg": [
        "expected goals",
        "xg",
    ],
    "touches_opposition_box": [
        "total touches in opposition box",
        "touches in opposition box",
    ],
    "final_third_touches": [
        "final third touches",
    ],
    "progressive_carries": [
        "progressive carries",
    ],
    "shots_created": [
        "shots created",
    ],
    "big_chances_created": [
        "total big chances created",
        "big chances created",
    ],
}


# =============================================================================
# Build raw player records
# =============================================================================

def extract_component_values(
    player: dict[str, Any],
) -> dict[str, Any]:
    output = {}

    for concept, aliases in (
        STAT_ALIASES.items()
    ):
        stat = find_stat(
            player,
            aliases,
        )

        output[concept] = {
            "value": stat_value(stat),
            "statsId": (
                stat.get("statsId")
                if stat
                else None
            ),
            "statsLabel": (
                stat.get("statsLabel")
                if stat
                else None
            ),
        }

    return output


def extract_supporting_values(
    player: dict[str, Any],
) -> dict[str, Any]:
    output = {}

    for concept, aliases in (
        SUPPORTING_STAT_ALIASES.items()
    ):
        stat = find_stat(
            player,
            aliases,
        )

        output[concept] = {
            "value": stat_value(stat),
            "statsId": (
                stat.get("statsId")
                if stat
                else None
            ),
            "statsLabel": (
                stat.get("statsLabel")
                if stat
                else None
            ),
        }

    return output


def build_raw_player(
    player: dict[str, Any],
) -> dict[str, Any]:
    components = (
        extract_component_values(
            player
        )
    )

    supporting = (
        extract_supporting_values(
            player
        )
    )

    minutes = (
        supporting
        .get("minutes", {})
        .get("value")
    )

    appearances = (
        supporting
        .get("appearances", {})
        .get("value")
    )

    # Prefer per-90.
    #
    # If minutes aren't available, fall back to per appearance.
    # If neither exists, retain raw values but mark the rate basis.
    if minutes and minutes > 0:
        denominator = (
            minutes / 90.0
        )
        rate_basis = "per90"

    elif (
        appearances
        and appearances > 0
    ):
        denominator = appearances
        rate_basis = "perAppearance"

    else:
        denominator = 1.0
        rate_basis = "raw"

    rates = {}

    for concept, info in (
        components.items()
    ):
        raw_value = info.get(
            "value"
        )

        if raw_value is None:
            rates[concept] = None
        else:
            rates[concept] = round(
                raw_value / denominator,
                4,
            )

    team = player_team(player)

    return {
        "playerId": player.get(
            "playerId"
        ),
        "providerId": player.get(
            "providerId"
        ),
        "name": player_name(player),
        "team": team,
        "position": (
            normalize_position(player)
        ),

        "minutes": minutes,
        "appearances": appearances,

        "starts": (
            supporting
            .get("starts", {})
            .get("value")
        ),

        "subOn": (
            supporting
            .get("sub_on", {})
            .get("value")
        ),

        "subOff": (
            supporting
            .get("sub_off", {})
            .get("value")
        ),

        "rateBasis": rate_basis,

        "components": components,

        "rates": rates,

        "supporting": supporting,
    }


# =============================================================================
# Percentile normalization
# =============================================================================
#
# Percentiles are much more robust here than fixed min/max scaling.
#
# A score of 80 means roughly:
# "this player's rate is better than ~80% of relevant players in the
# comparison pool."
#
# We normalize WITHIN POSITION so defenders are not punished for failing to
# shoot like forwards and forwards are not expected to clear like CBs.
# =============================================================================

def percentile_rank(
    value: float | None,
    population: list[float],
) -> float | None:
    if value is None:
        return None

    clean = sorted(
        x
        for x in population
        if x is not None
        and math.isfinite(x)
    )

    if not clean:
        return None

    if len(clean) == 1:
        return 50.0

    below = sum(
        1
        for x in clean
        if x < value
    )

    equal = sum(
        1
        for x in clean
        if x == value
    )

    # Midrank percentile.
    rank = (
        below
        + (equal - 1) / 2
    )

    percentile = (
        rank
        / (len(clean) - 1)
        * 100.0
    )

    return round(
        clamp(
            percentile,
            0.0,
            100.0,
        ),
        1,
    )


def eligible_for_normalization(
    player: dict[str, Any],
) -> bool:
    """
    Avoid letting a 10-minute cameo establish the position distribution.

    This does NOT remove low-minute players from the output. It only keeps
    tiny samples from distorting everybody else's percentile scale.
    """

    minutes = player.get(
        "minutes"
    )

    appearances = player.get(
        "appearances"
    )

    if (
        minutes is not None
        and minutes >= 180
    ):
        return True

    if (
        minutes is None
        and appearances is not None
        and appearances >= 3
    ):
        return True

    # If the API didn't expose either field, we cannot apply the filter.
    if (
        minutes is None
        and appearances is None
    ):
        return True

    return False


def build_position_populations(
    players: list[dict[str, Any]],
) -> dict[str, dict[str, list[float]]]:
    populations = defaultdict(
        lambda: defaultdict(list)
    )

    for player in players:
        position = player.get(
            "position"
        )

        if position not in (
            "GK",
            "DEF",
            "MID",
            "FOR",
        ):
            continue

        if not eligible_for_normalization(
            player
        ):
            continue

        for concept, value in (
            player.get(
                "rates",
                {},
            ).items()
        ):
            if value is not None:
                populations[
                    position
                ][concept].append(
                    value
                )

    return populations


# =============================================================================
# Composite involvement rating
# =============================================================================

def calculate_player_scores(
    player: dict[str, Any],
    populations: dict[
        str,
        dict[str, list[float]],
    ],
) -> None:
    position = player.get(
        "position"
    )

    weights = POSITION_WEIGHTS.get(
        position
    )

    if not weights:
        player[
            "componentPercentiles"
        ] = {}

        player[
            "involvementRating"
        ] = None

        return

    percentiles = {}

    weighted_total = 0.0
    available_weight = 0.0

    for concept, weight in (
        weights.items()
    ):
        rate = (
            player
            .get("rates", {})
            .get(concept)
        )

        population = (
            populations
            .get(position, {})
            .get(concept, [])
        )

        percentile = percentile_rank(
            rate,
            population,
        )

        percentiles[
            concept
        ] = percentile

        if (
            percentile is not None
            and weight > 0
        ):
            weighted_total += (
                percentile
                * weight
            )

            available_weight += (
                weight
            )

    if available_weight > 0:
        involvement_rating = round(
            weighted_total
            / available_weight,
            1,
        )
    else:
        involvement_rating = None

    player[
        "componentPercentiles"
    ] = percentiles

    player[
        "involvementRating"
    ] = involvement_rating


# =============================================================================
# Sample confidence
# =============================================================================

def calculate_sample_confidence(
    player: dict[str, Any],
) -> float:
    """
    0-100 indicator of how much season sample we have.

    This is intentionally NOT baked into Involvement Rating. A player can
    have an excellent involvement profile in a small sample; we simply want
    the consumer to know the sample is uncertain.

    900 minutes ~= full confidence.
    """

    minutes = player.get(
        "minutes"
    )

    appearances = player.get(
        "appearances"
    )

    if (
        minutes is not None
        and minutes >= 0
    ):
        return round(
            clamp(
                minutes / 900.0 * 100.0,
                0.0,
                100.0,
            ),
            1,
        )

    if (
        appearances is not None
        and appearances >= 0
    ):
        return round(
            clamp(
                appearances / 10.0 * 100.0,
                0.0,
                100.0,
            ),
            1,
        )

    return 0.0


# =============================================================================
# Position summaries
# =============================================================================

def build_position_summary(
    players: list[dict[str, Any]],
) -> dict[str, Any]:
    output = {}

    for position in (
        "GK",
        "DEF",
        "MID",
        "FOR",
    ):
        position_players = [
            player
            for player in players
            if player.get(
                "position"
            ) == position
            and player.get(
                "involvementRating"
            ) is not None
        ]

        position_players.sort(
            key=lambda player: (
                player.get(
                    "involvementRating"
                )
                or -1
            ),
            reverse=True,
        )

        ratings = [
            player[
                "involvementRating"
            ]
            for player
            in position_players
        ]

        output[position] = {
            "playerCount": len(
                position_players
            ),

            "medianRating": (
                round(
                    median(ratings),
                    1,
                )
                if ratings
                else None
            ),

            "topPlayers": [
                {
                    "name": player[
                        "name"
                    ],
                    "team": (
                        player
                        .get("team", {})
                        .get("shortName")
                    ),
                    "rating": (
                        player[
                            "involvementRating"
                        ]
                    ),
                    "minutes": (
                        player.get(
                            "minutes"
                        )
                    ),
                    "sampleConfidence": (
                        player.get(
                            "sampleConfidence"
                        )
                    ),
                }
                for player
                in position_players[:15]
            ],
        }

    return output


# =============================================================================
# Stat-resolution audit
# =============================================================================

def build_stat_resolution(
    players: list[dict[str, Any]],
) -> dict[str, Any]:
    output = {}

    for concept, aliases in (
        STAT_ALIASES.items()
    ):
        labels = defaultdict(int)

        found = 0

        for player in players:
            stat = find_stat(
                player,
                aliases,
            )

            if not stat:
                continue

            found += 1

            label = str(
                stat.get("statsLabel")
                or stat.get("statsId")
                or "UNKNOWN"
            )

            labels[label] += 1

        output[concept] = {
            "playersMatched": found,
            "labelsUsed": dict(
                sorted(
                    labels.items(),
                    key=lambda item: (
                        -item[1],
                        item[0],
                    ),
                )
            ),
        }

    return output


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    raw_players = fetch_players()

    players = [
        build_raw_player(
            player
        )
        for player in raw_players
    ]

    populations = (
        build_position_populations(
            players
        )
    )

    for player in players:
        calculate_player_scores(
            player,
            populations,
        )

        player[
            "sampleConfidence"
        ] = (
            calculate_sample_confidence(
                player
            )
        )

    # Highest involvement first makes the JSON pleasant to inspect.
    players.sort(
        key=lambda player: (
            player.get(
                "involvementRating"
            )
            if player.get(
                "involvementRating"
            ) is not None
            else -1
        ),
        reverse=True,
    )

    stat_resolution = (
        build_stat_resolution(
            raw_players
        )
    )

    position_summary = (
        build_position_summary(
            players
        )
    )

    metadata = {
        "generatedAtUtc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),

        "source": (
            "NWSL public SDP / Opta API"
        ),

        "seasonId": (
            PLAYER_STATS_SEASON_ID
        ),

        "playerStatsEndpoint": (
            PLAYER_STATS_URL
        ),

        "playerCount": len(
            players
        ),

        "modelVersion": (
            "nwsl-involvement-v1"
        ),

        "normalization": (
            "within-position percentile "
            "of per-90 rates where minutes "
            "are available"
        ),

        "minimumNormalizationSample": (
            "180 minutes, or 3 appearances "
            "when minutes unavailable"
        ),

        "notes": [
            (
                "Involvement Rating is intentionally "
                "separate from Fixture, Fantasy Form, "
                "Decision Rating, Visionary, and DGW value."
            ),
            (
                "Initial position weights are transparent "
                "starting weights and should be inspected/"
                "backtested before becoming part of the "
                "live Decision Rating."
            ),
            (
                "Recent match role/activity is not included "
                "in v1. Match-specific endpoints have been "
                "verified separately and will be added after "
                "reliable match enumeration is connected."
            ),
        ],
    }

    output = {
        "metadata": metadata,

        "weights": (
            POSITION_WEIGHTS
        ),

        "statResolution": (
            stat_resolution
        ),

        "positionSummary": (
            position_summary
        ),

        "players": players,
    }

    OUTPUT_PATH.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 88)
    print("NWSL INVOLVEMENT V1")
    print("=" * 88)

    print(
        "Players:",
        len(players),
    )

    print()
    print("STAT RESOLUTION")
    print("-" * 88)

    for concept, result in (
        stat_resolution.items()
    ):
        print(
            f"{concept:25} "
            f"{result['playersMatched']:4} players "
            f"{result['labelsUsed']}"
        )

    print()
    print("TOP PLAYERS BY POSITION")
    print("-" * 88)

    for position, summary in (
        position_summary.items()
    ):
        print()
        print(position)

        for row in (
            summary["topPlayers"][:10]
        ):
            print(
                f"  "
                f"{row['rating']:5.1f}  "
                f"{row['name']}  "
                f"{row['team']}  "
                f"min={row['minutes']}  "
                f"confidence="
                f"{row['sampleConfidence']}"
            )

    print()
    print(
        f"Wrote {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
