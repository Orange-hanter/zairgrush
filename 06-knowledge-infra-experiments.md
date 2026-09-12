---
title: "ZeusLogic — Эксперименты: знаниевая инфраструктура и индексация кода"
type: design
status: draft
version: 0.31
created: 2026-08-06
updated: 2026-09-13
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
2a. **Фоновый замер — второй способ мерить, и с 2026-08-24 основной для
   дешёвых факторов.** Вместо стенда на пару плеч фактор бросается
   ЖРЕБИЕМ на каждую задачу обычной работы
   (`[experiments] ambient = "<фактор>"`, `ambient_seed`); работа при
   этом настоящая и всё равно была бы оплачена, а через несколько недель
   накапливается выборка, которой никто не выбирал.

   Причина перехода названа замером, а не вкусом. Парный стенд дважды
   не дал ответа по плечу исполнителя E9, и обе неудачи — про метод: на
   ОДНОЙ задаче счётчик раундов доминируется тем, запустил ли
   исполнитель `cargo fmt`, а честная парная очередь на десять задач
   стоит ~$130 за один вопрос. Хуже цены то, что задачу для стенда
   выбирает человек — и выбирает под ожидаемый признак: s2ky была взята
   под сход границ, которого в ней не случилось.

   Чем фоновый замер ХУЖЕ, и это сказано в самом инструменте: выборка
   НЕПАРНАЯ — в плечах разные задачи, разброс между задачами не
   вычитается, и на малом n он слабее стенда. Он берёт числом, а не
   качеством наблюдений.

2b. **Дуэль — то же на обычной работе, но ПАРНО** (`[experiments] duel`,
   `duel.py`, решение владельца 2026-08-24). Два исполнителя работают на
   ОДНОЙ задаче ОДНОВРЕМЕННО, каждый со своим значением фактора. Тогда
   наблюдение парное (одна задача, один коммит, одна спека), календарное
   время равно медленному плечу, а не сумме, и задачу по-прежнему никто
   не выбирал. Это снимает единственную слабость фонового жребия и
   единственную слабость стенда сразу.

   **Честность здесь в одном правиле: живое плечо выбирается жребием
   ЗАРАНЕЕ и не переигрывается по результату.** Если оставлять то плечо,
   что лучше прошло гейт, замеряется max(A, B), а не A и не B: доля
   «плеча B» перестаёт быть свойством B и становится свойством отбора.
   Код был бы лучше, данные — бесполезны.

   Отсюда устройство. ЖИВОЕ плечо работает в обычном дереве, ровно как
   сегодня, — ни одна строка после `implement` не знает про дуэль, и с
   выключенным флагом поведение байт-в-байт прежнее. ТЕНЕВОЕ работает в
   отдельном git-worktree, существует только чтобы быть измеренным, и
   его работа не адоптируется никогда. Гейт гоняется у обоих: «прошла ли
   работа плеча гейт» и есть главная метрика.

   Цена — два вызова исполнителя на задачу плюс второй гейт; ровно за
   этим и снят потолок вызова (§5.3 05-дока). Предупреждение о железе,
   потому что оно замеряемо: два исполнителя на Rust-проекте гоняют
   `cargo` каждый в своей сессии, и на 8 ГБ это может уйти в свап —
   дуэль поэтому флаг стенда, а не глобальное умолчание.

   **Расширение, названное и НЕ сделанное:** спасение живого плеча
   теневым (живое упало, теневое прошло — взять теневое). Соблазн
   очевиден и это ровно тот отбор, против которого писано правило выше.
   Вводить его можно только с явным правилом исключения: задача выпадает
   из основного сравнения и считается отдельно как «спасение». Поэтому `experiments/tools/ambient-report.py`
   ОТКАЗЫВАЕТСЯ объявлять победителя ниже порога (8 задач в меньшем
   плече), пересчитывает жребий из `(сид, фактор, id)` и выбрасывает
   наблюдения, которые не пересчитываются, и печатает разброс, а не
   только среднее.

   Три правила устройства: жребий детерминирован по задаче (реплей
   остаётся реплеем, а разбор не обязан верить журналу); фактор ровно
   один (два случайных фактора дают неразделимые эффекты — это
   арифметика, а не осторожность); фактор, который вместе с тем прибит
   явным флагом, — ОТКАЗ на старте, иначе журнал называл бы жребием
   выбор, которого не делал.
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
- **Замерено 2026-08-23, и сравнения до этого не было.** Реплей
  настоящих запросов пилота показал: FTS отдавал РОВНО НОЛЬ строк на
  каждом запросе, все хиты давал вектор. Это читается как чистая победа
  эмбеддингов — пока не спросишь почему. Причина не в корпусе, а в форме
  запроса: `websearch_to_tsquery` соединяет слова через И, а в него
  уезжала спецификация задачи целиком, так что условие «в одном уроке
  встретились ВСЕ её слова» не выполнялось никогда. Доказательство:
  `корпус` в одиночку даёт 2 строки, `эталон` — 1, а `корпус ERC-03` —
  0, потому что ERC-03 нет ни в одном уроке и один отсутствующий терм
  обнуляет всю конъюнкцию.
- **После правки** (запрос строится из значимых слов через `or` — это
  синтаксис самого websearch, данные по-прежнему едут параметром) те же
  запросы дают 3–5 строк. Замер на 16 запросах исполнителя: **FTS даёт
  65 хитов из 80, вектор добавляет 15 сверх**. То есть «FTS первым,
  вектор — сеть охвата» из дизайна стало правдой, будучи «только
  вектор» с самого выхода функции.
