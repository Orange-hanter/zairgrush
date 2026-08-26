"""Команды запуска петли: go, run, resume и их хелперы."""
import argparse
import json
import pathlib
import shutil
import subprocess
import sys
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cli  # noqa: E402
from cliexplain import _prefix  # noqa: E402

# Коды возврата `run`/`go` — машинный контракт для скрипта поверх петли.
# Продолжение EXIT_* из loop.py: те объявлены контрактом, но до кода
# процесса не доезжали, и `swarm go` возвращал 0 что на закрытой очереди,
# что на исчерпанном бюджете — снаружи «сделано» и «встало» совпадали.
# 2 (usage/отказ на входе), 3 (нет готовых задач), 4 (квота) заняты;
# 11 совпадает по смыслу с EXIT_ASK_USER петли. Таблица — в EPILOG.
EXIT_QUEUE_DONE = 0    # очередь отработана: все взятые задачи done

EXIT_NEEDS_HUMAN = 11  # есть исходы blocked/ask_user — нужен человек

EXIT_BUDGET = 12       # прогон остановлен по бюджету

EXIT_NO_EXECUTOR = 13  # исполнитель недоступен, прогон остановлен



def _run_verdict(results: dict[str, str]) -> int:
    """Код возврата прогона из исходов петли — один на `run` и `go`.

    Порядок веток: сперва причина остановки очереди (`_executor`,
    `_budget` объясняют, ПОЧЕМУ петля прервалась), затем состояние
    взятых задач.
    """
    if results.get("_executor") == "unavailable":
        return EXIT_NO_EXECUTOR
    if (results.get("_budget") == "exhausted"
            or "budget_stop" in results.values()):
        return EXIT_BUDGET
    if any(v in ("blocked", "ask_user") for k, v in results.items()
           if not k.startswith("_")):
        return EXIT_NEEDS_HUMAN
    return EXIT_QUEUE_DONE



def _print_results(results: dict[str, str]) -> None:
    """Итог прогона печатается целиком, включая ключи-события с «_».

    `go` фильтровал `_budget`/`_executor` и показывал `итог: {}` у
    прогона, вставшего по бюджету: причина остановки — не служебный шум,
    а главное в последней строке. `run` и `go` печатают одинаково.
    """
    cli.ui("\nитог: " + json.dumps(results, ensure_ascii=False))



