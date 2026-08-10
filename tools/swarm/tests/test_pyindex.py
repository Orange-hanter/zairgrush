#!/usr/bin/env python3
"""Тесты точного индекса Python (SCIP-подход на stdlib `ast`).

Модуль оказался полностью непокрытым: при консолидации тесты наивной
карты были удалены вместе с ней, а на пришедший ей на смену индекс их не
написали. Мутационный аудит показал это прямо — все пять внесённых
дефектов выжили.

Цена дефекта здесь особая: индекс не падает, а тихо врёт. Ревьюер
построит finding на ложной связи, планировщик — `paths` задачи.
"""
import ast
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("pyindex", ROOT_DIR / "pyindex.py")
px = importlib.util.module_from_spec(spec)
sys.modules["pyindex"] = px
spec.loader.exec_module(px)

PROJECT = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        '"""Ядро."""\n\n\n'
        'def helper(x: int) -> int:\n'
        '    """Служебная."""\n'
        '    return x + 1\n\n\n'
        'def api(data: str) -> int:\n'
        '    """Вход."""\n'
        '    return helper(len(data.strip()))\n'
    ),
    "pkg/extra.py": (
        'from pkg.core import api\n'
        'from pkg import core\n\n\n'
        'def direct(s):\n'
        '    """Прямой вызов импортированного имени."""\n'
        '    return api(s)\n\n\n'
        'def via_module(s):\n'
        '    """Вызов через модуль."""\n'
        '    return core.helper(len(s))\n'
    ),
    "tests/test_core.py": (
        'from pkg.core import api\n\n\n'
        'def test_api():\n'
        '    assert api("x")\n'
    ),
    "broken.py": "def oops(:\n    pass\n",
}


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for rel, src in PROJECT.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        self.idx = px.Index(self.root)

    def tearDown(self):
        self.tmp.cleanup()


class TestSymbols(IndexCase):
    def test_qualified_ids(self):
        """Символы различаются по модулю, а не по голому имени."""
        self.assertIn("pkg.core.helper", self.idx.symbols)
        self.assertIn("pkg.extra.direct", self.idx.symbols)

    def test_signature_and_doc(self):
        sym = self.idx.symbols["pkg.core.api"]
        self.assertEqual(sym.sig, "api(data) -> int")
        self.assertEqual(sym.doc, "Вход.")

    def test_broken_file_does_not_break_index(self):
        """Один синтаксически битый файл не должен ронять весь индекс."""
        self.assertIn("pkg.core.api", self.idx.symbols)

    def test_same_name_in_two_modules_stays_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text("def run():\n    pass\n")
            (root / "b.py").write_text("def run():\n    pass\n")
            idx = px.Index(root)
            self.assertIn("a.run", idx.symbols)
            self.assertIn("b.run", idx.symbols)


