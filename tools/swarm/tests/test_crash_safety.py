#!/usr/bin/env python3
"""Тесты живучести: авария не должна ни терять работу, ни запирать задачу.

Аудит вскрыл два места, где сбой оборачивался ущербом, непропорциональным
причине. Первое: `set_status(in_progress)` не был защищён, поэтому таймаут
гейта, отказ по квоте или сетевой сбой оставляли задачу в `in_progress`
навсегда — `ready_tasks` берёт только `pending`, `retry` требовал
`blocked`, `resume` не чинил. Единственным выходом была ручная правка
JSON. Второе: PREFLIGHT §5.1 не был реализован, а петля откатывает файлы
и делает `git add -A` — незакоммиченная работа человека уничтожалась
безвозвратно.
"""
import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)
st_mod = cli.state_mod
lp = cli.loop_mod


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "a.py").write_text("def f():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = st_mod.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": [
            {"id": "aaaa", "title": "задача", "status": "pending", "deps": [],
             "type": "feature", "paths": ["a.py"]}]})

    def tearDown(self):
        self.tmp.cleanup()

    def status_of(self, tid="aaaa"):
        return next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == tid)["status"]


class TestCrashDoesNotStrandTask(RepoCase):
    """Задача обязана остаться разбираемой при любой аварии."""

    def _loop_that_explodes(self, exc):
        agents = type("A", (), {
            "implement": staticmethod(lambda *a, **k: (_ for _ in ()).throw(exc)),
            "commit_message": staticmethod(lambda task, diff: "m")})()
        loop = lp.Loop(self.state, {}, agents, ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        return loop

    def test_gate_timeout_leaves_task_blocked_not_in_progress(self):
        loop = self._loop_that_explodes(
            subprocess.TimeoutExpired("pytest", 900))
        loop.run()
        self.assertEqual(self.status_of(), "blocked",
                         "задача застряла в in_progress — вернуть её можно "
                         "было бы только правкой JSON")

    def test_quota_failure_leaves_task_blocked(self):
        loop = self._loop_that_explodes(RuntimeError("session limit reached"))
        loop.run()
        self.assertEqual(self.status_of(), "blocked")

    def test_crash_reason_is_recorded(self):
        loop = self._loop_that_explodes(RuntimeError("сеть отвалилась"))
        loop.run()
        journal = self.state.journal_path.read_text()
        self.assertIn("task_crashed", journal)
        self.assertIn("сеть отвалилась", journal)

    def test_crash_creates_question_for_human(self):
        loop = self._loop_that_explodes(RuntimeError("бум"))
        loop.run()
        self.assertTrue(self.state.questions(only_open=True),
                        "человек должен узнать об аварии из инбокса")

    def test_crash_stops_the_run(self):
        """Падать по кругу на каждой задаче — хуже, чем остановиться."""
        data = self.state.load_tasks()
        data["tasks"].append({"id": "bbbb", "title": "вторая", "status": "pending",
                              "deps": [], "type": "feature", "paths": ["b.py"]})
        self.state.save_tasks(data)
        loop = self._loop_that_explodes(RuntimeError("бум"))
        results = loop.run()
        self.assertEqual(len(results), 1)
        self.assertEqual(self.status_of("bbbb"), "pending",
                         "вторая задача не должна быть тронута")

    def test_keyboard_interrupt_also_rescues(self):
        loop = self._loop_that_explodes(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            loop.run()
        self.assertEqual(self.status_of(), "blocked")


class TestRetryUnsticksTask(RepoCase):
    def test_retry_accepts_in_progress(self):
        self.state.set_status("aaaa", "in_progress")
        code, _ = run_cli("--root", str(self.root), "retry", "aaaa")
        self.assertEqual(code, 0)
        self.assertEqual(self.status_of(), "pending")

    def test_retry_still_accepts_blocked(self):
        self.state.set_status("aaaa", "blocked", reason="dispute")
        code, _ = run_cli("--root", str(self.root), "retry", "aaaa")
        self.assertEqual(code, 0)
        self.assertEqual(self.status_of(), "pending")

    def test_retry_refuses_done(self):
        self.state.set_status("aaaa", "done")
        code, _ = run_cli("--root", str(self.root), "retry", "aaaa")
        self.assertEqual(code, 2, "закрытую задачу не возвращают вслепую")


class TestPreflight(RepoCase):
    """§5.1: грязное дерево — отказ, а не риск потери работы."""

    def test_dirty_tree_refuses_run(self):
        (self.root / "a.py").write_text("работа человека\n")
        code, out = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, 2)
        self.assertIn("PREFLIGHT", out)
        self.assertIn("a.py", out, "человек должен видеть, что именно мешает")

    def test_untracked_file_also_blocks(self):
        (self.root / "черновик.md").write_text("заметки\n")
        code, out = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, 2)

    def test_clean_tree_passes_preflight(self):
        code, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertNotIn("PREFLIGHT", out)

    def test_force_overrides_but_is_logged(self):
        (self.root / "a.py").write_text("работа человека\n")
        code, out = run_cli("--root", str(self.root), "run", "--dry-run", "--force")
        self.assertNotEqual(code, 2)
        self.assertIn("preflight_forced", self.state.journal_path.read_text(),
                      "осознанный риск обязан остаться в журнале")

    def test_swarm_state_does_not_trip_preflight(self):
        """Собственное состояние петли не считается чужой работой."""
        self.state.log("x", note="что-то")
        code, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertNotIn("PREFLIGHT", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
