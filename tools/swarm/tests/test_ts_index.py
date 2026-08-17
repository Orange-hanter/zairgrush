#!/usr/bin/env python3
"""Тесты индексатора на tree-sitter.

Пропускаются целиком, если tree-sitter не установлен: в петле он
опционален (для Python хватает stdlib `ast`), и отсутствие пакета не
должно ронять набор тестов.

Запуск с зависимостями: .venv-ts/bin/python -m unittest test_ts_index
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
R = pathlib.Path(__file__).resolve().parent

try:
    _spec = importlib.util.spec_from_file_location("tsindex", ROOT_DIR / "tsindex.py")
    ts = importlib.util.module_from_spec(_spec)
    sys.modules["ts_index"] = ts
    _spec.loader.exec_module(ts)
    HAVE_TS = True
except Exception:  # noqa: BLE001 — tree-sitter опционален, тесты пропускаются
    HAVE_TS = False

RUST = """
pub struct Library { items: Vec<String> }

impl Library {
    pub fn add(&mut self, title: &str) -> usize {
        self.items.push(normalize(title));
        self.items.len()
    }
}

fn normalize(s: &str) -> String { s.trim().to_lowercase() }

pub fn count_words(text: &str) -> usize { normalize(text).split(' ').count() }
"""

PYTHON = '''
def helper(x):
    """Служебная."""
    return x + 1


def api(data):
    """Вход."""
    return helper(len(data))
'''


@unittest.skipUnless(HAVE_TS, "tree-sitter не установлен (опционален)")
class TestRust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "lib.rs").write_text(RUST)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rust_functions_found(self):
        symbols, _ = ts.index_project(self.root)
        names = {s["name"] for s in symbols.values()}
        self.assertIn("normalize", names)
        self.assertIn("count_words", names)
        self.assertIn("add", names, "методы в impl-блоке тоже определения")

    def test_rust_signature_preserved(self):
        symbols, _ = ts.index_project(self.root)
        sig = next(s["sig"] for s in symbols.values() if s["name"] == "count_words")
        self.assertIn("text: &str", sig)
        self.assertIn("usize", sig)

    def test_rust_calls_counted(self):
        _, calls = ts.index_project(self.root)
        self.assertGreaterEqual(sum(1 for c in calls if c["name"] == "normalize"), 2)

    def test_ast_cannot_parse_rust(self):
        import ast
        with self.assertRaises(SyntaxError):
            ast.parse(RUST)


@unittest.skipUnless(HAVE_TS, "tree-sitter не установлен (опционален)")
class TestPython(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "mod.py").write_text(PYTHON)

    def tearDown(self):
        self.tmp.cleanup()

    def test_python_functions_found(self):
        symbols, _ = ts.index_project(self.root)
        self.assertEqual({s["name"] for s in symbols.values()}, {"helper", "api"})

    def test_map_marks_usage(self):
        text = ts.project_map(self.root, budget=10)
        self.assertIn("helper", text)
        self.assertIn("зовут", text)

    def test_unknown_language_ignored(self):
        (self.root / "notes.txt").write_text("не код")
        symbols, _ = ts.index_project(self.root)
        self.assertEqual(len(symbols), 2, "файлы без грамматики пропускаются")


@unittest.skipUnless(HAVE_TS, "tree-sitter не установлен (опционален)")
class TestJunkDirsExcluded(unittest.TestCase):
    def test_shared_exclusion_set_applied(self):
        """Дефект: собственный фильтр знал только .venv (и то подстрокой по
        всему пути), а rglob("*") материализовался в sorted целиком —
        вместе с потрохами .git. Набор исключений теперь общий
        (pyindex.EXCLUDED_DIRS), фильтр стоит до сортировки."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "app.py").write_text("def mine():\n    pass\n")
            junk = {"node_modules/x/i.py": "def alien():\n    pass\n",
                    ".swarm/raw/x.py": "def alien():\n    pass\n",
                    "build/gen.py": "def alien():\n    pass\n",
                    "target/debug/gen.rs": "fn alien() {}\n"}
            for rel, src in junk.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(src)
            symbols, _ = ts.index_project(root)
            self.assertEqual({s["name"] for s in symbols.values()}, {"mine"},
                             "служебные каталоги не индексируются")


@unittest.skipUnless(HAVE_TS, "tree-sitter не установлен (опционален)")
class TestPrecisionLimit(unittest.TestCase):
    """Главное ограничение: tree-sitter решает задачу ПАРСИНГА, но не
    задачу РАЗРЕШЕНИЯ ИМЁН — счётчик вызовов остаётся приблизительным."""

    def test_same_name_in_different_modules_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text("def run():\n    pass\n")
            (root / "b.py").write_text("def run():\n    pass\n\ndef go():\n    run()\n")
            symbols, calls = ts.index_project(root)
            runs = [s for s in symbols.values() if s["name"] == "run"]
            self.assertEqual(len(runs), 2, "определения различаются по файлу")
            # но вызов по имени не привязан к конкретному определению
            self.assertEqual(sum(1 for c in calls if c["name"] == "run"), 1)

    def test_method_call_is_counted_as_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "m.py").write_text("def f(s):\n    return s.strip()\n")
            _, calls = ts.index_project(root)
            self.assertIn("strip", {c["name"] for c in calls},
                          "метод объекта неотличим от функции проекта — "
                          "для точности нужен разрешатель имён (ast/SCIP)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
