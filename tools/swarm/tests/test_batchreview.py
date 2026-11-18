"""Пакетное ревью по майлстоуну (§5.8, `review_mode = "batch"`).

Полевая сессия ZeusLogic (2026-09-20): ревью — 68 % денег петли, цена
вызова почти не зависит от диффа (38 строк — $0.82, 1114 — $1.82).
Режим платит за контекст один раз на майлстоун, а не на задачу. Тесты
держат его обещания: задача без риска не зовёт ревьюера, пакет смотрится
одним вызовом по общему диффу, находка возвращается задачей-исправлением
с немедленным ревью, сорвавшийся пакет не теряет пометок и не крутится.
"""

import importlib.util
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


st = _load("state")
lp = _load("loop")
br = _load("batchreview")

GATE = [sys.executable, "-c", "print('suite ok')"]

APPROVE = {"analysis": "Разобрал дифф построчно и сверил с задачами пакета.",
           "verdict": "approve", "summary": "Пакет соответствует задачам.",
           "findings": [], "out_of_scope_notes": []}


def finding(file, task=None, category="correctness", severity="major"):
    f = {"file": file, "line": 1, "severity": severity, "category": category,
         "confidence": 0.9, "issue": f"ошибка в {file}",
         "suggestion": "поправить"}
    if task:
        f["task"] = task
    return f


def changes(*findings):
    return {"analysis": "Разобрал дифф построчно и сверил с задачами пакета.",
            "verdict": "request_changes",
            "summary": "Есть замечания к работе пакета.",
            "findings": list(findings), "out_of_scope_notes": []}


class Agents:
    """Исполнитель пишет файл задачи; ревьюер отвечает по очереди."""

    def __init__(self, root, verdicts=()):
        self.root = root
        self.verdicts = list(verdicts)
        self.reviews = []
        self.implemented = []
        self.last_tuning = {}
        self.last_implement_failure = None
        self.last_review_failure = None

    def implement(self, task, feedback, iteration):
        self.implemented.append(task["id"])
        for rel in task["paths"]:
            path = self.root / rel
            old = path.read_text() if path.exists() else ""
            path.write_text(old + f"# {task['id']} i{iteration}\n")
        return {"status": "done", "summary": "сделано"}

    def review(self, task, tail, iteration, confirming=False, diff=None, **kw):
        self.reviews.append({"task": task["id"], "diff": diff,
                             "batch": task.get("batch")})
        if not self.verdicts:
            return dict(APPROVE)
        v = self.verdicts.pop(0)
        if v is None:
            self.last_review_failure = "invalid"
        return v

    def commit_message(self, task, diff):
        return f"{task['id']}: {task['title']}"


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(".swarm/\n")
        (self.root / "a.py").write_text("A = 1\n")
        (self.root / "b.py").write_text("B = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root,
                       check=True)
        self.state = st.SwarmState(self.root)

    def tasks(self, *tasks):
        self.state.save_tasks({"goal": "g", "tasks": [
            {"type": "feature", "status": "pending", "deps": [],
             "acceptance": ["работает"], **t} for t in tasks]})

    def run_loop(self, agents, **config):
        cfg = {"gate_command": GATE, "review_mode": "batch",
               "confirmations": 1, **config}
        loop = lp.Loop(self.state, cfg, agents, ui=lambda *a: None)
        return loop.run()

    def task(self, tid):
        return next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == tid)

    def journal(self, kind):
        rows = [json.loads(line) for line in
                self.state.journal_path.read_text().splitlines()]
        return [r for r in rows if r.get("kind") == kind]


class TestImmediateReason(unittest.TestCase):
    CFG = {"review_mode": "batch"}

    def diff(self, path):
        return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                f"@@ -1 +1 @@\n-x\n+y\n")

    def test_task_mode_never_defers(self):
        self.assertEqual(br.immediate_reason(
            {}, {"milestone": "M1"}, self.diff("a.py")), "mode")

    def test_no_milestone_means_no_batch(self):
        self.assertEqual(br.immediate_reason(
            self.CFG, {}, self.diff("a.py")), "no_milestone")

    def test_fix_task_is_reviewed_at_once(self):
        self.assertEqual(br.immediate_reason(
            self.CFG, {"milestone": "M1", "fix_of": "aaaa"},
            self.diff("a.py")), "fix")

    def test_dependency_manifest_is_risk(self):
        self.assertEqual(br.immediate_reason(
            self.CFG, {"milestone": "M1"}, self.diff("pyproject.toml")),
            "new_dependency")

    def test_protected_path_is_risk(self):
        cfg = {**self.CFG, "protected_paths": ["migrations/**"]}
        self.assertEqual(br.immediate_reason(
            cfg, {"milestone": "M1"}, self.diff("migrations/0001.sql")),
            "protected")

    def test_plain_task_defers(self):
        self.assertIsNone(br.immediate_reason(
            self.CFG, {"milestone": "M1"}, self.diff("a.py")))


