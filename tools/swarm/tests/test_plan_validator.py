#!/usr/bin/env python3
"""Golden-тесты механической валидации план-диффа (§3.1).

Валидатор — единственное, что стоит между планировщиком и очередью задач:
если он пропускает мусор, петля исполняет мусор. Запуск:
    python3 -m unittest test_plan_validator -v
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
PLAN = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("planner", ROOT_DIR / "planner.py")
rp = importlib.util.module_from_spec(spec)
sys.modules["run_planner"] = rp
spec.loader.exec_module(rp)

QUEUE = [
    {"id": "aaaa", "title": "существующая", "type": "feature", "paths": ["a.py"],
     "acceptance": ["ok"], "deps": [], "status": "done"},
    {"id": "bbbb", "title": "вторая", "type": "feature", "paths": ["b.py"],
     "acceptance": ["ok"], "deps": ["aaaa"], "status": "blocked"},
]


def task(tid="cccc", **kw):
    t = {"id": tid, "title": "t", "type": "feature", "milestone": "M", "gate": "full",
         "paths": ["c.py"], "test_module": "tests.test_c", "spec": "s",
         "acceptance": ["ok"], "deps": [], "status": "pending"}
    t.update(kw)
    return t


def diff(*ops):
    return {"analysis": "a", "summary": "s", "ops": list(ops)}


def add(t, tid=None):
    return {"op": "add", "id": tid or t["id"], "task": t, "reason": "r"}


class TestValidPlans(unittest.TestCase):
    def test_minimal_add_is_valid(self):
        self.assertEqual(rp.validate_plan_diff(diff(add(task())), QUEUE), [])

    def test_update_existing_is_valid(self):
        d = diff({"op": "update", "id": "bbbb",
                  "task": task("bbbb", status="pending"), "reason": "r"})
        self.assertEqual(rp.validate_plan_diff(d, QUEUE), [])

    def test_partial_update_status_only_is_valid(self):
        """Дефект: валидатор требовал paths+acceptance в КАЖДОМ update,
        хотя применение сливает поля и частичный дифф законен — минимальный
        {"status": "pending"} отвергался на корректном плане."""
        d = diff({"op": "update", "id": "bbbb",
                  "task": {"status": "pending"}, "reason": "r"})
        self.assertEqual(rp.validate_plan_diff(d, QUEUE), [])

    def test_dep_on_added_task_is_valid(self):
        d = diff(add(task("cccc")), add(task("dddd", deps=["cccc"])))
        self.assertEqual(rp.validate_plan_diff(d, QUEUE), [])

    def test_remove_existing_is_valid(self):
        d = diff({"op": "remove", "id": "bbbb", "reason": "r"})
        self.assertEqual(rp.validate_plan_diff(d, QUEUE), [])


class TestRejectedPlans(unittest.TestCase):
    def assertRejected(self, d, needle):
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(errs, "дифф должен быть отвергнут")
        self.assertTrue(any(needle in e for e in errs),
                        f"{needle!r} не найдено в {errs}")

    def test_duplicate_id(self):
        self.assertRejected(diff(add(task("aaaa"))), "id уже существует")

    def test_empty_paths(self):
        self.assertRejected(diff(add(task(paths=[]))), "пустой paths")

    def test_empty_acceptance(self):
        self.assertRejected(diff(add(task(acceptance=[]))), "пустой acceptance")

    def test_illegal_status(self):
        self.assertRejected(diff(add(task(status="in_review"))), "недопустимый статус")

    def test_update_of_missing_task(self):
        d = diff({"op": "update", "id": "zzzz", "task": task("zzzz"), "reason": "r"})
        self.assertRejected(d, "цель не существует")

    def test_remove_of_missing_task(self):
        self.assertRejected(diff({"op": "remove", "id": "zzzz", "reason": "r"}),
                            "цель не существует")

    def test_update_wiping_acceptance(self):
        bad = task("bbbb")
        bad["acceptance"] = []
        d = diff({"op": "update", "id": "bbbb", "task": bad, "reason": "r"})
        self.assertRejected(d, "обнуляет acceptance")

    def test_update_wiping_paths(self):
        d = diff({"op": "update", "id": "bbbb", "task": {"paths": []},
                  "reason": "r"})
        self.assertRejected(d, "обнуляет paths")

    def test_dep_on_nonexistent_task(self):
        self.assertRejected(diff(add(task(deps=["zzzz"]))), "не существует")

    def test_dep_on_removed_task(self):
        d = diff({"op": "remove", "id": "aaaa", "reason": "r"},
                 add(task(deps=["aaaa"])))
        self.assertRejected(d, "не существует")

    def test_dependency_cycle(self):
        d = diff(add(task("cccc", deps=["dddd"])), add(task("dddd", deps=["cccc"])))
        self.assertRejected(d, "цикл")

    def test_self_dependency(self):
        self.assertRejected(diff(add(task("cccc", deps=["cccc"]))), "цикл")

    def test_empty_ops(self):
        self.assertRejected(diff(), "пустой дифф")

    def test_unknown_operation(self):
        self.assertRejected(diff({"op": "reorder", "id": "aaaa", "reason": "r"}),
                            "неизвестная операция")

    def test_task_id_mismatch(self):
        self.assertRejected(diff(add(task("cccc"), tid="dddd")), "task.id != op.id")

    def test_non_dict_diff(self):
        for bad in (None, [], "ops"):
            self.assertTrue(rp.validate_plan_diff(bad, QUEUE))


class TestApply(unittest.TestCase):
    def test_add_appends_and_keeps_order(self):
        out = rp.apply_plan_diff(diff(add(task("cccc"))), QUEUE)
        self.assertEqual([t["id"] for t in out], ["aaaa", "bbbb", "cccc"])

    def test_update_merges_fields(self):
        d = diff({"op": "update", "id": "bbbb",
                  "task": {"status": "pending", "paths": ["b.py", "n.py"]},
                  "reason": "r"})
        out = rp.apply_plan_diff(d, QUEUE)
        got = next(t for t in out if t["id"] == "bbbb")
        self.assertEqual(got["status"], "pending")
        self.assertEqual(got["paths"], ["b.py", "n.py"])
        self.assertEqual(got["title"], "вторая", "update не должен терять поля")

    def test_remove_drops_task(self):
        out = rp.apply_plan_diff(
            diff({"op": "remove", "id": "aaaa", "reason": "r"}), QUEUE)
        self.assertEqual([t["id"] for t in out], ["bbbb"])

    def test_input_queue_not_mutated(self):
        rp.apply_plan_diff(diff({"op": "remove", "id": "aaaa", "reason": "r"}), QUEUE)
        self.assertEqual([t["id"] for t in QUEUE], ["aaaa", "bbbb"])


class TestApplyIsolation(unittest.TestCase):
    """Применение диффа обязано работать с копией.

    Мутационный аудит показал, что поверхностной проверки id мало:
    очередь могла меняться вглубь, и вызывающий получал испорченные
    задачи, не подозревая об этом.
    """

    def test_update_does_not_touch_source_task(self):
        queue = [dict(t) for t in QUEUE]
        d = diff({"op": "update", "id": "bbbb",
                  "task": {"status": "pending", "title": "новое"}, "reason": "r"})
        rp.apply_plan_diff(d, queue)
        self.assertEqual(queue[1]["status"], "blocked",
                         "исходная задача не должна меняться")
        self.assertEqual(queue[1]["title"], "вторая")

    def test_result_is_independent_of_source(self):
        queue = [dict(t) for t in QUEUE]
        out = rp.apply_plan_diff(diff(add(task("cccc"))), queue)
        out[0]["title"] = "изменено в результате"
        self.assertEqual(queue[0]["title"], "существующая")

    def test_paths_list_not_shared(self):
        queue = [dict(t, paths=["a.py"]) for t in QUEUE]
        out = rp.apply_plan_diff(diff(add(task("cccc"))), queue)
        out[0]["paths"].append("b.py")
        self.assertEqual(queue[0]["paths"], ["a.py"],
                         "вложенные структуры тоже не должны быть общими")


class TestConflictingOps(unittest.TestCase):
    """Дифф — не набор независимых операций: их порядок и пересечения
    надо проверить ДО применения.

    Все четыре случая ниже валидатор раньше пропускал: два роняли
    оркестратор исключением, один тихо портил очередь, один давал
    ложный отказ на корректном плане.
    """

    def test_remove_then_update_same_task(self):
        d = diff({"op": "remove", "id": "bbbb", "reason": "r"},
                 {"op": "update", "id": "bbbb", "task": task("bbbb"), "reason": "r"})
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(errs, "конфликт операций обязан быть виден валидатору")
        self.assertTrue(any("повторная операция" in e for e in errs), errs)

    def test_conflicting_ops_would_crash_apply(self):
        """Показывает цену пропуска: применение падает KeyError."""
        d = diff({"op": "remove", "id": "bbbb", "reason": "r"},
                 {"op": "update", "id": "bbbb", "task": task("bbbb"), "reason": "r"})
        with self.assertRaises(KeyError):
            rp.apply_plan_diff(d, QUEUE)

    def test_two_updates_of_same_task(self):
        d = diff({"op": "update", "id": "bbbb", "task": task("bbbb"), "reason": "r"},
                 {"op": "update", "id": "bbbb", "task": task("bbbb"), "reason": "r"})
        self.assertTrue(rp.validate_plan_diff(d, QUEUE))

    def test_update_may_not_change_id(self):
        """Смена id рвёт deps других задач и рассогласует ключ с телом."""
        d = diff({"op": "update", "id": "bbbb", "task": task("ZZZZ"), "reason": "r"})
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(any("меняет id" in e for e in errs), errs)

    def test_update_without_id_field_is_fine(self):
        d = diff({"op": "update", "id": "bbbb", "reason": "r",
                  "task": {"paths": ["b.py"], "acceptance": ["ok"],
                           "status": "pending"}})
        self.assertEqual(rp.validate_plan_diff(d, QUEUE), [])

    def test_diff_without_analysis_rejected(self):
        """analysis печатается наравне с reason — без него падал вывод."""
        d = diff(add(task()))
        del d["analysis"]
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(any("analysis" in e for e in errs), errs)

    def test_blank_analysis_rejected(self):
        d = diff(add(task()))
        d["analysis"] = "   "
        self.assertTrue(rp.validate_plan_diff(d, QUEUE))

    def test_op_without_reason_rejected(self):
        """main() печатает reason: без него падает вывод плана."""
        d = diff({"op": "add", "id": "cccc", "task": task()})
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(any("reason" in e for e in errs), errs)

    def test_removed_task_deps_do_not_block_plan(self):
        """Ложный отказ: удаляем обе задачи, а deps между ними считались
        нарушением — планировщик получал ошибку на корректном плане."""
        queue = [*QUEUE, task("dddd", deps=["eeee"]), task("eeee")]
        d = diff({"op": "remove", "id": "dddd", "reason": "r"},
                 {"op": "remove", "id": "eeee", "reason": "r"})
        self.assertEqual(rp.validate_plan_diff(d, queue), [])

    def test_dangling_dep_after_removal_still_caught(self):
        """Обратная сторона: если ссылка остаётся живой, отказ обязан быть."""
        queue = [*QUEUE, task("dddd", deps=["eeee"]), task("eeee")]
        d = diff({"op": "remove", "id": "eeee", "reason": "r"})
        errs = rp.validate_plan_diff(d, queue)
        self.assertTrue(any("eeee" in e for e in errs), errs)

    def test_update_to_illegal_status_rejected(self):
        d = diff({"op": "update", "id": "bbbb", "reason": "r",
                  "task": {"paths": ["b.py"], "acceptance": ["ok"],
                           "status": "почти_готово"}})
        self.assertTrue(rp.validate_plan_diff(d, QUEUE))

    def test_update_with_non_dict_task(self):
        d = diff({"op": "update", "id": "bbbb", "task": "строка", "reason": "r"})
        self.assertTrue(rp.validate_plan_diff(d, QUEUE))

    def test_added_task_is_copied_not_aliased(self):
        d = diff(add(task("cccc")))
        out = rp.apply_plan_diff(d, QUEUE)
        out[-1]["paths"].append("hack.py")
        self.assertEqual(d["ops"][0]["task"]["paths"], ["c.py"],
                         "правка результата не должна менять сам дифф")


class TestGarbageResilience(unittest.TestCase):
    """Мусор от модели обязан становиться СПИСКОМ ОШИБОК, а не исключением:
    TypeError/KeyError пролетал сквозь plan_with_retry и убивал `swarm go`
    вместо честной повторной попытки."""

    def assertRejected(self, d, needle):
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(errs, "дифф должен быть отвергнут")
        self.assertTrue(any(needle in e for e in errs),
                        f"{needle!r} не найдено в {errs}")

    def test_add_without_id_is_error_not_crash(self):
        """add без id раньше проскакивал валидацию (tid=None обходил
        touched и уникальность), а применение падало KeyError."""
        self.assertRejected(diff({"op": "add", "task": task(), "reason": "r"}),
                            "без id")

    def test_update_without_id_is_error(self):
        self.assertRejected(
            diff({"op": "update", "task": {"status": "pending"}, "reason": "r"}),
            "без id")

    def test_non_string_id_is_error(self):
        self.assertRejected(diff({"op": "remove", "id": 7, "reason": "r"}),
                            "без id")

    def test_empty_string_id_is_error(self):
        self.assertRejected(diff({"op": "remove", "id": "", "reason": "r"}),
                            "без id")

    def test_added_id_must_match_promised_format(self):
        """Промпт обещает id в 4 символа латиницы+цифр — обещание без
        механической проверки остаётся пожеланием."""
        self.assertRejected(diff(add(task("task-01"))), "4 символа")

    def test_unhashable_deps_element_is_error_not_typeerror(self):
        """dict внутри deps ронял валидатор TypeError на `d not in universe`."""
        d = diff(add(task("cccc", deps=[{"id": "aaaa"}])))
        errs = rp.validate_plan_diff(d, QUEUE)  # главное — не исключение
        self.assertTrue(any("deps" in e for e in errs), errs)

    def test_non_list_deps_is_error(self):
        d = diff(add(task("cccc", deps="aaaa")))
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(any("deps" in e for e in errs), errs)

    def test_non_dict_op_is_error(self):
        errs = rp.validate_plan_diff(diff("строка"), QUEUE)
        self.assertTrue(any("не объект" in e for e in errs), errs)


class TestDoneIsOrchestratorsOnly(unittest.TestCase):
    """Дефект: список легальных статусов включал done — план-дифф мог
    добавить задачу сразу «сделанной» или перевести её в done, объявляя
    работу выполненной в обход gate и ревью."""

    def assertRejected(self, d, needle):
        errs = rp.validate_plan_diff(d, QUEUE)
        self.assertTrue(any(needle in e for e in errs),
                        f"{needle!r} не найдено в {errs}")

    def test_add_with_done_rejected(self):
        self.assertRejected(diff(add(task(status="done"))),
                            "недопустимый статус")

    def test_update_to_done_rejected(self):
        d = diff({"op": "update", "id": "bbbb", "task": {"status": "done"},
                  "reason": "r"})
        self.assertRejected(d, "недопустимый статус")

    def test_schema_enum_agrees_with_validator(self):
        """Схема — обещание модели: done не должен предлагаться в enum."""
        schema = json.loads(rp.SCHEMA)
        enum = (schema["properties"]["ops"]["items"]["properties"]
                ["task"]["properties"]["status"]["enum"])
        self.assertEqual(set(enum), rp.PLAN_STATUS)


class TestRepoMap(unittest.TestCase):
    """Карта репозитория: список файлов и состояние сьюта, оба идут прямо
    в промпт планировщика."""

    def setUp(self):
        self.orig_run = rp.subprocess.run
        self.tmp = tempfile.TemporaryDirectory()
        self.stand = pathlib.Path(self.tmp.name)
        (self.stand / "app.py").write_text("x = 1\n")

    def tearDown(self):
        rp.subprocess.run = self.orig_run
        self.tmp.cleanup()

    def fake_suite(self):
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw)
            return type("R", (), {"stdout": "OK\n", "stderr": ""})()
        rp.subprocess.run = fake
        return seen

    def test_suite_run_is_bounded_in_time(self):
        """Дефект: у прогона сьюта не было timeout — один зависший тест
        стенда держал `swarm plan`/`go` вечно и молча."""
        seen = self.fake_suite()
        rp.repo_map(self.stand)
        self.assertEqual(seen.get("timeout"), rp.SUITE_TIMEOUT)

    def test_hanging_suite_degrades_to_honest_unknown(self):
        def hang(cmd, **kw):
            raise rp.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        rp.subprocess.run = hang
        files, suite = rp.repo_map(self.stand)
        self.assertIn("неизвестно", suite,
                      "в промпт идёт честное «неизвестно», а не зависание")
        self.assertIn("app.py", files, "список файлов не теряется")

    def test_junk_dirs_excluded_from_listing(self):
        """Дефект: rglob без исключений уносил в промпт весь .venv стенда
        — тысячи файлов site-packages жгли бюджет планировщика."""
        for junk in (".venv/lib/site.py", "node_modules/m/i.py",
                     ".swarm/raw/x.py", "build/gen.py"):
            p = self.stand / junk
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("pass\n")
        self.fake_suite()
        files, _ = rp.repo_map(self.stand)
        self.assertEqual(files, "app.py")


class TestRetryMessaging(unittest.TestCase):
    """Дефект: на таймауте первой попытки UI объявлял «план-дифф
    невалиден» (ложный класс диагноза), а промпт ретрая получал инструкцию
    ОПЕРАТОРУ «подними plan_timeout в swarm.toml», адресованную модели."""

    def setUp(self):
        self.orig = rp.call_planner
        self.calls = []
        self.ui = []

    def tearDown(self):
        rp.call_planner = self.orig

    def planner_replies(self, results):
        seq = iter(results)

        def fake(prompt, mode, attempt=1, **kw):
            self.calls.append(prompt)
            return next(seq)
        rp.call_planner = fake

    def test_timeout_is_not_reported_as_invalid_diff(self):
        good = diff(add(task()))
        self.planner_replies([(None, "timeout"), (good, None)])
        got, errs, _ = rp.plan_with_retry("П", "plan", QUEUE,
                                          ui=self.ui.append)
        self.assertIs(got, good)
        self.assertEqual(errs, [])
        self.assertTrue(any("timeout" in m for m in self.ui), self.ui)
        self.assertFalse(any("невалиден" in m for m in self.ui), self.ui)

    def test_timeout_retry_keeps_prompt_clean(self):
        self.planner_replies([(None, "timeout"), (diff(add(task())), None)])
        rp.plan_with_retry("ПРОМПТ", "plan", QUEUE, ui=lambda s: None)
        self.assertEqual(self.calls[1], "ПРОМПТ",
                         "модели нечего исправлять — повтор с исходным "
                         "промптом, без советов оператору")

    def test_invalid_diff_still_feeds_errors_back(self):
        bad = diff({"op": "reorder", "id": "aaaa", "reason": "r"})
        good = diff(add(task()))
        self.planner_replies([(bad, None), (good, None)])
        got, _, _ = rp.plan_with_retry("П", "plan", QUEUE, ui=self.ui.append)
        self.assertIs(got, good)
        self.assertTrue(any("невалиден" in m for m in self.ui), self.ui)
        self.assertIn("Ошибки прошлой попытки", self.calls[1])


class TestStandaloneMain(unittest.TestCase):
    """Дефект: standalone main() не передавал root=stand — claude
    запускался в каталоге процесса, а артефакты и метрики падали в каталог
    пакета; CLI петли root передаёт, и два входа молча расходились."""

    def setUp(self):
        self.orig_pwr = rp.plan_with_retry
        self.orig_metric = rp.metric
        self.orig_map = rp.repo_map
        self.orig_argv = sys.argv

    def tearDown(self):
        rp.plan_with_retry = self.orig_pwr
        rp.metric = self.orig_metric
        rp.repo_map = self.orig_map
        sys.argv = self.orig_argv

    def test_main_routes_artifacts_to_stand(self):
        roots = {}

        def fake_pwr(prompt, mode, tasks, root=None, **kw):
            roots["plan"] = root
            return None, ["стоп"], "no_output"

        def fake_metric(root=None, **row):
            roots["metric"] = root
        rp.plan_with_retry = fake_pwr
        rp.metric = fake_metric
        rp.repo_map = lambda stand: ("", "OK")
        with tempfile.TemporaryDirectory() as tmp:
            tasks_file = pathlib.Path(tmp) / "tasks.json"
            tasks_file.write_text('{"tasks": []}')
            sys.argv = ["planner", "plan", "--stand", tmp,
                        "--tasks", str(tasks_file),
                        "--out", str(pathlib.Path(tmp) / "out.json"),
                        "--goal", "g"]
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit):
                rp.main()
            self.assertEqual(roots["plan"], tmp)
            self.assertEqual(roots["metric"], tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
