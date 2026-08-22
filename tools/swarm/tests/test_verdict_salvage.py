#!/usr/bin/env python3
"""Реплей отказов формы вердикта против замороженного набора E13.

Замер, ради которого набор заморожен: на бенче E13 пять вызовов ревьюера
из семнадцати не дали валидного вердикта и сожгли $0.67 — 37 % всех
денег ревью и больше, чем всё расстояние между сравниваемыми плечами.
Три из пяти — один и тот же отказ ФОРМЫ: модель складывает весь вердикт
в поле `analysis`, размечая остальные поля тегами, провайдер вызов
отклоняет, ретраи кончаются, конверт приходит пустым. Суждение при этом
лежит в потоке целиком и уже оплачено.

Набор состоит из НАСТОЯЩИХ полезных нагрузок этих отказов (стенды E13) и
синтетических негативов. Двусторонность — суть бенча: спасение обязано
возвращать вердикт там, где он есть, и обязано ОТКАЗЫВАТЬ там, где чинить
нечего или починенное негодно. Односторонний бенч («спасли N») толкал бы
код к тому, чтобы сочинять вердикты.

Гоняется в общем гейте: payload'ы лежат в наборе, стенда не требуется.
"""
import importlib.util
import json
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GOLD = REPO_ROOT / "experiments" / "goldset" / "verdicts"
CASES = GOLD / "cases.jsonl"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


parsing = _load("parsing")
verdicts = _load("verdicts")
driver = _load("driver")


def load_cases():
    """Набор обязан существовать: молчаливый skip не проверяет ничего."""
    if not CASES.exists():
        raise AssertionError(
            f"золотой набор вердиктов не найден: {CASES}. Бенч без набора "
            f"не проверяет ничего — восстановите файл или удалите бенч явно")
    rows = [json.loads(line) for line in
            CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise AssertionError(f"{CASES} пуст")
    return rows


class TestSalvageAgainstGoldSet(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()

    def _run(self, case):
        fixed = parsing.repair_verdict(case["payload"])
        verdict = fixed if fixed is not None else case["payload"]
        return fixed, verdict, verdicts.validate_verdict(verdict)

    def test_every_case_lands_where_the_gold_set_says(self):
        for case in self.cases:
            with self.subTest(case=case["id"], source=case["source"]):
                fixed, verdict, valid = self._run(case)
                expect = case["expect"]
                self.assertEqual(fixed is not None, expect["repaired"],
                                 f"{case['id']}: {case['why']}")
                self.assertEqual(valid, expect["valid"],
                                 f"{case['id']}: {case['why']}")
                if "verdict" in expect:
                    self.assertEqual(verdict.get("verdict"), expect["verdict"])
                if "findings" in expect:
                    self.assertEqual(len(verdict.get("findings") or []),
                                     expect["findings"])
                if "problem_has" in expect:
                    self.assertIn(expect["problem_has"],
                                  verdicts.verdict_problem(verdict) or "")

    def test_the_two_real_lost_verdicts_come_back(self):
        """Ради этих двух всё и делалось: настоящие суждения, за которые
        уже заплачено, а петля их выбрасывала и блокировала задачу."""
        saved = [c["id"] for c in self.cases
                 if c.get("real") and self._run(c)[2] and self._run(c)[0]]
        self.assertEqual(sorted(saved), ["e13-a-b2ng-a1", "e13-b-b4wr-a2"])

    def test_the_placeholder_verdicts_stay_refused(self):
        """`analysis: "Test"` — не форма, а отсутствие разбора. Спасение
        не имеет права выдать заглушку за суждение: её ловит порог
        существенности, и это единственное, что стоит между фальшивым
        approve и коммитом."""
        for case in self.cases:
            if not case.get("real") or case["expect"]["valid"]:
                continue
            with self.subTest(case=case["id"]):
                _fixed, verdict, valid = self._run(case)
                self.assertFalse(valid)
                self.assertIn("заглушка",
                              verdicts.verdict_problem(verdict) or "")

    def test_bench_keeps_both_sides(self):
        """Набор, из которого вымылись негативы, начнёт хвалить код за
        то, что тот сочиняет вердикты."""
        self.assertEqual(len(self.cases), 8)
        self.assertEqual(sum(1 for c in self.cases if c.get("real")), 4)
        self.assertTrue(any(not c["expect"]["repaired"] for c in self.cases))
        self.assertTrue(any(c["expect"]["repaired"]
                            and not c["expect"]["valid"] for c in self.cases))


class TestSalvageReadsTheStream(unittest.TestCase):
    """Мост от потока к полезной нагрузке: без него спасать нечего."""

    def test_last_call_wins(self):
        stream = "\n".join(json.dumps(ev) for ev in [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "StructuredOutput",
                 "input": {"analysis": "первая попытка"}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "мысли вслух"},
                {"type": "tool_use", "name": "StructuredOutput",
                 "input": {"analysis": "последняя попытка"}}]}},
        ])
        self.assertEqual(driver.last_structured_output(stream),
                         {"analysis": "последняя попытка"})

    def test_other_tools_are_not_a_verdict(self):
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
        self.assertIsNone(driver.last_structured_output(stream))

    def test_broken_lines_do_not_stop_the_scan(self):
        """Поток читается как данные: обрывок строки не имеет права
        унести с собой вердикт, лежащий следом."""
        stream = "\n".join([
            "{не json",
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "StructuredOutput",
                 "input": {"analysis": "есть"}}]}}),
        ])
        self.assertEqual(driver.last_structured_output(stream),
                         {"analysis": "есть"})

    def test_empty_stream(self):
        self.assertIsNone(driver.last_structured_output(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
