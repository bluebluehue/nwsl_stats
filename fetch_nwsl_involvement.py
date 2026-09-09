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
# Verified Opta involvement fields
# =============================================================================
#
# IMPORTANT:
#
# These are now STRICT mappings.
#
# We do NOT fuzzy-match similar-looking fields.
#
# If a verified field is absent from a player's Opta stat array, we treat
# that as zero. Opta frequently omits zero-valued player stats.
#
# This prevents errors such as:
#
#   Successful Dribbles
#       accidentally matching
#   Unsuccessful Dribbles
#
# or:
#
#   Successful Crosses & Corners
#       accidentally matching
#   Unsuccessful Crosses & Corners
#
# =============================================================================

INVOLVEMENT_STAT_FIELDS = {
    "shots_on_target": [
        "Shots On Target ( inc goals )",
    ],

    "key_passes": [
        "Key Passes (Attempt Assists)",
    ],

    "successful_crosses": [
        "Successful Crosses & Corners",
    ],

    "successful_dribbles": [
        "Successful Dribbles",
    ],

    "tackles_won": [
        "Tackles won",
    ],

    "interceptions": [
        "Interceptions",
    ],

    "clearances": [
        "Total Clearances",
    ],

    # Opta exposes both of these labels.
    #
    # We allow either exact label but NEVER fuzzy-match.
    # "Blocked Shots" is preferred when both happen to exist.
    "blocks": [
        "Blocked Shots",
        "Blocks",
    ],

    "recoveries": [
        "Recoveries",
    ],
}


# =============================================================================
# Supporting fields
# =============================================================================
#
# These are also exact mappings.
#
# This fixes another issue seen in v1 where "shots" could accidentally match
# "Shots Created".
#
# For counting fields such as goals, assists, starts, etc., absence is treated
# as zero.
#
# =============================================================================

SUPPORTING_STAT_FIELDS = {
    "minutes": [
        "Minutes played",
    ],

    "appearances": [
        "Appearances",
    ],

    "starts": [
        "Starts",
    ],

    "sub_on": [
        "Substitute On",
    ],

    "sub_off": [
        "Substitute Off",
    ],

    "goals": [
        "Goals",
    ],

    "assists": [
        "Assists",
    ],

    "shots": [
        "Total Shots",
    ],

    "xg": [
        "Xg",
    ],

    "touches_opposition_box": [
        "Total Touches In Opposition Box",
    ],

    "final_third_touches": [
        "Final Third Touches",
    ],

    "progressive_carries": [
        "Progressive Carries",
    ],

    "shots_created": [
        "Shots Created",
    ],

    "big_chances_created": [
        "Total Big Chances Created",
    ],
}


