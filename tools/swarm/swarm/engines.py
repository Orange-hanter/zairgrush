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
`ollama:`, `zcode:` — по первому двоеточию), иначе решает ключ
`executor_engine`, иначе `kimi`. Префикс старше ключа: так план-дифф
пришпиливает отдельную задачу к другому движку, не трогая настройку
прогона, — ровно тот приём, который E10 уже ввёл для заливок `ollama:`.

Незнакомое значение НЕ откатывается к умолчанию, а отказывает: молча
выбранное умолчание — тот самый отказ, что дал «ноль вызовов эмбеддера
за весь пилот» (см. закрытый список `MEMORY_MODES` в cli).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import parsing  # noqa: E402
import spending  # noqa: E402

# Закрытый список. `ollama` — не CLI, а маршрут на chat-fill (E10):
# движок в том же перечислении, потому что вопрос «кем исполнять» у него
# общий с остальными, а вот командной строки у него нет. `zcode` — CLI
# клиента Z.AI (ZCode.app); команда собирается ниже.
ENGINES = frozenset({"kimi", "claude", "ollama", "zcode"})
DEFAULT_ENGINE = "kimi"

# Бандл десктопа ZCode на macOS. `zcode` часто не в PATH: доктор и argv
# смотрят сюда, если which пуст. Linux/CI без приложения получат
# логическое имя `zcode` и FileNotFound на спавне — это конфигурация
# стенда, а не падение петли на чужом движке.
ZCODE_BUNDLE = pathlib.Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")

# Инструменты исполнителя. Имена без привязки к CLI: тот же набор живёт
# и у claude (--allowedTools), и у zcode (--allowed-tools) — отдельный
# список на движок превратил бы правку политики в тихую рассинхронизацию.
# Ревьюер живёт на read-only наборе (§3.2), исполнителю нужно писать
# файлы и гонять тесты — иначе он не может ни сделать работу, ни
# заполнить `evidence.tests`.
EXECUTOR_ALLOWED_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"

# Промпт исполнителя говорит «git для тебя ТОЛЬКО ДЛЯ ЧТЕНИЯ», и до сих
# пор это была фраза: единственной защитой оставались страж границ и
# `git reset` постфактум. У CLI-дижков запрет становится механикой —
# отказ приходит ДО выполнения команды, и попытка видна в конверте
# (`permission_denials`), то есть не теряется. Проверено пробой
# 2026-08-22: `git commit -am` отклонён, история стенда не сдвинулась.
#
# Списки РАЗНЫЕ, и это осознанно. Паттерны префиксные: `Bash(git tag:*)`
# не умеет отличить read-only `git tag -l` от `git tag v1` — а claude
# ветками и тегами инспектирует (read-only формы ему нужны), поэтому
# branch/tag у него НЕ закрыты. У zcode read-only `git branch -l` менее
# нужен, зато `--mode yolo` снимает подтверждения целиком, и deny-list
# остаётся единственной преградой — его список шире. clean/rm/revert/
# cherry-pick однозначны у обоих: read-only форм у них нет, и у claude
# они закрыты наравне с zcode.
CLAUDE_DENIED_TOOLS = ",".join(
    (
        "Bash(git commit:*)",
        "Bash(git push:*)",
        "Bash(git reset:*)",
        "Bash(git revert:*)",
        "Bash(git rebase:*)",
        "Bash(git merge:*)",
        "Bash(git cherry-pick:*)",
        "Bash(git checkout:*)",
        "Bash(git stash:*)",
        "Bash(git config:*)",
        "Bash(git clean:*)",
        "Bash(git rm:*)",
    )
)
ZCODE_DENIED_TOOLS = ",".join(
    (
        "Bash(git commit:*)",
        "Bash(git push:*)",
        "Bash(git reset:*)",
        "Bash(git revert:*)",
        "Bash(git rebase:*)",
        "Bash(git merge:*)",
        "Bash(git cherry-pick:*)",
        "Bash(git checkout:*)",
        "Bash(git stash:*)",
        "Bash(git config:*)",
        "Bash(git clean:*)",
        "Bash(git rm:*)",
        "Bash(git branch:*)",
        "Bash(git tag:*)",
        # Обход префиксов: `git -C <dir> commit` и `git --git-dir=… commit`
        # не начинаются с `git commit` и под прежние паттерны не попали
        # бы — а yolo не спросит подтверждения. Закрываем ведущие формы.
        # Остаток честно назван: инлайн-формы (`git -c x=y -C …`) язык
        # префиксных паттернов не выражает — без argv-парсера (сознательно
        # не строим) это известная дыра, см. тест ниже.
        "Bash(git -C:*)",
        "Bash(git --git-dir:*)",
    )
)

