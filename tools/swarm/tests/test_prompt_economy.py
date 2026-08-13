#!/usr/bin/env python3
"""Экономика промптов: порядок блоков, объём вывода, дисциплина объёма.

Разбор трат PILOT-1 по семи вызовам ревьюера ($12.15) дал такую картину:

    запись в кэш   $5.43  (45%)   входная ставка × 2 — часовой TTL
    выходные токены $3.59  (30%)   впятеро дороже входной ставки
    чтение из кэша $3.13  (26%)   входная ставка × 0.1

Отсюда три правки, проверяемые здесь.

ПОРЯДОК БЛОКОВ. Кэш промптов совпадает по ПРЕФИКСУ: первый разошедшийся
байт обнуляет всё после себя. `review_prompt` ставил самый изменчивый блок
(`verify_block`) ПЕРВЫМ, поэтому подтверждающий раунд — ревью того же
диффа тем же промптом — записывал 37 696 токенов заново вместо чтения.
Тесты проверяют не «красивый порядок», а длину общего префикса: это она
превращается в деньги.

ОБЪЁМ ВЫВОДА. На `p1fn` ревьюер написал 31 881 выходной токен — $0.80,
почти половину стоимости вызова. Уровень усилия эту статью не лечит,
лечит инструкция в промпте.

ДИСЦИПЛИНА ОБЪЁМА У ИСПОЛНИТЕЛЯ. Меньше кода — меньше дифф — дешевле
ревью. С обязательной оговоркой: экономия не распространяется на
валидацию, обработку ошибок и безопасность.
"""
import importlib.util
import os
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ag = _load("agents")


def common_prefix(a, b):
    """Длина общего префикса — ровно то, что кэшируется."""
    return len(os.path.commonprefix([a, b]))


class PromptCase(unittest.TestCase):
    """Промпты строятся без состояния на диске: подставляем минимум."""

    def setUp(self):
        self.agents = ag.Agents.__new__(ag.Agents)
        self.agents.config = {}
        self.agents.state = type("S", (), {
            "load_tasks": staticmethod(lambda: {"goal": "цель прогона"}),
        })()

    TASK = {"id": "t1", "title": "первая задача", "spec": "сделать A",
            "acceptance": ["критерий один", "критерий два"],
            "paths": ["mod.py"], "type": "feature"}
    OTHER = {"id": "t2", "title": "вторая задача", "spec": "сделать B",
             "acceptance": ["иной критерий"],
             "paths": ["other.py"], "type": "feature"}

    def prompt(self, task=None, **kw):
        return self.agents.review_prompt(
            dict(task or self.TASK), kw.pop("gate", "OK: 42 теста"),
            kw.pop("diff", "diff --git a/mod.py b/mod.py\n+код"), **kw)


class TestReviewPromptOrdering(PromptCase):
    """Стабильное вверх, изменчивое вниз — иначе кэш не читается."""

    def test_rules_precede_task(self):
        p = self.prompt()
        self.assertLess(p.index("## Правила ревью"), p.index("## Задача"),
                        "правила общие для всех задач и обязаны быть выше")

    def test_diff_precedes_gate_output(self):
        """Дифф — самый крупный блок; его нельзя пускать после того,
        что может измениться между раундами."""
        p = self.prompt()
        self.assertLess(p.index("## Diff"), p.index("## Вывод тестов"))

    def test_verification_block_is_last(self):
        p = self.prompt(want_verification=True)
        self.assertIn("## Проверка исполнением", p)
        self.assertLess(p.index("## Diff"), p.index("## Проверка исполнением"))
        self.assertLess(p.index("## Вывод тестов"),
                        p.index("## Проверка исполнением"))

    def test_verification_results_are_last(self):
        p = self.prompt(verify_results="все проверки прошли")
        self.assertLess(p.index("## Diff"),
                        p.index("## Результаты запрошенных тобой проверок"))


class TestCachablePrefix(PromptCase):
    """Главные тесты файла: длина общего префикса и есть деньги."""

    def test_different_tasks_share_the_rules_block(self):
        """Промпты РАЗНЫХ задач обязаны совпадать до блока правил."""
        shared = common_prefix(self.prompt(self.TASK), self.prompt(self.OTHER))
        self.assertGreater(shared, 600,
                           "общий префикс схлопнулся: блок правил больше не "
                           "первый, и каждая задача пишет его в кэш заново")
        self.assertIn("## Правила ревью", self.prompt()[:shared])

    def test_confirmation_round_reuses_almost_everything(self):
        """Подтверждающий раунд ревьюит ТОТ ЖЕ дифф.

        Исполнитель в нём не вызывался, код не менялся — значит промпт
        обязан совпадать почти целиком. Именно этот случай на PILOT-1
        стоил 37 696 токенов записи вместо чтения.
        """
        big = "diff --git a/mod.py b/mod.py\n" + "\n".join(
            f"+строка {i}" for i in range(200))
        first = self.prompt(diff=big)
        second = self.prompt(diff=big)          # тот же дифф, второй раунд
        self.assertEqual(first, second, "одинаковый вход дал разный промпт")

    def test_verification_toggle_keeps_the_diff_cached(self):
        """Включение верификации не должно выбивать дифф из кэша."""
        big = "diff --git a/mod.py b/mod.py\n" + "\n".join(
            f"+строка {i}" for i in range(200))
        plain = self.prompt(diff=big)
        asking = self.prompt(diff=big, want_verification=True)
        shared = common_prefix(plain, asking)
        self.assertGreater(shared, len(big),
                           "блок верификации разошёлся ДО диффа: весь дифф "
                           "придётся записывать в кэш заново")

    def test_human_decisions_do_not_evict_the_diff(self):
        """Ответ человека приходит в середине задачи — дифф под ним."""
        big = "diff --git a/mod.py b/mod.py\n" + "\n".join(
            f"+строка {i}" for i in range(200))
        task = dict(self.TASK, human_answer="решение владельца")
        p = self.prompt(task, diff=big)
        self.assertLess(p.index("Решения человека"), p.index("## Diff"))


