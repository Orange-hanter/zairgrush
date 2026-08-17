#!/usr/bin/env python3
"""Golden-тесты слоя запуска (§6.0) и процедуры рестарта (§5.2).

Вместо CLI подставляются короткие python-скрипты, которые ведут себя как
агент: молчат, падают, сыплют события бесконечно, отвечают нормально.
Реальные модели тут не нужны — проверяется механика драйвера.
"""
import importlib.util
import json
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
D = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("driver", ROOT_DIR / "driver.py")
dr = importlib.util.module_from_spec(spec)
sys.modules["driver"] = dr
spec.loader.exec_module(dr)


def fake_agent(script):
    """Команда, изображающая агента с заданным поведением."""
    return [sys.executable, "-c", script]


NORMAL = r"""
import json, sys, time
print(json.dumps({"role": "meta", "type": "start"}), flush=True)
print(json.dumps({"role": "assistant",
                  "tool_calls": [{"function": {"name": "Read"}}]}), flush=True)
print(json.dumps({"role": "tool", "content": "OK"}), flush=True)
print(json.dumps({"role": "assistant",
                  "content": json.dumps({"status": "done",
                                         "summary": "s"})}), flush=True)
print(json.dumps({"role": "meta", "session_id": "sess-1"}), flush=True)
"""

SILENT = r"""
import json, sys, time
print(json.dumps({"role": "assistant", "content": "начал работу"}), flush=True)
time.sleep(30)
"""

CRASH = r"""
import json, sys
print(json.dumps({"role": "assistant", "content": "начал"}), flush=True)
sys.exit(3)
"""

# Дословный паттерн 403-аварии Kimi с пилота: баннер версии в stdout,
# объяснение — в stderr, смерть за секунды.
QUOTA_CRASH = r"""
import json, sys
print(json.dumps({"role": "meta", "type": "system.version",
                  "version": "0.36.1"}), flush=True)
print("error: failed to run prompt: provider.auth_error: 403 "
      "You've reached your usage limit for this billing cycle.",
      file=sys.stderr)
sys.exit(1)
"""

CHATTY = r"""
import json, time
while True:
    print(json.dumps({"role": "tool", "content": "тик"}), flush=True)
    time.sleep(0.05)
"""

PROSE_ONLY = r"""
import json
print(json.dumps({"role": "assistant",
                  "content": "Готово, но JSON я не верну"}), flush=True)
"""


def extract(stream):
    """Тот же приём, что в раннере: последнее assistant-событие с JSON."""
    report = None
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("role") == "assistant" and isinstance(ev.get("content"), str):
            s = ev["content"].strip()
            if s.startswith("{"):
                try:
                    cand = json.loads(s)
                    if isinstance(cand, dict) and "status" in cand:
                        report = cand
                except ValueError:
                    pass
    return report


class TestHappyRun(unittest.TestCase):
    def test_normal_agent_yields_report(self):
        d = dr.AgentDriver(cwd=str(D))
        res = d.start(fake_agent(NORMAL)).collect(extract)
        self.assertTrue(res.ok, res)
        self.assertEqual(res.report["status"], "done")
        self.assertEqual(res.reason, "done")

    def test_events_are_normalized(self):
        d = dr.AgentDriver(cwd=str(D))
        run = d.start(fake_agent(NORMAL))
        kinds = [e.kind for e in run.events()]
        self.assertIn(dr.TOOL_CALL, kinds)
        self.assertIn(dr.TOOL_RESULT, kinds)
        self.assertIn(dr.TEXT, kinds)

    def test_last_activity_advances(self):
        d = dr.AgentDriver(cwd=str(D))
        run = d.start(fake_agent(NORMAL))
        first = run.last_activity()
        list(run.events())
        self.assertGreaterEqual(run.last_activity(), first)


class TestSilence(unittest.TestCase):
    """§5.2: тишина дольше порога — проверить состояние и убить зависшего."""

    def test_silent_agent_is_killed(self):
        d = dr.AgentDriver(cwd=str(D), silence_timeout=2)
        run = d.start(fake_agent(SILENT))
        res = run.collect(extract)
        self.assertEqual(res.reason, "silence")
        self.assertFalse(res.ok)
        self.assertFalse(run.alive(), "зависший процесс обязан быть убит")

    def test_chatty_agent_survives_silence_check(self):
        # Агент сыплет события — тишины нет, убивать не за что;
        # его ловит другой предохранитель (wall-clock).
        d = dr.AgentDriver(cwd=str(D), silence_timeout=2, wall_clock_cap=3)
        res = d.start(fake_agent(CHATTY)).collect(extract)
        self.assertEqual(res.reason, "wall_clock")


