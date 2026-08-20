#!/usr/bin/env python3
"""Конфигурируемые лимиты и кэш карты репозитория.

Обе вещи из разряда «работало на стенде, откажет на реальном проекте».
Лимит раундов и таймаут гейта были зашиты константами: холодная сборка
Rust не влезает в 900 секунд, а упереться в чужую константу — значит
эскалировать по причине, не имеющей отношения к задаче.

Кэш карты опаснее: ускорение здесь бессмысленно, если карта расходится с
кодом. Поэтому инвалидация по отпечатку дерева, а не по времени, и
главный тест — не «быстро», а «не отдаёт устаревшее».
"""
import importlib.util
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
ag = _load("agents")


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "mod_a.py").write_text("def alpha():\n    return 1\n")
        (self.root / "mod_b.py").write_text("def beta():\n    return 2\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": []})

    def tearDown(self):
        self.tmp.cleanup()


class TestConfigurableLimits(RepoCase):
    def test_default_iteration_limit_preserved(self):
        loop = lp.Loop(self.state, {}, None)
        self.assertEqual(loop.max_iter, lp.MAX_ITER)

    def test_iteration_limit_from_config(self):
        loop = lp.Loop(self.state, {"max_iterations": 6}, None)
        self.assertEqual(loop.max_iter, 6)

    def _run_with_agents(self, config, implement_result):
        """Прогон одной задачи на болванках; возвращает счётчики вызовов."""
        calls = {"implement": 0, "review": 0}

        def implement(task, feedback, iteration):
            calls["implement"] += 1
            return implement_result

        def review(task, tail, iteration, confirming=False, **kw):
            calls["review"] += 1
            # Число находок УБЫВАЕТ: иначе `decide` объявит несходимость
            # и задача уйдёт в эскалацию раньше лимита — тест мерил бы
            # детект топтания, а не лимит исправлений.
            left = max(1, 8 - calls["review"])
            return {"analysis": "Разобрал дифф построчно и сверил с задачей.",
                    "verdict": "request_changes",
                    "summary": "Замечания по существу задачи остаются.",
                    "findings": [{"file": "mod_a.py", "severity": "minor",
                                  "category": "correctness", "confidence": 0.7,
                                  "issue": f"замечание {n}",
                                  "suggestion": "как"} for n in range(left)],
                    "out_of_scope_notes": []}

        agents = type("A", (), {
            "implement": staticmethod(implement),
            "review": staticmethod(review),
            "last_tuning": {},
            "commit_message": staticmethod(lambda t, d: "m")})()
        loop = lp.Loop(self.state, config, agents, ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        self.state.save_tasks({"goal": "g", "tasks": [
            {"id": "aaaa", "title": "t", "status": "pending", "deps": [],
             "type": "feature", "paths": ["mod_a.py"]}]})
        loop.run_task({"id": "aaaa", "title": "t", "type": "feature",
                       "paths": ["mod_a.py"]})
        return calls

    def test_raised_limit_gives_more_fix_rounds(self):
        """Лимит обязан РАБОТАТЬ, а не просто читаться из конфига.

        Считаются раунды, дошедшие ДО ВЕРДИКТА: именно они — попытки
        исправления, и именно их ограничивает max_iterations."""
        calls = self._run_with_agents({"max_iterations": 5},
                                      {"status": "done", "summary": "s"})
        self.assertEqual(calls["review"], 5, "конфиг прочитан, но не применён")

    def test_futile_rounds_do_not_spend_the_fix_limit(self):
        """Раунд без отчёта исполнителя судить нечем: он тратит свой
        потолок (max_futile_rounds), а не лимит исправлений."""
        calls = self._run_with_agents({"max_iterations": 5,
                                       "max_futile_rounds": 2}, None)
        self.assertEqual(calls["implement"], 2)
        self.assertEqual(calls["review"], 0, "ревьюер не звался ни разу")

    def test_futile_ceiling_follows_the_raised_limit(self):
        """Терпение — одна ручка: подняв max_iterations, оператор
        поднимает и терпимость к срывам среды."""
        calls = self._run_with_agents({"max_iterations": 6}, None)
        self.assertEqual(calls["implement"], 6)

    def _gate_timeout_seen(self, config):
        seen = {}

        def fake_sh(cmd, timeout=900):
            seen["timeout"] = timeout
            return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()

        loop = lp.Loop(self.state, config, None)
        loop._sh = fake_sh
        loop.gate({"id": "t"})
        return seen["timeout"]

    def test_gate_timeout_from_config(self):
        self.assertEqual(self._gate_timeout_seen({"gate_timeout": 3600}), 3600,
                         "холодная сборка Rust не влезает в зашитые 900 с")

    def test_gate_timeout_default(self):
        self.assertEqual(self._gate_timeout_seen({}), 900)


class TestMapCache(RepoCase):
    TASK = {"id": "t1", "type": "feature", "paths": ["mod_a.py", "mod_b.py"]}

    def setUp(self):
        super().setUp()
        self.agents = ag.Agents(self.state, {})
        self.builds = {"n": 0}

        class Counting:
            def __init__(inner, root):
                self.builds["n"] += 1
                inner.root = root

            def project_map(inner, budget=25):
                files = sorted(p.name for p in pathlib.Path(inner.root).glob("*.py"))
                return "карта: " + ", ".join(files)

        self.agents._codemap = type("M", (), {"HybridIndex": Counting})

    def test_second_call_uses_cache(self):
        first = self.agents.repo_map(dict(self.TASK))
        second = self.agents.repo_map(dict(self.TASK))
        self.assertEqual(first, second)
        self.assertEqual(self.builds["n"], 1,
                         "карта пересобрана без изменений в дереве")

    def test_changed_file_invalidates_cache(self):
        """Устаревшая карта хуже медленной: агент пойдёт по ней и ошибётся."""
        self.agents.repo_map(dict(self.TASK))
        (self.root / "mod_c.py").write_text("def gamma():\n    return 3\n")
        text = self.agents.repo_map(dict(self.TASK))
        self.assertIn("mod_c.py", text, "карта отстала от дерева")
        self.assertEqual(self.builds["n"], 2)

    def test_edit_of_existing_file_invalidates(self):
        self.agents.repo_map(dict(self.TASK))
        (self.root / "mod_a.py").write_text("def alpha():\n    return 999\n")
        self.agents.repo_map(dict(self.TASK))
        self.assertEqual(self.builds["n"], 2,
                         "правка отслеживаемого файла обязана сбросить кэш")

    def test_commit_invalidates_cache(self):
        self.agents.repo_map(dict(self.TASK))
        (self.root / "mod_c.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "next"], cwd=self.root, check=True)
        self.agents.repo_map(dict(self.TASK))
        self.assertEqual(self.builds["n"], 2, "смена HEAD обязана сбросить кэш")

    def test_budget_change_invalidates(self):
        self.agents.repo_map(dict(self.TASK))
        self.agents.config = {"map_budget": 5}
        self.agents.repo_map(dict(self.TASK))
        self.assertEqual(self.builds["n"], 2)

    def test_build_failure_is_not_cached(self):
        """Сбой сборки не должен запоминаться как «карты нет»."""
        class Boom:
            def __init__(inner, root):
                self.builds["n"] += 1
                raise RuntimeError("индекс упал")

        self.agents._codemap = type("M", (), {"HybridIndex": Boom})
        self.assertIsNone(self.agents.repo_map(dict(self.TASK)))
        self.assertIsNone(self.agents.repo_map(dict(self.TASK)))
        self.assertEqual(self.builds["n"], 2, "неудачу закэшировали")


if __name__ == "__main__":
    unittest.main(verbosity=2)
