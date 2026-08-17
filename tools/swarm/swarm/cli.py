#!/usr/bin/env python3
"""Единая точка входа: `swarm <команда>`.

    swarm go --goal "цель"              ОТ А ДО Я: рой планирует сам и исполняет
    swarm run [--limit N] [--dry-run]   прогнать очередь задач
    swarm resume                        продолжить после падения
    swarm status                        состояние очереди и бюджета
    swarm inbox [--all]                 вопросы к человеку, накопленные петлёй
    swarm answer <id> "текст"           ответить и вернуть задачу в очередь
    swarm retry <task> [--note ...]     вернуть заблокированную задачу вручную
    swarm plan --goal "цель"            декомпозиция цели в задачи (план-дифф)
    swarm replan <task> --dispute файл  пересмотр плана по спору исполнителя
    swarm policy add "текст" --match .. решение уровня прогона, а не задачи
    swarm why [задача]                  почему встала и что делать дальше
    swarm report [--task ID] [--json]   хроника прогона связным текстом
    swarm board [--open]                доска прогона одной страницей (HTML)
    swarm map [--budget N]              карта символов репозитория
    swarm impact <symbol>               кто вызывает символ
    swarm doctor                        проверка окружения

Проверка окружения (`doctor`) вынесена в отдельную команду сознательно:
половина дефектов программы экспериментов была не в петле, а в среде —
не тот ctags, отсутствующий языковой сервер, старая версия CLI.
"""
import argparse
import contextlib
import fnmatch
import importlib.util
import inspect
import itertools
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
from types import ModuleType
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


state_mod = _load("state")
loop_mod = _load("loop")


# Все ключи, которые петля где-либо читает. Список закрытый намеренно:
# опечатка в имени ключа (`max_iteration` без s) молча включала умолчание,
# и оператор был уверен, что его настройка действует.
KNOWN_CONFIG_KEYS = frozenset({
    "gate_command", "protected_paths", "max_iterations", "confirmations",
    "gate_timeout", "silence_timeout", "wall_clock_cap", "executor_model",
    "review_budget_usd", "verification", "total_budget_usd", "live_board",
    "map_budget", "tuning_seed", "quota_backoff_s",
    "plan_budget_usd", "plan_model", "plan_effort", "plan_timeout",
    "review_model", "review_effort", "review_model_pool", "review_effort_pool",
    "confirm_model", "confirm_effort", "confirm_model_pool",
    "confirm_effort_pool",
})


def _config(root: str | pathlib.Path) -> dict[str, Any]:
    path = pathlib.Path(root) / "swarm.toml"
    cfg = {"gate_command": None, "protected_paths": ["tests/*", "tests/**"]}
    if path.exists():
        try:
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            # Молчать нельзя: дальше петля пойдёт на умолчаниях, а оператор
            # будет уверен, что его настройки применились.
            print(f"ВНИМАНИЕ: {path} не прочитан ({e}); "
                  f"работаем на умолчаниях", file=sys.stderr)
        else:
            unknown = sorted(set(parsed) - KNOWN_CONFIG_KEYS)
            if unknown:
                # Предупреждение, а не отказ: ключ может быть нужен
                # будущей версии или чужому инструменту, читающему тот же
                # файл. Но молчать нельзя — см. историю с умолчаниями выше.
                print(f"ВНИМАНИЕ: {path}: незнакомые ключи "
                      f"({', '.join(unknown)}) — петля их не читает; "
                      f"если это настройка петли, проверь имя",
                      file=sys.stderr)
            cfg.update(parsed)
    return cfg


# --- команды -------------------------------------------------------------

def _ui(*args: object) -> None:
    """Печать хода петли с немедленным сбросом буфера.

    Голый `print` буферизуется поблочно, когда stdout не терминал. Прогон
    в фоне (`swarm go > run.log`) писал в файл НОЛЬ БАЙТ пятьдесят минут,
    хотя петля исправно печатала каждый раунд: всё лежало в буфере до
    конца процесса. Инструмент, ценность которого в наблюдаемости хода,
    обязан быть виден и когда его вывод перенаправлен.
    """
    print(*args, flush=True)


