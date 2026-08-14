import requests
import json
from collections import defaultdict

BASE_URL = "https://app.americansocceranalysis.com/api/v1"


def get_json(path):
    url = f"{BASE_URL}{path}"
    print(f"\nRequesting: {url}")

    response = requests.get(url, timeout=30)
    print("Status:", response.status_code)
    response.raise_for_status()

    return response.json()


def main():
    # 1. Get NWSL team information
    teams = get_json("/nwsl/teams")

    print("\n=== TEAM DATA SAMPLE ===")
    print(json.dumps(teams[:5], indent=2))

    # Build team ID -> name mapping
    team_names = {}

    for team in teams:
        team_id = team.get("team_id")

        # We don't yet know exactly which name field ASA uses,
        # so try the likely possibilities.
        team_name = (
            team.get("team_name")
            or team.get("name")
            or team.get("team")
            or team.get("short_name")
        )

        if team_id and team_name:
            team_names[team_id] = team_name

    print("\n=== TEAM ID MAP ===")
    for team_id, team_name in team_names.items():
        print(team_id, "=>", team_name)

    # 2. Get game-level xG
    games = get_json("/nwsl/games/xgoals")

    print("\nTotal xG game records:", len(games))

    # 3. Keep only 2026 games
    games_2026 = [
        game for game in games
        if str(game.get("date_time_utc", "")).startswith("2026-")
    ]

    print("2026 xG game records:", len(games_2026))

    # 4. Print the latest 10 with team names if mapping worked
    print("\n=== LATEST 10 2026 NWSL GAMES ===")

    games_2026_sorted = sorted(
        games_2026,
        key=lambda g: g.get("date_time_utc", ""),
        reverse=True
    )

    for game in games_2026_sorted[:10]:
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")

        home_name = team_names.get(home_id, home_id)
        away_name = team_names.get(away_id, away_id)

        print(
            f"{game.get('date_time_utc')} | "
            f"{home_name} {game.get('home_goals')} "
            f"({game.get('home_team_xgoals')} xG) - "
            f"{game.get('away_goals')} "
            f"({game.get('away_team_xgoals')} xG) {away_name}"
        )

    # 5. Aggregate season xG/xGA from game records
    team_stats = defaultdict(lambda: {
        "games": 0,
        "xg": 0.0,
        "xga": 0.0,
        "goals": 0,
        "goals_against": 0
    })

    for game in games_2026:
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")

        home_xg = float(game.get("home_team_xgoals", 0) or 0)
        away_xg = float(game.get("away_team_xgoals", 0) or 0)

        home_goals = int(game.get("home_goals", 0) or 0)
        away_goals = int(game.get("away_goals", 0) or 0)

        team_stats[home_id]["games"] += 1
        team_stats[home_id]["xg"] += home_xg
        team_stats[home_id]["xga"] += away_xg
        team_stats[home_id]["goals"] += home_goals
        team_stats[home_id]["goals_against"] += away_goals

        team_stats[away_id]["games"] += 1
        team_stats[away_id]["xg"] += away_xg
        team_stats[away_id]["xga"] += home_xg
        team_stats[away_id]["goals"] += away_goals
        team_stats[away_id]["goals_against"] += home_goals

    print("\n=== 2026 AGGREGATED TEAM STATS ===")

    for team_id, stats in sorted(
        team_stats.items(),
        key=lambda item: item[1]["xg"] / item[1]["games"],
        reverse=True
    ):
        games_count = stats["games"]

        print(
            f"{team_names.get(team_id, team_id)} | "
            f"GP {games_count} | "
            f"xG {stats['xg']:.2f} "
            f"({stats['xg'] / games_count:.2f}/game) | "
            f"xGA {stats['xga']:.2f} "
            f"({stats['xga'] / games_count:.2f}/game) | "
            f"GF {stats['goals']} | "
            f"GA {stats['goals_against']}"
        )


if __name__ == "__main__":
    main()
