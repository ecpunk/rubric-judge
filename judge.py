"""LLM judge — stage 2 of the pipeline.

Scores a single item 0-10 against a prose rubric document (read at runtime,
passed in as `rubric_text`). The rubric is NEVER hardcoded here — this module
only supplies the mechanism: build the prompt, call the model, parse and
normalize the response. Operator feedback (see the pending-feedback inbox in
watcher.py) is folded in at the same authority as the rubric's own feedback
log, so future verdicts change with no code change.

Uses a cheap Haiku-class model through the anthropic SDK, gated by the
vendored cost gate (lib/cost_gate.py).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("rubric-judge")

SYSTEM_TEMPLATE = """You are an automated judge scoring items against a fixed rubric on behalf of an operator.

The CRITERIA DOCUMENT below is the single source of truth. Work in this exact order:

STEP 1 - HARD-KILL CHECK. Go through EVERY hard-kill the document lists, one at a time, \
and decide whether it applies to THIS item. Be strict and literal, not charitable. If the \
document defines a disqualifying condition (e.g. a location, category, or attribute the \
operator has ruled out), it disqualifies the item no matter how attractive it looks on \
other dimensions.
If ANY hard-kill applies: set hard_kill true, score 0, give the reason, and stop.

STEP 2 - SCORE. Only if no hard-kill applies, score 0-10 on the document's dimensions and \
weights. Do NOT anchor to a default score: a strong-sounding attribute is NOT by itself a \
passing score — the number must reflect this item's actual fit on each dimension as the \
document defines it. Compare any stated numeric ranges to the document's stated anchors; \
do not invent a value that is not stated. The operator Feedback Log (if present) has the \
highest priority and overrides everything else. Do not invent criteria not in the document.

Respond with VALID JSON ONLY - no markdown, no prose outside the JSON object:
{"score": <number 0-10, one decimal ok>, "hard_kill": <true|false>, \
"hard_kill_reason": <string or null>, "why": <string: at most two short lines naming \
which dimensions drove the score>}

=== CRITERIA DOCUMENT (authoritative) ===
{criteria}
=== END CRITERIA DOCUMENT ==="""

# Live operator feedback inbox, appended after the curated rubric. Same
# authority as the document's own Feedback Log; the mechanism lives here, the
# content is never in code.
FEEDBACK_TEMPLATE = """

=== PENDING OPERATOR FEEDBACK (live inbox — treat as binding, same authority as the \
document's Feedback Log; more recent entries win on conflict) ===
{feedback}
=== END PENDING OPERATOR FEEDBACK ==="""


def build_system_prompt(criteria_text: str, feedback_text: str = "") -> str:
    prompt = SYSTEM_TEMPLATE.replace("{criteria}", criteria_text.strip())
    fb = (feedback_text or "").strip()
    if fb:
        prompt += FEEDBACK_TEMPLATE.replace("{feedback}", fb)
    return prompt


def build_user_prompt(item: dict[str, Any]) -> str:
    """item carries the normalized fields the judge needs.

    The field set below matches the Greenhouse adapter's output (title,
    company, location, pay_band, url, metadata, content); swap in whatever
    fields your own adapter produces.
    """
    parts = [
        "ITEM TO SCORE:",
        f"  Title: {item.get('title', '')}",
        f"  Company: {item.get('company', '')}",
        f"  Location: {item.get('location', '')}",
        f"  Detected pay band (from source text): {item.get('pay_band') or 'not stated'}",
        f"  URL: {item.get('url', '')}",
    ]
    if item.get("metadata"):
        parts.append(f"  Metadata: {item['metadata']}")
    parts.append("")
    parts.append("BODY TEXT (decoded, may be truncated):")
    parts.append(item.get("content", "")[: item.get("content_max_chars", 6000)])
    return "\n".join(parts)


def parse_response(text: str) -> dict[str, Any] | None:
    """Parse the judge's JSON. Tolerates markdown fences and surrounding prose."""
    if not text:
        return None
    cleaned = text.strip()
    if "```" in cleaned:
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _estimate_input_tokens(system_prompt: str, user_prompt: str) -> int:
    return max(1, int((len(system_prompt) + len(user_prompt)) / 4))


def _normalize(parsed: dict[str, Any]) -> dict[str, Any]:
    try:
        score = float(parsed.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(10.0, score))
    hard_kill = bool(parsed.get("hard_kill", False))
    if hard_kill:
        score = 0.0
    return {
        "score": score,
        "hard_kill": hard_kill,
        "hard_kill_reason": parsed.get("hard_kill_reason"),
        "why": str(parsed.get("why", "")).strip(),
    }


def judge_item(
    item: dict[str, Any],
    rubric_text: str,
    config: dict[str, Any],
    cost_gate: Any | None = None,
    feedback_text: str = "",
) -> dict[str, Any]:
    """Score one item. Returns a verdict dict:

    {score, hard_kill, hard_kill_reason, why, model_used, tokens_in, tokens_out,
     error, budget_blocked}

    On any failure (no key, budget block, API error, unparseable) returns a
    verdict with error set and score 0 so the caller can log it without
    alerting.
    """
    llm_cfg = config.get("llm", {})
    model = llm_cfg.get("model", "claude-haiku-4-5-20251001")
    max_tokens = int(llm_cfg.get("max_tokens", 400))

    base = {
        "score": 0.0,
        "hard_kill": False,
        "hard_kill_reason": None,
        "why": "",
        "model_used": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "error": None,
        "budget_blocked": False,
    }

    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        base["error"] = "ANTHROPIC_API_KEY not set"
        log.warning("judge: no ANTHROPIC_API_KEY; skipping LLM call")
        return base

    system_prompt = build_system_prompt(rubric_text, feedback_text)
    user_prompt = build_user_prompt({**item, "content_max_chars": llm_cfg.get("content_max_chars", 6000)})

    if cost_gate is not None:
        est_in = _estimate_input_tokens(system_prompt, user_prompt)
        decision = cost_gate.preflight(
            model=model,
            provider="anthropic",
            input_tokens_estimate=est_in,
            expected_output_tokens=max_tokens,
        )
        if not decision.allowed:
            base["error"] = f"budget gate blocked: {decision.reason}"
            base["budget_blocked"] = True
            log.warning("judge budget-blocked: %s", decision.reason)
            return base

    try:
        from anthropic import Anthropic
    except ImportError:
        base["error"] = "anthropic SDK not installed"
        log.error("judge: anthropic SDK not installed")
        return base

    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - surface any API error as a logged verdict
        base["error"] = f"api error: {exc}"
        log.error("judge API call failed: %s", exc)
        return base

    text = ""
    for block in getattr(response, "content", []) or []:
        t = getattr(block, "text", None)
        if t:
            text += t

    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
    base["model_used"] = model
    base["tokens_in"] = tokens_in
    base["tokens_out"] = tokens_out

    if cost_gate is not None:
        cost_gate.record_usage(
            model=model,
            provider="anthropic",
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
        )

    parsed = parse_response(text)
    if not parsed:
        base["error"] = "unparseable judge response"
        log.warning("judge unparseable response: %s", text[:200])
        return base

    base.update(_normalize(parsed))
    return base
