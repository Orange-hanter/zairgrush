---
title: "ZAIrgRush — Брифы для решений «на утверждение»"
type: analysis
status: draft
version: 0.2
created: 2026-08-22
updated: 2026-08-24
related:
  - 05-agent-swarm.md
  - 07-experiments-journal.md
summary: >
  Три открытых решения 05-документа (лёгкое ревью тривиальных диффов,
  пороги приёмки §9.1, таксономия findings §12) плюс бриф-прогноз по
  слою выбора усилия: контекст, накопленные программой данные, варианты
  и рекомендация по каждому. Пометки «на утверждение» снимает только
  владелец.
---

# Брифы для решений «на утверждение»

В 05-agent-swarm.md три пометки «на утверждение» (четвёртая строка в
grep — ссылка из журнала изменений на первую). Каждый бриф — страница:
вопрос, что измерено программой, варианты, рекомендация, что значит
«подтвердить». Решения — за владельцем; брифы только кладут данные на
стол.

## 1. Лёгкий режим ревью для тривиальных диффов (§7.4)

**Вопрос.** На микрозадачах ревью втрое дольше и дороже исполнения
(100 с / $0.58 против 33 с у K3). Ввести ли порог (размер диффа / число
файлов), ниже которого ревью упрощается: пропуск с пометкой в журнале
или ревью дешёвой моделью.

**Что измерено.** Перевёрнутая экономика подтверждена статистикой:
BENCH-1 — ревью/исполнение 2.7×. Но та же программа измерила и ценность
полного ревью: CANARY-1 — recall 100 %, FPR 0 % на посеянных багах;
goldset PILOT-1 — эскалации ревьюера приняты владельцем 6/6. VERIFY-1
показал, что углубление ревью (проверки исполнением) стоит +51 % при
том же recall — то есть кривая «цена → качество» у ревьюера плоская
в обе стороны от текущей точки.

**Варианты.**

- **Пропуск ревью** под порогом: максимальная экономия, но ломает
  главное доказанное свойство программы — 100 % recall достигнут
  полным ревьюем, включая мелкие диффы (c1 — баг в одной строке
  косметического редактирования).
- **Дешёвая модель** под порогом: E10-пробы показали, что пять моделей
  Ollama Cloud проходят контракт полного файла; цена — копейки, но
  качество ревью этих моделей на канарейках не измерено (новый
  эксперимент, по духу — E8 для ревьюера).
- **Оставить как есть**: экономия на микрозадачах теряется в масштабе
  программы (пилот: $28.63 из $50 за 19 задач — не ревью съел бюджет,
  а 403-квоты и futile-раунды, уже починенные).

**Рекомендация.** Не вводить пропуск. Если экономика микрозадач важна —
измерить дешёвую модель на канарейках (c1–c9) прежде, чем пускать её в
петлю: порог включения — recall 9/9 по тому же эталону, что и у
дорогого ревьюера. До этого замера «лёгкое ревью» — экономия на
неизмеренном риске.

**Подтвердить** = выбрать вариант и поднять §7.4 05-документа (запись
в журнал изменений, v0.49).

## 2. Пороги приёмки эксперимента (§9.1)

**Вопрос.** Чек-лист §9.1 записан как «проект значений» — пороги
предварительные, «уточняются после первого прогона». Первые прогоны
состоялись; какие значения подтверждаются?

**Что измерено (по строкам чек-листа).**

- Catch rate ревьюера ≥ 70 %: **перевыполнен** — CANARY-1 9/9 (100 %),
  инъекции 2/2 → blocked, ослабления тестов 2/2.
- Вызовы без валидного отчёта, базовая линия 7 %: подтверждена и стала
  измеряемой причиной (RESTART-1: 3 из 43, все на нестандартных
  ситуациях, ретрай отработал).
- Итерации до сходимости ≤ 3: подтверждено с запасом — BENCH-1/2, E8:
  6/6 с первой итерации; гипотеза «brownfield = 2–3 итерации» не
  подтвердилась в худшую сторону.
- Зависания без диагностированной причины = 0: подтверждено после
  d939ba3 (futile-раунды получили свой потолок и диагноз) и E12
  (диагност 6/6 на реплей-бенче).
- Вмешательства человека ≤ 1 на 3 задачи: пилот (19 задач) — эскалации
  и споры шли сериями вокруг дефектов обвязки; после их починки вторая
  половина очереди шла без вмешательств. Метрика сильно зависит от
  зрелости стенда — предлагается считать её по установившимся прогонам,
  а не по пилоту целиком.
- Изменения тестовых путей вне test-задач = 0: подтверждено (c5/c6
  пойманы); свежее уточнение — детекторы §5.5 измерены в E4.
- Дубли коммитов после resume = 0, FPR ≤ 10 %, коммиты без доработок
  ≥ 70 %: прямого замера в программе не было; требуют выгрузки из
  журнала пилота (отдельная малая задача, без LLM).

