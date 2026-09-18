# Contest: Kimi Code vs Claude Sonnet 5

Полигон состязания по `14-kimi-vs-sonnet-contest.md`. Один харнес
(`harness.py`), одинаковый контур для обеих моделей через OpenRouter —
различия CLI-инструментов исключены (решение по открытому вопросу §9.1).

## Модели

| Слот | Model ID (OpenRouter) | env-переопределение |
|---|---|---|
| kimi | `moonshotai/kimi-k2.7-code` | `CONTEST_MODEL_KIMI` |
| sonnet | `anthropic/claude-sonnet-5` | `CONTEST_MODEL_SONNET` |

Точного «kimi 2.8» в каталоге OpenRouter нет; взят coding-тюнингованный
k2.7-code. Смена модели не требует правок кода.

## Раунд 1 — дисциплина A (кодинг, детерминированное судейство)

| Задача | Суть | Судейство |
|---|---|---|
| a1-lru | TTL LRU-кэш со статистикой, инъекция часов | `tasks/a1-lru/test_lru.py`, 17 тестов |
| a2-cron | Парсер подмножества cron + next_fire | `tasks/a2-cron/test_cron.py`, 22 теста |
| a6-migrate | SQLite→JSONL миграция: атомарность, идемпотентность, rollback | `tasks/a6-migrate/test_migrate.py`, 12 тестов |

Модель получает только `TASK.md`; тесты скрыты. Формат ответа — ровно один
блок ```python. Извлечение кода, прогон pytest, метрики — всё в `harness.py`.

## Запуск

```
python3 experiments/contest/harness.py            # все задачи × обе модели
python3 experiments/contest/harness.py --only a1-lru
```

Артефакты: `results/<UTC-ts>/` — `raw/<task>-<model>.md` (сырой ответ),
`solution-<task>-<model>.py`, `results.jsonl`, `REPORT.md`.
