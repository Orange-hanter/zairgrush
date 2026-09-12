# ADR-016: ZCode — третий CLI-движок исполнителя; умолчание остаётся kimi

Дата: 2026-09-11. Статус: принято. Не отменяет ADR-011.

## Контекст

ADR-011 (2026-08-22) сделал движок исполнителя ключом конфига: `kimi`
по умолчанию, `claude`, `ollama` как chat-fill. Ревьюер, планировщик и
документатор ходят через `claude`. Умолчание кода не менялось (ADR-002:
K3 внутри kimi).

На этой машине 2026-09-11 установлен клиент Z.AI для Mac (ZCode.app).
CLI — zcode 0.16.5, бинарь
`/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`, в PATH
часто нет. `--help` — контракт: `--prompt` (не stdin), `--json` (один
объект, не NDJSON), `--mode yolo`, `--allowed-tools` /
`--disallowed-tools`, `--cwd`, `--surface terminal`. Флагов `--model`,
`--json-schema`, `--effort`, `stream-json` нет. Пример справки
`Bash(git *)` слишком широк: закрыл бы `git diff`.

Модель в рантайме бандла видна как `zai/glm-5.1` / `zai/glm-4.7`; живые
веса — `~/.zcode/cli/config.json`. `zcode login` через OAuth ненадёжен.

Нужен третий CLI той же роли исполнителя, не смена ревьюера и не смена
умолчания. ADR-011 это не отменяет: список движков расширяется, правило
именования и отказ на незнакомом имени остаются.

## Варианты

- **A. Сменить умолчание на zcode.** Отвергнуто: ADR-011 и ADR-002 держат
  «ничего не настроено = kimi» ради сравнимости плеч E8/E10; этот
  репозиторий на zcode сам не переключается.
- **B. GLM через Claude Code** с `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`.
  Отвергнуто: другой продукт и чужой argv; ревьюер остаётся claude.
- **C. npm `zcode` / `zcode-app-cli` или GUI/CDP.** Отвергнуто: контракт
  снят с `--help` бандла 0.16.5, не с чужой обёртки.
- **D. Добавить `zcode` в закрытый список, умолчание kimi, ревьюер claude.**
  Принято.

## Решение

**Вариант D.** В `ENGINES` / `EXECUTOR_ENGINES` добавляется `zcode`.
Умолчание кода — `kimi`. Ревьюер — `claude`. Именование прежнее:
`executor_engine` либо префикс `executor_model` (`zcode:glm-5.3` старше
ключа). Незнакомое имя отказывает на старте.

Вызов: `which(zcode)`, иначе `node` + бандл ZCode.app. Argv всегда несёт
`--prompt`, `--json`, `--mode yolo`, allow/deny как у claude,
`--surface terminal`; `--cwd` — путь `AgentDriver`. `--model` на argv
не кладётся. Отчёт — `parsing.report_in(response)`. Usage camelCase
идёт в метрику; `cost_usd` не выдумывается.

Doctor проверяет выбранный zcode. Невыбранный отсутствующий — опционально.
Если модель задана — доктор говорит, что CLI `--model` не принимает.

## Последствия

- (+) Третий CLI у роли исполнителя; префикс `zcode:…` сажает одну
  задачу плана на ZCode, не трогая прогон.
- (+) Git-запись запрещена тем же списком, что у claude, не `Bash(git *)`.
- (−) Модель живёт в `~/.zcode/cli/config.json`, не во флаге CLI.
- (−) `--json` молчит до финала: `silence_timeout` по умолчанию 600 с.
- (−) Цены в конверте нет; квота ZCode в `quota_error` не заведена.
- (−) `zcode login` через OAuth ненадёжен — ключ Coding Plan или GUI.
