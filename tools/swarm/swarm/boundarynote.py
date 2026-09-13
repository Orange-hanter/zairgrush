"""Предревью-заметка о границах «заявление задачи ↔ дифф» (REV-005, P4).

Доказательная база — золотой набор PILOT-1: 7/7 споров исполнителя
удовлетворены, и повторяющийся дефект был ПОСТАНОВКОЙ задачи, а не кодом.
Страж gitops.scope_check ловит то же нарушение на стороне ИСПОЛНИТЕЛЯ
и тратит на него раунд; эта заметка поднимает ту же геометрию границ
ДО вызова ревьюера — рецензент видит расслоение «paths задачи ↔
фактический дифф» до того, как сожжёт вызов на спор о границах.

Правила намеренно те же, что у scope_check (явный защищённый паттерн в
paths отпирает защищённый файл; широкий глоб защиту не снимает) — два
судьи об одном файле не должны говорить на двух диалектах. Два
известных расхождения с scope_check, сознательных и маленьких:

- authored-паттерны. scope_check исключает из отпирания паттерны
  независимого тестировщика (`loop._authored_tests`); на стороне
  рецензента этот список недоступен (живёт на экземпляре Loop, а
  reviewer получает Agents), поэтому заметка отпирает их как обычные.
  Эффект симметричен fail-open: лишняя строчка заметки, не пропуск.
- режимные переименования без правки содержимого видны через заголовки
  `rename to`/`diff --git`; чистая смена mode попадает через `diff --git`
  (содержимого она не несёт, и заметка честно ограничена границами
  файлов, а не смыслом).

Заметка — текст ревьюеру и строка журнала; решает ревьюер, fail-open
при чистом диффе (None, промпт не меняется ни на байт).
"""
from __future__ import annotations

import ast
import fnmatch
import re
from typing import Any

DEFAULT_PROTECTED = ("tests/*", "tests/**")


def normalize_protected(value: Any) -> list[str]:
    """protected_paths строго списком строк; иначе — умолчание.

    Строка здесь — конфигурационная ошибка, а не список из символов:
    раньше fnmatch перебирал БУКВЫ паттерна, и защита молча пропадала.
    Предупреждение — на вызывающей стороне (у неё есть канал): здесь
    чистое решение «плохая форма → умолчание» (§7.3 fail-open).
    """
    if value is None:
        return list(DEFAULT_PROTECTED)
    if isinstance(value, list) and all(isinstance(p, str) for p in value):
        return list(value)
    return list(DEFAULT_PROTECTED)


def _unquote_git(token: str) -> str:
    """Снять C-экранирование git (core.quotepath): "b/\321\202.py" -> путь.

    git кодирует не-ASCII посимвольно в октальных байтах; literal_eval
    даёт codepoint=байт, дальше latin-1 -> utf-8 собирает символы обратно.
    """
    token = token.strip()
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return token
    try:
        raw: str = ast.literal_eval(token)
        return raw.encode("latin-1").decode("utf-8")
    except (ValueError, SyntaxError, UnicodeError):
        return token.strip('"')


def _b_side(token: str) -> str | None:
    name = _unquote_git(token)
    return name[2:] if name.startswith("b/") else None


def diff_files(diff_text: str) -> list[str]:
    """Файлы диффа: только заголовки, тело — нет.

    Три источника, по убыванию надёжности: пара `--- `/`+++ ` (принимается
    ТОЛЬКО парой — одиночная строка `+++ b/` из тела диффа форжится,
    пара — уже нет: строки тела несут префикс +/space), заголовки
    `rename to`/`copy to` (чистое переименование не имеет пары ---/+++),
    b-сторона `diff --git` (fallback для mode-only). C-экранирование
    снимается везде, квота `/dev/null` выбрасывается.
    """
    files: list[str] = []
    prev = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ ") and prev.startswith("--- "):
            name = _b_side(line[4:].strip())
            if name is not None:
                files.append(name)
        elif line.startswith(("rename to ", "copy to ")):
            files.append(line.split(" ", 2)[2].strip())
        elif line.startswith("diff --git "):
            for token in line[len("diff --git "):].split():
                name = _b_side(token)
                if name is not None:
                    files.append(name)
                    break
        prev = line
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _shown(paths: list[str], limit: int = 5, width: int = 80) -> str:
    """Пути в текст заметки: без управляющих символов, каждый до width."""
    clean = []
    for p in paths:
        s = _CTRL.sub("", p)
        clean.append(s if len(s) <= width else s[: width - 1] + "…")
    return ", ".join(clean[:limit]) + ("…" if len(paths) > limit else "")


def boundary_note(task: dict[str, Any],
                  diff_files: list[str],
                  protected_paths: Any = None) -> str | None:
    """Расслоение границ по файлам диффа: None, если его нет.

    Два детерминированных сигнала (05-doc §7.1: решается без LLM — LLM
    не нужен):

    - файл диффа не покрыт ни одним паттерном `paths` задачи — заявление
      не обещало эту правку (класс q005/q016: спор о том, чего постановка
      не называла);
    - файл диффа защищён и не отперт ЯВНЫМ защищённым паттерном из
      `paths` — та же семантика, что у scope_check, но со стороны
      рецензента: тест правится, и это видно до раунда.
    """
    allowed = [p for p in (task.get("paths") or []) if isinstance(p, str)]
    protected = normalize_protected(protected_paths)

    def is_protected(path: str) -> bool:
        return any(fnmatch.fnmatch(path, p) for p in protected)

    unlocking = [p for p in allowed if is_protected(p)]
    signals: list[str] = []
    if allowed:
        outside = sorted({f for f in diff_files
                          if not any(fnmatch.fnmatch(f, p) for p in allowed)})
        if outside:
            signals.append(
                f"дифф трогает файлы вне объявленных paths задачи: "
                f"{_shown(outside)}")
    touched = sorted({f for f in diff_files
                      if is_protected(f)
                      and not any(fnmatch.fnmatch(f, u) for u in unlocking)})
    if touched:
        signals.append(
            f"дифф правит защищённые файлы без явного отпирания в paths: "
            f"{_shown(touched)}")
    if not signals:
        return None
    return ("; ".join(signals)
            + ". Это заметка о границах, не блокировка: согласованность "
              "постановки задачи и диффа решает ревьюер")
