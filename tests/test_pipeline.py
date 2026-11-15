import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)


def test_mock_end_to_end_and_resume(tmp_path):
    out = tmp_path / "mock"
    r = run("run.py", "--config", "config.mock.toml", "--out", str(out), "--trials", "3",
            "--wordings", "original,alt1")
    assert r.returncode == 0, r.stderr
    recs = [json.loads(l) for l in (out / "trials.jsonl").read_text().splitlines()]
    assert len(recs) == 2 * 2 * 1 * 2 * 16 * 3
    assert all(r["prompt_version"] and r["model_id"] == "simulated" for r in recs)
    r = run("run.py", "--config", "config.mock.toml", "--out", str(out), "--trials", "3",
            "--wordings", "original,alt1")
    assert "0 trials to run" in r.stdout
    r = run("analyze.py", str(out))
    assert r.returncode == 0, r.stderr
    report = (out / "report.md").read_text()
    assert "MOCK DATA" in report and "Wording robustness" in report and "g* ($ fit)" in report


def test_max_calls_guard():
    r = run("run.py", "--config", "config.mock.toml", "--dry-run", "--max-calls", "100")
    assert r.returncode != 0 and "above the limit" in r.stderr


def test_changed_model_id_is_not_reused(tmp_path):
    out = tmp_path / "m"
    assert run("run.py", "--config", "config.mock.toml", "--out", str(out), "--trials", "1").returncode == 0
    cfg = (ROOT / "config.mock.toml").read_text().replace('model = "simulated"', 'model = "simulated-v2"')
    (tmp_path / "cfg.toml").write_text(cfg)
    r = run("run.py", "--config", str(tmp_path / "cfg.toml"), "--out", str(out), "--trials", "1")
    assert "64 trials to run (0 already done)" in r.stdout


def test_fatal_error_stops_run(tmp_path, monkeypatch):
    """A spend-limit error stops the run immediately and writes nothing for unfinished trials."""
    import asyncio
    import httpx
    sys.path.insert(0, str(ROOT))
    import run as runmod
    from lab import providers

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"code": "insufficient_quota"}},
                              text='{"error": {"code": "insufficient_quota"}}')

    real_client = httpx.AsyncClient
    monkeypatch.setattr(runmod.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("FAKE_KEY", "x")
    cfg = {"experiment": {"trials": 5, "temperature": 1.0, "max_tokens": 50, "grids": ["grid1"],
                          "arms": ["deception"], "framings": ["neutral"], "seed": 0, "concurrency": 2,
                          "counterbalance": True, "wordings": ["original"]},
           "models": [{"name": "m", "provider": "openai", "model": "x", "api_key_env": "FAKE_KEY"}]}
    out = tmp_path / "fatal"
    try:
        asyncio.run(runmod.run(cfg, out, cfg["models"]))
        raised = None
    except SystemExit as e:
        raised = e.code
    assert raised == 2
    assert calls["n"] <= 2                                   # stopped at once, no retry storm
    tp = out / "trials.jsonl"
    assert not tp.exists() or tp.read_text() == ""           # nothing recorded, so resume retries all


def test_preflight_passes_and_is_recorded(tmp_path):
    out = tmp_path / "pf"
    r = run("run.py", "--config", "config.mock.toml", "--out", str(out), "--trials", "1")
    assert r.returncode == 0, r.stderr
    assert "PASS" in r.stdout and "FAIL" not in r.stdout
    recs = [json.loads(l) for l in (out / "preflight.jsonl").read_text().splitlines()]
    assert sum(1 for x in recs if x["kind"] == "summary" and x["passed"]) == 2
    r = run("run.py", "--config", "config.mock.toml", "--out", str(out), "--trials", "1")
    assert "passed earlier" in r.stdout                       # not re-asked on resume


def test_preflight_failure_stops_before_any_trial(tmp_path):
    cfg = (ROOT / "config.mock.toml").read_text().replace(
        'name = "mock-selfish"', 'name = "mock-selfish"\nmock_preflight_error = 1.0')
    (tmp_path / "cfg.toml").write_text(cfg)
    out = tmp_path / "pf_fail"
    r = run("run.py", "--config", str(tmp_path / "cfg.toml"), "--out", str(out), "--trials", "1")
    assert r.returncode == 3
    assert "mock-selfish" in r.stdout and "failed the comprehension check" in r.stdout
    assert not (out / "trials.jsonl").exists()
