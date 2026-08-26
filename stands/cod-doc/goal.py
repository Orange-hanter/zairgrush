#!/usr/bin/env python3
"""Задача из БД cod-doc -> цель для `swarm go --goal`.

Бэклог cod-doc живёт в БД (`.cod-doc/state.db`), а не в голове
планировщика: у задачи есть идентификатор, описание и критерии приёмки,
написанные владельцем. Пересказывать их своими словами значит
планировать по копии, которая уже разошлась с оригиналом, — поэтому
цель собирается из первоисточника.

Идентификатор попадает в первую строку намеренно: планировщик переносит
его в названия задач очереди, а оттуда он доходит до сообщений коммитов
(`agents.commit_message` строит их из id и заголовка задачи). Без этого
работа роя не связывается с задачей cod-doc ничем, кроме памяти
оператора.

    python3 goal.py SYM-003
    swarm --root "$STAND" go --goal "$(python3 goal.py SYM-003)"

БД лежит в рабочем чекауте, а не в стенде (`.cod-doc/` в `.gitignore`),
поэтому скрипт зовёт CLI ОТТУДА. Путь переопределяется `COD_DOC_ROOT`.

`COLUMNS=400` — не косметика: `cod-doc task show --json` печатает через
rich, и на узком терминале rich переносит строки ВНУТРИ строковых
значений. Получается невалидный JSON (проверено 2026-08-26: `Invalid
control character`), причём молча и только когда окно узкое.
"""
import json
import os
import pathlib
import subprocess
import sys

CODDOC = pathlib.Path(os.environ.get("COD_DOC_ROOT", pathlib.Path.home()
                                     / "Git" / "_my" / "cod-doc"))
PROJECT = os.environ.get("COD_DOC_PROJECT", "cod-doc")


def fetch(task_id: str) -> dict:
    cli = CODDOC / ".venv" / "bin" / "cod-doc"
    if not cli.exists():
        sys.exit(f"нет CLI cod-doc: {cli} (задайте COD_DOC_ROOT)")
    env = {**os.environ, "COLUMNS": "400"}
    env.pop("FORCE_COLOR", None)
    out = subprocess.run([str(cli), "task", "show", task_id,
                          "--project", PROJECT, "--json"],
                         capture_output=True, text=True, env=env, check=False)
    if out.returncode != 0:
        sys.exit(out.stderr.strip() or f"задача {task_id} не найдена")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"CLI вернул не JSON ({e}); проверьте версию cod-doc")


def render(task: dict) -> str:
    parts = [f"{task['task_id']} — {task['title']}", ""]
    if task.get("description"):
        parts += [task["description"].strip(), ""]
    if task.get("acceptance"):
        parts += ["Критерии приёмки:", task["acceptance"].strip(), ""]
    blocked = [b for b in (task.get("blocked_by") or [])]
    if blocked:
        parts += [f"Зависит от: {', '.join(blocked)}", ""]
    parts += [
        "Каждая задача очереди обязана нести идентификатор "
        f"{task['task_id']} в названии: по нему работа роя связывается с "
        "задачей в БД cod-doc.",
    ]
    return "\n".join(parts).strip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("использование: goal.py <TASK-ID>   (например SYM-003)")
    sys.stdout.write(render(fetch(sys.argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
