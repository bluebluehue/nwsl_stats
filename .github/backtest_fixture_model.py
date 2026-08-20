import requests
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

FANTASY_URL = "https://api.fantasynwsl.com/graphql"
ASA_BASE_URL = "https://app.americansocceranalysis.com/api/v1"

BACKTEST_START_GW = 7
BACKTEST_END_GW = 18
FORM_RECENT_GAMES = 4

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

CURRENT_PARAMS = {
    "name": "current_v2",
    "recent_match_count": 6,
    "recency_decay": 0.82,
    "season_weight": 0.65,
    "attack_xg_weight": 0.75,
    "defense_xga_weight": 0.80,
    "dgw_exponent": 0.50,
}

MODEL_VARIANTS = [
    CURRENT_PARAMS,
    {**CURRENT_PARAMS, "name": "more_recent_50_50", "season_weight": 0.50},
    {**CURRENT_PARAMS, "name": "more_season_80_20", "season_weight": 0.80},
    {**CURRENT_PARAMS, "name": "recent_4", "recent_match_count": 4},
    {**CURRENT_PARAMS, "name": "recent_8", "recent_match_count": 8},
    {**CURRENT_PARAMS, "name": "faster_decay_070", "recency_decay": 0.70},
    {**CURRENT_PARAMS, "name": "slower_decay_090", "recency_decay": 0.90},
    {**CURRENT_PARAMS, "name": "xg_heavier_90", "attack_xg_weight": 0.90, "defense_xga_weight": 0.90},
    {**CURRENT_PARAMS, "name": "pure_xg", "attack_xg_weight": 1.00, "defense_xga_weight": 1.00},
    {**CURRENT_PARAMS, "name": "dgw_exp_035", "dgw_exponent": 0.35},
    {**CURRENT_PARAMS, "name": "dgw_exp_040", "dgw_exponent": 0.40},
    {**CURRENT_PARAMS, "name": "dgw_exp_060", "dgw_exponent": 0.60},
]

ATTACK_XG_FLOOR = 0.55
ATTACK_XG_CEILING = 2.25
DEFENSE_XG_BEST = 0.55
DEFENSE_XG_WORST = 2.25


def clamp(value, low, high):
    return max(low, min(high, value))


def weighted_average(values, decay):
    if not values:
        return 0.0
    weights = [decay ** i for i in range(len(values))]
    denom = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / denom if denom else sum(values) / len(values)


