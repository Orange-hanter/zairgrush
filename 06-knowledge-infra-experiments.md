---
title: "ZeusLogic — Эксперименты: знаниевая инфраструктура и индексация кода"
type: design
status: draft
version: 0.14
created: 2026-08-06
updated: 2026-08-22
related:
  - 05-agent-swarm.md
  - 05-agent-swarm-audit.md
summary: >
  Программа экспериментов для петли агентов: по каждой подсистеме
  (журнал, задачи, контекст для агентов, детект gaming'а, дрейф доков,
  поиск, доступ к навигации, модель исполнителя) реализуются 2–3
  дублирующих варианта и сравниваются на общей мини-практике. Все
  замечания и решения фиксируются: findings.jsonl + ADR. Проигравший
  вариант удаляется — дубли временные по построению.
---

# Эксперименты: знаниевая инфраструктура и индексация кода

Мы на экспериментальном этапе: вместо того чтобы выбирать по одному решению
на подсистему «на бумаге», реализуем **дублирующие варианты** и сравниваем
их на общей мини-практике. Документ фиксирует: список экспериментов,
методологию сравнения, протокол фиксации замечаний и решений, и правила,
не дающие временным дублям стать постоянным зоопарком.

Связь с 05-agent-swarm.md: петля (v0.6) — носитель экспериментов; всё
здесь — надстройки над ней, не изменения её контрактов.

---

## 1. Правила программы (анти-зоопарк)

1. **Дубль живёт за флагом.** Каждый вариант включается в
   `swarm.toml [experiments]` (`journal = "md" | "jsonl"` и т.п.).
   Базовый код петли не ветвится — варьируется только подсистема.
2. **Один фактор на прогон.** В одном прогоне бенча меняется ровно один
   экспериментальный флаг, иначе метрики неинтерпретируемы. Параллельно
   открыто не больше двух экспериментов.
3. **Эксперимент ограничен**: ≤ 2 недель или ≤ 3 прогонов бенча — что
   раньше. По истечении — решение обязательно (можно «продлить», но это
   тоже решение, записанное в ADR).
4. **Проигравший удаляется.** Код проигравшего варианта выпиливается в том
   же коммите, где ADR фиксирует решение. Дубль без даты казни — это не
   эксперимент, а технический долг.
5. **Ничья = выбираем более дешёвый в поддержке** (меньше кода, меньше
   зависимостей). Бремя доказательства — на более сложном варианте.
6. **Документация — в приоритете** (§3.3 петли). Прогон/эксперимент не
   закрыт, пока не обновлены: findings.jsonl, отчёт прогона, статусы в
   этом документе и затронутые разделы 05-дока. Развёрнутые
   человекочитаемые тексты (журнал программы, ADR-прозу) пишет отдельный
   агент-документатор — подробно, связно, с инженерными обоснованиями;
   его вход — структурные данные (jsonl/metrics/raw), не пересказы.

## 2. Мини-практика (общий бенч)

Все эксперименты меряются на одном наборе — иначе сравнение бессмысленно:

- **BENCH-задачи**: 6–10 маленьких реальных задач по `zeus-library`
  (те же, что для приёмки §9 петли), зафиксированы в
  `experiments/bench/tasks.json` — формат схемы задач §4.3 петли.
  Набор заморожен на всю программу; расширение = новая версия бенча.
- **Канарейки**: 5–10 диффов с посеянными багами (переиспользуются из
  §9 шаг 4 петли) — для экспериментов, влияющих на качество ревью.
- **Стартовое состояние**: фиксированный коммит репо; каждый прогон —
  с него (`git worktree` на прогон).

Метрики снимаются штатно из `metrics.jsonl` петли; каждый прогон помечается
полями `experiment` и `variant`. Общие для всех экспериментов:

| Метрика | Источник |
| --- | --- |
| tokens_in/out по ролям, USD на задачу | metrics.jsonl |
| wall-time на задачу, итераций до сходимости | metrics.jsonl |
| catch rate / FPR на канарейках (для ревью-экспериментов) | прогон канареек |
| стоимость поддержки: строки кода варианта, внешние зависимости | подсчёт в ADR |

## 3. Протокол фиксации замечаний и решений

Требование программы: **ни одно наблюдение не остаётся в чате/голове.**

1. **Замечания** — `experiments/findings.jsonl`, одна строка на наблюдение:

   ```json
   {"ts": "...", "exp": "E3", "variant": "repomap",
    "task": "a1b2", "kind": "observation | defect | surprise",
    "note": "ревьюер перестал грепать репо, но карта съела 900 токенов",
    "metric_ref": "run-014"}
   ```

   Пишутся по ходу: вручную командой `swarm exp note ...` или оркестратором
   (автозамечания: превышения бюджета, ретраи, невалидные ответы).
2. **Решения** — ADR-файлы `experiments/adr/NNN-<slug>.md` (формат:
   Контекст → Варианты → Решение → Последствия). ADR пишется при закрытии
   эксперимента и ссылается на findings и номера прогонов. ADR — рендер
   человеку; машинная сводка решений — `experiments/decisions.jsonl`.
