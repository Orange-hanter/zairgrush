---
type: guide
status: active
created: 2026-08-28
updated: 2026-09-12
---

# Документация проекта

> Централизованное хранилище: ADR-проекции, гайды, журналы экспериментов.

## Описание

Точка входа в документацию zairgrush. Источник истины по ADR — таблица `adr`
в cod-doc DB (ADR-025); файлы в `docs/adr/` — генерируемые проекции, руками не
править.

## Структура

```
/docs/
├── README.md          # этот файл — индекс секции
└── adr/               # ADR-NNN.md — проекции из cod-doc DB (adr export)
```

Основной корпус документов живёт в корне репозитория:

| Документ | Содержание |
|----------|------------|
| [MASTER.md](../MASTER.md) | Мастер-документ: секции, handoff, changelog |
| [05-agent-swarm.md](../05-agent-swarm.md) | Системная спецификация петли |
| [06-knowledge-infra-experiments.md](../06-knowledge-infra-experiments.md) | Программа экспериментов E1–E15 |
| [07-experiments-journal.md](../07-experiments-journal.md) | Журнал прогонов (нарративный) |
| [12-pilot-journal.md](../12-pilot-journal.md) | Журнал пилота (PILOT-1, вынос §19–20 из 07) |
| [13-findings-taxonomy.md](../13-findings-taxonomy.md) | Единая таксономия находок |
| [08-operators-guide.md](../08-operators-guide.md) | Гайд оператора: движки, конфиги, подводные камни |
| [10-decision-briefs.md](../10-decision-briefs.md) | Брифы решений |
| [arch/README.md](../arch/README.md) | Индекс ADR с DAG замещений |
| [site/](../site/index.html) | Публичная страница проекта |

## Статус

- **Статус секции:** `🟢 VERIFIED` (индекс соответствует содержимому)
- **Ответственный агент:** `@Orchestrator`

---
*Раздел наполнен 2026-09-12 (HRD-002): вымышленная структура system/guides/reports заменена реальной.*
