#!/usr/bin/env python3
"""Линтер границ задачи: спутники, о которых план обычно спотыкается.

Замер, из которого он вырос (золотой набор PILOT-1): СЕМЬ споров
исполнителя из семи признаны владельцем — граница задачи была
поставлена неверно, и каждый спор стоил раунда плюс ожидания ответа
(медиана 24 минуты, худшее 21 час).

Первая версия линтера искала одну улику — упоминание основы имени файла
задачи в чужом тексте — и на настоящем корпусе поймала 1 спор из 7 при
7,1 предупреждения на задачу. Замер вскрыл три дефекта, и каждый из них
закреплён здесь тестом: глоб выбрасывался вместо раскрытия; прямая
ссылка (файл задачи сам называет путь снаружи) не искалась вовсе;
пришпиленный эталон в 300 КБ не попадал даже в указатель имён, потому
что потолок чтения применялся к индексации.

Второе свойство, без которого линтер вреден, — молчание. Совет,
срабатывающий на каждой задаче, читать перестают, поэтому список
короткий по построению, а тесты требуют тишины там, где улик нет.
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
spec = importlib.util.spec_from_file_location("planner", ROOT_DIR / "planner.py")
pl = importlib.util.module_from_spec(spec)
sys.modules["planner"] = pl
spec.loader.exec_module(pl)


class LintCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def warn(self, paths, protected=("**/tests/**",), **kw):
        return pl.boundary_warnings(self.root, {"paths": list(paths)},
                                    list(protected), **kw)

    def files(self, warnings):
        return {w["file"] for w in warnings}


class TestPilotClasses(LintCase):
    """По кейсу на класс спора из золотого набора."""

    def test_registry_referrer_is_found(self):
        """q011: реестр правил в engine.rs звал новое правило, а engine.rs
        был вне границ — спор, раунд, ожидание человека."""
        self.write("crates/erc/src/rules/erc09.rs", "pub fn check() {}\n")
        self.write("crates/erc/src/engine.rs",
                   "use crate::rules::erc09;\nfn rules() { erc09::check(); }\n")
        w = self.warn(["crates/erc/src/rules/erc09.rs"])
        self.assertIn("crates/erc/src/engine.rs", self.files(w))

    def test_registry_is_recognised_by_calling_several_neighbours(self):
        """Реестр отличается от случайного упоминания тем, что зовёт
        СРАЗУ НЕСКОЛЬКО соседей — и обязан идти выше одиночной ссылки."""
        for n in ("erc01", "erc02", "erc09"):
            self.write(f"crates/erc/src/rules/{n}.rs", "pub fn check() {}\n")
        self.write("crates/erc/src/engine.rs",
                   "use crate::rules::{Erc01Rule, Erc02Rule, Erc09Rule};\n")
        self.write("crates/other/src/note.rs", "// про erc09 вскользь\n")
        w = self.warn(["crates/erc/src/rules/*.rs"])
        self.assertEqual(w[0]["file"], "crates/erc/src/engine.rs")
        self.assertEqual(w[0]["signal"], "registry")

    def test_case_does_not_hide_the_reference(self):
        """Файл зовут erc02.rs, а тип — Erc02Duplicate: на живом
        репозитории регистр съедал улику целиком (замер: q011 мимо)."""
        self.write("crates/erc/src/rules/erc02.rs", "pub struct X;\n")
        self.write("crates/erc/src/engine.rs",
                   "use crate::rules::Erc02DuplicateDesignator;\n")
        self.assertIn("crates/erc/src/engine.rs",
                      self.files(self.warn(["crates/erc/src/rules/erc02.rs"])))

    def test_forward_reference_from_the_task_file(self):
        """q012: тест внутри границ читает эталон снаружи и называет его
        путь буквально. Самая сильная улика — и её не искали вовсе."""
        self.write("crates/graph/tests/corpus_golden.rs",
                   'let p = "tests/fixtures/golden/nets.txt";\n')
        self.write("tests/fixtures/golden/nets.txt", "N1 A B\n")
        w = self.warn(["crates/graph/tests/*.rs"])
        self.assertEqual(w[0]["file"], "tests/fixtures/golden/nets.txt")
        self.assertEqual(w[0]["signal"], "forward")

    def test_big_pinned_reference_is_still_named(self):
        """Эталон почти всегда велик: в замере — 300 КБ. Потолок ЧТЕНИЯ
        не имеет права вычёркивать файл из указателя имён — назвать его
        спутником можно, не читая."""
        self.write("crates/graph/tests/corpus_golden.rs",
                   'let p = "tests/fixtures/golden/nets.txt";\n')
        self.write("tests/fixtures/golden/nets.txt",
                   "x" * (pl._MAX_FILE_BYTES + 1024))
        self.assertIn("tests/fixtures/golden/nets.txt",
                      self.files(self.warn(["crates/graph/tests/*.rs"])))

    def test_docs_pinning_the_format_is_found(self):
        """q014/q017: документ фиксировал замер и сам формат."""
        self.write("crates/model/src/keychain.rs", "pub fn terminal() {}\n")
        self.write("docs/04-project-format.md",
                   "§8 канонической строки: см. keychain.rs\n")
        w = self.warn(["crates/model/src/keychain.rs"])
        self.assertIn("docs/04-project-format.md", self.files(w))
        self.assertEqual(next(x["category"] for x in w
                              if x["file"].startswith("docs/")), "docs")

    def test_upstream_producer_is_found(self):
        """q005/q016: признак рождался в импортёре, вне границ задачи."""
        self.write("crates/model/src/components.rs", "pub struct Component;\n")
        self.write("crates/import-qet/src/convert.rs",
                   "use model::components::Component;\n")
        w = self.warn(["crates/model/src/components.rs"])
        self.assertIn("crates/import-qet/src/convert.rs", self.files(w))

    def test_pinned_directory_is_collapsed_into_one_warning(self):
        """q020: задача, правящая формат, называет по имени половину
        фикстуры. Десять строк про соседей одного каталога — это одна
        улика, и говорить о ней надо один раз (иначе спорный файл тонет
        в собственных соседях: замер поставил его на 22-е место)."""
        self.write("crates/model/src/save.rs",
                   'w("project.toml"); w("cables.toml"); w("suppressions.toml");\n')
        for name in ("project.toml", "cables.toml", "suppressions.toml"):
            self.write(f"tests/fixtures/demo/{name}", "x = 1\n")
        w = self.warn(["crates/model/src/save.rs"], protected=["tests/**"])
        self.assertIn("tests/fixtures/demo/", self.files(w))
        self.assertEqual(next(x["signal"] for x in w
                              if x["file"].endswith("demo/")), "pinned_dir")
        self.assertNotIn("tests/fixtures/demo/cables.toml", self.files(w),
                         "каталог назван — перечислять его файлы незачем")


class TestGlobsAreTheNormalCase(LintCase):
    """Реальные задачи пишут границу глобом — и это не «нет файлов»."""

    def test_glob_is_expanded_not_dropped(self):
        self.write("src/thing.rs", "pub fn x() {}\n")
        self.write("other/user.rs", "use thing;\n")
        self.assertIn("other/user.rs", self.files(self.warn(["src/**"])))

    def test_files_inside_an_expanded_glob_are_not_satellites(self):
        self.write("src/thing.rs", "pub fn x() {}\n")
        self.write("src/user.rs", "use thing;\n")
        self.assertEqual(self.warn(["src/**"]), [])


class TestLinterIsNotNoise(LintCase):
    """Совет, срабатывающий всегда, читать перестают."""

    def test_unrelated_files_are_silent(self):
        self.write("crates/erc/src/rules/erc09.rs", "pub fn check() {}\n")
        self.write("crates/other/src/painter.rs", "fn draw() {}\n")
        self.write("README.md", "проект про электрощиты\n")
        self.assertEqual(self.warn(["crates/erc/src/rules/erc09.rs"]), [])

    def test_own_paths_are_not_satellites(self):
        self.write("a/keychain.rs", "pub fn terminal() {}\n")
        self.write("a/suppress.rs", "use crate::keychain::terminal;\n")
        w = self.warn(["a/keychain.rs", "a/suppress.rs"])
        self.assertEqual(w, [], "файл внутри границ спутником не является")

    def test_generic_stems_do_not_fire(self):
        """`mod`, `lib`, `main`: по таким основам «ссылается» полрепозитория."""
        self.write("src/mod.rs", "pub fn x() {}\n")
        self.write("other/anything.rs", "// mod здесь просто слово\n")
        self.assertEqual(self.warn(["src/mod.rs"]), [])

    def test_word_common_across_the_repo_is_not_evidence(self):
        """«components» у половины файлов — общее слово, а не имя файла
        задачи. На широкой границе такие совпадения хоронили настоящих
        спутников под собой (замер: спорный файл на 22-м месте)."""
        self.write("src/components.rs", "pub struct C;\n")
        for i in range(40):
            self.write(f"other/f{i:02}.rs", "// components везде\n")
        self.write("registry/pins.rs", "use components; use widgets;\n")
        self.write("src/widgets.rs", "pub struct W;\n")
        w = self.warn(["src/components.rs", "src/widgets.rs"])
        self.assertLessEqual(len(w), pl._LIMIT)
        self.assertNotIn("other/f00.rs", self.files(w))

    def test_vendor_dirs_are_skipped(self):
        self.write("crates/erc/src/rules/erc09.rs", "pub fn check() {}\n")
        self.write("node_modules/pkg/erc09.js", "erc09\n")
        self.write("target/debug/erc09.txt", "erc09\n")
        self.assertEqual(self.warn(["crates/erc/src/rules/erc09.rs"]), [])

    def test_list_stays_short(self):
        """Потолок замерен, а не выбран: на корпусе PILOT-1 подъём с 3 до
        10 не добавил ни одного пойманного спора сверх пятого."""
        self.write("src/keychain.rs", "pub fn x() {}\n")
        for i in range(12):
            self.write(f"docs/d{i}.md", "смотри keychain\n")
        self.assertLessEqual(len(self.warn(["src/keychain.rs"])), pl._LIMIT)

    def test_warning_names_the_file_token_and_what_to_do(self):
        self.write("src/keychain.rs", "pub fn x() {}\n")
        self.write("tests/pinned.rs", "keychain == 42\n")
        w = self.warn(["src/keychain.rs"], protected=["tests/**"])
        self.assertEqual(w[0]["token"], "keychain")
        self.assertIn("PILOT-1", w[0]["hint"], "совет обязан назвать основание")


if __name__ == "__main__":
    unittest.main(verbosity=2)
