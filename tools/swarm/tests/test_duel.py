#!/usr/bin/env python3
"""Дуэль: два исполнителя на одной задаче, парно и одновременно.

Проверяется не «работает ли», а те два свойства, поломка которых не
падает, а портит ВЫБОРКУ: живое плечо выбрано жребием ЗАРАНЕЕ и не
переигрывается по результату, и теневое плечо физически не может
переписать работу живого.
"""
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import ambient  # noqa: E402
import duel  # noqa: E402
import loop as lp  # noqa: E402
import state as state_mod  # noqa: E402

DUEL = {"experiments": {"duel": "memory_executor", "ambient_seed": 7},
        "gate_command": ["true"]}
TASK = {"id": "d1", "title": "t", "paths": ["a.py"], "type": "feature"}


def _repo():
    root = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.py").write_text("x = 1\n")
    for cmd in (["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True)
    return root


class TestPlan(unittest.TestCase):

    def test_off_by_default(self):
        self.assertIsNone(duel.factor({}))
        self.assertIsNone(duel.plan({}, "d1"))

    def test_unknown_factor_refuses(self):
        with self.assertRaises(ValueError):
            duel.factor({"experiments": {"duel": "нет-такого"}})

    def test_arms_are_opposite_and_one_is_live(self):
        plan = duel.plan(DUEL, "d1")
        self.assertNotEqual(plan["live"]["arm"], plan["shadow"]["arm"])
        self.assertEqual({plan["live"]["arm"], plan["shadow"]["arm"]},
                         {"on", "off"})

    def test_live_arm_matches_the_ambient_draw(self):
        """Жребий один и тот же, что у фонового замера: наблюдения двух
        механик складываются в одну выборку, а не в две несравнимые.

        Первая редакция брала четыре id — и все четыре выпали в `on`,
        так что утверждение было пустым: мутация «живое плечо всегда on»
        его проходила. Мутационный аудит это и поймал. Поэтому здесь
        перебор, а НЕ горсть имён, и отдельно проверяется, что оба плеча
        вообще случаются живыми — детектор, не видевший обоих исходов,
        меряет себя.
        """
        cfg = {"experiments": {"ambient": "memory_executor",
                               "ambient_seed": 7}}
        seen = set()
        for i in range(60):
            tid = f"t{i:03d}"
            live = duel.plan(DUEL, tid)["live"]["arm"]
            self.assertEqual(live, ambient.arm(cfg, tid))
            seen.add(live)
        self.assertEqual(seen, {"on", "off"},
                         "живым обязано становиться каждое из плеч, иначе "
                         "жребия нет и сравнение односторонее")

    def test_arm_config_does_not_re_roll_inside_itself(self):
        """Плечо, унёсшее с собой флаг duel/ambient, разыграло бы фактор
        ещё раз — само от себя, и замер сравнил бы себя с собой."""
        plan = duel.plan(DUEL, "d1")
        for side in ("live", "shadow"):
            exp = plan[side]["config"]["experiments"]
            with self.subTest(side=side):
                self.assertNotIn("duel", exp)
                self.assertNotIn("ambient", exp)

    def test_conflicts_are_refused(self):
        self.assertIsNotNone(duel.conflict(
            {"experiments": {"duel": "tester", "tester": True}}))
        self.assertIsNotNone(duel.conflict(
            {"experiments": {"duel": "tester", "ambient": "tester"}}))
        self.assertIsNone(duel.conflict(DUEL))


class TestWorktreeIsolation(unittest.TestCase):

    def test_shadow_tree_is_a_separate_checkout_at_head(self):
        root = _repo()
        wt = duel.worktree(root, root / ".swarm", "d1")
        self.addCleanup(duel.drop_worktree, root, wt)
        self.assertTrue((wt / "a.py").exists())
        (wt / "a.py").write_text("x = 999\n")
        self.assertEqual((root / "a.py").read_text(), "x = 1\n",
                         "теневое плечо не имеет права тронуть общее дерево")

    def test_stat_counts_what_the_shadow_wrote(self):
        root = _repo()
        wt = duel.worktree(root, root / ".swarm", "d1")
        self.addCleanup(duel.drop_worktree, root, wt)
        (wt / "b.py").write_text("y = 2\ny = 3\n")
        stat = duel.shadow_diff_stat(wt)
        self.assertEqual(stat["files"], 1)
        self.assertEqual(stat["added"], 2)

    def test_worktree_is_recreated_not_reused(self):
        """Остатки прошлой задачи в теневом дереве — чужая работа,
        попавшая в замер, и заметить её было бы нечем."""
        root = _repo()
        first = duel.worktree(root, root / ".swarm", "d1")
        (first / "leftover.py").write_text("junk\n")
        second = duel.worktree(root, root / ".swarm", "d1")
        self.addCleanup(duel.drop_worktree, root, second)
        self.assertFalse((second / "leftover.py").exists())


class TestBothArmsActuallyRun(unittest.TestCase):
    """Сквозная проверка: два исполнителя, одна задача, оба вызваны."""

    def test_live_report_is_returned_and_shadow_only_measured(self):
        root = _repo()
        st = state_mod.SwarmState(root)
        calls, lock = [], threading.Lock()

        class FakeAgents:
            last_implement_failure = None

            def __init__(self, state=None, config=None):
                self.state = state or st
                self.config = config or DUEL
                self.work_root = getattr(state, "root", root)

            def implement(self, task, feedback, iteration):
                mode = (self.config.get("experiments") or {}).get("memory")
                with lock:
                    calls.append((mode, str(self.work_root)))
                # Теневое плечо пишет в СВОЁ дерево — если изоляция
                # сломана, это увидит проверка общего дерева ниже.
                pathlib.Path(self.work_root, "written.py").write_text(
                    f"# {mode}\n")
                return {"status": "done", "summary": f"плечо {mode}"}

        agents = FakeAgents(st, DUEL)
        loop = lp.Loop(st, DUEL, agents)
        import agents as agents_mod
        real = agents_mod.Agents
        agents_mod.Agents = FakeAgents
        self.addCleanup(lambda: setattr(agents_mod, "Agents", real))

        report = loop._implement_with_quota_wait(TASK, None, 1)

        self.assertEqual(len(calls), 2, "оба плеча обязаны быть вызваны")
        self.assertEqual({m for m, _ in calls}, {"executor", "off"},
                         "плечи обязаны отличаться значением фактора")
        roots = {r for _m, r in calls}
        self.assertEqual(len(roots), 2, "плечи обязаны работать в РАЗНЫХ "
                                        "деревьях")
        plan = duel.plan(DUEL, "d1")
        live_mode = ("executor" if plan["live"]["arm"] == "on" else "off")
        self.assertEqual(report["summary"], f"плечо {live_mode}",
                         "возвращается отчёт ЖИВОГО плеча, а не лучшего")
        journal = st.journal_path.read_text()
        self.assertIn("duel_start", journal)
        self.assertIn("duel_shadow", journal)


class TestSmokeDefects(unittest.TestCase):
    """Три дефекта, которых не поймал ни один из 1206 тестов гейта.

    Все три нашёл ПЕРВЫЙ настоящий прогон (2026-08-24). Это и есть цена
    зелёного набора: он говорит, что проверено, и молчит о том, что нет.
    """

    def test_shadow_failure_calls_the_error_hook(self):
        """Прибор, ломающийся невидимо, превращает замер в односторонний.

        В первой редакции исключение теневого плеча глоталось начисто: в
        журнале не осталось ни причины, ни трассировки, и диагноз
        собирался по ОТСУТСТВИЮ файлов.
        """
        seen = []

        def boom():
            raise RuntimeError("теневое плечо упало")

        live, shadow = duel.run_pair(lambda: "живое", boom, seen.append)
        self.assertEqual(live, "живое")
        self.assertIsNone(shadow)
        self.assertEqual(len(seen), 1)
        self.assertIn("упало", str(seen[0]))

    def test_live_arm_survives_a_broken_shadow(self):
        """Замер не имеет права стоить задач — только денег."""
        live, shadow = duel.run_pair(
            lambda: {"status": "done"},
            lambda: (_ for _ in ()).throw(OSError("нет дерева")),
            lambda exc: None)
        self.assertEqual(live["status"], "done")
        self.assertIsNone(shadow)

    def test_shadow_spend_is_distinguishable_from_work(self):
        """Деньги замера и деньги работы обязаны быть различимы.

        Обе траты настоящие и обе входят в бюджет прогона — но на v9lb
        из $7.30 задачи $2.76 стоил ПРИБОР, и в метрике это выглядело
        вторым исполнителем. Бюджет, в котором замер неотличим от
        работы, не врёт в сумме и врёт в смысле.
        """
        import inspect

        import executor
        src = inspect.getsource(executor.implement)
        self.assertIn('arm = "shadow"', src)
        self.assertIn("arm=arm", src)

    def test_arms_write_raw_streams_to_different_files(self):
        """Два потока, пишущие один путь, — молчаливая потеря журнала.

        Оба плеча звали `{task}-i{n}-executor.jsonl`, то есть теневой
        поток затирал живой.
        """
        import inspect

        import executor
        src = inspect.getsource(executor.implement)
        self.assertIn("log_tag", src,
                      "имя файла сырья обязано различать плечи")


if __name__ == "__main__":
    unittest.main()