def _engine_preflight(cfg: dict[str, Any]) -> bool:
    """Кем исполнять — сказать ДО первого потраченного доллара.

    Опечатка в имени движка не имеет права стать умолчанием: молча
    выбранный дефолт — тот же отказ, что дал «ноль вызовов эмбеддера за
    весь пилот». Здесь он стоит дороже: работа ушла бы не тому агенту,
    которого выбрал оператор, и плечо замера оказалось бы чужим.

    Заодно называется вторая цена выбора. Разделение ролей в §3.2
    опиралось и на НЕЗАВИСИМОСТЬ судьи: дифф пишет одна модель, судит
    другая. Исполнитель и ревьюер на одной и той же модели эту опору
    убирают — предупреждаем прямо, а не оставляем это знанием автора.
    """
    # Режим денег проверяется здесь же и первым: незнакомое значение
    # роняло бы каждый вызов роли по отдельности, посреди работы, вместо
    # одного отказа на старте.
    try:
        cli.load_mod("spending").mode(cfg)
    except ValueError as e:
        print(f"режим траты не выбран: {e}", file=sys.stderr)
        return False
    # Фоновый замер: незнакомый фактор и конфликт с явным флагом —
    # отказ ДО работы. Испорченную выборку не видно ни в одном выводе,
    # и заметить её можно было бы только по несходящимся числам через
    # недели обычных прогонов.
    ambient = cli.load_mod("ambient")
    try:
        clash = ambient.conflict(cfg)
    except ValueError as e:
        print(f"фоновый замер не настроен: {e}", file=sys.stderr)
        return False
    if clash:
        print(f"фоновый замер противоречит конфигу: {clash}", file=sys.stderr)
        return False
    factor = ambient.factor(cfg)
    if factor:
        cli.ui(f"фоновый замер: фактор {factor}, "
               f"сид {(cfg.get('experiments') or {}).get('ambient_seed')}")
    # Дуэль: два исполнителя на задачу. Проверки те же и по той же
    # причине — испорченную выборку не видно ни в одном выводе.
    duel = cli.load_mod("duel")
    try:
        clash = duel.conflict(cfg)
    except ValueError as e:
        print(f"дуэль не настроена: {e}", file=sys.stderr)
        return False
    if clash:
        print(f"дуэль противоречит конфигу: {clash}", file=sys.stderr)
        return False
    duel_factor = duel.factor(cfg)
    if duel_factor:
        cli.ui(f"ДУЭЛЬ: фактор {duel_factor} — на каждой задаче два "
               f"исполнителя параллельно; работа остаётся у плеча, "
               f"выбранного жребием ЗАРАНЕЕ")
    engines = cli.load_mod("engines")
    try:
        engine, model = engines.resolve(cfg)
    except ValueError as e:
        print(f"движок исполнителя не выбран: {e}", file=sys.stderr)
        return False
    cli.ui(f"исполнитель: {engine}" + (f" ({model})" if model else ""))
    same_model = engine == "claude" and str(model) == str(
        cfg.get("review_model") or "")
    # Подтверждающий раунд ревьюит ТОТ ЖЕ дифф. Если он не разведён ни
    # моделью, ни усилием, ни линзой, это не второй ВЗГЛЯД, а второй раз
    # тот же вопрос: §8.2 обещает угол зрения, а конфиг по умолчанию
    # оплачивает повтор. Молчать об этом нельзя — обещание документа и
    # поведение прогона расходятся именно здесь.
    diverged = any(cfg.get(k) for k in
                   ("confirm_model", "confirm_model_pool", "confirm_effort",
                    "confirm_effort_pool", "confirm_lens"))
    if int(cfg.get("confirmations", 2) or 0) > 1 and not diverged:
        cli.ui("    ВНИМАНИЕ: подтверждающий раунд ничем не разведён — тот "
               "же дифф тем же ревьюером с теми же параметрами; это второй "
               "образец, а не второй взгляд. Развести: confirm_model, "
               "confirm_effort или confirm_lens")
    if same_model:
        cli.ui("    ВНИМАНИЕ: исполнитель и ревьюер — одна и та же модель; "
               "независимость судьи (§3.2) держится только на "
               "confirm_model/confirm_effort/confirm_lens")
    return True


def cmd_go(args: argparse.Namespace) -> int:
    """От А до Я: рой сам декомпозирует цель и сам её исполняет.

    Разница с `plan` + `run` не в удобстве: план должен рождаться ВНУТРИ
    роя, а не приноситься снаружи. Иначе декомпозиция — работа человека
    (или другой системы), и самодостаточности нет.

    Останавливается только на том, что действительно требует человека:
    вопрос о замысле, исчерпанный бюджет, авария.
    """
    cfg = cli.load_config(args.root)
    if not _engine_preflight(cfg):
        return 2
    st = cli.state_mod.SwarmState(args.root)
    if not _preflight(st, getattr(args, "force", False)):
        return 2

    data = st.load_tasks()
    existing = data.get("tasks", [])
    pending = [t for t in existing if t.get("status") == "pending"]
    stored_goal = str(data.get("goal") or "")
    if args.goal and pending and args.goal != stored_goal:
        # Очередь не пуста — планирование пропустится, и переданная цель
        # НИКУДА не записалась бы: status и доска продолжали бы показывать
        # старую, а policies() фильтруют решения человека по цели — под
        # чужой вывеской они молча теряют силу. Честнее отказаться, чем
        # молча продолжить не под тем флагом.
        print("цель очереди и переданная --goal расходятся:", file=sys.stderr)
        print(f"  в очереди: {stored_goal or '(не задана)'}", file=sys.stderr)
        print(f"  передана:  {args.goal}", file=sys.stderr)
        print("продолжить старую очередь — `swarm go` без --goal; "
              'новая цель — `swarm plan --goal "…"`, затем `swarm go`',
              file=sys.stderr)
        return 2
    if args.goal and not pending:
        print(f"== планирование: {args.goal}\n")
        rc = cli.cmd_plan(argparse.Namespace(
            root=args.root, cmd="plan", goal=args.goal, task=None,
            dispute=None, dry_run=False))
        if rc != 0:
            return int(rc)
        print()
    elif pending:
        print(f"в очереди уже {len(pending)} задач(и) — планирование пропущено\n")

    # Печатается ВСЕГДА, а не только когда потолок задан: режим без
    # потолков обязан назвать себя вслух. Тихое «без ограничений» — это
    # то, как получают неожиданный счёт, и владелец имеет право узнать о
    # политике денег до старта, а не из счёта провайдера.
    spending = cli.load_mod("spending")
    print(spending.headline(cfg, st.total_spend()) + "\n")

    out, board_url = _board_open(args.root, cfg)
    cli.ui(f"доска: {board_url}" if board_url else f"доска: file://{out.resolve()}")

    with cli.state_mod.SwarmState(args.root) as locked:
        agents = cli.load_mod("agents").Agents(locked, cfg)
        loop = cli.loop_mod.Loop(locked, cfg, agents,
                                                  ui=cli.ui)
        results = loop.run(limit=args.limit)

    board_mod = cli.load_mod("board")
    out, board = board_mod.build(args.root)
    open_q = [q for q in board["questions"] if q["status"] == "open"]
    _print_results(results)
    run_cap = spending.run_budget(cfg)
    print(f"потрачено ${board['total']}"
          + (f" из ${run_cap}" if run_cap else " (потолка прогона нет)"))
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
    _board_close()
    return _run_verdict(results)



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
    _log_agent_versions(st)
    return True