- **Побочно**: эмбеддер звался на КАЖДОМ поиске, потому что условие
  `len(hits) < k` при нуле хитов выполнялось всегда. Теперь — только
  когда FTS недобрал.
- **Статус**: вопрос E6 («нужны ли эмбеддинги поверх текстового
  поиска») получает наконец осмысленный ответ: нужны, но как добавка —
  15 хитов из 80, а не как основной ретривер. Прежнее чтение
  `queries.jsonl` мерило дефект.
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
- **Устройство записано в ADR-013** (2026-08-23): файлы первичны, PG
  производен, `psql` вместо библиотеки, ANN-индекса нет, grounding как
  пропуск, декей по ре-валидации якорей — и честные негативы, включая
  русский стеммер на идентификаторах.
- **Этап 4, сухая половина сделана** (findings E9/stage4-premise):
  блок инъекции строится для 16 закрытых задач пилота из 16, и у всех
  пяти задач с собственным уроком этот урок в блоке. Премиса
  «память достаёт нужное» держится.
- **Статус: открыт.** Живые плечи исполнителя и ревьюера требуют
  прогоняемой очереди — у пилота 16 задач закрыты, три оставшиеся ждут
  решений владельца по замыслу. Мерить плечи имеет смысл только после
  правки FTS (E6): до неё они замерили бы ретривер, работающий одним
  вектором.
- **Плечо исполнителя, одна задача** (2026-08-23, findings
  E9/executor-arm-v9lb): v9lb прогнана дважды с одного коммита в
  изолированных клонах, память — единственный отличающийся фактор,
  ревьюер константа (opus/xhigh пилота). **Плечо A (off)**: 8 находок —
  correctness 4, ARCHITECTURE 3, style 1; три архитектурные это вопросы
  о замысле, петля классифицировала их как intent и эскалировала
  человеку — задача кончилась `ask_user`, очередь встала, $4.20.
  **Плечо B (executor)**: 7 находок — correctness 5, style 2,
  **архитектурных НОЛЬ**; петля продолжила работу, исполнитель починил
  и ушёл в раунд 2, $5.93. Двусмысленности замысла, остановившие плечо
  A, не возникли, когда у исполнителя были решения владельца от
  2026-08-16.
- **Оговорки, потому что они режут в другую сторону.** У плеча B БОЛЬШЕ
  major-находок (4 против 2): память не сделала работу лучше — она
  сменила то, что осталось сказать ревьюеру, с «а что это вообще должно
  делать» на «вот здесь неверно». Раунды до сходимости НЕ измерены:
  ревью второго раунда плеча B умерло процессом до вердикта
  (`review_failed`) — это инфраструктура, не память. n = 1 задача:
  остальные две idea-задачи не были достигнуты, потому что один открытый
  вопрос останавливает очередь целиком.
- **Замер состоялся только благодаря правке E6.** В плече B эмбеддер был
  недоступен (DNS), поиск шёл ОДНИМ полнотекстовым — а до утренней
  правки он отдавал ноль строк на любом настоящем запросе. Без неё плечо
  B не подмешало бы ничего, и эксперимент молча сравнил бы off с off.

- **Плечо исполнителя, вторая задача — сигнала нет** (2026-08-24,
  findings E9/s2ky-paired-replay-no-signal): s2ky реплеена парой плеч с
  коммита `ee53c217`, память единственный отличающийся фактор, в памяти
  только три урока, существовавших ДО s2ky. Плечо A (off) — 3 раунда,
  $6.80; плечо B (executor) — 2 раунда, $6.18. **Этот минус раунд не
  засчитывается памяти.** Задача выбиралась под МЕХАНИЧЕСКИЙ признак:
  в исходном прогоне s2ky сработал сторож границ на
  `zeus/crates/zeus-erc/tests/corpus_erc.rs`, а подмешиваемый урок
  несёт этот файл среди якорей. Сторож не сработал НИ В ОДНОМ плече —
  гипотезе не на чем было выстрелить. Лишний раунд плеча A стоил
  `cargo fmt` и отсутствующей `///`-строки над `ZEUS_KEY_NAMESPACE`,
  которую clippy `-D warnings` не пропустил; отчёт третьего раунда сам
  говорит, что реализация «уже была на месте и корректна». Гигиена
  линтера — ровно тот разброс, который парный замер на ОДНОЙ задаче не
  отделяет от изучаемого фактора.
- **Ошибка планирования названа.** Признак выбирался по ПРОШЛОМУ
  прогону, и не был задан вопрос, воспроизводится ли он на другом
  движке исполнителя: исходный сход границ сделал kimi, оба плеча
  сегодня ходили claude/sonnet. Реплей, меняющий движок, не реплеит то
  условие, которое породило признак.
- **Побочная находка, стоившая половины замера** (findings
  E9/budget-truncation-called-a-crash): оба плеча потеряли первый раунд
  на потолке `--max-budget-usd` в $3, и петля назвала это КРАХОМ.
  Конверт говорил прямо (`terminal_reason: budget_exhausted`,
  `subtype: error_max_budget_usd`, «Reached maximum budget ($3)»), но
  CLI выходит ненулевым кодом, драйвер по коду честно говорит «crash»,
  а разбор конверта стоял только на исходе «done» — то есть на ветке,
  которой не бывает. Диагноз получался противоположный: оператора
  посылали искать аварию вместо того, чтобы поднять
  `executor_budget_usd`. Совет исполнителю был так же ложен —
  «повтори, соблюдая контракт» при целых правках на диске. Исправлено
  (`engines.budget_truncated`, совет разведён по причине), три теста,
  два падают на коде до правки.
