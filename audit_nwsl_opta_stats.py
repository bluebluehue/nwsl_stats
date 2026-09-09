from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


BASE_URL = "https://api-sdp.nwslsoccer.com"
API_PREFIX = "/v1/nwsl/football"
LOCALE = "en-US"

DISCOVERY_OUTPUT = Path("nwsl_opta_discovery_audit.json")
STATS_OUTPUT = Path("nwsl_opta_stats_audit.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 NWSL fantasy stats discovery",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nwslsoccer.com/",
}


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


def normalize_text(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def request_json(
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"

    merged = {"locale": LOCALE}
    if params:
        merged.update(params)

    print()
    print("=" * 80)
    print(f"GET {url}")
    print(f"PARAMS {merged}")

    response = requests.get(
        url,
        params=merged,
        headers=HEADERS,
        timeout=30,
    )

    print(f"HTTP {response.status_code}")
    print(f"CONTENT-TYPE {response.headers.get('content-type')}")

    preview = response.text[:1000].replace("\n", " ")
    print(f"BODY PREVIEW {preview}")

    if response.status_code >= 400:
        raise requests.HTTPError(
            f"HTTP {response.status_code}",
            response=response,
        )

    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Response was not valid JSON: {exc}"
        ) from exc


def safe_probe(
    name: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "name": name,
        "path": path,
        "params": params or {},
        "ok": False,
        "status": None,
        "payload_type": None,
        "top_level_keys": [],
        "payload": None,
        "error": None,
    }

    try:
        payload = request_json(path, params)

        result["ok"] = True
        result["payload_type"] = type(payload).__name__

        if isinstance(payload, dict):
            result["top_level_keys"] = sorted(
                str(k) for k in payload.keys()
            )

        result["payload"] = payload

    except requests.HTTPError as exc:
        status = None

        if exc.response is not None:
            status = exc.response.status_code

        result["status"] = status
        result["error"] = str(exc)

    except Exception as exc:
        result["error"] = str(exc)

    return result


def flatten_dict(
    value: Any,
    prefix: str = "",
    depth: int = 0,
) -> list[tuple[str, Any]]:
    if depth > 6:
        return []

    out = []

    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.extend(flatten_dict(child, path, depth + 1))

    elif isinstance(value, list):
        for idx, child in enumerate(value[:100]):
            path = f"{prefix}[{idx}]"
            out.extend(flatten_dict(child, path, depth + 1))

    else:
        out.append((prefix, value))

    return out


def extract_candidate_season_ids(
    payload: Any,
) -> list[str]:
    found = set()

    # Internal season IDs appear to use the Football_Season namespace.
    pattern = re.compile(
        r"(nwsl::Football_Season::[A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )

    for path, value in flatten_dict(payload):
        for candidate in (
            str(value or ""),
            str(path or ""),
        ):
            match = pattern.search(candidate)
            if match:
                found.add(match.group(1))

    return sorted(found)


def inspect_possible_seasons(
    probes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = {}

    for probe in probes:
        if not probe.get("ok"):
            continue

        payload = probe.get("payload")

        for season_id in extract_candidate_season_ids(payload):
            row = candidates.setdefault(
                season_id,
                {
                    "season_id": season_id,
                    "found_in": [],
                    "2026_evidence": [],
                },
            )

            row["found_in"].append(probe["name"])

        # Also look for objects containing both "season" and a year.
        flat = flatten_dict(payload)

        for path, value in flat:
            text = f"{path} {value}"

            if "2026" not in text:
                continue

            lower_path = path.lower()

            if "season" in lower_path:
                for season_id in extract_candidate_season_ids(payload):
                    row = candidates.setdefault(
                        season_id,
                        {
                            "season_id": season_id,
                            "found_in": [],
                            "2026_evidence": [],
                        },
                    )

                    evidence = f"{path}={value}"

                    if evidence not in row["2026_evidence"]:
                        row["2026_evidence"].append(evidence)

    return list(candidates.values())


def discover_api_structure() -> dict[str, Any]:
    probes = []

    # ------------------------------------------------------------------
    # Broad discovery routes.
    #
    # We deliberately try several plausible forms because the public SDP
    # deployment is undocumented and may not expose all common SDP routes.
    # ------------------------------------------------------------------

    candidates = [
        (
            "football root",
            f"{API_PREFIX}",
            None,
        ),
        (
            "competitions",
            f"{API_PREFIX}/competitions",
            {"page": 1, "pageNumElement": 100},
        ),
        (
            "competitions simple",
            f"{API_PREFIX}/competitions",
            None,
        ),
        (
            "seasons",
            f"{API_PREFIX}/seasons",
            {"page": 1, "pageNumElement": 100},
        ),
        (
            "seasons simple",
            f"{API_PREFIX}/seasons",
            None,
        ),
        (
            "teams",
            f"{API_PREFIX}/teams",
            {"page": 1, "pageNumElement": 100},
        ),
        (
            "teams simple",
            f"{API_PREFIX}/teams",
            None,
        ),
    ]

    for name, path, params in candidates:
        probes.append(
            safe_probe(name, path, params)
        )

    seasons = inspect_possible_seasons(probes)

    result = {
        "base_url": BASE_URL,
        "api_prefix": API_PREFIX,
        "probes": probes,
        "candidate_seasons": seasons,
    }

    DISCOVERY_OUTPUT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return result


def extract_stat_rows(player: dict[str, Any]) -> list[dict[str, Any]]:
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
        if player.get(key):
            return str(player[key])

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

    full = f"{first} {last}".strip()

    return full or str(
        player.get("playerId")
        or player.get("id")
        or "Unknown Player"
    )


def player_team(player: dict[str, Any]) -> str:
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


def player_position(player: dict[str, Any]) -> str:
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


def find_players_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in (
            "players",
            "items",
            "content",
            "results",
            "data",
        ):
            value = payload.get(key)

            if isinstance(value, list):
                if value and isinstance(value[0], dict):
                    return value

            if isinstance(value, dict):
                nested = find_players_list(value)
                if nested:
                    return nested

    return []


def fetch_players_for_season(
    season_id: str,
) -> dict[str, Any]:
    encoded = quote(season_id, safe="")

    path = (
        f"{API_PREFIX}/seasons/"
        f"{encoded}/stats/players"
    )

    order_candidates = [
        "goals",
        "appearances",
        "minutes",
        "minutes-played",
        "total-points",
    ]

    for order_by in order_candidates:
        try:
            payload = request_json(
                path,
                {
                    "orderBy": order_by,
                    "direction": "desc",
                    "page": 1,
                    "pageNumElement": 500,
                },
            )

            players = find_players_list(payload)

            if players:
                return {
                    "season_id": season_id,
                    "order_by": order_by,
                    "payload": payload,
                    "players": players,
                }

        except Exception as exc:
            print(
                f"Player stats attempt failed "
                f"for {season_id} / {order_by}: {exc}"
            )

    return {}


def build_stats_audit(
    successful: dict[str, Any],
) -> dict[str, Any]:
    players = successful["players"]

    stat_catalog = {}
    player_examples = []
    position_examples = {}
    stat_counts = []

    for player in players:
        stats = extract_stat_rows(player)

        stat_counts.append(len(stats))

        pos = player_position(player)

        if pos and pos not in position_examples:
            position_examples[pos] = {
                "name": player_name(player),
                "team": player_team(player),
                "position": pos,
                "stats_count": len(stats),
                "sample_stats": stats[:40],
            }

        if len(player_examples) < 10:
            player_examples.append({
                "name": player_name(player),
                "team": player_team(player),
                "position": pos,
                "stats_count": len(stats),
                "top_level_keys": sorted(player.keys()),
            })

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

            row = stat_catalog.setdefault(
                stats_id,
                {
                    "statsId": stats_id,
                    "statsLabel": stats_label,
                    "exampleValue": stats_value,
                    "seenForPlayers": 0,
                },
            )

            row["seenForPlayers"] += 1

            if not row.get("statsLabel") and stats_label:
                row["statsLabel"] = stats_label

    target_matches = {}

    for concept, phrases in TARGET_CONCEPTS.items():
        matches = []

        for stats_id, row in stat_catalog.items():
            combined = normalize_text(
                f"{stats_id} {row.get('statsLabel') or ''}"
            )

            if any(
                normalize_text(phrase) in combined
                for phrase in phrases
            ):
                matches.append(row)

        target_matches[concept] = matches

    counts = stat_counts or [0]

    return {
        "metadata": {
            "source": "NWSL public SDP / Opta API",
            "season_id": successful["season_id"],
            "order_by": successful["order_by"],
            "player_count": len(players),
            "unique_stat_count": len(stat_catalog),
            "minimum_stats_per_player": min(counts),
            "maximum_stats_per_player": max(counts),
            "average_stats_per_player": round(
                sum(counts) / len(counts),
                2,
            ),
            "all_target_concepts_found": all(
                bool(v)
                for v in target_matches.values()
            ),
        },
        "target_involvement_concepts": target_matches,
        "position_examples": position_examples,
        "player_examples": player_examples,
        "all_available_stats": sorted(
            stat_catalog.values(),
            key=lambda row: (
                normalize_text(row.get("statsLabel")),
                normalize_text(row.get("statsId")),
            ),
        ),
    }


def main() -> int:
    print("Starting NWSL SDP discovery audit.")

    discovery = discover_api_structure()

    candidates = [
        row["season_id"]
        for row in discovery.get(
            "candidate_seasons",
            [],
        )
    ]

    print()
    print("=" * 80)
    print(
        f"DISCOVERED {len(candidates)} "
        f"Football_Season candidate IDs"
    )

    for season in candidates:
        print(season)

    if not candidates:
        print()
        print(
            "No Football_Season IDs were found "
            "from the discovery endpoints."
        )
        print()
        print(
            "This run is still useful. "
            "nwsl_opta_discovery_audit.json "
            "contains every successful and failed "
            "API probe plus response bodies."
        )
        print()
        print(
            "Commit that file and send it to me."
        )

        return 0

    # ------------------------------------------------------------------
    # Try all discovered seasons.
    #
    # We are looking for the one that actually exposes a current player
    # stats payload. If several work, preserve all attempts in discovery.
    # ------------------------------------------------------------------

    successful = None
    attempts = []

    for season_id in candidates:
        print()
        print(
            f"Trying player stats for season "
            f"{season_id}"
        )

        result = fetch_players_for_season(
            season_id
        )

        attempts.append({
            "season_id": season_id,
            "worked": bool(result),
            "player_count": (
                len(result.get("players", []))
                if result
                else 0
            ),
        })

        if result:
            successful = result
            break

    discovery["player_stats_attempts"] = attempts

    DISCOVERY_OUTPUT.write_text(
        json.dumps(
            discovery,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    if not successful:
        print()
        print(
            "Season IDs were discovered, "
            "but none returned player stats."
        )
        print(
            "Send me nwsl_opta_discovery_audit.json."
        )
        return 0

    audit = build_stats_audit(successful)

    STATS_OUTPUT.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    meta = audit["metadata"]

    print()
    print("=" * 80)
    print("NWSL OPTA STATS SUCCESS")
    print("=" * 80)
    print(f"Season ID: {meta['season_id']}")
    print(f"Players: {meta['player_count']}")
    print(
        f"Unique stats: "
        f"{meta['unique_stat_count']}"
    )
    print(
        f"Average stats/player: "
        f"{meta['average_stats_per_player']}"
    )
    print()

    for concept, matches in (
        audit[
            "target_involvement_concepts"
        ].items()
    ):
        mark = "YES" if matches else "NO"

        print(f"{mark:3}  {concept}")

        for row in matches[:5]:
            print(
                f"     {row.get('statsId')} "
                f"| {row.get('statsLabel')} "
                f"| example={row.get('exampleValue')}"
            )

    print()
    print(
        "All nine concepts found:",
        meta["all_target_concepts_found"],
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