def _log_agent_versions(st: Any) -> None:
    """Версии обоих CLI — в журнал прогона, рядом со `swarm_sha`.

    Отпечаток КОДА ПЕТЛИ в журнале есть с самого начала, а версии агентов,
    которыми петля работает, не записывались нигде. При этом петля
    разбирает их поверхности: `--json-schema`, форму конвертов, тексты
    сообщений о квоте. Смена мажорного поведения агента выглядела бы как
    поломка роя, и сравнить прогоны было бы не по чему — воспроизводимость
    держалась на удаче.

    `doctor` версии УЖЕ спрашивает, но только показывает их человеку и
    только когда его позвали. Здесь они попадают в журнал каждого прогона,
    то есть в те же данные, по которым потом считают замеры.

    Граница деградации: отсутствующий или молчащий CLI не отменяет прогона
    (движок мог быть и не выбран) — в журнал уезжает None.
    """
    versions: dict[str, str | None] = {}
    for name in ("kimi", "claude"):
        exe = shutil.which(name)
        if not exe:
            versions[name] = None
            continue
        try:
            out = subprocess.run([name, "--version"], capture_output=True,
                                 text=True, timeout=30,
                                 check=False).stdout.strip().splitlines()
            versions[name] = out[0][:120] if out else None
        except (OSError, subprocess.SubprocessError):
            versions[name] = None
    st.log("agent_versions", **versions)



# Один сервер на процесс, а не на вызов: `_board_open` дёргается один раз
# за `run`/`go`, но модульная переменная — единственное, что удерживает
# объект живым (без ссылки поток serve_forever пережил бы сборщик мусора
# только по счастливой случайности) и даёт тестам за что остановить его
# явно, не дожидаясь конца процесса.
_BOARD_SERVER: Any = None



