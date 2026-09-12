"""Независимый тестировщик (E11, за флагом `[experiments] tester`).

Плечо A — сегодняшнее: исполнитель пишет и код, и тесты к нему. Тогда
независимость держится на правилах плана («независимый эталон»),
запросах проверки от ревьюера (ADR-005) и мутационном аудите. За
программу набралось четыре случая полых тестов, и все четыре — про
одного автора у кода и у его проверки.

Плечо B — здесь: тесты пишет ОТДЕЛЬНЫЙ вызов, который кода не видел.
Независимость получается структурной, а не обещанной: тестировщик
работает ДО исполнителя, когда реализации ещё нет на диске, и видит
только спецификацию с критериями приёмки. Написанные им файлы после
этого защищены от исполнителя наравне с чужими тестами — иначе плечо
превратилось бы в плечо A с лишним вызовом.

Что тестировщику НЕ дают, и это существо замера, а не экономия: карту
репозитория, память прошлых прогонов, чужие реализации. Всё, что он
может знать о поведении, обязано следовать из спецификации; тест,
который он не смог написать, — это спецификация, которую нельзя
проверить, и знать об этом полезнее, чем получить тест, списанный с
кода.
"""

from __future__ import annotations

import pathlib
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents_types import AgentsLike

HERE = pathlib.Path(__file__).resolve().parent

