"""Предревью blast radius: кто вызывает изменённые символы (E3-C, REV-008).

Геометрия та же, что у boundarynote: детерминированный код ДО вызова
ревьюера, fail-open (сбой заметки стоит ноль — ревьюер идёт без неё,
как до флага). Дифф показывает ЧТО изменилось, но не ГДЕ аукнется;
заметка закрывает второе: символы, чьи определения правит дифф, и их
вызывающие из индекса кода (codemap.HybridIndex — те же уровни
достоверности, что у карты репозитория ADR-006: «точно» ≠ «по имени»).

Замер эффекта — REV-008 (experiments/reviewarm/rev008/prereg.json):
рука с заметкой против руки без на канарейках, ломающих НЕПОКРЫТОГО
вызывающего. Флаг выключен по умолчанию: с заметкой промпт длиннее и
дороже, и включать его в петле до положительного вердикта замера нельзя.
"""
from __future__ import annotations

import re
from typing import Any

# Определения, которые правка МОЖЕТ добавить строкой диффа. Сознательно
# узкий набор: это триггер «символ затронут», а не парсер языков —
# ложный символ в заметке дешевле пропущенного, потому что impact()
# честно ответит «ссылок не найдено».
_DEF_RES = (
    # Rust: fn name( / pub fn name( / pub(crate) async fn name(
    re.compile(r"^\+\s*(?:(?:pub(?:\s*\([^)]*\))?|crate)\s+)*"
               r"(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    # Python: def name( / async def name(
    re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    # JS/TS: function name( / export const name = (…) =>
    re.compile(r"^\+\s*(?:export\s+)?(?:async\s+)?function\s+"
               r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
)

# Больше символов — длиннее заметка, а вклад каждого следующего падает:
# дифф, правящий 9+ определений, обычно механический (rename/обвязка),
# и весь его blast перестаёт читаться ревьюером.
MAX_SYMBOLS = 8


def changed_symbols(diff_text: str) -> list[str]:
    """Короткие имена определений, добавленных или переписанных диффом.

    Только строки `+` с определением: правка тела без сигнатуры символ
    не добавляет (тогда blast берётся по соседним затронутым правкам —
    грубость задокументирована в пререге REV-008 как ограничение замера).
    Порядок первого появления, дубликаты сняты.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("+"):
            continue
        for rx in _DEF_RES:
            m = rx.match(line)
            if m:
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    out.append(name)
                break
        if len(out) >= MAX_SYMBOLS:
            break
    return out


def blast_note(index: Any, symbols: list[str],
               max_rows: int = 12) -> str | None:
    """Заметка ревьюеру: вызывающие каждого символа через index.impact().

    Пустая при отсутствии затронутых символов → None: промпт не должен
    меняться ни на байт, когда blast нечего сказать (та же геометрия,
    что у boundary_note на чистом диффе).
    """
    if not symbols:
        return None
    parts = [index.impact(s, max_rows=max_rows) for s in symbols]
    note = "\n".join(p for p in parts if p)
    return note or None
