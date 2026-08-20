---
title: "ZeusLogic — Cheap models in the review path: a plan"
type: design
status: draft
version: 0.4
created: 2026-08-20
updated: 2026-08-20
related:
  - 05-agent-swarm.md
  - 06-knowledge-infra-experiments.md
  - experiments/goldset/README.md
summary: >
  How to put cheap models (Ollama Cloud chat models on the flat
  subscription, Claude Haiku 4.5 through the CLI) into the review path so
  that review buys more angles per dollar, not the same angle cheaper.
  Written against measured numbers from PILOT-1 and the frozen reviewer
  gold set; every step is falsifiable before the next is built.
---

# Cheap models in the review path

This is a plan, not a decision. It extends §7 of
[05-agent-swarm.md](05-agent-swarm.md) (the third contour) from service
chores into the review path itself, and it is written to be judged by the
frozen gold set (`experiments/goldset/`) rather than by taste.

The rule §7 already sets stays intact and is the spine of everything
below: **a cheap model never owns the verdict.** What changes is that it
stops being only a commit-message writer and starts producing *material*
the expensive reviewer adjudicates.

---

## 1. What the money is actually spent on

Numbers below come from PILOT-1 (`experiments/goldset/reviews_metrics.jsonl`,
63 review calls) and the gold set's 22 labels.

| Fact | Number | Source |
| --- | --- | --- |
| Review-phase spend, whole pilot | **$77.76** over 63 calls (avg $1.23) | `reviews_metrics.jsonl` |
| `claude-opus-5` / `xhigh`, ordinary round | $2.03 per call (12 calls, $24.34) | same |
| `claude-sonnet-5` / `xhigh`, ordinary round | $1.53 per call (6 calls, $9.21) | same |
| Confirming rounds (all arms) | **10 calls, $12.36** — 16 % of the review bill | same |
| Human-endorsed **major** findings | **2** | `labels.jsonl` |
| Implied price of one endorsed major | **≈ $39** | $77.76 / 2 |
| Reviewer escalations endorsed by the owner | **6 / 6** | `labels.jsonl` |
| Executor boundary disputes granted | **7 / 7** | `labels.jsonl` |
| Loop "task too large" diagnoses correct | **0 / 6** | `labels.jsonl` |
| Share of cache-write tokens attributable to tool-walk depth | **94 %**, governed by effort | `agents.py::_tuning` (PILOT-1 spend decomposition) |
| Micro-diff inversion | review 100 s / $0.58 vs 33 s of execution | SMOKE-1, §7.4 |

Three readings follow, and they set the whole plan:

1. **The reviewer's bill is reading the repository, not reading the
   diff.** 94 % of the cache write comes from how deep the agent walks
   with its tools. Anything that hands the expensive model context it
   would otherwise go fetch is a direct cost lever — bigger than swapping
   the model.
2. **Precision at escalation is already at ceiling** (6/6 endorsed, 2 of
   them major). There is no false-positive problem to solve at the
   escalation boundary. The problem is *price per angle* and *recall we
   cannot see* — the pilot never ran two independent reviewers on the
   same diff on purpose, so unfound defects are invisible.
3. **The two measured defects of the loop are not review defects at
   all**: task boundaries (7/7 disputes granted) and the size-diagnoser
   (0/6). Both are cheap-model-shaped problems, and the second one has a
   measured baseline of zero — the easiest thing in this document to
   beat.

So the goal is not "a cheaper reviewer". It is: **remove reading work
from the expensive model, remove review calls that should not happen,
and buy extra angles at zero marginal cost.**

---

## 2. There are two cheap contours, and they are not interchangeable

Confusing them is the main way this goes wrong, so they get separate
names and separate rules.