3. **Сводка** — `swarm exp status`: открытые эксперименты, сроки, число
   findings, прогоны. Это та самая «автоматизация вместо кучи md»:
   md-файлов здесь два вида (этот док и ADR), всё остальное — jsonl.

## 4. Эксперименты

Формат: варианты → гипотеза → метрики решения → стоимость.

### E1. Журнал петли: md-первичный vs jsonl-первичный

- **A (текущее, v0.6)**: `.swarm/log/NNN-*.md` пишется как первичный.
- **B**: первичен `run.jsonl` (типизированные события: handoff, verdict,
  gate_result, commit, escalation); md — рендер `swarm report <task>`.
- **Гипотеза**: B почти бесплатен и сразу даёт фикстуры для replay-тестов
  FSM и вход для `swarm digest`; A выигрывает только сиюминутной
  простотой.
- **Решение по**: усилие на пост-анализ одного прогона (сколько минут
  собрать «что пробовали» по задаче), пригодность как фикстур тестов.
- **Стоимость**: B ≈ 100 строк + рендерер. Прогноз: B, эксперимент
  короткий — держим A как контроль один прогон.

### E2. Создание задач: только планировщик vs task-CLI

- **A (текущее)**: задачи создаёт планировщик (план-дифф) и оркестратор
  (ideas); человек редактирует tasks.json руками.
- **B**: `swarm task add/split/close` — один валидатор схемы для всех
  писателей, шаблоны по `type`, запрет ручного редактирования файла.
- **Гипотеза**: B убирает класс ошибок «невалидная задача от человека» и
  делает ideas/канарейки оформляемыми одной командой.
- **Решение по**: число невалидных задач/план-диффов за прогон, время
  оформления новой задачи человеком.
- **Стоимость**: B ≈ 150 строк. Вариант A остаётся возможным всегда —
  эксперимент фактически про то, обязателен ли CLI-путь.

### E3. Контекст для агентов (главный эксперимент)

Три варианта того, что оркестратор вкладывает агенту в handoff:

- **A (baseline, v0.6)**: ничего — агент сам исследует репо read-only
  инструментами.
- **B (repo map)**: символьная карта репо (tree-sitter/ctags +
  PageRank-ранжирование, приём Aider; для Rust + `cargo modules
  structure`), N строк в каждый handoff.
- **C (SCIP blast radius)**: индекс `rust-analyzer scip .` (обновление
  после каждого approve-коммита); ревьюеру дополнительно к diff'у —
  вызывающие/вызываемые изменённых функций; планировщику — данные для
  `paths`/`deps`.
- **Гипотеза**: главный скрытый налог архитектуры stateless-агентов —
  повторное исследование репо каждой свежей сессией; B срезает его
  дёшево, C дополнительно улучшает качество ревью за пределами diff'а.
- **Решение по**: tokens_in на роль (исследовательские tool-call'ы —
  видно в событиях stream-json), итераций до сходимости, catch rate на
  канарейках (для C — особенно баги «сломал вызывающего»), wall-time.
- **Стоимость**: B — умеренная (одна зависимость tree-sitter/ctags);
  C — заметная (индекс, инкрементальное обновление). Порядок: сначала
  A vs B; C подключается третьим прогоном, только если B выиграл.

### E4. Детект ослабления тестов (§5.5 петли): regex vs ast-grep

- **A**: regex-паттерны по diff'у (`#[ignore]`, `try/except: pass`,
  удаление строк с assert).
- **B**: правила ast-grep по AST (структурные паттерны, меньше ложных
  срабатываний на строки в комментариях/литералах).
- **Гипотеза**: B точнее и переносимее между языками; A — ноль
  зависимостей.
- **Решение по**: FP/FN на синтетическом наборе диффов-ослаблений
  (сделать 15–20 штук: удалённый assert, скип, mock реального кода,
  правка conftest — по мотивам ImpossibleBench).
- **Стоимость**: часы на оба. Самый дешёвый эксперимент — делать первым.
- **Статус: done 2026-08-22** (findings E4): замер на 20 кейсах без
  LLM-вызовов — A: recall 9/10, FPR 1/10; B: recall 9/10, FPR 0/10.
  Ложное срабатывание A — удалённый строковый литерал с `assertEqual(`,
  ровно предсказанный класс. Общая слепая зона обоих: семантическое
  ослабление c5 (`assertEqual` → `assertTrue(>0)`) — остаётся ревьюеру.
  Рекомендация: B как детектор §5.5 (ast-grep-py — одно колесо без
  транзитивных зависимостей), A — запасной вариант с нулём зависимостей;
  решение о зависимости — за владельцем.

### E5. Дрейф docs↔code: LLM-хелпер vs docmap + LLM

- **A (текущее, §7.2 петли)**: хелпер сверяет «изменённые файлы ↔
  упоминания в docs/» эвристически.
- **B**: `docmap.toml` (карта «файл/модуль кода → секции доков»);
  расхождение находится механически, LLM только формулирует правку.
- **Гипотеза**: B устраняет пропуски хелпера и половину его токенов;
  цена — поддержка карты (которую тоже можно проверять: секция без
  владельца-кода = warning).
- **Решение по**: пропущенные рассинхроны (ручная проверка после
  прогона), токены хелпера.

### E6. Поиск по журналу/докам: ripgrep vs эмбеддинги

