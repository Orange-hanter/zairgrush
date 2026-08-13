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
    swarm report [--task ID]            человекочитаемый отчёт из журнала
    swarm board [--open]                доска прогона одной страницей (HTML)
    swarm map [--budget N]              карта символов репозитория
    swarm impact <symbol>               кто вызывает символ
    swarm doctor                        проверка окружения

Проверка окружения (`doctor`) вынесена в отдельную команду сознательно:
половина дефектов программы экспериментов была не в петле, а в среде —
не тот ctags, отсутствующий языковой сервер, старая версия CLI.
"""
import argparse
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


state_mod = _load("state")
loop_mod = _load("loop")


def _config(root):
    path = pathlib.Path(root) / "swarm.toml"
    cfg = {"gate_command": None, "protected_paths": ["tests/*", "tests/**"]}
    if path.exists():
        try:
            import tomllib
            cfg.update(tomllib.loads(path.read_text(encoding="utf-8")))
        except Exception:                                   # noqa: BLE001
            pass
    return cfg


# --- команды -------------------------------------------------------------

def cmd_status(args):
    st = state_mod.SwarmState(args.root)
    data = st.load_tasks()
    tasks = data.get("tasks", [])
    if not tasks:
        print("очередь пуста (.swarm/tasks.json отсутствует или без задач)")
        return 0
    by_status = {}
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

    if st.metrics_path.exists():
        cost = 0.0
        for line in st.metrics_path.read_text().splitlines():
            try:
                cost += json.loads(line).get("cost_usd") or 0
            except ValueError:
                pass
        if cost:
            print(f"\nпотрачено дорогими ролями: ${cost:.2f}")
    return 0


def cmd_doctor(args):
    """Половина дефектов программы была в окружении, а не в петле."""
    root = pathlib.Path(args.root)
    print("=== окружение ===")
    checks = []

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
                                 timeout=30).stdout.strip().splitlines()
            checks.append((True, name, out[0] if out else exe))
        except Exception as e:                              # noqa: BLE001
            checks.append((False, name, f"ошибка запуска: {e}"))

    # ctags: важно отличить Universal от Exuberant — под именем `ctags`
    # ставится древняя реализация без JSON и ролей
    ctags = shutil.which("ctags")
    if ctags:
        ver = subprocess.run([ctags, "--version"], capture_output=True,
                             text=True).stdout
        if "Universal Ctags" in ver:
            checks.append((True, "ctags", ver.splitlines()[0]))
        else:
            checks.append((False, "ctags",
                           "Exuberant/BSD — нужен universal-ctags "
                           "(brew unlink ctags && brew install universal-ctags)"))
    else:
        checks.append((None, "ctags", "не установлен (опционально)"))

    try:
        importlib.util.find_spec("tree_sitter")
        checks.append((True, "tree-sitter", "доступен"))
    except Exception:                                       # noqa: BLE001
        checks.append((None, "tree-sitter", "не установлен (опционально)"))

    for ok, name, note in checks:
        mark = {True: "  ok ", False: "ПРОБЛ", None: " опц "}[ok]
        print(f"[{mark}] {name:12} {note}")

    print("\n=== репозиторий ===")
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                           capture_output=True, text=True)
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


def cmd_map(args):
    codemap = _load("codemap")
    idx = codemap.HybridIndex(args.root, use_tree_sitter=args.tree_sitter)
    print(idx.project_map(budget=args.budget))
    if args.verbose:
        print("\n" + json.dumps(idx.report(), ensure_ascii=False, indent=1))
    return 0


def cmd_impact(args):
    codemap = _load("codemap")
    idx = codemap.HybridIndex(args.root, use_tree_sitter=args.tree_sitter)
    print(idx.impact(args.symbol))
    return 0


def cmd_report(args):
    """Человекочитаемый рендер из jsonl (ADR-001: журнал первичен)."""
    st = state_mod.SwarmState(args.root)
    if not st.journal_path.exists():
        print("журнал пуст")
        return 0
    rows = []
    for line in st.journal_path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    if args.task:
        rows = [r for r in rows if r.get("task") == args.task]
    for r in rows:
        head = f"{r['ts']}  {r['kind']:14}"
        detail = {k: v for k, v in r.items()
                  if k not in ("ts", "kind", "step_id")}
        print(f"{head} {json.dumps(detail, ensure_ascii=False)[:160]}")
    return 0


def cmd_inbox(args):
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
              f"swarm answer <id> \"текст\"")
    return 0


def _paths_mentioned(text, allowed):
    """Файлы, названные в ответе, но отсутствующие в границах задачи.

    Ответ вроде «вынеси в _utils.py» невыполним, если этого файла нет в
    `paths`: исполнитель попробует, SCOPE-CHECK откатит, раунд сгорит.
    Дешевле предупредить человека сразу.
    """
    import re
    found = set(re.findall(r"[\w/.-]+\.(?:py|rs|ts|js|go|toml|md)", text))
    return sorted(f for f in found
                  if not any(f.endswith(a.lstrip("*")) or a.endswith(f)
                             for a in allowed))


def cmd_answer(args):
    """Ответ человека возвращает задачу в работу с его решением."""
    st = state_mod.SwarmState(args.root)
    questions = {q["qid"]: q for q in st.questions()}
    task_id = questions.get(args.qid, {}).get("task")
    task = next((t for t in st.load_tasks()["tasks"] if t["id"] == task_id), None)

    if task and not args.add_path:
        outside = _paths_mentioned(args.text, task.get("paths") or [])
        if outside:
            print(f"внимание: в ответе упомянуты файлы вне границ задачи: "
                  f"{', '.join(outside)}")
            print(f"границы задачи {task_id}: {task.get('paths')}")
            print("исполнитель не сможет их тронуть — SCOPE-CHECK откатит правки.")
            print(f"добавьте пути явно: swarm answer {args.qid} \"...\" "
                  f"--add-path {outside[0]}")
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


def cmd_policy(args):
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
            print("нужны ключевые слова: --match \"release note\"", file=sys.stderr)
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


def cmd_plan(args):
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
        budget=cfg.get("plan_budget_usd"))
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
    if args.cmd == "plan" and args.goal:
        data["goal"] = args.goal
    data["tasks"] = planner.apply_plan_diff(diff, tasks)
    st.save_tasks(data)
    st.log("plan_applied", mode=args.cmd, ops=len(diff["ops"]),
           tasks_after=len(data["tasks"]))
    print(f"\nприменено: в очереди {len(data['tasks'])} задач(и)")
    return 0


def cmd_go(args):
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
        loop = loop_mod.Loop(locked, cfg, agents, ui=print)
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
        print(f'  ответить: swarm --root {args.root} answer <id> "текст"')
        print(f"  затем продолжить: swarm --root {args.root} go")
    return 0


def cmd_board(args):
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


def cmd_retry(args):
    """Вернуть заблокированную задачу в очередь.

    Не всякая блокировка снимается ответом на вопрос: задача может
    исчерпать раунды на технических сбоях, и человеку нужен прямой способ
    вернуть её в работу — при желании с указанием и расширенными
    границами.
    """
    st = state_mod.SwarmState(args.root)
    data = st.load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == args.task), None)
    if task is None:
        print(f"задача {args.task!r} не найдена", file=sys.stderr)
        return 2
    # in_progress означает, что прогон умер на этой задаче (таймаут гейта,
    # отказ по квоте, сетевой сбой). Без этого выхода задача застревала
    # навсегда: ready_tasks берёт только pending, а retry требовал blocked.
    if task["status"] not in ("blocked", "in_progress"):
        print(f"задача {args.task} в статусе {task['status']}, возвращать нечего",
              file=sys.stderr)
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


def _preflight(st, force=False):
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


def cmd_run(args):
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
            ok, tail = loop_mod.Loop(st, cfg, None).gate(ready[0])
            print(f"  baseline gate: {'зелёный' if ok else 'КРАСНЫЙ'}")
            return 0
        agents = _load("agents").Agents(st, cfg)
        loop = loop_mod.Loop(st, cfg, agents, ui=print)
        results = loop.run(limit=args.limit)
        print("\nитог:", json.dumps(results, ensure_ascii=False))
    return 0


def cmd_resume(args):
    """Возобновление после падения: разобрать незавершённые шаги (§5.6).

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
    if unfinished:
        print(f"незавершённых шагов: {len(unfinished)}")
        for row in unfinished:
            print(f"  {row['task']}: {row['action']}")
        head = subprocess.run(["git", "log", "-1", "--format=%s"],
                              cwd=args.root, capture_output=True, text=True)
        print(f"  последний коммит: {head.stdout.strip()}")
        print("  проверьте, применился ли side-effect, и поправьте статус вручную")
        if not args.force:
            print("\nэскалация: возобновление требует решения человека "
                  "(повторить с `--force`, если состояние проверено)")
            return 2
    return cmd_run(args)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="swarm", description="петля агентов")
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

    p = sub.add_parser("report", help="отчёт из журнала")
    p.add_argument("--task")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except state_mod.StateError as e:
        print(f"состояние: {e}", file=sys.stderr)
        return 2
    except loop_mod.QuotaExceeded as e:
        print(f"пауза по квоте провайдера: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
