"""Гейт: измеренная длительность и отказ от повторного прогона по тому же дереву.

На стенде cod-doc (прогон 2026-09-23) каждая задача гоняла полный сьют
трижды по ~140 с — baseline, после исполнителя и перед подтверждающим
ревью, — и два прогона из трёх шли по дереву, которое уже прошло гейт.
Доска при этом рисовала гейт засечкой: метрика не несла длительности, и
минуты сьюта выглядели пустотой между исполнителем и ревью.
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

GATE = [sys.executable, "-c", "print('suite ok')"]


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "mod_a.py").write_text("def alpha():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root,
                       check=True)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": []})
        self.runs = 0

    def loop(self, config=None, agents=None):
        loop = lp.Loop(self.state, {"gate_command": GATE, **(config or {})},
                       agents, ui=lambda *a: None)
        orig = loop.sh

        def counting_sh(cmd, timeout=900):
            if cmd == GATE:
                self.runs += 1
            return orig(cmd, timeout)

        loop.sh = counting_sh
        return loop

    def gate_metrics(self):
        rows = [json.loads(line) for line in
                self.state.metrics_path.read_text().splitlines()]
        return [m for m in rows if m.get("phase") == "gate"]


class TestGateDuration(RepoCase):
    def test_gate_metric_carries_measured_duration(self):
        slow = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        self.loop({"gate_command": slow}).gate({"id": "t"})
        (m,) = self.gate_metrics()
        self.assertIn("dur_s", m, "без длительности доска рисует засечку")
        self.assertIsInstance(m["dur_s"], float)
        self.assertGreater(m["dur_s"], 0.0)


class TestGateReuse(RepoCase):
    def test_unchanged_tree_is_not_rerun(self):
        loop = self.loop()
        self.assertEqual(loop.gate({"id": "t"}), (True, "suite ok"))
        self.assertEqual(loop.gate({"id": "t"}), (True, "suite ok"))
        self.assertEqual(self.runs, 1)
        last = self.gate_metrics()[-1]
        self.assertTrue(last["reused"])
        self.assertTrue(last["ok"])

    def test_tracked_edit_forces_rerun(self):
        loop = self.loop()
        loop.gate({"id": "t"})
        (self.root / "mod_a.py").write_text("def alpha():\n    return 2\n")
        loop.gate({"id": "t"})
        self.assertEqual(self.runs, 2)

    def test_untracked_file_forces_rerun(self):
        loop = self.loop()
        loop.gate({"id": "t"})
        (self.root / "new_mod.py").write_text("x = 1\n")
        loop.gate({"id": "t"})
        self.assertEqual(self.runs, 2)

    def test_loop_state_writes_do_not_invalidate(self):
        """Журнал петли меняется каждой метрикой — в отпечаток он не входит,
        иначе повторное использование не срабатывало бы никогда.
        Самоигнор `.swarm/` через info/exclude в git-worktree не действует
        (стенд cod-doc — worktree), поэтому проверяется и неигнорируемый
        случай."""
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.write_text("")
        (self.root / "swarm.toml").write_text("max_iterations = 3\n")
        loop = self.loop()
        loop.gate({"id": "t"})
        self.state.metric(task="t", phase="review", ok=True)
        (self.root / "swarm.toml").write_text("max_iterations = 4\n")
        loop.gate({"id": "t"})
        self.assertEqual(self.runs, 1)

    def test_red_is_never_reused(self):
        red = [sys.executable, "-c", "raise SystemExit(1)"]
        loop = self.loop({"gate_command": red})
        orig = loop.sh
        seen = []
        loop.sh = lambda cmd, timeout=900: (seen.append(cmd), orig(cmd, timeout))[1]
        self.assertFalse(loop.gate({"id": "t"})[0])
        self.assertFalse(loop.gate({"id": "t"})[0])
        self.assertEqual(seen.count(red), 2)

    def test_other_command_forces_rerun(self):
        loop = self.loop()
        loop.gate({"id": "t"})
        loop.config["gate_command"] = [sys.executable, "-c", "print('other')"]
        loop.gate({"id": "t"})
        self.assertFalse(self.gate_metrics()[-1].get("reused", False))

    def test_opt_out(self):
        loop = self.loop({"gate_reuse": False})
        loop.gate({"id": "t"})
        loop.gate({"id": "t"})
        self.assertEqual(self.runs, 2)

    def test_executor_run_drops_the_cache(self):
        """Исполнитель может менять игнорируемые файлы, которых отпечаток
        не видит: после его запуска прошлый зелёный — не справка."""
        agents = type("A", (), {
            "implement": staticmethod(lambda t, f, i: {"status": "done"}),
            "last_implement_failure": None})()
        loop = self.loop(agents=agents)
        loop.gate({"id": "t"})
        loop._implement_with_quota_wait({"id": "t"}, None, 1)
        loop.gate({"id": "t"})
        self.assertEqual(self.runs, 2)


class TestBoard(unittest.TestCase):
    ROWS = [
        {"ts": "2026-09-23T10:35:50+00:00", "task": "k7p2", "phase": "gate",
         "ok": True, "dur_s": 183.2},
        {"ts": "2026-09-23T10:38:46+00:00", "task": "k7p2", "phase": "gate",
         "ok": True, "reused": True, "dur_s": 0.0},
    ]

    def test_gate_is_a_segment_not_a_mark(self):
        board = _load("board")
        run, reused = board._timeline(self.ROWS)
        self.assertEqual(run["dur"], 183.2)
        self.assertAlmostEqual(run["t1"] - run["t0"], 183.2, places=3)
        self.assertTrue(reused["reused"])
        self.assertFalse(run["reused"])

    def test_totals_count_reused_gates(self):
        board = _load("board")
        gate = board._totals(self.ROWS, [])["gate"]
        self.assertEqual(gate, {"ok": 2, "fail": 0, "reused": 1})


class TestConfirmationRound(RepoCase):
    """Сквозной путь: baseline → исполнитель → гейт → approve → подтверждение.
    Подтверждающий раунд не зовёт исполнителя, дерево то же — сьют второй
    раз не гоняется."""

    def test_confirmation_round_reuses_the_gate(self):
        def implement(task, feedback, iteration):
            (self.root / "mod_a.py").write_text("def alpha():\n    return 7\n")
            return {"status": "done", "summary": "поменял alpha"}

        def review(task, tail, iteration, confirming=False, **kw):
            return {"analysis": "Разобрал дифф построчно и сверил с задачей.",
                    "verdict": "approve",
                    "summary": "Изменение соответствует задаче.",
                    "findings": [], "out_of_scope_notes": []}

        agents = type("A", (), {
            "implement": staticmethod(implement),
            "review": staticmethod(review),
            "last_tuning": {},
            "last_implement_failure": None,
            "commit_message": staticmethod(lambda t, d: "m")})()
        loop = self.loop({"confirmations": 2}, agents)
        task = {"id": "aaaa", "title": "t", "type": "feature",
                "paths": ["mod_a.py"]}
        self.state.save_tasks({"goal": "g", "tasks": [
            {**task, "status": "pending", "deps": []}]})
        self.assertEqual(loop.run_task(task), "done")
        gates = self.gate_metrics()
        self.assertEqual(len(gates), 3, gates)
        self.assertEqual(self.runs, 2, "подтверждение гоняло сьют заново")
        self.assertEqual([bool(m.get("reused")) for m in gates],
                         [False, False, True])


if __name__ == "__main__":
    unittest.main()
