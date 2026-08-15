---
title: "Temporal как замена ручной обвязке оркестратора: разведка по первоисточникам"
type: research
status: draft
version: 0.1
updated: 2026-08-15
related:
  - 05-agent-swarm.md
  - 06-knowledge-infra-experiments.md
  - 08-operators-guide.md
  - tools/swarm/swarm/loop.py
  - tools/swarm/swarm/state.py
  - tools/swarm/swarm/agents.py
---

# Temporal как возможная замена ручной обвязке оркестратора

## 0. Что и как проверялось

Отправная точка — маркетинговая страница [temporal.io/solutions/platform-engineering](https://temporal.io/solutions/platform-engineering).
Она использована только как вход; все фактические утверждения ниже взяты из
`docs.temporal.io`, репозитория [github.com/temporalio/sdk-python](https://github.com/temporalio/sdk-python)
(README прочитан через GitHub API, ссылки на разделы даны по заголовкам),
страницы цен и метаданных PyPI. Формулировки маркетинговой страницы вынесены
в отдельный раздел §7 и там же сверены с доками — две из них первоисточником
**не подтверждаются**.

Оцениваемая система: `tools/swarm/` — ~4.5 тыс. строк Python, из них ядро
петли `loop.py` (687), `state.py` (417), `agents.py` (478). Код прочитан
целиком перед выводами.

---

## 1. Что Temporal решает из коробки, а что остаётся нашим

Модель Temporal: **Workflow** — детерминированный код, описывающий процесс;
**Activity** — «a normal function or method that executes a single,
well-defined action (either short or long running), such as calling another
service, transcoding a media file, or sending an email message», причём
«Activity code can be non-deterministic»
([docs.temporal.io/activities](https://docs.temporal.io/activities)).
Сервис пишет **Event History** и при сбое воспроизводит (replay) код workflow
по этой истории, восстанавливая состояние до точки падения
([Understanding Temporal](https://docs.temporal.io/evaluate/understanding-temporal)).

| Наша проблема | Кто решает | Первоисточник и оговорка |
|---|---|---|
| Возобновление после падения процесса (`swarm resume`, `state.unfinished_steps()`) | **Temporal, полностью** | «Durable Execution ensures that your application behaves correctly despite adverse conditions by guaranteeing that it will run to completion» ([understanding-temporal](https://docs.temporal.io/evaluate/understanding-temporal)). Replay из Event History заменяет наш step-journal как механизм восстановления *позиции* в процессе. |
| Ретраи с политикой (невалидный вердикт = один повтор; бюджетный обрыв = эскалация без повтора) | **Temporal, полностью** | `RetryPolicy` c полями `initial_interval`, `backoff_coefficient`, `maximum_interval`, `maximum_attempts`, `non_retryable_error_types` ([retry-policies](https://docs.temporal.io/encyclopedia/retry-policies), [python/failure-detection](https://docs.temporal.io/develop/python/failure-detection)). Наш ручной `attempt == 1 → self.review(..., attempt=2)` (`agents.py:416`) и особый случай `terminal == "budget_exhausted"` (`agents.py:407`) выражаются декларативно: `maximum_attempts=2` плюс `non_retryable_error_types`. |
| Идемпотентность шага с побочным эффектом (`git commit` ровно один раз) | **Остаётся нашим** | Temporal гарантирует, что *результат* завершённой Activity не будет пересчитан при replay, но не гарантирует однократность самого побочного эффекта: доки прямо рекомендуют делать Activity идемпотентными ([activities](https://docs.temporal.io/activities)). Падение worker'а между `git commit` и записью результата в историю даёт повторный запуск Activity — ровно тот сценарий, ради которого написан `_Step` в `state.py:392`. Логика «проверь, применился ли side-effect» остаётся нашей. |
| Таймеры и задержки | **Temporal** (нам почти не нужно) | Durable timers, максимальная длительность таймера — «maximum duration of 100 years» ([cloud/limits](https://docs.temporal.io/cloud/limits)). У нас таймеры — это `subprocess timeout` (`loop.py:189`, `gate_timeout`), они и остаются внутри Activity. |
| Стоп-условия: `max_iterations`, детект несходимости, стоп по бюджету в долларах | **Остаётся нашим целиком** | Первоисточник таких примитивов не описывает — это прикладная логика. `decide()` (`loop.py:140`), `_diagnose()` (`loop.py:361`), `total_spend()` (`state.py:204`) переносятся один в один. |
| Человек в середине, ожидание днями | **Temporal, механизм есть** | Signals/Updates и `await workflow.wait_condition(...)`, timeout опционален ([python/message-passing](https://docs.temporal.io/develop/python/message-passing)). Подробно — §3. Но семантика инбокса (вопрос с контекстом, накопление `human_decisions`, расширение `paths` через `add_paths` — `state.py:244`) остаётся нашей. |
| git как транзакционная граница: commit после approve, stash при терминальном исходе, `revert` работы агента | **Не решает вообще** | В доках нет ни одного примитива, работающего с рабочим деревом. Более того, модель Temporal предполагает переносимость Activity между worker'ами, а наша петля привязана к одному worktree на одной машине: пришлось бы держать один worker с параллелизмом 1. |
| Состояние, которое человек читает и правит руками (`.swarm/tasks.json`, jsonl-журнал, ADR-001) | **Теряется** | Event History не редактируется руками; доступные операции — просмотр в Web UI/CLI, `reset`, сигналы. Замена `vim .swarm/tasks.json` на `temporal workflow reset` — не равноценный обмен для нашего режима работы. |

Резюме по вопросу 1: **Temporal закрывает ровно два наших механизма —
возобновление и ретраи.** Идемпотентность побочных эффектов, стоп-условия,
границы задачи, работа с git и человеческий интерфейс остаются нашими
полностью.

---

## 2. Модель исполнения: что можно и что нельзя

### 2.1 Детерминизм workflow-кода

«Generally speaking, this means you must take care to ensure that any time
your Workflow code is executed it makes the same Workflow API calls in the
same sequence, given the same input»
([workflow-definition](https://docs.temporal.io/workflow-definition)).
Нельзя менять порядок, добавлять или убирать без версионирования: запуск и
отмену таймеров, планирование Activity (включая local activities), запуск
дочерних workflow, сигналы внешним workflow, Nexus-операции, завершение
workflow любым способом, upsert search attributes и memos, вызовы
`SideEffect`/`MutableSideEffect` (там же).

Важная точность: ограничение накладывается на **последовательность вызовов
Temporal API**, а не на произвольную чистую логику. Наш `decide()` —
чистая функция от `(round_no, verdict, history)`; менять в ней пороги,
категории и формулировки диагноза можно свободно. Ломает детерминизм другое:
добавление или перестановка *шагов* — а именно так наш FSM и растёт
(подтверждающий раунд `confirming`, раунд верификации в `agents.review`).

### 2.2 Python SDK: sandbox

Workflow выполняется в sandbox'е, который перехватывает недетерминированные
вызовы. Явно отключены, по README sdk-python (раздел «Asyncio and
Determinism»): «Thread related calls such as `to_thread()`,
`run_coroutine_threadsafe()`, `loop.run_in_executor()`, etc», «Calls that
alter the event loop», и — ключевое для нас — «Calls that use anything
external such as networking, subprocesses, disk IO, etc». Некоторые
`asyncio`-утилиты заменены детерминированными аналогами (`workflow.wait()`,
`workflow.as_completed()`).

Sandbox можно ослабить: passthrough-модули, `@workflow.defn(sandboxed=False)`,
`UnsandboxedWorkflowRunner`, — но «Skipping Workflow Sandboxing results in a
lack of determinism checks»
([python-sdk-sandbox](https://docs.temporal.io/develop/python/python-sdk-sandbox)).
Сам README предупреждает: «The sandbox is built to catch many
non-deterministic and state sharing issues, but it is not secure… The sandbox
is only a helper, it does not provide full protection».

**Что это значит для нас конкретно.** `loop.py` практически целиком состоит из
запрещённого: `self._sh(...)` (`loop.py:189`), `subprocess.run` для git
(`commit`, `cleanup`, `revert`, `_apply_patch`), чтение и запись файлов через
`SwarmState`. Всё это обязано переехать в Activity, а `run_task` превращается
в тонкую оболочку из `await workflow.execute_activity(...)`. Это механическая,
но не бесплатная переработка: перенос ~400 строк с побочными эффектами и
пересборка контракта каждого шага в дата-класс (README: «Activities can only
have positional arguments. Best practice is to only take a single argument
that is an object/dataclass»).

### 2.3 Долгие внешние вызовы: наши 10–15-минутные `kimi -p` / `claude -p`

Это ровно тот случай, под который Activity и сделаны. Практическая обвязка:

- Activity — **синхронная** функция; README прямо называет синхронный тип
  рекомендуемым, а для него «the `activity_executor` worker parameter must be
  set with a `concurrent.futures.Executor` instance». Доки добавляют:
  «By default, Activities should be synchronous rather than asynchronous»,
  потому что блокирующий вызов в `async def` «blocks your event loop and the
  rest of Temporal»
  ([python-sdk-sync-vs-async](https://docs.temporal.io/develop/python/best-practices/python-sdk-sync-vs-async)).
  То есть наш `subprocess.run` живёт в потоке `ThreadPoolExecutor`.
- Обязателен один из двух таймаутов: «An Activity Execution must have either
  the Start-To-Close or the Schedule-To-Close Timeout set»
  ([python/activities/timeouts](https://docs.temporal.io/develop/python/activities/timeouts)).
- Умолчания таймаутов
  ([detecting-activity-failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures)):
  - **Schedule-To-Start** — «∞ (infinity)», «non-retryable by design»;
  - **Schedule-To-Close** — «∞ (infinity)»;
  - **Start-To-Close** — «same as the default Schedule-To-Close Timeout», то
    есть по умолчанию бесконечность; «We strongly recommend setting a
    Start-To-Close Timeout», потому что «The Temporal Server relies on the
    Start-To-Close Timeout to force Activity retries»;
  - **Heartbeat Timeout** — «If this timeout is reached, the Activity Task
    fails and a retry occurs»; для долгих Activity «we recommend using a
    relatively short Heartbeat Timeout and a frequent Heartbeat».
- Heartbeat нужен не только для живости: без него нельзя отменить Activity.
  «In order for a non-local activity to be notified of cancellation requests,
  it must be given a `heartbeat_timeout` at invocation time and invoke
  `temporalio.activity.heartbeat()` inside the activity» (README sdk-python,
  «Heartbeating and Cancellation»). Наш `driver.py` со `silence_timeout` и
  `wall_clock_cap` — это самодельный heartbeat поверх потока stream-json; в
  Temporal он же становится источником `activity.heartbeat()`.

**Подводный камень, который надо назвать прямо: retry Activity перезапускает
функцию с начала.** README: «heartbeats also support detail data that is
persisted on the server for retrieval during activity retry. If an activity
calls `temporalio.activity.heartbeat(123, 456)` and then fails and is retried,
`temporalio.activity.info().heartbeat_details` will return an iterable
containing `123` and `456` on the next run». То есть возобновление внутри
Activity — ручное, через собственный чекпойнт. Для нас это значит: падение
worker'а на 12-й минуте вызова ревьюера приводит к **повторному платному
вызову модели**, если не выставить `maximum_attempts=1` или не сохранить
уже полученный ответ. С учётом того, что умолчание `maximum_attempts` — «∞»
([retry-policies](https://docs.temporal.io/encyclopedia/retry-policies)),
это дефолт, который для нас деньгами опасен: конфигурировать ретраи
придётся явно и с первого дня.

Ещё два ограничения самих workflow-таймаутов
([detecting-workflow-failures](https://docs.temporal.io/encyclopedia/detecting-workflow-failures)):
Workflow Execution Timeout по умолчанию «∞ (infinite)», а **Workflow Task
Timeout — «10 seconds»**. Второе часто путают с длительностью работы: это
время на обработку одного workflow task (шаг детерминированного кода), а не
на Activity. Наш «тупой» код между шагами укладывается в него с запасом,
но любое случайно оставленное в workflow тяжёлое вычисление (например,
построение repo map через `codemap.HybridIndex`, ~16 с на 3.2k файлов —
`agents.py:118`) немедленно упрётся в этот лимит. Такие вещи обязаны быть
Activity.

---

## 3. Человек в середине процесса

Механизм есть и выражается естественно:

- **Signal** — «asynchronous message sent to a running Workflow Execution to
  change its state and control its flow»; **Update** — «trackable synchronous
  request… can change the Workflow state, control its flow, and return a
  result»; **Query** — чтение без мутации
  ([python/message-passing](https://docs.temporal.io/develop/python/message-passing)).
- Ожидание: `await workflow.wait_condition(lambda: self.approved_for_release)`;
  функция «accepts a function that returns `True` or `False`, and you can
  optionally set a timeout». В README sdk-python: «`workflow.wait_condition`
  is an async function that doesn't return until a provided callback returns
  true», timeout опционален. **Ожидание без таймаута ничем не ограничено по
  времени** — Workflow Execution Timeout по умолчанию бесконечен
  ([detecting-workflow-failures](https://docs.temporal.io/encyclopedia/detecting-workflow-failures)).
  Задача может стоять в blocked часами и днями законно.
- Пока workflow ждёт, он не потребляет worker-слот. Биллинг Temporal Cloud
  считает Actions, а не время: в списке billable actions — «Workflow started»,
  «Activity started or retried», «Activity Heartbeat recorded», timer started,
  signal sent, query/update received; «Actions that occur during Workflow
  Replay do not count towards billed Actions»
  ([cloud/actions](https://docs.temporal.io/cloud/actions)). Простаивающий
  workflow стоит только Active Storage.

Ограничения, релевантные долгому ожиданию
([cloud/limits](https://docs.temporal.io/cloud/limits),
[self-hosted-guide/defaults](https://docs.temporal.io/self-hosted-guide/defaults)):

| Лимит | Значение |
|---|---|
| Event History | предупреждение на 10 240 событий / 10 MB, ошибка на **51 200 событий / 50 MB** (`HistoryCountLimitError`, `HistorySizeLimitError`) |
| Signals на один workflow | «10,000 Signals» |
| Updates в истории | «2000 total Updates», в полёте — «maximum of 10» |
| Незавершённых Activity/Signal/Child в полёте | 2 000, «optimal: 500 or fewer» |
| Длительность workflow | Workflow Execution Timeout по умолчанию ∞; ограничение — не время, а размер истории |

Обходной механизм для длинных процессов — **Continue-As-New**: «Continue-As-New
allows you to checkpoint your Workflow's state and start a fresh Workflow»;
применять нужно, когда история «may bog down and have performance issues» или
«generate more Events than allowed by the Event History limits», а также чтобы
не застревать на старой версии кода
([continue-as-new](https://docs.temporal.io/workflow-execution/continue-as-new)).
Оговорка, задевающая нас: «Temporal does not support Continue-As-New
functionality within Update handlers»
([python/message-passing](https://docs.temporal.io/develop/python/message-passing)).

**Вывод по вопросу 3.** Для нашей нагрузки лимиты истории не жмут: задача
живёт 10–30 минут и порождает единицы-десятки событий на раунд, при потолке
в 51 200. Ожидание человека выражается чисто. Но `swarm answer q003 "..."`
превращается в отправку сигнала, а «прочитать инбокс» — в query или во внешнее
хранилище. То есть человеческий интерфейс придётся переписать, и он станет
менее прозрачным, чем `cat .swarm/log/run.jsonl`.

---

## 4. Цена входа

### 4.1 Self-hosted

Temporal Service = Temporal Server (внутри — Frontend, History, Matching и
Worker services) плюс **Persistence store** и **Visibility store**
([temporal-service](https://docs.temporal.io/temporal-service)).

Поддерживаемые БД ([persistence](https://docs.temporal.io/temporal-service/persistence)):

- Persistence: Cassandra «v3.11, v4.0, and 5.0.4 and later»; PostgreSQL
  «13.18, 14.15, 15.10, and 16.6»; MySQL v5.7 и v8.0 (8.0.19+); SQLite v3.x.
- Visibility: SQL (MySQL/PostgreSQL/SQLite) либо Elasticsearch.
- SQLite «is meant only for development and testing, not production usage».

Практическая заметка под наше окружение: у нас нативный **PostgreSQL 18**
(глобальные правила), а в списке протестированных версий верхняя — 16.6.
Совместимость с 18 первоисточником **не подтверждена**.

Операционная нагрузка описана самим Temporal без прикрас
([production-checklist](https://docs.temporal.io/self-hosted-guide/production-checklist)):
нужно мониторить `service_requests`/`service_errors`/`service_latency`,
`persistence_*`, счётчики исходов workflow; число шардов задаётся при сборке
сервиса «and can't adjust it later» — расширение требует миграции на новый
сервис; «Temporal recommends upgrading sequentially, not skipping any minor
versions», «Server upgrades can negatively affect self-hosted Temporal Service
availability». Итог формулируется прямо: «Resolving these challenges takes
significant engineering and ongoing effort», нужны «trained, experienced
administrators familiar with Temporal Service architecture».

Есть третий, самый дешёвый вариант — dev-сервер CLI:
`temporal server start-dev` поднимает сервис одной командой, порт 7233 и Web UI
на 8233, по умолчанию база **в памяти** («By default, Workflow Executions are
lost when the server process dies»), персистентность включается флагом
`--db-filename` (SQLite). Но доки предупреждают: «The development server is not
intended for production use. It skips certain HTTP security checks to make
local use simpler» ([cli/server](https://docs.temporal.io/cli/server)).
Для одиночной локальной петли это технически рабочий путь, но это явно
неподдерживаемая для долговременного использования конфигурация.

### 4.2 Temporal Cloud и тарификация

- Actions — «the primary unit of consumption-based pricing… such as starting
  Workflows, recording a Heartbeat or sending messages»
  ([cloud/pricing](https://docs.temporal.io/cloud/pricing)).
- Цена сверх включённого объёма: следующие 5M — **$50 за миллион**, далее
  $45 / $40 / $35 / $30 / $25 по мере роста ([temporal.io/pricing](https://temporal.io/pricing)).
- Хранилище: Active Storage **$0.042 GBh**, Retained Storage **$0.00105 GBh**;
  «Storage costs are measured in gigabyte-hours (GBh)», 1 GB = 744 GBh
  ([cloud/pricing](https://docs.temporal.io/cloud/pricing)).
- Планы и минимумы ([cloud/pricing](https://docs.temporal.io/cloud/pricing),
  [temporal.io/pricing](https://temporal.io/pricing)): **Essentials** — «greater
  of $100/month or 5% of usage», включено 1M Actions, 1 GB active, 40 GB
  retained; **Business** — «greater of $500/month or 10% of usage», 2.5M
  Actions; Enterprise и Mission Critical — по договору. Новым пользователям —
  «$1,000 in credits».
- Namespace по умолчанию: retention 30 дней, настраивается «between 1 and 90
  days»; 500 actions/sec; 10 namespaces на аккаунт
  ([cloud/limits](https://docs.temporal.io/cloud/limits)).
- SLA: «99.9% guarantee against service errors» для одного региона и «99.99%»
  для High Availability namespace ([cloud/sla](https://docs.temporal.io/cloud/sla)).

**Прикидка под нас.** Одна задача петли — это порядка десятков billable
actions (старт workflow, 3–6 Activity на раунд, heartbeat'ы, редкие сигналы).
Сотня задач в месяц — тысячи actions против включённого миллиона. То есть в
Cloud мы платим **минимум $100/месяц за практически нулевое потребление**.
Для одного пользователя с одной петлёй это несопоставимо со стоимостью самой
петли (бюджеты прогонов у нас измеряются единицами долларов — см.
`review_budget_usd`, `total_budget_usd`).

### 4.3 Python SDK

- Пакет `temporalio`, текущая версия **1.31.0**, опубликована **2026-07-29**
  (GitHub Releases репозитория `temporalio/sdk-python`; PyPI-метаданные
  `pypi.org/pypi/temporalio/json`). Релизы выходят регулярно: 1.28.0 (июнь),
  1.29.0, 1.30.0 (июль), 1.31.0. SDK — GA.
- Требование к рантайму: `requires_python >= 3.10`; классификаторы
  перечисляют 3.10–3.14. README: «The Python SDK is built to work with Python
  3.10 and newer».
- Установка: `python -m pip install temporalio`.
- Worker запускается так (README):
  ```python
  worker = Worker(client, task_queue="my-task-queue",
                  workflows=[SayHello], activities=[say_hello])
  await worker.run()
  ```
  Для синхронных Activity дополнительно нужен `activity_executor=<Executor>`.
- Оговорки README, которые стоит знать заранее: «Clients do not work across
  forks»; при мультипроцессном executor'е требуется `shared_state_manager` и
  функции Activity обязаны быть picklable; «The time-skipping test environment
  does not work on ARM. The SDK will try to download the x64 binary on macOS
  for use with the Intel emulator» — на M1 это означает эмуляцию для части
  тестового инструментария (на сам dev-сервер это утверждение не
  распространяется, см. §8).

---

## 5. Ограничения и подводные камни из первоисточников

### 5.1 Размер payload — для нас это горячая точка

Умолчания self-hosted ([defaults](https://docs.temporal.io/self-hosted-guide/defaults)):
предупреждение на **256 KB** («Blob size exceeds limit»), ошибка на **2 MB**
(«ErrBlobSizeExceedsLimit»). В Cloud те же величины: «Payload size (single
request): 2 MB», gRPC-сообщение 4 MB, транзакция Event History 4 MB
([cloud/limits](https://docs.temporal.io/cloud/limits)).

Это прямо задевает наш поток данных. `agents.py:20-29` фиксирует реальный
случай: golden-эталон в 15 894 строки дал промпт в 285 000 токенов — это
порядка мегабайта UTF-8, то есть уже в зоне между warn и error. Если дифф
передавать как вход/выход Activity, он попадёт в Event History и будет
съедать и лимит 2 MB на payload, и лимит 50 MB на историю. Правильная
архитектура под Temporal — гонять через workflow **ссылки** (пути, sha256,
размеры), а тела держать на диске. Наш `condense_diff` с его sha256-отпечатком
(`agents.py:67`) уже наполовину такой; но принцип «единственный источник
правды — event history» при этом не выполняется, и часть durability теряется:
файл на диске Temporal не восстановит.

### 5.2 Версионирование при изменении кода — главный риск для нас

Что происходит при несовместимом изменении: «Using `patched` inserts a marker
into the Event History. During Replay, if a Worker encounters a history with
that marker, it will fail the Workflow task when the Workflow code doesn't
produce the same patch marker»
([python/versioning](https://docs.temporal.io/develop/python/versioning)).

Ошибка недетерминизма **не роняет workflow, а вешает его**: «Only Workflow
exceptions that are Temporal Failures cause the Workflow Execution to fail;
all other exceptions cause the Workflow Task to fail and be retried»; «These
types of failures will cause the Workflow Task to be retried until the
Workflow Execution Timeout, which is unlimited by default»
([references/failures](https://docs.temporal.io/references/failures)).
Это одновременно щадящее поведение (можно починить код и перезапустить
worker) и неприятное (задача молча зависает в бесконечном retry, а наш
инвариант — «терминальный исход обязан попасть в инбокс», `loop.py:619`).

Два штатных пути ([python/versioning](https://docs.temporal.io/develop/python/versioning),
[worker-versioning](https://docs.temporal.io/worker-versioning)):

1. **Patching** — `workflow.patched()`, затем `workflow.deprecate_patch()`,
   затем удаление вызовов после выхода старых executions из retention.
   Трёхшаговый ритуал на каждое изменение формы FSM; при нашей частоте правок
   код зарастает маркерами.
2. **Worker Deployment Versioning** — «tag your Workers and programmatically
   roll them out in versioned deployments, so that old Workers can run old code
   paths and new Workers can run new code paths»; поведения **Pinned**
   («guaranteed to complete on a single Worker Deployment Version») и
   **Auto-Upgrade**. Для одного разработчика на ноутбуке это означает держать
   несколько сборок worker'а живыми — заметный операционный вес.

**Как это ложится на нас.** Точность обязательна, иначе вывод получится
подогнанным. Ломает replay **не любая правка**, а изменение
последовательности вызовов Temporal API. Значит:

- безопасно: менять пороги в `decide()`, состав `SEVERITIES`/`CATEGORIES`,
  тексты промптов, `apply_policies`, `_diagnose`, логику `keep-best` — это
  чистая логика и содержимое payload'ов;
- ломает: добавление/удаление/перестановка шагов — а именно так наш FSM и
  эволюционировал. Подтверждающий раунд (`confirming`, `loop.py:424-434`)
  добавил ветку, где Activity исполнителя **не вызывается**; раунд
  верификации (`agents.py:432-461`) добавил условный второй вызов ревьюера.
  Обе правки — ровно того класса, который требует patching.

И здесь возникает главный узел: **самое ценное для нас свойство Temporal
(задача переживает дни ожидания человека) конфликтует с нашим темпом правок
FSM.** Задача, стоящая в blocked трое суток, за это время переживёт две-три
выкладки петли — и при получении сигнала пойдёт в replay уже под новым кодом.
Обходы — либо patching-дисциплина, либо pinned-версии worker'ов, либо не
держать workflow живым через ожидание человека (то есть отказаться именно от
той функции, ради которой Temporal и брали).

### 5.3 Прочее

- Workflow Task Timeout — 10 секунд по умолчанию: тяжёлые вычисления в
  workflow-коде запрещены не только семантически.
- Идентификаторы — 1 000 байт/символов
  ([cloud/limits](https://docs.temporal.io/cloud/limits), `limit.maxIDLength`).
- Workflows по умолчанию **не** ретраятся, в отличие от Activity: «Unlike
  Activities, Workflow Executions do not retry by default»
  ([retry-policies](https://docs.temporal.io/encyclopedia/retry-policies)).
- Handler'ы сигналов/апдейтов могут быть прерваны завершением workflow;
  рекомендуется `await workflow.wait_condition(workflow.all_handlers_finished)`
  ([python/message-passing](https://docs.temporal.io/develop/python/message-passing)).

---

## 6. Фактчек маркетинговой страницы

Со страницы [temporal.io/solutions/platform-engineering](https://temporal.io/solutions/platform-engineering):

| Утверждение | Статус по первоисточнику |
|---|---|
| «Guarantee all executions of all processes run to completion in spite of failures» | **Подтверждается с оговоркой.** Доки формулируют так же ([understanding-temporal](https://docs.temporal.io/evaluate/understanding-temporal)), но гарантия условна: при ошибке недетерминизма workflow не завершается, а бесконечно ретраит workflow task ([references/failures](https://docs.temporal.io/references/failures)). |
| «99.9999% trailing 30-day uptime» | **Не подтверждается как обязательство.** Контрактный SLA — «99.9%» для одного региона и «99.99%» для HA namespace ([cloud/sla](https://docs.temporal.io/cloud/sla)). Шесть девяток — измеренная статистика, не гарантия. |
| «Temporal Cloud never sees your code or receives sensitive data» | **Опровергается в части данных.** «The Temporal Service persists data from your Workflow Executions, including inputs, outputs, and results», и эти данные хранятся незашифрованными, пока не подключён Payload Codec: «Use a Payload Codec to encrypt payloads before they reach the Temporal Service» ([data-encryption](https://docs.temporal.io/production-deployment/data-encryption)). Про код утверждение верно — worker'ы исполняют его у вас. **Для нас это существенно: наши payload'ы — диффы приватного кода и промпты.** |
| «Empower developers to build 2x faster» | **Не проверяемо.** Первоисточника с методикой измерения не найдено. |
| «Full visibility into every step… structured, queryable execution histories» | **Подтверждается.** Event History + Visibility store, custom search attributes с квотами ([cloud/limits](https://docs.temporal.io/cloud/limits)). |
| «Built with safeguards to support SOC 2 Type II and HIPAA compliance» | **Не проверялось** в рамках этой разведки (нерелевантно нашему контексту). |

---

## 7. Честный вывод

### Что мы получим

1. Возобновление после падения перестаёт быть нашим кодом. `unfinished_steps`,
   `cmd_resume`, `_rescue` (~120–150 строк вместе с тестами) уходят в платформу.
2. Ретраи становятся декларативными и перестают быть россыпью ручных ветвей
   (`attempt == 1`, `terminal == "budget_exhausted"`).
3. Появляется бесплатно то, чего у нас нет: наблюдаемость каждого шага в Web UI,
   принудительная отмена задачи, история как запрашиваемый объект.
4. Ожидание человека выражается языком платформы (signal + `wait_condition`),
   а не «задача лежит в blocked, ждём `swarm answer`».

### Что мы потеряем

1. **Ручную правку состояния.** ADR-001 объявляет jsonl первичным, и человек
   читает и правит `.swarm/tasks.json` напрямую. Event History так не правится.
2. **Простоту запуска.** Сейчас `swarm run` — один процесс, который завершается.
   С Temporal нужны: живой сервер (+БД), живой worker-демон и клиент. На
   ноутбуке, который засыпает, это новый класс проблем.
3. **Свободу менять FSM.** Еженедельные правки формы автомата становятся
   операцией с ритуалом (patching либо версии worker'ов) — см. §5.2.
4. **Локальность данных.** Диффы и промпты либо едут в Event History (упираясь
   в 2 MB и 50 MB), либо остаются на диске — и тогда durability на них не
   распространяется.
5. Деньги или время: Cloud — от $100/месяц при нулевом потреблении;
   self-hosted — операционная нагрузка, которую Temporal сам описывает как
   «significant engineering and ongoing effort».

### Есть ли смысл при нашем масштабе

**Нет — миграция не оправдана.** Обоснование в трёх пунктах, все опираются на
факты выше:

1. **Соотношение выигрыша к переработке отрицательное.** Temporal закрывает
   два наших механизма из восьми (§1). Остальное — гейт, scope-check,
   валидация вердикта, политики, инбокс, диагностика несходимости, keep-best,
   вся работа с git, экономика промптов — не имеет в Temporal аналога и
   переносится один в один. При этом весь эффектный код (`~400 строк`
   subprocess/файловых операций) обязан быть перепакован в Activity ради
   sandbox'а (§2.2).
2. **Наш профиль нагрузки лежит вне зоны, где Temporal окупается.** Одна петля,
   один пользователь, задачи 10–30 минут, десятки actions на задачу против
   миллиона включённых. Мы платили бы минимальный тариф за неиспользуемую
   мощность либо администрировали бы кластер ради одного процесса.
3. **Главная ценность конфликтует с главным риском.** Durability по-настоящему
   нужна нам ровно там, где задача ждёт человека сутками, — и именно там
   еженедельные правки FSM превращаются в риск replay-недетерминизма (§5.2).

### Что стоит забрать, не мигрируя

Три идеи Temporal применимы к нашему коду напрямую и почти бесплатно:

1. **Явное разделение «чистое решение» / «эффект».** У нас оно уже наполовину
   есть (`decide()` — чистая функция), но `Loop.run_task` мешает решения и
   побочные эффекты. Доведение до конца даёт то же свойство тестируемости, что
   даёт Temporal replay-тестирование, — без Temporal.
2. **Декларативная политика ретраев на шаг** вместо ручных ветвей: набор
   `(max_attempts, non_retryable_errors)` на каждый шаг FSM. Это ровно наша
   формулировка «невалидный вердикт = один повтор, бюджетный обрыв = без
   повтора», но вынесенная из тела `agents.review` в конфигурацию шага.
3. **Обязательный heartbeat вместо `silence_timeout`.** `driver.py` уже
   отслеживает молчание потока; сделать из этого явный «прогресс шага» с
   записью в журнал — дешёвый способ отличить «агент думает» от «агент завис»
   при разборе аварий.

### Когда вернуться к вопросу

Пересмотреть решение имеет смысл, если изменится хотя бы одно из:
несколько параллельных петель на разных репозиториях; исполнение переезжает с
ноутбука на постоянно работающий хост; появляется второй пользователь и
потребность видеть чужие прогоны; либо задачи начинают жить неделями с
десятками точек ожидания человека. Первые три ослабляют аргумент масштаба,
четвёртый делает durability доминирующим требованием.

---

## 8. Что осталось непроверенным

1. **PostgreSQL 18.** В списке поддерживаемых версий верхняя — 16.6
   ([persistence](https://docs.temporal.io/temporal-service/persistence)).
   Работает ли Temporal Server с нашим нативным PG 18 — первоисточником не
   подтверждено ни в одну сторону.
2. **Пригодность `temporal server start-dev --db-filename` для постоянного
   локального использования.** Доки говорят только «not intended for production
   use» и «It skips certain HTTP security checks»
   ([cli/server](https://docs.temporal.io/cli/server)); есть ли иные
   ограничения (миграции схемы, сохранность при обновлении CLI) — не найдено.
3. **Работа dev-сервера и самого worker'а нативно на arm64 macOS.** README
   упоминает отсутствие ARM-сборки только для **time-skipping test
   environment**; про основной серверный бинарь и рантайм worker'а
   утверждений не найдено. Считать это подтверждением работоспособности на M1
   нельзя.
4. **Формула троттлинга heartbeat'ов** (`heartbeatTimeout * 0.8` либо
   `defaultHeartbeatThrottleInterval`). Встречена только в сводке поисковой
   выдачи по `docs.temporal.io`; дословно на открытой странице не подтверждена.
   Практический смысл — heartbeat'ы не всегда долетают до сервиса, и их частота
   не равна частоте billable actions.
5. **Статус GA у Worker Deployment Versioning**, минимальные версии сервера и
   SDK для него. Страница [worker-versioning](https://docs.temporal.io/worker-versioning)
   этого не указывает; отдельная страница production-развёртывания не читалась.
6. **Ресурсный след self-hosted сервера** (RAM/CPU) — критично при 8 ГБ на
   MacBook M1. Первоисточника с цифрами не найдено; production-checklist
   говорит о масштабировании, но не о нижней границе.
7. **Точный биллинг при нулевом потреблении.** Формулировка «greater of
   $100/month or 5% of usage» ([cloud/pricing](https://docs.temporal.io/cloud/pricing))
   читается как безусловный минимум, но прямого утверждения «месяц без единого
   action всё равно стоит $100» в доках не найдено.
8. **Сколько событий Event History порождает один наш раунд.** Оценка «единицы-
   десятки» — расчётная (по списку billable actions), измерением не
   подтверждена. Для вывода о лимитах это некритично (запас до 51 200 велик),
   но цифра в §3 — прикидка, а не факт.
9. **Официальная позиция Temporal о том, когда их платформа избыточна.**
   Целенаправленный поиск по `docs.temporal.io` и `temporal.io` такого
   документа не дал; вывод §7 построен на наших фактах, а не на их
   антирекомендациях.
10. **Стоимость переработки в человеко-часах.** Оценка «~400 строк эффектного
    кода в Activity» получена чтением `loop.py`/`state.py`/`agents.py`;
    прототип не строился, и реальная трудоёмкость не измерена.