# `acceptEdits` без списка инструментов не пускает Bash, а без Bash
# исполнитель не запустит тесты. Проба 2026-08-22: связка
# acceptEdits + allowedTools отработала полный круг (правка файла,
# прогон теста, отчёт по схеме) за $0.056.
CLAUDE_PERMISSION_MODE = "acceptEdits"

# ZCode `--prompt` по умолчанию уже yolo, но флаг обязан быть в argv:
# иначе сессионный /mode из TUI мог бы протечь в автономный прогон.
# Рядом в argv идёт --disallowed-tools: контракт --help 0.16.5 не
# фиксирует, чей приоритет выше в связке yolo + deny-list, и молча
# надеяться, что deny-list переживёт режим, нельзя — связка держится
# тестом (test_git_write_is_denied_not_all_git): сменит ли CLI приоритет,
# argv меняется ВМЕСТЕ с тестом, а не проходит незамеченной.
ZCODE_PERMISSION_MODE = "yolo"


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
            f"Имя модели с двоеточием обязано нести префикс движка"
        )
    return head, tail


def resolve(
    config: dict[str, Any], task: dict[str, Any] | None = None
) -> tuple[str, str]:
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
            f"{', '.join(sorted(ENGINES))}"
        )
    return str(engine), model


def run_kind(engine: str) -> str:
    """Как исполнять: CLI-семейство или chat-fill.

    Класс исполнения выводится из двух закрытых таблиц: `_ARGV_BUILDERS`
    (CLI-движки, имя совпадает с argv) и FILL_ENGINES (маршруты без
    командной строки — у них вместо argv петля идёт в chat-fill).
    Движок из ENGINES, забытый в обеих таблицах, отказывает здесь же:
    иначе implement() ушёл бы в ветку kimi с чужим именем.
    """
    if engine in FILL_ENGINES:
        return "fill"
    if engine in _ARGV_BUILDERS:
        return engine
    raise EngineError(f"у движка {engine!r} нет командной строки")


def which(name: str) -> str | None:
    """Обертка над shutil.which — единственная точка поиска бинарей.

    Не ради переиспользования, а ради seam: тест патчит эту функцию,
    а не глобальный shutil всего процесса.
    """
    return shutil.which(name)


def zcode_cmd() -> list[str]:
    """Как звать ZCode на этой машине.

    Не чистая: смотрит PATH и бандл приложения. Тест патчит which /
    is_file, живой прогон на Mac без `zcode` в PATH всё равно находит
    CLI внутри ZCode.app. Бандлу нужен node: если его нет в PATH,
    отказываем ПОНЯТНО здесь, а не FileNotFoundError на спавне, где
    первопричину уже не разглядеть.
    """
    exe = which("zcode")
    if exe:
        return [exe]
    if ZCODE_BUNDLE.is_file():
        node = which("node")
        if node is None:
            raise EngineError(
                "ZCode.app найден, но node нет в PATH — бандлу нужен "
                "node для запуска zcode.cjs"
            )
        return [node, str(ZCODE_BUNDLE)]
    return ["zcode"]


def _kimi_argv(
    model: str, prompt: str, config: dict[str, Any], schema: str, cwd: str
) -> list[str]:
    # Подпись единая со всеми builders таблицы ниже; настройки чужих
    # движков kimi не берёт (см. тест на утечку флагов).
    del config, schema, cwd
    cmd = ["kimi", "-p", prompt, "--output-format", "stream-json"]
    if model:
        cmd[1:1] = ["-m", model]
    return cmd


def _claude_argv(
    model: str, prompt: str, config: dict[str, Any], schema: str, cwd: str
) -> list[str]:
    del cwd  # рабочий каталог claude получает на спавне, см. executor_argv
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
        "--allowedTools",
        EXECUTOR_ALLOWED_TOOLS,
        "--disallowedTools",
        CLAUDE_DENIED_TOOLS,
        # Без --mcp-config это означает НОЛЬ mcp-серверов:
        # автономный исполнитель не обязан наследовать то, что
        # оператор смонтировал себе в сессию, — иначе состав
        # инструментов задачи зависит от чужой машины.
        "--strict-mcp-config",
    ]
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
    cmd += spending.budget_flags(config, "executor_budget_usd")
    return cmd


