import asyncio
import math

import pytest

from analyze import bootstrap_cutoff, cutoff_level, cutoff_log, newcombe_diff, wilson
from lab.design import GRIDS, enumerate_trials
from lab.parse import parse_reply, score
from lab.providers import MockProvider


def test_wilson_known_value():
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)


def test_newcombe_known_value():
    # Newcombe (1998), Statistics in Medicine 17: 873-890, example 56/70 vs 48/80, method 10
    lo, hi = newcombe_diff(56, 70, 48, 80)
    assert lo == pytest.approx(0.0524, abs=2e-3) and hi == pytest.approx(0.3339, abs=2e-3)


def simulate(params, grid="grid2", n=400):
    prov = MockProvider({"name": "m", "provider": "mock", **params}, 1.0, 300)
    trials = list(enumerate_trials(["m"], ["deception"], ["neutral"], [grid], n))

    async def go():
        return await asyncio.gather(*(prov.complete(None, t.prompt, t.to_dict()) for t in trials))

    res = asyncio.run(go())
    cells = {}
    for t, r in zip(trials, res):
        s = score(t.to_dict(), parse_reply("deception", r["text"]))
        c = cells.setdefault(t.g if grid == "grid2" else t.h, [0, 0])
        if s["valid"]:
            c[0] += s["lied"]
            c[1] += 1
    xs = sorted(cells)
    return xs, [cells[x][0] for x in xs], [cells[x][1] for x in xs]


def logistic(x):
    return 1 / (1 + math.exp(-x))


@pytest.mark.parametrize("alpha,c,phi", [(0.6, 3.0, 1.0), (0.1, 0.5, 1.0), (0.3, 2.0, 0.8)])
def test_cutoff_recovers_mock_parameters(alpha, c, phi):
    true_gstar = (alpha * 10 + c) / (2 * phi - 1)
    gs, ks, ns = simulate({"mock_alpha": alpha, "mock_lie_cost": c, "mock_phi": phi, "mock_noise": 2.0})
    est = cutoff_level(gs, ks, ns)
    assert est == pytest.approx(true_gstar, abs=max(1.0, 0.15 * true_gstar))
    lo, hi = bootstrap_cutoff(cutoff_level, gs, ks, ns, n_boot=300)
    assert lo <= est <= hi


def test_mock_noise_is_logistic():
    # lie rate at each g should match the logistic CDF (the old two-uniform noise was Laplace)
    alpha, c, s = 0.6, 3.0, 2.0
    gs, ks, ns = simulate({"mock_alpha": alpha, "mock_lie_cost": c, "mock_noise": s}, n=2000)
    for g, k, n in zip(gs, ks, ns):
        assert k / n == pytest.approx(logistic((g - alpha * 10 - c) / s), abs=0.035), g


def test_cutoff_edge_cases():
    assert cutoff_level([0, 1, 5], [0, 0, 0], [50, 50, 50]) == float("inf")
    assert cutoff_level([0, 1, 5], [40, 30, 10], [50, 50, 50]) == float("inf")   # falls with g
    assert cutoff_level([0, 1, 5], [45, 48, 50], [50, 50, 50]) < 0               # lies even at g=0
    assert cutoff_log([0, 1], [0, 10], [50, 50]) is None                         # needs two g > 0
    assert cutoff_log([1, 10, 100], [5, 25, 45], [50, 50, 50]) == pytest.approx(10, rel=0.2)
