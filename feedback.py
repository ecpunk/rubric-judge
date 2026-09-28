"""Operator-feedback contract — the judge loop's inbound lane.

This module owns the feedback contract: the accepted vocabulary, the
item-matching heuristic, the store path, and the record format. Something
low-friction on your own chat/ops surface — a bot handler, a small CLI, a cron
that drains a form — calls `build_record()` on each operator reply and
`append_feedback()` to land it; the consumer, watcher.py's
`load_pending_feedback()`, folds every pending record into the judge's context
on the very next run, no deploy required.

Vocabulary:
  * "good <item-id-or-title-fragment>" / "keep <...>"  -> verdict "good"
  * "bad <item-id-or-title-fragment>"  / "kill <...>"  -> verdict "bad"
  * anything else                                       -> free-text ruling,
    verdict "" (the judge still sees it — a free-text note is a correction too)

Stdlib-only on purpose: whatever surface calls this (a bot process, a cron, a
one-off script) can load it by path with importlib with no extra dependency.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
FEEDBACK_FILE = STATE_DIR / "feedback_pending.jsonl"
VERDICTS_FILE = STATE_DIR / "verdicts.jsonl"

# keyword -> normalized verdict. Order matters only for prefix stripping below.
_VERDICT_KEYWORDS = {"bad": "bad", "kill": "bad", "good": "good", "keep": "good"}


def parse_verdict(raw: str) -> tuple[str, str]:
    """Split a feedback message into (verdict, body).

    verdict is "good", "bad", or "" (free-text ruling). body is the message
    with the leading keyword stripped.
    """
    raw = (raw or "").strip()
    lower = raw.lower()
    for kw, verdict in _VERDICT_KEYWORDS.items():
        if lower == kw or lower.startswith(kw + " ") or lower.startswith(kw + ":"):
            return verdict, raw[len(kw):].lstrip(" :").strip()
    return "", raw


def resolve_item(text: str, verdicts_path: Path = VERDICTS_FILE) -> dict | None:
    """Best-effort match of a feedback message to a known item.

    Resolves by explicit item id (a >=4-digit token that exists in verdicts)
    first, else by a case-insensitive title-fragment overlap against the
    latest verdicts. Returns the matched verdict-ish dict or None. Read-only;
    never raises.
    """
    verdicts_path = Path(verdicts_path)
    if not verdicts_path.exists():
        return None
    latest: dict[str, dict] = {}
    try:
        for line in verdicts_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = str(rec.get("id"))
            if rid:
                latest[rid] = rec  # chronological -> last wins
    except OSError:
        return None
    if not latest:
        return None

    # 1) explicit item id
    for tok in re.findall(r"\d{4,}", text):
        if tok in latest:
            return latest[tok]

    # 2) title-fragment overlap (need a reasonably specific fragment)
    frag = text.strip().lower()
    frag = re.sub(r"^(good|bad|kill|keep|yes|no)\b[:,\s-]*", "", frag).strip()
    if len(frag) >= 4:
        # prefer the most recently-seen matching title
        for rec in reversed(list(latest.values())):
            title = str(rec.get("title") or "").lower()
            if not title:
                continue
            if frag in title or title in frag:
                return rec
    return None


def build_record(
    raw: str,
    *,
    author: str,
    author_id,
    channel_id,
    verdicts_path: Path = VERDICTS_FILE,
) -> dict:
    """One feedback record.

    Key set is the contract: watcher.load_pending_feedback() reads ts/author/
    verdict/text/raw/matched_title/matched_id from it.
    """
    raw = (raw or "").strip()
    verdict, body = parse_verdict(raw)
    matched = resolve_item(raw, verdicts_path)
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "author_id": author_id,
        "channel_id": channel_id,
        "raw": raw,
        "text": body or raw,
        "verdict": verdict,
        "matched_id": (matched or {}).get("id"),
        "matched_title": (matched or {}).get("title"),
        "matched_board": (matched or {}).get("board"),
        "matched_url": (matched or {}).get("url"),
    }


def append_feedback(record: dict, path: Path = FEEDBACK_FILE) -> None:
    """Append one record to the live feedback inbox (JSONL). Raises OSError on
    failure so the caller can decline to mark the event handled — a retry then
    gets another chance to land the write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