- **A**: BM25/ripgrep по jsonl-журналу и docs/.
- **B**: локальные эмбеддинги (Ollama) + векторный индекс.
- **Гипотеза**: на объёмах одного проекта A достаточно; B — оверкилл.
- **Статус: ожил в составе E9** (2026-08-18): потребитель поиска появился —
  retrieve уроков при сборке промпта. Этап 1 E9 отвечает вариантом A
  (FTS в PG + локальный скан по редким токенам как фолбэк); телеметрия
  `queries.jsonl` фиксирует backend каждого попадания, и сравнение с
  этапом 2 (эмбеддинги как сеть доп. охвата) закроет E6 замером,
  а не прогнозом. Прогноз прежний: A.

### E7. Доставка навигации агенту: push в промпт vs MCP-сервер

- **A**: оркестратор вкладывает карту/blast radius в handoff (push).
- **B**: read-only MCP-сервер (`repo_map`, `defs/refs/callers`,
  `doc_search`) — агент запрашивает сам (pull); это расширение
  MCP-сервера git из этапа 2 петли.
- **Гипотеза**: push дешевле и детерминированнее (агент не решает,
  смотреть ли карту); pull гибче для ревьюера, который знает, что искать.
- **Решение по**: токены, число «слепых» tool-call'ов, качество ревью.
- **Зависимость**: имеет смысл только после E3 (нужен сам индекс).

### E8. Модель исполнителя: Kimi K3 vs kimi-k2.7-code

- **A**: K3 на всех BENCH-задачах. **B**: k2.7-code на тех же.
  (Опционально **C**: роутинг — K3 на задачи с `deps`/архитектурные,
  k2.7 на механические, по полю `type`/эвристике планировщика.)
- **Гипотеза**: K3 сходится за меньше итераций; на механических задачах
  разница исчезает, а цена — в 3–4 раза выше ($3/$15 vs $0.95/$4).
- **Решение по**: итераций до сходимости, доля approve с первой итерации,
  USD на задачу (полная, включая ревью повторных итераций — дорогая
  итерация ревьюера может съесть экономию дешёвого исполнителя).
- Это единственный эксперимент над самой петлёй, а не над надстройками —
  но он использует тот же бенч и протокол, грех не снять.

### E9. Память между прогонами: уроки, дайджест, инъекция

- **A (текущее)**: между прогонами не переносится ничего; журнал и
  findings никто не читает обратно в промпты.
- **B**: двухъярусная память (эпизоды `.swarm/memory/lessons.jsonl` →
  детерминированный `LESSONS.md`), файлы — источник истины, PG(+pgvector,
  этап 2) — производный индекс; уроки пишутся механически из терминальных
  исходов задач (done → useful, blocked → dead_end с диагнозом, решения
  человека → corrected); инъекция ограниченным блоком (~500 токенов) за
  флагом `[experiments] memory`, сперва только исполнителю.
- **Гипотеза**: уроки прошлых прогонов сокращают повторные тупики и
  раунды (на PILOT-1 k3ad трижды сделал одну и ту же отвергаемую работу).
- **Решение по**: USD/задача, раунды до сходимости, счётные инциденты
  повторённых тупиков; телеметрия `queries.jsonl` — пользуются ли памятью.
- **Протокол замера**: посевной прогон копит память при выключенной
  инъекции; снапшот `.swarm/memory/` восстанавливается перед каждым
  плечом; плечи `off` vs `executor` — один фактор. Ревьюер и планировщик
  подключаются только после решения по исполнителю.
- **Ключевые механизмы** (перенесены из рабочей памяти graphify/Orakul):
  grounding как пропуск (useful без живого якоря отклоняется), распад по
  ре-валидации якорей (не TTL), уверенность из повторяемости,
  dead_end — первоклассный исход.
- **Статус: этап 1 реализован** (2026-08-18, за флагом, по умолчанию
  выключен; findings E9).
- **Auto-indexing added** (2026-08-20, findings E9/auto-index): index
  maintenance was split from the injection factor. `memory_index =
  "auto"` (per-stand opt-in, default `manual`) makes the loop `sync`
  rows AND vectors to PG on every terminal outcome and in after-run
  reflection; the injection flag alone previously left vectors to a
  manual `reindex` nobody ran — an entire pilot produced zero embedder
  calls (measured on the OpenRouter dashboard). The A/B protocol is
  unaffected: injection stays the single measured factor, index
  freshness is now identical in both arms.

- **Planner arm switched on** (2026-08-20, stand PILOT-1 commit
  a8808ed): `[experiments] memory = "planner"` — one role, one factor.
  It goes first because its ground truth is already paid for: all seven
  contested task boundaries of the pilot were granted by the owner, and
  every decision sits in `.swarm/memory` as a `corrected` lesson with
  anchors. Memory now also reaches `replan` — the path a boundary
  dispute actually takes; before this it fed `plan` only, i.e. never
  the call that answers a dispute. Executor and reviewer stay dry: their
  arms are measured separately. Flag values are validated as role names
  (`off | executor | reviewer | planner | all`) — a typo used to
  disable the subsystem in silence, the same failure mode as the
  zero-embedder-calls measurement.
