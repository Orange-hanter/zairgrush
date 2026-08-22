#!/usr/bin/env python3
"""Выбор движка исполнителя: правило именования, командная строка, разбор.

Роль исполнителя была прибита к одному CLI, и это стоило петле
единственной точки отказа: подписка провайдера кончалась — стоял весь
рой. Здесь проверяется ровно то, что от выбора движка требуется:
сегодняшняя форма вызова kimi не сдвинулась ни на байт (иначе замеренные
плечи E8/E10 сравнивать не с чем), опечатка не превращается в умолчание,
а конверт claude читается двумя каналами, а не одним.
"""
import importlib.util
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


eng = _load("engines")


class TestResolve(unittest.TestCase):
    """Правило одно: префикс старше ключа, ключ старше умолчания."""

    def test_default_is_kimi(self):
        """Умолчание кода не менялось: стенд, ничего не настроивший,
        обязан получить ровно вчерашнее поведение."""
        self.assertEqual(eng.resolve({}), ("kimi", ""))

    def test_bare_model_keeps_todays_meaning(self):
        self.assertEqual(eng.resolve({"executor_model": "kimi-k2"}),
                         ("kimi", "kimi-k2"))

    def test_engine_key_selects_engine(self):
        self.assertEqual(
            eng.resolve({"executor_engine": "claude",
                         "executor_model": "sonnet"}),
            ("claude", "sonnet"))

    def test_prefix_beats_the_key(self):
        """План-дифф пришпиливает ОДНУ задачу к другому движку, не трогая
        настройку прогона: иначе выбор движка на задачу требовал бы правки
        конфига между задачами очереди."""
        self.assertEqual(
            eng.resolve({"executor_engine": "kimi"},
                        {"executor_model": "claude:haiku"}),
            ("claude", "haiku"))

    def test_task_field_is_read_only_when_passed(self):
        """E10 за флагом: без разрешения читать поле задачи поведение
        обязано остаться прежним, а не «прежним с оговоркой»."""
        task = {"executor_model": "claude:sonnet"}
        self.assertEqual(eng.resolve({"executor_model": "k3"}, None),
                         ("kimi", "k3"))
        self.assertEqual(eng.resolve({"executor_model": "k3"}, task),
                         ("claude", "sonnet"))

    def test_model_name_may_contain_a_colon_after_the_prefix(self):
        self.assertEqual(eng.resolve({"executor_model": "ollama:qwen3:32b"}),
                         ("ollama", "qwen3:32b"))

    def test_unknown_prefix_is_refused_not_defaulted(self):
        """Молча выбранное умолчание — тот же отказ, что дал «ноль вызовов
        эмбеддера за пилот». Здесь он дороже: работа ушла бы не тому
        агенту, которого выбрал оператор."""
        with self.assertRaises(eng.EngineError) as cm:
            eng.resolve({"executor_model": "anthropic:sonnet"})
        self.assertIn("anthropic", str(cm.exception))
        for name in sorted(eng.ENGINES):
            self.assertIn(name, str(cm.exception))

    def test_unknown_engine_key_is_refused(self):
        with self.assertRaises(eng.EngineError):
            eng.resolve({"executor_engine": "claudee"})

    def test_empty_model_is_not_a_prefix_error(self):
        self.assertEqual(eng.resolve({"executor_model": ""}), ("kimi", ""))


class TestArgv(unittest.TestCase):
    def test_kimi_form_is_frozen(self):
        """Регрессионный якорь: всё, что измерено на плече A, обязано
        собираться сегодня той же строкой, включая порядок `-m` до `-p`."""
        self.assertEqual(
            eng.executor_argv("kimi", "k3", "PROMPT", {}),
            ["kimi", "-m", "k3", "-p", "PROMPT",
             "--output-format", "stream-json"])
        self.assertEqual(
            eng.executor_argv("kimi", "", "PROMPT", {}),
            ["kimi", "-p", "PROMPT", "--output-format", "stream-json"])

    def test_kimi_ignores_claude_only_settings(self):
        """Флаги движка claude не имеют права протечь в чужой CLI: kimi
        принял бы их за неизвестные опции и умер бы на старте."""
        argv = eng.executor_argv("kimi", "k3", "P",
                                 {"executor_effort": "high",
                                  "executor_budget_usd": 3})
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--max-budget-usd", argv)

    def test_claude_carries_permission_and_schema(self):
        argv = eng.executor_argv("claude", "sonnet", "P", {}, schema="{}")
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("-p") + 1], "P")
        self.assertEqual(argv[argv.index("--permission-mode") + 1],
                         "acceptEdits")
        self.assertEqual(argv[argv.index("--json-schema") + 1], "{}")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertIn("--strict-mcp-config", argv)

    def test_claude_executor_may_write_and_run(self):
        """Ревьюер живёт на read-only наборе, исполнитель — нет: без Edit
        он не сделает работу, без Bash не заполнит evidence.tests."""
        argv = eng.executor_argv("claude", "", "P", {})
        allowed = argv[argv.index("--allowedTools") + 1]
        for tool in ("Edit", "Write", "Bash", "Read"):
            self.assertIn(tool, allowed)

    def test_git_write_is_a_rule_not_a_sentence(self):
        """Промпт говорит «git только для чтения» с самого начала, но до
        сих пор это была фраза. Отказ обязан приходить ДО выполнения."""
        argv = eng.executor_argv("claude", "", "P", {})
        denied = argv[argv.index("--disallowedTools") + 1]
        for cmd in ("git commit", "git push", "git reset", "git rebase",
                    "git checkout", "git stash", "git config", "git merge"):
            self.assertIn(f"Bash({cmd}:*)", denied)

    def test_git_read_stays_allowed(self):
        """Исполнителю нужен `git diff`/`git log`: запрет на запись — не
        запрет на чтение, иначе он не увидит собственную работу."""
        denied = eng.executor_argv("claude", "", "P", {})[
            eng.executor_argv("claude", "", "P", {}).index(
                "--disallowedTools") + 1]
        self.assertNotIn("Bash(git diff", denied)
        self.assertNotIn("Bash(git log", denied)
        self.assertNotIn("Bash(git status", denied)

    def test_tuning_flags_only_when_configured(self):
        """То же правило, что у promptbuilder.tuning: без явной настройки
        роль наследует сессионные параметры и поведение не меняется."""
        bare = eng.executor_argv("claude", "", "P", {})
        self.assertNotIn("--effort", bare)
        self.assertNotIn("--max-budget-usd", bare)
        tuned = eng.executor_argv("claude", "", "P",
                                  {"executor_effort": "low",
                                   "executor_budget_usd": 2.5})
        self.assertEqual(tuned[tuned.index("--effort") + 1], "low")
        self.assertEqual(tuned[tuned.index("--max-budget-usd") + 1], "2.5")

    def test_schema_is_optional(self):
        self.assertNotIn("--json-schema",
                         eng.executor_argv("claude", "", "P", {}))

    def test_ollama_has_no_command_line(self):
        """`ollama:` — маршрут на chat-fill, а не CLI: дошедшее сюда
        значило бы, что маршрутизация в implement() сломана."""
        with self.assertRaises(eng.EngineError):
            eng.executor_argv("ollama", "m", "P", {})


