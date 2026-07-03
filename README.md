# rubric-judge

rubric-judge is a two-stage watch pipeline for streams of unstructured items:
deterministic pre-gates eliminate everything objectively out of scope, then an
LLM judge scores the survivors against a prose policy rubric — a plain-English
document, not a keyword list or a fine-tuned model. Every verdict, including
the ones the gates killed before the judge ever saw them, lands in an
append-only audit log, and an operator feedback loop lets a one-sentence
correction change future verdicts with no deploy.

Think of it as a mailroom clerk working for an editor. The clerk discards
everything addressed to the wrong department — cheap, mechanical, no judgment
required. The editor reads what's left against the house style guide and
decides what deserves attention. And when the editor-in-chief disagrees with
a call, the correction goes into the style guide, so the same mistake is
never made twice.

It ships with a Greenhouse job-board adapter as its example data source, but
the pattern — pre-gate, judge against a rubric, audit, feed back — applies to
any stream you can write a scoring policy for in plain English. It is not a
sketch: this pipeline was extracted from one that runs in production daily,
where the feedback loop converged on its operator's judgment within days of
going live.

## Why this pattern

Keyword rules fail silently. They match what you told them to match, miss what
you meant, and never tell you the difference. Fine-tuned classifiers fix the
"what you meant" problem but move the policy somewhere nobody can read it, and
changing your mind means retraining. A prose rubric splits the difference:
the policy is a plain-English document anyone can read, diff, and amend — and
the judge applies it with actual judgment, not substring matching.

The split between the two stages is where most of the design lives. **Anything
objective belongs in code**: a number below a floor, a location outside the
allowed set, a title in a class you never want. Those are pre-gates — free,
deterministic, and auditable. **The judge should only ever see the genuinely
ambiguous cases.** If you find the LLM making calls a regex could make, move
that call into a gate; if you find a gate needing exceptions, that call was
never objective and belongs in the rubric. In practice the gates kill 90%+ of
the stream, which is why the whole pipeline runs on a cheap model for pennies
a day.

The audit trail is what makes the system trustworthy over time. Every item
gets a verdict line — including the ones the gates killed before the judge
ever saw them. When something doesn't alert and you wonder why, the answer is
one grep away, not a shrug. Confidence in an LLM-scored pipeline doesn't come
from the scores it shows you; it comes from being able to inspect the ones it
didn't.

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

The rubric is the curated policy; the feedback inbox is the live one. They
compose like this:

- **`rubric.md`** carries the deliberate rules, including an append-only
  *Feedback Log* section at the bottom — rulings that started as corrections
  and got promoted to policy.
- **`state/feedback_pending.jsonl`** is the inbox. In a real deployment
  something low-friction writes to it — a chat-bot handler that catches the
  operator replying "bad — we don't care about backfills" under an alert, a
  small CLI, a cron that drains a form. The judge folds pending entries into
  its context on every run, so a one-sentence correction changes the next
  run's verdicts with no deploy and no edit.
- Periodically, pending entries get promoted into the rubric's Feedback Log
  (and the inbox drained), so the curated document stays the single source of
  truth.

When the inbox and the rubric disagree, **more recent wins** — both carry the
same authority (the operator's), and a later ruling is by definition a
refinement of an earlier one. Any other conflict rule forces the operator to
edit the rubric before their correction takes effect, which is exactly the
friction the inbox exists to remove.

The practical effect: the system converges on its operator's judgment in
days. Every correction is one sentence, costs nothing, and is permanent.

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
| `rubric.example.md` | Worked example rubric (competitor hiring-signal intelligence) — copy to `rubric.md` and make it yours |
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
