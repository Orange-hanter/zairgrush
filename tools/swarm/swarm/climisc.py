"""Служебные и вспомогательные команды: doctor, policy, plan, map, impact."""
import argparse
import ctypes.util
import json
import pathlib
import shutil
import subprocess
import sys

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402


def tree_sitter_clib() -> bool:
    """Есть ли на машине C-библиотека tree-sitter (brew/системная).

    Сама по себе она петле бесполезна — нужна, чтобы доктор отличил
    «не установлен вовсе» от «установлен не тот слой» и назвал точную
    команду. find_library на macOS не смотрит в /opt/homebrew, поэтому
    известные префиксы brew проверяются явно.
    """
    if ctypes.util.find_library("tree-sitter"):
        return True
    if shutil.which("tree-sitter"):
        return True
    return any(pathlib.Path(p, "lib", f"libtree-sitter{ext}").exists()
               for p in ("/opt/homebrew", "/usr/local")
               for ext in (".dylib", ".so"))



def cmd_doctor(args: argparse.Namespace) -> int:
    """Половина дефектов программы была в окружении, а не в петле."""
    root = pathlib.Path(args.root)
    cfg = cli.load_config(root)
    # Кем исполнять — выбор конфига, и доктор обязан проверять ВЫБРАННОЕ.
    # Пока движок был один, машина без `kimi` получала красную строку за
    # роль, которой на ней нет: диагноз говорил о чужой конфигурации.
    try:
        engine, model = cli.load_mod("engines").resolve(cfg)
    except ValueError as e:
        engine, model = "", ""
        checks_head = str(e)
    else:
        checks_head = ""
    print("=== окружение ===")
    checks: list[tuple[bool | None, str, str]] = []
    if checks_head:
        checks.append((False, "executor_engine", checks_head))
    else:
        checks.append((True, "движок исполнителя",
                       engine + (f", модель {model}" if model else
                                 ", модель по умолчанию CLI")))

    exec_hint = f"исполнитель (модель: {model})" if model else "исполнитель"
    for name, probe, hint, needed in (
        ("kimi", ["kimi", "--version"], exec_hint, engine == "kimi"),
        ("claude", ["claude", "--version"],
         ("ревьюер, планировщик и исполнитель" if engine == "claude"
          else "ревьюер и планировщик"), True),
        ("git", ["git", "--version"], "обязателен", True),
    ):
        exe = shutil.which(name)
        if not exe:
            # Ненужный движок отсутствовать ИМЕЕТ ПРАВО: это не поломка
            # стенда, а другая его конфигурация.
            checks.append((False if needed else None, name,
                           f"НЕ НАЙДЕН ({hint})" if needed
                           else f"не установлен (движок не выбран: {hint})"))
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

    # Стенд внутри `.claude/` — не поломка окружения, а ловушка среды:
    # файлы там Claude Code считает чувствительными и отказывается их
    # править, поэтому исполнитель честно возвращает dispute вместо
    # работы. Прозой это в руководстве оператора было, машиной — нет: на
    # cod-doc стенд оказался ровно там (`EnterWorktree` кладёт worktree
    # в `.claude/worktrees/`), и диагноз пришлось искать в тексте
    # руководства, а не в выводе доктора. Проверка по ЧАСТЯМ пути, а не
    # по префиксу: каталог встречается на любой глубине.
    if ".claude" in root.resolve().parts:
        checks.append((False, "стенд",
                       ("внутри .claude/ — Claude Code считает файлы там "
                        "чувствительными, исполнитель вернёт dispute "
                        "вместо работы; перенесите стенд наружу")))

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
    #
    # Петле нужны python-биндинги И обе грамматики (см. tsindex) — а
    # brew-пакет tree-sitter ставит только C-библиотеку. Доктор, смотревший
    # на один модуль tree_sitter, говорил «не установлен» человеку, у
    # которого brew-пакет стоит, — и спор шёл о двух разных вещах.
    ts_missing = []
    for ts_mod in ("tree_sitter", "tree_sitter_python", "tree_sitter_rust"):
        try:
            ts_spec = cli.importlib.util.find_spec(ts_mod)
        except Exception:  # noqa: BLE001 — доктор обязан досказать список до конца
            ts_spec = None
        if ts_spec is None:
            ts_missing.append(ts_mod.replace("_", "-"))
    if not ts_missing:
        checks.append((True, "tree-sitter", "python-биндинги и грамматики py/rs"))
    else:
        ts_hint = "pip install " + " ".join(ts_missing)
        if len(ts_missing) < 3:
            checks.append((None, "tree-sitter", f"биндинги неполные: {ts_hint}"))
        elif cli.tree_sitter_clib():
            checks.append((None, "tree-sitter",
                           ("стоит только C-библиотека (brew), петле нужны "
                            f"python-биндинги: {ts_hint}")))
        else:
            checks.append((None, "tree-sitter",
                           f"не установлен (опционально): {ts_hint}"))

    # Память (E9) — опциональна: её отсутствие деградирует поиск уроков,
    # а не петлю. Доктор называет точные команды настройки, но не
    # выполняет их сам: базу и роль создаёт владелец.
    if shutil.which("psql"):
        mem_mod = cli.load_mod("memory")
        cfg_doc = cli.load_config(args.root)
        ok_pg, out_pg = mem_mod.pg(cfg_doc, "SELECT version();")
        if ok_pg:
            checks.append((True, "memory-pg",
                           out_pg.split(" on ")[0][:40] or "доступен"))
        else:
            checks.append((None, "memory-pg",
                           ("недоступен (опционально): создать — "
                            "createdb swarm_memory; уроки при этом "
                            "копятся в файлах")))
    else:
        checks.append((None, "memory-pg", "psql не установлен (опционально)"))

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

    cfg = cli.load_config(root)
    gate_cmd = cfg.get("gate_command")
    if gate_cmd is None:
        print("[ опц ] gate: по умолчанию (unittest)")
    elif isinstance(gate_cmd, list) and all(isinstance(x, str) for x in gate_cmd):
        print(f"[  ok ] gate: {' '.join(gate_cmd)}")
    else:
        # Ровно та ошибка, которую доктор обязан ловить вместо петли:
        # строкой этот ключ пишут чаще, чем списком, а падает он посреди
        # первой задачи и выглядит как авария задачи, а не как конфиг.
        print(f"[ПРОБЛ] gate: {gate_cmd!r} — нужен СПИСОК аргументов "
              f'(["python3", "-m", "pytest"]), иначе петля ищет файл с '
              f"таким именем")
    st = cli.state_mod.SwarmState(root)
    print(f"[  ok ] состояние: {st.dir}")
    # Политика денег — то, о чём спрашивают доктора чаще всего постфактум
    # («почему ревьюер обрублен?», «почему прогон встал?»). Незнакомый
    # режим здесь не роняет доктора: он для того и нужен, чтобы назвать
    # проблему конфига до прогона, а не вместе с ним.
    spending = cli.load_mod("spending")
    try:
        print(f"[  ok ] деньги: {spending.headline(cfg, st.total_spend())}")
    except ValueError as e:
        checks.append((False, "деньги", str(e)))
        print(f"[ПРОБЛ] деньги: {e}")
    return 0 if all(c[0] is not False for c in checks) else 1