def cmd_status(args: argparse.Namespace) -> int:
    st = state_mod.SwarmState(args.root)
    data = st.load_tasks()
    tasks = data.get("tasks", [])
    if not tasks:
        print("очередь пуста (.swarm/tasks.json отсутствует или без задач)")
        return 0
    by_status: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_status.setdefault(t["status"], []).append(t)
    print(f"цель: {data.get('goal') or '(не задана)'}")
    print(f"задач: {len(tasks)}")
    for status in ("in_progress", "in_review", "pending", "blocked", "done"):
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"\n{status} ({len(group)}):")
        for t in group:
            extra = []
            if t.get("iterations"):
                extra.append(f"итераций {t['iterations']}")
            if t.get("reason"):
                extra.append(t["reason"])
            if t.get("stash"):
                extra.append(f"stash {t['stash']}")
            if t.get("commit"):
                extra.append(t["commit"])
            tail = f"  [{', '.join(extra)}]" if extra else ""
            print(f"  {t['id']}  {t['title'][:60]}{tail}")

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
    _print_next(args.root, tasks, open_q)
    return 0


def _print_next(root: str, tasks: list[dict[str, Any]],
                open_q: list[dict[str, Any]]) -> None:
    """Одна строка «дальше» вместо необходимости помнить весь набор команд.

    Сводка состояния отвечает на вопрос «что происходит», но человек
    приходит с другим — «что мне теперь делать». Ответ выводится из того
    же состояния и не требует держать в голове руководство оператора.
    """
    pre = _prefix(root)
    by_status: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_status.setdefault(str(t.get("status")), []).append(t)
    if open_q:
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Половина дефектов программы была в окружении, а не в петле."""
    root = pathlib.Path(args.root)
    print("=== окружение ===")
    checks: list[tuple[bool | None, str, str]] = []

    for name, probe, hint in (
        ("kimi", ["kimi", "--version"], "исполнитель"),
        ("claude", ["claude", "--version"], "ревьюер и планировщик"),
        ("git", ["git", "--version"], "обязателен"),
    ):
        exe = shutil.which(name)
        if not exe:
            checks.append((False, name, f"НЕ НАЙДЕН ({hint})"))
            continue
        try:
            out = subprocess.run(probe, capture_output=True, text=True,
                                 timeout=30,
                                 check=False).stdout.strip().splitlines()
            checks.append((True, name, out[0] if out else exe))
        except (OSError, subprocess.SubprocessError) as e:
            # Доктор проверяет ЗАПУСКАЕМОСТЬ: сюда попадают отсутствие
            # прав, битый бинарь и таймаут. Прочее — дефект самого
            # доктора, и он должен быть виден, а не превращён в строку
            # отчёта о чужом инструменте.
            checks.append((False, name, f"ошибка запуска: {e}"))

    # ctags: важно отличить Universal от Exuberant — под именем `ctags`
    # ставится древняя реализация без JSON и ролей
    ctags = shutil.which("ctags")
    if ctags:
        try:
            # Таймаут тот же, что у kimi/claude выше: доктор без таймаута
            # сам становился зависшим инструментом, который диагностирует.
            ver = subprocess.run([ctags, "--version"], capture_output=True,
                                 text=True, timeout=30, check=False).stdout
        except (OSError, subprocess.SubprocessError) as e:
            checks.append((False, "ctags", f"ошибка запуска: {e}"))
        else:
            if "Universal Ctags" in ver:
                checks.append((True, "ctags", ver.splitlines()[0]))
            else:
                checks.append((False, "ctags",
                               ("Exuberant/BSD — нужен universal-ctags "
                                "(brew unlink ctags && brew install "
                                "universal-ctags)")))
    else:
        checks.append((None, "ctags", "не установлен (опционально)"))

    # find_spec сообщает об отсутствии модуля значением None, а не
    # исключением: проверка через try ловила пустоту, и доктор объявлял
    # tree-sitter доступным на любой машине.
    try:
        ts_spec = importlib.util.find_spec("tree_sitter")
    except Exception:  # noqa: BLE001 — доктор обязан досказать список до конца
        ts_spec = None
    if ts_spec is not None:
        checks.append((True, "tree-sitter", "доступен"))
    else:
        checks.append((None, "tree-sitter", "не установлен (опционально)"))

    for ok, name, note in checks:
        mark = {True: "  ok ", False: "ПРОБЛ", None: " опц "}[ok]
        print(f"[{mark}] {name:12} {note}")

    print("\n=== репозиторий ===")
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                           capture_output=True, text=True, check=False)
    if dirty.returncode != 0:
        print("[ПРОБЛ] это не git-репозиторий")
    else:
        files = dirty.stdout.strip().splitlines()
        print(f"[{'  ok ' if not files else 'ПРОБЛ'}] worktree: "
              f"{'чист' if not files else f'{len(files)} изменённых файлов'}")

    cfg = _config(root)
    print(f"[ опц ] gate: {cfg.get('gate_command') or 'по умолчанию (unittest)'}")
    st = state_mod.SwarmState(root)
    print(f"[  ok ] состояние: {st.dir}")
    return 0 if all(c[0] is not False for c in checks) else 1


def cmd_map(args: argparse.Namespace) -> int:
    codemap = _load("codemap")
    idx = codemap.HybridIndex(args.root, use_tree_sitter=args.tree_sitter)
    print(idx.project_map(budget=args.budget))
    if args.verbose:
        print("\n" + json.dumps(idx.report(), ensure_ascii=False, indent=1))
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    codemap = _load("codemap")
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
    st = state_mod.SwarmState(args.root)
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
    vocab = _load("vocab")
    st = state_mod.SwarmState(args.root)
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

    run_level = groups.pop("", [])
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
    vocab = _load("vocab")
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
    board_mod = _load("board")
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


def cmd_inbox(args: argparse.Namespace) -> int:
    """Накопившиеся вопросы к человеку — разбираются пачкой."""
    st = state_mod.SwarmState(args.root)
    questions = st.questions(only_open=not args.all)
    if not questions:
        print("вопросов нет" if args.all else "открытых вопросов нет")
        return 0
    for q in questions:
        mark = "?" if q["status"] == "open" else "v"
        print(f"[{mark}] {q['qid']}  задача {q['task']}  ({q['qkind']})")
        print(f"     {q['question'][:150]}")
        if q.get("findings"):
            for f in q["findings"][:3]:
                print(f"       - [{f.get('category')}] {str(f.get('issue'))[:110]}")
                if f.get("suggestion"):
                    print(f"         предложение: {str(f['suggestion'])[:100]}")
        if q.get("mechanical_left"):
            print(f"     механических находок (чинятся сами): {q['mechanical_left']}")
        if q.get("stash"):
            print(f"     работа сохранена: {q['stash']}")
        if q["status"] == "answered":
            print(f"     ответ: {q['answer'][:150]}")
        print()
    open_count = sum(1 for q in questions if q["status"] == "open")
    if open_count:
        print(f"открытых: {open_count} — ответить: "
              f'swarm answer <id> "текст"')
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
    st = state_mod.SwarmState(args.root)
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
    except state_mod.StateError as e:
        print(f"{e}", file=sys.stderr)
        return 2
    print(f"вопрос {args.qid} закрыт, задача {task_id} возвращена в очередь")
    if args.add_path:
        print(f"границы задачи расширены: {', '.join(args.add_path)}")
    print("решение уйдёт исполнителю следующим запуском `swarm run`")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    """Политики прогона: решения человека уровня цели, а не задачи."""
    st = state_mod.SwarmState(args.root)
    if args.action == "list":
        policies = st.policies()
        if not policies:
            print("политик нет")
            return 0
        print(f"активные политики (цель: {st.load_tasks().get('goal', '')[:60]}):")
        for p in policies:
            print(f"  {p['pid']}  {p['text']}")
            print(f"        совпадение по: {', '.join(p['match'])}")
        # честность важнее удобства: видно, сколько находок уже подавлено
        total = 0
        if st.journal_path.exists():
            for line in st.journal_path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("kind") == "policy_suppressed":
                    total += row.get("count", 0)
        if total:
            print(f"\nподавлено находок за прогон: {total} "
                  f"(`swarm report` покажет какие)")
        return 0
    if args.action == "add":
        if not args.match:
            print('нужны ключевые слова: --match "release note"', file=sys.stderr)
            return 2
        pid = st.add_policy(args.text, args.match)
        print(f"политика {pid} добавлена: {args.text}")
        print(f"будет подавлять находки со словами: {', '.join(args.match)}")
        print("ревьюер по-прежнему их сообщает — фильтрует оркестратор, "
              "подавленное видно в `swarm report`")
        return 0
    if args.action == "remove":
        try:
            st.drop_policy(args.text)
        except state_mod.StateError as e:
            print(f"{e}", file=sys.stderr)
            return 2
        print(f"политика {args.text} снята")
        return 0
    return 2


def cmd_plan(args: argparse.Namespace) -> int:
    """Планировщик (§3.1): цель -> план-дифф, валидируемый механически.

    Модуль был написан и покрыт тестами, но не имел входа в CLI — роль
    существовала как библиотека, а не как участник петли.
    """
    planner = _load("planner")
    st = state_mod.SwarmState(args.root)
    data = st.load_tasks()
    tasks = data.get("tasks", [])
    files, suite = planner.repo_map(pathlib.Path(args.root))

    if args.cmd == "plan":
        if not args.goal:
            print("нужна --goal", file=sys.stderr)
            return 2
        prompt = planner.plan_prompt(args.goal, tasks, files, suite)
    else:
        task = next((t for t in tasks if t["id"] == args.task), None)
        if task is None:
            print(f"задача {args.task!r} не найдена", file=sys.stderr)
            return 2
        dispute = {}
        if args.dispute:
            dispute = json.loads(pathlib.Path(args.dispute).read_text())
        prompt = planner.replan_prompt(task, dispute, tasks, files, suite)

    cfg = _config(args.root)
    diff, errs, reason = planner.plan_with_retry(
        prompt, args.cmd, tasks, root=args.root,
        budget=cfg.get("plan_budget_usd"),
        model=cfg.get("plan_model"), effort=cfg.get("plan_effort"),
        timeout=cfg.get("plan_timeout"))
    if errs:
        print("ЭСКАЛАЦИЯ:", *errs, sep="\n  ", file=sys.stderr)
        st.log("plan_failed", mode=args.cmd, reason=reason, errors=errs)
        qid = st.ask("*", "plan_failed", errs[0], mode=args.cmd)
        print(f"вопрос оператору: {qid}", file=sys.stderr)
        return 2

    print(f"analysis: {diff['analysis'][:400]}\n")
    for op in diff["ops"]:
        task_body = op.get("task") or {}
        print(f"  {op['op']:6} {op['id']}  {task_body.get('title', '')[:60]}")
        print(f"         reason: {op['reason'][:150]}")
    if args.dry_run:
        print("\ndry-run: план не применён")
        return 0
    # Применение — под замком записи и на СВЕЖЕЙ очереди: планирование
    # длится минуты, и статусы, изменившиеся за это время (бегущий прогон,
    # ответ оператора), нельзя затирать снимком из начала команды.
    with st.mutate():
        data = st.load_tasks()
        if args.cmd == "plan" and args.goal:
            data["goal"] = args.goal
        data["tasks"] = planner.apply_plan_diff(diff, data["tasks"])
        st.save_tasks(data)
    st.log("plan_applied", mode=args.cmd, ops=len(diff["ops"]),
           tasks_after=len(data["tasks"]))
    print(f"\nприменено: в очереди {len(data['tasks'])} задач(и)")
    return 0


def cmd_go(args: argparse.Namespace) -> int:
    """От А до Я: рой сам декомпозирует цель и сам её исполняет.

    Разница с `plan` + `run` не в удобстве: план должен рождаться ВНУТРИ
    роя, а не приноситься снаружи. Иначе декомпозиция — работа человека
    (или другой системы), и самодостаточности нет.

    Останавливается только на том, что действительно требует человека:
    вопрос о замысле, исчерпанный бюджет, авария.
    """
    cfg = _config(args.root)
    st = state_mod.SwarmState(args.root)
    if not _preflight(st, getattr(args, "force", False)):
        return 2

    existing = st.load_tasks().get("tasks", [])
    pending = [t for t in existing if t.get("status") == "pending"]
    if args.goal and not pending:
        print(f"== планирование: {args.goal}\n")
        rc = cmd_plan(argparse.Namespace(
            root=args.root, cmd="plan", goal=args.goal, task=None,
            dispute=None, dry_run=False))
        if rc != 0:
            return rc
        print()
    elif pending:
        print(f"в очереди уже {len(pending)} задач(и) — планирование пропущено\n")

    budget = cfg.get("total_budget_usd")
    if budget:
        print(f"бюджет прогона: ${budget}, потрачено ${st.total_spend()}\n")

    with state_mod.SwarmState(args.root) as locked:
        agents = _load("agents").Agents(locked, cfg)
        loop = loop_mod.Loop(locked, cfg, agents, ui=_ui)
        results = loop.run(limit=args.limit)

    board_mod = _load("board")
    out, board = board_mod.build(args.root)
    open_q = [q for q in board["questions"] if q["status"] == "open"]
    print("\nитог:", json.dumps({k: v for k, v in results.items()
                                 if not k.startswith("_")}, ensure_ascii=False))
    print(f"потрачено ${board['total']}"
          + (f" из ${budget}" if budget else ""))
    print(f"доска: {out}")
    if open_q:
        print(f"\nЖДУТ ВАС ({len(open_q)}):")
        for q in open_q:
            print(f"  {q['qid']}  {q['task']}  {str(q.get('question',''))[:90]}")
        print(f'  ответить: {_prefix(str(args.root))} answer <id> "текст"')
        print(f"  затем продолжить: {_prefix(str(args.root))} go")
    else:
        # Заблокированная без вопроса — самый глухой исход: инбокс пуст,
        # и человеку неоткуда узнать, что очередь встала и почему.
        stuck = [t for t in board["tasks"] if t.get("status") == "blocked"]
        if stuck:
            print(f"\nВСТАЛО ({len(stuck)}), вопросов в инбоксе нет:")
            for t in stuck:
                print(f"  {t['id']}  {str(t.get('title', ''))[:60]}")
            print(f"  разобрать: {_prefix(str(args.root))} "
                  f"why {stuck[0]['id']}")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    """Доска прогона: всё происходящее одной страницей, без посредника."""
    board_mod = _load("board")
    out, board = board_mod.build(args.root, args.out)
    open_q = [q for q in board["questions"] if q["status"] == "open"]
    print(f"доска: {out}")
    print(f"  задач {len(board['tasks'])}, потрачено ${board['total']}, "
          f"ждут вас {len(open_q)}")
    if args.open:
        subprocess.run(["open", str(out)], check=False)
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Вернуть заблокированную задачу в очередь.

    Не всякая блокировка снимается ответом на вопрос: задача может
    исчерпать раунды на технических сбоях, и человеку нужен прямой способ
    вернуть её в работу — при желании с указанием и расширенными
    границами.
    """
    st = state_mod.SwarmState(args.root)
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


