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
        mem.breaker_state["failures"] = mem.BREAKER
        mem.breaker_state["unavailable_logged"] = False

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
        self.asked = []
        self._policies = []
        self._questions = []

    def load_tasks(self):
        return self._tasks

    def log(self, kind, **payload):
        self.logged.append((kind, payload))

    def ask(self, task, kind, question, **ctx):
        self.asked.append((task, kind, question))
        self._questions.append({"qid": f"q{len(self.asked):03d}",
                                "task": task, "qkind": kind,
                                "question": question, "status": "open"})
        return self._questions[-1]["qid"]

    def questions(self, only_open=False):
        return list(self._questions)

    def policies(self):
        return list(self._policies)


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
        mem.breaker_state["failures"] = 0          # psql подменён — путь безопасен
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
        mem.breaker_state["failures"] = 0          # psql подменён — путь безопасен
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


class TestVectorLayer(MemCase):
    """Вектор — сеть дополнительного охвата ПОСЛЕ FTS, не слияние рангов."""

    def test_vec_literal_is_pgvector_syntax(self):
        self.assertEqual(mem._vec_literal([0.25, -1.0]),
                         "[0.250000,-1.000000]")

    def test_hybrid_appends_only_fresh_hits(self):
        self.addCleanup(setattr, mem, "search_fts", mem.search_fts)
        self.addCleanup(setattr, mem, "search_vec", mem.search_vec)
        self.addCleanup(setattr, mem.helpers, "embed_text",
                        mem.helpers.embed_text)
        mem.search_fts = lambda *a, **k: [{"id": "f1", "body": "х",
                                           "outcome": "useful", "count": 1}]
        mem.search_vec = lambda *a, **k: [
            {"id": "f1", "body": "х", "outcome": "useful", "count": 1},
            {"id": "v2", "body": "y", "outcome": "useful", "count": 1}]
        mem.helpers.embed_text = lambda text, model: [0.1, 0.2]
        hits = mem.retrieve(_FakeState(self.root),
                            {"memory_embed_model": "m"}, "запрос")
        self.assertEqual([h["id"] for h in hits], ["f1", "v2"],
                         "FTS первым, векторные — только новые id")

    def test_embedder_down_leaves_fts_only(self):
        self.addCleanup(setattr, mem, "search_fts", mem.search_fts)
        self.addCleanup(setattr, mem.helpers, "embed_text",
                        mem.helpers.embed_text)
        mem.search_fts = lambda *a, **k: [{"id": "f1", "body": "х",
                                           "outcome": "useful", "count": 1}]
        mem.helpers.embed_text = lambda text, model: None
        hits = mem.retrieve(_FakeState(self.root),
                            {"memory_embed_model": "m"}, "запрос")
        self.assertEqual([h["id"] for h in hits], ["f1"],
                         "лежащий эмбеддер — не событие, FTS живёт")


