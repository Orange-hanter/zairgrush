#!/usr/bin/env python3
"""Golden-тесты хелперов: fail-open, секрет-фильтр, механическая часть.

Сеть НЕ используется: ollama_chat подменяется. Проверяется ровно то, что
обещает §7.3 — падение хелпера не ломает петлю, а секреты не улетают
во внешний API.
"""
import importlib.util
import io
import json
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
H = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("helpers", ROOT_DIR / "helpers.py")
hp = importlib.util.module_from_spec(spec)
sys.modules["helpers"] = hp
spec.loader.exec_module(hp)

TASK = {"id": "t1", "title": "Добавить проверку типа n"}
FALLBACK = "t1: Добавить проверку типа n"


class FakeResponse(io.StringIO):
    """Ответ /api/chat: тело + заголовки (клиент читает x-request-id)."""

    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload))
        self.headers = headers or {"x-request-id": "test-req-id"}


class HelperTestCase(unittest.TestCase):
    def setUp(self):
        self.orig = hp.ollama_chat
        self.sent = []

    def tearDown(self):
        hp.ollama_chat = self.orig

    def reply(self, text):
        def fake(prompt, name, **kw):
            self.sent.append(prompt)
            return text
        hp.ollama_chat = fake


class TestSecretScrub(unittest.TestCase):
    def test_bearer_token_removed(self):
        out = hp.scrub("Authorization: Bearer sk-abcdefghijklmnopqrstuvwx1234")
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertIn("[REDACTED]", out)

    def test_api_key_assignment_removed(self):
        out = hp.scrub('OLLAMA_API_KEY="a1b2c3d4e5f6g7h8i9j0"')
        self.assertNotIn("a1b2c3d4e5f6g7h8i9j0", out)

    def test_private_key_block_removed(self):
        blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\n-----END RSA PRIVATE KEY-----"
        self.assertNotIn("MIIabc123", hp.scrub(blob))

    def test_ordinary_code_survives(self):
        code = "def word_freq(text):\n    return {}"
        self.assertEqual(hp.scrub(code), code)

    def test_none_is_safe(self):
        self.assertIsNone(hp.scrub(None))


class TestCommitMessage(HelperTestCase):
    def test_uses_helper_line(self):
        self.reply("Добавить проверку типа аргумента n в ngrams")
        msg, src = hp.commit_message(TASK, "diff", FALLBACK)
        self.assertEqual(src, "helper")
        self.assertEqual(msg, "Добавить проверку типа аргумента n в ngrams")

    def test_fallback_when_helper_down(self):
        self.reply(None)
        msg, src = hp.commit_message(TASK, "diff", FALLBACK)
        self.assertEqual((msg, src), (FALLBACK, "fallback:no_response"))

    def test_fallback_when_too_long(self):
        self.reply("x" * 100)
        _, src = hp.commit_message(TASK, "diff", FALLBACK)
        self.assertEqual(src, "fallback:validation")

    def test_markdown_fence_is_unwrapped_not_rejected(self):
        # OLLAMA-1: format на облаке не принуждает, модель часто оборачивает
        # ответ в ```; выбрасывать такой ответ — терять годный результат.
        self.reply("```\nДобавить проверку типа n\n```")
        msg, src = hp.commit_message(TASK, "diff", FALLBACK)
        self.assertEqual((msg, src), ("Добавить проверку типа n", "helper"))

    def test_fallback_when_reply_is_prose(self):
        self.reply("Конечно! Вот подходящее сообщение коммита, которое "
                   "описывает суть изменений в этом диффе подробно и обстоятельно")
        _, src = hp.commit_message(TASK, "diff", FALLBACK)
        self.assertEqual(src, "fallback:validation")

    def test_multiline_reply_takes_first_line(self):
        self.reply("Добавить проверку типа n\n\nПодробности: ...")
        msg, src = hp.commit_message(TASK, "diff", FALLBACK)
        self.assertEqual((msg, src), ("Добавить проверку типа n", "helper"))

    def test_diff_reaches_helper_layer(self):
        self.reply("ок")
        hp.commit_message(TASK, "diff-body-marker", FALLBACK)
        self.assertIn("diff-body-marker", self.sent[0])


