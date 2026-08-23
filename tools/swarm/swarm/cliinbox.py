"""Команды работы с инбоксом: inbox и answer."""
import argparse
import fnmatch
import json
import pathlib
import re
import sys

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402
from cliexplain import _prefix  # noqa: E402


def cmd_inbox(args: argparse.Namespace) -> int:
    """Накопившиеся вопросы к человеку — разбираются пачкой."""
    vocab = cli.load_mod("vocab")
    st = cli.state_mod.SwarmState(args.root)
    questions = st.questions(only_open=not args.all)
    if not questions:
        print("вопросов нет" if args.all else "открытых вопросов нет")
        return 0
    for q in questions:
        mark = "?" if q["status"] == "open" else "v"
        # Устаревший вопрос виден с первого взгляда: иначе он выглядит
        # как задолженность человека, хотя отвечать уже нечему.
        stale = ", задача закрыта — очередь не держит" if q.get("stale") else ""
        print(f"[{mark}] {q['qid']}  задача {q.get('task')}  "
              f"({vocab.ru(vocab.QKIND_RU, q.get('qkind'))}{stale})")
        print(f"     {str(q.get('question') or '')[:150]}")
        dispute = q.get("dispute")
        if dispute:
            # Спор исполнителя целиком: решение о плане принимается по
            # полному доводу, а не по одной строке вопроса. Журнал —
            # данные: не-строку показываем компактным JSON, а не падаем
            # на splitlines.
            body = (dispute if isinstance(dispute, str)
                    else json.dumps(dispute, ensure_ascii=False))
            print("     довод исполнителя:")
            for line in body.splitlines():
                print(f"       {line}")
        findings = q.get("findings")
        if findings and not isinstance(findings, list):
            findings = [findings]
        for f in (findings or [])[:3]:
            # Через тот же словарь, что report и why: находка без
            # тяжести и места — половина находки.
            if isinstance(f, dict):
                print(f"       - {vocab.finding(f)}")
                if f.get("suggestion"):
                    print(f"         предложение: {str(f['suggestion'])[:100]}")
            else:
                print(f"       - {str(f)[:110]}")
        if q.get("mechanical_left"):
            print(f"     механических находок (чинятся сами): {q['mechanical_left']}")
        if q.get("stash"):
            print(f"     работа сохранена: {q['stash']}")
        if q["status"] == "answered":
            print(f"     ответ: {q['answer'][:150]}")
        print()
    open_q = [q for q in questions if q["status"] == "open"]
    blocking = [q for q in open_q if not q.get("stale")]
    if blocking:
        print(f"открытых: {len(blocking)} — ответить: "
              f'swarm answer <id> "текст"')
    if len(open_q) > len(blocking):
        print(f"устаревших: {len(open_q) - len(blocking)} — их задачи уже "
              f"закрыты; ответ на такой вопрос только записывается")
    return 0



def _paths_mentioned(text: str, allowed: list[str]) -> list[str]:
    """Файлы, названные в ответе, но отсутствующие в границах задачи.

    Ответ вроде «вынеси в _utils.py» невыполним, если этого файла нет в
    `paths`: исполнитель попробует, SCOPE-CHECK откатит, раунд сгорит.
    Дешевле предупредить человека сразу.

    Сверка — тем же fnmatch, что и настоящий SCOPE-CHECK. Собственная
    формула на суффиксах ошибалась в обе стороны: `tests/*` требовал
    --force за файл внутри границ, а тёзка по хвосту («vocab.py» против
    «ab.py») молча проходил. Страж с другой формулой границ — не страж.
    Голое имя без пути дополнительно сверяется с именем файла в паттерне:
    «поправь _utils.py» при paths=[«swarm/_utils.py»] — внутри границ.
    """
    found = set(re.findall(r"[\w/.-]+\.(?:py|rs|ts|js|go|toml|md)", text))

    def inside(mention: str) -> bool:
        m = mention.removeprefix("./")
        if any(fnmatch.fnmatch(m, a) for a in allowed):
            return True
        return "/" not in m and any(
            fnmatch.fnmatch(m, pathlib.PurePath(a).name) for a in allowed)

    return sorted(f for f in found if not inside(f))



def cmd_answer(args: argparse.Namespace) -> int:
    """Ответ человека возвращает задачу в работу с его решением."""
    st = cli.state_mod.SwarmState(args.root)
    questions = {q["qid"]: q for q in st.questions()}
    task_id = questions.get(args.qid, {}).get("task")
    task = next((t for t in st.load_tasks()["tasks"] if t["id"] == task_id), None)

    if task and not args.add_path and not args.force:
        outside = _paths_mentioned(args.text, task.get("paths") or [])
        if outside:
            print(f"внимание: в ответе упомянуты файлы вне границ задачи: "
                  f"{', '.join(outside)}")
            print(f"границы задачи {task_id}: {task.get('paths')}")
            print("исполнитель не сможет их тронуть — SCOPE-CHECK откатит правки.")
            print(f"если это УКАЗАНИЕ править файл: swarm answer {args.qid} "
                  f'"..." --add-path {outside[0]}')
            # Упоминание файла в объяснении — не указание его править.
            # На PILOT-1 ответ объяснял, что оператор закоммитил swarm.toml
            # пока задача шла в фоне; страж прочёл это как задание и
            # предложил ВЫДАТЬ исполнителю право на конфиг пилота — ровно
            # то, от чего защищает. Отсюда второй выход, а не только
            # расширение границ.
            print(f"если это лишь УПОМЯНУТО в объяснении: swarm answer "
                  f'{args.qid} "..." --force')
            return 2

    try:
        task_id = st.answer(args.qid, args.text, add_paths=args.add_path)
    except cli.state_mod.StateError as e:
        print(f"{e}", file=sys.stderr)
        return 2
    if task and task.get("status") == "done":
        # Ответ на вопрос закрытой задачи — запись, а не команда. Прежде
        # он не принимался вовсе, и разбор случившегося деть было некуда.
        print(f"вопрос {args.qid} закрыт — ответ записан")
        print(f"задача {task_id} уже закрыта: в очередь она НЕ возвращается. "
              f"Нужно переделать — `{_prefix(args.root)} retry {task_id}`")
        if args.add_path:
            print(f"границы расширены на будущее: {', '.join(args.add_path)} "
                  f"— подействует, если задачу переоткроют")
        return 0
    if task_id == "*":
        # Вопрос уровня прогона (plan_failed и т.п.): задачи за ним нет,
        # и фраза «задача * возвращена в очередь» была ложью — ничего не
        # менялось и ничего не перезапустится само. Честный итог: ответ
        # лежит в журнале, петлю перезапускают руками.
        print(f"вопрос {args.qid} закрыт — ответ записан в журнал")
        print("это вопрос уровня прогона, задач он не возвращает; "
              'перезапустите `swarm plan --goal "…"` или `swarm go`')
        return 0
    print(f"вопрос {args.qid} закрыт, задача {task_id} возвращена в очередь")
    if args.add_path:
        print(f"границы задачи расширены: {', '.join(args.add_path)}")
    print("решение уйдёт исполнителю следующим запуском `swarm run`")
    return 0

