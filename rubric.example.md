# Rubric — competitor hiring-signal watch

An example of a prose rubric this pipeline can judge against. The scenario: a
sales/strategy team watches a competitor's public job board, because hiring is
the loudest thing a company does quietly. A cluster of new reqs is a roadmap
announcement nobody meant to make.

The judge scores every new posting 0–10 against this document. Alert at ≥7.

## Hard kills (score 0, log, never alert)

1. **Backfill noise** — recruiters, office managers, IT support, generic
   sales AE reqs in an existing region. Companies breathe; this is breathing.
2. **Interns and contractors** — no strategy signal.
3. **Regions we don't compete in** and have no plans to.

## Scoring dimensions

- **New-capability signal (weight: high).** Titles naming technology the
  competitor doesn't ship yet — a "Principal Engineer, Agent Runtime" at a
  company with no agent product is a product announcement with a salary band.
  JD language counts too: unreleased codenames, "founding team," "0→1."
- **Seniority of the hire (high).** A VP or "first hire" for a function that
  didn't exist last quarter means budget and executive sponsorship. One staff
  engineer means an experiment.
- **Cluster velocity (medium).** Three reqs on the same new team inside a
  month is a funded initiative. Score the cluster, not just the posting —
  note prior related alerts in the "why."
- **Geographic expansion (medium).** First field roles in a new country or
  vertical (their first "Federal" req, their first "Healthcare" SE).
- **Compensation outliers (low).** A band far above the competitor's usual
  range for the level suggests they're buying scarce expertise in a hurry.

## Alert format

One line of *what it signals*, not just what the posting says. "They're
hiring their third agent-infra engineer since March — this is a product line,
not a feature" beats "New posting: Senior Engineer."

## Feedback Log (append-only; most recent ruling wins)

- *(empty — operator corrections land here and become policy)*
