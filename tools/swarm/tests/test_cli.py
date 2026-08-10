#!/usr/bin/env python3
"""Тесты единой точки входа: команды, коды возврата, dry-run.

Агенты не вызываются: проверяется поведение оркестратора вокруг них.
"""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import contextlib

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)

TASKS = {"goal": "тестовая цель", "tasks": [
    {"id": "aaaa", "title": "первая", "type": "feature", "status": "pending",
     "deps": [], "paths": ["src/a.py"], "acceptance": ["тесты проходят"]},
    {"id": "bbbb", "title": "вторая", "type": "feature", "status": "pending",
     "deps": ["aaaa"], "paths": ["src/b.py"], "acceptance": ["тесты проходят"]},
]}


def run_cli(*argv):
    """Вызов CLI с перехватом вывода -> (код возврата, текст)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("def a():\n    return 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = cli.state_mod.SwarmState(self.root)
        self.state.save_tasks(json.loads(json.dumps(TASKS)))

    def tearDown(self):
        self.tmp.cleanup()


class TestStatus(CliCase):
    def test_shows_goal_and_tasks(self):
        code, out = run_cli("--root", str(self.root), "status")
        self.assertEqual(code, 0)
        self.assertIn("тестовая цель", out)
        self.assertIn("aaaa", out)

    def test_empty_queue_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out = run_cli("--root", empty, "status")
            self.assertEqual(code, 0)
            self.assertIn("пуст", out)

    def test_reports_unfinished_steps_after_crash(self):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        code, out = run_cli("--root", str(self.root), "status")
        self.assertIn("НЕЗАВЕРШЁННЫЕ", out)
        self.assertIn("resume", out)

    def test_shows_blocked_reason_and_stash(self):
        self.state.set_status("aaaa", "blocked", reason="dispute",
                              stash="swarm:aaaa-dispute")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("dispute", out)
        self.assertIn("swarm:aaaa-dispute", out)


class TestDryRun(CliCase):
    def test_lists_only_ready_tasks(self):
        code, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("aaaa", out)
        self.assertNotIn("bbbb", out, "задача с незакрытой зависимостью "
                                      "не должна попадать в план прогона")

    def test_shows_baseline_gate(self):
        _, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertIn("baseline gate", out)

    def test_agents_are_not_invoked(self):
        # если бы агенты вызывались, команда не уложилась бы в секунды
        # и потребовала бы kimi/claude в PATH
        code, _ = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(code, 0)

    def test_nothing_ready_returns_distinct_code(self):
        for tid in ("aaaa", "bbbb"):
            self.state.set_status(tid, "done")
        code, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(code, 3, "пустая выборка — отдельный код, не тишина")


class TestResume(CliCase):
    def test_unfinished_step_escalates(self):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        code, out = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 2, "возобновление с незавершённым шагом "
                                  "требует решения человека")
        self.assertIn("эскалация", out)

    def test_force_proceeds(self):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        code, _ = run_cli("--root", str(self.root), "resume", "--dry-run",
                          "--force")
        self.assertEqual(code, 0)

    def test_clean_state_resumes_without_force(self):
        code, _ = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 0)


class TestDoctor(CliCase):
    def test_reports_environment(self):
        code, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("окружение", out)
        self.assertIn("git", out)

    def test_detects_clean_worktree(self):
        _, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("чист", out)

    def test_detects_dirty_worktree(self):
        (self.root / "src" / "new.py").write_text("x = 1\n")
        _, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("изменённых файлов", out)


class TestReport(CliCase):
    def test_renders_journal(self):
        self.state.log("dispute", task="aaaa", report={"status": "dispute"})
        code, out = run_cli("--root", str(self.root), "report")
        self.assertEqual(code, 0)
        self.assertIn("dispute", out)

    def test_filters_by_task(self):
        self.state.log("dispute", task="aaaa")
        self.state.log("dispute", task="bbbb")
        _, out = run_cli("--root", str(self.root), "report", "--task", "aaaa")
        self.assertIn("aaaa", out)
        self.assertNotIn("bbbb", out)


class TestLocking(CliCase):
    def test_second_run_is_refused(self):
        other = cli.state_mod.SwarmState(self.root)
        other.acquire()
        try:
            code, out = run_cli("--root", str(self.root), "run", "--dry-run")
            self.assertEqual(code, 2)
            self.assertIn("занято", out)
        finally:
            other.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
