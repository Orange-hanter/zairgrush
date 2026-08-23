"""Движок исполнителя: кем именно выполняется задача.

Роль исполнителя была прибита к одному CLI: `implement()` собирал
`["kimi", "-p", ...]` и другого варианта не имела. Ревьюер, планировщик и
документатор ходят через `claude`, и только у исполнителя не было
запасного пути — отказ одной подписки останавливал петлю целиком (E10,
плечо A так и стоит: «нужна квота kimi.com»).

Здесь живёт ВЫБОР движка и всё, что из него следует: как собирается
командная строка, каким разборщиком читается поток, откуда достаётся
отчёт. Функции чистые — ни subprocess, ни файлов: аргументы вызова
проверяются тестом без запуска агента, как разбор ответов в `parsing`.

Правило именования одно, второго способа сказать то же самое нет:
`executor_model` МОЖЕТ нести префикс движка (`kimi:`, `claude:`,
`ollama:` — по первому двоеточию), иначе решает ключ `executor_engine`,
иначе `kimi`. Префикс старше ключа: так план-дифф пришпиливает отдельную
задачу к другому движку, не трогая настройку прогона, — ровно тот
приём, который E10 уже ввёл для заливок `ollama:`.

Незнакомое значение НЕ откатывается к умолчанию, а отказывает: молча
выбранное умолчание — тот самый отказ, что дал «ноль вызовов эмбеддера
за весь пилот» (см. закрытый список `MEMORY_MODES` в cli).
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import parsing  # noqa: E402

# Закрытый список. `ollama` — не CLI, а маршрут на chat-fill (E10):
# движок в том же перечислении, потому что вопрос «кем исполнять» у него
# общий с остальными, а вот командной строки у него нет.
ENGINES = frozenset({"kimi", "claude", "ollama"})
DEFAULT_ENGINE = "kimi"

# Инструменты исполнителя на движке claude. Ревьюер живёт на read-only
# наборе (§3.2), исполнителю нужно писать файлы и гонять тесты — иначе
# он не может ни сделать работу, ни заполнить `evidence.tests`.
CLAUDE_ALLOWED_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"

# Промпт исполнителя говорит «git для тебя ТОЛЬКО ДЛЯ ЧТЕНИЯ», и до сих
# пор это была фраза: единственной защитой оставались страж границ и
# `git reset` постфактум. У движка claude запрет становится механикой —
# отказ приходит ДО выполнения команды, и попытка видна в конверте
# (`permission_denials`), то есть не теряется. Проверено пробой
# 2026-08-22: `git commit -am` отклонён, история стенда не сдвинулась.
CLAUDE_DENIED_TOOLS = ",".join((
    "Bash(git commit:*)", "Bash(git push:*)", "Bash(git reset:*)",
    "Bash(git rebase:*)", "Bash(git merge:*)", "Bash(git checkout:*)",
    "Bash(git stash:*)", "Bash(git config:*)",
))

# `acceptEdits` без списка инструментов не пускает Bash, а без Bash
# исполнитель не запустит тесты. Проба 2026-08-22: связка
# acceptEdits + allowedTools отработала полный круг (правка файла,
# прогон теста, отчёт по схеме) за $0.056.
CLAUDE_PERMISSION_MODE = "acceptEdits"


class EngineError(ValueError):
    """Движок назван неверно. Отдельный тип, потому что лечится это не
    ретраем и не ожиданием, а правкой конфига — и сказать об этом надо
    ДО первого потраченного доллара."""


def split_model(value: object) -> tuple[str | None, str]:
    """`"claude:sonnet"` -> `("claude", "sonnet")`, `"k3"` -> `(None, "k3")`.

    Делим по ПЕРВОМУ двоеточию: имя модели само может его содержать
    (`ollama:qwen3:32b` — движок ollama, модель `qwen3:32b`). Голова,
    не являющаяся именем движка, — не «модель с двоеточием», а опечатка:
    принять её значило бы отправить в CLI мусор под видом модели.
    """
    if not isinstance(value, str) or not value:
        return None, ""
    head, sep, tail = value.partition(":")
    if not sep:
        return None, value
    if head not in ENGINES:
        raise EngineError(
            f"executor_model={value!r}: {head!r} — не движок. "
            f"Допустимые префиксы: {', '.join(sorted(ENGINES))}. "
            f"Имя модели с двоеточием обязано нести префикс движка")
    return head, tail


def resolve(config: dict[str, Any],
            task: dict[str, Any] | None = None) -> tuple[str, str]:
    """(движок, модель) для этого вызова исполнителя.

    `task` передаётся ТОЛЬКО когда поле задачи разрешено читать (E10 за
    флагом `skeleton`) — иначе поведение с выключенным флагом перестало
    бы быть сегодняшним, и замер сравнивал бы не с ним.
    """
    raw = config.get("executor_model")
    if task is not None and task.get("executor_model"):
        # Задача переопределяет модель прогона — точечный выбор
        # исполнителя дороже/дешевле общего умолчания на конкретную
        # работу (E10), а не смена умолчания для всей очереди.
        raw = task["executor_model"]
    prefix, model = split_model(raw)
    engine = prefix or config.get("executor_engine") or DEFAULT_ENGINE
    if engine not in ENGINES:
        raise EngineError(
            f"executor_engine={engine!r} — не движок. Допустимо: "
            f"{', '.join(sorted(ENGINES))}")
    return str(engine), model


def executor_argv(engine: str, model: str, prompt: str,
                  config: dict[str, Any], schema: str = "") -> list[str]:
    """Командная строка исполнителя. Чистая функция — её проверяет тест,
    а не живой прогон.

    Форма kimi сохранена побайтово (включая порядок `-m` перед `-p`):
    это регрессионный якорь — всё, что было измерено на плече A, обязано
    собираться сегодня той же строкой.
    """
    if engine == "kimi":
        cmd = ["kimi", "-p", prompt, "--output-format", "stream-json"]
        if model:
            cmd[1:1] = ["-m", model]
        return cmd
    if engine == "claude":
        cmd = ["claude", "-p", prompt,
               "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",
               "--permission-mode", CLAUDE_PERMISSION_MODE,
               "--allowedTools", CLAUDE_ALLOWED_TOOLS,
               "--disallowedTools", CLAUDE_DENIED_TOOLS,
               # Без --mcp-config это означает НОЛЬ mcp-серверов:
               # автономный исполнитель не обязан наследовать то, что
               # оператор смонтировал себе в сессию, — иначе состав
               # инструментов задачи зависит от чужой машины.
               "--strict-mcp-config"]
        if schema:
            cmd += ["--json-schema", schema]
        if model:
            cmd += ["--model", str(model)]
        # Флаги настройки — только если заданы (то же правило, что в
        # promptbuilder.tuning): без явной настройки роль наследует
        # сессионные параметры и поведение не меняется.
        effort = config.get("executor_effort")
        if effort:
            cmd += ["--effort", str(effort)]
        budget = config.get("executor_budget_usd")
        if budget:
            cmd += ["--max-budget-usd", str(budget)]
        return cmd
    raise EngineError(f"у движка {engine!r} нет командной строки")


def report_from_envelope(env: Any) -> dict[str, Any] | None:
    """Отчёт исполнителя из конверта claude: два канала, не один.

    Первый — `structured_output`: контракт, проверенный схемой на стороне
    CLI. Второй — тот же разбор хвоста текста, которым живёт kimi
    (`parsing.report_in`): промпт и без схемы требует финальный JSON, и
    терять готовую работу из-за пустого структурного канала незачем.
    Порядок именно такой: схема старше текста.
    """
    if not isinstance(env, dict):
        return None
    out = env.get("structured_output")
    if isinstance(out, dict) and isinstance(out.get("status"), str):
        return out
    result: Any = env.get("result")
    if isinstance(result, dict):
        # Некоторые исходы кладут в `result` уже разобранный объект.
        return result if "status" in result else None
    text = "" if result is None else str(result)
    return parsing.report_in(text)


def envelope_facts(env: Any) -> dict[str, Any]:
    """Числа конверта claude для метрики: цена, токены, отказы.

    Поле отсутствует — значит его НЕТ, а не ноль: журнал читается как
    данные (§9.3). Именно поэтому здесь None, а не 0.0, и именно поэтому
    у движка kimi этих полей не будет вовсе — его поток их не содержит.
    """
    if not isinstance(env, dict):
        return {}
    usage = env.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    denials = env.get("permission_denials")
    facts: dict[str, Any] = {
        "cost_usd": env.get("total_cost_usd"),
        "terminal_reason": env.get("terminal_reason"),
        "tokens_in": usage.get("input_tokens"),
        "tokens_out": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read_input_tokens"),
        "cache_write": usage.get("cache_creation_input_tokens"),
    }
    if isinstance(denials, list) and denials:
        facts["denied"] = len(denials)
        facts["denied_tools"] = sorted({
            str(d.get("tool_name")) for d in denials if isinstance(d, dict)})
    return facts


# Обрыв по потолку `--max-budget-usd` CLI называет ДВУМЯ словами: в
# `terminal_reason` конверта и в `subtype` результата, и совпадают они не
# всегда. Читаются оба: одного мало, а промах здесь стоит не метрики, а
# диагноза — раунд, обрубленный по деньгам, выглядит аварией.
BUDGET_TRUNCATIONS = frozenset({"budget_exhausted", "error_max_budget_usd"})


def budget_truncated(env: Any) -> bool:
    """Конверт сам говорит, что прогон упёрся в потолок стоимости.

    Слово конверта сильнее кода возврата: CLI выходит НЕНУЛЁВЫМ кодом,
    когда обрубает себя по деньгам, и драйвер по коду честно говорит
    «crash» — а настоящая смерть процесса конверта не оставляет вовсе.
    Поэтому наличие конверта с этим именем и есть отличие «денег не
    хватило» от «процесс умер».
    """
    if not isinstance(env, dict):
        return False
    for key in ("terminal_reason", "subtype"):
        if str(env.get(key) or "") in BUDGET_TRUNCATIONS:
            return True
    return False


def denied_commands(env: Any) -> list[str]:
    """Что именно исполнителю не дали сделать — для журнала человека.

    Отказ по deny-списку не авария: работа могла быть сделана и без него.
    Но это единственное место, где видно, что агент ПЫТАЛСЯ выйти за
    правило, и терять такой факт нельзя — на нём держится разговор о
    том, работает ли запрет как правило или как пожелание.
    """
    if not isinstance(env, dict):
        return []
    out = []
    for d in env.get("permission_denials") or []:
        if not isinstance(d, dict):
            continue
        arg = d.get("tool_input")
        detail = ""
        if isinstance(arg, dict):
            detail = str(arg.get("command") or arg.get("file_path") or "")
        elif arg is not None:
            detail = json.dumps(arg, ensure_ascii=False)[:120]
        out.append(f"{d.get('tool_name')}: {detail}"[:200])
    return out
