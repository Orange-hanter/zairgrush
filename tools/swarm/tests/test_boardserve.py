#!/usr/bin/env python3
"""Тесты живого сервера доски (boardserve.py).

Разметку страницы проверяет test_board.py — здесь предмет другой:
контракт самого сервера. Код ответа, ETag, отказ пересобирать страницу
без изменений (payload несёт метку времени сборки — лишняя пересборка
выглядела бы для клиента как новый контент), откат на эфемерный порт при
занятом, деградация до последней удачной страницы при провале рендера.
"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"

spec = importlib.util.spec_from_file_location("boardserve", ROOT_DIR / "boardserve.py")
bs = importlib.util.module_from_spec(spec)
sys.modules["boardserve"] = bs
spec.loader.exec_module(bs)


def make_root(tmp, tasks=None, goal="цель"):
    """Минимальный `.swarm/`, как у test_board.py: только то, что читает
    board.collect() — без него сервер нечего было бы отдавать."""
    root = pathlib.Path(tmp)
    swarm = root / ".swarm"
    (swarm / "log").mkdir(parents=True)
    (swarm / "tasks.json").write_text(
        json.dumps({"goal": goal, "tasks": tasks or []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (swarm / "metrics.jsonl").write_text("", encoding="utf-8")
    (swarm / "log" / "run.jsonl").write_text("", encoding="utf-8")
    return root


def _get(url, etag=None):
    """GET через stdlib urllib. urlopen кидает HTTPError на ЛЮБОЙ код вне
    200-299 (в том числе 304) — тесту нужен код ответа, а не исключение,
    поэтому оно разбирается здесь один раз, а не в каждом тесте."""
    headers = {"If-None-Match": etag} if etag else {}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — адрес всегда http://127.0.0.1, схема не пользовательская
    try:
        res = urllib.request.urlopen(req, timeout=5)  # noqa: S310 — та же проверка выше
        return res.status, res.read().decode("utf-8"), res.headers.get("ETag")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), e.headers.get("ETag")


class BoardServeCase(unittest.TestCase):
    """Общий фикстур: временный .swarm и список серверов на остановку —
    поднятый и не остановленный ThreadingHTTPServer держит порт занятым
    для следующего теста и печатает исключения из фонового потока."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._servers = []
        self.addCleanup(self._stop_servers)

    def _stop_servers(self):
        for server in self._servers:
            server.stop()

    def start(self, **kw):
        # port=0: эфемерный, чтобы параллельные прогоны тестов и живой
        # `swarm run` разработчика на 7433 не сталкивались лбами.
        server = bs.BoardServer(self.root, port=0, **kw)
        server.start()
        self._servers.append(server)
        return server


class TestBasicGet(BoardServeCase):
    def test_root_returns_html_with_embedded_data(self):
        server = self.start()
        status, body, etag = _get(server.url)
        self.assertEqual(status, 200)
        self.assertIn('id="data"', body)
        self.assertIsNotNone(etag)


class TestEtagRoundTrip(BoardServeCase):
    def test_matching_if_none_match_gives_304(self):
        server = self.start()
        _status, _body, etag = _get(server.url)
        status2, body2, _etag2 = _get(server.url, etag=etag)
        self.assertEqual(status2, 304)
        self.assertEqual(body2, "")