def cmd_policy(args: argparse.Namespace) -> int:
    """Политики прогона: решения человека уровня цели, а не задачи."""
    st = cli.state_mod.SwarmState(args.root)
    if args.action == "list":
        policies = st.policies()
        if not policies:
            print("политик нет")
            return 0
        print(f"активные политики (цель: {st.load_tasks().get('goal', '')[:60]}):")
        for p in policies:
            print(f"  {p['pid']}  {p['text']}")
            # Журнал — данные: match могла оставить строка вместо списка
            # (прежний формат, правка руками), и `", ".join` рассыпал бы
            # её в буквы «r, e, l, e, a, s, e».
            match = p.get("match")
            words = match if isinstance(match, list) else [match] if match else []
            print(f"        совпадение по: {', '.join(str(m) for m in words)}")
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
        if not args.text.strip():
            # nargs="?" с умолчанием "" пропускал пустую политику: она
            # ничего не выражает, но занимает pid и место в журнале.
            print('нужен текст политики: policy add "текст" --match слово',
                  file=sys.stderr)
            return 2
        if not args.match:
            print('нужны ключевые слова: --match "release note"', file=sys.stderr)
            return 2
        pid = st.add_policy(args.text, args.match)
        print(f"политика {pid} добавлена: {args.text}")
        print(f"будет подавлять находки со словами: {', '.join(args.match)}")
        print("ревьюер по-прежнему их сообщает — фильтрует оркестратор, "
              "подавленное видно в `swarm report`")
        return 0
    # Осталось только remove: argparse через choices уже закрыл
    # пространство действий, и хвостовой `return 2` изображал обработку
    # ошибки, которой не бывает.
    try:
        st.drop_policy(args.text)
    except cli.state_mod.StateError as e:
        print(f"{e}", file=sys.stderr)
        return 2
    print(f"политика {args.text} снята")
    return 0