- **Planner arm measured** (2026-08-22, findings E9/planner-arm):
  one-factor A/B on the three blocked idea-tasks (replan --dry-run,
  memory=planner vs off, ~$13.5). With memory ON the v9lb plan folded
  in all three 2026-08-16 owner decisions as named tasks — including
  the s2ky escaping lesson born in another task's dispute; with memory
  OFF that satellite vanished from every replan. Both arms agreed on
  the verdict; quality call on the new plan-diffs is the owner's.
  Side finding: raw plan-diffs are overwritten per call
  (`.swarm/raw/replan-a1.json`) — evidence survived only in console
  capture.

### E10. Contract-first skeleton + model routing (in English per owner's rule)

- **A (current)**: every task is implemented whole by the expensive
  executor (K3 via kimi CLI).
- **B**: a strong model produces a *skeleton* — signatures, contract
  docstrings, protected contract tests, `NotImplementedError` bodies —
  and cheap chat models fill the bodies as single-file, mechanically
  bounded tasks. Fill transport: non-agentic Ollama Cloud chat
  (full-file-in-one-fence contract, probe-validated 2026-08-18: five
  models pass, no elision at ~100 lines, feedback rounds converge).
- **Hypothesis**: this is the unmeasured variant C of E8 made structural
  — the skeleton *constructs* mechanical tasks, so the cheap-model
  penalty measured on whole tasks (+21 % wall, +29 % review cost)
  should vanish, while executor cost drops 3–4×.
- **Mechanics** (behind `[experiments] skeleton`): per-task
  `executor_model` (plan-diff assigns it); fill-mode executor (chat
  completion, orchestrator writes the file and runs the gate);
  signature guard — pyindex signature snapshot pinned by the skeleton
  task, compared on every fill round; fill disputes route to replan,
  not to the human.
- **Decision by**: USD per goal (full, incl. review), rounds to
  convergence, post-review defects, share of contract disputes.
- **Provider-cache note** (knowledge, no code): Kimi auto-caches
  prefixes — stable fill prompts ride it for free; rate-based executor
  routing beyond that only via an E8-class bench.
- **Blocker on arm A removed 2026-08-22**: it no longer waits on the
  kimi.com quota. The executor engine is a config choice now (05-doc
  §3.2.0), so arm A can run on `executor_engine = "claude"` — at the
  price of naming the engine as a factor of the comparison instead of
  hiding it. Both arms must then run on the same engine.
- **Status: implemented behind the flag; live smoke PASSED 2026-08-18** —
  full loop on a scratch stand: ollama fill → green gate (contract tests,
  zero skips) → sonnet/low approve → security-lens confirming round →
  approve → orchestrator commit; $0.27 total review cost, confirming
  round hit the prompt cache (cache_read 76 948). Live-run lesson folded
  back into the planner rules: contract tests must SKIP on
  NotImplementedError, or the fill baseline is red. The comparative
  bench (arm A vs arm B) still pending — needs the kimi.com quota for
  arm A.

### E13. Executor engine: Kimi K3 vs Claude Sonnet

- **A (current default)**: the executor is `kimi -p` with model K3
  (ADR-002, measured on E8: 6/6 tasks from the first iteration, 193 s of
  implementation, $2.31 of review cost).
- **B**: the executor is `claude -p` with an explicit permission
  allowlist and a git deny list (05-doc §3.2.0), model `sonnet`.
- **Hypothesis**: on brownfield tasks the difference shows up as rounds
  to convergence, not as first-round success — the very thing E8's
  greenfield bench could not measure. ADR-002 says so itself and demands
  a revisit after BENCH-2.
- **Carried as a caveat by design, not discovered in the results**: on
  arm B the diff's author and its judge come from one model family, and
  §3.2 leant on their independence. The loop warns when the two models
  coincide; the experiment must either separate them (different tier,
  effort or `confirm_lens`) or report the coincidence as a condition of
  the measurement.
- **Decision by**: USD per task including review, rounds to convergence,
  post-review defects, scope violations, disputes per task. Note from E8
  that carries over: the review bill depends on WHO wrote the diff, so
  the executor's own price is only half the comparison — and on arm B it
  is visible at all for the first time (the kimi stream carries no usage
  and no cost).
- **Round 1 measured 2026-08-22, both arms the same day.** E8's stand
  survives on disk, so both arms ran the frozen BENCH-1 set from its
  red-state commit (`eed16f7`), configs byte-identical except two lines,
  reviewer held constant (sonnet/medium, no pool), `confirmations = 1`,
  memory off. E8's own numbers are used as a PRIOR, not as arm A: they
  were produced by a different loop version and a different reviewer.
- **Result: the two arms are indistinguishable on this bench.** 6/6
  closed on both, every task on the first iteration, zero scope
  violations, zero red gates, **zero findings on either arm**; both
  stands pass their full suite independently of the loop (47 tests, no
  skips left). Effective executor work 148 s (A) vs 146 s (B) — noise.
  Review that produced a verdict: $0.535 (A) vs $0.591 (B), i.e.
  claude's diffs cost ~10 % more to review — the same *author moves the
  reviewer's bill* effect E8 saw in the other direction. Executor price
  $0.686 on B and **unknown** on A: not zero, unmeasurable, paid in
  quota — so total cost is not comparable here at all. Overhead of the
  reviewer defect below is kept out of both figures and reported
  separately ($0.359 on A, $0.414 on B, including 21 s of executor work
  replayed after the one block); full stand spend $0.894 (A) and $1.691
  (B). Accounting rule used: rounds are keyed by *(task, iter)*, and two
  rows sharing an iter are one round played twice — a replay after a
  block is not a fix round and must not be charged to the engine.
