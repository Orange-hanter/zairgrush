"""Кандидаты автодополнения: `swarm __complete <вид> [--root R]`.

Вызывается оболочкой на КАЖДЫЙ Tab, поэтому живёт отдельно от `cli.py`:
импорт cli тянет весь рой и `git rev-parse` из obs (~0.3 с), а подсказка,
которая думает дольше, чем человек печатает, ею не пользуются. Обёртка
`swarm-cli` отправляет `__complete` сюда напрямую, минуя cli.

Правила модуля:

- только stdlib плюс три модуля без зависимостей (`lockprobe`,
  `registry`, `vocab`); `state`/`obs`/`board` не импортируются — тест
  это проверяет;
- ни одного следа на диске: `SwarmState(root)` создаёт `.swarm/log` и
  пишет в `.git/info/exclude`, поэтому файлы читаются напрямую;
- любая поломка — пустой вывод и код 0: трассировка в строке ввода
  хуже отсутствия подсказки. Это граница деградации (BLE001):
  трассировка уходит в `diag.jsonl`, но только если стенд уже есть.

Вывод — строки `значение<TAB>описание`; раскладку в список делает
`_swarm_dyn` в скрипте, а не `_describe`: тот склеивает кандидатов с
одинаковым описанием в одну строку и при этом пересортировывает весь
список — прогоны одной минуты без задач ломали порядок «новые первыми».
"""

import json
import pathlib
import sys
from collections.abc import Iterable
from typing import Any

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import lockprobe  # noqa: E402 — каталог добавлен строкой выше
import registry  # noqa: E402
import vocab  # noqa: E402

# Хвост журнала, который читается на Tab. Пилотный run.jsonl — мегабайты;
# последние прогоны и свежие вопросы живут в конце, и полный разбор файла
# ради подсказки — ровно та задержка, от которой модуль и отделён.
TAIL_BYTES = 256 * 1024
DESC_MAX = 70

# В каком порядке показывать задачи: то, с чем человек работает сейчас,
# — первым; закрытое — в конце, но не прячется (why/report по нему нужны).
TASK_ORDER = ("in_progress", "in_review", "blocked", "pending", "done")


def _esc(value: str) -> str:
    # Табуляция — разделитель строки кандидата; в значении ей не место.
    return value.replace("\t", " ")


def _short(text: Any) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= DESC_MAX else one[: DESC_MAX - 1] + "…"


def _line(value: str, desc: str) -> str:
    return f"{_esc(value)}\t{_short(desc)}"


