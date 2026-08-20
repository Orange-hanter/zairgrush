---
title: "ZAIrgRush — План декомпозиции монолитов tools/swarm"
type: plan
status: draft
version: 0.2
created: 2026-08-20
updated: 2026-08-20
related:
  - 05-agent-swarm.md
summary: >
  Пошаговый план разбиения четырёх крупных модулей оркестратора
  (cli.py, loop.py, memory.py, agents.py) на файлы по 150–600 строк
  без изменения логики и публичного API.
---

# План декомпозиции монолитов `tools/swarm`

Повод: четыре модуля оркестратора переросли комфортный для сопровождения
размер — `cli.py` (1790 строк), `loop.py` (1412), `memory.py` (1113),
`agents.py` (930). В каждом смешано несколько контуров ответственности,
что затрудняет навигацию и ревью. План описывает поэтапное разбиение
без изменения поведения.

## §1. Общие принципы

Применяются ко всем четырём файлам.

1. **Контракт загрузки — по файловому пути, не по пакету.** Тесты грузят
   модули через `importlib.util.spec_from_file_location("loop",
   ROOT_DIR / "loop.py")` и т.п.; внутренние импорты плоские
   (`import board`, `import memory as memory_mod`), каждый модуль сам
   добавляет свой каталог в `sys.path` (паттерн `_HERE`, см. `loop.py:28-31`).
   Следствия: (а) старые файлы обязаны остаться на прежних путях
   (`swarm/loop.py`, `swarm/cli.py`, `swarm/memory.py`, `swarm/agents.py`)
   как тонкие shim'ы с реэкспортом (`from verdicts import validate_verdict
   # noqa: F401` — импорт плоский, не относительный); (б) новые модули —
   **плоские файлы в `swarm/`**, а не пакеты: каталог `cli/` сломал бы
   загрузку `swarm/cli.py` по пути; (в) новый модуль, импортирующий
   соседей, повторяет паттерн `_HERE`/`sys.path`.
2. **Один шаг = один вынос = один коммит**, после каждого `./check.sh`
   зелёный (ruff → mypy --strict → pytest). Никаких правок логики при
   переносе — только перемещение кода и импорты. Тесты не трогаем:
   загрузка по пути продолжает работать через shim.
3. Порядок внутри файла: сначала чистые функции (не зависят от состояния
   класса), потом методы классов.
4. Аннотации типов сохраняются полностью — гейт `mypy --strict` не должен
   потребовать новых строк в `ignore`.

## §2. `cli.py` (1790 строк) → плоские модули `cli*.py`

16 обработчиков `cmd_*` — естественная граница. Целевой размер файлов
150–400 строк (имена плоские, по идиоме `boardserve.py`):

| Новый модуль | Что переезжает |
| --- | --- |
| `cli.py` (остаётся) | `main()`, сборка argparse, `_load`, `_config`, `_ui` + реэкспорт всех `cmd_*` |
| `clirun.py` | `cmd_go`, `cmd_run`, `cmd_resume`, `_preflight`, `_reconcile_decision`, `_reconcile_commit_step`, `_run_verdict`, `_print_results`, `_board_open` (~450 строк, самый связный кластер) |
| `cliexplain.py` | `cmd_why`, `cmd_status`, `cmd_retry`, `_explain`, `_next_steps`, `_print_next`, `_prefix`, `_round_label`, `_read_trend` |
| `clireport.py` | `cmd_report`, `cmd_board`, `cmd_ab`, `cmd_map`, `cmd_impact` |
| `cliinbox.py` | `cmd_inbox`, `cmd_answer`, `_paths_mentioned` |
| `climemory.py` | `cmd_memory`, `_memory_anchor` |
| `climisc.py` | `cmd_doctor`, `cmd_policy`, `cmd_plan`, `_tree_sitter_clib` |

Порядок выноса: `inbox` / `memory` / `misc` (изолированные, разминка) →
`explain` / `report` → `run` (самый рискованный — в конце).

## §3. `loop.py` (1412 строк) → три модуля

В файле три смешанных слоя:

- **`verdicts.py`** (~230 строк): чистые функции `parse_quota_reset`,
  `quota_exception`, `quota_error`, `validate_verdict`, `apply_policies`,
  `classify_findings`, `decide` + исключения (`ExecutorUnavailableError`,
  `QuotaExceededError`, `EscalationError`). Уже покрыты
  `test_verdict.py` / `test_loop_decide.py` — вынос почти безрисковый,
  делается первым.
- **`gitops.py`** (~350 строк): из `Loop` выносятся `gate`,
  `scope_check`, `revert`, `commit`, `cleanup`, `_apply_patch`,
  `integrity_check`, `_state_sha` / `_declared_state_sha` /
  `_state_fingerprint` — работа с git и файлами, а не логика петли.
  Состояние (`state`, `config`) передаётся параметрами; `Loop` держит
  тонкие делегаты для совместимости.
- **`reviewcycle.py`** (~250 строк): `_review_with_quota_wait`,
  `_reviewers_disagreed`, `_diagnose`, `_arm` — оркестрация раунда
  ревью.
- В `loop.py` остаётся ядро: `run_task`, `run`, `_quota_resume_wait`,
  `_rescue`, `refresh_board` (~450 строк).

## §4. `memory.py` (1113 строк) → плоские модули `mem*.py`

Четыре независимых контура в одном файле:

- `memstore.py` — `MemoryStore` (локальный JSONL, локи, digest) —
  самодостаточен.
- `mempg.py` — всё Postgres: `pg`, `ensure_schema`, `upsert`,
  `reindex`, `_backfill_embeddings`, `sync`, `search_fts`, `search_vec`,
  `ensure_vector` (~350 строк).
- `meminject.py` — `retrieve`, `inject_block`, `norms_block`,
  `_render_hits`, `enabled_for`.
- `memreflect.py` — `record_task_outcome`, `fp_candidates`,
  `_propose_policies`, `reflect_after_run`, `_trajectory`,
  `_commit_paths`, `_commit_symbols`.
- `memory.py` остаётся shim'ом — реэкспорт по текущей плоской схеме.

## §5. `agents.py` (930 строк) — последним и осторожно

Класс `Agents` — единый объект состояния, резать его на классы сейчас
рискованно. Минимальный безопасный шаг:

- **`parsing.py`** (~120 строк): чистые функции парсинга —
  `condense_diff`, `_extract_fenced_code`, `_report_in`,
  `_extract_report`.
- **`promptbuilder.py`** (опционально): сборка промптов — `handoff`,
  `_review_prompt_parts`, `review_prompt`, `memory_block`, `norms_for`,
  `repo_map` — как функции от `Agents`.

Дальнейшую резку `implement` / `_implement_fill` / `review` отложить до
появления боли — это самый переплетённый код.

## §6. Очередность и критерии приёмки

1. `loop.py` → `verdicts.py` (чистые функции, самый дешёвый шаг).
2. `cli.py` → плоские `cli*.py` (механический перенос обработчиков).
3. `memory.py` → плоские `mem*.py`.
4. `loop.py` → `gitops.py` + `reviewcycle.py`.
5. `agents.py` → `parsing.py` (+ `promptbuilder.py` опционально).

После каждого шага: `./check.sh` зелёный (ruff → mypy --strict → 988
тестов) и diff содержит только переносы. Итоговая цель — ни одного файла
свыше ~600 строк, загрузка по прежним путям (`swarm/cli.py`,
`swarm/loop.py`, …) и состав экспортируемых имён неизменны.

## Журнал изменений

### v0.2 (2026-08-20)

- Зафиксирован контракт загрузки модулей по файловому пути
  (`importlib.util.spec_from_file_location` в тестах, плоские импорты
  через `sys.path`-паттерн `_HERE`): пакеты заменены на плоские модули
  (`clirun.py`, `memstore.py`, …), старые файлы остаются shim'ами.
  Без этой поправки план ломал тестовую загрузку на первом же шаге.

### v0.1 (2026-08-20)

- Первая редакция. Составлена по фактической структуре модулей
  (инвентаризация функций и методов `cli.py`, `loop.py`, `memory.py`,
  `agents.py` на коммите `660e649`).