class TestReferenceResolution(IndexCase):
    """Главное свойство точного индекса — разрешение имён."""

    def _callers(self, sid):
        return {r.from_symbol for r in self.idx.callers(sid)}

    def test_direct_call_resolved_through_import(self):
        self.assertIn("pkg.extra.direct", self._callers("pkg.core.api"))

    def test_call_via_module_resolved(self):
        callers = self._callers("pkg.core.helper")
        self.assertIn("pkg.extra.via_module", callers)

    def test_local_call_resolved(self):
        self.assertIn("pkg.core.api", self._callers("pkg.core.helper"))

    def test_method_of_object_is_not_a_project_symbol(self):
        """`data.strip()` не должен считаться вызовом функции проекта.

        Ровно этим наивная карта и врала: strip, upper, append попадали в
        граф вызовов наравне с настоящими символами.
        """
        names = {r.symbol_id for r in self.idx.references}
        self.assertFalse(any(n.endswith(".strip") for n in names))

    def test_reference_records_position_and_kind(self):
        ref = next(r for r in self.idx.callers("pkg.core.api"))
        self.assertTrue(ref.file.endswith("extra.py"))
        self.assertGreater(ref.line, 0)
        self.assertIn(ref.how, ("direct", "via-module", "resolved-by-name"))

    def test_import_resolution_beats_name_guessing(self):
        """Разрешение импортов проверяется там, где угадывание по имени
        бессильно: два одноимённых символа в разных модулях.

        В простом проекте поломку маскирует fallback «единственный
        кандидат по имени» — мутационный аудит на этом и поскользнулся.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text("def run():\n    return 1\n")
            (root / "b.py").write_text("def run():\n    return 2\n")
            (root / "c.py").write_text(
                "from a import run\n\n\ndef use():\n    return run()\n")
            idx = px.Index(root)
            callers = {r.from_symbol for r in idx.callers("a.run")}
            self.assertEqual(callers, {"c.use"},
                             "вызов обязан привязаться к импортированному "
                             "модулю, а не к однофамильцу")
            self.assertEqual(idx.callers("b.run"), [],
                             "чужой одноимённый символ не должен получать "
                             "ложную ссылку")

    def test_aliased_import_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.py").write_text("def run():\n    return 1\n")
            (root / "b.py").write_text("def run():\n    return 2\n")
            (root / "c.py").write_text(
                "from b import run as go\n\n\ndef use():\n    return go()\n")
            idx = px.Index(root)
            self.assertEqual({r.from_symbol for r in idx.callers("b.run")},
                             {"c.use"}, "псевдоним импорта тоже разрешается")

    def test_unresolvable_name_is_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "m.py").write_text(
                "import os\n\n\ndef f():\n    return os.getpid()\n")
            idx = px.Index(root)
            self.assertEqual(idx.references, [],
                             "вызовы внешних библиотек не выдумываются")


class TestTestDetection(IndexCase):
    def test_tests_are_marked(self):
        self.assertTrue(self.idx.is_test("tests.test_core.test_api"))
        self.assertFalse(self.idx.is_test("pkg.core.api"))

    def test_map_excludes_tests(self):
        text = self.idx.project_map(budget=20)
        self.assertIn("api(", text)
        self.assertNotIn("test_api", text)

    def test_impact_separates_tests_from_production(self):
        text = self.idx.impact(["pkg.core.api"])
        self.assertIn("extra.py", text)
        self.assertIn("тесты:", text, "тестовые вызовы сворачиваются в счётчик")


class TestRanking(IndexCase):
    def test_called_symbol_ranks_above_caller(self):
        rank = self.idx.rank()
        self.assertGreater(rank["pkg.core.helper"], rank["pkg.extra.direct"],
                           "востребованный символ важнее вызывающего")

    def test_ranking_uses_incoming_references(self):
        """Ранг обязан зависеть от связей, а не быть равномерным."""
        rank = self.idx.rank()
        self.assertGreater(max(rank.values()), min(rank.values()))

    def test_ranking_reacts_to_added_caller(self):
        """Сильная проверка формулы: добавление вызывающего обязано
        поднять ранг символа относительно остальных."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "core.py").write_text(
                "def target():\n    return 1\n\n\ndef other():\n    return 2\n")
            (root / "one.py").write_text(
                "from core import target\n\n\ndef a():\n    return target()\n")
            before = px.Index(root).rank()
            (root / "two.py").write_text(
                "from core import target\n\n\ndef b():\n    return target()\n")
            (root / "three.py").write_text(
                "from core import target\n\n\ndef c():\n    return target()\n")
            after = px.Index(root).rank()
            self.assertGreater(after["core.target"] / after["core.other"],
                               before["core.target"] / before["core.other"],
                               "новые вызывающие обязаны поднимать ранг")

    def test_budget_limits_map(self):
        self.assertLessEqual(len(self.idx.project_map(budget=1).splitlines()), 2)

    def test_empty_project_is_safe(self):
        with tempfile.TemporaryDirectory() as empty:
            idx = px.Index(empty)
            self.assertEqual(idx.project_map(), "")
            self.assertEqual(idx.rank(), {})


class TestImpactOutput(IndexCase):
    def test_unknown_symbol_reported(self):
        self.assertIn("не найдены", self.idx.impact(["pkg.core.nope"]))

    def test_symbol_without_callers(self):
        text = self.idx.impact(["pkg.extra.direct"])
        self.assertIn("вызовов в проекте нет", text)

    def test_resolve_short_name(self):
        self.assertEqual(self.idx.resolve("api"), ["pkg.core.api"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
