#!/usr/bin/env python3
"""Тесты решений цикла: intent-триаж, подтверждения, несходимость.

Механика заимствована у FuguNano и здесь фиксируется тестами, потому что
она определяет, когда петля дёргает человека, — а это самое дорогое
действие системы.
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

ANALYSIS = "Разобрал дифф построчно и сверил с критериями приёмки задачи."
SUMMARY = "Изменение соответствует задаче, критичных замечаний нет."


def finding(category="correctness", severity="minor", intent=None):
    f = {"file": "a.py", "severity": severity, "category": category,
         "confidence": 0.7, "issue": "что-то не так"}
    if intent is not None:
        f["intent"] = intent
    return f


def verdict(kind="request_changes", findings=()):
    return {"analysis": ANALYSIS, "verdict": kind, "summary": SUMMARY,
            "findings": list(findings), "out_of_scope_notes": []}


def round_record(n, findings, categories, kind="request_changes"):
    return {"round": n, "verdict": kind, "findings": findings,
            "categories": categories}


class TestIntentTriage(unittest.TestCase):
    """Человека дёргают только по находкам о замысле."""

    def test_architecture_is_intent(self):
        intent, mech = lp.classify_findings([finding("architecture")])
        self.assertEqual(len(intent), 1)
        self.assertEqual(mech, [])

    def test_scope_is_intent(self):
        intent, _ = lp.classify_findings([finding("scope")])
        self.assertEqual(len(intent), 1)

    def test_style_and_tests_are_mechanical(self):
        _, mech = lp.classify_findings([finding("style"), finding("tests")])
        self.assertEqual(len(mech), 2)

    def test_explicit_flag_overrides_category(self):
        intent, mech = lp.classify_findings([finding("style", intent=True)])
        self.assertEqual(len(intent), 1, "явная пометка ревьюера сильнее "
                                         "категории по умолчанию")
        self.assertEqual(mech, [])

    def test_explicit_false_keeps_mechanical(self):
        intent, mech = lp.classify_findings([finding("architecture", intent=False)])
        self.assertEqual(intent, [])
        self.assertEqual(len(mech), 1)

    def test_empty_is_safe(self):
        self.assertEqual(lp.classify_findings(None), ([], []))


class TestConfirmations(unittest.TestCase):
    """Один approve — не терминал: верификация вероятностна."""

    def test_first_approve_asks_for_confirmation(self):
        outcome, code = lp.decide(1, verdict("approve"), [])
        self.assertEqual(outcome, lp.CONFIRM)
        self.assertEqual(code, lp.EXIT_WORKING)

    def test_second_approve_completes(self):
        history = [round_record(1, 0, [], kind="approve")]
        outcome, code = lp.decide(2, verdict("approve"), history)
        self.assertEqual(outcome, lp.DONE)
        self.assertEqual(code, lp.EXIT_OK)

    def test_single_confirmation_mode(self):
        outcome, _ = lp.decide(1, verdict("approve"), [], confirmations=1)
        self.assertEqual(outcome, lp.DONE)


class TestAskUser(unittest.TestCase):
    def test_intent_finding_asks_human(self):
        outcome, code = lp.decide(1, verdict(findings=[finding("architecture")]), [])
        self.assertEqual(outcome, lp.ASK_USER)
        self.assertEqual(code, lp.EXIT_ASK_USER)

    def test_mechanical_only_continues(self):
        outcome, code = lp.decide(1, verdict(findings=[finding("style")]), [])
        self.assertEqual(outcome, lp.CONTINUE)
        self.assertEqual(code, lp.EXIT_WORKING)

    def test_mixed_findings_ask_human(self):
        v = verdict(findings=[finding("style"), finding("architecture")])
        self.assertEqual(lp.decide(1, v, [])[0], lp.ASK_USER)


class TestNonConvergence(unittest.TestCase):
    """Повтор того же класса — повод для диагноза, а не для ретрая."""

    def test_same_categories_not_shrinking_escalates(self):
        history = [round_record(1, 2, ["correctness"])]
        v = verdict(findings=[finding("correctness"), finding("correctness")])
        outcome, code = lp.decide(2, v, history)
        self.assertEqual(outcome, lp.ESCALATE_NONCONV)
        self.assertEqual(code, lp.EXIT_ESCALATE)

    def test_shrinking_findings_continue(self):
        history = [round_record(1, 3, ["correctness"])]
        v = verdict(findings=[finding("correctness")])
        self.assertEqual(lp.decide(2, v, history)[0], lp.CONTINUE)

    def test_different_class_continues(self):
        history = [round_record(1, 1, ["correctness"])]
        v = verdict(findings=[finding("style")])
        self.assertEqual(lp.decide(2, v, history)[0], lp.CONTINUE)

    def test_max_rounds_escalates(self):
        v = verdict(findings=[finding("style")])
        outcome, code = lp.decide(3, v, [], max_rounds=3)
        self.assertEqual(outcome, lp.ESCALATE_MAX)
        self.assertEqual(code, lp.EXIT_ESCALATE)

    def test_reviewer_blocked_escalates(self):
        v = verdict("blocked", [finding("correctness", "blocker")])
        self.assertEqual(lp.decide(1, v, [])[1], lp.EXIT_ESCALATE)


class _FakeState:
    """Минимальное состояние для сквозных прогонов run_task."""

    root = "."

    def changed_files(self):
        return []

    def work_diff(self):
        return ""

    def set_status(self, *a, **k):
        self.last = (a, k)

    def log(self, *a, **k):
        pass

    def metric(self, **k):
        pass

    def policies(self):
        return []

    def ask(self, *a, **k):
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


class TestRoundBudgetIsOneNumber(unittest.TestCase):
    """Найдено на пилоте (e7in), стоило задаче эскалации.

    Цикл выдаёт подтверждающим раундам добавку к лимиту
    (`while iteration < max_iter + confirm_rounds`), а решение принимала
    функция, считавшая по голому `max_iter`. Две записи одного лимита
    расходились ровно на число выданных подтверждений.
    """

    def _run(self, verdicts, max_iterations=3):
        """Вердикты выдаются по счётчику РЕВЬЮ, а не исполнений: именно это
        и позволяет описать подтверждающий раунд, где исполнителя нет."""
        calls = {"implement": 0, "review": 0}
        state = _FakeState()

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                calls["implement"] += 1
                return {"status": "done"}

            def review(self, task, tail, iteration, **kw):
                idx = min(calls["review"], len(verdicts) - 1)
                calls["review"] += 1
                return verdicts[idx]

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(state, {"max_iterations": max_iterations,
                               "confirmations": 2}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop._sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        result = loop.run_task({"id": "e7in", "title": "t", "paths": ["a.py"],
                                "type": "feature"})
        return result, calls

    def test_flip_in_confirmation_does_not_burn_the_last_round(self):
        """Ровно сценарий e7in: request_changes -> approve -> подтверждающий
        раунд сменил вердикт. Цикл выдал добавку под подтверждение, значит
        раунд исправлений ещё есть — исполнитель обязан быть вызван снова,
        а не получить «раунды исчерпаны»."""
        v = self._v
        result, calls = self._run([v("request_changes", 2),
                                   v("approve"),
                                   v("request_changes", 4),
                                   v("approve"), v("approve")])
        self.assertEqual(calls["implement"], 3,
                         "после расхождения обязан быть раунд исправлений")
        self.assertNotEqual(result, "blocked",
                            "задачу нельзя эскалировать по лимиту, который "
                            "цикл сам же и расширил")

    def _v(self, kind, n_findings=0, category="correctness"):
        return {"analysis": "подробный разбор диффа по критериям приёмки",
                "verdict": kind, "summary": "итоговая оценка изменений",
                "findings": [finding(category) for _ in range(n_findings)]}

    def test_real_exhaustion_still_escalates(self):
        """Обратная сторона: добавка не должна превращаться в бесконечность."""
        v = verdict(findings=[finding("style")])
        self.assertEqual(lp.decide(4, v, [], max_rounds=4)[0], lp.ESCALATE_MAX)


class TestDisagreementIsNotTaskSize(unittest.TestCase):
    """Диагноз обязан называть только то, что диагност может знать.

    Подтверждающий раунд ревьюет ТОТ ЖЕ дифф: исполнитель не вызывался,
    код не менялся. Смена вердикта там — свойство ревьюеров. Пока это
    объявлялось «задача слишком крупная — расщепить», человека посылали
    резать задачу, одобренную раундом раньше.
    """

    @staticmethod
    def _confirming(n, findings, kind, model=None, effort=None):
        row = round_record(n, findings, ["correctness"], kind=kind)
        row["confirming"] = True
        if model:
            row["model"], row["effort"] = model, effort
        return row

    def test_flip_on_the_same_diff_is_named_as_such(self):
        history = [round_record(2, 2, ["tests"], kind="approve"),
                   self._confirming(3, 4, "request_changes")]
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, history)
        self.assertIn("разошлись", text)
        self.assertNotIn("расщепить", text,
                         "нельзя предлагать резать одобренную задачу")

    def test_diagnosis_names_both_arms(self):
        """Расхождение без указания рук нечем проверить: замер по моделям
        (§8.1) живёт ровно в этих двух строках."""
        history = [
            round_record(2, 2, ["tests"], kind="approve")
            | {"model": "claude-sonnet-5", "effort": "xhigh"},
            self._confirming(3, 4, "request_changes",
                             "claude-opus-5", "medium"),
        ]
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, history)
        self.assertIn("claude-sonnet-5/xhigh", text)
        self.assertIn("claude-opus-5/medium", text)

    def test_same_verdict_twice_is_not_a_disagreement(self):
        history = [round_record(2, 2, ["tests"]),
                   self._confirming(3, 2, "request_changes")]
        self.assertIn("расщепить", lp.Loop._diagnose(lp.ESCALATE_MAX, history))

    def test_non_confirming_round_is_not_a_disagreement(self):
        """Между обычными раундами работает исполнитель: дифф ДРУГОЙ, и
        смена вердикта ничего не говорит о ревьюерах."""
        history = [round_record(2, 2, ["tests"], kind="approve"),
                   round_record(3, 4, ["correctness"])]
        self.assertIn("расщепить", lp.Loop._diagnose(lp.ESCALATE_MAX, history))

    def test_empty_history_keeps_the_old_hypothesis(self):
        self.assertIn("расщепить", lp.Loop._diagnose(lp.ESCALATE_MAX, []))


class TestExitCodes(unittest.TestCase):
    """Коды возврата — машинный контракт для внешнего скрипта."""

    def test_codes_are_distinct(self):
        codes = {lp.EXIT_OK, lp.EXIT_WORKING, lp.EXIT_ASK_USER, lp.EXIT_ESCALATE}
        self.assertEqual(len(codes), 4)

    def test_ok_is_zero(self):
        self.assertEqual(lp.EXIT_OK, 0, "успех обязан быть нулём для shell")

    def test_human_needed_codes_differ(self):
        self.assertNotEqual(lp.EXIT_ASK_USER, lp.EXIT_ESCALATE,
                            "«ответь на вопрос» и «разберись целиком» — "
                            "разные обращения к человеку")


class TestDiagnosis(unittest.TestCase):
    """Эскалация несёт версию о причине, а не голый факт."""

    def test_max_rounds_suggests_splitting(self):
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, [])
        self.assertIn("расщепить", text)

    def test_repeated_class_suggests_spec_or_reviewer(self):
        history = [round_record(1, 2, ["style"]), round_record(2, 2, ["style"])]
        text = lp.Loop._diagnose(lp.ESCALATE_NONCONV, history)
        self.assertTrue("неясно" in text or "строже" in text)

    def test_growing_findings_suggests_rewrite(self):
        history = [round_record(1, 1, ["style"]), round_record(2, 3, ["tests"])]
        text = lp.Loop._diagnose(lp.ESCALATE_NONCONV, history)
        self.assertIn("качели", text)


class TestScopeCheck(unittest.TestCase):
    """Приёмка на tenacity: feature-tests обязан писать свои тесты."""

    class FakeState:
        def __init__(self, changed):
            self.changed = changed
            self.root = "."
            self.metrics = []

        def metric(self, **row):
            self.metrics.append(row)

        def changed_files(self):
            return list(self.changed)

        def work_diff(self):
            return "".join(f"--- a/{p}\n+++ b/{p}\n" for p in self.changed)

    def _loop(self, changed):
        state = self.FakeState(changed)
        loop = lp.Loop(state, {}, None)
        loop._sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "\n".join(f" M {p}" for p in changed),
                      "returncode": 0})()
        return loop

    def test_feature_tests_may_write_own_tests(self):
        task = {"id": "t", "type": "feature-tests",
                "paths": ["src/a.py", "tests/test_new.py"]}
        loop = self._loop(["src/a.py", "tests/test_new.py"])
        ok, bad, protected = loop.scope_check(task)
        self.assertTrue(ok, "собственные тесты задачи не защищены от неё же")
        self.assertEqual((bad, protected), ([], []))

    def test_feature_may_not_touch_tests(self):
        task = {"id": "t", "type": "feature", "paths": ["src/a.py", "tests/x.py"]}
        ok, _, protected = self._loop(["tests/x.py"]).scope_check(task)
        self.assertFalse(ok)
        self.assertEqual(protected, ["tests/x.py"])

    def test_file_outside_paths_is_rejected(self):
        task = {"id": "t", "type": "feature-tests", "paths": ["src/a.py"]}
        ok, bad, _ = self._loop(["src/other.py"]).scope_check(task)
        self.assertFalse(ok)
        self.assertEqual(bad, ["src/other.py"])

    def test_scope_result_is_logged(self):
        task = {"id": "t", "type": "feature-tests", "paths": ["src/a.py"]}
        loop = self._loop(["src/a.py"])
        loop.scope_check(task)
        self.assertTrue(loop.state.metrics, "без метрики диагностика слепа")
        self.assertEqual(loop.state.metrics[0]["phase"], "scope")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestConfirmDoesNotConsumeBudget(unittest.TestCase):
    """Конфликт двух механик, найденный на приёмке.

    Требование двух подтверждений и лимит раундов сталкивались: approve на
    последнем раунде было невозможно подтвердить, и задача уходила в
    эскалацию вместо закрытия.
    """

    def _run(self, verdicts):
        calls = {"implement": 0}

        class FakeState:
            def changed_files(self):
                return []

            def work_diff(self):
                return ""
            root = "."

            def set_status(self, *a, **k):
                self.last = (a, k)

            def log(self, *a, **k):
                pass

            def metric(self, **k):
                pass

            def policies(self):
                return []

            def ask(self, *a, **k):
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

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                calls["implement"] += 1
                return {"status": "done"}

            def review(self, task, tail, iteration, **kw):
                idx = min(calls["implement"], len(verdicts)) - 1
                return verdicts[idx]

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(FakeState(), {}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop._sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        result = loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                                "type": "feature"})
        return result, calls["implement"]

    def _v(self, kind, n_findings=0, category="style"):
        return {"analysis": "подробный разбор диффа по критериям приёмки",
                "verdict": kind, "summary": "итоговая оценка изменений",
                "findings": [finding(category) for _ in range(n_findings)],
                "out_of_scope_notes": []}

    def test_approve_on_last_round_still_closes(self):
        # два раунда правок, approve на третьем — подтверждение должно
        # получить дополнительный раунд, а не упереться в лимит
        verdicts = [self._v("request_changes", 2), self._v("request_changes", 1),
                    self._v("approve"), self._v("approve")]
        result, rounds = self._run(verdicts)
        self.assertEqual(result, "done",
                         "approve на последнем раунде обязан быть подтверждён")
        self.assertEqual(rounds, 3,
                         "три раунда правок; подтверждение идёт повторным "
                         "ревью и вызова исполнителя не стоит")

    def test_fix_rounds_still_limited(self):
        # категории разные и число находок убывает — детект несходимости не
        # срабатывает, и задача доходит ровно до лимита исправлений
        verdicts = [self._v("request_changes", 3, "style"),
                    self._v("request_changes", 2, "tests"),
                    self._v("request_changes", 1, "correctness")]
        result, rounds = self._run(verdicts)
        self.assertEqual(result, "blocked", "лимит исправлений остаётся жёстким")
        self.assertEqual(rounds, lp.MAX_ITER)

    def test_nonconvergence_stops_earlier_than_limit(self):
        rc = self._v("request_changes", 2, "style")
        result, rounds = self._run([rc, rc, rc])
        self.assertEqual(result, "blocked")
        self.assertLess(rounds, lp.MAX_ITER,
                        "повтор того же класса обрывает цикл раньше лимита")
