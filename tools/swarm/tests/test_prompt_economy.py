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
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
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


# Фаза в двойнике состояния: `agents.review` объявляет, чем занята петля
# прямо сейчас (`state.phase` → `.swarm/now.json`), и двойник, который
# этого не умеет, роняет вызов ещё до промпта. Пустой контекст — ровно
# то, чем фаза является для этих тестов: они меряют промпт, а не доску.
PHASE_STUB = staticmethod(lambda *a, **kw: contextlib.nullcontext())


class PromptCase(unittest.TestCase):
    """Промпты строятся без состояния на диске: подставляем минимум."""

    def setUp(self):
        self.agents = ag.Agents.__new__(ag.Agents)
        self.agents.config = {}
        self.agents.state = type("S", (), {
            "phase": PHASE_STUB,
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
        self.assertLess(p.index("## Rules"), p.index("## Задача"),
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
        self.assertIn("## Rules", self.prompt()[:shared])

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
        self.assertIn("only what the reader needs", p)

    def test_brevity_rule_lives_with_the_other_rules(self):
        """Инструкция обязана быть в стабильном блоке, иначе она сама
        станет расходом: попадёт в изменчивую часть и будет писаться
        в кэш на каждом вызове."""
        p = self.prompt()
        self.assertLess(p.index("only what the reader needs"),
                        p.index("## Задача"))


class TestSecurityLens(PromptCase):
    """E10 (confirm_lens="security"): линза добавляет фокус, не сужает его.

    Формулировка измерена (§4.2 задачи): «может запросить»/«фильтр» роняют
    recall находок. Здесь проверяется только форма — что явка прямая и
    что линза не вытесняет уже существующие правила.
    """

    def test_lens_off_by_default_is_byte_identical(self):
        self.assertEqual(self.prompt(), self.prompt(lens=""))

    def test_lens_adds_the_stated_focus_areas(self):
        p = self.prompt(lens="security")
        self.assertIn("## Security lens", p)
        for topic in ("injection", "credentials", "authn/authz",
                      "subprocess", "dependency"):
            self.assertIn(topic, p, f"тема линзы «{topic}» потеряна")

    def test_lens_declares_itself_a_focus_not_a_filter(self):
        """Ключевая формулировка §4.2 — без неё линза читается как veto."""
        p = self.prompt(lens="security")
        self.assertIn("Report EVERYTHING you see, security and otherwise", p)
        self.assertIn("the lens sets emphasis, not a filter", p)

    def test_lens_does_not_displace_the_general_rules(self):
        p = self.prompt(lens="security")
        self.assertIn("Report every finding with its confidence", p)
        self.assertIn("approve is allowed only when", p)

    def test_unknown_lens_value_is_ignored(self):
        """Только "security" — незнакомое значение не должно молча что-то
        подмешивать; закрытый список сверяет cli.load_config, здесь — что
        промпт остаётся ПРЕЖНИМ на любом другом значении."""
        self.assertEqual(self.prompt(), self.prompt(lens="paranoid"))

    def test_lens_sits_inside_the_stable_prefix(self):
        """Линза обязана попасть ДО `## Задача`: иначе она не часть
        rules_sha (Feature 4) и не общая для всех задач вызова."""
        p = self.prompt(lens="security")
        self.assertLess(p.index("## Security lens"), p.index("## Задача"))

    def test_lens_precedes_the_language_block(self):
        p = self.prompt(lens="security")
        self.assertLess(p.index("## Security lens"),
                        p.index("## Language of your output"))


class TestReviewPromptParts(PromptCase):
    """Feature 4: разрез промпта на части не меняет склеенный результат —
    review() хеширует эти части напрямую, а не текст, найденный поиском."""

    def test_parts_concatenate_to_the_same_prompt(self):
        rules, task_mid, tail = self.agents._review_prompt_parts(
            self.TASK, "OK: 42 теста", "diff --git a/mod.py b/mod.py\n+код")
        self.assertEqual(rules + task_mid + tail, self.prompt())

    def test_task_part_holds_task_and_not_diff(self):
        _rules, task_mid, tail = self.agents._review_prompt_parts(
            self.TASK, "OK", "diff --git a/mod.py b/mod.py\n+МЕТКА_ДИФФА")
        self.assertIn("## Задача", task_mid)
        self.assertIn("критерий один", task_mid)
        self.assertNotIn("МЕТКА_ДИФФА", task_mid)
        self.assertIn("МЕТКА_ДИФФА", tail)

    def test_rules_part_is_task_independent(self):
        rules_a, _, _ = self.agents._review_prompt_parts(
            self.TASK, "OK", "diff")
        rules_b, _, _ = self.agents._review_prompt_parts(
            self.OTHER, "OK", "diff")
        self.assertEqual(rules_a, rules_b)


class TestExecutorScopeDiscipline(unittest.TestCase):
    """Меньше кода — меньше дифф — дешевле ревью."""

    def setUp(self):
        self.agents = ag.Agents.__new__(ag.Agents)
        self.agents.config = {}
        self.agents.state = type("S", (), {
            "phase": PHASE_STUB,
            "load_tasks": staticmethod(lambda: {"goal": "цель"}),
        })()

    TASK = {"id": "t1", "title": "t", "spec": "s", "acceptance": ["ок"],
            "paths": ["mod.py"], "type": "feature"}

    def handoff(self, task=None):
        return self.agents.handoff(dict(task or self.TASK), None, None)

    def test_scope_discipline_is_stated(self):
        self.assertIn("Do exactly what the spec asks", self.handoff())

    def test_safety_is_carved_out(self):
        """Без этой оговорки инструкция режет то, что резать нельзя."""
        h = self.handoff()
        for must in ("input validation", "security requirements"):
            self.assertIn(must, h, f"оговорка про {must} потеряна")

    def test_disagreement_routes_to_dispute(self):
        """Исполнитель не решает сам, что задача лишняя, — он спорит."""
        self.assertIn("dispute", self.handoff())

    def test_constraints_are_intact(self):
        """Экономия объёма не должна вытеснить границы задачи."""
        h = self.handoff()
        self.assertIn("Editable paths, and only these", h)
        self.assertIn("git is READ-ONLY for you", h)


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
        import io
        import subprocess as sp
        seen = {}
        orig = sp.Popen

        def fake(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig(argv, **kw)
            seen["argv"] = argv
            # двойник процесса: ревьюер ходит через драйвер (Popen)
            return type("P", (), {
                "stdout": io.StringIO(""), "stderr": io.StringIO(""),
                "returncode": 0, "poll": lambda s: 0,
                "wait": lambda s, timeout=None: 0, "kill": lambda s: None})()

        sp.Popen = fake
        self.addCleanup(lambda: setattr(sp, "Popen", orig))

        a = ag.Agents.__new__(ag.Agents)
        a.config = {"review_model": "claude-sonnet-5", "review_effort": "low"}
        a.loop_mod = _load("loop")
        a.driver = _load("driver")
        a.last_review_failure = None
        a.state = type("S", (), {
            "phase": PHASE_STUB,
            "root": ".", "dir": pathlib.Path(tempfile.gettempdir()),
            "work_diff": staticmethod(lambda: "diff --git a/x b/x\n+1"),
            "metric": staticmethod(lambda **k: None),
            "log": staticmethod(lambda *a, **k: None),
        })()
        a.work_diff = lambda: "diff --git a/x b/x\n+1"
        # Вызов почти наверняка упадёт: подставной state не полон.
        # Проверяется argv, собранный ДО падения.
        with contextlib.suppress(Exception):
            a.review({"id": "t1", "title": "t", "spec": "s",
                      "acceptance": ["ок"]}, "OK", 1)
        self.assertIn("--model", seen.get("argv", []))
        self.assertIn("claude-sonnet-5", seen["argv"])
        self.assertIn("--effort", seen["argv"])


class TestConfirmationRoundAngle(unittest.TestCase):
    """Подтверждающий раунд как второй угол зрения, а не повтор.

    Замер на e4kb — один и тот же дифф, одно отличие (уровень усилия):
    `xhigh` и `medium` дали по три находки, совпала ОДНА. `xhigh` нашёл
    дыры в покрытии тестами, `medium` — два дефекта поведения: мёртвую
    запись severity (правило пишет своё значение, движок безусловно
    перезаписывает) и двойную диагностику на клеммах с одинаковым именем
    в одном УГО. Значит подтверждающий раунд, идущий теми же
    параметрами, покупает повтор одного взгляда; разведённый по усилию —
    второй взгляд, и на 25 % дешевле.
    """

    def _agents(self, config):
        a = ag.Agents.__new__(ag.Agents)
        a.config = config
        return a

    def test_confirm_effort_applies_only_on_confirmation(self):
        a = self._agents({"review_effort": "xhigh", "confirm_effort": "medium"})
        self.assertEqual(a._tuning("review"), ["--effort", "xhigh"])
        self.assertEqual(a._tuning("review", confirming=True),
                         ["--effort", "medium"])

    def test_confirm_falls_back_to_review(self):
        """Без confirm_* умолчание обязано остаться прежним."""
        a = self._agents({"review_effort": "xhigh"})
        self.assertEqual(a._tuning("review", confirming=True),
                         ["--effort", "xhigh"])

    def test_confirm_model_is_independent(self):
        a = self._agents({"review_model": "claude-opus-5",
                          "confirm_model": "claude-sonnet-5"})
        self.assertEqual(a._tuning("review"), ["--model", "claude-opus-5"])
        self.assertEqual(a._tuning("review", confirming=True),
                         ["--model", "claude-sonnet-5"])

    def test_no_config_still_means_no_flags(self):
        a = self._agents({})
        self.assertEqual(a._tuning("review", confirming=True), [])

    def test_loop_marks_the_confirmation_round(self):
        """Флаг обязан доехать из петли до вызова, иначе ручка мертва.

        Ревьюер-заглушка одобряет с первого раза; при confirmations=2
        второй раунд — подтверждающий, и он обязан прийти с confirming=True.
        """
        import tempfile
        seen = []
        st = _load("state")
        lp = _load("loop")

        class Reviewer:
            last_review_failure = None

            def implement(self, task, feedback, iteration):
                (root / "mod.py").write_text("def f():\n    return 2\n")
                return {"status": "done", "summary": "готово"}

            def review(self, task, gate_tail, iteration, attempt=1,
                       verify_results=None, confirming=False):
                seen.append(confirming)
                return {"verdict": "approve", "findings": [],
                        "analysis": "разобрал дифф и сверился со спецификацией",
                        "summary": "работа соответствует требованиям"}

            def repo_map(self, task):
                return None

            def commit_message(self, task, diff):
                return "c1: правка"

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for args in (["init", "-q"], ["config", "user.name", "t"],
                         ["config", "user.email", "t@t"]):
                subprocess.run(["git", *args], cwd=root, check=True)
            (root / "mod.py").write_text("def f():\n    return 1\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
            state = st.SwarmState(root)
            task = {"id": "c1", "title": "t", "spec": "s", "acceptance": ["ок"],
                    "paths": ["mod.py"], "type": "feature", "status": "pending",
                    "deps": []}
            state.save_tasks({"goal": "цель", "tasks": [dict(task)]})
            loop = lp.Loop(state, {"gate_command": ["true"], "confirmations": 2},
                           Reviewer())
            loop.run_task(dict(task))

        self.assertEqual(seen, [False, True],
                         "признак подтверждающего раунда не доехал до ревьюера")


class TestTuningPools(unittest.TestCase):
    """Жребий на КАЖДЫЙ вызов ревью — и запись выбора в журнал.

    Вывод по единственному замеру — сквозная ошибка всей сессии: сначала
    «разведённые раунды дают второй угол» по одной паре, потом «гипотеза
    не подтвердилась» по одному нулю, потом «Sonnet дешевле на 40 %» по
    ставкам без замера. Лечится не аккуратностью, а дизайном: жребий и
    накопление.

    Ключевой выбор дизайна — жребий на ВЫЗОВ, а не на задачу. Каждую
    задачу мы ревьюим дважды по одному диффу, поэтому независимый жребий
    сам рождает пары «две руки на одном диффе». Жребий на задачу дал бы
    сравнение между разными диффами, где число находок зависит от
    сложности кода сильнее, чем от модели.
    """

    def _agents(self, config, seed=0):
        a = ag.Agents.__new__(ag.Agents)
        a.config = config
        a.rng = __import__("random").Random(seed)
        a.last_tuning = {}
        return a

    def test_fixed_value_without_pool(self):
        a = self._agents({"review_model": "claude-opus-5"})
        self.assertEqual(a._tuning("review"), ["--model", "claude-opus-5"])

    def test_pool_is_drawn(self):
        pool = ["claude-opus-5", "claude-sonnet-5"]
        a = self._agents({"review_model_pool": pool})
        flags = a._tuning("review")
        self.assertEqual(flags[0], "--model")
        self.assertIn(flags[1], pool)

    def test_pool_outranks_fixed_value(self):
        """Иначе настройка и пул тихо конфликтуют, и замер невоспроизводим."""
        a = self._agents({"review_model": "claude-opus-5",
                          "review_model_pool": ["claude-sonnet-5"]})
        self.assertEqual(a._tuning("review"), ["--model", "claude-sonnet-5"])

    def test_draw_varies_across_calls(self):
        """Жребий на КАЖДЫЙ вызов: иначе пары на одном диффе не возникнут."""
        a = self._agents({"review_model_pool": ["a", "b"]}, seed=1)
        drawn = {a._draw("review_model", False) for _ in range(40)}
        self.assertEqual(drawn, {"a", "b"}, "жребий выродился в константу")

    def test_seed_makes_the_draw_reproducible(self):
        cfg = {"review_model_pool": ["a", "b", "c"], "tuning_seed": 7}
        first = [ag.Agents.__new__(ag.Agents) for _ in range(2)]
        seqs = []
        for inst in first:
            inst.config = cfg
            inst.rng = __import__("random").Random(cfg["tuning_seed"])
            inst.last_tuning = {}
            seqs.append([inst._draw("review_model", False) for _ in range(10)])
        self.assertEqual(seqs[0], seqs[1])

    def test_confirm_pool_overrides(self):
        a = self._agents({"review_model_pool": ["opus"],
                          "confirm_model_pool": ["sonnet"]})
        self.assertEqual(a._tuning("review"), ["--model", "opus"])
        self.assertEqual(a._tuning("review", confirming=True),
                         ["--model", "sonnet"])

    def test_confirm_falls_back_to_review_pool(self):
        a = self._agents({"review_model_pool": ["opus"]})
        self.assertEqual(a._tuning("review", confirming=True),
                         ["--model", "opus"])

    def test_choice_is_remembered_for_the_journal(self):
        a = self._agents({"review_model_pool": ["claude-sonnet-5"],
                          "review_effort_pool": ["medium"]})
        a._tuning("review")
        self.assertEqual(a.last_tuning,
                         {"model": "claude-sonnet-5", "effort": "medium"})

    def test_metric_carries_the_draw(self):
        """Жребий, не попавший в журнал, — это шум, а не замер."""
        import io
        import subprocess as sp
        rows = []
        orig = sp.Popen
        envelope = json.dumps({"type": "result", "structured_output": {
            "verdict": "approve", "findings": [],
            "analysis": "разобрал дифф и сверился со спецификацией",
            "summary": "работа соответствует требованиям"},
            "total_cost_usd": 0.5}, ensure_ascii=False)

        def fake(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig(argv, **kw)
            # двойник процесса: ревьюер ходит через драйвер (Popen)
            return type("P", (), {
                "stdout": io.StringIO(envelope + "\n"),
                "stderr": io.StringIO(""),
                "returncode": 0, "poll": lambda s: 0,
                "wait": lambda s, timeout=None: 0, "kill": lambda s: None})()

        sp.Popen = fake
        self.addCleanup(lambda: setattr(sp, "Popen", orig))

        a = self._agents({"review_model_pool": ["claude-sonnet-5"],
                          "review_effort_pool": ["low"]})
        a.loop_mod = _load("loop")
        a.driver = _load("driver")
        a.last_review_failure = None
        a.work_diff = lambda: "diff --git a/x b/x\n+1"
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (pathlib.Path(tmp.name) / "log").mkdir()
        a.state = type("S", (), {
            "phase": PHASE_STUB,
            "root": ".", "dir": pathlib.Path(tmp.name),
            "metric": staticmethod(lambda **k: rows.append(k)),
            "log": staticmethod(lambda *x, **k: None),
        })()
        a.review({"id": "t1", "title": "t", "spec": "s",
                  "acceptance": ["ок"]}, "OK", 1, confirming=True)
        row = next(r for r in rows if r.get("phase") == "review")
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["effort"], "low")
        self.assertTrue(row["confirming"])


class TestConfirmLensGating(unittest.TestCase):
    """Feature 3: линза едет в review() ТОЛЬКО на подтверждающем раунде —
    тем же правилом, что confirm_model/confirm_effort в _tuning."""

    def _prompt_sent(self, config, confirming):
        seen: dict[str, list[str]] = {}
        orig = subprocess.Popen

        def fake(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig(argv, **kw)
            seen["argv"] = argv
            return type("P", (), {
                "stdout": io.StringIO(""), "stderr": io.StringIO(""),
                "returncode": 0, "poll": lambda s: 0,
                "wait": lambda s, timeout=None: 0, "kill": lambda s: None})()

        subprocess.Popen = fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", orig))

        a = ag.Agents.__new__(ag.Agents)
        a.config = config
        a.loop_mod = _load("loop")
        a.driver = _load("driver")
        a.last_review_failure = None
        a.work_diff = lambda: "diff --git a/x b/x\n+1"
        a.state = type("S", (), {
            "phase": PHASE_STUB,
            "root": ".", "dir": pathlib.Path(tempfile.gettempdir()),
            "metric": staticmethod(lambda **k: None),
            "log": staticmethod(lambda *x, **k: None),
        })()
        with contextlib.suppress(Exception):
            a.review({"id": "t1", "title": "t", "spec": "s",
                      "acceptance": ["ок"]}, "OK", 1, confirming=confirming)
        argv = seen.get("argv", [])
        return argv[2] if len(argv) > 2 else ""

    def test_lens_absent_on_normal_round_even_with_config(self):
        prompt = self._prompt_sent({"confirm_lens": "security"}, confirming=False)
        self.assertNotIn("Security lens", prompt)

    def test_lens_present_on_confirming_round(self):
        prompt = self._prompt_sent({"confirm_lens": "security"}, confirming=True)
        self.assertIn("Security lens", prompt)

    def test_lens_absent_when_unset_even_on_confirming_round(self):
        prompt = self._prompt_sent({}, confirming=True)
        self.assertNotIn("Security lens", prompt)


class TestReviewMetricsTelemetry(unittest.TestCase):
    """Feature 4: кэш-телеметрия и sha стабильных секций в метрике review.

    sha — не секретность, а отпечаток (§9.3, «журнал как данные»): мутация
    неизменного блока промпта обязана быть ВИДНА в метрике как смена
    rules_sha, без повторного чтения текста промпта глазами.
    """

    def _review(self, task, config=None, confirming=False, usage=None):
        rows: list[dict[str, object]] = []
        orig = subprocess.Popen
        envelope = json.dumps({
            "type": "result",
            "structured_output": {
                "verdict": "approve", "findings": [],
                "analysis": "разобрал дифф и сверился со спецификацией",
                "summary": "работа соответствует требованиям"},
            "total_cost_usd": 0.5, "usage": usage or {}}, ensure_ascii=False)

        def fake(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig(argv, **kw)
            return type("P", (), {
                "stdout": io.StringIO(envelope + "\n"),
                "stderr": io.StringIO(""),
                "returncode": 0, "poll": lambda s: 0,
                "wait": lambda s, timeout=None: 0, "kill": lambda s: None})()

        subprocess.Popen = fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", orig))

        a = ag.Agents.__new__(ag.Agents)
        a.config = config or {}
        a.loop_mod = _load("loop")
        a.driver = _load("driver")
        a.last_review_failure = None
        a.work_diff = lambda: "diff --git a/x b/x\n+1"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (pathlib.Path(tmp.name) / "log").mkdir()
        a.state = type("S", (), {
            "phase": PHASE_STUB,
            "root": ".", "dir": pathlib.Path(tmp.name),
            "metric": staticmethod(lambda **k: rows.append(k)),
            "log": staticmethod(lambda *x, **k: None),
        })()
        a.review(task, "OK", 1, confirming=confirming)
        return next(r for r in rows if r.get("phase") == "review")

    T1 = {"id": "t1", "title": "первая", "spec": "s1", "acceptance": ["ок"]}
    T2 = {"id": "t2", "title": "вторая", "spec": "s2", "acceptance": ["иначе"]}

    def test_rules_sha_identical_across_tasks(self):
        row1 = self._review(self.T1)
        row2 = self._review(self.T2)
        self.assertEqual(row1["rules_sha"], row2["rules_sha"])

    def test_task_sha_differs_across_tasks(self):
        row1 = self._review(self.T1)
        row2 = self._review(self.T2)
        self.assertNotEqual(row1["task_sha"], row2["task_sha"])

    def test_task_sha_stable_across_rounds_of_one_task(self):
        row1 = self._review(self.T1)
        row2 = self._review(self.T1)
        self.assertEqual(row1["task_sha"], row2["task_sha"])

    def test_rules_sha_changes_with_the_lens(self):
        """Отпечаток и есть страж мутации: включение линзы меняет
        неизменный блок — метрика обязана это показать."""
        plain = self._review(self.T1)
        with_lens = self._review(self.T1, config={"confirm_lens": "security"},
                                 confirming=True)
        self.assertNotEqual(plain["rules_sha"], with_lens["rules_sha"])

    def test_metric_carries_cache_fields(self):
        row = self._review(self.T1, usage={
            "cache_read_input_tokens": 111, "cache_creation_input_tokens": 22,
            "input_tokens": 5, "output_tokens": 9})
        self.assertEqual(row["cache_read"], 111)
        self.assertEqual(row["cache_write"], 22)
        self.assertEqual(row["tokens_in"], 5)
        self.assertEqual(row["tokens_out"], 9)

    def test_missing_usage_fields_are_none_not_zero(self):
        """Журнал как данные (§9.3): отсутствие поля — None, а не 0 —
        0 токенов кэша и «неизвестно» это разные факты."""
        row = self._review(self.T1)
        self.assertIsNone(row["cache_read"])
        self.assertIsNone(row["cache_write"])
        self.assertIsNone(row["tokens_in"])
        self.assertIsNone(row["tokens_out"])


class TestAbSummary(unittest.TestCase):
    """Сводка обязана отличать пары на одном диффе от общей статистики."""

    def _root(self, rows):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / ".swarm").mkdir()
        with (root / ".swarm" / "metrics.jsonl").open("w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return root

    def _run(self, rows):
        import contextlib
        import io
        cli = _load("cli")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_ab(type("A", (), {"root": str(self._root(rows)),
                                      "run": None})())
        return buf.getvalue()

    ROW = {"phase": "review", "valid": True, "verdict": "approve"}

    def test_pair_on_the_same_diff_is_found(self):
        out = self._run([
            dict(self.ROW, task="t1", iter=1, model="opus", effort="xhigh",
                 cost_usd=1.8, findings=6),
            dict(self.ROW, task="t1", iter=1, model="sonnet", effort="xhigh",
                 cost_usd=1.2, findings=2),
        ])
        self.assertIn("пары на одном диффе: 1", out)
        self.assertIn("opus", out)
        self.assertIn("sonnet", out)

    def test_same_arm_twice_is_not_a_pair(self):
        """Два вызова одной рукой сравнивать не с чем."""
        out = self._run([
            dict(self.ROW, task="t1", iter=1, model="opus", effort="xhigh",
                 cost_usd=1.8, findings=6),
            dict(self.ROW, task="t1", iter=1, model="opus", effort="xhigh",
                 cost_usd=1.7, findings=5),
        ])
        self.assertIn("пары на одном диффе: 0", out)

    def test_different_tasks_are_not_a_pair(self):
        """Разные диффы — не пара: число находок зависит от сложности кода."""
        out = self._run([
            dict(self.ROW, task="t1", iter=1, model="opus", effort="xhigh",
                 cost_usd=1.8, findings=6),
            dict(self.ROW, task="t2", iter=1, model="sonnet", effort="xhigh",
                 cost_usd=1.2, findings=2),
        ])
        self.assertIn("пары на одном диффе: 0", out)

    def test_invalid_reviews_are_excluded(self):
        """Обрубленный вызов вердикта не дал — в статистику он не идёт."""
        out = self._run([
            dict(self.ROW, task="t1", iter=1, model="opus", effort="xhigh",
                 cost_usd=1.8, findings=6),
            {"phase": "review", "valid": False, "task": "t1", "iter": 1,
             "model": "sonnet", "effort": "xhigh", "cost_usd": 3.2},
        ])
        self.assertIn("(1 валидных ревью)", out)
        self.assertIn("пары на одном диффе: 0", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
