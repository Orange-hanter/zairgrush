#!/usr/bin/env python3
"""Golden-тесты verification-запросов ревьюера.

Здесь исполняются команды, пришедшие от LLM, — значит whitelist это
единственная граница безопасности, и проверять её надо злее обычного.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
V = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("verify", ROOT_DIR / "verify.py")
vf = importlib.util.module_from_spec(spec)
sys.modules["verify"] = vf
spec.loader.exec_module(vf)


class TestWhitelistAccepts(unittest.TestCase):
    def test_run_tests(self):
        self.assertEqual(vf.build({"kind": "run_tests", "arg": "tests.test_roman"}),
                         ["python3", "-m", "unittest", "tests.test_roman"])

    def test_run_all_tests_ignores_arg(self):
        self.assertEqual(vf.build({"kind": "run_all_tests"}),
                         ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."])

    def test_git_show(self):
        self.assertEqual(vf.build({"kind": "git_show", "arg": "HEAD~2"}),
                         ["git", "show", "--stat", "HEAD~2"])

    def test_git_log_with_path(self):
        self.assertIn("wordstat/rank.py",
                      vf.build({"kind": "git_log", "arg": "wordstat/rank.py"}))

    def test_python_snippet(self):
        argv = vf.build({"kind": "python", "arg": "print(sum(range(5)))"})
        self.assertEqual(argv[:2], ["python3", "-c"])


class TestWhitelistRejects(unittest.TestCase):
    """Всё, что мог бы попросить агент, если бы захотел выйти за рамки."""

    def assertRejected(self, req, needle=None):
        with self.assertRaises(vf.Rejected) as ctx:
            vf.build(req)
        if needle:
            self.assertIn(needle, str(ctx.exception))

    def test_unknown_kind(self):
        self.assertRejected({"kind": "bash", "arg": "rm -rf /"}, "неизвестный вид")

    def test_shell_metacharacters_in_test_target(self):
        self.assertRejected({"kind": "run_tests", "arg": "tests.test_a; rm -rf ."})

    def test_pipe_in_test_target(self):
        self.assertRejected({"kind": "run_tests", "arg": "tests.a | cat /etc/passwd"})

    def test_path_traversal_in_git_log(self):
        self.assertRejected({"kind": "git_log", "arg": "../../../etc/passwd"})

    def test_absolute_path_in_git_log(self):
        self.assertRejected({"kind": "git_log", "arg": "/etc/passwd"})

    def test_dotdot_segment_anywhere(self):
        self.assertRejected({"kind": "git_log", "arg": "wordstat/../../secrets"})

    def test_legit_relative_path_allowed(self):
        self.assertIn("wordstat/rank.py",
                      vf.build({"kind": "git_log", "arg": "wordstat/rank.py"}))

    def test_dotfile_name_is_not_traversal(self):
        self.assertIn("tests/.keep", vf.build({"kind": "git_log", "arg": "tests/.keep"}))

    def test_git_ref_with_spaces(self):
        self.assertRejected({"kind": "git_show", "arg": "HEAD; git push"})

    def test_snippet_with_os_import(self):
        self.assertRejected({"kind": "python", "arg": "import os\nos.system('id')"},
                            "запрещённая конструкция")

    def test_snippet_with_file_write(self):
        self.assertRejected({"kind": "python", "arg": "open('/tmp/x','w').write('1')"})

    def test_snippet_with_dunder_import(self):
        self.assertRejected({"kind": "python", "arg": "__import__('os').listdir('.')"})

    def test_snippet_too_long(self):
        self.assertRejected({"kind": "python", "arg": "x=1\n" * 300}, "длиннее")

    def test_non_dict_request(self):
        self.assertRejected("run_tests")

    def test_missing_kind(self):
        self.assertRejected({"arg": "tests.test_a"})


class TestRunRequests(unittest.TestCase):
    def test_executes_and_captures_output(self):
        res = vf.run_requests([{"kind": "python", "arg": "print(6*7)", "why": "проверка"}],
                              cwd=str(V))
        self.assertEqual(res[0]["status"], "ok")
        self.assertEqual(res[0]["exit_code"], 0)
        self.assertIn("42", res[0]["output"])

    def test_failing_command_is_not_an_error_of_the_loop(self):
        res = vf.run_requests([{"kind": "python", "arg": "raise SystemExit(2)"}],
                              cwd=str(V))
        self.assertEqual(res[0]["status"], "ok")
        self.assertEqual(res[0]["exit_code"], 2)

    def test_rejected_request_is_reported_back(self):
        res = vf.run_requests([{"kind": "bash", "arg": "id"}], cwd=str(V))
        self.assertEqual(res[0]["status"], "rejected")
        self.assertIn("неизвестный вид", res[0]["output"])

    def test_request_limit_enforced(self):
        reqs = [{"kind": "python", "arg": f"print({i})"} for i in range(10)]
        self.assertEqual(len(vf.run_requests(reqs, cwd=str(V))), vf.MAX_REQUESTS)

    def test_empty_requests(self):
        self.assertEqual(vf.run_requests(None, cwd=str(V)), [])

    def test_timeout_is_survived(self):
        # Тест обязан быть самодостаточным: раньше он полагался на то, что
        # кто-то снаружи уменьшит CMD_TIMEOUT, и в общем прогоне вешал набор
        # на две минуты.
        original = vf.CMD_TIMEOUT
        vf.CMD_TIMEOUT = 2
        try:
            res = vf.run_requests([{"kind": "python", "arg": "while True: pass"}],
                                  cwd=str(V), max_requests=1)
        finally:
            vf.CMD_TIMEOUT = original
        self.assertEqual(res[0]["status"], "timeout")


class TestSanitizeOutput(unittest.TestCase):
    """VERIFY-1: вывод команды идёт в промпт — управляющие байты ломают вызов."""

    def test_null_byte_removed(self):
        self.assertNotIn("\x00", vf.sanitize_output("a\x00b"))

    def test_control_chars_removed(self):
        self.assertNotIn("\x07", vf.sanitize_output("bell\x07here"))

    def test_newlines_and_tabs_preserved(self):
        self.assertEqual(vf.sanitize_output("a\n\tb"), "a\n\tb")

    def test_unicode_preserved(self):
        self.assertEqual(vf.sanitize_output("привет ﬁ"), "привет ﬁ")

    def test_empty_safe(self):
        self.assertEqual(vf.sanitize_output(""), "")

    def test_real_command_with_null_byte_survives(self):
        res = vf.run_requests([{"kind": "python",
                                "arg": "print('a' + chr(0) + 'b')", "why": "w"}],
                              cwd=str(V))
        self.assertEqual(res[0]["status"], "ok")
        self.assertNotIn("\x00", res[0]["output"])


class TestArgumentForms(unittest.TestCase):
    """VERIFY-1: ревьюер пишет цели тестов и ссылки git в разных формах."""

    def test_test_path_converted_to_module(self):
        self.assertEqual(vf.build({"kind": "run_tests", "arg": "tests/test_roman.py"}),
                         ["python3", "-m", "unittest", "tests.test_roman"])

    def test_test_module_still_works(self):
        self.assertEqual(vf.build({"kind": "run_tests", "arg": "tests.test_roman"}),
                         ["python3", "-m", "unittest", "tests.test_roman"])

    def test_git_show_file_at_ref(self):
        self.assertEqual(vf.build({"kind": "git_show", "arg": "HEAD:wordstat/roman.py"}),
                         ["git", "show", "HEAD:wordstat/roman.py"])

    def test_git_show_ref_only_keeps_stat(self):
        self.assertEqual(vf.build({"kind": "git_show", "arg": "HEAD~1"}),
                         ["git", "show", "--stat", "HEAD~1"])

    def test_git_show_traversal_in_path_rejected(self):
        with self.assertRaises(vf.Rejected):
            vf.build({"kind": "git_show", "arg": "HEAD:../../etc/passwd"})

    def test_test_path_traversal_rejected(self):
        with self.assertRaises(vf.Rejected):
            vf.build({"kind": "run_tests", "arg": "../../etc/passwd.py"})


class TestFormatting(unittest.TestCase):
    def test_format_includes_kind_and_output(self):
        text = vf.format_results([{"kind": "run_tests", "arg": "tests.t", "why": "зачем",
                                   "status": "ok", "exit_code": 0, "output": "OK"}])
        self.assertIn("run_tests", text)
        self.assertIn("OK", text)
        self.assertIn("зачем", text)

    def test_empty_results_message(self):
        self.assertIn("не запрашивались", vf.format_results([]))


class TestWhitelistMatchesSchema(unittest.TestCase):
    """Схема — это обещание ревьюеру; whitelist — то, что реально исполнится.

    Расхождение не падает и не логируется как ошибка: запрос просто
    отклоняется, а ревьюер получает пустой результат и решает, что
    проверка ничего не дала. На пилоте так молча отклонялись 2 запроса
    из 3 — схема давала `unittest`, whitelist ждал `run_tests`.
    """

    def setUp(self):
        schema_path = (pathlib.Path(__file__).resolve().parent.parent
                       / "schemas" / "verdict-v1.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.kinds = set(schema["properties"]["verification_requests"]
                         ["items"]["properties"]["kind"]["enum"])

    def test_every_promised_kind_is_executable(self):
        missing = sorted(self.kinds - set(vf.WHITELIST))
        self.assertEqual(missing, [], f"схема обещает, а исполнить нельзя: {missing}")

    def test_promised_kinds_build_a_command(self):
        args = {"unittest": "tests.test_x", "unittest_all": None,
                "git_show": "HEAD", "git_log": "HEAD", "python": "print(1)"}
        for kind in self.kinds:
            req = {"kind": kind, "why": "проверка"}
            if args.get(kind) is not None:
                req["arg"] = args[kind]
            argv = vf.build(req)
            self.assertTrue(argv, f"{kind} не собрал команду")

    def test_unknown_kind_still_rejected(self):
        with self.assertRaises(vf.Rejected):
            vf.build({"kind": "rm_rf", "arg": "/", "why": "злой"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
