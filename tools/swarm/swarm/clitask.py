"""Команда `swarm task`: человек правит очередь через валидатор (E2-B).

Эксперимент E2 (06-док §4): вариант A — tasks.json правится руками,
вариант B — `swarm task add/split/close`. Схему §4.3 здесь проверяет ОДНА
функция (`task_errors`) для всех трёх глаголов и для `check`; правила
те же, что у `planner.validate_plan_diff` (id формата §4.3, paths и
acceptance обязательны, deps — только на существующие задачи), с одной
уступкой самой §4.3: задача типа `idea` идёт без paths/acceptance —
замечание на будущее, а не работа.

«Запрет ручного редактирования файла» реализован не замком (плоский файл
не запереть), а следом целостности: save_tasks объявляет отпечаток в
журнале, и `task check` сравнивает его с текущим — правка в обход API
следа не оставляет. Это та же проверка, что §6.1 гоняет между задачами
петли; здесь она доступна человеку до запуска, а не после сюрприза.

Петлю команда не меняет и флага [experiments] не требует: это вход
человека, как `swarm memory add`, а не ветвление подсистемы.
"""
import argparse
import hashlib
import pathlib
import sys
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402

# Типы по §4.3 05-дока плюс feature-tests из практики (несёт тесты сам).
TASK_TYPES = ("feature", "feature-tests", "test-task", "idea")


def task_errors(t: dict[str, Any], existing_ids: set[str]) -> list[str]:
    """Одна задача против схемы §4.3. -> список ошибок (пусто = валидна)."""
    planner = cli.load_mod("planner")
    errs = []
    tid = t.get("id")
    # Формат id — одна формула на планировщик и CLI: вторая копия
    # правила со временем расходится с первой.
    id_re = planner._ID_RE  # noqa: SLF001 — _ID_RE и есть контракт §4.3
    if not isinstance(tid, str) or not id_re.fullmatch(tid):
        errs.append(f"id не 4 символа латиницы/цифр: {tid!r}")
    if not str(t.get("title") or "").strip():
        errs.append("пустой title")
    if t.get("type") not in TASK_TYPES:
        errs.append(f"недопустимый type {t.get('type')!r} "
                    f"(допустимы: {', '.join(TASK_TYPES)})")
    # idea — замечание на будущее: у неё нет ни границ, ни приёмки, и
    # требовать их было бы требовать работу, которой ещё нет.
    if t.get("type") != "idea":
        if not t.get("paths"):
            errs.append("пустой paths — без границ ревью и scope-check "
                        "не за что зацепиться")
        if not t.get("acceptance"):
            errs.append("пустой acceptance — ревью вырождается в "
                        "«нравится/не нравится» (§4.3)")
    deps = t.get("deps") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        errs.append("deps не список строк")
    else:
        errs.extend(f"deps -> {d}: задача не существует"
                    for d in deps if d not in existing_ids)
    return errs


def _new_id(title: str, used: set[str]) -> str:
    """Hash-ID §4.3: не порядковый номер, чтобы не конфликтовать при
    параллельной правке. Соль-счётчик — от коллизии с уже живыми id."""
    for n in range(1000):
        cand = hashlib.sha256(f"{title}#{n}".encode()).hexdigest()[:4]
        if cand not in used:
            return cand
    raise cli.state_mod.StateError("не удалось подобрать свободный id")


def _check_integrity(st: Any) -> str | None:
    """Отпечаток очереди против последнего объявленного в журнале."""
    text = st.tasks_path.read_text(encoding="utf-8")
    current = cli.state_mod.tasks_sha(text)
    declared = cli.state_mod.last_declared_sha(st.journal_path)
    if declared is not None and current != declared:
        return ("tasks.json изменён в обход API (отпечаток не объявлен в "
                "журнале) — права руками; E2-B предлагает `swarm task`")
    return None


