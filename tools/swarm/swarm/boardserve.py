"""Живая доска: локальный HTTP-сервер, который отдаёт board.html без
перезагрузки страницы у человека в браузере.

До этого модуля актуальность доски держалась на `location.reload()`
каждые 15 с: работающий приём, но неприятный — открытая карточка
схлопывалась, поиск и прокрутка сбрасывались ровно тогда, когда человек
читал находку. Обновление тем не менее нужно: страница — снимок на
момент сборки, и без него человек смотрит в прошлое, не зная об этом.
Решение — отдавать СВЕЖУЮ страницу по тому же адресу и подменять DOM на
стороне клиента (см. `board.JS`), а не файл на диске: файлом продолжает
владеть петля (`Loop.refresh_board`), сервер его никогда не пишет — два
писателя в один путь дают гонку без выигрыша (сервер и так строит
страницу в памяти на каждый запрос).

Сборка — тем же кодом, что и статический файл (`board.collect` +
`board.render`): третьей копии разметки здесь нет и не будет, разошлись
бы при первой же правке одного из мест.
"""
import hashlib
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см.
# board.py, state.py и остальные — приём тот же, четвёртая копия не нужна).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import board as board_mod  # noqa: E402 — каталог добавлен строкой выше
import obs  # noqa: E402 — каталог добавлен строкой выше

log = obs.get_logger("boardserve")

# Имя, mtime в наносекундах, размер — не содержимое файла: хэшировать
# сотни КБ исполнительских логов на каждый GET дороже, чем поверить, что
# не тронутый файл не изменился. Три поля ловят и правку, и досрочный
# rename поверх старого имени того же размера (наносекунды почти никогда
# не совпадут случайно).
_StateKey = tuple[tuple[str, int, int], ...]


