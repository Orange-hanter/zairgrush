#!/usr/bin/env python3
"""Тесты решений цикла: intent-триаж, подтверждения, несходимость.

Механика заимствована у FuguNano и здесь фиксируется тестами, потому что
она определяет, когда петля дёргает человека, — а это самое дорогое
действие системы.
"""
import importlib.util
import pathlib
import sys
import tempfile
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
        loop.sh = lambda cmd, timeout=900: type(
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


class TestExecutorUnavailable(unittest.TestCase):
    """Регрессия на пилот: квота Kimi кончилась, процесс умирал на старте
    (59 байт потока, секунды), а петля жгла об это по три раунда на задачу
    и каскадом прошлась по очереди с диагнозом «слишком крупная»."""

    def _run(self, failure, n_tasks=1):
        state = _FakeState()
        statuses = []
        state.set_status = lambda tid, st, **k: statuses.append((tid, st, k))

        class FakeAgents:
            last_implement_failure = failure

            def implement(self, task, feedback, iteration):
                return None

            def review(self, *a, **k):
                raise AssertionError("до ревью дойти не должно")

        loop = lp.Loop(state, {}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.cleanup = lambda task, reason: None
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        return loop, statuses

    def test_instant_crash_stops_instead_of_burning_rounds(self):
        loop, statuses = self._run(
            {"reason": "crash", "wall_s": 0.3, "events": 1,
             "stderr": "403 usage limit"})
        with self.assertRaises(lp.ExecutorUnavailableError) as ctx:
            loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                           "type": "feature"})
        self.assertIn("403", str(ctx.exception),
                      "stderr провайдера обязан дойти до человека")
        self.assertIn(("t1", "pending",
                       {"reason": "executor_unavailable"}), statuses,
                      "задача ни в чём не виновата — обратно в очередь")

    def test_budget_truncation_tells_the_executor_to_continue(self):
        """Совет обязан лечить ту болезнь, что была.

        Обрыв по потолку стоимости — не нарушение контракта: правки уже
        на диске, оборвался отчёт. Совет «повтори, соблюдая контракт»
        посылает исполнителя переделывать сделанное, то есть платить за
        раунд дважды — а именно этот раунд и обрубили за дороговизну.
        """
        notes = []
        state = _FakeState()

        class FakeAgents:
            last_implement_failure = {"reason": "budget_exhausted",
                                      "wall_s": 800.0, "events": 1900}

            def implement(self, task, feedback, iteration):
                notes.append((feedback or {}).get("note"))
                return

            def review(self, *a, **k):
                raise AssertionError("до ревью дойти не должно")

        loop = lp.Loop(state, {}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.cleanup = lambda task, reason: None
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                       "type": "feature"})
        follow = [n for n in notes if n]
        self.assertTrue(follow, "второй раунд обязан получить совет")
        self.assertIn("ЦЕЛЫ", follow[0])
        self.assertNotIn("контракт", follow[0])

    def test_slow_crash_still_burns_rounds(self):
        """Авария в середине настоящей работы — не «недоступен»: процесс
        жил, события шли. Такое честно стоит раунда (s2ky, раунд 3)."""
        loop, _statuses = self._run(
            {"reason": "crash", "wall_s": 300.0, "events": 58,
             "stderr": ""})
        result = loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                                "type": "feature"})
        self.assertEqual(result, "blocked")

    def test_run_stops_queue_and_reports(self):
        """Следующая задача умерла бы так же: очередь стоит, ключ _executor
        в итогах говорит оператору, что чинить надо среду, а не задачи."""
        loop, _ = self._run(
            {"reason": "crash", "wall_s": 0.2, "events": 0,
             "stderr": "403 usage limit"})
        loop.state.ready_tasks = lambda: [
            {"id": "t1", "title": "t", "paths": ["a.py"], "type": "feature"}]
        loop.state.total_spend = lambda: 0.0
        loop.refresh_board = lambda: None
        results = loop.run()
        self.assertEqual(results.get("_executor"), "unavailable")
        self.assertNotIn("t1", results,
                         "задача не получила ложного исхода")


