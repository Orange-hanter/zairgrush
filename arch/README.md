---
type: guide
status: draft
source_of_truth: true
owner: cod-doc core
created: 2026-08-28
updated: 2026-09-28 (experiments/adr — заглушки; колонка «Проекция» ведёт в docs/adr)
---

# 🏗️ Architecture

Архитектурные решения зафиксированы как ADR. Источник истины по статусам и
связям — таблица `adr` в cod-doc DB (см. `cod-doc adr list -p zairgrush`,
`cod-doc adr graph -p zairgrush`); `docs/adr/ADR-NNN.md` — генерируемые
проекции из DB (`cod-doc adr export`, руками не править). Файлы
`experiments/adr/NNN-*.md` — заглушки-переадресации на проекции (старые
ссылки не ломаются); прозаические оригиналы — в истории git.

## Действующие ADR (accepted / proposed)

| ADR | Решение | Статус | Проекция |
|-----|---------|--------|----------|
| ADR-002 | Модель исполнителя по умолчанию — K3 | proposed | [ADR-002](../docs/adr/ADR-002.md) |
| ADR-011 | Движок исполнителя — выбор конфига, умолчание kimi | accepted | [ADR-011](../docs/adr/ADR-011.md) |
| ADR-014 | cod-doc — единая поверхность планирования | accepted | только в DB (docs/adr/ADR-014.md) |
| ADR-015 | Журнал петли — jsonl-первичный | accepted | [ADR-015](../docs/adr/ADR-015.md) |
| ADR-016 | ZCode — третий CLI-движок исполнителя | accepted | [ADR-016](../docs/adr/ADR-016.md) |
| ADR-017 | Хелперы через нативный Ollama /api/chat | accepted | [ADR-017](../docs/adr/ADR-017.md) |
| ADR-018 | Ревьюер проверяет исполнением, выборочно | accepted | [ADR-018](../docs/adr/ADR-018.md) |
| ADR-019 | Карта репозитория в handoff — точечно | accepted | [ADR-019](../docs/adr/ADR-019.md) |
| ADR-020 | Гибридная навигация по коду | accepted | [ADR-020](../docs/adr/ADR-020.md) |
| ADR-021 | Инбокс и триаж: вопрос человеку не останавливает прогон | accepted | [ADR-021](../docs/adr/ADR-021.md) |
| ADR-022 | Дефолт kimi — k3-256k; пул субагентов без force | accepted | [ADR-022](../docs/adr/ADR-022.md) |
| ADR-023 | Качество на малых задачах решает рука ревьюера | accepted | [ADR-023](../docs/adr/ADR-023.md) |
| ADR-024 | Память между прогонами: файлы первичны, PG — индекс | accepted | [ADR-024](../docs/adr/ADR-024.md) |
| ADR-025 | Каноникал ADR — таблица `adr` в DB; `docs/adr/` — проекции, не Documents (`import docs --exclude 'docs/adr'`) | accepted | только в DB (docs/adr/ADR-025.md) |
| ADR-026 | Ревьюер и исполнитель — разные семейства моделей; совпадение — заявляемое исключение (замерено E15) | accepted | только в DB (docs/adr/ADR-026.md) |
| ADR-027 | Дешёвый контур ревью: триаж детерминированный — да; haiku-плечо, панель+adjudicator, дайджест — нет (замерено, цикл REV-001…005) | accepted | только в DB (docs/adr/ADR-027.md) |

## Исторические (superseded)

ADR-001→015, ADR-003→004→017, ADR-005→018, ADR-006→019, ADR-007→008→020,
ADR-009→021, ADR-010→022, ADR-012→023, ADR-013→024. Цепочки 015–024 —
переиздание решений при миграции в cod-doc («решение то же», нормализация
полей), не смена позиции. Полный контекст исходных записей — в их
проекциях `docs/adr/ADR-001…013.md` (тела перенесены в DB при импорте).

---
*Секция создана 2026-08-28, наполнена 2026-09-12.*
