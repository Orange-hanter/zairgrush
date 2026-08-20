#!/usr/bin/env python3
"""Тесты общего словаря событий.

Главное свойство здесь не красота фразы, а полнота: проза заменяет собой
дамп журнала, и если она теряет поля, человек делает вывод по неполной
записи, не зная об этом. Дамп был безобразен, но честен — замена обязана
быть честной тоже.
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("vocab", ROOT_DIR / "vocab.py")
vocab = importlib.util.module_from_spec(spec)
sys.modules["vocab"] = vocab
spec.loader.exec_module(vocab)


class TestNothingIsLost(unittest.TestCase):
    def test_unexpected_field_lands_in_the_tail(self):
        """Поле, которого шаблон не ждал, обязано появиться само."""
        text = vocab.narrate({"kind": "round", "task": "aaaa", "round": 1,
                              "verdict": "approve", "findings": 0,
                              "поле_из_будущего": "значение"})
        self.assertIn("поле_из_будущего=значение", text)

    def test_unknown_kind_keeps_its_name_and_payload(self):
        text = vocab.narrate({"kind": "такого_вида_нет", "task": "aaaa",
                              "деталь": 7})
        self.assertIn("такого_вида_нет", text)
        self.assertIn("деталь=7", text)

    def test_named_fields_are_not_duplicated_by_the_tail(self):
        """Названное шаблоном не повторяется — иначе фраза утонет в хвосте."""
        for kind, narrator in vocab.NARRATORS.items():
            _phrase, named = narrator({})
            row = {"kind": kind, **dict.fromkeys(named, "ЗНАЧ")}
            text = vocab.narrate(row)
            for key in named:
                self.assertNotIn(f"{key}=ЗНАЧ", text,
                                 f"{kind}: поле {key} продублировано хвостом")

    def test_frame_fields_stay_out_of_the_phrase(self):
        text = vocab.narrate({"kind": "round", "ts": "2026-08-16T10:00:00+00:00",
                              "run_id": "20260816T100000-abcdef", "task": "aaaa",
                              "round": 1, "verdict": "approve", "findings": 0})
        self.assertNotIn("run_id=", text)
        self.assertNotIn("ts=", text)

    def test_zero_is_a_fact_and_survives(self):
        """«находок нет» — результат раунда, а не пустое поле."""
        text = vocab.narrate({"kind": "round", "round": 2,
                              "verdict": "approve", "findings": 0})
        self.assertIn("находок нет", text)

    def test_every_narrator_survives_an_empty_row(self):
        """Журнал пишут аварийно и разными версиями — фраза обязана быть."""
        for kind in vocab.NARRATORS:
            with self.subTest(kind=kind):
                self.assertTrue(vocab.narrate({"kind": kind}))

    def test_every_narrator_survives_wrong_field_types(self):
        """Отчёт читают, когда уже сломано: падать ему нельзя.

        Поля журнала бывают не той формы — прежний формат записи, правка
        руками, обрезанная на аварии строка. Фраза обязана получиться.
        """
        junk = ["строка вместо списка", 42, None, [], {}, [{"нет": "issue"}],
                ["не словарь"], {"вложенный": {"словарь": 1}}]
        for kind, narrator in vocab.NARRATORS.items():
            _phrase, named = narrator({})
            for value in junk:
                row = {"kind": kind, **dict.fromkeys(named, value)}
                with self.subTest(kind=kind, value=value):
                    self.assertTrue(vocab.narrate(row))


class TestPhrases(unittest.TestCase):
    def test_round_reads_as_a_sentence(self):
        text = vocab.narrate({"kind": "round", "task": "aaaa", "round": 2,
                              "verdict": "request_changes", "findings": 3,
                              "intent": 1, "outcome": "ask_user"})
        self.assertIn("раунд 2 → request_changes", text)
        self.assertIn("находок 3", text)
        self.assertIn("о замысле 1", text)
        self.assertIn("вопрос человеку", text)

    def test_budget_names_the_task_it_stopped_before(self):
        text = vocab.narrate({"kind": "budget_exhausted", "spent": 51.2,
                              "budget": 50, "stopped_before": "v2wf"})
        self.assertIn("$51.2", text)
        self.assertIn("v2wf", text)

    def test_review_failure_says_the_work_is_intact(self):
        text = vocab.narrate({"kind": "review_failed", "task": "aaaa",
                              "round": 2, "why": "budget_exhausted",
                              "gate_passed": True, "stash": "swarm:aaaa-x"})
        self.assertIn("гейт при этом был зелёный", text)
        self.assertIn("swarm:aaaa-x", text)

    def test_signature_violation_names_round_and_changed(self):
        text = vocab.narrate({"kind": "signature_violation", "task": "f1x1",
                              "round": 2, "changed": ["add(a, b)",
                                                       "add(a, b, c)"]})
        self.assertIn("раунде 2", text)
        self.assertIn("add(a, b, c)", text)

    def test_deviations_declared_names_the_deviations(self):
        text = vocab.narrate({"kind": "deviations_declared", "task": "f1x1",
                              "round": 1, "deviations": ["не добавил тип X"]})
        self.assertIn("отступления", text)
        self.assertIn("не добавил тип X", text)

    def test_finding_names_severity_class_and_place(self):
        text = vocab.finding({"severity": "major", "category": "correctness",
                              "file": "src/a.py", "line": 12,
                              "issue": "не проверен None"})
        self.assertIn("важное", text)
        self.assertIn("корректность", text)
        self.assertIn("src/a.py:12", text)

    def test_unknown_code_shows_as_is_for_grep(self):
        """Незнакомый код не заменяется прочерком: его ищут в сыром журнале."""
        self.assertEqual(vocab.ru(vocab.SEVERITY_RU, "catastrophic"),
                         "catastrophic")


class TestOneVocabularyForAllSurfaces(unittest.TestCase):
    """Доска и терминал обязаны звать событие одинаково."""

    def test_board_calls_events_the_same_way(self):
        # Тождество (`assertIs`) здесь проверять нельзя: модули грузятся по
        # путям, и у каждого тестового файла свой экземпляр vocab. Равенство
        # ловит то, ради чего словарь и вынесен, — расхождение имён.
        s = importlib.util.spec_from_file_location("board", ROOT_DIR / "board.py")
        board = importlib.util.module_from_spec(s)
        sys.modules["board"] = board
        s.loader.exec_module(board)
        self.assertEqual(board.KIND_RU, vocab.KIND_RU)
        self.assertEqual(board.SEVERITY_RU, vocab.SEVERITY_RU)
        self.assertEqual(board.STATUS_RU, vocab.STATUS_RU)


class TestEveryEmittedKindIsNamed(unittest.TestCase):
    """Страж дрейфа: одно событие — одно имя (§9.3).

    Четыре вида журнала (gate_failed, scope_violation,
    executor_unavailable, state_written) жили без имени, и report/board
    показывали оператору сырой английский код ровно на тех событиях,
    которые он разбирает. Список ниже — ВСЕ виды, которые код петли
    пишет в журнал; новый log(...) обязан принести имя в KIND_RU.
    """

    EMITTED = [
        "answer", "baseline_red", "budget_exhausted", "commit_message",
        "deviations_declared", "executor_failed", "executor_unavailable",
        "gate_failed", "integrity_violation", "paths_extended", "plan_applied",
        "plan_failed", "policy", "policy_dropped", "policy_suppressed",
        "pre_existing_dirt", "preflight_forced", "question", "quota_pause", "run_dirt",
        "quota_wait", "restore_failed", "retry", "review_budget_exhausted",
        "review_failed", "round", "scope_violation", "signature_violation",
        "stash_failed", "state_written", "step_done", "step_failed",
        "step_intent", "task_crashed", "verification",
        "verification_inconclusive", "memory_written", "memory_injected",
        "memory_reflect", "memory_unavailable", "memory_forgotten",
        "memory_synced", "quota_resume", "round_futile",
        "futile_exhausted",
    ]

    def test_every_emitted_kind_has_a_russian_name(self):
        missing = [k for k in self.EMITTED if k not in vocab.KIND_RU]
        self.assertEqual(missing, [],
                         "вид журнала без имени: поверхность покажет "
                         "сырой код")


if __name__ == "__main__":
    unittest.main(verbosity=2)