class TestAnchorDecay(MemCase):
    """Распад по ре-валидации: отпечаток ловит «файл есть, но переписан»."""

    def test_fp_mismatch_kills_path_anchor(self):
        target = self.root / "a.py"
        target.write_text("x = 1\n")
        anchor = {"kind": "path", "ref": "a.py",
                  "fp": mem.file_fp(target)}
        self.assertTrue(mem.resolve_anchor(anchor, self.root,
                                           self.store.journal_path))
        target.write_text("x = 2  # переписан\n")
        self.assertFalse(mem.resolve_anchor(anchor, self.root,
                                            self.store.journal_path),
                         "урок о прежнем содержимом — уже не правда")

    def test_symbol_anchor_resolves_via_git_grep(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "m.py").write_text("def rare_symbol_name():\n    pass\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        anchor = {"kind": "symbol", "ref": "rare_symbol_name"}
        self.assertTrue(mem.resolve_anchor(anchor, self.root,
                                           self.store.journal_path))
        self.assertFalse(mem.resolve_anchor(
            {"kind": "symbol", "ref": "nonexistent_symbol_zz"},
            self.root, self.store.journal_path))

    def test_commit_symbols_parsed_from_hunk_headers(self):
        raw = ("@@ -1,2 +1,3 @@ def compute_totals(self):\n"
               "@@ -9,1 +10,1 @@ class Ledger:\n"
               "@@ -20,1 +21,1 @@ def compute_totals(self):\n")
        self.addCleanup(setattr, mem.subprocess, "run", mem.subprocess.run)
        mem.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout=raw, stderr="")
        self.assertEqual(mem._commit_symbols(self.root, "abc"),
                         ["compute_totals", "Ledger"])

    def test_reflect_moves_dead_anchor_lessons_to_unlinked(self):
        target = self.root / "b.py"
        target.write_text("y = 1\n")
        self.store.append(lesson(
            "урок о файле b", anchors=[{"kind": "path", "ref": "b.py",
                                        "fp": mem.file_fp(target)}]))
        target.write_text("y = 2\n")
        state = _FakeState(self.root)
        mem.reflect_after_run(state, {})
        self.assertIn("Отвязанные", self.store.digest_path.read_text())
        reflected = next(p for k, p in state.logged
                         if k == "memory_reflect")
        self.assertEqual(reflected["unlinked"], 1)


class TestVectorRepin(MemCase):
    """Перепинить размерность вправе только reindex: первая версия
    отказывала и ему, и подсказка «нужен reindex» водила по кругу."""

    def _pg_with_pinned(self, pinned):
        calls = []
        self.addCleanup(setattr, mem, "pg", mem.pg)

        def fake(config, sql, sql_vars=None, stdin=None):
            calls.append(sql)
            if "SELECT value FROM meta" in sql:
                return True, pinned
            return True, ""
        mem.pg = fake
        return calls

    def test_mismatch_without_repin_refuses(self):
        calls = self._pg_with_pinned("1024")
        self.assertFalse(mem.ensure_vector({}, 2048))
        self.assertFalse(any("DROP COLUMN" in c for c in calls))

    def test_reindex_repin_drops_and_repins(self):
        calls = self._pg_with_pinned("1024")
        self.assertTrue(mem.ensure_vector({}, 2048, repin=True))
        self.assertTrue(any("DROP COLUMN" in c for c in calls))


class TestNormsBlock(MemCase):
    """Persona ревьюера: только принятые решения, никаких команд молчать."""

    CFG = {"experiments": {"memory": "all"}}

    def _seed(self):
        self.store.append(lesson("констант в конфиге не заводить",
                                 outcome="corrected"))
        self.store.append(lesson("обычный урок", outcome="dead_end"))

    def test_only_corrected_and_norm_tagged(self):
        self._seed()
        block = mem.norms_block(_FakeState(self.root), self.CFG, {"id": "t"})
        self.assertIn("констант в конфиге", block)
        self.assertNotIn("обычный урок", block,
                         "тупики — исполнителю, ревьюеру — только нормы")

    def test_block_never_orders_silence(self):
        """Измерено (§4.2): команда «не выноси findings» роняет recall.
        Блок обязан явно требовать сообщать всё."""
        self._seed()
        block = mem.norms_block(_FakeState(self.root), self.CFG, {"id": "t"})
        self.assertIn("СООБЩАЙ ВСЁ", block)
        self.assertNotIn("не выноси", block.lower())

    def test_executor_mode_gives_reviewer_nothing(self):
        self._seed()
        cfg = {"experiments": {"memory": "executor"}}
        self.assertEqual(
            mem.norms_block(_FakeState(self.root), cfg, {"id": "t"}), "")


