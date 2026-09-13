#!/usr/bin/env python3
"""Smoke-тест `swarm task` (E2-B): add/split/close/list/check через валидатор.

Главные свойства: невалидная задача от человека не попадает в очередь,
расщепление оставляет исходную blocked и называет части, ручная правка
tasks.json видна по следу целостности в журнале.
"""
import contextlib
import importlib.util
import io
import json
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


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class TaskCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "README").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = st_mod.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": []})

    def tearDown(self):
        self.tmp.cleanup()

    def tasks(self):
        return self.state.load_tasks()["tasks"]


class TestAdd(TaskCliCase):
    def test_add_feature_with_schema(self):
        code, out = run_cli("--root", str(self.root), "task", "add",
                            "первый модуль", "--type", "feature",
                            "--path", "src/a.py", "--acceptance",
                            "pytest проходит")
        self.assertEqual(code, 0, out)
        (t,) = self.tasks()
        self.assertRegex(t["id"], r"^[A-Za-z0-9]{4}$")
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["paths"], ["src/a.py"])

    def test_add_without_paths_rejected(self):
        code, out = run_cli("--root", str(self.root), "task", "add", "голая")
        self.assertEqual(code, 2)
        self.assertIn("paths", out)
        self.assertEqual(self.tasks(), [])

    def test_add_idea_needs_no_paths(self):
        code, out = run_cli("--root", str(self.root), "task", "add",
                            "замечание на будущее", "--type", "idea")
        self.assertEqual(code, 0, out)

    def test_add_dep_on_missing_task_rejected(self):
        code, out = run_cli("--root", str(self.root), "task", "add", "с dep",
                            "--path", "a.py", "--acceptance", "ок",
                            "--dep", "zzzz")
        self.assertEqual(code, 2)
        self.assertIn("не существует", out)

    def test_ids_do_not_collide(self):
        run_cli("--root", str(self.root), "task", "add", "одна",
                "--path", "a.py", "--acceptance", "ок")
        run_cli("--root", str(self.root), "task", "add", "одна",
                "--path", "b.py", "--acceptance", "ок")
        ids = [t["id"] for t in self.tasks()]
        self.assertEqual(len(ids), len(set(ids)))


class TestSplitCloseCheck(TaskCliCase):
    def _add(self, title="крупная"):
        code, out = run_cli("--root", str(self.root), "task", "add", title,
                            "--path", "src/big.py", "--acceptance", "ок")
        self.assertEqual(code, 0, out)
        return self.tasks()[-1]["id"]

    def test_split_marks_source_blocked(self):
        tid = self._add()
        code, out = run_cli("--root", str(self.root), "task", "split", tid,
                            "--part", "половина раз", "--part", "половина два")
        self.assertEqual(code, 0, out)
        tasks = {t["id"]: t for t in self.tasks()}
        self.assertEqual(tasks[tid]["status"], "blocked")
        parts = tasks[tid]["split_into"]
        self.assertEqual(len(parts), 2)
        for p in parts:
            self.assertEqual(tasks[p]["paths"], ["src/big.py"])
            self.assertEqual(tasks[p]["status"], "pending")

    def test_split_single_part_rejected(self):
        tid = self._add()
        code, _ = run_cli("--root", str(self.root), "task", "split", tid,
                          "--part", "одна часть")
        self.assertEqual(code, 2)

    def test_close_pending(self):
        tid = self._add()
        code, out = run_cli("--root", str(self.root), "task", "close", tid,
                            "--note", "решено руками")
        self.assertEqual(code, 0, out)
        (t,) = [t for t in self.tasks() if t["id"] == tid]
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["note"], "решено руками")

    def test_close_warns_about_dependents(self):
        tid = self._add()
        run_cli("--root", str(self.root), "task", "add", "зависимая",
                "--path", "b.py", "--acceptance", "ок", "--dep", tid)
        code, out = run_cli("--root", str(self.root), "task", "close", tid)
        self.assertEqual(code, 0, out)
        self.assertIn("зависят", out)

    def test_check_clean_queue(self):
        self._add()
        code, out = run_cli("--root", str(self.root), "task", "check")
        self.assertEqual(code, 0, out)
        self.assertIn("валидна", out)

    def test_check_sees_hand_edit(self):
        # Правка в обход API: отпечаток файла расходится с объявленным
        # в журнале — именно это и должен ловить E2-B.
        self._add()
        path = self.state.tasks_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["title"] = "переименована руками"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
        code, out = run_cli("--root", str(self.root), "task", "check")
        self.assertEqual(code, 2)
        self.assertIn("в обход API", out)

    def test_check_sees_schema_drift(self):
        tid = self._add()
        path = self.state.tasks_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["paths"] = []
        # Запись через API, чтобы след целостности сходился: тогда check
        # обязан сказать про схему, а не про отпечаток.
        self.state.save_tasks(data)
        code, out = run_cli("--root", str(self.root), "task", "check")
        self.assertEqual(code, 2)
        self.assertIn(f"{tid}: ", out)
        self.assertNotIn("в обход API", out)

    def test_list(self):
        self._add()
        code, out = run_cli("--root", str(self.root), "task", "list")
        self.assertEqual(code, 0, out)
        self.assertIn("крупная", out)
        self.assertIn("pending", out)


if __name__ == "__main__":
    unittest.main()
