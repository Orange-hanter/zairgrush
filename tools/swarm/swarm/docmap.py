"""docmap.toml: механический детектор дрейфа docs↔code (E5, вариант B).

Карта «файл/glob кода → секции документов» лежит в корне целевого
репозитория (`docmap.toml`). Изменился покрытый файл — перечисленные
секции подозреваются на рассинхрон БЕЗ единого вызова LLM; модель нужна
только чтобы сформулировать правку, и это отдельный (пока не реализованный)
шаг. Вариант A (docctx/cod-doc) не трогается: оба хелпера живут рядом,
сравнение — по пропущенным рассинхронам и токенам хелпера.

Модуль чистый stdlib (tomllib, fnmatch): его же импортирует реплей-замер
experiments/tools/e5-docdrift-replay.py, и тащить obs сюда — лишняя связь.
"""
from __future__ import annotations

import fnmatch
import pathlib
import tomllib
from typing import NamedTuple

MAP_NAME = "docmap.toml"


class DocRef(NamedTuple):
    """Ссылка на документ: путь от корня репо и необязательный якорь."""

    path: str
    anchor: str | None  # подстрока заголовка markdown, регистр не важен


class Entry(NamedTuple):
    """Одна строка карты: glob кода → ссылки на документы."""

    code: str
    docs: tuple[DocRef, ...]


class Suspect(NamedTuple):
    """Подозреваемая секция: какая, по какому glob, из-за какого файла."""

    doc: str  # "path#anchor" или "path"
    code: str  # сработавший glob из карты
    file: str  # изменённый файл


def parse_ref(ref: object) -> DocRef:
    """"путь.md" или "путь.md#якорь" → DocRef. Мусор — ValueError."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"doc-ссылка должна быть непустой строкой: {ref!r}")
    path, _, anchor = ref.partition("#")
    path = path.strip()
    if not path:
        raise ValueError(f"doc-ссылка без пути: {ref!r}")
    return DocRef(path, anchor.strip() or None)


def load(path: pathlib.Path | str) -> list[Entry]:
    """Прочитать и проверить форму карты. Ошибки формата — ValueError."""
    p = pathlib.Path(path)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    raw = data.get("map")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{p}: карта пуста или нет таблиц [[map]]")
    entries: list[Entry] = []
    for i, item in enumerate(raw):
        where = f"{p}: [[map]] #{i + 1}"
        if not isinstance(item, dict):
            raise TypeError(f"{where}: запись не таблица")
        code = item.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"{where}: нет непустого поля code")
        docs_raw = item.get("docs")
        if not isinstance(docs_raw, list) or not docs_raw:
            raise ValueError(f"{where}: нет непустого списка docs")
        docs = tuple(parse_ref(r) for r in docs_raw)
        entries.append(Entry(code.strip(), docs))
    return entries


def _headings(text: str) -> list[str]:
    """Тексты заголовков markdown без решёток, в нижнем регистре."""
    out = []
    for line in text.splitlines():
        s = line.lstrip("#").strip() if line.startswith("#") else ""
        if s:
            out.append(s.lower())
    return out


def check(root: pathlib.Path | str, entries: list[Entry]) -> list[str]:
    """Состояние самой карты: список предупреждений (пусто = карта жива).

    Три класса, все механические: glob не покрывает ни одного файла,
    документ не существует, якоря нет ни в одном заголовке документа.
    """
    root = pathlib.Path(root)
    all_files = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    ]
    warnings: list[str] = []
    heading_cache: dict[str, list[str] | None] = {}
    for e in entries:
        if not any(fnmatch.fnmatchcase(f, e.code) for f in all_files):
            warnings.append(f"glob ничего не покрывает: {e.code}")
        for ref in e.docs:
            if ref.path not in heading_cache:
                doc = root / ref.path
                heading_cache[ref.path] = (
                    _headings(doc.read_text(encoding="utf-8"))
                    if doc.is_file()
                    else None
                )
            heads = heading_cache[ref.path]
            if heads is None:
                warnings.append(f"документ не найден: {ref.path} (код: {e.code})")
            elif ref.anchor and not any(
                ref.anchor.lower() in h for h in heads
            ):
                warnings.append(
                    f"якорь {ref.anchor!r} не найден в заголовках {ref.path}"
                )
    return warnings


def suspects(changed: list[str], entries: list[Entry]) -> list[Suspect]:
    """Изменённые файлы → подозреваемые секции документов.

    Дедупликация по (doc, file): одна правка одного файла не должна
    называть одну секцию дважды, даже если её поймали два glob'а.
    """
    out: list[Suspect] = []
    seen: set[tuple[str, str]] = set()
    for f in changed:
        for e in entries:
            if not fnmatch.fnmatchcase(f, e.code):
                continue
            for ref in e.docs:
                doc = f"{ref.path}#{ref.anchor}" if ref.anchor else ref.path
                if (doc, f) in seen:
                    continue
                seen.add((doc, f))
                out.append(Suspect(doc, e.code, f))
    return out
