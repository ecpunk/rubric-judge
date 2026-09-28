#!/usr/bin/env python3
"""rubric-judge — a two-stage watch pipeline over a Greenhouse job board.

Stage 1 (deterministic): poll Greenhouse board APIs, diff each item against
  persisted state (seen id + content hash); only NEW or materially-changed
  items proceed. A handful of cheap deterministic pre-gates (location,
  compensation floor, function/title) drop obviously-ineligible items before
  spending an LLM call.
Stage 2 (LLM judge): score each surviving item 0-10 against the prose rubric
  document (read at runtime — the rubric is never hardcoded). Cheap
  Haiku-class model behind a vendored LLM cost gate.

Delivery: score >= alert -> configured notifier; near-miss band -> logged for a periodic
digest; everything -> append-only verdicts JSONL audit log.

First run seeds state without per-item flooding: it judges the currently-open
items and sends ONE digest of those scoring >= alert.

This is a generic template. It ships with a Greenhouse job-board adapter as
its illustrative data source, but the pattern (deterministic pre-gates + an
LLM judge scoring against a prose rubric + an audit trail + an operator
feedback loop) generalizes to any source of unstructured items you want
scored against a rubric you can write down in English.

Usage:
  watcher.py                        # normal run
  watcher.py --dry-run              # judge + print, no sends, no state/audit writes
  watcher.py --board examplecorp    # limit to one board
  watcher.py --only-ids 123,456     # judge only these item ids
  watcher.py --limit 20             # cap items judged per board (testing)
  watcher.py --weekly-digest        # emit the near-miss digest and exit
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import greenhouse  # noqa: E402
import judge as judge_mod  # noqa: E402
import notify as notify_mod  # noqa: E402
from lib.cost_gate import CostPolicyGate  # noqa: E402

LOG_DIR = REPO_ROOT / "logs"
STATE_DIR = REPO_ROOT / "state"
SEEN_FILE = STATE_DIR / "seen.json"
VERDICTS_FILE = STATE_DIR / "verdicts.jsonl"
NEAR_MISS_FILE = STATE_DIR / "near_misses.jsonl"
# Live operator feedback inbox — appended to the judge's rubric context at
# judge time. In production this is populated by whatever chat/ops surface
# your operator uses to correct verdicts; here it's just a JSONL file anyone
# (a bot, a script, a human editing the file) can append to. The curated
# rubric stays in rubric.md; this is the live corrections lane.
FEEDBACK_FILE = STATE_DIR / "feedback_pending.jsonl"

log = logging.getLogger("rubric-judge")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "watcher.log")
    stream = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    stream.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(stream)


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or (REPO_ROOT / "config.yaml")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_rubric(config: dict) -> str:
    """Read the prose rubric document the judge scores against.

    Path is a config value (default "./rubric.md"), resolved relative to the
    repo root — never a hardcoded path, so swapping the rubric is a config
    change, not a code change.
    """
    rel = config.get("rubric_doc", "./rubric.md")
    path = REPO_ROOT / rel
    return path.read_text(encoding="utf-8")


def load_pending_feedback(max_entries: int = 200) -> str:
    """Read the live operator feedback inbox into a compact text block for the judge.

    Mechanism only — no rubric content lives here. Each line is one operator
    ruling (verdict + raw text + the item it was matched to, if resolvable).
    Returns "" when the inbox is empty or unreadable so the judge context is
    simply unchanged.
    """
    if not FEEDBACK_FILE.exists():
        return ""
    lines: list[str] = []
    try:
        raw = FEEDBACK_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(rec.get("ts", ""))[:10]
        author = rec.get("author") or "operator"
        verdict = (rec.get("verdict") or "").strip()
        text = (rec.get("text") or rec.get("raw") or "").strip()
        matched = rec.get("matched_title") or rec.get("matched_id")
        prefix = f"{verdict.upper()} " if verdict else ""
        tail = f" (re: {matched})" if matched else ""
        lines.append(f"- [{ts} {author}] {prefix}{text}{tail}")
    if not lines:
        return ""
    return "\n".join(lines[-max_entries:])


def build_cost_gate(config: dict) -> CostPolicyGate:
    gate_cfg = config.get("llm", {}).get("cost_gate", {}) or {}
    caller_id = str(gate_cfg.get("caller_id", "rubric-judge")).strip() or "rubric-judge"
    state_file_cfg = str(gate_cfg.get("state_file", "state/cost_gate.json"))
    state_path = Path(state_file_cfg)
    if not state_path.is_absolute():
        state_path = REPO_ROOT / state_file_cfg
    return CostPolicyGate(caller_id=caller_id, policy=gate_cfg, state_file=state_path, logger=log)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_seen() -> dict[str, Any]:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("seen.json unreadable; treating as empty")
    return {"seeded": False, "boards": {}}


def save_seen(seen: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def item_hash(job: dict, decoded: str) -> str:
    basis = "|".join([
        str(job.get("title", "")),
        greenhouse.location_name(job),
        greenhouse.metadata_summary(job),
        decoded,
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Deterministic pre-gates (cost control — NOT the scoring rubric)
# --------------------------------------------------------------------------- #
def function_gate(title: str, gate_cfg: dict) -> bool:
    """True if the title deterministically fails the configured function filter (kill).

    save_titles wins over kill_titles — e.g. a customer-facing title survives
    a bare substring match against a general engineering kill-pattern. Titles
    matching neither list pass through to the LLM judge. This whole gate is a
    cost-control shortcut for the unambiguous cases; the judge's rubric should
    carry the same policy for everything else.
    """
    t = (title or "").lower()
    if any(s in t for s in gate_cfg.get("save_titles", [])):
        return False
    return any(k in t for k in gate_cfg.get("kill_titles", []))


def location_eligible(location: str, tokens: list[str]) -> bool:
    loc = (location or "").lower()
    if not loc:
        return True  # unknown location -> let the judge decide
    return any(tok.lower() in loc for tok in tokens)


def home_region_cfg(config: dict) -> tuple[str, str]:
    """(name, abbr) for the optional stricter home-region gate, or ("", "")
    when off (config.example.yaml's default). See
    greenhouse.home_region_pre_gate for what the gate does with these."""
    cfg = config.get("eligibility", {}).get("home_region", {}) or {}
    if not cfg.get("enabled"):
        return "", ""
    return str(cfg.get("name") or "").strip(), str(cfg.get("abbr") or "").strip()


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _band_display(v: dict) -> str:
    return v.get("pay_band") or "band not stated"


def _loc_display(v: dict) -> str:
    return v.get("loc_token") or v.get("location") or "-"


def fmt_alert(v: dict) -> str:
    # Title — score * band * one-token location * url  (+ short WHY line)
    return (
        f"\U0001F3AF **{v['title']}** — {v['score']:.1f} · "
        f"{_band_display(v)} · {_loc_display(v)}\n"
        f"{v['company']} · {v.get('why') or ''}\n"
        f"{v['url']}"
    )


def fmt_digest_item(v: dict) -> str:
    return (
        f"\U0001F3AF **{v['title']}** — {v['score']:.1f} · "
        f"{_band_display(v)} · {_loc_display(v)}\n"
        f"{v['company']} · {v.get('why') or ''}\n{v['url']}"
    )


# --------------------------------------------------------------------------- #
# Core run
# --------------------------------------------------------------------------- #
def process_board(
    slug: str,
    config: dict,
    rubric: str,
    seen: dict,
    cost_gate: CostPolicyGate,
    args: argparse.Namespace,
    first_run: bool,
    feedback: str = "",
    fetch_board_fn=None,
    judge_fn=None,
) -> tuple[list[dict], list[dict], dict]:
    """Returns (alerts, near_misses, stats) for one board.

    fetch_board_fn / judge_fn default to the real greenhouse/judge modules,
    resolved at call time (not bound as default-argument values) so tests can
    monkeypatch `greenhouse.fetch_board` / `judge as judge_mod` (via
    `judge_mod.judge_item`) and have it take effect even when going through
    run() rather than calling process_board() directly.
    """
    fetch_board_fn = fetch_board_fn or greenhouse.fetch_board
    judge_fn = judge_fn or judge_mod.judge_item
    llm_cfg = config.get("llm", {})
    content_max = int(llm_cfg.get("content_max_chars", 6000))
    loc_tokens = config.get("eligibility", {}).get("location_include", [])
    home_name, home_abbr = home_region_cfg(config)
    alert_th = float(config["thresholds"]["alert"])
    near_floor = float(config["thresholds"]["near_miss_floor"])
    comp_floor = float(config["thresholds"].get("comp_floor_base_top", 100000))
    ote_floor = float(config["thresholds"].get("comp_floor_ote_top", 150000))
    fn_gate_cfg = config.get("function_gate", {})

    only_ids = set(args.only_ids.split(",")) if args.only_ids else None

    try:
        jobs = fetch_board_fn(slug)
    except Exception as exc:  # noqa: BLE001
        log.error("board %s fetch failed: %s", slug, exc)
        return [], [], {"error": str(exc)}

    board_seen = seen["boards"].setdefault(slug, {})
    alerts: list[dict] = []
    near_misses: list[dict] = []
    stats = {"total": len(jobs), "candidates": 0, "eligible": 0, "judged": 0,
             "alerts": 0, "near": 0, "skipped_eligibility": 0,
             "skipped_home_region": 0, "skipped_comp": 0,
             "skipped_function": 0, "errors": 0}

    judged_count = 0
    for job in jobs:
        jid = str(job.get("id"))
        if only_ids is not None and jid not in only_ids:
            continue

        decoded = greenhouse.decode_content(job.get("content"))
        h = item_hash(job, decoded)

        # Stage 1: diff against state.
        if only_ids is None and board_seen.get(jid) == h:
            continue  # unchanged
        stats["candidates"] += 1

        location = greenhouse.location_name(job)
        blob = greenhouse.eligibility_blob(job)
        loc_token = greenhouse.location_token(job, home_name, home_abbr)
        pay_lo, pay_hi = greenhouse.extract_pay_range(job, decoded)
        pay_basis = greenhouse.comp_basis(decoded)
        pay_band = greenhouse.fmt_band_compact(pay_lo, pay_hi)
        if pay_band and pay_basis == "ote":
            pay_band += " OTE"
        company = job.get("company_name") or slug
        posted_at = job.get("first_published") or job.get("updated_at")

        def _skip(reason_code: str, why: str) -> None:
            verdict = {
                "ts": dt.datetime.utcnow().isoformat() + "Z", "board": slug, "id": jid,
                "title": job.get("title"), "company": company, "location": location,
                "loc_token": loc_token, "url": job.get("absolute_url"),
                "pay_band": pay_band, "posted_at": posted_at,
                "stage": "eligibility_skip", "pre_gate": reason_code,
                "score": None, "why": why, "first_run": first_run,
            }
            if not args.dry_run:
                append_jsonl(VERDICTS_FILE, verdict)

        # Pre-gate 1 (deterministic, geography): must carry a configured
        # location/remote signal. Real geography sometimes lives in offices
        # rather than location on a given board, so both are matched. Items
        # with no signal at all cost no LLM call.
        if not location_eligible(blob, loc_tokens):
            stats["skipped_eligibility"] += 1
            _skip("no_location_signal", "location pre-gate: no configured location match")
            board_seen[jid] = h
            continue

        # Pre-gate 1.5 (deterministic, geography, OFF by default): a stricter
        # single-region hard-kill on top of the coarse check above — see
        # eligibility.home_region in config.example.yaml. Only fires when a
        # home region is configured; otherwise home_region_pre_gate always
        # returns None and this never skips anything.
        if greenhouse.home_region_pre_gate(blob, home_name, home_abbr) == "outside_home_region":
            stats["skipped_home_region"] += 1
            _skip("outside_home_region",
                  "home-region pre-gate: enumerates a different specific US region, "
                  "no home region / no nationwide (outside_home_region)")
            board_seen[jid] = h
            continue

        # Pre-gate 2 (deterministic, compensation): a stated band top below
        # the configured floor is a hard-kill. Unknown band still judges — it
        # is a risk factor flagged "band not stated", not a kill.
        effective_floor = ote_floor if pay_basis == "ote" else comp_floor
        if pay_hi is not None and pay_hi < effective_floor:
            stats["skipped_comp"] += 1
            _skip("comp_below_floor",
                  f"comp pre-gate: {pay_basis}-band top ${pay_hi:,} below "
                  f"${int(effective_floor):,} {pay_basis} floor (comp_below_floor)")
            board_seen[jid] = h
            continue

        # Pre-gate 3 (deterministic, function): titles configured as never a
        # fit are hard-killed here regardless of level or comp; save_titles
        # wins over kill_titles so an ambiguous engineering-adjacent title can
        # still be judged.
        if function_gate(job.get("title") or "", fn_gate_cfg):
            stats["skipped_function"] += 1
            _skip("function_not_target",
                  "function pre-gate: title matches the configured non-target list "
                  "(function_not_target)")
            board_seen[jid] = h
            continue
        stats["eligible"] += 1

        if args.limit and judged_count >= args.limit:
            log.info("board %s: hit --limit %d, stopping", slug, args.limit)
            break

        # Stage 2: LLM judge.
        item = {
            "title": job.get("title"), "company": company, "location": location,
            "pay_band": pay_band, "url": job.get("absolute_url"),
            "metadata": greenhouse.metadata_summary(job), "content": decoded,
        }
        result = judge_fn(item, rubric, config, cost_gate=cost_gate, feedback_text=feedback)
        judged_count += 1
        stats["judged"] += 1
        if result.get("budget_blocked"):
            log.warning("board %s: budget gate blocked at item %s; stopping board", slug, jid)
            stats["errors"] += 1
            break
        if result.get("error"):
            stats["errors"] += 1

        verdict = {
            "ts": dt.datetime.utcnow().isoformat() + "Z", "board": slug, "id": jid,
            "title": job.get("title"), "company": company, "location": location,
            "loc_token": loc_token, "url": job.get("absolute_url"),
            "pay_band": pay_band, "posted_at": posted_at, "stage": "judged",
            "score": result.get("score"), "hard_kill": result.get("hard_kill"),
            "hard_kill_reason": result.get("hard_kill_reason"), "why": result.get("why"),
            "model": result.get("model_used"), "tokens_in": result.get("tokens_in"),
            "tokens_out": result.get("tokens_out"), "error": result.get("error"),
            "first_run": first_run,
        }
        if not args.dry_run:
            append_jsonl(VERDICTS_FILE, verdict)

        score = result.get("score") or 0.0
        if result.get("error"):
            pass  # errored items: logged only, not alerted; state still advances
        elif score >= alert_th:
            alerts.append(verdict)
            stats["alerts"] += 1
        elif score >= near_floor:
            near_misses.append(verdict)
            stats["near"] += 1
            if not args.dry_run:
                append_jsonl(NEAR_MISS_FILE, verdict)

        board_seen[jid] = h

    # Roll-off: any item we have state for that is absent from THIS successful
    # full board fetch has been closed/removed. Tombstone it (stage "closed")
    # so consumers drop it, and forget it in board_seen — if it ever reposts
    # under the same id it will be re-judged as new. Skipped under
    # --only-ids (surgical re-judge mode, not a full-board pass); a failed
    # fetch already returned above, so a board outage can never mass-close
    # items.
    if only_ids is None:
        live_ids = {str(j.get("id")) for j in jobs}
        closed_ids = [jid for jid in board_seen if jid not in live_ids]
        stats["closed"] = len(closed_ids)
        for jid in closed_ids:
            tombstone = {
                "ts": dt.datetime.utcnow().isoformat() + "Z", "board": slug, "id": jid,
                "stage": "closed", "score": None,
                "why": "item no longer present on board fetch (closed/removed)",
            }
            if not args.dry_run:
                append_jsonl(VERDICTS_FILE, tombstone)
                del board_seen[jid]
        if closed_ids:
            log.info("board %s: %d item(s) closed since last run: %s",
                     slug, len(closed_ids), ",".join(closed_ids[:20]))
    else:
        stats["closed"] = 0

    return alerts, near_misses, stats


def run(args: argparse.Namespace) -> int:
    config = load_config()
    rubric = load_rubric(config)
    feedback = load_pending_feedback()
    cost_gate = build_cost_gate(config)
    seen = load_seen()
    first_run = not seen.get("seeded", False)
    if feedback:
        log.info("pending operator feedback: %d entr(y|ies) folded into judge context",
                 len(feedback.splitlines()))

    boards = [args.board] if args.board else config.get("boards", [])
    notifier = notify_mod.resolve_notifier(REPO_ROOT, config.get("notifier") or {})

    all_alerts: list[dict] = []
    all_near: list[dict] = []
    log.info("rubric-judge run start | first_run=%s dry_run=%s boards=%s",
             first_run, args.dry_run, boards)

    for slug in boards:
        alerts, near, stats = process_board(slug, config, rubric, seen, cost_gate, args,
                                            first_run, feedback=feedback)
        log.info("board %s: %s", slug, json.dumps(stats))
        all_alerts.extend(alerts)
        all_near.extend(near)

    # Delivery.
    if first_run:
        _deliver_first_run(all_alerts, boards, seen, notifier, args)
    else:
        _deliver_alerts(all_alerts, notifier, args)

    if all_near and not first_run:
        log.info("%d near-miss(es) logged for periodic digest", len(all_near))

    # Persist.
    if not args.dry_run:
        seen["seeded"] = True
        seen["last_run"] = dt.datetime.utcnow().isoformat() + "Z"
        save_seen(seen)
    else:
        log.info("dry-run: state and audit NOT persisted")

    budget = cost_gate.get_run_summary()
    log.info("run complete | alerts=%d near=%d | llm_run_usd=$%.4f calls=%d",
             len(all_alerts), len(all_near), budget.get("usd", 0.0), budget.get("calls", 0))
    return 0


def _deliver_alerts(alerts: list[dict], notifier: "notify_mod.Notifier",
                    args: argparse.Namespace) -> None:
    if not alerts:
        return
    if args.dry_run:
        for v in alerts:
            log.info("[ALERT dry-run]\n%s", fmt_alert(v))
        return
    hits = sorted(alerts, key=lambda v: v.get("score") or 0, reverse=True)
    header = f"\U0001F3AF **rubric-judge — {len(hits)} new alert(s) at score >= alert threshold**"
    notifier.send_chunked(header, [fmt_alert(v) for v in hits])


def _deliver_first_run(alerts: list[dict], boards: list[str], seen: dict,
                       notifier: "notify_mod.Notifier", args: argparse.Namespace) -> None:
    n_indexed = sum(len(b) for b in seen.get("boards", {}).values())
    hits = sorted(alerts, key=lambda v: v["score"], reverse=True)
    header = (
        f"\U0001F9ED **rubric-judge — first-run digest**\n"
        f"Seeded {len(boards)} board(s) ({', '.join(boards)}); {n_indexed} items indexed. "
        f"{len(hits)} currently open at score ≥ alert threshold:"
    )
    if not hits:
        header += "\n_(none at threshold right now — steady-state alerting is now armed)_"
        items: list[str] = []
    else:
        items = [fmt_digest_item(v) for v in hits]

    if args.dry_run:
        log.info("[FIRST-RUN DIGEST dry-run]\n%s\n\n%s", header, "\n\n".join(items))
    else:
        notifier.send_chunked(header, items)


def weekly_digest(args: argparse.Namespace) -> int:
    config = load_config()
    window_days = int(config.get("digest", {}).get("near_miss_window_days", 7))
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=window_days)
    notifier = notify_mod.resolve_notifier(REPO_ROOT, config.get("notifier") or {})

    recent: dict[str, dict] = {}
    if NEAR_MISS_FILE.exists():
        for line in NEAR_MISS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", "")
            try:
                when = dt.datetime.fromisoformat(ts.replace("Z", ""))
            except ValueError:
                continue
            if when >= cutoff:
                recent[f"{rec.get('board')}:{rec.get('id')}"] = rec  # dedup, keep latest

    items = [fmt_digest_item(v) for v in sorted(recent.values(), key=lambda v: v.get("score", 0), reverse=True)]
    header = (
        f"\U0001F4CA **rubric-judge — periodic near-miss digest** "
        f"(near-miss band, last {window_days}d)\n"
        f"{len(items)} item(s) worth a calibration glance:"
    )
    if not items:
        log.info("digest: no near-misses in window; nothing to send")
        return 0
    if args.dry_run:
        log.info("[DIGEST dry-run]\n%s\n\n%s", header, "\n\n".join(items))
    else:
        notifier.send_chunked(header, items)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="rubric-judge — Greenhouse job-board watch pipeline")
    p.add_argument("--dry-run", action="store_true",
                   help="judge + print, no sends, no state/audit writes")
    p.add_argument("--board", help="limit to a single board slug")
    p.add_argument("--only-ids", help="comma-separated item ids to judge (ignores diff)")
    p.add_argument("--limit", type=int, default=0, help="cap items judged per board")
    p.add_argument("--weekly-digest", action="store_true",
                   help="emit the periodic near-miss digest and exit")
    args = p.parse_args()

    setup_logging()
    try:
        if args.weekly_digest:
            return weekly_digest(args)
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log.exception("rubric-judge run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