def _tail_rows(path: pathlib.Path, limit: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """Строки jsonl из хвоста файла; первая (обрезанная) отбрасывается."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]
    rows = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _all_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    # Вопросы и политики нужны ВСЕ: вопрос из начала журнала может быть
    # всё ещё открыт. Хвост здесь дал бы тихую ложь «вопросов нет».
    return _tail_rows(path, limit=1 << 62)


def _tasks_doc(root: pathlib.Path) -> dict[str, Any]:
    try:
        doc = json.loads((root / ".swarm" / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def tasks(root: pathlib.Path, statuses: set[str] | None = None) -> list[str]:
    items = [
        t
        for t in _tasks_doc(root).get("tasks", [])
        if isinstance(t, dict) and t.get("id") is not None
    ]
    if statuses:
        items = [t for t in items if t.get("status") in statuses]

    def rank(t: dict[str, Any]) -> int:
        st = t.get("status")
        return TASK_ORDER.index(st) if st in TASK_ORDER else len(TASK_ORDER)

    out = []
    for t in sorted(items, key=rank):
        st = str(t.get("status") or "?")
        out.append(
            _line(
                str(t["id"]), f"{vocab.STATUS_RU.get(st, st)} · {t.get('title') or ''}"
            )
        )
    return out


def questions(root: pathlib.Path) -> list[str]:
    asked: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()
    for r in _all_rows(root / ".swarm" / "log" / "run.jsonl"):
        if r.get("kind") == "question" and r.get("qid"):
            asked[str(r["qid"])] = r
        elif r.get("kind") == "answer" and r.get("qid"):
            answered.add(str(r["qid"]))
    # Открытые — первыми: `answer` почти всегда про них.
    order = sorted(asked, key=lambda q: (q in answered, q))
    return [
        _line(
            q,
            f"{'отвечен' if q in answered else 'открыт'} · "
            f"{asked[q].get('task') or ''} · {asked[q].get('question') or ''}",
        )
        for q in order
    ]


def policies(root: pathlib.Path) -> list[str]:
    active: dict[str, dict[str, Any]] = {}
    dropped: set[str] = set()
    for r in _all_rows(root / ".swarm" / "log" / "run.jsonl"):
        if r.get("kind") == "policy" and r.get("pid"):
            active[str(r["pid"])] = r
        elif r.get("kind") == "policy_dropped" and r.get("pid"):
            dropped.add(str(r["pid"]))
    # Та же привязка к цели, что у `state.policies`: снимать можно только
    # действующее, а действующее — это политики текущей цели.
    goal = _tasks_doc(root).get("goal", "")
    return [
        _line(p, str(r.get("text") or ""))
        for p, r in sorted(active.items())
        if p not in dropped and (not goal or r.get("goal") == goal)
    ]


def lessons(root: pathlib.Path) -> list[str]:
    folded: dict[str, dict[str, Any]] = {}
    dead: set[str] = set()
    for r in _all_rows(root / ".swarm" / "memory" / "lessons.jsonl"):
        rid = str(r.get("id") or "")
        kind = r.get("rec", "lesson")
        if kind == "tombstone" and rid:
            dead.add(rid)
        elif kind == "lesson" and rid and rid not in folded:
            folded[rid] = r
    return [
        _line(rid, f"{r.get('outcome') or ''} · {r.get('body') or ''}")
        for rid, r in folded.items()
        if rid not in dead
    ]


def _runs_of(root: pathlib.Path) -> list[dict[str, Any]]:
    """Прогоны стенда по хвостам журнала и метрик, новые первыми."""
    seen: dict[str, dict[str, Any]] = {}
    for name in ("metrics.jsonl", "log/run.jsonl"):
        for r in _tail_rows(root / ".swarm" / name):
            rid = r.get("run_id")
            if not rid:
                continue
            info = seen.setdefault(str(rid), {"id": str(rid), "tasks": [], "t1": ""})
            task = r.get("task")
            if task and task not in info["tasks"]:
                info["tasks"].append(str(task))
            info["t1"] = max(info["t1"], str(r.get("ts") or ""))
    # run_id начинается меткой времени — строковая сортировка хронологична.
    return sorted(seen.values(), key=lambda i: i["id"], reverse=True)


def _live(root: pathlib.Path) -> bool:
    return lockprobe.probe(root / ".swarm" / "state.lock")


def runs(root: pathlib.Path) -> list[str]:
    found = _runs_of(root)
    # Живой прогон — самый свежий run_id при занятом замке: у петли нет
    # записи «я начался», и доска (`board._run_facts`) решает так же.
    live = _live(root)
    out = []
    for i, r in enumerate(found):
        state = "идёт" if live and i == 0 else "завершён"
        tasks_s = ",".join(r["tasks"][:4]) + ("…" if len(r["tasks"]) > 4 else "")
        out.append(_line(r["id"], f"{state} · {r['t1'][:16]} · {tasks_s}"))
    return out


def _now(root: pathlib.Path) -> dict[str, Any]:
    try:
        row = json.loads((root / ".swarm" / "now.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def roots() -> list[str]:
    live_rows: list[str] = []
    idle: list[tuple[str, str]] = []
    for root, _seen in registry.roots():
        goal = _tasks_doc(root).get("goal", "")
        last = _runs_of(root)
        if _live(root):
            now = _now(root)
            where = " ".join(str(now.get(k) or "") for k in ("phase", "task")).strip()
            rid = last[0]["id"] if last else ""
            live_rows.append(_line(str(root), f"● идёт {rid} {where} · {goal}"))
        else:
            # Дата последнего ПРОГОНА, а не отметки реестра: реестр
            # отмечает и `swarm status`, и разовый скан — «видел сегодня»
            # у всех стендов сразу ничего не говорит о том, где работали.
            when = last[0]["t1"][:10] if last else ""
            idle.append(
                (
                    when,
                    _line(
                        str(root), f"простаивает · {when or 'прогонов нет'} · {goal}"
                    ),
                )
            )
    idle.sort(key=lambda r: r[0], reverse=True)
    return live_rows + [line for _, line in idle]


def candidates(kind: str, root: pathlib.Path) -> list[str]:
    name, _, arg = kind.partition(":")
    if name == "tasks":
        return tasks(root, set(filter(None, arg.split(","))) or None)
    if name == "questions":
        return questions(root)
    if name == "policies":
        return policies(root)
    if name == "lessons":
        return lessons(root)
    if name == "runs":
        return runs(root)
    if name == "roots":
        return roots()
    return []


def _parse(argv: list[str]) -> tuple[str, pathlib.Path]:
    args = [a for a in argv if a != "__complete"]
    root = pathlib.Path()
    rest: list[str] = []
    it = iter(args)
    for a in it:
        if a == "--root":
            root = pathlib.Path(next(it, "."))
        elif a.startswith("--root="):
            root = pathlib.Path(a.split("=", 1)[1])
        else:
            rest.append(a)
    return (rest[0] if rest else ""), root.expanduser()


def _diag(root: pathlib.Path) -> None:
    """Трассировку — в диагностику стенда, если он есть; иначе никуда."""
    swarm_dir = root / ".swarm"
    if not (swarm_dir / "log").is_dir():
        return
    try:
        import obs  # noqa: PLC0415 — тяжёлый импорт только на пути поломки

        obs.setup(swarm_dir)
        obs.get_logger("clitab").exception("автодополнение упало")
    except Exception:  # noqa: BLE001 — вторая поломка на пути первой: молчим
        return


def main(argv: Iterable[str] | None = None) -> int:
    kind, root = _parse(list(sys.argv[1:] if argv is None else argv))
    try:
        lines = candidates(kind, root)
    except Exception:  # noqa: BLE001 — граница деградации: Tab не падает
        _diag(root)
        return 0
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
