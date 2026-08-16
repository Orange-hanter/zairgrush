#!/usr/bin/env python3
"""Интеграционные тесты против НАСТОЯЩЕГО git.

Остальные наборы работают на моках `_sh`, и это скрыло целый класс
дефектов: петля разговаривала с git через три несогласованных
представления — границы по `git status --porcelain`, ревью по `git diff`,
коммит по `git add -A`. Мок возвращал то, что от него ждали, поэтому
расхождение между представлениями было невидимо.

Здесь всё наоборот: временный репозиторий, настоящие команды, проверка
того, что три представления сходятся на одном и том же множестве
изменений. Главный случай — СОЗДАНИЕ файла: `git diff` его не показывает,
и ревьюер одобрял пустоту, пока `git add -A` этот файл коммитил.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("state")
lp = _load("loop")
ag = _load("agents")


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=False)


class GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "t")
        git(self.root, "config", "user.email", "t@t")
        (self.root / "src").mkdir()
        (self.root / "src" / "existing.py").write_text("def old():\n    return 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_existing.py").write_text(
            "def test_old():\n    pass\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": []})
        self.loop = lp.Loop(self.state, {}, None)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, rel, text="def added():\n    return 2\n"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    TASK = {"id": "t1", "title": "t", "type": "feature",
            "paths": ["src/new_module.py", "src/existing.py"]}


class TestNewFileIsVisible(GitCase):
    """Созданный файл обязан быть виден всем трём представлениям."""

    def test_scope_check_sees_new_file_by_name(self):
        """Без -uall статус схлопывает каталог в `?? src/`.

        Такая строка не совпадёт ни с одним глобом из paths, и задача
        «создай модуль» гарантированно выжигает лимит итераций с ложным
        диагнозом «слишком крупная».
        """
        self.create("src/new_module.py")
        ok, bad, _ = self.loop.scope_check(dict(self.TASK))
        self.assertTrue(ok, f"новый файл в границах задачи отвергнут: {bad}")

    def test_new_file_outside_paths_is_caught(self):
        self.create("src/sneaky.py")
        ok, bad, _ = self.loop.scope_check(dict(self.TASK))
        self.assertFalse(ok)
        self.assertIn("src/sneaky.py", bad,
                      "нарушение обязано называться поимённо, а не каталогом")

    def test_new_file_in_nested_dir_named_precisely(self):
        self.create("src/deep/nested/mod.py")
        _, bad, _ = self.loop.scope_check(dict(self.TASK))
        self.assertIn("src/deep/nested/mod.py", bad)

    def test_new_test_file_is_protected(self):
        """Чужой тест нельзя создать в обход защиты.

        «Чужой» = не названный в paths. Прежняя версия давала задаче
        paths=["tests/**"] и ждала отказа — но явный глоб в защищённую
        зону теперь СНИМАЕТ защиту (это решение владельца при постановке,
        см. scope_check): барьер по типу задачи убил k3ad и s2ky на
        пилоте. Угроза, от которой защищаемся, — исполнитель, а он paths
        не меняет.
        """
        task = dict(self.TASK, paths=["src/**"], type="feature")
        self.create("tests/test_sneaky.py")
        ok, bad, _ = self.loop.scope_check(task)
        self.assertFalse(ok)
        self.assertIn("tests/test_sneaky.py", bad)

    def test_explicit_tests_glob_unlocks_creation(self):
        """Обратная сторона: владелец, celившийся paths'ами в тесты,
        получает право их создавать — независимо от типа задачи."""
        task = dict(self.TASK, paths=["tests/**"], type="feature")
        self.create("tests/test_wanted.py")
        ok, bad, protected = self.loop.scope_check(task)
        self.assertTrue(ok, (bad, protected))


class TestReviewerSeesNewFiles(GitCase):
    """Ревьюер не должен получать пустой дифф при созданном файле."""

    def _diff(self):
        return ag.Agents(self.state, {}).work_diff()

    def test_created_file_appears_in_diff(self):
        self.create("src/new_module.py", "def evil():\n    eval(input())\n")
        diff = self._diff()
        self.assertIn("new_module.py", diff,
                      "ревьюер обязан видеть созданный файл, иначе одобряет "
                      "пустоту, а git add -A её коммитит")
        self.assertIn("eval(input())", diff,
                      "содержимое нового файла обязано попасть на ревью")

    def test_modified_file_still_in_diff(self):
        (self.root / "src" / "existing.py").write_text("def old():\n    return 42\n")
        self.assertIn("42", self._diff())

    def test_both_kinds_together(self):
        self.create("src/new_module.py")
        (self.root / "src" / "existing.py").write_text("def old():\n    return 42\n")
        diff = self._diff()
        self.assertIn("new_module.py", diff)
        self.assertIn("42", diff)

    def test_clean_tree_gives_empty_diff(self):
        self.assertEqual(self._diff().strip(), "",
                         "на чистом дереве дифф пуст — иначе ревьюер получит шум")

    def test_intent_to_add_does_not_commit_by_itself(self):
        """Снятие диффа не должно менять историю."""
        head_before = git(self.root, "rev-parse", "HEAD").stdout
        self.create("src/new_module.py")
        self._diff()
        self.assertEqual(git(self.root, "rev-parse", "HEAD").stdout, head_before)


class TestRevertRemovesNewFiles(GitCase):
    """`git checkout -- .` не удаляет untracked: нарушение границ
    оставалось на диске и повторялось каждый раунд."""

    def test_revert_deletes_created_file(self):
        self.create("src/sneaky.py")
        self.loop.revert()
        self.assertFalse((self.root / "src" / "sneaky.py").exists(),
                         "созданный файл обязан исчезнуть при откате")

    def test_revert_restores_modified_file(self):
        (self.root / "src" / "existing.py").write_text("сломано")
        self.loop.revert()
        self.assertIn("def old()", (self.root / "src" / "existing.py").read_text())

    def test_revert_leaves_tree_clean(self):
        self.create("src/sneaky.py")
        (self.root / "src" / "existing.py").write_text("сломано")
        self.loop.revert()
        self.assertEqual(git(self.root, "status", "--porcelain").stdout.strip(), "")

    def test_revert_does_not_touch_pre_existing_work(self):
        """Незакоммиченная работа человека обязана пережить откат.

        Пока откат шёл по всему дереву (`git checkout -- .`), первое же
        нарушение границ уничтожало её безвозвратно — единственный вид
        ущерба, который нельзя починить постфактум.
        """
        (self.root / "src" / "existing.py").write_text("работа человека\n")
        self.create("human_notes.md", "черновик человека\n")
        self.loop._pre_existing = set(self.state.changed_files())
        self.create("src/sneaky.py")            # это уже агент
        self.loop.revert()
        self.assertEqual((self.root / "src" / "existing.py").read_text(),
                         "работа человека\n", "правка человека откачена")
        self.assertTrue((self.root / "human_notes.md").exists(),
                        "новый файл человека удалён откатом")
        self.assertFalse((self.root / "src" / "sneaky.py").exists(),
                         "работа агента обязана быть откачена")

    def test_revert_of_tracked_file_spares_human_tracked_file(self):
        """Самый острый случай: и человек, и агент правят ОТСЛЕЖИВАЕМЫЕ файлы.

        Здесь откат по всему дереву неотличим от точечного по последствиям
        для агента, но разница фатальна для человека.
        """
        (self.root / "tests" / "test_existing.py").write_text("работа человека\n")
        self.loop._pre_existing = set(self.state.changed_files())
        (self.root / "src" / "existing.py").write_text("правка агента\n")
        self.loop.revert()
        self.assertEqual((self.root / "tests" / "test_existing.py").read_text(),
                         "работа человека\n",
                         "откат задел отслеживаемый файл человека")
        self.assertIn("def old()", (self.root / "src" / "existing.py").read_text(),
                      "правка агента обязана быть откачена")

    def test_revert_without_baseline_still_works(self):
        """Без снимка (прямой вызов) откат ведёт себя как раньше."""
        self.create("src/sneaky.py")
        self.loop.revert()
        self.assertFalse((self.root / "src" / "sneaky.py").exists())

    def test_revert_keeps_swarm_state(self):
        """Откат не должен сносить состояние петли."""
        self.create("src/sneaky.py")
        self.loop.revert()
        self.assertTrue(self.state.tasks_path.exists(),
                        "`git clean` не должен уносить .swarm/")


class TestCommitMatchesReview(GitCase):
    """Коммитится ровно то, что видел ревьюер."""

    def test_created_file_lands_in_commit(self):
        self.create("src/new_module.py")
        agents = ag.Agents(self.state, {})
        reviewed = agents.work_diff()
        loop = lp.Loop(self.state, {}, type("A", (), {
            "commit_message": staticmethod(lambda task, diff: "msg")})())
        sha = loop.commit(dict(self.TASK))
        self.assertIsNotNone(sha)
        committed = git(self.root, "show", "--stat", sha).stdout
        self.assertIn("new_module.py", committed)
        self.assertIn("new_module.py", reviewed,
                      "то, что закоммичено, обязано было пройти ревью")

    def test_clean_tree_commits_nothing(self):
        loop = lp.Loop(self.state, {}, type("A", (), {
            "commit_message": staticmethod(lambda task, diff: "msg")})())
        self.assertIsNone(loop.commit(dict(self.TASK)))


class TestIntegrityCheck(GitCase):
    """§6.1: исполнителю нельзя трогать историю и состояние петли.

    Deny-правила у Kimi недокументированы, а запрет в промпте — не
    гарантия. Поэтому проверяется не намерение, а факт: постфактум, но
    механически. Без этого агент мог закоммитить сам, сделать `git reset`
    или пометить задачу выполненной правкой `.swarm/tasks.json`.
    """

    def _armed(self):
        loop = lp.Loop(self.state, {}, None)
        loop._head_before = git(self.root, "rev-parse", "HEAD").stdout.strip()
        loop._state_before = loop._state_fingerprint()
        return loop

    def test_clean_work_passes(self):
        loop = self._armed()
        self.create("src/new_module.py")
        self.assertEqual(loop.integrity_check(), [])

    def test_agent_commit_is_caught(self):
        loop = self._armed()
        self.create("src/new_module.py")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "я сам себя закоммитил")
        bad = loop.integrity_check()
        self.assertTrue(bad)
        self.assertIn("история изменилась", bad[0])

    def test_message_names_the_commit_author(self):
        """Проверка знает ФАКТ расхождения, но не автора.

        На PILOT-1 HEAD сдвинул ОПЕРАТОР — закоммитил правку конфига,
        пока задача шла в фоне, — а формулировка «исполнитель вышел за
        границы доверия» обвинила агента и стоила круга разбирательства.
        Автор и заголовок коммита в сообщении отвечают на вопрос «моё это
        или нет» с одного взгляда.
        """
        loop = self._armed()
        (self.root / "новый.txt").write_text("x")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "правка конфига оператором")
        bad = loop.integrity_check()
        self.assertIn("правка конфига оператором", bad[0],
                      "заголовок коммита не показан — оператор не поймёт, чей он")
        self.assertNotIn("исполнитель", bad[0].lower(),
                         "проверка не может знать автора и не должна обвинять")

    def test_agent_reset_is_caught(self):
        git(self.root, "commit", "-q", "--allow-empty", "-m", "второй")
        loop = self._armed()
        git(self.root, "reset", "-q", "--hard", "HEAD~1")
        self.assertTrue(any("история изменилась" in b
                            for b in loop.integrity_check()))

    def test_state_tampering_is_caught(self):
        """Пометить задачу done правкой состояния — самый дешёвый обман.

        Правка делается ФАЙЛОМ, а не через `state.save_tasks`, потому что
        именно так её и делает агент: объекта состояния петли у него нет,
        у него есть shell и путь. Прежняя версия теста звала API — то есть
        проверяла законный путь и называла его обманом.
        """
        loop = self._armed()
        (self.state.tasks_path).write_text(
            json.dumps({"goal": "", "tasks": [
                {"id": "x", "title": "t", "status": "done", "deps": []}]},
                ensure_ascii=False),
            encoding="utf-8")
        bad = loop.integrity_check()
        self.assertTrue(any(".swarm/" in b for b in bad), bad)
        self.assertTrue(any("в обход API" in b for b in bad), bad)

    def test_legitimate_write_is_not_tampering(self):
        """Регрессия на пилот (c4rp): оператор ответил через `swarm answer`
        на вопрос ОДНОЙ задачи, пока шла ДРУГАЯ, — и уронил бегущую.

        Инбокс заведён затем, чтобы спор не останавливал очередь; проверка,
        считающая любую запись состояния нарушением, останавливала её сама.
        """
        loop = self._armed()
        data = self.state.load_tasks()
        data["tasks"] = [{"id": "x", "title": "t", "status": "pending", "deps": []}]
        self.state.save_tasks(data)          # законный путь: объявляет себя
        self.assertEqual([b for b in loop.integrity_check() if ".swarm/" in b], [])

    def test_second_check_does_not_refire_on_the_same_change(self):
        """База сдвигается после объяснённого изменения: иначе одна и та же
        законная запись срабатывала бы на каждом раунде задачи."""
        loop = self._armed()
        data = self.state.load_tasks()
        data["tasks"] = [{"id": "x", "title": "t", "status": "pending", "deps": []}]
        self.state.save_tasks(data)
        loop.integrity_check()
        self.assertEqual([b for b in loop.integrity_check() if ".swarm/" in b], [])

    def test_unfinished_merge_is_caught(self):
        loop = self._armed()
        (self.root / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
        self.assertTrue(any("merge" in b for b in loop.integrity_check()))

    def test_violation_stops_the_task(self):
        """Мало обнаружить нарушение — задача обязана остановиться.

        Проверка вызывалась в тестах напрямую, и мутация «нарушение не
        останавливает задачу» это переживала: связи с run_task не было.
        """
        def implement(task, feedback, iteration):
            git(self.root, "commit", "-q", "--allow-empty", "-m", "я сам")
            return {"status": "done", "summary": "готово"}

        agents = type("A", (), {"implement": staticmethod(implement),
                                "review": staticmethod(
                                    lambda *a, **k: (_ for _ in ()).throw(
                                        AssertionError("ревьюер не должен "
                                                       "вызываться"))),
                                "commit_message": staticmethod(lambda t, d: "m")})()
        loop = lp.Loop(self.state, {}, agents, ui=lambda *a: None)
        loop.gate = lambda task: (True, "OK")
        self.state.save_tasks({"goal": "g", "tasks": [
            {"id": "t1", "title": "t", "status": "pending", "deps": [],
             "type": "feature", "paths": ["src/new_module.py"]}]})
        result = loop.run_task(dict(self.TASK))
        self.assertEqual(result, "blocked", "нарушение доверия не остановило задачу")
        task = next(t for t in self.state.load_tasks()["tasks"] if t["id"] == "t1")
        self.assertEqual(task["reason"], "integrity")

    def test_orchestrator_commit_moves_the_baseline(self):
        """Коммит оркестратора легален и не должен обвинять агента."""
        loop = lp.Loop(self.state, {}, type("A", (), {
            "commit_message": staticmethod(lambda task, diff: "msg")})())
        loop._head_before = git(self.root, "rev-parse", "HEAD").stdout.strip()
        loop._state_before = loop._state_fingerprint()
        self.create("src/new_module.py")
        loop.commit(dict(self.TASK))
        self.assertEqual(loop.integrity_check(), [],
                         "оркестратор обвинил агента в собственном коммите")


if __name__ == "__main__":
    unittest.main(verbosity=2)
