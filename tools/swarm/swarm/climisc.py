"""Служебные и вспомогательные команды: doctor, policy, plan, map, impact."""

import argparse
import ctypes.util
import json
import pathlib
import shutil
import subprocess
import sys
import traceback

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
    return any(
        pathlib.Path(p, "lib", f"libtree-sitter{ext}").exists()
        for p in ("/opt/homebrew", "/usr/local")
        for ext in (".dylib", ".so")
    )


def _run_probe(
    cmd: list[str], *, cwd: str | pathlib.Path | None = None
) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    """Запуск probe-команды с общим таймаутом: (результат, ошибка запуска).

    Стержень для всех проб доктора — таймаут и обработка запуска живут в
    одном месте, а не копией в каждой проверке. Ошибка запуска — не отказ
    инструмента, а дефект среды: доктор обязан досказать список до конца.
    """
    try:
        return (
            subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False,
                cwd=cwd,
            ),
            "",
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"ошибка запуска: {e}"


def _probe_version(
    checks: list[tuple[bool | None, str, str]],
    name: str,
    cmd: list[str],
    fallback: str,
    needed: bool,
    *,
    stderr_ok: bool = False,
    launch_red: bool = False,
) -> None:
    """Один прогон проверки CLI: общий стержень для цикла движков и zcode.

    Один таймаут и один обработчик запуска на все пробы — иначе политика
    правилась бы в двух местах и расходилась. stderr_ok — для zcode,
    печатающего version в stderr: stderr читается ТОЛЬКО при нулевом коде
    возврата, а при ненулевом его первая строка идёт в строку диагноза
    как есть — по ней видно, это авария или предупреждение с версией.
    launch_red — правило цикла движков (kimi/claude/git): бинарь есть,
    но не запускается — красная строка даже для невыбранного движка.
    zcode его намеренно НЕ берёт: полусобранный бандл ZCode.app на
    стенде, который zcode не выбрал, — другая конфигурация, а не дефект
    (мягкое правило, что стояло здесь до рефакторинга, сохранено).
    """
    fail: bool | None = False if (launch_red or needed) else None
    proc, err = _run_probe(cmd)
    if proc is None:
        checks.append((fail, name, err))
        return
    if proc.returncode != 0:
        # Ненулевой код — отказ у ЛЮБОГО CLI: бинарь запустился и упал.
        # Для stderr_ok-инструментов stderr — штатный канал: показываем,
        # что именно напечатано (по нему видно, авария это или
        # предупреждение с версией). Для остальных — классический
        # диагноз запуска; хвост stderr/stdout — единственное место,
        # где CLI объясняет свой отказ.
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        note = f"код {proc.returncode}" + (
            f": {tail[0][:80]}" if tail else ": вывод пуст"
        )
        if not stderr_ok:
            note = f"ошибка запуска: {note}"
        checks.append((fail, name, note))
        return
    if stderr_ok:
        # Канал версии у таких CLI — stderr; stdout может нести баннерный
        # шум, и показывать шум вместо версии нельзя.
        out = (proc.stderr or proc.stdout or "").strip().splitlines()
    else:
        out = (proc.stdout or "").strip().splitlines()
    checks.append((True, name, out[0] if out else fallback))


