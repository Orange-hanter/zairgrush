#!/usr/bin/env python3
"""Тесты единой точки входа: команды, коды возврата, dry-run.

Агенты не вызываются: проверяется поведение оркестратора вокруг них.
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
spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)

TASKS = {"goal": "тестовая цель", "tasks": [
    {"id": "aaaa", "title": "первая", "type": "feature", "status": "pending",
     "deps": [], "paths": ["src/a.py"], "acceptance": ["тесты проходят"]},
    {"id": "bbbb", "title": "вторая", "type": "feature", "status": "pending",
     "deps": ["aaaa"], "paths": ["src/b.py"], "acceptance": ["тесты проходят"]},
]}


def run_cli(*argv):
    """Вызов CLI с перехватом вывода -> (код возврата, текст)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("def a():\n    return 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = cli.state_mod.SwarmState(self.root)
        self.state.save_tasks(json.loads(json.dumps(TASKS)))

    def tearDown(self):
        self.tmp.cleanup()


class TestStatus(CliCase):
    def test_shows_goal_and_tasks(self):
        code, out = run_cli("--root", str(self.root), "status")
        self.assertEqual(code, 0)
        self.assertIn("тестовая цель", out)
        self.assertIn("aaaa", out)

    def test_empty_queue_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out = run_cli("--root", empty, "status")
            self.assertEqual(code, 0)
            self.assertIn("пуст", out)

    def test_reports_unfinished_steps_after_crash(self):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        _code, out = run_cli("--root", str(self.root), "status")
        self.assertIn("НЕЗАВЕРШЁННЫЕ", out)
        self.assertIn("resume", out)

    def test_shows_blocked_reason_and_stash(self):
        self.state.set_status("aaaa", "blocked", reason="dispute",
                              stash="swarm:aaaa-dispute")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("dispute", out)
        self.assertIn("swarm:aaaa-dispute", out)


class TestDryRun(CliCase):
    def test_lists_only_ready_tasks(self):
        code, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("aaaa", out)
        self.assertNotIn("bbbb", out, "задача с незакрытой зависимостью "
                                      "не должна попадать в план прогона")

    def test_shows_baseline_gate(self):
        _, out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertIn("baseline gate", out)

    def test_agents_are_not_invoked(self):
        # если бы агенты вызывались, команда не уложилась бы в секунды
        # и потребовала бы kimi/claude в PATH
        code, _ = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(code, 0)

    def test_nothing_ready_returns_distinct_code(self):
        for tid in ("aaaa", "bbbb"):
            self.state.set_status(tid, "done")
        code, _out = run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(code, 3, "пустая выборка — отдельный код, не тишина")


class TestResume(CliCase):
    def test_unfinished_step_escalates(self):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        code, out = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 2, "возобновление с незавершённым шагом "
                                  "требует решения человека")
        self.assertIn("эскалация", out)

    def test_force_proceeds(self):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        code, _ = run_cli("--root", str(self.root), "resume", "--dry-run",
                          "--force")
        self.assertEqual(code, 0)

    def test_clean_state_resumes_without_force(self):
        code, _ = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 0)