class TestRolesAreSwitchedOneAtATime(MemCase):
    """Правило программы: один фактор на прогон. Флаг памяти — имя РОЛИ,
    и включённая роль не имеет права тянуть за собой остальные."""

    def test_planner_alone_leaves_executor_and_reviewer_dry(self):
        cfg = {"experiments": {"memory": "planner"}}
        self.assertTrue(mem.enabled_for(cfg, "planner"))
        self.assertFalse(mem.enabled_for(cfg, "executor"))
        self.assertFalse(mem.enabled_for(cfg, "reviewer"))

    def test_all_opens_every_role(self):
        cfg = {"experiments": {"memory": "all"}}
        for role in ("planner", "executor", "reviewer"):
            self.assertTrue(mem.enabled_for(cfg, role), role)

    def test_off_is_the_default(self):
        for cfg in ({}, {"experiments": {}}, {"experiments": {"memory": "off"}}):
            self.assertFalse(mem.enabled_for(cfg, "planner"), cfg)

    def test_planner_block_is_built_for_the_planner_role(self):
        self.addCleanup(setattr, mem, "retrieve", mem.retrieve)
        mem.retrieve = lambda *a, **k: [
            {"id": "b1", "outcome": "corrected", "count": 1,
             "body": "границы задачи расширены на реестр правил"}]
        cfg = {"experiments": {"memory": "planner"}}
        task = {"id": "*", "title": "новое правило ERC"}
        block = mem.inject_block("planner", task, _FakeState(self.root), cfg)
        self.assertIn("Project memory", block)
        self.assertIn("реестр правил", block)
        self.assertEqual(
            mem.inject_block("executor", task, _FakeState(self.root), cfg), "",
            "включённый планировщик не смеет включать исполнителя")


class TestFpPromotion(MemCase):
    """Промоция подавлений — только через человека: память не смеет
    затыкать ревьюера сама."""

    def _journal(self, rows):
        self.store.journal_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        self.store.journal_path.write_text(
            "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")

    SUPPRESSED = {"kind": "policy_suppressed", "task": "t", "items": [
        {"policy": "p001", "severity": "minor",
         "issue": "нет release note"}]}
    POLICY = {"kind": "policy", "pid": "p001",
              "text": "release notes не трогаем", "match": ["release"]}

    def test_threshold_two(self):
        self._journal([self.POLICY, self.SUPPRESSED])
        state = _FakeState(self.root)
        self.assertEqual(mem.fp_candidates(state), [],
                         "единичное подавление — ещё не норма")
        self._journal([self.POLICY, self.SUPPRESSED, self.SUPPRESSED])
        cands = mem.fp_candidates(state)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["count"], 2)
        self.assertEqual(cands[0]["match"], ["release"])

    def test_active_policy_excludes_candidate(self):
        self._journal([self.POLICY, self.SUPPRESSED, self.SUPPRESSED])
        state = _FakeState(self.root)
        state._policies = [{"pid": "p009", "match": ["release note"]}]
        self.assertEqual(mem.fp_candidates(state), [],
                         "действующая политика уже покрывает")

    def test_reflect_files_question_once(self):
        self._journal([self.POLICY, self.SUPPRESSED, self.SUPPRESSED])
        self.store.append(lesson("якорь дайджеста"))
        state = _FakeState(self.root)
        mem.reflect_after_run(state, {})
        mem.reflect_after_run(state, {})
        fp_questions = [a for a in state.asked if a[1] == "fp_promotion"]
        self.assertEqual(len(fp_questions), 1,
                         "открытый вопрос не дублируется")
        self.assertIn("swarm policy add", fp_questions[0][2])


class TestConsolidationSection(MemCase):
    def test_digest_includes_labeled_llm_section(self):
        self.store.append(lesson("урок раз"))
        self.store.write_consolidation("тема — суть (abc123)")
        text = self.store.digest_path.read_text()
        self.assertIn("Сводка хелпера (LLM)", text)
        self.assertIn("тема — суть (abc123)", text)
        self.assertEqual(self.store.digest(), self.store.digest(),
                         "детерминизм дайджеста сохранён")


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
        hits = mem.local_scan(records, "валидацию листа", 5)
        self.assertEqual(len(hits), 1)
        self.assertIn("валидацию", hits[0]["body"])

    def test_short_tokens_ignored(self):
        self.assertEqual(mem.local_scan([lesson("а и б")], "а и", 5), [])


