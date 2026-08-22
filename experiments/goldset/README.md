# Reviewer gold set — PILOT-1 (ZeusLogic stand)

Frozen, human-labeled ground truth for review quality, harvested from
the completed PILOT-1 run set (`~/work/zeus-pilot`, 19 tasks, 16
closed, 2026-08-09 … 2026-08-20). Every label here was **paid for
once already** — it is the owner's real decision on a real escalation,
captured as a by-product of the pilot. This corpus is what judges the
review-arm experiment ("human-endorsed majors per dollar") and what any
future prompt/model/effort change regresses against.

## Files

| File | Rows | What it is |
| --- | --- | --- |
| `harvest.py` | — | Deterministic extractor; re-run against any stand |
| `verdicts.jsonl` | 35 | Every `*-review.json` envelope: verdict, full findings, summary, capped analysis, cost, models |
| `questions.jsonl` | 21 | ask_user question/answer pairs — the raw label material |
| `reviews_metrics.jsonl` | 63 | Every review-phase metrics row: arm (model/effort), cost, cache telemetry, `confirming` flag |
| `tasks.jsonl` | 19 | Task outcomes, `human_decisions`, commits |
| `labels.jsonl` | 22 | **Authored labels**: one row per judged claim — subject, disposition, quote-anchored rationale |

Provenance: harvested 2026-08-20 by
`python3 harvest.py --stand ~/work/zeus-pilot --out .`; sources are the
stand's `.swarm/log/run.jsonl`, `metrics.jsonl`, `log/*-review.json`,
`tasks.json`. Labels authored by reading every question/answer pair in
full; each row quotes the owner's answer.

## Label taxonomy

`subject`: `reviewer_finding` (an escalated review finding),
`executor_dispute` (a boundary/spec dispute), `loop_diagnosis` (the
orchestrator's rounds-exhausted diagnosis), `integrity_alarm`,
`review_failure`. `disposition`: `endorsed` (adopted as-is),
`endorsed_modified` (claim real, remedy changed), `rejected`, `moot`.

## What the labels already show (n is small; treat as priors)

- **Reviewer findings: 6/6 escalations endorsed**, two of them
  `major` (q002 duplicated ordering primitive; q017 escaping missing
  from the spec), one endorsed with a swapped remedy (q018). Zero
  reviewer escalations were rejected by the owner in this pilot.
- **Executor disputes: 7/7 granted** (q001, q005, q011, q012, q014,
  q016, q020) — every boundary the executor contested was in fact set
  wrong. The scope guard's *detections* were right; the *task
  boundaries* were the recurring defect.
- **Loop size-diagnoses: 0/6 correct** — every «задача слишком
  крупная» verdict had a different real cause (arm disagreement, test
  protection, timeouts, provider quota). The diagnoser was rewritten
  mid-pilot because of exactly this.
- **Integrity alarms: 2/2 operator-caused false alarms**, both of
  which produced tool fixes.

## Derived benches

Three sub-corpora turn labels into regressions. Both are frozen the same
way the labels are, and both exist because a claim nobody re-runs is a
memory, not a measurement.

`diagnosis/` — the six escalations where the owner named the true
cause (q004, q007, q008, q009, q010, q019), replayed against
`Loop._diagnose` with the evidence in the exact shape the diagnoser
sees. `replay.py` prints a scorecard; `tools/swarm/tests/
test_diagnosis_bench.py` runs the same cases inside the gate and fails
loudly if the case file disappears. Current score 6/6
(`executor_environment=4, reviewer_disagreement=1, scope_guard=1`);
the pre-2026-08-20 diagnoser scores 2/6 on it.

`verdicts/` — eight frozen payloads of reviewer answers that failed
their own contract, four of them **real** (E13, 2026-08-22, where 5 of
17 reviewer calls returned no valid verdict and burned 37 % of review
spend) and four synthetic negatives. Two failure shapes: a placeholder
(`analysis: "Test"` with `verdict: approve`), which must stay refused,
and a verdict crammed into the `analysis` field with pseudo-XML tags,
which must be recovered. The bench is deliberately two-sided — a
one-sided one ("N rescued") would push the code toward inventing
verdicts. Needs no stand, so it runs in the gate
(`tools/swarm/tests/test_verdict_salvage.py`); current score 8/8, and on
the real streams the repair returns 2 of the 4 losses.

`boundaries/` — the seven boundary grants (six executor disputes plus
one intent question, q005 q011 q012 q014 q016 q017 q020) replayed
against `planner.boundary_warnings`: for each, the boundary is rolled
back to its pre-dispute state (subtract exactly what the owner added)
and the question is whether the linter would have named that file in
advance. Needs the stand on disk, so it is **not** in the gate —
`python3 boundaries/replay.py [stand]`, and it exits quietly when the
stand is absent. Measured 2026-08-20: 4/7 at the shipped cap of 6
advisory lines, 5/7 at 8; q014 and q016 are not found at any depth.
Honest caveat: the stand's repository has moved on since the pilot, so
today's number is an upper bound on what the linter knew on planning
day.

## The metric

For a review arm A (model/effort):

    endorsed_majors_per_dollar(A) =
        count(labels: subject=reviewer_finding, disposition∈{endorsed,
              endorsed_modified}, severity=major, arm(finding)=A)
      / sum(reviews_metrics: cost_usd where model/effort = A)

Arm attribution goes through `reviews_metrics.jsonl` (join on
task/iter/attempt and timestamp); two endorsed majors so far both
belong to `claude-opus-5/xhigh`.

## Rules

1. **Freeze**: rows are never edited retroactively. New pilots append
   new harvests (subdirectory per stand/date) — they do not overwrite
   this one.
2. **A failing case is a signal, not garbage** (rule borrowed from the
   ai-reviewer gold set): if a future reviewer configuration
   contradicts an endorsed label, that is a finding to investigate,
   never a reason to delete the label.
3. **Honest gaps**: `verdicts.jsonl` holds only the *last* envelope
   per (task, iteration, attempt) — requeued iterations overwrite the
   file; `reviews_metrics.jsonl` is the complete call record, use it
   for counting and cost. Executor (Kimi) costs are invisible to the
   loop and absent here. Labels cover *escalated* findings; the many
   mechanically-fixed findings carry only the weak implicit label
   "executor complied and the reviewer confirmed".
