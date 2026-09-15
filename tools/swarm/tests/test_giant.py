#!/usr/bin/env python3
"""NXT-012: детерминированное исключение гигантских дифов из ревью.

Доказательство размера — на корпусе пилота (rev001/ground/*.raw.diff,
18 диффов): нормальные ≤ 692 строк / 46_072 символов, гиганты — g1nt
(16_086 / 371_519) и c4rp (54_870 / 3_755_327); дефолты 2_000 строк и
200_000 символов лежат внутри разрыва с запасом ≥ ~2x от обеих сторон.

Тесты держат свойства, без которых правило вредно: гигант не уходит НИ
В ОДИН вызов ревью (проверка стоит ДО бэнды триажа — даже docs_only не
пропускается молча, а эскалируется владельцу); исключение видимо
(журнальное `giant_excluded` + строка метрик `triage="giant-excluded"`);
вердикт — synthetic blocked (работу никто не судил, approve запрещён
смыслом); пороги настраиваемы, а сбой предиката и мусорная настройка
стоят ноль (fail-open на обычный путь).
"""
import json
import unittest

from tests import test_review_failure as _trf
from tests.test_review_failure import (
    VALID,
    RepoCase,
    ag,
    patch_claude_popen,
    rv,
)

tr = _trf._load("triage")

TASK = {"id": "t1", "title": "t", "spec": "s",
        "acceptance": ["гейт зелёный"], "paths": ["mod.py"],
        "type": "feature"}


def _journal(state):
    return [json.loads(line)
            for line in state.journal_path.read_text().splitlines()]


def _review_rows(state):
    rows = [json.loads(x)
            for x in state.metrics_path.read_text().splitlines() if x.strip()]
    return [r for r in rows if r.get("phase") == "review"]


class TestGiantReason(unittest.TestCase):
    """Предикат и его пороги: разрыв корпуса между 692 и 16_086 строк."""

    def test_boundary_at_default_lines_is_strict(self):
        self.assertIsNone(tr.giant_reason(tr.GIANT_MAX_LINES, 0, {}))
        self.assertEqual(
            tr.giant_reason(tr.GIANT_MAX_LINES + 1, 0, {}), "lines")

    def test_boundary_at_default_chars_is_strict(self):
        self.assertIsNone(tr.giant_reason(0, tr.GIANT_MAX_CHARS, {}))
        self.assertEqual(
            tr.giant_reason(0, tr.GIANT_MAX_CHARS + 1, {}), "chars")

    def test_either_threshold_triggers(self):
        """Триггер — ИЛИ: длинный дифф с длинными строками ловится по
        символам, дифф из коротких строк — по строкам."""
        self.assertEqual(tr.giant_reason(50, tr.GIANT_MAX_CHARS + 1, {}),
                         "chars")
        self.assertEqual(tr.giant_reason(tr.GIANT_MAX_LINES + 1, 50, {}),
                         "lines")

    def test_pilot_giants_caught_normals_pass(self):
        """Точки замера корпуса: оба гиганта за порогом, максимум
        нормального (z8ck 692 строк / s2ky-i1 46_072 символов) — перед."""
        self.assertEqual(tr.giant_reason(16_086, 371_519, {}), "lines")
        self.assertEqual(tr.giant_reason(54_870, 3_755_327, {}), "lines")
        self.assertIsNone(tr.giant_reason(692, 46_072, {}))

    def test_config_override(self):
        self.assertEqual(tr.giant_reason(11, 0, {"giant_max_lines": 10}),
                         "lines")
        self.assertIsNone(tr.giant_reason(10, 0, {"giant_max_lines": 10}))
        self.assertEqual(tr.giant_reason(0, 11, {"giant_max_chars": 10}),
                         "chars")

    def test_exclusion_disabled_per_stand(self):
        self.assertIsNone(tr.giant_reason(10**9, 10**9,
                                          {"giant_exclusion": False}))

    def test_garbage_threshold_fails_open_to_default(self):
        """Мусорная настройка не может ни запретить нормальное, ни
        пропустить гиганта: умолчание, а не её значение."""
        for bad in (True, "5", None, 3.5):
            self.assertIsNone(tr.giant_reason(692, 46_072,
                                              {"giant_max_lines": bad}), bad)
            self.assertEqual(tr.giant_reason(16_086, 0,
                                             {"giant_max_lines": bad}),
                             "lines", bad)


