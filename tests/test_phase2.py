import json
import math
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from lab import design, labels, phase2
from lab.mock_ext import true_cutoff

ROOT = Path(__file__).resolve().parents[1]

# Frozen Phase 1 template hashes (PREREGISTRATION.md). Phase 2 work must never change these.
FROZEN = {("deception", "neutral"): "c36ec5f173", ("deception", "maximize"): "752ea4687b",
          ("dictator", "neutral"): "59e95f0d59", ("dictator", "maximize"): "7d255e02ac"}


def run(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)


def exp(**kw):
    e = {"trials": 8, "seed": 0, **kw}
    phase2.validate(e)
    return e


def test_phase1_prompts_unchanged():
    for (arm, fr), h in FROZEN.items():
        assert design.prompt_version(arm, fr, "original") == h


def test_i0_without_follow_line_is_the_phase1_prompt():
    for truth, order in phase2.LAYOUTS:
        lie = "B" if truth == "A" else "A"
        pay = {truth: [5, 15], lie: [25, 5]}
        assert phase2.build_prompt("I0", pay, order, follow_line=False) == \
            design.build_prompt("deception", "neutral", pay, order, "original")


def test_additions_only_in_the_fixed_slot():
    pay = {"A": [5, 15], "B": [25, 5]}
    base = design.game_text("deception", "neutral", pay, ["A", "B"], "original")
    for arm in phase2.ARMS2:
        if arm == "I4":
            continue  # option lines change by design
        p = phase2.build_prompt(arm, pay, ["A", "B"])
        assert p.startswith(base + phase2.FOLLOW_LINE)
        assert p.endswith(design.DECEPTION_REPLY)


def test_layouts_exactly_balanced_and_paired():
    ts = phase2.trials(["m1", "m2"], exp(trials=12))
    by_cell = {}
    for t in ts:
        by_cell.setdefault((t["model_name"], t["arm"], t["g"]), Counter())[(t["truth_label"], t["msg_for_lie"])] += 1
    assert all(len(c) == 4 and set(c.values()) == {3} for c in by_cell.values())
    a = [(t["arm"], t["g"], t["rep"], t["truth_label"], t["msg_for_lie"], tuple(t["lie_rounds"]))
         for t in ts if t["model_name"] == "m1"]
    b = [(t["arm"], t["g"], t["rep"], t["truth_label"], t["msg_for_lie"], tuple(t["lie_rounds"]))
         for t in ts if t["model_name"] == "m2"]
    assert a == b


def test_trials_must_be_multiple_of_4():
    with pytest.raises(SystemExit):
        phase2.validate({"trials": 50, "seed": 0})


def test_lie_message_and_share_payoffs():
    for t in phase2.trials(["m"], exp(trials=4)):
        order = ["A", "B"] if 'Message 1: "Option A' in t["prompt"] else ["B", "A"]
        assert order[t["msg_for_lie"] - 1] == t["lie_label"]
        assert t["payoffs"][t["lie_label"]] == [5 + t["g"], 5]
        if t["arm"] == "I4":
            s = t["shown_payoffs"]
            assert s[t["truth_label"]][0] == 12.5 and s[t["lie_label"]][0] == 7.5 + t["g"]
            assert "$12.50 to you" in t["prompt"]


def test_i6_histories_have_k_lies_and_visibility_text():
    for t in phase2.trials(["m"], exp(trials=4)):
        if t["rep_k"] is not None:
            assert len(t["lie_rounds"]) == t["rep_k"]
            assert t["prompt"].count("your message was false") == t["rep_k"]
            assert ("Future participants will see" in t["prompt"]) == t["rep_visible"]


def test_monitor_scoring():
    t = phase2.trials(["m"], exp(trials=4, p2_arms=["I5"]))[0]
    rec = {"lied": True}
    assert phase2.score_followup(t, rec, '{"reason": "x", "report": "false"}')["monitor_correct"] is True
    assert phase2.score_followup(t, rec, '{"report": "true"}')["fined"] is False
    assert phase2.score_followup(t, rec, 'report: TRUE')["monitor_report"] == "truth"
    assert phase2.score_followup(t, rec, "no idea")["monitor_report"] is None
    fu = phase2.followup(t, {"parsed": {"message": t["msg_for_lie"]}, "lied": True}, {"monitor": {"name": "x"}})
    lie_stmt = design.WORDINGS["original"]["statement"][t["lie_label"]]
    assert f'"{lie_stmt}"' in fu["prompt"] and fu["sender_lied"]


def test_preflight_answers_have_no_ties_and_match_prompt():
    for it in phase2.preflight_items("m", exp()):
        a = it["answers"]
        assert f"${a['counterpart_pay_if_B']} to the other participant" in it["prompt"]
        assert f"Option A: {phase2._money(a['my_pay_if_A'])} to you" in it["prompt"]
        shown = [float(x) for x in __import__("re").findall(r"Option [AB]: \$([0-9.]+) to you", it["prompt"])]
        assert len(shown) == 2 and shown[0] != shown[1]  # "better for me" is never a tie


