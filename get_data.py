import time
import requests
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import schedule
import os
import subprocess

DEBUG = False

def get_fixture_data() -> list[dict]:
    """Fetches all games from the API and returns the games list."""
    base_url = "https://api.fantasynwsl.com/graphql"
    query = """
        {
            games {
                id
                scheduledAt
                hasStarted
                stage { id }
                home {
                    party {
                        __typename
                        ... on Club {
                            id
                            name
                            shortName
                        }
                    }
                    score
                }
                away {
                    party {
                        __typename
                        ... on Club {
                            id
                            name
                            shortName
                        }
                    }
                    score
                }
            }
        }
    """
    print("Fetching fixture data from API...")
    res = requests.post(base_url, json={"query": query})
    res.raise_for_status()
    data = res.json()
    return data["data"]["games"]


def process_game(game):
    """
    Transforms a complex game object into the simplified fixture format
    """
    home_name = game.get('home', {}).get('party', {}).get('name')
    home_short_name = game.get('home', {}).get('party', {}).get('shortName')
    home_id = game.get('home', {}).get('party', {}).get('id')
    away_name = game.get('away', {}).get('party', {}).get('name')
    away_short_name = game.get('away', {}).get('party', {}).get('shortName')
    away_id = game.get('away', {}).get('party', {}).get('id')
    stage_id = game.get('stage', {}).get('id')

    # --- Date/Time Transformation ---
    scheduled_at_str = game.get('scheduledAt')
    date_obj = datetime.fromisoformat(scheduled_at_str.replace('Z', '+00:00'))

    game_date = date_obj.strftime("%-d %b")
    kick_off_time = date_obj.strftime("%H:%M")

    return {
        "game_date": game_date,
        "kick_off_time": kick_off_time,
        "game_week": stage_id,
        "home_id": home_id.upper(),
        "home_name": home_name,
        "home_short_name": home_short_name,
        "away_id": away_id.upper(),
        "away_name": away_name,
        "away_short_name": away_short_name,
    }


def filter_fixtures(games_data):
    """
    Build a map of {club_id: [upcoming_fixtures]} from top-level games data.
    Uses club id codes like was, por, la, etc.
    """
    club_fixtures_map = defaultdict(list)

    now = datetime.now(timezone.utc)

    if DEBUG:
        print(f"DEBUG raw total games from API: {len(games_data)}")
        
    for i, game in enumerate(games_data):
        if DEBUG and i < 5:
            print(
                f"DEBUG raw top-level game: "
                f"id={game.get('id')}, "
                f"scheduledAt={game.get('scheduledAt')}, "
                f"hasStarted={game.get('hasStarted')}, "
                f"stage={game.get('stage', {}).get('id')}"
            )

        scheduled_at_str = game.get("scheduledAt")
        if not scheduled_at_str:
            continue

        try:
            game_date = datetime.fromisoformat(scheduled_at_str.replace("Z", "+00:00"))
        except Exception as e:
            if DEBUG:
                print(f"DEBUG skipping game due to date parse issue: {e}")
            continue

        if game_date <= now:
            continue

        simplified_fixture = process_game(game)

        home_id = simplified_fixture.get("home_id", "").upper()
        away_id = simplified_fixture.get("away_id", "").upper()

        if home_id:
            club_fixtures_map[home_id].append(simplified_fixture)
        if away_id:
            club_fixtures_map[away_id].append(simplified_fixture)

    if DEBUG:
        for club_id in list(club_fixtures_map.keys())[:20]:
            print(f"DEBUG fixture count for {club_id}: {len(club_fixtures_map[club_id])}")

        print("DEBUG fixture map keys:", list(club_fixtures_map.keys())[:20])

    return dict(club_fixtures_map)

