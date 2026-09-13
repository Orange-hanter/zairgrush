#!/usr/bin/env python3
"""Подключение написанного к петле.

Аудит отделил этот класс проблем от дефектов: планировщик и `verify.py`
были написаны и покрыты тестами, но не вызывались ни из одной строки
рабочего кода. Тесты у них зелёные — а роли в системе нет. Здесь
проверяется именно связь: команда доходит до модуля, запрос ревьюера
доходит до исполнения, результат возвращается обратно.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)
st_mod = cli.state_mod


def _load(name):
    s = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    m = importlib.util.module_from_spec(s)
    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


ag = _load("agents")


class FakeClaudeProc:
    """Процесс-двойник ревьюера: конверт одним result-событием stream-json.

    Ревьюер ходит через драйвер (Popen + поток), а не subprocess.run, —
    двойник отдаёт ровно то, что драйвер читает: stdout построчно,
    пустой stderr, код возврата.
    """

    def __init__(self, envelope, returncode=0):
        if envelope is None:      # поток оборвался, result-события не было
            self.stdout = io.StringIO(json.dumps({"type": "assistant"}) + "\n")
        else:
            line = json.dumps({"type": "result", **envelope},
                              ensure_ascii=False)
            self.stdout = io.StringIO(line + "\n")
        self.stderr = io.StringIO("")
        # Промпт с NXT-006 едет через stdin (ARG_MAX), и двойник обязан
        # принять запись потока-кормильца драйвера.
        self.stdin = _RecordingStdin()
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


class _RecordingStdin(io.StringIO):
    """stdin двойника, читаемый ПОСЛЕ close(): драйвер закрывает канал
    после записи промпта (EOF для CLI), а тест обязан видеть, что
    доехало."""

    _saved = ""

    def close(self):
        self._saved = super().getvalue()
        super().close()

    def getvalue(self):
        try:
            return super().getvalue()
        except ValueError:
            return self._saved


def _fed_prompt(proc, timeout=5.0):
    """Промпт из stdin двойника: с NXT-006 он едет потоком, а не в argv
    (ARG_MAX). Пишет поток-кормилец драйвера — ждём запись."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = proc.stdin.getvalue()
        if text:
            return text
        time.sleep(0.01)
    raise AssertionError("промпт не доехал до stdin процесса")


