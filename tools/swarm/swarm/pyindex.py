#!/usr/bin/env python3
"""Точный индекс кода в духе SCIP/LSP — на stdlib `ast`.

Чем отличается от наивной карты (`repo_map.py`):

Наивная версия сопоставляет вызовы по ГОЛОМУ ИМЕНИ. Из-за этого
`text.upper()` считается вызовом функции `upper`, а две одноимённые
функции в разных модулях сливаются в один узел. Для карты «что вообще
есть в проекте» это терпимо, для ответа «кто именно зовёт эту функцию» —
нет: ревьюер получит ложные связи и пропустит настоящие.

Здесь символы получают квалифицированные идентификаторы
(`wordstat.stats.word_freq`), импорты разрешаются, а ссылки различают
прямой вызов, вызов через модуль (`stats.word_freq(...)`) и обращение к
атрибуту объекта (которое ссылкой на функцию проекта НЕ считается).

Это тот же принцип, что у SCIP: стабильный symbol ID, набор definitions и
occurrences. Полноценный SCIP-индексатор дал бы ещё типы и наследование,
но требует внешнего тулчейна; здесь достаточно точности по вызовам.
"""
import ast
import pathlib
from collections.abc import Iterator
from typing import Any


class Symbol:
    __slots__ = ("doc", "file", "id", "kind", "line", "module", "name", "sig")

    doc: str | None
    file: str
    id: str
    kind: str
    line: int
    module: str
    name: str
    sig: str | None

    def __init__(self, **kw: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self) -> str:
        return f"Symbol({self.id})"


class Reference:
    __slots__ = ("file", "from_symbol", "how", "line", "symbol_id")

    file: str
    from_symbol: str
    how: str
    line: int
    symbol_id: str

    def __init__(self, **kw: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self) -> str:
        return f"Ref({self.symbol_id} <- {self.from_symbol} {self.how})"