def _board_open(root: str | pathlib.Path,
                cfg: dict[str, Any]) -> tuple[pathlib.Path, str | None]:
    """Доска открывается сама при старте прогона — правило оператора:
    открывать, пока не отключили явно (`board_open = false`).

    Путь к статическому файлу возвращается ВСЕГДА, даже когда живой
    сервер не поднялся: заголовку прогона он нужен независимо от того,
    состоялась ли живая доска, а сам файл тут же перепишет первый вызов
    `Loop.run()` — путь верен и до первой настоящей сборки. Адрес живой
    доски — второй элемент пары, `None`, если сервер не запущен или
    выключен конфигом.

    Живой сервер и статическая сборка обёрнуты в РАЗНЫЕ try/except:
    провал сервера не должен лишать человека хотя бы файла, и наоборот.
    Наблюдение — не работа (правило доски refresh_board), и ни один из
    двух сбоев не имеет права остановить прогон — только диагностика.
    """
    global _BOARD_SERVER  # noqa: PLW0603 — один сервер на процесс, см. докстринг переменной
    out = pathlib.Path(root) / ".swarm" / "board.html"
    if cfg.get("live_board") is False or cfg.get("board_open") is False:
        return out, None
    url: str | None = None
    try:
        if _BOARD_SERVER is None:
            _BOARD_SERVER = cli.load_mod("boardserve").BoardServer(
                root, port=cfg.get("board_port", 7433))
            _BOARD_SERVER.start()
        url = _BOARD_SERVER.url
    except Exception:  # noqa: BLE001 — см. докстринг: сбой доски не останавливает прогон
        cli.log.warning(
            "живая доска не поднялась — открою статический файл", exc_info=True)
    try:
        board_mod = cli.load_mod("board")
        out, _board = board_mod.build(root)
        if sys.platform == "darwin":
            subprocess.run(["open", url or str(out)], check=False)
    except Exception:  # noqa: BLE001 — см. докстринг: сбой открытия не останавливает прогон
        cli.log.warning(
            "доска не открылась автоматически", exc_info=True)
    return out, url



def _board_close() -> None:
    """Остановить живой сервер доски по окончании команды.

    Поток сервера — daemon, и в продакшене он умрёт вместе с процессом.
    Но в тестах процесс живёт дольше одной команды: неостановленный
    сервер мог дотянуться до временного каталога уже после tearDown
    (обработчик HTTP вызывает `board.collect`, а тот создаёт `SwarmState`
    и, следовательно, каталог `.swarm/log`). Явная остановка убирает эту
    гонку, не меняя поведения для пользователя: доска всё так же
    поднимается в начале прогона и обновляется, пока идёт петля.
    """
    global _BOARD_SERVER  # noqa: PLW0603 — сброс синглтона, см. докстринг переменной
    if _BOARD_SERVER is not None:
        _BOARD_SERVER.stop()
        _BOARD_SERVER = None



def cmd_run(args: argparse.Namespace) -> int:
    cfg = cli.load_config(args.root)
    if not _engine_preflight(cfg):
        return 2
    with cli.state_mod.SwarmState(args.root) as st:
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
            ok, _tail = cli.loop_mod.Loop(st, cfg, None).gate(ready[0])
            print(f"  baseline gate: {'зелёный' if ok else 'КРАСНЫЙ'}")
            return 0
        out, board_url = _board_open(args.root, cfg)
        cli.ui(f"доска: {board_url}" if board_url else f"доска: file://{out.resolve()}")
        agents = cli.load_mod("agents").Agents(st, cfg)
        loop = cli.loop_mod.Loop(st, cfg, agents,
                                                  ui=cli.ui)
        results = loop.run(limit=args.limit)
        _print_results(results)
    _board_close()
    return _run_verdict(results)



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
    st = cli.state_mod.SwarmState(args.root)
    policies = st.policies()
    if policies:
        print(f"\nполитики прогона ({len(policies)}):")
        for p in policies:
            print(f"  {p['pid']}  {p['text'][:70]}")

    open_q = st.questions(blocking=True)
    if open_q:
        print(f"\nВОПРОСЫ К ЧЕЛОВЕКУ ({len(open_q)}):")
        for q in open_q:
            print(f"  {q['qid']}  {q['task']}  {q['question'][:70]}")
        print("  -> `swarm inbox` покажет детали")
    # Вопросы закрытых задач очередь не держат, но и не исчезают: они
    # остаются записью о случившемся (E13). Отдельным списком, чтобы
    # «вас ждут N вопросов» относилось только к тем, что и правда ждут.
    stale_q = [q for q in st.questions(only_open=True) if q.get("stale")]
    if stale_q:
        print(f"\nустаревшие вопросы ({len(stale_q)}) — их задачи уже "
              f"закрыты, очередь они не держат:")
        for q in stale_q:
            print(f"  {q['qid']}  {q['task']}  {q['question'][:70]}")

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
    return int(cli.cmd_run(args))