def patch_claude_popen(testcase, reply):
    """Перехват ТОЛЬКО вызова ревьюера: остальные Popen идут по-настоящему.

    `reply(argv)` возвращает dict конверта. Подменяется внешний CLI, путь
    review остаётся настоящим — тот же принцип, что у прежнего перехвата
    subprocess.run. Двойники складываются в testcase.claude_procs: промпт
    читается из их stdin, а не из argv.
    """
    orig = subprocess.Popen
    testcase.claude_procs = []

    def fake_popen(argv, **kw):
        if not (argv and argv[0] == "claude"):
            return orig(argv, **kw)
        proc = FakeClaudeProc(reply(argv))
        testcase.claude_procs.append(proc)
        return proc

    subprocess.Popen = fake_popen
    testcase.addCleanup(lambda: setattr(subprocess, "Popen", orig))


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
        (self.root / "mod.py").write_text("def f():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = st_mod.SwarmState(self.root)
        self.state.save_tasks({"goal": "старая цель", "tasks": [
            {"id": "aaaa", "title": "первая", "status": "done", "deps": [],
             "type": "feature", "paths": ["mod.py"], "acceptance": ["ok"]}]})

    def tearDown(self):
        self.tmp.cleanup()


DIFF = {
    "analysis": "разбор состояния репозитория и декомпозиция цели по модулям",
    "summary": "две задачи",
    "ops": [{
        "op": "add", "id": "bbbb", "reason": "нужен новый модуль",
        "task": {"id": "bbbb", "title": "новая задача", "type": "feature",
                 "status": "pending", "paths": ["mod.py"],
                 "acceptance": ["тесты проходят"], "deps": []}}],
}


class TestPlannerIsReachable(RepoCase):
    """Планировщик существовал как библиотека — теперь у него есть вход."""

    def setUp(self):
        super().setUp()
        planner = _load("planner")
        self.calls = []
        # call_planner отдаёт (дифф, причина отказа): причина нужна, чтобы
        # обрыв по бюджету не уходил в обречённый повтор.
        planner.call_planner = lambda prompt, tag, attempt=1, root=None, \
                budget=None, model=None, effort=None, timeout=None: (
            self.calls.append((tag, attempt, prompt)),
            (json.loads(json.dumps(DIFF)), None))[1]
        planner.metric = lambda **k: None
        self._orig_load = cli.load_mod
        cli.load_mod = (lambda name: planner if name == "planner"
                        else self._orig_load(name))

    def tearDown(self):
        cli.load_mod = self._orig_load
        super().tearDown()

    def test_plan_command_exists(self):
        code, out = run_cli("--root", str(self.root), "plan", "--goal", "новая цель")
        self.assertEqual(code, 0, out)

    def test_plan_applies_diff_to_queue(self):
        run_cli("--root", str(self.root), "plan", "--goal", "новая цель")
        ids = [t["id"] for t in self.state.load_tasks()["tasks"]]
        self.assertIn("bbbb", ids, "план-дифф не применён к очереди")

    def test_plan_updates_goal(self):
        run_cli("--root", str(self.root), "plan", "--goal", "новая цель")
        self.assertEqual(self.state.load_tasks()["goal"], "новая цель")

    def test_dry_run_does_not_touch_queue(self):
        run_cli("--root", str(self.root), "plan", "--goal", "цель", "--dry-run")
        ids = [t["id"] for t in self.state.load_tasks()["tasks"]]
        self.assertNotIn("bbbb", ids)

    def test_invalid_diff_escalates_after_retry(self):
        planner = sys.modules["planner"]
        planner.call_planner = (
            lambda prompt, tag, attempt=1, root=None, budget=None, \
                   model=None, effort=None, timeout=None: ({"ops": []}, None))
        code, out = run_cli("--root", str(self.root), "plan", "--goal", "цель")
        self.assertEqual(code, 2)
        self.assertIn("ЭСКАЛАЦИЯ", out)

    def test_replan_requires_existing_task(self):
        code, out = run_cli("--root", str(self.root), "replan", "zzzz")
        self.assertEqual(code, 2)
        self.assertIn("не найдена", out)

    def test_application_is_journaled(self):
        run_cli("--root", str(self.root), "plan", "--goal", "цель")
        self.assertIn("plan_applied", self.state.journal_path.read_text())


class TestVerificationIsReachable(RepoCase):
    """Запрос ревьюера обязан дойти до исполнения и вернуться обратно."""

    def _agents(self, config):
        """Подменяется ТОЛЬКО внешний CLI: путь review остаётся настоящим.

        Первая версия этого теста собирала собственный fake_review,
        повторявший логику двухфазного вызова, — и проверяла тем самым
        свою же копию. Мутация «запросы ревьюера не исполняются» её
        переживала: настоящий код в тесте не участвовал.
        """
        agents = ag.Agents(self.state, config)
        calls = {"n": 0}
        replies = [
            {"analysis": "первый проход по диффу с проверкой критериев приёмки",
             "verdict": "request_changes", "summary": "нужна проверка исполнением",
             "findings": [{"file": "mod.py", "severity": "minor",
                           "category": "tests", "confidence": 0.6,
                           "issue": "не уверен, что тест ловит регресс"}],
             "out_of_scope_notes": [],
             "verification_requests": [
                 {"kind": "unittest", "arg": "tests.test_x", "why": "проверить тест"}]},
            {"analysis": "второй проход с учётом результатов запрошенной проверки",
             "verdict": "approve", "summary": "проверка подтвердила корректность",
             "findings": [], "out_of_scope_notes": []},
        ]

        def reply(argv):
            calls["n"] += 1
            body = replies[min(calls["n"], len(replies)) - 1]
            return {"structured_output": body, "total_cost_usd": 0.1}

        patch_claude_popen(self, reply)
        return agents

    TASK = {"id": "aaaa", "title": "t", "spec": "s", "acceptance": ["ок"],
            "paths": ["mod.py"], "type": "feature"}

    def test_disabled_by_default_for_ordinary_task(self):
        agents = self._agents({})
        text = agents.review_prompt(dict(self.TASK), "OK", "diff",
                                    want_verification=agents._wants_verification(
                                        dict(self.TASK)))
        self.assertNotIn("Проверка исполнением", text,
                         "механизм стоит +51 %, включать его всегда незачем")

    def test_enabled_on_milestone_close(self):
        agents = self._agents({})
        task = dict(self.TASK, milestone_close=True)
        self.assertTrue(agents._wants_verification(task))

    def test_always_mode(self):
        self.assertTrue(self._agents({"verification": "always"})
                        ._wants_verification(dict(self.TASK)))

    def test_never_mode_overrides_task_flag(self):
        agents = self._agents({"verification": "never"})
        self.assertFalse(agents._wants_verification(dict(self.TASK, verify=True)))

    def test_prompt_demands_listing_not_offering(self):
        """Формулировка и есть механизм (ADR-005): «перечисли», не «можешь»."""
        agents = self._agents({"verification": "always"})
        text = agents.review_prompt(dict(self.TASK), "OK", "diff",
                                    want_verification=True)
        self.assertIn("Перечисли", text)
        self.assertIn("сам ты ничего не запускаешь", text)

    def _delivered_prompts(self):
        """Промпты вызовов из канала доставки (NXT-006): в argv их
        больше нет — ревьюер отдаёт промпт через stdin процесса."""
        return [_fed_prompt(p) for p in self.claude_procs]

    def test_requests_are_executed_and_returned(self):
        agents = self._agents({"verification": "always"})
        verdict = agents.review(dict(self.TASK), "OK", 1)
        prompts = self._delivered_prompts()
        self.assertEqual(len(prompts), 2, "второго вызова не было")
        self.assertIn("Результаты запрошенных тобой проверок", prompts[1])
        self.assertEqual(verdict["verdict"], "approve")

    def test_second_round_does_not_ask_again(self):
        """Анти-петля: один раунд верификации на итерацию."""
        agents = self._agents({"verification": "always"})
        agents.review(dict(self.TASK), "OK", 1)
        self.assertNotIn("Проверка исполнением",
                         self._delivered_prompts()[1])

    def test_verification_round_is_capped(self):
        """Анти-петля: один раунд верификации на итерацию.

        Если ревьюер просит проверки в КАЖДОМ ответе, а ограничения нет,
        пара «запрос — исполнение» крутится бесконечно и жжёт бюджет.
        """
        agents = ag.Agents(self.state, {"verification": "always"})
        calls = {"n": 0}
        asking = {"analysis": "разбор диффа с проверкой критериев приёмки задачи",
                  "verdict": "request_changes",
                  "summary": "нужна ещё одна проверка исполнением",
                  "findings": [{"file": "mod.py", "severity": "minor",
                                "category": "tests", "confidence": 0.6,
                                "issue": "сомнение в покрытии"}],
                  "out_of_scope_notes": [],
                  "verification_requests": [
                      # намеренно дешёвая проверка: при регрессе цикл
                      # крутится, и тест не должен из-за этого идти минуту
                      {"kind": "git_log", "arg": "HEAD", "why": "посмотреть историю"}]}
        def reply(argv):
            calls["n"] += 1
            # Обрываем цикл здесь, а не ждём RecursionError: при регрессе
            # каждый виток запускает внешние команды, и тест шёл бы минуту.
            if calls["n"] > 2:
                raise AssertionError("верификация зациклилась: "
                                     "раунд не ограничен")
            return {"structured_output": asking, "total_cost_usd": 0.1}

        patch_claude_popen(self, reply)
        agents.review(dict(self.TASK), "OK", 1)
        self.assertEqual(len(self.claude_procs), 2,
                         "верификация зациклилась: раунд не ограничен")

    def test_requests_ignored_when_mechanism_disabled(self):
        """Ревьюер может прислать запросы и без спроса.

        На первой задаче пилота так и вышло: verification="milestone",
        задача без milestone_close, секции в промпте нет — а запросы
        пришли, и оркестратор их исполнил, удвоив стоимость ревью.
        """
        agents = ag.Agents(self.state, {"verification": "never"})
        reply = {"analysis": "разбор диффа по критериям приёмки задачи целиком",
                 "verdict": "approve", "summary": "замечаний нет, работа принята",
                 "findings": [], "out_of_scope_notes": [],
                 "verification_requests": [
                     {"kind": "unittest_all", "why": "на всякий случай"}]}
        def answer(argv):
            return {"structured_output": reply, "total_cost_usd": 0.1}

        patch_claude_popen(self, answer)
        v = agents.review(dict(self.TASK), "OK", 1)
        self.assertEqual(len(self.claude_procs), 1,
                         "проверки исполнены, хотя механизм выключен")
        self.assertEqual(v["verdict"], "approve")

    def test_failed_second_pass_keeps_first_verdict(self):
        """Верификация — улучшение вердикта, а не условие его силы.

        На пилоте первый вердикт был валидным approve ($1.22), второй
        проход упёрся в бюджет — и задача ушла в blocked, потеряв
        готовую работу из-за сбоя необязательного шага.
        """
        agents = ag.Agents(self.state, {"verification": "always"})
        calls = {"n": 0}
        first = {"analysis": "подробный разбор диффа по критериям приёмки",
                 "verdict": "approve", "summary": "работа соответствует задаче",
                 "findings": [], "out_of_scope_notes": [],
                 "verification_requests": [
                     {"kind": "git_log", "arg": "HEAD", "why": "история"}]}
        def reply(argv):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"structured_output": first, "total_cost_usd": 1.2}
            # второй проход и ретрай — отказ по бюджету, как на пилоте
            return {"is_error": True, "result": None,
                    "subtype": "error_max_budget_usd"}

        patch_claude_popen(self, reply)
        v = agents.review(dict(self.TASK), "OK", 1)
        self.assertIsNotNone(v, "валидный первый вердикт потерян")
        self.assertEqual(v["verdict"], "approve")
        self.assertIn("verification_inconclusive",
                      self.state.journal_path.read_text(),
                      "потеря второго прохода должна быть видна в журнале")

    def test_verification_pass_writes_separate_raw_file(self):
        """Второй проход не должен затирать сырой ответ первого."""
        agents = ag.Agents(self.state, {"verification": "always"})
        reply = {"analysis": "подробный разбор диффа по критериям приёмки",
                 "verdict": "approve", "summary": "работа соответствует задаче",
                 "findings": [], "out_of_scope_notes": [],
                 "verification_requests": [
                     {"kind": "git_log", "arg": "HEAD", "why": "история"}]}
        second = dict(reply)
        second.pop("verification_requests")
        calls = {"n": 0}

        def answer(argv):
            calls["n"] += 1
            body = reply if calls["n"] == 1 else second
            return {"structured_output": body, "total_cost_usd": 0.1}

        patch_claude_popen(self, answer)
        agents.review(dict(self.TASK), "OK", 1)
        names = sorted(p.name for p in (self.state.dir / "log").glob("*review.json"))
        self.assertEqual(len(names), 2, f"ответы затёрли друг друга: {names}")

    def test_rejected_request_still_reported(self):
        """Отклонённый whitelist'ом запрос обязан вернуться ревьюеру."""
        vf = _load("verify")
        results = vf.run_requests(
            [{"kind": "python", "arg": "__import__('os').system('rm -rf /')",
              "why": "злой"}], self.root)
        text = vf.format_results(results)
        self.assertIn("rejected", text)