def cmd_doctor(args: argparse.Namespace) -> int:
    """Половина дефектов программы была в окружении, а не в петле.

    Контракт кода возврата: 0 — ни одной красной строки (None-строки
    «опц» коду не мешают), 1 — есть хоть одна (False). Каждая красная
    ПЕЧАТЬ обязана иметь парную запись в checks — иначе CI, опирающийся
    на exit code, примет сломанный стенд за успех (проверено ревью:
    «не git-репозиторий» и битый gate_command когда-то печатались без
    записи и не влияли на код).
    """
    root = pathlib.Path(args.root)
    cfg = cli.load_config(root)
    # Кем исполнять — выбор конфига, и доктор обязан проверять ВЫБРАННОЕ.
    # Пока движок был один, машина без `kimi` получала красную строку за
    # роль, которой на ней нет: диагноз говорил о чужой конфигурации.
    try:
        eng_mod = cli.load_mod("engines")
        engine, model = eng_mod.resolve(cfg)
    except Exception as e:  # noqa: BLE001 — доктор обязан досказать список до конца
        # Диагностический инструмент не имеет права упасть на дефектном
        # конфиге: ровно за такими диагнозами его и звали. Кетч широкий
        # осознанно, но программный баг (AttributeError из рефакторинга)
        # не должен прятаться за строкой отчёта — стектрейс уходит в
        # stderr наряду с диагнозом. Код возврата при этом остаётся 0:
        # доктор обязан досказать отчёт до конца, и стектрейс в stderr —
        # ровно тот сигнал, по которому оператор отличает баг петли от
        # битого конфига. eng_mod=None — страж ниже: без него
        # zcode-проба превратилась бы в NameError.
        traceback.print_exc()
        engine, model = "", ""
        checks_head = str(e)
        eng_mod = None
    else:
        checks_head = ""
    print("=== окружение ===")
    checks: list[tuple[bool | None, str, str]] = []
    if checks_head:
        checks.append((False, "executor_engine", checks_head))
    else:
        checks.append(
            (
                True,
                "движок исполнителя",
                engine
                + (f", модель {model}" if model else ", модель по умолчанию CLI"),
            )
        )

    exec_hint = f"исполнитель (модель: {model})" if model else "исполнитель"
    for name, probe, hint, needed in (
        ("kimi", ["kimi", "--version"], exec_hint, engine == "kimi"),
        (
            "claude",
            ["claude", "--version"],
            (
                "ревьюер, планировщик и исполнитель"
                if engine == "claude"
                else "ревьюер и планировщик"
            ),
            True,
        ),
        ("git", ["git", "--version"], "обязателен", True),
    ):
        exe = shutil.which(name)
        if not exe:
            # Ненужный движок отсутствовать ИМЕЕТ ПРАВО: это не поломка
            # стенда, а другая его конфигурация.
            checks.append(
                (
                    False if needed else None,
                    name,
                    f"НЕ НАЙДЕН ({hint})"
                    if needed
                    else f"не установлен (движок не выбран: {hint})",
                )
            )
            continue
        _probe_version(checks, name, probe, exe, needed, launch_red=True)

    # zcode часто не в PATH: доктор зовёт бандл ZCode.app через zcode_cmd,
    # а не which("zcode"). Иначе стенд с установленным клиентом получал бы
    # «не найден» за роль, которая на машине есть. Зовём через общий
    # стержень проб; сверка «не найден» — по первому элементу списка
    # (то, что реально уйдёт на спавн): она переживёт появление флагов в
    # zcode_cmd(), на котором ломалось поэлементное сравнение списка.
    zcode_needed = engine == "zcode"
    if eng_mod is None:
        # Движок не загрузился — zcode_cmd неоткуда взять: одна красная
        # строка вместо NameError посреди отчёта.
        checks.append((False, "zcode", f"движок не загружен: {checks_head}"))
    else:
        try:
            zcmd = eng_mod.zcode_cmd()
        except ValueError as e:
            # zcode_cmd отказывает понятно (например, бандлу не хватает
            # node в PATH): доктор называет причину, а не падает.
            checks.append(
                (False if zcode_needed else None, "zcode", f"zcode_cmd: {e}")
            )
            zcmd = None
        if zcmd is not None:
            zcode_missing = shutil.which(zcmd[0]) is None
            if zcode_missing:
                checks.append(
                    (
                        False if zcode_needed else None,
                        "zcode",
                        f"НЕ НАЙДЕН ({exec_hint})"
                        if zcode_needed
                        else f"не установлен (движок не выбран: {exec_hint})",
                    )
                )
            else:
                _probe_version(
                    checks, "zcode", [*zcmd, "version"], " ".join(zcmd),
                    zcode_needed,
                    stderr_ok=True,
                )
    if zcode_needed and model:
        # --model в CLI 0.16 нет: ключ конфига не имеет права молча
        # притвориться флагом. Доктор называет, куда модель реально идёт.
        checks.append(
            (
                None,
                "zcode-model",
                (
                    f"{model} — в метрике; CLI не принимает --model, "
                    "веса из ~/.zcode/cli/config.json"
                ),
            )
        )

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
        # Тот же стержень проб, что у движков: таймаут и обработка
        # запуска живут в _run_probe, а не третьей копией.
        ctags_out, ctags_err = _run_probe([ctags, "--version"])
        if ctags_out is None:
            checks.append((False, "ctags", ctags_err))
        else:
            ver = ctags_out.stdout
            if "Universal Ctags" in ver:
                lines = ver.strip().splitlines()
                checks.append((True, "ctags", lines[0] if lines else ctags))
            else:
                checks.append(
                    (
                        False,
                        "ctags",
                        (
                            "Exuberant/BSD — нужен universal-ctags "
                            "(brew unlink ctags && brew install "
                            "universal-ctags)"
                        ),
                    )
                )
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
        elif tree_sitter_clib():
            checks.append(
                (
                    None,
                    "tree-sitter",
                    (
                        "стоит только C-библиотека (brew), петле нужны "
                        f"python-биндинги: {ts_hint}"
                    ),
                )
            )
        else:
            checks.append(
                (None, "tree-sitter", f"не установлен (опционально): {ts_hint}")
            )

    # Память (E9) — опциональна: её отсутствие деградирует поиск уроков,
    # а не петлю. Доктор называет точные команды настройки, но не
    # выполняет их сам: базу и роль создаёт владелец.
    if shutil.which("psql"):
        mem_mod = cli.load_mod("memory")
        # cfg уже прочитан на входе доктора: повторное чтение — лишний
        # I/O и шанс поймать рассинхрон, если файл успели поправить.
        ok_pg, out_pg = mem_mod.pg(cfg, "SELECT version();")
        if ok_pg:
            checks.append(
                (True, "memory-pg", out_pg.split(" on ")[0][:40] or "доступен")
            )
        else:
            checks.append(
                (
                    None,
                    "memory-pg",
                    (
                        "недоступен (опционально): создать — "
                        "createdb swarm_memory; уроки при этом "
                        "копятся в файлах"
                    ),
                )
            )
    else:
        checks.append((None, "memory-pg", "psql не установлен (опционально)"))

    for ok, name, note in checks:
        mark = {True: "  ok ", False: "ПРОБЛ", None: " опц "}[ok]
        print(f"[{mark}] {name:12} {note}")

    print("\n=== репозиторий ===")
    # Тот же стержень проб, что у движков: повреждённый .git/index,
    # блокировка git-каталога или зависший маунт способны удержать
    # `git status` бесконечно — доктор без таймаута сам становился бы
    # зависшим инструментом, который диагностирует.
    dirty, git_err = _run_probe(["git", "status", "--porcelain"], cwd=root)
    if dirty is None:
        print(f"[ПРОБЛ] git-статус: {git_err}")
        checks.append((False, "git-репозиторий", git_err))
    elif dirty.returncode != 0:
        print("[ПРОБЛ] это не git-репозиторий")
        # Красная строка обязана влиять на код возврата: CI, опирающийся
        # на exit code, принял бы сломанный стенд за успех.
        checks.append((False, "git-репозиторий", "не git-репозиторий"))
    else:
        files = dirty.stdout.strip().splitlines()
        dirty_tree = bool(files)
        print(
            f"[{'  ok ' if not dirty_tree else 'ПРОБЛ'}] worktree: "
            f"{'чист' if not dirty_tree else f'{len(files)} изменённых файлов'}"
        )
        # Грязное дерево — красная строка не только в печати: контракт
        # cmd_doctor (см. docstring) требует парной записи в checks,
        # иначе exit code останется 0 на заведомо сломанном стенде.
        checks.append(
            (
                not dirty_tree,
                "worktree",
                "чист" if not dirty_tree else f"{len(files)} изменённых файлов",
            )
        )

    gate_cmd = cfg.get("gate_command")
    if gate_cmd is None:
        print("[ опц ] gate: по умолчанию (unittest)")
    elif isinstance(gate_cmd, list) and all(isinstance(x, str) for x in gate_cmd):
        print(f"[  ok ] gate: {' '.join(gate_cmd)}")
    else:
        # Ровно та ошибка, которую доктор обязан ловить вместо петли:
        # строкой этот ключ пишут чаще, чем списком, а падает он посреди
        # первой задачи и выглядит как авария задачи, а не как конфиг.
        # Кладём в checks, а не только в печать: иначе код возврата
        # скажет «успех» на заведомо битом gate.
        checks.append(
            (
                False,
                "gate",
                (
                    f"{gate_cmd!r} — нужен СПИСОК аргументов "
                    f'(["python3", "-m", "pytest"]), иначе петля ищет файл '
                    f"с таким именем"
                ),
            )
        )
        print(f"[ПРОБЛ] gate: {gate_cmd!r} — нужен СПИСОК аргументов")
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


