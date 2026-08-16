#!/usr/bin/env python3
"""Наблюдаемость: время, идентификатор прогона, диагностика.

Тесты проверяют не «функция что-то вернула», а те три свойства, ради
которых модуль написан: метки времени сравнимы между прогонами, строки
разных прогонов различимы, а проглоченное исключение оставляет след.
"""
import importlib.util
import json
import logging
import pathlib
import re
import sys
import tempfile
import unittest
from datetime import datetime

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


obs = _load("obs")


class TestTimestamps(unittest.TestCase):
    def test_ts_carries_timezone(self):
        """Наивная метка не сравнима ни с чем: ни с другим хостом, ни с самой
        собой через перевод часов. Смещение обязано быть в строке."""
        parsed = datetime.fromisoformat(obs.now())
        self.assertIsNotNone(parsed.tzinfo,
                             "метка без пояса — та же болезнь, что была")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0, "ожидался UTC")

    def test_ts_sorts_lexicographically(self):
        """Журнал разбирают `sort` и `jq`, а не только datetime: порядок строк
        обязан совпадать с порядком событий."""
        first, second = obs.now(), obs.now()
        self.assertLessEqual(first, second)


class TestRunId(unittest.TestCase):
    def test_run_id_is_time_sortable(self):
        rid = obs._new_run_id()
        self.assertRegex(rid, r"^\d{8}T\d{6}-[0-9a-f]{6}$")

    def test_two_ids_in_same_second_differ(self):
        """Прогоны запускают подряд; совпадение идентификаторов слило бы их
        выборки в одну — ровно та беда, от которой run_id и заведён."""
        ids = {obs._new_run_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_stamp_adds_both_fields(self):
        row = obs.stamp({"kind": "round"})
        self.assertIn("ts", row)
        self.assertEqual(row["run_id"], obs.RUN_ID)

    def test_stamp_does_not_rewrite_existing_time(self):
        """Запись о шаге собирают заранее и дописывают после падения.
        Переписать её время — соврать о том, когда событие случилось."""
        row = obs.stamp({"ts": "2020-01-01T00:00:00+00:00", "kind": "step"})
        self.assertEqual(row["ts"], "2020-01-01T00:00:00+00:00")

    def test_stamp_carries_orchestrator_sha(self):
        """Задача, возвращённая через два дня, исполняется тем кодом, что
        на диске СЕЙЧАС, — без отпечатка нигде не записано каким. Для
        программы замеров это ломает привязку находки к версии петли."""
        self.assertTrue(obs.SWARM_SHA,
                        "оркестратор живёт в git — отпечатка нет почему?")
        row = obs.stamp({"kind": "round"})
        self.assertEqual(row["swarm_sha"], obs.SWARM_SHA)


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(logging.getLogger("swarm").handlers.clear)

    def _rows(self):
        path = self.dir / "log" / obs.LOG_FILE
        if not path.exists():
            return []
        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()]

    def test_swallowed_exception_leaves_a_trace(self):
        """Смысл всего модуля: `except ...: pass` больше не стирает причину."""
        obs.setup(self.dir, level="INFO", force=True)
        log = obs.get_logger("test")
        def boom():
            raise ValueError("почему-то упало")

        try:
            boom()
        except ValueError:
            log.warning("граница деградации", exc_info=True)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("ValueError: почему-то упало", rows[0]["exc"])
        self.assertEqual(rows[0]["run_id"], obs.RUN_ID)

    def test_extra_fields_are_searchable(self):
        """Структурная диагностика: задача ищется полем, а не глазами."""
        obs.setup(self.dir, level="INFO", force=True)
        obs.get_logger("test").warning("что-то", extra={"swarm_task": "a1b2"})
        self.assertEqual(self._rows()[0]["task"], "a1b2")

    def test_debug_is_silent_by_default(self):
        """Диагностика не должна стоить денег и места, пока её не попросили."""
        obs.setup(self.dir, level="INFO", force=True)
        obs.get_logger("test").debug("подробность")
        self.assertEqual(self._rows(), [])

    def test_debug_appears_when_asked(self):
        obs.setup(self.dir, level="DEBUG", force=True)
        obs.get_logger("test").debug("подробность")
        self.assertEqual(len(self._rows()), 1)

    def test_repeat_setup_does_not_double_handlers(self):
        """Модули грузятся по путям в разном порядке; повторная настройка не
        имеет права умножать записи."""
        obs.setup(self.dir, level="INFO", force=True)
        obs.setup(self.dir, level="INFO")
        obs.setup(self.dir, level="INFO")
        obs.get_logger("test").warning("однажды")
        self.assertEqual(len(self._rows()), 1)

    def test_works_without_state_dir(self):
        """Без каталога состояния предупреждения всё равно не глотаются."""
        obs.setup(None, level="INFO", force=True)
        handlers = logging.getLogger("swarm").handlers
        self.assertTrue(handlers, "не осталось ни одного обработчика")

    def test_rows_are_jsonl_like_the_journal(self):
        """Один формат на каталог: иначе человеку и инструментам нужен
        второй парсер."""
        obs.setup(self.dir, level="INFO", force=True)
        obs.get_logger("test").warning("строка")
        row = self._rows()[0]
        self.assertEqual(set(row) >= {"ts", "run_id", "level", "logger", "msg"},
                         True)
        self.assertRegex(row["ts"], r"^\d{4}-\d\d-\d\dT[\d:.]+\+00:00$")


class TestStopwatch(unittest.TestCase):
    def test_uses_monotonic_clock(self):
        """Стенные часы могут пойти назад в середине прогона и дать
        отрицательную длительность в метриках."""
        src = (ROOT_DIR / "obs.py").read_text(encoding="utf-8")
        self.assertIn("time.monotonic()", src)
        self.assertNotIn("time.time()", src)

    def test_reports_non_negative(self):
        self.assertGreaterEqual(obs.timer().s(), 0.0)


class TestJournalUsesObs(unittest.TestCase):
    """Регресс-страховка: модули, пишущие журнал, обязаны брать время из
    одного места. Возврат к `strftime` вернул бы наивные метки."""

    def test_no_naive_timestamps_left(self):
        offenders = []
        for path in sorted(ROOT_DIR.glob("*.py")):
            if path.name == "obs.py":
                continue          # он и заменил этот вызов, называя его в доке
            src = path.read_text(encoding="utf-8")
            for match in re.finditer(r'strftime\("%Y-%m-%dT', src):
                line = src[:match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
        self.assertEqual(offenders, [],
                         "наивная метка времени вернулась в журнал")


if __name__ == "__main__":
    unittest.main()
