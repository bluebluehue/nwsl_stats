from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://api-sdp.nwslsoccer.com"

MATCH_SEASON_ID = (
    "nwsl::Football_Season::fad050beee834db88fa9f2eb28ce5a5c"
)

MATCH_ID = (
    "nwsl::Football_Match::4f89a6e4705c470f8afb28aff5c5ad06"
)

MATCH_UUID = "4f89a6e4705c470f8afb28aff5c5ad06"

OUTPUT_PATH = Path("nwsl_opta_match_audit.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 NWSL fantasy match audit",
    "Accept": "application/json,text/plain,*/*",
    "Referer": (
        "https://www.nwslsoccer.com/"
        f"match/{MATCH_UUID}/gotham-fc-vs-portland-thorns/"
    ),
}


# ===========================================================================
# Exact endpoints confirmed in Firefox
# ===========================================================================

LINEUPS_URL = (
    f"{BASE_URL}/v1/nwsl/football/"
    f"seasons/{MATCH_SEASON_ID}/"
    f"matches/{MATCH_ID}/lineups"
)

FEED_URL = (
    f"{BASE_URL}/v1/nwsl/football/"
    f"seasons/{MATCH_SEASON_ID}/"
    f"matches/{MATCH_ID}/feed"
)

TEAMSTATS_URL = (
    f"{BASE_URL}/v1/nwsl/football/"
    f"seasons/{MATCH_SEASON_ID}/"
    f"match/{MATCH_ID}/teamstats"
)


# ===========================================================================
# HTTP
# ===========================================================================

