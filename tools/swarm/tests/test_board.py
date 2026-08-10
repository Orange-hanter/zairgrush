#!/usr/bin/env python3
"""Тесты доски прогона (board.py).

Доска — единственное окно человека в прогон, и читает она файлы, которые
пишутся аварийно, по кусочкам и разными версиями оркестратора. Поэтому
главное требование — не красота, а живучесть: любое усечённое или
устаревшее состояние `.swarm/` должно давать страницу, а не traceback.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("board", ROOT_DIR / "board.py")
bd = importlib.util.module_from_spec(spec)
sys.modules["board"] = bd
spec.loader.exec_module(bd)


def make_root(tmp, tasks=None, goal="цель", metrics=(), journal=()):
    """Минимальный `.swarm/`: задачи, метрики, журнал."""
    root = pathlib.Path(tmp)
    swarm = root / ".swarm"
    (swarm / "log").mkdir(parents=True)
    (swarm / "tasks.json").write_text(
        json.dumps({"goal": goal, "tasks": tasks or []}, ensure_ascii=False),
        encoding="utf-8")
    (swarm / "metrics.jsonl").write_text(
        "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in metrics),
        encoding="utf-8")
    (swarm / "log" / "run.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in journal),
        encoding="utf-8")
    return root


class TestCollectSurvives(unittest.TestCase):
    """Состояние .swarm бывает любым — доска обязана собраться."""

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = bd.collect(tmp)
        self.assertEqual(board["tasks"], [])
        self.assertEqual(board["total"], 0)

    def test_broken_tasks_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / ".swarm" / "tasks.json").write_text("{не json", encoding="utf-8")
            board = bd.collect(root)
        self.assertEqual(board["tasks"], [])

    def test_broken_lines_in_jsonl_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, metrics=[{"task": "t1", "cost_usd": 1.0}])
            with open(root / ".swarm" / "metrics.jsonl", "a", encoding="utf-8") as fh:
                fh.write("{обрезано на аварии\n")
            board = bd.collect(root)
        self.assertEqual(board["total"], 1.0)

    def test_question_without_qid_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, journal=[{"kind": "question", "text": "как?"}])
            board = bd.collect(root)
        self.assertEqual(board["questions"], [])

    def test_verdict_file_with_garbage_marked_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, tasks=[{"id": "t1", "status": "in_progress"}])
            (root / ".swarm" / "log" / "t1-i1-a1-review.json").write_text(
                "не json", encoding="utf-8")
            board = bd.collect(root)
        verdicts = board["tasks"][0]["_verdicts"]
        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0]["failed"])

    def test_verdict_without_structured_output_shows_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, tasks=[{"id": "t1", "status": "blocked"}])
            (root / ".swarm" / "log" / "t1-i1-v1-review.json").write_text(
                json.dumps({"is_error": True, "subtype": "error_max_budget_usd"}),
                encoding="utf-8")
            board = bd.collect(root)
        verdicts = board["tasks"][0]["_verdicts"]
        self.assertTrue(verdicts[0]["failed"])
        self.assertEqual(verdicts[0]["why"], "error_max_budget_usd")
        self.assertEqual(verdicts[0]["round"], "i1-v1")


class TestCollectFacts(unittest.TestCase):
    """Доска ничего не додумывает: факты из файлов попадают как есть."""

    def test_spend_summed_per_task_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(
                tmp,
                tasks=[{"id": "t1", "status": "done"}, {"id": "t2", "status": "done"}],
                metrics=[{"task": "t1", "cost_usd": 1.22},
                         {"task": "t1", "cost_usd": 1.71},
                         {"task": "t2", "cost_usd": 0.30}])
            board = bd.collect(root)
        self.assertEqual(board["spend"], {"t1": 2.93, "t2": 0.30})
        self.assertEqual(board["total"], 3.23)

    def test_cost_without_task_counts_in_total_only(self):
        # Вызовы вне задачи (планировщик, хелперы) — в общий итог, но не в
        # карточки: потерять их значит соврать человеку о цене прогона.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, tasks=[{"id": "t1", "status": "done"}],
                             metrics=[{"task": "t1", "cost_usd": 1.0},
                                      {"cost_usd": 0.5}])
            board = bd.collect(root)
        self.assertEqual(board["total"], 1.5)
        self.assertEqual(board["tasks"][0]["_cost"], 1.0)

    def test_answer_closes_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, journal=[
                {"kind": "question", "qid": "q1", "task": "t1", "question": "принять?"},
                {"kind": "answer", "qid": "q1", "text": "да"}])
            board = bd.collect(root)
        (q,) = board["questions"]
        self.assertEqual(q["status"], "answered")
        self.assertEqual(q["answer"], "да")

    def test_run_level_events_not_attributed_to_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, journal=[
                {"kind": "budget_exhausted", "total_usd": 50.0},
                {"kind": "round", "task": "t1", "round": 1}])
            board = bd.collect(root)
        self.assertEqual([r["kind"] for r in board["run_level"]], ["budget_exhausted"])

    def test_unfinished_step_intent_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, journal=[
                {"kind": "step_intent", "step_id": "s1"},
                {"kind": "step_intent", "step_id": "s2"},
                {"kind": "step_done", "step_id": "s1"}])
            board = bd.collect(root)
        self.assertEqual([r["step_id"] for r in board["unfinished"]], ["s2"])

    def test_suppressed_grouped_by_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, tasks=[{"id": "t1", "status": "in_review"}],
                             journal=[{"kind": "policy_suppressed", "task": "t1",
                                       "items": [{"policy": "шум", "issue": "мелочь"}]}])
            board = bd.collect(root)
        self.assertEqual(board["tasks"][0]["_suppressed"],
                         [{"policy": "шум", "issue": "мелочь"}])


class TestRenderSafety(unittest.TestCase):
    """Страница встраивает данные из файлов — эскейпинг это граница."""

    def render_one(self, **task):
        board = {"goal": "цель", "tasks": [task], "questions": [],
                 "events": [], "run_level": [], "unfinished": [],
                 "spend": {}, "total": 0, "root": "/r", "swarm_dir": "/r/.swarm",
                 "built": "2026-08-10 00:00:00"}
        return bd.render(board)

    def test_title_does_not_break_out_of_json_payload(self):
        page = self.render_one(id="t1", status="done",
                               title="x</script><script>alert(1)</script>")
        self.assertNotIn("x</script>", page)
        self.assertIn("x<\\/script>", page)

    def test_goal_html_escaped(self):
        board = {"goal": "цель <b>жирная</b>", "tasks": [], "questions": [],
                 "events": [], "run_level": [], "unfinished": [],
                 "spend": {}, "total": 0, "root": "/r", "swarm_dir": "/r/.swarm",
                 "built": "сейчас"}
        page = bd.render(board)
        self.assertIn("цель &lt;b&gt;жирная&lt;/b&gt;", page)

    def test_page_is_self_contained(self):
        page = self.render_one(id="t1", status="done")
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertIn('id="data"', page)


class TestBuild(unittest.TestCase):
    def test_writes_board_html_next_to_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, tasks=[{"id": "t1", "status": "done"}],
                             goal="проверка")
            out, board = bd.build(root)
            page = out.read_text(encoding="utf-8")
        self.assertEqual(out.name, "board.html")
        self.assertIn("проверка", page)
        self.assertEqual(board["tasks"][0]["id"], "t1")


if __name__ == "__main__":
    unittest.main()