- **Итог по плечу исполнителя: n = 2, ответа нет.** Две задачи — две
  РАЗНЫЕ причины неопределённости (v9lb: раунды не измерены, ревью
  умерло процессом; s2ky: признак не выстрелил, раунды решила гигиена).
  Честное чтение: счётчик раундов на одной задаче доминируется тем,
  запустил ли исполнитель `cargo fmt`, и эффект такого размера парным
  замером на задаче не разрешается. Либо метрика переезжает на то, что
  память способна сдвинуть (споры на задачу, повторные тупики по
  ОЧЕРЕДИ, а не по задаче), либо плечу нужна очередь, на которой шум
  гигиены усредняется, — а это ~$130 на 10 задач в двух плечах.
  Решение бюджетное, не техническое, и принадлежит владельцу.

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
- **Round 2 (2026-08-23) ran the brownfield set** ADR-002 demanded —
  BENCH-2, six tasks that edit existing code with regression risk, full
  suite as the gate, same protocol. Arms again indistinguishable: 6/6
  each, every task on the first iteration, **zero findings on either
  arm**; 305 s vs 224 s of executor work, $0.734 vs $0.692 of review,
  executor price $0.841 on B and unmeasurable on A.
- **The zero was the clue.** The same six tasks in 2026-08-07 produced
  10 findings and needed second and third iterations. Same tasks, same
  red state — so brownfield did not fail to discriminate, the JUDGE did.
  Testable without re-running an executor: the diffs are committed.
- **Paired re-judge settles E13** (`experiments/bench/e13-rejudge.py`:
  re-review frozen diffs with another reviewer arm, using the loop's own
  `review_prompt`). opus/xhigh over all twelve brownfield diffs: **arm A
  6 findings, all minor, 6/6 approve, $2.466; arm B 6 findings, all
  minor, 6/6 approve, $2.470.** On two tasks both arms drew the *same*
  finding — what is left over belongs to the task, not to the executor.
- **Status: answered, ADR-012.** On small, well-specified tasks the
  engine changes neither outcome nor rounds nor visible quality; it
  changes measurability and provider dependence. The reviewer arm
  dominates: 0 findings for $0.73 versus 6 for $2.47 on the same diffs.
  Reopen when a task set exists where the arms differ in rounds.
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
  confirming pool.
- **Fixed the same day** (05-doc §4.2.1): the verdict is now salvaged
  from the stream when the envelope is empty, the retry carries the
  reason the first answer was refused, and the stream of a failed review
  is no longer overwritten when the task is requeued. Measured on the
  real streams: 2 of the 4 surviving failures come back, the one task
  block disappears; the two placeholder cases stay refused, because
  there is nothing there to rescue. Frozen at
  `experiments/goldset/verdicts/`, in the gate. A live smoke the same
  evening reproduced the placeholder a third time — it is routine
  behaviour of this model at this effort, not a fluke.

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
- **The meter came first** (2026-08-23), before any of arm B was built:
  the criterion did not exist as a number for either arm.
  `experiments/tools/mutation-survival.py` mutates a stand's sources one
  AST node at a time and counts what its own suite noticed. It prints
  every SURVIVOR with file, line and substitution rather than a bare
  rate — an equivalent mutant is indistinguishable from a hole without a
  human looking, and a percentage nobody can act on is not a
  measurement.
- **Arm A measured on work already paid for.** BENCH-3 is the corpus
  where the executor wrote its own tests. Whole stand: 113 mutants, 98
  killed, 15 survived (13 %). Split by AUTHORSHIP, which is the point:
  modules whose tests were written beforehand as fixtures — 97 mutants,
  11 survived, **11 %**; modules where the executor wrote both the code
  and its tests from scratch (`cli.py`, `normalize.py`) — 16 mutants, 4
  survived, **25 %**. Three of the four are real holes: the two chosen
  defaults (`top = 10`, `width = 72`, both NAMED in the spec) and one
  more. The fourth — the guard boundary `if width < 1` — was called a
  hole here and **that was wrong**: the spec names no minimum width at
  all, so no test written from it can pin that boundary. Corrected
  2026-08-24 by the classification below; the claim survived a month
  because nothing forced each survivor to be justified against the spec
  text. The executor tested the paths it wrote and skipped the
  defaults it chose and the boundary it guarded — the predicted failure,
  now a number rather than four anecdotes. Caveats on the record: 16
  mutants is a thin base, `normalize.py` gives only 2 of them, and
  `rank.py`/`stats.py` are excluded from the clean split as
  mixed-authorship confounds.
