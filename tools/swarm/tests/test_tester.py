#!/usr/bin/env python3
"""Независимый тестировщик (E11, плечо B) — за флагом и без поблажек.

Замер сравнивает двух авторов тестов, поэтому цена ошибки здесь не
«роль работает плохо», а «роль работает не тем, чем объявлена». Три
свойства держат плечо B плечом B: тестировщик не видит реализации
(структурно — он идёт ДО исполнителя), исполнитель не может переписать
его тесты (иначе это плечо A с лишним вызовом), и выключенный флаг
означает сегодняшнее поведение байт-в-байт.
"""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ts = _load("tester")
st = _load("state")
lp = _load("loop")

TASK = {"id": "t1", "title": "нормализация", "type": "feature-tests",
        "status": "pending", "deps": [],
        "spec": "normalize_text(s) приводит строку к сравнимому виду",
        "paths": ["wordstat/normalize.py", "tests/test_normalize.py"],
        "acceptance": ["полный сьют проходит", "добавлены тесты"]}


class TestWhenTheArmApplies(unittest.TestCase):
    """Плечо B стоит денег: звать тестировщика туда, где тестов не ждут,
    значит платить за пустой вызов."""

    def test_off_by_default(self):
        self.assertFalse(ts.wants_tester({}, TASK))
        self.assertFalse(ts.enabled({}))

    def test_on_for_feature_tests(self):
        self.assertTrue(ts.wants_tester({"experiments": {"tester": True}}, TASK))

    def test_plain_feature_carries_no_tests(self):
        """Обычный feature тесты не несёт: их пишет соседняя задача или
        они уже есть."""
        task = dict(TASK, type="feature")
        self.assertFalse(ts.wants_tester({"experiments": {"tester": True}}, task))

    def test_task_without_test_paths_is_skipped(self):
        task = dict(TASK, paths=["wordstat/normalize.py"])
        self.assertFalse(ts.wants_tester({"experiments": {"tester": True}}, task))

    def test_test_files_are_told_from_code_mechanically(self):
        """Разделять код и тесты по списку задачи нельзя — он один. Иначе
        тестировщик получил бы право писать реализацию."""
        self.assertEqual(ts.test_paths(TASK), ["tests/test_normalize.py"])

    def test_glob_is_not_a_target(self):
        """По маске писать нечего: тестировщик обязан знать ИМЯ файла."""
        task = dict(TASK, paths=["src/a.py", "tests/test_*.py"])
        self.assertEqual(ts.test_paths(task), [])


class TestPromptIsAboutCatchingMistakes(unittest.TestCase):
    """Формулировка и есть механизм (урок ADR-005): «покрой поведение»
    давало тесты, повторяющие docstring."""

    def _text(self, task=None):
        task = task or TASK
        return ts.prompt(task, "цель прогона", ts.test_paths(task), "pytest -q")

    def test_asks_for_tests_that_catch_a_wrong_implementation(self):
        text = self._text()
        self.assertIn("catch a WRONG implementation", text)
        self.assertIn("not tests that restate the", text)

    def test_says_the_implementation_does_not_exist(self):
        """Независимость структурная, и тестировщик должен это знать:
        иначе он пойдёт искать реализацию, которой ещё нет."""
        self.assertIn("DOES NOT EXIST YET", self._text())

    def test_names_only_the_test_files(self):
        text = self._text()
        self.assertIn("tests/test_normalize.py", text)
        self.assertNotIn("wordstat/normalize.py", text,
                         "реализацию тестировщику не показывают и писать "
                         "её он не имеет права")

    def test_ambiguity_goes_to_unclear_not_to_a_guess(self):
        """Спецификация, не решающая случай, — находка о постановке.
        Угаданный ответ превратил бы её в закреплённую выдумку."""
        text = self._text()
        self.assertIn("unclear", text)
        self.assertIn("instead of\n  inventing an answer", text)

    def test_forbids_making_tests_green_by_skipping(self):
        self.assertIn("never add skips or xfail", self._text())

    def test_does_not_demand_that_every_test_start_red(self):
        """Раунд 1 E11: прежнее «тесты обязаны падать сегодня» ложно для
        задач, которые сохраняют поведение и меняют способ (кэш, ускорение,
        рефакторинг). Буквальное послушание заставило бы выдумывать
        искусственные падения."""
        text = self._text()
        self.assertIn("PRESERVES behaviour", text)
        self.assertIn("pass before AND\n  after", text)
        self.assertIn("Never invent an artificial failure", text)

    def test_carries_acceptance_and_spec(self):
        text = self._text()
        self.assertIn("полный сьют проходит", text)
        self.assertIn("normalize_text(s)", text)