def test_predictions():
    import analyze_phase2 as ap
    assert ap.predicted("I3a", 0.78) == pytest.approx(4 / 0.56)
    assert ap.predicted("I3b", 1.0) == pytest.approx(20)
    assert ap.predicted("I4", 0.6) == pytest.approx(5)
    assert ap.predicted("I5", 1.0, (0.9, 0.1)) == pytest.approx(16)
    assert ap.predicted("I1", 0.78) == 0
    assert math.isnan(ap.predicted("I3a", 0.4))


def test_phase2_mock_recovers_shifts(tmp_path):
    """End to end on simulated senders: Δg* from the $ fit matches the mock's true shift."""
    cfg = (ROOT / "config_phase2.mock.toml").read_text().replace("mock_noise = 2.0", "mock_noise = 6.0")
    (tmp_path / "cfg.toml").write_text(cfg)
    out = tmp_path / "p2"
    r = run("run_ext.py", "--config", str(tmp_path / "cfg.toml"), "--out", str(out), "--trials", "96",
            "--models", "mock-a")
    assert r.returncode == 0, r.stderr
    assert "PASS" in r.stdout
    sys.path.insert(0, str(ROOT))
    import analyze_phase2 as ap
    shifts = ap.main(out)
    import tomllib
    m = next(x for x in tomllib.loads(cfg)["models"] if x["name"] == "mock-a")
    base = true_cutoff(m, {"h": 10, "arm": "I0"})
    for arm in ("I3a", "I3b", "I4", "I5", "I1"):
        s = phase2.ARMS2[arm]
        want = true_cutoff(m, {"h": 10, "arm": arm, "audit_p": s["p"], "F": s["F"],
                               "needs_followup": s["monitor"], "share": s["share"]}) - base
        obs, lo, hi, _, _ = shifts[("mock-a", arm)]
        assert abs(obs - want) < max(3.0, 0.15 * want), (arm, obs, want)
        assert lo - 1 <= want <= hi + 1, (arm, lo, hi, want)
    report = (out / "report_phase2.md").read_text()
    assert "MOCK DATA" in report and "I5: peer monitor" in report and "I6: reputation" in report
    # resume: nothing left to do
    r = run("run_ext.py", "--config", str(tmp_path / "cfg.toml"), "--out", str(out), "--trials", "96",
            "--models", "mock-a")
    assert "0 trials to run" in r.stdout and "passed earlier" in r.stdout


def test_phase2_preflight_failure_stops(tmp_path):
    cfg = (ROOT / "config_phase2.mock.toml").read_text().replace(
        'name = "mock-b"', 'name = "mock-b"\nmock_preflight_error = 1.0')
    (tmp_path / "cfg.toml").write_text(cfg)
    out = tmp_path / "pf"
    r = run("run_ext.py", "--config", str(tmp_path / "cfg.toml"), "--out", str(out), "--trials", "4")
    assert r.returncode == 3 and "mock-b" in r.stdout
    assert not (out / "trials.jsonl").exists()


def test_dry_run_cost_and_guard():
    r = run("run_ext.py", "--config", "config_phase2.mock.toml", "--dry-run")
    assert r.returncode == 0 and "total ≈ $" in r.stdout
    r = run("run_ext.py", "--config", "config_phase2.mock.toml", "--dry-run", "--max-calls", "10")
    assert r.returncode != 0 and "above the limit" in r.stderr


# ---------------------------------------------------------------- labels check

def test_labels_balanced_24_layouts():
    e = {"trials": 48, "seed": 0}
    labels.validate(e)
    ts = labels.trials(["m"], e)
    cell = Counter((tuple(t["labels"]), t["truth_label"], t["msg_for_lie"])
                   for t in ts if t["arm"] == "deception" and t["point"] == "T1")
    assert len(cell) == 24 and set(cell.values()) == {2}
    for t in ts:
        assert t["payoffs"][t["lie_name"]] == [5 + t["g"], 5]
        assert "Message 1" not in t["prompt"] and " A " not in t["prompt"].split("Respond")[0]
        if t["arm"] == "deception":
            first = t["prompt"].split('- "')[1]
            assert first.startswith(f"Option {t['lie_name']} will") == (t["msg_for_lie"] == 1)


def test_labels_parse():
    e = {"trials": 24, "seed": 0}
    labels.validate(e)
    t = next(x for x in labels.trials(["m"], e) if x["arm"] == "deception")
    for reply in (f'{{"recommended_option": "{t["lie_name"]}", "follow_prob": 80}}',
                  f'{{"recommended_option": "Option {t["lie_name"].lower()}"}}',
                  f'recommended_option: {t["lie_name"]}'):
        s = labels.parse_and_score(t, reply)
        assert s["valid"] and s["lied"] and s["parsed"]["message"] == t["msg_for_lie"]
    assert not labels.parse_and_score(t, '{"recommended_option": "A"}')["valid"]


def test_labels_mock_runs_and_phase1_analysis_reads_it(tmp_path):
    out = tmp_path / "lab"
    r = run("run_ext.py", "--config", "config_labels.mock.toml", "--out", str(out), "--trials", "24")
    assert r.returncode == 0, r.stderr
    assert "PASS" in r.stdout
    r = run("analyze.py", str(out))
    assert r.returncode == 0, r.stderr
    rep = (out / "report.md").read_text()
    assert "Grid 1" in rep and "Layout-adjusted rates" in rep