def request_json(
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    print()
    print("=" * 88)
    print(f"GET {url}")
    print(f"PARAMS {params or {}}")

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

    preview = response.text[:500].replace("\n", " ")
    print("BODY PREVIEW:", preview)

    response.raise_for_status()

    return response.json()


# ===========================================================================
# Feed
# ===========================================================================

def extract_feed_items(
    payload: Any,
) -> list[dict[str, Any]]:
    """
    Firefox showed the feed response as a JSON list, but keep this defensive
    in case some pages are wrapped differently.
    """

    if isinstance(payload, list):
        return [
            row
            for row in payload
            if isinstance(row, dict)
        ]

    if not isinstance(payload, dict):
        return []

    for key in (
        "feed",
        "events",
        "items",
        "content",
        "results",
        "data",
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return [
                row
                for row in value
                if isinstance(row, dict)
            ]

        if isinstance(value, dict):
            nested = extract_feed_items(value)

            if nested:
                return nested

    return []


def event_signature(
    events: list[dict[str, Any]],
) -> str:
    """
    Used to detect an endpoint that ignores page= and returns the same page
    repeatedly.
    """

    return json.dumps(
        events,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def fetch_all_feed_pages(
    max_pages: int = 100,
) -> dict[str, Any]:
    all_events = []
    page_summaries = []

    seen_signatures = set()

    for page in range(1, max_pages + 1):
        payload = request_json(
            FEED_URL,
            {
                "locale": "en-US",
                "page": page,
            },
        )

        events = extract_feed_items(payload)

        print(
            f"Feed page {page}: "
            f"{len(events)} events"
        )

        page_summaries.append(
            {
                "page": page,
                "eventCount": len(events),
                "payloadType": type(payload).__name__,
                "topLevelKeys": (
                    sorted(payload.keys())
                    if isinstance(payload, dict)
                    else []
                ),
            }
        )

        # Normal end of pagination.
        if not events:
            print(
                f"Page {page} returned no events. "
                "Pagination complete."
            )
            break

        signature = event_signature(events)

        if signature in seen_signatures:
            print(
                f"Page {page} repeated a previously "
                "seen response. Stopping."
            )

            page_summaries[-1][
                "repeatedPreviousPage"
            ] = True

            break

        seen_signatures.add(signature)

        all_events.extend(events)

    return {
        "events": all_events,
        "pages": page_summaries,
    }


# ===========================================================================
# Generic JSON walker
# ===========================================================================

def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


# ===========================================================================
# Lineups
# ===========================================================================

def is_player_dict(
    row: dict[str, Any],
) -> bool:
    provider = row.get("providerId")
    player_id = row.get("playerId")

    return bool(
        player_id
        or (
            isinstance(provider, str)
            and "Player:" in provider
        )
    )


def player_display_name(
    player: dict[str, Any],
) -> str:
    for key in (
        "displayName",
        "shortName",
        "shirtName",
        "mediaName",
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

    return combined or str(
        player.get("playerId")
        or player.get("providerId")
        or "Unknown"
    )


def extract_lineup_statuses(
    payload: Any,
) -> dict[str, str]:
    statuses = {}

    def recurse(
        value: Any,
        status: str | None = None,
        team: str | None = None,
    ):
        if isinstance(value, dict):
            current_team = team

            if value.get("shortName"):
                current_team = str(
                    value.get("shortName")
                )

            if is_player_dict(value):
                identity = (
                    value.get("providerId")
                    or value.get("playerId")
                )

                if identity:
                    statuses[identity] = {
                        "status": status,
                        "team": current_team,
                    }

            for key, child in value.items():
                next_status = status

                normalized = str(key).lower()

                if normalized == "fielded":
                    next_status = "fielded"

                elif normalized == "benched":
                    next_status = "benched"

                recurse(
                    child,
                    next_status,
                    current_team,
                )

        elif isinstance(value, list):
            for child in value:
                recurse(
                    child,
                    status,
                    team,
                )

    recurse(payload)

    return statuses


def summarize_lineups(
    payload: Any,
) -> dict[str, Any]:
    status_lookup = extract_lineup_statuses(
        payload
    )

    players = []
    seen = set()

    for row in walk_dicts(payload):
        if not is_player_dict(row):
            continue

        identity = (
            row.get("providerId")
            or row.get("playerId")
        )

        if not identity:
            continue

        if identity in seen:
            continue

        seen.add(identity)

        status_info = status_lookup.get(
            identity,
            {},
        )

        embedded_events = row.get("events")

        if not isinstance(
            embedded_events,
            list,
        ):
            embedded_events = []

        players.append(
            {
                "name": player_display_name(
                    row
                ),
                "providerId": row.get(
                    "providerId"
                ),
                "playerId": row.get(
                    "playerId"
                ),
                "team": status_info.get(
                    "team"
                ),
                "lineupStatus": (
                    status_info.get(
                        "status"
                    )
                ),
                "roleLabel": row.get(
                    "roleLabel"
                ),
                "role": row.get("role"),
                "bibNumber": row.get(
                    "bibNumber"
                ),
                "isGoalkeeper": row.get(
                    "isGoalkeeper"
                ),
                "isCaptain": row.get(
                    "isCaptain"
                ),
                "embeddedEventCount": (
                    len(embedded_events)
                ),
                "embeddedEvents": (
                    embedded_events
                ),
            }
        )

    return {
        "playerCount": len(players),
        "fieldedCount": sum(
            1
            for row in players
            if row["lineupStatus"]
            == "fielded"
        ),
        "benchedCount": sum(
            1
            for row in players
            if row["lineupStatus"]
            == "benched"
        ),
        "players": players,
    }


# ===========================================================================
# Feed analysis
# ===========================================================================

def event_type(
    event: dict[str, Any],
) -> str:
    return str(
        event.get("type")
        or event.get("eventType")
        or event.get("label")
        or "UNKNOWN"
    )


def analyze_feed(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    type_counts = Counter()

    top_level_field_counts = Counter()

    fields_by_type = defaultdict(
        Counter
    )

    value_catalog = defaultdict(
        Counter
    )

    interesting_examples = defaultdict(
        list
    )

    keywords = {
        "shot": [
            "shot",
            "effort on goal",
        ],
        "key_pass": [
            "key pass",
            "attempt assist",
            "chance created",
        ],
        "cross": [
            "cross",
        ],
        "dribble": [
            "dribble",
            "take on",
            "take-on",
        ],
        "tackle": [
            "tackle",
        ],
        "interception": [
            "interception",
        ],
        "clearance": [
            "clearance",
        ],
        "block": [
            "block",
        ],
        "recovery": [
            "recovery",
        ],
        "substitution": [
            "substitution",
            "substitute",
        ],
    }

    for event in events:
        e_type = event_type(event)

        type_counts[e_type] += 1

        for key, value in event.items():
            top_level_field_counts[
                key
            ] += 1

            fields_by_type[
                e_type
            ][key] += 1

            # Keep a small catalog of common scalar values.
            if isinstance(
                value,
                (
                    str,
                    int,
                    float,
                    bool,
                    type(None),
                ),
            ):
                value_catalog[
                    key
                ][str(value)] += 1

        blob = json.dumps(
            event,
            ensure_ascii=False,
        ).lower()

        for bucket, terms in (
            keywords.items()
        ):
            if any(
                term in blob
                for term in terms
            ):
                if (
                    len(
                        interesting_examples[
                            bucket
                        ]
                    )
                    < 12
                ):
                    interesting_examples[
                        bucket
                    ].append(
                        event
                    )

    return {
        "eventCount": len(events),

        "eventTypeCounts": dict(
            type_counts.most_common()
        ),

        "topLevelFieldCounts": dict(
            top_level_field_counts.most_common()
        ),

        "fieldsByEventType": {
            key: dict(
                counts.most_common()
            )
            for key, counts
            in fields_by_type.items()
        },

        "scalarValueCatalog": {
            key: dict(
                counts.most_common(30)
            )
            for key, counts
            in value_catalog.items()
        },

        "interestingExamples": dict(
            interesting_examples
        ),
    }


# ===========================================================================
# Team stats
# ===========================================================================

def extract_teamstats(
    payload: Any,
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload

    elif isinstance(payload, dict):
        rows = []

        for key in (
            "stats",
            "teamStats",
            "items",
            "content",
            "data",
        ):
            value = payload.get(key)

            if isinstance(
                value,
                list,
            ):
                rows = value
                break

    else:
        rows = []

    output = []

    for row in rows:
        if not isinstance(
            row,
            dict,
        ):
            continue

        output.append(
            {
                "statsId": row.get(
                    "statsId"
                ),
                "statsLabel": row.get(
                    "statsLabel"
                ),
                "statsValueHome": row.get(
                    "statsValueHome"
                ),
                "statsValueAway": row.get(
                    "statsValueAway"
                ),
            }
        )

    return output


# ===========================================================================
# Feed sanity counts
# ===========================================================================

def feed_sanity_counts(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = Counter()

    shot_results = Counter()

    for event in events:
        e_type = event_type(
            event
        ).lower()

        if e_type == "shot":
            counts["shot_events"] += 1

        shot_result = event.get(
            "shotResult"
        )

        if shot_result is not None:
            shot_results[
                str(shot_result)
            ] += 1

        blob = json.dumps(
            event,
            ensure_ascii=False,
        ).lower()

        if "tackle" in blob:
            counts[
                "events_containing_tackle"
            ] += 1

        if "interception" in blob:
            counts[
                "events_containing_interception"
            ] += 1

        if "clearance" in blob:
            counts[
                "events_containing_clearance"
            ] += 1

        if "recovery" in blob:
            counts[
                "events_containing_recovery"
            ] += 1

        if "cross" in blob:
            counts[
                "events_containing_cross"
            ] += 1

        if "dribble" in blob:
            counts[
                "events_containing_dribble"
            ] += 1

        if (
            "key pass" in blob
            or "attempt assist" in blob
        ):
            counts[
                "events_containing_key_pass"
            ] += 1

    return {
        "counts": dict(counts),
        "shotResults": dict(
            shot_results
        ),
    }


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    print("=" * 88)
    print("NWSL OPTA MATCH-LEVEL AUDIT")
    print("=" * 88)

    print()
    print("Match season:")
    print(MATCH_SEASON_ID)

    print()
    print("Match:")
    print(MATCH_ID)

    # ------------------------------------------------------------------
    # Lineups
    # ------------------------------------------------------------------

    print()
    print("FETCHING LINEUPS")

    lineups_raw = request_json(
        LINEUPS_URL,
        {
            "locale": "en-US",
        },
    )

    lineups = summarize_lineups(
        lineups_raw
    )

    # ------------------------------------------------------------------
    # Team stats
    # ------------------------------------------------------------------

    print()
    print("FETCHING TEAM STATS")

    teamstats_raw = request_json(
        TEAMSTATS_URL,
        {
            "locale": "en-US",
        },
    )

    teamstats = extract_teamstats(
        teamstats_raw
    )

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    print()
    print("FETCHING ALL FEED PAGES")

    feed_result = (
        fetch_all_feed_pages()
    )

    events = feed_result[
        "events"
    ]

    feed_analysis = analyze_feed(
        events
    )

    sanity = feed_sanity_counts(
        events
    )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    audit = {
        "metadata": {
            "source": (
                "NWSL public SDP / Opta API"
            ),
            "matchSeasonId": (
                MATCH_SEASON_ID
            ),
            "matchId": MATCH_ID,
            "matchUuid": MATCH_UUID,

            "lineupsUrl": (
                LINEUPS_URL
            ),
            "feedUrl": FEED_URL,
            "teamstatsUrl": (
                TEAMSTATS_URL
            ),

            "lineupPlayerCount": (
                lineups[
                    "playerCount"
                ]
            ),

            "fieldedPlayerCount": (
                lineups[
                    "fieldedCount"
                ]
            ),

            "benchedPlayerCount": (
                lineups[
                    "benchedCount"
                ]
            ),

            "feedPageCount": len(
                feed_result[
                    "pages"
                ]
            ),

            "feedEventCount": len(
                events
            ),

            "teamStatCount": len(
                teamstats
            ),
        },

        "lineups": lineups,

        "feedPagination": (
            feed_result[
                "pages"
            ]
        ),

        "feedAnalysis": (
            feed_analysis
        ),

        "feedSanity": sanity,

        "teamStats": teamstats,

        "raw": {
            "lineups": (
                lineups_raw
            ),
            "teamstats": (
                teamstats_raw
            ),
            "feed": events,
        },
    }

    OUTPUT_PATH.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------

    print()
    print("=" * 88)
    print("AUDIT COMPLETE")
    print("=" * 88)

    print(
        "Lineup players:",
        lineups[
            "playerCount"
        ],
    )

    print(
        "Fielded:",
        lineups[
            "fieldedCount"
        ],
    )

    print(
        "Benched:",
        lineups[
            "benchedCount"
        ],
    )

    print(
        "Feed pages:",
        len(
            feed_result[
                "pages"
            ]
        ),
    )

    print(
        "Feed events:",
        len(events),
    )

    print(
        "Team stat rows:",
        len(teamstats),
    )

    print()
    print("EVENT TYPES")
    print("-" * 88)

    for key, value in (
        feed_analysis[
            "eventTypeCounts"
        ].items()
    ):
        print(
            f"{key:32} {value}"
        )

    print()
    print("SHOT RESULTS")
    print("-" * 88)

    for key, value in (
        sanity[
            "shotResults"
        ].items()
    ):
        print(
            f"{key:32} {value}"
        )

    print()
    print("KEYWORD SANITY COUNTS")
    print("-" * 88)

    for key, value in (
        sanity[
            "counts"
        ].items()
    ):
        print(
            f"{key:40} {value}"
        )

    print()
    print("OFFICIAL TEAM STATS")
    print("-" * 88)

    for row in teamstats:
        print(
            f"{str(row.get('statsLabel')):35} "
            f"home={row.get('statsValueHome')} "
            f"away={row.get('statsValueAway')}"
        )

    print()
    print(
        f"Wrote {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
