import json

from lab.design import WORDINGS
from lab.preflight import build_items, grade


def items(**kw):
    return list(build_items("m", ["deception", "dictator"], list(WORDINGS), 6, **kw))


def test_answers_match_the_prompt_text():
    for it in items():
        p, a = it["payoffs"], it["answers"]
        w = WORDINGS[it["wording"]]
        # the option lines in the prompt carry the payoffs the answers are computed from
        for L in "AB":
            assert w["option"].format(L=L, s=f"${p[L][0]}", r=f"${p[L][1]}") in it["prompt"]
        assert a["my_pay_if_A"] == p["A"][0] and a["counterpart_pay_if_B"] == p["B"][1]
        assert p[a["better_for_me"]][0] > p[a["better_for_counterpart"]][0]
        assert p[a["better_for_counterpart"]][1] > p[a["better_for_me"]][1]
        if it["game"] == "deception":
            stmt_true = w["statement"][a["better_for_counterpart"]]
            assert (f'Message 1: "{stmt_true}"' in it["prompt"]) == a["message_1_is_true"]


def test_both_truth_values_and_labels_occur():
    dec = [it for it in items() if it["game"] == "deception"]
    assert {it["answers"]["message_1_is_true"] for it in dec} == {True, False}
    assert {it["answers"]["better_for_me"] for it in items()} == {"A", "B"}


def test_grading():
    it = next(i for i in items() if i["game"] == "deception")
    good = json.dumps(it["answers"])
    assert grade(it, good)["ok"]
    assert grade(it, "```json\n" + good + "\n```")["ok"]
    loose = {**it["answers"], "my_pay_if_A": f"${it['answers']['my_pay_if_A']}",
             "message_1_is_true": str(it["answers"]["message_1_is_true"]).lower()}
    assert grade(it, json.dumps(loose))["ok"]
    bad = {**it["answers"], "better_for_me": it["answers"]["better_for_counterpart"]}
    g = grade(it, json.dumps(bad))
    assert not g["ok"] and g["wrong"] == ["better_for_me"]
    assert not grade(it, "I am not sure.")["ok"]


def test_truth_subgate_can_fail_alone(tmp_path):
    """A model that gets everything right except message truth must fail."""
    import asyncio
    from lab.preflight import run_preflight
    from lab.providers import AdaptiveLimiter

    class Liar:
        model = "x"
        async def complete(self, client, prompt, item):
            ans = dict(item["answers"])
            if "message_1_is_true" in ans and item["item_id"].endswith(("|0", "|1")):
                ans["message_1_is_true"] = not ans["message_1_is_true"]   # 2 of 10 wrong -> 80%
            return {"text": json.dumps(ans)}

    exp = {"arms": ["deception", "dictator"], "wordings": ["original"], "seed": 0}
    s = asyncio.run(run_preflight(None, {"m": Liar()}, {"m": AdaptiveLimiter(4)}, {"m": "x"}, exp, tmp_path))["m"]
    assert s["rate"] == 0.9 and s["truth_rate"] == 0.8 and not s["passed"]
