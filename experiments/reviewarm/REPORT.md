# REVIEWARM — Step 0 report: does a free juror panel produce usable material?

**Experiment**: E12 (cheap contour in the review path),
[09-cheap-review-contour.md](../../09-cheap-review-contour.md) §4, Step 0.
**Runs**: four, all $0.00 metered.
**A** — invalid, kept below as evidence (wrong `think` contract).
**B** — corrected contract, original roster, 900-token ceiling.
**C** — modern roster (five different vendors), 12000-token ceiling.
**D** — same modern roster, per-juror cap 2 instead of 5.
Plus a bake-off of **all 19 models** on the account.
**Verdict**: the panel is viable and reaches **4 of 4** of the paid
reviewer's major-finding files. The volume gate can be met — but only by
the *right* lever. Cutting each juror's output (cap, severity filter)
passes the gate by **halving** major coverage; **dropping redundant
jurors** halves volume while keeping 4/4. The gate itself is a guess that
has never been checked against an actual adjudicator, and pricing that
adjudicator is now the measurement that decides P2.

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

## Result 3 — the volume gate, and which lever actually moves it

The plan's §4 gate: fewer than 10 candidates per diff after dedup,
otherwise the adjudicator is drowned and the free contour has moved cost
rather than removed it. Run B fails it at **14.3 per diff** (median 16,
max 20).

Three levers were measured on identical material. The first two behave
the same way, and it is not a good way:

| Configuration | per diff | max | gate | address | **major-files** |
| --- | ---: | ---: | --- | ---: | ---: |
| B — original roster, cap 5 | 14.3 | 20 | ✗ | 28/34 | **4/4** |
| B + keep only self-rated `major` | 4.3 | 9 | ✓ | 22/34 | 2/4 |
| B + only files named by ≥2 jurors | 13.2 | 20 | ✗ | 22/34 | 3/4 |
| C — modern roster, cap 5, 12000 tok | 10.6 | 19 | ✗ | 25/33 | **4/4** |
| **D — modern roster, cap 2** | **5.0** | **8** | **✓** | 21/34 | 2/4 |

Read the last column. **Every lever that squeezes what a juror may say
passes the gate by throwing away half the major-finding files.** Cap 2
was the plan's own recommended next step; it works exactly as designed
and costs exactly what we cannot afford. The panel's volume is not
padding — it is signal, and muzzling the jurors cuts signal.

The lever that does work was found by decomposition rather than by
another run. Scoring each juror by *unique* address contribution — files
the paid reviewer named that **only** this juror reached — shows the
panel is not five points of view:

| Run C juror | unique addresses |
| --- | ---: |
| `kimi-k3` | 2 |
| `minimax-m3` | 2 |
| `deepseek-v4-pro:0813` | **0** |
| `glm-5.2` | **0** |
| `nemotron-3-super` | **0** |

Three of five contribute nothing no one else found. Computing subsets
offline from candidates already collected — **no new calls at all**:

| Subset | per diff | max | address | **major-files** |
| --- | ---: | ---: | ---: | ---: |
| Run B, all five | 14.3 | 20 | 28/34 | 4/4 |
| **Run B, `glm-5.1` + `gpt-oss:120b` + `qwen3.5:397b`** | **7.4** | 11 | **27/34** | **4/4** |
| Run B, `glm-5.1` + `qwen3.5:397b` | 6.6 | 10 | 25/34 | 4/4 |
| Run C, all five | 10.6 | 19 | 25/33 | 4/4 |
| **Run C, `glm-5.2` + `kimi-k3` + `minimax-m3`** | **7.9** | 14 | **25/33** | **4/4** |

Dropping two of five jurors **halves the volume and loses one address**,
holding major coverage at 4/4. On run C's data the three-juror subset
loses *nothing* at all. So the volume problem was never that jurors talk
too much; it was that some jurors are redundant. **Cut viewpoints, not
sentences.**

One caveat kept in plain sight: the `< 10` ceiling is the plan's guess,
written before an adjudicator had ever been run. Nothing measured here
validates it. The three-juror subsets sit at 7.4–7.9 average with a max
of 11–14, so even they clear it only on average. The honest next step is
therefore **not** more squeezing — it is to price the adjudicator: run an
expensive model over the three-juror output and see what it actually
costs. That step is metered and needs owner approval.

## Result 5 — "newer" is not "better", measured

The roster in runs A and B was assembled from whatever was at hand, and
half of it was stale — `glm-5.1` while `glm-5.2` exists, no `minimax`, no
`kimi-k3`. Run C fixed that: five different vendors, all current.

It did not help. Address coverage **fell**, 28/34 (82 %) to 25/33 (76 %),
while wall-clock rose 270 s to 1370 s. `nemotron-3-super` is silent on 13
of 14 diffs under the safety lens; `deepseek-v4-pro:0813` on 5 of 14; and
as the table above shows, three of the five newest models contribute zero
unique addresses. `minimax-m3` earns its place on reach (2 unique) but is
expensive: **70 s per call**, and it truncates on 5 of 14 diffs *even at
12000 tokens* — big diffs need more still. A parallel panel costs its
slowest juror, so one `minimax-m3` sets the panel's latency floor.

Picking models because they are newer is the same mistake as picking them
out of habit. Both skip the measurement.

## What the whole-catalogue bake-off settled

All 19 models on the account, one diff, a `think` ladder
(`False` → `"low"` → `"medium"`) stopping at the first parseable answer:

- **At a 4000-token ceiling, 18 of 19 answer.** The one holdout,
  `minimax-m3`, emits a 17 000-character reasoning trace and is fine at
  12000. **There are no unusable models in the catalogue** — every prior
  "this model is unusable" verdict was a verdict on our ceiling.
- **The ceiling was wrong as reasoning, not just as a number.** `900` came
  from rule §7.1, "we don't pay for thinking", which was written for the
  *metered* contour where each output token is money. On a flat
  subscription output tokens cost only latency. The same applies to
  `HELPER_TIMEOUT = 90`, which cut the slowest juror mid-thought. Both
  raised.
- **Four models ignore the `think` boolean** and reason regardless: both
  `gpt-oss` sizes and both `minimax`. This also refines the Run A
  post-mortem above — a *level* was never strictly required for gpt-oss,
  the *budget* was. At 4000 tokens it answers fine with `think: false`,
  simply paying 5395 characters of trace for it.
- **`gemma4:31b` produced 1 candidate where leaders produced 5** — and it
  is the configured default for every helper in the loop
  (`HELPER_MODEL`). Worth revisiting, separately from E12.
- `nemotron-3-ultra` works but takes **77 s**; not roster material while
  the panel is parallel.
- Structured outputs remain unavailable on the cloud — now stated by the
  vendor too, and re-measured: a schema request came back shaped by the
  prompt, not the schema. ADR-004 stands.

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
