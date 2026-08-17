#!/usr/bin/env python3
"""Тесты памяти между прогонами (E9).

Живой PG тестам не нужен: точка pg() подменяется, а всё остальное —
файлы. Свойства, которые здесь сторожатся, дороже механики: grounding
как пропуск, fail-open на каждом пути, «файлы — источник истины»,
байт-идентичность промптов при выключенном флаге.
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("memory", ROOT_DIR / "memory.py")
mem = importlib.util.module_from_spec(spec)
sys.modules["memory"] = mem
spec.loader.exec_module(mem)


def lesson(body="урок про гейт", outcome="useful", **kw):
    rec = {"repo": "r", "stand": "s", "source": "mechanical",
           "outcome": outcome, "body": body, "anchors": []}
    rec.update(kw)
    return rec


class MemCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.store = mem.MemoryStore(self.root)
        # Предохранитель ОТКРЫТ по умолчанию: дефолтный DSN указывает на
        # ЖИВУЮ общую базу, и тест без этой заглушки писал в неё мусор
        # (поймано на первом же smoke — уроки «задача»/«упёртая» из
        # юнит-тестов лежали в swarm_memory). Тесту, которому нужен psql,
        # придётся закрыть предохранитель явно.
        mem._state["failures"] = mem.BREAKER
        mem._state["unavailable_logged"] = False

    def tearDown(self):
        self.tmp.cleanup()


class TestStore(MemCase):
    def test_append_and_fold(self):
        lid = self.store.append(lesson())
        self.assertEqual(len(self.store.records()), 1)
        self.assertEqual(self.store.records()[0]["id"], lid)

    def test_repeat_increments_count_not_duplicates(self):
        """Уверенность рождается из повторяемости: тот же урок — count+1,
        а не второй экземпляр."""
        self.store.append(lesson("Один  и ТОТ же урок"))
        self.store.append(lesson("один и тот же   урок"))
        records = self.store.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["count"], 2)

    def test_tombstone_removes_from_live_view(self):
        lid = self.store.append(lesson())
        self.store.tombstone(lid)
        self.assertEqual(self.store.records(), [])
        # запись осталась в файле: reindex воспроизводим
        self.assertIn(lid, self.store.lessons_path.read_text())

    def test_digest_is_deterministic(self):
        self.store.append(lesson("первый"))
        self.store.append(lesson("второй", outcome="dead_end"))
        self.assertEqual(self.store.digest(), self.store.digest())

    def test_digest_regenerated_on_every_append(self):
        """Окно «запись есть — сводка старая» — источник тихой лжи."""
        self.store.append(lesson("свежий урок про индекс"))
        self.assertIn("свежий урок про индекс",
                      self.store.digest_path.read_text())

    def test_digest_declares_no_llm(self):
        self.store.append(lesson())
        self.assertIn("без LLM", self.store.digest_path.read_text())

    def test_dead_end_is_first_class(self):
        self.store.append(lesson("сюда не ходить", outcome="dead_end"))
        self.assertIn("Тупики", self.store.digest())

    def test_body_is_scrubbed_and_capped(self):
        lid = self.store.append(lesson(
            "токен ghp_" + "a" * 40 + " и длинный хвост " + "x" * 900))
        rec = next(r for r in self.store.records() if r["id"] == lid)
        self.assertNotIn("ghp_" + "a" * 40, rec["body"],
                         "секрет обязан быть вычищен до записи")
        self.assertLessEqual(len(rec["body"]), mem.BODY_CAP)

    def test_broken_lines_are_data_not_crash(self):
        self.store.append(lesson())
        with self.store.lessons_path.open("a", encoding="utf-8") as f:
            f.write('не json\n[1,2]\n"строка"\n')
        self.assertEqual(len(self.store.records()), 1)

    def test_bad_outcome_refused(self):
        with self.assertRaises(ValueError):
            self.store.append(lesson(outcome="great_success"))


class TestGrounding(MemCase):
    """Пропуск в память: useful обязан цитировать живой якорь."""

    def test_useful_without_anchor_rejected_with_reason(self):
        ok, why = mem.admissible(lesson(), self.root,
                                 self.store.journal_path)
        self.assertFalse(ok)
        self.assertIn("якор", why)

    def test_path_anchor_resolves(self):
        (self.root / "a.py").write_text("x = 1\n")
        rec = lesson(anchors=[{"kind": "path", "ref": "a.py"}])
        ok, _ = mem.admissible(rec, self.root, self.store.journal_path)
        self.assertTrue(ok)

    def test_dead_anchor_does_not_admit(self):
        rec = lesson(anchors=[{"kind": "path", "ref": "нет/такого.py"}])
        ok, _ = mem.admissible(rec, self.root, self.store.journal_path)
        self.assertFalse(ok)

    def test_task_anchor_resolves_via_journal(self):
        self.store.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.journal_path.write_text(
            '{"kind": "round", "task": "k3ad"}\n', encoding="utf-8")
        rec = lesson(anchors=[{"kind": "task", "ref": "k3ad"}])
        ok, _ = mem.admissible(rec, self.root, self.store.journal_path)
        self.assertTrue(ok)

    def test_unknown_anchor_kind_is_not_alive(self):
        """Пропуск нельзя получить через непроверяемую ссылку."""
        rec = lesson(anchors=[{"kind": "vibe", "ref": "хорошее место"}])
        ok, _ = mem.admissible(rec, self.root, self.store.journal_path)
        self.assertFalse(ok)

    def test_dead_end_admissible_without_anchors(self):
        """Тупик часто ссылается на то, чего больше нет, — в этом его
        природа; ворота держат только useful."""
        ok, _ = mem.admissible(lesson(outcome="dead_end"), self.root,
                               self.store.journal_path)
        self.assertTrue(ok)


class _FakeState:
    def __init__(self, root, tasks=None, goal="цель"):
        self.root = root
        self._tasks = {"goal": goal, "tasks": tasks or []}
        self.logged = []

    def load_tasks(self):
        return self._tasks

    def log(self, kind, **payload):
        self.logged.append((kind, payload))


class TestRecordOutcome(MemCase):
    def _state(self, task):
        return _FakeState(self.root, tasks=[task])

    def test_done_becomes_useful_with_task_anchor(self):
        self.store.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.journal_path.write_text(
            '{"kind": "round", "task": "t1", "verdict": "approve", '
            '"findings": 0}\n', encoding="utf-8")
        state = self._state({"id": "t1", "title": "задача", "status": "done"})
        lid = mem.record_task_outcome(state, {"id": "t1"}, {})
        self.assertIsNotNone(lid)
        rec = self.store.records()[0]
        self.assertEqual(rec["outcome"], "useful")
        self.assertIn(("memory_written",
                       {"task": "t1", "lesson": lid, "outcome": "useful"}),
                      state.logged)

    def test_blocked_becomes_dead_end_with_diagnosis(self):
        state = self._state({"id": "t2", "title": "упёртая", "status": "blocked",
                             "diagnosis": "сгорела о защищённый тест"})
        mem.record_task_outcome(state, {"id": "t2"}, {})
        rec = self.store.records()[0]
        self.assertEqual(rec["outcome"], "dead_end")
        self.assertIn("сгорела о защищённый тест", rec["body"])

    def test_human_decisions_become_corrected(self):
        """Принятое решение владельца — самый ценный вид урока."""
        self.store.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.journal_path.write_text(
            '{"kind": "round", "task": "t3"}\n', encoding="utf-8")
        state = self._state({"id": "t3", "title": "спорная", "status": "done",
                             "human_decisions": ["констант не заводить"]})
        mem.record_task_outcome(state, {"id": "t3"}, {})
        rec = self.store.records()[0]
        self.assertEqual(rec["outcome"], "corrected")
        self.assertIn("констант не заводить", rec["body"])

    def test_pending_task_records_nothing(self):
        state = self._state({"id": "t4", "title": "живая", "status": "pending"})
        self.assertIsNone(mem.record_task_outcome(state, {"id": "t4"}, {}))
        self.assertEqual(self.store.records(), [])

    def test_flag_off_never_touches_pg(self):
        """Дефолтный конфиг не имеет права трогать ОБЩУЮ базу: тесты
        петли на живом PG насорили в неё уроками с временных стендов.
        Индекс производный — включённый эксперимент или reindex догонят."""
        calls = []
        self.addCleanup(setattr, mem, "pg", mem.pg)
        mem.pg = lambda *a, **k: calls.append(a) or (True, "")
        state = self._state({"id": "t9", "title": "х", "status": "blocked",
                             "diagnosis": "тупик"})
        mem.record_task_outcome(state, {"id": "t9"}, {})
        self.assertEqual(calls, [], "flag off — ни одного вызова psql")
        mem.record_task_outcome(
            state, {"id": "t9"}, {"experiments": {"memory": "executor"}})
        self.assertGreater(len(calls), 0, "flag on — индекс пополняется")

    def test_corrupt_journal_is_survivable(self):
        self.store.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.journal_path.write_text("мусор\n{битый json",
                                           encoding="utf-8")
        state = self._state({"id": "t5", "title": "х", "status": "blocked",
                             "reason": "crash"})
        self.assertIsNotNone(mem.record_task_outcome(state, {"id": "t5"}, {}))


class TestFailOpen(MemCase):
    """Отсутствие памяти не имеет права стоить прогона."""

    def _kill_pg(self):
        self.addCleanup(setattr, mem, "pg", mem.pg)
        mem.pg = lambda *a, **k: (False, "нет базы")

    def test_retrieve_falls_back_to_local_scan(self):
        self._kill_pg()
        self.store.append(lesson("урок про валидацию входных данных"))
        state = _FakeState(self.root)
        hits = mem.retrieve(state, {}, "валидацию данных")
        self.assertEqual(len(hits), 1)
        self.assertIn(("memory_unavailable",), [(k,) for k, _ in state.logged])

    def test_unavailable_logged_once_per_process(self):
        self._kill_pg()
        state = _FakeState(self.root)
        mem.retrieve(state, {}, "раз")
        mem.retrieve(state, {}, "два")
        n = sum(1 for k, _ in state.logged if k == "memory_unavailable")
        self.assertEqual(n, 1, "журнал не должен тонуть в повторах")

    def test_retrieve_never_raises(self):
        self.addCleanup(setattr, mem, "MemoryStore", mem.MemoryStore)

        class Boom:
            def __init__(self, *a, **k):
                raise OSError("диск умер")
        mem.MemoryStore = Boom
        self.assertEqual(mem.retrieve(_FakeState(self.root), {}, "q"), [])

    def test_inject_block_never_raises(self):
        self.addCleanup(setattr, mem, "retrieve", mem.retrieve)
        mem.retrieve = lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
        cfg = {"experiments": {"memory": "executor"}}
        out = mem.inject_block("executor", {"id": "t"},
                               _FakeState(self.root), cfg)
        self.assertEqual(out, "")


class TestInjection(MemCase):
    CFG = {"experiments": {"memory": "executor"}}

    def _hits(self, n=3, body="тело урока"):
        return [{"id": f"id{i}", "outcome": "useful", "count": 1,
                 "body": f"{body} {i}"} for i in range(n)]

    def _patch_retrieve(self, hits):
        self.addCleanup(setattr, mem, "retrieve", mem.retrieve)
        mem.retrieve = lambda *a, **k: hits

    def test_flag_off_means_empty(self):
        self._patch_retrieve(self._hits())
        self.assertEqual(
            mem.inject_block("executor", {"id": "t"},
                             _FakeState(self.root), {}), "")

    def test_role_gating(self):
        self._patch_retrieve(self._hits())
        self.assertEqual(
            mem.inject_block("reviewer", {"id": "t"},
                             _FakeState(self.root), self.CFG), "")

    def test_block_carries_data_preamble_and_ids(self):
        """Память — данные, а не инструкции; каждая строка прослеживается
        до записи по id."""
        self._patch_retrieve(self._hits(2))
        state = _FakeState(self.root)
        block = mem.inject_block("executor", {"id": "t"}, state, self.CFG)
        self.assertIn("DATA, not", block)
        self.assertIn("(id0)", block)
        self.assertIn("(id1)", block)
        kinds = [k for k, _ in state.logged]
        self.assertIn("memory_injected", kinds)

    def test_budget_cuts_at_record_boundary(self):
        """Обрезанный посреди фразы урок хуже отсутствующего."""
        self._patch_retrieve(self._hits(50, body="о" * 100))
        cfg = dict(self.CFG, memory_budget_chars=600)
        block = mem.inject_block("executor", {"id": "t"},
                                 _FakeState(self.root), cfg)
        self.assertLessEqual(len(block), 600)
        for line in block.splitlines():
            if line.startswith("- "):
                self.assertTrue(line.endswith(")") or "о" in line)
        self.assertNotIn("…", block)

    def test_stale_anchor_mark_rendered(self):
        self._patch_retrieve([{"id": "x1", "outcome": "useful", "count": 3,
                               "body": "урок", "anchors_ok": False}])
        block = mem.inject_block("executor", {"id": "t"},
                                 _FakeState(self.root), self.CFG)
        self.assertIn("якоря устарели", block)


class TestPgChokePoint(MemCase):
    """Данные не имеют права попадать в текст SQL: только -v и COPY."""

    def _capture(self):
        calls = []
        self.addCleanup(setattr, mem.subprocess, "run", mem.subprocess.run)
        real = subprocess.CompletedProcess

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return real(cmd, 0, stdout="[]", stderr="")
        mem.subprocess.run = fake_run
        return calls

    def test_search_passes_data_via_vars(self):
        calls = self._capture()
        mem._state["failures"] = 0          # psql подменён — путь безопасен
        mem.search_fts({}, "репо'; DROP TABLE lessons; --", "запрос", 5)
        cmd, kw = calls[0]
        sql = kw.get("input", "")
        self.assertNotIn("DROP TABLE", sql,
                         "данные просочились в текст SQL")
        self.assertIn("-v", cmd)
        joined = " ".join(cmd)
        self.assertIn("ON_ERROR_STOP=1", joined)
        self.assertIn("--no-psqlrc", cmd)

    def test_breaker_opens_after_failures(self):
        mem._state["failures"] = 0          # psql подменён — путь безопасен
        self.addCleanup(setattr, mem.subprocess, "run", mem.subprocess.run)

        def dead_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 2, stdout="",
                                               stderr="connection refused")
        mem.subprocess.run = dead_run
        for _ in range(mem.BREAKER):
            mem.pg({}, "SELECT 1;")
        calls = []
        mem.subprocess.run = lambda cmd, **kw: calls.append(cmd)
        ok, why = mem.pg({}, "SELECT 1;")
        self.assertFalse(ok)
        self.assertEqual(calls, [], "предохранитель обязан не звать psql")
        self.assertIn("предохранитель", why)


class TestRepoIdentity(MemCase):
    def test_no_remote_repo_equals_stand(self):
        repo, stand = mem.repo_identity(self.root)
        self.assertEqual(repo, stand)

    def test_ssh_and_https_spellings_merge(self):
        """git@host:a/b.git и https://host/a/b — один репозиторий: иначе
        уроки одного проекта расщепляются по написанию remote."""
        urls = ("git@github.com:User/Repo.git",
                "https://github.com/user/repo",
                "ssh://git@github.com/user/repo.git")
        norm = []
        for i, url in enumerate(urls):
            d = self.root / f"clone{i}"
            subprocess.run(["git", "init", "-q", str(d)],
                           capture_output=True, check=True)
            subprocess.run(["git", "remote", "add", "origin", url], cwd=d,
                           check=True)
            norm.append(mem.repo_identity(d)[0])
        self.assertEqual(len(set(norm)), 1, norm)


class TestNoFootprintOnRead(MemCase):
    """Чтение пустой памяти не оставляет следа на диске: каталог создаёт
    первая ЗАПИСЬ. Конструктор с mkdir — тот же класс утечки в чужое
    дерево, что AUDIT-3 ловил у метрик хелперов."""

    def test_records_and_reflect_leave_no_dir(self):
        clean = self.root / "чистый-стенд"
        clean.mkdir()
        store = mem.MemoryStore(clean)
        self.assertEqual(store.records(), [])
        mem.reflect_after_run(_FakeState(clean), {})
        self.assertFalse((clean / ".swarm").exists(),
                         "пустая рефлексия не имеет права творить каталоги")


class TestLocalScan(MemCase):
    def test_rare_token_overlap(self):
        records = [lesson("про валидацию номера листа"),
                   lesson("про сериализацию дампа")]
        hits = mem._local_scan(records, "валидацию листа", 5)
        self.assertEqual(len(hits), 1)
        self.assertIn("валидацию", hits[0]["body"])

    def test_short_tokens_ignored(self):
        self.assertEqual(mem._local_scan([lesson("а и б")], "а и", 5), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