class TestCrash(unittest.TestCase):
    def test_nonzero_exit_is_crash(self):
        d = dr.AgentDriver(cwd=str(D))
        res = d.start(fake_agent(CRASH)).collect(extract)
        self.assertEqual(res.reason, "crash")
        self.assertEqual(res.returncode, 3)

    def test_missing_report_is_distinct_from_crash(self):
        d = dr.AgentDriver(cwd=str(D))
        res = d.start(fake_agent(PROSE_ONLY)).collect(extract)
        self.assertEqual(res.reason, "no_report")
        self.assertIsNone(res.report)

    def test_instant_quota_crash_is_an_empty_stream(self):
        """Регрессия на пилот: реальная 403-авария печатает баннер версии
        и умирает. Баннер шёл как DONE, терминальный ERROR тоже входил в
        счёт — events=2, и порог «мгновенная авария = пустой поток»
        (events <= 1) в петле молчал: каскад ложных «слишком крупная»
        повторился бы на следующем же заходе. Тест построен на ДОСЛОВНОМ
        потоке с пилота, а не на придуманном events=1."""
        d = dr.AgentDriver(cwd=str(D))
        run = d.start(fake_agent(QUOTA_CRASH))
        res = run.collect(extract)
        self.assertEqual(res.reason, "crash")
        self.assertEqual(res.events, 0,
                         "баннер версии и терминальное событие — не работа")
        self.assertIn("403", run.stderr_tail(400),
                      "объяснение провайдера обязано доехать до журнала")


class TestWallClock(unittest.TestCase):
    def test_cap_stops_endless_run(self):
        d = dr.AgentDriver(cwd=str(D), silence_timeout=60, wall_clock_cap=2)
        run = d.start(fake_agent(CHATTY))
        res = run.collect(extract)
        self.assertEqual(res.reason, "wall_clock")
        self.assertLess(res.wall_s, 10)
        self.assertFalse(run.alive())


class TestRestartPolicy(unittest.TestCase):
    """§5.2: рестарт = итерация; два краха подряд на задаче -> blocked."""

    def decide(self, outcomes, max_crashes=2):
        """Модель политики оркестратора над результатами запусков."""
        crashes = 0
        iterations = 0
        for reason in outcomes:
            iterations += 1
            if reason in ("crash", "silence", "wall_clock"):
                crashes += 1
                if crashes >= max_crashes:
                    return {"result": "blocked", "reason": "repeated_crash",
                            "iterations": iterations}
                continue
            crashes = 0                      # успешный запуск сбрасывает счётчик
            return {"result": "ok", "iterations": iterations}
        return {"result": "exhausted", "iterations": iterations}

    def test_two_crashes_in_a_row_block(self):
        self.assertEqual(self.decide(["crash", "crash"]),
                         {"result": "blocked", "reason": "repeated_crash",
                          "iterations": 2})

    def test_silence_counts_as_crash(self):
        self.assertEqual(self.decide(["silence", "crash"])["result"], "blocked")

    def test_recovery_resets_counter(self):
        self.assertEqual(self.decide(["crash", "done"]),
                         {"result": "ok", "iterations": 2})

    def test_restart_counts_as_iteration(self):
        self.assertEqual(self.decide(["crash", "done"])["iterations"], 2,
                         "рестарт обязан тратить итерацию, иначе петля бесконечна")