def _module_name(path: pathlib.Path, root: pathlib.Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = node.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    ret = ""
    if node.returns is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:
            ret = ""
    return f"{node.name}({', '.join(names)}){ret}"


class Index:
    """Индекс проекта: определения + разрешённые ссылки."""

    def __init__(self, root: str | pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.symbols: dict[str, Symbol] = {}
        self.references: list[Reference] = []
        self._by_name: dict[str, list[str]] = {}   # короткое имя -> [id]
        self._build()

    # --- построение -----------------------------------------------------

    def _files(self) -> Iterator[pathlib.Path]:
        for p in sorted(self.root.rglob("*.py")):
            if "__pycache__" in p.parts or ".git" in p.parts:
                continue
            yield p

    def _build(self) -> None:
        trees: dict[pathlib.Path, ast.Module] = {}
        for path in self._files():
            try:
                trees[path] = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
        for path, tree in trees.items():
            self._collect_defs(path, tree)
        for path, tree in trees.items():
            self._collect_refs(path, tree)

    def _collect_defs(self, path: pathlib.Path, tree: ast.Module) -> None:
        module = _module_name(path, self.root)
        rel = path.relative_to(self.root).as_posix()

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sid = f"{prefix}.{child.name}" if prefix else child.name
                    doc = ast.get_docstring(child) or ""
                    self.symbols[sid] = Symbol(
                        id=sid, name=child.name, module=module, file=rel,
                        line=child.lineno, sig=_sig(child),
                        doc=doc.strip().splitlines()[0] if doc.strip() else "",
                        kind="function")
                    self._by_name.setdefault(child.name, []).append(sid)
                    walk(child, sid)
                elif isinstance(child, ast.ClassDef):
                    cid = f"{prefix}.{child.name}" if prefix else child.name
                    self.symbols[cid] = Symbol(
                        id=cid, name=child.name, module=module, file=rel,
                        line=child.lineno, sig=f"class {child.name}",
                        doc=(ast.get_docstring(child) or "").strip().splitlines()[0]
                        if ast.get_docstring(child) else "",
                        kind="class")
                    self._by_name.setdefault(child.name, []).append(cid)
                    walk(child, cid)

        walk(tree, module)

    def _imports(self, tree: ast.Module, module: str) -> dict[str, str]:
        """Локальное имя -> квалифицированный id (насколько можно вывести)."""
        table = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                base = node.module
                if node.level:                       # относительный импорт
                    pkg = module.rsplit(".", node.level)[0] if "." in module else ""
                    base = f"{pkg}.{node.module}" if pkg else node.module
                for alias in node.names:
                    local = alias.asname or alias.name
                    table[local] = f"{base}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    table[local] = alias.name
        return table

    def _collect_refs(self, path: pathlib.Path, tree: ast.Module) -> None:
        module = _module_name(path, self.root)
        rel = path.relative_to(self.root).as_posix()
        imports = self._imports(tree, module)

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    walk(child, f"{prefix}.{child.name}" if prefix else child.name)
                    continue
                if isinstance(child, ast.Call):
                    self._record_call(child, prefix, rel, module, imports)
                walk(child, prefix)

        walk(tree, module)

    def _record_call(self, call: ast.Call, from_symbol: str, rel: str,
                     module: str, imports: dict[str, str]) -> None:
        fn = call.func
        target, how = None, None
        if isinstance(fn, ast.Name):
            # прямой вызов: либо импортировано, либо определено в модуле
            target = imports.get(fn.id) or f"{module}.{fn.id}"
            how = "direct"
            if target not in self.symbols:
                # может быть вложенной функцией или из другого места
                candidates = self._by_name.get(fn.id, [])
                target = candidates[0] if len(candidates) == 1 else None
                how = "resolved-by-name" if target else None
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            base = imports.get(fn.value.id)
            if base:                                   # stats.word_freq(...)
                target = f"{base}.{fn.attr}"
                how = "via-module"
            # иначе это метод объекта: text.upper() — не символ проекта
        if target and target in self.symbols:
            self.references.append(Reference(
                symbol_id=target, file=rel, line=call.lineno,
                from_symbol=from_symbol, how=how))

    # --- запросы --------------------------------------------------------

    def callers(self, symbol_id: str) -> list[Reference]:
        return [r for r in self.references if r.symbol_id == symbol_id]

    def rank(self, damping: float = 0.85, rounds: int = 20) -> dict[str, float]:
        """PageRank по ТОЧНЫМ ссылкам, а не по совпадению имён."""
        nodes = [s for s, sym in self.symbols.items() if sym.kind == "function"]
        if not nodes:
            return {}
        incoming: dict[str, set[str]] = {n: set() for n in nodes}
        outdeg = dict.fromkeys(nodes, 0)
        for r in self.references:
            src = r.from_symbol
            if r.symbol_id in incoming and src in outdeg and src != r.symbol_id:
                incoming[r.symbol_id].add(src)
                outdeg[src] += 1
        for n in outdeg:
            outdeg[n] = outdeg[n] or 1
        rk = {n: 1.0 / len(nodes) for n in nodes}
        for _ in range(rounds):
            rk = {n: (1 - damping) / len(nodes)
                     + damping * sum(rk[s] / outdeg[s] for s in incoming[n])
                  for n in nodes}
        return rk

    def is_test(self, symbol_id: str) -> bool:
        s = self.symbols[symbol_id]
        return s.file.startswith("tests/") or s.name.startswith("test_")

    # --- представления для промпта ---------------------------------------

    def project_map(self, budget: int = 25, include_tests: bool = False) -> str:
        rk = self.rank()
        ids = [s for s in rk if include_tests or not self.is_test(s)]
        top = sorted(ids, key=lambda s: (-rk[s], s))[:budget]
        by_file: dict[str, list[str]] = {}
        for sid in top:
            by_file.setdefault(self.symbols[sid].file, []).append(sid)
        out = []
        for f in sorted(by_file):
            out.append(f)
            for sid in sorted(by_file[f], key=lambda s: self.symbols[s].line):
                sym = self.symbols[sid]
                used = len([r for r in self.callers(sid)
                            if not r.file.startswith("tests/")])
                doc = f" — {sym.doc}" if sym.doc else ""
                mark = f"  [зовут: {used}]" if used else ""
                out.append(f"  {sym.sig}{doc}{mark}")
        return "\n".join(out)

    def impact(self, symbol_ids: list[str]) -> str:
        """Blast radius по точным ссылкам: продукционные вызывающие поимённо,
        тесты — числом."""
        lines = []
        for sid in symbol_ids:
            if sid not in self.symbols:
                continue
            refs = self.callers(sid)
            prod = [r for r in refs if not r.file.startswith("tests/")]
            tests = [r for r in refs if r.file.startswith("tests/")]
            head = f"{sid}"
            if not prod and not tests:
                lines.append(f"{head}: вызовов в проекте нет")
                continue
            lines.append(f"{head} вызывается из:")
            for r in sorted(prod, key=lambda r: (r.file, r.line)):
                lines.append(f"  {r.file}:{r.line} ({r.from_symbol}, {r.how})")
            if tests:
                files = len({r.file for r in tests})
                lines.append(f"  + тесты: {len(tests)} вызов(ов) в {files} файл(ах)")
        return "\n".join(lines) or "(символы не найдены)"

    def resolve(self, short_name: str) -> list[str]:
        """Короткое имя -> список квалифицированных id."""
        return list(self._by_name.get(short_name, []))
