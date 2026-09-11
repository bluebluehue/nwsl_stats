#!/usr/bin/env python3
"""Rolling temporal validation for NWSL Decision architecture.

NEW audit only. Requires backtest_decision_architecture_v2.py in repo root.
Does not modify production files.

Expanding folds:
  train 5-10 -> test 11-12
  train 5-12 -> test 13-14
  train 5-14 -> test 15-16
  train 5-16 -> test 17-18
  train 5-18 -> test 19-20
  train 5-20 -> test 21

Searches blend weights in 5% increments and reports:
- rolling single-signal performance
- per-fold train-optimal weights applied to the next holdout block
- most stable fixed weights across all holdout blocks
- current live Decision formula benchmark
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import backtest_decision_architecture_v2 as base

OUT_JSON = Path("decision_rolling_validation.json")
OUT_CSV = Path("decision_rolling_validation_candidates.csv")
STEP = 0.05

FOLDS = [
    ((5, 10), (11, 12)),
    ((5, 12), (13, 14)),
    ((5, 14), (15, 16)),
    ((5, 16), (17, 18)),
    ((5, 18), (19, 20)),
    ((5, 20), (21, 21)),
]

FAMILIES = {
    "GK": {
        "three_way_raw": ["form_raw", "recent_opportunity", "fixture_raw"],
        "three_way_cmp": ["form_cmp", "recent_opportunity", "fixture_cmp"],
        "recent_fixture_raw": ["recent_opportunity", "fixture_raw"],
        "form_fixture_raw": ["form_raw", "fixture_raw"],
    },
    "DEF": {
        "three_way_raw": ["form_raw", "recent_opportunity", "fixture_raw"],
        "three_way_cmp": ["form_cmp", "recent_opportunity", "fixture_cmp"],
        "recent_fixture_raw": ["recent_opportunity", "fixture_raw"],
        "form_fixture_raw": ["form_raw", "fixture_raw"],
    },
    "MID": {
        "three_way_raw": ["form_raw", "recent_opportunity", "fixture_raw"],
        "three_way_cmp": ["form_cmp", "recent_opportunity", "fixture_cmp"],
        "recent_fixture_raw": ["recent_opportunity", "fixture_raw"],
        "form_recent_raw": ["form_raw", "recent_opportunity"],
        "form_fixture_raw": ["form_raw", "fixture_raw"],
    },
    "FOR": {
        "three_way_raw": ["form_raw", "recent_opportunity", "fixture_raw"],
        "three_way_cmp": ["form_cmp", "recent_opportunity", "fixture_cmp"],
        "recent_fixture_raw": ["recent_opportunity", "fixture_raw"],
        "form_recent_raw": ["form_raw", "recent_opportunity"],
        "form_fixture_raw": ["form_raw", "fixture_raw"],
    },
}


def grid(n: int) -> list[tuple[float, ...]]:
    units = round(1 / STEP)
    if n == 2:
        return [(a / units, (units - a) / units) for a in range(units + 1)]
    if n == 3:
        out = []
        for a in range(units + 1):
            for b in range(units + 1 - a):
                c = units - a - b
                out.append((a / units, b / units, c / units))
        return out
    raise ValueError(n)


def in_gws(rows: list[dict[str, Any]], lo: int, hi: int) -> list[dict[str, Any]]:
    return [r for r in rows if lo <= int(r["gw"]) <= hi]


def rho(rows: list[dict[str, Any]], fields: list[str], weights: tuple[float, ...]) -> float | None:
    xs, ys = [], []
    for r in rows:
        vals = [base.safe_float(r.get(f)) for f in fields]
        y = base.safe_float(r.get("target_points"))
        if y is None or any(v is None for v in vals):
            continue
        xs.append(sum(v * w for v, w in zip(vals, weights)))
        ys.append(y)
    return base.spearman(xs, ys) if len(xs) >= 3 else None


def single_rho(rows: list[dict[str, Any]], field: str) -> float | None:
    return rho(rows, [field], (1.0,))


def wdict(fields: list[str], weights: tuple[float, ...]) -> dict[str, float]:
    return {f: round(w, 2) for f, w in zip(fields, weights)}


def best_training(train: list[dict[str, Any]], fields: list[str]) -> tuple[tuple[float, ...], float] | None:
    best = None
    target = 1 / len(fields)
    for weights in grid(len(fields)):
        r = rho(train, fields, weights)
        if r is None:
            continue
        extremeness = sum(abs(w - target) for w in weights)
        candidate = (r, -extremeness, weights)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return None
    return best[2], best[0]


def summarize(vals: list[float]) -> dict[str, Any]:
    return {
        "mean": round(statistics.fmean(vals), 4) if vals else None,
        "median": round(statistics.median(vals), 4) if vals else None,
        "worst": round(min(vals), 4) if vals else None,
        "best": round(max(vals), 4) if vals else None,
        "positive_folds": sum(v > 0 for v in vals),
        "fold_count": len(vals),
    }


def analyze_position(samples: list[dict[str, Any]], pos: str) -> dict[str, Any]:
    rows = [r for r in samples if r["position"] == pos]
    folds = []
    for tr_rng, te_rng in FOLDS:
        tr = in_gws(rows, *tr_rng)
        te = in_gws(rows, *te_rng)
        if len(tr) >= 20 and len(te) >= 10:
            folds.append((tr_rng, te_rng, tr, te))

    out: dict[str, Any] = {
        "samples": len(rows),
        "folds": len(folds),
        "single_signals": {},
        "families": {},
    }

    # Single signals on each rolling holdout.
    for field in [
        "form_raw", "form_cmp", "recent_activity", "recent_role",
        "recent_opportunity", "fixture_raw", "fixture_cmp",
    ]:
        fold_rows = []
        vals = []
        for _, te_rng, _, te in folds:
            r = single_rho(te, field)
            fold_rows.append({"test_gws": list(te_rng), "n": len(te), "rho": round(r, 4) if r is not None else None})
            if r is not None:
                vals.append(r)
        out["single_signals"][field] = {"folds": fold_rows, **summarize(vals)}

    # Current live benchmark.
    live = base.LIVE_DECISION_WEIGHTS[pos]
    live_fields = ["form_raw", "fixture_raw"]
    live_weights = (float(live["form"]), float(live["fixture"]))
    vals, detail = [], []
    for _, te_rng, _, te in folds:
        r = rho(te, live_fields, live_weights)
        detail.append({"test_gws": list(te_rng), "n": len(te), "rho": round(r, 4) if r is not None else None})
        if r is not None:
            vals.append(r)
    out["live_formula"] = {
        "fields": live_fields,
        "weights": wdict(live_fields, live_weights),
        "folds": detail,
        **summarize(vals),
    }

    # Candidate blend families.
    for family, fields in FAMILIES[pos].items():
        per_fold = []
        for tr_rng, te_rng, tr, te in folds:
            best = best_training(tr, fields)
            if not best:
                continue
            weights, train_rho = best
            test_rho = rho(te, fields, weights)
            per_fold.append({
                "train_gws": list(tr_rng),
                "test_gws": list(te_rng),
                "train_n": len(tr),
                "test_n": len(te),
                "weights": wdict(fields, weights),
                "train_rho": round(train_rho, 4),
                "test_rho": round(test_rho, 4) if test_rho is not None else None,
            })

        candidates = []
        for weights in grid(len(fields)):
            fold_rhos = []
            for _, _, _, te in folds:
                r = rho(te, fields, weights)
                fold_rhos.append(r)
            numeric = [v for v in fold_rhos if v is not None]
            if not numeric:
                continue
            s = summarize(numeric)
            candidates.append({
                "weights": wdict(fields, weights),
                "fold_rhos": [round(v, 4) if v is not None else None for v in fold_rhos],
                "mean": s["mean"],
                "median": s["median"],
                "worst": s["worst"],
                "best": s["best"],
                "positive_folds": s["positive_folds"],
                "fold_count": s["fold_count"],
                "std": round(statistics.pstdev(numeric), 4) if len(numeric) > 1 else 0.0,
            })

        candidates.sort(key=lambda c: (c["mean"], c["worst"], -c["std"]), reverse=True)
        out["families"][family] = {
            "fields": fields,
            "per_fold_train_optimum": per_fold,
            "stable_top10": candidates[:10],
        }

    return out


def pr(v: Any) -> str:
    return "NA" if v is None else f"{float(v):.4f}"


def print_position(pos: str, r: dict[str, Any]) -> None:
    print()
    print(f"=== {pos}: {r['samples']} samples | {r['folds']} rolling folds ===")
    print("Single-signal rolling holdout means:")
    for field in ["form_raw", "recent_activity", "recent_role", "recent_opportunity", "fixture_raw", "fixture_cmp"]:
        s = r["single_signals"][field]
        print(f"  {field:20s} mean={pr(s['mean'])} median={pr(s['median'])} worst={pr(s['worst'])} positive={s['positive_folds']}/{s['fold_count']}")

    live = r["live_formula"]
    print(f"Current live {live['weights']} -> mean={pr(live['mean'])} median={pr(live['median'])} worst={pr(live['worst'])}")

    for family, fam in r["families"].items():
        print()
        print(f"{family} | {fam['fields']}")
        print("  Per-fold TRAIN optimum -> TEST:")
        for f in fam["per_fold_train_optimum"]:
            print(
                f"    train {f['train_gws'][0]}-{f['train_gws'][1]} -> test {f['test_gws'][0]}-{f['test_gws'][1]} | "
                f"{f['weights']} | train={pr(f['train_rho'])} test={pr(f['test_rho'])}"
            )
        print("  Most stable FIXED weights across all holdouts:")
        for i, c in enumerate(fam["stable_top10"][:5], 1):
            print(
                f"    #{i} {c['weights']} | mean={pr(c['mean'])} median={pr(c['median'])} "
                f"worst={pr(c['worst'])} positive={c['positive_folds']}/{c['fold_count']} folds={c['fold_rhos']}"
            )


def write_csv(results: dict[str, Any]) -> None:
    rows = []
    for pos, rr in results.items():
        for family, fam in rr["families"].items():
            for rank, c in enumerate(fam["stable_top10"], 1):
                rows.append({
                    "position": pos,
                    "family": family,
                    "rank": rank,
                    "fields": " | ".join(fam["fields"]),
                    "weights": json.dumps(c["weights"], sort_keys=True),
                    "mean_holdout_rho": c["mean"],
                    "median_holdout_rho": c["median"],
                    "worst_holdout_rho": c["worst"],
                    "std_holdout_rho": c["std"],
                    "positive_folds": c["positive_folds"],
                    "fold_count": c["fold_count"],
                    "fold_rhos": json.dumps(c["fold_rhos"]),
                })
    fields = list(rows[0]) if rows else ["position"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    print("=== NWSL DECISION ROLLING TEMPORAL VALIDATION ===")
    print("Uses v2's exact historical sample construction.")
    print("Weight search step: 5%.")
    print("No production model files are changed.")
    print()

    c = base.Client()
    fantasy = c.fantasy()
    fgames = fantasy.get("games", []) or []
    fplayers = fantasy.get("players", []) or []
    print(f"Fantasy API: {len(fgames)} games, {len(fplayers)} players.")

    fotmob, fot_errors = base.fetch_fotmob_matches(c)
    fmap, map_diag = base.map_fantasy_to_fotmob(fgames, fotmob)
    print(f"FotMob: {len(fotmob)} completed; {len(fot_errors)} fetch errors.")
    print(f"Fantasy->FotMob: {map_diag['mapped']}/{map_diag['completed_fantasy_games']} mapped; {len(map_diag['unresolved'])} unresolved.")

    team_history = base.build_team_history(fotmob)
    asa_games, asa_team_map = base.fetch_asa(c)
    asa_regular = base.filter_asa_to_regular_season(asa_games, asa_team_map, fotmob)
    print(f"ASA: {len(asa_games)} 2026 records; {len(asa_regular)} regular-season matches aligned.")

    samples, sample_diag = base.build_samples(fplayers, fgames, team_history, asa_regular, asa_team_map)
    base.add_same_gw_percentiles(samples)
    print(f"Historical player-GW samples built: {len(samples)}")

    results = {pos: analyze_position(samples, pos) for pos in ("GK", "DEF", "MID", "FOR")}

    print()
    print("=== ROLLING VALIDATION SUMMARY ===")
    for pos in ("GK", "DEF", "MID", "FOR"):
        print_position(pos, results[pos])
    print()
    print("=== END ROLLING VALIDATION SUMMARY ===")

    payload = {
        "metadata": {
            "version": "nwsl-decision-rolling-validation-v1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "production_changed": False,
            "weight_step": STEP,
            "folds": [{"train": list(a), "test": list(b)} for a, b in FOLDS],
            "recent_window": "previous 4 team matches",
            "fantasy_games": len(fgames),
            "fantasy_players": len(fplayers),
            "fotmob_completed_matches": len(fotmob),
            "fantasy_fotmob_mapped": map_diag["mapped"],
            "asa_regular_aligned": len(asa_regular),
            "player_gw_samples": len(samples),
        },
        "sample_diagnostics": sample_diag,
        "results": results,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(results)
    print(f"Saved: {OUT_JSON}")
    print(f"Saved: {OUT_CSV}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise
