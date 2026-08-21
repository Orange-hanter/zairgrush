#!/usr/bin/env python3
"""Тесты инбокса: очередь не встаёт на спорной задаче.

Главное свойство: вопрос откладывается, работа продолжается, ответ
человека возвращает задачу в очередь и доезжает до исполнителя.
"""
import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)
st_mod = cli.state_mod

TASKS = {"goal": "цель", "tasks": [
    {"id": "aaaa", "title": "спорная", "status": "pending", "deps": [],
     "paths": ["src/a.py"], "acceptance": ["ок"]},
    {"id": "bbbb", "title": "независимая", "status": "pending", "deps": [],
     "paths": ["src/b.py"], "acceptance": ["ок"]},
]}


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "README").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = st_mod.SwarmState(self.root)
        import json
        self.state.save_tasks(json.loads(json.dumps(TASKS)))

    def tearDown(self):
        self.tmp.cleanup()


class TestAskAndAnswer(InboxCase):
    def test_question_appears_in_inbox(self):
        self.state.ask("aaaa", "intent", "нужна ли эта абстракция?",
                       findings=[{"category": "architecture",
                                  "issue": "лишний слой"}])
        code, out = run_cli("--root", str(self.root), "inbox")
        self.assertEqual(code, 0)
        self.assertIn("q001", out)
        self.assertIn("абстракц", out)

    def test_answer_returns_task_to_queue(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.set_status("aaaa", "blocked", reason="ask_user")
        code, _out = run_cli("--root", str(self.root), "answer", qid,
                            "слой оставить")
        self.assertEqual(code, 0)
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["human_answer"], "слой оставить")

    def test_answered_question_leaves_open_list(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.answer(qid, "ответ")
        _, out = run_cli("--root", str(self.root), "inbox")
        self.assertIn("открытых вопросов нет", out)

    def test_answered_visible_with_all(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.answer(qid, "решение принято")
        _, out = run_cli("--root", str(self.root), "inbox", "--all")
        self.assertIn("решение принято", out)

    def test_double_answer_refused(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.answer(qid, "первый")
        code, out = run_cli("--root", str(self.root), "answer", qid, "второй")
        self.assertEqual(code, 2)
        self.assertIn("уже отвечен", out)

    def test_unknown_question_refused(self):
        code, out = run_cli("--root", str(self.root), "answer", "q999", "текст")
        self.assertEqual(code, 2)
        self.assertIn("не найден", out)


    def test_second_answer_does_not_erase_first(self):
        """Решения человека накапливаются.

        Одно поле означало, что следующий ответ затирает предыдущий: на
        приёмке v3st архитектурное решение «вынеси валидатор в _utils.py»
        стёрлось техническим ответом на следующий вопрос, и задача
        закрылась вопреки ему — ревьюер решения уже не видел.
        """
        qid1 = self.state.ask("aaaa", "intent", "первый вопрос")
        self.state.answer(qid1, "вынеси валидатор в _utils.py")
        qid2 = self.state.ask("aaaa", "escalate_max", "второй вопрос")
        self.state.answer(qid2, "диагноз неверен, причина в оркестраторе")
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertIn("_utils.py", task["human_answer"],
                      "первое решение не должно теряться")
        self.assertIn("оркестраторе", task["human_answer"])

    def test_repeated_answer_not_duplicated(self):
        qid1 = self.state.ask("aaaa", "intent", "вопрос")
        self.state.answer(qid1, "одно и то же")
        qid2 = self.state.ask("aaaa", "intent", "снова")
        self.state.answer(qid2, "одно и то же")
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertEqual(task["human_answer"].count("одно и то же"), 1)


class TestIdUniqueness(InboxCase):
    """Реюз id — ответ не на тот вопрос: номер не выводится из количества.

    Счётчик по длине выдаёт занятый id, как только хоть одна запись
    потерялась (ротация, обрезанный журнал, снятая политика), а answer
    ключуется именно по qid.
    """

    def test_qid_continues_after_answered(self):
        q1 = self.state.ask("aaaa", "intent", "первый вопрос")
        self.state.answer(q1, "решено")
        self.assertEqual(self.state.ask("bbbb", "intent", "второй"), "q002")

    def test_live_reference_survives_journal_loss(self):
        q1 = self.state.ask("aaaa", "intent", "первый вопрос")   # q001
        self.state.set_status("aaaa", "blocked", question_id=q1)
        self.state.journal_path.unlink()                          # журнал утрачен
        q2 = self.state.ask("bbbb", "intent", "второй вопрос")
        self.assertNotEqual(q2, q1,
                            "id живой ссылки из tasks.json выдан повторно")

    def test_policy_id_not_reused_after_drop(self):
        p1 = self.state.add_policy("release notes не трогаем", ["release"])
        self.state.drop_policy(p1)
        p2 = self.state.add_policy("иное решение", ["иное"])
        self.assertNotEqual(p2, p1,
                            "id снятой политики достался новой — вместе с "
                            "историей подавлений старой")


class TestQueueKeepsGoing(InboxCase):
    """Одна спорная задача не должна останавливать остальные."""

    def test_blocked_task_does_not_block_others(self):
        self.state.ask("aaaa", "intent", "вопрос")
        self.state.set_status("aaaa", "blocked", reason="ask_user")
        ready = [t["id"] for t in self.state.ready_tasks()]
        self.assertEqual(ready, ["bbbb"], "независимая задача обязана "
                                          "остаться доступной")

    def test_answer_puts_task_back_before_others(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.set_status("aaaa", "blocked", reason="ask_user")
        self.state.answer(qid, "решение")
        ready = [t["id"] for t in self.state.ready_tasks()]
        self.assertIn("aaaa", ready)


class TestStatusIntegration(InboxCase):
    def test_status_shows_open_questions(self):
        self.state.ask("aaaa", "intent", "требуется решение по слою")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("ВОПРОСЫ К ЧЕЛОВЕКУ", out)
        self.assertIn("q001", out)

    def test_status_quiet_without_questions(self):
        _, out = run_cli("--root", str(self.root), "status")
        self.assertNotIn("ВОПРОСЫ", out)


class TestAnswerReachesExecutor(InboxCase):
    """Ответ человека обязан попасть в handoff, иначе он бесполезен."""

    def test_human_answer_lands_in_feedback(self):
        agents_mod = importlib.util.spec_from_file_location(
            "agents", ROOT_DIR / "agents.py")
        agents = importlib.util.module_from_spec(agents_mod)
        sys.modules["agents"] = agents
        agents_mod.loader.exec_module(agents)
        a = agents.Agents(self.state, {})
        task = {"id": "aaaa", "title": "t", "paths": ["src/a.py"],
                "acceptance": ["ок"], "spec": "сделай"}
        text = a.handoff(task, {"human_answer": "слой оставить",
                                "note": "решение человека"}, None)
        self.assertIn("слой оставить", text)
        self.assertIn("Feedback", text)


class TestAnswerPersists(unittest.TestCase):
    """Решение человека действует на всю задачу, а не на одну итерацию.

    Поймано на приёмке: после первого ревью feedback перезаписывался
    находками, указание человека терялось, и ревьюер повторно поднимал то
    же самое замечание.
    """

    def setUp(self):
        loop_spec = importlib.util.spec_from_file_location(
            "loop", ROOT_DIR / "loop.py")
        self.lp = importlib.util.module_from_spec(loop_spec)
        sys.modules["loop"] = self.lp
        loop_spec.loader.exec_module(self.lp)
        self.prompts = []

    def _loop_with(self, verdicts):
        """Петля с mock-агентами: собираем feedback каждой итерации."""
        outer = self

        class FakeState:
            def changed_files(self):
                return []

            def work_diff(self):
                return ""
            root = "."

            def __init__(self):
                self.status = {}

            def set_status(self, tid, status, **f):
                self.status[tid] = status

            def log(self, *a, **k):
                pass

            def metric(self, **k):
                pass

            def policies(self):
                return []

            def ask(self, *a, **k):
                return "q001"

            def step(self, *a, **k):
                import contextlib

                @contextlib.contextmanager
                def noop():
                    class S:
                        def result(self, **kw):
                            pass
                    yield S()
                return noop()

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                outer.prompts.append(feedback)
                return {"status": "done"}

            def review(self, task, tail, iteration, **kw):
                return verdicts[min(iteration, len(verdicts)) - 1]

            def commit_message(self, task, diff):
                return "msg"

        loop = self.lp.Loop(FakeState(), {}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.scope_check = lambda task: (True, [], [])
        loop.commit = lambda task: "abc123"
        loop.cleanup = lambda task, reason: None
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        return loop

    def _verdict(self, kind, findings=()):
        return {"analysis": "разбор диффа по критериям приёмки задачи целиком",
                "verdict": kind, "summary": "итог проверки изменений",
                "findings": list(findings), "out_of_scope_notes": []}

    def test_answer_repeated_in_every_iteration(self):
        rc = self._verdict("request_changes",
                           [{"file": "a.py", "severity": "minor",
                             "category": "style", "issue": "мелочь"}])
        loop = self._loop_with([rc, rc, rc])
        task = {"id": "t1", "title": "t", "paths": ["a.py"], "type": "feature",
                "human_answer": "вынеси хелперы в _utils"}
        loop.run_task(task)
        self.assertGreaterEqual(len(self.prompts), 2)
        for i, fb in enumerate(self.prompts[1:], start=2):
            self.assertIn("human_answer", fb or {},
                          f"на итерации {i} решение человека потеряно")

    def test_confirm_round_does_not_call_executor(self):
        """Подтверждение перепроверяет уже принятый дифф.

        Раньше confirm-раунд шёл через исполнителя, и тот был волен
        изменить код — второй голос относился уже к другому состоянию.
        Проверяем, что исполнителя зовут ровно один раз, а ревьюера два.
        """
        loop = self._loop_with([self._verdict("approve"),
                                self._verdict("approve")])
        task = {"id": "t1", "title": "t", "paths": ["a.py"], "type": "feature",
                "human_answer": "решение"}
        self.assertEqual(loop.run_task(task), "done")
        self.assertEqual(len(self.prompts), 1,
                         "подтверждение не должно стоить вызова исполнителя")

    def test_answer_reaches_executor_in_fix_rounds(self):
        """Решение человека обязано доходить до исполнителя там, где он
        действительно работает, — в раундах исправлений."""
        rc = self._verdict("request_changes",
                           [{"file": "a.py", "severity": "minor",
                             "category": "style", "issue": "мелочь"}])
        loop = self._loop_with([rc, self._verdict("approve"),
                                self._verdict("approve")])
        loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                       "type": "feature", "human_answer": "решение"})
        self.assertIn("human_answer", self.prompts[1] or {},
                      "во втором раунде правок решение человека потеряно")

    def test_no_answer_means_no_noise(self):
        loop = self._loop_with([self._verdict("approve"),
                                self._verdict("approve")])
        loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                       "type": "feature"})
        self.assertIsNone(self.prompts[0])


class TestAnswerChangesPlan(InboxCase):
    """Ответ человека часто меняет ПЛАН, а не только инструкцию.

    Поймано на приёмке tenacity: ответ «вынеси хелперы в _utils.py» был
    невыполним, потому что этого файла не было в paths — исполнитель
    дважды пытался, SCOPE-CHECK дважды откатывал, задача исчерпала раунды.
    """

    def test_add_path_extends_task(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.answer(qid, "вынеси в helpers", add_paths=["src/helpers.py"])
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertIn("src/helpers.py", task["paths"])

    def test_add_path_is_idempotent(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        self.state.answer(qid, "текст", add_paths=["src/a.py"])
        task = next(t for t in self.state.load_tasks()["tasks"]
                    if t["id"] == "aaaa")
        self.assertEqual(task["paths"].count("src/a.py"), 1,
                         "существующий путь не должен дублироваться")

    def test_answer_mentioning_outside_file_is_refused(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        code, out = run_cli("--root", str(self.root), "answer", qid,
                            "вынеси хелперы в src/utils.py")
        self.assertEqual(code, 2, "невыполнимый ответ должен отвергаться сразу")
        self.assertIn("вне границ задачи", out)
        self.assertIn("--add-path", out)

    def test_answer_with_explicit_path_passes(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        code, _ = run_cli("--root", str(self.root), "answer", qid,
                          "вынеси хелперы в src/utils.py",
                          "--add-path", "src/utils.py")
        self.assertEqual(code, 0)

    def test_answer_without_filenames_passes(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        code, _ = run_cli("--root", str(self.root), "answer", qid,
                          "оставить как есть, абстракция оправдана")
        self.assertEqual(code, 0)

    def test_mentioning_own_path_is_fine(self):
        qid = self.state.ask("aaaa", "intent", "вопрос")
        code, _ = run_cli("--root", str(self.root), "answer", qid,
                          "поправь src/a.py и всё")
        self.assertEqual(code, 0, "файл внутри границ задачи не повод отказывать")




class TestAnswerReachesReviewer(InboxCase):
    """Решение человека обязано быть видно и ревьюеру.

    Поймано на приёмке: человек отклонил требование release note, а
    ревьюер трижды повторял его в findings, потому что не знал о решении.
    """

    def _agents(self):
        spec_a = importlib.util.spec_from_file_location(
            "agents", ROOT_DIR / "agents.py")
        agents = importlib.util.module_from_spec(spec_a)
        sys.modules["agents"] = agents
        spec_a.loader.exec_module(agents)
        return agents.Agents(self.state, {})

    def test_decision_appears_in_review_prompt(self):
        task = {"id": "aaaa", "title": "t", "spec": "сделай",
                "acceptance": ["ок"], "paths": ["src/a.py"],
                "human_answer": "release note не добавлять — вне scope"}
        text = self._agents().review_prompt(task, "OK", "diff")
        self.assertIn("release note не добавлять", text)
        self.assertIn("НЕ оспариваются", text)

    def test_no_decision_no_section(self):
        task = {"id": "aaaa", "title": "t", "spec": "сделай",
                "acceptance": ["ок"], "paths": ["src/a.py"]}
        text = self._agents().review_prompt(task, "OK", "diff")
        self.assertNotIn("Решения человека", text)




class TestNoSilentBlocking(unittest.TestCase):
    """Любой терминальный исход обязан попадать в инбокс.

    Поймано на приёмке: задача, где исполнитель трижды не вернул валидный
    отчёт, заблокировалась молча — вопроса в инбоксе не появилось, и
    человек о ней не узнал.
    """

    def setUp(self):
        loop_spec = importlib.util.spec_from_file_location(
            "loop", ROOT_DIR / "loop.py")
        self.lp = importlib.util.module_from_spec(loop_spec)
        sys.modules["loop"] = self.lp
        loop_spec.loader.exec_module(self.lp)
        self.asked = []

    def _loop(self):
        outer = self

        class FakeState:
            def changed_files(self):
                return []

            def work_diff(self):
                return ""
            root = "."

            def set_status(self, *a, **k):
                pass

            def log(self, *a, **k):
                pass

            def metric(self, **k):
                pass

            def policies(self):
                return []

            def ask(self, task_id, kind, question, **ctx):
                outer.asked.append((task_id, kind, question))
                return f"q{len(outer.asked):03d}"

        class FakeAgents:
            def implement(self, task, feedback, iteration):
                return None          # исполнитель ни разу не вернул отчёт

            def review(self, *a, **k):
                raise AssertionError("ревьюер не должен вызываться")

        loop = self.lp.Loop(FakeState(), {}, FakeAgents())
        loop.gate = lambda task: (True, "OK")
        loop.cleanup = lambda task, reason: f"swarm:{task['id']}-{reason}"
        loop.sh = lambda cmd, timeout=900: type(
            "R", (), {"stdout": "", "returncode": 0})()
        return loop

    def test_exhausted_rounds_ask_human(self):
        loop = self._loop()
        result = loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                                "type": "feature"})
        self.assertEqual(result, "blocked")
        self.assertEqual(len(self.asked), 1, "исход обязан попасть в инбокс")
        self.assertEqual(self.asked[0][1], self.lp.ESCALATE_MAX)

    def test_question_carries_diagnosis(self):
        loop = self._loop()
        loop.run_task({"id": "t1", "title": "t", "paths": ["a.py"],
                       "type": "feature"})
        # Версия о причине, а не голый факт. Догадка «слишком крупная»
        # выведена из диагноста (золотой набор: 0 из 6 верных), поэтому
        # здесь проверяется именно НАЛИЧИЕ разбора, а не его прежний текст.
        self.assertIn("сорвались", self.asked[0][2],
                      "вопрос обязан нести версию о причине")
        self.assertIn("лимит исправлений не тронут", self.asked[0][2],
                      "человеку важно знать, что попыток ещё не было")


class TestAnswerPathGuardEscape(unittest.TestCase):
    """Упоминание файла в объяснении — не указание его править.

    Страж полезен: сказать исполнителю «почини X», когда X вне границ,
    гарантирует нарушение и потерянный круг. Но отличить указание от
    объяснения он не может. На PILOT-1 ответ объяснял, что HEAD сдвинул
    ОПЕРАТОР, закоммитив swarm.toml во время задачи, — страж прочёл это
    как задание и предложил выдать исполнителю право на конфиг пилота,
    ровно то, от чего защищает. Отсюда `--force`: явное «упомянуто, а не
    задано», вместо опасного расширения границ.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.state = st_mod.SwarmState(self.root)
        self.state.save_tasks({"goal": "ц", "tasks": [
            {"id": "t1", "title": "t", "status": "blocked", "deps": [],
             "type": "feature", "paths": ["src/only.py"], "acceptance": ["ок"]}]})
        self.qid = self.state.ask("t1", "ask_user", "вопрос?")

    def _answer(self, text, **kw):
        fields = {"root": str(self.root), "qid": self.qid, "text": text,
                  "add_path": [], "force": False}
        fields.update(kw)
        args = type("A", (), fields)()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_answer(args)
        return code, buf.getvalue()

    def test_mention_is_refused_by_default(self):
        code, out = self._answer("я закоммитил swarm.toml пока шла задача")
        self.assertEqual(code, 2)
        self.assertIn("swarm.toml", out)

    def test_guard_offers_both_exits(self):
        """Один выход — расширить границы, другой — сказать «упомянуто».

        Без второго оператор вынужден либо переписывать объяснение, либо
        выдавать права, которых давать не хотел.
        """
        _, out = self._answer("я закоммитил swarm.toml пока шла задача")
        self.assertIn("--add-path", out)
        self.assertIn("--force", out)

    def test_force_lets_the_answer_through(self):
        code, _ = self._answer("я закоммитил swarm.toml пока шла задача",
                               force=True)
        self.assertEqual(code, 0)
        task = next(iter(self.state.load_tasks()["tasks"]))
        self.assertEqual(task["status"], "pending")

    def test_force_does_not_widen_the_boundaries(self):
        """Главное отличие от --add-path: границы остаются прежними."""
        self._answer("я закоммитил swarm.toml пока шла задача", force=True)
        task = next(iter(self.state.load_tasks()["tasks"]))
        self.assertEqual(task["paths"], ["src/only.py"],
                         "--force не должен выдавать прав на упомянутый файл")

    def test_clean_answer_needs_no_flag(self):
        code, _ = self._answer("правь src/only.py и ничего больше")
        self.assertEqual(code, 0)


class TestRunLevelAnswer(InboxCase):
    """Ответ на вопрос уровня прогона (task="*") ничего не перезапускает.

    cmd_answer печатал «задача * возвращена в очередь», хотя за вопросом
    plan_failed задачи нет: состояние не менялось и само ничего не
    перезапустится — оператор ждал продолжения, которого не будет.
    """

    def test_star_answer_tells_the_truth(self):
        qid = self.state.ask("*", "plan_failed", "план не прошёл схему")
        code, out = run_cli("--root", str(self.root), "answer", qid,
                            "цель уточнена, пробуем снова")
        self.assertEqual(code, 0, out)
        self.assertNotIn("возвращена в очередь", out)
        self.assertIn("журнал", out, "сказано, куда лёг ответ")
        self.assertIn("swarm plan", out, "сказано, что перезапуск ручной")

    def test_star_answer_is_recorded(self):
        qid = self.state.ask("*", "plan_failed", "план не прошёл схему")
        run_cli("--root", str(self.root), "answer", qid, "ответ")
        q = next(q for q in self.state.questions() if q["qid"] == qid)
        self.assertEqual(q["status"], "answered")


class TestDisputeBodyInInbox(InboxCase):
    """Полный довод исполнителя доходит до оператора.

    Вопрос-спор нёс поле dispute с полным аргументом из отчёта
    исполнителя, но инбокс печатал только однострочную сводку — решение
    о пересмотре плана принималось вслепую.
    """

    def test_dispute_body_is_shown(self):
        self.state.ask(
            "aaaa", "dispute", "задача невыполнима как поставлена",
            dispute=("Полный аргумент: критерий приёмки противоречит "
                     "границам,\nнужно менять план."))
        _, out = run_cli("--root", str(self.root), "inbox")
        self.assertIn("Полный аргумент", out)
        self.assertIn("нужно менять план", out)

    def test_dispute_of_wrong_shape_does_not_crash(self):
        """Журнал — данные: dispute-словарь из чужой версии оркестратора
        показывается как JSON, а не роняет команду на splitlines."""
        self.state.ask("aaaa", "dispute", "вопрос",
                       dispute={"text": "аргумент словарём"})
        code, out = run_cli("--root", str(self.root), "inbox")
        self.assertEqual(code, 0, out)
        self.assertIn("аргумент словарём", out)


class TestInboxSpeaksHuman(InboxCase):
    """Инбокс печатал сырой qkind и терял поля находок."""

    def test_qkind_is_translated(self):
        self.state.ask("aaaa", "intent", "вопрос?")
        _, out = run_cli("--root", str(self.root), "inbox")
        self.assertIn("находка о замысле", out)
        self.assertNotIn("(intent)", out)

    def test_findings_keep_severity_and_place(self):
        """Печатались только категория и суть: severity/file/line
        терялись, а решение о замысле принимается по тяжести и месту.
        Фраза находки — общая с report/why (vocab.finding)."""
        self.state.ask("aaaa", "intent", "вопрос?", findings=[
            {"severity": "major", "category": "architecture",
             "file": "src/a.py", "line": 10, "issue": "лишний слой"}])
        _, out = run_cli("--root", str(self.root), "inbox")
        self.assertIn("важное", out)
        self.assertIn("архитектура", out)
        self.assertIn("src/a.py:10", out)
        self.assertIn("лишний слой", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