class TestGiantExcluded(RepoCase):
    """Гигант: вызова нет, вердикт blocked, исключение видимо."""

    def _run(self, config=None):
        calls = []

        def reply(argv):
            calls.append(argv)
            return {"structured_output": VALID, "total_cost_usd": 0.5}

        patch_claude_popen(self, reply)
        agents = ag.Agents(self.state, config or {})
        verdict = agents.review(dict(TASK), "OK", 1)
        return verdict, calls

    def test_giant_never_calls_the_reviewer(self):
        (self.root / "mod.py").write_text(
            "".join(f"строка {i}\n" for i in range(2001)))
        verdict, calls = self._run()
        self.assertEqual(calls, [], "гигант обязан обходиться без вызова")
        self.assertEqual(verdict["verdict"], "blocked",
                         "работу никто не судил — approve запрещён смыслом")
        self.assertEqual(verdict["triaged"], "giant-excluded",
                         "исключение обязано отличаться от вердикта руки")

    def test_exclusion_is_journalled_and_metered(self):
        # Новый файл: diff_lines — ровно число его строк (замена mod.py
        # дала бы +2 removed сверху, и проверять пришлось бы арифметику
        # фикстуры, а не факт журнала).
        (self.root / "big.py").write_text(
            "".join(f"строка {i}\n" for i in range(2001)))
        self._run()
        hits = [r for r in _journal(self.state) if r["kind"] == "giant_excluded"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reason"], "lines")
        self.assertEqual(hits[0]["diff_lines"], 2001)
        self.assertIn("diff_chars", hits[0])
        row = _review_rows(self.state)[-1]
        self.assertEqual(row["triage"], "giant-excluded")
        self.assertEqual((row["verdict"], row["cost_usd"]), ("blocked", 0.0))

    def test_chars_trigger_with_few_lines(self):
        """Одна строка на 200_001 символ — гигант по символам."""
        (self.root / "mod.py").write_text("x" * 200_001 + "\n")
        verdict, calls = self._run()
        self.assertEqual(calls, [])
        self.assertEqual(verdict["triaged"], "giant-excluded")
        hits = [r for r in _journal(self.state) if r["kind"] == "giant_excluded"]
        self.assertEqual(hits[0]["reason"], "chars")

    def test_giant_check_runs_before_the_band(self):
        """Гигантский docs_only НЕ пропускается молча: правило стоит до
        бэнды, и владелец получает эскалацию, а не synthetic approve."""
        (self.root / "README.md").write_text(
            "".join(f"строка {i}\n" for i in range(2001)))
        verdict, calls = self._run()
        self.assertEqual(calls, [])
        self.assertEqual(verdict["verdict"], "blocked")
        kinds = {r["kind"] for r in _journal(self.state)}
        self.assertIn("giant_excluded", kinds)
        self.assertNotIn("band_hit", kinds)

    def test_threshold_override_routes_normal_diff(self):
        """Порог из конфига действует: дифф на две строки при
        giant_max_lines=1 — гигант."""
        with (self.root / "mod.py").open("a") as f:
            f.write("    return 2\n    return 3\n")
        verdict, calls = self._run({"giant_max_lines": 1})
        self.assertEqual(calls, [])
        self.assertEqual(verdict["triaged"], "giant-excluded")

    def test_disabled_exclusion_calls_the_reviewer(self):
        """`giant_exclusion = false` — стенд, измеряющий гигантов: путь
        ревью не меняется ни на байт."""
        (self.root / "mod.py").write_text(
            "".join(f"строка {i}\n" for i in range(2001)))
        verdict, calls = self._run({"giant_exclusion": False})
        self.assertEqual(len(calls), 1)
        self.assertEqual(verdict["verdict"], "approve")
        kinds = {r["kind"] for r in _journal(self.state)}
        self.assertNotIn("giant_excluded", kinds)

    def test_classifier_failure_fails_open(self):
        """Сбой предиката стоит ноль: обычный путь триажа и ревью."""
        with (self.root / "mod.py").open("a") as f:
            f.write("    return 2\n")
        orig = rv.triage_mod.giant_reason
        rv.triage_mod.giant_reason = lambda *a, **k: 1 / 0
        self.addCleanup(lambda: setattr(rv.triage_mod, "giant_reason", orig))
        verdict, calls = self._run()
        self.assertEqual(len(calls), 1, "исключение предиката отменило ревью")
        self.assertEqual(verdict["verdict"], "approve")

    def test_default_boundary_diff_is_reviewed(self):
        """Ровно на пороге (2_000 строк) ревью идёт: порог строгий, и
        существующие 2000-строчные фикстуры — граница, а не гигант."""
        (self.root / "big.py").write_text(
            "".join(f"строка {i}\n" for i in range(2000)))
        verdict, calls = self._run()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(verdict.get("triaged"), "giant-excluded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