def cmd_docmap(args: argparse.Namespace) -> int:
    """Карта «код → документы» (E5, вариант B): проверка и подозреваемые.

    Без --changed печатает предупреждения о состоянии самой карты
    (мёртвый glob, нет документа, нет якоря); с --changed — секции,
    подозреваемые на дрейф по перечисленным изменённым файлам. Детект
    механический: LLM здесь не зовётся вовсе.
    """
    docmap = cli.load_mod("docmap")
    root = pathlib.Path(args.root)
    map_path = root / docmap.MAP_NAME
    if not map_path.is_file():
        print(f"{map_path}: карты нет — детектор дрейфа не настроен")
        return 0
    try:
        entries = docmap.load(map_path)
    except (TypeError, ValueError, OSError) as e:
        print(f"docmap: {e}", file=sys.stderr)
        return 2
    if args.changed:
        hits = docmap.suspects(list(args.changed), entries)
        if not hits:
            print("подозреваемых секций нет")
            return 0
        for h in hits:
            print(f"{h.doc}  (код: {h.code}, правка: {h.file})")
        return 0
    warnings = docmap.check(root, entries)
    for w in warnings:
        print(f"[ПРЕД] docmap: {w}")
    if warnings:
        print(f"предупреждений: {len(warnings)}")
        return 1
    print(f"[  ok ] docmap: {len(entries)} записей, предупреждений нет")
    return 0


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
            print(
                f"\nподавлено находок за прогон: {total} (`swarm report` покажет какие)"
            )
        return 0
    if args.action == "add":
        if not args.text.strip():
            # nargs="?" с умолчанием "" пропускал пустую политику: она
            # ничего не выражает, но занимает pid и место в журнале.
            print(
                'нужен текст политики: policy add "текст" --match слово',
                file=sys.stderr,
            )
            return 2
        if not args.match:
            print('нужны ключевые слова: --match "release note"', file=sys.stderr)
            return 2
        pid = st.add_policy(args.text, args.match)
        print(f"политика {pid} добавлена: {args.text}")
        print(f"будет подавлять находки со словами: {', '.join(args.match)}")
        print(
            "ревьюер по-прежнему их сообщает — фильтрует оркестратор, "
            "подавленное видно в `swarm report`"
        )
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
            "planner", {"id": "*", "title": args.goal, "paths": []}, st, cfg
        )
        prompt = planner.plan_prompt(args.goal, tasks, files, suite, memory=mem_block)
    else:
        task = next((t for t in tasks if t["id"] == args.task), None)
        if task is None:
            print(f"задача {args.task!r} не найдена", file=sys.stderr)
            return 2
        dispute = {}
        if args.dispute:
            try:
                dispute = json.loads(pathlib.Path(args.dispute).read_text())
            except (OSError, ValueError) as e:
                # Путь ввода управляет оператор: битый (или пустой)
                # dispute — ошибка аргумента, а не авария петли. Код 2,
                # как у прочих отказов аргументов команды.
                print(f"не читается dispute {args.dispute}: {e}", file=sys.stderr)
                return 2
        # Та же память, что и у plan: спор о границах решается знанием
        # прошлых таких решений, а не заново с чистого листа.
        mem_block = cli.load_mod("memory").inject_block("planner", task, st, cfg)
        prompt = planner.replan_prompt(
            task, dispute, tasks, files, suite, memory=mem_block
        )
    # Потолок вызова планировщика — через общую политику денег, а не
    # чтением ключа: иначе `money_bin` действовал бы на две роли из
    # трёх, и режим врал бы своим названием.
    spending_mod = cli.load_mod("spending")
    diff, errs, reason = planner.plan_with_retry(
        prompt,
        args.cmd,
        tasks,
        root=args.root,
        budget=spending_mod.call_cap(cfg, "plan_budget_usd"),
        model=cfg.get("plan_model"),
        effort=cfg.get("plan_effort"),
        timeout=cfg.get("plan_timeout"),
    )
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
            print(f"         ⚠ граница: {w['file']} ({w['token']}) — {w['hint']}")
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
    st.log(
        "plan_applied",
        mode=args.cmd,
        ops=len(diff["ops"]),
        tasks_after=len(data["tasks"]),
    )
    print(f"\nприменено: в очереди {len(data['tasks'])} задач(и)")
    return 0