def _preflight(st: Any, force: bool = False) -> bool:
    """§5.1: петля не запускается на грязном дереве.

    Оркестратор откатывает файлы и делает `git add -A`. Если в дереве
    лежит незакоммиченная работа человека, она может быть уничтожена
    безвозвратно — это единственный ущерб, который нельзя починить
    постфактум. Дешевле отказаться на входе.
    """
    dirty = st.changed_files()
    if dirty and not force:
        print("PREFLIGHT: рабочее дерево грязное — запуск отменён",
              file=sys.stderr)
        for path in dirty[:10]:
            print(f"  {path}", file=sys.stderr)
        if len(dirty) > 10:
            print(f"  ... ещё {len(dirty) - 10}", file=sys.stderr)
        print("закоммить или спрячь работу, либо запусти с --force",
              file=sys.stderr)
        return False
    if dirty:
        st.log("preflight_forced", dirty=dirty)
        print(f"PREFLIGHT: дерево грязное ({len(dirty)}), продолжаю по --force")
    return True


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args.root)
    with state_mod.SwarmState(args.root) as st:
        if not _preflight(st, getattr(args, "force", False)):
            return 2
        ready = st.ready_tasks()
        if not ready:
            print("нет задач, готовых к запуску")
            return 3
        if args.dry_run:
            print(f"dry-run: к исполнению {len(ready)} задач(и), агенты не вызываются")
            for t in ready:
                print(f"  {t['id']}  {t['title'][:60]}")
                print(f"     paths={t.get('paths')} deps={t.get('deps') or []}")
            ok, _tail = loop_mod.Loop(st, cfg, None).gate(ready[0])
            print(f"  baseline gate: {'зелёный' if ok else 'КРАСНЫЙ'}")
            return 0
        agents = _load("agents").Agents(st, cfg)
        loop = loop_mod.Loop(st, cfg, agents, ui=_ui)
        results = loop.run(limit=args.limit)
        print("\nитог:", json.dumps(results, ensure_ascii=False))
    return 0


