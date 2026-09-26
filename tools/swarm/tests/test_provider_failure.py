#!/usr/bin/env python3
"""AUD-003: отказ провайдера не штрафует руку ревью.

Вызов, умерший без вердикта по вине провайдера (квота, ложное
срабатывание safeguards, сеть, таймаут), оплачен — и обязан оставаться в
total_spend, иначе потолок прогона врёт. Но в знаменатель «мажоров на
доллар» руки он не идёт: рука к отказу отношения не имеет. Невалидный
ответ живой модели — провал руки, он в знаменателе.

Живой случай — d4rn на стенде zeus-pilot (2026-09-20): подтверждающий
раунд opus-5, $0.50 и 8 ходов, «safeguards flagged this message».
"""

import json
import pathlib

from tests import test_cli as tc, test_review_failure as _trf
from tests.test_review_failure import VALID, RepoCase, ag, patch_claude_popen

verdicts = _trf._load("verdicts")

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
D4RN = json.loads((FIXTURES / "d4rn-review-safeguard.json").read_text(encoding="utf-8"))

TASK = {
    "id": "t1",
    "title": "t",
    "spec": "s",
    "acceptance": ["гейт зелёный"],
    "paths": ["mod.py"],
    "type": "feature",
}


class TestClassifier(_trf.unittest.TestCase):
    def test_real_d4rn_envelope_is_a_safeguard(self):
        self.assertEqual(verdicts.provider_failure(D4RN, "crash"), "safeguard")

    def test_quota_wins_over_everything(self):
        env = {
            "is_error": True,
            "result": "usage limit reached",
            "api_error_status": 429,
        }
        self.assertEqual(verdicts.provider_failure(env, "done"), "quota")

    def test_generic_api_error_is_network(self):
        env = {
            "is_error": True,
            "terminal_reason": "api_error",
            "result": "API Error: overloaded",
            "total_cost_usd": 0.1,
        }
        self.assertEqual(verdicts.provider_failure(env, "crash"), "network")

    def test_silence_and_wall_clock_are_timeouts(self):
        for reason in ("silence", "wall_clock"):
            self.assertEqual(verdicts.provider_failure(None, reason), "timeout")

    def test_crash_without_envelope_is_network(self):
        self.assertEqual(verdicts.provider_failure(None, "crash"), "network")

    def test_live_model_answer_is_not_a_provider_failure(self):
        """Кривой вердикт от ответившей модели — вина руки, не провайдера."""
        env = {
            "is_error": False,
            "terminal_reason": "completed",
            "structured_output": {"verdict": "maybe"},
        }
        self.assertIsNone(verdicts.provider_failure(env, "done"))
        self.assertIsNone(verdicts.provider_failure({"x": 1}, "no_report"))


class TestMetricRowCarriesTheFailure(RepoCase):
    def _run(self, replies):
        it = iter(replies)
        patch_claude_popen(self, lambda argv: next(it))
        agents = ag.Agents(self.state, {})
        agents.review(dict(TASK), "OK", 1)
        return [
            json.loads(x)
            for x in self.state.metrics_path.read_text().splitlines()
            if json.loads(x).get("phase") == "review"
        ]

    def test_safeguard_row_is_marked_and_still_costs(self):
        rows = self._run([D4RN, {"structured_output": VALID, "total_cost_usd": 0.3}])
        self.assertEqual(rows[0]["failure"], "safeguard")
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.497869)
        self.assertIsNone(rows[1]["failure"], "валидный вердикт без метки")
        self.assertAlmostEqual(
            self.state.total_spend(),
            0.797869,
            places=2,
            msg="потолок прогона видит деньги отказа (сумма в центах)",
        )

    def test_invalid_answer_is_the_arms_failure(self):
        bad = {
            "structured_output": {"verdict": "maybe"},
            "total_cost_usd": 0.2,
            "terminal_reason": "completed",
        }
        rows = self._run([bad, {"structured_output": VALID, "total_cost_usd": 0.3}])
        self.assertEqual(rows[0]["failure"], "invalid_verdict")


class TestAbSeparatesLostMoney(tc.CliCase):
    def _review(self, **kw):
        row = {
            "phase": "review",
            "task": "aaaa",
            "iter": 1,
            "attempt": 1,
            "model": "opus",
            "effort": "high",
        }
        row.update(kw)
        self.state.metric(**row)

    def test_lost_line_and_denominator_without_provider_failures(self):
        self._review(
            valid=True,
            verdict="request_changes",
            cost_usd=1.0,
            findings=2,
            majors=2,
            blockers=0,
            minors=0,
        )
        self._review(attempt=2, valid=False, cost_usd=1.0, failure="safeguard")
        self._review(iter=2, valid=False, cost_usd=1.0, failure="invalid_verdict")
        _, out = tc.run_cli("--root", str(self.root), "ab")
        self.assertIn("потеряно на отказах провайдера: $1.00 в 1", out)
        self.assertIn("safeguard 1", out)
        # 2 мажора на $2: валидный вызов + провал руки; отказ провайдера
        # в знаменатель не вошёл (иначе было бы 0.67).
        self.assertIn("1.00", out.split("мажоров/$")[1])
        self.assertIn("исключено отказов провайдера: 1", out)

    def test_old_row_is_classified_from_its_envelope(self):
        """Строка до AUD-003 поля failure не несёт — причина берётся из
        сохранённого конверта, как у d4rn на стенде."""
        self._review(
            valid=True,
            verdict="approve",
            cost_usd=1.0,
            findings=0,
            majors=0,
            blockers=0,
            minors=0,
        )
        self._review(
            iter=2,
            attempt=2,
            valid=False,
            cost_usd=0.497869,
            run_reason="crash",
            terminal_reason="api_error",
        )
        (self.state.dir / "log").mkdir(exist_ok=True)
        (self.state.dir / "log" / "aaaa-i2-a2-review.json").write_text(
            json.dumps(D4RN), encoding="utf-8"
        )
        _, out = tc.run_cli("--root", str(self.root), "ab")
        self.assertIn(
            "aaaa: итерация 2, попытка 2, модель opus — safeguard, $0.50", out
        )

    def test_old_row_without_envelope_stays_with_the_arm(self):
        self._review(valid=True, verdict="approve", cost_usd=1.0, findings=0)
        self._review(iter=2, valid=False, cost_usd=0.5, run_reason="done")
        _, out = tc.run_cli("--root", str(self.root), "ab")
        self.assertNotIn("потеряно на отказах", out)