| | **Ollama contour** — 19 models on the account; measured leaders `kimi-k2.7-code`, `nemotron-3-nano:30b`, `mistral-large-3:675b`, `glm-5.2`, `gpt-oss:20b`; 1 M context from `minimax-m3` | **Haiku contour** — `claude-haiku-4-5` via `claude -p` |
| --- | --- | --- |
| Billing | flat subscription — **$0 marginal per call** | metered: **$1 / $5** per MTok in/out (Opus 5 is $5 / $25) |
| Output contract | none. `format` with a schema is **silently ignored** — now stated by the vendor too ("Ollama's Cloud currently does not support structured outputs") and re-measured 2026-08-20; prompt-coerce + own validator (ADR-004) | `--json-schema` honored — 25/25 valid verdicts across the whole program |
| Tools | none. Bare chat: the orchestrator must feed every byte of context | full agentic CLI — it walks the repo itself |
| Context | 262 K (gemma4 / qwen3.5 / nemotron), **1 M** (deepseek-v4-flash) | 200 K |
| Depth knob | `think` **per model**: a boolean for models that can disable the trace; four catalogue models ignore it and reason regardless (both `gpt-oss`, both `minimax`), where a level (`low`/`medium`/`high`) shortens the trace. The trace spends `num_predict`, so the ceiling — not the level — is what decides whether an answer arrives | `--effort` **is accepted** by `claude -p` on Haiku 4.5, but bought no depth in the probe (2026-08-20: `xhigh` → 36 thinking tokens / $0.0204, `low` → 43 / $0.0162) |
| Latency | 2–6 s (kimi-k2.7-code, deepseek-v4-flash fastest, probe 2026-08-18) | tens of seconds |
| Cost telemetry | `usage` in tokens; **no USD** — invisible to the budget | real `total_cost_usd` in the envelope |

What follows from the table:

- **Ollama is for unbounded fan-out over material we already hold.**
  Five jurors on the same diff cost nothing and take ~6 s wall in
  parallel. It cannot be trusted to produce a schema-valid envelope and
  it cannot fetch anything itself.
- **Haiku is the only cheap arm that can be a reviewer** in the current
  sense: strict envelope + its own repository walk. It is metered, so it
  shows up honestly in the budget and in `endorsed_majors_per_dollar`.
- **Never let the two share a metric denominator.** Moving work onto an
  unmetered subscription makes any per-dollar metric look infinitely
  good (§6 below fixes this).

---

## 3. Five insertion points, ranked by measured payoff

### P0 — Haiku as a review arm (config, plus one small code fix)

The arm machinery already exists: `review_model_pool`, `review_effort_pool`,
`confirm_model_pool`, `confirm_effort_pool` are drawn per call, and every
draw is written into `metrics.jsonl` (`agents.py::_draw`, `review`). The
paired design means every diff is already reviewed twice, so adding
`claude-haiku-4-5` to the pool yields **opus↔haiku pairs on the same diff
for free**, and `harvest.py` attributes them without changes.

*Required fix, small but real*: `_draw` picks model and effort as two
independent factors, so a mixed pool emits the **cross product** of the
two pools — every model paired with every effort, whether or not that
pair is a configuration anyone meant to measure.

The probe (2026-08-20) settled what the flag does: `claude -p --model
claude-haiku-4-5 --effort xhigh` runs without error ($0.0204, 36 thinking
tokens), and so does `--effort low` ($0.0162, 43 thinking tokens). So the
flag is neither rejected nor, on this probe, load-bearing — a plan
written against "Haiku rejects effort" would have been wrong. The tuple
draw is still the right fix, for the reason that survives the probe:
**an arm is a pair, not a product.** A journal that records `model` and
`effort` from two independent draws cannot name the configuration that
produced a verdict, and the pair `(haiku, xhigh)` — which we now know is
runnable — is a different arm from `(opus, xhigh)` and must be drawn, and
attributed, as one thing.

Implemented: `review_arm_pool` / `confirm_arm_pool` are drawn as one
choice (`agents.py::_draw_arm`), e.g.
`review_arm_pool = [["claude-opus-5","xhigh"], ["claude-haiku-4-5",""]]`,
outranking the scalar pools, which stay as the fallback. Arm shapes are
read as data (string, list, or `{model, effort}`); a malformed entry
warns in `swarm config` and falls back to the old path rather than
failing the run.

Expected effect: a Haiku round at comparable walk depth should land near
$0.3–0.4 against $2.03. If the Haiku arm re-finds even one of the two
endorsed majors, endorsed-majors-per-dollar improves ~5×; if it finds
none, we learned that on a pilot we were running anyway.

