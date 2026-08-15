#!/usr/bin/env python3
"""Планировщик петли (§3.1): goal -> план-дифф с механической валидацией.

Два режима:
  plan   --goal "<цель>"          — декомпозиция цели в задачи
  replan --task <id> --dispute f  — пересмотр плана по спору исполнителя

Планировщик выдаёт НЕ новый tasks.json, а дифф к нему (add/update/remove).
Оркестратор валидирует дифф механически и только потом применяет:
схема, уникальность id, существование целей update/remove, разрешимость
deps, отсутствие циклов, обязательность paths/acceptance, легальность
статусов. Невалидный дифф = одна повторная попытка, затем эскалация.

Артефакты: plan-metrics.jsonl, raw/<mode>-<n>.json, применённый tasks.json.
"""
import argparse
import copy
import json
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import obs  # noqa: E402 — каталог добавлен строкой выше

PLAN = pathlib.Path(__file__).resolve().parent
SCHEMA = (PLAN.parent / "schemas" / "plan-diff.schema.json").read_text()
METRICS = PLAN / "plan-metrics.jsonl"
RAW = PLAN / "raw"
LEGAL_STATUS = {"pending", "blocked", "done"}

# Планировщику нужен тот же запас, что и ревьюеру: на реальном проекте
# зашитые $1.50 обрубали ОБЕ попытки (PILOT-1: $1.63 и $1.67, ops=0), и
# роль, объявленная в §3.1, ни разу не отработала.
DEFAULT_PLAN_BUDGET = 4.0


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
    files = sorted(p.relative_to(stand).as_posix()
                   for p in stand.rglob("*.py")
                   if "__pycache__" not in p.parts and ".git" not in p.parts)
    # Красный сьют — не ошибка вызова, а факт о стенде, который
    # планировщику как раз и нужно знать: он идёт в промпт.
    r = subprocess.run(["python3", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
                       capture_output=True, text=True, cwd=stand, check=False)
    tail = (r.stdout + r.stderr).strip().splitlines()[-2:]
    return "\n".join(files), " ".join(tail)


def plan_prompt(goal: str, tasks: list[dict[str, Any]], files: str,
                suite: str) -> str:
    return f"""Ты — планировщик в автоматической петле разработки. \
Ответ парсится механически.

## Цель
{goal}

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
- `type`: feature (правит продукционный код; тесты трогать нельзя),
  feature-tests (сам пишет свои новые тесты), test-task (правит только
  тесты), idea (замечание на будущее);
- `gate`: full (полный сьют — по умолчанию для правок существующего кода);
- `test_module` — модуль тестов, которым проверяется задача;
- задачи должны быть маленькими: одна задача = один связный результат,
  сходящийся за 1–2 итерации;
- `paths` обязаны учитывать ФАКТИЧЕСКОЕ состояние репозитория выше, включая
  файлы, созданные соседними задачами очереди.

В поле analysis сначала рассуждай, потом формируй ops.
"""


def replan_prompt(task: dict[str, Any], dispute: str,
                  tasks: list[dict[str, Any]], files: str, suite: str) -> str:
    return f"""Ты — планировщик в автоматической петле разработки. \
Ответ парсится механически.

## Ситуация
Задача ушла в blocked через канал dispute: исполнитель заявил, что требования
невыполнимы в заданных рамках. Твоя работа — устранить причину диффом к плану.

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
                 effort: str | None = None,
                 ) -> tuple[dict[str, Any] | None, str | None]:
    """Вызов планировщика. -> (план-дифф | None, причина отказа | None).

    Причина возвращается отдельно, потому что «модель ответила мусором» и
    «вызов обрублен по бюджету» лечатся по-разному, а оператор различает их
    только по тому, что ему сказали. На PILOT-1 обе попытки были обрублены
    по зашитым $1.50, а в консоль ушло «невалидный JSON» — диагноз, ведущий
    искать поломку в схеме вместо лимита.
    """
    t0 = time.time()
    r = subprocess.run(["claude", "-p", prompt, "--output-format", "json",
                        "--json-schema", SCHEMA, "--allowedTools",
                        "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*)",
                        "--max-budget-usd",
                        str(budget or DEFAULT_PLAN_BUDGET),
                        *tuning_flags(model, effort)],
                       capture_output=True, text=True,
                       # Без cwd планировщик читает репозиторий по каталогу
                       # процесса, а не по --root: Read/Grep смотрели бы не
                       # в тот проект, для которого строится план.
                       cwd=str(root) if root else None, check=False)
    dur = round(time.time() - t0, 1)
    raw_dir = artifacts_dir(root) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{tag}-a{attempt}.json").write_text(r.stdout)
    diff, cost, reason = None, None, None
    try:
        env = json.loads(r.stdout)
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
}


def plan_with_retry(prompt: str, mode: str, tasks: list[dict[str, Any]],
                    root: str | pathlib.Path | None = None,
                    budget: float | None = None,
                    ui: Callable[[str], None] = print,
                    model: str | None = None, effort: str | None = None,
                    ) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """Не более двух попыток, и вторая — только если она осмысленна.

    -> (diff | None, список ошибок, причина отказа | None).

    Политика повтора живёт здесь одна на всех вызывающих: раньше она была
    продублирована в CLI петли и в собственном main планировщика, и
    разошлась — второй экземпляр молча ретраил обрыв по бюджету.
    """
    diff, reason = call_planner(prompt, mode, root=root, budget=budget,
                                model=model, effort=effort)
    if reason in TERMINAL_REASONS:
        return None, [PLAN_DIAGNOSIS[reason]], reason
    errs = validate_plan_diff(diff, tasks) if diff else [
        PLAN_DIAGNOSIS.get(reason or "", "план-дифф не получен")]
    if not errs:
        return diff, [], None
    ui("план-дифф невалиден, повторная попытка:")
    for e in errs:
        ui(f"  {e}")
    diff, reason = call_planner(
        prompt + "\n\n## Ошибки прошлой попытки\n" + "\n".join(errs),
        mode, attempt=2, root=root, budget=budget, model=model, effort=effort)
    if reason in TERMINAL_REASONS:
        return None, [PLAN_DIAGNOSIS[reason]], reason
    errs = validate_plan_diff(diff, tasks) if diff else [
        PLAN_DIAGNOSIS.get(reason or "", "план-дифф не получен")]
    return (diff, [], None) if not errs else (None, errs, reason)


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
        kind, tid = op.get("op"), op.get("id")
        where = f"ops[{i}] {kind} {tid}"
        # `reason` печатается и уходит в журнал: без него падает вывод.
        if not op.get("reason"):
            errs.append(f"{where}: операция без reason")
        # Две операции над одной задачей в одном диффе неоднозначны по
        # порядку, а remove+update ещё и роняет применение.
        if tid in touched:
            errs.append(f"{where}: повторная операция над задачей "
                        f"(уже {touched[tid]})")
        if tid is not None:
            touched[tid] = kind
        if kind == "add":
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
            if t.get("status") not in LEGAL_STATUS:
                errs.append(f"{where}: недопустимый статус {t.get('status')}")
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
                if not t.get("paths") or not t.get("acceptance"):
                    errs.append(f"{where}: update обнуляет paths/acceptance")
                if "status" in t and t["status"] not in LEGAL_STATUS:
                    errs.append(f"{where}: недопустимый статус {t['status']}")
        else:
            errs.append(f"{where}: неизвестная операция")
    # deps: ссылки только на существующие/добавляемые, без циклов.
    # Удаляемые задачи выпадают и из universe, и из графа: иначе их
    # собственные deps дают ложный отказ уже после того, как задача ушла.
    universe = (existing | added) - removed
    graph = {t["id"]: list(t.get("deps") or [])
             for t in tasks if t["id"] not in removed}
    for op in diff["ops"]:
        t = op.get("task")
        # id берётся из операции: частичный update законно не повторяет его
        # в теле задачи, и обращение к t["id"] роняло валидатор.
        tid = op.get("id")
        if isinstance(t, dict) and tid is not None and tid not in removed:
            if "deps" in t:
                graph[tid] = list(t.get("deps") or [])
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

    diff, errs, reason = plan_with_retry(prompt, args.mode, tasks)
    if errs or diff is None:
        print("ЭСКАЛАЦИЯ:", *errs, sep="\n  ")
        metric(mode=args.mode, result="escalation", errors=errs, reason=reason)
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
    metric(mode=args.mode, result="applied", tasks_after=len(data["tasks"]))
    print(f"\nприменено -> {args.out} ({len(data['tasks'])} задач)")
    return 0


if __name__ == "__main__":
    main()