class TestOutputBrevity(PromptCase):
    """30% бюджета — выходные токены; лечится промптом, не усилием."""

    def test_prompt_asks_for_brevity(self):
        p = self.prompt()
        self.assertIn("без воды", p)

    def test_brevity_rule_lives_with_the_other_rules(self):
        """Инструкция обязана быть в стабильном блоке, иначе она сама
        станет расходом: попадёт в изменчивую часть и будет писаться
        в кэш на каждом вызове."""
        p = self.prompt()
        self.assertLess(p.index("без воды"), p.index("## Задача"))


class TestExecutorScopeDiscipline(unittest.TestCase):
    """Меньше кода — меньше дифф — дешевле ревью."""

    def setUp(self):
        self.agents = ag.Agents.__new__(ag.Agents)
        self.agents.config = {}
        self.agents.state = type("S", (), {
            "load_tasks": staticmethod(lambda: {"goal": "цель"}),
        })()

    TASK = {"id": "t1", "title": "t", "spec": "s", "acceptance": ["ок"],
            "paths": ["mod.py"], "type": "feature"}

    def handoff(self, task=None):
        return self.agents.handoff(dict(task or self.TASK), None, None)

    def test_scope_discipline_is_stated(self):
        self.assertIn("ровно то, что требует спека", self.handoff())

    def test_safety_is_carved_out(self):
        """Без этой оговорки инструкция режет то, что резать нельзя."""
        h = self.handoff()
        for must in ("валидацию", "безопасност"):
            self.assertIn(must, h, f"оговорка про {must} потеряна")

    def test_disagreement_routes_to_dispute(self):
        """Исполнитель не решает сам, что задача лишняя, — он спорит."""
        self.assertIn("dispute", self.handoff())

    def test_constraints_are_intact(self):
        """Экономия объёма не должна вытеснить границы задачи."""
        h = self.handoff()
        self.assertIn("Разрешено править ТОЛЬКО эти пути", h)
        self.assertIn("git для тебя ТОЛЬКО на чтение", h)


pl = _load("planner")


class TestRoleTuning(unittest.TestCase):
    """Ручки модели и усилия: без них замерить рычаги нечем.

    Требование к умолчанию жёсткое: БЕЗ настройки в конфиге поведение
    прогона не меняется ни на байт — роль наследует сессионные параметры.
    Иначе добавление ручки само становится изменением, и сравнивать
    прогоны «до» и «после» уже нельзя.
    """

    def _agents(self, config):
        a = ag.Agents.__new__(ag.Agents)
        a.config = config
        return a

    def test_no_flags_without_config(self):
        self.assertEqual(self._agents({})._tuning("review"), [])

    def test_model_flag(self):
        a = self._agents({"review_model": "claude-sonnet-5"})
        self.assertEqual(a._tuning("review"), ["--model", "claude-sonnet-5"])

    def test_effort_flag(self):
        a = self._agents({"review_effort": "medium"})
        self.assertEqual(a._tuning("review"), ["--effort", "medium"])

    def test_both_flags(self):
        a = self._agents({"review_model": "claude-sonnet-5",
                          "review_effort": "high"})
        self.assertEqual(a._tuning("review"),
                         ["--model", "claude-sonnet-5", "--effort", "high"])

    def test_prefix_isolates_roles(self):
        """Настройка ревьюера не должна протекать в планировщика."""
        a = self._agents({"review_model": "claude-sonnet-5"})
        self.assertEqual(a._tuning("plan"), [])

    def test_planner_helper_matches(self):
        self.assertEqual(pl.tuning_flags(), [])
        self.assertEqual(pl.tuning_flags("claude-sonnet-5", "low"),
                         ["--model", "claude-sonnet-5", "--effort", "low"])

    def test_flags_reach_the_actual_call(self):
        """Ручка, не доехавшая до argv, — это ручка, которой нет."""
        import subprocess as sp
        seen = {}
        orig = sp.run

        def fake(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig(argv, **kw)
            seen["argv"] = argv
            return type("R", (), {"stdout": "{}", "stderr": "",
                                  "returncode": 0})()

        sp.run = fake
        self.addCleanup(lambda: setattr(sp, "run", orig))

        a = ag.Agents.__new__(ag.Agents)
        a.config = {"review_model": "claude-sonnet-5", "review_effort": "low"}
        a.loop_mod = _load("loop")
        a.last_review_failure = None
        a.state = type("S", (), {
            "root": ".", "dir": pathlib.Path("/tmp"),
            "work_diff": staticmethod(lambda: "diff --git a/x b/x\n+1"),
            "metric": staticmethod(lambda **k: None),
            "log": staticmethod(lambda *a, **k: None),
        })()
        a.work_diff = lambda: "diff --git a/x b/x\n+1"
        try:
            a.review({"id": "t1", "title": "t", "spec": "s",
                      "acceptance": ["ок"]}, "OK", 1)
        except Exception:                                   # noqa: BLE001
            pass                                            # интересует argv
        self.assertIn("--model", seen.get("argv", []))
        self.assertIn("claude-sonnet-5", seen["argv"])
        self.assertIn("--effort", seen["argv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