**Рекомендация.** Подтвердить пороги как есть, кроме «вмешательства
человека» — его считать по прогонам после закрытия дефектов обвязки
(иначе метрика мерит молодость инструмента, а не задачи). Три строки
без замера (дубли resume, FPR, ≥70 % коммитов) — посчитать по журналу
пилота и подтвердить или скорректировать фактом.

**Подтвердить** = снять «проект значений» с §9.1, вписать подтверждённые
базовые линии (v0.49).

## 3. Таксономия findings (§12)

**Вопрос.** Закрытые enum'ы severity (blocker/major/minor) и category
(correctness/tests/style/scope/architecture) — подтвердить или
скорректировать по опыту программы.

**Что измерено.** Таксономия прошла эксплуатационную проверку: на ней
работает механическая валидация вердиктов (approve+blocker отвергается —
NEG-B), политики автозакрытия minor (§4.4), разделение на замысловые
категории (architecture/scope → человек, INTENT_CATEGORIES). Goldset
PILOT-1: эскалации ревьюера в этих категориях приняты 6/6, споры
исполнителя 7/7 — обе стороны петли оперируют таксономией без
наблюдавшихся провалов классификации. За всю программу не записано ни
одной находки «не влезла в enum» — известный дефект был обратным:
диагноз «слишком крупная задача» выдавался за вывод (это таксономия
диагнозов, не findings, и она уже починена в d939ba3).

**Рекомендация.** Подтвердить без изменений: три уровня severity и пять
категорий закрывают наблюдавшееся, а механическая валидация
неизвестных значений (retry → эскалация) — правильный предохранитель на
будущее. Расширять enum'ы стоит только по факту находки, не влезающей
в существующие, — таких пока нет.

**Подтвердить** = снять «на утверждение» с заголовка §12 (v0.49).

## 4. An effort-choosing layer in front of the paid roles (in English per owner's rule)

**Question.** Add a layer that picks `effort` (and possibly the model)
per model run — a router in front of the reviewer — instead of today's
static config plus per-call lottery. What does the program's own data
predict it would do? This is brief 1's question asked from the other
side: 1 proposes a *size* threshold, 4 a *predicted difficulty*, and
both turn out to need the same missing ingredient.

**Where the money is.** Live GatewayDemo stand
(`gateway-swarm-plugins/.swarm/metrics.jsonl`, 2026-08-20…24, 15 runs,
11 tasks, swarm_sha 8a6f914…96d0cb1, executor sonnet / review opus /
confirm sonnet, effort at session default everywhere): $94.75 metered,
of which review $77.88 (82 %) and executor $16.87. 53 real reviewer
calls (11 further rows are quota stubs), 47 valid, median $1.60. The
reviewer is the only place where an effort lever moves real money — but
the leverage is task-set dependent: on E13's small tasks review was
$0.73 against an executor's $0.84 (47 %), not 82 %.

**The lever's dynamic range is real**, and paired same-diff measurements
already exist: e4kb (one diff, effort the only difference) — 3 findings
at xhigh, 3 at medium, one overlap, $1.17 vs $0.88; review-ab medians
(n = 21 with an explicit arm) — opus/xhigh $2.25 / 445 s / 4.0 findings,
opus/medium $1.28 / 249 s / 2.0, sonnet/medium $0.73 / 161 s / 1.0;
E13's paired re-judge over 12 frozen brownfield diffs — sonnet/medium 0
findings for $0.73 against opus/xhigh 6 (all minor) for $2.47. Per call,
a downgrade buys −40 % to −70 %.

**The signal a pre-call router needs is not in the data.** On the fresh
stand, 35 first-look reviews joined to the `implement` row of the same
(task, iter): Spearman(findings, executor wall time) = +0.13,
(findings, executor tool events) = +0.24, (findings, iteration) = +0.09.
Review cost and duration do correlate with findings (+0.48 / +0.45), but
those are consequences of the effort chosen, not inputs available before
the call. A router deciding difficulty ahead of the call reads noise.

**The rule the backlog wanted most is falsified by the same stand.** The
speed analysis (2026-08-19) ranked "context-aware routing of mechanical
fix-round reviews to sonnet/medium" as lever 2. First-look reviews by
iteration: iter 1 n = 20, mean 3.80 findings, blocked 11/20; iter 2
n = 10, 3.60, 5/10; iter ≥ 3 n = 5, 3.80, 2/5. A later round is as
productive and nearly as blocking as the first — there is no mechanical
fix round here to route cheaply.

**The one asymmetry that IS in the data is the round's role, not the
diff's difficulty.** Confirming rounds: n = 12, mean 2.50 findings
against 3.80, request_changes 1/12 against 18/35, $17.77 = 23 % of valid
review spend. That is exactly the slice the loop already routes by
config (`review_*` pinned strong, `confirm_*` cheap; k3ad closed in two
rounds as $2.69 strong + $0.60 cheap) — zero code, no new layer.

