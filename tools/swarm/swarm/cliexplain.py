"""Команды диагностики и разбора состояния: status, why, retry."""
import argparse
import contextlib
import itertools
import pathlib
import re
import sys
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402


def cmd_status(args: argparse.Namespace) -> int:
    vocab = cli._load("vocab")
    st = cli.state_mod.SwarmState(args.root)
    data = st.load_tasks()
    tasks = data.get("tasks", [])
    if not tasks:
        print("очередь пуста (.swarm/tasks.json отсутствует или без задач)")
        return 0
    # tasks.json правят и руками: запись не той формы — повод показать её
    # в «прочем», а не упасть трассировкой в момент, когда состояние и так
    # разбирают. Доска любое содержимое переживает — сводка обязана не хуже.
    rows = [t for t in tasks if isinstance(t, dict)]
    broken = [t for t in tasks if not isinstance(t, dict)]
    by_status: dict[str, list[dict[str, Any]]] = {}
    for t in rows:
        by_status.setdefault(str(t.get("status")), []).append(t)
    print(f"цель: {data.get('goal') or '(не задана)'}")
    print(f"задач: {len(tasks)}")
    known = ("in_progress", "in_review", "pending", "blocked", "done")
    for status in known:
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"\n{vocab.ru(vocab.STATUS_RU, status)} ({len(group)}):")
        for t in group:
            extra = []
            if t.get("iterations"):
                extra.append(f"итераций {t['iterations']}")
            if t.get("reason"):
                # Через общий словарь: голый «invalid_verdict» — буквальный
                # антипример, ради которого словарь причин и заведён.
                extra.append(vocab.ru(vocab.REASON_RU, t["reason"]))
            if t.get("stash"):
                extra.append(f"stash {t['stash']}")
            if t.get("commit"):
                extra.append(t["commit"])
            tail = f"  [{', '.join(extra)}]" if extra else ""
            print(f"  {t.get('id') or '(без id)'}  "
                  f"{str(t.get('title') or '')[:60]}{tail}")

    # Нелегальный статус — в «прочее», а не в никуда: счёт «задач: N»
    # обязан сходиться с тем, что показано ниже, иначе задача с опечаткой
    # в статусе числится в сумме, но не видна нигде.
    strange = [t for s, g in sorted(by_status.items())
               if s not in known for t in g]
    if strange or broken:
        print(f"\nпрочее ({len(strange) + len(broken)}) — записи вне "
              f"словаря петли, проверьте tasks.json:")
        for t in strange:
            print(f"  {t.get('id') or '(без id)'}  "
                  f"{str(t.get('title') or '')[:60]}"
                  f"  [статус {t.get('status')!r}]")
        for b in broken:
            print(f"  (запись не разобрана: {str(b)[:60]})")

    policies = st.policies()
    if policies:
        print(f"\nполитики прогона ({len(policies)}):")
        for p in policies:
            print(f"  {p['pid']}  {p['text'][:70]}")

    open_q = st.questions(only_open=True)
    if open_q:
        print(f"\nВОПРОСЫ К ЧЕЛОВЕКУ ({len(open_q)}):")
        for q in open_q:
            print(f"  {q['qid']}  {q['task']}  {q['question'][:70]}")
        print("  -> `swarm inbox` покажет детали")

    unfinished = st.unfinished_steps()
    if unfinished:
        print(f"\nНЕЗАВЕРШЁННЫЕ ШАГИ ({len(unfinished)}) — прогон падал:")
        for row in unfinished:
            print(f"  {row['task']}: {row['action']} (нет записи о завершении)")
        print("  -> `swarm resume` разберётся с ними")

    # Одна величина — одна формула: собственный цикл по metrics.jsonl не
    # видел плановый поток, и status расходился со стражем бюджета ровно
    # на стоимость планирования (рецидив «$38.43 против $40.12» с пилота).
    cost = st.total_spend()
    if cost:
        print(f"\nпотрачено дорогими ролями: ${cost:.2f}")
    _print_next(args.root, rows, open_q, unfinished)
    return 0



def _print_next(root: str, tasks: list[dict[str, Any]],
                open_q: list[dict[str, Any]],
                unfinished: list[dict[str, Any]]) -> None:
    """Одна строка «дальше» вместо необходимости помнить весь набор команд.

    Сводка состояния отвечает на вопрос «что происходит», но человек
    приходит с другим — «что мне теперь делать». Ответ выводится из того
    же состояния и не требует держать в голове руководство оператора.

    Оборванная работа (in_progress/in_review, незавершённые шаги)
    распознаётся раньше остального: после аварии подсказка «работа
    закончена — остался просмотр глазами» стояла прямо под списком
    НЕЗАВЕРШЁННЫХ ШАГОВ и звала человека мимо `resume`.
    """
    pre = _prefix(root)
    by_status: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_status.setdefault(str(t.get("status")), []).append(t)
    halfway = by_status.get("in_progress") or by_status.get("in_review")
    if unfinished or halfway:
        nxt = (f"{pre} resume",
               ("есть работа, оборванная на полпути, — resume разберёт "
                "незакрытые шаги (если прогон ещё идёт, просто дождитесь)"))
    elif open_q:
        nxt = (f'{pre} answer {open_q[0]["qid"]} "…"',
               (f"вас ждут {len(open_q)} вопрос(ов) — очередь не пойдёт "
                f"дальше без решения"))
    elif by_status.get("blocked"):
        stuck = by_status["blocked"][0]
        nxt = (f'{pre} why {stuck["id"]}',
               "разобрать, почему задача встала")
    elif by_status.get("pending"):
        nxt = (f"{pre} go", "продолжить прогон")
    elif by_status.get("done"):
        nxt = (f"git -C {root} log -p",
               "работа закончена — остался просмотр глазами")
    else:
        return
    print(f"\nдальше: {nxt[0]}\n        {nxt[1]}")