class TestReportFromEnvelope(unittest.TestCase):
    """Два канала отчёта, и порядок между ними важен."""

    def test_structured_output_wins(self):
        env = {"structured_output": {"status": "done", "summary": "с"},
               "result": '{"status": "dispute", "summary": "т"}'}
        self.assertEqual(eng.report_from_envelope(env)["status"], "done")

    def test_text_tail_is_the_fallback(self):
        """Промпт требует финальный JSON и без схемы: терять готовую
        работу из-за пустого структурного канала незачем."""
        env = {"result": 'готово. {"status": "done", "summary": "с"}'}
        self.assertEqual(eng.report_from_envelope(env)["status"], "done")

    def test_structured_output_without_status_is_not_a_report(self):
        env = {"structured_output": {"summary": "нет статуса"},
               "result": "текста тоже нет"}
        self.assertIsNone(eng.report_from_envelope(env))

    def test_no_envelope_no_report(self):
        self.assertIsNone(eng.report_from_envelope(None))
        self.assertIsNone(eng.report_from_envelope("строка"))

    def test_result_may_arrive_already_parsed(self):
        env = {"result": {"status": "done", "summary": "с"}}
        self.assertEqual(eng.report_from_envelope(env)["status"], "done")


class TestEnvelopeFacts(unittest.TestCase):
    def test_missing_fields_are_none_not_zero(self):
        """Журнал читается как данные (§9.3): отсутствующая цена — это
        НЕ нулевая цена, и подменять одно другим значит врать бюджету."""
        facts = eng.envelope_facts({})
        self.assertIsNone(facts["cost_usd"])
        self.assertIsNone(facts["tokens_in"])

    def test_cost_and_tokens_are_taken(self):
        facts = eng.envelope_facts({
            "total_cost_usd": 0.055, "terminal_reason": "completed",
            "usage": {"input_tokens": 8, "output_tokens": 492,
                      "cache_read_input_tokens": 93827,
                      "cache_creation_input_tokens": 7968}})
        self.assertEqual(facts["cost_usd"], 0.055)
        self.assertEqual(facts["tokens_out"], 492)
        self.assertEqual(facts["cache_read"], 93827)
        self.assertEqual(facts["terminal_reason"], "completed")

    def test_denials_are_counted(self):
        facts = eng.envelope_facts({"permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "git commit -am x"}}]})
        self.assertEqual(facts["denied"], 1)
        self.assertEqual(facts["denied_tools"], ["Bash"])

    def test_no_denials_no_field(self):
        """Поле, которого нет, лучше поля со значением 0: `denied=0` в
        каждой строке журнала — шум, `denied` в редкой — факт."""
        self.assertNotIn("denied", eng.envelope_facts({"usage": {}}))

    def test_broken_usage_does_not_crash(self):
        self.assertIsNone(eng.envelope_facts({"usage": "строка"})["tokens_in"])


class TestDeniedCommands(unittest.TestCase):
    def test_command_is_named(self):
        out = eng.denied_commands({"permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "git push"}}]})
        self.assertEqual(out, ["Bash: git push"])

    def test_garbage_rows_are_skipped_not_fatal(self):
        out = eng.denied_commands({"permission_denials": [
            "строка", {"tool_name": "Write", "tool_input": None}]})
        self.assertEqual(out, ["Write: "])

    def test_no_envelope(self):
        self.assertEqual(eng.denied_commands(None), [])


class TestTwoListsOfEnginesAgree(unittest.TestCase):
    """cli грузится раньше плоских модулей петли, поэтому список движков
    продублирован строкой. Дубликат без стража — будущее расхождение."""

    def test_cli_knows_the_same_engines(self):
        cli = _load("cli")
        self.assertEqual(cli.EXECUTOR_ENGINES, eng.ENGINES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