def _zcode_argv(
    model: str, prompt: str, config: dict[str, Any], schema: str, cwd: str
) -> list[str]:
    # Контракт снят с --help CLI 0.16.5. `--model` в нём нет: хвост
    # executor_model пишется в метрику, на argv не кладётся.
    del model, config, schema
    cmd = [
        *zcode_cmd(),
        "--prompt",
        prompt,
        "--json",
        "--mode",
        ZCODE_PERMISSION_MODE,
        "--allowed-tools",
        EXECUTOR_ALLOWED_TOOLS,
        "--disallowed-tools",
        ZCODE_DENIED_TOOLS,
        "--surface",
        "terminal",
    ]
    if cwd:
        cmd += ["--cwd", cwd]
    return cmd


# Таблица диспетчера — единый перечень CLI-движков: run_kind выводит
# свой класс из этих ключей, так что рассинхрон «пройден run_kind,
# EngineError из argv» невозможен в принципе.
_ARGV_BUILDERS = {
    "kimi": _kimi_argv,
    "claude": _claude_argv,
    "zcode": _zcode_argv,
}
CLI_ENGINES = frozenset(_ARGV_BUILDERS)

# Маршруты без командной строки: у них вместо argv петля идёт в
# chat-fill. Отдельная таблица, а не ветка «engine == "ollama"», чтобы
# новый fill-движок попадал сюда явно, а не молчал в EngineError.
FILL_ENGINES = frozenset({"ollama"})

# Метка теневого плеча дуэли в именах файлов и атрибуте log_tag.
# Живёт здесь, а не рядом с loop.py, из-за графа импортов: loop грузит
# agents лениво (круг), executor agents грузить не может вовсе, а engines
# видят оба. Смена значения — в одном месте; расхождение executor/loop
# стоило бы слитой метрики плеч (см. arm в executor.implement).
SHADOW_LOG_TAG = "-shadow"


def stream_pipeline(kind: str, driver: Any) -> tuple[Any, Any]:
    """(parser, extract) по классу исполнения — каноническая маршрутизация
    потока для всех ролей (executor, tester, unclear).

    Жила копией в трёх файлах, и ветка zcode успела разъехаться по
    дороге: новый движок теперь правится в одном месте. parser —
    функции драйвера (у kimi и zcode поток одинаковый), extract —
    свойство ДВИЖКА: конверт claude читается из result-события, конверт
    zcode — из одного объекта `--json`, у kimi — хвост текста. Класс,
    которого здесь нет, отказывает громко: молчаливый откат в ветку
    kimi разобрал бы чужой формат потока без единого предупреждения.
    """
    if kind == "claude":
        return driver.parse_claude, driver.extract_result_envelope
    if kind == "zcode":
        return driver.parse_kimi, extract_zcode_envelope
    if kind == "kimi":
        return driver.parse_kimi, parsing.extract_report
    raise EngineError(f"у класса исполнения {kind!r} нет конвейера потока")


def executor_argv(
    engine: str,
    model: str,
    prompt: str,
    config: dict[str, Any],
    schema: str = "",
    cwd: str = "",
) -> list[str]:
    """Командная строка исполнителя. Её проверяет тест, а не живой прогон.

    Форма kimi сохранена побайтово (включая порядок `-m` перед `-p`):
    это регрессионный якорь — всё, что было измерено на плече A, обязано
    собираться сегодня той же строкой.

    Единственное обращение к окружению — выбор бинаря zcode в ветке
    zcode (seam `engines.which` и `ZCODE_BUNDLE.is_file`): argv зависит
    от машины, и притворяться чистой функцией она не станет. Тесты
    патчят эти seams, а не filesystem.

    `cwd` в argv попадает только у zcode (`--cwd`): kimi и claude
    получают рабочий каталог при спавне — драйвер передаёт его в
    subprocess.Popen, отдельной опции у этих CLI нет. Параметр оставлен
    в подписи у всех веток, чтобы вызывающая сторона не гадала, какой
    движок его примет, — но не дублируется в их argv.
    """
    builder = _ARGV_BUILDERS.get(engine)
    if builder is None:
        raise EngineError(f"у движка {engine!r} нет командной строки")
    return builder(model, prompt, config, schema, cwd)


