#!/usr/bin/env python3
"""Пурист: находит развилки спецификации и не заполняет их (E14).

Это дешёвая КОНТРгипотеза к двум дорогим ролям, и потому она обязана
падать быстро и не стоить ничего, когда выключена. Первое свойство,
которое здесь проверяется, — именно второе: с флагом off промпт
исполнителя байт-в-байт прежний, иначе плечи E8/E10/E11 перестают быть
сравнимыми с уже замеренными.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import promptbuilder  # noqa: E402
import state as state_mod  # noqa: E402
import unclear  # noqa: E402

TASK = {"id": "u1", "title": "t", "spec": "спека", "paths": ["a.py"],
        "type": "feature", "acceptance": ["сьют проходит"],
        "status": "pending"}
ITEM = {"question": "сохраняется ли порядок при равных частотах?",
        "why_it_matters": "порядок результата расходится"}


def _agents(config):
    root = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    st = state_mod.SwarmState(root)
    st.save_tasks({"goal": "цель", "tasks": [TASK]})

    class A:
        pass

    a = A()
    a.state = st
    a.config = config
    a.work_root = root
    a.unclear_cache = None
    a.map_cache = None
    a.codemap = None
    a.memory_cache = None
    a.norms_cache = None
    return a


class TestFlagOffChangesNothing(unittest.TestCase):

    def test_disabled_by_default(self):
        self.assertFalse(unclear.enabled({}))
        self.assertFalse(unclear.enabled({"experiments": {}}))

    def test_prompt_is_byte_identical_without_the_block(self):
        """Промпт исполнителя — предмет замера и кэша провайдера. Новая
        подсистема не имеет права его тронуть, пока выключена."""
        a = _agents({})
        before = promptbuilder.handoff(a, TASK, None, None)
        after = promptbuilder.handoff(a, TASK, None, None, unclear=None)
        self.assertEqual(before, after)
        self.assertNotIn("does NOT decide", before)

    def test_empty_list_produces_no_block(self):
        """Строка «пробелов нет» платится токенами каждый раунд и не
        говорит исполнителю ничего нового."""
        self.assertEqual(unclear.block({"unclear": [], "summary": "полна"}),
                         "")
        self.assertEqual(unclear.block(None), "")

    def test_items_without_a_question_are_dropped(self):
        """Журнал читается как данные: строка не той формы молчит, а не
        роняет промпт."""
        self.assertEqual(unclear.block({"unclear": [{"why_it_matters": "x"},
                                                    "строка", None]}), "")


class TestBlock(unittest.TestCase):

    def test_block_lands_in_the_prompt_after_acceptance(self):
        a = _agents({"experiments": {"unclear": True}})
        block = unclear.block({"unclear": [ITEM]})
        text = promptbuilder.handoff(a, TASK, None, None, unclear=block)
        self.assertIn("does NOT decide", text)
        self.assertLess(text.index("Acceptance:"), text.index("does NOT"))

    def test_block_tells_the_executor_to_declare_the_choice(self):
        """Механика E14 целиком в этом требовании: молчаливое изобретение
        превращается в ОБЪЯВЛЕННОЕ, и только это можно посчитать."""
        block = unclear.block({"unclear": [ITEM]})
        self.assertIn("deviations", block)

    def test_block_is_capped(self):
        many = [{"question": f"развилка {i}?", "why_it_matters": "x"}
                for i in range(20)]
        block = unclear.block({"unclear": many})
        self.assertEqual(block.count("- развилка"), unclear.MAX_ITEMS)
        self.assertIn("и ещё", block)

    def test_why_it_matters_is_carried_not_dropped(self):
        block = unclear.block({"unclear": [ITEM]})
        self.assertIn("порядок результата расходится", block)


class TestPromptDiscipline(unittest.TestCase):
    """Промпт пуриста — то место, где ложные срабатывания и рождаются."""

    def test_prompt_forbids_answering(self):
        text = unclear.prompt(TASK, "цель")
        self.assertIn("Do NOT answer the questions", text)

    def test_prompt_calls_an_empty_list_a_success(self):
        text = unclear.prompt(TASK, "цель")
        self.assertIn("empty list is a correct", text)

    def test_prompt_forbids_gaps_found_by_reading_code(self):
        """Пробел, найденный чтением реализации, — не молчание спеки, а
        пересказ чужого выбора. Та же дисциплина, что у тестировщика."""
        text = unclear.prompt(TASK, "цель")
        self.assertIn("READ THE CODE", text)

    def test_prompt_carries_the_spec_and_acceptance(self):
        text = unclear.prompt(TASK, "цель")
        self.assertIn("спека", text)
        self.assertIn("сьют проходит", text)

    def test_schema_requires_the_list_and_forbids_answers(self):
        schema = json.loads((HERE.parent / "schemas"
                             / "unclear-v1.schema.json").read_text())
        self.assertEqual(set(schema["required"]), {"unclear", "summary"})
        item = schema["properties"]["unclear"]["items"]["properties"]
        self.assertNotIn("answer", item)
        self.assertNotIn("recommendation", item)


class TestReportShapeIsNotTheExecutors(unittest.TestCase):
    """Форма отчёта ИСПОЛНИТЕЛЯ навязывалась всем ролям.

    `report_from_envelope` требовал поля `status`, которого у пуриста
    нет и быть не должно: он отдаёт список развилок, а не исход. На
    первом же настоящем прогоне (2026-08-24) пурист нашёл ровно ту
    развилку, ради которой ставился замер, — и петля выбросила ответ,
    заплатив за него, и сказала человеку «развилок не оставила».
    Сказать «нет пробелов» вместо «нет ответа» — худший из возможных
    выводов: он звучит как знание.
    """

    def test_purist_report_survives_without_a_status_field(self):
        import engines
        env = {"structured_output": {"unclear": [ITEM], "summary": "есть"}}
        got = engines.report_from_envelope(env, "unclear")
        self.assertIsNotNone(got)
        self.assertEqual(len(got["unclear"]), 1)

    def test_executor_contract_is_still_the_default(self):
        import engines
        env = {"structured_output": {"status": "done", "summary": "s"}}
        self.assertIsNotNone(engines.report_from_envelope(env))
        self.assertIsNone(engines.report_from_envelope(
            {"structured_output": {"summary": "нет статуса"}}))

    def test_wrong_shape_is_still_refused(self):
        """Параметр ослабляет проверку ровно на одно поле, а не снимает
        её: чужой объект по-прежнему не считается отчётом."""
        import engines
        env = {"structured_output": {"status": "done", "summary": "s"}}
        self.assertIsNone(engines.report_from_envelope(env, "unclear"))


if __name__ == "__main__":
    unittest.main()


J4VS = HERE / "fixtures" / "j4vs-unclear-envelope.json"
# Хвост вопроса j4vs — ровно то, что съедала обрезка журнала до 160 знаков.
J4VS_TAIL = "как пустой массив [], или отсутствует в документе вовсе"


class TestJournalReachesTheHuman(unittest.TestCase):
    """AUD-002: находка пуриста адресована человеку — журнал обязан
    держать её целиком, а поверхности вывода — показывать текстом."""

    def test_all_items_with_all_fields(self):
        """Промпт получает MAX_ITEMS, журнал — всё: блок сам отсылает
        туда за остатком («…и ещё N, см. журнал»)."""
        many = [dict(ITEM, question=f"вопрос {n}?", where=f"§{n}")
                for n in range(unclear.MAX_ITEMS + 3)]
        items = unclear.journal_items({"unclear": many})
        self.assertEqual(len(items), unclear.MAX_ITEMS + 3)
        self.assertEqual(items[-1], {"question": f"вопрос {unclear.MAX_ITEMS + 2}?",
                                     "why_it_matters": ITEM["why_it_matters"],
                                     "where": f"§{unclear.MAX_ITEMS + 2}"})

    def test_malformed_items_are_dropped_not_raised(self):
        for bad in (None, "строка", {"unclear": "не список"},
                    {"unclear": [None, "x", {"question": " "},
                                 {"why_it_matters": "w"}]}):
            self.assertEqual(unclear.journal_items(bad), [], repr(bad))

    def test_real_j4vs_question_survives_whole(self):
        """Живой случай аудита: сырой поток пуриста j4vs (стенд
        zeus-pilot, 2026-09-20) → запись журнала → фраза отчёта. Суть
        вопроса — в его конце, и она обязана дойти."""
        import engines
        import vocab
        env = json.loads(J4VS.read_text(encoding="utf-8"))
        report = engines.report_from_envelope(env, "unclear")
        items = unclear.journal_items(report)
        self.assertEqual(len(items), 1)
        self.assertIn(J4VS_TAIL, items[0]["question"])
        self.assertIn("DOC-10", items[0]["why_it_matters"])
        self.assertIn("docs/09-erc.md §9", items[0]["where"])
        line = vocab.narrate({"kind": "unclear_found", "task": "j4vs",
                              "count": 1, "items": items})
        self.assertIn(J4VS_TAIL, line)
        self.assertIn("зависит:", line)

    def test_block_is_unchanged_by_the_journal(self):
        """Кэш блока для промпта байт-стабилен: журнал — отдельный путь."""
        env = json.loads(J4VS.read_text(encoding="utf-8"))
        import engines
        report = engines.report_from_envelope(env, "unclear")
        before = unclear.block(report)
        unclear.journal_items(report)
        self.assertEqual(unclear.block(report), before)


class TestOldJournalRowsStillRead(unittest.TestCase):
    """Журнал — данные: записи до AUD-002 несут `questions` строками."""

    def test_old_questions_are_shown(self):
        import vocab
        row = {"kind": "unclear_found", "task": "j4vs", "count": 1,
               "questions": ["обрезанный вопрос присутс"]}
        self.assertIn("обрезанный вопрос присутс", vocab.narrate(row))
        self.assertEqual(vocab.unclear_items(row),
                         [{"question": "обрезанный вопрос присутс"}])

    def test_wrong_shapes_do_not_crash_the_phrase(self):
        import vocab
        for items in ("строка", [None, 3, {"question": ""}], {"a": 1}, 7):
            line = vocab.narrate({"kind": "unclear_found", "count": 0,
                                  "items": items})
            self.assertIn("пурист", line, repr(items))
