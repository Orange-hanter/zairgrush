#!/usr/bin/env python3
"""Канал доставки промпта (NXT-006): промпт не едет в argv никогда.

Дефект: `kimi -p <промпт>`, `claude -p <промпт>` и `zcode --prompt
<промпт>` клали промпт в argv целиком. argv связан ARG_MAX (~1 МБ на
macOS), и промпт с диффом задачи в 3.7 млн символов ронял СПАВН
процесса с E2BIG — до всякой модели и без строки в журнале.

Контракт: claude получает промпт через stdin (документированный канал
режима -p), kimi и zcode — через временный файл (указатель + Read у
kimi, `--attach` у zcode), и ни один argv не содержит промпта.
"""
import contextlib
import json
import pathlib
import re
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "swarm"))

import driver as dr  # noqa: E402
import engines  # noqa: E402

# Пилотный размер, пробивший ARG_MAX: 3.7 млн символов в argv — E2BIG.
HUGE = "ж" * 3_700_000


def _path_from_pointer(pointer):
    m = re.search(r"(/\S*swarm-prompt-\S+\.txt)", pointer)
    return pathlib.Path(m.group(1)) if m else None


def _extract_report(stream):
    """Тот же приём, что в test_driver: последнее assistant-событие
    с JSON в content."""
    report = None
    for line in stream.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("role") == "assistant":
            with contextlib.suppress(ValueError):
                report = json.loads(ev.get("content") or "")
    return report


class TestDeliveryPacking(unittest.TestCase):
    """Упаковка промпта по движку."""

    def test_claude_gets_stdin_and_an_empty_argv_slot(self):
        with engines.prompt_delivery("claude", HUGE) as dlv:
            self.assertEqual(dlv.tokens, ())
            self.assertEqual(dlv.stdin, HUGE)

    def test_kimi_gets_a_pointer_to_a_temp_file(self):
        """Piped stdin до модели kimi не доходит (проверено пробой) —
        поэтому файл и указатель с просьбой прочитать его инструментом."""
        with engines.prompt_delivery("kimi", HUGE) as dlv:
            self.assertIsNone(dlv.stdin)
            self.assertEqual(len(dlv.tokens), 1)
            path = _path_from_pointer(dlv.tokens[0])
            self.assertIsNotNone(path, "указатель не назвал файл")
            self.assertEqual(path.read_text(encoding="utf-8"), HUGE,
                             "файл обязан нести промпт байт-в-байт")
        self.assertFalse(path.exists(), "временный файл пережил вызов")

    def test_zcode_attaches_the_file(self):
        with engines.prompt_delivery("zcode", HUGE) as dlv:
            self.assertIsNone(dlv.stdin)
            self.assertIn("--attach", dlv.tokens)
            path = pathlib.Path(dlv.tokens[dlv.tokens.index("--attach") + 1])
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), HUGE)
            self.assertIn(str(path), dlv.tokens[0])
        self.assertFalse(path.exists(), "временный файл пережил вызов")

    def test_temp_file_is_removed_even_on_error(self):
        with self.assertRaises(RuntimeError), \
                engines.prompt_delivery("kimi", HUGE) as dlv:
            path = _path_from_pointer(dlv.tokens[0])
            raise RuntimeError("вызов упал")
        self.assertFalse(path.exists())


class TestArgvStaysSmall(unittest.TestCase):
    """Ни один движок не получает промпт в argv — при любом размере."""

    def test_no_engine_puts_the_prompt_in_argv(self):
        for engine in ("kimi", "claude", "zcode"):
            with self.subTest(engine=engine):
                with engines.prompt_delivery(engine, HUGE) as dlv:
                    argv = engines.executor_argv(engine, "m", dlv, {})
                total = sum(len(a) for a in argv)
                self.assertLess(total, 10_000,
                                f"argv движка {engine} несёт промпт")
                self.assertNotIn(HUGE[:1000], "".join(argv))


class TestArgmaxIsTheWall(unittest.TestCase):
    """Доказательство стены: тот же текст аргументом — E2BIG от ядра,
    каналом — доезжает до процесса."""

    def test_huge_argv_is_refused_by_the_kernel(self):
        with self.assertRaises(OSError, msg="ARG_MAX не сработал — "
                                     "тест перестал что-то доказывать"):
            subprocess.run([sys.executable, "-c", "pass", HUGE],
                           check=False)

    def test_small_argv_spawns_fine(self):
        r = subprocess.run([sys.executable, "-c", "pass", "короткий"],
                           check=False)
        self.assertEqual(r.returncode, 0)


class TestStdinChannelEndToEnd(unittest.TestCase):
    """Промпт пилотного размера через поток-кормилец драйвера."""

    ECHO_LEN = r"""
import json, sys
data = sys.stdin.read()
print(json.dumps({"role": "assistant",
                  "content": json.dumps({"status": "done",
                                         "summary": str(len(data))})}),
      flush=True)
"""

    def test_huge_prompt_reaches_the_process_via_stdin(self):
        drv = dr.AgentDriver(cwd="/", silence_timeout=30, wall_clock_cap=60)
        run = drv.start([sys.executable, "-c", self.ECHO_LEN],
                        parser=dr.parse_kimi, stdin_text=HUGE)
        result = run.collect(_extract_report)
        self.assertEqual(result.reason, "done")
        self.assertEqual((result.report or {}).get("summary"),
                         str(len(HUGE)),
                         "процесс получил не весь промпт через stdin")

    def test_without_stdin_text_the_channel_stays_closed(self):
        """Прежнее поведение обязано остаться: без stdin_text процесс
        получает DEVNULL и читает пустоту, а не терминал оператора."""
        drv = dr.AgentDriver(cwd="/", silence_timeout=30, wall_clock_cap=60)
        run = drv.start([sys.executable, "-c", self.ECHO_LEN],
                        parser=dr.parse_kimi)
        result = run.collect(_extract_report)
        self.assertEqual((result.report or {}).get("summary"), "0")


if __name__ == "__main__":
    unittest.main()
