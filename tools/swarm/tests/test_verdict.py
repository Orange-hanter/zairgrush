#!/usr/bin/env python3
"""Тесты контрактов: валидация вердикта, отказ по квоте, разбор отчёта.

Написаны после мутационного аудита: он показал, что дефект «валидатор
пропускает approve с blocker» проходил незамеченным. Причина — тесты
конечного автомата остались привязаны к прототипу и при консолидации не
переехали, из-за чего главная механическая защита от «взаимных
комплиментов» в собранной системе не проверялась вовсе.
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("loop", ROOT_DIR / "loop.py")
lp = importlib.util.module_from_spec(spec)
sys.modules["loop"] = lp
spec.loader.exec_module(lp)

ANALYSIS = "Разобрал дифф построчно и сверил его с критериями приёмки задачи."
SUMMARY = "Изменение соответствует задаче, критичных замечаний нет."


def finding(severity="minor", category="correctness"):
    return {"file": "a.py", "severity": severity, "category": category,
            "confidence": 0.7, "issue": "описание проблемы", "suggestion": "как"}


def verdict(kind="approve", findings=(), analysis=ANALYSIS, summary=SUMMARY):
    return {"analysis": analysis, "verdict": kind, "summary": summary,
            "findings": list(findings), "out_of_scope_notes": []}


class TestVerdictForm(unittest.TestCase):
    """Закрытые enum'ы §12: неизвестное значение = невалидный вердикт."""

    def test_valid_approve(self):
        self.assertTrue(lp.validate_verdict(verdict()))

    def test_unknown_verdict_value(self):
        self.assertFalse(lp.validate_verdict(verdict("lgtm")))

    def test_unknown_severity(self):
        self.assertFalse(lp.validate_verdict(
            verdict("request_changes", [finding(severity="critical")])))

    def test_unknown_category(self):
        self.assertFalse(lp.validate_verdict(
            verdict("request_changes", [finding(category="perf")])))

    def test_non_dict_rejected(self):
        for bad in (None, [], "approve", 42):
            self.assertFalse(lp.validate_verdict(bad))

    def test_missing_verdict_key(self):
        v = verdict()
        del v["verdict"]
        self.assertFalse(lp.validate_verdict(v))


class TestApproveGuard(unittest.TestCase):
    """Главная механическая защита от «взаимных комплиментов» (§10).

    Ровно этот дефект мутационный аудит пронёс мимо тестов.
    """

    def test_approve_with_blocker_rejected(self):
        self.assertFalse(lp.validate_verdict(
            verdict("approve", [finding("blocker")])))

    def test_approve_with_major_rejected(self):
        self.assertFalse(lp.validate_verdict(
            verdict("approve", [finding("major")])))

    def test_approve_with_minor_allowed(self):
        self.assertTrue(lp.validate_verdict(
            verdict("approve", [finding("minor")])))

    def test_approve_with_mixed_rejected(self):
        self.assertFalse(lp.validate_verdict(
            verdict("approve", [finding("minor"), finding("blocker")])))

    def test_request_changes_with_blocker_allowed(self):
        self.assertTrue(lp.validate_verdict(
            verdict("request_changes", [finding("blocker")])))


class TestVerdictMeaning(unittest.TestCase):
    """Схема гарантирует форму, но не смысл (урок VERIFY-1)."""

    def test_stub_reply_rejected(self):
        self.assertFalse(lp.validate_verdict(
            {"analysis": "test", "verdict": "request_changes", "summary": "test",
             "findings": [], "out_of_scope_notes": []}))

    def test_request_changes_without_findings_rejected(self):
        self.assertFalse(lp.validate_verdict(verdict("request_changes")))

    def test_blocked_without_findings_rejected(self):
        self.assertFalse(lp.validate_verdict(verdict("blocked")))

    def test_approve_without_findings_allowed(self):
        self.assertTrue(lp.validate_verdict(verdict("approve")))

    def test_short_analysis_rejected(self):
        self.assertFalse(lp.validate_verdict(verdict(analysis="ок")))

    def test_short_summary_rejected(self):
        self.assertFalse(lp.validate_verdict(verdict(summary="ок")))

    def test_whitespace_analysis_rejected(self):
        self.assertFalse(lp.validate_verdict(verdict(analysis="   " * 20)))


