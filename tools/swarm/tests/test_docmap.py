"""Тесты механического детектора дрейфа docs↔code (E5, вариант B)."""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("docmap", ROOT_DIR / "docmap.py")
docmap = importlib.util.module_from_spec(spec)
sys.modules["docmap"] = docmap
spec.loader.exec_module(docmap)

MAP = """\
[[map]]
code = "src/loop.py"
docs = ["docs/design.md#Цикл работы", "docs/guide.md"]

[[map]]
code = "src/mem*.py"
docs = ["docs/design.md#Память"]
"""


class Fixture(unittest.TestCase):
    """Мини-репо: два модуля кода, два документа с заголовками."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        (root / "src" / "loop.py").write_text("x = 1\n", encoding="utf-8")
        (root / "src" / "memory.py").write_text("x = 1\n", encoding="utf-8")
        (root / "docs" / "design.md").write_text(
            "# Дизайн\n\n## 5. Цикл работы (конечный автомат)\n\n"
            "## 8.0. Знаниевая инфраструктура и память\n",
            encoding="utf-8",
        )
        (root / "docs" / "guide.md").write_text("# Гайд\n", encoding="utf-8")
        (root / docmap.MAP_NAME).write_text(MAP, encoding="utf-8")
        self.root = root
        self.entries = docmap.load(root / docmap.MAP_NAME)

    def tearDown(self):
        self.tmp.cleanup()


class TestLoad(unittest.TestCase):
    """Формат карты: мусор отвергается по имени записи, не молча."""

    def _load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / docmap.MAP_NAME
            p.write_text(text, encoding="utf-8")
            return docmap.load(p)

    def test_valid_map_parses(self):
        entries = self._load(MAP)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].code, "src/loop.py")
        self.assertEqual(
            entries[0].docs[0], docmap.DocRef("docs/design.md", "Цикл работы")
        )
        self.assertEqual(entries[0].docs[1], docmap.DocRef("docs/guide.md", None))

    def test_empty_map_rejected(self):
        with self.assertRaises(ValueError):
            self._load("# пусто\n")

    def test_entry_without_code_rejected(self):
        with self.assertRaisesRegex(ValueError, "code"):
            self._load('[[map]]\ndocs = ["a.md"]\n')

    def test_entry_without_docs_rejected(self):
        with self.assertRaisesRegex(ValueError, "docs"):
            self._load('[[map]]\ncode = "a.py"\ndocs = []\n')

    def test_bad_ref_rejected(self):
        with self.assertRaises(ValueError):
            self._load('[[map]]\ncode = "a.py"\ndocs = [42]\n')

    def test_anchor_without_path_rejected(self):
        with self.assertRaises(ValueError):
            self._load('[[map]]\ncode = "a.py"\ndocs = ["#якорь"]\n')


class TestSuspects(Fixture):
    """Изменённые файлы → подозреваемые секции, механически."""

    def test_changed_file_flags_its_sections(self):
        hits = docmap.suspects(["src/loop.py"], self.entries)
        docs = {h.doc for h in hits}
        self.assertEqual(
            docs, {"docs/design.md#Цикл работы", "docs/guide.md"}
        )

    def test_glob_entry_matches(self):
        hits = docmap.suspects(["src/memory.py"], self.entries)
        self.assertEqual([h.doc for h in hits], ["docs/design.md#Память"])

    def test_uncovered_file_flags_nothing(self):
        self.assertEqual(docmap.suspects(["src/other.py"], self.entries), [])

    def test_no_duplicate_per_doc_and_file(self):
        """Два glob'а на один файл не называют одну секцию дважды."""
        double = [
            *self.entries,
            docmap.Entry("src/*.py", (docmap.DocRef("docs/guide.md", None),)),
        ]
        hits = docmap.suspects(["src/loop.py"], double)
        self.assertEqual(
            [h.doc for h in hits].count("docs/guide.md"), 1
        )


class TestCheck(Fixture):
    """Состояние самой карты: три механических класса предупреждений."""

    def test_healthy_map_is_quiet(self):
        self.assertEqual(docmap.check(self.root, self.entries), [])

    def test_dead_glob_warns(self):
        entries = [
            *self.entries,
            docmap.Entry("src/gone*.py", (docmap.DocRef("docs/guide.md", None),)),
        ]
        warnings = docmap.check(self.root, entries)
        self.assertTrue(any("glob" in w for w in warnings))

    def test_missing_doc_warns(self):
        entries = [
            docmap.Entry("src/loop.py", (docmap.DocRef("docs/none.md", None),))
        ]
        warnings = docmap.check(self.root, entries)
        self.assertTrue(any("не найден" in w for w in warnings))

    def test_missing_anchor_warns(self):
        entries = [
            docmap.Entry(
                "src/loop.py",
                (docmap.DocRef("docs/design.md", "Такого раздела нет"),),
            )
        ]
        warnings = docmap.check(self.root, entries)
        self.assertTrue(any("якорь" in w for w in warnings))

    def test_anchor_matching_is_case_insensitive(self):
        """Якорь в другом регистре — тот же заголовок, предупреждения нет."""
        entries = [
            docmap.Entry(
                "src/loop.py",
                (docmap.DocRef("docs/design.md", "цикл РАБОТЫ"),),
            )
        ]
        self.assertEqual(docmap.check(self.root, entries), [])


class TestRealMap(unittest.TestCase):
    """Карта этого репозитория — живая: check на ней молчит."""

    def test_repo_docmap_has_no_warnings(self):
        repo = pathlib.Path(__file__).resolve().parents[3]
        map_path = repo / docmap.MAP_NAME
        if not map_path.is_file():
            self.skipTest("docmap.toml в корне репо нет")
        entries = docmap.load(map_path)
        self.assertEqual(docmap.check(repo, entries), [])


if __name__ == "__main__":
    unittest.main()
