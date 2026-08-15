"""Наблюдаемость петли: время, идентификатор прогона, диагностика.

Разделение, ради которого модуль и существует: у петли ДВА потока записи,
и путать их нельзя.

1. **Журнал** (`.swarm/log/run.jsonl`, `metrics.jsonl`) — факты о работе:
   какой раунд, какой вердикт, сколько стоило. Это контракт: его читают
   доска, `swarm ab` и человек, разбирающий прогон. Append-only, без
   уровней — факт либо случился, либо нет.
2. **Диагностика** (`.swarm/log/diag.jsonl`) — то, что нужно ЧИНЯЩЕМУ:
   трассировки, отладочные подробности, предупреждения о деградации.
   Уровни, ротация, по умолчанию не мешает.

Пока их не разделяли, диагностика просто ТЕРЯЛАСЬ: `except Exception:
pass` съедал трассировку, а писать её в журнал было нельзя — она не факт
о работе, а подробность о поломке.

Что здесь решено раз и навсегда для обоих потоков:

- **Время в UTC со смещением.** Было `time.strftime("%Y-%m-%dT%H:%M:%S")`
  — наивное локальное. Такие метки не сортируются через переход на зимнее
  время (час повторяется), не сравниваются с метками агентов и не
  говорят читателю, в каком поясе он находится.
- **`run_id` в каждой строке.** Прогоны дописываются в ОДИН файл, и без
  идентификатора разобрать «это находки сегодняшнего прогона или
  вчерашнего» нельзя ничем. Для замера рук ревьюера (§8.1) это прямо
  ломает выборку: строки разных прогонов сливаются в одну кучу.
"""
import json
import logging
import logging.handlers
import os
import pathlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any

LOG_FILE = "diag.jsonl"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
ENV_LEVEL = "SWARM_LOG_LEVEL"
ENV_RUN_ID = "SWARM_RUN_ID"


def now() -> str:
    """Метка времени журнала: ISO-8601 в UTC со смещением."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_run_id() -> str:
    """Идентификатор прогона: сортируемая по времени метка плюс случайный
    хвост. Время впереди, чтобы `sort` по строке давал хронологию; хвост —
    чтобы два прогона, стартовавшие в одну секунду, не слиплись."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


# Один идентификатор на процесс. Через переменную окружения его можно
# передать вложенному вызову (планировщик запускается отдельным процессом)
# — тогда строки обоих процессов сойдутся в одну выборку.
RUN_ID = os.environ.get(ENV_RUN_ID) or _new_run_id()
os.environ.setdefault(ENV_RUN_ID, RUN_ID)


def stamp(row: dict[str, Any]) -> dict[str, Any]:
    """Проставить в строку журнала время и прогон, не затирая заданное.

    `setdefault`, а не присваивание: строку могут собирать заранее (запись
    о шаге, восстановленная после падения), и переписать её время значило
    бы соврать о том, когда событие случилось.
    """
    row.setdefault("ts", now())
    row.setdefault("run_id", RUN_ID)
    return row


class JsonlFormatter(logging.Formatter):
    """Диагностика тем же форматом, что и журнал: одна строка — один JSON.

    Читать два формата в одном каталоге — лишняя работа для человека и
    лишний парсер для инструментов. Трассировка кладётся полем, а не
    хвостом текста: `grep` по уровню и модулю остаётся возможным.
    """

    def format(self, record: logging.LogRecord) -> str:
        row: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
                          .isoformat(timespec="milliseconds"),
            "run_id": RUN_ID,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            row["exc"] = self.formatException(record.exc_info)
        # Поля, переданные через extra=, — то, ради чего диагностика
        # структурная: `task`, `round`, `path` ищутся, а не вычитываются
        # глазами из текста сообщения.
        for key, value in record.__dict__.items():
            if key.startswith("swarm_"):
                row[key[6:]] = value
        return json.dumps(row, ensure_ascii=False)


def setup(swarm_dir: pathlib.Path | None = None, level: str | None = None,
          *, force: bool = False) -> None:
    """Настроить диагностику. Вызывается один раз на процесс.

    Идемпотентна намеренно: модули петли грузятся через importlib по
    путям и могут инициализироваться в разном порядке; повторный вызов не
    должен множить обработчики, иначе каждая запись попадёт в файл
    столько раз, сколько раз загрузился модуль.

    Без каталога состояния настраивается только вывод в stderr: писать
    файл некуда, а глотать предупреждения из-за этого нельзя.
    """
    root = logging.getLogger("swarm")
    # Состояние берётся из самого logging, а не из флага модуля: модули
    # грузятся через importlib по путям, и «свой» флаг у каждой копии
    # оказался бы свой, а обработчики — общими и удвоенными.
    if root.handlers and not force:
        return
    root.handlers.clear()
    root.setLevel(getattr(logging, (level or os.environ.get(ENV_LEVEL)
                                    or "INFO").upper(), logging.INFO))
    # Человеку в терминал — только то, что требует внимания: петля и так
    # печатает свой ход через ui(), и дублировать его в stderr значит
    # утопить настоящие предупреждения в потоке нормальной работы.
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    if swarm_dir is not None:
        log_dir = pathlib.Path(swarm_dir) / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Ротация обязательна: диагностика пишется на каждый вызов агента,
        # а прогон живёт часами. Журнал фактов растёт медленно и его режут
        # по смыслу, а не по размеру, — здесь наоборот.
        handler = logging.handlers.RotatingFileHandler(
            log_dir / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS,
            encoding="utf-8")
        handler.setFormatter(JsonlFormatter())
        root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Логгер модуля. Имя — короткое, без пути: `swarm.loop`, `swarm.agents`."""
    return logging.getLogger(f"swarm.{name}")


def timer() -> "Stopwatch":
    """Секундомер для полей `dur_s`. Монотонные часы, а не стенные:
    перевод времени в середине прогона не должен давать отрицательную
    длительность."""
    return Stopwatch()


class Stopwatch:
    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    def s(self, digits: int = 1) -> float:
        return round(time.monotonic() - self._start, digits)