class TestQuotaDetection(unittest.TestCase):
    """Отказ провайдера ≠ невалидный ответ агента (§5.3)."""

    def test_session_limit_is_quota(self):
        self.assertIsNotNone(lp.quota_error(
            {"is_error": True, "result": "You've hit your session limit"}))

    def test_rate_limit_variants(self):
        for msg in ("Rate limit exceeded", "HTTP 429 Too Many Requests",
                    "usage limit reached", "quota exhausted"):
            self.assertIsNotNone(lp.quota_error({"is_error": True, "result": msg}),
                                 msg)

    def test_ordinary_error_is_not_quota(self):
        self.assertIsNone(lp.quota_error(
            {"is_error": True, "result": "Model returned malformed JSON"}))

    def test_budget_error_is_not_quota(self):
        """Бюджетный отказ похож по форме, но лечится иначе — не паузой."""
        self.assertIsNone(lp.quota_error(
            {"is_error": True, "result": None,
             "subtype": "error_max_budget_usd"}))

    def test_success_is_not_quota(self):
        self.assertIsNone(lp.quota_error({"is_error": False, "result": "{}"}))

    def test_non_dict_safe(self):
        for bad in (None, [], "session limit"):
            self.assertIsNone(lp.quota_error(bad))


class _LoopHarness:
    """Общий стенд на mock-агентах: FakeState/FakeAgents и `_loop`.

    Не наследует unittest.TestCase намеренно — иначе второй класс на том
    же стенде (TestQuestionIsVisibleInOutput) унаследовал бы и все test_*
    отсюда, и каждый прогонялся бы дважды под разными именами.
    """

    def setUp(self):
        self.events = []
        outer = self

        class FakeState:
            def changed_files(self):
                return []

            def work_diff(self):
                return ""
            root = "."

            def set_status(self, tid, status, **fields):
                outer.events.append(("status", status, fields.get("reason")))

            def log(self, kind, **k):
                outer.events.append(("log", kind, None))

            def metric(self, **k):
                pass

            def policies(self):
                return []

            def ask(self, task_id, kind, question, **ctx):
                outer.events.append(("ask", kind, None))
                return "q001"

            def step(self, *a, **k):
                import contextlib

                @contextlib.contextmanager
                def noop():
                    class S:
                        def result(self, **kw):
                            pass
                    yield S()
                return noop()

        self.FakeState = FakeState

    def _loop(self, implement=None, gate=None, scope=None, review=None):

        class FakeAgents:
            def __init__(self):
                self.calls = {"implement": 0, "review": 0}

            def implement(self, task, feedback, iteration):
                self.calls["implement"] += 1
                seq = implement or []
                idx = min(self.calls["implement"], len(seq)) - 1
                return seq[idx] if seq else {"status": "done"}

            def review(self, task, tail, iteration, **kw):
                self.calls["review"] += 1
                seq = review or []
                idx = min(self.calls["review"], len(seq)) - 1
                return seq[idx] if seq else verdict("approve")

            def commit_message(self, task, diff):
                return "msg"

        agents = FakeAgents()
        # Захват self.ui(...) отдельным списком: ветки петли печатают ход
        # прогона человеку, а не только меняют состояние, и это тоже
        # часть контракта — без записи текстов проверить нечем.
        self.ui_messages: list[str] = []

        def capture(*args: object) -> None:
            self.ui_messages.append(" ".join(str(a) for a in args))

        loop = lp.Loop(self.FakeState(), {}, agents, ui=capture)
        gates = list(gate or [])

        def fake_gate(task):
            return gates.pop(0) if gates else (True, "OK")

        loop.gate = fake_gate
        loop.scope_check = lambda task: (scope or (True, [], []))
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: f"stash:{reason}"
        loop._sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        return loop, agents

    TASK = {"id": "t1", "title": "t", "paths": ["a.py"], "type": "feature"}