class TestResumeReconciliation(CliCase):
    """§5.6: интент коммита с `head` доигрывается или откатывается сам.

    Прежде resume печатал «поправьте статус вручную» на ЛЮБОЙ
    незавершённый шаг — обещание «доиграть или откатить» из дизайн-дока
    оставалось на человеке.
    """

    def _intent(self, head):
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit", head=head)

    def _head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_no_commit_rolls_back_to_pending(self):
        self.state.set_status("aaaa", "in_progress")
        self._intent(self._head())          # HEAD не сдвигался: коммита не было
        code, out = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("реконсиляция", out)
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertEqual(task["status"], "pending",
                         "интент без действия обязан откатиться в очередь")

    def test_orchestrator_commit_completes_the_task(self):
        self.state.set_status("aaaa", "in_progress")
        self._intent(self._head())
        (self.root / "src" / "a.py").write_text("def a():\n    return 2\n")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "swarm-executor",
               "GIT_AUTHOR_EMAIL": "executor@swarm.local",
               "GIT_COMMITTER_NAME": "swarm-orchestrator",
               "GIT_COMMITTER_EMAIL": "orchestrator@swarm.local"}
        subprocess.run(["git", "commit", "-qam", "aaaa: работа"],
                       cwd=self.root, env=env, check=True)
        code, out = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 0, out)
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertEqual(task["status"], "done",
                         "коммит оркестратора найден — задачу надо доиграть")
        self.assertTrue(task.get("commit"), "sha коммита не записан в задачу")
        self.assertIn("step_done", self.state.journal_path.read_text())

    def test_foreign_commit_still_escalates(self):
        """Чужой коммит поверх интента — решает человек, а не эвристика."""
        self.state.set_status("aaaa", "in_progress")
        self._intent(self._head())
        (self.root / "src" / "a.py").write_text("# правка человека\n")
        subprocess.run(["git", "-c", "user.name=h", "-c", "user.email=h@h",
                        "commit", "-qam", "человеческий коммит"],
                       cwd=self.root, check=True)
        code, out = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("эскалация", out)


