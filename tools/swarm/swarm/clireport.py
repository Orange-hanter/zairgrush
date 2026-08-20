"""Команды отчётности и наблюдения: report, board, ab, map, impact."""
import argparse
import contextlib
import json
import pathlib
import subprocess
import sys
import time
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402
from cliexplain import _prefix  # noqa: E402


def cmd_map(args: argparse.Namespace) -> int:
    codemap = cli._load("codemap")
    idx = codemap.HybridIndex(args.root, use_tree_sitter=args.tree_sitter)
    print(idx.project_map(budget=args.budget))
    if args.verbose:
        print("\n" + json.dumps(idx.report(), ensure_ascii=False, indent=1))
    return 0



def cmd_impact(args: argparse.Namespace) -> int:
    codemap = cli._load("codemap")
    idx = codemap.HybridIndex(args.root, use_tree_sitter=args.tree_sitter)
    print(idx.impact(args.symbol))
    return 0



def cmd_ab(args: argparse.Namespace) -> int:
    """Сводка по рукам замера: что дал жребий за все прогоны.

    Отвечает на два разных вопроса, и путать их нельзя.

    СВОДКА ПО РУКАМ — сравнение между разными диффами. Быстро считается,
    но число находок зависит от сложности кода сильнее, чем от модели:
    на PILOT-1 один дифф дал 5 находок, другой 2, и разница была про код.
    Такому сравнению нужны десятки задач.

    ПАРЫ — два вызова с РАЗНЫМИ параметрами по одному и тому же диффу.
    Это и есть ценность парного дизайна: дифф одинаков, значит разница в
    находках относится к руке, а не к задаче. Пары рождаются сами, потому
    что каждую задачу мы ревьюим дважды.

    Ни то, ни другое не измеряет СУЩЕСТВО находок: «шесть против двух»
    может значить и «нашёл больше», и «нашумел». Счёт — повод прочитать
    сырые вердикты в .swarm/log, а не замена чтению.
    """
    st = cli.state_mod.SwarmState(args.root)
    if not st.metrics_path.exists():
        print("метрик нет")
        return 0
    rows = []
    for line in st.metrics_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("phase") != "review" or not e.get("valid"):
            continue
        if args.run and e.get("run_id") != args.run:
            continue
        rows.append(e)
    if not rows:
        print("нет валидных ревью в метриках")
        return 0
    # Метрики дописываются в ОДИН файл прогон за прогоном. Пока сводка их
    # не различала, «пул моделей» и «одна модель на весь прогон» ложились
    # в одну кучу и давали сравнение, которого никто не ставил.
    runs = sorted({str(e.get("run_id") or "(без прогона)") for e in rows})
    if len(runs) > 1 and not args.run:
        print(f"в выборке {len(runs)} прогонов; отдельно — `swarm ab "
              f"--run {runs[-1]}`\n")

    def arm(e: dict[str, Any]) -> tuple[str, str]:
        return (e.get("model") or "(сессионная)", e.get("effort") or "(сессионный)")

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in rows:
        groups.setdefault(arm(e), []).append(e)

    print(f"=== руки замера ({len(rows)} валидных ревью) ===")
    print(f"{'модель':<20}{'усилие':<14}{'n':>4}{'$/вызов':>10}"
          f"{'находок':>10}{'approve':>9}")
    for key in sorted(groups):
        g = groups[key]
        cost = sum(e.get("cost_usd") or 0 for e in g) / len(g)
        find = sum(e.get("findings") or 0 for e in g) / len(g)
        appr = sum(1 for e in g if e.get("verdict") == "approve")
        print(f"{key[0]:<20}{key[1]:<14}{len(g):>4}{cost:>10.2f}"
              f"{find:>10.1f}{appr:>6}/{len(g)}")

    # Пары: один и тот же дифф (задача + итерация) двумя разными руками.
    # Итерация — правильный ключ диффа: внутри неё исполнитель не
    # вызывался, значит код тот же.
    by_diff: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for e in rows:
        by_diff.setdefault((e.get("task"), e.get("iter")), []).append(e)
    pairs = [(k, v) for k, v in by_diff.items()
             if len({arm(e) for e in v}) > 1]
    print(f"\n=== пары на одном диффе: {len(pairs)} ===")
    if not pairs:
        print("  пока нет. Пара возникает, когда жребий на двух вызовах")
        print("  одной итерации выпал разным — задайте пул в swarm.toml:")
        print('  review_model_pool = ["claude-opus-5", "claude-sonnet-5"]')
        return 0
    for (task, it), g in sorted(pairs):
        print(f"  {task} итерация {it}:")
        for e in g:
            m, ef = arm(e)
            print(f"    {m} / {ef}: ${e.get('cost_usd') or 0:.2f}, "
                  f"находок {e.get('findings')}, {e.get('verdict')}")
    print("\nСчёт находок — повод прочитать вердикты в .swarm/log, а не вывод.")
    return 0