class TestDedupFindings(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.prev = [{"file": "a.py", "category": "correctness", "issue": "не проверен тип n"}]
        self.curr = [
            {"file": "a.py", "category": "correctness", "issue": "тип n не валидируется"},
            {"file": "b.py", "category": "style", "issue": "длинная строка"},
        ]

    def test_exact_match_found_without_llm(self):
        called = []
        hp.ollama_chat = lambda *a, **k: called.append(1) or None
        out = hp.dedup_findings(self.prev, self.curr)
        self.assertEqual(len(out["mechanical"]), 1)
        self.assertEqual(out["mechanical"][0]["file"], "a.py")

    def test_no_llm_call_when_nothing_left(self):
        called = []
        hp.ollama_chat = lambda *a, **k: called.append(1) or None
        hp.dedup_findings(self.prev, [self.curr[0]])
        self.assertEqual(called, [], "§7.1: без остатка модель не зовём")

    def test_semantic_repeat_parsed(self):
        self.reply('{"repeated": [0]}')
        out = hp.dedup_findings(self.prev, [self.curr[1]])
        self.assertEqual(len(out["semantic"]), 1)

    def test_garbage_reply_is_survived(self):
        self.reply("извините, не понял задачу")
        out = hp.dedup_findings(self.prev, [self.curr[1]])
        self.assertEqual(out["semantic"], [])

    def test_out_of_range_index_ignored(self):
        self.reply('{"repeated": [7]}')
        out = hp.dedup_findings(self.prev, [self.curr[1]])
        self.assertEqual(out["semantic"], [])

    def test_helper_down_keeps_mechanical_part(self):
        self.reply(None)
        out = hp.dedup_findings(self.prev, self.curr)
        self.assertEqual(len(out["mechanical"]), 1)
        self.assertEqual(out["semantic"], [])


class TestStagnationHint(HelperTestCase):
    def test_same(self):
        self.reply("SAME")
        self.assertIs(hp.stagnation_hint("a", "b"), True)

    def test_different(self):
        self.reply("DIFFERENT")
        self.assertIs(hp.stagnation_hint("a", "b"), False)

    def test_unparseable_defers_to_mechanics(self):
        self.reply("возможно, похоже")
        self.assertIsNone(hp.stagnation_hint("a", "b"))

    def test_helper_down_defers_to_mechanics(self):
        self.reply(None)
        self.assertIsNone(hp.stagnation_hint("a", "b"))


class TestCleanMarkup(unittest.TestCase):
    """Найдено на HELP-1: gemma4 подмешивает LaTeX в простой текст."""

    def test_latex_arrow_replaced(self):
        self.assertEqual(hp.clean_markup(r"r1cf: dispute $\rightarrow$ blocked"),
                         "r1cf: dispute -> blocked")

    def test_bare_latex_command_replaced(self):
        self.assertEqual(hp.clean_markup(r"a \to b"), "a -> b")

    def test_inline_math_unwrapped(self):
        self.assertEqual(hp.clean_markup("время $t_1$ секунд"), "время t_1 секунд")

    def test_markdown_bold_and_ticks_removed(self):
        self.assertEqual(hp.clean_markup("**итог**: `done`"), "итог: done")

    def test_plain_text_untouched(self):
        line = "r5rp: done за 54.9с, ревью approve"
        self.assertEqual(hp.clean_markup(line), line)


class TestSummarizeLog(HelperTestCase):
    def test_latex_is_cleaned_in_output(self):
        self.reply(r"- r1cf: dispute $\rightarrow$ blocked")
        self.assertEqual(hp.summarize_log(["a"]), "- r1cf: dispute -> blocked")

    def test_truncates_to_max_lines(self):
        self.reply("\n".join(f"- строка {i}" for i in range(20)))
        out = hp.summarize_log(["a", "b"], max_lines=3)
        self.assertEqual(len(out.splitlines()), 3)

    def test_empty_entries_skip_call(self):
        called = []
        hp.ollama_chat = lambda *a, **k: called.append(1) or "x"
        self.assertIsNone(hp.summarize_log([]))
        self.assertEqual(called, [])

    def test_helper_down_returns_none(self):
        self.reply(None)
        self.assertIsNone(hp.summarize_log(["a"]))


class TestNetworkLayer(unittest.TestCase):
    """Секрет-фильтр, fail-open и разбор нативного ответа /api/chat."""

    def setUp(self):
        import urllib.request
        self.mod = urllib.request
        self.orig = urllib.request.urlopen
        self.bodies = []

    def tearDown(self):
        self.mod.urlopen = self.orig

    def fake_transport(self, payload=None, raises=None):
        def fake(req, timeout=None):
            self.bodies.append(req.data.decode())
            if raises:
                raise raises
            return FakeResponse(payload)
        self.mod.urlopen = fake

    def test_secrets_never_leave_the_process(self):
        self.fake_transport({"message": {"content": "ok"}})
        import os
        os.environ["OLLAMA_API_KEY"] = "dummy"
        hp.ollama_chat('OLLAMA_API_KEY="a1b2c3d4e5f6g7h8"', "test")
        self.assertNotIn("a1b2c3d4e5f6g7h8", self.bodies[0])
        self.assertIn("REDACTED", self.bodies[0])

    def test_network_error_returns_none(self):
        self.fake_transport(raises=OSError("no net"))
        import os
        os.environ["OLLAMA_API_KEY"] = "dummy"
        self.assertIsNone(hp.ollama_chat("p", "test"))

    def test_missing_key_skips_call(self):
        import os
        saved = os.environ.pop("OLLAMA_API_KEY", None)
        try:
            self.fake_transport({"message": {"content": "x"}})
            self.assertIsNone(hp.ollama_chat("p", "test"))
            self.assertEqual(self.bodies, [], "без ключа сетевой вызов не делается")
        finally:
            if saved:
                os.environ["OLLAMA_API_KEY"] = saved


class TestNativeApiContract(unittest.TestCase):
    """OLLAMA-1: нативный /api/chat, think=false, детект обрезания."""

    def setUp(self):
        import urllib.request
        self.mod = urllib.request
        self.orig = urllib.request.urlopen
        self.reqs = []
        import os
        os.environ["OLLAMA_API_KEY"] = "dummy"

    def tearDown(self):
        self.mod.urlopen = self.orig

    def transport(self, payload):
        import json as js

        def fake(req, timeout=None):
            self.reqs.append(js.loads(req.data.decode()))
            self.url = req.full_url
            return FakeResponse(payload)
        self.mod.urlopen = fake

    def test_uses_native_endpoint_with_think_disabled(self):
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t", max_tokens=42)
        body = self.reqs[0]
        self.assertTrue(self.url.endswith("/api/chat"))
        self.assertIs(body["think"], False)
        self.assertEqual(body["options"]["num_predict"], 42)
        self.assertIn("seed", body["options"])
        self.assertNotIn("max_tokens", body, "max_tokens — поле /v1, нативный API его не знает")

    def test_deterministic_sampling_options(self):
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t")
        opts = self.reqs[0]["options"]
        self.assertEqual(opts["temperature"], 0.0)
        self.assertEqual(opts["top_k"], 1, "temperature=0 сама по себе не даёт greedy")
        self.assertEqual(opts["repeat_penalty"], 1.0, "дефолт 1.1 давит повторы ключей JSON")

    def test_cloud_ignored_knobs_are_not_sent(self):
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t")
        body = self.reqs[0]
        self.assertNotIn("num_ctx", body["options"], "на облаке игнорируется")
        self.assertNotIn("truncate", body)
        self.assertNotIn("shift", body)

    def test_error_in_body_with_http_200(self):
        self.transport({"error": "model is currently unavailable"})
        self.assertIsNone(hp.ollama_chat("p", "t"))

    def test_truncated_reply_is_discarded(self):
        self.transport({"message": {"content": '{"partial": '}, "done_reason": "length"})
        self.assertIsNone(hp.ollama_chat("p", "t"),
                          "обрезанный ответ хуже отсутствующего")

    def test_complete_reply_passes(self):
        self.transport({"message": {"content": "готово"}, "done_reason": "stop"})
        self.assertEqual(hp.ollama_chat("p", "t"), "готово")


class TestStripFences(unittest.TestCase):
    """OLLAMA-1: format на облаке не принуждает — чистим сами."""

    def test_json_fence_removed(self):
        self.assertEqual(hp.strip_fences('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_bare_fence_removed(self):
        self.assertEqual(hp.strip_fences("```\nтекст\n```"), "текст")

    def test_plain_text_untouched(self):
        self.assertEqual(hp.strip_fences("обычный текст"), "обычный текст")

    def test_none_safe(self):
        self.assertIsNone(hp.strip_fences(None))


class TestFailOpenContract(HelperTestCase):
    """§7.3: падение хелпера НИКОГДА не всплывает в петлю."""

    def setUp(self):
        super().setUp()
        def boom(*a, **k):
            raise RuntimeError("внутренняя поломка хелпера")
        hp.ollama_chat = boom

    def test_commit_message_falls_back(self):
        msg, src = hp.commit_message(TASK, "d", FALLBACK)
        self.assertEqual((msg, src), (FALLBACK, "fallback:exception"))

    def test_dedup_returns_empty_structure(self):
        out = hp.dedup_findings([{"file": "a", "category": "c", "issue": "i"}],
                                [{"file": "b", "category": "d", "issue": "j"}])
        self.assertEqual(out, {"mechanical": [], "semantic": []})

    def test_stagnation_returns_none(self):
        self.assertIsNone(hp.stagnation_hint("a", "b"))

    def test_summarize_returns_none(self):
        self.assertIsNone(hp.summarize_log(["a"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