class TestLoopBranches(_LoopHarness, unittest.TestCase):
    """Аварийные ветки конечного автомата на mock-агентах."""

    def test_red_baseline_skips_agents(self):
        loop, agents = self._loop(gate=[(False, "FAILED")])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        self.assertEqual(agents.calls["implement"], 0,
                         "красный baseline не должен стоить ни одного вызова")

    def test_dispute_skips_review(self):
        loop, agents = self._loop(implement=[{"status": "dispute"}])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        self.assertEqual(agents.calls["review"], 0)
        self.assertIn(("ask", "dispute", None), self.events)

    def test_failed_gate_skips_review(self):
        """Красный gate не должен стоить ни одного вызова ревьюера.

        Сформулировано прямо, через ноль вызовов: сравнение счётчиков
        review и implement перестало быть корректной мерой, когда
        подтверждающий раунд стал повторным ревью без исполнителя.
        """
        # baseline зелёный, дальше все итерации красные
        loop, agents = self._loop(gate=[(True, "OK"), (False, "FAILED"),
                                        (False, "FAILED"), (False, "FAILED")])
        loop.run_task(dict(self.TASK))
        self.assertEqual(agents.calls["review"], 0,
                         "красная итерация не должна стоить вызова ревьюера")
        self.assertEqual(agents.calls["implement"], lp.MAX_ITER,
                         "исполнитель при этом честно отрабатывает лимит")

    def test_confirm_round_reviews_without_executor(self):
        """Подтверждение — повторное ревью того же диффа.

        Пока оно шло через исполнителя, тот мог изменить код между двумя
        голосами, а его сбой (no_report) съедал раунд и отправлял
        одобренную задачу в эскалацию — так v3st дважды заблокировалась
        на приёмке при зелёном гейте.
        """
        loop, agents = self._loop(review=[verdict("approve"),
                                          verdict("approve")])
        self.assertEqual(loop.run_task(dict(self.TASK)), "done")
        self.assertEqual(agents.calls["implement"], 1)
        self.assertEqual(agents.calls["review"], 2,
                         "второй голос обязан быть, но без нового круга работы")

    def test_scope_violation_reverts(self):
        loop, agents = self._loop(scope=(False, ["other.py"], []))
        result = loop.run_task(dict(self.TASK))
        self.assertEqual(result, "blocked")
        self.assertEqual(agents.calls["review"], 0,
                         "нарушение границ не доходит до ревьюера")

    def test_invalid_verdict_is_fail_closed(self):
        loop, _ = self._loop(review=[None])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        reasons = [e[2] for e in self.events if e[0] == "status"]
        self.assertIn("invalid_verdict", reasons,
                      "невалидный вердикт никогда не трактуется как approve")

    def test_no_report_retries_then_escalates(self):
        loop, agents = self._loop(implement=[None, None, None])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        self.assertEqual(agents.calls["implement"], lp.MAX_ITER)
        self.assertEqual(agents.calls["review"], 0)

    def test_reviewer_blocked_escalates(self):
        loop, _ = self._loop(review=[verdict("blocked", [finding("blocker")])])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        self.assertIn(("ask", "escalate_max", None), self.events)


class TestQuestionIsVisibleInOutput(_LoopHarness, unittest.TestCase):
    """«спор исполнителя [q011]» называет id, а не то, о чём спор: чтобы
    узнать содержание, оператор был вынужден открывать `.swarm/log`
    посреди прогона. Ветки, где петля заводит вопрос человеку, обязаны
    печатать сам текст, а не только его номер.
    """

    def test_dispute_prints_question_snippet_and_inbox_hint(self):
        loop, _ = self._loop(implement=[
            {"status": "dispute",
             "summary": "задача противоречит контракту скелета"}])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        joined = "\n".join(self.ui_messages)
        self.assertIn("задача противоречит контракту скелета", joined)
        self.assertIn("swarm inbox", joined)

    def test_ask_user_prints_question_snippet_and_inbox_hint(self):
        loop, _ = self._loop(review=[verdict(
            "request_changes", [finding(category="architecture")])])
        self.assertEqual(loop.run_task(dict(self.TASK)), "ask_user")
        joined = "\n".join(self.ui_messages)
        self.assertIn("описание проблемы", joined,
                      "issue находки — сама формулировка вопроса")
        self.assertIn("swarm inbox", joined)

    def test_snippet_is_truncated_not_dumped_whole(self):
        """~160 символов — черновой предел строки в терминале, а не
        точное число: тест проверяет усечение, а не конкретную длину."""
        long_text = "деталь. " * 60
        loop, _ = self._loop(
            implement=[{"status": "dispute", "summary": long_text}])
        loop.run_task(dict(self.TASK))
        snippet_lines = [m for m in self.ui_messages if "деталь." in m]
        self.assertTrue(snippet_lines)
        self.assertLess(len(snippet_lines[0]), len(long_text))

    def test_diagnosis_carrying_branches_are_left_alone(self):
        """escalate_max уже печатает диагноз — второй виток той же
        информации не добавляется этой веткой изменений."""
        loop, _ = self._loop(review=[
            verdict("request_changes", [finding()]),
            verdict("request_changes", [finding()]),
            verdict("request_changes", [finding()])])
        self.assertEqual(loop.run_task(dict(self.TASK)), "blocked")
        joined = "\n".join(self.ui_messages)
        self.assertNotIn("swarm inbox", joined,
                         "у эскалации свой диагноз, не вопрос-заглушка")


if __name__ == "__main__":
    unittest.main(verbosity=2)
