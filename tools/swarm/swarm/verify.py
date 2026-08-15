#!/usr/bin/env python3
"""Этап 2: verification-запросы ревьюера (предложение №21 аудита).

Мотив измерен, а не придуман: в BENCH-1 ревьюер в 4 задачах из 6 писал в
out_of_scope_notes «собственный прогон выполнить не удалось — Bash
отклонён», а в CANARY-1 — «полный перебор против независимой реализации
выполнить не удалось». Он хочет проверять находки исполнением, но по
дизайну (§6.0) остаётся read-only. Литература на стороне ревьюера: критик
силён внешними инструментами, а не мнением (CRITIC, arXiv:2305.11738), и
ревьюер Codex запускает код для проверки своих находок.

Решение: ревьюер не получает Bash. Он возвращает СПИСОК ЗАПРОСОВ на
проверку, оркестратор исполняет только то, что разрешено whitelist'ом, и
делает второй вызов с результатами. Так read-only-контракт сохраняется:
исполняет по-прежнему оркестратор, а не агент.

Безопасность: команда описывается структурно (kind + аргументы), а не
строкой шелла; shell=True не используется никогда; whitelist —
положительный список, а не запрет опасного.
"""
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

MAX_REQUESTS = 4          # больше — это уже не проверка, а исследование
MAX_ROUNDS = 1            # один раунд верификации на итерацию (анти-петля)
CMD_TIMEOUT = 120


class RejectedError(Exception):
    """Запрос не прошёл whitelist. Не ошибка петли — повод сообщить агенту."""


# Whitelist: kind -> (builder, валидатор аргумента).
# Аргументы валидируются по «форме», а не по чёрному списку символов.
_TEST_TARGET = re.compile(r"^[A-Za-z0-9_.]+$")          # tests.test_roman
_GIT_REF = re.compile(r"^[A-Za-z0-9_./~^-]{1,64}$")     # HEAD~2, a1b2c3d
_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,120}$")         # wordstat/rank.py


def _safe_path(arg: str | None) -> bool:
    """Путь обязан оставаться внутри репозитория: без `..` и без ведущего
    слэша. Поймано собственным тестом — исходный шаблон пропускал
    `../../../etc/passwd`, потому что точка и слэш в нём разрешены."""
    if not arg or not _PATH.match(arg):
        return False
    return not (arg.startswith("/") or ".." in arg.split("/"))


def _as_test_module(arg: str | None) -> str:
    """Ревьюер одинаково охотно пишет и `tests.test_x`, и `tests/test_x.py`
    (замерено на VERIFY-1). Принимаем обе формы: отклонять из-за записи —
    значит тратить запрос впустую и терять проверку."""
    a = (arg or "").strip()
    if a.endswith(".py"):
        # Путь сначала проверяется КАК ПУТЬ: иначе `../../etc/passwd.py`
        # превратится в `...etc.passwd` и проскочит проверку traversal —
        # поймано собственным тестом сразу после добавления этого удобства.
        if not _safe_path(a):
            raise RejectedError(f"недопустимый путь к тестам: {arg!r}")
        a = a[:-3].replace("/", ".").replace("\\", ".")
    return a


def _unittest(arg: str | None) -> list[str]:
    target = _as_test_module(arg)
    if not _TEST_TARGET.match(target):
        raise RejectedError(f"недопустимая цель тестов: {arg!r}")
    return ["python3", "-m", "unittest", target]


def _unittest_all(_arg: str | None) -> list[str]:
    return ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."]


def _git_show(arg: str | None) -> list[str]:
    """Поддержаны обе формы: `HEAD~2` (тогда --stat) и `HEAD:path/file.py`
    (показать файл на коммите) — вторую ревьюер просит чаще, чтобы сравнить
    новую версию файла со старой."""
    a = (arg or "").strip()
    if ":" in a:
        ref, _, path = a.partition(":")
        if not _GIT_REF.match(ref) or not _safe_path(path):
            raise RejectedError(f"недопустимая ссылка git: {arg!r}")
        return ["git", "show", f"{ref}:{path}"]
    if not _GIT_REF.match(a):
        raise RejectedError(f"недопустимая ссылка git: {arg!r}")
    return ["git", "show", "--stat", a]


def _git_log(arg: str | None) -> list[str]:
    if arg and not _safe_path(arg):
        raise RejectedError(f"недопустимый путь: {arg!r}")
    return ["git", "log", "--oneline", "-10"] + ([arg] if arg else [])


