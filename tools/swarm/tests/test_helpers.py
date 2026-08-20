#!/usr/bin/env python3
"""Golden-тесты хелперов: fail-open, секрет-фильтр, механическая часть.

Сеть НЕ используется: ollama_chat подменяется. Проверяется ровно то, что
обещает §7.3 — падение хелпера не ломает петлю, а секреты не улетают
во внешний API.
"""
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
H = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("helpers", ROOT_DIR / "helpers.py")
hp = importlib.util.module_from_spec(spec)
sys.modules["helpers"] = hp
spec.loader.exec_module(hp)

TASK = {"id": "t1", "title": "Добавить проверку типа n"}
FALLBACK = "t1: Добавить проверку типа n"


def set_env(case, key, value):
    """Подменить переменную окружения с гарантированным откатом.

    Раньше тесты писали OLLAMA_API_KEY прямо в os.environ и не убирали:
    значение протекало во все последующие тесты процесса.
    """
    saved = os.environ.get(key)
    os.environ[key] = value

    def restore():
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
    case.addCleanup(restore)


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
        blob = ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\n"
                "-----END RSA PRIVATE KEY-----")
        self.assertNotIn("MIIabc123", hp.scrub(blob))

    def test_github_token_with_underscore_removed(self):
        """Дефект: шаблон требовал ДЕФИС после префикса, а реальные токены
        GitHub идут через подчёркивание — ghp_/gho_ улетали в API целиком."""
        out = hp.scrub("push via ghp_Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv")
        self.assertNotIn("Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv", out)
        self.assertIn("[REDACTED]", out)

    def test_github_pat_prefix_removed(self):
        """github_pat_ — отдельный префикс fine-grained PAT, его не было."""
        out = hp.scrub("github_pat_11ABCDE0F_lmnopqrstuvwx1234567890")
        self.assertNotIn("11ABCDE0F", out)

    def test_stripe_live_key_removed(self):
        out = hp.scrub("charge with sk_live_4eC39HqLyjWDarjtT1zdp7dc")
        self.assertNotIn("4eC39HqLyjWDarjtT1zdp7dc", out)

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
        self.prev = [{"file": "a.py", "category": "correctness",
                      "issue": "не проверен тип n"}]
        # curr[0] — ТОЧНЫЙ повтор прошлой находки: механика ловит только
        # дословное совпадение, перефразированное — работа модели.
        self.curr = [
            {"file": "a.py", "category": "correctness",
             "issue": "не проверен тип n"},
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

    def test_new_issue_in_same_file_category_is_not_a_repeat(self):
        """Дефект: ключ (file, category) записывал в повторы НОВУЮ находку
        в том же файле той же категории — и она молча терялась. Текст
        находки обязан быть частью механического ключа."""
        called = []
        hp.ollama_chat = lambda *a, **k: called.append(1) or None
        curr = [{"file": "a.py", "category": "correctness",
                 "issue": "деление на ноль в mean"}]
        out = hp.dedup_findings(self.prev, curr)
        self.assertEqual(out["mechanical"], [],
                         "непохожая находка — не механический повтор")
        self.assertTrue(called, "решение о перефразировке — за моделью")

    def test_issue_text_normalized_for_mechanical_match(self):
        """Повтор с другим регистром и пробелами — всё ещё точный повтор."""
        called = []
        hp.ollama_chat = lambda *a, **k: called.append(1) or None
        curr = [{"file": "a.py", "category": "correctness",
                 "issue": "  НЕ ПРОВЕРЕН   ТИП N "}]
        out = hp.dedup_findings(self.prev, curr)
        self.assertEqual(len(out["mechanical"]), 1)
        self.assertEqual(called, [], "точный повтор модель не тревожит")


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

    def test_negated_same_is_not_same(self):
        """Дефект: подстрочный поиск SAME давал True на «NOT THE SAME» —
        отрицание превращалось в подтверждение стагнации."""
        self.reply("NOT THE SAME")
        self.assertIs(hp.stagnation_hint("a", "b"), False)

    def test_bold_same_still_parsed(self):
        self.reply("**SAME**")
        self.assertIs(hp.stagnation_hint("a", "b"), True)

    def test_same_with_trailing_explanation(self):
        self.reply("SAME — обе претензии о валидации входа")
        self.assertIs(hp.stagnation_hint("a", "b"), True)

    def test_same_buried_in_prose_is_not_trusted(self):
        """SAME не первым словом — это уже не ответ на вопрос формата."""
        self.reply("Кажется, THE SAME, но не уверен")
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
        # Предохранитель — состояние процесса: чужие отказы из соседних
        # тестов не должны размыкать его здесь.
        hp._state["failures"] = 0
        self.addCleanup(hp._state.__setitem__, "failures", 0)

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
        set_env(self, "OLLAMA_API_KEY", "dummy")
        hp.ollama_chat('OLLAMA_API_KEY="a1b2c3d4e5f6g7h8"', "test")
        self.assertNotIn("a1b2c3d4e5f6g7h8", self.bodies[0])
        self.assertIn("REDACTED", self.bodies[0])

    def test_network_error_returns_none(self):
        self.fake_transport(raises=OSError("no net"))
        set_env(self, "OLLAMA_API_KEY", "dummy")
        self.assertIsNone(hp.ollama_chat("p", "test"))

    def test_missing_key_skips_call(self):
        saved = os.environ.pop("OLLAMA_API_KEY", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "OLLAMA_API_KEY", saved)
        self.fake_transport({"message": {"content": "x"}})
        self.assertIsNone(hp.ollama_chat("p", "test"))
        self.assertEqual(self.bodies, [], "без ключа сетевой вызов не делается")


class TestNativeApiContract(unittest.TestCase):
    """OLLAMA-1: нативный /api/chat, think=false, детект обрезания."""

    def setUp(self):
        import urllib.request
        self.mod = urllib.request
        self.orig = urllib.request.urlopen
        self.reqs = []
        set_env(self, "OLLAMA_API_KEY", "dummy")
        hp._state["failures"] = 0
        self.addCleanup(hp._state.__setitem__, "failures", 0)

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
        self.assertNotIn("max_tokens", body,
                         "max_tokens — поле /v1, нативный API его не знает")

    def test_think_level_reaches_the_wire_verbatim(self):
        """gpt-оss булево ИГНОРИРУЕТ и ждёт уровень (замер REVIEWARM).

        Значит уровень обязан доехать до тела запроса как строка, а не
        быть приведённым к булеву по дороге: `bool("low")` — это True,
        то есть «думай сколько хочешь», ровно противоположное намерению.
        """
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t", think="low")
        self.assertEqual(self.reqs[0]["think"], "low")

    def test_think_defaults_to_false_for_every_other_model(self):
        """Умолчание не съехало: контур не платит за размышления."""
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t")
        self.assertIs(self.reqs[0]["think"], False)

    def test_thinking_volume_is_metered_apart_from_the_answer(self):
        """Размышления не идут в результат, но тратят тот же num_predict.

        Без этого числа строка «обрезан» читается как «модель плоха»,
        хотя бюджет ушёл в размышления, которых не просили, — именно так
        первый прогон REVIEWARM списал годную модель.
        """
        rows = []
        set_env(self, "SWARM_HELPER_METRICS", "")
        self.addCleanup(setattr, hp, "_metric", hp._metric)
        hp._metric = lambda **kw: rows.append(kw)
        self.transport({"message": {"content": "", "thinking": "ааа" * 10},
                        "done_reason": "length"})
        hp.ollama_chat("p", "t", think="low")
        self.assertEqual(rows[0]["thinking_chars"], 30)
        self.assertEqual(rows[0]["think"], "low")
        self.assertTrue(rows[0]["truncated"])
        self.assertFalse(rows[0]["ok"])

    def test_structured_output_is_not_requested(self):
        """Облако Ollama не поддерживает structured outputs (документация
        вендора + замер 2026-08-20: ответ приходит по промпту, не по схеме).
        Поле `format` в запросе означало бы контракт, которого нет."""
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t")
        self.assertNotIn("format", self.reqs[0])

    def test_deterministic_sampling_options(self):
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t")
        opts = self.reqs[0]["options"]
        self.assertEqual(opts["temperature"], 0.0)
        self.assertEqual(opts["top_k"], 1, "temperature=0 сама по себе не даёт greedy")
        self.assertEqual(opts["repeat_penalty"], 1.0,
                         "дефолт 1.1 давит повторы ключей JSON")

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
        self.transport({"message": {"content": '{"partial": '},
                        "done_reason": "length"})
        self.assertIsNone(hp.ollama_chat("p", "t"),
                          "обрезанный ответ хуже отсутствующего")

    def test_truncated_reply_metric_is_not_ok(self):
        """Дефект: метрика писала ok=True, а результат строкой ниже
        выбрасывался как обрезанный — по журналу вызов выглядел удачным."""
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "m.jsonl"
            hp.configure(target)
            self.addCleanup(hp.configure, None)
            self.transport({"message": {"content": '{"partial": '},
                            "done_reason": "length"})
            self.assertIsNone(hp.ollama_chat("p", "t"))
            row = json.loads(target.read_text().splitlines()[-1])
            self.assertIs(row["ok"], False, "ok — пригодность результата")
            self.assertIs(row["truncated"], True)

    def test_complete_reply_passes(self):
        self.transport({"message": {"content": "готово"}, "done_reason": "stop"})
        self.assertEqual(hp.ollama_chat("p", "t"), "готово")

    def test_explicit_model_overrides_the_module_default(self):
        """E10 (chat-fill): любая модель Ollama Cloud идёт тем же
        контрактом, что и хелперы третьего контура — транспорт общий."""
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t", model="kimi-k2.7-code")
        self.assertEqual(self.reqs[0]["model"], "kimi-k2.7-code")

    def test_omitted_model_keeps_the_helper_default(self):
        self.transport({"message": {"content": "ok"}})
        hp.ollama_chat("p", "t")
        self.assertEqual(self.reqs[0]["model"], hp.MODEL)

    def test_metric_row_names_the_explicit_model(self):
        """Метрика обязана показать модель, что реально звалась, — иначе
        по журналу chat-fill выглядел бы как вызов хелпера по умолчанию."""
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "m.jsonl"
            hp.configure(target)
            self.addCleanup(hp.configure, None)
            self.transport({"message": {"content": "ok"}, "done_reason": "stop"})
            hp.ollama_chat("p", "fill", model="glm-5.1")
            row = json.loads(target.read_text().splitlines()[-1])
            self.assertEqual(row["model"], "glm-5.1")


class TestMetricsDestination(unittest.TestCase):
    """Дефект: METRICS по умолчанию указывал в каталог ПАКЕТА, а env
    HELPER_METRICS читался один раз при импорте — метрики каждого прогона
    тестов и каждого реального запуска оседали в исходниках инструмента."""

    def setUp(self):
        hp.configure(None)
        self.addCleanup(hp.configure, None)
        saved = os.environ.pop("HELPER_METRICS", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "HELPER_METRICS", saved)

    def test_no_destination_means_no_write(self):
        """Без configure и без env строка не пишется никуда — и уж точно
        не в каталог пакета, как делал прежний дефолт."""
        hp._metric(helper="t", ok=True)
        self.assertIsNone(hp._metrics_path())
        self.assertFalse((ROOT_DIR / "metrics.jsonl").exists(),
                         "файл в исходниках инструмента — та самая течь")

    def test_configure_redirects_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "m.jsonl"
            hp.configure(target)
            hp._metric(helper="t", ok=True)
            row = json.loads(target.read_text().splitlines()[0])
            self.assertEqual(row["helper"], "t")
            self.assertIn("ts", row, "строка проходит через obs.stamp")

    def test_env_read_at_call_time(self):
        """env читается в момент записи: выставленный ПОСЛЕ импорта модуля
        HELPER_METRICS обязан работать."""
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "env.jsonl"
            os.environ["HELPER_METRICS"] = str(target)
            self.addCleanup(os.environ.pop, "HELPER_METRICS", None)
            hp._metric(helper="t")
            self.assertTrue(target.exists())

    def test_configured_path_beats_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured = pathlib.Path(tmp) / "cfg.jsonl"
            enved = pathlib.Path(tmp) / "env.jsonl"
            os.environ["HELPER_METRICS"] = str(enved)
            self.addCleanup(os.environ.pop, "HELPER_METRICS", None)
            hp.configure(configured)
            hp._metric(helper="t")
            self.assertTrue(configured.exists())
            self.assertFalse(enved.exists())


class TestCircuitBreaker(unittest.TestCase):
    """Дефект: предохранителя не было — при лежащем API каждый вызов
    хелпера платил до TIMEOUT=90с за опциональный слой."""

    def setUp(self):
        import urllib.request
        self.mod = urllib.request
        self.orig = urllib.request.urlopen
        self.bodies = []
        hp._state["failures"] = 0
        self.addCleanup(hp._state.__setitem__, "failures", 0)
        set_env(self, "OLLAMA_API_KEY", "dummy")

    def tearDown(self):
        self.mod.urlopen = self.orig

    def transport(self, payload=None, raises=None):
        def fake(req, timeout=None):
            self.bodies.append(req.data.decode())
            if raises:
                raise raises
            return FakeResponse(payload)
        self.mod.urlopen = fake

    def test_opens_after_consecutive_failures(self):
        self.transport(raises=OSError("api down"))
        for _ in range(hp.BREAKER_THRESHOLD):
            self.assertIsNone(hp.ollama_chat("p", "t"))
        attempts = len(self.bodies)
        self.assertIsNone(hp.ollama_chat("p", "t"))
        self.assertEqual(len(self.bodies), attempts,
                         "после размыкания сетевых попыток больше нет")

    def test_success_resets_counter(self):
        self.transport(raises=OSError("blip"))
        for _ in range(hp.BREAKER_THRESHOLD - 1):
            hp.ollama_chat("p", "t")
        self.transport(payload={"message": {"content": "ok"}})
        self.assertEqual(hp.ollama_chat("p", "t"), "ok")
        self.assertEqual(hp._state["failures"], 0,
                         "успех обнуляет счётчик: рвём только серию подряд")

    def test_skip_leaves_honest_metric_row(self):
        """Пропуск по предохранителю — факт о прогоне, он обязан попасть
        в журнал, а не выглядеть как «хелпер не звался вовсе»."""
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "m.jsonl"
            hp.configure(target)
            self.addCleanup(hp.configure, None)
            hp._state["failures"] = hp.BREAKER_THRESHOLD
            self.transport(payload={"message": {"content": "ok"}})
            self.assertIsNone(hp.ollama_chat("p", "t"))
            row = json.loads(target.read_text().splitlines()[-1])
            self.assertEqual(row["skipped"], "circuit_breaker")
            self.assertEqual(self.bodies, [])


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


class TestConsolidateLessons(HelperTestCase):
    """§7.2 буквально: строка сводки без ссылки на существующий id урока
    отбрасывается — хелпер не добавляет в память факты без происхождения."""

    RECORDS = [{"id": "aaa111", "body": "урок раз"},
               {"id": "bbb222", "body": "урок два"}]

    def test_uncited_lines_are_dropped(self):
        self.reply("Тема гейта — суть (aaa111)\n"
                   "Выдумка без единой ссылки\n"
                   "Ещё тема (bbb222)")
        out = hp.consolidate_lessons(self.RECORDS)
        self.assertIn("aaa111", out)
        self.assertIn("bbb222", out)
        self.assertNotIn("Выдумка", out)

    def test_helper_down_is_none(self):
        self.reply(None)
        self.assertIsNone(hp.consolidate_lessons(self.RECORDS))

    def test_no_records_means_no_call(self):
        called = []
        hp.ollama_chat = lambda *a, **k: called.append(1) or "x"
        self.assertIsNone(hp.consolidate_lessons([]))
        self.assertEqual(called, [], "без уроков модель не зовём (§7.1)")


class TestEmbedText(unittest.TestCase):
    """Эмбеддер памяти (E9): опционален по построению, любой сбой -> None.

    Живой probe /api/embed не проведён (в окружении нет ключа) — формат
    ответа взят из документации нативного API и закреплён здесь; первый
    живой вызов обязан подтвердить его метрикой ok=True с dim.
    """

    def setUp(self):
        import urllib.request
        self.mod = urllib.request
        self.orig = urllib.request.urlopen
        self.bodies = []
        self.urls = []
        hp._state["failures"] = 0
        self.addCleanup(hp._state.__setitem__, "failures", 0)

    def tearDown(self):
        self.mod.urlopen = self.orig

    def transport(self, payload=None, raises=None):
        def fake(req, timeout=None):
            self.bodies.append(req.data.decode())
            self.urls.append(req.full_url)
            if raises:
                raise raises
            return FakeResponse(payload)
        self.mod.urlopen = fake

    def test_no_key_skips_without_network(self):
        saved = os.environ.pop("OLLAMA_API_KEY", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "OLLAMA_API_KEY", saved)
        self.transport({"embeddings": [[0.1]]})
        self.assertIsNone(hp.embed_text("текст", "m"))
        self.assertEqual(self.bodies, [], "без ключа сети быть не должно")

    def test_vector_returned_and_secrets_scrubbed(self):
        set_env(self, "OLLAMA_API_KEY", "dummy")
        self.transport({"embeddings": [[0.25, -1.0, 0.5]]})
        vec = hp.embed_text("token ghp_" + "b" * 40 + " рядом", "m")
        self.assertEqual(vec, [0.25, -1.0, 0.5])
        self.assertNotIn("ghp_" + "b" * 40, self.bodies[0],
                         "текст урока едет наружу — секреты вычищаются")

    def test_error_body_is_none(self):
        set_env(self, "OLLAMA_API_KEY", "dummy")
        self.transport({"error": "model not found"})
        self.assertIsNone(hp.embed_text("текст", "m"))

    def test_empty_embeddings_is_none(self):
        set_env(self, "OLLAMA_API_KEY", "dummy")
        self.transport({"embeddings": []})
        self.assertIsNone(hp.embed_text("текст", "m"))

    def test_openrouter_route_with_dimensions(self):
        """Транспорт по префиксу модели: openrouter:slug@dim идёт на
        OpenAI-совместимый эндпоинт с параметром dimensions (Matryoshka
        2048 — замер оракула: без потерь против 4096)."""
        set_env(self, "OPENROUTER_API_KEY", "dummy-or")
        saved = os.environ.pop("OLLAMA_API_KEY", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "OLLAMA_API_KEY", saved)
        self.transport({"data": [{"embedding": [0.5, 0.25]}],
                        "usage": {"cost": 5.6e-07}})
        vec = hp.embed_text("урок", "openrouter:qwen/qwen3-embedding-8b@2048")
        self.assertEqual(vec, [0.5, 0.25])
        self.assertIn("openrouter.ai", self.urls[0])
        body = json.loads(self.bodies[0])
        self.assertEqual(body["model"], "qwen/qwen3-embedding-8b")
        self.assertEqual(body["dimensions"], 2048)

    def test_openrouter_without_its_key_skips(self):
        for var in ("OPENROUTER_API_KEY",):
            saved = os.environ.pop(var, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, var, saved)
        set_env(self, "OLLAMA_API_KEY", "dummy")   # чужой ключ не подходит
        self.transport({"data": [{"embedding": [0.5]}]})
        self.assertIsNone(hp.embed_text("урок", "openrouter:m"))
        self.assertEqual(self.bodies, [])

    def test_network_error_trips_breaker(self):
        set_env(self, "OLLAMA_API_KEY", "dummy")
        self.transport(raises=OSError("no net"))
        for _ in range(hp.BREAKER_THRESHOLD):
            self.assertIsNone(hp.embed_text("т", "m"))
        calls_before = len(self.bodies)
        self.assertIsNone(hp.embed_text("т", "m"))
        self.assertEqual(len(self.bodies), calls_before,
                         "предохранитель обязан не звать сеть")


if __name__ == "__main__":
    unittest.main(verbosity=2)