def get_player_data() -> list[dict[str, int | str | float]]:
    """Fetches the player details from the API and returns a list of dictionaries."""
    base_url = "https://api.fantasynwsl.com/graphql"
    query = """
        {
            players {
                slug
                firstName
                lastName
                club {id, shortName}
                position
                news
                nationality
                visionaryNextStage
                price
                totalPoints
                selected
                performanceV2 {
                    games {
                        game {
                            id
                            scheduledAt
                            stage {id}
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
                        }                        points
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
    res = requests.post(base_url, json={"query": query})
    res.raise_for_status()
    data = res.json()
    return data["data"]["players"]


# --- TEAM NAME EXTRACTION ---
def extract_teams_from_game(game):
    """
    Extracts home and away team IDs directly from the game object.
    This is safer than guessing from the game ID string.
    """
    home_team = game.get("home", {}).get("party", {}).get("id", "")
    away_team = game.get("away", {}).get("party", {}).get("id", "")
    return home_team.upper(), away_team.upper()


def get_wsl_team_code(team_name):
    """
    Transforms a full or partial WSL team name into its standardized 3-character code.
    """
    TEAM_CODE_MAP = {
        "ARSENAL": "ARS",
        "ASTON VILLA": "AVL",
        "BRIGHTON": "BHA",
        "BRIGHTON & HOVE ALBION": "BHA",
        "CHELSEA": "CHE",
        "EVERTON": "EVE",
        "LEICESTER CITY": "LEI",
        "LIVERPOOL": "LIV",
        "LONDON CITY LIONESSES": "LCL",
        "MANCHESTER CITY": "MCI",
        "MANCHESTER UNITED": "MUN",
        "TOTTENHAM HOTSPUR": "TOT",
        "WEST HAM UNITED": "WHU",
    }
    if not team_name:
        return ""
    normalized_name = team_name.upper().strip()
    return TEAM_CODE_MAP.get(normalized_name, team_name)


def get_position_code(position):
    """
    Transforms a players position into a standardized code.
    """
    POSITION_MAP = {
        "GOALKEEPER": "GK",
        "DEFENDER": "DEF",
        "MIDFIELDER": "MID",
        "FORWARD": "FOR",
    }
    if not position:
        return ""
    return POSITION_MAP.get(position.upper().strip(), position[:3].upper())


# --- TOOLTIP FUNCTION ---
def create_gw_tooltip(game_data, player_team_code):
    """
    Creates a detailed, multi-line tooltip string for Gameweek match.
    Works directly with API game data structure.
    """
    game = game_data["game"]
    # 1. Date Formatting
    try:
        date_obj = datetime.fromisoformat(game["scheduledAt"].replace("Z", "+00:00"))
        date_str = date_obj.strftime("%-d %b")
    except (KeyError, ValueError, ImportError):
        date_str = "Date Unknown"

    # 2. Extract team codes from game ID
    home_team, away_team = extract_teams_from_game(game)

    # 3. Determine opponent and location
    if player_team_code == home_team:
        opponent_team = away_team
        location = "(H)"
    elif player_team_code == away_team:
        opponent_team = home_team
        location = "(A)"
    else:
        opponent_team = "Unknown"
        location = ""

    fixture_line = f"{location} {opponent_team}"

    # 4. Score and Result (W/D/L)
    home_score = game.get("home", {}).get("score", 0)
    away_score = game.get("away", {}).get("score", 0)
    score = f"{home_score}-{away_score}"

    result_abbr = "D"
    if player_team_code == home_team:
        player_score, opponent_score = home_score, away_score
    elif player_team_code == away_team:
        player_score, opponent_score = away_score, home_score
    else:
        player_score, opponent_score = home_score, away_score

    if player_score > opponent_score:
        result_abbr = "W"
    elif player_score < opponent_score:
        result_abbr = "L"

    score_line = f"{result_abbr} {score}"
    header_line = f"{date_str} {fixture_line} {score_line}"

    #  5. Process contributions (REVISED SECTION)
    contribution_lines = []
    CONTRIBUTION_MAP = {
        "PlayedOneMinute": "1 min",
        "PlayedSixtyMinutes": "60 min",
        "Scored": "Goal",
        "Assisted": "Assist",
        "CleanSheet": "Clean Sheet",
        "Bonus": "Bonus",
        "ThreeSaves": "Saves",
        "GoalLineClearance": "Clearance",
        "MissedPenalty": "Missed Pen",
        "ReceivedRedCard": "Red Card",
        "ReceivedYellowCard": "Yellow Card",
        "ScoredOwnGoal": "Own Goal",
        "ConcededGoals": "Conceded",
    }

    contributions = game_data.get("contributions", [])
    # ----------------------------------------------------------------------
    # 1. Sort by total_points (descending: use negative value)
    # 2. Sort by contribution type name (ascending: use name string)
    # This provides a stable and deterministic secondary sort key.
    # ----------------------------------------------------------------------
    sorted_contributions = sorted(
        contributions,
        key=lambda c: (
            -1 * (c.get("quantity", 1) * c.get("individualPoints", 0)),  # Primary: Total Pts (Negative for Descending)
            c["contribution"]  # Secondary: Contribution Type Name (Ascending)
        )
    )

    for contrib in sorted_contributions:
        contrib_type = contrib["contribution"]
        label = CONTRIBUTION_MAP.get(contrib_type, contrib_type)
        quantity = contrib.get("quantity", 1)
        individual_points = contrib.get("individualPoints", 0)
        total_points = quantity * individual_points

        # Only include contributions with non-zero points or where it's a card/event
        if total_points != 0 or contrib_type in [
            "ReceivedRedCard",
            "ReceivedYellowCard",
            "MissedPenalty",
            "ScoredOwnGoal",
        ]:
            sign = "+" if total_points > 0 else ""

            if quantity > 1:
                line = f"{label} x{quantity} ({sign}{total_points}pt{'' if abs(total_points) == 1 else 's'})"
            else:
                line = f"{label} ({sign}{total_points}pt{'' if abs(total_points) == 1 else 's'})"

            contribution_lines.append(line)

    # Combine: Header line, separator, Contribution lines
    tooltip_parts = [header_line] + contribution_lines
    return "\n".join(tooltip_parts)


def combine_player_and_fixture_data(final_player_list, fixtures_map):
    """
    Filters upcoming fixtures and joins them to the player data.

    Args:
        final_player_list (list): The list of fully processed player dictionaries.
        fixtures_map (dict): The map of {club_id: [upcoming_fixtures]}.
    """
    print("Combine player data and fixture data.")

    # Initialize the final output list
    all_players_with_fixtures = []

    # Iterate directly over the list of players (which is the input 'final_player_list')
    # Use 'player' here as the loop variable.
    for player in final_player_list:
        # NOTE: The player dicts inside final_output still have the 'Club' key
        # since it was transformed but not removed yet.
        club_id = player.get('Club', '').upper()

        if DEBUG and len(all_players_with_fixtures) < 10:
            print(
                f"DEBUG combine lookup for {player.get('Name')}: "
                f"club_id='{club_id}', fixture_keys_sample={list(fixtures_map.keys())[:10]}"
            )

        # Retrieve the upcoming fixtures list for this player's club
        upcoming_fixtures = fixtures_map.get(club_id, [])
        
        if DEBUG and len(all_players_with_fixtures) < 10:
            print(
                f"DEBUG combine result for {player.get('Name')}: "
                f"club_id='{club_id}', upcoming_count={len(upcoming_fixtures)}"
            )

        # Attach the fixtures data
        player['upcoming_fixtures'] = upcoming_fixtures

        all_players_with_fixtures.append(player)

    print(f"{len(all_players_with_fixtures)} players combined with fixtures.")

    return all_players_with_fixtures

# --- FIXTURE MODEL V2: ASA xG + RECENT FORM ---
ASA_BASE_URL = "https://app.americansocceranalysis.com/api/v1"

# Current NWSL team names in ASA -> Fantasy NWSL club codes.
# The ASA /teams endpoint contains historical teams too, so matching by current
# team name is safer than assuming every ASA abbreviation matches Fantasy NWSL.
ASA_TEAM_NAME_TO_FANTASY_CODE = {
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

# Model controls. These are intentionally grouped here so they are easy to tune
# after we backtest the first working version.
ASA_SEASON_YEAR = 2026
RECENT_MATCH_COUNT = 6
RECENCY_DECAY = 0.82

# Blend season-level strength with recent form.
SEASON_WEIGHT = 0.65
RECENT_WEIGHT = 0.35

# Within season/recent components, xG is the backbone and actual goals are
# included as a smaller reality/finishing component.
ATTACK_XG_WEIGHT = 0.75
ATTACK_GOALS_WEIGHT = 0.25
DEFENSE_XGA_WEIGHT = 0.80
DEFENSE_GOALS_WEIGHT = 0.20

# Fixed calibration anchors for the 0-100 fixture opportunity scale.
# These are NOT weekly min/max values, so the best fixture is not automatically 100.
ATTACK_XG_FLOOR = 0.55
ATTACK_XG_CEILING = 2.25
DEFENSE_XG_BEST = 0.55
DEFENSE_XG_WORST = 2.25


def clamp(value, low, high):
    return max(low, min(high, value))


def weighted_average(values, decay=RECENCY_DECAY):
    """
    Recency-weighted average where the first value is the most recent.
    Example weights with decay=.82: 1.00, .82, .67, .55, ...
    """
    if not values:
        return 0.0

    weights = [decay ** i for i in range(len(values))]
    total_weight = sum(weights)

    if total_weight <= 0:
        return sum(values) / len(values)

    return sum(v * w for v, w in zip(values, weights)) / total_weight


def get_asa_json(path, params=None):
    """Fetch JSON from the American Soccer Analysis API."""
    url = f"{ASA_BASE_URL}{path}"
    response = requests.get(url, params=params or {}, timeout=30)
    response.raise_for_status()
    return response.json()


def get_asa_team_code_map():
    """
    Returns {ASA team_id: Fantasy NWSL club code} for current NWSL teams.
    """
    teams = get_asa_json("/nwsl/teams")
    team_code_map = {}

    for team in teams:
        team_id = team.get("team_id")
        team_name = team.get("team_name", "")

        fantasy_code = ASA_TEAM_NAME_TO_FANTASY_CODE.get(team_name)

        if team_id and fantasy_code:
            team_code_map[team_id] = fantasy_code

    return team_code_map


def get_asa_nwsl_xg_games(season_year=ASA_SEASON_YEAR):
    """
    Fetch game-level NWSL xG from ASA and keep the requested calendar season.
    ASA returns historical NWSL records from this endpoint, so we filter locally
    by date instead of depending on a provider-specific season label.
    """
    games = get_asa_json("/nwsl/games/xgoals")
    season_prefix = f"{season_year}-"

    season_games = [
        game for game in games
        if str(game.get("date_time_utc", "")).startswith(season_prefix)
    ]

    print(
        f"Loaded {len(season_games)} ASA NWSL xG records for {season_year} "
        f"({len(games)} historical records available)."
    )

    return season_games


def filter_asa_games_to_fantasy_schedule(
    asa_games,
    asa_team_code_map,
    fantasy_games,
):
    """
    Keep only ASA xG games that correspond to completed Fantasy NWSL fixtures.

    Matching rules:
    - same home team
    - same away team
    - date may differ by up to 1 calendar day

    This handles provider timezone/date discrepancies while still excluding
    non-fantasy competitions such as the NWSL Challenge Cup.
    """

    fantasy_games_prepared = []

    for game in fantasy_games:
        home = str(
            game.get("home", {}).get("party", {}).get("id", "")
        ).upper()

        away = str(
            game.get("away", {}).get("party", {}).get("id", "")
        ).upper()

        scheduled_at = game.get("scheduledAt")
        has_started = game.get("hasStarted")

        if not home or not away or not scheduled_at:
            continue

        if has_started is not True:
            continue

        try:
            fantasy_date = datetime.fromisoformat(
                scheduled_at.replace("Z", "+00:00")
            ).date()
        except Exception:
            continue

        fantasy_games_prepared.append({
            "home": home,
            "away": away,
            "date": fantasy_date,
            "game": game,
        })

    matched_games = []
    matched_fantasy_indexes = set()

    asa_unmatched = []

    for asa_game in asa_games:
        home = asa_team_code_map.get(asa_game.get("home_team_id"))
        away = asa_team_code_map.get(asa_game.get("away_team_id"))

        if not home or not away:
            continue

        try:
            asa_date = datetime.strptime(
                str(asa_game.get("date_time_utc", "")).replace(" UTC", ""),
                "%Y-%m-%d %H:%M:%S",
            ).date()
        except Exception:
            continue

        best_match_index = None
        best_date_difference = None

        for index, fantasy_game in enumerate(fantasy_games_prepared):

            if index in matched_fantasy_indexes:
                continue

            if fantasy_game["home"] != home:
                continue

            if fantasy_game["away"] != away:
                continue

            date_difference = abs(
                (fantasy_game["date"] - asa_date).days
            )

            if date_difference > 1:
                continue

            if (
                best_date_difference is None
                or date_difference < best_date_difference
            ):
                best_match_index = index
                best_date_difference = date_difference

        if best_match_index is not None:
            matched_games.append(asa_game)
            matched_fantasy_indexes.add(best_match_index)

        else:
            asa_unmatched.append(asa_game)

    print(
        f"Matched {len(matched_games)} ASA xG games to "
        f"{len(fantasy_games_prepared)} completed Fantasy NWSL fixtures."
    )

    unmatched_fantasy = [
        fantasy_game
        for index, fantasy_game in enumerate(fantasy_games_prepared)
        if index not in matched_fantasy_indexes
    ]

    if unmatched_fantasy:
        print()
        print("=== UNMATCHED COMPLETED FANTASY FIXTURES ===")

        for item in unmatched_fantasy:
            game = item["game"]

            home_score = game.get("home", {}).get("score")
            away_score = game.get("away", {}).get("score")
            stage = game.get("stage", {}).get("id")

            print(
                f"{item['date']} | GW {stage} | "
                f"{item['home']} {home_score} - "
                f"{away_score} {item['away']}"
            )
    else:
        print("All completed Fantasy fixtures matched ASA xG data.")

    if asa_unmatched:
        print()
        print("=== ASA GAMES NOT IN COMPLETED FANTASY SCHEDULE ===")

        for game in asa_unmatched:
            home = asa_team_code_map.get(game.get("home_team_id"), "???")
            away = asa_team_code_map.get(game.get("away_team_id"), "???")

            date_text = str(game.get("date_time_utc", ""))[:10]

            print(
                f"{date_text} | "
                f"{home} {game.get('home_goals')} - "
                f"{game.get('away_goals')} {away}"
            )

    print()

    if not matched_games:
        raise ValueError(
            "No ASA xG games matched the Fantasy NWSL schedule."
        )

    return matched_games


def build_team_strength_v2(asa_games, asa_team_code_map, fantasy_games):
    """
    Build team strength from game-level ASA xG.

    For attack:
      season component = 75% xG/game + 25% goals/game
      recent component = 75% weighted xG + 25% weighted goals
      final = 65% season + 35% recent

    For defense, lower allowed values are better:
      season component = 80% xGA/game + 20% GA/game
      recent component = 80% weighted xGA + 20% weighted GA
      final = 65% season + 35% recent

    Returns team metrics plus league averages and a data-derived home advantage.
    """
    asa_games = filter_asa_games_to_fantasy_schedule(
        asa_games,
        asa_team_code_map,
        fantasy_games,
    )

    team_games = defaultdict(list)

    for game in asa_games:
        home_asa_id = game.get("home_team_id")
        away_asa_id = game.get("away_team_id")

        home_team = asa_team_code_map.get(home_asa_id)
        away_team = asa_team_code_map.get(away_asa_id)

        if not home_team or not away_team:
            continue

        try:
            game_date = datetime.strptime(
                str(game.get("date_time_utc", "")).replace(" UTC", ""),
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)

            home_xg = float(game.get("home_team_xgoals", 0) or 0)
            away_xg = float(game.get("away_team_xgoals", 0) or 0)
            home_goals = int(game.get("home_goals", 0) or 0)
            away_goals = int(game.get("away_goals", 0) or 0)
        except (ValueError, TypeError):
            continue

        team_games[home_team].append({
            "date": game_date,
            "is_home": True,
            "xg_for": home_xg,
            "xg_against": away_xg,
            "goals_for": home_goals,
            "goals_against": away_goals,
        })

        team_games[away_team].append({
            "date": game_date,
            "is_home": False,
            "xg_for": away_xg,
            "xg_against": home_xg,
            "goals_for": away_goals,
            "goals_against": home_goals,
        })

    team_strength = {}

    all_home_xg = []
    all_away_xg = []

    for game in asa_games:
        home_team = asa_team_code_map.get(game.get("home_team_id"))
        away_team = asa_team_code_map.get(game.get("away_team_id"))

        if not home_team or not away_team:
            continue

        try:
            all_home_xg.append(float(game.get("home_team_xgoals", 0) or 0))
            all_away_xg.append(float(game.get("away_team_xgoals", 0) or 0))
        except (ValueError, TypeError):
            continue

    for team_id, matches in team_games.items():
        matches_sorted = sorted(matches, key=lambda m: m["date"], reverse=True)

        games_played = len(matches_sorted)
        if games_played == 0:
            continue

        season_xg_pg = sum(m["xg_for"] for m in matches_sorted) / games_played
        season_xga_pg = sum(m["xg_against"] for m in matches_sorted) / games_played
        season_gf_pg = sum(m["goals_for"] for m in matches_sorted) / games_played
        season_ga_pg = sum(m["goals_against"] for m in matches_sorted) / games_played

        recent_matches = matches_sorted[:RECENT_MATCH_COUNT]

        recent_xg = weighted_average([m["xg_for"] for m in recent_matches])
        recent_xga = weighted_average([m["xg_against"] for m in recent_matches])
        recent_gf = weighted_average([m["goals_for"] for m in recent_matches])
        recent_ga = weighted_average([m["goals_against"] for m in recent_matches])

        season_attack = (
            ATTACK_XG_WEIGHT * season_xg_pg
            + ATTACK_GOALS_WEIGHT * season_gf_pg
        )
        recent_attack = (
            ATTACK_XG_WEIGHT * recent_xg
            + ATTACK_GOALS_WEIGHT * recent_gf
        )

        season_defense_allowed = (
            DEFENSE_XGA_WEIGHT * season_xga_pg
            + DEFENSE_GOALS_WEIGHT * season_ga_pg
        )
        recent_defense_allowed = (
            DEFENSE_XGA_WEIGHT * recent_xga
            + DEFENSE_GOALS_WEIGHT * recent_ga
        )

        attack_metric = (
            SEASON_WEIGHT * season_attack
            + RECENT_WEIGHT * recent_attack
        )
        defense_allowed_metric = (
            SEASON_WEIGHT * season_defense_allowed
            + RECENT_WEIGHT * recent_defense_allowed
        )

        clean_sheets = sum(1 for m in matches_sorted if m["goals_against"] == 0)
        clean_sheet_rate = clean_sheets / games_played

        team_strength[team_id] = {
            "games_played": games_played,
            "season_xg_pg": round(season_xg_pg, 3),
            "season_xga_pg": round(season_xga_pg, 3),
            "season_gf_pg": round(season_gf_pg, 3),
            "season_ga_pg": round(season_ga_pg, 3),
            "recent_xg": round(recent_xg, 3),
            "recent_xga": round(recent_xga, 3),
            "recent_gf": round(recent_gf, 3),
            "recent_ga": round(recent_ga, 3),
            "attack_metric": round(attack_metric, 3),
            "defense_allowed_metric": round(defense_allowed_metric, 3),
            "clean_sheet_rate": round(clean_sheet_rate, 3),
        }

    if not team_strength:
        raise ValueError("ASA team strength model produced no current-team data.")

    league_avg_attack = (
        sum(stats["attack_metric"] for stats in team_strength.values())
        / len(team_strength)
    )
    league_avg_defense_allowed = (
        sum(stats["defense_allowed_metric"] for stats in team_strength.values())
        / len(team_strength)
    )

    league_avg_xg = (
        sum(stats["season_xg_pg"] for stats in team_strength.values())
        / len(team_strength)
    )

    avg_home_xg = sum(all_home_xg) / len(all_home_xg) if all_home_xg else league_avg_xg
    avg_away_xg = sum(all_away_xg) / len(all_away_xg) if all_away_xg else league_avg_xg

    if avg_home_xg > 0 and avg_away_xg > 0:
        # Symmetric adjustment around 1.0. Capped so a noisy partial season
        # cannot create an oversized venue effect.
        home_factor = clamp((avg_home_xg / avg_away_xg) ** 0.5, 0.90, 1.10)
    else:
        home_factor = 1.0

    away_factor = 1.0 / home_factor if home_factor else 1.0

    model_context = {
        "league_avg_attack": league_avg_attack,
        "league_avg_defense_allowed": league_avg_defense_allowed,
        "league_avg_xg": league_avg_xg,
        "avg_home_xg": avg_home_xg,
        "avg_away_xg": avg_away_xg,
        "home_factor": home_factor,
        "away_factor": away_factor,
    }

    print("Fixture Model v2 team strengths:")
    for team_id, stats in sorted(
        team_strength.items(),
        key=lambda item: item[1]["attack_metric"],
        reverse=True,
    ):
        print(
            f"{team_id}: "
            f"xG={stats['season_xg_pg']:.2f}, "
            f"recent xG={stats['recent_xg']:.2f}, "
            f"attack={stats['attack_metric']:.2f}, "
            f"xGA={stats['season_xga_pg']:.2f}, "
            f"recent xGA={stats['recent_xga']:.2f}, "
            f"def allowed={stats['defense_allowed_metric']:.2f}"
        )

    print(
        f"League xG avg={league_avg_xg:.3f}; "
        f"home xG avg={avg_home_xg:.3f}; "
        f"away xG avg={avg_away_xg:.3f}; "
        f"home factor={home_factor:.3f}; "
        f"away factor={away_factor:.3f}"
    )

    return team_strength, model_context


def metric_to_strength_score(value, low, high, higher_is_better=True):
    """
    Convert a team metric to an explanatory 0-100 strength score.
    This is for tooltip transparency; fixture opportunity itself is derived
    from projected xG below.
    """
    if high <= low:
        return 50.0

    normalized = (value - low) / (high - low)
    normalized = clamp(normalized, 0.0, 1.0)

    if higher_is_better:
        return round(normalized * 100, 1)

    return round((1.0 - normalized) * 100, 1)


def projected_xg_for_fixture(
    attacking_team,
    defending_team,
    attacking_is_home,
    team_strength,
    model_context,
):
    """
    Estimate attacking-team xG for one fixture.

    We combine:
    - own blended attacking production relative to league average
    - opponent blended defensive allowance relative to league average
    - league-wide 2026 home/away xG effect

    The geometric mean keeps one extreme component from completely dominating.
    """
    attack_stats = team_strength.get(attacking_team)
    defense_stats = team_strength.get(defending_team)

    if not attack_stats or not defense_stats:
        return None

    league_attack = model_context["league_avg_attack"]
    league_def_allowed = model_context["league_avg_defense_allowed"]
    league_xg = model_context["league_avg_xg"]

    if league_attack <= 0 or league_def_allowed <= 0 or league_xg <= 0:
        return None

    attack_index = attack_stats["attack_metric"] / league_attack
    defense_weakness_index = (
        defense_stats["defense_allowed_metric"] / league_def_allowed
    )

    matchup_index = max(0.01, attack_index * defense_weakness_index) ** 0.5

    venue_factor = (
        model_context["home_factor"]
        if attacking_is_home
        else model_context["away_factor"]
    )

    projected_xg = league_xg * matchup_index * venue_factor

    return round(clamp(projected_xg, 0.05, 4.0), 3)


def projected_xg_to_attack_score(projected_xg):
    """
    Continuous 0-100 attacking fixture opportunity score.
    Higher = better.

    Fixed anchors mean the best fixture in a week is NOT automatically 100.
    """
    if projected_xg is None:
        return 50.0

    normalized = (
        (projected_xg - ATTACK_XG_FLOOR)
        / (ATTACK_XG_CEILING - ATTACK_XG_FLOOR)
    )
    return round(clamp(normalized * 100, 0, 100), 1)


def projected_xga_to_defense_score(projected_xga):
    """
    Continuous 0-100 defensive fixture opportunity score.
    Higher = better. Lower projected opponent xG is better.
    """
    if projected_xga is None:
        return 50.0

    normalized = (
        (DEFENSE_XG_WORST - projected_xga)
        / (DEFENSE_XG_WORST - DEFENSE_XG_BEST)
    )
    return round(clamp(normalized * 100, 0, 100), 1)


def fixture_score_to_legacy_bucket(score):
    """
    Keep the existing 1-5 filter field working:
      1 = elite
      2 = favorable
      3 = neutral
      4 = difficult
      5 = very difficult

    The displayed raw fixture rating is now the more granular 0-100 score.
    """
    if score is None:
        return None
    if score >= 80:
        return 1
    if score >= 65:
        return 2
    if score >= 45:
        return 3
    if score >= 25:
        return 4
    return 5


def score_single_fixture(
    player_position,
    player_team,
    opponent_team,
    location,
    team_strength,
    model_context,
):
    """
    Score one fixture from 0-100. Higher = better.

    FOR:
      attacking opportunity

    MID:
      90% attacking opportunity + 10% defensive opportunity,
      because midfielders also receive a clean-sheet point.

    DEF/GK:
      defensive/clean-sheet opportunity
    """
    own_stats = team_strength.get(player_team)
    opponent_stats = team_strength.get(opponent_team)

    if not own_stats or not opponent_stats:
        return 50.0, {
            "rating": 50.0,
            "attack_score": None,
            "defense_score": None,
            "projected_team_xg": None,
            "projected_opponent_xg": None,
            "estimated_clean_sheet_pct": None,
        }

    player_is_home = location == "(H)"

    projected_team_xg = projected_xg_for_fixture(
        player_team,
        opponent_team,
        player_is_home,
        team_strength,
        model_context,
    )

    projected_opponent_xg = projected_xg_for_fixture(
        opponent_team,
        player_team,
        not player_is_home,
        team_strength,
        model_context,
    )

    attack_score = projected_xg_to_attack_score(projected_team_xg)
    defense_score = projected_xga_to_defense_score(projected_opponent_xg)

    # Poisson approximation: P(0 goals) = e^-lambda.
    estimated_clean_sheet_pct = (
        round((2.718281828459045 ** (-projected_opponent_xg)) * 100, 1)
        if projected_opponent_xg is not None
        else None
    )

    if player_position == "FOR":
        rating = attack_score
    elif player_position == "MID":
        rating = (0.90 * attack_score) + (0.10 * defense_score)
    elif player_position in ["DEF", "GK"]:
        rating = defense_score
    else:
        rating = 50.0

    # Explanatory strength scores for tooltip.
    attack_values = [s["attack_metric"] for s in team_strength.values()]
    defense_values = [s["defense_allowed_metric"] for s in team_strength.values()]

    attack_low = min(attack_values)
    attack_high = max(attack_values)
    defense_low = min(defense_values)
    defense_high = max(defense_values)

    own_attack_strength = metric_to_strength_score(
        own_stats["attack_metric"],
        attack_low,
        attack_high,
        higher_is_better=True,
    )
    own_defensive_strength = metric_to_strength_score(
        own_stats["defense_allowed_metric"],
        defense_low,
        defense_high,
        higher_is_better=False,
    )
    opponent_attack_strength = metric_to_strength_score(
        opponent_stats["attack_metric"],
        attack_low,
        attack_high,
        higher_is_better=True,
    )
    opponent_defensive_strength = metric_to_strength_score(
        opponent_stats["defense_allowed_metric"],
        defense_low,
        defense_high,
        higher_is_better=False,
    )

    return round(clamp(rating, 0, 100), 1), {
        "rating": round(clamp(rating, 0, 100), 1),
        "attack_score": attack_score,
        "defense_score": defense_score,
        "projected_team_xg": projected_team_xg,
        "projected_opponent_xg": projected_opponent_xg,
        "estimated_clean_sheet_pct": estimated_clean_sheet_pct,
        "own_attack_strength": own_attack_strength,
        "own_defensive_strength": own_defensive_strength,
        "opponent_attack_strength": opponent_attack_strength,
        "opponent_defensive_strength": opponent_defensive_strength,
        "own_season_xg_pg": own_stats["season_xg_pg"],
        "own_recent_xg": own_stats["recent_xg"],
        "own_season_xga_pg": own_stats["season_xga_pg"],
        "own_recent_xga": own_stats["recent_xga"],
        "opponent_season_xg_pg": opponent_stats["season_xg_pg"],
        "opponent_recent_xg": opponent_stats["recent_xg"],
        "opponent_season_xga_pg": opponent_stats["season_xga_pg"],
        "opponent_recent_xga": opponent_stats["recent_xga"],
    }


def get_next_global_gameweek(fixtures_map):
    """
    Finds the next upcoming gameweek across the whole league.
    This prevents blank teams from skipping ahead to their next fixture.
    """
    upcoming_gws = []

    for fixtures in fixtures_map.values():
        for fixture in fixtures:
            gw = fixture.get("game_week")
            if gw is not None:
                try:
                    upcoming_gws.append(int(gw))
                except (ValueError, TypeError):
                    continue

    if not upcoming_gws:
        return None

    return min(upcoming_gws)


def get_fixtures_for_specific_gameweek(player, target_gw):
    """
    Return all fixtures for this player in a specific league gameweek.
    If the player/team blanks that week, returns [].
    """
    if target_gw is None:
        return []

    fixtures = player.get("upcoming_fixtures", [])

    return [
        f for f in fixtures
        if str(f.get("game_week")) == str(target_gw)
    ]


def get_gameweek_fixtures_by_offset(player, gameweek_offset=0):
    """
    Return all fixtures for the player's upcoming gameweek by offset.

    gameweek_offset=0 -> next upcoming gameweek
    gameweek_offset=1 -> following upcoming gameweek
    """
    fixtures = player.get("upcoming_fixtures", [])

    if not fixtures:
        return []

    upcoming_gws = []
    for fixture in fixtures:
        gw = fixture.get("game_week")
        if gw is not None:
            gw = str(gw)
            if gw not in upcoming_gws:
                upcoming_gws.append(gw)

    if len(upcoming_gws) <= gameweek_offset:
        return []

    target_gw = upcoming_gws[gameweek_offset]

    return [
        f for f in fixtures
        if str(f.get("game_week")) == target_gw
    ]


def get_next_gameweek_fixtures(player):
    """
    Backward-compatible wrapper for the next upcoming gameweek.
    """
    return get_gameweek_fixtures_by_offset(player, gameweek_offset=0)


def combine_fixture_scores(scores):
    """
    Combine one or more 0-100 fixture opportunity scores.

    For a single fixture, return that fixture's score.

    For DGWs/TGWs, volume matters. We use:
        combined = average_score * sqrt(number_of_fixtures)

    This gives a meaningful boost for multiple games without making two poor
    fixtures automatically better than one elite fixture.
    """
    if not scores:
        return None, None

    avg_score = sum(scores) / len(scores)
    fixture_count = len(scores)

    if fixture_count > 1:
        combined_score = avg_score * (fixture_count ** 0.5)
    else:
        combined_score = avg_score

    combined_score = round(clamp(combined_score, 0, 100), 1)
    display_bucket = fixture_score_to_legacy_bucket(combined_score)

    return combined_score, display_bucket


def build_fixture_details_text(fixture_details, raw_rating, display_score):
    """
    Build transparent tooltip text for the 0-100 Fixture Model v2.
    """
    if not fixture_details:
        return ""

    lines = ["Next GW fixtures:"]
    ratings = []

    for detail in fixture_details:
        opponent = detail.get("opponent_team", "???")
        location = detail.get("location", "")
        rating = detail.get("rating")

        if rating is not None:
            ratings.append(rating)
            lines.append(f"vs {opponent} {location}: {rating:.1f}/100")
        else:
            lines.append(f"vs {opponent} {location}: -")

        attack_score = detail.get("attack_score")
        defense_score = detail.get("defense_score")
        projected_team_xg = detail.get("projected_team_xg")
        projected_opponent_xg = detail.get("projected_opponent_xg")
        clean_sheet_pct = detail.get("estimated_clean_sheet_pct")

        if attack_score is not None:
            lines.append(f"  Attack opportunity: {attack_score:.1f}/100")
        if defense_score is not None:
            lines.append(f"  Defense opportunity: {defense_score:.1f}/100")
        if projected_team_xg is not None:
            lines.append(f"  Projected team xG: {projected_team_xg:.2f}")
        if projected_opponent_xg is not None:
            lines.append(f"  Projected opponent xG: {projected_opponent_xg:.2f}")
        if clean_sheet_pct is not None:
            lines.append(f"  Est. clean-sheet chance: {clean_sheet_pct:.1f}%")

        opp_att = detail.get("opponent_attack_strength")
        opp_def = detail.get("opponent_defensive_strength")

        if opp_att is not None:
            lines.append(f"  {opponent} attack strength: {opp_att:.1f}/100")
        if opp_def is not None:
            lines.append(f"  {opponent} defense strength: {opp_def:.1f}/100")

        opp_season_xg = detail.get("opponent_season_xg_pg")
        opp_recent_xg = detail.get("opponent_recent_xg")
        opp_season_xga = detail.get("opponent_season_xga_pg")
        opp_recent_xga = detail.get("opponent_recent_xga")

        if opp_season_xg is not None and opp_recent_xg is not None:
            lines.append(
                f"  {opponent} xG/game: {opp_season_xg:.2f} season, "
                f"{opp_recent_xg:.2f} weighted recent"
            )
        if opp_season_xga is not None and opp_recent_xga is not None:
            lines.append(
                f"  {opponent} xGA/game: {opp_season_xga:.2f} season, "
                f"{opp_recent_xga:.2f} weighted recent"
            )

    if ratings:
        avg_rating = sum(ratings) / len(ratings)
        lines.append("")
        lines.append(f"Average fixture opportunity: {avg_rating:.1f}/100")

        if len(ratings) > 1:
            volume_bonus = (raw_rating or avg_rating) - avg_rating
            lines.append(
                f"{len(ratings)}-fixture volume bonus: +{max(0, volume_bonus):.1f}"
            )
        else:
            lines.append("Fixture volume bonus: +0.0")

    if raw_rating is not None:
        lines.append(f"Final GW fixture score: {raw_rating:.1f}/100")

    first_detail = fixture_details[0]

    lines.append("")

    own_attack = first_detail.get("own_attack_strength")
    own_def = first_detail.get("own_defensive_strength")

    if own_attack is not None:
        lines.append(f"Own team attack strength: {own_attack:.1f}/100")
    if own_def is not None:
        lines.append(f"Own team defense strength: {own_def:.1f}/100")

    own_season_xg = first_detail.get("own_season_xg_pg")
    own_recent_xg = first_detail.get("own_recent_xg")
    own_season_xga = first_detail.get("own_season_xga_pg")
    own_recent_xga = first_detail.get("own_recent_xga")

    if own_season_xg is not None and own_recent_xg is not None:
        lines.append(
            f"Own xG/game: {own_season_xg:.2f} season, "
            f"{own_recent_xg:.2f} weighted recent"
        )

    if own_season_xga is not None and own_recent_xga is not None:
        lines.append(
            f"Own xGA/game: {own_season_xga:.2f} season, "
            f"{own_recent_xga:.2f} weighted recent"
        )

    return "\n".join(lines)


def get_next_fixture_score(
    player,
    team_strength,
    model_context,
    target_gw=None,
):
    """
    Compute a player's gameweek fixture opportunity.

    Returns:
    - raw 0-100 opportunity score (higher = better)
    - legacy 1-5 bucket used by the existing Fixture Score filter
    - detailed tooltip text
    """
    next_gw_fixtures = get_fixtures_for_specific_gameweek(player, target_gw)

    if DEBUG:
        print(
            f"DEBUG next fixtures for {player.get('Name')}: "
            f"{len(next_gw_fixtures)} fixtures"
        )

    if not next_gw_fixtures:
        return None, None, ""

    player_team = str(player.get("Club", "")).strip().upper()
    player_position = player.get("Position")

    scores = []
    fixture_details = []

    for fixture in next_gw_fixtures:
        home_team = str(fixture.get("home_id", "")).strip().upper()
        away_team = str(fixture.get("away_id", "")).strip().upper()

        if player_team == home_team:
            opponent_team = away_team
            location = "(H)"
        elif player_team == away_team:
            opponent_team = home_team
            location = "(A)"
        else:
            if DEBUG:
                print(
                    f"WARNING no team match for {player.get('Name')}: "
                    f"player_team='{player_team}', "
                    f"home_team='{home_team}', away_team='{away_team}'"
                )
            continue

        fixture_score, detail = score_single_fixture(
            player_position,
            player_team,
            opponent_team,
            location,
            team_strength,
            model_context,
        )

        scores.append(fixture_score)

        detail["opponent_team"] = opponent_team
        detail["location"] = location
        detail["rating"] = fixture_score

        fixture_details.append(detail)

    if not scores:
        if DEBUG:
            print(f"WARNING no fixture scores generated for {player.get('Name')}")
        return None, None, ""

    raw_rating, display_score = combine_fixture_scores(scores)
    details_text = build_fixture_details_text(
        fixture_details,
        raw_rating,
        display_score,
    )

    return raw_rating, display_score, details_text


def load_history_data(history_file="player_history.json"):
    """
    Loads historical player data from JSON file.
    """
    if os.path.exists(history_file) and os.path.getsize(history_file) > 0:
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: {history_file} is corrupted or empty.")
            return {}
    return {}

def get_last_global_price_change_date(history_data, min_changes=4):
    """
    Finds the most recent date that appears to be a global price-change event,
    defined as a date where at least `min_changes` players changed in value
    compared to their previous recorded date.

    If no such date exists, falls back to the most recent date where any player changed.

    Returns:
        datetime.date | None
    """
    change_counts_by_date = defaultdict(int)

    for player_name, player_history in history_data.items():
        if not isinstance(player_history, dict):
            continue

        dated_entries = []
        for date_str, stats in player_history.items():
            try:
                entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                value = stats.get("Value")
                if value is not None:
                    dated_entries.append((entry_date, value))
            except (ValueError, TypeError):
                continue

        dated_entries.sort(key=lambda x: x[0])

        for i in range(1, len(dated_entries)):
            prev_date, prev_value = dated_entries[i - 1]
            curr_date, curr_value = dated_entries[i]

            if prev_value != curr_value:
                change_counts_by_date[curr_date] += 1

    if not change_counts_by_date:
        return None

    qualifying_dates = [
        date for date, count in change_counts_by_date.items()
        if count >= min_changes
    ]

    if qualifying_dates:
        best_date = max(qualifying_dates)
        print(
            f"Inferred last global price change date: {best_date} "
            f"({change_counts_by_date[best_date]} players changed, threshold={min_changes})"
        )
        return best_date

    best_date = max(change_counts_by_date.keys())
    print(
        f"No date met threshold={min_changes}. "
        f"Falling back to most recent price change date: {best_date} "
        f"({change_counts_by_date[best_date]} players changed)"
    )
    return best_date


def get_selected_percentage_delta_1w(player_name, current_selected_percentage, history_data):
    """
    Returns the change in selected percentage compared with the closest
    available record from 7 days ago or earlier.

    Example:
    current = 12.4
    one week ago = 10.1
    delta = +2.3
    """
    player_history = history_data.get(player_name, {})
    if not player_history:
        return None

    target_date = (datetime.now() - timedelta(days=7)).date()

    valid_entries = []
    for date_str, stats in player_history.items():
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            selected_pct = stats.get("Selected Percentage")
            if selected_pct is not None and entry_date <= target_date:
                valid_entries.append((entry_date, selected_pct))
        except (ValueError, TypeError):
            continue

    if not valid_entries:
        return None

    best_date, previous_selected_percentage = max(valid_entries, key=lambda x: x[0])
    return round(current_selected_percentage - previous_selected_percentage, 1)


def get_selected_percentage_delta_since_date(player_name, current_selected_percentage, history_data, target_date):
    """
    Returns the change in selected percentage compared with the most recent
    available record on or before target_date.
    """
    if target_date is None:
        return None

    player_history = history_data.get(player_name, {})
    if not player_history:
        return None

    valid_entries = []
    for date_str, stats in player_history.items():
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            selected_pct = stats.get("Selected Percentage")
            if selected_pct is not None and entry_date <= target_date:
                valid_entries.append((entry_date, selected_pct))
        except (ValueError, TypeError):
            continue

    if not valid_entries:
        return None

    best_date, previous_selected_percentage = max(valid_entries, key=lambda x: x[0])
    return round(current_selected_percentage - previous_selected_percentage, 1)
    
def get_recommendation(
    position,
    value,
    selected_percentage,
    ownership_delta,
    ppg_4gw,
    ppm_4gw,
    news
):
    """
    Returns a simple recommendation label based on form, ownership trend,
    value, and any news flag.
    """
    has_news = bool(news and str(news).strip())

    if has_news and ppg_4gw < 2.0:
        return "Avoid"

    if ppg_4gw >= 4.0 and ownership_delta is not None and ownership_delta >= 1.0:
        return "Hot Pick"

    if ppg_4gw >= 4.0 and selected_percentage < 10.0:
        return "Differential"

    if ppm_4gw >= 0.8 and value <= 7.0:
        return "Value Pick"

    if ownership_delta is not None and ownership_delta >= 2.0 and ppg_4gw < 4.0:
        return "Bandwagon"

    if ppg_4gw < 2.0 and (ownership_delta is None or ownership_delta <= 0):
        return "Cold"

    return "Steady"

def calculate_form_rating(recent_points, recent_bonus_games):
    """
    Returns a 0-100 form rating based on last 4 games.

    Components:
    - 50% recent points per game
    - 35% consistency: % of last 4 games with 3+ points
    - 15% bonus involvement: % of last 4 games with bonus points
    """
    if not recent_points:
        return 0

    games_count = len(recent_points)

    # 1. Recent PPG score
    # 8+ PPG = 100, 0 PPG = 0
    recent_ppg = sum(recent_points) / games_count
    ppg_score = min(100, max(0, (recent_ppg / 8) * 100))

    # 2. Consistency score
    # 3+ points means a fantasy return above just playing
    consistency_games = sum(1 for pts in recent_points if pts >= 3)
    consistency_score = (consistency_games / games_count) * 100

    # 3. Bonus involvement score
    bonus_score = (recent_bonus_games / games_count) * 100

    form_rating = (
        0.50 * ppg_score +
        0.35 * consistency_score +
        0.15 * bonus_score
    )

    return round(form_rating)

def apply_recent_play_penalty(form_rating, last_played_gw, latest_gw):
    """
    Penalize form rating if a player has not appeared recently.
    """
    if form_rating == 0:
        return 0

    if last_played_gw is None or latest_gw is None:
        return form_rating

    missed_gws = latest_gw - last_played_gw

    if missed_gws <= 1:
        return form_rating
    elif missed_gws == 2:
        return round(form_rating * 0.5)
    else:
        return 0

# --- POSITION-SPECIFIC OVERALL / DECISION RATING ---
DECISION_WEIGHTS = {
    "GK":  {"fixture": 0.85, "form": 0.15},
    "DEF": {"fixture": 0.65, "form": 0.35},
    "MID": {"fixture": 0.35, "form": 0.65},
    "FOR": {"fixture": 0.25, "form": 0.75},
}

def calculate_decision_rating(position, form_rating, fixture_rating):
    """
    Combine current Form Rating and next-GW Fixture Rating into a 0-100
    position-specific Decision Rating.

    Weighting from the historical backtest:
      GK  = 85% fixture / 15% form
      DEF = 65% fixture / 35% form
      MID = 35% fixture / 65% form
      FOR = 25% fixture / 75% form

    A player with no upcoming fixture receives a fixture component of 0.
    DGW value is already contained in Fixture Rating, so it is not added again.
    """
    weights = DECISION_WEIGHTS.get(position, {"fixture": 0.50, "form": 0.50})

    try:
        form = float(form_rating or 0)
    except (TypeError, ValueError):
        form = 0.0

    try:
        fixture = float(fixture_rating) if fixture_rating is not None else 0.0
    except (TypeError, ValueError):
        fixture = 0.0

    rating = (weights["fixture"] * fixture) + (weights["form"] * form)
    return round(clamp(rating, 0, 100), 1)

def is_hot_pick(recent_points):
    """
    Returns True if the player scored more than 4 points
    in at least 3 of their last 4 gameweeks.
    """
    if not recent_points:
        return False

    qualifying_games = sum(1 for pts in recent_points if pts >= 4)
    return qualifying_games >= 3
    
# --- NEW FUNCTION FOR HISTORY FILE ---
def update_player_history(final_player_list, history_file="player_history.json"):
    """
    Updates the daily historical record for player value and selected percentage.
    The file will store the latest run's data for the current day.
    """
    print(f"Updating historical data in {history_file}...")
    today_date_str = datetime.now().strftime("%Y-%m-%d")

    # 1. Load existing history data
    history_data = {}
    if os.path.exists(history_file) and os.path.getsize(history_file) > 0:
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history_data = json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: {history_file} is corrupted or empty. Starting new history.")
            history_data = {}

    # 2. Prepare today's data (Player Slug -> {Value, Selected Percentage})
    today_player_data = {}
    for player in final_player_list:
        # Assuming the 'slug' is available in the original API response and should be preserved
        # Since 'slug' is fetched in get_player_data, let's grab it from there
        # For simplicity, we'll use the combined 'Name' as a key since the final_player_list
        # doesn't contain the 'slug' directly from the API response
        # *A better long-term solution would be to include 'slug' in the final_player object.*
        player_key = player.get('Name')

        if player_key:
            # Create a dictionary for the player's current stats

            today_player_data[player_key] = {
                "Value": player.get("Value"),
                "Selected Percentage": player.get("Selected Percentage"),
                "Form Rating": player.get("Form Rating"),
            }

    # 3. Update history data for the current date
    # History data structure: { "Player Name": { "YYYY-MM-DD": { "Value": X, "Selected Percentage": Y }, ... } }

    # Iterate through all players in the current run
    for player_name, current_stats in today_player_data.items():
        # Get or initialize the historical record for this player
        player_history = history_data.get(player_name, {})

        # Update/overwrite the record for today
        player_history[today_date_str] = current_stats

        # Save the updated history back to the main data structure
        history_data[player_name] = player_history

    # 4. Save the updated history file
    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history_data, f, indent=4, ensure_ascii=False)
        print(f"Historical data for {today_date_str} saved to {history_file}.")
    except Exception as e:
        print(f"Error saving historical data: {e}")

    return history_file  # Return file path for potential git staging


# --- MAIN TRANSFORMATION FUNCTION ---
def transform_data(output_file="transformed_data.json", history_file="player_history.json", recent_games_count=4):
    """
    Fetches data from API, transforms it directly, and saves to output file(s).
    """

    # 1. Fetch data from API
    print("Fetching player data from API...")
    try:
        api_players = get_player_data()
    except Exception as e:
        print(f"Error fetching API data: {e}")
        return

    print(f"Loaded {len(api_players)} players from API.")
    history_data = load_history_data(history_file)
    last_global_price_change_date = get_last_global_price_change_date(history_data, min_changes=4)

    # 2. Determine max gameweeks from all players
    all_gameweek_numbers = set()
    for player in api_players:
        for performance in player.get("performanceV2", []):
            for game_data in performance.get("games", []):
                try:
                    gw = game_data["game"]["stage"]["id"]
                    all_gameweek_numbers.add(int(gw))
                except (KeyError, ValueError, TypeError):
                    continue
                
    if not all_gameweek_numbers:
        final_gameweeks = []
    else:
        max_gw = max(all_gameweek_numbers)
        final_gameweeks = [str(gw) for gw in range(1, max_gw + 1)]

    latest_gw = max([int(gw) for gw in final_gameweeks]) if final_gameweeks else None

    # 3. Stat accumulation mappings
    STAT_ACCUMULATORS = {
        "CleanSheet": "Total Clean Sheet",
        "Scored": "Total Goals",
        "Assisted": "Total Assists",
        "Bonus": "Total Bonus Points",
        "ReceivedYellowCard": "Total Yellow Cards",
        "ReceivedRedCard": "Total Red Cards",
        "MissedPenalty": "Total Missed Penalties",
        "ScoredOwnGoal": "Total Own Goals",
        "ThreeSaves": "Total Saves",
        "GoalLineClearance": "Total Clearances",
        "PlayedOneMinute": "Total 1 min Appearances",
        "PlayedSixtyMinutes": "Total 60 min Appearances",
        "ConcededGoals": "Total Conceeded",
    }

    final_output = []

    # 4. Process each player
    for player in api_players:
        # Extract basic info directly from API
        name = f"{player.get('firstName', '')} {player.get('lastName', '')}".strip()
        if not name:
            continue

        club = player.get("club", {}).get("id", "").upper()
        if DEBUG and len(final_output) < 10:
            print(f"DEBUG player club for {name}: '{club}'")
        nationality = player.get("nationality", "")
        news = player.get("news", "")
        visionary = player.get("visionaryNextStage", "")
        position = get_position_code(player.get("position", ""))
        value = player.get("price", 0) / 10.0  # API returns in tenths
        selected_percentage = player.get("selected", 0) * 100  # Convert to percentage
        ownership_delta_1w = get_selected_percentage_delta_1w(
            name,
            selected_percentage,
            history_data
        )
        ownership_delta_since_last_price_change = get_selected_percentage_delta_since_date(
            name,
            selected_percentage,
            history_data,
            last_global_price_change_date
        )

        # Initialize stats
        gw_data_map = {}
        gw_visionary_bonus_map = defaultdict(int)

        gw_points_total_4gw = 0
        gw_games_played_4gw = 0
        recent_points = []
        recent_bonus_games = 0
        recent_game_records = []

        last_played_gw = None
        bonus_games_count = 0
        overall_stats = defaultdict(int)
        overall_games_played = 0

        # We calculate Total Points ourselves from:
        #   individual game points
        #   + any gameweek-level Visionary bonus in performanceV2.extras
        total_points = 0

        # ---------------------------------------------------------
        # Collect games AND gameweek-level Visionary extras
        # ---------------------------------------------------------
        all_games = []

        for performance in player.get("performanceV2", []):

            performance_games = performance.get("games", []) or []

            # Preserve all individual games for the existing processing below.
            for game_data in performance_games:
                all_games.append(game_data)

            # A performance entry represents a gameweek. In a DGW it can contain
            # multiple games, but they should all carry the same stage/gameweek ID.
            performance_gws = set()

            for game_data in performance_games:
                gw_value = (
                    game_data
                    .get("game", {})
                    .get("stage", {})
                    .get("id")
                )

                if gw_value is not None and str(gw_value).strip() != "":
                    performance_gws.add(str(gw_value))

            # Extras are gameweek-level points such as the +3 Visionary award.
            visionary_extra_points = 0

            extras = performance.get("extras", {}) or {}

            for contrib in extras.get("contributions", []) or []:
                if contrib.get("contribution") == "Visionary":
                    quantity = contrib.get("quantity", 1) or 0
                    individual_points = contrib.get("individualPoints", 0) or 0

                    visionary_extra_points += quantity * individual_points

            if visionary_extra_points:
                if len(performance_gws) == 1:
                    performance_gw = next(iter(performance_gws))
                    gw_visionary_bonus_map[performance_gw] += visionary_extra_points

                else:
                    print(
                        f"WARNING: Could not assign Visionary bonus for {name}. "
                        f"Found gameweeks={sorted(performance_gws)} "
                        f"and bonus={visionary_extra_points}."
                    )

        # Sort games by gameweek (most recent first)
        sorted_games = sorted(
            all_games,
            key=lambda g: int(
                g.get("game", {}).get("stage", {}).get("id", 0)
            ),
            reverse=True,
        )

        # ---------------------------------------------------------
        # Process individual games
        # ---------------------------------------------------------
        for i, game_data in enumerate(sorted_games):

            game = game_data.get("game", {})
            gw = str(game.get("stage", {}).get("id", ""))
            points = game_data.get("points", 0) or 0
            got_bonus_this_game = False

            try:
                gw_int = int(gw)

                if last_played_gw is None or gw_int > last_played_gw:
                    last_played_gw = gw_int

            except (ValueError, TypeError):
                pass

            if not gw:
                continue

            # Create normal match tooltip.
            tooltip_str = create_gw_tooltip(game_data, club)

            # Store BASE game points first.
            # If this is a DGW, combine the individual match scores.
            if gw in gw_data_map:
                gw_data_map[gw]["points"] += points
                gw_data_map[gw]["tooltip"] += (
                    "\n\n---\n\n" + tooltip_str
                )
            else:
                gw_data_map[gw] = {
                    "points": points,
                    "base_points": points,
                    "visionary_bonus": 0,
                    "tooltip": tooltip_str,
                }

            # If it became a DGW, keep base_points synced to the accumulated
            # individual-game total.
            gw_data_map[gw]["base_points"] = gw_data_map[gw]["points"]

            # Base game points count toward season total now.
            # Visionary extras are added once per gameweek below.
            total_points += points

            # ---------------------------------------------------------
            # Accumulate ordinary game contribution stats
            # ---------------------------------------------------------
            for contrib in game_data.get("contributions", []):

                contrib_type = contrib["contribution"]

                if (
                    contrib_type == "Bonus"
                    and contrib.get("quantity", 0) > 0
                ):
                    got_bonus_this_game = True

                target_stat_key = STAT_ACCUMULATORS.get(contrib_type)

                if target_stat_key:

                    if contrib_type == "ConcededGoals":
                        quantity = contrib.get("quantity", 1)
                        individual_pts = contrib.get(
                            "individualPoints",
                            0
                        )

                        overall_stats[target_stat_key] += (
                            quantity * individual_pts
                        )

                    else:
                        overall_stats[target_stat_key] += (
                            contrib.get("quantity", 1)
                        )

            if got_bonus_this_game:
                bonus_games_count += 1

            # Save the last N individual games now.
            # We apply a Visionary award to these records AFTER processing
            # all games so the once-per-GW bonus cannot be double-counted
            # during a DGW.
            if i < recent_games_count:
                recent_game_records.append({
                    "gw": gw,
                    "points": points,
                    "got_bonus": got_bonus_this_game,
                })

            overall_games_played += 1

        # ---------------------------------------------------------
        # Apply gameweek-level Visionary bonuses
        # ---------------------------------------------------------
        for gw, visionary_bonus in gw_visionary_bonus_map.items():

            if gw not in gw_data_map:
                print(
                    f"WARNING: {name} has a Visionary bonus in GW{gw} "
                    f"but no matching game data."
                )
                continue

            base_points = gw_data_map[gw]["points"]

            gw_data_map[gw]["base_points"] = base_points
            gw_data_map[gw]["visionary_bonus"] = visionary_bonus
            gw_data_map[gw]["points"] = base_points + visionary_bonus

            gw_data_map[gw]["tooltip"] += (
                f"\n\nVisionary bonus: +{visionary_bonus} pts"
            )

            # Visionary is not contained in the individual game's points,
            # so add it to the season total here exactly once.
            total_points += visionary_bonus

        # ---------------------------------------------------------
        # Build recent-form points using TRUE fantasy scores
        # ---------------------------------------------------------
        visionary_bonus_used_for_recent_gw = set()

        for record in recent_game_records:

            adjusted_points = record["points"]
            record_gw = record["gw"]

            # A Visionary award is once per gameweek, even in a DGW.
            # Attach it to only one of the recent game records for that GW.
            if (
                record_gw in gw_visionary_bonus_map
                and record_gw not in visionary_bonus_used_for_recent_gw
            ):
                adjusted_points += gw_visionary_bonus_map[record_gw]
                visionary_bonus_used_for_recent_gw.add(record_gw)

            recent_points.append(adjusted_points)

            if record["got_bonus"]:
                recent_bonus_games += 1

        gw_points_total_4gw = sum(recent_points)
        gw_games_played_4gw = len(recent_points)
        
        # Calculate derived metrics
        ppm_total = total_points / value if value > 0 else 0.0
        ppm_4gw = gw_points_total_4gw / value if value > 0 else 0.0
        ppg_4gw = (
            gw_points_total_4gw / recent_games_count if recent_games_count > 0 else 0.0
        )

        hot_pick = is_hot_pick(recent_points)

        recommendation = get_recommendation(
            position=position,
            value=value,
            selected_percentage=selected_percentage,
            ownership_delta=ownership_delta_1w,
            ppg_4gw=ppg_4gw,
            ppm_4gw=ppm_4gw,
            news=news
        )

        # Assemble final player object
        final_player = {
            "Name": name,
            "Club": club,
            "Position": position,
            "Value": round(value, 1),
            "Nationality": nationality,
            "News": news,
            "Visionary": visionary,
            "Total Points": total_points,
            "Selected Percentage": round(selected_percentage, 1),
            "Selected Percentage Change 1W": ownership_delta_1w,
            "Selected Percentage Change Since Last Global Price Change": ownership_delta_since_last_price_change,
            "Recommendation": recommendation,
            "Hot Pick": hot_pick,
            "Total Games Played": overall_games_played,
            "Total Over 4 Gameweeks": gw_points_total_4gw,
            "Form Rating": apply_recent_play_penalty(
                calculate_form_rating(recent_points, recent_bonus_games),
                last_played_gw,
                latest_gw
            ),
            "Games Played Over 4 Gameweeks": gw_games_played_4gw,
            "Points Per Game Over 4 Gameweeks": round(ppg_4gw, 1),
            "Points Per Million": round(ppm_total, 1),
            "Points Per Million Over 4 Gameweeks": round(ppm_4gw, 1),
            "Total Goals": overall_stats["Total Goals"],
            "Total Assists": overall_stats["Total Assists"],
            "Total Goals + Assists": overall_stats["Total Goals"] + overall_stats["Total Assists"],
            "Total Red Cards": overall_stats["Total Red Cards"],
            "Total Yellow Cards": overall_stats["Total Yellow Cards"],
            "Total Saves": overall_stats["Total Saves"],
            "Total Own Goals": overall_stats["Total Own Goals"],
            "Total Conceeded": overall_stats["Total Conceeded"],
            "Total Clean Sheet": overall_stats["Total Clean Sheet"],
            "Total Bonus Points": overall_stats["Total Bonus Points"],
            "Total Bonus Games": bonus_games_count,
            "Total Missed Penalties": overall_stats["Total Missed Penalties"],
            "Total Clearances": overall_stats["Total Clearances"],
            "Total 1 min Appearances": overall_stats["Total 1 min Appearances"],
            "Total 60 min Appearances": overall_stats["Total 60 min Appearances"],
        }

        # Add dynamic gameweek columns
        for gw in final_gameweeks:
            final_player[gw] = gw_data_map.get(gw, "-")

        final_output.append(final_player)

    print(f"{len(final_output)} players processed.")

    # 5. Update the player history file
    update_player_history(final_output, history_file)

    # 6. Get fixture data for teams and combine with main data
    try:
        fixtures = get_fixture_data()
        print("Loaded fixtures from API.")

        filtered_fixtures = filter_fixtures(fixtures)
        combined_data = combine_player_and_fixture_data(final_output, filtered_fixtures)

        next_global_gw = get_next_global_gameweek(filtered_fixtures)
        following_global_gw = next_global_gw + 1 if next_global_gw is not None else None
        
        print(f"Next global GW: {next_global_gw}")
        print(f"Following global GW: {following_global_gw}")

        # Build Fixture Model v2 from American Soccer Analysis game-level xG.
        # Fantasy NWSL remains the source of truth for players, fixtures and gameweeks.
        print("Fetching American Soccer Analysis NWSL xG data...")
        asa_team_code_map = get_asa_team_code_map()
        asa_xg_games = get_asa_nwsl_xg_games(ASA_SEASON_YEAR)

        team_strength, fixture_model_context = build_team_strength_v2(
            asa_xg_games,
            asa_team_code_map,
            fixtures,
        )

        # Add next and following gameweek fixture scores for each player.
        # "Rating" is now a granular 0-100 opportunity score (higher = better).
        # "Score" remains a 1-5 bucket so the existing Fixture Score filter
        # can continue to function until/unless the frontend is changed.
        for player in combined_data:

            next_fixture_rating, next_fixture_score, next_fixture_details = get_next_fixture_score(
                player,
                team_strength,
                fixture_model_context,
                target_gw=next_global_gw,
            )

            player["Next Fixture Rating"] = next_fixture_rating
            player["Next Fixture Score"] = next_fixture_score
            player["Next Fixture Details"] = next_fixture_details

            following_fixture_rating, following_fixture_score, following_fixture_details = get_next_fixture_score(
                player,
                team_strength,
                fixture_model_context,
                target_gw=following_global_gw,
            )

            player["Following Fixture Rating"] = following_fixture_rating
            player["Following Fixture Score"] = following_fixture_score
            player["Following Fixture Details"] = following_fixture_details

            # Overall / Decision Rating: position-specific blend of current form
            # and the NEXT gameweek fixture opportunity.
            player["Decision Rating"] = calculate_decision_rating(
                player.get("Position"),
                player.get("Form Rating"),
                next_fixture_rating,
            )

        output_payload = {
            "metadata": {
                "last_global_price_change_date": (
                    last_global_price_change_date.isoformat()
                    if last_global_price_change_date else None
                ),
                "fixture_model": {
                    "version": "v2-asa-xg",
                    "season_year": ASA_SEASON_YEAR,
                    "recent_match_count": RECENT_MATCH_COUNT,
                    "season_weight": SEASON_WEIGHT,
                    "recent_weight": RECENT_WEIGHT,
                    "home_factor": round(fixture_model_context["home_factor"], 4),
                    "away_factor": round(fixture_model_context["away_factor"], 4),
                    "league_avg_xg": round(fixture_model_context["league_avg_xg"], 4),
                },
                "decision_rating": {
                    "version": "v1-position-specific",
                    "weights": DECISION_WEIGHTS,
                    "uses": ["Form Rating", "Next Fixture Rating"],
                },
            },
            "players": combined_data
        }

        # 7. Save main output
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_payload, f, indent=4, ensure_ascii=False)
        print(f"Data saved to {output_file}")

        print("Data refresh complete.")
    except Exception as e:
        print("Unable to get fixture data")
        print(f"Error details: {e}")
        fixtures = []

def commit_changes_to_git():
    try:
        print("Starting Git operations...")

        # 1. Stage the file (Add is required even for status check)
        # Add both files to staging
        subprocess.run(["git", "add", "transformed_data.json", "player_history.json"], check=True, cwd=os.getcwd())

        # 2. Check the status: Run 'git status --porcelain'
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            cwd=os.getcwd()
        )

        # Check if either staged file appears in the status output
        # ' M transformed_data.json', 'A  transformed_data.json',
        # ' M player_history.json', 'A  player_history.json' indicates a change
        if "transformed_data.json" in status_result.stdout or "player_history.json" in status_result.stdout:
            print("One or both files were modified. Proceeding with commit and push.")

            # 3. Commit the changes
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            commit_message = f"Automated data refresh: {timestamp}"
            subprocess.run(["git", "commit", "-m", commit_message], check=True, cwd=os.getcwd())

            # 4. Push to the remote repository
            subprocess.run(["git", "push", "origin", "main"], check=True, cwd=os.getcwd())

            print("Successfully committed and pushed new data to GitHub.")
        else:
            print("No changes detected in tracked files. Skipping commit and push.")

    except subprocess.CalledProcessError as e:
        print("A Git command failed.")
        print(f"Stdout: {e.stdout}")
        print(f"Stderr: {e.stderr}")
    except Exception as e:
        print(f"An unexpected error occurred during Git operations: {e}")


print("Started")

if __name__ == "__main__":
    transform_data()
