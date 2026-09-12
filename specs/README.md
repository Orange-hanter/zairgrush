---
type: guide
status: active
created: 2026-08-28
updated: 2026-09-12
---

# 📋 Specifications

> Спецификации системы zairgrush: петля агентов, контракты ролей, протоколы обмена.

## Описание

Канонические спецификации петли Исполнитель↔Ревьюер и её подсистем. Форматы
данных (конверты отчётов и вердиктов) описаны в разделе [Models](../models/README.md);
история решений — в [ADR](../arch/README.md).

## Действующие спецификации

| Документ | Что специфицирует | Статус |
|----------|-------------------|--------|
| [05-agent-swarm.md](../05-agent-swarm.md) | Системная спецификация петли: роли, FSM, гейты, бюджеты, денежный чан | 🟢 active, живой документ |
| [09-cheap-review-contour.md](../09-cheap-review-contour.md) | Контур дешёвого ревью: модели, effort, пулы | 🟢 active |
| [09-swarm-decomposition-plan.md](../09-swarm-decomposition-plan.md) | План декомпозиции сворма (рабочие направления) | 🟡 draft |
| [10-decision-briefs.md](../10-decision-briefs.md) | Брифы решений по компонентам | 🟡 draft |

## Эталонные спецификации стендов (experiments/)

| Документ | Что специфицирует |
|----------|-------------------|
| `experiments/stand-e9-exec/docs/04-project-format.md` | Формат проекта `.zeus` (эталон ZeusLogic) |
| `experiments/stand-e9-exec/docs/06-docs-tooling.md` | Инструменты контроля документации (docs-check) |
| `experiments/stand-e9-exec/docs/01-requirements.md` | Анализ требований ZeusLogic |
| `experiments/stand-e9-exec/bench/shu-01/SPEC.md` | Эталонная постановка SHU-01 (бенч) |

Стенды `experiments/stand-*/` gitignored — это замороженные копии, а не
развиваемые спецификации.

---
*Раздел наполнен 2026-09-12 (HRD-002): заглушка заменена индексом реальных спецификаций.*