### P1 — Triage: stop paying $2 to review a typo

This is §7.4's own open question ("лёгкий режим ревью для тривиальных
диффов"), now with a baseline: SMOKE-1 showed review costing three times
execution on micro-diffs.

Deterministic first, per §7.1 (*if it can be decided without an LLM, no
LLM*): docs-only, test-only-with-gate-green, single file under N changed
lines, nothing under `protected_paths`, no new dependency. Those route to
the Haiku arm or skip with an explicit journal mark. A cheap classifier
runs **only** in the ambiguous band, and it holds one asymmetric right:
it may *downgrade* the arm, never approve and never skip on its own.
Fail-open = full review.

### P2 — A free juror panel with one expensive adjudicator

This is the "multiply effort" step.

K Ollama models read the same diff, the gate tail and the task statement
in parallel, each under a distinct lens (the `confirm_lens` machinery
from E10 already exists and already threads a lens string into the review
prompt). They return *candidates*, never verdicts. The orchestrator
validates them the way §7.3 demands — fence-stripping, JSON parse, field
ranges — dedups mechanically first (`helpers.dedup_findings` already does
the exact-match pass before calling any model), and drops everything that
does not carry a `file:line` anchor it can resolve.

Two variants, and the second is where the money is:

- **B1 — leads into the existing review.** Panel candidates are appended
  to the reviewer prompt as clearly-fenced untrusted data. Cost: adds
  input tokens to the expensive call; risk: anchoring.
- **B2 — panel replaces the confirming round.** The confirming round
  exists to give a second angle on the same diff (measured on e4kb: three
  findings per effort level, only one overlapping). Five free jurors are
  a wider second angle than one paid round. The expensive confirming call
  then fires **only** when the panel raises an anchored candidate the
  first reviewer did not have. Direct target: the $12.36 / 10 calls the
  pilot spent on confirming rounds.

### P3 — A context digest from the 1 M-context cheap model

Aimed straight at the 94 %. `deepseek-v4-flash:0731` (1 M context, 2–5 s)
reads the touched files plus their callers and returns a bounded brief:
call graph around the change, invariants the code relies on, prior art in
the repo. It goes into the reviewer prompt so the expensive model does
not have to walk for it.

Hard constraint: **the brief carries quoted evidence with `file:line`, not
conclusions.** A wrong conclusion from a cheap model that the reviewer
trusts is worse than no brief at all; a wrong quote is checkable, and
ADR-005 verification requests give the reviewer a way to check it.
Measure at fixed effort: cache-write tokens and cost down, endorsed
findings held constant against the gold set.

### P4 — Two repairs the gold set explicitly asks for

- **Boundary checker (free).** 7/7 executor disputes were granted — the
  recurring defect of the pilot was the *task statement*, not the code.
  A cheap pre-review check of "task statement ↔ plan ↔ diff" costs
  nothing and fires before the expensive reviewer burns a call on a
  boundary argument.
- **Replace the size-diagnoser.** 0/6 correct; the real causes were arm
  disagreement, test protection, timeouts and provider quota — all of
  them visible in the run journal. A cheap classifier over the journal
  is strictly better than a measured zero, and its errors are cheap.
  Independent of everything else here, and the highest-confidence item on
  the list.
- **Refuters (optional, free).** K cheap models must *locate the
  evidence* for a finding and vote. Note the ceiling honestly: escalation
  precision is already 6/6, so this cannot improve escalations. Its value
  is on the non-escalated bulk that the executor silently reworks — which
  the gold set only labels implicitly ("executor complied").

---

## 4. Sequencing — each step falsifiable before the next is built

**Step 0 — offline replay. No expensive calls at all. — RUN TWICE
2026-08-20, gate NOT met.** `experiments/reviewarm/replay.py` +
`analyze.py`, full report in
[experiments/reviewarm/REPORT.md](experiments/reviewarm/REPORT.md).
Five models under five distinct lenses over the 14 PILOT-1 diffs that
carry a closing commit: **70 calls, $0.00 metered, 270 s wall, 548 K in /
21 K out tokens**.

