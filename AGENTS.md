# AGENTS.md — гид для агента, работающего в этом репозитории

> **Кому это.** Любому coding-агенту за работой в ZAIrgRush — ZCode, Kimi Code,
> Claude Code. ZCode читает этот файл автоматически при старте сессии в
> рабочем пространстве; остальным — прочесть до первой правки.

## Закон репозитория

Полный закон — [`CLAUDE.md`](CLAUDE.md): что это за репозиторий, где лежит
реализация петли (`tools/swarm/`), как устроены доки и конвенции правок.
Этот файл не дублирует его, а даёт вход и ZCode-специфику.

## Проверка — одна команда

```sh
cd tools/swarm && ./check.sh   # линт → типы → тесты, в этом порядке
```

Инструменты — в `tools/swarm/.venv` (создаётся `uv venv .venv && uv pip
install --python .venv ruff mypy`). Отдельно от гейта: мутационный аудит
`python3 tools/swarm/mutate.py` и `.venv/bin/mypy` strict для нового кода.

## ZCode в этом проекте

- **Исполнитель петли.** Сворм поддерживает четыре движка исполнителя
  (`kimi`, `claude`, `ollama`, `zcode`); умолчание — `kimi`, ZCode — запасной
  путь (E10). Выбор: `executor_engine = "zcode"` в `swarm.toml` стенда либо
  префикс `zcode:` в `executor_model` план-диффа (пришпиливает отдельную
  задачу). Свежесть — `swarm doctor`: строка `[ ok ] zcode`.
- **CLI.** `zcode` — бандл десктопа ZCode.app
  (`/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`, запускается
  через node). Если в PATH команды `zcode` нет, сварм находит бандл сам
  (`zcode_cmd()` в `tools/swarm/swarm/engines.py`); на машине разработчика
  стоит шим `~/.local/bin/zcode` — тот же приём, что у `kimi` и `claude`.
- **MCP-хост.** `.zcode/config.json` — workspace-конфиг ZCode: `cod-doc`
  (HTTP `127.0.0.1:8801`, сервис должен быть поднят) и `ai-reviewer` /
  `ai-reviewer-structure` (stdio). Ровно те же серверы, что у
  `.kimi-code/mcp.json` и `.cursor/mcp.json`; это не merge-гейт. Пути в
  конфигах абсолютные и machine-local — осознанно, на другой машине правятся
  под локальный layout.
- **Промпт в файл, не в argv.** У zcode stdin нет вовсе (ADR-016); петля
  отдаёт промпт временным файлом — это механика `engines.py`, вручную
  длинный промпт тоже лучше класть в файл.

## Приоритет документации

Документация — всегда в приоритете (§3.3 в 05-agent-swarm.md): рабочая
сессия не закончена, пока затронутые доки не актуализированы. Язык правок —
русский, конвенции документа — в CLAUDE.md.
