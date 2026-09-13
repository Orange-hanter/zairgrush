#!/usr/bin/env python3
"""Выбор движка исполнителя: правило именования, командная строка, разбор.

Роль исполнителя была прибита к одному CLI, и это стоило петле
единственной точки отказа: подписка провайдера кончалась — стоял весь
рой. Здесь проверяется ровно то, что от выбора движка требуется:
сегодняшняя форма вызова kimi не сдвинулась ни на байт (иначе замеренные
плечи E8/E10 сравнивать не с чем), опечатка не превращается в умолчание,
а конверт claude читается двумя каналами, а не одним.
"""

import ast
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


def _p(text):
    """Промпт в упаковке канала доставки (NXT-006): сборщики argv
    принимают PromptDelivery, а не голую строку — в argv сам промпт
    не едет (ARG_MAX), здесь он подставлен в слот напрямую, потому что
    тестам важен скелет argv, а не канал."""
    return eng.PromptDelivery((text,), None)


class TestResolve(unittest.TestCase):
    """Правило одно: префикс старше ключа, ключ старше умолчания."""

    def test_default_is_kimi(self):
        """Умолчание кода не менялось: стенд, ничего не настроивший,
        обязан получить ровно вчерашнее поведение."""
        self.assertEqual(eng.resolve({}), ("kimi", ""))

    def test_bare_model_keeps_todays_meaning(self):
        self.assertEqual(
            eng.resolve({"executor_model": "kimi-k2"}), ("kimi", "kimi-k2")
        )

    def test_engine_key_selects_engine(self):
        self.assertEqual(
            eng.resolve({"executor_engine": "claude", "executor_model": "sonnet"}),
            ("claude", "sonnet"),
        )

    def test_zcode_engine_key(self):
        self.assertEqual(
            eng.resolve({"executor_engine": "zcode", "executor_model": "glm-5.3"}),
            ("zcode", "glm-5.3"),
        )

    def test_zcode_prefix_beats_the_key(self):
        self.assertEqual(
            eng.resolve(
                {"executor_engine": "kimi"}, {"executor_model": "zcode:glm-5.3"}
            ),
            ("zcode", "glm-5.3"),
        )

    def test_prefix_beats_the_key(self):
        """План-дифф пришпиливает ОДНУ задачу к другому движку, не трогая
        настройку прогона: иначе выбор движка на задачу требовал бы правки
        конфига между задачами очереди."""
        self.assertEqual(
            eng.resolve(
                {"executor_engine": "kimi"}, {"executor_model": "claude:haiku"}
            ),
            ("claude", "haiku"),
        )

    def test_task_field_is_read_only_when_passed(self):
        """E10 за флагом: без разрешения читать поле задачи поведение
        обязано остаться прежним, а не «прежним с оговоркой»."""
        task = {"executor_model": "claude:sonnet"}
        self.assertEqual(eng.resolve({"executor_model": "k3"}, None), ("kimi", "k3"))
        self.assertEqual(
            eng.resolve({"executor_model": "k3"}, task), ("claude", "sonnet")
        )

    def test_model_name_may_contain_a_colon_after_the_prefix(self):
        self.assertEqual(
            eng.resolve({"executor_model": "ollama:qwen3:32b"}), ("ollama", "qwen3:32b")
        )

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
        собираться сегодня той же строкой, включая порядок `-m` до `-p`.
        Заморожен скелет, а не содержимое слота промпта: с NXT-006 там
        указатель канала доставки, а не сам промпт (в тесте — токен из
        `_p`, подставленный в слот как есть)."""
        self.assertEqual(
            eng.executor_argv("kimi", "k3", _p("PROMPT"), {}),
            ["kimi", "-m", "k3", "-p", "PROMPT", "--output-format", "stream-json"],
        )
        self.assertEqual(
            eng.executor_argv("kimi", "", _p("PROMPT"), {}),
            ["kimi", "-p", "PROMPT", "--output-format", "stream-json"],
        )

    def test_kimi_ignores_claude_only_settings(self):
        """Флаги движка claude не имеют права протечь в чужой CLI: kimi
        принял бы их за неизвестные опции и умер бы на старте."""
        argv = eng.executor_argv(
            "kimi", "k3", _p("P"), {"executor_effort": "high", "executor_budget_usd": 3}
        )
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--max-budget-usd", argv)

    def test_claude_carries_permission_and_schema(self):
        argv = eng.executor_argv("claude", "sonnet", _p("P"), {}, schema="{}")
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("-p") + 1], "P")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--json-schema") + 1], "{}")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertIn("--strict-mcp-config", argv)

    def test_claude_executor_may_write_and_run(self):
        """Ревьюер живёт на read-only наборе, исполнитель — нет: без Edit
        он не сделает работу, без Bash не заполнит evidence.tests."""
        argv = eng.executor_argv("claude", "", _p("P"), {})
        allowed = argv[argv.index("--allowedTools") + 1]
        for tool in ("Edit", "Write", "Bash", "Read"):
            self.assertIn(tool, allowed)

    def test_git_write_is_a_rule_not_a_sentence(self):
        """Промпт говорит «git только для чтения» с самого начала, но до
        сих пор это была фраза. Отказ обязан приходить ДО выполнения."""
        argv = eng.executor_argv("claude", "", _p("P"), {})
        denied = argv[argv.index("--disallowedTools") + 1]
        for cmd in (
            "git commit",
            "git push",
            "git reset",
            "git rebase",
            "git checkout",
            "git stash",
            "git config",
            "git merge",
        ):
            self.assertIn(f"Bash({cmd}:*)", denied)

    def test_git_read_stays_allowed(self):
        """Исполнителю нужен `git diff`/`git log`: запрет на запись — не
        запрет на чтение, иначе он не увидит собственную работу."""
        denied = eng.executor_argv("claude", "", _p("P"), {})[
            eng.executor_argv("claude", "", _p("P"), {}).index("--disallowedTools") + 1
        ]
        self.assertNotIn("Bash(git diff", denied)
        self.assertNotIn("Bash(git log", denied)
        self.assertNotIn("Bash(git status", denied)

    def test_read_only_refs_stay_allowed_for_claude(self):
        """Граница списка claude — префиксная неоднозначность, а не
        мягкость к разрушению: `git branch`/`git tag` у него открыты,
        потому что паттерн `Bash(git branch:*)` не отличит read-only
        `git branch -l` от `git branch -D`, а инспектировать refs
        исполнителю нужно. clean/rm/revert/cherry-pick read-only форм
        не имеют — они закрыты у claude наравне с zcode."""
        denied = eng.executor_argv("claude", "", _p("P"), {})[
            eng.executor_argv("claude", "", _p("P"), {}).index("--disallowedTools") + 1
        ]
        entries = _csv_entries(denied)
        for entry in (
            "Bash(git clean:*)",
            "Bash(git rm:*)",
            "Bash(git revert:*)",
            "Bash(git cherry-pick:*)",
        ):
            self.assertIn(entry, entries)
        for entry in ("Bash(git branch:*)", "Bash(git tag:*)"):
            self.assertNotIn(entry, entries)
        # Широкий список (ветки/теги/-C/--git-dir) живёт только у zcode
        # под yolo — см. engines.py.
        zargv = eng.executor_argv("zcode", "m", _p("P"), {}, cwd="/w")
        zdenied = zargv[zargv.index("--disallowed-tools") + 1]
        for entry in ("Bash(git branch:*)", "Bash(git tag:*)",
                      "Bash(git -C:*)", "Bash(git --git-dir:*)"):
            self.assertIn(entry, _csv_entries(zdenied))

    def test_tuning_flags_only_when_configured(self):
        """То же правило, что у promptbuilder.tuning: без явной настройки
        роль наследует сессионные параметры и поведение не меняется."""
        bare = eng.executor_argv("claude", "", _p("P"), {})
        self.assertNotIn("--effort", bare)
        self.assertNotIn("--max-budget-usd", bare)
        tuned = eng.executor_argv(
            "claude", "", _p("P"),
            {"executor_effort": "low", "executor_budget_usd": 2.5}
        )
        self.assertEqual(tuned[tuned.index("--effort") + 1], "low")
        self.assertEqual(tuned[tuned.index("--max-budget-usd") + 1], "2.5")

    def test_schema_is_optional(self):
        self.assertNotIn("--json-schema", eng.executor_argv("claude", "", _p("P"), {}))

    def test_ollama_has_no_command_line(self):
        """`ollama:` — маршрут на chat-fill, а не CLI: дошедшее сюда
        значило бы, что маршрутизация в implement() сломана."""
        with self.assertRaises(eng.EngineError):
            eng.executor_argv("ollama", "m", _p("P"), {})


class TestReportFromEnvelope(unittest.TestCase):
    """Два канала отчёта, и порядок между ними важен."""

    def test_structured_output_wins(self):
        env = {
            "structured_output": {"status": "done", "summary": "с"},
            "result": '{"status": "dispute", "summary": "т"}',
        }
        self.assertEqual(eng.report_from_envelope(env)["status"], "done")

    def test_text_tail_is_the_fallback(self):
        """Промпт требует финальный JSON и без схемы: терять готовую
        работу из-за пустого структурного канала незачем."""
        env = {"result": 'готово. {"status": "done", "summary": "с"}'}
        self.assertEqual(eng.report_from_envelope(env)["status"], "done")

    def test_structured_output_without_status_is_not_a_report(self):
        env = {
            "structured_output": {"summary": "нет статуса"},
            "result": "текста тоже нет",
        }
        self.assertIsNone(eng.report_from_envelope(env))

    def test_empty_require_value_rejected_in_every_channel(self):
        """Паритет трёх каналов: пустая строка в require-поле — не отчёт
        нигде (как в report_from_zcode). Раньше structured_output её
        отбрасывал, а result и текст-хвост пропускали — один конверт
        мог быть отчётом или нет в зависимости от того, каким каналом
        пришёл. Пустой список (честный unclear=[] пуриста) валиден."""
        self.assertIsNone(
            eng.report_from_envelope({"structured_output": {"status": ""}})
        )
        self.assertIsNone(eng.report_from_envelope({"result": {"status": ""}}))
        self.assertIsNone(
            eng.report_from_envelope({"result": 'готово. {"status": ""}'})
        )
        # zcode-разборщик держит те же правила — паритет между функциями.
        self.assertIsNone(eng.report_from_zcode({"status": ""}))
        # Пустой список — валидный ответ пуриста во всех каналах.
        self.assertEqual(
            eng.report_from_envelope(
                {"structured_output": {"unclear": []}}, "unclear"
            ),
            {"unclear": []},
        )
        self.assertEqual(
            eng.report_from_envelope({"result": {"unclear": []}}, "unclear"),
            {"unclear": []},
        )
        self.assertEqual(
            eng.report_from_zcode({"unclear": []}, "unclear"), {"unclear": []}
        )

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
        facts = eng.envelope_facts(
            {
                "total_cost_usd": 0.055,
                "terminal_reason": "completed",
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 492,
                    "cache_read_input_tokens": 93827,
                    "cache_creation_input_tokens": 7968,
                },
            }
        )
        self.assertEqual(facts["cost_usd"], 0.055)
        self.assertEqual(facts["tokens_out"], 492)
        self.assertEqual(facts["cache_read"], 93827)
        self.assertEqual(facts["terminal_reason"], "completed")

    def test_denials_are_counted(self):
        facts = eng.envelope_facts(
            {
                "permission_denials": [
                    {"tool_name": "Bash", "tool_input": {"command": "git commit -am x"}}
                ]
            }
        )
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
        out = eng.denied_commands(
            {
                "permission_denials": [
                    {"tool_name": "Bash", "tool_input": {"command": "git push"}}
                ]
            }
        )
        self.assertEqual(out, ["Bash: git push"])

    def test_garbage_rows_are_skipped_not_fatal(self):
        out = eng.denied_commands(
            {
                "permission_denials": [
                    "строка",
                    {"tool_name": "Write", "tool_input": None},
                ]
            }
        )
        self.assertEqual(out, ["Write: "])

    def test_no_envelope(self):
        self.assertEqual(eng.denied_commands(None), [])


class TestRunKind(unittest.TestCase):
    def test_known_engines(self):
        self.assertEqual(eng.run_kind("kimi"), "kimi")
        self.assertEqual(eng.run_kind("claude"), "claude")
        self.assertEqual(eng.run_kind("zcode"), "zcode")
        self.assertEqual(eng.run_kind("ollama"), "fill")

    def test_unknown_is_refused(self):
        with self.assertRaises(eng.EngineError):
            eng.run_kind("claudee")


def _csv_entries(value: object) -> list[str]:
    """Разбор CSV-флага argv в нормализованные записи.

    Контракт engines.py — строка через запятую; тесты сверяют по
    элементам, а не подстрокам, и нормализуют записи, чтобы смена
    разделителя или лишний пробел не давали ложного «не найдено».
    """
    if isinstance(value, str):
        return [e.strip() for e in value.split(",") if e.strip()]
    return [str(e).strip() for e in value]


class TestZcodeArgv(unittest.TestCase):
    def _argv(self, model="glm-5.3", cwd="/work"):
        return eng.executor_argv("zcode", model, _p("P"), {}, cwd=cwd)

    def test_prompt_json_mode_and_surface(self):
        argv = self._argv()
        self.assertEqual(argv[argv.index("--prompt") + 1], "P")
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "yolo")
        self.assertEqual(argv[argv.index("--surface") + 1], "terminal")
        self.assertEqual(argv[argv.index("--cwd") + 1], "/work")

    def test_model_is_not_on_argv(self):
        """CLI 0.16 не имеет --model: положить хвост в argv — соврать."""
        argv = self._argv("glm-5.3")
        self.assertNotIn("--model", argv)
        # Регрессионный якорь рядом с отсутствием флага: для сегодняшнего
        # zcode модель могла уйти в argv только через --model, поэтому
        # проверка избыточна, но дёшево страхует от появления второго
        # способа передать имя.
        self.assertNotIn("glm-5.3", argv)

    def test_git_write_is_denied_not_all_git(self):
        denied = self._argv()[self._argv().index("--disallowed-tools") + 1]
        for cmd in (
            "git commit",
            "git push",
            "git reset",
            "git rebase",
            "git checkout",
            "git stash",
            "git config",
            "git merge",
            # История и дерево целиком: yolo снимает подтверждения,
            # deny-list остаётся единственной преградой (см. engines.py).
            "git revert",
            "git cherry-pick",
            "git clean",
            "git rm",
            "git branch",
            "git tag",
            # Обход префиксов `git -C <dir> …` / `git --git-dir=… …`:
            # ведущие формы закрыты, инлайн-остаток — известная дыра
            # языка префиксных паттернов (см. комментарий в engines.py).
            "git -C",
            "git --git-dir",
        ):
            self.assertIn(f"Bash({cmd}:*)", _csv_entries(denied))
        # blanket-ban по префиксу `git` был бы шире нужного (read-only
        # команды исполнителю нужны) — синтаксис тот же colon-формат.
        self.assertNotIn("Bash(git:*)", _csv_entries(denied))

    def test_executor_may_write_and_run(self):
        allowed = self._argv()[self._argv().index("--allowed-tools") + 1]
        # Поэлементная сверка: подстрочный поиск по строке молча нашёл бы
        # "Edit" внутри "MultiEdit", "Bash" — внутри "BashOutput".
        allowed_set = set(_csv_entries(allowed))
        for tool in ("Edit", "Write", "Bash", "Read"):
            self.assertIn(tool, allowed_set)

    def test_cwd_omitted_when_empty(self):
        argv = eng.executor_argv("zcode", "", _p("P"), {})
        self.assertNotIn("--cwd", argv)

    def test_claude_flags_do_not_leak(self):
        argv = self._argv()
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--json-schema", argv)
        self.assertNotIn("--strict-mcp-config", argv)


class TestZcodeEnvelope(unittest.TestCase):
    def test_compact_object(self):
        env = eng.extract_zcode_envelope(
            '{"sessionId": "s", "response": "{\\"status\\": \\"done\\"}"}'
        )
        self.assertEqual(env["sessionId"], "s")

    def test_pretty_print(self):
        blob = '{\n  "sessionId": "s",\n  "response": "ok"\n}\n'
        self.assertEqual(eng.extract_zcode_envelope(blob)["response"], "ok")

    def test_banner_around_object(self):
        env = eng.extract_zcode_envelope('hi\n{"response": "x"}\n')
        self.assertEqual(env["response"], "x")

    def test_trailing_output_is_tolerated(self):
        """Статусные строки после JSON не имеют права прятать конверт."""
        env = eng.extract_zcode_envelope('{"response": "x"}\nDone in 5s\n')
        self.assertEqual(env["response"], "x")

    def test_wrapped_object_is_not_an_envelope(self):
        """Массив вокруг объекта — чужое значение, не конверт `--json`."""
        self.assertIsNone(eng.extract_zcode_envelope('[{"response": "x"}]'))

    def test_object_mid_array_with_trailing_comma_is_not_an_envelope(self):
        """Запятая сразу после объекта — он элемент массива, а не
        конверт верхнего уровня."""
        self.assertIsNone(eng.extract_zcode_envelope('[{"response": "x"}, 42]'))

    def test_garbage(self):
        self.assertIsNone(eng.extract_zcode_envelope(""))
        self.assertIsNone(eng.extract_zcode_envelope("not json"))

    def test_report_from_response_text(self):
        env = {"response": 'готово. {"status": "done", "summary": "с"}'}
        self.assertEqual(eng.report_from_zcode(env)["status"], "done")

    def test_unclear_field_is_the_contract(self):
        env = {"response": '{"unclear": [{"question": "q"}], "summary": "s"}'}
        got = eng.report_from_zcode(env, "unclear")
        self.assertEqual(got["unclear"][0]["question"], "q")

    def test_outer_object_wins_over_nested_with_same_key(self):
        """Вложенный объект с тем же полем — не отчёт: при равной форме
        отчётом считается контейнер, а не его кусок."""
        env = {"response": '{"unclear": [{"question": "внеш"}], '
                          '"nested": {"unclear": [{"question": "внутр"}]}}'}
        got = eng.report_from_zcode(env, "unclear")
        self.assertEqual(got["unclear"][0]["question"], "внеш")

    def test_rightmost_of_separate_objects_still_wins(self):
        """Отдельные объекты в прозе — прежняя семантика: правый."""
        env = {"response": 'a {"unclear": [{"question": "лев"}]} b '
                          '{"unclear": [{"question": "прав"}]}'}
        got = eng.report_from_zcode(env, "unclear")
        self.assertEqual(got["unclear"][0]["question"], "прав")

    def test_no_envelope(self):
        self.assertIsNone(eng.report_from_zcode(None))


class TestZcodeFacts(unittest.TestCase):
    def test_camel_case_usage_without_invented_price(self):
        facts = eng.zcode_facts(
            {"usage": {"inputTokens": 8, "outputTokens": 492, "cacheReadTokens": 12}}
        )
        self.assertEqual(facts["tokens_in"], 8)
        self.assertEqual(facts["tokens_out"], 492)
        self.assertEqual(facts["cache_read"], 12)
        self.assertNotIn("cost_usd", facts)

    def test_missing_fields_are_absent(self):
        self.assertEqual(eng.zcode_facts({}), {})
        self.assertEqual(eng.zcode_facts(None), {})


class TestZcodeCmd(unittest.TestCase):
    def test_path_wins(self):
        # Патчим seam в engines.py, а не глобальный shutil процесса: при
        # `from shutil import which` внутри модуля этот тест продолжил бы
        # работать — он держится за точку входа, а не за форму импорта.
        orig = getattr(eng, "which", None)
        if orig is None:
            self.fail(
                "seam engines.which отсутствует — zcode_cmd больше не "
                "через него, обнови тест под новую форму импорта"
            )

        def fake(name):
            return "/opt/zcode" if name == "zcode" else orig(name)

        eng.which = fake
        self.addCleanup(lambda: setattr(eng, "which", orig))
        self.assertEqual(eng.zcode_cmd(), ["/opt/zcode"])


class TestMutationAnchorsStayValid(unittest.TestCase):
    """Мутационный аудит привязан к точному тексту исходников: якорь,
    которого файл больше не содержит, выпадает из аудита, а мутант,
    не собирающийся обратно в валидный код, «ловится» тестами ложно
    (pytest падает на SyntaxError — не защитой, а мусором). Гейт обязан
    заметить рассинхрон сам — поэтому каждая мутация проверяется обычным
    тестом: якорь существует ровно один раз, и splice парсится. Связность
    посимвольная намеренно: якорь — это кусок кода, который мутация
    правит."""

    def _mutate_module(self):
        # mutate.py лежит на уровень выше swarm/, поэтому грузим его
        # явно по пути файла, а не `import mutate`: последний молча
        # зависит от того, что tools/swarm оказался в sys.path.
        mutate_path = pathlib.Path(__file__).resolve().parent.parent / "mutate.py"
        spec = importlib.util.spec_from_file_location("mutate", mutate_path)
        mutate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mutate)
        return mutate

    def test_anchors_exist_exactly_once_and_splice(self):
        mutate = self._mutate_module()
        for mutation in mutate.MUTATIONS:
            if len(mutation) != 5:
                self.fail(
                    f"мутация изменила арность ({len(mutation)}): {mutation!r} — "
                    "тест сверяет поля по позиции, обнови его под новый формат"
                )
            name, rel, old, new, _catcher = mutation
            src = (mutate.SW / rel).read_text(encoding="utf-8")
            self.assertEqual(
                src.count(old),
                1,
                f"якорь мутации «{name}» ({rel}) не найден или неоднозначен",
            )
            spliced = src.replace(old, new)
            try:
                ast.parse(spliced)
            except SyntaxError as e:
                self.fail(
                    f"мутант «{name}» ({rel}) не собирается обратно в "
                    f"валидный код: {e} — аудит ловил бы его мусором"
                )


class TestTwoListsOfEnginesAgree(unittest.TestCase):
    """cli грузится раньше плоских модулей петли, поэтому список движков
    продублирован строкой. Дубликат без стража — будущее расхождение."""

    def test_cli_knows_the_same_engines(self):
        cli = _load("cli")
        self.assertEqual(cli.EXECUTOR_ENGINES, eng.ENGINES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