def cmd_task(args: argparse.Namespace) -> int:
    st = cli.state_mod.SwarmState(args.root)
    data = st.load_tasks()
    tasks: list[dict[str, Any]] = data["tasks"]
    by_id = {t["id"]: t for t in tasks}

    if args.task_cmd == "list":
        for t in tasks:
            print(f"{t['id']}  {t.get('status', '?'):<12} "
                  f"{t.get('type', '?'):<14} {t.get('title', '')}")
        if not tasks:
            print("очередь пуста")
        return 0

    if args.task_cmd == "check":
        errs = []
        warn = _check_integrity(st)
        if warn:
            errs.append(warn)
        for t in tasks:
            errs.extend(f"{t.get('id')}: {e}"
                        for e in task_errors(t, set(by_id)))
        for e in errs:
            print(f"ошибка: {e}", file=sys.stderr)
        print("очередь валидна" if not errs else f"ошибок: {len(errs)}")
        return 2 if errs else 0

    if args.task_cmd == "close":
        task = by_id.get(args.id)
        if task is None:
            raise cli.state_mod.StateError(f"задача {args.id!r} не найдена")
        if task.get("status") in ("in_progress", "in_review"):
            raise cli.state_mod.StateError(
                f"задача {args.id} в работе у петли ({task['status']}) — "
                "закрывать её из-под оркестратора нельзя")
        dependents = [t["id"] for t in tasks if args.id in (t.get("deps") or [])]
        st.set_status(args.id, "done", closed_by="task-cli",
                      note=args.note or "")
        print(f"задача {args.id} закрыта человеком (done)")
        if dependents:
            print(f"внимание: от неё зависят {', '.join(dependents)} — "
                  "для петли они теперь готовы к запуску")
        return 0

    # add и split — через один конвейер: собрать, проверить, записать.
    new: list[dict[str, Any]]
    if args.task_cmd == "add":
        new = [{"id": _new_id(args.title, set(by_id)), "title": args.title,
                "type": args.type, "status": "pending",
                "deps": args.dep, "paths": args.path,
                "acceptance": args.acceptance}]
        if args.milestone:
            new[0]["milestone"] = args.milestone
    else:  # split
        src = by_id.get(args.id)
        if src is None:
            raise cli.state_mod.StateError(f"задача {args.id!r} не найдена")
        if src.get("status") in ("in_progress", "in_review"):
            raise cli.state_mod.StateError(
                f"задача {args.id} в работе у петли — расщеплять её "
                "из-под оркестратора нельзя")
        used = set(by_id)
        new = []
        for p in args.part:
            tid = _new_id(p, used)
            used.add(tid)
            new.append({"id": tid, "title": p,
                        "type": args.type or src.get("type"),
                        "status": "pending",
                        "deps": list(src.get("deps") or []),
                        "paths": args.path or list(src.get("paths") or []),
                        "acceptance": (args.acceptance
                                       or list(src.get("acceptance") or []))})
        if len(new) < 2:
            raise cli.state_mod.StateError(
                "расщепление в одну часть — не расщепление (--part дважды+)")

    errs = [f"{t['id']}: {e}" for t in new
            for e in task_errors(t, set(by_id))]
    if errs:
        for e in errs:
            print(f"ошибка: {e}", file=sys.stderr)
        return 2
    with st.mutate():
        data = st.load_tasks()          # перечитать под замком
        data["tasks"].extend(new)
        if args.task_cmd == "split":
            for t in data["tasks"]:
                if t["id"] == args.id:
                    t["status"] = "blocked"
                    t["reason"] = "split"
                    t["split_into"] = [n["id"] for n in new]
        st.save_tasks(data)
    for t in new:
        print(f"{t['id']}  добавлена: {t['title']}")
    if args.task_cmd == "split":
        print(f"{args.id} расщеплена -> {', '.join(t['id'] for t in new)} "
              "(исходная blocked)")
    return 0
