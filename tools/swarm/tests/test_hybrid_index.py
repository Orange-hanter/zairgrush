#!/usr/bin/env python3
"""Тесты гибридного индекса.

Главное, что здесь проверяется, — не полнота, а ЧЕСТНОСТЬ: индекс обязан
помечать, что он знает точно, а что предполагает. Если приблизительная
ссылка выдаётся как точная, ревьюер построит на ней finding, а исполнитель
— правку.
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
H = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("codemap", ROOT_DIR / "codemap.py")
hi = importlib.util.module_from_spec(_spec)
sys.modules["hybrid_index"] = hi
_spec.loader.exec_module(hi)

HAVE_CTAGS = hi.have_ctags() is not None

PY_CORE = 'def helper(x):\n    """Ядро."""\n    return x * 2\n'
PY_UTIL = ('from py.core import helper\n\n\n'
           'def wrapper(x):\n    """Обёртка."""\n    return helper(x)\n')
RUST = ("fn normalize(s: &str) -> String { s.trim().to_lowercase() }\n\n"
        "pub fn run(t: &str) -> String { normalize(t) }\n")


class HybridCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "py").mkdir()
        (self.root / "py" / "core.py").write_text(PY_CORE)
        (self.root / "py" / "util.py").write_text(PY_UTIL)
        (self.root / "src").mkdir()
        (self.root / "src" / "lib.rs").write_text(RUST)

    def tearDown(self):
        self.tmp.cleanup()


class TestLayering(HybridCase):
    def test_python_gets_resolved_references(self):
        idx = hi.HybridIndex(self.root)
        refs = idx.callers("helper")
        resolved = [r for r in refs if r["confidence"] == hi.RESOLVED]
        self.assertTrue(resolved, "для Python обязаны быть точные ссылки")
        self.assertEqual(resolved[0]["from"], "py.util.wrapper")

    def test_precise_layer_wins_over_broad(self):
        idx = hi.HybridIndex(self.root)
        helper = next(s for s in idx.symbols.values() if s["name"] == "helper")
        self.assertIn("ast", helper["source"])
        self.assertEqual(helper.get("doc"), "Ядро.",
                         "docstring приходит из точного слоя, ctags его не даёт")

    def test_callers_sorted_by_confidence(self):
        idx = hi.HybridIndex(self.root)
        refs = idx.callers("helper")
        confs = [r["confidence"] for r in refs]
        order = {hi.RESOLVED: 0, hi.IMPORT: 1, hi.NAME_MATCH: 2}
        self.assertEqual(confs, sorted(confs, key=lambda c: order[c]),
                         "точное знание должно идти первым")

    def test_works_without_any_optional_tool(self):
        # ast есть всегда: даже без ctags и tree-sitter индекс не пуст
        idx = hi.HybridIndex(self.root)
        self.assertTrue(idx.symbols)
        self.assertTrue(any("ast" in s for s in idx.sources))


@unittest.skipUnless(HAVE_CTAGS, "universal-ctags не установлен")
class TestCtagsLayer(HybridCase):
    def test_rust_symbols_come_from_ctags(self):
        idx = hi.HybridIndex(self.root)
        rust = [s for s in idx.symbols.values() if s.get("lang") == "Rust"]
        self.assertTrue(rust, "ctags обязан покрыть язык, которого нет у ast")
        self.assertTrue(any(s["name"] == "normalize" for s in rust))

    def test_rust_signature_includes_name(self):
        idx = hi.HybridIndex(self.root)
        sym = next(s for s in idx.symbols.values()
                   if s["name"] == "normalize" and s.get("lang") == "Rust")
        self.assertTrue(sym["sig"].startswith("normalize("),
                        "имя и сигнатура склеиваются: ctags отдаёт их раздельно")

    def test_imports_are_marked_as_import_not_call(self):
        idx = hi.HybridIndex(self.root)
        imports = [e for e in idx.edges if e["confidence"] == hi.IMPORT]
        self.assertTrue(imports, "ctags даёт импорты, и они не должны "
                                 "выдаваться за вызовы")

    # Проверка отбраковки Exuberant живёт в TestCtagsVersionGuard ниже.
    # Прежний тест здесь сводился к assertIsNone(None) и проходил всегда,
    # создавая видимость покрытия на месте настоящей дыры.


class TestHonesty(HybridCase):
    """Ключевое требование: не выдавать догадку за факт."""

    def test_impact_labels_confidence(self):
        idx = hi.HybridIndex(self.root)
        text = idx.impact("helper")
        self.assertIn("точно", text)

    def test_unknown_symbol_is_explicit(self):
        idx = hi.HybridIndex(self.root)
        self.assertIn("не найдено", idx.impact("nonexistent_symbol"))

    def test_report_lists_active_layers(self):
        idx = hi.HybridIndex(self.root)
        rep = idx.report()
        self.assertIn("symbols", rep)
        self.assertIn("by_confidence", rep)
        self.assertTrue(rep["sources"], "индекс обязан сообщать, чем построен")

    def test_map_marks_only_resolved_usage(self):
        idx = hi.HybridIndex(self.root)
        text = idx.project_map(budget=10)
        self.assertIn("helper", text)
        # счётчик «зовут» строится только на точных ссылках
        self.assertIn("[зовут: 1]", text)


class TestCtagsVersionGuard(unittest.TestCase):
    """`brew install ctags` ставит Exuberant 5.8 (2009) под тем же именем.

    Он не умеет ни JSON, ни ролей: если принять его за universal-ctags,
    индекс молча деградирует до плоского списка имён.
    """

    def setUp(self):
        self.orig_which = hi.shutil.which
        self.orig_run = hi.subprocess.run

    def tearDown(self):
        hi.shutil.which = self.orig_which
        hi.subprocess.run = self.orig_run

    def _fake_ctags(self, version_output, exe="/usr/bin/ctags"):
        hi.shutil.which = lambda name: exe if name == "ctags" else None

        def fake_run(cmd, **kw):
            return type("R", (), {"stdout": version_output, "returncode": 0})()
        hi.subprocess.run = fake_run

    def test_universal_accepted(self):
        self._fake_ctags("Universal Ctags 6.2.1, Copyright (C) 2015-2025")
        self.assertEqual(hi.have_ctags(), "/usr/bin/ctags")

    def test_exuberant_rejected(self):
        self._fake_ctags("Exuberant Ctags 5.8, Copyright (C) 1996-2009")
        self.assertIsNone(hi.have_ctags(),
                          "устаревшая реализация не должна приниматься")

    def test_bsd_ctags_rejected(self):
        self._fake_ctags("ctags (GNU) 1.0")
        self.assertIsNone(hi.have_ctags())

    def test_absent_ctags(self):
        hi.shutil.which = lambda name: None
        self.assertIsNone(hi.have_ctags())

    def test_broken_binary_does_not_raise(self):
        hi.shutil.which = lambda name: "/usr/bin/ctags"

        def boom(cmd, **kw):
            raise OSError("не запускается")
        hi.subprocess.run = boom
        self.assertIsNone(hi.have_ctags())


if __name__ == "__main__":
    unittest.main(verbosity=2)