class TestAuthoredTestsAreOffLimits(unittest.TestCase):
    """Главное свойство плеча: исполнитель не переписывает чужой тест.

    Без него независимость кончается на первом же неудобном случае —
    исполнитель поправит проверку под свою реализацию, и плечо B станет
    плечом A с лишним вызовом.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "tests").mkdir()
        (self.root / "wordstat").mkdir()
        (self.root / "wordstat" / "normalize.py").write_text("x = 1\n")
        (self.root / "tests" / "test_normalize.py").write_text("# тест\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root,
                       check=True)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "ц", "tasks": [dict(TASK)]})
        self.loop = lp.Loop(self.state, {"protected_paths": ["tests/**"]}, None)
        self.loop.pre_existing = set()
        self.loop.run_dirt = set()

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, rel, text):
        (self.root / rel).write_text(text, encoding="utf-8")

    def test_executor_may_edit_its_own_tests_in_arm_a(self):
        """Плечо A не изменилось: `paths` называет тест явно — значит он
        свой, и правка законна (урок приёмки)."""
        self.loop._authored_tests = []
        self._touch("tests/test_normalize.py", "# правка исполнителя\n")
        ok, bad, tests = self.loop.scope_check(dict(TASK))
        self.assertTrue(ok, f"плечо A сломано: {bad} {tests}")

    def _authored(self, rel):
        """Пометить файл написанным тестировщиком — по его содержимому."""
        return {rel: self.loop.file_fingerprint(rel)}

    def test_executor_may_not_edit_independently_written_tests(self):
        self.loop._authored_tests = self._authored("tests/test_normalize.py")
        self._touch("tests/test_normalize.py", "# правка исполнителя\n")
        ok, _bad, tests = self.loop.scope_check(dict(TASK))
        self.assertFalse(ok)
        self.assertIn("tests/test_normalize.py", tests)

    def test_untouched_authored_test_is_not_a_violation(self):
        """Файл тестировщика лежит незакоммиченным и потому есть в списке
        изменений ВСЕГДА. Без сверки по содержимому страж срывал бы
        каждый раунд на собственной же подготовке."""
        self._touch("tests/test_normalize.py", "# написал тестировщик\n")
        self.loop._authored_tests = self._authored("tests/test_normalize.py")
        ok, bad, tests = self.loop.scope_check(dict(TASK))
        self.assertTrue(ok, f"{bad} {tests}")

    def test_the_implementation_stays_editable(self):
        """Запрет узкий: код задачи исполнитель правит как обычно."""
        self.loop._authored_tests = self._authored("tests/test_normalize.py")
        self._touch("wordstat/normalize.py", "x = 2\n")
        ok, bad, tests = self.loop.scope_check(dict(TASK))
        self.assertTrue(ok, f"{bad} {tests}")


class TestDegradationIsHonest(unittest.TestCase):
    """Отказ тестировщика не роняет задачу, но и не выдаётся за успех."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "ц", "tasks": [dict(TASK)]})

    def tearDown(self):
        self.tmp.cleanup()

    def _loop(self, report, write_file=False):
        calls = []

        def fake(agents, task, goal, suite):
            calls.append((task["id"], goal, suite))
            if write_file:
                (self.root / "tests").mkdir(exist_ok=True)
                (self.root / "tests" / "test_normalize.py").write_text("# t\n")
            return report

        orig = ts.write_tests
        ts.write_tests = fake
        self.addCleanup(lambda: setattr(ts, "write_tests", orig))
        loop = lp.Loop(self.state, {"experiments": {"tester": True}}, None)
        self.calls = calls
        return loop

    def test_no_report_degrades_to_arm_a_and_says_so(self):
        loop = self._loop(None)
        self.assertEqual(loop._author_tests(dict(TASK)), {})
        journal = self.state.journal_path.read_text()
        self.assertIn("tests_not_authored", journal)
        self.assertIn("плечо B выродилось в плечо A", journal)

    def test_report_without_files_is_not_success(self):
        """Отчёт «сделано» без файлов на диске — не сделано. Верить
        отчёту вместо дерева значит замерить обещание."""
        loop = self._loop({"status": "done", "summary": "готово"})
        self.assertEqual(loop._author_tests(dict(TASK)), {})
        self.assertIn("файлы не появились",
                      self.state.journal_path.read_text())

    def test_written_files_are_journalled_with_the_unclear_cases(self):
        loop = self._loop({"status": "done", "summary": "закреплено",
                           "cases": ["пустая строка", "юникод"],
                           "unclear": ["что делать с NBSP"]}, write_file=True)
        buf = io.StringIO()
        loop.ui = lambda *a: buf.write(" ".join(map(str, a)))
        written = loop._author_tests(dict(TASK))
        self.assertEqual(sorted(written), ["tests/test_normalize.py"])
        row = next(json.loads(x) for x in
                   self.state.journal_path.read_text().splitlines()
                   if "tests_authored" in x)
        self.assertEqual(row["cases"], 2)
        self.assertEqual(row["unclear"], ["что делать с NBSP"])
        self.assertIn("неясного в спеке", buf.getvalue())

    def test_flag_off_never_calls_the_tester(self):
        loop = self._loop({"status": "done", "summary": "s"})
        loop.config = {}
        self.assertEqual(loop._author_tests(dict(TASK)), {})
        self.assertEqual(self.calls, [], "выключенный флаг обязан молчать")


if __name__ == "__main__":
    unittest.main(verbosity=2)
