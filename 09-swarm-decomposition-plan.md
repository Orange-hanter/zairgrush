---
title: "ZAIrgRush — План декомпозиции монолитов tools/swarm"
type: plan
status: draft
version: 0.1
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

1. **Шаг 0 — зафиксировать контракт импортов.** Прогнать
   `grep -rh "from swarm\|import swarm" tests/`, чтобы знать, что тесты
   считают публичным API. Каждый старый модуль становится тонким shim'ом
   с реэкспортом (`from swarm.cli_run import cmd_run  # noqa: F401`) —
   тесты и внешние вызовы не ломаются ни на одном шаге.
2. **Один шаг = один вынос = один коммит**, после каждого `./check.sh`
   зелёный (ruff → mypy --strict → pytest). Никаких правок логики при
   переносе — только перемещение кода и импорты.
3. Порядок внутри файла: сначала чистые функции (не зависят от состояния
   класса), потом методы классов.
4. Аннотации типов сохраняются полностью — гейт `mypy --strict` не должен
   потребовать новых строк в `ignore`.

## §2. `cli.py` (1790 строк) → пакет `cli/`

16 обработчиков `cmd_*` — естественная граница. Целевой размер файлов
150–400 строк:

| Новый модуль | Что переезжает |
| --- | --- |
| `cli/__init__.py` | `main()`, сборка argparse, `_load`, `_config`, `_ui` + реэкспорт всех `cmd_*` |
| `cli/run.py` | `cmd_go`, `cmd_run`, `cmd_resume`, `_preflight`, `_reconcile_decision`, `_reconcile_commit_step`, `_run_verdict`, `_print_results`, `_board_open` (~450 строк, самый связный кластер) |
| `cli/explain.py` | `cmd_why`, `cmd_status`, `cmd_retry`, `_explain`, `_next_steps`, `_print_next`, `_prefix`, `_round_label`, `_read_trend` |
| `cli/report.py` | `cmd_report`, `cmd_board`, `cmd_ab`, `cmd_map`, `cmd_impact` |
| `cli/inbox.py` | `cmd_inbox`, `cmd_answer`, `_paths_mentioned` |
| `cli/memory.py` | `cmd_memory`, `_memory_anchor` |
| `cli/misc.py` | `cmd_doctor`, `cmd_policy`, `cmd_plan`, `_tree_sitter_clib` |

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
- **`review_cycle.py`** (~250 строк): `_review_with_quota_wait`,
  `_reviewers_disagreed`, `_diagnose`, `_arm` — оркестрация раунда
  ревью.
- В `loop.py` остаётся ядро: `run_task`, `run`, `_quota_resume_wait`,
  `_rescue`, `refresh_board` (~450 строк).

## §4. `memory.py` (1113 строк) → пакет `memory/`

Четыре независимых контура в одном файле:

- `memory/store.py` — `MemoryStore` (локальный JSONL, локи, digest) —
  самодостаточен.
- `memory/pg.py` — всё Postgres: `pg`, `ensure_schema`, `upsert`,
  `reindex`, `_backfill_embeddings`, `sync`, `search_fts`, `search_vec`,
  `ensure_vector` (~350 строк).
- `memory/inject.py` — `retrieve`, `inject_block`, `norms_block`,
  `_render_hits`, `enabled_for`.
- `memory/reflect.py` — `record_task_outcome`, `fp_candidates`,
  `_propose_policies`, `reflect_after_run`, `_trajectory`,
  `_commit_paths`, `_commit_symbols`.
- `memory/__init__.py` — реэкспорт по текущей плоской схеме.

## §5. `agents.py` (930 строк) — последним и осторожно

Класс `Agents` — единый объект состояния, резать его на классы сейчас
рискованно. Минимальный безопасный шаг:

- **`parsing.py`** (~120 строк): чистые функции парсинга —
  `condense_diff`, `_extract_fenced_code`, `_report_in`,
  `_extract_report`.
- **`prompt_builder.py`** (опционально): сборка промптов — `handoff`,
  `_review_prompt_parts`, `review_prompt`, `memory_block`, `norms_for`,
  `repo_map` — как функции от `Agents`.

Дальнейшую резку `implement` / `_implement_fill` / `review` отложить до
появления боли — это самый переплетённый код.

## §6. Очередность и критерии приёмки

1. `loop.py` → `verdicts.py` (чистые функции, самый дешёвый шаг).
2. `cli.py` → пакет (механический перенос обработчиков).
3. `memory.py` → пакет.
4. `loop.py` → `gitops.py` + `review_cycle.py`.
5. `agents.py` → `parsing.py` (+ `prompt_builder.py` опционально).

После каждого шага: `./check.sh` зелёный (ruff → mypy --strict → 988
тестов) и diff содержит только переносы. Итоговая цель — ни одного файла
свыше ~600 строк, публичный API (`swarm.cli`, `swarm.loop`, …)
неизменен.

## Журнал изменений

### v0.1 (2026-08-20)

- Первая редакция. Составлена по фактической структуре модулей
  (инвентаризация функций и методов `cli.py`, `loop.py`, `memory.py`,
  `agents.py` на коммите `660e649`).
