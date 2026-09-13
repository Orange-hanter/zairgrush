#!/usr/bin/env python3
"""Планировщик петли (§3.1): goal -> план-дифф с механической валидацией.

Два режима:
  plan   --goal "<цель>"          — декомпозиция цели в задачи
  replan --task <id> --dispute f  — пересмотр плана по спору исполнителя

Планировщик выдаёт НЕ новый tasks.json, а дифф к нему (add/update/remove).
Оркестратор валидирует дифф механически и только потом применяет:
схема, уникальность id, существование целей update/remove, разрешимость
deps, отсутствие циклов, paths/acceptance обязательны в add и не могут
обнуляться в update, легальность статусов (done недоступен плану).
Невалидный дифф = одна повторная попытка, затем эскалация.

Артефакты: plan-metrics.jsonl, raw/<mode>-<n>.json, применённый tasks.json.
"""
import argparse
import copy
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# loop — нормальным import'ом, а не _load: если петля уже загрузила его,
# берётся ТА ЖЕ копия модуля — а с ней и тот же детектор квоты. Класс
# QuotaExceededError теперь единый в verdicts.py, но лишние копии loop
# всё равно не нужны; см. verdicts.quota_exception.
import loop  # noqa: E402 — каталог добавлен строкой выше
import obs  # noqa: E402 — каталог добавлен строкой выше
import pyindex  # noqa: E402 — каталог добавлен строкой выше

PLAN = pathlib.Path(__file__).resolve().parent
SCHEMA = (PLAN.parent / "schemas" / "plan-diff.schema.json").read_text()
# Статусы, доступные план-диффу. `done` намеренно исключён: «сделано»
# ставит только оркестратор после approve, а план, объявляющий работу
# готовой, обходил бы и gate, и ревью.
PLAN_STATUS = {"pending", "blocked"}

# Планировщику нужен тот же запас, что и ревьюеру: на реальном проекте
# зашитые $1.50 обрубали ОБЕ попытки (PILOT-1: $1.63 и $1.67, ops=0), и
# роль, объявленная в §3.1, ни разу не отработала.
DEFAULT_PLAN_BUDGET = 4.0

# Потолок стены времени на один вызов. У ревьюера он был с самого начала,
# у планировщика subprocess.run шёл вовсе без timeout: зависший claude
# держал бы `swarm go` вечно и молча.
DEFAULT_PLAN_TIMEOUT = 900

# Потолок на прогон сьюта стенда при сборке карты: зависший тест держал
# бы `swarm plan`/`go` вечно тем же молчанием, что и зависший claude.
SUITE_TIMEOUT = 300


def artifacts_dir(root: str | pathlib.Path | None = None) -> pathlib.Path:
    """Куда складывать сырые ответы и метрики планировщика.

    Раньше — всегда внутрь исходников инструмента (`swarm/raw/`): следы
    прогона по чужому репозиторию оседали в самом рое, на доске проекта их
    не было, а два проекта подряд писали в один файл. Место артефактов —
    рядом с остальным состоянием петли, в `.swarm/` целевого репозитория.
    """
    if root is None:
        return PLAN
    return pathlib.Path(root) / ".swarm"


