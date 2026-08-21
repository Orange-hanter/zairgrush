"""Чистые функции парсинга ответов агентов.

Вынесены из `agents.py`, чтобы класс дорогих ролей занимался только
оркестрацией вызовов, а разбор текста жил отдельно и был покрыт тестами
без сети и git.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Ревьюер получает дифф целиком, а размер сгенерированных артефактов ничем
# не ограничен. На пилоте golden-эталон в 15 894 строки дал промпт в 285 000
# токенов: вызов обрубался по `--max-budget-usd`, и так дважды подряд —
# $6.59 за ноль вердиктов при полностью готовой и зелёной работе.
#
# Поднимать бюджет бессмысленно: 16 000 строк построчно ревьюеру не
# прочесть за разумные деньги. Поэтому длинные файлы диффа сворачиваются
# до сводки — с прямым объявлением, что показано не всё.
#
# Оркестратор знает ОБЪЁМ и только его. Прежняя формулировка сообщала
# ревьюеру, что файл «судя по объёму, порождён машинно», — и на пилоте
# соврала: под порог попали 604 строки рукописного теста (e7in), а
# ревьюер начал вердикт с опровержения. Порог по числу строк не является
# признаком происхождения, и называть происхождение нельзя (§5.7.2:
# компонент называет факт, а причину — только если она следует из
# наблюдаемого однозначно).
DIFF_FILE_LIMIT = 400
DIFF_EXCERPT = 40

# Контракт chat-fill (E10, flag skeleton): весь файл — ровно в одном fence,
# без текста вокруг. Постороннее вокруг fence — не «почти прошло», а отказ:
# у чат-модели нет структурированного канала отчёта, весь контракт держится
# на форме ответа, и файл, срезанный посреди преамбулы, хуже отсутствия.
FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)\n?```", re.DOTALL)


def extract_fenced_code(text: str) -> str | None:
    """Единственный код-блок ответа chat-fill, либо None при нарушении формы.

    Несколько fence или текст вне них — отказ, а не «берём что есть»:
    берём ПОСЛЕДНИЙ fence, но только когда всё, что осталось после вычитания
    fence-блоков, пусто. Без этого условия преамбула вида «Вот файл:» тихо
    проходила бы как валидный ответ.
    """
    fences: list[str] = FENCE_RE.findall(text)
    if not fences:
        return None
    outside = FENCE_RE.sub("", text).strip()
    if outside:
        return None
    return fences[-1]


def condense_diff(diff: str, limit: int = DIFF_FILE_LIMIT,
                  excerpt: int = DIFF_EXCERPT) -> str:
    """Свернуть файлы диффа длиннее `limit` строк до сводки.

    Сводка называет файл, число добавленных и удалённых строк, хэш
    содержимого и выдержку сверху. Утаивание объявляется ПРЯМО: ревьюер,
    не знающий, что видит не всё, одобряет невиданное — а это ровно то,
    от чего защищает `git add -A -N` в work_diff.

    Хэш нужен, чтобы вердикт вообще был привязан к содержимому: без него
    два разных эталона одинаковой длины для ревьюера неразличимы.
    """
    if not diff:
        return diff
    out = []
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        if not chunk:
            continue
        lines = chunk.splitlines()
        if not lines[0].startswith("diff --git ") or len(lines) <= limit:
            out.append(chunk.rstrip("\n"))
            continue
        body = lines[1:]
        added = sum(1 for x in body
                    if x.startswith("+") and not x.startswith("+++"))
        removed = sum(1 for x in body
                      if x.startswith("-") and not x.startswith("---"))
        digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:12]
        # Выдержка обязана быть СОДЕРЖИМЫМ. Первые строки куска — это
        # `new file mode`, `index`, `---`, `+++`, `@@`: их пять, и в
        # выдержке из пяти строк ревьюер не увидел бы ни одной строки
        # файла. Заголовок отдаём целиком (он короткий и полезный),
        # выдержку берём после первого `@@`.
        cut = next((i + 1 for i, x in enumerate(body) if x.startswith("@@")), 0)
        head = "\n".join(body[cut:cut + excerpt])
        out.append(
            "\n".join(lines[:cut + 1]) + "\n"
            f"[оркестратор свернул этот файл: {len(body)} строк диффа, "
            f"+{added} −{removed}, sha256={digest}]\n"
            f"[показано не всё. Причина одна: файл длиннее {limit} строк. "
            f"О происхождении файла оркестратор ничего не знает — если это "
            f"написанный человеком код, суди его как код. Если сгенерированные "
            f"данные — оценивай КОД, который их строит. Определяешь по "
            f"содержимому ты, не оркестратор.]\n"
            f"[если для вердикта нужен файл целиком — это finding "
            f"severity=major с verdict=request_changes, а не approve вслепую]\n"
            f"[выдержка, первые {excerpt} строк:]\n{head}\n[…]")
    return "\n".join(out)


def report_in(text: str) -> dict[str, Any] | None:
    """Последний JSON-объект с полем `status` внутри текста.

    Контракт требует голый JSON, но исполнитель регулярно предваряет его
    фразой «готово, тесты зелёные». Требовать, чтобы контент НАЧИНАЛСЯ с
    `{`, — значит терять готовую работу из-за преамбулы: на приёмке v3st
    так потеряла три круга подряд и заблокировалась при зелёном гейте.
    Строгость здесь ничего не защищала: отчёт присутствовал и был валиден.

    Сканируем кандидатов с конца и берём первый разобравшийся — так
    случайный `{` из примера кода в преамбуле не может подменить отчёт.
    """
    dec = json.JSONDecoder()
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "{":
            continue
        try:
            cand, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(cand, dict) and "status" in cand:
            return cand
    return None


def extract_report(stream: str) -> dict[str, Any] | None:
    """Финальный JSON лежит в последнем assistant-событии, а не в
    последней строке потока (урок SMOKE-1)."""
    report: dict[str, Any] | None = None
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("role") == "assistant" and isinstance(ev.get("content"), str):
            cand = report_in(ev["content"])
            if cand is not None:
                report = cand
    return report
