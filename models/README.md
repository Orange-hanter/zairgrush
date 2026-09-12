---
type: guide
status: active
created: 2026-08-28
updated: 2026-09-12
---

# 🗃️ Models — Модели данных

> Форматы данных петли zairgrush: конверты отчётов и вердиктов, журналы, конфиг.

## Описание

Канонические определения форматов живут в коде `tools/swarm/` (валидируются
схемами и golden-тестами) — этот раздел указывает, где какой формат определён,
и не дублирует схемы текстом.

## Конверты агентов (исполнение ↔ ревью)

| Формат | Где определён | Назначение |
|--------|---------------|------------|
| Отчёт исполнителя | `tools/swarm/swarm/executor.py`, `agents_types.py` | Статус, deviations, файлы — парсинг и валидация отчёта петлёй |
| Вердикт ревьюера | `tools/swarm/swarm/verdicts.py`, `reviewer.py` | approve / request_changes / dispute + findings; fail-closed валидация |
| Запросы верификации | `tools/swarm/swarm/verify.py` | Проверки исполнением по запросу ревьюера (ADR-005/018) |
| Список развилок спеки | `tools/swarm/swarm/unclear.py` | Выход «пуриста» E14: развилки без предложения ответов |
| План-дифф | `tools/swarm/swarm/planner.py` | add/update/remove задач с deps; транзакционная валидация |

## Журналы и данные экспериментов

| Формат | Где определён | Назначение |
|--------|---------------|------------|
| Журнал петли (jsonl-первичный, ADR-015) | `tools/swarm/swarm/obs.py`, `state.py` | События прогона, состояние для рестарта stateless-агентов |
| `findings.jsonl` / `decisions.jsonl` | 06-док §1, `experiments/` | Наблюдение → решение; протокол программы экспериментов |
| Goldset (`verdicts/labels/reviews_metrics.jsonl`) | `experiments/goldset/README.md` | Замороженная разметка PILOT-1; метрика endorsed_majors_per_dollar |
| Конфиг `swarm.toml` | `tools/swarm/swarm/cli.py` (`KNOWN_CONFIG_KEYS`) | Модели/effort ролей, бюджеты, движки, память |

## Состояние и память

- Состояние прогона и очередь задач — `.swarm/` (jsonl + lock-файлы, `state.py`, `modlock.py`).
- Память между прогонами (ADR-024): файлы первичны, PG — индекс; `memory.py`, `memstore.py`, `mempg.py`.

---

## 📖 Связанные разделы

- [Specifications](../specs/README.md) — спецификации петли и контуров
- [Architecture](../arch/README.md) — ADR (каноникал: таблица `adr` в cod-doc DB, ADR-025)
- [Docs](../docs/README.md) — документация проекта

---
*Раздел наполнен 2026-09-12 (HRD-002): заглушка с TODO-таблицей заменена указателями на канонические определения в коде.*