def cmd_plan(args: argparse.Namespace) -> int:
    """Планировщик (§3.1): цель -> план-дифф, валидируемый механически.

    Модуль был написан и покрыт тестами, но не имел входа в CLI — роль
    существовала как библиотека, а не как участник петли.
    """
    planner = cli.load_mod("planner")
    st = cli.state_mod.SwarmState(args.root)
    data = st.load_tasks()
    tasks = data.get("tasks", [])
    files, suite = planner.repo_map(pathlib.Path(args.root))
    cfg = cli.load_config(args.root)

    if args.cmd == "plan":
        if not args.goal:
            print("нужна --goal", file=sys.stderr)
            return 2
        # Память (E9): уроки прошлых прогонов по этой цели — тупики
        # прошлых декомпозиций дороже всего именно планировщику.
        mem_block = cli.load_mod("memory").inject_block(
            "planner", {"id": "*", "title": args.goal, "paths": []}, st, cfg)
        prompt = planner.plan_prompt(args.goal, tasks, files, suite,
                                     memory=mem_block)
    else:
        task = next((t for t in tasks if t["id"] == args.task), None)
        if task is None:
            print(f"задача {args.task!r} не найдена", file=sys.stderr)
            return 2
        dispute = {}
        if args.dispute:
            dispute = json.loads(pathlib.Path(args.dispute).read_text())
        # Та же память, что и у plan: спор о границах решается знанием
        # прошлых таких решений, а не заново с чистого листа.
        mem_block = cli.load_mod("memory").inject_block(
            "planner", task, st, cfg)
        prompt = planner.replan_prompt(task, dispute, tasks, files, suite,
                                       memory=mem_block)
    # Потолок вызова планировщика — через общую политику денег, а не
    # чтением ключа: иначе `money_bin` действовал бы на две роли из
    # трёх, и режим врал бы своим названием.
    spending_mod = cli.load_mod("spending")
    diff, errs, reason = planner.plan_with_retry(
        prompt, args.cmd, tasks, root=args.root,
        budget=spending_mod.call_cap(cfg, "plan_budget_usd"),
        model=cfg.get("plan_model"), effort=cfg.get("plan_effort"),
        timeout=cfg.get("plan_timeout"))
    if errs:
        print("ЭСКАЛАЦИЯ:", *errs, sep="\n  ", file=sys.stderr)
        st.log("plan_failed", mode=args.cmd, reason=reason, errors=errs)
        qid = st.ask("*", "plan_failed", errs[0], mode=args.cmd)
        print(f"вопрос оператору: {qid}", file=sys.stderr)
        return 2

    print(f"analysis: {diff['analysis'][:400]}\n")
    protected = cfg.get("protected_paths") or []
    for op in diff["ops"]:
        task_body = op.get("task") or {}
        print(f"  {op['op']:6} {op['id']}  {task_body.get('title', '')[:60]}")
        print(f"         reason: {op['reason'][:150]}")
        # Спутники границы — совет, а не запрет (§3.1.1). На PILOT-1 семь
        # споров из семи были признаны: граница ставилась неверно, и
        # каждый спор стоил раунда плюс ожидания человека. Здесь это
        # видно ДО прогона и правится одной строкой в paths.
        if not task_body.get("paths"):
            continue
        for w in planner.boundary_warnings(args.root, task_body, protected):
            print(f"         ⚠ граница: {w['file']} ({w['token']}) — "
                  f"{w['hint']}")
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