class TestAttribution(unittest.TestCase):
    TASKS = [{"id": "aaaa"}, {"id": "bbbb"}]
    OWNER = {"a.py": "aaaa", "b.py": "bbbb"}

    def test_reviewer_named_task_wins(self):
        got = br.attribute([finding("a.py", task="bbbb")], self.TASKS, self.OWNER)
        self.assertEqual(list(got), ["bbbb"])

    def test_unknown_task_falls_back_to_file_owner(self):
        got = br.attribute([finding("a.py", task="zzzz")], self.TASKS, self.OWNER)
        self.assertEqual(list(got), ["aaaa"])

    def test_unowned_file_goes_to_last_task(self):
        got = br.attribute([finding("c.py")], self.TASKS, self.OWNER)
        self.assertEqual(list(got), ["bbbb"])


class TestFixTask(unittest.TestCase):
    def test_fix_task_shape(self):
        orig = {"id": "aaaa", "title": "t", "paths": ["a.py"], "commit": "abc"}
        fix = br.fix_task(orig, "M1", [finding("c.py")], {"aaaa"})
        self.assertEqual(fix["fix_of"], "aaaa")
        self.assertEqual(fix["review"], "task")
        self.assertEqual(fix["milestone"], "M1")
        self.assertIn("c.py", fix["paths"], "страж границ откатит правку")
        self.assertIn("приёмка исходной задачи aaaa не нарушена",
                      fix["acceptance"])
        self.assertNotIn(fix["id"], {"aaaa"})


class TestBatchTask(unittest.TestCase):
    def test_milestone_close_carries_over_to_the_batch(self):
        """verification="milestone" проверяет закрывающую задачу; её ревью
        теперь — ревью пакета, и флаг обязан доехать до него."""
        tasks = [{"id": "aaaa", "title": "a", "commit": "1"},
                 {"id": "bbbb", "title": "b", "commit": "2",
                  "milestone_close": True}]
        self.assertTrue(br.batch_task("M1", tasks, {})["milestone_close"])
        self.assertNotIn("milestone_close", br.batch_task("M1", tasks[:1], {}))

    def test_acceptance_is_labelled_by_task(self):
        tasks = [{"id": "aaaa", "title": "a", "acceptance": ["x"]}]
        self.assertEqual(br.batch_task("M1", tasks, {})["acceptance"],
                         ["[aaaa] x"])


class TestBatchApprove(RepoCase):
    def test_milestone_is_reviewed_once_with_both_changes(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]},
                   {"id": "bbbb", "title": "tb", "milestone": "M1",
                    "paths": ["b.py"]})
        agents = Agents(self.root)
        self.run_loop(agents)
        self.assertEqual(len(agents.reviews), 1, agents.reviews)
        call = agents.reviews[0]
        self.assertEqual(call["batch"], ["aaaa", "bbbb"])
        self.assertIn("# aaaa i1", call["diff"])
        self.assertIn("# bbbb i1", call["diff"])
        for tid in ("aaaa", "bbbb"):
            t = self.task(tid)
            self.assertEqual(t["status"], "done")
            self.assertNotIn("review_pending", t)
            self.assertEqual(t["batch_review"]["verdict"], "approve")
        (row,) = self.journal("batch_review")
        self.assertEqual(row["verdict"], "approve")
        self.assertEqual(len(self.journal("review_deferred")), 2)

    def test_task_mode_reviews_every_task(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]},
                   {"id": "bbbb", "title": "tb", "milestone": "M1",
                    "paths": ["b.py"]})
        agents = Agents(self.root)
        self.run_loop(agents, review_mode="task")
        self.assertEqual([r["task"] for r in agents.reviews], ["aaaa", "bbbb"])
        self.assertTrue(all(r["diff"] is None for r in agents.reviews))
        self.assertEqual(self.journal("review_deferred"), [])

    def test_task_without_milestone_is_reviewed_at_once(self):
        self.tasks({"id": "aaaa", "title": "ta", "paths": ["a.py"]})
        agents = Agents(self.root)
        self.run_loop(agents)
        self.assertEqual([r["task"] for r in agents.reviews], ["aaaa"])
        self.assertIsNone(agents.reviews[0]["diff"])

    def test_dependency_change_is_reviewed_at_once(self):
        (self.root / "pyproject.toml").write_text("[project]\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "deps"], cwd=self.root,
                       check=True)
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["pyproject.toml"]})
        agents = Agents(self.root)
        self.run_loop(agents)
        self.assertEqual([r["task"] for r in agents.reviews], ["aaaa"])
        (row,) = self.journal("review_immediate")
        self.assertEqual(row["why"], "new_dependency")


