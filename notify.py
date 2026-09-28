"""Delivery for the watcher — pluggable, no bot/token required.

Chosen by `notifier:` in config.yaml (default "stdout" — always available, no
setup). Three backends, all stdlib-only:

  - "stdout" (default): print the digest to the log.
  - "file": append the digest to a local file (`path`), for a "check it when I
    look" workflow.
  - "webhook": POST a generic `{"text": "..."}` JSON body to `webhook_url`.
    This is the same body shape a Slack "Incoming Webhook" integration
    accepts, so pointing it at one delivers to Slack with no other change and
    no bot token; most other chat platforms' generic incoming-webhook
    endpoints accept the same shape too.

Every backend is wrapped in the same Notifier interface (`.send_chunked(header,
items)`), so watcher.py's delivery call sites don't care which backend fired.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger("rubric-judge")

# Conservative default: comfortably under Slack's practical per-message limit
# (its hard cap is 40k chars but long messages collapse behind "show more"),
# and harmless for the file/stdout backends where there is no real limit.
DEFAULT_CHUNK_LIMIT = 3800
_INTER_CHUNK_PAUSE = 1.1


class Notifier:
    """Wraps one backend's single-message `send` into the shared chunking
    rule: pack header + items into as few messages as fit under `chunk_limit`,
    never splitting an item across two messages."""

    def __init__(self, send_fn: Callable[[str], bool], chunk_limit: int = DEFAULT_CHUNK_LIMIT):
        self._send_fn = send_fn
        self._chunk_limit = chunk_limit

    def send_chunked(self, header: str, items: list[str]) -> bool:
        ok = True
        chunks: list[str] = []
        current = header
        for item in items:
            candidate = f"{current}\n\n{item}" if current else item
            if len(candidate) > self._chunk_limit and current:
                chunks.append(current)
                current = item
            else:
                current = candidate
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(_INTER_CHUNK_PAUSE)
            ok = self._send_fn(chunk) and ok
        return ok


def _stdout_send(message: str) -> bool:
    log.info("[notify]\n%s", message)
    return True


def _file_send(path: Path, message: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(message + "\n\n")
    return True


def _webhook_send(url: str, message: str) -> bool:
    body = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        log.error("notify: webhook POST failed: %s", exc)
        return False


def resolve_notifier(repo_root: Path, notifier_cfg: dict) -> Notifier:
    """Build the Notifier for `notifier_cfg` (the `notifier:` block of
    config.yaml). Never returns None — an unset or unrecognized `type`
    degrades to "stdout" (log-only) rather than silently dropping alerts."""
    kind = str(notifier_cfg.get("type") or "stdout").strip().lower()

    if kind == "stdout":
        return Notifier(_stdout_send)

    if kind == "file":
        rel = notifier_cfg.get("path", "state/notifications.log")
        path = Path(rel)
        if not path.is_absolute():
            path = repo_root / rel
        return Notifier(lambda msg, p=path: _file_send(p, msg))

    if kind == "webhook":
        url = str(notifier_cfg.get("webhook_url") or "").strip()
        if not url:
            log.warning("notifier.type=webhook but no webhook_url configured; falling back to stdout")
            return Notifier(_stdout_send)
        return Notifier(lambda msg, u=url: _webhook_send(u, msg))

    log.warning("notifier.type=%r unrecognized; falling back to stdout", kind)
    return Notifier(_stdout_send)