# =============================================================================
# Position-specific component weights
# =============================================================================
#
# These remain INITIAL transparent weights.
#
# We will inspect and backtest them before allowing Involvement Rating to
# affect the live Decision Rating.
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
        number = float(value)

        if math.isfinite(number):
            return number

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

    print(
        "HTTP:",
        response.status_code,
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(
            "NWSL Opta response was not a JSON object."
        )

    players = payload.get(
        "players"
    )

    if not isinstance(players, list):
        raise RuntimeError(
            "NWSL Opta response did not contain a players list."
        )

    players = [
        player
        for player in players
        if isinstance(player, dict)
    ]

    if len(players) < 100:
        raise RuntimeError(
            "Unexpectedly small player response. "
            f"Only {len(players)} players returned."
        )

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

    combined = (
        f"{first} {last}"
        .strip()
    )

    if combined:
        return combined

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
        "id": player.get(
            "teamId"
        ),

        "providerId": player.get(
            "teamProviderId"
        ),

        "name": player.get(
            "teamName"
        ),

        "shortName": (
            player.get("teamShortName")
            or player.get(
                "teamAcronymName"
            )
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
        text = normalize_text(
            candidate
        )

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

    role = player.get(
        "role"
    )

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
# Raw Opta stats
# =============================================================================

def extract_stats(
    player: dict[str, Any],
) -> list[dict[str, Any]]:
    stats = player.get(
        "stats"
    )

    if isinstance(stats, list):
        return [
            row
            for row in stats
            if isinstance(row, dict)
        ]

    if isinstance(stats, dict):
        output = []

        for key, value in (
            stats.items()
        ):
            if isinstance(
                value,
                dict,
            ):
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


def stat_identity_values(
    stat: dict[str, Any],
) -> set[str]:
    """
    Return normalized exact identity strings for a stat.

    We compare the requested field against these exact normalized strings.
    We do NOT use substring matching.
    """

    identities = set()

    for key in (
        "statsId",
        "statId",
        "statsLabel",
        "statLabel",
        "label",
        "id",
    ):
        value = stat.get(key)

        if value is None:
            continue

        normalized = normalize_text(
            value
        )

        if normalized:
            identities.add(
                normalized
            )

    return identities


def find_exact_stat(
    player: dict[str, Any],
    accepted_names: list[str],
) -> dict[str, Any] | None:
    """
    Find a stat using exact normalized identity equality only.

    The order of accepted_names matters:
    the first exact field available wins.
    """

    stats = extract_stats(
        player
    )

    normalized_targets = [
        normalize_text(name)
        for name in accepted_names
    ]

    for target in normalized_targets:
        for stat in stats:
            identities = (
                stat_identity_values(
                    stat
                )
            )

            if target in identities:
                return stat

    return None


def stat_value(
    stat: dict[str, Any] | None,
) -> float | None:
    if not stat:
        return None

    for key in (
        "statsValue",
        "statValue",
        "value",
    ):
        if key not in stat:
            continue

        value = safe_float(
            stat.get(key)
        )

        if value is not None:
            return value

    return None


def stat_metadata(
    stat: dict[str, Any] | None,
) -> dict[str, Any]:
    if not stat:
        return {
            "found": False,
            "statsId": None,
            "statsLabel": None,
        }

    return {
        "found": True,

        "statsId": (
            stat.get("statsId")
            or stat.get("statId")
            or stat.get("id")
        ),

        "statsLabel": (
            stat.get("statsLabel")
            or stat.get("statLabel")
            or stat.get("label")
        ),
    }


# =============================================================================
# Strict field extraction
# =============================================================================

def extract_verified_counting_stat(
    player: dict[str, Any],
    accepted_names: list[str],
) -> dict[str, Any]:
    """
    For verified counting stats:

    exact stat present -> use its value
    exact stat absent  -> 0

    This is deliberate because the NWSL Opta player endpoint commonly omits
    zero-valued stats from an individual player's stat array.
    """

    stat = find_exact_stat(
        player,
        accepted_names,
    )

    metadata = stat_metadata(
        stat
    )

    if stat is None:
        value = 0.0
    else:
        value = stat_value(
            stat
        )

        if value is None:
            value = 0.0

    return {
        "value": value,
        **metadata,
    }


def extract_component_values(
    player: dict[str, Any],
) -> dict[str, Any]:
    output = {}

    for concept, names in (
        INVOLVEMENT_STAT_FIELDS.items()
    ):
        output[concept] = (
            extract_verified_counting_stat(
                player,
                names,
            )
        )

    return output


def extract_supporting_values(
    player: dict[str, Any],
) -> dict[str, Any]:
    output = {}

    for concept, names in (
        SUPPORTING_STAT_FIELDS.items()
    ):
        output[concept] = (
            extract_verified_counting_stat(
                player,
                names,
            )
        )

    return output


# =============================================================================
# Build raw player records
# =============================================================================

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
        supporting[
            "minutes"
        ]["value"]
    )

    appearances = (
        supporting[
            "appearances"
        ]["value"]
    )

    if (
        minutes is not None
        and minutes > 0
    ):
        denominator = (
            minutes / 90.0
        )

        rate_basis = "per90"

    elif (
        appearances is not None
        and appearances > 0
    ):
        denominator = (
            appearances
        )

        rate_basis = (
            "perAppearance"
        )

    else:
        denominator = None
        rate_basis = "noMinutes"

    rates = {}

    for concept, info in (
        components.items()
    ):
        raw_value = info.get(
            "value"
        )

        if (
            denominator is None
            or denominator <= 0
        ):
            rates[concept] = None

        else:
            rates[concept] = round(
                float(raw_value or 0)
                / denominator,
                4,
            )

    team = player_team(
        player
    )

    return {
        "playerId": player.get(
            "playerId"
        ),

        "providerId": player.get(
            "providerId"
        ),

        "name": player_name(
            player
        ),

        "team": team,

        "position": (
            normalize_position(
                player
            )
        ),

        "minutes": minutes,

        "appearances": (
            appearances
        ),

        "starts": (
            supporting[
                "starts"
            ]["value"]
        ),

        "subOn": (
            supporting[
                "sub_on"
            ]["value"]
        ),

        "subOff": (
            supporting[
                "sub_off"
            ]["value"]
        ),

        "rateBasis": rate_basis,

        "components": (
            components
        ),

        "rates": rates,

        "supporting": (
            supporting
        ),
    }


# =============================================================================
# Normalization eligibility
# =============================================================================

def eligible_for_normalization(
    player: dict[str, Any],
) -> bool:
    """
    Low-minute players remain in the output, but they do not define the
    percentile population.

    This prevents a five-minute cameo from resetting the scale for an entire
    position group.
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
        (
            minutes is None
            or minutes <= 0
        )
        and appearances is not None
        and appearances >= 3
    ):
        return True

    return False


# =============================================================================
# Percentile normalization
# =============================================================================

def percentile_rank(
    value: float | None,
    population: list[float],
) -> float | None:
    if value is None:
        return None

    clean = sorted(
        float(x)
        for x in population
        if x is not None
        and math.isfinite(
            float(x)
        )
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


def build_position_populations(
    players: list[dict[str, Any]],
) -> dict[
    str,
    dict[str, list[float]],
]:
    populations = defaultdict(
        lambda: defaultdict(
            list
        )
    )

    for player in players:
        position = player.get(
            "position"
        )

        if position not in {
            "GK",
            "DEF",
            "MID",
            "FOR",
        }:
            continue

        if not eligible_for_normalization(
            player
        ):
            continue

        for concept, value in (
            player
            .get(
                "rates",
                {},
            )
            .items()
        ):
            if value is None:
                continue

            populations[
                position
            ][concept].append(
                float(value)
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

    weights = (
        POSITION_WEIGHTS.get(
            position
        )
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
            .get(
                "rates",
                {},
            )
            .get(concept)
        )

        population = (
            populations
            .get(
                position,
                {},
            )
            .get(
                concept,
                [],
            )
        )

        percentile = (
            percentile_rank(
                rate,
                population,
            )
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

    if (
        available_weight > 0
    ):
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
    Separate reliability signal.

    900 minutes ~= full confidence.

    This is NOT baked into the raw involvement rating yet.
    """

    minutes = player.get(
        "minutes"
    )

    appearances = player.get(
        "appearances"
    )

    if (
        minutes is not None
        and minutes > 0
    ):
        return round(
            clamp(
                minutes
                / 900.0
                * 100.0,
                0.0,
                100.0,
            ),
            1,
        )

    if (
        appearances is not None
        and appearances > 0
    ):
        return round(
            clamp(
                appearances
                / 10.0
                * 100.0,
                0.0,
                100.0,
            ),
            1,
        )

    return 0.0


# =============================================================================
# More useful confidence-adjusted inspection score
# =============================================================================
#
# This is NOT the live Decision component.
#
# It is simply included so we can inspect established players without tiny
# cameo samples dominating the top of every list.
#
# We shrink low-confidence involvement ratings toward a neutral 50.
#
# Example:
#
#   confidence 100 -> full rating
#   confidence  50 -> halfway between rating and 50
#   confidence   0 -> 50
#
# =============================================================================

def confidence_adjusted_rating(
    rating: float | None,
    confidence: float,
) -> float | None:
    if rating is None:
        return None

    weight = clamp(
        confidence / 100.0,
        0.0,
        1.0,
    )

    adjusted = (
        50.0
        + (
            rating - 50.0
        )
        * weight
    )

    return round(
        adjusted,
        1,
    )


# =============================================================================
# Field-resolution audit
# =============================================================================

def build_stat_resolution(
    raw_players: list[dict[str, Any]],
) -> dict[str, Any]:
    output = {}

    for concept, names in (
        INVOLVEMENT_STAT_FIELDS.items()
    ):
        labels = defaultdict(
            int
        )

        matched = 0
        absent = 0

        for player in raw_players:
            stat = find_exact_stat(
                player,
                names,
            )

            if stat is None:
                absent += 1
                continue

            matched += 1

            label = str(
                stat.get(
                    "statsLabel"
                )
                or stat.get(
                    "statsId"
                )
                or "UNKNOWN"
            )

            labels[
                label
            ] += 1

        output[concept] = {
            "playersMatched": matched,
            "playersAbsentTreatedAsZero": (
                absent
            ),

            "acceptedFields": names,

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


def build_supporting_resolution(
    raw_players: list[dict[str, Any]],
) -> dict[str, Any]:
    output = {}

    for concept, names in (
        SUPPORTING_STAT_FIELDS.items()
    ):
        labels = defaultdict(
            int
        )

        matched = 0
        absent = 0

        for player in raw_players:
            stat = find_exact_stat(
                player,
                names,
            )

            if stat is None:
                absent += 1
                continue

            matched += 1

            label = str(
                stat.get(
                    "statsLabel"
                )
                or stat.get(
                    "statsId"
                )
                or "UNKNOWN"
            )

            labels[
                label
            ] += 1

        output[concept] = {
            "playersMatched": (
                matched
            ),

            "playersAbsentTreatedAsZero": (
                absent
            ),

            "acceptedFields": names,

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
            if (
                player.get(
                    "position"
                ) == position
                and player.get(
                    "involvementRating"
                ) is not None
            )
        ]

        ratings = [
            player[
                "involvementRating"
            ]
            for player in (
                position_players
            )
        ]

        raw_sorted = sorted(
            position_players,
            key=lambda player: (
                player.get(
                    "involvementRating"
                )
                if player.get(
                    "involvementRating"
                )
                is not None
                else -1
            ),
            reverse=True,
        )

        trusted_sorted = sorted(
            position_players,
            key=lambda player: (
                player.get(
                    "confidenceAdjustedRating"
                )
                if player.get(
                    "confidenceAdjustedRating"
                )
                is not None
                else -1
            ),
            reverse=True,
        )

        established = [
            player
            for player in (
                position_players
            )
            if (
                player.get(
                    "minutes",
                    0,
                )
                or 0
            ) >= 180
        ]

        established_sorted = sorted(
            established,
            key=lambda player: (
                player.get(
                    "involvementRating"
                )
                if player.get(
                    "involvementRating"
                )
                is not None
                else -1
            ),
            reverse=True,
        )

        def compact(
            player: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "name": (
                    player.get(
                        "name"
                    )
                ),

                "team": (
                    player
                    .get(
                        "team",
                        {},
                    )
                    .get(
                        "shortName"
                    )
                ),

                "rating": (
                    player.get(
                        "involvementRating"
                    )
                ),

                "confidenceAdjustedRating": (
                    player.get(
                        "confidenceAdjustedRating"
                    )
                ),

                "minutes": (
                    player.get(
                        "minutes"
                    )
                ),

                "appearances": (
                    player.get(
                        "appearances"
                    )
                ),

                "sampleConfidence": (
                    player.get(
                        "sampleConfidence"
                    )
                ),
            }

        output[position] = {
            "playerCount": len(
                position_players
            ),

            "establishedPlayerCount": (
                len(established)
            ),

            "medianRating": (
                round(
                    median(
                        ratings
                    ),
                    1,
                )
                if ratings
                else None
            ),

            "topRawPlayers": [
                compact(player)
                for player in (
                    raw_sorted[:15]
                )
            ],

            "topEstablishedPlayers": [
                compact(player)
                for player in (
                    established_sorted[
                        :15
                    ]
                )
            ],

            "topConfidenceAdjustedPlayers": [
                compact(player)
                for player in (
                    trusted_sorted[:15]
                )
            ],
        }

    return output


# =============================================================================
# Validation
# =============================================================================

def validate_resolution(
    stat_resolution: dict[str, Any],
) -> None:
    """
    Fail loudly if an unexpected Opta label slips into production.
    """

    for concept, info in (
        stat_resolution.items()
    ):
        accepted_normalized = {
            normalize_text(
                name
            )
            for name in info[
                "acceptedFields"
            ]
        }

        for label in (
            info[
                "labelsUsed"
            ].keys()
        ):
            if (
                normalize_text(
                    label
                )
                not in accepted_normalized
            ):
                raise RuntimeError(
                    "Unexpected Opta stat mapping: "
                    f"{concept} -> {label}"
                )


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    raw_players = (
        fetch_players()
    )

    players = [
        build_raw_player(
            player
        )
        for player in (
            raw_players
        )
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

        confidence = (
            calculate_sample_confidence(
                player
            )
        )

        player[
            "sampleConfidence"
        ] = confidence

        player[
            "confidenceAdjustedRating"
        ] = (
            confidence_adjusted_rating(
                player.get(
                    "involvementRating"
                ),
                confidence,
            )
        )

    stat_resolution = (
        build_stat_resolution(
            raw_players
        )
    )

    supporting_resolution = (
        build_supporting_resolution(
            raw_players
        )
    )

    validate_resolution(
        stat_resolution
    )

    position_summary = (
        build_position_summary(
            players
        )
    )

    players.sort(
        key=lambda player: (
            player.get(
                "confidenceAdjustedRating"
            )
            if player.get(
                "confidenceAdjustedRating"
            )
            is not None
            else -1
        ),
        reverse=True,
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

        "playerCount": (
            len(players)
        ),

        "modelVersion": (
            "nwsl-involvement-v1.1"
        ),

        "normalization": (
            "within-position percentile "
            "of per-90 rates"
        ),

        "minimumNormalizationSample": (
            "180 minutes"
        ),

        "missingStatTreatment": (
            "verified counting stat absent "
            "from player payload = zero"
        ),

        "statMatching": (
            "exact normalized Opta label/ID "
            "matching only; no substring fallback"
        ),

        "notes": [
            (
                "Involvement Rating remains separate "
                "from Fixture, Fantasy Form, Decision "
                "Rating, Visionary, and DGW value."
            ),

            (
                "Raw Involvement Rating is not sample "
                "confidence adjusted."
            ),

            (
                "Confidence Adjusted Rating is included "
                "for inspection only and shrinks tiny "
                "samples toward neutral 50."
            ),

            (
                "Players under 180 minutes remain in "
                "the output but do not define the "
                "position percentile distributions."
            ),

            (
                "Recent match role/activity is not "
                "included yet."
            ),
        ],
    }

    output = {
        "metadata": (
            metadata
        ),

        "weights": (
            POSITION_WEIGHTS
        ),

        "statResolution": (
            stat_resolution
        ),

        "supportingStatResolution": (
            supporting_resolution
        ),

        "positionSummary": (
            position_summary
        ),

        "players": (
            players
        ),
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
    print("NWSL INVOLVEMENT V1.1")
    print("=" * 88)

    print(
        "Players:",
        len(players),
    )

    print()
    print("INVOLVEMENT STAT RESOLUTION")
    print("-" * 88)

    for concept, result in (
        stat_resolution.items()
    ):
        print(
            f"{concept:25} "
            f"matched="
            f"{result['playersMatched']:3} "
            f"zero="
            f"{result['playersAbsentTreatedAsZero']:3} "
            f"{result['labelsUsed']}"
        )

    print()
    print("SUPPORTING STAT RESOLUTION")
    print("-" * 88)

    for concept, result in (
        supporting_resolution.items()
    ):
        print(
            f"{concept:25} "
            f"matched="
            f"{result['playersMatched']:3} "
            f"zero="
            f"{result['playersAbsentTreatedAsZero']:3} "
            f"{result['labelsUsed']}"
        )

    print()
    print("TOP ESTABLISHED PLAYERS BY POSITION")
    print("-" * 88)

    for position, summary in (
        position_summary.items()
    ):
        print()
        print(position)

        for row in (
            summary[
                "topEstablishedPlayers"
            ][:10]
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
