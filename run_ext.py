"""Run a post-Phase-1 experiment: Phase 2 interventions or the label-robustness check.

  python run_ext.py --config config_phase2.mock.toml --out results/mock_p2      # fake data, no API calls
  python run_ext.py --config config_phase2.toml --dry-run                       # plan, cost, one prompt
  python run_ext.py --config config_phase2.toml --pilot --out results/p2_pilot  # small attended pilot
  python run_ext.py --config config_phase2.toml --out results/phase2            # full run (resumable)

The experiment is chosen by `kind` in [experiment] ("phase2" or "labels"). Phase 1 uses run.py.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from lab import labels, phase2, runner

MODULES = {"phase2": phase2, "labels": labels}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    exp = cfg.setdefault("experiment", {})
    kind = exp.get("kind")
    if kind not in MODULES:
        sys.exit(f"set kind in [experiment] to one of {list(MODULES)}")
    for k, v in {"trials": 48, "temperature": 1.0, "max_tokens": 300, "seed": 0, "concurrency": 8}.items():
        exp.setdefault(k, v)
    if not cfg.get("models"):
        sys.exit("config has no [[models]] entries")
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out")
    ap.add_argument("--pilot", action="store_true", help="small pilot (see each module's PILOT settings)")
    ap.add_argument("--trials", type=int)
    ap.add_argument("--models", help="only these model names, comma-separated")
    ap.add_argument("--max-calls", type=int)
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="plan, cost estimate, one prompt; no API calls")
    args = ap.parse_args()

    cfg = load_config(args.config)
    exp = cfg["experiment"]
    mod = MODULES[exp["kind"]]
    if args.pilot:
        exp.update(mod.PILOT)
    if args.trials:
        exp["trials"] = args.trials
    mod.validate(exp)
    models = cfg["models"]
    if args.models:
        keep = set(args.models.split(","))
        models = [m for m in models if m["name"] in keep]

    plan = mod.trials([m["name"] for m in models], exp)
    pf_items = 0 if args.skip_preflight else len(list(mod.preflight_items("_", exp)))
    n_calls = len(models) * pf_items if args.preflight_only else \
        sum(mod.calls_per_trial(t) for t in plan) + len(models) * pf_items
    print(f"experiment={mod.NAME}  models={[m['name'] for m in models]}")
    print(mod.describe(exp))
    print(f"{len(plan)} trials + {len(models) * pf_items} preflight items = {n_calls} calls")
    for line in runner.estimate_cost(plan, pf_items, models, cfg.get("monitor")):
        print(line)

    max_calls = args.max_calls if args.max_calls is not None else exp.get("max_calls")
    if max_calls is not None and n_calls > max_calls:
        sys.exit(f"stopped: plan is {n_calls} calls, above the limit of {max_calls} (--max-calls / max_calls)")

    if args.dry_run:
        first = plan[0]
        print("\n--- example prompt ---\n" + first["prompt"] + "\n--- end ---")
        return

    out = Path(args.out or f"results/{mod.NAME}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    asyncio.run(runner.run(mod, cfg, out, models, args.skip_preflight, args.preflight_only))
    print(f"done. analyze with:  python {'analyze_phase2.py' if mod is phase2 else 'analyze.py'} {out}")


if __name__ == "__main__":
    main()
