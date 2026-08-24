"""Пурист: находит пробелы спецификации и НЕ заполняет их (E14).

Линия, которую владелец увидел в экспериментах, — «строгие правила
против творчества» — проходит не между темпераментами агентов. Она
проходит там, где ОДИН агент находит неоднозначность и сам же её молча
закрывает: находка не доезжает до человека, а в код уезжает изобретение,
которого никто не просил. Четыре независимых свидетельства собраны в
06-доке (ловушка s3nm, замер E11, падение intent-находок с памятью в E9,
3 из 7 споров о границах без текстуального следа).

Полный вариант E14 — две дорогие роли: пурист находит, изобретатель
предлагает ответы человеку. Здесь СОЗНАТЕЛЬНО реализована дешёвая
контргипотеза, и она обязана падать первой: один короткий вызов на
задачу, который выдаёт только СПИСОК развилок. Если этого хватает, две
дорогие роли не нужны; если не хватает, известно почему.

## Что делает список

Он уезжает в промпт исполнителя одним блоком с одним требованием: если
выбор всё же приходится сделать, назови его в `deviations`. Механика
поэтому превращает МОЛЧАЛИВОЕ изобретение в ОБЪЯВЛЕННОЕ — а это ровно та
разница, которую E14 и берётся мерить. Список не блокирует очередь и не
уходит в инбокс: вопрос человеку на каждой задаче остановил бы работу
целиком, а замер требует, чтобы работа шла.

## Чего пурист не видит

Кода, карты репозитория, памяти прошлых прогонов, чужих реализаций —
того же, чего не видит тестировщик E11, и по той же причине. Пробел,
найденный чтением реализации, — не пробел спецификации, а пересказ
чужого выбора.

## Почему пустой список — успех, а не отказ

Спецификация бывает полной. Пустой ответ полезнее выдуманной развилки:
выдуманная развилка стоит исполнителю внимания и уводит его от задачи,
а в замере выглядит работой. Промпт поэтому просит пустой список прямо,
а не намёком.
"""
from __future__ import annotations

import pathlib
import sys
from typing import TYPE_CHECKING, Any

HERE = pathlib.Path(__file__).resolve().parent