Run A was invalid and is kept in the report as evidence: `ollama_chat`
hardcoded `think: false`, which **gpt-oss ignores** — it reasons
regardless — so that juror spent its whole `num_predict` on the trace and
returned empty answers 12 times in 14. (The catalogue bake-off later
refined this: a level was never strictly *required*, the budget was. At
`num_predict = 4000` gpt-oss answers with `think: false` too, simply
paying 5395 characters of trace for it.) Run A read that as "the model is unusable".
`think` turned out to be a property of the model, not a constant of the
call, and the inverse holds too: a level would destroy the other four.
Fixed in gated code (per-model `think`, `thinking_chars` in metrics) and
recorded as an amendment to ADR-004, whose prose had contained the rule
all along. Numbers below are Run B.

The gate as written was *"recovers ≥1 of the 2 majors with < 10
candidates per diff"*. Neither half survived contact intact, and for
different reasons:

- **Volume: failed.** 14.3 candidates per diff after dedup (median 16,
  max 20), over on 11 of 14 diffs — slightly *worse* than Run A, because a
  juror that had been silently failing now works. Dedup is not the lever:
  200 raw candidates merge to ~200, because jurors under different lenses
  make genuinely *different* claims about the same file. The panel does
  not repeat itself. And the obvious noise filter is worse than it looked:
  keeping only self-rated `major` passes the gate at 4.3/diff while
  **halving** coverage of the paid reviewer's major-finding files, 4/4 down
  to 2/4. Cross-juror agreement is worse still — it barely reduces volume
  (jurors agree on files, differ on claims) and costs six addresses.
- **Recall: not measurable this way at all.** Every endorsed finding of
  PILOT-1 was fixed *before* the commit closing its task, so `git show`
  shows the correction, not the defect. What Step 0 can report is
  *address* agreement — the panel named 28 of the 34 files the paid
  reviewer named (82 %), and **all 4** of its major-finding files — and
  that is not recall. Recall needs a replay over the executor session logs, which
  do carry full tool-call arguments; that harness does not exist yet.

One thing the runs settled cheaply and permanently: **zero off-diff
candidates in 140 calls and 200 candidates across both runs** — the
address validator the plan worried about rejects nothing, so contract-free
jurors are not confused about where they are looking. The roster keeps all
five: correctly configured, `gpt-oss:120b` is the *quietest* juror (0.8
candidates per diff, silent on 9 of 14 diffs) and the *fastest* (2.1 s),
and it still ties for the most unique address reach — removing it now
costs coverage, the exact reverse of Run A.

*That revised gate was then measured, and it failed on its own terms.*
Per-juror cap 2 (run D) passes the volume gate for the first time — 5.0
candidates per diff, max 8 — and **halves major-file coverage, 4/4 to
2/4**, exactly like the severity filter it was meant to improve on. Every
lever that squeezes what a juror may *say* buys the gate with the material
the panel exists to find.

**The lever that works is the roster, not the muzzle.** Scored by *unique*
address contribution, three of the five modern jurors reach nothing no one
else reached. Subsets computed offline from candidates already collected,
with no new calls: `glm-5.1 + gpt-oss:120b + qwen3.5:397b` gives **7.4**
candidates per diff against 14.3 for all five, loses exactly one address
(27/34), and **holds major-file coverage at 4/4**. On the modern roster,
`glm-5.2 + kimi-k3 + minimax-m3` gives 7.9 against 10.6 and loses nothing
at all. The panel's volume was never padding — it was signal plus
redundancy, and only the redundancy is safe to cut.

*Next step for P2, and it is not another squeeze*: the `< 10` ceiling is
this plan's own guess, written before any adjudicator had been run, and
nothing measured validates it. What decides P2 is **the price of the
adjudicator** — run an expensive model over the three-juror output and
measure what it costs. That step is metered, so it awaits owner approval.
Until then P2 stays unbuilt and P0/P1/P3 are unaffected.

**Step 1 — P0**: tuple-draw fix + Haiku in the pool. Runs inside the next
pilot queue with no protocol change. *Tuple draw implemented and gated
(997 tests + 378 subtests green); Haiku in the pool still awaits owner
approval, since it is metered.*