def _read_trend(counts: list[int]) -> str:
    """Что говорит ряд находок по раундам.

    Убывание проверяется СТРОГО. Первая редакция считала ряд убывающим,
    если он не растёт, — и на ряде 3, 3, 3 печатала «находки убывают,
    работа сходилась» прямо под диагнозом петли «число находок не
    убывает». Читателю предъявлялись два противоположных вывода об одних
    и тех же трёх числах.
    """
    if len(set(counts)) == 1:
        return (f"число находок стоит на месте ({counts[0]}) — "
                f"это топтание, а не схождение")
    non_increasing = all(b <= a for a, b in itertools.pairwise(counts))
    if non_increasing and counts[-1] < counts[0]:
        return ("находки убывают — работа сходилась"
                + ("" if counts[-1] else " до нуля"))
    return "число находок не убывает — вероятны качели fix→break"



def _prefix(root: str) -> str:
    """`swarm` вместо `swarm --root <длинный путь>`, когда корень и так текущий.

    Готовая команда ценна тем, что её копируют целиком. Лишний флаг с
    абсолютным путём переносится на вторую строку и ломает ровно это.
    """
    with contextlib.suppress(OSError):
        if pathlib.Path(root).resolve() == pathlib.Path.cwd():
            return "swarm"
    return f"swarm --root {root}"



def _round_label(stem: Any) -> str:
    """`i3-a1` — это раунд 3, проход a1. Имя файла человеку ничего не говорит."""
    m = re.fullmatch(r"i(\d+)-([a-z])(\d+)", str(stem))
    if not m:
        return str(stem)
    phase = {"a": "обычный проход", "v": "повторный после проверок"}.get(
        m.group(2), m.group(2))
    return f"раунд {m.group(1)}, {phase} #{m.group(3)}"



def _explain(task: dict[str, Any], root: str) -> None:
    """Разбор одной задачи: почему она в этом состоянии и что дальше."""
    vocab = cli._load("vocab")
    print(f"=== {task['id']}: {task.get('title', '')}")
    status = vocab.ru(vocab.STATUS_RU, task.get("status"))
    reason = task.get("reason")
    print(f"статус: {status}"
          + (f" — {vocab.ru(vocab.REASON_RU, reason)}"
             if reason and task.get("status") == "blocked" else ""))
    if task.get("_cost"):
        print(f"стоила: ${task['_cost']}")

    # Диагноз петли — её собственные слова о том, почему не сошлось. Он
    # уже посчитан и лежит в задаче, но до сих пор показывался только в
    # момент эскалации, в потоке, который к утру уже прокручен.
    if task.get("diagnosis"):
        print(f"\nдиагноз петли:\n  {task['diagnosis']}")

    rounds = task.get("_rounds") or []
    if rounds:
        print("\nтраектория:")
        for r in rounds:
            # Через тот же словарь, что и отчёт: раунд, названный здесь
            # иначе, чем в `report`, — это два знания об одном событии.
            print("  " + vocab.narrate({"kind": "round", **r}))
        counts = [r.get("findings") for r in rounds
                  if isinstance(r.get("findings"), int)]
        if len(counts) > 1:
            print("  " + _read_trend(counts))

    verdicts = task.get("_verdicts") or []
    if verdicts:
        last = verdicts[-1]
        if last.get("failed"):
            print(f"\nпоследнее ревью ({_round_label(last.get('round'))}): "
                  f"ответа нет — {last.get('why')}")
        else:
            print(f"\nпоследний вердикт ({_round_label(last.get('round'))}) → "
                  f"{last.get('verdict')}")
            if last.get("summary"):
                print(f"  {last['summary']}")
            for f in last.get("findings") or []:
                print(f"  · {vocab.finding(f)}")
                if f.get("suggestion"):
                    print(f"      → {str(f['suggestion'])[:200]}")
            for note in last.get("notes") or []:
                print(f"  (вне рамок задачи) {str(note)[:200]}")

    for q in task.get("_questions") or []:
        if q.get("status") == "open":
            print(f"\nждёт вас: {q['qid']} "
                  f"({vocab.ru(vocab.QKIND_RU, q.get('qkind'))})\n"
                  f"  {str(q.get('question', ''))[:400]}")

    if task.get("stash"):
        print(f"\nработа сохранена: {task['stash']} "
              f"(видна в `git stash list`; петля сама её не применяет)")
    if task.get("commit"):
        print(f"\nвошло в код: {task['commit']} — `git show {task['commit']}`")
    if task.get("_streams"):
        print(f"\nсырьё: .swarm/log/ — {', '.join(task['_streams'])}")

    steps = _next_steps(task, root)
    if steps:
        print("\nдальше:")
        for cmd, why in steps:
            print(f"  {cmd}\n      {why}")
    print()