class TestIndexEnabled(MemCase):
    """Право на общий PG-индекс: эксперимент ИЛИ явный opt-in стенда."""

    def test_default_is_off(self):
        self.assertFalse(mem.index_enabled({}))

    def test_experiment_flag_enables(self):
        self.assertTrue(mem.index_enabled(
            {"experiments": {"memory": "executor"}}))

    def test_auto_enables_without_experiment(self):
        self.assertTrue(mem.index_enabled({"memory_index": "auto"}))

    def test_explicit_manual_stays_off(self):
        self.assertFalse(mem.index_enabled({"memory_index": "manual"}))


class TestSync(MemCase):
    """Автоиндексация: досыпка строк и векторов до состояния файлов.

    Родилась из замера: за весь пилот OpenRouter не увидел НИ ОДНОГО
    вызова эмбеддера — вектора появлялись только от ручного reindex."""

    CFG = {"memory_embed_model": "openrouter:m@3"}

    def _wire(self, missing_ids, upsert_ok=True):
        calls = []

        def fake_pg(config, sql, params=None):
            calls.append((sql, params or {}))
            if "INSERT INTO lessons" in sql and not upsert_ok:
                return False, "err"
            if "SELECT id FROM lessons" in sql:
                return True, "\n".join(missing_ids())
            return True, ""

        self.addCleanup(setattr, mem, "pg", mem.pg)
        mem.pg = fake_pg
        self.addCleanup(setattr, mem, "ensure_schema", mem.ensure_schema)
        mem.ensure_schema = lambda cfg: True
        self.addCleanup(setattr, mem, "ensure_vector", mem.ensure_vector)
        mem.ensure_vector = lambda cfg, dim, repin=False: True
        embedded = []
        self.addCleanup(setattr, mem.helpers, "embed_text",
                        mem.helpers.embed_text)
        mem.helpers.embed_text = (
            lambda text, model: embedded.append(text) or [0.1, 0.2])
        return calls, embedded

    def test_embeds_only_rows_without_vector(self):
        self.store.append(lesson("первый урок про стеш"))
        lid2 = self.store.append(lesson("второй совсем другой про квоту"))
        _calls, embedded = self._wire(lambda: [lid2])
        got = mem.sync(self.CFG, self.store, "репо", "стенд")
        self.assertEqual(got, (2, 1, 0))
        self.assertEqual(len(embedded), 1)
        self.assertIn("квоту", embedded[0])

    def test_cap_limits_embeddings_and_reports_remainder(self):
        ids = [self.store.append(lesson(f"урок номер {i} про {'х' * i}"))
               for i in range(1, 4)]
        _calls, embedded = self._wire(lambda: ids)
        got = mem.sync(self.CFG, self.store, "репо", "стенд", embed_cap=2)
        self.assertEqual(got, (3, 2, 1))
        self.assertEqual(len(embedded), 2)

    def test_without_embed_model_rows_only(self):
        self.store.append(lesson("урок без модели"))
        calls, embedded = self._wire(lambda: ["никогда"])
        got = mem.sync({}, self.store, "репо", "стенд")
        self.assertEqual(got, (1, 0, 0))
        self.assertEqual(embedded, [])
        self.assertFalse(any("embedding IS NULL" in sql for sql, _p in calls),
                         "без модели нечего искать среди безвекторных")

    def test_pg_down_is_none_not_crash(self):
        self.store.append(lesson("урок при лежащем PG"))
        self._wire(list)
        mem.ensure_schema = lambda cfg: False
        self.assertIsNone(mem.sync(self.CFG, self.store, "репо", "стенд"))

    def test_upsert_failure_is_none(self):
        self.store.append(lesson("урок при битой досыпке"))
        self._wire(list, upsert_ok=False)
        self.assertIsNone(mem.sync(self.CFG, self.store, "репо", "стенд"))