_HERE = str(HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import engines  # noqa: E402
import parsing as parsing_mod  # noqa: E402

if TYPE_CHECKING:
    from agents_types import AgentsLike

# Сколько развилок показывать исполнителю. Список длиннее вытесняет из
# промпта саму задачу, а пурист, нашедший пятнадцать пробелов в короткой
# спеке, скорее пересказывает спеку, чем находит развилки.
MAX_ITEMS = 6


def enabled(config: dict[str, Any]) -> bool:
    """Флаг `[experiments] unclear`. Умолчание — выключено."""
    return bool((config.get("experiments") or {}).get("unclear", False))


def prompt(task: dict[str, Any], goal: str) -> str:
    acc = "\n".join("- " + a for a in task.get("acceptance") or [])
    return f"""You are the SPEC PURIST in an automated dev loop. Your reply is
parsed by machine.

Your one job: list the decisions this specification does NOT make.

## Goal of the project
{goal}

## Task
{task['id']}: {task.get('spec') or task['title']}

Acceptance:
{acc}

Files the task may touch: {', '.join(task.get('paths') or []) or '—'}

## What counts as a gap
A gap is an UNRESOLVED FORK: two readings of this text produce different
observable behaviour, and the text does not say which is meant. Name the fork
and what depends on the answer.

These are NOT gaps, and listing them is a false positive:
- style, naming, structure — anything invisible from outside;
- a fork where both readings behave identically;
- something the acceptance criteria already decide, even indirectly;
- your opinion that the task is a bad idea, or should be done differently;
- anything you would need to READ THE CODE to notice. You have not seen the
  code and must not guess at it: a gap discovered from an implementation is
  that implementation's choice, not the spec's silence.

## An empty list is a correct and common answer
Specifications are often complete. Returning `"unclear": []` is a success. An
invented fork is worse than none: it costs the executor attention and points
it away from the task.

## Do NOT answer the questions
You find the forks. Someone else decides them. A proposed answer here would
close the gap silently — exactly the failure this role exists to prevent. No
recommendations, no "probably", no defaults.

## Output
Finish with EXACTLY one JSON object, no markdown fence:
  {{"unclear": [{{"question": "развилка ВОПРОСОМ по-русски",
 "why_it_matters": "какое наблюдаемое поведение расходится",
 "where": "цитата места, которое молчит — или пусто"}}],
"summary": "одно предложение ПО-РУССКИ"}}
Everything you write is read by a human — write it in RUSSIAN.
"""


def block(report: dict[str, Any] | None) -> str:
    """Блок для промпта исполнителя. Пустая строка — норма.

    Пустой список пуриста НЕ порождает блока: строка «пробелов нет» в
    промпте платится токенами за каждый раунд и не говорит исполнителю
    ничего, чего он не знал бы по умолчанию.
    """
    if not report:
        return ""
    items = [i for i in (report.get("unclear") or [])
             if isinstance(i, dict) and str(i.get("question") or "").strip()]
    if not items:
        return ""
    lines = ["## What the spec does NOT decide",
             "A separate pass over the specification alone found these forks.",
             "They are NOT instructions and NOT defects — nobody has decided",
             "them yet. If your work requires choosing, choose, and then NAME",
             "the choice in `deviations`. An undeclared choice here is the",
             "failure this list exists to prevent."]
    for item in items[:MAX_ITEMS]:
        line = f"- {str(item['question']).strip()}"
        why = str(item.get("why_it_matters") or "").strip()
        if why:
            line += f" (зависит: {why})"
        lines.append(line)
    if len(items) > MAX_ITEMS:
        lines.append(f"- …и ещё {len(items) - MAX_ITEMS}, "
                     f"см. журнал (`unclear_found`)")
    return "\n".join(lines)


def find(agents: AgentsLike, task: dict[str, Any],
         goal: str) -> dict[str, Any] | None:
    """Один вызов пуриста. Отчёт либо None.

    Провал НЕ роняет задачу: без списка петля вырождается в сегодняшнее
    поведение, и это честно пишется в журнал. Прибор не имеет права
    стоить задач.
    """
    engine, model = engines.resolve(agents.config)
    override = agents.config.get("unclear_model")
    if override:
        engine, model = engines.split_model(override)[0] or engine, \
            engines.split_model(override)[1]
    text = prompt(task, goal)
    schema = ""
    if engine == "claude":
        schema = (HERE.parent / "schemas"
                  / "unclear-v1.schema.json").read_text(encoding="utf-8")
    cmd = engines.executor_argv(engine, model, text, agents.config, schema)
    drv = agents.driver.AgentDriver(
        cwd=str(agents.state.root),
        silence_timeout=agents.config.get("silence_timeout", 600),
        wall_clock_cap=agents.config.get("wall_clock_cap", 1800))
    claude = engine == "claude"
    run = drv.start(cmd, parser=(agents.driver.parse_claude if claude
                                 else agents.driver.parse_kimi))
    result = run.collect(agents.driver.extract_result_envelope if claude
                         else parsing_mod.extract_report)
    raw = agents.state.dir / "log" / f"{task['id']}-unclear.jsonl"
    raw.write_text(run.raw_stream())
    report: dict[str, Any] | None = result.report
    facts: dict[str, Any] = {}
    if claude:
        facts = engines.envelope_facts(result.report)
        report = engines.report_from_envelope(result.report)
    found = len((report or {}).get("unclear") or []) if report else None
    agents.state.metric(task=task["id"], phase="unclear", engine=engine,
                        model=model or None, reason=result.reason,
                        wall_s=round(result.wall_s, 1),
                        found=found, **facts)
    agents.state.log("unclear_found", task=task["id"], count=found,
                     summary=str((report or {}).get("summary") or "")[:300],
                     questions=[str(i.get("question"))[:160]
                                for i in ((report or {}).get("unclear") or [])
                                if isinstance(i, dict)][:MAX_ITEMS])
    return report
