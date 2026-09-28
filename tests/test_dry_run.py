"""End-to-end dry-run test with a stubbed judge and a fixture board.

Exercises the full pipeline — fetch -> diff -> deterministic pre-gates ->
LLM judge -> delivery decision -> (skipped) persistence — without any
network access and without an ANTHROPIC_API_KEY, by injecting a fake board
fetch and a fake judge function. This proves the wiring end-to-end: config
loading, rubric loading, pre-gate logic, and the alert/near-miss/audit
branching all run correctly on their own, independent of the real LLM call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import watcher  # noqa: E402
from lib.cost_gate import CostPolicyGate  # noqa: E402

FIXTURE_BOARD = REPO_ROOT / "tests" / "fixtures" / "board_examplecorp.json"


def fake_fetch_board(slug: str):
    assert slug == "examplecorp"
    data = json.loads(FIXTURE_BOARD.read_text(encoding="utf-8"))
    return data["jobs"]


def fake_judge_item(item, rubric_text, config, cost_gate=None, feedback_text=""):
    """Deterministic stand-in for the real LLM call — no API key needed.

    Scores by a trivial rule on the title so the test can assert on alert vs.
    near-miss routing without touching the network.
    """
    title = (item.get("title") or "").lower()
    if "platform" in title:
        score, why = 8.5, "stub: strong title match"
    elif "customer success" in title:
        score, why = 5.5, "stub: partial title match"
    else:
        score, why = 3.0, "stub: weak title match"
    return {
        "score": score,
        "hard_kill": False,
        "hard_kill_reason": None,
        "why": why,
        "model_used": "stub-judge",
        "tokens_in": 0,
        "tokens_out": 0,
        "error": None,
        "budget_blocked": False,
    }


@pytest.fixture()
def config():
    with (REPO_ROOT / "config.example.yaml").open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["boards"] = ["examplecorp"]
    return cfg


@pytest.fixture()
def cost_gate(tmp_path):
    return CostPolicyGate(
        caller_id="test",
        policy={"enabled": True, "enforce": True, "caller_run_max_usd": 100.0},
        state_file=tmp_path / "cost_gate.json",
    )


class Args:
    def __init__(self, dry_run=True, board=None, only_ids=None, limit=0):
        self.dry_run = dry_run
        self.board = board
        self.only_ids = only_ids
        self.limit = limit


def test_pregates_and_judge_routing(config, cost_gate):
    seen = {"seeded": False, "boards": {}}
    args = Args(dry_run=True)

    alerts, near, stats = watcher.process_board(
        "examplecorp", config, "RUBRIC PLACEHOLDER", seen, cost_gate, args,
        first_run=True, feedback="",
        fetch_board_fn=fake_fetch_board, judge_fn=fake_judge_item,
    )

    # 5 fixture jobs: one foreign-location skip, one comp-floor skip, one
    # function-kill skip, and two that reach the judge (one alert, one
    # near-miss).
    assert stats["total"] == 5
    assert stats["skipped_eligibility"] == 1   # Netherlands-based item
    assert stats["skipped_comp"] == 1           # $60K-$80K band, below floor
    assert stats["skipped_function"] == 1       # "Software Engineer" kill-title
    assert stats["eligible"] == 2
    assert stats["judged"] == 2
    assert stats["errors"] == 0

    assert len(alerts) == 1
    assert alerts[0]["id"] == "1001"
    assert alerts[0]["score"] == 8.5

    assert len(near) == 1
    assert near[0]["id"] == "1005"
    assert near[0]["score"] == 5.5


def test_dry_run_writes_no_state_or_audit_files(config, cost_gate, tmp_path, monkeypatch):
    # Redirect all persistence paths into a scratch dir so this test can
    # assert nothing was written under --dry-run, without touching the repo.
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "VERDICTS_FILE", tmp_path / "verdicts.jsonl")
    monkeypatch.setattr(watcher, "NEAR_MISS_FILE", tmp_path / "near_misses.jsonl")
    monkeypatch.setattr(watcher, "SEEN_FILE", tmp_path / "seen.json")

    seen = {"seeded": False, "boards": {}}
    args = Args(dry_run=True)

    watcher.process_board(
        "examplecorp", config, "RUBRIC PLACEHOLDER", seen, cost_gate, args,
        first_run=True, feedback="",
        fetch_board_fn=fake_fetch_board, judge_fn=fake_judge_item,
    )

    assert not (tmp_path / "verdicts.jsonl").exists()
    assert not (tmp_path / "near_misses.jsonl").exists()
    assert not (tmp_path / "seen.json").exists()


def test_run_end_to_end_via_monkeypatched_modules(config, tmp_path, monkeypatch, capsys):
    """Drives watcher.run() itself (the real CLI entry point), not just
    process_board(), by monkeypatching the greenhouse/judge modules that
    run() resolves internally — proving the injection points actually take
    effect through the full call path."""
    import greenhouse
    import judge as judge_mod

    monkeypatch.setattr(watcher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(watcher, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(watcher, "SEEN_FILE", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(watcher, "VERDICTS_FILE", tmp_path / "state" / "verdicts.jsonl")
    monkeypatch.setattr(watcher, "NEAR_MISS_FILE", tmp_path / "state" / "near_misses.jsonl")
    monkeypatch.setattr(watcher, "FEEDBACK_FILE", tmp_path / "state" / "feedback_pending.jsonl")
    monkeypatch.setattr(watcher, "load_config", lambda config_path=None: dict(config))
    monkeypatch.setattr(watcher, "load_rubric", lambda cfg: "RUBRIC PLACEHOLDER")
    monkeypatch.setattr(greenhouse, "fetch_board", fake_fetch_board)
    monkeypatch.setattr(judge_mod, "judge_item", fake_judge_item)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    watcher.setup_logging()
    rc = watcher.run(Args(dry_run=True))
    assert rc == 0

    # --dry-run must not create any state files.
    assert not (tmp_path / "state" / "seen.json").exists()
    assert not (tmp_path / "state" / "verdicts.jsonl").exists()

    log_text = capsys.readouterr().err + (tmp_path / "logs" / "watcher.log").read_text(encoding="utf-8")
    assert "alerts=1" in log_text
