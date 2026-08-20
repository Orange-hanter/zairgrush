# REVIEWARM — Step 0 report: does a free juror panel produce usable material?

**Experiment**: E12 (cheap contour in the review path),
[09-cheap-review-contour.md](../../09-cheap-review-contour.md) §4, Step 0.
**Runs**: two. Run A 2026-08-20 `20260820T175249-a3460c` — **invalid, kept
below as evidence**. Run B 2026-08-20, after the API contract was fixed —
the run this report's numbers come from.
**Cost**: **$0.00 metered** in both. Run B: 70 calls, 270 s wall,
548 K input / 21 K output tokens on the flat Ollama subscription.
**Verdict**: the roster is viable and covers **4 of 4** of the paid
reviewer's major-finding files; the **volume gate is still not met**, and
that is now the only thing standing between P2 and its first metered test.

---

## What Step 0 was for

The plan's rule is that nothing gets wired into the loop until a free
panel is shown to produce material an expensive adjudicator can *use* —
and not to drown it. That is answerable against diffs already paid for,
with zero expensive calls, so it gates every other insertion point.

Harness: [`replay.py`](replay.py) (run) + [`analyze.py`](analyze.py)
(score), both through the house wrapper `swarm/helpers.ollama_chat`.
Material: the 14 PILOT-1 tasks carrying a closing commit, replayed as
`git show --unified=3 <commit>` against `~/work/zeus-pilot`. Roster: five
models, one distinct lens each, so five jurors buy five angles rather
than five copies of one.

---

## Run A was invalid, and the way it was invalid is the most useful thing here

Run A reported that `gpt-oss:120b` was unusable: 12 of its 14 answers hit
the 900-token cap and were discarded under ADR-004's
`done_reason == "length"` rule, and it produced 3 candidates all run. The
report concluded the model leaves the roster.

**That was a verdict on our own call, not on the model.** Ollama's `think`
field accepts a boolean *or* a level (`"low"|"medium"|"high"|"max"`), and
the vendor documentation says plainly that gpt-oss **ignores booleans** —
it always reasons and only accepts a level. `ollama_chat` hardcoded
`think: false`. So gpt-oss reasoned at its default depth, reasoning tokens
counted against the same `num_predict`, and the budget was gone before it
wrote a single character of answer.

Measured directly, same diff, `num_predict = 900`:

| Model | `think: false` | `think: "low"` |
| --- | --- | --- |
| `deepseek-v4-flash:0731` | ✅ 349 tok | ❌ truncated, 3916 chars of thinking |
| `glm-5.1` | ✅ 340 tok | ❌ truncated |
| `qwen3.5:397b` | ✅ 317 tok | ❌ truncated |
| **`gpt-oss:120b`** | **❌ truncated, 4450 chars thinking, EMPTY answer** | **✅ 558 tok, clean JSON** |
| `kimi-k2.7-code` | ✅ 422 tok | ❌ truncated |

Note the inversion: `"low"` is **not** a safe universal default — for the
four models that *can* disable thinking, a level turns it back on and
destroys them. **`think` is a property of the model, not a constant of the
call.**

Two things make this worth writing down rather than quietly fixing.

First, **the knowledge already existed in this repository**. ADR-004
contains the sentence "ограничение сохраняется лишь для семейств, где
трассу нельзя выключить совсем (`gpt-oss:*` — только понижение до
`"low"`)". It was written seven months ago and it is exactly right. The
tool did not carry it: the wrapper had no `think` parameter, so nothing
prevented a caller from selecting a model whose contract the ADR
documented as different. **A constraint that lives only in prose is not a
constraint.**

Second, ADR-004's own closing section is titled "Урок методологии" and
says: a conclusion drawn inside one transport was passed off as a property
of the model. Run A repeated that error one level down — a conclusion
drawn inside one `think` setting, passed off as a property of the model.
The difference is that the first time the knowledge did not exist, and
this time it did.

Fixed, in gated code: `ollama_chat(..., think=)` (default `False`, so every
existing caller is unchanged), a per-model `THINK` map in the harness, and
a `thinking_chars` field in the metrics next to `truncated` — because
without it a truncation row reads as "the model is bad" when the truth is
"the budget went to reasoning nobody asked for". Four new tests, including
one pinning that a level reaches the wire as a string: `bool("low")` is
`True`, i.e. the precise opposite of the intent.