class TestStateChangeIsVisible(BoardServeCase):
    def test_task_title_change_reaches_the_page_and_etag_changes(self):
        # min_rebuild_s=0: убираем троттлинг, иначе тест зависел бы от
        # реального времени между двумя GET.
        server = self.start(min_rebuild_s=0)
        (self.root / ".swarm" / "tasks.json").write_text(
            json.dumps(
                {
                    "goal": "цель",
                    "tasks": [{"id": "t1", "title": "старое имя", "status": "pending"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _status1, body1, etag1 = _get(server.url)
        self.assertIn("старое имя", body1)

        (self.root / ".swarm" / "tasks.json").write_text(
            json.dumps(
                {
                    "goal": "цель",
                    "tasks": [{"id": "t1", "title": "новое имя", "status": "pending"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _status2, body2, etag2 = _get(server.url)
        self.assertIn("новое имя", body2)
        self.assertNotIn("старое имя", body2)
        self.assertNotEqual(etag1, etag2)


class TestNoSpuriousRebuild(BoardServeCase):
    def test_two_gets_without_change_share_the_same_etag(self):
        # Страница несёт метку времени сборки: пересобери сервер её без
        # причины — и опрашивающий клиент увидел бы «новый» контент там,
        # где ничего не произошло.
        server = self.start()
        _s1, _b1, etag1 = _get(server.url)
        _s2, _b2, etag2 = _get(server.url)
        self.assertEqual(etag1, etag2)


class TestExecutorStreamsAreNotAStateChange(BoardServeCase):
    def test_growing_executor_stream_keeps_the_etag(self):
        """Поток исполнителя дописывается на каждый токен весь раунд
        напролёт, а доска его содержимое не читает — рост потока не имеет
        права выглядеть для сервера изменением состояния, иначе страница
        пересобиралась бы каждый цикл поллинга (докстринг _state_key
        обещал это с первого дня, но код обещание не выполнял)."""
        server = self.start(min_rebuild_s=0)
        _s1, _b1, etag1 = _get(server.url)

        stream = self.root / ".swarm" / "log" / "t1-i1-executor.jsonl"
        stream.write_text('{"kind": "token"}\n' * 50, encoding="utf-8")
        _s2, _b2, etag2 = _get(server.url)
        self.assertEqual(etag1, etag2)

        # Контрольная точка честности теста: журнал ту же машинерию
        # обязан триггерить — иначе равенство ETag выше доказывало бы
        # лишь то, что сервер вообще ничего не пересобирает.
        run = self.root / ".swarm" / "log" / "run.jsonl"
        run.write_text('{"kind": "round", "task": "t1"}\n', encoding="utf-8")
        _s3, _b3, etag3 = _get(server.url)
        self.assertNotEqual(etag1, etag3)


class TestPortFallback(BoardServeCase):
    def test_busy_port_falls_back_to_ephemeral_and_both_respond(self):
        first = self.start()
        busy_port = int(first.url.rsplit(":", 1)[1].rstrip("/"))
        second = bs.BoardServer(self.root, port=busy_port)
        second.start()
        self._servers.append(second)

        self.assertNotEqual(first.url, second.url)
        status1, _b1, _e1 = _get(first.url)
        status2, _b2, _e2 = _get(second.url)
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)


class TestUnknownPath(BoardServeCase):
    def test_unknown_path_is_404(self):
        server = self.start()
        status, _body, _etag = _get(server.url + "no-such-path")
        self.assertEqual(status, 404)


class TestRenderFailureDegrades(BoardServeCase):
    """Наблюдение не имеет права моргнуть при первой же кривой записи в
    .swarm/ — прогон живёт часами, и один противоречивый снимок на
    середине записи не должен на секунду показать 500 вместо того, что
    уже было хорошо собрано."""

    def test_broken_render_after_a_good_get_still_serves_the_last_page(self):
        server = self.start(min_rebuild_s=0)
        status1, body1, etag1 = _get(server.url)
        self.assertEqual(status1, 200)

        original_render = bs.board_mod.render

        def boom(_board):
            msg = "рендер сломан нарочно"
            raise RuntimeError(msg)

        bs.board_mod.render = boom
        try:
            # Состояние обязано измениться, иначе кэш сочтёт страницу
            # актуальной и вообще не попытается пересобрать — деградация
            # тогда осталась бы непроверенной.
            (self.root / ".swarm" / "tasks.json").write_text(
                json.dumps({"goal": "цель после поломки", "tasks": []}),
                encoding="utf-8",
            )
            status2, body2, etag2 = _get(server.url)
        finally:
            bs.board_mod.render = original_render

        self.assertEqual(status2, 200)
        self.assertEqual(body2, body1)
        self.assertEqual(etag2, etag1)

    def test_never_a_good_render_gives_500(self):
        server = self.start()
        original_render = bs.board_mod.render

        def boom(_board):
            msg = "рендер сломан с самого начала"
            raise RuntimeError(msg)

        bs.board_mod.render = boom
        try:
            status, _body, _etag = _get(server.url)
        finally:
            bs.board_mod.render = original_render
        self.assertEqual(status, 500)


class TestBoardOpenDisabled(unittest.TestCase):
    """`_board_open` — граница между петлёй и живым сервером (§ cli.py):
    выключенная конфигом доска обязана не завести сервер вовсе, а не
    завести и тут же его игнорировать."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        cli_spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
        self.cli = importlib.util.module_from_spec(cli_spec)
        sys.modules["cli"] = self.cli
        cli_spec.loader.exec_module(self.cli)
        self.cli._BOARD_SERVER = None
        self.addCleanup(self._stop_if_started)

    def _stop_if_started(self):
        if self.cli._BOARD_SERVER is not None:
            self.cli._BOARD_SERVER.stop()

    def test_board_open_false_returns_no_url_and_starts_no_server(self):
        out, url = self.cli._board_open(self.root, {"board_open": False})
        self.assertEqual(out, self.root / ".swarm" / "board.html")
        self.assertIsNone(url)
        self.assertIsNone(self.cli._BOARD_SERVER)

    def test_live_board_default_off_starts_no_server(self):
        out, url = self.cli._board_open(self.root, {})
        self.assertEqual(out, self.root / ".swarm" / "board.html")
        self.assertIsNone(url)
        self.assertIsNone(self.cli._BOARD_SERVER)


if __name__ == "__main__":
    unittest.main()
