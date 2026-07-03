"""Minimal Discord webhook delivery.

Reads the webhook URL from the `DISCORD_WEBHOOK_URL` environment variable (or
an explicit argument, for tests). Deliberately tiny: one POST, one truncation
rule, no bot/gateway dependency. A production stack usually has a shared
version of this with severity routing to multiple channels; this is the
one-webhook version of the same idea.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("rubric-judge")

DISCORD_MESSAGE_LIMIT = 1900  # Discord's hard cap is 2000; leave headroom


def load_webhook_url(explicit: str | None = None) -> str | None:
    """Return the configured webhook URL, or None if unset."""
    url = (explicit or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    return url or None


def send_discord(message: str, webhook_url: str | None = None, user_agent: str = "rubric-judge") -> bool:
    """POST a message to Discord. Returns True on success, False otherwise.

    Never raises — delivery failures are logged and swallowed so a notify
    hiccup can't take down the run.
    """
    url = load_webhook_url(webhook_url)
    if not url:
        log.warning("no Discord webhook configured (DISCORD_WEBHOOK_URL unset); skipping send")
        return False

    if len(message) > DISCORD_MESSAGE_LIMIT:
        message = message[:DISCORD_MESSAGE_LIMIT] + "\n... (truncated)"

    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": user_agent},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("webhook send failed: %s", exc)
        return False
