#!/usr/bin/env python3
"""Страж потолка прогона ВНУТРИ хода (NXT-005).

Дефект: `total_budget_usd` проверялся только между итерациями и между
задачами, а один ход — исполнитель, потом ревьюер, потом повторы ревью —
проходил без единой проверки. На g1nt ход сжёг $2.75 при потолке $0.70:
проверка перед раундом видела ноль, следующая видела уже факт.

Контракт, который здесь закрепляется: ни один вызов LLM не уходит, когда
потраченное (плюс честная оценка предстоящего вызова) достигло потолка,
а срабатывание стража сводится к тому же терминальному исходу, что у
проверки между раундами: задача pending, работа в stash, прогон стоит.
"""
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import agents as ag  # noqa: E402
import loop as lp  # noqa: E402
import spending  # noqa: E402
import state as state_mod  # noqa: E402


def _stub_state(spent):
    return type("S", (), {"total_spend": staticmethod(lambda: spent)})()


class TestGuard(unittest.TestCase):
    """Единица решения стража: уйдёт ли СЛЕДУЮЩИЙ вызов."""

    def test_no_run_budget_means_no_guard(self):
        """Потолок прогона — решение владельца; без него страж молчит,
        сколько бы ни было потрачено."""
        spending.guard({}, _stub_state(999.0), "review")

    def test_spent_below_budget_passes(self):
        spending.guard({"total_budget_usd": 1.0}, _stub_state(0.60))

    def test_spent_at_budget_stops_the_next_call(self):
        with self.assertRaises(spending.BudgetExhaustedError) as cm:
            spending.guard({"total_budget_usd": 0.70}, _stub_state(0.70))
        self.assertEqual(cm.exception.spent, 0.70)
        self.assertEqual(cm.exception.budget, 0.70)

    def test_pre_dispatch_counts_the_call_cap_as_estimate(self):
        """До диспетча известен потолок вызова роли — худшая честная
        оценка его цены. Потрачено меньше бюджета, а потраченное плюс
        оценка — уже нет: вызов не уходит."""
        cfg = {"total_budget_usd": 1.0, "review_budget_usd": 0.5}
        with self.assertRaises(spending.BudgetExhaustedError) as cm:
            spending.guard(cfg, _stub_state(0.60), "review",
                           "review_budget_usd")
        self.assertEqual(cm.exception.role, "review")

    def test_role_without_a_cap_gives_zero_estimate(self):
        """Потолка роли нет (money_bin) — оценка ноль, и решает только
        записанный факт, а не выдуманная цена."""
        cfg = {"total_budget_usd": 1.0}
        spending.guard(cfg, _stub_state(0.60), "review", "review_budget_usd")

    def test_explicit_zero_cap_is_no_estimate_not_instant_stop(self):
        """Явный ноль снимает потолок вызова (spending.call_cap) — страж
        не имеет права читать его как «вызов бесплатен, но остановись»."""
        cfg = {"total_budget_usd": 1.0, "review_budget_usd": 0}
        spending.guard(cfg, _stub_state(0.60), "review", "review_budget_usd")


class RepoCase(unittest.TestCase):
    """Минимальный настоящий репозиторий + настоящее состояние петли."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "a.py").write_text("def f():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root,
                       check=True)
        self.state = state_mod.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": [
            {"id": "aaaa", "title": "задача", "status": "pending",
             "deps": [], "type": "feature", "paths": ["a.py"]}]})

    def status_of(self, tid="aaaa"):
        return next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == tid)["status"]

    def _fake_popen(self, envelope):
        """Процесс-агент, который отвечает одним result-событием."""
        calls = []
        orig = subprocess.Popen

        def fake(argv, **kw):
            if argv and argv[0] in ("claude", "kimi"):
                calls.append(argv)
                return type("P", (), {
                    "stdin": io.StringIO(),
                    "stdout": io.StringIO(json.dumps(envelope) + "\n"),
                    "stderr": io.StringIO(""), "returncode": 0,
                    "poll": lambda s: 0, "wait": lambda s, timeout=None: 0,
                    "kill": lambda s: None})()
            return orig(argv, **kw)

        subprocess.Popen = fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", orig))
        return calls


class TestMidTurnStop(RepoCase):
    """Приёмка дефекта: один вызов исполнителя за $2.75 при потолке $0.70.

    Старое поведение: раунд завершался, ревьюер уходил следом, останов
    случался либо никогда (задача закрывалась), либо постфактум. Теперь
    цена записана — и СЛЕДУЮЩИЙ вызов того же хода не имеет права уйти.
    """

    def test_overspent_turn_stops_before_review(self):
        envelope = {
            "type": "result", "subtype": "success",
            "structured_output": {"status": "done", "summary": "с",
                                  "evidence": {}},
            "total_cost_usd": 2.75, "usage": {},
        }
        calls = self._fake_popen(envelope)
        config = {"total_budget_usd": 0.70, "executor_engine": "claude"}
        agents = ag.Agents(self.state, config)
        loop = lp.Loop(self.state, config, agents, ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        results = loop.run()
        self.assertEqual(results.get("_budget"), "exhausted",
                         "прогон обязан встать, а не дойти до ревью")
        self.assertEqual(len(calls), 1,
                         "ревьюер ушёл в провайдера после пробитого потолка")
        self.assertEqual(self.status_of(), "pending",
                         "задача ни в чём не виновата: pending, не blocked")
        journal = self.state.journal_path.read_text()
        self.assertIn("budget_exhausted", journal)
        self.assertIn('"spent": 2.75', journal)

    def test_estimate_of_next_call_blocks_it_before_dispatch(self):
        """Потрачено $0.60 из $1.00, потолок вызова исполнителя $0.50:
        между задачами проверка проходит (0.60 < 1.00), а страж перед
        диспетчем видит худшую честную оценку — и процесс не уходит."""
        self.state.metric(task="t0", phase="implement", cost_usd=0.60)
        calls = self._fake_popen({"type": "result"})
        config = {"total_budget_usd": 1.0, "executor_engine": "claude",
                  "executor_budget_usd": 0.5}
        agents = ag.Agents(self.state, config)
        loop = lp.Loop(self.state, config, agents, ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        results = loop.run()
        self.assertEqual(results.get("_budget"), "exhausted")
        self.assertEqual(calls, [], "вызов ушёл при исчерпанном по оценке "
                                    "бюджете")
        self.assertEqual(self.status_of(), "pending")


class TestRecordedOverspendBlocksReviewer(RepoCase):
    """Между записями метрик страж тоже работает: ревьюер — отдельный
    вызов, и цена исполнителя обязана остановить его до диспетча."""

    def test_review_refuses_to_dispatch_after_overspend(self):
        self.state.metric(task="t0", phase="implement", cost_usd=5.0)
        calls = self._fake_popen({"type": "result"})
        agents = ag.Agents(self.state, {"total_budget_usd": 0.70})
        with self.assertRaises(spending.BudgetExhaustedError):
            agents.review({"id": "t1", "title": "t", "paths": ["a.py"],
                           "type": "feature"}, "diff", 1)
        self.assertEqual(calls, [], "процесс ревьюера запущен вслепую")


if __name__ == "__main__":
    unittest.main()