class TestAutoIndexTriggers(MemCase):
    """memory_index="auto": индекс досыпается сам на терминальном исходе."""

    def _state(self, task):
        return _FakeState(self.root, tasks=[task])

    def test_outcome_triggers_sync_and_journals_vectors(self):
        seen = []
        self.addCleanup(setattr, mem, "sync", mem.sync)
        mem.sync = lambda *a, **k: seen.append(a) or (5, 2, 1)
        state = self._state({"id": "t9", "title": "х", "status": "blocked",
                             "diagnosis": "тупик"})
        mem.record_task_outcome(state, {"id": "t9"},
                                {"memory_index": "auto"})
        self.assertEqual(len(seen), 1, "auto обязан звать sync")
        self.assertIn(("memory_synced",
                       {"rows": 5, "vectors": 2, "unembedded": 1}),
                      state.logged)

    def test_rows_only_sync_stays_out_of_journal(self):
        """Нечего рассказывать — нет записи: вектора не добавлялись."""
        self.addCleanup(setattr, mem, "sync", mem.sync)
        mem.sync = lambda *a, **k: (5, 0, 0)
        state = self._state({"id": "t9", "title": "х", "status": "blocked",
                             "diagnosis": "тупик"})
        mem.record_task_outcome(state, {"id": "t9"},
                                {"memory_index": "auto"})
        self.assertFalse(any(k == "memory_synced" for k, _p in state.logged))

    def test_default_config_never_syncs(self):
        self.addCleanup(setattr, mem, "sync", mem.sync)
        mem.sync = lambda *a, **k: self.fail("дефолт не имеет права в PG")
        state = self._state({"id": "t9", "title": "х", "status": "blocked",
                             "diagnosis": "тупик"})
        mem.record_task_outcome(state, {"id": "t9"}, {})


class TestFtsQueryIsOrNotAnd(unittest.TestCase):
    """Замер на пилоте 2026-08-23: по НАСТОЯЩИМ запросам FTS отдавал ровно
    ноль строк, потому что `websearch_to_tsquery` соединяет слова через И,
    а в запрос уезжала спецификация задачи целиком. Условие «в одном уроке
    встретились все её слова» не выполнялось никогда, и весь поиск
    держался на векторе — при том что дизайн обещает обратное."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "mempg", ROOT_DIR / "mempg.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mempg"] = mod
        spec.loader.exec_module(mod)
        self.fts = mod.fts_query

    def test_words_are_joined_by_or(self):
        self.assertEqual(self.fts("корпус эталон"), "корпус or эталон")

    def test_short_words_are_dropped(self):
        """Слово короче четырёх букв стеммер сотрёт всё равно; в запросе
        оно только удлиняет разбор."""
        self.assertEqual(self.fts("в шкафу и на корпус"), "шкафу or корпус")

    def test_long_query_is_capped(self):
        text = " ".join(f"слово{i:02}" for i in range(40))
        self.assertEqual(len(self.fts(text).split(" or ")), 12)

    def test_duplicates_collapse_and_order_survives(self):
        self.assertEqual(self.fts("корпус эталон корпус"),
                         "корпус or эталон")

    def test_punctuation_and_paths_do_not_break_the_query(self):
        """Запрос строится из спеки задачи, где есть пути и знаки: они не
        имеют права превратиться в синтаксис websearch."""
        out = self.fts("ERC-03: клемма zeus/crates/zeus-erc/src/rules/*.rs")
        self.assertNotIn("*", out)
        self.assertNotIn(":", out)
        self.assertIn("erc-03", out)

    def test_empty_text_yields_empty_query(self):
        """Пустой запрос обязан остаться пустым: `search_fts` подставит
        исходную строку сам, а «or» из ничего сломал бы разбор."""
        self.assertEqual(self.fts(""), "")
        self.assertEqual(self.fts("и в на"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
