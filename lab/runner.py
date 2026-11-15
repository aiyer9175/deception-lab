"""Generic runner for experiments after Phase 1 (Phase 2 interventions, the label-robustness check).

Phase 1 keeps its own frozen code path (run.py, lab/design.py, lab/preflight.py). An experiment module
here exposes:
    NAME                                   short name, e.g. "phase2"
    validate(exp)                          fill defaults / exit on bad config
    trials(model_names, exp) -> [dict]     every trial: trial_id, model_name, prompt, prompt_version, ...
    preflight_items(model_name, exp)       comprehension items: item_id, prompt, answers, ...
    preflight_version(exp) -> str
    parse_and_score(trial, text) -> dict   {"parsed": ..., "valid": ..., <outcome fields>}
    followup(trial, rec, exp) -> dict|None optional second call (I5 monitor): {"model_name", "prompt", ...}
    score_followup(trial, rec, text) -> dict
    calls_per_trial(trial) -> int
Each record in trials.jsonl is the trial dict plus the reply, parse and score, like Phase 1.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from lab import mock_ext  # noqa: F401  (registers the "mock_ext" provider)
from lab.preflight import grade
from lab.providers import AdaptiveLimiter, FatalProviderError, ProviderError, make_provider


def done_ids(path: Path, model_ids: dict[str, str]) -> set[str]:
    ids = set()
    if path.exists():
        for line in path.open():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok" and rec.get("model_id") == model_ids.get(rec.get("model_name")):
                ids.add(rec["trial_id"])
    return ids


async def run_preflight(mod, client, providers, limiters, model_ids, exp, out_dir, names):
    """Comprehension check for `names` (models not yet passed in this folder at this version)."""
    threshold = exp.get("preflight_pass", 0.9)
    truth_threshold = exp.get("preflight_truth_pass", 0.9)
    version = mod.preflight_version(exp)
    path = out_dir / "preflight.jsonl"
    passed_before = set()
    if path.exists():
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "summary" and rec["passed"] and rec["version"] == version \
                    and rec["model_id"] == model_ids.get(rec["model_name"]):
                passed_before.add(rec["model_name"])
    summaries, stop = {}, []
    with path.open("a") as fh:
        for name in names:
            if name in passed_before:
                summaries[name] = {"passed": True, "skipped": True}
                continue
            prov = providers[name]
            items = list(mod.preflight_items(name, exp))

            async def ask(item, prov=prov, name=name):
                async with limiters[name]:
                    if stop:
                        return item, None, "stopped"
                    try:
                        res = await prov.complete(client, item["prompt"], item)
                        return item, res["text"], None
                    except FatalProviderError as e:
                        stop.append(str(e))
                        raise
                    except ProviderError as e:
                        return item, None, str(e)

            results = await asyncio.gather(*(ask(it) for it in items))
            n_ok, errs, t_ok, t_n = 0, {}, 0, 0
            for item, text, err in results:
                g = grade(item, text) if err is None else {"ok": False, "parsed": False, "wrong": ["(api error)"]}
                n_ok += g["ok"]
                tf = item.get("truth_field")
                if tf:
                    t_n += 1
                    t_ok += tf not in g["wrong"]
                for f in g["wrong"]:
                    errs[f] = errs.get(f, 0) + 1
                fh.write(json.dumps({"kind": "item", "model_name": name, "item_id": item["item_id"],
                                     "game": item.get("game"), "answers": item["answers"], "raw_text": text,
                                     "error": err, **g}) + "\n")
            rate = n_ok / len(items)
            t_rate = t_ok / t_n if t_n else 1.0
            s = {"kind": "summary", "model_name": name, "model_id": model_ids[name], "version": version,
                 "correct": n_ok, "total": len(items), "rate": rate, "threshold": threshold,
                 "truth_correct": t_ok, "truth_total": t_n, "truth_rate": t_rate,
                 "truth_threshold": truth_threshold,
                 "passed": rate >= threshold and t_rate >= truth_threshold, "field_errors": errs}
            fh.write(json.dumps(s) + "\n")
            fh.flush()
            summaries[name] = s
    return summaries


async def run(mod, cfg: dict, out_dir: Path, models: list[dict], skip_preflight=False, preflight_only=False):
    exp = cfg["experiment"]
    out_dir.mkdir(parents=True, exist_ok=True)
    trials_path = out_dir / "trials.jsonl"
    all_models = list(models)
    # a helper model (e.g. the I5 monitor) is called but not studied; it needs a provider too
    helper = cfg.get("monitor")
    if helper and helper["name"] not in {m["name"] for m in all_models}:
        all_models.append(helper)
    model_ids = {m["name"]: m.get("model") for m in all_models}
    providers = {m["name"]: make_provider(m, exp["temperature"], exp["max_tokens"]) for m in all_models}
    for name, prov in providers.items():
        try:
            prov._key()
        except ProviderError as e:
            sys.exit(f"stopped before running: {e}")
        if str(prov.model).startswith("REPLACE"):
            sys.exit(f"stopped before running: set a real model ID for '{name}' in the config")
    sems = {m["name"]: AdaptiveLimiter(m.get("concurrency", exp["concurrency"]), m["name"]) for m in all_models}
    for name, prov in providers.items():
        prov.limiter = sems[name]
    studied = [m["name"] for m in models]

    if skip_preflight:
        print("preflight check SKIPPED (--skip-preflight)")
    else:
        async with httpx.AsyncClient() as client:
            try:
                summaries = await run_preflight(mod, client, providers, sems, model_ids, exp, out_dir, studied)
            except FatalProviderError as e:
                print(f"\nSTOPPING during preflight: {e}")
                sys.exit(2)
        print(f"preflight comprehension check ({mod.NAME}):")
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
            print(f"\nSTOPPED: {', '.join(failed)} failed the comprehension check. No trials were run. "
                  f"Details: {out_dir / 'preflight.jsonl'}")
            sys.exit(3)
        if preflight_only:
            print("preflight only: all models passed; no trials run.")
            return

    already = done_ids(trials_path, model_ids)
    trials = [t for t in mod.trials(studied, exp) if t["trial_id"] not in already]
    total = len(trials)
    print(f"{total} trials to run ({len(already)} already done) -> {trials_path}")
    if total == 0:
        return
    with (out_dir / f"meta_{int(time.time())}.json").open("w") as f:
        json.dump({"started_utc": datetime.now(timezone.utc).isoformat(), "experiment": mod.NAME,
                   "config": cfg, "n_new_trials": total}, f, indent=2, default=str)

    lock = asyncio.Lock()
    counter = {"done": 0, "errors": 0, "invalid": 0}
    t0 = time.time()
    fatal: list[str] = []

    async def call(name, prompt, td):
        prov = providers[name]
        async with sems[name]:
            if fatal:
                return None
            return await prov.complete(client, prompt, td)

    async with httpx.AsyncClient() as client:
        fh = trials_path.open("a")

        async def one(td):
            prov = providers[td["model_name"]]
            start = time.time()
            try:
                res = await call(td["model_name"], td["prompt"], td)
                if res is None:
                    return
                rec = {**td, "status": "ok", "provider": prov.cfg["provider"], "model_id": prov.model,
                       "temperature": prov.temperature, "extra": prov.cfg.get("extra"),
                       "raw_text": res["text"], "usage": res["usage"], **mod.parse_and_score(td, res["text"])}
                fu = mod.followup(td, rec, cfg) if rec.get("valid") else None
                if fu is not None:
                    fres = await call(fu["model_name"], fu["prompt"], {**td, **fu})
                    if fres is None:
                        return
                    rec.update({"followup_model_id": providers[fu["model_name"]].model,
                                "followup_prompt": fu["prompt"] if td["rep"] == 0 else None,
                                "followup_raw_text": fres["text"], "followup_usage": fres["usage"],
                                **mod.score_followup(td, rec, fres["text"])})
                rec["latency_s"] = round(time.time() - start, 2)
            except FatalProviderError as e:
                if not fatal:
                    fatal.append(str(e))
                    print(f"\nSTOPPING: {e}\n", flush=True)
                return
            except ProviderError as e:
                rec = {**td, "status": "error", "error": str(e)}
            if td["rep"] > 0:
                rec.pop("prompt", None)
            async with lock:
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                counter["done"] += 1
                counter["errors"] += rec["status"] != "ok"
                counter["invalid"] += rec["status"] == "ok" and not rec.get("valid")
                if counter["done"] % 50 == 0 or counter["done"] == total:
                    print(f"  {counter['done']}/{total}  errors={counter['errors']}  "
                          f"unparseable={counter['invalid']}  {time.time() - t0:.0f}s elapsed", flush=True)

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


# Rough $ per 1M tokens (input, output) for the dry-run estimate. Check current prices before relying on it.
PRICES = {
    "gpt-4o-2024-11-20": (2.50, 10.00),
    "gpt-4.1-2025-04-14": (2.00, 8.00),
    "gpt-5-mini-2025-08-07": (0.25, 2.00),
}
# Mean output tokens per call in the Phase 1 run (reasoning tokens included for gpt-5-mini).
CHARS_PER_TOKEN = 4.6  # Phase 1 prompts: 1,373 characters = 299 tokens (gpt-4o)
PHASE1_OUTPUT_TOKENS = {"gpt-4o-2024-11-20": 33, "gpt-4.1-2025-04-14": 38, "gpt-5-mini-2025-08-07": 200}


def estimate_cost(trials, preflight_n, models, monitor=None) -> list[str]:
    """Per-model call counts and an approximate $ cost (input tokens ≈ characters / 4.6, from Phase 1)."""
    lines, total = [], 0.0
    by_model = {}
    for t in trials:
        d = by_model.setdefault(t["model_name"], {"calls": 0, "in": 0, "mon_calls": 0, "mon_in": 0})
        d["calls"] += 1
        d["in"] += len(t["prompt"]) / CHARS_PER_TOKEN
        if t.get("needs_followup"):
            d["mon_calls"] += 1
            d["mon_in"] += 250  # monitor prompt, tokens
    ids = {m["name"]: m.get("model") for m in models}
    mon_id = monitor.get("model") if monitor else None
    for name, d in by_model.items():
        pi, po = PRICES.get(ids[name], (0, 0))
        out = PHASE1_OUTPUT_TOKENS.get(ids[name], 50)
        pf_calls = preflight_n
        cost = (d["in"] + pf_calls * 350) * pi / 1e6 + (d["calls"] + pf_calls) * out * po / 1e6
        mcost = 0.0
        if d["mon_calls"] and mon_id:
            mi, mo = PRICES.get(mon_id, (0, 0))
            mcost = d["mon_in"] * mi / 1e6 + d["mon_calls"] * PHASE1_OUTPUT_TOKENS.get(mon_id, 50) * mo / 1e6
        total += cost + mcost
        mon = f" + {d['mon_calls']} monitor calls (${mcost:.2f})" if d["mon_calls"] else ""
        lines.append(f"  {name:12s} {d['calls']} trials + {pf_calls} preflight{mon}: ~${cost + mcost:.2f}")
    lines.append(f"  total ≈ ${total:.2f} (prices in lab/runner.py PRICES; output length from Phase 1)")
    return lines