def metric(root: str | pathlib.Path | None = None, **row: Any) -> None:
    obs.stamp(row)
    path = artifacts_dir(root) / "plan-metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def repo_map(stand: str | pathlib.Path) -> tuple[str, str]:
    """Карта репозитория для планировщика: модули, тесты, размер сьюта."""
    stand = pathlib.Path(stand)
    # Общий набор исключений (pyindex.EXCLUDED_DIRS): без него список файлов
    # стенда уносил в промпт весь .venv — тысячи строк site-packages.
    files = sorted(p.relative_to(stand).as_posix()
                   for p in stand.rglob("*.py")
                   if not pyindex.excluded(p, stand))
    # Красный сьют — не ошибка вызова, а факт о стенде, который
    # планировщику как раз и нужно знать: он идёт в промпт.
    try:
        r = subprocess.run(["python3", "-m", "unittest", "discover",
                            "-s", "tests", "-t", "."],
                           capture_output=True, text=True, cwd=stand,
                           timeout=SUITE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        # Зависший сьют не имеет права вешать `swarm plan`/`go`: в промпт
        # идёт честное «неизвестно» вместо вечного молчаливого ожидания.
        return "\n".join(files), (f"неизвестно — прогон тестов не уложился "
                                  f"в {SUITE_TIMEOUT}с и был остановлен")
    tail = (r.stdout + r.stderr).strip().splitlines()[-2:]
    return "\n".join(files), " ".join(tail)


def plan_prompt(goal: str, tasks: list[dict[str, Any]], files: str,
                suite: str, memory: str = "") -> str:
    # Память (E9): уроки прошлых прогонов — сразу после цели, до
    # изменчивых блоков. Пустая строка — промпт байт-в-байт прежний.
    mem = f"\n{memory.rstrip()}\n" if memory else ""
    return f"""Ты — планировщик в автоматической петле разработки. \
Ответ парсится механически.

## Цель
{goal}
{mem}
## Текущее состояние репозитория
Файлы:
{files}

Состояние тестов: {suite}

## Текущая очередь задач
{json.dumps(tasks, ensure_ascii=False, indent=1)}

## Что от тебя требуется
Выдай ПЛАН-ДИФФ — список операций add/update/remove над очередью, а не новый
файл целиком. Правила, которые оркестратор проверяет механически:

- `id` задачи — короткий уникальный хеш-подобный идентификатор (4 символа,
  латиница+цифры), не порядковый номер; не должен совпадать с существующими;
- обязательны `paths` (glob-список файлов, которые разрешено править) и
  `acceptance` (проверяемые критерии приёмки, по ним будет судить ревьюер);
- `deps` — только id задач, существующих в очереди или добавляемых этим же
  диффом; циклы запрещены;
- `type`: feature (правит продукционный код), feature-tests (пишет и свои
  новые тесты), test-task (правит только тесты), idea (замечание на будущее).
  Для ЛЮБОГО типа защищённый файл (tests/**, *.toml, lock-файлы) можно
  править, только если `paths` целятся в него ЯВНО — широкий глоб защиту
  не снимает;
- если правка меняет поведение, зафиксированное существующим тестом
  (golden-числа, пришпиленные значения), включи этот тест в `paths` —
  иначе задача сгорит на проверке границ, не дойдя до ревью;
- если задача добавляет зависимость крейта/пакета, включи соответствующие
  манифесты и lock-файлы в `paths` явно;
- `gate`: full (полный сьют — по умолчанию для правок существующего кода);
- `test_module` — модуль тестов, которым проверяется задача;
- задачи должны быть маленькими: одна задача = один связный результат,
  сходящийся за 1–2 итерации;
- `paths` обязаны учитывать ФАКТИЧЕСКОЕ состояние репозитория выше, включая
  файлы, созданные соседними задачами очереди;
- если задача требует НЕЗАВИСИМОГО эталона, критерий приёмки обязан назвать
  и ЧЕМ сверять, и чем сверять ЗАПРЕЩЕНО — поля, которые вычисляет сама
  проверяемая реализация. Иначе тест сверяет реализацию саму с собой, и
  дефект в ней невидим;
- при декомпозиции модуля с несколькими функциями план МОЖЕТ использовать
  skeleton-режим вместо одной большой задачи: одна задача-«скелет»
  (сильный исполнитель) создаёт файл(ы) с ФИНАЛЬНЫМИ сигнатурами,
  контрактными docstring'ами, защищёнными контрактными тестами (явно
  перечисленными в её `paths` — явное перечисление снимает защиту) и
  телами функций `raise NotImplementedError`; затем по одной задаче-
  «заливке» на файл — механическое заполнение тел. Контрактные тесты
  скелета ОБЯЗАНЫ пропускаться (unittest.SkipTest) на
  `NotImplementedError`, а не падать: незаполненное тело — «ещё не
  сделано», не красный baseline, иначе ни скелет не пройдёт свой гейт,
  ни заливка не стартует (урок живого прогона E10);
- задача-заливка ОБЯЗАНА: целиться РОВНО в один конкретный путь без
  glob-символов (`*?[`) в `paths`; зависеть (`deps`) от задачи-скелета;
  задавать `executor_model` (например, "ollama:kimi-k2.7-code" — заливку
  ведёт дешёвая модель); называть контрактный тест скелета в `acceptance`;
  выставлять `frozen_signatures: true`. Первые два требования оркестратор
  проверяет механически: `executor_model`, начинающийся с `ollama:`, без
  ровно одного пути без glob-символов или без непустого `deps` —
  отклоняется;
- каждый критерий приёмки задачи типа feature-tests и задачи-заливки
  обязан НАЗЫВАТЬ доказывающий тест в форме `module::test_name`, либо
  прямо объяснить, почему критерий не проверяется тестом — такие задачи
  несут тесты по определению, и acceptance без ссылки на тест непроверяем
  механически.

В поле analysis сначала рассуждай, потом формируй ops.
"""


def replan_prompt(task: dict[str, Any], dispute: str,
                  tasks: list[dict[str, Any]], files: str, suite: str,
                  memory: str = "") -> str:
    # Память (E9) нужна ЗДЕСЬ больше, чем где-либо: replan вызывают
    # ровно после спора о границах, а прошлые споры — это и есть то,
    # что владелец уже решил (PILOT-1: семь границ из семи расширены,
    # каждая — раунд и ожидание человека). Пустая строка оставляет
    # промпт байт-в-байт прежним.
    mem = f"\n{memory.rstrip()}\n" if memory else ""
    return f"""Ты — планировщик в автоматической петле разработки. \
Ответ парсится механически.

## Ситуация
Задача ушла в blocked через канал dispute: исполнитель заявил, что требования
невыполнимы в заданных рамках. Твоя работа — устранить причину диффом к плану.
{mem}
## Задача
{json.dumps(task, ensure_ascii=False, indent=1)}

## Спор исполнителя (dispute)
{json.dumps(dispute, ensure_ascii=False, indent=1)}

## Текущее состояние репозитория
Файлы:
{files}

Состояние тестов: {suite}

## Текущая очередь задач
{json.dumps(tasks, ensure_ascii=False, indent=1)}

## Что от тебя требуется
Выдай ПЛАН-ДИФФ, который делает задачу выполнимой: расширь `paths`, уточни
`acceptance`, при необходимости разбей задачу на несколько или сними
неисполнимое требование. Ограничения те же, что и при планировании: paths и
acceptance обязательны, deps без циклов, id уникальны, задачи маленькие.
Верни задачу в статус pending, если она снова исполнима.

Если спорящая задача — часть skeleton-режима (`executor_model`/
`frozen_signatures`), контракт скелета не свят: если
исполнитель прав и сигнатуры мешают, план вправе их изменить в
задаче-скелете — но тогда объясни это решение в `reason`, а не снимай
`frozen_signatures` тихо. Как и при планировании: задача-заливка целится
РОВНО в один путь без glob-символов (`*?[`) в `paths`, зависит (`deps`) от
скелета и задаёт `executor_model`; каждый критерий приёмки задачи типа
feature-tests и задачи-заливки называет доказывающий тест
(`module::test_name`) либо прямо объясняет, почему он не тестируем.

В поле analysis объясни, кто прав в споре и почему план оказался устаревшим.
"""


def tuning_flags(model: str | None = None,
                 effort: str | None = None) -> list[str]:
    """Флаги модели и уровня усилия — только если заданы.

    Пустой список по умолчанию: без явной настройки роль наследует
    сессионные параметры, и поведение прогона не меняется.
    """
    flags: list[str] = []
    if model:
        flags += ["--model", str(model)]
    if effort:
        flags += ["--effort", str(effort)]
    return flags


def call_planner(prompt: str, tag: str, attempt: int = 1,
                 root: str | pathlib.Path | None = None,
                 budget: float | None = None, model: str | None = None,
                 effort: str | None = None, timeout: float | None = None,
                 ) -> tuple[dict[str, Any] | None, str | None]:
    """Вызов планировщика. -> (план-дифф | None, причина отказа | None).

    Причина возвращается отдельно, потому что «модель ответила мусором» и
    «вызов обрублен по бюджету» лечатся по-разному, а оператор различает их
    только по тому, что ему сказали. На PILOT-1 обе попытки были обрублены
    по зашитым $1.50, а в консоль ушло «невалидный JSON» — диагноз, ведущий
    искать поломку в схеме вместо лимита.

    Отказ по квоте — исключение, а не причина в кортеже: это состояние
    ПРОГОНА, а не вызова. Пока квота шла обычным «no_output», повтор
    сжигал второй заведомо обречённый вызов, а диагноз посылал оператора
    читать сырой ответ вместо «подожди и повтори».
    """
    t0 = time.time()
    try:
        # Промпт едет через stdin, не argv: планировочный промпт несёт
        # цель и список задач целиком и под ARG_MAX не обязан помещаться
        # (NXT-006). У claude stdin — документированный канал -p.
        r = subprocess.run(["claude", "-p", "--output-format", "json",
                            "--json-schema", SCHEMA, "--allowedTools",
                            "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*)",
                            "--max-budget-usd",
                            str(budget or DEFAULT_PLAN_BUDGET),
                            *tuning_flags(model, effort)],
                           input=prompt,
                           capture_output=True, text=True,
                           timeout=timeout or DEFAULT_PLAN_TIMEOUT,
                           # Без cwd планировщик читает репозиторий по каталогу
                           # процесса, а не по --root: Read/Grep смотрели бы не
                           # в тот проект, для которого строится план.
                           cwd=str(root) if root else None, check=False)
    except subprocess.TimeoutExpired:
        # Зависший вызов — результат со своей причиной, а не смерть
        # `swarm go`: решение о повторе принимает plan_with_retry.
        metric(root=root, mode=tag, attempt=attempt,
               dur_s=round(time.time() - t0, 1), reason="timeout")
        return None, "timeout"
    dur = round(time.time() - t0, 1)
    raw_dir = artifacts_dir(root) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{tag}-a{attempt}.json").write_text(r.stdout)
    diff, cost, reason = None, None, None
    try:
        env = json.loads(r.stdout)
        quota = loop.quota_error(env)
        if quota:
            # Метрика — ДО исключения: вызов случился и стоил времени,
            # и прогон без этой строки невоспроизводим.
            metric(root=root, mode=tag, attempt=attempt, dur_s=dur,
                   reason="quota", quota_wait=True, provider_message=quota)
            raise loop.QuotaExceededError(quota)
        diff = env.get("structured_output")
        cost = env.get("total_cost_usd")
        if not isinstance(diff, dict):
            reason = env.get("terminal_reason") or env.get("subtype") or "no_output"
    except ValueError:
        reason = "unparsable_envelope"
    metric(root=root, mode=tag, attempt=attempt, dur_s=dur, cost_usd=cost,
           ops=len((diff or {}).get("ops", [])), reason=reason)
    return diff, reason


# Причины, при которых повтор обречён: вызов не «ответил плохо», а был
# оборван снаружи, и второй такой же оборвётся там же. На PILOT-1 повтор
# стоил ещё $1.67 и дал тот же ноль.
TERMINAL_REASONS = {"budget_exhausted", "error_max_budget_usd"}

PLAN_DIAGNOSIS = {
    "budget_exhausted": (
        "планировщик обрублен по бюджету, плана нет. Это НЕ ошибка формата: "
        "ответ модели корректен, в нём просто нет плана. Подними "
        "plan_budget_usd в swarm.toml или сузь цель"),
    "error_max_budget_usd": (
        "планировщик обрублен по бюджету, плана нет. Подними "
        "plan_budget_usd в swarm.toml или сузь цель"),
    "unparsable_envelope": (
        "ответ планировщика не разобран как JSON — смотри сырой ответ в "
        ".swarm/raw/"),
    "no_output": (
        "планировщик завершился без структурированного плана — смотри "
        "сырой ответ в .swarm/raw/"),
    "timeout": (
        "планировщик не уложился в потолок времени и был остановлен; "
        "сырого ответа нет — вызов не завершился. Подними plan_timeout "
        "в swarm.toml или сузь цель"),
}


def plan_with_retry(prompt: str, mode: str, tasks: list[dict[str, Any]],
                    root: str | pathlib.Path | None = None,
                    budget: float | None = None,
                    ui: Callable[[str], None] = print,
                    model: str | None = None, effort: str | None = None,
                    timeout: float | None = None,
                    ) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """Не более двух попыток, и вторая — только если она осмысленна.

    -> (diff | None, список ошибок, причина отказа | None).

    Политика повтора живёт здесь одна на всех вызывающих: раньше она была
    продублирована в CLI петли и в собственном main планировщика, и
    разошлась — второй экземпляр молча ретраил обрыв по бюджету.

    Таймаут ретраится (зависание не детерминировано), обрыв по бюджету —
    нет (тот же промпт кончится там же), квота выходит исключением ещё из
    call_planner (это состояние прогона, повтор сейчас обречён).
    """
    diff, reason = call_planner(prompt, mode, root=root, budget=budget,
                                model=model, effort=effort, timeout=timeout)
    if reason in TERMINAL_REASONS:
        return None, [PLAN_DIAGNOSIS[reason]], reason
    if diff is None:
        # Плана нет вовсе (timeout/no_output) — это НЕ ошибка формата, и
        # модели нечего «исправлять»: повтор идёт с исходным промптом.
        # Раньше сюда шло «план-дифф невалиден» (ложный класс диагноза), а
        # в промпт ретрая попадала инструкция оператору про swarm.toml.
        ui(f"план не получен ({reason or 'нет ответа'}), повторная попытка")
        retry_prompt = prompt
    else:
        errs = validate_plan_diff(diff, tasks)
        if not errs:
            return diff, [], None
        ui("план-дифф невалиден, повторная попытка:")
        for e in errs:
            ui(f"  {e}")
        retry_prompt = (prompt + "\n\n## Ошибки прошлой попытки\n"
                        + "\n".join(errs))
    diff, reason = call_planner(
        retry_prompt, mode, attempt=2, root=root, budget=budget, model=model,
        effort=effort, timeout=timeout)
    if reason in TERMINAL_REASONS:
        return None, [PLAN_DIAGNOSIS[reason]], reason
    errs = validate_plan_diff(diff, tasks) if diff else [
        PLAN_DIAGNOSIS.get(reason or "", "план-дифф не получен")]
    return (diff, [], None) if not errs else (None, errs, reason)


# Формат id из промпта планировщика: 4 символа, латиница+цифры. Проверяется
# механически на add — иначе обещание промпта остаётся пожеланием.
_ID_RE = re.compile(r"[A-Za-z0-9]{4}")


def _bad_deps(t: dict[str, Any]) -> bool:
    """deps обязаны быть списком строк: dict внутри `d not in universe`
    ронял валидатор TypeError, и мусор улетал исключением вместо ошибки."""
    deps = t.get("deps")
    return deps is not None and (
        not isinstance(deps, list)
        or any(not isinstance(d, str) for d in deps))


# Glob-символы: путь fill-задачи обязан быть КОНКРЕТНЫМ файлом, иначе
# страж loop.frozen_signatures (E10) не может назвать единственный файл,
# чьи сигнатуры снимать.
_GLOB_CHARS = "*?["


def _skeleton_errors(t: dict[str, Any]) -> list[str]:
    """E10: механическая половина skeleton-режима — обещания промпта
    (единственный конкретный путь, зависимость от скелета) без проверки
    здесь остаются пожеланием модели, как и id-формат до `_ID_RE`.

    Оба поля необязательны для ОБЫЧНОЙ задачи — проверка срабатывает,
    только если поле присутствует в теле ЭТОЙ операции: частичный update,
    не трогающий executor_model/frozen_signatures, её не касается (тот же
    принцип, что у `_bad_deps`).
    """
    errs = []
    model = t.get("executor_model")
    if isinstance(model, str) and model.startswith("ollama:"):
        paths = t.get("paths")
        if not (isinstance(paths, list) and len(paths) == 1
                and isinstance(paths[0], str)
                and not any(c in paths[0] for c in _GLOB_CHARS)):
            errs.append("executor_model=ollama:* требует ровно один путь "
                        "без glob-символов (*?[) в paths — заливка не имеет "
                        "права трогать чужие файлы")
        deps = t.get("deps")
        if not (isinstance(deps, list) and deps):
            errs.append("executor_model=ollama:* требует непустой deps — "
                        "заливка обязана зависеть от задачи-скелета")
    fs = t.get("frozen_signatures")
    if fs is not None and not isinstance(fs, bool):
        errs.append("frozen_signatures должен быть bool")
    return errs


# Типы, которые НЕСУТ тесты по определению: feature-tests пишет их сама, а
# fill-задача (executor_model=ollama:*) доказывается контрактным тестом
# скелета. Обычный feature — намеренно НЕ проверяется: тест для него часто
# пишет другая задача (или ревьюер), и требование ссылки на КАЖДОМ feature
# было бы просто шумом, забивающим редкие настоящие пропуски.
def _missing_ac_ref(t: dict[str, Any]) -> bool:
    """Feature 4: критерий приёмки без `module::test_name` непроверяем
    механически — либо ссылка есть, либо acceptance обязан явно сказать,
    что критерий тестом не доказывается (см. правило в plan_prompt)."""
    model = t.get("executor_model")
    is_test_carrying = (t.get("type") == "feature-tests"
                        or (isinstance(model, str)
                            and model.startswith("ollama:")))
    if not is_test_carrying:
        return False
    acceptance = t.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        # Пустой/нелегальный acceptance уже ловит отдельная проверка —
        # здесь дублировать нечем и незачем.
        return False
    return not any("::" in str(a) for a in acceptance)


# --- граница задачи: спутники, которых план обычно не замечает ----------
#
# Золотой набор PILOT-1: СЕМЬ споров исполнителя из семи признаны
# владельцем — каждый раз граница задачи была поставлена неверно, и
# каждый раз это стоило раунда плюс ожидания ответа человека (медиана
# 24 минуты, худшее 21 час). Споры складываются в повторяющиеся классы
# файлов-спутников:
#
#   реестр/регистрация  — engine.rs зовёт новое правило (q011)
#   пришпиленный эталон — golden/corpus/фикстура ждёт прежних чисел
#                         (q012, q020)
#   документ с числами  — docs фиксируют формат или замер (q014, q017)
#   производитель выше  — признак рождается в импортёре (q005, q016)
#
# Первая версия искала ОДНУ улику — упоминание основы имени файла задачи
# в чужом тексте — и на реальном корпусе дала 1 спор из 7 при 7,1
# предупреждения на задачу: полезного меньше, чем шума. Замер вскрыл
# три дефекта, и все три чинятся здесь:
#
#   1. Глоб выбрасывался целиком, а реальные задачи почти всегда
#      пишут границу глобом (`src/**`, `rules/*.rs`) — линтер работал
#      на огрызке границы. Теперь глобы РАСКРЫВАЮТСЯ по дереву.
#   2. Ссылка бывает прямой: файл ЗАДАЧИ сам называет путь снаружи
#      (`include_str!("../../tests/fixtures/golden/nets.txt")`). Это
#      самая сильная улика из всех, и её вообще не искали.
#   3. Отбор шёл по порядку обхода, а не по силе улики: настоящие
#      спутники не влезали в потолок, вытесненные случайными
#      совпадениями из bench/ и .scratch/. Теперь улики ранжируются, и
#      наружу выходит короткий верх списка.
#
# Линтер СОВЕТУЕТ, а не запрещает: ссылка — улика, а не доказательство,
# и решает человек. Молчание тоже не гарантия: два спора из семи
# (q005, q016 — «признак рождается в импортёре») текстового следа не
# имеют вовсе и механически не ловятся ничем.
_SATELLITE_STOP = {
    "mod", "lib", "main", "test", "tests", "index", "init", "impl",
    "util", "utils", "types", "type", "config", "common", "core", "api",
    "app", "src", "data", "base", "node", "item", "list", "file", "path",
    "keys", "save", "load", "error", "errors", "state", "model", "view",
}
_SKIP_DIRS = {".git", ".svn", ".hg", "target", "node_modules", ".venv",
              "venv", "__pycache__", ".swarm", "dist", "build", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".claude"}
_TEXT_SUFFIXES = {".rs", ".py", ".toml", ".md", ".txt", ".json", ".yaml",
                  ".yml", ".js", ".ts", ".tsx", ".go", ".c", ".h", ".cpp",
                  ".hpp", ".java", ".rb", ".sh", ".sql", ".cfg", ".ini"}
_PINNED_MARKERS = ("/tests/", "/test/", "/fixtures/", "/fixture/", "/golden/",
                   "/corpus/", "/snapshots/", "/testdata/")
_MAX_SCAN_FILES = 4000
_MAX_FILE_BYTES = 262_144
_MAX_OWN_READS = 80
_LIMIT = 6
# Путь, названный строкой: `"tests/fixtures/golden/nets.txt"`,
# `include_str!(...)`, ссылка из markdown. Расширение обязательно —
# без него в улов идут слова.
_PATHISH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\-]{2,}\.[A-Za-z0-9]{1,5}")