def graphql(query):
    response = requests.post(FANTASY_URL, json={"query": query}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def get_fantasy_data():
    print("Fetching Fantasy NWSL players + games...")
    query = r"""
    {
      games {
        id
        scheduledAt
        hasStarted
        stage { id }
        home {
          party {
            __typename
            ... on Club { id shortName name }
          }
          score
        }
        away {
          party {
            __typename
            ... on Club { id shortName name }
          }
          score
        }
      }
      players {
        slug
        firstName
        lastName
        club { id shortName }
        position
        visionaryNextStage
        performanceV2 {
          games {
            game {
              id
              scheduledAt
              stage { id }
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
            }
            points
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
    data = graphql(query)
    print(f"Loaded {len(data['players'])} players and {len(data['games'])} Fantasy games.")
    return data["players"], data["games"]


def get_asa_json(path):
    response = requests.get(f"{ASA_BASE_URL}{path}", timeout=60)
    response.raise_for_status()
    return response.json()


def get_asa_data():
    print("Fetching ASA NWSL team map + xG...")
    teams = get_asa_json("/nwsl/teams")
    all_xg = get_asa_json("/nwsl/games/xgoals")

    team_map = {}
    for team in teams:
        code = ASA_TEAM_NAME_TO_FANTASY_CODE.get(team.get("team_name"))
        if code and team.get("team_id"):
            team_map[team["team_id"]] = code

    xg_2026 = [
        g for g in all_xg
        if str(g.get("date_time_utc", "")).startswith("2026-")
    ]
    print(f"Loaded {len(xg_2026)} ASA 2026 xG games.")
    return team_map, xg_2026


def parse_fantasy_date(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def parse_asa_date(text):
    return datetime.strptime(
        str(text).replace(" UTC", ""),
        "%Y-%m-%d %H:%M:%S",
    ).date()


def enrich_asa_with_fantasy_gw(asa_games, asa_team_map, fantasy_games):
    """
    Match ASA regular-season games to Fantasy games by home/away teams
    and a date difference of <= 1 day. The Fantasy schedule is the source
    of truth for regular-season membership and GW number.
    """
    fantasy_candidates = []
    for game in fantasy_games:
        home = str(game.get("home", {}).get("party", {}).get("id", "")).upper()
        away = str(game.get("away", {}).get("party", {}).get("id", "")).upper()
        scheduled = game.get("scheduledAt")
        stage = game.get("stage", {}).get("id")
        if not home or not away or not scheduled or stage is None:
            continue
        try:
            dt = parse_fantasy_date(scheduled)
            gw = int(stage)
        except Exception:
            continue
        fantasy_candidates.append({
            "home": home, "away": away, "date": dt, "gw": gw, "game": game
        })

    matched = []
    used = set()

    for asa in asa_games:
        home = asa_team_map.get(asa.get("home_team_id"))
        away = asa_team_map.get(asa.get("away_team_id"))
        if not home or not away:
            continue
        try:
            adate = parse_asa_date(asa.get("date_time_utc", ""))
        except Exception:
            continue

        best = None
        for idx, fg in enumerate(fantasy_candidates):
            if idx in used:
                continue
            if fg["home"] != home or fg["away"] != away:
                continue
            diff = abs((fg["date"] - adate).days)
            if diff > 1:
                continue
            if best is None or diff < best[0]:
                best = (diff, idx, fg)

        if best is not None:
            _, idx, fg = best
            used.add(idx)
            rec = dict(asa)
            rec["fantasy_gw"] = fg["gw"]
            rec["fantasy_home"] = home
            rec["fantasy_away"] = away
            matched.append(rec)

    print(f"Matched {len(matched)} ASA games to Fantasy regular-season GWs.")
    return matched


def build_team_strength(prior_games, params):
    team_games = defaultdict(list)
    home_xg = []
    away_xg = []

    for g in prior_games:
        home = g["fantasy_home"]
        away = g["fantasy_away"]
        try:
            hxg = float(g.get("home_team_xgoals", 0) or 0)
            axg = float(g.get("away_team_xgoals", 0) or 0)
            hg = int(g.get("home_goals", 0) or 0)
            ag = int(g.get("away_goals", 0) or 0)
            dt = parse_asa_date(g.get("date_time_utc", ""))
        except Exception:
            continue

        home_xg.append(hxg)
        away_xg.append(axg)

        team_games[home].append({
            "date": dt, "xg_for": hxg, "xg_against": axg,
            "goals_for": hg, "goals_against": ag,
        })
        team_games[away].append({
            "date": dt, "xg_for": axg, "xg_against": hxg,
            "goals_for": ag, "goals_against": hg,
        })

    strength = {}
    sw = params["season_weight"]
    rw = 1.0 - sw
    attack_xg_w = params["attack_xg_weight"]
    defense_xga_w = params["defense_xga_weight"]

    for team, matches in team_games.items():
        matches = sorted(matches, key=lambda x: x["date"], reverse=True)
        n = len(matches)
        if n == 0:
            continue

        season_xg = sum(m["xg_for"] for m in matches) / n
        season_xga = sum(m["xg_against"] for m in matches) / n
        season_gf = sum(m["goals_for"] for m in matches) / n
        season_ga = sum(m["goals_against"] for m in matches) / n

        recent = matches[:params["recent_match_count"]]
        decay = params["recency_decay"]
        recent_xg = weighted_average([m["xg_for"] for m in recent], decay)
        recent_xga = weighted_average([m["xg_against"] for m in recent], decay)
        recent_gf = weighted_average([m["goals_for"] for m in recent], decay)
        recent_ga = weighted_average([m["goals_against"] for m in recent], decay)

        season_attack = attack_xg_w * season_xg + (1 - attack_xg_w) * season_gf
        recent_attack = attack_xg_w * recent_xg + (1 - attack_xg_w) * recent_gf
        season_def = defense_xga_w * season_xga + (1 - defense_xga_w) * season_ga
        recent_def = defense_xga_w * recent_xga + (1 - defense_xga_w) * recent_ga

        strength[team] = {
            "attack_metric": sw * season_attack + rw * recent_attack,
            "defense_allowed_metric": sw * season_def + rw * recent_def,
            "games_played": n,
        }

    if not strength:
        return {}, {}

    league_attack = sum(v["attack_metric"] for v in strength.values()) / len(strength)
    league_def = sum(v["defense_allowed_metric"] for v in strength.values()) / len(strength)

    # Same approach as live v2: league average xG from team season xG.
    all_team_xg = []
    for team, matches in team_games.items():
        if matches:
            all_team_xg.append(sum(m["xg_for"] for m in matches) / len(matches))
    league_xg = sum(all_team_xg) / len(all_team_xg)

    avg_home = sum(home_xg) / len(home_xg) if home_xg else league_xg
    avg_away = sum(away_xg) / len(away_xg) if away_xg else league_xg
    home_factor = clamp((avg_home / avg_away) ** 0.5, 0.90, 1.10) if avg_home > 0 and avg_away > 0 else 1.0

    context = {
        "league_avg_attack": league_attack,
        "league_avg_defense_allowed": league_def,
        "league_avg_xg": league_xg,
        "home_factor": home_factor,
        "away_factor": 1 / home_factor if home_factor else 1.0,
    }
    return strength, context


def projected_xg(att_team, def_team, is_home, strength, context):
    a = strength.get(att_team)
    d = strength.get(def_team)
    if not a or not d:
        return None

    la = context["league_avg_attack"]
    ld = context["league_avg_defense_allowed"]
    lxg = context["league_avg_xg"]
    if la <= 0 or ld <= 0 or lxg <= 0:
        return None

    attack_index = a["attack_metric"] / la
    weakness_index = d["defense_allowed_metric"] / ld
    matchup_index = max(0.01, attack_index * weakness_index) ** 0.5
    venue = context["home_factor"] if is_home else context["away_factor"]
    return clamp(lxg * matchup_index * venue, 0.05, 4.0)


def attack_score(pxg):
    if pxg is None:
        return 50.0
    return clamp((pxg - ATTACK_XG_FLOOR) / (ATTACK_XG_CEILING - ATTACK_XG_FLOOR) * 100, 0, 100)


def defense_score(pxga):
    if pxga is None:
        return 50.0
    return clamp((DEFENSE_XG_WORST - pxga) / (DEFENSE_XG_WORST - DEFENSE_XG_BEST) * 100, 0, 100)


def score_fixture(position, team, opponent, is_home, strength, context):
    team_xg = projected_xg(team, opponent, is_home, strength, context)
    opp_xg = projected_xg(opponent, team, not is_home, strength, context)
    a = attack_score(team_xg)
    d = defense_score(opp_xg)

    if position == "FOR":
        rating = a
    elif position == "MID":
        rating = 0.90 * a + 0.10 * d
    elif position in ("DEF", "GK"):
        rating = d
    else:
        return None
    return clamp(rating, 0, 100)


def combine_fixture_scores(scores, exponent):
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    if len(scores) > 1:
        avg *= len(scores) ** exponent
    return clamp(avg, 0, 100)


def position_code(raw):
    mapping = {
        "GOALKEEPER": "GK",
        "DEFENDER": "DEF",
        "MIDFIELDER": "MID",
        "FORWARD": "FOR",
    }
    raw = str(raw or "").upper().strip()
    return mapping.get(raw, raw[:3])


def extract_player_history(player):
    """
    Returns:
      games_by_gw: gw -> list of game_data
      visionary_by_gw: gw -> awarded Visionary points
      flat_games: individual game records with gw/points/bonus/team-match info
    """
    games_by_gw = defaultdict(list)
    visionary_by_gw = defaultdict(int)
    flat_games = []

    for perf in player.get("performanceV2", []) or []:
        perf_games = perf.get("games", []) or []
        perf_gws = set()

        for gd in perf_games:
            try:
                gw = int(gd.get("game", {}).get("stage", {}).get("id"))
            except Exception:
                continue
            perf_gws.add(gw)
            games_by_gw[gw].append(gd)

            got_bonus = any(
                c.get("contribution") == "Bonus" and (c.get("quantity", 0) or 0) > 0
                for c in gd.get("contributions", []) or []
            )

            flat_games.append({
                "gw": gw,
                "points": float(gd.get("points", 0) or 0),
                "got_bonus": got_bonus,
                "game": gd.get("game", {}),
            })

        vpts = 0
        for c in (perf.get("extras", {}) or {}).get("contributions", []) or []:
            if c.get("contribution") == "Visionary":
                vpts += (c.get("quantity", 1) or 0) * (c.get("individualPoints", 0) or 0)

        if vpts and len(perf_gws) == 1:
            visionary_by_gw[next(iter(perf_gws))] += vpts

    return games_by_gw, visionary_by_gw, flat_games


def actual_gw_points(games_by_gw, visionary_by_gw, gw):
    base = sum(float(g.get("points", 0) or 0) for g in games_by_gw.get(gw, []))
    return base + visionary_by_gw.get(gw, 0)


def played_in_prior_n_gws(games_by_gw, target_gw, n=2):
    return any(games_by_gw.get(gw) for gw in range(max(1, target_gw - n), target_gw))


def current_club_is_plausible_for_history(player, flat_games, target_gw, lookback=4):
    """
    Midseason transfers are the one historical-roster detail the API query does
    not expose. Avoid contaminating the backtest by skipping old player-weeks
    when the player's current club does not appear in any of their recent games.
    """
    current = str(player.get("club", {}).get("id", "")).upper()
    if not current:
        return False

    recent = [g for g in flat_games if g["gw"] < target_gw]
    recent.sort(key=lambda r: r["gw"], reverse=True)
    recent = recent[:lookback]
    if not recent:
        return True

    for rec in recent:
        game = rec["game"]
        home = str(game.get("home", {}).get("party", {}).get("id", "")).upper()
        away = str(game.get("away", {}).get("party", {}).get("id", "")).upper()
        if current == home or current == away:
            return True
    return False


def calculate_form_rating_as_of(flat_games, visionary_by_gw, target_gw):
    prior = [g for g in flat_games if g["gw"] < target_gw]
    prior.sort(key=lambda r: r["gw"], reverse=True)
    recent = prior[:FORM_RECENT_GAMES]
    if not recent:
        return 0

    points = []
    used_vgw = set()
    bonus_games = 0

    for rec in recent:
        pts = rec["points"]
        gw = rec["gw"]
        if gw in visionary_by_gw and gw not in used_vgw:
            pts += visionary_by_gw[gw]
            used_vgw.add(gw)
        points.append(pts)
        if rec["got_bonus"]:
            bonus_games += 1

    count = len(points)
    recent_ppg = sum(points) / count
    ppg_score = clamp((recent_ppg / 8.0) * 100, 0, 100)
    consistency = sum(1 for p in points if p >= 3) / count * 100
    bonus_score = bonus_games / count * 100

    rating = round(0.50 * ppg_score + 0.35 * consistency + 0.15 * bonus_score)

    last_played_gw = max((g["gw"] for g in prior), default=None)
    latest_gw = target_gw - 1
    if last_played_gw is not None:
        missed = latest_gw - last_played_gw
        if missed == 2:
            rating = round(rating * 0.5)
        elif missed > 2:
            rating = 0

    return rating


def fantasy_fixture_map_by_gw(fantasy_games):
    out = defaultdict(list)
    for g in fantasy_games:
        try:
            gw = int(g.get("stage", {}).get("id"))
        except Exception:
            continue
        home = str(g.get("home", {}).get("party", {}).get("id", "")).upper()
        away = str(g.get("away", {}).get("party", {}).get("id", "")).upper()
        if home and away:
            out[gw].append({"home": home, "away": away})
    return out


def fixtures_for_team(fixture_map, gw, team):
    result = []
    for f in fixture_map.get(gw, []):
        if f["home"] == team:
            result.append((f["away"], True))
        elif f["away"] == team:
            result.append((f["home"], False))
    return result


def rankdata(values):
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    denom = math.sqrt(sum(x*x for x in dx) * sum(y*y for y in dy))
    if denom == 0:
        return None
    return sum(x*y for x, y in zip(dx, dy)) / denom


def spearman(xs, ys):
    if len(xs) < 3:
        return None
    return pearson(rankdata(xs), rankdata(ys))


def mean(values):
    return sum(values) / len(values) if values else None


def summarize_rows(rows):
    x_fix = [r["fixture_rating"] for r in rows if r["fixture_rating"] is not None]
    y_fix = [r["actual_points"] for r in rows if r["fixture_rating"] is not None]
    x_form = [r["form_rating"] for r in rows]
    y_form = [r["actual_points"] for r in rows]

    out = {
        "n": len(rows),
        "avg_actual_points": mean([r["actual_points"] for r in rows]),
        "fixture_spearman": spearman(x_fix, y_fix),
        "form_spearman": spearman(x_form, y_form),
    }

    # 50/50 starter combined score.
    c = [(0.5 * r["fixture_rating"] + 0.5 * r["form_rating"], r["actual_points"])
         for r in rows if r["fixture_rating"] is not None]
    if c:
        out["combined_50_50_spearman"] = spearman([a for a, _ in c], [b for _, b in c])
    else:
        out["combined_50_50_spearman"] = None
    return out


def evaluate_variant(players, fantasy_games, matched_asa, params):
    fixture_map = fantasy_fixture_map_by_gw(fantasy_games)
    histories = {}
    for p in players:
        histories[p.get("slug") or (p.get("firstName","") + p.get("lastName",""))] = extract_player_history(p)

    rows = []

    for target_gw in range(BACKTEST_START_GW, BACKTEST_END_GW + 1):
        prior_asa = [g for g in matched_asa if g["fantasy_gw"] < target_gw]
        strength, context = build_team_strength(prior_asa, params)
        if not strength:
            continue

        for p in players:
            key = p.get("slug") or (p.get("firstName","") + p.get("lastName",""))
            games_by_gw, visionary_by_gw, flat_games = histories[key]

            team = str(p.get("club", {}).get("id", "")).upper()
            pos = position_code(p.get("position"))
            if not team or pos not in ("GK", "DEF", "MID", "FOR"):
                continue

            # Avoid historical transfer contamination.
            if not current_club_is_plausible_for_history(p, flat_games, target_gw):
                continue

            team_fixtures = fixtures_for_team(fixture_map, target_gw, team)
            if not team_fixtures:
                fixture_rating = None
                fixture_count = 0
            else:
                fscores = []
                for opp, is_home in team_fixtures:
                    s = score_fixture(pos, team, opp, is_home, strength, context)
                    if s is not None:
                        fscores.append(s)
                fixture_rating = combine_fixture_scores(fscores, params["dgw_exponent"]) if fscores else None
                fixture_count = len(fscores)

            form = calculate_form_rating_as_of(flat_games, visionary_by_gw, target_gw)
            actual = actual_gw_points(games_by_gw, visionary_by_gw, target_gw)
            recent2 = played_in_prior_n_gws(games_by_gw, target_gw, 2)

            rows.append({
                "model": params["name"],
                "gw": target_gw,
                "name": f"{p.get('firstName','')} {p.get('lastName','')}".strip(),
                "club": team,
                "position": pos,
                "visionary": bool(p.get("visionaryNextStage")),
                "fixture_count": fixture_count,
                "fixture_rating": round(fixture_rating, 2) if fixture_rating is not None else None,
                "form_rating": form,
                "actual_points": actual,
                "played_prior_2_gws": recent2,
                "played_target_gw": bool(games_by_gw.get(target_gw)),
            })

    return rows


def write_csv(path, rows):
    if not rows:
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def analyze_baseline(rows):
    eligible = [
        r for r in rows
        if r["played_prior_2_gws"] and r["fixture_rating"] is not None
    ]

    summary = {
        "definition": "Core sample = players who appeared in either of the prior 2 GWs, using only pre-GW information. Target-GW nonappearances remain as 0 points.",
        "overall": summarize_rows(eligible),
        "by_position": {},
        "by_gw": {},
        "fixture_bands": {},
        "dgw_vs_sgw": {},
        "top_k": {},
    }

    for pos in ("GK", "DEF", "MID", "FOR"):
        subset = [r for r in eligible if r["position"] == pos]
        summary["by_position"][pos] = summarize_rows(subset)

    for gw in range(BACKTEST_START_GW, BACKTEST_END_GW + 1):
        subset = [r for r in eligible if r["gw"] == gw]
        if subset:
            summary["by_gw"][str(gw)] = summarize_rows(subset)

    bands = [
        ("0-39", 0, 40),
        ("40-59", 40, 60),
        ("60-69", 60, 70),
        ("70-79", 70, 80),
        ("80-100", 80, 101),
    ]
    for label, lo, hi in bands:
        subset = [r for r in eligible if lo <= r["fixture_rating"] < hi]
        summary["fixture_bands"][label] = {
            "n": len(subset),
            "avg_points": mean([r["actual_points"] for r in subset]),
            "pct_5_plus": (sum(r["actual_points"] >= 5 for r in subset) / len(subset)) if subset else None,
        }

    for label, predicate in [
        ("SGW", lambda r: r["fixture_count"] == 1),
        ("DGW_plus", lambda r: r["fixture_count"] >= 2),
    ]:
        subset = [r for r in eligible if predicate(r)]
        summary["dgw_vs_sgw"][label] = {
            "n": len(subset),
            "avg_points": mean([r["actual_points"] for r in subset]),
            "fixture_spearman": spearman(
                [r["fixture_rating"] for r in subset],
                [r["actual_points"] for r in subset],
            ) if subset else None,
        }

    for k in (10, 25, 50):
        for metric in ("fixture_rating", "form_rating"):
            ranked = sorted(eligible, key=lambda r: r[metric], reverse=True)[:k]
            summary["top_k"][f"{metric}_top_{k}"] = {
                "avg_points": mean([r["actual_points"] for r in ranked]),
                "pct_5_plus": sum(r["actual_points"] >= 5 for r in ranked) / len(ranked) if ranked else None,
            }

    # Combined 50/50 top-k
    scored = sorted(
        eligible,
        key=lambda r: 0.5 * r["fixture_rating"] + 0.5 * r["form_rating"],
        reverse=True,
    )
    for k in (10, 25, 50):
        ranked = scored[:k]
        summary["top_k"][f"combined_50_50_top_{k}"] = {
            "avg_points": mean([r["actual_points"] for r in ranked]),
            "pct_5_plus": sum(r["actual_points"] >= 5 for r in ranked) / len(ranked) if ranked else None,
        }

    return summary, eligible


def combined_weight_grid(eligible):
    rows = []
    for pos_scope in ("ALL", "GK", "DEF", "MID", "FOR"):
        subset = eligible if pos_scope == "ALL" else [r for r in eligible if r["position"] == pos_scope]
        if len(subset) < 10:
            continue
        for fixture_weight_int in range(0, 11):
            fw = fixture_weight_int / 10
            scores = [fw * r["fixture_rating"] + (1-fw) * r["form_rating"] for r in subset]
            actual = [r["actual_points"] for r in subset]
            corr = spearman(scores, actual)

            ranked = sorted(
                subset,
                key=lambda r: fw * r["fixture_rating"] + (1-fw) * r["form_rating"],
                reverse=True,
            )
            top25 = ranked[:25]
            rows.append({
                "position_scope": pos_scope,
                "fixture_weight": fw,
                "form_weight": 1-fw,
                "n": len(subset),
                "spearman": round(corr, 4) if corr is not None else None,
                "top25_avg_points": round(mean([r["actual_points"] for r in top25]), 3) if top25 else None,
                "top25_pct_5_plus": round(sum(r["actual_points"] >= 5 for r in top25) / len(top25), 3) if top25 else None,
            })
    return rows


def model_variant_summary(all_variant_rows):
    result = []
    for model_name, rows in all_variant_rows.items():
        eligible = [
            r for r in rows
            if r["played_prior_2_gws"] and r["fixture_rating"] is not None
        ]
        corr = spearman(
            [r["fixture_rating"] for r in eligible],
            [r["actual_points"] for r in eligible],
        ) if eligible else None
        top25 = sorted(eligible, key=lambda r: r["fixture_rating"], reverse=True)[:25]
        result.append({
            "model": model_name,
            "n": len(eligible),
            "fixture_spearman": round(corr, 4) if corr is not None else None,
            "top25_avg_points": round(mean([r["actual_points"] for r in top25]), 3) if top25 else None,
            "top25_pct_5_plus": round(sum(r["actual_points"] >= 5 for r in top25) / len(top25), 3) if top25 else None,
        })
    return result


def main():
    players, fantasy_games = get_fantasy_data()
    asa_map, asa_games = get_asa_data()
    matched_asa = enrich_asa_with_fantasy_gw(asa_games, asa_map, fantasy_games)

    all_variant_rows = {}
    baseline_rows = None

    for params in MODEL_VARIANTS:
        print(f"Backtesting model: {params['name']}")
        rows = evaluate_variant(players, fantasy_games, matched_asa, params)
        all_variant_rows[params["name"]] = rows
        if params["name"] == "current_v2":
            baseline_rows = rows

    write_csv("backtest_player_weeks.csv", baseline_rows)

    summary, eligible = analyze_baseline(baseline_rows)
    summary["backtest_gw_range"] = [BACKTEST_START_GW, BACKTEST_END_GW]
    summary["model_params"] = CURRENT_PARAMS

    with open("backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    combo_rows = combined_weight_grid(eligible)
    write_csv("backtest_combined_weight_grid.csv", combo_rows)

    variants = model_variant_summary(all_variant_rows)
    write_csv("backtest_model_variants.csv", variants)

    print("\n=== BASELINE CURRENT V2 ===")
    print(json.dumps(summary["overall"], indent=2))

    print("\n=== MODEL VARIANTS ===")
    for row in sorted(
        variants,
        key=lambda r: (r["fixture_spearman"] if r["fixture_spearman"] is not None else -999),
        reverse=True,
    ):
        print(row)

    print("\n=== BEST COMBINED WEIGHTS BY SPEARMAN ===")
    for scope in ("ALL", "GK", "DEF", "MID", "FOR"):
        candidates = [r for r in combo_rows if r["position_scope"] == scope and r["spearman"] is not None]
        if not candidates:
            continue
        best = max(candidates, key=lambda r: r["spearman"])
        print(scope, best)

    print("\nCreated:")
    print("  backtest_player_weeks.csv")
    print("  backtest_summary.json")
    print("  backtest_combined_weight_grid.csv")
    print("  backtest_model_variants.csv")


if __name__ == "__main__":
    main()