**Step 2 — P1**: deterministic band, measured against the pilot's own
diff-size distribution before any classifier is written.

**Step 3 — P2/B2** behind `[experiments] panel`, fail-open, metrics into
the stand's `.swarm/helper-metrics.jsonl` (artifact hygiene, AUDIT-3).

**Step 4 — P3 digest** — one factor per run, per the program rule.

P4's diagnoser replacement can land at any time; it does not touch the
review path.

---

## 5. Contracts and safety — carried over, not renegotiated

- Cheap models never own the verdict, never write or fix code, never
  decompose `tasks.json` (§7.2). Panel output is *candidates*; the
  expensive arm adjudicates.
- Panel and digest text entering an expensive prompt is **untrusted
  input**. Fence it, label it as data, never as instruction; keep the
  closed enum + `--json-schema` on the verdict (§6.4) so injected text
  cannot widen the verdict format or invent actions.
- **Fail-open at the API of each helper**, not merely around the HTTP
  call (the HELP-1 lesson): no panel, no digest, no triage → the loop
  behaves exactly as it does today, with a warning in the journal.
- Secret scrub before every external call (`helpers.scrub`); Ollama Cloud
  sends no rate-limit headers, so the circuit breaker stays.
- Determinism per ADR-004: native `/api/chat`, `think` **chosen per
  model** (four models in the catalogue ignore the boolean and reason
  regardless — both `gpt-oss`, both `minimax`),
  `temperature 0`, `top_k 1`, `repeat_penalty 1.0`, explicit
  `num_predict`, fixed seed; `done_reason == "length"` → discard the
  answer, a truncated one is worse than none.
- **`num_predict` is not a place to economise on this contour.** The old
  ceiling of 900 was inherited from the metered contour, where every
  output token is money; on a flat subscription output tokens cost only
  latency. Measured 2026-08-20 across all 19 catalogue models: at 4000,
  **18 of 19** produce a valid answer; the one holdout (`minimax-m3`,
  17 K characters of reasoning trace) is fine at 12000. **There are no
  unusable models in the catalogue** — every prior "this model is
  unusable" verdict was a verdict on our ceiling. Default raised to 4000;
  `HELPER_TIMEOUT` likewise (90 s cut the slowest juror mid-thought).
- Every draw and every helper call lands in metrics. An arm that is not
  in the journal turns the run into unreproducible noise.

---

## 6. The metric must not be allowed to lie

`endorsed_majors_per_dollar` goes to infinity the moment work moves onto
an unmetered subscription. The gold set's metric therefore reports a
**pair**, and the pair is the acceptance criterion:

1. **metered $ per endorsed major** — Anthropic spend only, comparable to
   the pilot's $39 baseline;
2. **wall-clock seconds and cheap-token volume per endorsed major** — so
   the free contour still shows its true cost (latency, subscription
   pressure, context bloat).

This is the same mixed-billing caveat §10 already states as a risk
("смешанный биллинг: подписка + API"); here it is a gate, not a footnote.

The gold set's own rules stand: rows are frozen, and **a configuration
that contradicts an endorsed label is a finding to investigate, never a
reason to edit the label.**

---

## 7. Stop conditions

- ~~Panel recall of endorsed majors is 0 in Step 0 replay → drop P2.~~ **Void: this condition cannot be evaluated from committed diffs** (§4, Step 0). It is replaced by the volume gate — panel output over 10 candidates per diff that cannot be brought under it without losing major-file coverage → drop P2 — and reinstated in its original form only once an executor-log replay exists.
- The digest raises reviewer cost or lowers endorsed findings → drop P3;
  the reviewer's own walk was buying something we mispriced.
- Any cheap arm produces an `approve` that the gold set contradicts →
  hard stop on ever moving the verdict owner down a tier. Cheap stays
  advisory, permanently.
- Triage skips a diff that later needs a fix → the deterministic band was
  wrong; narrow it, do not patch it with a model.

---

## 8. Rough projection (a projection, not a measurement)

Against the pilot's $77.76 review bill: triage removing the trivial tail,
B2 removing most of the $12.36 confirming spend, and the Haiku arm taking
half the ordinary rounds would put the same queue near $30–40 — while
each diff gets five additional lenses instead of one paid second opinion.
The honest version of that sentence is that only Step 0 and Step 1 can
confirm it, and both are cheap.