ORCHESTRATOR_EMAIL = "orchestrator@swarm.local"


def _reconcile_decision(row: dict[str, Any],
                        root: str | pathlib.Path) -> tuple[str, str] | None:
    """Механическое решение по незавершённому интенту коммита (§5.6).

    Интент без done означает падение между действием и записью о нём.
    Повторять вслепую нельзя (дубль коммита), бросать тоже (задача висит).
    Сравнение записанного в интенте `head` с фактической историей отвечает
    на вопрос механически:

    - HEAD не сдвинулся → коммита не было: интент закрыть как
      проваленный, задачу вернуть в очередь;
    - первый коммит после записанного `head` сделан оркестратором →
      действие состоялось, падение пришлось на запись статуса: интент
      закрыть как выполненный, задача — done.

    Возвращает ("rollback", "") или ("complete", sha), либо None, если
    решить механически нельзя (интент без `head` — старый журнал; первый
    коммит чужой; история переписана) — тогда эскалация человеку.

    Решение отделено от применения намеренно: --dry-run обязан УЗНАТЬ
    исход, ничего не записывая, — прежде «сухой» прогон писал в журнал и
    переписывал tasks.json до всякой проверки флага.
    """
    head_before = row.get("head")
    if row.get("action") != "commit" or not head_before:
        return None
    cur = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=False)
    if cur.returncode != 0:
        return None
    if cur.stdout.strip() == head_before:
        return ("rollback", "")
    # Смотрим ПЕРВЫЙ коммит после записанной точки, а не HEAD: после
    # падения поверх могли коммитить и оператор, и следующий прогон.
    after = subprocess.run(["git", "log", "--reverse", "--format=%H %ce",
                            f"{head_before}..HEAD"], cwd=root,
                           capture_output=True, text=True, check=False)
    first = (after.stdout.strip().splitlines() or [""])[0].split()
    if after.returncode != 0 or len(first) < 2 or first[1] != ORCHESTRATOR_EMAIL:
        return None
    return ("complete", first[0][:8])