_HERE = str(HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import engines  # noqa: E402
import modlock  # noqa: E402
import pathsafe  # noqa: E402

log = modlock.load_module("obs").get_logger("tester")

# Тестовый файл узнаётся по имени, а не по списку в задаче: задача
# перечисляет и код, и тесты в одном `paths`, и разделить их надо
# механически, иначе тестировщик получит право писать реализацию.
_TEST_MARKERS = ("test_", "_test.", "/tests/", "spec_")


def enabled(config: dict[str, Any]) -> bool:
    """E11 за флагом (06-док, §1): умолчание — сегодняшнее поведение."""
    return bool((config.get("experiments") or {}).get("tester", False))


def test_paths(task: dict[str, Any]) -> list[str]:
    """Пути задачи, похожие на тесты. Пусто — задача не для плеча B."""
    return [
        p
        for p in (task.get("paths") or [])
        if isinstance(p, str)
        and not any(ch in p for ch in "*?[")
        and any(m in p for m in _TEST_MARKERS)
    ]


def wants_tester(config: dict[str, Any], task: dict[str, Any]) -> bool:
    """Задача, где тесты пишет сама задача, — единственный класс, где у
    плеча B вообще есть работа.

    Обычный `feature` тесты не несёт: их пишет соседняя задача или они
    уже есть, и звать тестировщика значило бы платить за пустой вызов.
    """
    return (
        enabled(config)
        and str(task.get("type")) == "feature-tests"
        and bool(test_paths(task))
    )


def prompt(task: dict[str, Any], goal: str, targets: list[str], suite: str) -> str:
    """Промпт тестировщика: спецификация есть, реализации нет.

    Формулировка «напиши тесты, которые ПОЙМАЮТ неверную реализацию»
    выбрана намеренно: «покрой поведение» на канарейках давало тесты,
    повторяющие docstring. Это тот же урок, что у ADR-005 с запросами
    проверки — формулировка и есть механизм.

    Правка после раунда 1 E11: прежний текст требовал, чтобы тесты
    ПАДАЛИ сегодня. Для задачи, добавляющей поведение, это верно (s3nm и
    s4cli вышли красными целиком — модулей ещё не было), а для задачи,
    которая сохраняет поведение и меняет способ, ложно: у кэширования
    (s1ch) красным был 1 тест из 6, у ускорения (s2tn) — 2 из 5, потому
    что остальные охраняют то, что меняться НЕ должно. Тестировщик
    проигнорировал инструкцию ровно там, где она не применима, и это
    была удача, а не устройство: буквальное послушание заставило бы его
    выдумывать искусственные падения.
    """
    acc = "\n".join("- " + a for a in task.get("acceptance") or [])
    files = "\n".join("- " + t for t in targets)
    return f"""You are the independent tester in an automated dev loop. Your \
reply is parsed by machine.

## Goal
{goal}

## Task whose behaviour you must pin ({task["id"]})
{task.get("spec") or task.get("title", "")}

Acceptance:
{acc}

## What you may write, and only this
{files}

## How the suite is run
{suite}

## The point of your role
The implementation DOES NOT EXIST YET and you will never see it. Write the
tests that would catch a WRONG implementation — not tests that restate the
spec. For every rule in the spec, ask what a plausible mistake would look
like (off-by-one, wrong tie-break, missing validation, silent empty result,
a value returned instead of an error raised) and write the case that fails
on that mistake and passes on the correct one.

- Pin BEHAVIOUR from the spec, never an implementation detail: no calls to
  private names, no assertions about internal data structures.
- Cover the error paths the acceptance names. An error that is documented
  and untested is the cheapest defect there is.
- Do not write a test whose expected value you cannot derive from the spec
  alone. If the spec does not decide a case, say so in `unclear` instead of
  inventing an answer — an ambiguous spec is a finding, not a guess.
- Some of your tests will fail today and some will not, and BOTH are
  correct. A task that ADDS behaviour leaves you nothing to run against:
  those tests fail now and pass once the spec is implemented. A task that
  PRESERVES behaviour while changing how it is achieved — caching, a
  faster algorithm, a refactor — is guarded by tests that pass before AND
  after; they exist to fail if the change breaks what must not change.
  Write whichever the task calls for. Never invent an artificial failure
  to make a test look red, and never add skips or xfail to make one green.
- Write only the files listed above. Do not create or edit anything else.

## Output
Finish with EXACTLY one JSON object, no markdown fence:
  {{"status": "done | dispute",
"summary": "одно предложение ПО-РУССКИ: что закреплено",
"cases": ["короткий список того, ЧТО ловит каждый тест — по-русски"],
"unclear": ["ONLY if the spec leaves a case undecided — назови его ПО-РУССКИ"],
"dispute": "ONLY when status=dispute: почему по этой спецификации нельзя
 написать проверяемые тесты"}}
Free text you write is read by a human — write it in RUSSIAN.
"""


def write_tests(
    agents: AgentsLike, task: dict[str, Any], goal: str, suite: str
) -> dict[str, Any] | None:
    """Один вызов тестировщика. Возвращает отчёт либо None.

    Провал тестировщика НЕ обязан ронять задачу: плечо B без тестов
    вырождается в плечо A, и об этом честно пишется в журнал. Решение,
    продолжать ли, принимает петля — здесь только факт.
    """
    targets = test_paths(task)
    # resolve без task намеренно: канала executor_model уровня задачи у
    # тестировщика нет (E11 видит спецификацию, а не маршрутизацию
    # исполнителя) — открывать его значило бы дать задаче тайно менять
    # движок чужой роли.
    engine, model = engines.resolve(agents.config)
    kind = engines.run_kind(engine)
    text = prompt(task, goal, targets, suite)
    schema = ""
    if kind == "claude":
        schema = (HERE.parent / "schemas" / "tester-v1.schema.json").read_text(
            encoding="utf-8"
        )
    # То же дерево, что и у implement(): теневое плечо дуэли работает в
    # worktree, и запуск тестировщика в общем корне переписал бы работу
    # живого плеча — замер стал бы несравнимым.
    work = str(getattr(agents, "work_root", None) or agents.state.root)
    cmd = engines.executor_argv(engine, model, text, agents.config, schema, cwd=work)
    drv = agents.driver.AgentDriver(
        cwd=work,
        silence_timeout=agents.config.get("silence_timeout", 600),
        wall_clock_cap=agents.config.get("wall_clock_cap", 1800),
    )
    parser, extract = engines.stream_pipeline(kind, agents.driver)
    run = drv.start(cmd, parser=parser)
    result = run.collect(extract)
    # id задачи — данные недоверенные: в имя файла только через санитайзер,
    # иначе '../..' в id уводил запись лога за пределы каталога состояния.
    # Дайджест сырого id рядом: санитайзер детерминированно схлопывает
    # разные id в одно имя ('a/b' и 'a_b'), без хвоста их логи затирали
    # бы друг друга.
    log_name = (
        f"{pathsafe.safe_filename(task['id'])}-"
        f"{pathsafe.short_digest(task['id'])}-tester.jsonl"
    )
    raw = agents.state.dir / "log" / log_name
    raw.write_text(run.raw_stream(), encoding="utf-8")
    report: dict[str, Any] | None = result.report
    facts: dict[str, Any] = {}
    if kind == "claude":
        facts = engines.envelope_facts(result.report)
        report = engines.report_from_envelope(result.report)
    elif kind == "zcode":
        facts = engines.zcode_facts(result.report)
        report = engines.report_from_zcode(result.report)
    agents.state.metric(
        task=task["id"],
        phase="tester",
        engine=engine,
        model=model or None,
        reason=result.reason,
        wall_s=round(result.wall_s, 1),
        report=bool(report),
        **facts,
    )
    if report is None:
        log.warning(
            "тестировщик не отдал отчёт", extra={"swarm_task": str(task.get("id"))}
        )
    return report
