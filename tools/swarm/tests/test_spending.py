#!/usr/bin/env python3
"""Политика денег: явное сильнее режима, режим сильнее умолчания кода.

Замечание, с которого начался этот файл: неявный потолок ревьюера
($1 в argv) исчез, и НИ ОДИН из 1155 тестов гейта этого не заметил.
Самая дорогая роль петли жила с умолчанием, которое никто не проверял, —
и именно оно давало штатный диагноз «ревьюер обрублен по бюджету».
"""
import io
import json
import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import engines  # noqa: E402
import spending  # noqa: E402

_PD = engines.PromptDelivery


class TestModeResolution(unittest.TestCase):

    def test_default_is_the_money_bin(self):
        """Решение владельца 2026-08-24: из коробки потолков нет."""
        self.assertEqual(spending.mode({}), "money_bin")

    def test_unknown_mode_refuses_instead_of_defaulting(self):
        """Тот же довод, что у движка исполнителя: опечатка в имени
        режима не имеет права молча включить противоположную политику."""
        with self.assertRaises(ValueError) as cm:
            spending.mode({"spending": "moneybin"})
        self.assertIn("money_bin", str(cm.exception))
        self.assertIn("capped", str(cm.exception))

    def test_money_bin_has_no_implicit_cap_anywhere(self):
        for key in spending.CALL_CAP_KEYS:
            with self.subTest(key=key):
                self.assertIsNone(spending.call_cap({}, key))

    def test_capped_restores_the_old_reviewer_default(self):
        """Прежнее поведение обязано быть достижимо одним словом —
        иначе смена умолчания необратима для того, кто на неё не
        подписывался."""
        self.assertEqual(
            spending.call_cap({"spending": "capped"}, "review_budget_usd"),
            1.0)

    def test_explicit_value_beats_the_mode(self):
        """Настройка, которую оператор написал руками и закоммитил, не
        имеет права молча перестать действовать — весь закрытый список
        ключей в cli.py существует ровно из-за этого класса отказов."""
        cfg = {"spending": "money_bin", "review_budget_usd": 2.5}
        self.assertEqual(spending.call_cap(cfg, "review_budget_usd"), 2.5)

    def test_explicit_zero_means_no_cap_not_instant_truncation(self):
        """`--max-budget-usd 0` обрубил бы вызов немедленно. Ноль читается
        как «снять потолок», иначе он был бы ловушкой."""
        cfg = {"spending": "capped", "review_budget_usd": 0}
        self.assertIsNone(spending.call_cap(cfg, "review_budget_usd"))

    def test_run_budget_is_never_implicit(self):
        """Сколько стоит цель — решение владельца, и выдумать его за него
        нельзя ни в ту, ни в другую сторону."""
        self.assertIsNone(spending.run_budget({}))
        self.assertIsNone(spending.run_budget({"spending": "capped"}))
        self.assertEqual(spending.run_budget({"total_budget_usd": 120}), 120.0)


class TestHeadlineTellsTheTruth(unittest.TestCase):
    """Строка отчёта называет ДЕЙСТВУЮЩЕЕ положение, а не имя режима.

    Первая редакция врала в обе стороны сразу: «потолков нет» при явно
    заданном числе и «потолок $1» при явном нуле, который его снимает.
    """

    def test_no_caps_says_so(self):
        line = spending.headline({}, 0.0)
        self.assertIn("потолков вызова НЕТ", line)

    def test_explicit_cap_in_money_bin_is_not_called_absent(self):
        line = spending.headline({"review_budget_usd": 2.5}, 0.0)
        self.assertIn("review $2.5", line)
        self.assertNotIn("потолков вызова НЕТ", line)

    def test_explicit_zero_in_capped_is_not_called_a_cap(self):
        line = spending.headline(
            {"spending": "capped", "review_budget_usd": 0}, 0.0)
        self.assertNotIn("$1.0", line)

    def test_run_budget_absence_is_named_not_omitted(self):
        self.assertIn("не задан", spending.headline({}, 3.0))


class TestArgvActuallyChanges(unittest.TestCase):
    """Политика без следа в argv — пожелание, а не политика."""

    def test_executor_argv_carries_no_cap_by_default(self):
        argv = engines.executor_argv("claude", "sonnet", _PD(("p",), None), {})
        self.assertNotIn("--max-budget-usd", argv)

    def test_executor_argv_honours_an_explicit_cap(self):
        argv = engines.executor_argv("claude", "sonnet", _PD(("p",), None),
                                     {"executor_budget_usd": 3})
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.0")

    def test_kimi_argv_never_grew_a_budget_flag(self):
        """Поток kimi цены не знает вовсе; флаг здесь был бы выдумкой."""
        argv = engines.executor_argv("kimi", "kimi-k2", _PD(("p",), None),
                                     {"executor_budget_usd": 3})
        self.assertNotIn("--max-budget-usd", argv)

    def test_reviewer_argv_has_no_implicit_cap(self):
        """Умолчание $1 у САМОЙ ДОРОГОЙ роли не замечал ни один из 1155
        тестов гейта — этот тест и существует, чтобы заметить."""
        argv = self._review_argv({})
        self.assertNotIn("--max-budget-usd", argv)

    def test_reviewer_argv_capped_keeps_the_dollar(self):
        argv = self._review_argv({"spending": "capped"})
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1.0")

    def _review_argv(self, config):
        import tempfile

        import agents as ag
        import state as state_mod
        root = pathlib.Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        seen = {}
        orig = subprocess.Popen

        def fake(argv, **kw):
            if argv and argv[0] == "claude":
                seen["argv"] = argv
                return type("P", (), {
                    "stdin": io.StringIO(),
                    "stdout": io.StringIO(json.dumps({"type": "result"})),
                    "stderr": io.StringIO(""), "returncode": 0,
                    "poll": lambda s: 0, "wait": lambda s, timeout=None: 0,
                    "kill": lambda s: None})()
            return orig(argv, **kw)

        subprocess.Popen = fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", orig))
        st = state_mod.SwarmState(root)
        agents = ag.Agents(st, config)
        agents.review({"id": "t1", "title": "t", "paths": ["a.py"],
                       "type": "feature"}, "diff", 1)
        return seen.get("argv", [])


if __name__ == "__main__":
    unittest.main()