- **Conclusion, and it is the one ADR-002 predicted:** trivial
  greenfield tasks cannot discriminate executors, because both converge
  in one round and the reviewer finds nothing to say. **E13 stays open
  and needs a brownfield set** (BENCH-2-class: existing code, larger
  diffs, 2–3 plausible rounds). What round 1 did settle: switching the
  engine is operationally neutral — same outcomes, same speed, no new
  failure mode attributable to the engine.
- **The bench found a bigger lever than the one it measured.** 5 of 17
  reviewer calls (29 %) failed to return a valid verdict, burning $0.67
  — 37 % of all review spend and more than the entire distance between
  the arms. Two modes, both on sonnet/medium: a *placeholder approve*
  (`{"analysis": "Test", "verdict": "approve", …}`, caught by the
  substance guard `MIN_ANALYSIS`/`MIN_SUMMARY` — its first live catch),
  and *structured-output retry exhausted* (the model packs the whole
  verdict into `analysis` as pseudo-XML and then rewrites the prose
  instead of the shape). Not deterministic: the same reviewer approved
  the same diff on a third call. The loop behaved correctly — one silent
  retry rescued four of five, the fifth was blocked with an inbox
  question rather than guessed at. This is live in the pilot's own
  confirming pool, and it is the next thing worth fixing.

### E11. Independent Tester role

- **A (current)**: executor writes its own tests; independence comes
  from plan rules (independent oracle), reviewer verification requests
  (ADR-005) and mutation audits.
- **B**: a dedicated Tester agent writes tests from acceptance criteria
  only, never seeing the executor's code or reasoning.
- **Hypothesis**: four hollow-test cases across the program argue that
  authorship independence catches what the current mechanisms miss;
  the counter-hypothesis is that mutation audit already covers this
  cheaper.
- **Decision by**: mutation-survival rate of B-tests vs A-tests on the
  frozen bench; USD per task delta.
- **Status: queued** (one factor per run; after E10's first
  measurement).

### E12. Frozen replay benches for the loop's own judgements

- **A (текущее)**: качество суждений петли (диагноз несходимости,
  граница задачи) проверяется чтением примеров руками.
- **B**: замороженные реплей-стенды поверх золотого набора PILOT-1 —
  `experiments/goldset/diagnosis/` (шесть эскалаций, где владелец назвал
  истинную причину; гоняется в гейте `test_diagnosis_bench.py`) и
  `experiments/goldset/boundaries/` (семь признанных споров о границах;
  в гейте с 2026-08-22 как `test_boundary_bench.py`: на машине со
  стендом держит замороженную линию 4/7, без стенда — громкий skip,
  стенды в git не входят по §1.4).
- **Гипотеза**: суждение, у которого нет стенда, деградирует незаметно.
  Проверено на месте: линтер границ проходил синтетические тесты и давал
  1 спор из 7 на настоящем репозитории — стенд поймал то, чего тесты по
  замыслу поймать не могли.
- **Решение по**: счёт стенда до/после каждой правки судящего кода;
  дрейф места спорного файла в ранжировании (`replay.py` печатает «было»).
- **Статус**: оба стенда заморожены 2026-08-20. Диагноз 6/6 (прежний
  диагност — 2/6). Границы 4/7 при потолке 6 строк, 5/7 при 8; два
  класса споров текстового следа не имеют и механически недостижимы.

## 5. Порядок и зависимости

```
E4 (часы) → E1 (дни) → E2 ─┐
                            ├→ E3 (A vs B → +C) → E7
E8 — любым свободным слотом ┘
E5 — после первых прогонов петли (нужен реальный дрейф доков)
E6 — отложен (ждёт E1 и потребителя)
```

Первый слот: E4 + E1 (независимы, укладываются в правило «не больше двух»).

## 6. Что уже решено без эксперимента

Зафиксировано решениями по итогам аудита (не дублируем, ADR не нужен):

- Тяжёлая индексная инфраструктура (Glean/Kythe/CodeQL-сервер) и
  векторная БД по коду — **нет**: для одного локального репо структурный
  SCIP-граф строго лучше, поддержка дешевле.
- GraphRAG-стайл граф знаний — **не для кода**; для журнала/доков —
  вернуться только если E6-A провалится на практике.
- Первичность структурных данных над md как принцип — предмет E1 лишь
  в части «когда переходить», не «переходить ли».

## 7. Критерий завершения программы

Программа закрыта, когда по E1–E5, E7, E8 есть ADR (E6 может остаться
«отложен»), а выжившие варианты вписаны в 05-agent-swarm.md очередной
версией (раздел «Знаниевая инфраструктура»). Ожидаемый горизонт — 4–6
недель календарных, побочный продукт — обкатанный протокол
findings/ADR, который остаётся жить и после экспериментов.

---

## 8. Статус прогонов

