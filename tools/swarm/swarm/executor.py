"""Вызов исполнителя: прямой прогон и chat-fill (E10). Вынесено из
agents.py: функции получают объект Agents первым аргументом (AgentsLike),
класс держит делегаты — точки вызова не изменились.
"""
import ast
import importlib.util
import json
import pathlib
import sys
import time
from types import ModuleType
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schemas"

_HERE = str(HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import engines  # noqa: E402
import parsing as parsing_mod  # noqa: E402
import promptbuilder  # noqa: E402
from agents_types import AgentsLike  # noqa: E402


def load_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Имя логера оставлено "agents": журнал наблюдаемости — контракт,
# переименование модуля не должно менять имена потоков.
log = load_module("obs").get_logger("agents")


def implement(agents: AgentsLike, task: dict[str, Any], feedback: str | None,
              iteration: int) -> dict[str, Any] | None:
    # Поле задачи читается ТОЛЬКО за флагом: с flag=off implement()
    # обязан остаться байт-в-байт сегодняшним (E10, замер против
    # исходного поведения). Ключ прогона `executor_engine` флагом не
    # закрыт — это не эксперимент, а ответ на вопрос «кем исполнять»:
    # у роли исполнителя до сих пор не было запасного пути, и отказ
    # одной подписки останавливал петлю целиком.
    engine, model = engines.resolve(agents.config,
                                    task if skeleton_on(agents) else None)
    if engine == "ollama":
        return implement_fill(agents, task, feedback, iteration, model)
    prompt = promptbuilder.handoff(
        agents, task, feedback, promptbuilder.repo_map(agents, task),
        memory=promptbuilder.memory_block(agents, task) or None,
        unclear=promptbuilder.unclear_block(agents, task) or None)
    cmd = engines.executor_argv(engine, model, prompt, agents.config,
                                report_schema() if engine == "claude" else "")
    # Разборщик потока и способ достать отчёт — свойства ДВИЖКА, а не
    # роли: у kimi финальный JSON лежит в assistant-событии, у claude —
    # в конверте финального result-события (тот же путь, которым живёт
    # ревьюер).
    claude = engine == "claude"
    drv = agents.driver.AgentDriver(
        # Дерево плеча, не корень состояния: теневое плечо дуэли живёт в
        # worktree, и запусти мы его в общем дереве — два исполнителя
        # переписали бы работу друг друга, а замер сравнил бы кашу.
        cwd=str(getattr(agents, "work_root", None) or agents.state.root),
        silence_timeout=agents.config.get("silence_timeout", 600),
        wall_clock_cap=agents.config.get("wall_clock_cap", 1800))
    run = drv.start(cmd, parser=(agents.driver.parse_claude if claude
                                 else agents.driver.parse_kimi))
    result = run.collect(agents.driver.extract_result_envelope if claude
                         else parsing_mod.extract_report)
    raw = agents.state.dir / "log" / f"{task['id']}-i{iteration}-executor.jsonl"
    raw.write_text(run.raw_stream())
    report: dict[str, Any] | None = result.report
    reason = result.reason
    facts: dict[str, Any] = {}
    if claude:
        report, reason, facts = claude_outcome(agents, task, iteration, result)
    agents.state.metric(task=task["id"], iter=iteration, phase="implement",
                      reason=reason, wall_s=round(result.wall_s, 1),
                      report=bool(report), events=result.events,
                      engine=engine, model=model or None, **facts)
    if report is None or reason != "done":
        # stderr — единственное место, где провайдер объясняет отказ.
        # Пока он не сохранялся, диагноз «квота Kimi исчерпана» занял
        # шесть запросов вместо чтения одной строки журнала: три
        # мгновенные аварии подряд выглядели как «нет отчёта».
        stderr = run.stderr_tail(400).strip()
        agents.last_implement_failure = {
            "reason": reason, "wall_s": result.wall_s,
            "events": result.events, "stderr": stderr}
        agents.state.log("executor_failed", task=task["id"], round=iteration,
                       reason=reason, wall_s=round(result.wall_s, 1),
                       events=result.events, engine=engine, stderr=stderr)
    else:
        agents.last_implement_failure = None
    return report


def report_schema() -> str:
    """Схема отчёта исполнителя как ТЕКСТ: `--json-schema` принимает саму
    схему, а не путь к ней (так же читает свою схему ревьюер)."""
    return (SCHEMAS / "report-v1.schema.json").read_text(encoding="utf-8")


def claude_outcome(agents: AgentsLike, task: dict[str, Any], iteration: int,
                   result: Any) -> tuple[dict[str, Any] | None, str,
                                         dict[str, Any]]:
    """Разбор конверта claude: отчёт, причина исхода, числа для метрики.

    Движок claude отдаёт то, чего поток kimi не содержит вовсе, — цену,
    токены и список отклонённых вызовов. Пока исполнитель был только на
    kimi, `total_spend()` честно не знал цены целой роли; теперь она
    известна, и не считать её значило бы врать бюджету прогона.

    Отказ по квоте поднимается исключением, а не превращается в
    «нет отчёта»: квота лечится ожиданием (§5.3), и у петли для этого
    есть механика — ровно та же, что у ревьюера.
    """
    env = result.report
    quota = agents.loop_mod.quota_error(env)
    if quota:
        agents.state.metric(task=task["id"], iter=iteration, phase="implement",
                          quota_wait=True, provider_message=quota)
        raise agents.loop_mod.QuotaExceededError(quota)
    facts = engines.envelope_facts(env)
    denied = engines.denied_commands(env)
    if denied:
        # Отказ по deny-списку не авария: работа могла быть сделана и без
        # запрещённой команды. Но это единственное место, где видно, что
        # исполнитель ПЫТАЛСЯ выйти за правило, и терять такой факт
        # нельзя — на нём держится ответ на вопрос, работает ли запрет
        # как правило или как пожелание.
        agents.state.log("executor_denied", task=task["id"], round=iteration,
                       count=len(denied), commands=denied[:5])
    report = engines.report_from_envelope(env)
    reason = result.reason
    if report is None and reason in ("done", "crash"):
        # Обрыв по деньгам — не плохая работа и не авария, а раунд, в
        # котором работу не о чем судить: петля обязана назвать его тем,
        # чем он был (правило честности бюджета раундов, §5.3).
        #
        # «crash» здесь разбирается наравне с «done», и это не
        # перестраховка. CLI выходит НЕНУЛЁВЫМ кодом, когда обрубает
        # себя по потолку стоимости, драйвер по коду возврата честно
        # говорит «crash» — и диагноз получается противоположный
        # правде: оператор идёт искать аварию вместо того, чтобы
        # поднять executor_budget_usd или разбить задачу. Поймано на
        # плечах E9 (2026-08-24): оба потеряли первый раунд на потолке
        # в $3, журнал обоих сказал «крах». Конверт при этом ЕСТЬ и сам
        # называет причину, а настоящая смерть процесса конверта не
        # оставляет — поэтому слово конверта сильнее кода возврата.
        if engines.budget_truncated(env):
            reason = "budget_exhausted"
        elif reason == "done":
            reason = "no_report"
    return report, reason, facts


def skeleton_on(agents: AgentsLike) -> bool:
    """E10 за флагом (§1, 06-док): опечатка в имени эксперимента не
    обязана включать умолчание — незнакомый ключ гейт уже подсвечивает
    в cli.load_config, здесь просто явный дефолт "выключено"."""
    return bool((agents.config.get("experiments") or {}).get("skeleton", False))


def fill_target(agents: AgentsLike, task: dict[str, Any]) -> pathlib.Path | None:
    """Единственный конкретный существующий файл задачи, либо None.

    Ответ chat-fill — весь файл в ОДНОМ fence: формат не умеет назвать,
    к какому из нескольких файлов относится код. Контракт ломается уже
    на втором пути или на маске, а не на исполнении.
    """
    paths = task.get("paths") or []
    if len(paths) != 1 or not isinstance(paths[0], str):
        return None
    rel = paths[0]
    if any(ch in rel for ch in "*?[]"):
        return None
    full = agents.state.root / rel
    return full if full.is_file() else None


def fill_failure(agents: AgentsLike, task_id: str, iteration: int, wall_s: float,
                  model: str, reason: str, detail: str) -> None:
    """Общий выход провала chat-fill: метрика + диагноз, НИКОГДА raise.

    Причина в last_implement_failure — то же место, что и у обычного
    исполнителя: петля различает провалы одинаково, независимо от
    того, каким путём implement() до них дошёл (§7.3, тот же принцип
    fail-open, что и у хелперов третьего контура)."""
    agents.state.metric(task=task_id, iter=iteration, phase="implement",
                      reason=reason, wall_s=wall_s, report=False,
                      fill=True, model=model)
    agents.last_implement_failure = {"reason": reason, "detail": detail}
    return


def implement_fill(agents: AgentsLike, task: dict[str, Any],
                    feedback: str | None, iteration: int,
                    model: str = "") -> dict[str, Any] | None:
    """E10: заполнение контракта дешёвой чат-моделью (`ollama:` префикс).

    Формат вывода дешёвых чат-моделей на Ollama Cloud снят на пробе
    (2026-08-18): один ```python fence без посторонней прозы, сигнатуры
    целы, обрезание ловится по `done_reason`. Контракт держится не на
    CLI-конверте (его тут нет), а на механическом разборе ответа —
    отсюда строгость формы.
    """
    task_id = str(task.get("id"))
    target = fill_target(agents, task)
    if target is None:
        agents.last_implement_failure = {
            "reason": "fill_misconfigured",
            "detail": f"paths обязан быть ровно один конкретный "
                      f"существующий файл: {task.get('paths')!r}"}
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as e:
        agents.last_implement_failure = {"reason": "fill_misconfigured",
                                       "detail": str(e)}
        return None
    acc = "\n".join("- " + a for a in task.get("acceptance") or [])
    fb = ""
    if feedback:
        fb = ("\n## Feedback — you must address it\n"
              + json.dumps(feedback, ensure_ascii=False, indent=1) + "\n")
    prompt = f"""You are filling in a contracted stub file. Your reply is \
parsed by machine.

## Task
{task_id}: {task.get('spec') or task.get('title', '')}

Acceptance:
{acc}
{fb}
## Current file ({target.relative_to(agents.state.root)})
```python
{content}
```

## Output contract
Return the COMPLETE file in ONE fenced block ```python ...```; do not change
any signature or docstring of existing defs; no elision; no text outside the
fence.
"""
    if not model:
        model = str(task.get("executor_model")).removeprefix("ollama:")
    if agents.helpers is None:
        try:
            agents.helpers = load_module("helpers")
        except Exception:
            log.warning("хелперы недоступны для chat-fill", exc_info=True,
                        extra={"swarm_task": task_id})
            fill_failure(agents, task_id, iteration, 0.0, model,
                               "fill_no_reply", "модуль хелперов не загружен")
            return None
        agents.helpers.configure(agents.state.dir / "helper-metrics.jsonl")
    max_tokens = agents.config.get("fill_num_predict", 8000)
    t0 = time.time()
    try:
        reply = agents.helpers.ollama_chat(prompt, "fill",
                                          max_tokens=max_tokens, model=model)
    except Exception:
        # ollama_chat сам fail-open (§7.3) и не должен бросать, но
        # chat-fill обязан пережить и дефект самого хелпера — это его
        # СОБСТВЕННОЕ обещание "никогда не роняет петлю", не только
        # обещание вызываемого модуля.
        log.warning("chat-fill упал при вызове модели", exc_info=True,
                    extra={"swarm_task": task_id})
        reply = None
    wall_s = round(time.time() - t0, 1)
    if not reply:
        fill_failure(agents, task_id, iteration, wall_s, model,
                           "fill_no_reply", "модель не ответила")
        return None
    code = parsing_mod.extract_fenced_code(reply)
    if code is None:
        fill_failure(agents, task_id, iteration, wall_s, model, "fill_no_fence",
                           "ответ не прошёл контракт: не один чистый fence")
        return None
    if target.suffix == ".py":
        try:
            ast.parse(code)
        except SyntaxError as e:
            fill_failure(agents, task_id, iteration, wall_s, model,
                               "fill_syntax", str(e))
            return None
    try:
        target.write_text(code, encoding="utf-8")
        log_path = agents.state.dir / "log" / f"{task_id}-i{iteration}-fill.txt"
        log_path.write_text(reply, encoding="utf-8")
    except OSError as e:
        fill_failure(agents, task_id, iteration, wall_s, model,
                           "fill_misconfigured", str(e))
        return None
    agents.state.metric(task=task_id, iter=iteration, phase="implement",
                      reason="done", wall_s=wall_s, report=True, fill=True,
                      model=model)
    agents.last_implement_failure = None
    return {"status": "done", "summary": "заполнение по контракту применено",
           "fill": True}
