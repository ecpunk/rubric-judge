"""Greenhouse public board API client.

Fetches open requisitions for a board slug, decodes the HTML job description,
and extracts a coarse base-pay band from the JD text (Greenhouse boards embed
pay-transparency ranges in `content` rather than a structured field for most
US-remote roles). This is the pipeline's data-source adapter — one example of
the kind of unstructured input the two-stage pipeline (deterministic pre-gate
+ LLM judge) is built to consume. Swap in a different adapter for a different
source and the rest of the pipeline is unchanged.

No auth required — these are the public job-board endpoints.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from typing import Any

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Dollar figures like $175,875 or $175,875.00 or $175K
_MONEY_RE = re.compile(r"\$\s?([\d][\d,]{2,})(?:\.\d+)?\s?[kK]?")

# A dollar RANGE, e.g. "$170,625 - $251,250 USD" / "$170,625—$251,250" /
# "$170,625 to 251,250". Greenhouse boards embed the pay-transparency band in
# the JD HTML (often labeled "Base Pay Range") rather than a structured field,
# so a labeled-range scan recovers bands the coarse single-figure scan misses.
_RANGE_RE = re.compile(
    r"\$\s?([\d][\d,]{3,})(?:\.\d+)?\s*(?:USD)?\s*(?:-|–|—|to)\s*"
    r"\$?\s?([\d][\d,]{3,})(?:\.\d+)?\s*(?:USD)?",
    re.I,
)
_BASE_LABEL_RE = re.compile(
    r"(base pay range|salary range|pay range|base salary|compensation range|"
    r"target base|annual base|base compensation)",
    re.I,
)

_OTE_BASIS_RE = re.compile(
    r"(salary|pay|compensation)\s+range\s+includes[^.]{0,120}?(incentive|commission|variable|on[- ]target)"
    r"|on[- ]target\s+earnings"
    r"|\bOTE\b"
    r"|includes\s+the\s+on[- ]target\s+incentive",
    re.IGNORECASE,
)

# Nationwide-remote catch-alls that make a req eligible regardless of any
# specific city/state (used by the location pre-gate below). Bare "United
# States" (a US-office marker present on nearly every US role) does NOT
# count on its own — only remote-nationwide phrasing does.
_NATIONWIDE_RE = re.compile(
    r"("
    r"remote\s*[-–,]?\s*(?:u\.?\s?s\.?\s?a?|united\s+states)"
    r"|(?:u\.?\s?s\.?\s?a?|united\s+states)\s*[-–]?\s*remote"
    r"|remote\s*\(\s*u\.?\s?s\.?\s?a?\s*\)"
    r"|nationwide"
    r"|anywhere\s+in\s+the\s+u\.?\s?s"
    r"|remote\s+anywhere"
    r")",
    re.I,
)


def _to_int(digits: str) -> int | None:
    try:
        return int(str(digits).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_board(slug: str, timeout: int = 30) -> list[dict[str, Any]]:
    """Return the list of open job dicts for a Greenhouse board slug.

    Raises urllib errors on network/HTTP failure so the caller can decide
    whether to continue with the other boards.
    """
    url = BOARD_URL.format(slug=slug)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "rubric-judge/1.0 (+github.com/ecpunk/rubric-judge)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def decode_content(raw_html: str | None) -> str:
    """Strip HTML + unescape entities into readable plain text."""
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def extract_pay_range(job: dict[str, Any], decoded_text: str) -> tuple[int | None, int | None]:
    """Best-effort numeric pay band as (low, high) whole dollars.

    Priority:
      1. structured Greenhouse pay_input_ranges field;
      2. a labeled dollar RANGE in the JD ("Base Pay Range: $X - $Y USD") —
         preferring one adjacent to a base/salary label, then one marked USD;
      3. coarse scan of single dollar figures (min/max), as a last resort.

    Returns (None, None) if nothing plausible is found. `high` is what the
    deterministic comp-floor gate keys on.
    """
    ranges = job.get("pay_input_ranges")
    if isinstance(ranges, list) and ranges:
        r = ranges[0]
        lo, hi = r.get("min_cents"), r.get("max_cents")
        if isinstance(lo, int) and isinstance(hi, int):
            return lo // 100, hi // 100

    text = decoded_text or ""
    candidates: list[tuple[int, int, int]] = []  # (priority, lo, hi)
    for m in _RANGE_RE.finditer(text):
        lo, hi = _to_int(m.group(1)), _to_int(m.group(2))
        if lo is None or hi is None:
            continue
        if hi < lo:
            lo, hi = hi, lo
        if hi < 40_000 or hi > 2_000_000:  # implausible as an annual band
            continue
        window = text[max(0, m.start() - 60): m.start()]
        if _BASE_LABEL_RE.search(window):
            prio = 2
        elif "usd" in m.group(0).lower():
            prio = 1
        else:
            prio = 0
        candidates.append((prio, lo, hi))
    if candidates:
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, lo, hi = candidates[0]
        return lo, hi

    figures: list[int] = []
    for m in _MONEY_RE.finditer(text):
        val = _to_int(m.group(1))
        if val is None:
            continue
        if m.group(0).lower().rstrip().endswith("k"):
            val *= 1000
        if 40_000 <= val <= 2_000_000:
            figures.append(val)
    if not figures:
        return None, None
    return min(figures), max(figures)


def comp_basis(decoded_text: str) -> str:
    """'ote' if the JD says its pay range already includes variable/commission,
    else 'base'. Objective text classification — the two bases are meant to be
    compared against different floors in config (an OTE band top isn't apples-
    to-apples with a base-only band)."""
    return "ote" if _OTE_BASIS_RE.search(decoded_text or "") else "base"


def _fmt_k(dollars: int) -> str:
    """$170,625 -> "$170.6K" (compact, one decimal)."""
    return f"${dollars / 1000:.1f}K"


def fmt_band_compact(lo: int | None, hi: int | None) -> str | None:
    """Compact band display, e.g. "$170.6K-$251.2K" or "$180.0K". None if unknown."""
    if lo is None and hi is None:
        return None
    if lo is None or hi is None:
        one = hi if hi is not None else lo
        return _fmt_k(int(one))
    if lo == hi:
        return _fmt_k(int(lo))
    return f"{_fmt_k(int(lo))}-{_fmt_k(int(hi))}"


def extract_pay_band(job: dict[str, Any], decoded_text: str) -> str | None:
    """Compact display band string, e.g. "$170.6K-$251.2K". None if unknown."""
    lo, hi = extract_pay_range(job, decoded_text)
    return fmt_band_compact(lo, hi)


def location_token(job: dict[str, Any]) -> str:
    """One-token location for alerts/digests.

    "Remote-USA" for a nationwide catch-all, else the leading city/label from
    the location name (onsite/hybrid roles).
    """
    blob = eligibility_blob(job)
    if _NATIONWIDE_RE.search(blob):
        return "Remote-USA"
    name = location_name(job).strip()
    if not name:
        return "-"
    # Leading component of "Austin, TX, United States" -> "Austin".
    return name.split(",")[0].strip() or name


def location_name(job: dict[str, Any]) -> str:
    loc = job.get("location")
    if isinstance(loc, dict):
        return str(loc.get("name") or "")
    return ""


def eligibility_blob(job: dict[str, Any]) -> str:
    """All geography-bearing strings for the location pre-gate.

    Greenhouse boards vary: some put the country in `location.name`, others
    put an opaque label there (e.g. "Hybrid"/"Distributed") and carry the real
    place in `offices[].location` (e.g. "Austin, TX, United States", "Remote
    US"). Concatenate both so the pre-gate sees the real geography regardless
    of board convention.
    """
    parts = [location_name(job)]
    for office in job.get("offices") or []:
        if isinstance(office, dict):
            parts.append(str(office.get("name") or ""))
            parts.append(str(office.get("location") or ""))
    return " | ".join(p for p in parts if p)


def metadata_summary(job: dict[str, Any], max_fields: int = 12) -> str:
    """Flatten Greenhouse custom metadata into a compact 'name: value' block."""
    md = job.get("metadata")
    if not isinstance(md, list):
        return ""
    parts = []
    for item in md[:max_fields]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if value in (None, "", []):
            continue
        parts.append(f"{name}: {value}")
    return "; ".join(parts)