| Прогон | Статус | Артефакты | Итог |
| --- | --- | --- | --- |
| SMOKE-1 (ручной, 1 задача) | done 2026-08-07 | experiments/smoke1/ | Полный цикл без ретраев; 4 урока → 05-док v0.7 |
| BENCH-1 (раннер, 6 задач) | done 2026-08-07 | experiments/bench/ | 6/6 done с 1-й итерации; ревью/исполнение 2.7×; $2.31; recall не измерен |
| CANARY-1 (посеянные баги) | done 2026-08-07 | experiments/canary/ | **9/9 с предрегистрированными ожиданиями: recall 100 %, FPR 0 %, инъекции 2/2 → blocked, ослабление тестов 2/2; ветка request_changes обкатана 6 раз** |
| NEG-B (golden-тесты FSM на mock) | done 2026-08-07 | experiments/neg/test_fsm.py | 26/26 зелёных: dispute, невалидный отчёт, провал gate, нарушение scope, невалидный вердикт (fail-closed), max_iterations, approve+blocker отвергается, cleanup/no-commit. 0 токенов |
| E8 (K3 vs K2.7 Coding) | done 2026-08-07 | experiments/bench/e8-* | Оба 6/6 за 1 итерацию; K2.7 на 21 % медленнее, ревью его диффов на 29 % дороже → **ADR-002: K3 остаётся дефолтом, роутинг не вводить** |
| NEG-A (живые провокации) | done 2026-08-07 | experiments/neg/ | 3/3 по ожиданиям: dispute вживую (×2, в т.ч. отклонена ловушка reward hacking), no_change_needed. **Вскрыты 3 дефекта оркестратора** → правила §5.5.1/§4.4/§4.3 в v0.9 |
| BENCH-2 (brownfield, 6 задач) | done 2026-08-07 | experiments/bench/report-b2.md | 6/6 done с 1-й итерации, 0 регрессий при gate по полному сьюту (57 тестов), 10 minor findings. Вскрыты ещё 2 дефекта оркестратора → §5.0 baseline gate и §6.2 полный список paths (v0.10). Гипотеза «brownfield = 2–3 итерации» не подтвердилась |
| BENCH-3 (задачи на провал, 6 задач) | done 2026-08-07 | experiments/bench/report-b3.md | 5/6 done с 1-й итерации; **впервые вживую request_changes → dispute** (обе стороны правы, причина — устаревший план) → §3.1 в v0.11. Ловушки на типовые ошибки не сработали; ревьюер дал 22 findings, включая оценку качества тестов исполнителя. Сьют 85 тестов |
| PLAN-1 (планировщик, репланинг спора) | done 2026-08-07 | experiments/plan/report.md | **Контур замкнут**: спор из BENCH-3 разрешён планировщиком ($0.53, дифф валиден с 1-й попытки), план исполнен петлёй 4/4 с 1-й итерации. Планировщик нашёл 2 дефекта постановки, пропущенных человеком. Валидатор план-диффа + 23 golden-теста; deps теперь управляют порядком запуска |
| HELP-1 (третий контур, хелперы) | done 2026-08-07 | experiments/helpers/report.md | Ollama Cloud (ключ из Hermes), `gemma4:31b`. Коммит-сообщения 5/5, детект топтания §5.3 верен на обоих кейсах, суммаризация журнала работает. Найдены 2 дефекта: неполный fail-open и LaTeX в тексте хелпера → §7.3 в v0.13, **ADR-003**. §7.4 подтверждён: 5.4k токенов против $20.32 дорогих ролей |
| OLLAMA-1 (изучение API провайдера) | done 2026-08-07 | experiments/helpers/ollama-reference.md | Нативный `/api/*` доступен на облаке; `think` включён по умолчанию и на `/v1` игнорируется → **ADR-003 пересмотрен, заменён ADR-004**. С `think:false` reasoning-модель дешевле в 45 раз. `format` со схемой на облаке игнорируется; `num_ctx`/`truncate`/`shift` тоже. Клиент переписан, тестов 48 |
| RESTART-1 (слой запуска и рестарт) | done 2026-08-07 | experiments/driver/report.md | Реализован `AgentDriver` §6.0 (нормализация событий, heartbeat, wall-clock cap), 12 golden-тестов. Живая проверка §5.2: kill реального агента → stash → чистый рестарт. **stash спас готовую работу** при исходе no_report. Измерена базовая линия: 7 % вызовов исполнителя без валидного отчёта |
| VERIFY-1 (этап 2: проверки ревьюера) | done 2026-08-08 | experiments/verify/report.md | **ADR-005**. Формулировка решает: «можешь запросить» → 0 запросов, «перечисли, что проверил бы исполнением» → 39 запросов (мутанты, фаззинг, timeit). Recall тот же (9/9), стоимость +51 % → включать выборочно. Найдено: схема не гарантирует смысла (заглушка `analysis: "test"` прошла), нулевой байт в выводе ломает вызов, отказ по квоте ≠ невалидный вердикт |
| **E3** (карта репозитория, A/B) | done 2026-08-08 | experiments/repomap/report.md | **ADR-006**. Обращения к репо 17 → 10 (−41 %), `Glob` 3 → 0. На локальной задаче эффект обратный → включать по условию. Карта на stdlib `ast` (~470 токенов на весь стенд), 17 тестов |
| **E3-v2** (замер на модели с телеметрией) | done 2026-08-08 | experiments/repomap/report.md | **ADR-007**. Исполнитель — Claude (есть `usage`): точный индекс −29 % стоимости против «без карты», −14 % против наивной; наивная карта на одной задаче ДОРОЖЕ отсутствия карты. Прокси-метрика E3 («число обращений») оказалась несостоятельной: 18/14/17 при падении стоимости на 29 %. Сравнены ctags (тупик), tree-sitter (нужен для Rust, неточен для Python), SCIP-подход на `ast` (лучший) |
| **TEST-AUDIT** (качество тестового набора) | done 2026-08-09 | experiments/findings.jsonl | Мерили не покрытием, а выживаемостью мутаций. 2 из 21 мутации выжили, обе на критичном: валидатор пропускал `approve` при blocker-находке (тесты FSM не переехали из прототипа при консолидации), `save_tasks` не валидировал статусы (тест ловил только `set_status`). Написаны `test_verdict.py` (31) и `test_agents.py` (22); покрытие `loop.py` 70 → 84 %, `agents.py` 38 → 57 % |
| **INDEX-AUDIT** (индексаторы и планировщик) | done 2026-08-09 | experiments/findings.jsonl | **7 из 7 мутаций выжили** — `pyindex.py` был непокрыт полностью: тесты удалили вместе с проигравшим наивным вариантом, победившему не написали. Вскрыто 8 дефектов кода: проверка версии ctags существовала только в комментарии; 5 дефектов транзакционной целостности план-диффа (`remove`+`update` → KeyError, смена `id` через `update`, отсутствие `reason`/`analysis` роняло вывод, ложный отказ на `deps` удаляемых задач); частичный `update` ронял валидатор; поверхностная копия очереди. Найден тест-пустышка (`assertIsNone(None)`) на месте настоящей проверки. Тестов 299 → 345 |
| **SANDBOX-1** (изоляция исполнителя, живые вызовы Claude) | done 2026-08-09 | experiments/findings.jsonl | Флаги `--allowedTools`/`--disallowedTools` с паттернами и `--settings` работают: коммит заблокирован (HEAD не изменился), чтение `.env` заблокировано. **Но паттерн-deny — не граница**: агент отказался обходить запрет сам (решение модели, не механика), обход возможен через неперечисленный инструмент; границу даёт положительный `--allowedTools` (или sandbox-exec). **Решение владельца: §6.1 технически не закрывать до итогов пилота** — изоляция организационная (отдельный клон) + пост-проверка целостности; делегирование исполнения Claude ломает разнородность моделей и удорожает исполнение |
| **PILOT-1** (пилот на ZeusLogic, Rust, задача p1fn) | начат 2026-08-09 | experiments/findings.jsonl, experiments/pilot/commands.md | Первая задача пилота закрыта: review APPROVE ($1.22), +64 строки/0 удалений, 6/6 критериев, zeus-model 92 → 94 теста; принята оператором вручную (коммит d8d18e3), т.к. оркестратор потерял валидный вердикт. Вскрыты **4 дефекта обвязки, ни одного в работе агентов**: верификация трактовалась как условие силы вердикта (теперь `verification_inconclusive`); запросы исполнялись без включённого механизма (расхождение с ADR-005); бюджет не учитывал двухфазность → `review_budget_usd` 1.5 → 3.0; сырой ответ второго прохода затирал первый. Гейт переведён на штатный `zeus/scripts/check.sh`, `total_budget_usd = 50` (решение владельца) |