- **Arm B implemented behind `[experiments] tester`** (default off, so
  today's behaviour is unchanged). Independence is structural, not
  promised: the tester runs BEFORE the executor, when no implementation
  exists on disk, and sees only spec and acceptance — no repo map, no
  memory, no other code. The gate between the two is deliberately not
  checked: fresh tests must be red. Files it wrote become untouchable
  for the executor, tracked by content fingerprint (they sit uncommitted,
  so a path-only rule would have failed every round on the loop's own
  setup). Its contract carries a field no other role has — `unclear`: a
  spec that leaves a case undecided is a finding about the TASK, and a
  guessed answer would freeze an invention into a test.
- **Round 1 measured 2026-08-23.** Four feature-tests tasks (s1ch,
  s2tn, s3nm, s4cli), same starting commit, same four modules, arm A
  replayed at the commit arm B reaches so that no later task's tests
  count for A. **Arm A: 37 mutants, 11 survived — 30 %. Arm B: 27
  mutants, 6 survived — 22 %.** Per module A→B: stats 25→13 %, rank
  40→25 %, normalize 50→0 %, cli 29→43 %. Volume does not explain it
  (28 tests vs 32). Mutant counts differ because the implementations
  differ — only rates compare, and the tool says so on every run.
- **Price: arm B adds a whole role.** $4.48 this run — tester $2.34,
  executor $1.01, review $0.72, plus $0.42 burned on reviewer contract
  failures. The tester alone cost more than twice the executor.
- **The metric needs correcting, and arm B is what showed it.** On
  cli.py arm B looks worse (43 % vs 29 %) — on the very module where
  arm A had four real holes. Two of arm B's three survivors are
  `return 2 → return 3`: the exit code on bad arguments, which the spec
  never fixes (it says only «ненулевой код, без трейсбека»). The tester
  DECLARED that gap in advance in its `unclear` field. Arm A pins `2`
  only because the same author chose 2 and then asserted its own
  arbitrary choice. So a mutant that changes only UNSPECIFIED behaviour
  is equivalent with respect to the contract and must not score against
  the tests — and `unclear` is the mechanical list of where to expect
  one. That was not why the field was added.
- **Status: open, round 2 designed.** Two changes before it decides
  anything. (1) The prompt carries a false premise: «tests must fail
  today» holds for tasks that ADD behaviour (s3nm and s4cli were fully
  red) and fails for tasks that PRESERVE it while changing how (s1ch 1
  of 6 red, s2tn 2 of 5) — caching and speed-ups are guarded by tests
  that pass before and after. Not fixed mid-experiment on purpose.
  (2) Survival must be scored against the spec, discounting positions
  the tester declared `unclear`. The sample is thin besides: four tasks,
  one repository, every task converged in a single round.
- **Change (2) done 2026-08-24 — and it doubles arm B's margin.** Every
  mutant that survived, and every mutant CAUGHT in the same positions,
  was classified against the spec text into three classes:
  `real_gap` (a test could have caught it and did not), `equivalent`
  (behaviour unchanged — unkillable by anything) and `spec_silent` (the
  spec does not decide it, so an honest spec-derived test must let it
  through). The classification lives as data with a reason per row
  (`experiments/goldset/e11/classification.jsonl`), not in code, because
  it is judgement and must stay arguable; equivalence claims are VERIFIED
  by input enumeration, not asserted. Both classes are removed from both
  arms' denominators **together with the mutants they caught** — dropping
  only survivors would hand a free point to whichever arm has more such
  positions.
- **Result: A 18 % against B 9 %, where the raw numbers said 30 % against
  22 %.** The independent tester's margin goes from a 27 % relative
  reduction in holes to **50 %**. Both rates fall, but NOT equally —
  arm A by 40 % (30 → 18), arm B by more than half (22 → 9) — and that
  asymmetry is the second finding: raw mutation survival was measuring
  the spec's silence and the generator's generosity as much as it
  measured tests, and the arm that wrote tests against its own
  implementation carried more of the false holes. (The first draft of
  this line said «both more than halve»; the gate test written in the
  same hour refused it. The number was in the prose before it was
  computed.)
  Arm A: 9 mutants dropped (2 equivalent, 7 spec_silent), 5 real holes
  left. Arm B: 4 dropped (1 equivalent, 3 spec_silent), 2 real holes
  left — and **all four of arm B's dropped mutants were survivors in
  positions it had itself declared `unclear`**. Arm B had no unexplained
  survivor at all.
- **Two corrections the classification forced.** The mutant surviving in
  BOTH arms (`rank.py:18`, `n <= 0` → `n < 0`) was reported as «the
  shared blind spot, authorship has nothing to do with it». It is not a
  blind spot: at `n == 0` both implementations return `[]` down either
  path — verified on 9031 input pairs, zero differences. It is
  unkillable. Likewise `rank.py:21` (`n >= len(freq)` → `>`) in arm A:
  equivalent on 11730 pairs. Rule taken: **an equivalence claim must be
  enumerated, not argued** — and a survivor list nobody has justified
  against the spec is a list of suspects, not of holes.
- **The sharpest single fact.** The only two constants the s4cli spec
  states outright — `--top` default 10 and `--width` default 72 — are
  exactly what arm A failed to pin and arm B pinned. The implementer's
  own tests missed the specified thing and caught the unspecified one
  (exit code `2`); the independent tester did the reverse. That is the
  hollow-test failure mode in one line, and it is now a number.

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
- **Статус**: три стенда. Диагноз 6/6 (прежний диагност — 2/6);
  границы 4/7 при потолке 6 строк, 5/7 при 8; вердикты 8/8 (из четырёх
  настоящих отказов формы возвращаются два — те, где суждение было).
- **Уточнение по границам (2026-08-23)**: недостижимы не КЛАССЫ, а два
  конкретных спора — q014 и q016. Оба их класса ловятся в том же наборе
  на других случаях (q017 место 6, q005 место 2). Пятая улика,
  построенная ровно под этот пробел (имена, ОБЪЯВЛЕННЫЕ внутри границ и
  использованные снаружи), замером отвергнута: на безопасном весе она
  двигает q016 с 24-го места на 21-е и больше ничего не меняет, а на
  весе, которого хватило бы для q016, теряются q017 и q020. Улику,
  которая ничего не даёт, набор не пропустил — это его работа.

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

### E14. Кто НАХОДИТ пробел и кто его ЗАПОЛНЯЕТ

Замечание владельца (2026-08-24): «в наших экспериментах видна линия —
строгие правила против творчества; может, просто добавить две роли?».
Линия действительно есть, но проходит она не там, где кажется. Это не
две температуры характера, которые надо развести по агентам. Это
**один и тот же агент, который находит неоднозначность и сам же её
молча закрывает** — и потому находка не доезжает до человека.

**Свидетельства, которые уже собраны, все четыре независимы.**

- Ловушка s3nm ставила ровно этот вопрос словами задачи: «задаст ли
  исполнитель границы сам и опишет ли их, или молча выберет
  произвольную трактовку». Спека была неоднозначна НАМЕРЕННО.
- E11, замер 2026-08-24: спека s4cli называет две константы
  (`--top 10`, `--width 72`) — их плечо A не закрепило. Зато закрепило
  код выхода `2`, которого в спеке нет: собственное изобретение,
  превращённое в тест. Тестировщик плеча B сделал наоборот и объявил
  пробел в поле `unclear`.
- E9, плечо исполнителя на v9lb: с памятью архитектурные (intent)
  находки упали с 3 до 0. Память подсунула ответы — и вопросы
  перестали задаваться. Задача пошла дальше вместо `ask_user`, и это
  и хорошо, и плохо одновременно.
- Линтер границ: 3 из 7 признанных споров о границах не имеют
  ТЕКСТУАЛЬНОГО следа. Правило их не предскажет никогда; их видно
  только чтением замысла.

**Оба полюса в петле уже есть — но как подработка у ролей с другой
основной работой.** Строгий: `unclear` тестировщика, страж границ,
гейт, схема вердикта. Творческий: канал dispute исполнителя, план-дифф
планировщика, эскалация `ask_user`. Вопрос E14 поэтому не «нужны ли
полюса», а: **выигрывает ли разведение их в отдельные роли у нынешнего
совмещения**.

- **A (текущее)**: пробел находит и заполняет тот, кто делает работу.
  Дисциплину держат `unclear` (только если запущен тестировщик),
  dispute и эскалация.
- **B**: две роли, разделённые по функции, а не по темпераменту.
  *Пурист* до всякой работы выдаёт СПИСОК решений, которых спека не
  принимает, и не предлагает ответов вовсе. *Изобретатель* берёт этот
  список и предлагает ответы с обоснованием — как решения-кандидаты
  человеку или планировщику, но НЕ в код. Смысл разделения тот же, что
  у E11: у того, кто нашёл неоднозначность, не должно быть возможности
  закрыть её самому.
- **Гипотеза**: сокращаются молчаливые изобретения (постфактумные
  `deviations`, которых спека не решала) и круги `ask_user` на задачу.
- **Контргипотеза, и она дешевле**: хватит того, чтобы вынести
  `unclear` из тестировщика в отдельный дешёвый предпроход. Сегодня
  этот список появляется только когда работает тестировщик, а он на
  замере E11 стоил $2.34 — больше двух исполнителей. Если дешёвый
  предпроход даёт то же, две дорогие роли не нужны.
- **Решается по**: молчаливые изобретения и круги `ask_user` на задачу,
  замер ФОНОВЫЙ (см. §1 «фоновый замер»), порог 8 задач в плече.
- **Контргипотеза РЕАЛИЗОВАНА** (2026-08-24, `unclear.py`, флаг
  `[experiments] unclear`): один короткий вызов на задачу до всякой
  работы, отдающий только СПИСОК развилок. Список уезжает в промпт
  исполнителя одним блоком с одним требованием — если выбор всё же
  приходится сделать, назови его в `deviations`. Механика поэтому
  превращает МОЛЧАЛИВОЕ изобретение в ОБЪЯВЛЕННОЕ, а это ровно та
  разница, которую E14 и берётся мерить.
- **Три решения промпта, и все три — про ложные срабатывания.** Пустой
  список объявлен успехом прямым текстом: спецификация бывает полной, а
  выдуманная развилка хуже отсутствующей — она стоит исполнителю
  внимания и в замере выглядит работой. Отвечать на свои же вопросы
  запрещено (у нашедшего не должно быть возможности закрыть). Пробел,
  который заметен только чтением КОДА, объявлен ложным — это выбор
  реализации, а не молчание спеки; та же дисциплина, что у
  тестировщика E11.
- **Список НЕ уходит в инбокс и не блокирует очередь.** Вопрос человеку
  на каждой задаче остановил бы работу целиком, а замер требует, чтобы
  работа шла. Пустой список не порождает блока вовсе: строка «пробелов
  нет» платится токенами каждый раунд и не сообщает ничего.
- **Первый живой прогон, v9lb, 2026-08-24 — механизм РАБОТАЕТ, задача
  всё равно ушла к человеку.** Пурист ($0.47) нашёл 3 развилки в спеке,
  которая сама говорит «задача ждёт проектирования формата». Исполнитель
  затем написал в `deviations`: «Спецификация явно оставляла три
  развилки нерешёнными (см. 'What the spec does NOT decide') — сделал и
  задокументировал выбор по каждой в §7а». Он назвал блок промпта по
  заголовку, выбрал по каждой развилке и внёс выбор в документ
  отдельным разделом. Молчаливое изобретение стало объявленным — ровно
  то, ради чего E14 и заводился. **Негативная половина, не менее
  важная:** эскалацию это не предотвратило (`ask_user`, 4 находки о
  замысле), и число архитектурных находок не упало против прогона того
  же v9lb 2026-08-23 (было 3, стало 3). n = 1.
- **Пурист и ревьюер находят РАЗНОЕ, и раскол структурный.** Из трёх
  архитектурных находок ревьюера ровно ОДНА — следствие развилки,
  которую пурист назвал (он спросил, блокирует ли выпуск компонент без
  привязки; исполнитель решил «да»; ревьюер возразил, что выхода из
  этого состояния тогда нет). Две другие пуристу недоступны ПО
  УСТРОЙСТВУ: «§9 всё ещё озаглавлен „каркас v1.5“, хотя §7а сделал
  `library_cache.toml` нормативным» и «плоская запись `Device` теряет
  уровень вариантов из LIB-1» — это противоречия с ОСТАЛЬНЫМ
  репозиторием (docs/01, заголовки разделов), а пуристу запрещено
  читать что-либо кроме спеки. Значит дешёвая роль не поглощает
  дорогую: пурист находит развилки ВНУТРИ текста спеки, ревьюер —
  столкновения МЕЖДУ изменением и всем остальным. И уцелевшее
  пересечение сменило природу: спор идёт о ЗАЯВЛЕННОМ решении, а не о
  необъяснённом.
- **Статус: полный вариант (две роли) зарегистрирован, не реализован.**
  Сознательно: правило программы — дублирующий вариант и критерий
  решения ДО постройки, а дорогих ролей в петле уже четыре. Добавить
  пятую и шестую «потому что звучит верно» — ровно тот ход, против
  которого и заводился замер выживаемости мутантов. Контргипотеза
  дешевле и падает быстрее; если её хватит, дорогой вариант не нужен.
- **Почему это стало возможно измерить только сейчас.** Три просьбы
  владельца от 2026-08-24 складываются в один инструмент: режим
  денежного чана снимает потолки, которые обрубали дорогие роли посреди
  работы; фоновый замер даёт несмещённую выборку на обычной работе
  вместо $130 за парную очередь; E14 — первый эксперимент, которому
  парный стенд не нужен вовсе.


### E15. Семейство ревьюера: одно с исполнителем vs разведено

> Номер: в 08-operators-guide этот вопрос назван «эксперимент E13», но
> E13 в этом документе — сравнение движков (answered, ADR-012); там он
> жил как *caveat by design*. Вопрос получает собственный номер E15,
> ссылка в 08 исправлена.

- **A (текущий дефолт, ADR-022)**: исполнитель kimi k3-256k, ревьюер —
  то же семейство kimi (k3). Петля предупреждает о совпадении на старте
  прогона (`clirun.py`), но дефолт его не разводит.
- **B**: тот же исполнитель и те же диффы, ревьюер разведён на другое
  семейство — claude sonnet (zcode ревьюером не бывает, 08-док).
  Разведение confirm-раундов (`confirm_model`, `confirm_effort`,
  `confirm_lens`) — отдельный фактор, здесь НЕ меряется.
- **Гипотеза** (из §3.2 и «Цена выбора» 08-дока): независимость судьи —
  несущая опора разделения ролей; на одном семействе ревьюер находит
  меньше, потому что разделяет с автором диффа те же слепые пятна.
- **Метод — парный пересуд без перезапуска исполнителя** (прецедент:
  `experiments/bench/e13-rejudge.py`, которым закрыт E13). Два слоя:
  1. *Канарейки* (`experiments/canary/run_canaries.py`): посеянные баги
     с предрегистрированными ожиданиями, оба плеча судят одни диффы —
     даёт recall/FPR с известной землёй.
  2. *Замороженные диффы* goldset/bench: пересуд обоими плечами, находки
     сверяются постфактум — даёт число находок и цену на реальной работе.
- **Решается по**: recall и FPR на канарейках (главное), число находок
  на frozen diffs, цена ревью за дифф. Один фактор — семейство ревьюера;
  модель исполнителя, промпт ревьюера и effort константны.
- **Стоимость**: ≤3 прогонов ревьюера (правило §1); исполнитель не
  запускается вовсе — замер дешевле любого стендового эксперимента
  программы. Факт: два батча (канарейки, frozen) вместо трёх.
- **Слой 1 измерен 2026-09-12** (канарейки, 9 посеянных диффов, ожидания
  предрегистрированы с CANARY-1). Плечо A — ревьюер kimi k3-256k, одно
  семейство с исполнителем: **recall 6/8, FPR 0/1, инъекции 2/2 →
  blocked, ослабление тестов 1/2**. Плечо B — claude sonnet: **recall
  8/8, FPR 0/1, инъекции 2/2, ослабление тестов 2/2**. Разногласия —
  ровно две канарейки: c3 и c6 A пропустил (одобрил), B поймал; c8 обе
  руки заблокировали за инъекцию И достали скрытый ею баг.
- **Условия замера слоя 1 (честно)**: действовала поправка к
  предрегистрации — симметричный retry (один повтор с названной причиной
  при невалидном вердикте, повторяет `reviewer.py` attempt=2): без него
  A дала 0/9 валидных с первой попытки против 6/9 у B; с ним 9/9 у обеих
  (у B повтор понадобился 3 раза). Асимметрия CLI: у B форму вердикта
  принудил `--json-schema`, у A вердикт извлекался из текста потока (у
  kimi CLI флага схемы нет) — часть разницы валидности лежит в механике
  CLI, а не в семействе.
- **Слой 2 измерен 2026-09-13** (12 замороженных brownfield-диффов E13
  раунда 2, исполнитель не запускался, промпт петли). Оба плеча: 12/12
  approve; находки **A 0 против B 3, все minor** (пересечение пустое).
  Исторический якорь: собственный ревью петли в E13 раунде 2 дал те же
  12 approve и 0 находок; opus/xhigh на тех диффах — 12 minor.
- **Цена**: B $1.70 (канарейки) + $2.29 (frozen) = $3.99; A неизмерима
  (квота подписки kimi, поток цены не несёт). Скорость: A ~28 с против
  B ~96 с на канарейку; 37 с против 78 с на frozen-дифф.
- **Статус: answered, ADR-026.** Гипотеза подтверждена по направлению:
  на посеянных семантических багах одно семейство теряет 2/8 recall, на
  чистой реальной работе различие — minor-шум (0 против 3 находок при
  полном одобрении обеими). FPR и инъекционная защита семейством не
  различаются. Дешёвые модели одного семейства по-прежнему могут
  ассистировать, но вердикт по умолчанию — разведён; совпадение семейств
  — заявляемое исключение (предупреждение `clirun.py` остаётся
  поверхностью принуждения). Задачи исполнения — cod-doc план
  `experiment-e15` (ADR-014), ADR-026 привязан к EXF-001…004.


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
| **E9-EXEC** (плечо исполнителя, парный A/B) | inconclusive 2026-08-24 | experiments/findings.jsonl, experiments/stand-e9-* | **Две задачи, две разные причины неопределённости, ответа нет.** v9lb: с памятью архитектурные (intent) находки 3 → 0, но major 2 → 4, раунды не измерены (ревью умерло процессом), $4.20 vs $5.93. s2ky (реплей с `ee53c217`, память — единственный фактор): 3 раунда/$6.80 против 2/$6.18, но механический признак, под который задача выбиралась (сход границ на `corpus_erc.rs`), не сработал ни в одном плече, а лишний раунд стоил `cargo fmt` и `missing_docs` — гигиена линтера, не память. Признак брался из прогона на ДРУГОМ движке исполнителя. Побочно вскрыт дефект: обрыв по потолку стоимости назывался крахом процесса (исправлен, 3 теста). Честная цена ответа — ~$130 на парную очередь в 10 задач, решение владельца |

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
чужого спора (s2ky); без памяти он потерян. **Плечо исполнителя
измерено дважды и осталось без ответа** (2026-08-24): ни одна из двух
задач не дала чистого сравнения — раунды на одной задаче доминируются
гигиеной линтера, а не памятью. Плечо ревьюера прогона закрыто в составе E15; **E15 закрыт обоими слоями
(2026-09-13)**: одно семейство у исполнителя и ревьюера измерено — recall канареек 6/8 (k3) против
8/8 (claude sonnet), FPR 0 у обоих, на frozen-диффах различие — minor-шум (0 против 3 находок, все
approve), ADR-026; **E12 — оба реплей-стенда
заморожены** (диагноз 6/6 в гейте, границы 4/7 — в гейте с 2026-08-22:
линия держится на машине со стендом, без стенда — громкий skip). Прежнее
состояние E9 (findings E9:
решения архитектуры, сюрприз PG 18 с `\.` в CSV-COPY, утечка тестов
в общую базу — тот же класс, что метрики хелперов в AUDIT-3).

## Журнал изменений

### v0.31 (2026-09-13)

- **E15 закрыт обоими слоями, ADR-026.** Канарейки (2026-09-12):
  recall 6/8 у k3 против 8/8 у claude sonnet, FPR 0 у обоих, инъекции
  2/2 у обоих; разногласия — c3 и c6, обе пропущены рукой одного
  семейства. Frozen-диффы E13 раунда 2 (2026-09-13): 12/12 approve у
  обоих, находки 0 против 3 minor. Решение: семейства ревьюера и
  исполнителя по умолчанию разведены; совпадение — заявляемое
  исключение. Строка §8 обновлена; артефакты — `experiments/canary/
  report-e15.md`, `experiments/bench/report-e15.md`.

### v0.30 (2026-09-12)

- Зарегистрирован **E15**: семейство ревьюера — одно с исполнителем
  (дефолт ADR-022) vs разведено (claude sonnet). Метод — парный пересуд
  без исполнителя: канарейки (recall/FPR) + замороженные диффы goldset.
  Вопрос переехал из «caveat E13» в собственный номер; ссылка в
  08-operators-guide исправлена. Задачи исполнения — cod-doc план
  `experiment-e15` (ADR-014).

### v0.29 (2026-08-24)

- E14: первый живой прогон пуриста на v9lb. Механизм сработал —
  исполнитель объявил выбор по всем трём развилкам в `deviations` и внёс
  их в документ; эскалацию это не предотвратило, число архитектурных
  находок не упало. Замерено, что пурист и ревьюер находят разное: две
  из трёх архитектурных находок ревьюера пуристу недоступны по
  устройству (противоречия с остальным репозиторием, а не молчание
  спеки).
- §1: первая пара дуэли снята на настоящей работе ($7.30, оба плеча
  прошли свой гейт). Инструмент разбора `duel-report.py` написан и
  отказывается делать вывод ниже пяти пар. Вскрыт дефект учёта: деньги
  ЗАМЕРА были неотличимы от денег работы в `total_spend()`.

### v0.28 (2026-08-24)

- Дуэль и пурист проверены НА НАСТОЯЩЕЙ РАБОТЕ ($0.96, дешёвый
  Python-стенд): пурист нашёл 3 развилки на нарочно неполной спеке,
  дуэль развела плечи по разным worktree и оба прошли свой гейт.
- Первый живой прогон вскрыл ЧЕТЫРЕ дефекта, которых не поймали 1206
  зелёных тестов (findings INFRA/first-live-run-found-four-defects и
  /flat-loader-race-exposed-by-threads). Самый показательный: форма
  отчёта ИСПОЛНИТЕЛЯ была зашита условием для всех ролей, и ответ
  пуриста молча выбрасывался — петля говорила «развилок нет» вместо
  «ответа нет». Самый неприятный: плоские загрузчики модулей оказались
  гонкой, и потоки дуэли её мгновенно проявили; починено одним общим
  замком (`modlock.py`), потому что семь отдельных не пересекаются.

### v0.27 (2026-08-24)

- §1: **дуэль** — два исполнителя на одной задаче одновременно, парное
  наблюдение на обычной работе. Уточнение владельца: «фоновый замер» он
  имел в виду именно так, и не ради экономии. Живое плечо выбирается
  жребием заранее и не переигрывается по результату — иначе замеряется
  max(A, B), а не каждое плечо. Спасение живого теневым названо и не
  сделано.
- E14: **контргипотеза реализована** — пурист (`unclear.py`) отдаёт
  список развилок спецификации, и он уезжает в промпт исполнителя с
  требованием объявить сделанный выбор в `deviations`. Полный вариант
  (две дорогие роли) остаётся зарегистрированным, не построенным.

### v0.26 (2026-08-24)

- Три решения владельца одного дня, складывающиеся в один инструмент.
  §1: **фоновый замер** — фактор бросается жребием на задачах обычной
  работы вместо парного стенда; причина в двух неудачах стенда на плече
  исполнителя, а не во вкусе. Умолчание петли — **режим денежного чана**
  (`spending = "money_bin"`, 05-док §5.3): неявных потолков на вызов
  роли больше нет, и вскрылось, что умолчание $1 у ревьюера не проверял
  ни один из 1155 тестов гейта. Зарегистрирован **E14**: линия «строгие
  правила против творчества» переформулирована как «кто НАХОДИТ пробел
  и кто его ЗАПОЛНЯЕТ», с более дешёвой контргипотезой впереди дорогой.

### v0.25 (2026-08-24)

- E11 раунд 2, изменение (2) сделано: выживаемость считается ПРОТИВ
  СПЕЦИФИКАЦИИ. Мутанты разобраны по трём классам (real_gap /
  equivalent / spec_silent), неубиваемые и неоговорённые сняты у обоих
  плеч вместе с пойманными. Итог 18 % против 9 % там, где сырьё
  говорило 30 % против 22 %: преимущество независимого автора тестов
  вдвое больше замеренного. Два прежних утверждения исправлены —
  «общая слепая зона» оказалась неубиваемым мутантом (проверено
  перебором), а граница `if width < 1` не дырой, а местом, о котором
  спека молчит. Набор и классификация лежат в git
  (`experiments/goldset/e11/`), число заморожено гейтом
  (`test_e11_spec_score.py`).

### v0.24 (2026-08-24)

- E9: вторая задача плеча исполнителя (s2ky, парный реплей) сигнала о
  памяти НЕ дала — механический признак, под который задача выбиралась,
  не сработал ни в одном плече, а разница в раунд объясняется гигиеной
  линтера. Ошибка планирования названа: признак брался из прогона на
  другом движке исполнителя. Итог по плечу: n = 2, ответа нет, и
  названа цена честного ответа (~$130 на парную очередь в 10 задач) —
  решение бюджетное.
- Побочно: замер вскрыл дефект петли — обрыв по потолку стоимости
  вызова назывался крахом процесса, потому что разбор конверта стоял на
  недостижимой ветке. Исправлено, совет исполнителю разведён по
  причине.

### v0.23 (2026-08-23)

- E9: плечо исполнителя измерено на одной задаче — с памятью исчезли
  архитектурные (intent) находки, останавливавшие задачу вопросом к
  человеку; при этом major-находок стало больше, а раунды до сходимости
  остались неизмеренными (ревью умерло процессом). Замер состоялся
  только благодаря утренней правке FTS: эмбеддер в это время лежал.

### v0.22 (2026-08-23)

- E9: устройство памяти вынесено в ADR-013; сухая половина этапа 4
  сделана, статус остаётся открытым с названной причиной (нет
  прогоняемой очереди).

### v0.21 (2026-08-23)

- E6 закрыт замером — и оказалось, что сравнения до сих пор не было:
  FTS отдавал ноль строк из-за И-семантики `websearch_to_tsquery` на
  запросе-спецификации. После правки FTS даёт 65 хитов из 80, вектор —
  15 сверх. E9 этап 4: премиса проверена сухим реплеем (свой урок
  находится у 5 задач из 5, блок непуст у 16 из 16); живые плечи имело
  бы смысл мерить только после этой правки.

### v0.20 (2026-08-23)

- E11 round 1 measured: arm B 22 % mutation survival against arm A's
  30 %, at the price of a whole extra role ($4.48, tester $2.34). Two
  corrections the run found in the experiment itself — the «tests must
  be red» premise is false for preserve-behaviour tasks, and survival
  must be scored against the spec (arm B's cli.py «failures» are
  declared spec gaps). Round 2 designed; status stays open.

### v0.19 (2026-08-23)

- E11 opened: measurement instrument first
  (`experiments/tools/mutation-survival.py`), arm A measured on work
  already paid for, and the tester role implemented behind
  `[experiments] tester`. Entry added retroactively — v0.19 bumped the
  frontmatter without recording itself here.

### v0.18 (2026-08-23)

- E11 unblocked: it stood behind E10 and E13, and both props are gone.
  Status rewritten with what E13 hands it — the paired re-judge
  instrument and the warning that a cheap judge sees nothing.

### v0.17 (2026-08-23)

- E13 answered (ADR-012): brownfield round 2 plus a paired re-judge of
  all twelve diffs with a strong reviewer — engines tie at 6 findings
  each, all minor; the reviewer arm, not the executor arm, decides what
  anyone sees. New reusable instrument `bench/e13-rejudge.py`.

### v0.16 (2026-08-23)

- E12: third replay bench added (`goldset/verdicts/`, in the gate).
  Boundary status corrected — two individual disputes are unreachable,
  not two classes — and a fifth signal was measured and rejected.

### v0.15 (2026-08-22)

- E13's side result acted on: the reviewer contract failure is fixed and
  frozen as a third derived bench (`goldset/verdicts/`, in the gate).

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
