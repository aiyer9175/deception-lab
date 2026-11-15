"""Simulated agents for Phase 2 and the label-robustness check (FAKE DATA, for testing only).

Registers provider "mock_ext". The sender lies if

    (2φ − 1)·(g − s·h) − α·h − c − c_arm − K_belief + noise > 0,   noise ~ Logistic(0, mock_noise)

where s is the arm's reward share (I4), c_arm a behavioral cost per arm (mock_arm_cost = {"I1": 3, ...}),
and K_belief the expected cost of a lie that the sender believes in:
    audits (I3a/b):   p·F
    monitor (I5):     F·(TPR − FPR), with TPR = mock_monitor_acc and FPR = 1 − mock_monitor_acc
    reputation (I6):  mock_reputation_cost if the record is visible
So the true cutoff is g* = s·h + (α·h + c + c_arm + K_belief) / (2φ − 1), and
Δg* = K / (2φ − 1) for message costs and s·h for the share, as in the brief.
The monitor answers correctly with probability mock_monitor_acc. Dictator trials (labels check): selfish if
g − α·h + noise > 0. Comprehension items are answered correctly unless mock_preflight_error.
"""

from __future__ import annotations

import asyncio
import json
import math
import random

from lab.providers import PROVIDERS, BaseProvider


class ExtMockProvider(BaseProvider):
    async def complete(self, client, prompt, trial):
        await asyncio.sleep(0)
        c = self.cfg
        rng = random.Random(f"{trial['trial_id']}|{trial.get('role', 'sender')}")
        if trial.get("arm") == "preflight":
            ans = dict(trial["answers"])
            if rng.random() < c.get("mock_preflight_error", 0.0):
                ans["better_for_counterpart"] = "wrong"
            return {"text": json.dumps(ans), "usage": None, "error": None}
        acc = c.get("mock_monitor_acc", 0.9)
        if trial.get("role") == "monitor":
            correct = rng.random() < acc
            says_lie = trial["sender_lied"] if correct else not trial["sender_lied"]
            return {"text": json.dumps({"reason": "mock", "report": "false" if says_lie else "true"}),
                    "usage": None, "error": None}
        if rng.random() < c.get("mock_invalid_rate", 0.0):
            return {"text": "I would rather not choose.", "usage": None, "error": None}
        u = rng.random() or 1e-12
        noise = c.get("mock_noise", 2.0) * math.log(u / (1 - u))
        alpha, lie_cost, phi = c.get("mock_alpha", 0.5), c.get("mock_lie_cost", 1.0), c.get("mock_phi", 0.78)
        g, h = trial["g"], trial["h"]
        if trial["game"] == "dictator":
            lie = g - alpha * h + noise > 0
        else:
            K = 0.0
            if trial.get("audit_p"):
                K = trial["audit_p"] * trial["F"]
            elif trial.get("needs_followup"):
                K = trial["F"] * (2 * acc - 1)
            elif trial.get("rep_visible"):
                K = c.get("mock_reputation_cost", 0.0)
            c_arm = c.get("mock_arm_cost", {}).get(trial.get("arm"), 0.0)
            s = trial.get("share", 0.0)
            lie = (2 * phi - 1) * (g - s * h) - alpha * h - lie_cost - c_arm - K + noise > 0
        obj = {"reason": "mock", trial["reply_key"]: trial["lie_value"] if lie else trial["truth_value"]}
        if trial["game"] == "deception":
            obj["follow_prob"] = max(0, min(100, round(100 * phi + rng.uniform(-5, 5))))
        return {"text": json.dumps(obj), "usage": None, "error": None}


def true_cutoff(cfg: dict, trial_like: dict) -> float:
    """The mock's true g* for an arm (deception)."""
    alpha, lie_cost, phi = cfg.get("mock_alpha", 0.5), cfg.get("mock_lie_cost", 1.0), cfg.get("mock_phi", 0.78)
    acc, h = cfg.get("mock_monitor_acc", 0.9), trial_like["h"]
    K = 0.0
    if trial_like.get("audit_p"):
        K = trial_like["audit_p"] * trial_like["F"]
    elif trial_like.get("needs_followup"):
        K = trial_like["F"] * (2 * acc - 1)
    elif trial_like.get("rep_visible"):
        K = cfg.get("mock_reputation_cost", 0.0)
    c_arm = cfg.get("mock_arm_cost", {}).get(trial_like.get("arm"), 0.0)
    return trial_like.get("share", 0.0) * h + (alpha * h + lie_cost + c_arm + K) / (2 * phi - 1)


PROVIDERS["mock_ext"] = ExtMockProvider
