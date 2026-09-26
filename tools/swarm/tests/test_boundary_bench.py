#!/usr/bin/env python3
"""Реплей-бенч линтера границ против семи признанных споров PILOT-1.

Замер, ради которого набор заморожен: каждый из семи споров исполнителя
владелец признал и ДОБАВИЛ в `paths` конкретный файл. Бенч восстанавливает
до-спорную границу и требует, чтобы текущий линтер назвал этот файл
заранее — не хуже замеренной линии 4/7 при потолке `_LIMIT` (findings
E12/boundary-lint: первая версия была 1/7).

В отличие от бенча диагнозов (test_diagnosis_bench.py), ground truth
здесь включает СТЕНД — репозиторий ZeusLogic, а стенды по правилу §1.4
06-дока в git не входят. Поэтому мерить бенч обязан по ЗАМОРОЖЕННОМУ
снапшоту пилота (worktree стенда на теге `pilot-2026-08`), а не по живому
чекауту: живой стенд уехал вперёд 2026-09-20 (XlsxRenderer и пр.) —
спорные файлы engine.rs и golden/nets.txt удалились, производители
размножились, и замер по нему обнулился (0/7 вместо 4/7) без всякого
регресса линтера (проверено 2026-09-25; сам линтер с даты заморозки не
менялся ни разу — один коммит в его истории, 150a5db). Снапшота нет —
громкий skip с рецептом, а не молчаливая зелень и не ложная краснота
(тот же договор, что у replay.py: честно сказать, что мерить нечего).
Падение на поднятой машине со снапшотом — сигнал: либо линтер сломали,
либо снапшот подменили, — и тогда набор пересматривает человек, а не
тест подбирается под ответ.
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

# Снапшот пилота в worktree стенда; SWARM_BOUNDARY_STAND перекрывает.
# Живой чекаут — НЕ фолбэк: на нём замер честен, но бессмыслен (см.
# докстринг), и молча мерить по нему — та же ловушка, что молчаливый skip.
SNAPSHOT = pathlib.Path.home() / "work" / "zeus-pilot-bench"
SNAPSHOT_RECIPE = (
    "git -C ~/work/zeus-pilot worktree add ~/work/zeus-pilot-bench "
    "pilot-2026-08 && mkdir -p ~/work/zeus-pilot-bench/.swarm && "
    "cp ~/work/zeus-pilot/.swarm/tasks.json ~/work/zeus-pilot-bench/.swarm/"
)


def stand() -> pathlib.Path:
    env = os.environ.get("SWARM_BOUNDARY_STAND")
    if env:
        return pathlib.Path(env)
    return SNAPSHOT


class TestBoundaryAgainstGoldSet(unittest.TestCase):
    def setUp(self):
        self.ranks = replay.measure(stand())
        if self.ranks is None:
            self.skipTest(
                f"снапшот {stand()} не поднят — бенчу нечего мерить "
                f"(стенды в git не входят, §1.4 06-дока). Поднять: "
                f"{SNAPSHOT_RECIPE}")

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
