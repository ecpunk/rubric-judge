"""Discord delivery for the watcher.

Thin wrapper over lib/discord_webhook.py: resolve whether a webhook is
configured, and provide a chunking helper so long digests split into
Discord-sized messages without breaking an item mid-text.
"""

from __future__ import annotations

import logging

from lib.discord_webhook import load_webhook_url, send_discord

log = logging.getLogger("rubric-judge")

DISCORD_LIMIT = 1900  # shared lib truncates above this


def resolve_webhook() -> str | None:
    """Return the configured webhook URL (from DISCORD_WEBHOOK_URL), or None."""
    return load_webhook_url()


def send(message: str, webhook_url: str) -> bool:
    return send_discord(message, webhook_url, user_agent="rubric-judge")


def send_chunked(header: str, items: list[str], webhook_url: str) -> bool:
    """Send header + items across as few messages as possible, each <= limit.

    Each item is kept whole (never split mid-item). Returns True if every
    chunk sent successfully.
    """
    ok = True
    chunks: list[str] = []
    current = header
    for item in items:
        candidate = f"{current}\n\n{item}" if current else item
        if len(candidate) > DISCORD_LIMIT and current:
            chunks.append(current)
            current = item
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        ok = send(chunk, webhook_url) and ok
    return ok