def _rel(root: pathlib.Path, p: pathlib.Path) -> str:
    return str(p.relative_to(root)).replace(os.sep, "/")


def _own_files(root: pathlib.Path, paths: list[str]) -> set[str]:
    """Границы задачи в виде КОНКРЕТНЫХ файлов.

    Глоб — это не «нет файлов», а «файлы перечислены иначе»: пока он
    выбрасывался, линтер видел долю границы и судил по ней (замер
    PILOT-1: 1 спор из 7).
    """
    own: set[str] = set()
    for p in paths:
        if not isinstance(p, str) or not p:
            continue
        own.add(p.rstrip("/"))
        if any(ch in p for ch in "*?["):
            # `src/**` в pathlib раскрывается в КАТАЛОГИ, а не в файлы:
            # без досыпки `src/**/*` самая частая форма границы давала
            # пустое множество, и линтер молчал ровно там, где нужен.
            pats = [p] + ([p.rstrip("/") + "/*"] if p.endswith("**") else [])
            for pat in pats:
                try:
                    matched = list(root.glob(pat))
                except (ValueError, OSError):
                    continue
                own.update(_rel(root, m) for m in matched if m.is_file())
            continue
        here = root / p
        if here.is_dir():
            own.update(_rel(root, m) for m in here.rglob("*") if m.is_file())
    return own