def report_from_envelope(env: Any, require: str = "status") -> dict[str, Any] | None:
    """Отчёт роли из конверта claude: два канала, не один.

    Первый — `structured_output`: контракт, проверенный схемой на стороне
    CLI. Второй — тот же разбор хвоста текста, которым живёт kimi
    (`parsing.report_in`): промпт и без схемы требует финальный JSON, и
    терять готовую работу из-за пустого структурного канала незачем.
    Порядок именно такой: схема старше текста.

    `require` — поле, по которому объект опознаётся как отчёт. Умолчание
    `status` — контракт ИСПОЛНИТЕЛЯ, и раньше оно было зашито. Это
    молча выбрасывало ответы ролей с другим контрактом: пурист (E14)
    отдаёт список развилок, у него никакого `status` нет и быть не
    должно. На дымовом прогоне 2026-08-24 он нашёл ровно ту развилку,
    ради которой ставился замер, — и петля сказала человеку
    «спецификация развилок не оставила», заплатив за ответ и выбросив
    его. Форма отчёта одной роли не имеет права быть условием для всех.
    """
    if not isinstance(env, dict):
        return None
    out = env.get("structured_output")
    # Пустое значение поля — не отчёт: require единообразен во всех
    # каналах и с report_from_zcode (там пустая строка отвергается).
    # Пустой СПИСОК при этом валиден: пурист честно отвечает unclear=[].
    if isinstance(out, dict) and out.get(require) not in (None, ""):
        return out
    result: Any = env.get("result")
    if isinstance(result, dict):
        # Некоторые исходы кладут в `result` уже разобранный объект.
        # Та же проверка пустоты, что в structured_output: ключ с пустой
        # строкой — не отчёт (пустой список unclear=[] у пуриста валиден).
        return result if result.get(require) not in (None, "") else None
    text = "" if result is None else str(result)
    parsed = parsing.report_in(text)
    # require единообразен для ВСЕХ каналов: раньше текст-хвост для
    # require == "status" фильтровался молча — любой попавший в result
    # JSON (хвост чужой роли, отчёт пуриста со списком развилок) шёл
    # дальше как status-отчёт исполнителя. Форма отчёта одной роли не
    # имеет права быть условием для всех — в обе стороны.
    if parsed is not None and parsed.get(require) in (None, ""):
        return None
    return parsed


def extract_zcode_envelope(stream: str) -> dict[str, Any] | None:
    """Конверт `--json` ZCode: один объект на весь stdout, не NDJSON.

    Pretty-print обязан разбираться: CLI не обещает одну строку. Скан
    балансный: raw_decode от каждой '{' (тот же приём, что в
    report_from_zcode ниже) берёт первый объект ВЕРХНЕГО УРОВНЯ — префикс
    и суффикс вокруг него терпим: преамбула version съедала бы готовый
    ответ, а статусные строки после JSON («Done in 5s») не имеют права
    его прятать. Граница объекта — строго после закрывающей скобки:
    запятая, `]` или `}` сразу за ним значат, что объект ВЛОЖЕН в чужое
    значение (массив `[{...}, ...]`, обёртка-объект) — конверт `--json`
    это объект верхнего уровня, а не первый попавшийся.
    """
    text = stream.strip()
    if not text:
        return None
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            continue
        rest = text[end:].lstrip()
        if rest.startswith((",", "]", "}")):
            # Запятая или закрывающая скобка сразу после объекта — он
            # вложен в массив/обёртку, это не конверт верхнего уровня.
            continue
        if isinstance(obj, dict):
            return obj
    return None


def report_from_zcode(env: Any, require: str = "status") -> dict[str, Any] | None:
    """Отчёт роли из конверта zcode: поле `response`, не structured_output.

    Если сам объект уже несёт `require` — это и есть отчёт (тестовый
    конверт). Иначе сканируем текст `response` с конца, как report_in,
    но с нужным полем: пурист отдаёт `unclear`, а не `status`.
    """
    if not isinstance(require, str) or not require:
        # Пустое имя поля — ошибка вызывающей стороны, а не «нет отчёта»:
        # иначе молчаливый None замазал бы опечатку в роли.
        raise EngineError(f"имя поля отчёта пустое: {require!r}")
    if not isinstance(env, dict):
        return None
    if env.get(require) not in (None, ""):
        return env
    text = env.get("response")
    blob = "" if text is None else str(text)
    if require == "status":
        return parsing.report_in(blob)
    dec = json.JSONDecoder()
    # Скан с хвоста. Берём самый правый объект с require, но если он
    # вложен в объект с тем же полем, отдаём ВНЕШНИЙ: при равной форме
    # отчёт — контейнер, а не его кусок. Отдельные объекты в прозе
    # вложенными не бывают: правый выигрывает, как и раньше.
    # raw_decode от позиции в самой строке, а не от среза blob[i:]:
    # иначе каждая попытка копировала хвост ответа, и худший случай
    # (совпадений нет) был квадратичным и по времени, и по памяти.
    match: tuple[dict[str, Any], int] | None = None
    for i in range(len(blob) - 1, -1, -1):
        if blob[i] != "{":
            continue
        try:
            cand, end = dec.raw_decode(blob, i)
        except ValueError:
            continue
        if not (isinstance(cand, dict) and require in cand):
            continue
        if match is None or end >= match[1]:
            # end >= конца найденного — кандидат содержит его целиком
            # (или заканчивается там же): он внешнее. Кандидат, что
            # закрылся раньше, — отдельный объект левее, правый сильнее.
            match = (cand, end)
    return match[0] if match else None