def _reconcile_commit_step(st: Any, row: dict[str, Any],
                           root: str | pathlib.Path) -> str | None:
    """Применить решение реконсиляции: журнал + статус задачи (§5.6)."""
    decision = _reconcile_decision(row, root)
    if decision is None:
        return None
    outcome, sha = decision
    if outcome == "rollback":
        st.log("step_failed", step_id=row["step_id"], task=row["task"],
               action="commit", reconciled=True,
               error="реконсиляция resume: HEAD не сдвинулся, коммита не было")
        data = st.load_tasks()
        for t in data["tasks"]:
            if t["id"] == row["task"] and t["status"] == "in_progress":
                t["status"] = "pending"
                t.pop("reason", None)
        st.save_tasks(data)
        return "коммита не было — задача возвращена в очередь"
    st.log("step_done", step_id=row["step_id"], task=row["task"],
           action="commit", reconciled=True, commit=sha)
    data = st.load_tasks()
    for t in data["tasks"]:
        if t["id"] == row["task"] and t["status"] != "done":
            t["status"] = "done"
            t["commit"] = sha
            t.pop("reason", None)
    st.save_tasks(data)
    return f"коммит {sha} состоялся — задача закрыта"


def cmd_resume(args: argparse.Namespace) -> int:
    """Возобновление после падения: разобрать незавершённые шаги (§5.6).

    Сначала реконсиляция: незавершённый интент коммита с записанным
    `head` доигрывается или откатывается механически. Человеку остаётся
    только то, что механически решить нельзя.

    Блокировку здесь НЕ берём: её возьмёт cmd_run. Иначе оркестратор
    отказывает сам себе — поймано тестами CLI.
    """
    st = state_mod.SwarmState(args.root)
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
    leftover = []
    previewed = 0
    for row in unfinished:
        if args.dry_run:
            # Сухой прогон обещает не трогать состояние, а реконсиляция
            # пишет в журнал и переписывает tasks.json — поэтому здесь
            # только решение, без применения.
            decision = _reconcile_decision(row, args.root)
            if decision is None:
                leftover.append(row)
                continue
            previewed += 1
            kind, sha = decision
            would = ("коммита не было — задача вернётся в очередь"
                     if kind == "rollback"
                     else f"коммит {sha} состоялся — задача закроется")
            print(f"реконсиляция (dry-run): {row['task']}: {would}")
            continue
        outcome = _reconcile_commit_step(st, row, args.root)
        if outcome:
            print(f"реконсиляция: {row['task']}: {outcome}")
        else:
            leftover.append(row)
    if leftover:
        print(f"незавершённых шагов: {len(leftover)}")
        for row in leftover:
            print(f"  {row['task']}: {row['action']}")
        head = subprocess.run(["git", "log", "-1", "--format=%s"],
                              cwd=args.root, capture_output=True, text=True,
                              check=False)
        print(f"  последний коммит: {head.stdout.strip()}")
        print("  проверьте, применился ли side-effect, и поправьте статус вручную")
        if not args.force:
            print("\nэскалация: возобновление требует решения человека "
                  "(повторить с `--force`, если состояние проверено)")
            return 2
    return cmd_run(args)


