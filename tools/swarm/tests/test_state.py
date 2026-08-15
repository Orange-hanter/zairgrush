#!/usr/bin/env python3
"""Тесты состояния петли: блокировка, атомарность, step-journal, deps."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("state", ROOT / "swarm" / "state.py")
st = importlib.util.module_from_spec(spec)
sys.modules["state"] = st
spec.loader.exec_module(st)

TASKS = {"goal": "тест", "tasks": [
    {"id": "aaaa", "title": "первая", "status": "pending", "deps": []},
    {"id": "bbbb", "title": "вторая", "status": "pending", "deps": ["aaaa"]},
]}


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks(dict(TASKS))

    def tearDown(self):
        self.state.release()
        self.tmp.cleanup()


class TestQueue(StateCase):
    def test_ready_respects_deps(self):
        ready = [t["id"] for t in self.state.ready_tasks()]
        self.assertEqual(ready, ["aaaa"], "задача с незакрытой зависимостью "
                                          "не должна попадать в работу")

    def test_dependency_unlocks_next(self):
        self.state.set_status("aaaa", "done")
        self.assertEqual([t["id"] for t in self.state.ready_tasks()], ["bbbb"])

    def test_deadlock_is_loud(self):
        data = self.state.load_tasks()
        data["tasks"][0]["deps"] = ["bbbb"]      # цикл между aaaa и bbbb
        self.state.save_tasks(data)
        with self.assertRaises(st.StateError):
            self.state.ready_tasks()

    def test_illegal_status_rejected(self):
        with self.assertRaises(st.StateError):
            self.state.set_status("aaaa", "почти_готово")

    def test_save_tasks_validates_status(self):
        """Прямая запись очереди — путь планировщика и ручной правки.

        Мутационный аудит показал, что проверка здесь не была покрыта:
        тест ловил только `set_status`, а невалидный статус, записанный
        через `save_tasks`, проходил молча.
        """
        data = self.state.load_tasks()
        data["tasks"][0]["status"] = "почти_готово"
        with self.assertRaises(st.StateError):
            self.state.save_tasks(data)

    def test_save_tasks_rejects_missing_status(self):
        data = self.state.load_tasks()
        del data["tasks"][0]["status"]
        with self.assertRaises(st.StateError):
            self.state.save_tasks(data)

    def test_rejected_save_leaves_file_intact(self):
        """Отказ не должен портить состояние на диске."""
        before = self.state.tasks_path.read_text()
        data = self.state.load_tasks()
        data["tasks"][0]["status"] = "мусор"
        with self.assertRaises(st.StateError):
            self.state.save_tasks(data)
        self.assertEqual(self.state.tasks_path.read_text(), before)

    def test_unknown_task_rejected(self):
        with self.assertRaises(st.StateError):
            self.state.set_status("zzzz", "done")

    def test_write_is_atomic_no_tmp_left(self):
        self.state.set_status("aaaa", "done")
        leftovers = list(self.state.dir.glob("*.tmp"))
        self.assertEqual(leftovers, [], "временный файл не должен оставаться")


class TestLock(StateCase):
    def test_second_orchestrator_is_refused(self):
        self.state.acquire()
        other = st.SwarmState(self.root)
        with self.assertRaises(st.StateError):
            other.acquire()

    def test_lock_released_allows_next(self):
        self.state.acquire()
        self.state.release()
        other = st.SwarmState(self.root)
        other.acquire()            # не должно бросить
        other.release()


class TestStepJournal(StateCase):
    """§5.6: intent до side-effect'а, done после — иначе падение между
    коммитом и статусом даёт дубль."""

    def test_successful_step_writes_both_records(self):
        with self.state.step("aaaa", "commit") as step:
            step.result(commit="abc123")
        kinds = [json.loads(line)["kind"]
                 for line in self.state.journal_path.read_text().splitlines()]
        self.assertEqual(kinds, ["step_intent", "step_done"])

    def test_result_payload_lands_in_journal(self):
        with self.state.step("aaaa", "commit") as step:
            step.result(commit="abc123")
        last = json.loads(self.state.journal_path.read_text().splitlines()[-1])
        self.assertEqual(last["commit"], "abc123")

    def test_failed_step_is_marked(self):
        with self.assertRaises(RuntimeError), self.state.step("aaaa", "commit"):
            raise RuntimeError("git упал")
        kinds = [json.loads(line)["kind"]
                 for line in self.state.journal_path.read_text().splitlines()]
        self.assertIn("step_failed", kinds)

    def test_unfinished_step_is_detectable_after_crash(self):
        # имитируем падение процесса: intent записан, done — нет
        self.state.log("step_intent", step_id="aaaa:commit:1", task="aaaa",
                       action="commit")
        unfinished = self.state.unfinished_steps()
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(unfinished[0]["action"], "commit")

    def test_completed_steps_are_not_reported(self):
        with self.state.step("aaaa", "commit"):
            pass
        self.assertEqual(self.state.unfinished_steps(), [])


class TestPersistence(StateCase):
    def test_state_survives_reopen(self):
        self.state.set_status("aaaa", "done", commit="abc")
        reopened = st.SwarmState(self.root)
        task = next(t for t in reopened.load_tasks()["tasks"] if t["id"] == "aaaa")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["commit"], "abc")

    def test_missing_tasks_file_is_empty_queue(self):
        with tempfile.TemporaryDirectory() as empty:
            fresh = st.SwarmState(empty)
            self.assertEqual(fresh.load_tasks()["tasks"], [])

    def test_metrics_are_appended(self):
        self.state.metric(task="aaaa", phase="gate", ok=True)
        self.state.metric(task="aaaa", phase="review", ok=False)
        rows = self.state.metrics_path.read_text().strip().splitlines()
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