WHY_ANSWER = ("ответить по существу замысла; если решение требует тронуть "
              "файл вне границ — добавьте --add-path путь")

WHY_SPLIT = ("или расщепить задачу: диагноз «слишком крупная» чаще всего "
             "именно об этом")

WHY_EYES = ("просмотр глазами — единственное место, где ловится «сделано "
            "правильно, но не то»")



def _next_steps(task: dict[str, Any], root: str) -> list[tuple[str, str]]:
    """Готовые команды под конкретное состояние задачи.

    Знание «что теперь нажать» лежало в руководстве оператора, то есть в
    другом окне и в другой момент времени. Оно нужно здесь.
    """
    pre = _prefix(root)
    open_q = [q for q in task.get("_questions") or []
              if q.get("status") == "open"]
    steps: list[tuple[str, str]] = [
        (f'{pre} answer {q["qid"]} "…"', WHY_ANSWER) for q in open_q]
    if task.get("status") == "blocked":
        steps.append((f'{pre} retry {task["id"]} --note "…"',
                      "вернуть в очередь с указанием, что сделать иначе"))
        if task.get("reason") in ("escalate_max", "max_iterations"):
            steps.append((f'{pre} plan --goal "…"', WHY_SPLIT))
    if task.get("status") == "done" and task.get("commit"):
        steps.append((f"git -C {root} show {task['commit']}", WHY_EYES))
    return steps



def cmd_why(args: argparse.Namespace) -> int:
    """Почему задача в таком состоянии и что делать дальше.

    Ответ существовал и раньше — но по частям: статус в `status`, вопрос
    в `inbox`, траектория в `report`, вердикт в `.swarm/log/*.json`,
    следующий шаг в руководстве оператора. Человек собирал его руками из
    пяти мест, причём именно тогда, когда прогон уже встал и разбираться
    хочется меньше всего.

    Данные берутся тем же сборщиком, что и доска: два представления
    одного знания расходиться не должны.
    """
    board_mod = cli._load("board")
    board = board_mod.collect(args.root)
    tasks = board["tasks"]
    if not tasks:
        print("очередь пуста — рассказывать не о чем")
        return 0
    if args.task:
        chosen = [t for t in tasks if t.get("id") == args.task] or [
            t for t in tasks if str(t.get("id", "")).startswith(args.task)]
        if not chosen:
            print(f"задача {args.task!r} не найдена", file=sys.stderr)
            return 2
    else:
        # Без аргумента разбираем ту, что остановила очередь: спрашивая
        # «почему», человек почти всегда имеет в виду именно её.
        order = {"blocked": 0, "in_progress": 1, "in_review": 2,
                 "pending": 3, "done": 4}
        chosen = [min(tasks, key=lambda t: order.get(str(t.get("status")), 9))]
        print(f"(задача не названа — разбираю {chosen[0]['id']}, "
              f"она первой требует внимания)\n")
    for task in chosen:
        _explain(task, str(args.root))
    return 0



def cmd_retry(args: argparse.Namespace) -> int:
    """Вернуть заблокированную задачу в очередь.

    Не всякая блокировка снимается ответом на вопрос: задача может
    исчерпать раунды на технических сбоях, и человеку нужен прямой способ
    вернуть её в работу — при желании с указанием и расширенными
    границами.
    """
    st = cli.state_mod.SwarmState(args.root)
    # Составное load→modify→save — под коротким замком записи: retry
    # зовут и во время прогона, и гонка с set_status бегущей петли
    # молча теряла одну из записей (lost update).
    with st.mutate():
        data = st.load_tasks()
        task = next((t for t in data["tasks"] if t["id"] == args.task), None)
        if task is None:
            print(f"задача {args.task!r} не найдена", file=sys.stderr)
            return 2
        # in_progress означает, что прогон умер на этой задаче (таймаут
        # гейта, отказ по квоте, сетевой сбой). Без этого выхода задача
        # застревала навсегда: ready_tasks берёт только pending, а retry
        # требовал blocked.
        if task["status"] not in ("blocked", "in_progress"):
            print(f"задача {args.task} в статусе {task['status']}, "
                  f"возвращать нечего", file=sys.stderr)
            return 2
        task["status"] = "pending"
        task.pop("reason", None)
        if args.note:
            task["human_answer"] = args.note
        for extra in args.add_path or []:
            if extra not in task.setdefault("paths", []):
                task["paths"].append(extra)
        st.save_tasks(data)
    st.log("retry", task=args.task, note=args.note,
           added_paths=args.add_path or [])
    print(f"задача {args.task} возвращена в очередь")
    if args.add_path:
        print(f"границы расширены: {', '.join(args.add_path)}")
    return 0

