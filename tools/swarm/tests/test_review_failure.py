#!/usr/bin/env python3
"""Ревью, которое не состоялось: свёртка диффа, стеш, эскалация.

Всё в этом файле пришло из одного прогона пилота на ZeusLogic. Задача
`g1nt` (golden-снимок сетей) была сделана полностью: гейт зелёный, границы
чистые, тест проходит. И всё равно ушла в `blocked`, потратив $6.59 и не
дав ни одного вердикта. Разбор вскрыл три независимых дефекта:

1. Эталон на 15 894 строки ушёл ревьюеру целиком — промпт в 285 000
   токенов, вызов обрублен по `--max-budget-usd`. Читать построчно
   машинно порождённые данные ревьюеру всё равно нечего.
2. Обрыв по бюджету пошёл в retry. Тот же промпт кончается на том же
   месте — вторая попытка стоила ещё $3.23 и упала так же.
3. `git stash push` отказался работать поверх записей intent-to-add,
   которые оставляет `work_diff`; `cleanup` не проверил код возврата и
   записал в задачу метку стеша, которого не существует.

Плюс четвёртое, обнаруженное при разборе: заблокированная задача не
задала человеку ни одного вопроса. `swarm inbox` был пуст.
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


def _diff_for(path, body_lines):
    """Кусок диффа на один файл с `body_lines` добавленными строками."""
    head = (f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "index 0000000..1111111\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{len(body_lines)} @@\n")
    return head + "".join(f"+{line}\n" for line in body_lines)


class TestCondenseDiff(unittest.TestCase):
    """Свёртка — чистая функция, поэтому проверяется без git и без сети."""

    def test_empty_diff_untouched(self):
        self.assertEqual(ag.condense_diff(""), "")

    def test_short_file_passes_through_verbatim(self):
        diff = _diff_for("src/mod.py", ["def f():", "    return 1"])
        self.assertEqual(ag.condense_diff(diff), diff.rstrip("\n"))

    def test_text_without_diff_header_survives(self):
        """Не всякий вход — дифф; функция не должна его терять."""
        self.assertEqual(ag.condense_diff("просто текст"), "просто текст")

    def test_long_file_is_collapsed(self):
        diff = _diff_for("fixtures/golden.txt", [f"line {i}" for i in range(1000)])
        out = ag.condense_diff(diff)
        self.assertIn("diff --git a/fixtures/golden.txt", out,
                      "имя файла обязано остаться: без него сводка бесполезна")
        self.assertIn("свернул", out)
        self.assertLess(len(out.splitlines()), 200,
                        "свёрнутый файл всё ещё огромен")

    def test_collapse_declares_the_withholding(self):
        """Ревьюер, не знающий, что видит не всё, одобряет невиданное."""
        diff = _diff_for("fixtures/golden.txt", [f"line {i}" for i in range(1000)])
        out = ag.condense_diff(diff)
        self.assertIn("показано не всё", out)
        self.assertIn("needs_changes", out,
                      "должен быть назван легальный выход: потребовать файл")

    def test_collapse_keeps_an_excerpt(self):
        diff = _diff_for("fixtures/golden.txt",
                         [f"line {i}" for i in range(1000)])
        out = ag.condense_diff(diff, excerpt=5)
        self.assertIn("+line 0", out)
        self.assertIn("+line 4", out)
        self.assertNotIn("+line 900", out)

    def test_counts_added_and_removed(self):
        body = "".join(f"+added {i}\n" for i in range(500))
        body += "".join(f"-removed {i}\n" for i in range(7))
        diff = (f"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n{body}")
        out = ag.condense_diff(diff)
        self.assertIn("+500", out)
        self.assertIn("−7", out)

    def test_plus_plus_plus_is_not_counted_as_addition(self):
        """`+++ b/path` — заголовок, а не добавленная строка."""
        diff = _diff_for("x.txt", [f"l{i}" for i in range(500)])
        out = ag.condense_diff(diff)
        self.assertIn("+500", out, "заголовок посчитали содержимым")

    def test_hash_distinguishes_content_beyond_the_excerpt(self):
        """Без хэша два разных эталона дают ОДНУ сводку.

        Главный тест файла, и он намеренно построен так, что различить
        входы может только хэш: одинаковая длина, одинаковое число
        добавленных строк, одинаковые первые строки. Различие спрятано на
        500-й строке — там, куда выдержка не достаёт. Свёртка, отдающая
        одинаковую сводку на разное содержимое, превращает ревью в
        фикцию: вердикт перестаёт быть привязан к тому, что одобряют.
        """
        base = [f"line {i}" for i in range(600)]
        changed = list(base)
        changed[500] = "line 500 ИЗМЕНЕНА"
        a = ag.condense_diff(_diff_for("g.txt", base), excerpt=5)
        b = ag.condense_diff(_diff_for("g.txt", changed), excerpt=5)
        self.assertEqual(len(a.splitlines()), len(b.splitlines()),
                         "тест обязан отличаться ТОЛЬКО скрытым содержимым")
        self.assertNotEqual(a, b, "разное содержимое дало одинаковую сводку")

    def test_boundary_at_limit(self):
        """Ровно `limit` строк — ещё целиком, на одну больше — уже сводка."""
        exact = _diff_for("x.txt", [f"l{i}" for i in range(94)])
        self.assertEqual(len(exact.splitlines()), 100)
        self.assertEqual(ag.condense_diff(exact, limit=100), exact.rstrip("\n"))
        over = _diff_for("x.txt", [f"l{i}" for i in range(95)])
        self.assertIn("свернул", ag.condense_diff(over, limit=100))

    def test_short_file_survives_beside_long_one(self):
        """Свёртка одного файла не должна съедать соседний.

        Ровно этот случай и есть рабочий: код теста плюс эталон рядом.
        """
        code = _diff_for("tests/golden_test.rs", ["fn main() {}", "// hi"])
        data = _diff_for("fixtures/nets.txt", [f"net {i}" for i in range(900)])
        out = ag.condense_diff(code + data)
        self.assertIn("+fn main() {}", out, "код ревьюера потерян")
        self.assertIn("+// hi", out)
        self.assertIn("diff --git a/fixtures/nets.txt", out)
        self.assertNotIn("+net 800", out, "данные не свернулись")


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True)
        (self.root / "mod.py").write_text("def f():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        self.state = st.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель", "tasks": []})

    def tearDown(self):
        self.tmp.cleanup()

    def _stashes(self):
        out = subprocess.run(["git", "stash", "list"], cwd=self.root,
                             capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if l.strip()]


class TestCleanupStash(RepoCase):
    """Терминальный исход обязан оставить дерево чистым, а работу — findable."""

    def _loop(self):
        return lp.Loop(self.state, {}, agents=None)

    def test_clean_tree_yields_no_label(self):
        self.assertIsNone(self._loop().cleanup({"id": "t1"}, "reason"))

    def test_stash_survives_intent_to_add(self):
        """Регрессия пилота: `work_diff` делает стеш невозможным.

        `git add -A -N` нужен, чтобы созданные файлы попали в `git diff`.
        Но `git stash push` поверх intent-to-add падает с «Entry ... not
        uptodate. Cannot merge», и работа исполнителя оставалась в дереве
        при статусе «стеш такой-то».
        """
        (self.root / "new_file.txt").write_text("создано исполнителем\n")
        self.state.work_diff()                      # оставляет intent-to-add
        label = self._loop().cleanup({"id": "g1nt"}, "invalid-verdict")
        self.assertEqual(label, "swarm:g1nt-invalid-verdict")
        self.assertEqual(len(self._stashes()), 1, "стеш не создан")
        dirty = subprocess.run(["git", "status", "--porcelain", "-uall"],
                               cwd=self.root, capture_output=True,
                               text=True).stdout.strip()
        self.assertEqual(dirty, "", "дерево осталось грязным")

    def test_stashed_work_is_recoverable(self):
        """Стеш бесполезен, если из него нельзя достать работу."""
        (self.root / "new_file.txt").write_text("создано исполнителем\n")
        self.state.work_diff()
        self._loop().cleanup({"id": "g1nt"}, "invalid-verdict")
        subprocess.run(["git", "stash", "pop"], cwd=self.root,
                       capture_output=True, text=True)
        self.assertEqual((self.root / "new_file.txt").read_text(),
                         "создано исполнителем\n")

    def test_failed_stash_reports_no_label(self):
        """Метка несуществующего стеша отправляет оператора искать пустоту."""
        loop = self._loop()
        (self.root / "new_file.txt").write_text("x\n")
        real_sh = loop._sh

        def broken(cmd, timeout=900):
            if cmd[:2] == ["git", "stash"]:
                return type("R", (), {"returncode": 1, "stdout": "",
                                      "stderr": "Cannot merge."})()
            return real_sh(cmd, timeout)

        loop._sh = broken
        self.assertIsNone(loop.cleanup({"id": "g1nt"}, "invalid-verdict"),
                          "стеш не создан, а метка выдана")

    def test_failed_stash_is_journalled(self):
        loop = self._loop()
        (self.root / "new_file.txt").write_text("x\n")
        real_sh = loop._sh

        def broken(cmd, timeout=900):
            if cmd[:2] == ["git", "stash"]:
                return type("R", (), {"returncode": 1, "stdout": "",
                                      "stderr": "Cannot merge."})()
            return real_sh(cmd, timeout)

        loop._sh = broken
        loop.cleanup({"id": "g1nt"}, "invalid-verdict")
        kinds = [json.loads(l)["kind"]
                 for l in self.state.journal_path.read_text().splitlines()]
        self.assertIn("stash_failed", kinds)


VALID = {"verdict": "approve", "findings": [],
         "analysis": "разобрал дифф целиком, замечаний по существу не нашлось",
         "summary": "работа соответствует спецификации"}


class TestBudgetExhausted(RepoCase):
    """Обрыв по бюджету детерминирован — ретраить его нельзя."""

    TASK = {"id": "g1nt", "title": "t", "spec": "s", "acceptance": ["ок"],
            "paths": ["mod.py"], "type": "feature"}

    def _agents(self, replies, config=None):
        agents = ag.Agents(self.state, config or {"review_budget_usd": 3.0})
        self.calls = []
        orig_run = subprocess.run

        def fake_run(argv, **kw):
            if not (argv and argv[0] == "claude"):
                return orig_run(argv, **kw)
            self.calls.append(argv)
            body = replies[min(len(self.calls), len(replies)) - 1]
            return type("R", (), {"stdout": json.dumps(body), "stderr": "",
                                  "returncode": 0})()

        subprocess.run = fake_run
        self.addCleanup(lambda: setattr(subprocess, "run", orig_run))
        return agents

    def test_budget_exhausted_is_not_retried(self):
        cut = {"is_error": True, "terminal_reason": "budget_exhausted",
               "total_cost_usd": 3.37}
        agents = self._agents([cut, cut])
        self.assertIsNone(agents.review(dict(self.TASK), "OK", 1))
        self.assertEqual(len(self.calls), 1,
                         "повтор обречён и стоит ещё столько же")

    def test_budget_exhausted_is_named(self):
        cut = {"is_error": True, "terminal_reason": "budget_exhausted",
               "total_cost_usd": 3.37}
        agents = self._agents([cut])
        agents.review(dict(self.TASK), "OK", 1)
        self.assertEqual(agents.last_review_failure, "budget_exhausted")

    def test_budget_exhausted_is_journalled(self):
        cut = {"is_error": True, "terminal_reason": "budget_exhausted",
               "total_cost_usd": 3.37}
        agents = self._agents([cut])
        agents.review(dict(self.TASK), "OK", 1)
        kinds = [json.loads(l)["kind"]
                 for l in self.state.journal_path.read_text().splitlines()]
        self.assertIn("review_budget_exhausted", kinds)

    def test_ordinary_garbage_is_still_retried(self):
        """Починка обрыва по бюджету не должна отменить обычный retry."""
        agents = self._agents([{"structured_output": {"verdict": "мусор"}},
                               {"structured_output": {"verdict": "мусор"}}])
        self.assertIsNone(agents.review(dict(self.TASK), "OK", 1))
        self.assertEqual(len(self.calls), 2, "повтор при мусоре потерян")

    def test_retry_that_succeeds_clears_the_failure(self):
        agents = self._agents([{"structured_output": {"verdict": "мусор"}},
                               {"structured_output": VALID,
                                "total_cost_usd": 0.5}])
        self.assertIsNotNone(agents.review(dict(self.TASK), "OK", 1))
        self.assertIsNone(agents.last_review_failure,
                          "прошлая неудача осталась висеть на удачном ревью")

    def test_review_prompt_gets_condensed_diff(self):
        """Ревьюер не должен получать 336 КБ сгенерированных данных."""
        (self.root / "generated.txt").write_text(
            "".join(f"строка {i}\n" for i in range(2000)))
        agents = self._agents([{"structured_output": VALID,
                                "total_cost_usd": 0.5}])
        agents.review(dict(self.TASK), "OK", 1)
        prompt = self.calls[0][self.calls[0].index("-p") + 1]
        self.assertIn("свернул", prompt)
        self.assertNotIn("строка 1900", prompt,
                         "сгенерированный файл ушёл ревьюеру целиком")


class TestReviewFailureEscalates(RepoCase):
    """Молчаливый blocked оставляет оператора без единого слова."""

    TASK = {"id": "g1nt", "title": "t", "spec": "s", "acceptance": ["ок"],
            "paths": ["mod.py"], "type": "feature", "status": "pending",
            "deps": []}

    class DeadReviewer:
        """Исполнитель отработал, ревьюер вердикта не дал."""

        last_review_failure = "budget_exhausted"

        def implement(self, task, feedback, iteration):
            return {"status": "done", "summary": "работа сделана"}

        def review(self, *a, **kw):
            return None

        def repo_map(self, task):
            return None

    def _run(self):
        self.state.save_tasks({"goal": "цель", "tasks": [dict(self.TASK)]})
        loop = lp.Loop(self.state, {"gate_command": ["true"]},
                       self.DeadReviewer())
        return loop.run_task(dict(self.TASK))

    def test_task_is_blocked(self):
        self.assertEqual(self._run(), "blocked")

    def test_operator_is_asked(self):
        self._run()
        self.assertTrue(self.state.questions(),
                        "инбокс пуст: оператор не узнает, что случилось")

    def test_question_names_the_cause(self):
        self._run()
        text = json.dumps(self.state.questions(), ensure_ascii=False)
        self.assertIn("бюджет", text,
                      "вопрос обязан назвать причину, а не факт блокировки")

    def test_failure_is_journalled(self):
        self._run()
        kinds = [json.loads(l)["kind"]
                 for l in self.state.journal_path.read_text().splitlines()]
        self.assertIn("review_failed", kinds)

    def test_status_carries_question_id(self):
        """Без ссылки на вопрос задачу и вопрос не связать."""
        self._run()
        task = [t for t in self.state.load_tasks()["tasks"]
                if t["id"] == "g1nt"][0]
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(task.get("question_id"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