---

## Журнал изменений

### v0.4 (2026-08-20)

- Замечание владельца о составе и о потолке токенов проверено и оказалось
  верным дважды. Бейк-офф всего каталога (19 моделей): при потолке 4000
  годны 18, оставшаяся `minimax-m3` годна при 12000 — **непригодных
  моделей в каталоге нет**, а прежние вердикты «модель непригодна» были
  вердиктами о нашем потолке. Потолок 900 пришёл из МЕТРИРУЕМОГО контура
  (§7.1 «не платим за размышления»), где выходной токен — деньги; на
  плоской подписке он стоит только задержки. Правило добавлено в §5,
  умолчание поднято до 4000, `HELPER_TIMEOUT` — до 300.
- Современный состав замерен (прогон C) и оказался ХУЖЕ прежнего по
  адресному покрытию: 25/33 против 28/34, при вчетверо большем времени.
  Три модели из пяти новейших не дали ни одного уникального адреса.
  «Новее» — не замер.
- **Найден рычаг ворот объёма, и это не тот рычаг, который план
  рекомендовал.** Потолок находок 2 (прогон D) ворота проходит впервые
  (5.0 на дифф), но роняет покрытие major-файлов 4/4 → 2/4 — ровно как
  фильтр по severity. Сокращение СОСТАВА до трёх присяжных даёт 7.4
  против 14.3, теряет один адрес и удерживает 4/4. Объём панели —
  сигнал плюс избыточность; резать безопасно только избыточность.
- Названо честно: ворота «<10» — догадка плана, не замер. Решающий шаг
  P2 — не сжатие, а ЦЕНА АДЪЮДИКАТОРА; шаг метрируемый и ждёт решения
  владельца.

### v0.3 (2026-08-20)

- Нулевой шаг пересчитан на исправленном контракте Ollama. Вскрыто, что
  `think` — свойство МОДЕЛИ, а не константа вызова: обёртка зашивала
  `think: false`, который gpt-oss игнорирует, и присяжный сжигал весь
  `num_predict` на размышления. Прогон A признан недействительным в части
  состава; его вывод «gpt-oss непригоден» отозван — исправленный, это
  самый быстрый и самый немногословный присяжный, и удаление его теперь
  стоит покрытия. Обновлены §2 (строки про глубину и про structured
  outputs) и §4.
- Числа §4 заменены на прогон B: адресное совпадение 28/34 (82%), файлы с
  major дорогого ревьюера покрыты полностью (4/4), 270 с, 21K выходных
  токенов. Ворота объёма по-прежнему НЕ пройдены — 14.3 на дифф, и размен
  стал резче: фильтр по самооценке `major` режет покрытие major-файлов
  вдвое.

### v0.2 (2026-08-20)

- Нулевой шаг проведён, §4 переписан по факту замера: ворота объёма **не
  пройдены** (13.4 кандидата на дифф против потолка 10), а recall по
  одобренным ярлыкам из коммитов оказался **принципиально неизмерим** —
  каждая одобренная находка исправлена до закрывающего коммита.
  Зафиксированы два дешёвых и окончательных вывода: ноль кандидатов мимо
  диффа за 70 вызовов и вывод `gpt-oss:120b` из состава. Названы
  пересмотренные ворота для P2 (тот же состав, потолок находок у
  присяжного 5 → 2).
- Исправлено ложное утверждение §2/§3: `--effort` на `claude-haiku-4-5`
  через `claude -p` **принимается** (проба: xhigh $0.0204, low $0.0162).
  Обоснование парной руки замера переписано на то, что пробу пережило:
  рука — это пара, а не произведение двух пулов.

### v0.1 (2026-08-20)

- Первая редакция. Написана на числах PILOT-1 и золотого набора; вводит
  разделение двух дешёвых контуров (Ollama-подписка и Haiku через CLI),
  пять точек внедрения (P0–P4), порядок с офлайн-реплеем как нулевым
  шагом и парную метрику, не позволяющую бесплатному контуру врать.
  Статус — предложение, решения не приняты.