class TestBatchFindings(RepoCase):
    def test_finding_becomes_fix_task_with_its_own_review(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]},
                   {"id": "bbbb", "title": "tb", "milestone": "M1",
                    "paths": ["b.py"]})
        agents = Agents(self.root, [changes(finding("b.py", task="bbbb"))])
        self.run_loop(agents)
        fixes = [t for t in self.state.load_tasks()["tasks"]
                 if t.get("fix_of")]
        self.assertEqual(len(fixes), 1)
        fix = fixes[0]
        self.assertEqual(fix["fix_of"], "bbbb")
        self.assertEqual(fix["status"], "done")
        self.assertIn(fix["id"], agents.implemented)
        # Пакет — один вызов с диффом; исправление — своё ревью по
        # рабочему дереву, без пакета.
        self.assertEqual([r["diff"] is None for r in agents.reviews],
                         [False, True])
        self.assertEqual(agents.reviews[1]["task"], fix["id"])
        self.assertEqual(self.task("aaaa")["batch_review"]["verdict"], "approve")
        b = self.task("bbbb")["batch_review"]
        self.assertEqual((b["verdict"], b["fix"]), ("request_changes", fix["id"]))
        self.assertEqual(len(self.journal("batch_fix_created")), 1)

    def test_intent_finding_goes_to_inbox(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]})
        agents = Agents(self.root, [changes(
            finding("a.py", task="aaaa", category="architecture"))])
        self.run_loop(agents)
        (fix,) = [t for t in self.state.load_tasks()["tasks"]
                  if t.get("fix_of")]
        self.assertEqual((fix["status"], fix["reason"]), ("blocked", "ask_user"))
        self.assertTrue(fix.get("question_id"))
        self.assertNotIn(fix["id"], agents.implemented,
                         "замысел решает человек, не исполнитель")
        (q,) = self.state.questions(only_open=True)
        self.assertEqual(q["task"], fix["id"])


class TestBatchFailure(RepoCase):
    def test_failed_review_keeps_marks_and_does_not_spin(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]})
        agents = Agents(self.root, [None, None, None])
        self.run_loop(agents)
        self.assertEqual(len(agents.reviews), 1, "повтор — следующим go")
        self.assertEqual(self.task("aaaa")["review_pending"], "M1")
        (row,) = self.journal("batch_review_failed")
        self.assertEqual(row["why"], "invalid")
        # Следующий прогон берёт пакет снова.
        agents.verdicts = []
        self.run_loop(agents)
        self.assertEqual(len(agents.reviews), 2)
        self.assertNotIn("review_pending", self.task("aaaa"))


class TestBatchCap(RepoCase):
    def test_oversized_batch_is_reviewed_before_milestone_closes(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]},
                   {"id": "bbbb", "title": "tb", "milestone": "M1",
                    "paths": ["b.py"], "deps": ["aaaa"]})
        agents = Agents(self.root)
        order = []
        impl, rev = agents.implement, agents.review
        agents.implement = lambda t, f, i: (order.append(("impl", t["id"])),
                                            impl(t, f, i))[1]
        agents.review = lambda t, *a, **k: (order.append(("review", t["id"])),
                                            rev(t, *a, **k))[1]
        self.run_loop(agents, batch_max_lines=1)
        self.assertEqual(order, [("impl", "aaaa"), ("review", "batch-M1"),
                                 ("impl", "bbbb"), ("review", "batch-M1")])

    def test_open_work_holds_the_batch(self):
        self.tasks({"id": "aaaa", "title": "ta", "milestone": "M1",
                    "paths": ["a.py"]},
                   {"id": "bbbb", "title": "tb", "milestone": "M1",
                    "paths": ["b.py"], "deps": ["aaaa"]})
        agents = Agents(self.root)
        self.run_loop(agents)
        self.assertEqual(len(agents.reviews), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