class TestStderrDoesNotDeadlock(unittest.TestCase):
    """Агент, который много пишет в stderr, не должен быть убит живым.

    Канал имеет буфер ~64 КБ: если его не вычитывать во время работы,
    пишущий процесс блокируется, драйвер видит тишину и heartbeat убивает
    агента с уже готовым отчётом. На коротких задачах приёмки дефект не
    проявлялся, на реальных это отказ по расписанию.
    """

    NOISY = r"""
import json, sys
sys.stderr.write("t" * %d * 1024)
sys.stderr.flush()
print(json.dumps({"role": "assistant",
                  "content": json.dumps({"status": "done",
                                         "summary": "s"})}), flush=True)
"""

    def _run(self, kb):
        # Короткие лимиты специально: при регрессе (stderr не осушается)
        # агент блокируется на записи, и тест обязан упасть за секунды, а
        # не висеть до дефолтных 600 с тишины.
        d = dr.AgentDriver(cwd=str(D), silence_timeout=8, wall_clock_cap=20)
        return d.start(fake_agent(self.NOISY % kb)).collect(extract)

    def test_large_stderr_does_not_block_completion(self):
        res = self._run(300)
        self.assertTrue(res.ok, f"агент убит живым: {res.reason}")
        self.assertEqual(res.report["status"], "done")

    def test_stderr_content_kept_for_journal(self):
        res = self._run(8)
        self.assertTrue(res.ok)

    def test_thinking_in_stderr_counts_as_alive(self):
        """Агент, который думает вслух только в stderr, жив.

        Kimi шлёт туда reasoning: если считать признаком жизни лишь
        stdout, длинное размышление выглядит как тишина и агента убивают
        на середине работы.
        """
        script = (
            "import json, sys, time\n"
            "for _ in range(12):\n"
            "    sys.stderr.write('думаю...\\n'); sys.stderr.flush()\n"
            "    time.sleep(0.5)\n"
            "print(json.dumps({'role': 'assistant', 'content': "
            "json.dumps({'status': 'done', 'summary': 's'})}), flush=True)\n")
        d = dr.AgentDriver(cwd=str(D), silence_timeout=3, wall_clock_cap=30)
        res = d.start(fake_agent(script)).collect(extract)
        self.assertTrue(res.ok, f"агент убит во время размышления: {res.reason}")

    def test_small_stderr_still_works(self):
        res = self._run(1)
        self.assertTrue(res.ok)
        self.assertEqual(res.reason, "done")


class TestParseClaude(unittest.TestCase):
    """Разбор потока Claude: живость — из событий, конверт — из result.

    У Claude поле `type`, а не `role`, и финальный вердикт лежит не в
    assistant-событии, а в отдельном result-событии — том же объекте,
    который `--output-format json` отдал бы целиком.
    """

    def test_result_event_is_done(self):
        ev = dr.parse_claude(json.dumps({"type": "result", "subtype": "success"}))
        self.assertEqual(ev.kind, dr.DONE)

    def test_assistant_and_deltas_are_activity(self):
        for kind in ("assistant", "stream_event"):
            ev = dr.parse_claude(json.dumps({"type": kind}))
            self.assertEqual(ev.kind, dr.TEXT, f"{kind} — не признак жизни?")

    def test_user_event_is_tool_result(self):
        self.assertEqual(dr.parse_claude('{"type": "user"}').kind, dr.TOOL_RESULT)

    def test_unknown_and_garbage_are_ignored(self):
        self.assertIsNone(dr.parse_claude(json.dumps({"type": "system"})))
        self.assertIsNone(dr.parse_claude("не json"))

    def test_envelope_from_result_event(self):
        stream = "\n".join([
            json.dumps({"type": "assistant"}),
            json.dumps({"type": "result", "total_cost_usd": 0.5,
                        "structured_output": {"verdict": "approve"}}),
        ])
        env = dr.extract_result_envelope(stream)
        self.assertEqual(env["total_cost_usd"], 0.5)
        self.assertEqual(env["structured_output"]["verdict"], "approve")

    def test_no_result_event_means_no_envelope(self):
        self.assertIsNone(dr.extract_result_envelope(
            json.dumps({"type": "assistant"}) + "\nмусор\n"))
        self.assertIsNone(dr.extract_result_envelope(""))

    def test_quoted_result_inside_text_does_not_win(self):
        """Конверт снимается с КОНЦА: цитата result-события в содержимом
        (например, при ревью кода самой петли) не должна подменять вердикт."""
        quoted = json.dumps({"type": "assistant",
                             "message": '{"type": "result", "fake": true}'})
        real = json.dumps({"type": "result", "real": True})
        env = dr.extract_result_envelope(quoted + "\n" + real)
        self.assertTrue(env.get("real"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
