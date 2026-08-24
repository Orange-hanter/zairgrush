"""Гибридный индекс: каждый инструмент делает то, в чём он сильнее.

Замеры показали, что «лучшего» индексатора нет — есть три разные задачи:

| Инструмент       | Сильная сторона                     | Чего не даёт          |
|------------------|-------------------------------------|-----------------------|
| universal-ctags  | 164 языка, 14 мс, JSON, импорты     | вызовы (кто зовёт)    |
| stdlib `ast`     | точные вызовы с позициями (Python)  | только Python         |
| tree-sitter      | парсинг любого языка с грамматикой  | разрешение имён       |

Отсюда слоистая схема с деградацией:

  слой 1 (широкий):  ctags -> определения + импорты для ВСЕХ файлов репо;
  слой 2 (точный):   для языков, где есть разрешатель, — настоящие ссылки
                     (Python: ast с разбором импортов);
  слой 3 (запасной): tree-sitter, если ctags нет, а грамматика есть.

Главное свойство: **каждый факт помечен уровнем достоверности**. Агенту
нельзя выдавать догадку за факт — «эту функцию точно зовут отсюда» и
«возможно, зовут» должны выглядеть по-разному, иначе ревьюер построит на
догадке finding, а исполнитель — правку.
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
from types import ModuleType
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import modlock  # noqa: E402

# Уровни достоверности ссылки, от сильного к слабому.
RESOLVED = "resolved"        # разрешён импорт, известен вызывающий символ
IMPORT = "import"            # известно, что модуль/имя импортируется
NAME_GUESS = "name-guess"    # догадка ast-слоя: единственный кандидат по имени
NAME_MATCH = "name-match"    # совпадение по голому имени, возможны ложные


def _load(name: str, filename: str) -> ModuleType:
    # Общий замок загрузчиков: два плеча дуэли грузят модули из
    # потоков, и без него сосед видит наполовину выполненный модуль
    # (см. modlock.py — поймано первым же настоящим прогоном).
    with modlock.LOCK:
        cached = modlock.ready(name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(name, HERE / filename)
        if spec is None or spec.loader is None:
            raise ImportError(f"не удалось загрузить {filename}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


obs = _load("obs", "obs.py")
log = obs.get_logger("codemap")
# Общий набор исключаемых каталогов живёт в pyindex: обходы дерева обязаны
# совпадать у всех слоёв, иначе ctags индексирует .venv, который ast не видит.
pyindex = _load("pyindex", "pyindex.py")


def have_ctags() -> str | None:
    exe = shutil.which("ctags")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=10, check=False).stdout
    except Exception:
        log.warning("ctags не опрошен: %s", exe, exc_info=True)
        return None
    # Exuberant 5.8 (2009) не умеет ни JSON, ни ролей, а `brew install ctags`
    # ставит именно его под тем же именем. Принять его за universal — значит
    # молча получить плоский список имён вместо индекса.
    return exe if "Universal Ctags" in out else None


class HybridIndex:
    def __init__(self, root: str | pathlib.Path,
                 use_tree_sitter: bool = False) -> None:
        self.root = pathlib.Path(root)
        # id -> {name, file, line, kind, lang, source}
        self.symbols: dict[str, dict[str, Any]] = {}
        # {from, to, file, line, confidence, kind}
        self.edges: list[dict[str, Any]] = []
        self.sources: list[str] = []   # какие слои реально отработали
        self._ctags()
        self._python_precise()
        # tree-sitter дополняет там, где точного разрешателя нет: для Rust и
        # прочих языков он даёт хотя бы приблизительные ссылки, тогда как
        # ctags не даёт никаких. Для Python не нужен — ast точнее.
        if use_tree_sitter:
            self._tree_sitter()

    # --- слой 1: широкий ------------------------------------------------

    def _ctags(self) -> None:
        exe = have_ctags()
        if not exe:
            return
        # +l — язык символа, +r — роли (без него `roles` не приходит вовсе,
        # и импортные рёбра теряются молча
        cmd = [exe, "--output-format=json", "--fields=+nKSlzr", "--extras=+r",
               # Без --exclude ctags -R честно индексирует .venv и node_modules
               # стенда — тысячи чужих символов поверх сотни своих.
               *(f"--exclude={d}" for d in sorted(pyindex.EXCLUDED_DIRS)),
               "-R", "-f", "-", "."]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 cwd=self.root, timeout=120, check=False).stdout
        except Exception:
            log.warning("слой ctags не отработал", exc_info=True)
            return
        count = 0
        for line in out.splitlines():
            try:
                tag = json.loads(line)
            except ValueError:
                continue
            if tag.get("_type") != "tag":
                continue
            name, path = tag.get("name"), tag.get("path")
            if not name or not path:
                continue
            # removeprefix, не lstrip: lstrip("./") ест ЛЮБЫЕ точки и слэши
            # в начале и превращал ".github/x.py" в "github/x.py".
            path = path.removeprefix("./")
            roles = tag.get("roles") or ""
            if "imported" in roles:
                # ctags знает, кто импортирует символ, но не кто зовёт
                self.edges.append({"from": path, "to": name,
                                   "file": path, "line": tag.get("line"),
                                   "confidence": IMPORT, "kind": "import"})
                continue
            sid = f"{path}::{name}"
            # ctags отдаёт имя и сигнатуру раздельно: без склейки в карте
            # получаются безымянные строки вида «(s: &str) -> String»
            sig = tag.get("signature") or ""
            self.symbols[sid] = {
                "name": name, "file": path, "line": tag.get("line"),
                "kind": tag.get("kind"), "lang": tag.get("language"),
                "sig": f"{name}{sig}" if sig else name, "source": "ctags"}
            count += 1
        if count:
            self.sources.append(f"ctags({count} симв.)")

    # --- слой 2: точный, по языку ----------------------------------------

    def _python_precise(self) -> None:
        """Для Python поднимаем достоверность до RESOLVED: настоящие вызовы
        с позициями, полученные разбором импортов."""
        try:
            idx = pyindex.Index(self.root)
        except Exception:
            log.warning("точный слой (ast) не отработал", exc_info=True)
            return
        for sid, sym in idx.symbols.items():
            # Ключ несёт квалификацию (sid), а не голое имя: по `file::name`
            # два `__init__` двух классов одного файла сливались в один,
            # и последний молча затирал первого.
            key = f"{sym.file}::{sid}"
            # Запись слоя ctags знает символ только по имени — забираем её
            # как основу, чтобы не плодить дубликат того же определения.
            entry = self.symbols.pop(f"{sym.file}::{sym.name}", {})
            merged = bool(entry)
            entry.update({"name": sym.name, "file": sym.file, "line": sym.line,
                          "kind": sym.kind, "lang": "Python",
                          "sig": sym.sig or entry.get("sig", ""),
                          "doc": sym.doc, "qualified": sid,
                          "source": "ctags+ast" if merged else "ast"})
            self.symbols[key] = entry
        for ref in idx.references:
            # Догадка «единственный кандидат по голому имени» — не разрешённый
            # импорт: выдавать её за RESOLVED значит нарушать собственную
            # доктрину честности уровней.
            conf = NAME_GUESS if ref.how == "resolved-by-name" else RESOLVED
            self.edges.append({"from": ref.from_symbol, "to": ref.symbol_id,
                               "file": ref.file, "line": ref.line,
                               "confidence": conf, "kind": ref.how})
        if idx.symbols:
            self.sources.append(f"ast({len(idx.symbols)} симв., "
                                f"{len(idx.references)} точных ссылок)")

    # --- слой 3: запасной ------------------------------------------------

    def _tree_sitter(self) -> None:
        try:
            ts = _load("tsindex", "tsindex.py")
            symbols, calls = ts.index_project(self.root)
        except Exception:
            log.warning("слой tree-sitter не отработал", exc_info=True)
            return
        for s in symbols.values():
            # Python уже покрыт ast-слоем (его ключи квалифицированы):
            # setdefault по голому имени создал бы дубликаты его символов.
            if s["file"].endswith(".py"):
                continue
            self.symbols.setdefault(f"{s['file']}::{s['name']}", {
                "name": s["name"], "file": s["file"], "line": s["line"],
                "kind": "function", "lang": s["lang"], "sig": s["sig"],
                "source": "tree-sitter"})
        added = 0
        for c in calls:
            # Python уже покрыт точными ссылками — не зашумляем его
            # приблизительными; берём только языки без разрешателя.
            if c["file"].endswith(".py"):
                continue
            self.edges.append({"from": c["file"], "to": c["name"],
                               "file": c["file"], "line": c["line"],
                               "confidence": NAME_MATCH, "kind": "call"})
            added += 1
        if symbols:
            self.sources.append(f"tree-sitter({len(symbols)} симв., "
                                f"{added} прибл. ссылок)")

    # --- запросы ----------------------------------------------------------

    def callers(self, symbol_name_or_id: str) -> list[dict[str, Any]]:
        """Все, кто ссылается на символ, с уровнем достоверности.

        Сортировка от точного к приблизительному: агент должен сначала
        увидеть то, что известно наверняка.
        """
        order = {RESOLVED: 0, IMPORT: 1, NAME_GUESS: 2, NAME_MATCH: 3}
        hits = [e for e in self.edges
                if e["to"] == symbol_name_or_id
                or e["to"].endswith("." + symbol_name_or_id)]
        return sorted(hits, key=lambda e: (order.get(e["confidence"], 9),
                                           e["file"], e["line"] or 0))

    def project_map(self, budget: int = 25, skip_tests: bool = True) -> str:
        used: dict[str, int] = {}
        for e in self.edges:
            if e["confidence"] == RESOLVED and not e["file"].startswith("tests/"):
                used[e["to"].rsplit(".", 1)[-1]] = used.get(
                    e["to"].rsplit(".", 1)[-1], 0) + 1
        items = [s for s in self.symbols.values()
                 if s.get("kind") in ("function", "method", "class")
                 and not (skip_tests and (s["file"].startswith("tests/")
                                          or s["name"].startswith("test_")))]
        items.sort(key=lambda s: (-used.get(s["name"], 0), s["file"], s["line"] or 0))
        by_file: dict[str, list[dict[str, Any]]] = {}
        for s in items[:budget]:
            by_file.setdefault(s["file"], []).append(s)
        out = []
        for f in sorted(by_file):
            out.append(f)
            for s in sorted(by_file[f], key=lambda s: s["line"] or 0):
                sig = s.get("sig") or f"{s['name']}(...)"
                doc = f" — {s['doc']}" if s.get("doc") else ""
                mark = f"  [зовут: {used[s['name']]}]" if used.get(s["name"]) else ""
                out.append(f"  {sig}{doc}{mark}")
        return "\n".join(out)

    def impact(self, symbol: str, max_rows: int = 12) -> str:
        """Blast radius с честной пометкой, что известно точно, а что нет."""
        hits = self.callers(symbol)
        if not hits:
            return f"{symbol}: ссылок не найдено"
        lines = [f"{symbol} — ссылки ({len(hits)}):"]
        shown = 0
        tests = 0
        for e in hits:
            if e["file"].startswith("tests/"):
                tests += 1
                continue
            if shown >= max_rows:
                continue
            note = {RESOLVED: "точно", IMPORT: "импорт",
                    NAME_GUESS: "догадка по имени", NAME_MATCH: "по имени"}[
                e["confidence"]]
            lines.append(f"  {e['file']}:{e['line']} ({e['from']}, {note})")
            shown += 1
        if tests:
            lines.append(f"  + тесты: {tests} ссылок")
        return "\n".join(lines)

    def report(self) -> dict[str, Any]:
        by_conf: dict[str, int] = {}
        for e in self.edges:
            by_conf[e["confidence"]] = by_conf.get(e["confidence"], 0) + 1
        langs: dict[str, int] = {}
        for s in self.symbols.values():
            langs[s.get("lang") or "?"] = langs.get(s.get("lang") or "?", 0) + 1
        return {"symbols": len(self.symbols), "edges": len(self.edges),
                "by_confidence": by_conf, "languages": langs,
                "sources": self.sources}
