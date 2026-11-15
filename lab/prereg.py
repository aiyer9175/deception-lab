"""Write a PREREGISTRATION.md draft for a config: prompt hashes, models, grids, trials, predictions.

  python -m lab.prereg config.toml > PREREGISTRATION.md
Run it after the pilot is approved and before the full run. Any later prompt edit changes its hash
(and its trial IDs), so record a new version with a note explaining why.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import date

from pathlib import Path

from lab.design import GRIDS, build_prompt, prompt_version
from lab.preflight import n_preflight_calls, preflight_version

sys.path.insert(0, ".")
from run import load_config  # noqa: E402


def main(path: str):
    cfg = load_config(path)
    exp = cfg["experiment"]
    L = [f"# Preregistration (draft generated {date.today().isoformat()} from `{path}`)\n",
         "Fill in the notes, check every line, then commit this file before the full run.\n",
         "## Prompt templates\n",
         "| Arm | Framing | Wording | Template hash | Full-text SHA-256 (T1 layout, A truthful) |",
         "|---|---|---|---|---|"]
    for arm in exp["arms"]:
        for fr in exp["framings"]:
            for w in exp["wordings"]:
                full = build_prompt(arm, fr, {"A": [5, 6], "B": [6, 5]}, ["A", "B"], w)
                L.append(f"| {arm} | {fr} | {w} | `{prompt_version(arm, fr, w)}` | "
                         f"`{hashlib.sha256(full.encode()).hexdigest()[:16]}` |")
    L += ["\n## Code versions (SHA-256, first 16 hex)\n", "| File | Hash |", "|---|---|"]
    for f in ["lab/design.py", "lab/parse.py", "lab/preflight.py", "lab/providers.py", "run.py", "analyze.py"]:
        L.append(f"| `{f}` | `{hashlib.sha256(Path(f).read_bytes()).hexdigest()[:16]}` |")
    n_pf = exp.get("preflight_items", 10)
    L += ["\n## Comprehension check (preflight)\n",
          f"- Version `{preflight_version(exp['arms'], exp['wordings'], n_pf, exp['seed'])}`; {n_pf} items per arm per "
          f"wording ({n_preflight_calls(1, exp)} per model), payoffs outside all grids.",
          f"- Pass: ≥ {100 * exp.get('preflight_pass', 0.9):.0f}% of items fully correct **and** ≥ "
          f"{100 * exp.get('preflight_truth_pass', 0.9):.0f}% correct on whether Message 1 is true.",
          "- A failing model is excluded before any trial; the run does not start until all listed models pass."]
    L += ["\n## Models\n", "| Name | Provider | Model ID | Temperature | Max tokens | Extra |", "|---|---|---|---|---|---|"]
    for m in cfg["models"]:
        L.append(f"| {m['name']} | {m['provider']} | `{m.get('model')}` | {m.get('temperature', exp['temperature'])} | "
                 f"{m.get('max_tokens', exp['max_tokens'])} | {m.get('extra', '')} |")
    L += ["\n## Design\n",
          f"- Grids: {', '.join(f'{g} {[(p, gg, hh) for p, gg, hh in GRIDS[g]]}' for g in exp['grids'])}",
          f"- Arms: {exp['arms']}; framings: {exp['framings']}; wordings: {exp['wordings']} "
          f"(primary: {exp['wordings'][0]})",
          f"- Trials per cell: {exp['trials']}; seed: {exp['seed']}; counterbalance: {exp['counterbalance']}",
          "\n## Predictions and analysis plan (Phase 1)\n",
          "- Lie rate rises with g (grid2) and falls with h (grid3).",
          "- Lie aversion: selfish rate (dictator) exceeds lie rate (deception) at equal payoffs (grid1), "
          "as for Gneezy's humans (+30, +25, +38 points).",
          "- Primary outcome: cutoff g* per model from the logistic fit, reported on both ln g and $ scales "
          "with bootstrap 95% intervals; lie aversion with Newcombe 95% intervals.",
          "- Exclusions: unparseable replies are excluded and reported per cell; if any model x arm exceeds 5%, "
          "stop and investigate before analysis.",
          "\n## Notes\n", "- (deviations, reasons for any later prompt version)\n"]
    print("\n".join(L))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m lab.prereg <config.toml>")
    main(sys.argv[1])
