"""Tests for the two generalizable additions: the optional home-region gate
(greenhouse.home_region_pre_gate) and the pluggable notifier (notify.py).

No network access; the webhook test stubs urllib.request.urlopen.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import greenhouse  # noqa: E402
import notify  # noqa: E402
import watcher  # noqa: E402

TEXAS = ("Texas", "TX")
CALIFORNIA = ("California", "CA")
OFF = ("", "")


# ---- home_region_pre_gate: off by default, generalizes to any region --------


def test_gate_off_never_kills_on_geography():
    """The config.example.yaml default: home_region unconfigured must never
    turn a real, workable posting into a hard-kill."""
    blob = "AI Specialist - East | Remote-MA/NJ/NY/PA"
    assert greenhouse.home_region_pre_gate(blob, *OFF) is None


def test_explicit_home_region_is_eligible():
    assert greenhouse.home_region_pre_gate("Remote - Texas, USA", *TEXAS) is None
    assert greenhouse.home_region_pre_gate("Austin, TX", *TEXAS) is None


def test_nationwide_remote_is_eligible_regardless_of_home_region():
    for blob in ("Remote - USA", "Remote (USA)", "Nationwide"):
        assert greenhouse.home_region_pre_gate(blob, *TEXAS) is None, blob


def test_a_different_specific_region_is_killed():
    blob = "AI Specialist - East | Remote-MA/NJ/NY/PA"
    assert greenhouse.home_region_pre_gate(blob, *TEXAS) == "outside_home_region"


def test_bare_city_plus_country_with_no_state_token_is_killed():
    assert greenhouse.home_region_pre_gate(
        "Cincinnati, United States of America", *TEXAS) == "outside_home_region"


def test_opaque_or_empty_location_is_undecided():
    assert greenhouse.home_region_pre_gate("Hybrid", *TEXAS) is None
    assert greenhouse.home_region_pre_gate("", *TEXAS) is None


def test_a_different_home_region_flips_which_state_is_excluded():
    """Proves the exclusion set is genuinely config-driven, not hardcoded to
    one region: with California as home, Texas becomes a kill and California
    becomes eligible — the mirror image of the Texas-home behavior above."""
    assert greenhouse.home_region_pre_gate("Remote - Texas, USA", *CALIFORNIA) == "outside_home_region"
    assert greenhouse.home_region_pre_gate("Remote - California, USA", *CALIFORNIA) is None


def test_location_token_reflects_the_configured_home_region():
    job = {"location": {"name": "Remote - Texas, USA"}}
    assert greenhouse.location_token(job, *TEXAS) == "TX-remote"
    assert greenhouse.location_token(job, *OFF) != "TX-remote"


def test_home_region_cfg_reads_config_yaml_shape():
    cfg = {"eligibility": {"home_region": {"enabled": True, "name": "Texas", "abbr": "TX"}}}
    assert watcher.home_region_cfg(cfg) == TEXAS
    assert watcher.home_region_cfg({}) == OFF
    assert watcher.home_region_cfg(
        {"eligibility": {"home_region": {"enabled": False, "name": "Texas", "abbr": "TX"}}}) == OFF


# ---- notify.py: stdout/file/webhook, all no-secrets-required -----------------


def test_stdout_is_the_default_and_never_raises():
    n = notify.resolve_notifier(REPO_ROOT, {})
    assert n.send_chunked("hdr", ["a", "b"]) is True


def test_unrecognized_type_falls_back_to_stdout():
    n = notify.resolve_notifier(REPO_ROOT, {"type": "carrier-pigeon"})
    assert n.send_chunked("hdr", []) is True


def test_file_backend_appends_relative_to_repo_root(tmp_path):
    n = notify.resolve_notifier(tmp_path, {"type": "file", "path": "state/notes.log"})
    n.send_chunked("header", ["item one"])
    assert "item one" in (tmp_path / "state" / "notes.log").read_text(encoding="utf-8")


def test_webhook_backend_posts_a_generic_slack_compatible_body(monkeypatch):
    posted = []

    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=15):
        posted.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    n = notify.resolve_notifier(REPO_ROOT, {"type": "webhook", "webhook_url": "https://hooks.example.com/x"})
    assert n.send_chunked("header", ["item"]) is True
    assert posted == [{"text": "header\n\nitem"}]


def test_webhook_backend_with_no_url_degrades_to_stdout():
    n = notify.resolve_notifier(REPO_ROOT, {"type": "webhook"})
    assert n.send_chunked("h", ["i"]) is True  # logged, not a crash