class TestBudgetStopsRun(RepoCase):
    """Бюджет прогона — деньги владельца, а не абстракция."""

    def _loop(self, config, spent):
        for _ in range(spent):
            self.state.metric(task="aaaa", phase="review", cost_usd=1.0)
        calls = {"n": 0}

        def implement(task, feedback, iteration):
            calls["n"] += 1
            return {"status": "done", "summary": "готово"}

        agents = type("A", (), {"implement": staticmethod(implement),
                                "review": staticmethod(lambda *a, **k: None),
                                "commit_message": staticmethod(lambda t, d: "m")})()
        loop = cli.loop_mod.Loop(self.state, config, agents, ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        self.state.save_tasks({"goal": "g", "tasks": [
            {"id": "aaaa", "title": "t", "status": "pending", "deps": [],
             "type": "feature", "paths": ["mod.py"]}]})
        return loop, calls

    def test_exhausted_budget_stops_before_task(self):
        loop, calls = self._loop({"total_budget_usd": 5}, spent=6)
        loop.run()
        self.assertEqual(calls["n"], 0,
                         "прогон продолжился после исчерпания бюджета")
        self.assertIn("budget_exhausted", self.state.journal_path.read_text())

    def test_budget_within_limit_runs(self):
        loop, calls = self._loop({"total_budget_usd": 50}, spent=2)
        loop.run()
        self.assertGreater(calls["n"], 0, "задача не запущена в пределах бюджета")

    def test_no_budget_means_no_limit(self):
        loop, calls = self._loop({}, spent=100)
        loop.run()
        self.assertGreater(calls["n"], 0)

    def test_spend_counted_from_metrics(self):
        self.state.metric(task="aaaa", phase="review", cost_usd=1.25)
        self.state.metric(task="aaaa", phase="implement", cost_usd=0.75)
        self.assertEqual(self.state.total_spend(), 2.0)

    def test_spend_survives_broken_metric_line(self):
        self.state.metric(task="aaaa", phase="review", cost_usd=1.0)
        with self.state.metrics_path.open("a") as f:
            f.write("не json\n")
        self.assertEqual(self.state.total_spend(), 1.0)

    def test_budget_stops_mid_task_between_iterations(self):
        """Проверка раз в задачу позволяла одной задаче пробить потолок
        на любую величину — сколько раундов влезет."""
        def implement(task, feedback, iteration):
            # каждый раунд дорожает; отчёта нет — цикл идёт дальше
            self.state.metric(task="aaaa", phase="review", cost_usd=10.0)
            return

        agents = type("A", (), {"implement": staticmethod(implement),
                                "review": staticmethod(lambda *a, **k: None),
                                "commit_message": staticmethod(lambda t, d: "m")})()
        loop = cli.loop_mod.Loop(self.state, {"total_budget_usd": 5}, agents,
                                 ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        self.state.save_tasks({"goal": "g", "tasks": [
            {"id": "aaaa", "title": "t", "status": "pending", "deps": [],
             "type": "feature", "paths": ["mod.py"]}]})
        results = loop.run()
        self.assertEqual(results.get("_budget"), "exhausted",
                         "прогон не остановился посреди задачи")
        task = next(t for t in self.state.load_tasks()["tasks"])
        self.assertEqual(task["status"], "pending",
                         "задача не виновата в исчерпании бюджета")
        journal = self.state.journal_path.read_text()
        self.assertIn("budget_exhausted", journal)


class TestGoIsSelfSufficient(RepoCase):
    """План рождается ВНУТРИ роя, а не приносится снаружи."""

    def setUp(self):
        super().setUp()
        planner = _load("planner")
        self.planned = []
        planner.call_planner = lambda prompt, tag, attempt=1, root=None, \
                budget=None, model=None, effort=None, timeout=None: (
            self.planned.append(tag), (json.loads(json.dumps(DIFF)), None))[1]
        planner.metric = lambda **k: None
        self._orig_load = cli.load_mod
        cli.load_mod = (lambda name: planner if name == "planner"
                        else self._orig_load(name))
        self.ran = []
        self._orig_run = cli.loop_mod.Loop.run
        cli.loop_mod.Loop.run = lambda s, limit=None: (
            self.ran.append(limit) or {})

    def tearDown(self):
        cli.load_mod = self._orig_load
        cli.loop_mod.Loop.run = self._orig_run
        super().tearDown()

    def test_go_plans_then_runs(self):
        code, out = run_cli("--root", str(self.root), "go", "--goal", "новая цель")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.planned, ["plan"], "рой не спланировал сам")
        self.assertEqual(len(self.ran), 1, "после планирования не было прогона")

    def test_go_without_goal_uses_existing_queue(self):
        data = self.state.load_tasks()
        data["tasks"].append({"id": "cccc", "title": "ждёт", "status": "pending",
                              "deps": [], "type": "feature", "paths": ["mod.py"],
                              "acceptance": ["ok"]})
        self.state.save_tasks(data)
        code, _out = run_cli("--root", str(self.root), "go")
        self.assertEqual(code, 0)
        self.assertEqual(self.planned, [], "перепланировал непустую очередь")

    def test_go_skips_planning_when_queue_has_pending(self):
        data = self.state.load_tasks()
        data["tasks"].append({"id": "cccc", "title": "ждёт", "status": "pending",
                              "deps": [], "type": "feature", "paths": ["mod.py"],
                              "acceptance": ["ok"]})
        self.state.save_tasks(data)
        run_cli("--root", str(self.root), "go", "--goal", "другая цель")
        self.assertEqual(self.planned, [],
                         "незаконченная очередь затёрта новым планом")

    def test_go_refuses_dirty_tree(self):
        (self.root / "mod.py").write_text("работа человека\n")
        code, out = run_cli("--root", str(self.root), "go", "--goal", "цель")
        self.assertEqual(code, 2)
        self.assertIn("PREFLIGHT", out)

    def test_go_builds_board(self):
        run_cli("--root", str(self.root), "go", "--goal", "цель")
        self.assertTrue((self.state.dir / "board.html").exists(),
                        "доска не собрана — человеку опять не на что смотреть")


if __name__ == "__main__":
    unittest.main(verbosity=2)
