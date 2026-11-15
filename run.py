"""Run the deception lab.

Examples:
  python run.py --config config.mock.toml --out results/mock            # test pipeline, no API calls
  python run.py --config config.toml --dry-run                          # show counts + one prompt, no calls
  python run.py --config config.toml --pilot --out results/pilot        # 10 trials/cell, grid1 only
  python run.py --config config.toml --out results/main                 # full run (resumable)
  python run.py --config config.toml --max-calls 500 ...                # refuse to start above 500 calls

Results are appended to <out>/trials.jsonl as they finish. Re-running the same command skips trials
that already succeeded, so an interrupted overnight run can simply be restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import httpx

from lab.design import ARMS, FRAMINGS, GRIDS, WORDINGS, enumerate_trials
from lab.parse import parse_reply, score
from lab.preflight import n_preflight_calls, run_preflight
from lab.providers import AdaptiveLimiter, FatalProviderError, ProviderError, make_provider


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    exp = cfg.setdefault("experiment", {})
    exp.setdefault("trials", 50)
    exp.setdefault("temperature", 1.0)
    exp.setdefault("max_tokens", 300)
    exp.setdefault("grids", ["grid1"])
    exp.setdefault("arms", list(ARMS))
    exp.setdefault("framings", ["neutral"])
    exp.setdefault("seed", 0)
    exp.setdefault("concurrency", 8)
    exp.setdefault("counterbalance", True)
    exp.setdefault("wordings", ["original"])   # first one is primary; others are robustness checks
    for g in exp["grids"]:
        if g not in GRIDS:
            sys.exit(f"unknown grid {g}; choose from {list(GRIDS)}")
    for fr in exp["framings"]:
        if fr not in FRAMINGS:
            sys.exit(f"unknown framing {fr}; choose from {list(FRAMINGS)}")
    for w in exp["wordings"]:
        if w not in WORDINGS:
            sys.exit(f"unknown wording {w}; choose from {list(WORDINGS)}")
    if not cfg.get("models"):
        sys.exit("config has no [[models]] entries")
    return cfg


def done_ids(path: Path, model_ids: dict[str, str]) -> set[str]:
    """Trials already finished with the same model ID. (The prompt version is part of the trial ID, so
    an edited prompt never counts as done.) A record from a different model ID under the same name is
    not reused."""
    ids = set()
    if path.exists():
        with path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "ok" and rec.get("model_id") == model_ids.get(rec.get("model_name")):
                    ids.add(rec["trial_id"])
    return ids


async def run(cfg: dict, out_dir: Path, models: list[dict], skip_preflight: bool = False,
              preflight_only: bool = False):
    exp = cfg["experiment"]
    out_dir.mkdir(parents=True, exist_ok=True)
    trials_path = out_dir / "trials.jsonl"
    already = done_ids(trials_path, {m["name"]: m.get("model") for m in models})
    providers = {m["name"]: make_provider(m, exp["temperature"], exp["max_tokens"]) for m in models}
    for name, prov in providers.items():  # fail fast on missing keys or unfilled model IDs
        try:
            prov._key()
        except ProviderError as e:
            sys.exit(f"stopped before running: {e}")
        if str(prov.model).startswith("REPLACE"):
            sys.exit(f"stopped before running: set a real model ID for '{name}' in the config")
    sems = {m["name"]: AdaptiveLimiter(m.get("concurrency", exp["concurrency"]), m["name"]) for m in models}
    for name, prov in providers.items():
        prov.limiter = sems[name]

    # Preflight comprehension check: every model must show it understands the game before any trial.
    if skip_preflight:
        print("preflight check SKIPPED (--skip-preflight)")
    else:
        async with httpx.AsyncClient() as client:
            try:
                summaries = await run_preflight(client, providers, sems, {m["name"]: m.get("model") for m in models},
                                                exp, out_dir)
            except FatalProviderError as e:
                print(f"\nSTOPPING during preflight: {e}")
                sys.exit(2)
        print("preflight comprehension check:")
        failed = []
        for name, s in summaries.items():
            if s.get("skipped"):
                print(f"  {name:14s} passed earlier in this folder")
                continue
            errs = ", ".join(f"{f} x{k}" for f, k in sorted(s["field_errors"].items(), key=lambda x: -x[1])) or "none"
            truth = f"message-truth {s['truth_correct']}/{s['truth_total']}" if s["truth_total"] else ""
            print(f"  {name:14s} {s['correct']}/{s['total']} fully correct ({100 * s['rate']:.0f}%)  {truth}  "
                  f"{'PASS' if s['passed'] else 'FAIL'}   wrong fields: {errs}")
            if not s["passed"]:
                failed.append(name)
        if failed:
            print(f"\nSTOPPED: {', '.join(failed)} failed the comprehension check (need "
                  f"{100 * exp.get('preflight_pass', 0.9):.0f}% of items fully correct and "
                  f"{100 * exp.get('preflight_truth_pass', 0.9):.0f}% on message truth). No trials were run. "
                  f"Details: {out_dir / 'preflight.jsonl'}")
            sys.exit(3)
        if preflight_only:
            print("preflight only: all models passed; no trials run.")
            return

    trials = [t for t in enumerate_trials([m["name"] for m in models], exp["arms"], exp["framings"],
                                          exp["grids"], exp["trials"], exp["seed"], exp["counterbalance"],
                                          exp["wordings"])
              if t.trial_id not in already]
    total = len(trials)
    print(f"{total} trials to run ({len(already)} already done) -> {trials_path}")
    if total == 0:
        return

    meta = {"started_utc": datetime.now(timezone.utc).isoformat(), "config": cfg,
            "models": models, "n_new_trials": total}
    with (out_dir / f"meta_{int(time.time())}.json").open("w") as f:
        json.dump(meta, f, indent=2, default=str)

    lock = asyncio.Lock()
    counter = {"done": 0, "errors": 0, "invalid": 0}
    t0 = time.time()

    fatal: list[str] = []
    async with httpx.AsyncClient() as client:
        fh = trials_path.open("a")

        async def one(trial):
            td = trial.to_dict()
            prov = providers[trial.model_name]
            async with sems[trial.model_name]:
                if fatal:  # stop launching calls once a fatal error has been seen
                    return
                start = time.time()
                try:
                    res = await prov.complete(client, trial.prompt, td)
                    parsed = parse_reply(trial.arm, res["text"])
                    rec = {**td, "status": "ok", "provider": prov.cfg["provider"], "model_id": prov.model,
                           "temperature": prov.temperature, "extra": prov.cfg.get("extra"),
                           "raw_text": res["text"], "usage": res["usage"], "parsed": parsed,
                           **score(td, parsed), "latency_s": round(time.time() - start, 2)}
                except FatalProviderError as e:
                    if not fatal:
                        fatal.append(str(e))
                        print(f"\nSTOPPING: {e}\n", flush=True)
                    return  # nothing written, so a re-run retries this trial
                except ProviderError as e:
                    rec = {**td, "status": "error", "error": str(e)}
            if trial.rep > 0:  # prompts are reproducible from the seed; keep one per cell for reference
                rec.pop("prompt", None)
            async with lock:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                counter["done"] += 1
                if rec["status"] != "ok":
                    counter["errors"] += 1
                elif not rec.get("valid"):
                    counter["invalid"] += 1
                if counter["done"] % 50 == 0 or counter["done"] == total:
                    el = time.time() - t0
                    print(f"  {counter['done']}/{total}  errors={counter['errors']}  "
                          f"unparseable={counter['invalid']}  {el:.0f}s elapsed", flush=True)

        try:
            await asyncio.gather(*(one(t) for t in trials))
        finally:
            fh.close()
    if counter["errors"]:
        print(f"{counter['errors']} trials errored (see 'error' field). Re-run the same command to retry them.")
    if fatal:
        print(f"Run stopped early after {counter['done']} trials: {fatal[0][:300]}\n"
              "Likely a spend limit or a key problem. Fix it, then re-run the same command to resume.")
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default=None, help="output folder (default results/<timestamp>)")
    ap.add_argument("--pilot", action="store_true", help="10 trials per cell, grid1, neutral framing")
    ap.add_argument("--trials", type=int, help="override trials per cell")
    ap.add_argument("--grids", help="override, comma-separated, e.g. grid1,grid2")
    ap.add_argument("--models", help="only these model names, comma-separated")
    ap.add_argument("--preflight-only", action="store_true",
                    help="run only the comprehension check (a few calls per model), then stop")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the comprehension check (not recommended for real runs)")
    ap.add_argument("--wordings", help="override, comma-separated, e.g. original,alt1,alt2")
    ap.add_argument("--max-calls", type=int, help="refuse to start if the plan exceeds this many calls "
                    "(also settable as max_calls in [experiment])")
    ap.add_argument("--dry-run", action="store_true", help="print plan and one prompt; no API calls")
    args = ap.parse_args()

    cfg = load_config(args.config)
    exp = cfg["experiment"]
    if args.pilot:
        exp.update(trials=10, grids=["grid1"], framings=["neutral"])
    if args.trials:
        exp["trials"] = args.trials
    if args.grids:
        exp["grids"] = args.grids.split(",")
    if args.wordings:
        exp["wordings"] = args.wordings.split(",")
        for w in exp["wordings"]:
            if w not in WORDINGS:
                sys.exit(f"unknown wording {w}; choose from {list(WORDINGS)}")
    models = cfg["models"]
    if args.models:
        keep = set(args.models.split(","))
        models = [m for m in models if m["name"] in keep]

    n_points = sum(len(GRIDS[g]) for g in exp["grids"])
    n_cells = len(models) * len(exp["arms"]) * len(exp["framings"]) * len(exp["wordings"]) * n_points
    n_pf = 0 if args.skip_preflight else n_preflight_calls(len(models), exp)
    n_calls = n_pf if args.preflight_only else n_cells * exp["trials"] + n_pf
    print(f"models={[m['name'] for m in models]}  arms={exp['arms']}  framings={exp['framings']}  "
          f"grids={exp['grids']}  wordings={exp['wordings']}")
    print(f"{n_cells} cells x {exp['trials']} trials + preflight = {n_calls} calls "
          f"(~{n_calls * 450 / 1e6:.2f}M tokens at ~450 tokens/call)")

    max_calls = args.max_calls if args.max_calls is not None else exp.get("max_calls")
    if max_calls is not None and n_calls > max_calls:
        sys.exit(f"stopped: plan is {n_calls} calls, above the limit of {max_calls} (--max-calls / max_calls)")

    if args.dry_run:
        first = next(enumerate_trials([models[0]["name"]], exp["arms"], exp["framings"], exp["grids"], 1,
                                      exp["seed"], exp["counterbalance"], exp["wordings"]))
        print("\n--- example prompt ---\n" + first.prompt + "\n--- end ---")
        print(f"(lie = message {first.msg_for_lie}; selfish option = {first.lie_label})")
        return

    out = Path(args.out or f"results/{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    asyncio.run(run(cfg, out, models, args.skip_preflight, args.preflight_only))
    print(f"done. analyze with:  python analyze.py {out}")


if __name__ == "__main__":
    main()
