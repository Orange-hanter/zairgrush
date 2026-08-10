#!/usr/bin/env python3
"""Слой запуска агентов (§6.0): AgentDriver с heartbeat и рестартом.

Заменяет `subprocess.run(...)` раннера, который не умел ничего из §5.2:
не видел, что агент замолчал, не отличал «жив, но думает» от «завис»,
не сохранял незакоммиченную работу при крахе.

Ключевые свойства:
- события обоих CLI нормализуются в один поток Event — конечный автомат
  не знает, Kimi на том конце или Claude;
- `last_activity` обновляется на каждом событии: тишина дольше
  `silence_timeout` — повод ПРОВЕРИТЬ состояние, а не слепо убивать
  (жив ли процесс, движется ли вывод);
- отдельный жёсткий `wall_clock_cap`: агент, исправно сыплющий события по
  кругу, иначе крутится часами;
- kill не роняет петлю: возвращается RunResult с причиной, решение
  принимает конечный автомат.
"""
import json
import queue
import subprocess
import threading
import time

TEXT, TOOL_CALL, TOOL_RESULT, USAGE, DONE, ERROR = (
    "text", "tool_call", "tool_result", "usage", "done", "error")


class Event:
    __slots__ = ("kind", "payload", "ts")

    def __init__(self, kind, payload=None):
        self.kind = kind
        self.payload = payload or {}
        self.ts = time.time()

    def __repr__(self):
        return f"Event({self.kind}, {list(self.payload)[:3]})"


class RunResult:
    __slots__ = ("reason", "report", "events", "wall_s", "returncode")

    def __init__(self, reason, report=None, events=0, wall_s=0.0, returncode=None):
        self.reason = reason          # done | silence | wall_clock | crash | error
        self.report = report          # извлечённый structured output
        self.events = events
        self.wall_s = wall_s
        self.returncode = returncode

    @property
    def ok(self):
        return self.reason == "done" and self.report is not None

    def __repr__(self):
        return (f"RunResult({self.reason}, report={bool(self.report)}, "
                f"events={self.events}, {self.wall_s:.1f}s)")


def parse_kimi(line):
    """stream-json Kimi -> Event. Урок SMOKE-1: финальный JSON лежит в
    content последнего assistant-события, а не в последней строке потока."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    role = ev.get("role")
    if role == "assistant":
        if ev.get("tool_calls"):
            names = [t.get("function", {}).get("name") for t in ev["tool_calls"]]
            return Event(TOOL_CALL, {"tools": names})
        return Event(TEXT, {"content": ev.get("content") or ""})
    if role == "tool":
        return Event(TOOL_RESULT, {"content": (ev.get("content") or "")[:200]})
    if role == "meta":
        return Event(DONE, {"session_id": ev.get("session_id")})
    return None


class Run:
    """Один запуск агента. Поток-читатель складывает события в очередь,
    основной поток ждёт их с таймаутом — так тишина отличима от активности."""

    def __init__(self, cmd, cwd, parser, silence_timeout, wall_clock_cap):
        self.cmd = cmd
        self.parser = parser
        self.silence_timeout = silence_timeout
        self.wall_clock_cap = wall_clock_cap
        self._q = queue.Queue()
        self._last_activity = time.time()
        self._started = time.time()
        self._lines = []
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self._err = []
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        # stderr обязан вычитываться ПАРАЛЛЕЛЬНО, а не после завершения.
        # Kimi шлёт туда thinking и прогресс: на ~64 КБ (размер буфера
        # канала) процесс блокируется на записи, драйвер видит тишину и
        # heartbeat убивает живого агента с готовым отчётом. На коротких
        # задачах приёмки это не проявлялось.
        self._err_reader = threading.Thread(target=self._drain_err, daemon=True)
        self._err_reader.start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self._q.put(line)
        finally:
            self._q.put(None)          # сигнал конца потока

    def _drain_err(self):
        """Осушение stderr: содержимое копим для журнала, канал держим пустым."""
        try:
            for line in self.proc.stderr:
                self._err.append(line)
                # stderr — тоже признак жизни: агент думает вслух
                self._last_activity = time.time()
                if len(self._err) > 2000:
                    del self._err[:1000]
        except (OSError, ValueError):
            pass

    def last_activity(self):
        return self._last_activity

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        if self.alive():
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self._close_pipes()

    def _close_pipes(self):
        """Без этого длинный прогон течёт дескрипторами: каждый убитый
        агент оставляет открытыми stdout/stderr."""
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except (OSError, ValueError):
                pass

    def events(self):
        """Поток нормализованных событий. Прерывается по тишине или
        wall-clock; сам решение не принимает — только сообщает причину."""
        while True:
            if time.time() - self._started > self.wall_clock_cap:
                self.kill()
                yield Event(ERROR, {"reason": "wall_clock"})
                return
            try:
                line = self._q.get(timeout=1.0)
            except queue.Empty:
                silent_for = time.time() - self._last_activity
                if silent_for > self.silence_timeout:
                    # §5.2: тишина — повод проверить состояние, а не убивать.
                    if not self.alive():
                        yield Event(ERROR, {"reason": "crash",
                                            "returncode": self.proc.returncode})
                        return
                    self.kill()
                    yield Event(ERROR, {"reason": "silence",
                                        "silent_for": round(silent_for, 1)})
                    return
                continue
            if line is None:
                rc = self.proc.wait()
                if rc != 0:
                    yield Event(ERROR, {"reason": "crash", "returncode": rc})
                else:
                    yield Event(DONE, {"returncode": rc})
                return
            self._last_activity = time.time()
            self._lines.append(line)
            ev = self.parser(line)
            if ev is not None:
                yield ev

    def collect(self, extract_report):
        """Прогнать поток до конца и собрать результат."""
        count = 0
        reason = "done"
        for ev in self.events():
            count += 1
            if ev.kind == ERROR:
                reason = ev.payload.get("reason", "error")
                break
        self._close_pipes()
        report = extract_report("".join(self._lines))
        if reason == "done" and report is None:
            reason = "no_report"
        return RunResult(reason, report, count, time.time() - self._started,
                         self.proc.returncode)

    def stderr_tail(self, limit=2000):
        """Kimi шлёт thinking/прогресс в stderr — в журнал для человека.

        Читаем из накопленного осушителем: обращаться к каналу здесь
        нельзя — он уже вычитан до конца параллельным потоком.
        """
        return ("".join(self._err))[-limit:]


class AgentDriver:
    """Единый интерфейс запуска (§6.0). Конкретный CLI — деталь конфига."""

    def __init__(self, cwd, silence_timeout=600, wall_clock_cap=1800):
        self.cwd = cwd
        self.silence_timeout = silence_timeout
        self.wall_clock_cap = wall_clock_cap

    def start(self, cmd, parser=parse_kimi):
        return Run(cmd, self.cwd, parser, self.silence_timeout, self.wall_clock_cap)