class BoardServer:
    """HTTP-сервер живой доски поверх одного каталога `.swarm/`.

    Один экземпляр — один прогон: сервер знает свой `root` и отдаёт по
    `/` актуальную страницу, пересобирая её только когда файлы состояния
    действительно изменились (см. `_state_key`). Это не оптимизация ради
    оптимизации: страница несёт метку времени сборки, и пересборка без
    изменений выглядела бы для опрашивающего клиента как новый контент —
    он бы обновлял DOM вхолостую на каждый цикл поллинга.
    """

    def __init__(self, root: str | pathlib.Path, port: int = 7433,
                 min_rebuild_s: float = 2.0) -> None:
        self._root = pathlib.Path(root)
        self._port = port
        self._min_rebuild_s = min_rebuild_s
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Кэш последней успешной сборки — под замком: обработчики бегут в
        # пуле потоков ThreadingHTTPServer, и без замка два одновременных
        # GET могли бы пересобрать страницу дважды или увидеть половинку
        # записи.
        self._lock = threading.Lock()
        self._page: str | None = None
        self._etag: str | None = None
        self._key: _StateKey | None = None
        self._last_build = 0.0

    @property
    def url(self) -> str:
        """Адрес живой доски. Спрашивать его до `start()` — ошибка
        вызывающего кода, а не деградация: без сервера адреса не бывает."""
        if self._httpd is None:
            msg = "живая доска ещё не запущена — вызови start()"
            raise RuntimeError(msg)
        return f"http://127.0.0.1:{self._httpd.server_port}/"

    def start(self) -> None:
        """Поднять сервер в фоновом потоке.

        Порт может быть занят — своим же процессом с прошлого прогона,
        который не закрылся, или чужим сервисом. Наблюдение не имеет
        права остановить прогон из-за этого: при занятом порте берём
        эфемерный (0 -> ядро ОС выдаёт свободный), а не падаем.
        """
        handler = _make_handler(self)
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", self._port), handler)
        except OSError:
            log.warning("порт %s занят — беру случайный свободный",
                        self._port, exc_info=True)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd = httpd
        # daemon=True: поток сервера не имеет права держать процесс живым
        # после того, как петля закончила работу и не вызвала stop() —
        # такой поток пережил бы `swarm run` навсегда.
        thread = threading.Thread(target=httpd.serve_forever,
                                  name="board-server", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """Остановить сервер. Идемпотентна: повторный вызов — не ошибка."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _state_key(self) -> _StateKey:
        """Ключ состояния `.swarm/` — плоско, без рекурсии в подкаталоги
        `log/`, кроме одного уровня: сырые потоки исполнителя (`*-executor
        .jsonl`) там тоже лежат и меняются на каждый токен, но доска их не
        читает — включать их в ключ значило бы пересобирать страницу от
        событий, которых на ней никогда не будет."""
        entries: list[tuple[str, int, int]] = []
        swarm = self._root / ".swarm"
        for d in (swarm, swarm / "log"):
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.suffix not in (".json", ".jsonl"):
                    continue
                # Обещание из докстринга — кодом: поток исполнителя растёт
                # на каждый токен, и с ним в ключе страница пересобиралась
                # бы каждый цикл поллинга весь раунд напролёт. Появление
                # нового потока доска увидит через run.jsonl того же раунда.
                if p.name.endswith("executor.jsonl"):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    # Файл исчез между iterdir и stat (ротация диагностики,
                    # параллельная запись) — не повод падать, просто он не
                    # войдёт в ключ этого снимка.
                    continue
                entries.append((str(p.relative_to(swarm)),
                               st.st_mtime_ns, st.st_size))
        return tuple(entries)

    def _serve_page(self) -> tuple[str, str]:
        """Актуальная (страница, ETag), пересобирая по необходимости.

        Исключение из пересборки уходит наверх, только если хорошей
        сборки не было ни разу, — тогда отдавать нечего и обработчик
        превратит его в 500. Есть кэш — значит, есть что вернуть.
        """
        with self._lock:
            self._rebuild_if_stale()
            if self._page is None or self._etag is None:
                msg = "живая доска: ни одной удачной сборки"
                raise RuntimeError(msg)
            return self._page, self._etag

    def _rebuild_if_stale(self) -> None:
        """Обновить кэш, если состояние изменилось и вышел `min_rebuild_s`.

        Провал сборки — не повод стереть последнюю рабочую версию:
        `.swarm/` пишется по частям, и чтение во время записи (не под
        замком петли — тот замок только против гонки записи-vs-записи)
        может на секунду оказаться противоречивым (см. board.collect).
        """
        key = self._state_key()
        now = time.monotonic()
        changed = key != self._key
        # min_rebuild_s — не троттлинг ради троттлинга: `.swarm/` может
        # дописываться пачкой записей (несколько kind в одном раунде), и
        # пересборка на КАЖДУЮ была бы той же лишней работой, ради
        # экономии которой кэш и существует.
        due = (now - self._last_build) >= self._min_rebuild_s
        if self._page is not None and not (changed and due):
            return
        try:
            board = board_mod.collect(self._root)
            page = board_mod.render(board)
        except Exception:
            log.warning("сборка живой доски провалилась", exc_info=True)
            if self._page is None:
                raise
            return
        self._page = page
        self._etag = hashlib.sha1(page.encode("utf-8")).hexdigest()  # noqa: S324 — не крипто, а отпечаток для ETag
        self._key = key
        self._last_build = now


def _make_handler(server: BoardServer) -> type[BaseHTTPRequestHandler]:
    """Фабрика обработчика: `server` приходит через замыкание, а не через
    `self.server` — там сидит объект `ThreadingHTTPServer`, у которого
    для нашего сервера нет штатного места, и заводить ему новый атрибут
    подклассом ради одного поля не с руки."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            try:
                page, etag = server._serve_page()  # noqa: SLF001 — Handler и BoardServer одна пара, не чужой объект
            except Exception:
                log.warning("живая доска: ни одной удачной сборки — отдаю 500",
                            exc_info=True)
                self._send_text(500, "доска ещё не собралась ни разу — "
                                     "подробности в .swarm/log/diag.jsonl")
                return
            if self.headers.get("If-None-Match") == f'"{etag}"':
                self.send_response(304)
                self.end_headers()
                return
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, code: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — сигнатура базового класса
            # Живая доска дёргает `/` раз в 5 с на каждое открытое окно —
            # построчный access-log в stderr прогона тонул бы в этом шуме,
            # а искать в нём нужно диагностику самой петли.
            pass

    return Handler
