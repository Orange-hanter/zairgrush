#!/usr/bin/env python3
"""Тесты состояния петли: блокировка, атомарность, step-journal, deps."""
import importlib.util
import json
import pathlib
import subprocess
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
        self.assertEqual(kinds, ["state_written", "step_intent", "step_done"])

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


class TestSpendSurvivesGarbage(StateCase):
    """Журнал метрик читается как данные, а не как контракт: валидная
    JSON-строка не-объект и строковый cost_usd роняли total_spend —
    то есть именно ту формулу, которой сверяют деньги."""

    def test_non_dict_rows_and_string_costs_are_skipped(self):
        self.state.metric(task="aaaa", phase="review", cost_usd=1.25)
        with self.state.metrics_path.open("a", encoding="utf-8") as f:
            f.write('"строка"\n[1, 2]\n{"cost_usd": "дорого"}\n'
                    '{"cost_usd": 0.25}\nне json\n')
        self.assertEqual(self.state.total_spend(), 1.5)


class TestAnswerDoneGuard(StateCase):
    """Ответ на залежавшийся вопрос не воскрешает закрытую задачу:
    безусловный pending отправлял done-работу на повторное исполнение.

    Охраняемое свойство осталось прежним, а вот его цена изменилась. До
    2026-08-23 отказ уносил с собой САМ ОТВЕТ: вопрос оставался открытым,
    разбор случившегося записать было некуда, а `status` требовал решения
    над закрытой очередью (E13, задача b4wr вернулась в работу через
    `swarm retry`). Теперь ответ записывается, решение копится при
    задаче, а статус не трогается — переоткрывает только `swarm retry`.
    """

    def _answered_after_close(self):
        qid = self.state.ask("aaaa", "intent", "вопрос по замыслу")
        self.state.set_status("aaaa", "done")
        self.state.answer(qid, "поздний ответ")
        return qid

    def test_done_task_stays_done(self):
        self._answered_after_close()
        task = {t["id"]: t
                for t in self.state.load_tasks()["tasks"]}["aaaa"]
        self.assertEqual(task["status"], "done")

    def test_the_answer_is_not_lost(self):
        qid = self._answered_after_close()
        q = {x["qid"]: x for x in self.state.questions()}[qid]
        self.assertEqual(q["status"], "answered")
        self.assertEqual(q["answer"], "поздний ответ")

    def test_such_a_question_no_longer_blocks_the_queue(self):
        self._answered_after_close()
        self.assertEqual(self.state.questions(blocking=True), [])


class TestConcurrentWriters(StateCase):
    """Lost update между процессами: `swarm answer`/`retry` зовут во время
    прогона — так задуман инбокс, — и их load→modify→save гонялся с
    set_status бегущей петли. Пропавшую запись не ловит даже проверка
    целостности: последний отпечаток объявлен честно, он просто собран из
    устаревшего чтения. Замок записи (mutate) обязан сериализовать
    транзакции так, чтобы не терялось НИЧЕГО."""

    WORKER = r"""
import importlib.util, sys
state_py, root, tag, n = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("state", state_py)
mod = importlib.util.module_from_spec(spec)
sys.modules["state"] = mod
spec.loader.exec_module(mod)
s = mod.SwarmState(root)
for i in range(int(n)):
    s.set_status("aaaa", "pending", **{f"mark_{tag}_{i}": True})
"""

    def test_parallel_set_status_loses_nothing(self):
        n = 20
        procs = [subprocess.Popen(
            [sys.executable, "-c", self.WORKER,
             str(ROOT / "swarm" / "state.py"), str(self.root), tag, str(n)])
            for tag in ("a", "b")]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        task = {t["id"]: t
                for t in self.state.load_tasks()["tasks"]}["aaaa"]
        missing = [f"mark_{tag}_{i}" for tag in ("a", "b")
                   for i in range(n) if f"mark_{tag}_{i}" not in task]
        self.assertEqual(missing, [],
                         "потерянные записи конкурирующих писателей")


if __name__ == "__main__":
    unittest.main(verbosity=2)