def cmd_report(args: argparse.Namespace) -> int:
    """Хроника прогона связным текстом (ADR-001: журнал первичен, md — рендер).

    Прежде здесь печаталось `ts kind {json…}` с обрезкой на 160 символах —
    то есть `jq` для бедных, притом обрезка могла срезать ровно то поле,
    ради которого отчёт и открывали. Между тем руководство оператора
    называет эту команду ПЕРВОЙ в порядке диагностики: человек приходит
    сюда в тот момент, когда прогон уже встал, и получает дамп.

    Записи сгруппированы по задачам, потому что разбирают прогон именно
    так — «что было с этой». События прогона (бюджет, план, preflight)
    ничьи и идут отдельным блоком: приклеенные к задаче, они объясняли бы
    остановку очереди не тем.
    """
    vocab = cli._load("vocab")
    st = cli.state_mod.SwarmState(args.root)
    if not st.journal_path.exists():
        print("журнал пуст")
        return 0
    rows = []
    for line in st.journal_path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            rows.append(json.loads(line))
    if args.task:
        rows = [r for r in rows if r.get("task") == args.task]
    if not rows:
        print(f"в журнале нет записей по задаче {args.task!r}" if args.task
              else "журнал пуст")
        return 0
    if args.json:
        # Сырьё остаётся доступным и БЕЗ обрезки: проза — удобство, а
        # первоисточник обязан быть достижим целиком.
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    titles = {t["id"]: t for t in st.load_tasks().get("tasks", [])}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(str(r.get("task") or ""), []).append(r)

    # Блок прогона — открытый фильтр «запись без задачи», а не список
    # видов (то же правило, что у board.collect). Бухгалтерия
    # (BOOKKEEPING_KINDS) наружу не идёт: state_written сопровождает
    # каждую запись состояния и хоронил под собой редкие события —
    # бюджет, план; сырьё через --json остаётся полным.
    run_level = [r for r in groups.pop("", [])
                 if r.get("kind") not in vocab.BOOKKEEPING_KINDS]
    if run_level:
        print("=== прогон в целом ===")
        for r in run_level:
            print(f"  {str(r.get('ts', ''))[11:19]}  {vocab.narrate(r)}")
        print()

    for tid in sorted(groups):
        task = titles.get(tid, {})
        head = f"=== {tid}"
        if task.get("title"):
            head += f": {task['title']}"
        if task.get("status"):
            head += f"  [{vocab.ru(vocab.STATUS_RU, task['status'])}]"
        print(head)
        for r in groups[tid]:
            print(f"  {str(r.get('ts', ''))[11:19]}  {vocab.narrate(r)}")
        print()

    print(f"записей: {len(rows)}; сырьё без обрезки — "
          f"`{_prefix(str(args.root))} report --json`")
    return 0



def cmd_board(args: argparse.Namespace) -> int:
    """Доска прогона: всё происходящее одной страницей, без посредника.

    `--serve` — не то же самое, что живая доска во время `run`/`go`: там
    сервер живёт фоновым потоком внутри процесса петли и не занимает
    терминал. Здесь человек попросил посмотреть доску живьём САМ по
    себе — ей естественно занять терминал на переднем плане и явно
    остановиться по Ctrl+C, а не повиснуть в фоне процессом, о котором
    забыли.
    """
    board_mod = cli._load("board")
    out, board = board_mod.build(args.root, args.out)
    open_q = [q for q in board["questions"] if q["status"] == "open"]
    print(f"доска: {out}")
    print(f"  задач {len(board['tasks'])}, потрачено ${board['total']}, "
          f"ждут вас {len(open_q)}")
    if args.serve:
        cfg = cli._config(args.root)
        port = args.port if args.port is not None else cfg.get("board_port", 7433)
        server = cli._load("boardserve").BoardServer(
            args.root, port=port)
        server.start()
        print(f"живая доска: {server.url}")
        print("Ctrl+C — остановить")
        if args.open and sys.platform == "darwin":
            subprocess.run(["open", server.url], check=False)
        try:
            # Полезной работы у главного потока нет — сервер уже крутится
            # в своём daemon-потоке (serve_forever); здесь только ждём
            # сигнала на выход.
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nостанавливаю живую доску…")
        finally:
            server.stop()
        return 0
    if args.open:
        subprocess.run(["open", str(out)], check=False)
    return 0