# Отображение «поле журнала -> синонимы в конверте». Имена у CLI
# различаются: zcode пишет camelCase, claude — snake_case. Журнал один,
# поэтому нормализация жива в одном месте, а не копией в каждой
# facts-функции.
_ZCODE_USAGE = {
    "tokens_in": ("inputTokens", "input_tokens"),
    "tokens_out": ("outputTokens", "output_tokens"),
    "cache_read": ("cacheReadTokens", "cache_read_input_tokens"),
}
_CLAUDE_USAGE = {
    "tokens_in": ("input_tokens",),
    "tokens_out": ("output_tokens",),
    "cache_read": ("cache_read_input_tokens",),
    "cache_write": ("cache_creation_input_tokens",),
}


def usage_facts(
    usage: Any, fields: Mapping[str, tuple[str, ...]], *, keep_none: bool = False
) -> dict[str, Any]:
    """Нормализация usage-блока конверта по списку синонимов.

    keep_none=False (zcode): поля нет в конверте — его нет и в фактах.
    keep_none=True (claude): поле пишется и со значением None — журнал
    читается как данные (§9.3), и отсутствие цены там НЕ ноль, а факт
    её отсутствия.
    """
    if not isinstance(usage, dict):
        usage = {}
    facts: dict[str, Any] = {}
    for out, names in fields.items():
        value = None
        for n in names:
            if usage.get(n) is not None:
                value = usage[n]
                break
        if value is not None or keep_none:
            facts[out] = value
    return facts


def _denial_facts(env: dict[str, Any]) -> dict[str, Any]:
    """Отказы по deny-списку из конверта: число и список инструментов.

    Общий кусок envelope_facts и zcode_facts: отказ — не авария, но это
    единственное место, где видно, что агент ПЫТАЛСЯ выйти за правило,
    и терять такой факт в метрике нельзя ни у одного CLI-движка.
    """
    denials = env.get("permission_denials")
    if not (isinstance(denials, list) and denials):
        return {}
    return {
        "denied": len(denials),
        "denied_tools": sorted(
            {str(d.get("tool_name")) for d in denials if isinstance(d, dict)}
        ),
    }


def zcode_facts(env: Any) -> dict[str, Any]:
    """Числа конверта zcode для метрики. Поле есть — пишем, нет — молчим.

    Usage в CLI 0.16 camelCase (`inputTokens`). USD читается по факту:
    `total_cost_usd` есть в конверте — пишем в cost_usd, нет — молчим
    (в захваченном на пробе конверте его не было). Нулевую цену выдумывать
    нельзя — как у kimi. Отказы по deny-списку пишем наравне с claude:
    конверт CLI-движков их несёт, и терять их значит ослепнуть на
    вопрос, работает ли запрет как правило или как пожелание.
    """
    if not isinstance(env, dict):
        return {}
    facts = usage_facts(env.get("usage"), _ZCODE_USAGE)
    facts.update(_denial_facts(env))
    cost = env.get("total_cost_usd")
    if cost is not None:
        facts["cost_usd"] = cost
    return facts


def envelope_facts(env: Any) -> dict[str, Any]:
    """Числа конверта claude для метрики: цена, токены, отказы.

    Поле отсутствует — значит его НЕТ, а не ноль: журнал читается как
    данные (§9.3). Именно поэтому здесь None, а не 0.0, и именно поэтому
    у движка kimi этих полей не будет вовсе — его поток их не содержит.
    """
    if not isinstance(env, dict):
        return {}
    facts: dict[str, Any] = {
        "cost_usd": env.get("total_cost_usd"),
        "terminal_reason": env.get("terminal_reason"),
        **usage_facts(env.get("usage"), _CLAUDE_USAGE, keep_none=True),
    }
    facts.update(_denial_facts(env))
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
