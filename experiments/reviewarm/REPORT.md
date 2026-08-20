# REVIEWARM — Step 0 report: does a free juror panel produce usable material?

**Experiment**: E12 (cheap contour in the review path),
[09-cheap-review-contour.md](../../09-cheap-review-contour.md) §4, Step 0.
**Run**: 2026-08-20, `run_id 20260820T175249-a3460c`, `swarm_sha ba23877`.
**Cost**: **$0.00 metered.** 70 model calls on the flat Ollama Cloud
subscription, 347 s of wall clock in total.
**Verdict**: the roster is viable, the **volume gate is not met as
configured**, and one of the five jurors must be replaced.

---

## What Step 0 was for

The plan's own rule is that nothing gets wired into the loop until a free
panel is shown to produce material an expensive adjudicator can *use* —
and, just as important, not to drown it. That question is answerable
against diffs already paid for, with zero expensive calls, so it is the
gate in front of every other insertion point.

Harness: [`replay.py`](replay.py) (run) + [`analyze.py`](analyze.py)
(score). Both go through the house wrapper `swarm/helpers.ollama_chat` —
native `/api/chat`, `think: false`, temperature 0, `top_k 1`, fixed seed,
secret scrub, circuit breaker, fail-open — so the run is reproducible
from the same roster.

Material: the 14 PILOT-1 tasks that carry a closing commit, taken from
the frozen gold set (`experiments/goldset/tasks.jsonl`), replayed as
`git show --unified=3 <commit>` against the stand `~/work/zeus-pilot`.

Roster: five models, one distinct lens each, so that five jurors buy five
angles rather than five copies of one.

| Juror | Lens |
| --- | --- |
| `deepseek-v4-flash:0731` | correctness |
| `glm-5.1` | duplication |
| `qwen3.5:397b` | tests |
| `gpt-oss:120b` | contract |
| `kimi-k2.7-code` | safety |

---

## Result 1 — jurors answer, and they do not invent addresses

| Juror / lens | calls | empty | truncated | candidates | **off-diff** | s/call |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `deepseek-v4-flash:0731` / correctness | 14 | 0 | 0 | 54 | **0** | 2.7 |
| `glm-5.1` / duplication | 14 | 2 | 1 | 40 | **0** | 5.4 |
| `qwen3.5:397b` / tests | 14 | 1 | 0 | 52 | **0** | 9.7 |
| `gpt-oss:120b` / contract | 14 | 13 | **12** | 3 | **0** | 4.2 |
| `kimi-k2.7-code` / safety | 14 | 2 | 0 | 46 | **0** | 2.8 |

The single most useful number here is the **off-diff column: zero across
all 70 calls.** Not one juror named a file that was not in the diff it
was shown. The plan treated address hallucination as the main mechanical
risk of a contract-free contour and put a validator in front of it; the
validator turns out to reject nothing. Cheap models on this task are not
confused about *where* they are looking — the open question is only
whether the claim is true, which is exactly the question an adjudicator
is for.

Everything else in the table is a roster fact, not a contour fact:
prompt-coerced JSON parsed on essentially every non-truncated answer,
confirming ADR-004's finding that the format has to be extracted by
prompt and checked locally, but that this works.

## Result 2 — `gpt-oss:120b` is unusable at this budget, and costs nothing to drop

`gpt-oss:120b` produced 3 candidates in 14 calls because **12 of its 14
answers hit the 900-token `num_predict` cap** and were discarded under
ADR-004's `done_reason == "length"` rule. This is not an API failure —
the calls returned `ok` at the transport level; the model simply narrates
at a length this budget does not buy. Two options exist (raise
`--max-tokens` for that juror, or replace it), and the measurement says
which one is cheap: **removing `gpt-oss:120b` entirely costs zero address
coverage** (26/34 files with it, 26/34 without it). It contributed no
unique address in the whole run.

Unique address contribution per juror — files the paid reviewer also
named, that *only* this juror reached:

| Juror | unique addresses |
| --- | ---: |
| `qwen3.5:397b` | **3** |
| `glm-5.1` | 1 |
| `kimi-k2.7-code` | 1 |
| `deepseek-v4-flash:0731` | 0 |
| `gpt-oss:120b` | 0 |

`qwen3.5:397b` is the only juror that repeatedly reaches material the
others miss, and it is also the slowest (9.7 s). Since a parallel panel
costs the *slowest* juror, not the average, this is the one place where
the panel's latency is actually bought rather than wasted.

## Result 3 — the volume gate FAILS as configured

The plan's §4 gate: fewer than 10 candidates per diff after dedup,
otherwise the adjudicator is drowned and the free contour has moved cost
rather than removed it.

**Measured: 13.4 per diff on average, median 15, maximum 19.** Ten of
the 14 diffs are over the ceiling. The gate is not met.

Two things are worth separating here. First, the cap is being obeyed —
each juror was allowed 5 findings and averaged 2.9–3.9, so the volume is
five honest jurors summing, not one juror padding. Second, **dedup
barely fires**: 187 raw candidates merge to 187 minus a handful. The
dedup rule is same-file plus 0.6 word overlap, and jurors under different
lenses describe genuinely different claims about the same file in
different words. Deduping the *panel* was the wrong model of the problem;
the panel does not repeat itself, it genuinely produces 13 distinct
claims per diff.

What actually passes the gate, measured over the same run:

| Filter | candidates | per diff | max | gate | address coverage | major-file coverage |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| everything, as run | 187 | 13.4 | 19 | ✗ | 26/34 | 3/4 |
| drop `nit` | 167 | 11.9 | 18 | ✗ | 26/34 | 3/4 |
| file named by ≥2 jurors | 175 | 12.5 | 19 | ✗ | 21/34 | 3/4 |
| **`severity == major` only** | 57 | **4.1** | 8 | **✓** | 20/34 | 2/4 |
| ≥2 jurors **and** major | 52 | 3.7 | 7 | ✓ | 18/34 | 2/4 |

Read honestly, this is a trade and not a free win. Filtering to the
jurors' own `major` self-rating passes the gate with room to spare, but
it drops address coverage from 26/34 to 20/34 and — the number that
matters most — **from 3 of the paid reviewer's 4 major-finding files down
to 2**. Cross-juror agreement is the worst of the options: it removes
almost no volume (jurors agree on files, they differ on claims) while
costing five addresses.

The conclusion for P2 in the plan is therefore narrower than the plan
assumed: **the panel's output cannot be handed to the adjudicator whole,
and severity self-rating is a lossy filter.** The volume problem is real
and the obvious knobs solve it by throwing away exactly the material the
panel exists to find. The next configuration to measure is a lower
per-juror cap (5 → 2, forcing each juror to rank its own findings)
against the same gold set, since that spends the ranking budget inside
the juror, where the lens context still exists, rather than in a filter
that only sees a severity label.

## Result 4 — address agreement with the paid reviewer

Of the 34 files the expensive Opus reviewer named across these 14 tasks,
**the panel named 26 (76 %)**, and it named 9 files the paid reviewer did
not. Of the 4 files carrying a `major` finding from the paid reviewer,
the panel reached 3.

**This is not recall, and must not be reported as recall.** It is
agreement of *address*, not of claim: the panel naming `load.rs` and the
reviewer naming `load.rs` says nothing about whether they saw the same
defect. It is reported because it is the only join that survives the
material problem below.

## The measurement that Step 0 could not make

Recall against the gold set's endorsed labels — the number everyone
actually wants — **is not obtainable from committed diffs**, and this was
discovered while building the harness rather than assumed.

Every endorsed reviewer finding in PILOT-1 was *fixed before the commit
that closed its task*, because the loop only commits after approve.
`git show <commit>` therefore shows the corrected code, not the defect
the reviewer saw. Verified on two rows: `e9tf`/`baf027e` already contains
the fix, and `q004`/`e7in`'s own label records that the task was re-queued
with tightened acceptance, so its closing commit belongs to the second
attempt. Scoring a panel for "recall" against that material would produce
a number that looks like recall and is not one.

The defective state does survive, in the executor session logs
(`.swarm/log/<task>-i<N>-executor.jsonl`), which were checked and do
carry full tool-call arguments — i.e. the writes that produced the tree
the reviewer reviewed. Recall is measurable, but it needs an
executor-log replay that does not exist yet. This is recorded in
`experiments/findings.jsonl` as `E12/goldset-recall` and stated in the
harness docstring so the gap cannot be quietly forgotten by the next
person to run it.

## Cost, stated in both currencies

Per §6 of the plan, a free contour must never be reported in dollars
alone, or moving work onto a flat subscription makes any per-dollar
metric look infinitely good.

- **Metered**: $0.00. Nothing in this run touched a metered API.
- **Unmetered**: 70 calls, 347 s total wall clock, **548 K input tokens
  and 30 K output tokens** consumed off the subscription. Sequentially
  that is 25 s per diff; run in parallel, a diff costs the slowest juror,
  **≈ 9.7 s**.

  For scale, the same 548 K input tokens on the metered Haiku contour
  would be ≈ $0.55, and on Opus ≈ $2.74 — the panel is free, but it is
  not *small*, and that volume is what §6 requires be reported next to
  the zero.

Against the paid baseline of $2.03 and tens of seconds for one Opus
review round, a parallel panel is roughly latency-neutral and
dollar-free. That is the case for continuing — but it is a statement
about *input to* the adjudicator, and the adjudicator is still unpriced,
because the volume gate failed and the adjudicator prompt was therefore
never run.

## What this run decides, and what it does not

Decided:

- The Ollama contour produces mechanically valid, addressed, parseable
  review material at $0 and near-neutral latency. **Zero hallucinated
  addresses in 70 calls.**
- `gpt-oss:120b` leaves the roster (12/14 truncated, 0 unique addresses,
  no coverage cost to remove).
- `qwen3.5:397b` stays despite being slowest — it is the only juror with
  repeated unique reach.
- Panel dedup is not the volume lever; the panel does not repeat itself.

Not decided, and explicitly still open:

- Whether the adjudicator can use this material profitably — untestable
  until volume is under the gate.
- Recall against endorsed labels — needs executor-log replay.
- Every metered insertion point (P0 Haiku arm, P1 triage, P3 digest);
  Step 0 says nothing about them beyond clearing the way.

**Next measurement**: re-run this roster with a per-juror cap of 2 and
`gpt-oss:120b` replaced, then score the same way. If the gate passes with
major-file coverage still at 3/4, P2 gets an adjudicator prompt and its
first metered number.

---

Artifacts (scratch, gitignored — reproduce with the command below):
`out/panel-raw.jsonl`, `out/panel-candidates.jsonl`,
`out/panel-summary.json`, `out/helper-metrics.jsonl`.

```
python3 replay.py --stand ~/work/zeus-pilot \
    --models deepseek-v4-flash:0731,glm-5.1,qwen3.5:397b,gpt-oss:120b,kimi-k2.7-code \
    --out out
python3 analyze.py --out out
```
