from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


BASE_URL = "https://api-sdp.nwslsoccer.com"

# Gotham FC 1-1 Portland Thorns
# August 28, 2026
MATCH_UUID = "4f89a6e4705c470f8afb28aff5c5ad06"

# SDP normally uses this namespaced form internally.
MATCH_ID = f"nwsl::Football_Match::{MATCH_UUID}"

OUTPUT_PATH = Path("nwsl_opta_match_audit.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 NWSL fantasy match audit",
    "Accept": "application/json,text/plain,*/*",
    "Referer": (
        "https://www.nwslsoccer.com/"
        f"match/{MATCH_UUID}/gotham-fc-vs-portland-thorns/"
    ),
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

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


def candidate_match_bases() -> list[str]:
    """
    Try the common SDP match URL forms.

    We already know the public page UUID. The site may accept either:
      /matches/<namespaced Football_Match id>
    or
      /matches/<raw uuid>

    The diagnostic discovers which one works.
    """

    encoded_namespaced = quote(
        MATCH_ID,
        safe=":",
    )

    return [
        (
            f"{BASE_URL}/v1/nwsl/football/"
            f"matches/{encoded_namespaced}"
        ),
        (
            f"{BASE_URL}/v1/nwsl/football/"
            f"matches/{MATCH_UUID}"
        ),
    ]


def discover_working_base() -> tuple[str, Any]:
    """
    Use /header as the probe because Firefox confirmed that endpoint exists.
    """

    errors = []

    for base in candidate_match_bases():
        try:
            payload = request_json(
                f"{base}/header",
                {"locale": "en-US"},
            )

            print()
            print("SUCCESSFUL MATCH BASE:")
            print(base)

            return base, payload

        except Exception as exc:
            errors.append(
                {
                    "base": base,
                    "error": str(exc),
                }
            )

    raise RuntimeError(
        "Could not discover a working match API base.\n"
        + json.dumps(errors, indent=2)
    )


# ---------------------------------------------------------------------------
# Endpoint fetchers
# ---------------------------------------------------------------------------

def fetch_simple_endpoint(
    base: str,
    endpoint: str,
) -> Any:
    return request_json(
        f"{base}/{endpoint}",
        {"locale": "en-US"},
    )


def extract_feed_items(payload: Any) -> list[dict[str, Any]]:
    """
    The match-feed response may be:
      - a list directly
      - {"feed": [...]}
      - {"events": [...]}
      - {"items": [...]}
      - {"content": [...]}

    Keep the diagnostic defensive.
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


def payload_has_more_pages(
    payload: Any,
    page: int,
    item_count: int,
) -> bool:
    """
    Look for explicit pagination metadata first.

    If there is none, a zero-length next page will stop the loop.
    """

    if not isinstance(payload, dict):
        return item_count > 0

    # Explicit booleans
    for key in (
        "hasNext",
        "hasNextPage",
        "hasMore",
        "more",
    ):
        value = payload.get(key)

        if isinstance(value, bool):
            return value

    # Page totals
    for key in (
        "totalPages",
        "pages",
        "pageCount",
    ):
        value = payload.get(key)

        if isinstance(value, int):
            return page < value

    # Current / next page values
    next_page = payload.get("nextPage")

    if next_page is not None:
        return bool(next_page)

    pagination = payload.get("pagination")

    if isinstance(pagination, dict):
        for key in (
            "hasNext",
            "hasNextPage",
            "hasMore",
        ):
            value = pagination.get(key)

            if isinstance(value, bool):
                return value

        total_pages = (
            pagination.get("totalPages")
            or pagination.get("pages")
        )

        if isinstance(total_pages, int):
            return page < total_pages

    # No metadata. Continue until a page returns no events.
    return item_count > 0


def fetch_all_feed_pages(
    base: str,
    max_pages: int = 50,
) -> dict[str, Any]:
    all_items = []
    page_summaries = []

    seen_signatures = set()

    for page in range(1, max_pages + 1):
        payload = request_json(
            f"{base}/feed",
            {
                "locale": "en-US",
                "page": page,
            },
        )

        items = extract_feed_items(payload)

        signature = json.dumps(
            items[:5],
            sort_keys=True,
            default=str,
        )

        print(
            f"Feed page {page}: "
            f"{len(items)} event rows"
        )

        # Prevent a badly behaved endpoint from returning page 1 forever.
        if signature in seen_signatures and items:
            print(
                "Repeated feed page detected. "
                "Stopping pagination."
            )
            break

        seen_signatures.add(signature)

        page_summaries.append(
            {
                "page": page,
                "itemCount": len(items),
                "topLevelType": type(payload).__name__,
                "topLevelKeys": (
                    sorted(payload.keys())
                    if isinstance(payload, dict)
                    else []
                ),
            }
        )

        if not items:
            break

        all_items.extend(items)

        if not payload_has_more_pages(
            payload,
            page,
            len(items),
        ):
            break

    return {
        "events": all_items,
        "pages": page_summaries,
    }


# ---------------------------------------------------------------------------
# Lineups
# ---------------------------------------------------------------------------

def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def identify_player_dict(row: dict[str, Any]) -> bool:
    return bool(
        row.get("playerId")
        or (
            isinstance(row.get("providerId"), str)
            and "Player:" in row["providerId"]
        )
    )


def extract_lineup_players(
    lineups_payload: Any,
) -> list[dict[str, Any]]:
    players = []

    seen = set()

    for row in walk_dicts(lineups_payload):
        if not identify_player_dict(row):
            continue

        identity = (
            row.get("providerId")
            or row.get("playerId")
        )

        if identity in seen:
            continue

        seen.add(identity)

        players.append(row)

    return players


def infer_lineup_statuses(
    lineups_payload: Any,
) -> dict[str, str]:
    """
    Record whether a player appeared under a `fielded` or `benched`
    branch in the lineup JSON.
    """

    statuses = {}

    def recurse(
        value: Any,
        current_status: str | None = None,
    ):
        if isinstance(value, dict):
            if identify_player_dict(value):
                identity = (
                    value.get("providerId")
                    or value.get("playerId")
                )

                if identity and current_status:
                    statuses[identity] = current_status

            for key, child in value.items():
                next_status = current_status

                normalized = str(key).lower()

                if normalized == "fielded":
                    next_status = "fielded"

                elif normalized == "benched":
                    next_status = "benched"

                recurse(child, next_status)

        elif isinstance(value, list):
            for child in value:
                recurse(child, current_status)

    recurse(lineups_payload)

    return statuses


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


def summarize_lineups(
    payload: Any,
) -> dict[str, Any]:
    players = extract_lineup_players(payload)

    statuses = infer_lineup_statuses(payload)

    rows = []

    for player in players:
        identity = (
            player.get("providerId")
            or player.get("playerId")
        )

        rows.append(
            {
                "name": player_display_name(player),
                "providerId": player.get(
                    "providerId"
                ),
                "playerId": player.get(
                    "playerId"
                ),
                "roleLabel": player.get(
                    "roleLabel"
                ),
                "role": player.get("role"),
                "bibNumber": player.get(
                    "bibNumber"
                ),
                "isGoalkeeper": player.get(
                    "isGoalkeeper"
                ),
                "isCaptain": player.get(
                    "isCaptain"
                ),
                "lineupStatus": statuses.get(
                    identity
                ),
                "embeddedEvents": (
                    player.get("events")
                    if isinstance(
                        player.get("events"),
                        list,
                    )
                    else []
                ),
            }
        )

    return {
        "playerCount": len(rows),
        "players": rows,
    }


# ---------------------------------------------------------------------------
# Feed analysis
# ---------------------------------------------------------------------------

def get_event_type(
    event: dict[str, Any],
) -> str:
    return str(
        event.get("type")
        or event.get("eventType")
        or event.get("label")
        or "UNKNOWN"
    )


def get_event_player_candidates(
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = []

    for row in walk_dicts(event):
        provider = row.get("providerId")
        player_id = row.get("playerId")

        if (
            isinstance(provider, str)
            and "Player:" in provider
        ) or player_id:
            candidates.append(row)

    return candidates


def analyze_feed(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    type_counts = Counter()
    field_counts = Counter()

    event_type_fields = defaultdict(Counter)

    player_event_counts = defaultdict(Counter)

    player_names = {}

    interesting_examples = defaultdict(list)

    for event in events:
        event_type = get_event_type(event)

        type_counts[event_type] += 1

        for key in event.keys():
            field_counts[key] += 1
            event_type_fields[event_type][key] += 1

        candidates = get_event_player_candidates(
            event
        )

        for player in candidates:
            identity = (
                player.get("providerId")
                or player.get("playerId")
            )

            if not identity:
                continue

            player_event_counts[
                identity
            ][event_type] += 1

            player_names.setdefault(
                identity,
                player_display_name(player),
            )

        lower_blob = json.dumps(
            event,
            ensure_ascii=False,
        ).lower()

        keyword_buckets = {
            "shot": [
                "shot",
                "effort on goal",
            ],
            "cross": [
                "cross",
            ],
            "tackle": [
                "tackle",
            ],
            "interception": [
                "interception",
            ],
            "recovery": [
                "recovery",
            ],
            "clearance": [
                "clearance",
            ],
            "block": [
                "block",
            ],
            "assist_or_key_pass": [
                "assist",
                "key pass",
                "chance created",
            ],
            "substitution": [
                "substitution",
                "substitute",
                "subbed",
            ],
        }

        for bucket, keywords in (
            keyword_buckets.items()
        ):
            if any(
                keyword in lower_blob
                for keyword in keywords
            ):
                if len(
                    interesting_examples[bucket]
                ) < 10:
                    interesting_examples[
                        bucket
                    ].append(event)

    type_field_output = {}

    for event_type, counts in (
        event_type_fields.items()
    ):
        type_field_output[event_type] = dict(
            counts.most_common()
        )

    player_output = []

    for identity, counts in (
        player_event_counts.items()
    ):
        player_output.append(
            {
                "identity": identity,
                "name": player_names.get(
                    identity
                ),
                "eventCounts": dict(
                    counts.most_common()
                ),
                "totalAttributedEvents": sum(
                    counts.values()
                ),
            }
        )

    player_output.sort(
        key=lambda row: row[
            "totalAttributedEvents"
        ],
        reverse=True,
    )

    return {
        "eventCount": len(events),
        "eventTypeCounts": dict(
            type_counts.most_common()
        ),
        "allTopLevelFields": dict(
            field_counts.most_common()
        ),
        "fieldsByEventType": type_field_output,
        "playerAttributedEvents": (
            player_output
        ),
        "interestingExamples": dict(
            interesting_examples
        ),
    }


# ---------------------------------------------------------------------------
# Team stats
# ---------------------------------------------------------------------------

def extract_team_stat_rows(
    payload: Any,
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [
            row
            for row in payload
            if isinstance(row, dict)
        ]

    if isinstance(payload, dict):
        for key in (
            "stats",
            "teamStats",
            "items",
            "data",
            "content",
        ):
            value = payload.get(key)

            if isinstance(value, list):
                return [
                    row
                    for row in value
                    if isinstance(row, dict)
                ]

    return []


def summarize_teamstats(
    payload: Any,
) -> list[dict[str, Any]]:
    rows = extract_team_stat_rows(payload)

    output = []

    for row in rows:
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


# ---------------------------------------------------------------------------
# Cross-check feed vs team totals
# ---------------------------------------------------------------------------

def classify_feed_events(
    events: list[dict[str, Any]],
) -> dict[str, int]:
    """
    Very conservative counts.

    These are NOT intended to become the model yet.
    They're just sanity checks to see whether the public feed
    contains enough raw events to reproduce official team totals.
    """

    counts = Counter()

    for event in events:
        event_type = get_event_type(
            event
        ).lower()

        blob = json.dumps(
            event,
            ensure_ascii=False,
        ).lower()

        if event_type == "shot" or (
            '"label": "shot"' in blob
        ):
            counts["shots"] += 1

        shot_result = str(
            event.get("shotResult")
            or ""
        ).lower()

        if "ontarget" in shot_result:
            counts["shots_on_target"] += 1

        if "tackle" in event_type:
            counts["tackle_events"] += 1

        if "interception" in event_type:
            counts["interception_events"] += 1

        if "clearance" in event_type:
            counts["clearance_events"] += 1

        if "recovery" in event_type:
            counts["recovery_events"] += 1

        if "cross" in event_type:
            counts["cross_events"] += 1

    return dict(counts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 88)
    print("NWSL OPTA MATCH-LEVEL AUDIT")
    print("=" * 88)

    base, header = discover_working_base()

    print()
    print("Fetching lineups...")

    lineups = fetch_simple_endpoint(
        base,
        "lineups",
    )

    print()
    print("Fetching team stats...")

    teamstats = fetch_simple_endpoint(
        base,
        "teamstats",
    )

    print()
    print("Fetching match summary...")

    try:
        summary = fetch_simple_endpoint(
            base,
            "summary",
        )
    except Exception as exc:
        print(
            "Summary endpoint unavailable:",
            exc,
        )

        summary = None

    print()
    print("Fetching ALL feed pages...")

    feed_result = fetch_all_feed_pages(
        base
    )

    events = feed_result["events"]

    lineup_summary = summarize_lineups(
        lineups
    )

    feed_analysis = analyze_feed(
        events
    )

    teamstats_summary = (
        summarize_teamstats(
            teamstats
        )
    )

    feed_sanity = classify_feed_events(
        events
    )

    audit = {
        "metadata": {
            "source": (
                "NWSL public SDP / Opta API"
            ),
            "matchUuid": MATCH_UUID,
            "matchId": MATCH_ID,
            "workingBaseUrl": base,
            "feedPageCount": len(
                feed_result["pages"]
            ),
            "feedEventCount": len(
                events
            ),
            "lineupPlayerCount": (
                lineup_summary[
                    "playerCount"
                ]
            ),
        },

        "header": header,

        "lineups": lineup_summary,

        "feedPagination": (
            feed_result["pages"]
        ),

        "feedAnalysis": feed_analysis,

        "feedSanityCounts": (
            feed_sanity
        ),

        "teamStats": (
            teamstats_summary
        ),

        "rawSummary": summary,

        # Include raw endpoint payloads for this ONE match.
        # Very useful while reverse engineering.
        "raw": {
            "lineups": lineups,
            "teamstats": teamstats,
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

    print()
    print("=" * 88)
    print("AUDIT SUMMARY")
    print("=" * 88)

    print(
        "Working API base:",
        base,
    )

    print(
        "Lineup players:",
        lineup_summary[
            "playerCount"
        ],
    )

    print(
        "Feed pages:",
        len(feed_result["pages"]),
    )

    print(
        "Feed events:",
        len(events),
    )

    print()
    print("EVENT TYPES")
    print("-" * 88)

    for event_type, count in (
        feed_analysis[
            "eventTypeCounts"
        ].items()
    ):
        print(
            f"{event_type:30} {count}"
        )

    print()
    print("SANITY COUNTS")
    print("-" * 88)

    for key, value in (
        feed_sanity.items()
    ):
        print(
            f"{key:30} {value}"
        )

    print()
    print("TEAM STATS")
    print("-" * 88)

    for row in teamstats_summary:
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
