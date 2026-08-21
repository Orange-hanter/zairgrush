#!/usr/bin/env python3
"""Реплей-бенч линтера границ против семи признанных споров PILOT-1.

Замер, ради которого набор заморожен: каждый из семи споров исполнителя
владелец признал и ДОБАВИЛ в `paths` конкретный файл. Бенч восстанавливает
до-спорную границу и требует, чтобы текущий линтер назвал этот файл
заранее — не хуже замеренной линии 4/7 при потолке `_LIMIT` (findings
E12/boundary-lint: первая версия была 1/7).

В отличие от бенча диагнозов (test_diagnosis_bench.py), ground truth
здесь включает СТЕНД — репозиторий ZeusLogic, а стенды по правилу §1.4
06-дока в git не входят. Поэтому: стенд есть — бенч бежит по-настоящему
и падает при регрессе линтера; стенда нет — громкий skip с причиной,
а не молчаливая зелень (тот же договор, что у replay.py: честно сказать,
что мерить нечего). Падение на машине со стендом — сигнал: либо линтер
сломали, либо стенд ушёл так далеко, что замер перестал что-то значить, —
и тогда набор пересматривает человек, а не тест подбирается под ответ.
"""
import importlib.util
import json
import os
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
REPLAY = REPO_ROOT / "experiments" / "goldset" / "boundaries" / "replay.py"
CASES = REPO_ROOT / "experiments" / "goldset" / "boundaries" / "cases.jsonl"

spec = importlib.util.spec_from_file_location("boundary_replay", REPLAY)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

# Замер 2026-08-20, замороженный в findings E12/boundary-lint.
FROZEN_CAUGHT, FROZEN_TOTAL = 4, 7
EXPECTED_QIDS = {"q005", "q011", "q012", "q014", "q016", "q017", "q020"}


def stand() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "SWARM_BOUNDARY_STAND", pathlib.Path.home() / "work" / "zeus-pilot"))


class TestBoundaryAgainstGoldSet(unittest.TestCase):
    def setUp(self):
        self.ranks = replay.measure(stand())
        if self.ranks is None:
            self.skipTest(f"стенд {stand()} недоступен — бенчу нечего "
                          f"мерить (стенды в git не входят, §1.4 06-дока)")

    def test_cases_cover_all_seven_granted_disputes(self):
        rows = [json.loads(ln) for ln in
                CASES.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(rows), FROZEN_TOTAL,
                         "набор усох — регрессии перестанет ловить молча")
        self.assertEqual({r["qid"] for r in rows}, EXPECTED_QIDS)

    def test_linter_holds_the_measured_floor(self):
        caught = sum(1 for p in self.ranks.values()
                     if p and p <= replay.load_planner()._LIMIT)
        self.assertGreaterEqual(
            caught, FROZEN_CAUGHT,
            f"линтер границ ниже замороженной линии: {caught}/"
            f"{FROZEN_TOTAL} при поле {FROZEN_CAUGHT}/{FROZEN_TOTAL} "
            f"(ранги: {self.ranks}) — регресс линтера или ушедший стенд")


if __name__ == "__main__":
    unittest.main(verbosity=2)