def _satellite_tokens(own: set[str]) -> tuple[set[str], set[str]]:
    """Слова, по которым файл задачи узнаётся в чужом тексте.

    Две разные улики, и путать их дорого (замер PILOT-1):

    * ОСНОВА имени (`erc02`) — как файл кода зовут в импортах, реестрах
      и прозе: `use crate::rules::Erc02…`, `mod erc02`, ссылка из
      markdown. Регистр не значит ничего: тип называется `Erc02Duplicate…`,
      а файл — `erc02.rs`, и поиск обязан считать это одним и тем же.
    * ПОЛНОЕ имя с расширением (`suppressions.toml`) — единственная
      форма, годная для данных. Основы фикстур и корпусов («Basis_1»,
      «ArduinoLCD») в чужом тексте — шум: по ним «ссылались» .scratch и
      бенч, вытесняя настоящих спутников из потолка.
    """
    stems: set[str] = set()
    names: set[str] = set()
    for p in own:
        if any(ch in p for ch in "*?["):
            continue
        pp = pathlib.PurePosixPath(p)
        if pp.suffix and len(pp.name) >= 5:
            names.add(pp.name)
        if any(m in "/" + p.lower() for m in _PINNED_MARKERS):
            continue           # данные узнаются только полным именем
        if len(pp.stem) >= 4 and pp.stem.lower() not in _SATELLITE_STOP:
            stems.add(pp.stem)
    return stems, names