Решения: **ADR-001** — E1 закрыт (jsonl-первичный журнал); **ADR-002** —
E8 закрыт (K3 остаётся дефолтом); **ADR-003 → ADR-004** — хелперы через
нативный API с `think: false`; **ADR-005** — проверки ревьюера по запросу,
выборочно; **ADR-006** — E3 закрыт по варианту B (карта в handoff при
задачах, требующих ориентации). **E4 закрыт замером (2026-08-22)**:
B (ast-grep) точнее A (FPR 0/10 против 1/10), общая слепая зона —
семантическое ослабление c5. Открытые: E3 вариант C (blast radius
ревьюеру — код готов, замер не проведён), E2/E5/E7 — в очереди; E6 ожил
в составе E9;
**E9 — плечо планировщика измерено на стенде PILOT-1** (2026-08-22):
с памятью план сложил все три решения владельца, включая спутник из
чужого спора (s2ky); без памяти он потерян. Плечи исполнителя и
ревьюера ждут своих прогонов; **E12 — оба реплей-стенда
заморожены** (диагноз 6/6 в гейте, границы 4/7 — в гейте с 2026-08-22:
линия держится на машине со стендом, без стенда — громкий skip). Прежнее
состояние E9 (findings E9:
решения архитектуры, сюрприз PG 18 с `\.` в CSV-COPY, утечка тестов
в общую базу — тот же класс, что метрики хелперов в AUDIT-3).

## Журнал изменений

### v0.14 (2026-08-22)

