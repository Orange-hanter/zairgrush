#!/usr/bin/env python3
"""Тесты единой точки входа: команды, коды возврата, dry-run.

Агенты не вызываются: проверяется поведение оркестратора вокруг них.
"""
import builtins
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

# `cli` импортирует функции из clirun через `from clirun import ...`, то есть
# копирует ссылки в свой namespace. Патчить `cli._board_open` бесполезно:
# реальная команда `run`/`go` берёт `_board_open` из модуля clirun.
clirun = sys.modules["clirun"]

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
        # Автооткрытие доски (feature 2) реально дёргает macOS `open` —
        # на машине разработчика это буквально распахнуло бы окно на
        # каждый вызов run/go в этом файле. Тесты про само поведение
        # (TestBoardAutoOpen) возвращают настоящий помощник явно.
        # Патчим реальный `_board_open` в clirun, а не re-export в cli:
        # иначе почти каждый тест тихо поднимал HTTP-сервер доски и
        # вызывал macOS `open`, что приводило к гонке с tearDown.
        self._real_board_open = clirun._board_open
        clirun._board_open = (lambda root, cfg:
                              (pathlib.Path(root) / ".swarm" / "board.html", None))
        self.addCleanup(setattr, clirun, "_board_open", self._real_board_open)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_loop(self, results):
        """Подставная петля с заготовленными исходами.

        Проверяется обвязка вокруг петли (коды возврата, итоговая
        строка), а не сама петля — живые агенты здесь не нужны.
        """
        class FakeLoop:
            def __init__(self, *a, **kw):
                pass

            def run(self, limit=None):
                return dict(results)

        original = cli.loop_mod.Loop
        cli.loop_mod.Loop = FakeLoop
        self.addCleanup(setattr, cli.loop_mod, "Loop", original)


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

    def _stub_run(self):
        """Хвост resume — обычный `run`; здесь проверяется реконсиляция,
        и живой цикл с вызовом агентов ей только мешает."""
        original = cli.cmd_run
        cli.cmd_run = lambda args: 0
        self.addCleanup(setattr, cli, "cmd_run", original)

    def _head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_no_commit_rolls_back_to_pending(self):
        self.state.set_status("aaaa", "in_progress")
        self._intent(self._head())          # HEAD не сдвигался: коммита не было
        self._stub_run()
        code, out = run_cli("--root", str(self.root), "resume")
        self.assertEqual(code, 0, out)
        self.assertIn("реконсиляция", out)
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertEqual(task["status"], "pending",
                         "интент без действия обязан откатиться в очередь")

    def test_dry_run_previews_without_touching_state(self):
        """Сухой прогон обещает не трогать состояние — а реконсиляция
        писала step_failed в журнал и переписывала tasks.json до всякой
        проверки флага: оператор хотел ПОСМОТРЕТЬ, что сделает resume, и
        получал уже сделанное."""
        self.state.set_status("aaaa", "in_progress")
        self._intent(self._head())
        journal_before = self.state.journal_path.read_text()
        tasks_before = self.state.tasks_path.read_text()
        self._stub_run()
        code, out = run_cli("--root", str(self.root), "resume", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("dry-run", out)
        self.assertIn("вернётся в очередь", out,
                      "оператору обязаны сказать, ЧТО сделает resume")
        self.assertEqual(self.state.journal_path.read_text(), journal_before,
                         "сухой прогон не имеет права писать в журнал")
        self.assertEqual(self.state.tasks_path.read_text(), tasks_before,
                         "сухой прогон не имеет права менять очередь")

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
        self._stub_run()
        code, out = run_cli("--root", str(self.root), "resume")
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
    """Пауза по квоте доходит до кода возврата 4 через весь путь cli -> planner.

    Модули петли грузятся по путям (importlib), поэтому важно, что
    детектор quota_exception сверяет ИМЯ класса, а не identity. Этот тест
    идёт настоящим путём cli -> planner и проверяет, что квотный отказ
    из планировщика корректно превращается в exit-код 4.
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
            cfg = cli.load_config(self.root)
        return cfg, buf.getvalue()

    def test_unknown_key_warns(self):
        _cfg, err = self._config_stderr("max_iteration = 5\n")
        self.assertIn("незнакомые ключи", err)
        self.assertIn("max_iteration", err)

    def test_known_keys_stay_silent(self):
        cfg, err = self._config_stderr("max_iterations = 5\n")
        self.assertEqual(err, "")
        self.assertEqual(cfg["max_iterations"], 5)

    def test_memory_mode_that_is_not_a_role_warns(self):
        """Значение флага памяти — имя роли. Опечатка в нём выключает
        подсистему целиком и ничем не отличима от `off`: замер «ноль
        вызовов эмбеддера за пилот» начинался ровно с такой тишины."""
        _cfg, err = self._config_stderr(
            '[experiments]\nmemory = "planer"\n')
        self.assertIn("не роль", err)
        self.assertIn("planner", err, "подсказка обязана назвать роли")

    def test_planner_mode_is_a_role_and_stays_silent(self):
        _cfg, err = self._config_stderr(
            '[experiments]\nmemory = "planner"\n')
        self.assertEqual(err, "")

    def test_doc_context_mode_that_is_not_a_role_warns(self):
        _cfg, err = self._config_stderr(
            '[experiments]\ndoc_context = "planer"\n')
        self.assertIn("не роль", err)
        self.assertIn("executor", err, "подсказка обязана назвать роли")

    def test_executor_doc_context_mode_is_a_role_and_stays_silent(self):
        _cfg, err = self._config_stderr(
            '[experiments]\ndoc_context = "executor"\n')
        self.assertEqual(err, "")

    def test_engine_that_is_not_an_engine_warns(self):
        """Опечатка в имени движка дороже прочих: работа ушла бы не тому
        агенту, которого выбрал оператор, и плечо замера оказалось бы
        чужим. Отказ — на старте прогона, предупреждение — уже здесь."""
        _cfg, err = self._config_stderr('executor_engine = "claudee"\n')
        self.assertIn("не движок", err)
        self.assertIn("claude", err, "подсказка обязана назвать движки")

    def test_gate_command_as_a_string_warns(self):
        """Строкой этот ключ пишут чаще, чем списком, а падает он посреди
        первой задачи: петля передаёт его в exec без шелла, строка
        становится ИМЕНЕМ файла, и задача уходит в blocked как «авария».
        Замерено живым прогоном 2026-08-22."""
        _cfg, err = self._config_stderr(
            'gate_command = "python3 -m unittest discover -q"\n')
        self.assertIn("gate_command", err)
        self.assertIn("списком", err)

    def test_gate_command_as_a_list_stays_silent(self):
        cfg, err = self._config_stderr(
            'gate_command = ["python3", "-m", "pytest"]\n')
        self.assertEqual(err, "")
        self.assertEqual(cfg["gate_command"], ["python3", "-m", "pytest"])

    def test_engine_keys_are_known(self):
        cfg, err = self._config_stderr(
            'executor_engine = "claude"\nexecutor_effort = "low"\n'
            "executor_budget_usd = 2.5\n")
        self.assertEqual(err, "")
        self.assertEqual(cfg["executor_engine"], "claude")


class TestEnginePreflight(CliCase):
    """Кем исполнять — говорится ДО первого потраченного доллара."""

    def test_run_refuses_an_unknown_engine(self):
        (self.root / "swarm.toml").write_text('executor_engine = "sonnet"\n')
        code, out = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, 2)
        self.assertIn("движок исполнителя не выбран", out)

    def test_go_also_refuses_an_unknown_engine(self):
        """Мутационный аудит нашёл эту дыру: `go` — вход «от и до», и
        именно он тратит деньги на планирование ДО первой задачи. Проверка
        движка в одном из двух входов — это проверка в половине случаев."""
        (self.root / "swarm.toml").write_text('executor_engine = "sonnet"\n')
        code, out = run_cli("--root", str(self.root), "go")
        self.assertEqual(code, 2)
        self.assertIn("движок исполнителя не выбран", out)

    def test_run_names_the_engine_it_will_use(self):
        """Строка вывода — не украшение: прогон на чужом движке выглядит
        точно так же, как прогон на своём, пока никто не назвал движок."""
        (self.root / "swarm.toml").write_text(
            'executor_engine = "claude"\nexecutor_model = "sonnet"\n')
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertIn("исполнитель: claude (sonnet)", out)

    def test_same_model_for_writer_and_judge_is_named(self):
        """§3.2 опирался и на независимость судьи: дифф пишет одна
        модель, судит другая. Одинаковая модель эту опору убирает, и
        молчать об этом нельзя."""
        (self.root / "swarm.toml").write_text(
            'executor_engine = "claude"\nexecutor_model = "sonnet"\n'
            'review_model = "sonnet"\n')
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertIn("независимость судьи", out)

    def test_undiverged_confirming_round_is_named(self):
        """§8.2 обещает второй ВЗГЛЯД, а конфиг по умолчанию оплачивает
        второй раз тот же вопрос: подтверждение ревьюит тот же дифф теми
        же параметрами. Расхождение обещания и поведения обязано быть
        видно на старте, а не выясняться по счёту."""
        (self.root / "swarm.toml").write_text("confirmations = 2\n")
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertIn("не разведён", out)

    def test_diverged_confirming_round_stays_quiet(self):
        (self.root / "swarm.toml").write_text(
            'confirmations = 2\nconfirm_effort = "medium"\n')
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertNotIn("не разведён", out)

    def test_single_confirmation_needs_no_divergence(self):
        """Один подтверждающий раунд — это и есть отсутствие второго
        прохода: разводить нечего, и предупреждение было бы шумом."""
        (self.root / "swarm.toml").write_text("confirmations = 1\n")
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertNotIn("не разведён", out)

    def test_default_engine_says_kimi_and_stays_quiet(self):
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertIn("исполнитель: kimi", out)
        self.assertNotIn("независимость судьи", out)


class TestDoctor(CliCase):
    def test_names_the_engine_it_checked(self):
        """Доктор обязан проверять ВЫБРАННОЕ. Пока движок был один,
        машина без `kimi` получала красную строку за роль, которой на
        ней нет: диагноз говорил о чужой конфигурации."""
        (self.root / "swarm.toml").write_text(
            'executor_engine = "claude"\nexecutor_model = "sonnet"\n')
        _code, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("движок исполнителя", out)
        self.assertIn("claude, модель sonnet", out)

    def test_gate_command_shape_is_checked(self):
        (self.root / "swarm.toml").write_text(
            'gate_command = "python3 -m pytest"\n')
        _code, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("ПРОБЛ", out)
        self.assertIn("СПИСОК", out)

    def test_broken_engine_is_a_problem_line(self):
        (self.root / "swarm.toml").write_text('executor_engine = "nope"\n')
        _code, out = run_cli("--root", str(self.root), "doctor")
        self.assertIn("executor_engine", out)
        self.assertIn("не движок", out)

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

    def _patch_tree_sitter(self, present: set[str], clib: bool) -> None:
        original = cli.importlib.util.find_spec
        cli.importlib.util.find_spec = (
            lambda name: object() if name in present else None)
        self.addCleanup(setattr, cli.importlib.util, "find_spec", original)
        original_clib = cli.tree_sitter_clib
        cli.tree_sitter_clib = lambda: clib
        self.addCleanup(setattr, cli, "_tree_sitter_clib", original_clib)

    def test_missing_tree_sitter_is_reported_honestly(self):
        """find_spec сообщает об отсутствии top-level модуля значением
        None, а не исключением: проверка через try ловила пустоту, и
        доктор объявлял tree-sitter доступным на ЛЮБОЙ машине — ровно
        тот класс лжи о среде, ради которого doctor заведён."""
        self._patch_tree_sitter(present=set(), clib=False)
        _, out = run_cli("--root", str(self.root), "doctor")
        line = next(ln for ln in out.splitlines() if "tree-sitter" in ln)
        self.assertIn("не установлен", line)
        self.assertIn("pip install tree-sitter tree-sitter-python "
                      "tree-sitter-rust", line)

    def test_brew_clib_alone_is_named_not_denied(self):
        """brew-пакет tree-sitter — только C-библиотека. Доктор говорил
        «не установлен» человеку, у которого `brew list` показывает
        tree-sitter, — спор шёл о двух разных вещах. Теперь доктор
        называет установленный слой и недостающий."""
        self._patch_tree_sitter(present=set(), clib=True)
        _, out = run_cli("--root", str(self.root), "doctor")
        line = next(ln for ln in out.splitlines() if "tree-sitter" in ln)
        self.assertIn("C-библиотека", line)
        self.assertIn("pip install", line)
        self.assertNotIn("не установлен", line)

    def test_partial_bindings_name_the_missing_grammar(self):
        """tsindex требует обе грамматики: один модуль tree_sitter без
        них давал «доступен», а слой падал на импорте грамматик."""
        self._patch_tree_sitter(present={"tree_sitter", "tree_sitter_python"},
                                clib=False)
        _, out = run_cli("--root", str(self.root), "doctor")
        line = next(ln for ln in out.splitlines() if "tree-sitter" in ln)
        self.assertIn("неполные", line)
        self.assertIn("pip install tree-sitter-rust", line)

    def test_full_bindings_report_ok(self):
        self._patch_tree_sitter(
            present={"tree_sitter", "tree_sitter_python", "tree_sitter_rust"},
            clib=False)
        _, out = run_cli("--root", str(self.root), "doctor")
        line = next(ln for ln in out.splitlines() if "tree-sitter" in ln)
        self.assertIn("[  ok ]", line)
        self.assertIn("грамматики py/rs", line)


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


class TestLoopOutputIsVisible(unittest.TestCase):
    """Прогон в фоне писал в файл НОЛЬ БАЙТ пятьдесят минут.

    Петля исправно печатала каждый раунд, но голый `print` при
    перенаправлении stdout буферизуется поблочно, и всё лежало в буфере до
    конца процесса. Для инструмента, чья ценность в наблюдаемости хода,
    невидимый вывод равносилен отсутствию вывода.
    """

    def test_ui_flushes_every_line(self):
        seen = []
        real = builtins.print

        def spy(*args, **kwargs):
            seen.append(kwargs.get("flush"))
            real(*args, **{k: v for k, v in kwargs.items() if k != "flush"})

        builtins.print = spy
        try:
            cli.ui("=== задача")
            cli.ui("    раунд 1")
        finally:
            builtins.print = real
        self.assertEqual(seen, [True, True],
                         "без flush вывод не доходит до файла до конца прогона")

    def test_loop_gets_the_flushing_ui(self):
        """Проверка проводки: сам по себе `_ui` бесполезен, если петле
        по-прежнему передают голый `print`."""
        src = (pathlib.Path(cli.__file__).read_text(encoding="utf-8")
               if hasattr(cli, "__file__") else "")
        self.assertNotIn("ui=print", src,
                         "петля обязана получать печать со сбросом буфера")


class TestRunExitCodes(CliCase):
    """Коды возврата прогона — машинный контракт, а не украшение.

    loop.py объявляет EXIT_* «машинным контрактом для внешнего скрипта»,
    но ни `run`, ни `go` исходы петли в код процесса не отображали:
    прогон, вставший по бюджету или на недоступном исполнителе, выходил
    с нулём, и скрипт поверх не мог отличить «сделано» от «встало».
    """

    def test_budget_stop_gets_its_own_code(self):
        self.fake_loop({"aaaa": "done", "_budget": "exhausted"})
        code, out = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, cli.EXIT_BUDGET, out)

    def test_blocked_outcome_needs_human(self):
        self.fake_loop({"aaaa": "blocked"})
        code, _ = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, cli.EXIT_NEEDS_HUMAN)

    def test_ask_user_outcome_needs_human(self):
        self.fake_loop({"aaaa": "ask_user"})
        code, _ = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, cli.EXIT_NEEDS_HUMAN)

    def test_executor_unavailable_gets_its_own_code(self):
        self.fake_loop({"_executor": "unavailable"})
        code, _ = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, cli.EXIT_NO_EXECUTOR)

    def test_all_done_is_zero(self):
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        code, _ = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, cli.EXIT_QUEUE_DONE)

    def test_go_uses_the_same_mapping(self):
        """Одна формула на обе команды: своя копия в `go` разъехалась бы
        с `run` при первом же новом исходе петли."""
        self.fake_loop({"aaaa": "blocked"})
        code, _ = run_cli("--root", str(self.root), "go")
        self.assertEqual(code, cli.EXIT_NEEDS_HUMAN)

    def test_go_prints_run_events_in_summary(self):
        """`go` фильтровал ключи с «_» и печатал `итог: {}` у прогона,
        вставшего по бюджету, — причина остановки пряталась ровно из
        той строки, где её ищут (в `run` она при этом была видна)."""
        self.fake_loop({"_budget": "exhausted"})
        code, out = run_cli("--root", str(self.root), "go")
        self.assertIn("_budget", out, "причина остановки не служебный шум")
        self.assertEqual(code, cli.EXIT_BUDGET)

    def test_epilog_documents_the_contract(self):
        _, out = run_cli("--help")
        self.assertIn("коды возврата", out)
        self.assertIn("12", out)


class TestGoGoalGuard(CliCase):
    """`go --goal` при непустой очереди молча пропускал планирование.

    data["goal"] при этом не обновлялся: status и доска показывали
    старую цель, а policies() фильтруют решения человека по цели — под
    чужой вывеской они молча теряют силу. Расхождение целей — повод
    отказаться, а не продолжить не под тем флагом.
    """

    def test_different_goal_is_refused_loudly(self):
        code, out = run_cli("--root", str(self.root), "go",
                            "--goal", "совсем другая цель")
        self.assertEqual(code, 2)
        self.assertIn("тестовая цель", out, "старая цель названа")
        self.assertIn("совсем другая цель", out, "новая цель названа")
        self.assertIn("plan --goal", out, "выход подсказан")
        goal = cli.state_mod.SwarmState(self.root).load_tasks()["goal"]
        self.assertEqual(goal, "тестовая цель",
                         "цель не должна подменяться молча")

    def test_same_goal_proceeds(self):
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        code, out = run_cli("--root", str(self.root), "go",
                            "--goal", "тестовая цель")
        self.assertEqual(code, 0, out)
        self.assertIn("планирование пропущено", out)


class TestBoardAutoOpen(CliCase):
    """Доска открывается сама при старте прогона — оператор во время
    живого прогона не понимал, как за ним следить, пока не открывал
    доску руками. Правило: открывать, пока не отключили явно.
    """

    def setUp(self):
        super().setUp()
        # Базовый CliCase глушит автооткрытие для всех прочих тестов —
        # здесь проверяется само поведение, поэтому возвращаем помощника.
        # Работать надо с модулем clirun, потому что именно там cmd_run
        # разрешает имя `_board_open`.
        clirun._board_open = self._real_board_open
        self._orig_platform = cli.sys.platform
        self.addCleanup(setattr, cli.sys, "platform", self._orig_platform)
        # Живой сервер — модульный синглтон (см. докстринг _BOARD_SERVER):
        # без остановки между тестами он пережил бы тест, привязанный к
        # уже удалённому tempdir предыдущего теста.
        self.addCleanup(self._stop_board_server)

    def _stop_board_server(self):
        if clirun._BOARD_SERVER is not None:
            clirun._BOARD_SERVER.stop()
        clirun._BOARD_SERVER = None

    def _capture_open_calls(self):
        calls = []
        orig_run = subprocess.run

        def fake_run(argv, **kw):
            if argv and argv[0] == "open":
                calls.append(argv)
                return type("R", (), {"returncode": 0})()
            return orig_run(argv, **kw)

        subprocess.run = fake_run
        self.addCleanup(lambda: setattr(subprocess, "run", orig_run))
        return calls

    def test_disabled_by_config_skips_open(self):
        (self.root / "swarm.toml").write_text("board_open = false\n")
        cli.sys.platform = "darwin"
        calls = self._capture_open_calls()
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        run_cli("--root", str(self.root), "run")
        self.assertEqual(calls, [])

    def test_default_opens_on_darwin(self):
        cli.sys.platform = "darwin"
        calls = self._capture_open_calls()
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        run_cli("--root", str(self.root), "run")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "open")

    def test_dry_run_never_opens(self):
        cli.sys.platform = "darwin"
        calls = self._capture_open_calls()
        run_cli("--root", str(self.root), "run", "--dry-run")
        self.assertEqual(calls, [])

    def test_not_darwin_prints_header_but_does_not_open(self):
        """Живой сервер поднимается на любой платформе — платформа решает
        только судьбу команды `open` (macOS-специфична), не самого
        сервера. На не-macOS в шапке печатается адрес живой доски, а
        браузер сам не распахивается."""
        cli.sys.platform = "linux"
        calls = self._capture_open_calls()
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        _code, out = run_cli("--root", str(self.root), "run")
        self.assertEqual(calls, [])
        self.assertRegex(out, r"доска: http://127\.0\.0\.1:\d+/")

    def test_go_prints_the_board_header_line(self):
        cli.sys.platform = "linux"
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        _code, out = run_cli("--root", str(self.root), "go")
        self.assertRegex(out, r"доска: http://127\.0\.0\.1:\d+/")

    def test_failed_open_only_warns_and_does_not_stop_the_run(self):
        """Наблюдение — не работа (правило доски): любой сбой автооткрытия
        обязан остаться диагностикой, а не остановить прогон."""
        cli.sys.platform = "darwin"
        orig_run = subprocess.run

        def boom(argv, **kw):
            if argv and argv[0] == "open":
                raise OSError("open недоступен в этой песочнице")
            return orig_run(argv, **kw)

        subprocess.run = boom
        self.addCleanup(lambda: setattr(subprocess, "run", orig_run))
        self.fake_loop({"aaaa": "done", "bbbb": "done"})
        code, _out = run_cli("--root", str(self.root), "run")
        self.assertEqual(code, 0)


class TestNextStepAfterCrash(CliCase):
    """_print_next не знал про оборванную работу.

    После аварии сводка либо молчала, либо звала «работа закончена —
    остался просмотр глазами» — прямо под блоком НЕЗАВЕРШЁННЫЕ ШАГИ,
    который печатался строкой выше и звал в `resume`.
    """

    def test_in_progress_recommends_resume(self):
        self.state.set_status("aaaa", "in_progress")
        self.state.set_status("bbbb", "done")
        _, out = run_cli("--root", str(self.root), "status")
        tail = out.split("дальше:")[1]
        self.assertIn("resume", tail)
        self.assertNotIn("просмотр глазами", tail)

    def test_in_review_recommends_resume(self):
        self.state.set_status("aaaa", "in_review")
        self.state.set_status("bbbb", "done")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("resume", out.split("дальше:")[1])

    def test_unfinished_step_outranks_done_review(self):
        for tid in ("aaaa", "bbbb"):
            self.state.set_status(tid, "done")
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        _, out = run_cli("--root", str(self.root), "status")
        tail = out.split("дальше:")[1]
        self.assertIn("resume", tail)
        self.assertNotIn("просмотр глазами", tail)


class TestStatusSpeaksHuman(CliCase):
    """status печатал сырые коды состояния и причины.

    «invalid_verdict» — буквальный антипример из комментария к словарю
    причин в vocab.py: человек читает его в момент, когда прогон уже
    встал, и код ему в этот момент не помогает.
    """

    def test_reason_is_translated(self):
        self.state.set_status("aaaa", "blocked", reason="invalid_verdict")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("вердикт не разобран", out)
        self.assertNotIn("invalid_verdict", out)

    def test_status_bucket_is_translated(self):
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("в очереди (2)", out)
        self.assertNotIn("pending (", out)


class TestStatusSurvivesHandEditedQueue(CliCase):
    """Правленный руками tasks.json ронял status голой трассировкой.

    Доска любое содержимое `.swarm/` переживает — сводка падала на
    первом же t["status"]. Запись не той формы уходит в «прочее», а не
    в никуда: иначе задача с опечаткой в статусе числится в «задач: N»,
    но не видна нигде.
    """

    def test_broken_rows_degrade_not_crash(self):
        raw = {"goal": "тестовая цель", "tasks": [
            {"id": "aaaa", "title": "первая", "status": "pending"},
            {"id": "xxxx", "title": "опечатка", "status": "half-done"},
            {"id": "yyyy", "status": "pending"},
            {"title": "без id и статуса"},
            "просто строка",
        ]}
        self.state.tasks_path.write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        code, out = run_cli("--root", str(self.root), "status")
        self.assertEqual(code, 0, out)
        self.assertIn("задач: 5", out)
        self.assertIn("yyyy", out, "задача без title видна в своей группе")
        self.assertIn("прочее", out)
        self.assertIn("xxxx", out)
        self.assertIn("half-done", out, "нелегальный статус назван, не спрятан")
        self.assertIn("просто строка", out, "неразобранная запись показана")


class TestPolicyCli(CliCase):
    def test_add_without_text_is_refused(self):
        """nargs="?" с умолчанием "" пропускал пустую политику: pid
        занят, в журнале запись, а решения в ней нет."""
        code, out = run_cli("--root", str(self.root), "policy", "add",
                            "--match", "release")
        self.assertEqual(code, 2)
        self.assertIn("текст", out)

    def test_string_match_is_not_scattered_into_letters(self):
        """`", ".join` строку рассыпает в буквы: match="release" из
        старого журнала печатался как «r, e, l, e, a, s, e»."""
        self.state.log("policy", pid="p001", text="release notes не трогаем",
                       match="release", goal="тестовая цель")
        code, out = run_cli("--root", str(self.root), "policy", "list")
        self.assertEqual(code, 0, out)
        self.assertIn("совпадение по: release", out)
        self.assertNotIn("r, e, l", out)


class TestReportRunLevelBlock(CliCase):
    """Блок «прогон в целом» — редкие события, а не бухгалтерия."""

    def test_bookkeeping_does_not_bury_run_events(self):
        """state_written сопровождает КАЖДУЮ запись состояния и в блоке
        прогона хоронил под собой редкие события — бюджет, план. Доска
        фильтрует его через BOOKKEEPING_KINDS — отчёт обязан так же;
        сырьё через --json остаётся полным."""
        self.state.log("budget_exhausted", spent=51, budget=50,
                       stopped_before="aaaa")
        _, out = run_cli("--root", str(self.root), "report")
        self.assertIn("бюджет прогона исчерпан", out)
        self.assertNotIn("состояние записано", out)
        _, raw = run_cli("--root", str(self.root), "report", "--json")
        self.assertIn("state_written", raw, "сырьё не фильтруется")

    def test_plan_failed_lands_in_run_block(self):
        """Фильтр блока открытый — «запись без задачи», не список видов:
        закрытый перечень молча терял plan_failed (как когда-то доска)."""
        self.state.log("plan_failed", mode="plan", reason="invalid",
                       errors=["схема не прошла"])
        _, out = run_cli("--root", str(self.root), "report")
        self.assertIn("прогон в целом", out)
        self.assertIn("планирование не удалось", out)


if __name__ == "__main__":
    unittest.main()