def _classify_satellite(rel: str, protected: list[str]) -> str:
    low = "/" + rel.lower()
    if any(fnmatch.fnmatch(rel, pat) for pat in protected):
        return "pinned"
    if any(m in low for m in _PINNED_MARKERS):
        return "pinned"
    if rel.lower().endswith((".md", ".rst", ".adoc")):
        return "docs"
    return "referrer"


_SATELLITE_HINT = {
    "forward": ("файл задачи прямо ссылается на этот файл — правка почти "
                "наверняка потребует обновить и его (PILOT-1: q012)"),
    "pinned_dir": ("файлы задачи называют по имени содержимое этого "
                   "каталога пришпиленных данных — правка формата тянет "
                   "за собой фикстуру целиком (PILOT-1: q020)"),
    "registry": ("файл вне границ ссылается сразу на несколько файлов "
                 "задачи: похоже на реестр или регистрацию "
                 "(PILOT-1: q011)"),
    "referrer": ("файл вне границ ссылается на файл задачи: реестр, "
                 "документ с числами или производитель выше по потоку "
                 "(PILOT-1: q014, q017)"),
}


def _proximity(rel: str, own: set[str]) -> int:
    """Улика тем весомее, чем ближе файл к самой задаче.

    Совпадение основы имени в чужом крейте — чаще всего случайность
    (замер: bench/ и .scratch/ вытесняли настоящие спутники из потолка).
    """
    parts = rel.split("/")
    best = 0
    for o in own:
        op = o.split("/")
        n = 0
        while n < min(3, len(parts) - 1, len(op) - 1) and parts[n] == op[n]:
            n += 1
        best = max(best, n)
    return best