EPILOG = """
порядок применения (первый прогон):
  doctor                      проверить среду — тридцать секунд здесь
                              экономят час диагностики потом
  go --goal "цель"            от А до Я: рой сам планирует и сам исполняет
  board --open                смотреть, как идёт (страница живая: петля
                              переписывает её после каждого раунда)
  inbox -> answer <id> "…"    разобрать вопросы, которые петля отложила
  go                          продолжить с того же места

когда что-то пошло не так:
  why [задача]                почему встала и что нажать дальше
  report --task <id>          хроника задачи связным текстом
  report --json               то же сырьём, без обрезки
  retry <задача> --note "…"   вернуть в очередь с указанием

первое правило разбора: подозревайте обвязку, а не модель.
"""


def main(argv: list[str] | None = None) -> int:
    # Карта команд с порядком применения жила в докстринге модуля, то есть
    # была видна кому угодно, кроме того, кто набрал `swarm --help`.
    ap = argparse.ArgumentParser(
        prog="swarm", description="петля агентов: исполнитель ↔ ревьюер",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="корень целевого репозитория")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="прогнать очередь")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true",
                   help="показать план без вызова агентов")
    p.add_argument("--force", action="store_true",
                   help="запуститься на грязном дереве (риск потери работы)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", help="продолжить после падения")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("plan", help="декомпозиция цели в задачи")
    p.add_argument("--goal", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("replan", help="пересмотр плана по спору исполнителя")
    p.add_argument("task")
    p.add_argument("--dispute", help="файл с dispute исполнителя")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("go", help="от А до Я: рой планирует сам и исполняет")
    p.add_argument("--goal", help="цель; без неё берётся существующая очередь")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("board", help="доска прогона одной страницей")
    p.add_argument("--out", help="куда писать (по умолчанию .swarm/board.html)")
    p.add_argument("--open", action="store_true", help="открыть в браузере")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("status", help="состояние очереди")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="проверка окружения")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("map", help="карта символов репозитория")
    p.add_argument("--budget", type=int, default=25)
    p.add_argument("--tree-sitter", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("impact", help="кто вызывает символ")
    p.add_argument("symbol")
    p.add_argument("--tree-sitter", action="store_true")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("inbox", help="вопросы к человеку")
    p.add_argument("--all", action="store_true", help="включая отвеченные")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("answer", help="ответить на вопрос петли")
    p.add_argument("qid")
    p.add_argument("text")
    p.add_argument("--add-path", action="append", default=[],
                   help="расширить границы задачи (можно повторять)")
    p.add_argument("--force", action="store_true",
                   help="файл лишь упомянут в объяснении, а не задан к правке")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("policy", help="политики прогона")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("text", nargs="?", default="",
                   help="текст политики (add) или её id (remove)")
    p.add_argument("--match", action="append", default=[],
                   help="ключевое слово для сопоставления (можно повторять)")
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("retry", help="вернуть заблокированную задачу в очередь")
    p.add_argument("task")
    p.add_argument("--note", help="указание исполнителю")
    p.add_argument("--add-path", action="append", default=[])
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("ab", help="сводка по рукам замера (модель/усилие)",
                       description=inspect.getdoc(cmd_ab),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", help="только строки одного прогона (run_id)")
    p.set_defaults(func=cmd_ab)

    p = sub.add_parser("report", help="хроника прогона связным текстом",
                       description=inspect.getdoc(cmd_report),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--task", help="только события одной задачи")
    p.add_argument("--json", action="store_true",
                   help="сырые записи журнала без обрезки, по строке на запись")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("why", help="почему задача встала и что делать дальше",
                       description=inspect.getdoc(cmd_why),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("task", nargs="?",
                   help="id задачи или его начало; без аргумента — та, "
                        "что первой требует внимания")
    p.set_defaults(func=cmd_why)

    args = ap.parse_args(argv)
    # Диагностика включается ЗДЕСЬ, в единственной точке входа: модули
    # грузятся по путям и не знают, где состояние прогона, а знать
    # каталог обязан тот, кто разобрал --root.
    _load("obs").setup(pathlib.Path(args.root) / ".swarm")
    try:
        code: int = args.func(args)
    except state_mod.StateError as e:
        print(f"состояние: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        # По имени, не по классу: квоту поднимают Agents и планировщик из
        # СВОИХ копий модуля loop, и `except loop_mod.QuotaExceededError`
        # ловил только исключение собственной копии (см. quota_exception).
        if loop_mod.quota_exception(e):
            print(f"пауза по квоте провайдера: {e}", file=sys.stderr)
            return 4
        raise
    else:
        return code


if __name__ == "__main__":
    sys.exit(main())