class TestQuotaExitCode(CliCase):
    """Пауза по квоте доходит до кода возврата 4 через ЛЮБЫЕ копии модулей.

    Модули грузятся по путям, и у cli, у Agents и у планировщика — свои
    объекты класса QuotaExceededError. `except loop_mod.QuotaExceededError`
    ловил только исключение собственной копии: квота из планировщика или
    агентов пролетала мимо и падала трассировкой. Ловушки сверяют имя
    класса — этот тест идёт настоящим путём cli -> planner и потому
    проверяет именно межкопийную сцепку.
    """

    def test_plan_quota_pauses_with_exit_4(self):
        orig_run = subprocess.run

        def fake_run(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig_run(argv, **kw)
            return type("R", (), {"stdout": json.dumps(
                {"is_error": True, "result": "usage limit reached"}),
                "stderr": "", "returncode": 1})()

        subprocess.run = fake_run
        self.addCleanup(lambda: setattr(subprocess, "run", orig_run))
        code, out = run_cli("--root", str(self.root), "plan", "--goal", "цель")
        self.assertEqual(code, 4, out)
        self.assertIn("пауза по квоте", out)


class TestConfigValidation(CliCase):
    """Опечатка в ключе молча включала умолчание — теперь она видима."""

    def _config_stderr(self, text):
        (self.root / "swarm.toml").write_text(text)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cfg = cli._config(self.root)
        return cfg, buf.getvalue()

    def test_unknown_key_warns(self):
        _cfg, err = self._config_stderr("max_iteration = 5\n")
        self.assertIn("незнакомые ключи", err)
        self.assertIn("max_iteration", err)

    def test_known_keys_stay_silent(self):
        cfg, err = self._config_stderr("max_iterations = 5\n")
        self.assertEqual(err, "")
        self.assertEqual(cfg["max_iterations"], 5)


class TestDoctor(CliCase):
    def test_reports_environment(self):
        _code, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("окружение", out)
        self.assertIn("git", out)

    def test_detects_clean_worktree(self):
        _, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("чист", out)

    def test_detects_dirty_worktree(self):
        (self.root / "src" / "new.py").write_text("x = 1\n")
        _, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("изменённых файлов", out)


class TestReport(CliCase):
    def test_renders_journal(self):
        self.state.log("dispute", task="aaaa", report={"status": "dispute"})
        code, out = run_cli("--root", str(self.root), "report")
        self.assertEqual(code, 0)
        self.assertIn("dispute", out)

    def test_filters_by_task(self):
        self.state.log("dispute", task="aaaa")
        self.state.log("dispute", task="bbbb")
        _, out = run_cli("--root", str(self.root), "report", "--task", "aaaa")
        self.assertIn("aaaa", out)
        self.assertNotIn("bbbb", out)


class TestReportIsProse(CliCase):
    """Отчёт — первая команда в порядке диагностики, а не дамп jsonl."""

    def test_events_read_as_sentences(self):
        self.state.log("round", task="aaaa", round=1, verdict="approve",
                       findings=0, outcome="confirm")
        _, out = run_cli("--root", str(self.root), "report")
        self.assertIn("раунд 1 → approve", out)
        self.assertIn("находок нет", out)
        self.assertIn("нужно подтверждение", out)
        self.assertNotIn('{"kind"', out, "проза, а не JSON")

    def test_groups_under_the_task_it_belongs_to(self):
        self.state.log("round", task="aaaa", round=1, verdict="approve",
                       findings=0)
        _, out = run_cli("--root", str(self.root), "report")
        self.assertIn("первая", out, "заголовок задачи берётся из очереди")

    def test_run_level_events_are_not_glued_to_a_task(self):
        # Событие прогона, приклеенное к задаче, объясняло бы остановку
        # очереди не тем.
        self.state.log("budget_exhausted", spent=51, budget=50,
                       stopped_before="aaaa")
        _, out = run_cli("--root", str(self.root), "report")
        self.assertIn("прогон в целом", out)
        self.assertIn("бюджет прогона исчерпан", out)

    def test_json_keeps_the_source_whole(self):
        """Проза — удобство; первоисточник обязан быть достижим целиком."""
        self.state.log("round", task="aaaa", round=1, verdict="approve",
                       findings=0, поле="x" * 400)
        _, out = run_cli("--root", str(self.root), "report", "--json")
        self.assertIn("x" * 400, out, "сырьё не обрезается")
        self.assertIn('"kind"', out)

    def test_nothing_for_this_task_is_said_plainly(self):
        self.state.log("round", task="aaaa", round=1, verdict="approve")
        _, out = run_cli("--root", str(self.root), "report", "--task", "zzzz")
        self.assertIn("zzzz", out)


class TestWhy(CliCase):
    """Один ответ на «почему встало», собранный из тех же файлов."""

    def _stall(self):
        self.state.set_status(
            "aaaa", "blocked", reason="max_iterations", iterations=3,
            stash="swarm:aaaa-max-iterations",
            diagnosis="число находок не убывает — вероятны качели fix→break")
        for rnd in (1, 2, 3):
            self.state.log("round", task="aaaa", round=rnd,
                           verdict="request_changes", findings=3)

    def test_explains_a_blocked_task(self):
        self._stall()
        code, out = run_cli("--root", str(self.root), "why", "aaaa")
        self.assertEqual(code, 0)
        self.assertIn("заблокирована", out)
        self.assertIn("раунды исчерпаны", out, "reason переведён, а не код")
        self.assertIn("качели", out, "диагноз петли показан человеку")

    def test_shows_the_trajectory_and_reads_it(self):
        self._stall()
        _, out = run_cli("--root", str(self.root), "why", "aaaa")
        self.assertIn("раунд 3 → request_changes", out)
        self.assertIn("стоит на месте", out)
        self.assertNotIn("сходилась", out,
                         "нельзя утверждать схождение под диагнозом о топтании")

    def test_offers_the_exact_command(self):
        self._stall()
        _, out = run_cli("--root", str(self.root), "why", "aaaa")
        self.assertIn("дальше:", out)
        self.assertIn("retry aaaa", out)

    def test_names_where_the_work_was_saved(self):
        self._stall()
        _, out = run_cli("--root", str(self.root), "why", "aaaa")
        self.assertIn("swarm:aaaa-max-iterations", out)

    def test_open_question_comes_with_its_answer_command(self):
        qid = self.state.ask("aaaa", "intent", "какой из двух путей верный?")
        self.state.set_status("aaaa", "blocked", reason="ask_user",
                              question_id=qid)
        _, out = run_cli("--root", str(self.root), "why", "aaaa")
        self.assertIn("какой из двух путей верный?", out)
        self.assertIn(f"answer {qid}", out)

    def test_without_argument_takes_the_task_that_stopped_the_queue(self):
        self._stall()
        code, out = run_cli("--root", str(self.root), "why")
        self.assertEqual(code, 0)
        self.assertIn("aaaa", out)

    def test_prefix_of_an_id_is_enough(self):
        self._stall()
        code, out = run_cli("--root", str(self.root), "why", "aa")
        self.assertEqual(code, 0)
        self.assertIn("aaaa", out)

    def test_unknown_task_is_an_error_not_silence(self):
        code, _out = run_cli("--root", str(self.root), "why", "zzzz")
        self.assertEqual(code, 2)

    def test_empty_queue_does_not_crash(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out = run_cli("--root", empty, "why")
            self.assertEqual(code, 0)
            self.assertIn("пуст", out)


class TestNextStep(CliCase):
    """Сводка отвечает «что происходит»; человек приходит с «что делать»."""

    def test_status_names_the_next_command(self):
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("дальше:", out)
        self.assertIn("go", out)

    def test_open_question_outranks_pending_work(self):
        self.state.ask("aaaa", "intent", "как быть?")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("answer q001", out)

    def test_blocked_task_sends_to_why(self):
        self.state.set_status("aaaa", "blocked", reason="dispute")
        self.state.set_status("bbbb", "blocked", reason="dispute")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("why aaaa", out)


class TestTrajectoryReading(unittest.TestCase):
    """Вывод по ряду находок делается строго.

    Первая редакция считала ряд убывающим, если он не растёт, и на 3,3,3
    печатала «работа сходилась» прямо под диагнозом петли «число находок
    не убывает». Ложное утверждение хуже, чем никакого: человек уходит
    чинить не то.
    """

    def test_flat_series_is_never_called_shrinking(self):
        text = cli._read_trend([3, 3, 3])
        self.assertIn("стоит на месте", text)
        self.assertNotIn("сходилась", text)

    def test_shrinking_series(self):
        self.assertIn("убывают", cli._read_trend([5, 3, 1]))

    def test_shrinking_to_zero_is_said_so(self):
        self.assertIn("до нуля", cli._read_trend([2, 0]))

    def test_swinging_series_is_named_as_such(self):
        self.assertIn("качели", cli._read_trend([2, 5, 3]))


class TestReadyToCopyCommands(unittest.TestCase):
    def test_current_root_does_not_get_a_flag(self):
        """Готовую команду копируют целиком — лишний путь ломает это."""
        self.assertEqual(cli._prefix("."), "swarm")
        self.assertEqual(cli._prefix(str(pathlib.Path.cwd())), "swarm")

    def test_other_root_keeps_the_flag(self):
        other = str(pathlib.Path.cwd().parent)
        self.assertIn("--root", cli._prefix(other))

    def test_round_label_is_readable(self):
        self.assertIn("раунд 3", cli._round_label("i3-a1"))
        self.assertIn("повторный", cli._round_label("i2-v1"))

    def test_unparsable_label_shown_as_is(self):
        self.assertEqual(cli._round_label("странное"), "странное")


class TestHelpCarriesTheMap(CliCase):
    def test_root_help_shows_the_order_of_use(self):
        _code, out = run_cli("--help")
        self.assertIn("порядок применения", out)
        self.assertIn("why", out)

    def test_subcommand_help_explains_itself(self):
        _code, out = run_cli("report", "--help")
        self.assertIn("хроника", out.lower())


class TestLocking(CliCase):
    def test_second_run_is_refused(self):
        other = cli.state_mod.SwarmState(self.root)
        other.acquire()
        try:
            code, out = run_cli("--root", str(self.root), "run", "--dry-run")
            self.assertEqual(code, 2)
            self.assertIn("занято", out)
        finally:
            other.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