def boundary_warnings(root: str | pathlib.Path, task: dict[str, Any],
                      protected_paths: list[str] | None = None,
                      limit: int = _LIMIT) -> list[dict[str, str]]:
    """Файлы-спутники вне `paths`, о которых задача, вероятно, споткнётся.

    Возвращает предупреждения (не ошибки), отсортированные по силе
    улики и обрезанные до `limit`. Потолок мал намеренно и замером
    оправдан: на корпусе PILOT-1 подъём потолка с 3 до 10 не добавил
    НИ ОДНОГО пойманного спора — то, что линтер знает, он знает сразу,
    а длинный хвост состоит из совпадений. Ничего не читает у
    планировщика и не зовёт LLM: обычный обход дерева с потолками.
    """
    root_p = pathlib.Path(root)
    paths = [p for p in (task.get("paths") or []) if isinstance(p, str)]
    own = _own_files(root_p, paths)
    stems, names = _satellite_tokens(own)
    protected = list(protected_paths or [])

    # Один обход дерева. Тексты НЕ копятся: с каждого файла снимаются
    # только совпадения — иначе линтер держал бы в памяти весь репозиторий.
    inside: list[str] = []                        # файлы задачи
    all_rel: list[str] = []                       # все пути снаружи границ
    by_name: dict[str, list[str]] = {}
    seen: list[tuple[str, set[str], list[str]]] = []   # rel, основы, имена
    df: dict[str, int] = {}
    scanned = 0
    for path in sorted(root_p.rglob("*")):
        if scanned >= _MAX_SCAN_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        rel = _rel(root_p, path)
        scanned += 1
        if rel in own:
            inside.append(rel)
            continue
        # Указатель — по ПУТИ: чтобы назвать файл спутником, читать его
        # не нужно. Пришпиленный эталон почти всегда велик (в замере —
        # 300 КБ), и потолок чтения вычёркивал ровно тот класс файлов,
        # ради которого линтер написан.
        by_name.setdefault(path.name, []).append(rel)
        all_rel.append(rel)
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            low = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        matched = {t for t in stems if t.lower() in low}
        named = sorted({n for n in names if n.lower() in low})
        if matched or named:
            seen.append((rel, matched, named))
        for t in matched:
            df[t] = df.get(t, 0) + 1

    # Редкость слова — часть улики. «components» или «project» встречает
    # полрепозитория: это общее слово, а не имя файла задачи, и на
    # широкой границе (`src/**`) такие совпадения хоронили настоящих
    # спутников под собой (замер PILOT-1: спорный файл на 22-м месте).
    common = max(3, len(all_rel) // 10)
    rare = {t for t in stems if df.get(t, 0) <= common}

    found: dict[str, dict[str, Any]] = {}

    def offer(rel: str, signal: str, token: str, score: int) -> None:
        prev = found.get(rel)
        if prev is None or score > int(prev["score"]):
            found[rel] = {"file": rel, "category": _classify_satellite(
                rel, protected), "token": token, "signal": signal,
                "hint": _SATELLITE_HINT[signal], "score": score}

    # 1. Прямая ссылка: файл ЗАДАЧИ называет путь снаружи. Улика сильнее
    #    всех прочих — она явная, а не выведенная.
    loose: dict[str, list[str]] = {}
    for rel in inside[:_MAX_OWN_READS]:
        try:
            text = (root_p / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in dict.fromkeys(_PATHISH.findall(text)):
            cand = m.replace("\\", "/").lstrip("./")
            if not cand or cand in own:
                continue
            if "/" in cand:
                for hit in [r for r in all_rel
                            if r == cand or r.endswith("/" + cand)][:2]:
                    offer(hit, "forward", cand, 9 + _proximity(hit, own))
                continue
            # Голое имя файла — улика слабее: путь не назван, и годится
            # она, только если снаружи это пришпиленные данные.
            for hit in [r for r in by_name.get(cand, [])
                        if _classify_satellite(r, protected) == "pinned"][:2]:
                loose.setdefault(str(pathlib.PurePosixPath(hit).parent),
                                 []).append(hit)

    # Пришпиленные данные ходят каталогами: задача, правящая формат,
    # называет по имени половину фикстуры. Десять строк про соседние
    # файлы одного каталога — это одна улика, и говорить о ней надо
    # один раз, каталогом (замер: иначе спорный файл тонул в своих же
    # соседях).
    for folder, hits in loose.items():
        uniq = sorted(set(hits))
        if len(uniq) >= 3:
            offer(folder + "/", "pinned_dir",
                  f"{len(uniq)} файл(ов)", 8 + _proximity(uniq[0], own))
        else:
            for hit in uniq:
                offer(hit, "forward", pathlib.PurePosixPath(hit).name,
                      7 + _proximity(hit, own))

    # 2. Обратная ссылка: файл снаружи упоминает файлы задачи.
    for rel, matched, named in seen:
        prox = _proximity(rel, own)
        if named:
            # Файл снаружи называет файл задачи ПО ИМЕНИ: так пишут
            # документы, пришпиливающие формат, и тесты, читающие данные
            # (PILOT-1: q017 — docs/04 §8 держит формат suppressions.toml).
            # Класс улики в вес НЕ входит: попытка поднять «документы и
            # пришпиленное» замером отвергнута — вместе со спорным файлом
            # поднимаются и его соседи по классу, и он же уезжает вниз.
            offer(rel, "referrer", named[0], 7 + prox)
        strong = sorted(matched & rare)
        if len(strong) >= 2:
            # Реестр узнаётся тем, что зовёт СРАЗУ НЕСКОЛЬКО соседей.
            offer(rel, "registry", ", ".join(strong[:3]),
                  5 + min(len(strong), 4) + prox)
        elif strong:
            offer(rel, "referrer", strong[0], 2 + prox)

    ranked = sorted(found.values(),
                    key=lambda w: (-int(w["score"]), str(w["file"])))
    return [{k: str(v) for k, v in w.items() if k != "score"}
            for w in ranked[:limit]]


def validate_plan_diff(diff: dict[str, Any] | None,
                       tasks: list[dict[str, Any]]) -> list[str]:
    """Механическая валидация плана-диффа (§3.1). -> список ошибок."""
    errs = []
    if not isinstance(diff, dict) or not isinstance(diff.get("ops"), list):
        return ["дифф не объект или нет ops"]
    if not diff["ops"]:
        return ["пустой дифф: планировщик не предложил ни одной операции"]
    # analysis печатается и уходит в журнал наравне с reason: отсутствие
    # поля роняло вывод уже после успешной валидации.
    if not str(diff.get("analysis") or "").strip():
        errs.append("дифф без analysis: планировщик не обосновал план")
    existing = {t["id"] for t in tasks}
    added = set()
    removed = set()
    touched: dict[str, Any] = {}
    for i, op in enumerate(diff["ops"]):
        if not isinstance(op, dict):
            errs.append(f"ops[{i}]: операция не объект")
            continue
        kind, tid = op.get("op"), op.get("id")
        where = f"ops[{i}] {kind} {tid}"
        # id проверяется ПЕРВЫМ: без него не работают ни touched, ни
        # уникальность, а применение падало KeyError уже после валидации.
        if not isinstance(tid, str) or not tid:
            errs.append(f"{where}: операция без id")
            continue
        # `reason` печатается и уходит в журнал: без него падает вывод.
        if not op.get("reason"):
            errs.append(f"{where}: операция без reason")
        # Две операции над одной задачей в одном диффе неоднозначны по
        # порядку, а remove+update ещё и роняет применение.
        if tid in touched:
            errs.append(f"{where}: повторная операция над задачей "
                        f"(уже {touched[tid]})")
        touched[tid] = kind
        if kind == "add":
            if not _ID_RE.fullmatch(tid):
                errs.append(f"{where}: id не 4 символа латиницы/цифр")
            if tid in existing or tid in added:
                errs.append(f"{where}: id уже существует")
            t = op.get("task")
            if not isinstance(t, dict):
                errs.append(f"{where}: add без task")
                continue
            if t.get("id") != tid:
                errs.append(f"{where}: task.id != op.id")
            if not t.get("paths"):
                errs.append(f"{where}: пустой paths")
            if not t.get("acceptance"):
                errs.append(f"{where}: пустой acceptance")
            if t.get("status") not in PLAN_STATUS:
                errs.append(f"{where}: недопустимый статус {t.get('status')!r}"
                            " (плану доступны pending/blocked; done ставит "
                            "только оркестратор после approve)")
            if _bad_deps(t):
                errs.append(f"{where}: deps не список строк")
            errs.extend(f"{where}: {e}" for e in _skeleton_errors(t))
            if _missing_ac_ref(t):
                errs.append(f"{where}: ни один acceptance не называет "
                            f"проверяющий тест (module::test_name) — тип "
                            f"{t.get('type')!r} несёт тесты по определению")
            added.add(tid)
        elif kind in ("update", "remove"):
            if tid not in existing:
                errs.append(f"{where}: цель не существует в очереди")
            if kind == "remove":
                removed.add(tid)
            else:
                t = op.get("task")
                if not isinstance(t, dict):
                    errs.append(f"{where}: update без task")
                    continue
                # Смена id рвёт deps, ссылающиеся на прежний id, и делает
                # ключ очереди рассогласованным с телом задачи.
                if "id" in t and t["id"] != tid:
                    errs.append(f"{where}: update меняет id на {t['id']}")
                # Частичный update законен — применение сливает поля.
                # Запрещено не ОТСУТСТВИЕ paths/acceptance, а их ОБНУЛЕНИЕ:
                # прежняя проверка отвергала минимальный {"status": ...}.
                zeroed = [k for k in ("paths", "acceptance")
                          if k in t and not t[k]]
                if zeroed:
                    errs.append(f"{where}: update обнуляет {'/'.join(zeroed)}")
                if "status" in t and t["status"] not in PLAN_STATUS:
                    errs.append(f"{where}: недопустимый статус {t['status']!r}"
                                " (плану доступны pending/blocked; done "
                                "ставит только оркестратор после approve)")
                if _bad_deps(t):
                    errs.append(f"{where}: deps не список строк")
                errs.extend(f"{where}: {e}" for e in _skeleton_errors(t))
                if _missing_ac_ref(t):
                    errs.append(f"{where}: ни один acceptance не называет "
                                f"проверяющий тест (module::test_name) — тип "
                                f"{t.get('type')!r} несёт тесты по "
                                f"определению")
        else:
            errs.append(f"{where}: неизвестная операция")
    # deps: ссылки только на существующие/добавляемые, без циклов.
    # Удаляемые задачи выпадают и из universe, и из графа: иначе их
    # собственные deps дают ложный отказ уже после того, как задача ушла.
    universe = (existing | added) - removed
    graph = {t["id"]: list(t.get("deps") or [])
             for t in tasks if t["id"] not in removed}
    for op in diff["ops"]:
        if not isinstance(op, dict):
            continue
        t = op.get("task")
        # id берётся из операции: частичный update законно не повторяет его
        # в теле задачи, и обращение к t["id"] роняло валидатор.
        tid = op.get("id")
        if not (isinstance(t, dict) and isinstance(tid, str) and tid
                and tid not in removed):
            continue
        if "deps" in t:
            deps = t["deps"]
            # Мусор в deps уже назван ошибкой выше; граф строится только из
            # строк — unhashable элемент в `in universe` давал TypeError.
            graph[tid] = ([d for d in deps if isinstance(d, str)]
                          if isinstance(deps, list) else [])
        else:
            graph.setdefault(tid, [])
    errs.extend(f"deps {tid} -> {d}: задача не существует"
                for tid, deps in graph.items()
                for d in deps if d not in universe)
    color: dict[str, int] = {}

    def cyclic(node: str) -> bool:
        color[node] = 1
        for nxt in graph.get(node, []):
            if color.get(nxt) == 1:
                return True
            if color.get(nxt, 0) == 0 and cyclic(nxt):
                return True
        color[node] = 2
        return False

    for tid in list(graph):
        if color.get(tid, 0) == 0 and cyclic(tid):
            errs.append(f"цикл в deps около {tid}")
            break
    return errs


def apply_plan_diff(diff: dict[str, Any],
                    tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Копия обязана быть глубокой: `dict(t)` оставляет общими вложенные
    # структуры (paths, deps, acceptance), и правка результата тихо меняет
    # исходную очередь — поймано мутационным аудитом.
    by_id = {t["id"]: copy.deepcopy(t) for t in tasks}
    order = [t["id"] for t in tasks]
    for op in diff["ops"]:
        if op["op"] == "add":
            by_id[op["id"]] = copy.deepcopy(op["task"])
            order.append(op["id"])
        elif op["op"] == "update":
            by_id[op["id"]].update(op["task"])
        elif op["op"] == "remove":
            by_id.pop(op["id"], None)
            order = [x for x in order if x != op["id"]]
    return [by_id[i] for i in order if i in by_id]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "replan"])
    ap.add_argument("--stand", required=True)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--goal")
    ap.add_argument("--task")
    ap.add_argument("--dispute")
    args = ap.parse_args()
    obs.setup(artifacts_dir(args.stand))

    stand = pathlib.Path(args.stand)
    data = json.loads(pathlib.Path(args.tasks).read_text())
    tasks = data["tasks"]
    files, suite = repo_map(stand)

    if args.mode == "plan":
        prompt = plan_prompt(args.goal, tasks, files, suite)
    else:
        task = next(t for t in tasks if t["id"] == args.task)
        dispute = json.loads(pathlib.Path(args.dispute).read_text())
        prompt = replan_prompt(task, dispute, tasks, files, suite)

    # root обязателен и здесь: standalone-вход без него запускал claude в
    # каталоге процесса, а артефакты и метрики падали в каталог пакета —
    # CLI петли передавал root корректно, и два входа молча расходились.
    diff, errs, reason = plan_with_retry(prompt, args.mode, tasks,
                                         root=args.stand)
    if errs or diff is None:
        print("ЭСКАЛАЦИЯ:", *errs, sep="\n  ")
        metric(root=args.stand, mode=args.mode, result="escalation",
               errors=errs, reason=reason)
        raise SystemExit(2)

    print(f"analysis: {diff['analysis'][:400]}\n")
    for op in diff["ops"]:
        t = op.get("task") or {}
        print(f"  {op['op']:6} {op['id']}  {t.get('title', '')[:60]}")
        print(f"         paths={t.get('paths')} deps={t.get('deps')}")
        print(f"         reason: {op['reason'][:150]}")
    data["tasks"] = apply_plan_diff(diff, tasks)
    blob = json.dumps(data, ensure_ascii=False, indent=1)
    pathlib.Path(args.out).write_text(blob + "\n")
    metric(root=args.stand, mode=args.mode, result="applied",
           tasks_after=len(data["tasks"]))
    print(f"\nприменено -> {args.out} ({len(data['tasks'])} задач)")
    return 0


if __name__ == "__main__":
    main()
