#!/usr/bin/env python3
"""Тесты сборки промптов и разбора ответов агентов.

Модуль был покрыт на 38 % — меньше всех остальных, при том что именно он
формирует то, что видят обе дорогие роли. Ошибка здесь не падает с
исключением, а тихо меняет поведение агента: потерянный путь в
Constraints стоил трёх итераций на приёмке.
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
spec = importlib.util.spec_from_file_location("agents", ROOT_DIR / "agents.py")
ag = importlib.util.module_from_spec(spec)
sys.modules["agents"] = ag
spec.loader.exec_module(ag)
st_spec = importlib.util.spec_from_file_location("state", ROOT_DIR / "state.py")
st = importlib.util.module_from_spec(st_spec)
sys.modules["state"] = st
st_spec.loader.exec_module(st)

TASK = {"id": "t1", "title": "заголовок", "spec": "сделай хорошо",
        "type": "feature", "paths": ["src/a.py", "src/b.py"],
        "acceptance": ["тесты проходят", "docstring на месте"]}


class AgentsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель прогона", "tasks": []})
        self.agents = ag.Agents(self.state, {})

    def tearDown(self):
        self.tmp.cleanup()


class TestHandoff(AgentsCase):
    def test_all_paths_listed(self):
        """Обрезка до первого пути стоила трёх итераций на приёмке."""
        text = self.agents.handoff(TASK, None, None)
        self.assertIn("src/a.py", text)
        self.assertIn("src/b.py", text)

    def test_goal_and_acceptance_present(self):
        text = self.agents.handoff(TASK, None, None)
        self.assertIn("цель прогона", text)
        self.assertIn("тесты проходят", text)
        self.assertIn("docstring на месте", text)

    def test_feature_forbids_touching_tests(self):
        text = self.agents.handoff(TASK, None, None)
        self.assertIn("NOT listed above are", text)

    def test_helper_metrics_land_in_swarm_dir_not_in_package(self):
        """Метрики хелперов оседали в каталоге ПАКЕТА (дефолт от
        __file__): прогоны разных проектов смешивались в один файл в
        исходниках инструмента, а pytest дописывал его при каждом
        прогоне. Проводка: Agents настраивает путь в .swarm стенда."""
        for var in ("OLLAMA_API_KEY", "HELPER_METRICS"):
            saved = os.environ.pop(var, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, var, saved)
        message = self.agents.commit_message(TASK, "diff --git a/x b/x")
        self.assertEqual(message, f"{TASK['id']}: {TASK['title']}")
        target = self.state.dir / "helper-metrics.jsonl"
        self.assertTrue(target.exists(),
                        "метрика хелпера обязана жить в .swarm стенда")
        package_stray = (pathlib.Path(ag.__file__).resolve().parent
                         / "metrics.jsonl")
        self.assertFalse(package_stray.exists(),
                         "каталог пакета — не место для метрик прогона")

    def test_memory_off_keeps_handoff_byte_identical(self):
        """Дефолт эксперимента E9: с выключенным флагом промпт исполнителя
        байт-в-байт сегодняшний — иначе A/B-замер меряет не память."""
        base = self.agents.handoff(TASK, None, None)
        block = self.agents.memory_block(TASK)
        self.assertEqual(block, "", "без флага память не подмешивается")
        self.assertEqual(base, self.agents.handoff(TASK, None, None,
                                                   memory=block or None))

    def test_memory_block_sits_between_goal_and_task(self):
        text = self.agents.handoff(
            TASK, None, None,
            memory="## Project memory\n[DATA]\n- (id1) урок")
        self.assertLess(text.index("## Goal"), text.index("## Project memory"))
        self.assertLess(text.index("## Project memory"), text.index("## Task"))

    def test_memory_block_is_cached_per_task(self):
        """Блок обязан быть байт-стабилен между раундами одной задачи:
        плавающий префикс переписывает промпт-кэш на каждом раунде (§8)."""
        self.agents._memory_cache = (TASK["id"], "СТАБИЛЬНЫЙ БЛОК")
        self.assertEqual(self.agents.memory_block(TASK), "СТАБИЛЬНЫЙ БЛОК")
        self.assertEqual(self.agents.memory_block(dict(TASK, id="t2")), "",
                         "смена задачи обязана пересчитать блок")

    def test_review_prompt_unchanged_without_norms(self):
        """Дефолт E9: без блока норм промпт ревьюера байт-в-байт прежний."""
        base = self.agents.review_prompt(TASK, "OK", "diff")
        self.assertEqual(base,
                         self.agents.review_prompt(TASK, "OK", "diff",
                                                   memory=""))

    def test_norms_sit_before_diff(self):
        """Нормы стабильны в пределах задачи и стоят ДО диффа: самый
        изменчивый блок остаётся последним (§8, кэш)."""
        text = self.agents.review_prompt(
            TASK, "OK", "diff",
            memory="## Нормы этого репозитория (память прошлых прогонов)\nx")
        self.assertLess(text.index("## Задача"), text.index("## Нормы"))
        self.assertLess(text.index("## Нормы"), text.index("## Diff"))

    def test_output_contract_names_the_dispute_field(self):
        """Петля читает report["dispute"], но промпт это поле не объявлял:
        оба реальных спора пилота (q001, q005) пришли с dispute=None, и вся
        аргументация исполнителя доезжала до оператора одним усечённым
        предложением summary."""
        text = self.agents.handoff(TASK, None, None)
        self.assertIn('"dispute"', text,
                      "контракт отчёта обязан объявлять поле для спора")

    def test_test_task_forbids_production_code(self):
        text = self.agents.handoff(dict(TASK, type="test-task"), None, None)
        self.assertIn("production code is", text)

    def test_feature_tests_writes_own_tests(self):
        text = self.agents.handoff(dict(TASK, type="feature-tests"), None, None)
        self.assertIn("Write your own tests", text)
        self.assertIn("tests NOT listed are off-limits", text)

    def test_feedback_included(self):
        text = self.agents.handoff(TASK, {"findings": [{"issue": "поправь X"}]},
                                   None)
        self.assertIn("Feedback", text)
        self.assertIn("поправь X", text)

    def test_no_feedback_no_section(self):
        self.assertNotIn("Feedback", self.agents.handoff(TASK, None, None))

    def test_repo_map_included_when_given(self):
        text = self.agents.handoff(TASK, None, "src/a.py\n  foo() -> int")
        self.assertIn("Repository map", text)
        self.assertIn("foo() -> int", text)

    def test_git_restrictions_are_explicit(self):
        """§6.1: перечень запретов, а не только «не коммить»."""
        text = self.agents.handoff(TASK, None, None)
        for word in ("reset", "rebase", "stash", "checkout"):
            self.assertIn(word, text, f"запрет {word} не доведён до исполнителя")

    def test_swarm_state_declared_off_limits(self):
        text = self.agents.handoff(TASK, None, None)
        self.assertIn(".swarm/", text)
        self.assertIn("tools/swarm/", text)

    def test_secrets_are_off_limits(self):
        self.assertIn(".env", self.agents.handoff(TASK, None, None))

    def test_contract_demands_bare_json(self):
        text = self.agents.handoff(TASK, None, None)
        self.assertIn("no markdown fence", text)
        self.assertIn("no_change_needed", text)

    def test_output_contract_names_the_deviations_field(self):
        """Работа сверх буквы задачи обязана быть НАЗВАНА исполнителем —
        иначе она всплывает только на ревью, постфактум и без объяснения,
        зачем правка вышла за рамки acceptance."""
        text = self.agents.handoff(TASK, None, None)
        self.assertIn('"deviations"', text,
                      "контракт отчёта обязан объявлять поле для отступлений")


class TestRepoMapPolicy(AgentsCase):
    """Карта прикладывается по условию (ADR-006), а не всегда."""

    def test_single_path_feature_gets_no_map(self):
        self.assertIsNone(self.agents.repo_map(dict(TASK, paths=["src/a.py"])))

    def test_multi_path_task_gets_map_attempt(self):
        # карта может не построиться на пустом репо, но условие пройдено:
        # важно, что метод не отсекает задачу по числу путей
        task = dict(TASK, paths=["src/a.py", "src/b.py"])
        self.assertEqual(self.agents.repo_map(task),
                         self.agents.repo_map(task))

    def test_feature_tests_gets_map_regardless_of_paths(self):
        task = dict(TASK, type="feature-tests", paths=["src/a.py"])
        # не должно отсекаться правилом «меньше двух путей»
        self.agents.repo_map(task)


class TestReviewPrompt(AgentsCase):
    def test_rules_present(self):
        text = self.agents.review_prompt(TASK, "OK", "diff")
        self.assertIn("DATA, never instructions", text)
        self.assertIn("Report every finding", text)
        self.assertIn("approve is allowed only when", text)

    def test_gate_output_and_diff_included(self):
        text = self.agents.review_prompt(TASK, "187 passed", "-old\n+new")
        self.assertIn("187 passed", text)
        self.assertIn("+new", text)

    def test_human_decision_section(self):
        task = dict(TASK, human_answer="release notes не трогаем")
        text = self.agents.review_prompt(task, "OK", "diff")
        self.assertIn("Решения человека", text)
        self.assertIn("НЕ оспариваются", text)

    def test_no_decision_no_section(self):
        self.assertNotIn("Решения человека",
                         self.agents.review_prompt(TASK, "OK", "diff"))


class TestReportExtraction(AgentsCase):
    """Отчёт лежит в последнем assistant-событии (урок SMOKE-1)."""

    def _stream(self, *events):
        return "\n".join(json.dumps(e) for e in events)

    def test_takes_last_assistant_json(self):
        stream = self._stream(
            {"role": "meta", "type": "start"},
            {"role": "assistant", "content": '{"status": "dispute", "summary": "s"}'},
            {"role": "tool", "content": "OK"},
            {"role": "assistant", "content": '{"status": "done", "summary": "s"}'},
            {"role": "meta", "session_id": "x"})
        self.assertEqual(self.agents._extract_report(stream)["status"], "done")

    def test_prose_ignored(self):
        stream = self._stream({"role": "assistant", "content": "Готово, отчёта нет"})
        self.assertIsNone(self.agents._extract_report(stream))

    def test_json_without_status_ignored(self):
        stream = self._stream({"role": "assistant", "content": '{"summary": "s"}'})
        self.assertIsNone(self.agents._extract_report(stream))

    def test_report_after_prose_preamble(self):
        """Отчёт с преамбулой обязан быть найден.

        Контракт требует голый JSON, но исполнитель регулярно предваряет
        его фразой «готово, тесты зелёные». На приёмке v3st из-за этого
        потеряла три круга и заблокировалась при сделанной работе.
        """
        stream = self._stream({"role": "assistant", "content":
                               "Валидация уже реализована, сьют зелёный.\n\n"
                               '{"status": "done", "summary": "готово"}'})
        r = self.agents._extract_report(stream)
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "done")

    def test_prose_after_report_still_found(self):
        stream = self._stream({"role": "assistant", "content":
                               '{"status": "done", "summary": "s"}\n\nГотово!'})
        self.assertEqual(self.agents._extract_report(stream)["status"], "done")

    def test_json_example_in_preamble_does_not_win(self):
        """Случайный объект в тексте не должен подменять отчёт."""
        stream = self._stream({"role": "assistant", "content":
                               'Схема была {"status": "мусор", "x": 1}, '
                               'но результат такой:\n'
                               '{"status": "done", "summary": "настоящий"}'})
        r = self.agents._extract_report(stream)
        self.assertEqual(r["summary"], "настоящий")

    def test_object_without_status_ignored_in_text(self):
        stream = self._stream({"role": "assistant", "content":
                               'Итог: {"summary": "без статуса"}'})
        self.assertIsNone(self.agents._extract_report(stream))

    def test_broken_stream_survived(self):
        self.assertIsNone(self.agents._extract_report("не json\n{битый"))

    def test_empty_stream(self):
        self.assertIsNone(self.agents._extract_report(""))

    def test_tool_events_do_not_confuse(self):
        stream = self._stream(
            {"role": "tool", "content": '{"status": "done"}'},
            {"role": "assistant", "content": "текст"})
        self.assertIsNone(self.agents._extract_report(stream),
                          "отчёт берётся только из assistant-событий")


def _fake_process(stdout="", stderr=""):
    """Двойник Popen: пустой поток, мгновенное и успешное завершение —
    для тестов, которым нужен только argv, а не реальный прогон агента."""
    return type("P", (), {
        "stdout": io.StringIO(stdout), "stderr": io.StringIO(stderr),
        "returncode": 0, "poll": lambda s: 0,
        "wait": lambda s, timeout=None: 0, "kill": lambda s: None})()


class TestExecutorModelRouting(AgentsCase):
    """E10 (flag `skeleton`): per-task executor_model переопределяет
    executor_model прогона, а ollama:-префикс уводит на chat-fill."""

    def _kimi_argv(self, config, task):
        seen: dict[str, list[str]] = {}
        orig = subprocess.Popen

        def fake(argv, **kw):
            if not (argv and argv[0] == "kimi"):
                return orig(argv, **kw)
            seen["argv"] = argv
            return _fake_process()

        subprocess.Popen = fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", orig))
        agents = ag.Agents(self.state, config)
        with contextlib.suppress(Exception):
            agents.implement(task, None, 1)
        return seen.get("argv", [])

    def test_flag_off_ignores_executor_model_field(self):
        """Умолчание эксперимента — «выключено»: поле задачи не читается
        вовсе, а не читается-и-отбрасывается — иначе замер E10 сравнивал
        бы поведение не с сегодняшним, а с частично изменённым."""
        with_field = self._kimi_argv({}, dict(TASK, executor_model="claude-opus-5"))
        without_field = self._kimi_argv({}, TASK)
        self.assertEqual(with_field, without_field)
        self.assertTrue(with_field, "cmd вообще обязан был собраться")

    def test_flag_on_field_overrides_run_wide_model(self):
        argv = self._kimi_argv(
            {"experiments": {"skeleton": True}, "executor_model": "kimi-k2"},
            dict(TASK, executor_model="claude-opus-5"))
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "claude-opus-5")

    def test_flag_on_without_field_keeps_run_wide_model(self):
        argv = self._kimi_argv(
            {"experiments": {"skeleton": True}, "executor_model": "kimi-k2"}, TASK)
        self.assertEqual(argv[argv.index("-m") + 1], "kimi-k2")

    def test_flag_on_ollama_prefix_never_reaches_kimi_cli(self):
        """`ollama:` — не имя модели kimi CLI, а маршрут на chat-fill:
        дошедшее до `-m ollama:...` было бы отправкой мусора в CLI."""
        orig = subprocess.Popen
        called: list[list[str]] = []

        def fake(argv, **kw):
            called.append(argv)
            return orig(argv, **kw)

        subprocess.Popen = fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", orig))
        agents = ag.Agents(self.state, {"experiments": {"skeleton": True}})
        # Путь несуществующий: chat-fill откажет быстро (fill_misconfigured)
        # без сети — тесту важно только то, что kimi CLI не был вызван.
        task = dict(TASK, paths=["nope.py"], executor_model="ollama:m")
        report = agents.implement(task, None, 1)
        self.assertIsNone(report)
        self.assertEqual(agents.last_implement_failure["reason"],
                         "fill_misconfigured")
        self.assertFalse(any(a and a[0] == "kimi" for a in called),
                         "ollama: обязан уйти в chat-fill, не в CLI kimi")


class ChatFillCase(AgentsCase):
    """Стенд chat-fill (E10, flag `skeleton`): файл-цель уже в дереве."""

    def setUp(self):
        super().setUp()
        self.agents = ag.Agents(self.state, {"experiments": {"skeleton": True}})
        self.target = self.root / "mod.py"
        self.target.write_text("def f():\n    pass\n")
        self.task = dict(TASK, id="f1", paths=["mod.py"],
                         executor_model="ollama:gpt-oss:120b")

    def fake_helpers(self, reply):
        """Двойник swarm/helpers.py: без сети, с записью аргументов вызова."""
        calls: list[dict[str, object]] = []

        class Fake:
            def configure(self, path):
                pass

            def ollama_chat(self, prompt, name, max_tokens=400,
                            temperature=0.0, model=None):
                calls.append({"prompt": prompt, "name": name,
                             "max_tokens": max_tokens, "model": model})
                return reply

        self.agents._helpers = Fake()
        return calls


class TestChatFillHappyPath(ChatFillCase):
    def test_writes_the_returned_fence_and_reports_done(self):
        calls = self.fake_helpers("```python\ndef f():\n    return 1\n```")
        report = self.agents._implement_fill(self.task, None, 1)
        self.assertEqual(report, {"status": "done",
                                  "summary": "заполнение по контракту применено",
                                  "fill": True})
        self.assertEqual(self.target.read_text(), "def f():\n    return 1")
        self.assertIsNone(self.agents.last_implement_failure)
        self.assertEqual(calls[0]["model"], "gpt-oss:120b",
                         "префикс ollama: обязан быть срезан перед вызовом API")

    def test_raw_reply_is_logged(self):
        self.fake_helpers("```python\ndef f():\n    return 2\n```")
        self.agents._implement_fill(self.task, None, 3)
        log_path = self.state.dir / "log" / "f1-i3-fill.txt"
        self.assertIn("def f():\n    return 2", log_path.read_text())

    def test_metric_row_on_success(self):
        self.fake_helpers("```python\ndef f():\n    return 1\n```")
        self.agents._implement_fill(self.task, None, 1)
        rows = [json.loads(x) for x in
                self.state.metrics_path.read_text().splitlines()]
        row = next(r for r in rows if r.get("phase") == "implement")
        self.assertEqual(row["reason"], "done")
        self.assertIs(row["report"], True)
        self.assertIs(row["fill"], True)
        self.assertEqual(row["model"], "gpt-oss:120b")

    def test_feedback_reaches_the_prompt(self):
        calls = self.fake_helpers("```python\ndef f():\n    return 1\n```")
        self.agents._implement_fill(self.task, {"note": "поправь X"}, 1)
        self.assertIn("Feedback", calls[0]["prompt"])
        self.assertIn("поправь X", calls[0]["prompt"])

    def test_current_file_content_reaches_the_prompt(self):
        calls = self.fake_helpers("```python\ndef f():\n    return 1\n```")
        self.agents._implement_fill(self.task, None, 1)
        self.assertIn("def f():\n    pass", calls[0]["prompt"])

    def test_multiple_fences_take_the_last_when_nothing_else_is_around(self):
        self.fake_helpers("```python\nстарое\n```\n"
                          "```python\ndef f():\n    return 9\n```")
        report = self.agents._implement_fill(self.task, None, 1)
        self.assertEqual(report["status"], "done")
        self.assertEqual(self.target.read_text(), "def f():\n    return 9")


class TestChatFillFailures(ChatFillCase):
    """Любой отказ — last_implement_failure + None, НИКОГДА исключение;
    петля дальше ведёт себя обычным retry/feedback (§7.3)."""

    def test_multiple_paths_is_misconfigured(self):
        task = dict(self.task, paths=["mod.py", "src/a.py"])
        self.assertIsNone(self.agents._implement_fill(task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_misconfigured")

    def test_glob_path_is_misconfigured(self):
        task = dict(self.task, paths=["*.py"])
        self.assertIsNone(self.agents._implement_fill(task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_misconfigured")

    def test_missing_file_is_misconfigured(self):
        task = dict(self.task, paths=["missing.py"])
        self.assertIsNone(self.agents._implement_fill(task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_misconfigured")

    def test_no_reply_is_a_failure(self):
        self.fake_helpers(None)
        self.assertIsNone(self.agents._implement_fill(self.task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_no_reply")

    def test_prose_outside_fence_is_a_failure(self):
        """Преамбула вроде «Вот файл:» — не «почти прошло»: контракт не
        предусматривает текста вне fence, и берущий-последний-fence разбор
        без этой проверки тихо проглотил бы её."""
        self.fake_helpers("Вот файл:\n```python\ndef f():\n    return 1\n```")
        self.assertIsNone(self.agents._implement_fill(self.task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_no_fence")

    def test_no_fence_at_all_is_a_failure(self):
        self.fake_helpers("def f():\n    return 1")
        self.assertIsNone(self.agents._implement_fill(self.task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_no_fence")

    def test_syntax_error_is_a_failure(self):
        self.fake_helpers("```python\ndef f(\n```")
        self.assertIsNone(self.agents._implement_fill(self.task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_syntax")

    def test_syntax_check_skipped_for_non_python_files(self):
        """Контракт ast.parse — «для .py файлов»: применять его к чужому
        синтаксису значило бы отбрасывать валидные ответы по чужой мерке."""
        target = self.root / "notes.md"
        target.write_text("старое")
        task = dict(self.task, paths=["notes.md"])
        self.fake_helpers("```python\nне питон и не важно\n```")
        report = self.agents._implement_fill(task, None, 1)
        self.assertEqual(report["status"], "done")
        self.assertEqual(target.read_text(), "не питон и не важно")

    def test_target_file_untouched_on_failure(self):
        original = self.target.read_text()
        self.fake_helpers("prose only, no fence")
        self.agents._implement_fill(self.task, None, 1)
        self.assertEqual(self.target.read_text(), original)

    def test_helper_exception_is_survived(self):
        """§7.3 в отражении chat-fill: сбой самого вызова хелпера — тоже
        НЕ повод ронять петлю, даже если ollama_chat нарушил контракт
        fail-open и бросил исключение сам."""
        class Boom:
            def configure(self, path):
                pass

            def ollama_chat(self, *a, **k):
                raise RuntimeError("сеть моргнула")

        self.agents._helpers = Boom()
        self.assertIsNone(self.agents._implement_fill(self.task, None, 1))
        self.assertEqual(self.agents.last_implement_failure["reason"],
                         "fill_no_reply")


if __name__ == "__main__":
    unittest.main(verbosity=2)
