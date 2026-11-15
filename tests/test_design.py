from collections import Counter

import pytest

from lab import design
from lab.design import GRIDS, WORDINGS, enumerate_trials, prompt_version


def trials(**kw):
    args = dict(model_names=["m1"], arms=["deception", "dictator"], framings=["neutral", "maximize"],
                grids=["grid1", "grid2", "grid3"], n_trials=5, wordings=tuple(WORDINGS))
    args.update(kw)
    return list(enumerate_trials(**args))


def test_gneezy_payoffs():
    want = {"T1": ([5, 6], [6, 5]), "T2": ([5, 15], [6, 5]), "T3": ([5, 15], [15, 5])}
    for t in trials(grids=["grid1"]):
        truth, lie = want[t.point]
        assert t.payoffs[t.truth_label] == truth
        assert t.payoffs[t.lie_label] == lie


def test_g_and_h_definitions():
    for t in trials():
        s_lie, r_lie = t.payoffs[t.lie_label]
        s_tru, r_tru = t.payoffs[t.truth_label]
        assert s_lie - s_tru == t.g
        assert r_tru - r_lie == t.h


@pytest.mark.parametrize("wording", list(WORDINGS))
def test_lie_message_claims_the_lie_option(wording):
    stmt = WORDINGS[wording]["statement"]
    for t in trials(arms=["deception"], wordings=(wording,)):
        assert f'Message {t.msg_for_lie}: "{stmt[t.lie_label]}"' in t.prompt
        assert f'Message {3 - t.msg_for_lie}: "{stmt[t.truth_label]}"' in t.prompt


def test_prompts_show_both_options_with_amounts():
    for t in trials():
        for L in "AB":
            s, r = t.payoffs[L]
            line = WORDINGS[t.wording]["option"].format(L=L, s=f"${s}", r=f"${r}")
            assert line in t.prompt


def test_deterministic_and_unique_ids():
    a, b = trials(), trials()
    assert [t.to_dict() for t in a] == [t.to_dict() for t in b]
    ids = [t.trial_id for t in a]
    assert len(ids) == len(set(ids))


def test_layouts_shared_across_models():
    a = trials(model_names=["m1"])
    b = trials(model_names=["m2"])
    for x, y in zip(a, b):
        assert (x.truth_label, x.msg_for_lie, x.prompt) == (y.truth_label, y.msg_for_lie, y.prompt)
        assert x.trial_id != y.trial_id


def test_counterbalancing_is_balanced():
    ts = trials(arms=["deception"], n_trials=50)
    c = Counter(t.truth_label for t in ts)
    m = Counter(t.msg_for_lie for t in ts)
    assert abs(c["A"] / len(ts) - 0.5) < 0.05
    assert abs(m[1] / len(ts) - 0.5) < 0.05


def test_no_counterbalance_is_gneezy_layout():
    for t in trials(arms=["deception"], counterbalance=False):
        assert t.truth_label == "A" and t.msg_for_lie == 2


def test_prompt_version_tracks_template(monkeypatch):
    v = {w: prompt_version("deception", "neutral", w) for w in WORDINGS}
    assert len(set(v.values())) == len(WORDINGS)
    patched = {**WORDINGS["original"], "intro": "Something else.\n\n"}
    monkeypatch.setitem(design.WORDINGS, "original", patched)
    assert prompt_version("deception", "neutral", "original") != v["original"]


def test_grid_sizes():
    assert [len(GRIDS[g]) for g in ("grid1", "grid2", "grid3")] == [3, 8, 5]