- E13 round 1 closed: the one task blocked by the reviewer defect was
  requeued and closed, both arms stand at 6/6, and the accounting now
  separates executor work from the overhead of that defect. Conclusion
  unchanged — greenfield cannot discriminate engines.

### v0.13 (2026-08-22)

- E13 round 1 measured: both arms on the frozen BENCH-1 set the same day.
  Engines indistinguishable on greenfield (6/6, one round, zero findings
  each); experiment stays open pending a brownfield set. Side result,
  larger than the one sought: 29 % of reviewer calls returned no valid
  verdict and burned 37 % of review spend.

### v0.12 (2026-08-22)

- E13 added: executor engine A/B (Kimi K3 vs Claude Sonnet). The
  same-family independence caveat is written into the design of the
  experiment rather than left to be found in its results.
- E10: the blocker on arm A is gone — the engine is a config choice now,
  so arm A can run on claude if the engine is named as a factor.

### v0.11 (2026-08-22)

- E9: planner arm measured — one-factor A/B on the three blocked
  idea-tasks. Memory ON folded all three owner decisions into the plan
  (incl. the s2ky satellite from another task's dispute); memory OFF
  lost it. Executor/reviewer arms still pending; quality call on the
  new plan-diffs is the owner's.


### v0.10 (2026-08-22)

- E12: boundary replay bench moved into the gate
  (`test_boundary_bench.py`) — frozen 4/7 floor on machines with the
  stand, loud skip without it; `replay.py` exposes `measure()` as the
  shared source of truth for script and test.


### v0.9 (2026-08-22)

- E4 closed by measurement: regex (A) vs ast-grep (B) on a 20-case
  synthetic set, zero LLM calls — A: recall 9/10, FPR 1/10; B: recall
  9/10, FPR 0/10; shared blind spot is c5-style semantic weakening.
  Recommendation: B as the §5.5 detector, dependency decision is the
  owner's.

### v0.8 (2026-08-20)

- E9: planner arm switched on for the stand (`memory = "planner"`),
  memory wired into `replan`, flag values validated as role names.
- E12 added: frozen replay benches for the loop's own judgements
  (`goldset/diagnosis` in the gate, `goldset/boundaries` outside it) —
  born from a linter that passed synthetic tests and scored 1/7 on the
  real corpus.

### v0.7 (2026-08-20)

- E9: auto-indexing status note — index maintenance split from the
  injection factor (`memory_index = "auto"`), A/B protocol unaffected.

### v0.6 (2026-08-18)

- E10 (contract-first skeleton + model routing) and E11 (independent
  Tester) registered — in English per the owner's documentation-language
  rule. E10 carries the fill-executor probe results: five Ollama Cloud
  chat models pass a strict full-file completion contract; no elision at
  ~100 lines; feedback rounds converge. Probe know-how formalized as
  project skills `ollama-cloud-api` and `openrouter-embeddings`.

### v0.5 (2026-08-18)

- Добавлен E9 «Память между прогонами»: двухъярусная память
  (эпизоды → детерминированный дайджест), файлы — источник истины,
  PG — производный индекс; этап 1 реализован за флагом
  `[experiments] memory`, протокол замера описан. Ключевые механизмы
  перенесены из рабочей памяти graphify/Orakul (grounding как пропуск,
  распад по ре-валидации, уверенность из повторяемости).
- E6 ожил в составе E9: потребитель поиска появился (retrieve уроков
  при сборке промпта); сравнение FTS против эмбеддингов закроется
  телеметрией queries.jsonl, а не прогнозом.

### v0.4 (2026-08-10)

- В «Статус прогонов» добавлены SANDBOX-1 (изоляция исполнителя: флаги
  Claude работают, но паттерн-deny — не граница; техническое закрытие
  §6.1 отложено до итогов пилота) и PILOT-1 (первая задача пилота на
  ZeusLogic закрыта: четыре дефекта обвязки, ни одного в работе агентов;
  гейт — штатный `zeus/scripts/check.sh`, бюджет прогона $50).

### v0.3 (2026-08-09)

- В «Статус прогонов» добавлены TEST-AUDIT и INDEX-AUDIT — два аудита
  собственного тестового набора мутациями.
- Зафиксировано правило программы: **мутационный аудит запускается при
  каждой замене подсистемы**, а не разово. Оба прогона нашли дыры именно
  там, где менялся код, а тесты за ним не пошли: покрытие оставалось
  высоким, потому что модуль исполнялся из соседних тестов, но инвариант
  не проверял никто.
- Второе правило: выжившая мутация — сначала гипотеза о слабости самой
  мутации. Две мутации пережили уже новые тесты по вине методики (запасной
  путь индекса подставлял верный ответ; обнулено одно слагаемое формулы
  вместо всего вклада графа).

### v0.2 (2026-08-07)

- Правило 6 «документация — в приоритете» + роль документатора (ссылка на
  §3.3 петли). Раздел 8 «Статус прогонов»: SMOKE-1 и BENCH-1 закрыты,
  CANARY-1 открыт; зафиксирован кандидат ADR-001 (E1 → jsonl).

### v0.1 (2026-08-06)

- Первая редакция: правила программы, общий бенч, протокол
  findings.jsonl + ADR, эксперименты E1–E8 с гипотезами и критериями
  решений, порядок, список решённого без экспериментов.
