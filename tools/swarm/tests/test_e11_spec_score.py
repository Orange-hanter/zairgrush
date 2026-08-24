#!/usr/bin/env python3
"""Замороженный счёт E11: выживаемость мутантов ПРОТИВ СПЕЦИФИКАЦИИ.

Сырая выживаемость — не про тесты. Мутант выживает по трём разным
причинам, и лишь одна из них дыра: он мог быть неубиваем (поведение не
изменилось) или неоговорён (спека о таком не говорит, и честный тест по
ней ОБЯЗАН его пропустить). Пока считали сырьё, плечи сравнивались
вместе с молчанием спецификации: 30 % против 22 %. После разбора —
18 % против 9 %, то есть преимущество независимого автора тестов вдвое
больше, а обе доли упали больше чем вдвое.

Число заморожено здесь по тем же соображениям, что и реплей границ
(E12): замер, живущий только в прозе доков, тихо расходится с кодом,
который его считает. В отличие от бенча границ стенд не нужен — оба
набора мутантов лежат в git (`experiments/goldset/e11/`, 64 строки),
потому что замер, чьи входные данные не пережили чистку рабочего
каталога, невоспроизводим, а его число превращается в фольклор.

Тест падает в двух случаях, и оба требуют человека, а не подгонки:
сломали `spec_score`/классификацию — либо кто-то поправил
классификацию, не обновив вывод в 06-доке.
"""
import importlib.util
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "experiments" / "tools" / "e11-compare.py"

spec = importlib.util.spec_from_file_location("e11_compare", TOOL)
e11 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e11)

# Замер 2026-08-24, findings E11/spec-scored-survival-doubles-the-margin.
FROZEN = {
    # плечо: (всего сырых, выжило сырых, осталось по спеке, дыр по спеке)
    "A": (37, 11, 28, 5),
    "B": (27, 6, 23, 2),
}


class TestSpecScoredSurvival(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.got = e11.measure()

    def test_frozen_counts_hold(self):
        self.assertEqual(self.got, FROZEN)

    def test_independent_tester_still_wins_on_spec_score(self):
        """Направление вывода, а не только числа: если оно перевернётся,
        06-док будет утверждать обратное тому, что считает код."""
        _a_tot, _a_raw, a_kept, a_holes = self.got["A"]
        _b_tot, _b_raw, b_kept, b_holes = self.got["B"]
        self.assertLess(b_holes / b_kept, a_holes / a_kept)

    def test_the_correction_is_not_lost(self):
        """Счёт по спеке ОБЯЗАН быть строже сырого у обоих плеч.

        Именно это и было находкой: сырая доля завышала обе. Падение
        РАЗНОЕ, и первая редакция этого теста ловила меня на попытке
        сказать «обе упали больше чем вдвое» — у плеча A падение 40 %
        (30 -> 18), больше чем вдвое падает только B (22 -> 9). Число
        было написано в доке до того, как его посчитали; тест поймал.
        """
        for arm, (tot, raw, kept, holes) in self.got.items():
            with self.subTest(arm=arm):
                self.assertLess(holes / kept, raw / tot,
                                "разбор обязан убирать ложные дыры")
        _at, a_raw, a_kept, a_holes = self.got["A"]
        _bt, b_raw, b_kept, b_holes = self.got["B"]
        self.assertLess((b_holes / b_kept) / (b_raw / _bt), 0.5,
                        "у плеча B падение больше чем вдвое")
        self.assertGreater((a_holes / a_kept) / (a_raw / _at), 0.5,
                           "у плеча A падение меньше чем вдвое — так и "
                           "замерено, и так должно быть написано в доке")

    def test_every_dropped_mutant_carries_a_reason(self):
        """Классификация — суждение, и потому обязана быть оспоримой:
        строка без причины непроверяема и молча становится подгонкой."""
        rows = e11.load(e11.DEFAULT_CLASSES)
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=(row["arm"], row["file"], row["line"])):
                self.assertIn(row["class"], ("equivalent", "spec_silent",
                                             "real_gap"))
                self.assertGreater(len(row.get("reason") or ""), 80,
                                   "причина в одну строку — не причина")

    def test_classification_matches_real_mutants(self):
        """Классифицировать позицию, которой в наборе нет, — значит
        вычитать из знаменателя воздух."""
        known = set()
        for arm, path in (("A", e11.DEFAULT_ARMS[0]),
                          ("B", e11.DEFAULT_ARMS[1])):
            for r in e11.load(path):
                known.add((arm, r["file"], r["line"], r["kind"],
                           r["before"], r["after"]))
        for row in e11.load(e11.DEFAULT_CLASSES):
            key = (row["arm"], row["file"], row["line"], row["kind"],
                   row["before"], row["after"])
            with self.subTest(key=key):
                self.assertIn(key, known)


if __name__ == "__main__":
    unittest.main()