Everything below is Run B.

---

## Result 1 — jurors answer, and they do not invent addresses

| Juror / lens | calls | silent | truncated | candidates | **off-diff** | s/call |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `deepseek-v4-flash:0731` / correctness | 14 | 1 | 0 | 55 | **0** | 3.2 |
| `glm-5.1` / duplication | 14 | 2 | 1 | 42 | **0** | 5.1 |
| `qwen3.5:397b` / tests | 14 | 2 | 0 | 52 | **0** | 5.3 |
| `gpt-oss:120b` / contract | 14 | 9 | 0 | 11 | **0** | 2.1 |
| `kimi-k2.7-code` / safety | 14 | 2 | 1 | 43 | **0** | 3.6 |

The most useful column is **off-diff: zero, across 70 calls and 200
candidates, in both runs**. Not one juror named a file absent from the
diff it was shown. The plan treated address hallucination as the main
mechanical risk of a contract-free contour and put a validator in front of
it; the validator rejects nothing. Cheap models on this task are not
confused about *where* they are looking — the open question is only
whether the claim is true, which is what an adjudicator is for.

`gpt-oss:120b` deserves a second look now that it is configured correctly.
It is the **quietest** juror (11 candidates, 0.8 per diff, silent on 9 of
14 diffs) and the **fastest** (2.1 s) — and it still contributes 2 unique
addresses, tied for the best in the roster. It is the only juror with the
discipline to return an empty array. Run A called it unusable; corrected,
it has the highest signal density of the five.

## Result 2 — coverage of the paid reviewer

| | Run A (invalid) | **Run B** |
| --- | ---: | ---: |
| Files the paid reviewer named, also named by the panel | 26/34 (76 %) | **28/34 (82 %)** |
| Files carrying a `major` paid finding, reached by the panel | 3/4 | **4/4** |
| Files named beyond the paid reviewer | 9 | 9 |
| Wall clock, total | 347 s | 270 s |
| Output tokens (mostly discarded reasoning in A) | 30 K | 21 K |

Unique address contribution — files the paid reviewer also named, that
*only* this juror reached:

| Juror | Run A | Run B |
| --- | ---: | ---: |
| `qwen3.5:397b` | 3 | 2 |
| `gpt-oss:120b` | **0** | **2** |
| `deepseek-v4-flash:0731` | 0 | 1 |
| `glm-5.1` | 1 | 1 |
| `kimi-k2.7-code` | 1 | 0 |

Dropping `gpt-oss:120b` now costs coverage (28/34 → 26/34), the exact
reverse of Run A's finding, which said removal was free.

**This is not recall, and must not be reported as recall.** It is agreement
of *address*, not of claim: the panel naming `load.rs` and the reviewer
naming `load.rs` says nothing about whether they saw the same defect. It
is reported because it is the only join that survives the material problem
below.

## Result 3 — the volume gate still FAILS

The plan's §4 gate: fewer than 10 candidates per diff after dedup,
otherwise the adjudicator is drowned and the free contour has moved cost
rather than removed it.

**Measured: 14.3 per diff, median 16, maximum 20.** Eleven of 14 diffs are
over the ceiling. Fixing the contract made this slightly *worse* than Run
A's 13.4, because a juror that was silently failing now contributes.

The cap is being obeyed — each juror was allowed 5 findings and averaged
0.8–3.9, so this is five honest jurors summing, not one padding. And
**dedup barely fires**: 200 raw candidates merge to ~200. The dedup rule is
same-file plus 0.6 word overlap, and jurors under different lenses
describe genuinely different claims about the same file. Deduping the
*panel* was the wrong model of the problem — the panel does not repeat
itself.

What actually passes the gate, measured over Run B:

| Filter | candidates | per diff | max | gate | address | **major-file** |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| everything, as run | 200 | 14.3 | 20 | ✗ | 28/34 | **4/4** |
| drop `nit` | 178 | 12.7 | 18 | ✗ | 26/34 | 3/4 |
| file named by ≥2 jurors | 185 | 13.2 | 20 | ✗ | 22/34 | 3/4 |
| **`severity == major` only** | 60 | **4.3** | 9 | **✓** | 22/34 | **2/4** |
| ≥2 jurors **and** major | 55 | 3.9 | 9 | ✓ | 20/34 | 2/4 |

