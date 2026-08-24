#!/usr/bin/env python3
"""Фоновый замер: жребий воспроизводим, выборка не смешивается.

Дефекты этой подсистемы не падают. Они портят ВЫБОРКУ — молча, так что
заметить их можно было бы только через недели по несходящимся числам.
Поэтому здесь проверяется не «работает ли», а «нельзя ли получить
испорченные данные, ничего не заметив».
"""
import collections
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import ambient  # noqa: E402

ON = {"experiments": {"ambient": "tester", "ambient_seed": 20260824}}


class TestDraw(unittest.TestCase):

    def test_off_by_default(self):
        self.assertIsNone(ambient.factor({}))
        self.assertIsNone(ambient.overlay({}, "t1"))
        self.assertEqual(ambient.facts({}, "t1"), {})

    def test_unknown_factor_refuses(self):
        with self.assertRaises(ValueError):
            ambient.factor({"experiments": {"ambient": "memory"}})

    def test_draw_is_a_function_of_the_task_not_the_call_order(self):
        """Реплей обязан оставаться реплеем: та же задача — то же плечо."""
        first = [ambient.arm(ON, t) for t in ("a", "b", "c", "d")]
        second = [ambient.arm(ON, t) for t in ("d", "c", "b", "a")]
        self.assertEqual(first, list(reversed(second)))

    def test_seed_changes_the_assignment(self):
        other = {"experiments": {"ambient": "tester", "ambient_seed": 1}}
        a = [ambient.arm(ON, f"t{i}") for i in range(40)]
        b = [ambient.arm(other, f"t{i}") for i in range(40)]
        self.assertNotEqual(a, b)

    def test_both_arms_actually_occur(self):
        """Жребий, который всегда даёт одно плечо, — не жребий, и на
        малой очереди это заметить нечем."""
        counts = collections.Counter(
            ambient.arm(ON, f"t{i:03d}") for i in range(200))
        self.assertGreater(counts["on"], 60)
        self.assertGreater(counts["off"], 60)

    def test_overlay_never_mutates_the_run_config(self):
        """Плечо одной задачи, протёкшее в следующую, не упало бы, а
        тихо смешало бы выборку — худший дефект из возможных здесь."""
        cfg = {"experiments": dict(ON["experiments"])}
        before = dict(cfg["experiments"])
        ambient.overlay(cfg, "t1")
        self.assertEqual(cfg["experiments"], before)

    def test_overlay_sets_the_flag_the_factor_names(self):
        for name, (key, on_v, off_v) in ambient.FACTORS.items():
            cfg = {"experiments": {"ambient": name, "ambient_seed": 7}}
            with self.subTest(factor=name):
                got = ambient.overlay(cfg, "zz")["experiments"][key]
                self.assertIn(got, (on_v, off_v))

    def test_facts_carry_the_seed(self):
        """Без сида запись не проверяема, а непроверяемая телеметрия
        замера — та же фольклорная цифра, что процент от трёх задач."""
        self.assertEqual(ambient.facts(ON, "t1")["ambient_seed"], 20260824)


class TestConflictIsRefused(unittest.TestCase):

    def test_pinned_and_drawn_at_once_is_a_refusal(self):
        cfg = {"experiments": {"ambient": "tester", "tester": True}}
        self.assertIsNotNone(ambient.conflict(cfg))

    def test_pinned_other_factor_is_fine(self):
        """Один фактор жребием, другой прибит — законно: смешиваются
        только ДВА случайных, а закреплённый фон одинаков в обоих плечах."""
        cfg = {"experiments": {"ambient": "tester", "skeleton": True}}
        self.assertIsNone(ambient.conflict(cfg))

    def test_memory_factor_conflicts_with_a_pinned_memory(self):
        cfg = {"experiments": {"ambient": "memory_executor",
                               "memory": "planner"}}
        self.assertIsNotNone(ambient.conflict(cfg))

    def test_no_ambient_no_conflict(self):
        self.assertIsNone(ambient.conflict({"experiments": {"tester": True}}))


if __name__ == "__main__":
    unittest.main()