def _python_snippet(arg: str | None) -> list[str]:
    """Проверка гипотезы кодом. Самый мощный и самый опасный вид запроса,
    поэтому ограничен жёстче остальных: без импорта os/sys/subprocess,
    без файловых операций, короткий, с таймаутом."""
    src = arg or ""
    if len(src) > 600:
        raise RejectedError("сниппет длиннее 600 символов")
    forbidden = ("import os", "import sys", "import subprocess", "import shutil",
                 "import socket", "open(", "__import__", "eval(", "exec(",
                 "input(", "compile(")
    for bad in forbidden:
        if bad in src:
            raise RejectedError(f"запрещённая конструкция в сниппете: {bad}")
    return ["python3", "-c", src]


# Ключи обязаны совпадать с enum `verification_requests.kind` в
# schemas/verdict-v1.schema.json: схема — это то, что обещано ревьюеру.
# Рассинхрон уже стоил живого прогона: схема давала `unittest`, whitelist
# ждал `run_tests`, и самые полезные проверки молча отклонялись
# (на пилоте — 2 из 3 запросов). Старые имена оставлены псевдонимами,
# чтобы вердикты прошлых прогонов не ломались.
WHITELIST: dict[str, Callable[[str | None], list[str]]] = {
    "unittest": _unittest,
    "unittest_all": _unittest_all,
    "git_show": _git_show,
    "git_log": _git_log,
    "python": _python_snippet,
    "run_tests": _unittest,            # псевдоним прежней схемы
    "run_all_tests": _unittest_all,    # псевдоним прежней схемы
}


def build(request: Any) -> list[str]:
    """Запрос ревьюера -> argv. Бросает Rejected, если запрос не разрешён."""
    if not isinstance(request, dict):
        raise RejectedError("запрос не объект")
    kind = request.get("kind")
    if kind not in WHITELIST:
        raise RejectedError(f"неизвестный вид проверки: {kind!r}; "
                       f"разрешены {sorted(WHITELIST)}")
    return WHITELIST[kind](request.get("arg"))


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_output(text: str | None) -> str:
    """Вывод исполненной команды идёт прямо в промпт следующего вызова.

    Найдено на VERIFY-1: ревьюер попросил напечатать символы по диапазону
    кодов, вывод содержал \\x00, и формирование команды упало с
    `ValueError: embedded null byte`. Управляющие байты вычищаются, длина
    ограничивается — это граница между «исполнили» и «показали модели».
    """
    if not text:
        return ""
    return _CTRL.sub("�", text)


def run_requests(requests: list[dict[str, Any]] | None, cwd: str,
                 max_requests: int = MAX_REQUESTS) -> list[dict[str, Any]]:
    """Исполнить разрешённые запросы. Возвращает список результатов —
    отклонённые тоже попадают в вывод, чтобы ревьюер понял, почему пусто."""
    results: list[dict[str, Any]] = []
    for req in (requests or [])[:max_requests]:
        entry = {"kind": req.get("kind"), "arg": req.get("arg"),
                 "why": (req.get("why") or "")[:200]}
        try:
            argv = build(req)
        except RejectedError as e:
            entry.update(status="rejected", output=str(e))
            results.append(entry)
            continue
        t0 = time.time()
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  timeout=CMD_TIMEOUT)
            out = sanitize_output((proc.stdout or "") + (proc.stderr or "")).strip()
            entry.update(status="ok", exit_code=proc.returncode,
                         output=out[-2000:], dur_s=round(time.time() - t0, 1))
        except subprocess.TimeoutExpired:
            entry.update(status="timeout", output=f"превышен лимит {CMD_TIMEOUT}s")
        except Exception as e:
            entry.update(status="error", output=f"{type(e).__name__}: {e}")
        results.append(entry)
    return results


def format_results(results: list[dict[str, Any]]) -> str:
    """Блок для второго промпта ревьюера."""
    if not results:
        return "(проверки не запрашивались)"
    parts: list[str] = []
    for r in results:
        head = f"### {r['kind']}({r.get('arg')!r}) → {r['status']}"
        if r.get("exit_code") is not None:
            head += f", exit={r['exit_code']}"
        parts.append(f"{head}\nзачем: {r['why']}\n```\n{r.get('output', '')}\n```")
    return "\n\n".join(parts)


def worktree_dirty(cwd: str) -> list[str]:
    """Проверки не должны менять рабочее дерево (§5.1). Если изменили —
    это дефект самой проверки, а не работы исполнителя."""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                       capture_output=True, text=True)
    return [line[3:].strip() for line in r.stdout.splitlines()]
