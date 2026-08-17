"""Индекс на tree-sitter: проверка, даёт ли он value поверх stdlib `ast`.

Вопрос стоял так: индустрия не зря пользуется ctags/tree-sitter/SCIP —
что из этого реально нужно нам?

Ответ зависит от языка целевого репозитория:

- **Python**: stdlib `ast` разбирает язык полностью и точно, зависимостей
  не требует. tree-sitter здесь ничего не добавляет — та же информация
  ценой внешнего пакета.
- **Rust** (а целевой проект ZeusLogic именно на нём): `ast` бесполезен,
  и вот тут tree-sitter незаменим — один и тот же код индексирует любой
  язык, для которого есть грамматика.

Этот модуль — единый индексатор поверх tree-sitter для обоих языков;
он существует, чтобы сравнение было честным, а не умозрительным.

Требует: pip install tree_sitter tree-sitter-python tree-sitter-rust
(в петле — опционально: без него работает ast-индекс для Python).
"""
import pathlib
import sys
from typing import Any

import tree_sitter_python
import tree_sitter_rust
from tree_sitter import Language, Parser, Query, QueryCursor

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Набор исключаемых каталогов общий для всех обходов дерева (см. pyindex):
# собственный список здесь уже разошёлся с остальными — знал про .venv,
# но не про node_modules и .swarm.
from pyindex import excluded  # noqa: E402 — каталог добавлен строкой выше

LANGS = {
    ".py": (Language(tree_sitter_python.language()), "python"),
    ".rs": (Language(tree_sitter_rust.language()), "rust"),
}

# Запросы к дереву: что считать определением функции в каждом языке.
QUERIES = {
    "python": """
        (function_definition name: (identifier) @name) @def
    """,
    "rust": """
        (function_item name: (identifier) @name) @def
    """,
}

CALL_QUERIES = {
    "python": """
        (call function: (identifier) @callee)
        (call function: (attribute attribute: (identifier) @callee))
    """,
    "rust": """
        (call_expression function: (identifier) @callee)
        (call_expression function: (field_expression field: (field_identifier) @callee))
        (call_expression function: (scoped_identifier name: (identifier) @callee))
    """,
}


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def index_file(
    path: pathlib.Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """-> (definitions, calls) для одного файла любого поддержанного языка."""
    suffix = pathlib.Path(path).suffix
    if suffix not in LANGS:
        return [], []
    lang, kind = LANGS[suffix]
    src = pathlib.Path(path).read_bytes()
    parser = Parser(lang)
    tree = parser.parse(src)

    defs = []
    # API tree-sitter 0.26: Query + QueryCursor вместо Language.query()
    captures = QueryCursor(Query(lang, QUERIES[kind])).captures(tree.root_node)
    for node in captures.get("def", []):
        name_node = next((n for n in captures.get("name", [])
                          if n.start_byte >= node.start_byte
                          and n.end_byte <= node.end_byte), None)
        if name_node is None:
            continue
        # сигнатура — первая строка определения, как её видит человек
        first_line = _text(node, src).splitlines()[0].strip()
        defs.append({"name": _text(name_node, src), "line": node.start_point[0] + 1,
                     "sig": first_line.rstrip("{:").strip(), "lang": kind})

    call_caps = QueryCursor(Query(lang, CALL_QUERIES[kind])).captures(tree.root_node)
    calls = [{"name": _text(node, src), "line": node.start_point[0] + 1}
             for node in call_caps.get("callee", [])]
    return defs, calls


def index_project(root: str | pathlib.Path,
                  suffixes: tuple[str, ...] = (".py", ".rs"),
                  ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = pathlib.Path(root)
    symbols: dict[str, dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    # Фильтр — ДО sorted: материализовать rglob("*") целиком значило бы
    # держать в памяти всё содержимое .git и .venv ради его выбрасывания.
    files = sorted(p for p in root.rglob("*")
                   if p.suffix in suffixes and not excluded(p, root))
    for path in files:
        defs, cs = index_file(path)
        rel = path.relative_to(root).as_posix()
        for d in defs:
            symbols[f"{rel}::{d['name']}"] = dict(d, file=rel)
        calls.extend(dict(c, file=rel) for c in cs)
    return symbols, calls


def project_map(root: str | pathlib.Path, budget: int = 25,
                skip_tests: bool = True) -> str:
    symbols, calls = index_project(root)
    used: dict[str, int] = {}
    for c in calls:
        used[c["name"]] = used.get(c["name"], 0) + 1
    items = [(sid, s) for sid, s in symbols.items()
             if not (skip_tests and (s["file"].startswith("tests/")
                                     or s["name"].startswith("test_")))]
    items.sort(key=lambda kv: (-used.get(kv[1]["name"], 0),
                               kv[1]["file"], kv[1]["line"]))
    out: list[str] = []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for _sid, s in items[:budget]:
        by_file.setdefault(s["file"], []).append(s)
    for f in sorted(by_file):
        out.append(f)
        for s in sorted(by_file[f], key=lambda s: s["line"]):
            mark = f"  [зовут: {used.get(s['name'], 0)}]" if used.get(s["name"]) else ""
            out.append(f"  {s['sig']}{mark}")
    return "\n".join(out)