The trade is now sharper than it was in Run A. Filtering to the jurors'
own `major` self-rating passes the gate with room to spare — and **halves
major-file coverage, 4/4 down to 2/4.** The panel finds every file that
carried a paid major finding, and the obvious noise filter throws half of
them away. Severity self-rating is a lossy filter; cross-juror agreement
is worse still (it removes almost no volume, because jurors agree on files
and differ on claims, while costing six addresses).

The conclusion for P2 is therefore narrower than the plan assumed: **the
panel's output cannot be handed to the adjudicator whole, and neither
obvious way to shrink it is safe.** The next configuration to measure is a
lower per-juror cap (5 → 2), which spends the ranking budget *inside* the
juror, where the lens context still exists, rather than in a filter that
sees only a severity label.

## The measurement Step 0 could not make

Recall against the gold set's endorsed labels — the number everyone
actually wants — **is not obtainable from committed diffs**, and this was
discovered while building the harness rather than assumed.

Every endorsed reviewer finding in PILOT-1 was *fixed before the commit
that closed its task*, because the loop only commits after approve.
`git show <commit>` therefore shows the corrected code, not the defect the
reviewer saw. Verified on two rows: `e9tf`/`baf027e` already contains the
fix, and `q004`/`e7in`'s own label records that the task was re-queued with
tightened acceptance, so its closing commit belongs to the second attempt.
Scoring a panel for "recall" against that material would produce a number
that looks like recall and is not one.

The defective state does survive, in the executor session logs
(`.swarm/log/<task>-i<N>-executor.jsonl`), which were checked and do carry
full tool-call arguments. Recall is measurable, but it needs an
executor-log replay that does not exist yet. Recorded in
`experiments/findings.jsonl` as `E12/goldset-recall`, and stated in the
harness docstring so the gap cannot be quietly forgotten.

## Cost, stated in both currencies

Per §6 of the plan, a free contour must never be reported in dollars
alone, or moving work onto a flat subscription makes any per-dollar metric
look infinitely good.

- **Metered**: $0.00. Nothing in this run touched a metered API.
- **Unmetered**: 70 calls, 270 s total wall clock, **548 K input and 21 K
  output tokens** off the subscription. Sequentially 19 s per diff; run in
  parallel, a diff costs the slowest juror, **≈ 5.3 s**.

  For scale, those 548 K input tokens would be ≈ $0.55 on metered Haiku
  and ≈ $2.74 on Opus. The panel is free, but it is not *small*, and that
  volume is what §6 requires be reported next to the zero.

Against the paid baseline of $2.03 and tens of seconds for one Opus round,
a parallel panel is latency-neutral and dollar-free. That is the case for
continuing — but it describes *input to* the adjudicator, and the
adjudicator is still unpriced, because the gate it depends on failed.

## What this decides, and what it does not

Decided:

- The Ollama contour produces mechanically valid, addressed, parseable
  review material at $0 and near-neutral latency. **Zero hallucinated
  addresses in 140 calls across both runs.**
- The panel reaches **every file** that carried a major finding from the
  paid reviewer (4/4) and 82 % of all files it named.
- `think` is per-model and must be configured per-model; the wrapper and
  the metrics now carry that, and ADR-004's prose constraint is enforced
  in code.
- Panel dedup is not the volume lever; the panel does not repeat itself.
- `gpt-oss:120b` stays — quietest, fastest, tied for most unique reach.

Not decided, and explicitly still open:

- Whether the adjudicator can use this material profitably — untestable
  until volume is under the gate.
- Recall against endorsed labels — needs executor-log replay.
- Every metered insertion point (P0 Haiku arm, P1 triage, P3 digest).
- Whether ADR-004's "we don't take such models for helpers" clause should
  be lifted now that levels are supported — an owner decision, raised in
  the ADR amendment, not taken here.

**Next measurement**: same roster at per-juror cap 2, scored the same way.
Passes if volume drops under 10 with major-file coverage still at 4/4.

---

Artifacts (scratch, gitignored — reproduce with the commands below):
`out2/panel-raw.jsonl`, `out2/panel-candidates.jsonl`,
`out2/panel-summary.json`, `out2/helper-metrics.jsonl`.

```
python3 replay.py --stand ~/work/zeus-pilot \
    --models deepseek-v4-flash:0731,glm-5.1,qwen3.5:397b,gpt-oss:120b,kimi-k2.7-code \
    --out out2
python3 analyze.py --out out2
```
