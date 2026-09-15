#!/usr/bin/env python3
"""Парный стенд (pair.py): два арм-исполнителя на одной задаче, одни метрики.

Проверяются те свойства, поломка которых не падает, а портит ЗАМЕР:
армы отличаются ровно переданными перекрытиями и работают в РАЗНЫХ
worktree, метрики обеих армов различимы в общем файле, падение одного
арма не роняет второй, а общее дерево и очередь стенд не трогает.
"""
import contextlib
import importlib.util
import inspect
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import pair  # noqa: E402
import state as state_mod  # noqa: E402

TASK = {"id": "p1", "title": "парная задача", "paths": ["a.py"],
        "type": "feature"}
CFG_A = {"experiments": {"memory": "executor"}, "gate_command": ["true"]}
CFG_B = {"experiments": {"memory": "off"}, "gate_command": ["true"]}


def _repo():
    root = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n")
    for cmd in (["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True)
    return root


class FakeAgents:
    """Исполнитель-двойник: пишет в СВОЁ дерево, помнит свой конфиг."""

    last_implement_failure = None

    def __init__(self, state=None, config=None, boom=False):
        self.state = state
        self.config = config or {}
        self.work_root = getattr(state, "root", None)
        self.log_tag = ""
        self.boom = boom

    def implement(self, task, feedback, iteration):
        if self.boom:
            raise RuntimeError("арм-авария")
        mode = (self.config.get("experiments") or {}).get("memory")
        with self._lock:
            self.calls.append((mode, str(self.work_root), self.log_tag))
        # Работа арма идёт в его worktree: изоляция сломана — увидит
        # проверка общего дерева ниже.
        pathlib.Path(self.work_root, "written.py").write_text(f"# {mode}\n")
        return {"status": "done", "summary": f"плечо {mode}"}


def _patch_agents(testcase, calls, boom_arms=()):
    import agents as agents_mod

    lock = threading.Lock()

    class Stub(FakeAgents):
        _lock = lock

        def __init__(self, state=None, config=None):
            super().__init__(state, config)

    def factory(state=None, config=None):
        memory = (config or {}).get("experiments", {}).get("memory")
        arm = "b" if memory == "off" else "a"
        stub = Stub(state, config)
        stub.calls = calls
        stub.boom = arm in boom_arms
        return stub

    real = agents_mod.Agents
    agents_mod.Agents = factory
    testcase.addCleanup(lambda: setattr(agents_mod, "Agents", real))


def _metrics(st, phase):
    rows = []
    for line in st.metrics_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("phase") == phase:
            rows.append(row)
    return rows


class TestParseOverride(unittest.TestCase):

    def test_plain_string_value(self):
        path, value = pair.parse_override("executor_engine=claude")
        self.assertEqual(path, ("executor_engine",))
        self.assertEqual(value, "claude")

    def test_json_values_keep_type(self):
        self.assertEqual(pair.parse_override("fill_num_predict=8000")[1], 8000)
        self.assertIs(pair.parse_override("live_board=true")[1], True)
        self.assertEqual(
            pair.parse_override('gate_command=["true"]')[1], ["true"])

    def test_dotted_key_path(self):
        path, value = pair.parse_override("experiments.memory=executor")
        self.assertEqual(path, ("experiments", "memory"))
        self.assertEqual(value, "executor")

    def test_malformed_refuses(self):
        for spec in ("без-равно", "=x", "ключ=", "..a=1", " a =1"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                pair.parse_override(spec)


class TestArmConfig(unittest.TestCase):

    def test_override_merges_without_mutating_base(self):
        cfg = {"experiments": {"memory": "off"}, "gate_command": ["true"]}
        arm = pair.arm_config(cfg, [pair.parse_override(
            "experiments.memory=executor")])
        self.assertEqual(arm["experiments"]["memory"], "executor")
        self.assertEqual(cfg["experiments"]["memory"], "off",
                         "базовый конфиг обязан остаться для второго арма")
        self.assertIsNot(arm, cfg)

    def test_same_key_twice_last_wins(self):
        arm = pair.arm_config({}, [pair.parse_override("a.b=1"),
                                   pair.parse_override("a.b=2")])
        self.assertEqual(arm["a"]["b"], 2)


class TestRunPairTask(unittest.TestCase):
    """Сквозная проверка стенда: оба арма, разные деревья, общие метрики."""

    def setUp(self):
        self.root = _repo()
        self.st = state_mod.SwarmState(self.root)
        self.calls = []
        _patch_agents(self, self.calls)

    def _run(self):
        return pair.run_pair_task(self.st, CFG_A, CFG_B, TASK,
                                  overrides={"a": [pair.parse_override(
                                      "experiments.memory=executor")],
                                             "b": [pair.parse_override(
                                                 "experiments.memory=off")]})

    def test_both_arms_run_with_their_config_in_their_tree(self):
        facts = self._run()
        self.assertEqual(len(self.calls), 2, "оба арма обязаны быть вызваны")
        self.assertEqual({c[0] for c in self.calls}, {"executor", "off"},
                         "армы обязаны отличаться значением фактора")
        roots = {c[1] for c in self.calls}
        self.assertEqual(len(roots), 2, "армы обязаны работать в РАЗНЫХ "
                                        "деревьях")
        self.assertTrue(all(r.startswith(str(self.root)) for r in roots),
                        "worktree арма обязан жить под корнем репозитория")
        self.assertEqual({c[2] for c in self.calls}, {"-pair-a", "-pair-b"},
                         "метки сырья обязаны различать армы")
        self.assertTrue(facts["a"]["report"])
        self.assertTrue(facts["b"]["report"])
        self.assertIs(facts["a"]["gate"], True)
        self.assertIs(facts["b"]["gate"], True)
        self.assertFalse((self.root / "written.py").exists(),
                         "общее дерево стенд не трогает")

    def test_worktrees_are_dropped(self):
        self._run()
        arms = self.st.dir / "arms"
        leftovers = [p.name for p in arms.glob("p1-*")] if arms.exists() else []
        self.assertEqual(leftovers, [],
                         f"worktree армов остались на диске: {leftovers}")

    def test_metrics_are_unified_and_arm_tagged(self):
        self._run()
        rows = _metrics(self.st, "pair")
        self.assertEqual({r["arm"] for r in rows}, {"a", "b"},
                         "метрики обеих армов — одна фаза, различимые арм-поля")
        for row in rows:
            with self.subTest(arm=row["arm"]):
                self.assertIn("gate", row)
                self.assertIn("diff", row)
                self.assertIn("wall_s", row)
        journal = self.st.journal_path.read_text()
        for kind in ("pair_start", "pair_arm", "pair_done"):
            self.assertIn(kind, journal)

    def test_pair_start_logs_overrides_not_full_config(self):
        self._run()
        start = None
        for line in self.st.journal_path.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "pair_start":
                start = row
        self.assertIsNotNone(start)
        self.assertEqual(start["arms"]["a"], {"experiments.memory": "executor"})
        self.assertNotIn("gate_command", json.dumps(start["arms"]))

    def test_render_shows_both_arms_and_gate_divergence(self):
        facts = self._run()
        facts["b"]["gate"] = False
        text = pair.render(TASK, facts)
        self.assertIn("арм A", text)
        self.assertIn("арм B", text)
        self.assertIn("расхождение гейта: есть", text)
        self.assertIn("не тронуты", text)


class TestBrokenInstrument(unittest.TestCase):
    """Падение арма — факт о замере, а не повод ронять второй арм."""

    def setUp(self):
        self.root = _repo()
        self.st = state_mod.SwarmState(self.root)
        self.calls = []
        _patch_agents(self, self.calls, boom_arms=("b",))

    def test_arm_a_survives_arm_b_crash(self):
        facts = pair.run_pair_task(self.st, CFG_A, CFG_B, TASK)
        self.assertTrue(facts["a"]["report"])
        self.assertIn("error", facts["b"])
        self.assertIn("арм-авария", facts["b"]["error"])
        self.assertIn("pair_arm_failed", self.st.journal_path.read_text())
        self.assertTrue(facts["a"]["report"],
                        "замер односторонним стать не обязан")

    def test_render_marks_broken_arm(self):
        facts = pair.run_pair_task(self.st, CFG_A, CFG_B, TASK)
        text = pair.render(TASK, facts)
        self.assertIn("ПРИБОР СЛОМАН", text)


class TestArmTaggingContract(unittest.TestCase):
    """Тот же контракт, что чинили для дуэли: деньги прибора отличимы.

    Без метки плеча в метрике `phase="implement"` траты двух армов
    сливаются в одну сумму, в которой замер неотличим от работы
    (findings.jsonl, INFRA, 2026-08-24) — поэтому проверка по исходнику,
    как у дуэли: функционально это видно только на настоящем вызове.
    """

    def test_executor_tags_metric_rows_by_log_tag(self):
        import executor

        src = inspect.getsource(executor.implement)
        self.assertIn('arm = "shadow"', src)
        self.assertIn("arm=arm", src)
        self.assertIn('tag.startswith("-")', src,
                      "метки парного стенда обязаны попадать в arm по тому "
                      "же правилу, что и метка дуэли")


ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)
clirun = sys.modules["clirun"]


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class TestPairCommand(unittest.TestCase):
    """Команда одной строкой: отказы на входе и отдача фактов наружу."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root,
                       check=True)
        self.st = state_mod.SwarmState(self.root)
        self.st.save_tasks({"goal": "g", "tasks": [dict(TASK, status="pending",
                                                         deps=[])]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_task_refuses(self):
        code, out = run_cli("--root", str(self.root), "pair", "zzzz")
        self.assertEqual(code, 2)
        self.assertIn("не найдена", out)

    def test_malformed_override_refuses(self):
        code, out = run_cli("--root", str(self.root), "pair", "p1",
                            "--set-a", "без-равно")
        self.assertEqual(code, 2)
        self.assertIn("перекрытие", out)

    def test_unknown_config_key_refuses(self):
        code, out = run_cli("--root", str(self.root), "pair", "p1",
                            "--set-a", "max_iteration=5")
        self.assertEqual(code, 2)
        self.assertIn("не читает", out)

    def test_duel_flag_in_override_refuses(self):
        code, out = run_cli("--root", str(self.root), "pair", "p1",
                            "--set-a", "experiments.duel=tester")
        self.assertEqual(code, 2)
        self.assertIn("жребий", out)

    def test_duel_factor_in_config_refuses(self):
        (self.root / "swarm.toml").write_text(
            '[experiments]\nduel = "tester"\n', encoding="utf-8")
        code, out = run_cli("--root", str(self.root), "pair", "p1")
        self.assertEqual(code, 2)
        self.assertIn("не читает фактор", out)

    def test_ambient_factor_in_config_refuses(self):
        (self.root / "swarm.toml").write_text(
            '[experiments]\nambient = "tester"\n', encoding="utf-8")
        code, out = run_cli("--root", str(self.root), "pair", "p1")
        self.assertEqual(code, 2)
        self.assertIn("не читает фактор", out)

    def test_happy_path_runs_both_arms_one_command(self):
        seen = {}

        def fake_run_pair_task(st, cfg_a, cfg_b, task, overrides=None):
            seen["cfgs"] = (cfg_a, cfg_b)
            seen["task"] = task
            seen["overrides"] = overrides
            return {"a": {"arm": "a", "report": True, "gate": True,
                          "diff": {"files": 1, "added": 2, "removed": 0},
                          "wall_s": 3.0},
                    "b": {"arm": "b", "report": True, "gate": False,
                          "diff": {"files": 1, "added": 1, "removed": 1},
                          "wall_s": 5.0}}

        real = clirun.pair.run_pair_task
        clirun.pair.run_pair_task = fake_run_pair_task
        self.addCleanup(lambda: setattr(clirun.pair, "run_pair_task", real))
        code, out = run_cli("--root", str(self.root), "pair", "p1",
                            "--set-a", "experiments.memory=executor")
        self.assertEqual(code, 0, out)
        self.assertIn("pair p1", out)
        self.assertIn("расхождение гейта: есть", out)
        cfg_a, cfg_b = seen["cfgs"]
        self.assertEqual(cfg_a["experiments"]["memory"], "executor")
        self.assertNotIn("experiments", cfg_b,
                         "без --set-b арм B обязан ехать на базовом конфиге "
                         "без единого добавленного ключа")
        self.assertEqual(seen["overrides"]["a"],
                         [(("experiments", "memory"), "executor")])
        self.assertEqual(seen["task"]["id"], "p1")

    def test_json_flag_prints_machine_readable_facts(self):
        def fake_run_pair_task(st, cfg_a, cfg_b, task, overrides=None):
            return {"a": {"arm": "a", "report": True},
                    "b": {"arm": "b", "error": "RuntimeError: упало"}}

        real = clirun.pair.run_pair_task
        clirun.pair.run_pair_task = fake_run_pair_task
        self.addCleanup(lambda: setattr(clirun.pair, "run_pair_task", real))
        code, out = run_cli("--root", str(self.root), "pair", "p1", "--json")
        self.assertEqual(code, 0, out)
        line = next(ln for ln in out.splitlines() if ln.startswith("{"))
        facts = json.loads(line)
        self.assertEqual(facts["a"]["arm"], "a")
        self.assertIn("error", facts["b"])


if __name__ == "__main__":
    unittest.main()
