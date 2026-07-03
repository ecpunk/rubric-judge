# rubric-judge

> **DRAFT** — this README is a structural skeleton (architecture, quickstart,
> section headings) staged for coordinator review. Final prose, tone, and any
> additional framing are TBD; do not treat the wording below as final copy.

A two-stage watch pipeline: deterministic pre-gates filter a stream of items
down to the ones worth an expensive look, then an LLM judge scores the
survivors against a prose policy rubric — a plain-English document, not a
keyword list or a fine-tuned model. Every verdict, including the ones the
pre-gates killed before they ever reached the judge, lands in an append-only
audit log. An operator feedback loop lets corrections change future verdicts
with no code change.

It ships with a Greenhouse job-board adapter as its example data source, but
the pattern — pre-gate, judge-against-a-rubric, audit, feedback — generalizes
to any stream of unstructured items you can write a scoring rubric for.

## Why this pattern

*(TBD — coordinator to expand: the general case for "prose rubric + LLM
judge" over keyword rules or a fine-tuned classifier; why the pre-gate stage
exists — cost control, not correctness; why the audit trail and feedback loop
matter for trusting an LLM-scored pipeline over time.)*

## How it works

Two stages, deliberately split so the expensive stage runs rarely:

1. **Poll + diff (deterministic, free).** Fetch each watched board via the
   Greenhouse public API, hash each item's content, and compare against
   persisted state (`state/seen.json`). Only **new** or **materially-changed**
   items proceed. A handful of deterministic pre-gates — location, a
   compensation floor, a function/title filter — then drop obviously
   out-of-scope items before any LLM budget is spent. These pre-gates are a
   cost-control shortcut for the unambiguous cases, not the scoring policy
   itself.

2. **Judge (LLM, cheap model).** Each surviving item is scored 0–10 by a
   Haiku-class model reading the rubric document (`rubric.md`) at runtime.
   The rubric is **never** in the code — swapping it, or appending an
   operator correction to its Feedback Log, changes future verdicts with no
   deploy. The judge returns a score, any hard-kill reason, and a short "why".

**Delivery.** Score ≥ `thresholds.alert` → a Discord alert (title, band,
location, score, one-line why, URL). Scores in the near-miss band → logged
for a periodic digest. **Every** verdict, including pre-gate skips → an
append-only JSONL audit log (`state/verdicts.jsonl`).

**First run** seeds state from the currently open items without per-item
flooding: it judges everything once and sends a single digest of what's
already scoring at or above the alert threshold, then arms steady-state
alerting.

## The operator feedback loop

*(TBD — coordinator to expand: how `state/feedback_pending.jsonl` gets
populated in a real deployment, how it composes with the rubric doc's own
Feedback Log section, and why "same authority, more recent wins" is the right
conflict rule.)*

## Layout

| Path | Role |
|------|------|
| `watcher.py` | Orchestrator + CLI (`--dry-run`, `--board`, `--only-ids`, `--limit`, `--weekly-digest`) |
| `greenhouse.py` | Example data-source adapter — Greenhouse board client, HTML decode, pay-band extraction |
| `judge.py` | LLM judge — supplies the mechanism only; the rubric is read from `rubric.md` at runtime |
| `notify.py` | Discord delivery |
| `lib/cost_gate.py` | Vendored per-run / per-day USD + call-count budget gate |
| `lib/discord_webhook.py` | Vendored minimal Discord webhook sender |
| `config.example.yaml` | Boards, thresholds, pre-gate config, LLM + cost-gate policy (copy to `config.yaml`) |
| `rubric.example.md` | Stub — copy to `rubric.md` and write your own prose rubric |
| `state/seen.json` | Persisted seen-item ids + content hashes (the diff state) |
| `state/verdicts.jsonl` | Append-only audit log of every verdict, including pre-gate skips |
| `state/near_misses.jsonl` | Near-miss records feeding the periodic digest |
| `state/feedback_pending.jsonl` | Live operator feedback inbox, folded into the judge's context |
| `tests/` | Dry-run pipeline test against a fixture board, with a stubbed judge (no API key needed) |

## Quickstart

```bash
git clone <this-repo>
cd rubric-judge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp config.example.yaml config.yaml   # edit boards / thresholds / pre-gates
cp rubric.example.md rubric.md       # write your prose rubric here

export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."  # optional
export ANTHROPIC_API_KEY="sk-ant-..."                              # required for real judging

# Dry run against a real board, no sends, no state writes:
python watcher.py --dry-run --board <your-board-slug>

# Run the pipeline tests (fixture board + stubbed judge, no API key needed):
pytest
```

## Configuration & secrets

Nothing sensitive lives in the code or in `config.yaml`. The Anthropic API key
comes from `ANTHROPIC_API_KEY`; Discord delivery comes from
`DISCORD_WEBHOOK_URL`. Boards, thresholds, pre-gate lists, and cost-gate
policy are all in `config.yaml`; the scoring rubric is a separate prose file
(`rubric.md`).

## Cost control

The LLM judge runs behind `lib/cost_gate.py`: a per-run USD cap, a per-day USD
cap (global and per-caller), and a per-run call-count cap, backed by a
file-locked JSON ledger so it holds even across concurrent runs on one host.
