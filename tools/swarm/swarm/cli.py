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
import importlib.util
import inspect
import pathlib
import sys
import tomllib
from types import ModuleType
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent


def load_mod(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


state_mod = load_mod("state")
loop_mod = load_mod("loop")
log = load_mod("obs").get_logger("cli")


# Все ключи, которые петля где-либо читает. Список закрытый намеренно:
# опечатка в имени ключа (`max_iteration` без s) молча включала умолчание,
# и оператор был уверен, что его настройка действует.
KNOWN_CONFIG_KEYS = frozenset({
    "gate_command", "protected_paths", "max_iterations", "confirmations",
    "gate_timeout", "silence_timeout", "wall_clock_cap", "executor_model",
    "executor_engine", "executor_effort", "executor_budget_usd",
    "review_budget_usd", "verification", "total_budget_usd", "live_board",
    "board_open", "board_port", "map_budget", "tuning_seed", "quota_backoff_s",
    "quota_resume", "quota_resume_max", "quota_resume_max_wait_s",
    "max_futile_rounds",
    "quota_resume_fallback_s",
    "plan_budget_usd", "plan_model", "plan_effort", "plan_timeout",
    "review_model", "review_effort", "review_model_pool", "review_effort_pool",
    "confirm_model", "confirm_effort", "confirm_model_pool",
    "confirm_effort_pool", "confirm_lens",
    "memory_db", "memory_budget_chars", "memory_top_k", "memory_embed_model",
    "memory_index",
    "doc_context_budget_tokens",
    "doc_context_paths",
    "fill_num_predict",
    "spending", "unclear_model",
    "experiments",
})

# Режимы траты денег (см. spending.MODES). Продублировано строкой по той
# же причине, что и список движков: cli грузится раньше плоских модулей.
SPENDING_MODES = frozenset({"money_bin", "capped"})

# Экспериментальные флаги (06-док, §1): та же семантика, что у основного
# списка, — опечатка в имени флага молча включала бы умолчание.
KNOWN_EXPERIMENT_KEYS = frozenset({"memory", "memory_llm_consolidation",
                                   "skeleton", "tester",
                                   "ambient", "ambient_seed", "duel",
                                   "unclear", "doc_context"})

# Значения флага `[experiments] memory` — это имена ролей (см.
# memory.enabled_for): каждая включается отдельно, чтобы замер шёл по
# одному фактору за прогон, и "all" существует только для полного
# включения после того, как роли замерены поодиночке.
MEMORY_MODES = frozenset({"off", "executor", "reviewer", "planner", "all"})

# Значения флага `[experiments] doc_context` — роли, которым подмешивается
# блок документов из cod-doc (RFC 22 §3.4, E5-C).
DOC_CONTEXT_MODES = frozenset({"off", "executor", "reviewer", "all"})

# Значение `executor_engine` — имя движка (см. engines.ENGINES). Тот же
# довод, что у режимов памяти, но цена ошибки выше: опечатка здесь молча
# отправляла бы работу не тому агенту, которого выбрал оператор, — и
# «плечо A» замера оказалось бы плечом B. Список продублирован строкой,
# а не импортом: cli грузится раньше плоских модулей петли, а расхождение
# двух списков ловится тестом.
EXECUTOR_ENGINES = frozenset({"kimi", "claude", "ollama"})


def load_config(root: str | pathlib.Path) -> dict[str, Any]:
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
            exp = parsed.get("experiments")
            if isinstance(exp, dict):
                unknown_exp = sorted(set(exp) - KNOWN_EXPERIMENT_KEYS)
                if unknown_exp:
                    print(f"ВНИМАНИЕ: {path}: незнакомые флаги "
                          f"[experiments] ({', '.join(unknown_exp)}) — "
                          f"петля их не читает", file=sys.stderr)
                # Значение флага памяти — имя РОЛИ, и опечатка в нём
                # молча выключает подсистему целиком: `memory = "planer"`
                # неотличим от `"off"` ни в одном выводе. Тот же довод,
                # что и у закрытого списка ключей.
                mem_mode = exp.get("memory")
                if mem_mode is not None and mem_mode not in MEMORY_MODES:
                    print(f"ВНИМАНИЕ: {path}: [experiments] memory = "
                          f"{mem_mode!r} — не роль; память ВЫКЛЮЧЕНА. "
                          f"Допустимо: {', '.join(sorted(MEMORY_MODES))}",
                          file=sys.stderr)
                doc_mode = exp.get("doc_context")
                if doc_mode is not None and doc_mode not in DOC_CONTEXT_MODES:
                    print(f"ВНИМАНИЕ: {path}: [experiments] doc_context = "
                          f"{doc_mode!r} — не роль; doc_context ВЫКЛЮЧЕН. "
                          f"Допустимо: {', '.join(sorted(DOC_CONTEXT_MODES))}",
                          file=sys.stderr)
            gate_cmd = parsed.get("gate_command")
            if gate_cmd is not None and not (
                    isinstance(gate_cmd, list)
                    and all(isinstance(x, str) for x in gate_cmd)):
                # Строкой этот ключ выглядит естественнее всего, и именно
                # так его пишут. Петля же передаёт его в exec без шелла:
                # строка становится ИМЕНЕМ файла, и прогон падает
                # FileNotFoundError посреди первой задачи, объявив её
                # аварийной. Ошибка конфига обязана называться до старта.
                print(f"ВНИМАНИЕ: {path}: gate_command обязан быть списком "
                      f"аргументов, а не строкой — иначе петля ищет файл с "
                      f'таким именем. Пример: ["python3", "-m", "pytest"]',
                      file=sys.stderr)
            spend = parsed.get("spending")
            if spend is not None and spend not in SPENDING_MODES:
                # Опечатка в режиме денег не имеет права молча включить
                # противоположную политику: «capped» и «money_bin»
                # отличаются наличием потолка у самой дорогой роли.
                print(f"ВНИМАНИЕ: {path}: spending = {spend!r} — неизвестный "
                      f"режим траты; прогон откажется стартовать. "
                      f"Допустимо: {', '.join(sorted(SPENDING_MODES))}",
                      file=sys.stderr)
            engine = parsed.get("executor_engine")
            if engine is not None and engine not in EXECUTOR_ENGINES:
                # Предупреждение здесь, отказ — на старте прогона
                # (`clirun._engine_preflight`): читателем конфига
                # пользуются и команды, которым исполнитель не нужен
                # (`status`, `report`), и ронять их незачем.
                print(f"ВНИМАНИЕ: {path}: executor_engine = {engine!r} — "
                      f"не движок. Допустимо: "
                      f"{', '.join(sorted(EXECUTOR_ENGINES))}",
                      file=sys.stderr)
            cfg.update(parsed)
    return cfg


# --- команды -------------------------------------------------------------

def ui(*args: object) -> None:
    """Печать хода петли с немедленным сбросом буфера.

    Голый `print` буферизуется поблочно, когда stdout не терминал. Прогон
    в фоне (`swarm go > run.log`) писал в файл НОЛЬ БАЙТ пятьдесят минут,
    хотя петля исправно печатала каждый раунд: всё лежало в буфере до
    конца процесса. Инструмент, ценность которого в наблюдаемости хода,
    обязан быть виден и когда его вывод перенаправлен.
    """
    print(*args, flush=True)

# Тесты перезагружают cli.py через importlib.util; без принудительной
# перезагрузки плоских модулей они остались бы привязаны к предыдущей
# копии cli, и патчи `cli.load_mod` / `cli.loop_mod` новой копии не брались бы.
for _cli_mod in ("cliexplain", "cliinbox", "climemory", "climisc",
                 "clireport", "clirun"):
    sys.modules.pop(_cli_mod, None)

from cliexplain import (  # noqa: E402,F401
    WHY_ANSWER,
    WHY_EYES,
    WHY_SPLIT,
    _explain,
    _next_steps,
    _prefix,
    _print_next,
    _read_trend,
    _round_label,
    cmd_retry,
    cmd_status,
    cmd_why,
)
from cliinbox import _paths_mentioned, cmd_answer, cmd_inbox  # noqa: E402,F401
from climemory import _memory_anchor, cmd_memory  # noqa: E402,F401
from climisc import (  # noqa: E402
    cmd_doctor,
    cmd_plan,
    cmd_policy,
    tree_sitter_clib,
)
from clireport import (  # noqa: E402
    cmd_ab,
    cmd_board,
    cmd_impact,
    cmd_map,
    cmd_report,
)
from clirun import (  # noqa: E402,F401
    _BOARD_SERVER,
    EXIT_BUDGET,
    EXIT_NEEDS_HUMAN,
    EXIT_NO_EXECUTOR,
    EXIT_QUEUE_DONE,
    ORCHESTRATOR_EMAIL,
    _board_open,
    _preflight,
    _print_results,
    _reconcile_commit_step,
    _reconcile_decision,
    _run_verdict,
    cmd_go,
    cmd_resume,
    cmd_run,
)

# Явный re-export: подмодули cli обращаются к этим именам через `cli.X`.
__all__ = [
    "cmd_ab",
    "cmd_answer",
    "cmd_board",
    "cmd_doctor",
    "cmd_go",
    "cmd_impact",
    "cmd_inbox",
    "cmd_map",
    "cmd_memory",
    "cmd_plan",
    "cmd_policy",
    "cmd_report",
    "cmd_resume",
    "cmd_run",
    "importlib",
    "tree_sitter_clib",
]

EPILOG = """
порядок применения (первый прогон):
  doctor                      проверить среду — тридцать секунд здесь
                              экономят час диагностики потом
  go --goal "цель"            от А до Я: рой сам планирует и сам исполняет
                              (живая доска откроется сама, адрес — в шапке;
                              страница обновляется на месте, без перезагрузок)
  inbox -> answer <id> "…"    разобрать вопросы, которые петля отложила
  go                          продолжить с того же места

когда что-то пошло не так:
  why [задача]                почему встала и что нажать дальше
  report --task <id>          хроника задачи связным текстом
  report --json               то же сырьём, без обрезки
  retry <задача> --note "…"   вернуть в очередь с указанием
  board --serve               живая доска вне прогона (снимок: board)

память между прогонами (E9, инъекция за флагом [experiments]):
  memory search "…"           уроки прошлых прогонов (FTS + вектор)
  memory add "…" --anchor п   урок вручную; useful требует живой якорь
  memory sync                 досыпать PG-индекс до файлов (строки+вектора);
                              memory_index = "auto" делает это автоматически
                              на каждом терминальном исходе и в рефлексии

тонкая настройка — swarm.toml в корне репозитория: гейт, бюджеты, пулы
моделей ревьюера, память E9, эксперименты ([experiments] memory|skeleton),
confirm_lens, board_open/board_port. Опечатку в имени ключа петля назовёт
при старте; примеры значений — в руководстве оператора (08-док).

коды возврата run/go — машинный контракт для скрипта поверх петли:
  0   очередь отработана: все взятые задачи done
  2   ошибка использования или отказ на входе (preflight, границы, цель)
  3   нет задач, готовых к запуску (run)
  4   пауза по квоте провайдера — повторить, когда квота отпустит
  11  есть исходы blocked/ask_user — нужен человек: inbox -> answer, why
  12  прогон остановлен по бюджету
  13  исполнитель недоступен — прогон остановлен

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
    p.add_argument("--limit", type=int,
                   help="взять из очереди не больше N задач")
    p.add_argument("--dry-run", action="store_true",
                   help="показать план без вызова агентов")
    p.add_argument("--force", action="store_true",
                   help="запуститься на грязном дереве (риск потери работы)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", help="продолжить после падения")
    p.add_argument("--limit", type=int,
                   help="взять из очереди не больше N задач")
    p.add_argument("--dry-run", action="store_true",
                   help="показать решения реконсиляции, ничего не меняя")
    p.add_argument("--force", action="store_true",
                   help="продолжить, несмотря на незавершённый шаг")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("plan", help="декомпозиция цели в задачи")
    p.add_argument("--goal", required=True, help="цель прогона одной фразой")
    p.add_argument("--dry-run", action="store_true",
                   help="показать план-дифф, не применяя его к очереди")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("replan", help="пересмотр плана по спору исполнителя")
    p.add_argument("task", help="id задачи, вокруг которой спор")
    p.add_argument("--dispute", help="файл с dispute исполнителя")
    p.add_argument("--dry-run", action="store_true",
                   help="показать план-дифф, не применяя его к очереди")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("go", help="от А до Я: рой планирует сам и исполняет")
    p.add_argument("--goal", help="цель; без неё берётся существующая очередь")
    p.add_argument("--limit", type=int,
                   help="взять из очереди не больше N задач")
    p.add_argument("--force", action="store_true",
                   help="запуститься на грязном дереве (риск потери работы)")
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("board", help="доска прогона одной страницей")
    p.add_argument("--out", help="куда писать (по умолчанию .swarm/board.html)")
    p.add_argument("--open", action="store_true", help="открыть в браузере")
    p.add_argument("--serve", action="store_true",
                   help="живой сервер доски на переднем плане (Ctrl+C — "
                        "остановить); с --open открывает адрес, а не файл")
    p.add_argument("--port", type=int, default=None,
                   help="порт живого сервера (по умолчанию board_port из "
                        "swarm.toml или 7433)")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("status", help="состояние очереди")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="проверка окружения")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("map", help="карта символов репозитория")
    p.add_argument("--budget", type=int, default=25,
                   help="сколько символов включить в карту")
    p.add_argument("--tree-sitter", action="store_true",
                   help="добавить слой tree-sitter (Rust и другие языки "
                        "без точного разрешателя; нужны python-биндинги, "
                        "см. doctor)")
    p.add_argument("--verbose", action="store_true",
                   help="печатать источники слоёв и сырой JSON карты")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("impact", help="кто вызывает символ")
    p.add_argument("symbol", help="имя функции/класса или файл::имя")
    p.add_argument("--tree-sitter", action="store_true",
                   help="добавить слой tree-sitter (см. map)")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("inbox", help="вопросы к человеку")
    p.add_argument("--all", action="store_true", help="включая отвеченные")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("answer", help="ответить на вопрос петли")
    p.add_argument("qid", help="id вопроса из inbox (например q015)")
    p.add_argument("text",
                   help="текст решения — попадёт исполнителю в новый раунд")
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
    p.add_argument("task", help="id заблокированной задачи")
    p.add_argument("--note", help="указание исполнителю")
    p.add_argument("--add-path", action="append", default=[],
                   help="расширить границы задачи (можно повторять)")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("memory", help="память между прогонами (E9)")
    mem_sub = p.add_subparsers(dest="mem_cmd", required=True)
    mp = mem_sub.add_parser("add", help="записать урок вручную")
    mp.add_argument("text", help="текст урока (до 700 символов)")
    mp.add_argument("--outcome", choices=["useful", "dead_end", "corrected"],
                    default="useful",
                    help="класс урока: полезный / тупик / исправленное "
                         "заблуждение (по умолчанию useful)")
    mp.add_argument("--anchor", action="append", default=[],
                    help="якорь: путь, коммит или id задачи (можно повторять)")
    mp = mem_sub.add_parser("search", help="поиск по урокам")
    mp.add_argument("query",
                    help="запрос: FTS первым, вектор сетью охвата")
    mp.add_argument("-k", type=int, default=5,
                    help="сколько уроков вернуть")
    mp.add_argument("--json", action="store_true",
                    help="сырые записи вместо прозы")
    mp = mem_sub.add_parser("show", help="урок целиком по id")
    mp.add_argument("id", help="id урока (печатает search)")
    mp = mem_sub.add_parser("forget", help="затомбстоунить урок")
    mp.add_argument("id", help="id урока (печатает search)")
    mem_sub.add_parser("reflect", help="пересобрать дайджест LESSONS.md")
    mem_sub.add_parser("reindex", help="пересобрать PG-индекс из файлов")
    mem_sub.add_parser("sync", help="досыпать индекс до файлов: строки и "
                                    "вектора (инкрементально, без DELETE)")
    p.set_defaults(func=cmd_memory)

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
    load_mod("obs").setup(pathlib.Path(args.root) / ".swarm")
    try:
        code: int = args.func(args)
    except state_mod.StateError as e:
        print(f"состояние: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        # По имени, не по классу: класс QuotaExceededError теперь единый
        # в verdicts.py, но модули петли всё ещё грузятся по путям
        # (importlib), а исключение может прийти откуда угодно. Детектор
        # quota_exception сверяет имя класса — это стабильный контракт
        # независимо от копии модуля (см. quota_exception).
        if loop_mod.quota_exception(e):
            print(f"пауза по квоте провайдера: {e}", file=sys.stderr)
            return 4
        raise
    else:
        return code



if __name__ == "__main__":
    sys.exit(main())