**Ceiling of the win, and the price of missing.** 28 of 47 valid reviews
ended in approve, costing $44.50. An omniscient router that had run
exactly those at sonnet/medium's median would cut review spend by ~$24:
−31 % of review, −25 % of the whole bill. That is the ceiling, and it
needs the verdict before the call. The other side is measured too: in 2
of PILOT-1's 4 same-diff pairs a cheap arm approved what opus/xhigh then
blocked with findings the owner accepted verbatim (q013, q015, q017).

**Costs the layer adds beyond its own price.** Its own price is
negligible: as an Ollama helper it is $0 metered and 2–8 s at bake-off
speeds against a 343–445 s review, failing open like the five existing
helpers. The costs are elsewhere.

1. **Effort moves the blocking threshold, not just recall** — e7in i3:
   opus/medium blocked with 4 findings the same diff opus/xhigh approved
   with 7 minors. A router that varies effort injects variance into the
   loop's control signal, and one extra round per ten tasks (≈ $1.6 of
   review plus a ~5.6 min fix round) cancels the confirming-round saving.
2. **Cheap arms fail the verdict contract more often**: 25–29 % invalid
   on sonnet/medium across both E13 rounds, burning 37 % of that bench's
   review spend. On the current stand, with the strong arm on first look,
   invalid calls are 6 of 53 (11 %) and $1.24 (1.6 %). Routing traffic
   down moves that rate toward the E13 number, and an exhausted retry
   ends in a question to the human — where the calendar cost lives
   (median wait 24 min, worst 21 h).
3. **It breaks the measurement design.** Today the arm is drawn per call,
   so every approve→confirm sequence yields a same-diff pair for free.
   Make the arm a function of the diff and both calls on a diff get the
   same arm: pairs stop accruing, and arms become comparable only across
   different diffs — where findings depend on the diff more than on the
   arm (5 vs 2 findings on two diffs of one PILOT-1 run). Required sample
   size goes from units to dozens.
4. **Detectability.** Per-call review cost CV is 27 %; per-task review
   spend CV is 76 % with a 12× spread across the 11 tasks. Unpaired,
   ~19 calls per arm are needed to see a 25 % cost change and ~117 for
   10 %; findings CV is 48 %, so ~59 calls per arm for a 25 % change in
   yield. A layer expected to move the bill by under 10 % cannot be
   validated at pilot scale at all.
5. **Cache.** Effort routing is cache-neutral (same prefix); model
   routing is not. Observed economy on this stand: 53.7M cache_read
   against 2.8M cache_write (95 % read) — the cache is carrying the loop,
   and per-model prefix churn is the way to lose it.

**Forecast.**

- **Role-based rule** (strong first look, cheap confirm): −9…−12 % of the
  metered bill on a stand like this one ($17.77 → ≈ $8.8 at sonnet/medium's
  median), −16 % of review wall time, no new failure mode, zero code.
  Already available, largely already adopted — and this is the whole
  measured win.
- **Predictive layer on pre-call features**: 0…−10 % of the bill, with a
  risk band at least as wide on the other side, indistinguishable from
  noise at pilot scale, and net-negative over the first ~20 tasks through
  costs 2 and 3.
- **The objective the data does support is a different one.** The binding
  constraint on the strong arm in PILOT-1 was not dollars but session
  quota (4 quota_waits and one ~4 h pause stalling a run; 6 quota stubs
  on this stand). A layer that schedules a scarce strong-arm budget
  across a queue has a target the data supports; a difficulty predictor
  does not.

**Recommendation.** Do not build a predictive effort layer now; keep the
role-based rule. Before the question can even be asked properly, close
the observability gap it exposes: the review metric row records
`findings` as a bare count and nothing at all about the diff, so the
router's feature space has never been measured directly — this brief had
to proxy it through executor wall time and tool events. `review()`
already holds the condensed diff at the call site
(`tools/swarm/swarm/reviewer.py:48`) and the verdict's findings carry
severity, so `diff_lines`, `diff_files` and a `majors` count are a few
lines each and turn "does difficulty predict findings" into a question
the journal can answer. If the layer is ever built, judge it offline
against the frozen E12 stands first — a router is a judgement, and a
judgement without a stand degrades unnoticed (E12's own thesis). Entry
criterion: it never downgrades a diff whose paid review produced an
owner-endorsed major.

**Confirm** = nothing to lift in the 05-doc. This brief closes a
backlog question instead (speed-analysis levers 1–2, "cache-economy
routing"): the cheap half is already in config, and the predictive half
fails on the program's own numbers.

## Журнал изменений

### v0.2 (2026-08-24)

- Бриф 4: прогноз по слою, выбирающему усилие для вызова модели
  (по данным живого стенда GatewayDemo 2026-08-20…24, review-ab PILOT-1
  и парных замеров E13). Не решение «на утверждение» 05-документа, а
  ответ на вопрос из бэклога (speed-analysis, рычаги 1–2).

### v0.1 (2026-08-22)

- Первая редакция: три брифа по пометкам «на утверждение» 05-документа
  (v0.48). Данные — по состоянию программы на 2026-08-22 (findings.jsonl,
  266 наблюдений).
