---
type: section-index
status: draft
source_of_truth: true
owner: cod-doc core
created: 2026-08-28
updated: 2026-09-12
---

# 🏗️ Architecture

Архитектурные решения зафиксированы как ADR. Источник истины по статусам и
связям — таблица `adr` в cod-doc DB (см. `cod-doc adr list -p zairgrush`,
`cod-doc adr graph -p zairgrush`); файлы в `experiments/adr/` — прозаические
оригиналы решений, `docs/adr/ADR-NNN.md` — генерируемые проекции из DB
(`cod-doc adr export`, руками не править).

## Действующие ADR (accepted / proposed)

| ADR | Решение | Статус | Оригинал |
|-----|---------|--------|----------|
| ADR-002 | Модель исполнителя по умолчанию — K3 | proposed | [002](../experiments/adr/002-executor-model-k3-default.md) |
| ADR-011 | Движок исполнителя — выбор конфига, умолчание kimi | accepted | [011](../experiments/adr/011-executor-engine-is-a-config-choice.md) |
| ADR-014 | cod-doc — единая поверхность планирования | accepted | только в DB (docs/adr/ADR-014.md) |
| ADR-015 | Журнал петли — jsonl-первичный | accepted | [001](../experiments/adr/001-journal-jsonl-first.md) |
| ADR-016 | ZCode — третий CLI-движок исполнителя | accepted | [016](../experiments/adr/016-zcode-executor-engine.md) |
| ADR-017 | Хелперы через нативный Ollama /api/chat | accepted | [004](../experiments/adr/004-ollama-native-api.md) |
| ADR-018 | Ревьюер проверяет исполнением, выборочно | accepted | [005](../experiments/adr/005-reviewer-verification-requests.md) |
| ADR-019 | Карта репозитория в handoff — точечно | accepted | [006](../experiments/adr/006-repo-map-in-handoff.md) |
| ADR-020 | Гибридная навигация по коду | accepted | [008](../experiments/adr/008-hybrid-code-intelligence.md) |
| ADR-021 | Инбокс и триаж: вопрос человеку не останавливает прогон | accepted | [009](../experiments/adr/009-loop-decisions-and-inbox.md) |
| ADR-022 | Дефолт kimi — k3-256k; пул субагентов без force | accepted | [010](../experiments/adr/010-swarm-quota-model-routing.md) |
| ADR-023 | Качество на малых задачах решает рука ревьюера | accepted | [012](../experiments/adr/012-executor-engine-does-not-decide-quality.md) |
| ADR-024 | Память между прогонами: файлы первичны, PG — индекс | accepted | [013](../experiments/adr/013-memory-architecture.md) |

## Исторические (superseded)

ADR-001→015, ADR-003→004→017, ADR-005→018, ADR-006→019, ADR-007→008→020,
ADR-009→021, ADR-010→022, ADR-012→023, ADR-013→024. Цепочки 015–024 —
переиздание решений при миграции в cod-doc («решение то же», нормализация
полей), не смена позиции. Файлы `experiments/adr/00x-*.md` остаются
историей с полным контекстом.

---
*Секция создана 2026-08-28, наполнена 2026-09-12.*
