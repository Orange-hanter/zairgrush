#!/usr/bin/env python3
"""Реплей-бенч диагноста против ЗОЛОТОГО НАБОРА PILOT-1.

Замер, ради которого набор и заморожен: на пилоте диагноз «задача,
вероятно, слишком крупная — расщепить» был неверен ШЕСТЬ раз из шести,
и каждый раз посылал человека резать задачу, у которой была совсем
другая беда — защищённый тест, квота провайдера, таймаут, расхождение
двух ревьюеров на одном диффе.

Каждый кейс — реальная эскалация с реальной уликой из журнала стенда и
причиной, которую подтвердил владелец (`human_quote`). Бенч гоняет
ТЕКУЩИЙ диагност по этим уликам и требует: назвать то, что человек
подтвердил (`must_mention`), и НЕ повторять опровергнутую догадку
(`must_not_claim`).

Кейс, который начнёт падать, — сигнал, а не мусор: либо диагност
сломали, либо улику стали собирать иначе. Правило набора (см.
experiments/goldset/README.md): ярлык не переписывается задним числом.
"""
import importlib.util
import json
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CASES = REPO_ROOT / "experiments" / "goldset" / "diagnosis" / "cases.jsonl"

spec = importlib.util.spec_from_file_location("loop", ROOT_DIR / "loop.py")
lp = importlib.util.module_from_spec(spec)
sys.modules["loop"] = lp
spec.loader.exec_module(lp)


def load_cases():
    """Набор обязан существовать: молчаливый skip убил бы регрессию."""
    if not CASES.exists():
        raise AssertionError(
            f"золотой набор диагнозов не найден: {CASES}. Бенч без набора "
            f"не проверяет ничего — восстановите файл или удалите бенч явно")
    rows = [json.loads(line) for line in
            CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise AssertionError(f"{CASES} пуст")
    return rows


class TestDiagnosisAgainstGoldSet(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()

    def test_every_case_names_the_confirmed_cause(self):
        for case in self.cases:
            ev = case["evidence"]
            with self.subTest(qid=case["qid"], cause=case["human_cause"]):
                text = lp.Loop._diagnose(
                    lp.ESCALATE_MAX,
                    ev.get("history") or [],
                    scope_failures=ev.get("scope_failures") or [],
                    sig_failures=ev.get("sig_failures") or [],
                    exec_failures=ev.get("exec_failures") or [])
                for token in case["must_mention"]:
                    self.assertIn(token, text,
                                  f"{case['qid']}: диагноз не назвал того, "
                                  f"что подтвердил владелец "
                                  f"(«{case['human_quote'][:80]}…»)")

    def test_no_case_repeats_the_refuted_guess(self):
        """Догадка о размере была опровергнута 6 раз из 6 — ни один кейс
        набора не имеет права услышать её снова."""
        for case in self.cases:
            ev = case["evidence"]
            with self.subTest(qid=case["qid"]):
                text = lp.Loop._diagnose(
                    lp.ESCALATE_MAX,
                    ev.get("history") or [],
                    scope_failures=ev.get("scope_failures") or [],
                    sig_failures=ev.get("sig_failures") or [],
                    exec_failures=ev.get("exec_failures") or [])
                for token in case["must_not_claim"]:
                    self.assertNotIn(token, text,
                                     f"{case['qid']}: опровергнутая догадка "
                                     f"вернулась в диагноз")

    def test_bench_covers_every_refuted_diagnosis_of_the_pilot(self):
        """Шесть эскалаций пилота — шесть кейсов. Молчаливо усохший
        набор перестал бы ловить регрессию, ничего об этом не сказав."""
        self.assertGreaterEqual(len(self.cases), 6)
        self.assertEqual(
            {c["qid"] for c in self.cases},
            {"q004", "q007", "q008", "q009", "q010", "q019"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