class TestScopeBurnoutDiagnosis(unittest.TestCase):
    """Регрессия на пилот: k3ad и s2ky сгорели на границах, а диагноз
    сказал «задача слишком крупная — расщепить». Расщепление не помогло
    бы: любой осколок упёрся бы в тот же защищённый файл."""

    def test_repeated_scope_burn_names_the_files(self):
        text = lp.Loop._diagnose(
            lp.ESCALATE_MAX, [],
            scope_failures=[["tests/corpus.rs"], ["tests/corpus.rs"]])
        self.assertIn("границ", text)
        self.assertIn("tests/corpus.rs", text)
        self.assertNotIn("расщеп", text.split("Расщепление не поможет")[0],
                         "нельзя предлагать резать то, что упрётся туда же")

    def test_single_scope_burn_is_not_yet_a_pattern(self):
        """Один срыв — ещё не паттерн: диагноз не объявляет границы
        причиной, но и не подменяет их догадкой о размере."""
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, [],
                                 scope_failures=[["tests/x.rs"]])
        self.assertNotIn("сгорели на нарушении границ", text)
        self.assertNotIn("расщепить", text)


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
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, history)
        self.assertNotIn("разошлись", text)
        self.assertIn("Траектория", text, "нет причины — предъяви факты")

    def test_non_confirming_round_is_not_a_disagreement(self):
        """Между обычными раундами работает исполнитель: дифф ДРУГОЙ, и
        смена вердикта ничего не говорит о ревьюерах."""
        history = [round_record(2, 2, ["tests"], kind="approve"),
                   round_record(3, 4, ["correctness"])]
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, history)
        self.assertNotIn("разошлись", text)

    def test_empty_history_says_no_verdict_was_reached(self):
        """Раньше здесь стояла догадка о размере. Пустая история значит
        ровно одно: судить было нечего — так и надо сказать."""
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, [])
        self.assertIn("ни один не дошёл до вердикта", text)
        self.assertNotIn("расщепить", text)


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

    def test_max_rounds_without_evidence_reports_the_trail_not_a_guess(self):
        """Золотой набор PILOT-1: «задача слишком крупная» была неверна
        6 раз из 6. Без механической улики диагноз обязан предъявить
        траекторию и честно сказать, что причины он не нашёл."""
        history = [round_record(1, 2, ["tests"]),
                   round_record(2, 2, ["correctness"])]
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, history)
        self.assertIn("Траектория", text)
        self.assertIn("механической причины петля не нашла", text)
        self.assertNotIn("расщепить", text)

    def test_wide_unshrinking_front_is_where_size_is_earned(self):
        """Единственный случай, когда размер — вывод из данных, а не
        догадка: широкий фронт находок, который не сужается."""
        history = [round_record(1, 5, ["tests", "correctness", "style"]),
                   round_record(2, 6, ["tests", "architecture", "scope"])]
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, history)
        self.assertIn("расщепление действительно правдоподобно", text)

    def test_executor_failures_blame_the_environment(self):
        """k3ad и s2ky: раунды сгорели на квоте и таймауте исполнителя —
        работа не дошла до ревью ни разу, а обвинили размер задачи."""
        text = lp.Loop._diagnose(
            lp.ESCALATE_MAX, [],
            exec_failures=[{"round": 1, "reason": "wall_clock"},
                           {"round": 2, "reason": "crash"}])
        self.assertIn("на стороне исполнителя", text)
        self.assertIn("wall_clock", text)
        self.assertIn("crash", text)
        self.assertNotIn("расщепить", text)

    def test_scope_beats_executor_when_both_present(self):
        """Порядок улик: точная причина важнее общей. Границы называют
        файл, аварии — только среду."""
        text = lp.Loop._diagnose(
            lp.ESCALATE_MAX, [],
            scope_failures=[["a.rs"], ["a.rs"]],
            exec_failures=[{"round": 1, "reason": "crash"},
                           {"round": 2, "reason": "crash"}])
        self.assertIn("границ", text)

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
        loop.sh = lambda cmd, timeout=900: type(
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

    def test_feature_may_edit_explicitly_listed_test(self):
        """Регрессия на пилот (k3ad, s2ky — шесть сгоревших раундов).

        Задача меняет поведение правила, чьё число диагностик пришпилено
        существующим тестом, и тест ЯВНО назван в paths. Старая проверка
        снимала защиту по типу задачи, а не по явности, — и ни один тип
        не позволял «поменять код и обновить пришпиленный к нему тест».
        """
        task = {"id": "t", "type": "feature", "paths": ["src/a.py", "tests/x.py"]}
        ok, bad, protected = self._loop(["tests/x.py"]).scope_check(task)
        self.assertTrue(ok, (bad, protected))

    def test_broad_glob_does_not_unlock_protection(self):
        """Широкий глоб покрывает тесты, но не целится в них: `src и всё
        вокруг` не должен молча открывать анти-gaming (§5.5)."""
        task = {"id": "t", "type": "feature", "paths": ["**"]}
        ok, _, protected = self._loop(["tests/x.py"]).scope_check(task)
        self.assertFalse(ok)
        self.assertEqual(protected, ["tests/x.py"])

    def test_feature_may_not_touch_unlisted_tests(self):
        """Защита осталась защитой: тест, которого нет в paths, — чужой."""
        task = {"id": "t", "type": "feature", "paths": ["src/a.py"]}
        ok, bad, touched = self._loop(["tests/x.py"]).scope_check(task)
        self.assertFalse(ok)
        self.assertEqual(bad, ["tests/x.py"])
        self.assertEqual(touched, [])

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
        loop.sh = lambda cmd, timeout=900: type(
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


class TestConfirmationsBindToDiff(unittest.TestCase):
    """Подтверждение — свойство ОДНОГО состояния кода, а не задачи.

    Пока approvals считались по всей истории, цепочка approve → флип на
    подтверждающем раунде → исправление → approve закрывала задачу,
    финальный код которой видел ровно один ревьюер: первый approve
    относился к уже снесённому диффу, но шёл в счёт подтверждений.
    """

    def test_approve_of_new_diff_needs_its_own_confirmation(self):
        history = [
            {"round": 1, "verdict": "approve", "findings": 0,
             "categories": [], "diff_sha": "aaa"},
            {"round": 2, "verdict": "request_changes", "findings": 1,
             "categories": ["style"], "confirming": True, "diff_sha": "aaa"},
        ]
        outcome, _ = lp.decide(3, verdict("approve"), history, max_rounds=5,
                               diff_sha="bbb")
        self.assertEqual(outcome, lp.CONFIRM,
                         "approve чужого диффа не подтверждает новый код")

    def test_second_approve_of_same_diff_closes(self):
        history = [
            {"round": 1, "verdict": "approve", "findings": 0,
             "categories": [], "diff_sha": "aaa"},
        ]
        outcome, code = lp.decide(2, verdict("approve"), history,
                                  max_rounds=5, diff_sha="aaa")
        self.assertEqual(outcome, lp.DONE)
        self.assertEqual(code, lp.EXIT_OK)

    def test_histories_without_sha_keep_old_semantics(self):
        """Старые записи без diff_sha (прогон до этой правки) не должны
        ломать resume: без отпечатка счёт остаётся прежним."""
        history = [{"round": 1, "verdict": "approve", "findings": 0,
                    "categories": []}]
        outcome, _ = lp.decide(2, verdict("approve"), history, max_rounds=5)
        self.assertEqual(outcome, lp.DONE)


class TestNonConvergenceIgnoresConfirmRounds(unittest.TestCase):
    """Сходимость меряется по исправительным раундам.

    Подтверждающий ревьюит ТОТ ЖЕ дифф — с пулами моделей часто другой
    рукой жребия, — и его находки говорят о разбросе ревьюеров, а не о
    динамике задачи. Сравнение с ним объявляло несходимость там, где
    исполнитель ещё ничего не менял.
    """

    def test_confirming_round_never_declares_nonconvergence(self):
        history = [round_record(1, 1, ["style"])]
        outcome, _ = lp.decide(
            2, verdict(findings=[finding("style")]), history,
            max_rounds=5, confirming=True)
        self.assertNotEqual(outcome, lp.ESCALATE_NONCONV)

    def test_comparison_skips_confirming_predecessor(self):
        history = [
            {"round": 1, "verdict": "approve", "findings": 0,
             "categories": []},
            {"round": 2, "verdict": "request_changes", "findings": 1,
             "categories": ["style"], "confirming": True},
        ]
        outcome, _ = lp.decide(
            3, verdict(findings=[finding("style")]), history, max_rounds=5)
        self.assertNotEqual(outcome, lp.ESCALATE_NONCONV,
                            "сравнение с подтверждающим раундом — шум "
                            "ревьюеров, а не динамика задачи")


class TestKeepBestRollback(unittest.TestCase):
    """Откат к лучшему раунду обязан согласовать дерево и feedback.

    Два дефекта одним сюжетом: (1) findings худшего раунда уходили
    исполнителю ПОСЛЕ отката — про код, которого в дереве уже нет, и
    раунд тратился на починку призраков; (2) approve с лишними minor —
    не регресс: откатить одобренное дерево и закоммитить вместо него
    прошлый раунд значило бы подменить предмет вердикта.
    """

    def _run(self, verdicts, diffs):
        feedbacks = []
        restores = []
        calls = {"implement": 0, "review": 0}
        state = _FakeState()
        state.work_diff = lambda: diffs[max(0, calls["review"] - 1)]

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                calls["implement"] += 1
                feedbacks.append(feedback)
                return {"status": "done"}

            def review(self, task, tail, iteration, **kw):
                idx = min(calls["review"], len(verdicts) - 1)
                calls["review"] += 1
                return verdicts[idx]

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(state, {"max_iterations": 3, "confirmations": 2},
                       FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop._apply_patch = lambda diff: bool(restores.append(diff)) or True
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        result = loop.run_task({"id": "kb", "title": "t", "paths": ["a.py"],
                                "type": "feature"})
        return result, feedbacks, restores

    def test_feedback_after_rollback_speaks_about_the_best_round(self):
        rc1 = verdict(findings=[finding("style")])
        rc2 = verdict(findings=[finding("correctness"), finding("tests")])
        ok = verdict("approve")
        _, feedbacks, restores = self._run(
            [rc1, rc2, ok, ok], diffs=("d1", "d2", "d3", "d3"))
        self.assertEqual(restores, ["d1"], "дерево возвращено к лучшему")
        fb = feedbacks[2]
        self.assertIn("лучшему раунду 1", fb.get("note", ""),
                      "исполнителю обязаны сказать, какой код он увидит")
        self.assertEqual(fb["findings"], rc1["findings"],
                         "замечания — по коду в дереве, а не по снесённому")

    def test_approve_with_extra_minors_is_not_a_regression(self):
        rc1 = verdict(findings=[finding("style")])
        ok2 = verdict("approve", findings=[finding("style"),
                                           finding("tests")])
        result, _, restores = self._run(
            [rc1, ok2, ok2], diffs=("d1", "d2", "d2"))
        self.assertEqual(restores, [],
                         "одобренное дерево не откатывается")
        self.assertEqual(result, "done")


class TestFileSignatures(unittest.TestCase):
    """E10: `pyindex.file_signatures` — точечный снимок одного файла для
    стража frozen_signatures. Полный `Index` строит граф ссылок по всему
    дереву; страж спрашивает про один и тот же файл каждый раунд, и гонять
    полную сборку ради него значило бы платить за проверку впустую."""

    def test_extracts_sorted_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "mod.py"
            p.write_text("def b(x):\n    pass\n\n\ndef a(y, z):\n    pass\n")
            self.assertEqual(lp.pyindex.file_signatures(p),
                             ["a(y, z)", "b(x)"])

    def test_includes_nested_and_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "mod.py"
            p.write_text("class C:\n    def m(self):\n"
                         "        def inner():\n            pass\n")
            sigs = lp.pyindex.file_signatures(p)
            self.assertIn("m(self)", sigs)
            self.assertIn("inner()", sigs)

    def test_missing_file_is_empty_not_a_crash(self):
        """Битый/недоступный файл — факт о файле, не авария наблюдателя
        (тот же принцип, что у Index._build)."""
        self.assertEqual(
            lp.pyindex.file_signatures(pathlib.Path("/нет/такого/файла.py")),
            [])

    def test_syntax_error_is_empty_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "broken.py"
            p.write_text("def f(:\n")
            self.assertEqual(lp.pyindex.file_signatures(p), [])


class TestSignatureGuard(unittest.TestCase):
    """E10: сигнатуры скелета — контракт fill-задачи, не предмет спора
    исполнителя. Guard снимает снимок ДО первого вызова исполнителя и
    сверяет после КАЖДОГО успешного scope_check; несовпадение жжёт раунд
    БЕЗ отката — в отличие от scope, тело внутри пришпиленной сигнатуры
    может быть спасаемо."""

    def _run(self, tmp, initial, write_bodies, confirmations=1):
        calls = {"implement": 0}
        journal = []
        feedbacks = []
        reverted = []
        state = _FakeState()
        state.root = tmp
        state.log = lambda kind, **k: journal.append((kind, k))
        (tmp / "mod.py").write_text(initial)

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                calls["implement"] += 1
                feedbacks.append(feedback)
                idx = min(calls["implement"], len(write_bodies)) - 1
                (tmp / "mod.py").write_text(write_bodies[idx])
                return {"status": "done"}

            def review(self, task, tail, iteration, **kw):
                return verdict("approve")

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(state, {"max_iterations": 3,
                               "confirmations": confirmations}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop.revert = lambda: reverted.append(1) or []
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        task = {"id": "f1x1", "title": "t", "type": "feature",
               "paths": ["mod.py"], "frozen_signatures": True}
        result = loop.run_task(task)
        return result, journal, feedbacks, calls, reverted

    def test_violation_burns_a_round_without_revert_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = pathlib.Path(tmp_s)
            result, journal, feedbacks, calls, reverted = self._run(
                tmp, "def add(a, b):\n    raise NotImplementedError\n",
                ["def add(a, b, c):\n    return a + b + c\n",
                 "def add(a, b):\n    return a + b\n"])
            self.assertEqual(result, "done",
                             "фиксированный контракт закрывает задачу")
            self.assertEqual(calls["implement"], 2,
                             "нарушение не пропускает следующий вызов "
                             "исполнителя — заливка может быть спасена")
            kinds = [k for k, _ in journal]
            self.assertIn("signature_violation", kinds)
            payload = next(p for k, p in journal if k == "signature_violation")
            self.assertEqual(payload["round"], 1)
            self.assertIn("add(a, b, c)", payload["changed"])
            self.assertEqual(reverted, [], "сигнатуры не откатывают дерево")
            fb = feedbacks[1]
            self.assertIn("changed_signatures", fb)
            self.assertIn("add(a, b, c)", fb["changed_signatures"])
            self.assertIn("сигнатуры контракта изменены", fb["note"])

    def test_persistent_violation_escalates_naming_signatures(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = pathlib.Path(tmp_s)
            state = _FakeState()
            state.root = tmp
            asked = []
            state.ask = lambda tid, kind, question, **k: (
                asked.append(question), "q001")[1]
            (tmp / "mod.py").write_text(
                "def add(a, b):\n    raise NotImplementedError\n")

            class FakeAgents:
                def implement(self, task, feedback, iteration):
                    (tmp / "mod.py").write_text(
                        f"def add(a, b, x{iteration}):\n    pass\n")
                    return {"status": "done"}

                def review(self, *a, **k):
                    raise AssertionError("до ревью дойти не должно")

                def commit_message(self, task, diff):
                    return "msg"

            loop = lp.Loop(state, {"max_iterations": 3}, FakeAgents())
            loop.gate = lambda task: (True, "OK")
            loop.scope_check = lambda task: (True, [], [])
            loop.cleanup = lambda task, reason: None
            loop.sh = lambda cmd, timeout=900: type(
                "R", (), {"stdout": "", "returncode": 0})()
            result = loop.run_task({"id": "f1x1", "title": "t",
                                    "type": "feature", "paths": ["mod.py"],
                                    "frozen_signatures": True})
            self.assertEqual(result, "blocked")
            self.assertTrue(asked, "эскалация обязана попасть в инбокс")
            self.assertIn("сигнатур", asked[-1])
            self.assertIn("замороженных", asked[-1])

    def test_no_snapshot_without_frozen_signatures(self):
        """Обычная задача не платит за снимок, которого не просила."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = pathlib.Path(tmp_s)
            state = _FakeState()
            state.root = tmp
            journal = []
            state.log = lambda kind, **k: journal.append((kind, k))
            (tmp / "mod.py").write_text("def add(a, b):\n    return 0\n")

            class FakeAgents:
                def implement(self, task, feedback, iteration):
                    (tmp / "mod.py").write_text(
                        "def add(a, b, c):\n    return a + b + c\n")
                    return {"status": "done"}

                def review(self, task, tail, iteration, **kw):
                    return verdict("approve")

                def commit_message(self, task, diff):
                    return "msg"

            loop = lp.Loop(state, {"confirmations": 1}, FakeAgents())
            loop.gate = lambda task: (True, "OK")
            loop.scope_check = lambda task: (True, [], [])
            loop.commit = lambda task: "abc123"
            loop.cleanup = lambda task, reason: None
            loop.sh = lambda cmd, timeout=900: type(
                "R", (), {"stdout": "", "returncode": 0})()
            result = loop.run_task({"id": "t1", "title": "t",
                                    "type": "feature", "paths": ["mod.py"]})
            self.assertEqual(result, "done")
            self.assertNotIn("signature_violation", [k for k, _ in journal])


class TestSignatureDiagnosisPriority(unittest.TestCase):
    """`_diagnose` называет сигнатуры РАНЬШЕ границ: контракт fill-задачи
    нарушен явно, и общая причина не может быть точнее известной."""

    def test_repeated_signature_burn_is_named_first(self):
        text = lp.Loop._diagnose(
            lp.ESCALATE_MAX, [],
            sig_failures=[["add(a, b, c)"], ["add(a, b, c)"]])
        self.assertIn("сигнатур", text)
        self.assertIn("add(a, b, c)", text)

    def test_single_signature_burn_is_not_yet_a_pattern(self):
        """Один срыв — не паттерн; но и догадкой о размере его не
        подменяют (золотой набор: 0 из 6 таких догадок были верны)."""
        text = lp.Loop._diagnose(lp.ESCALATE_MAX, [],
                                 sig_failures=[["add(a, b, c)"]])
        self.assertNotIn("сгорели на нарушении", text)
        self.assertNotIn("расщепить", text)

    def test_signatures_outrank_scope_when_both_repeat(self):
        text = lp.Loop._diagnose(
            lp.ESCALATE_MAX, [],
            scope_failures=[["tests/x.rs"], ["tests/x.rs"]],
            sig_failures=[["add(a, b, c)"], ["add(a, b, c)"]])
        self.assertIn("сигнатур", text)
        self.assertNotIn("tests/x.rs", text,
                         "при известной точной причине общая не называется")


class TestFillTaskDisputeHint(unittest.TestCase):
    """Спор fill-задачи чаще всего означает сломанный контракт скелета, не
    саму заливку: подсказка экономит круг ручного разбора — оператор сразу
    знает, какую задачу пересматривать."""

    def _run(self, task):
        state = _FakeState()
        questions = []

        def fake_ask(tid, kind, question, **k):
            questions.append(question)
            return "q001"
        state.ask = fake_ask

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                return {"status": "dispute", "summary": "не могу",
                       "dispute": {"claim": "конфликт"}}

            def review(self, *a, **k):
                raise AssertionError("до ревью дойти не должно")

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(state, {}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.cleanup = lambda task, reason: None
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        loop.run_task(task)
        return questions

    def test_fill_task_dispute_hints_replan_target(self):
        task = {"id": "fill1", "title": "t", "type": "feature",
               "paths": ["a.py"], "executor_model": "ollama:kimi-k2.7-code",
               "deps": ["skl1"]}
        questions = self._run(task)
        self.assertIn("swarm replan skl1", questions[0])

    def test_plain_task_dispute_has_no_hint(self):
        task = {"id": "t1", "title": "t", "type": "feature", "paths": ["a.py"]}
        questions = self._run(task)
        self.assertNotIn("swarm replan", questions[0])

    def test_fill_task_without_deps_has_no_hint(self):
        """executor_model в одиночку ничего не значит — подсказывать
        задачу-скелет нечем, если deps пуст."""
        task = {"id": "fill1", "title": "t", "type": "feature",
               "paths": ["a.py"], "executor_model": "ollama:kimi-k2.7-code"}
        questions = self._run(task)
        self.assertNotIn("swarm replan", questions[0])


class TestDeviationsJournaling(unittest.TestCase):
    """Отступления — заявление исполнителя О СЕБЕ; ревьюер их не видит
    (асимметрия §3), а журнал принимает их как данные, а не контракт."""

    def _run(self, report):
        state = _FakeState()
        journal = []
        state.log = lambda kind, **k: journal.append((kind, k))

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                return report

            def review(self, task, tail, iteration, **kw):
                return verdict("approve")

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(state, {"confirmations": 1}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        loop.run_task({"id": "t1", "title": "t", "type": "feature",
                       "paths": ["a.py"]})
        return journal

    def test_list_of_deviations_is_journaled(self):
        journal = self._run({"status": "done",
                             "deviations": ["не добавил тип X", "упростил Y"]})
        entry = next(p for k, p in journal if k == "deviations_declared")
        self.assertEqual(entry["deviations"],
                         ["не добавил тип X", "упростил Y"])
        self.assertEqual(entry["round"], 1)

    def test_string_deviation_is_wrapped_into_one_element_list(self):
        journal = self._run({"status": "done", "deviations": "одно отступление"})
        entry = next(p for k, p in journal if k == "deviations_declared")
        self.assertEqual(entry["deviations"], ["одно отступление"])

    def test_empty_list_is_not_journaled(self):
        journal = self._run({"status": "done", "deviations": []})
        self.assertNotIn("deviations_declared", [k for k, _ in journal])

    def test_blank_string_is_not_journaled(self):
        journal = self._run({"status": "done", "deviations": "   "})
        self.assertNotIn("deviations_declared", [k for k, _ in journal])

    def test_malformed_shape_does_not_crash(self):
        """Журнал читается как данные: dict вместо списка/строки не
        роняет петлю — просто не журналируется."""
        journal = self._run({"status": "done",
                             "deviations": {"не": "тот тип"}})
        self.assertNotIn("deviations_declared", [k for k, _ in journal])

    def test_missing_field_does_not_crash(self):
        journal = self._run({"status": "done"})
        self.assertNotIn("deviations_declared", [k for k, _ in journal])

    def test_deviations_are_not_fed_to_the_reviewer(self):
        """Асимметрия §3: канал одностороннего действия — исполнитель
        заявляет о себе журналу, а не ревьюеру."""
        seen_tails = []

        state = _FakeState()

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                return {"status": "done", "deviations": ["упростил X"]}

            def review(self, task, tail, iteration, **kw):
                seen_tails.append(tail)
                return verdict("approve")

            def commit_message(self, task, diff):
                return "msg"

        loop = lp.Loop(state, {"confirmations": 1}, FakeAgents())
        loop.gate = lambda task: (True, "проход гейта")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        loop.run_task({"id": "t1", "title": "t", "type": "feature",
                       "paths": ["a.py"]})
        self.assertTrue(seen_tails)
        self.assertNotIn("упростил X", seen_tails[0],
                         "ревьюер не должен видеть заявленные отступления")
