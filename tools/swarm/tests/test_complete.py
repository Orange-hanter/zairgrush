#!/usr/bin/env python3
"""Автодополнение: скрипт из парсера, кандидаты из состояния, реестр стендов.

Три обещания, которые здесь держатся тестами:

1. Tab не расходится с `--help` — скрипт выводится из парсера, и новый
   флаг или новый id-аргумент без подсказки роняет набор.
2. Tab отвечает правду о живости — тот же флок, что у доски.
3. Tab не оставляет следов и не падает — ни `.swarm/` в чужом каталоге,
   ни трассировки в строке ввода.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SW = pathlib.Path(__file__).resolve().parent.parent
ROOT_DIR = SW / "swarm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cli = sys.modules.get("cli") or _load("cli")
clitab = _load("clitab")
clicomplete = sys.modules["clicomplete"]
registry = sys.modules["registry"]


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


def _task(tid, status, title="t"):
    return {
        "id": tid,
        "title": title,
        "type": "feature",
        "status": status,
        "deps": [],
        "paths": ["src/x.py"],
        "acceptance": ["ok"],
    }


class StandCase(unittest.TestCase):
    """Стенд без git: кандидатам git не нужен, и тест это подтверждает."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".swarm" / "log").mkdir(parents=True)
        self.write_tasks(
            [
                _task("pend", "pending", "ждёт"),
                _task("blok", "blocked", "встала"),
                _task("done", "done", "закрыта"),
                _task("work", "in_progress", "идёт сейчас"),
            ]
        )
        self.reg = self.root.parent / f"{self.root.name}-roots.json"
        self._env = os.environ.get("SWARM_REGISTRY")
        os.environ["SWARM_REGISTRY"] = str(self.reg)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("SWARM_REGISTRY", None)
        else:
            os.environ["SWARM_REGISTRY"] = self._env
        self.reg.unlink(missing_ok=True)
        self.tmp.cleanup()

    def write_tasks(self, tasks, goal="цель"):
        (self.root / ".swarm" / "tasks.json").write_text(
            json.dumps({"goal": goal, "tasks": tasks}), encoding="utf-8"
        )

    def journal(self, *rows, name="log/run.jsonl"):
        with (self.root / ".swarm" / name).open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def ids(self, kind, root=None):
        lines = clitab.candidates(kind, root or self.root)
        return [ln.split("\t", 1)[0] for ln in lines]


# --- скрипт из парсера ---------------------------------------------------


def _walk(p, path=()):
    """(путь, action) по всему дереву парсера."""
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, child in a.choices.items():
                yield (path, a, name)
                yield from _walk(child, (*path, name))
        else:
            yield (path, a, None)


class TestScriptFollowsParser(unittest.TestCase):
    def setUp(self):
        self.ap = cli.build_parser()
        self.script = clicomplete.zsh_script(self.ap)

    def test_every_command_flag_and_choice_reaches_the_script(self):
        for path, a, sub in _walk(self.ap):
            if sub is not None:
                self.assertIn(
                    f"{sub}) {clicomplete._fname((*path, sub))} ;;",
                    self.script,
                    f"подкоманда {(*path, sub)}",
                )
                continue
            for opt in a.option_strings:
                self.assertIn(f"{opt}", self.script, f"флаг {path} {opt}")
            for c in a.choices or ():
                self.assertIn(str(c), self.script, f"choice {path} {c}")

    def test_every_id_argument_has_a_value_source(self):
        # Новый аргумент с id из состояния без записи в DYNAMIC молча
        # дополнялся бы ничем — ровно то, от чего скрипт выводится из парсера.
        for path, a, sub in _walk(self.ap):
            if sub is None and a.dest in clicomplete.ID_DESTS:
                self.assertIn(
                    (path, a.dest),
                    clicomplete.DYNAMIC,
                    f"{' '.join(path) or '<global>'} {a.dest}",
                )

    def test_dynamic_map_points_at_real_arguments(self):
        # Обратная сторона: переименованный dest оставил бы мёртвую запись.
        real = {(path, a.dest) for path, a, sub in _walk(self.ap) if sub is None}
        for key in clicomplete.DYNAMIC:
            self.assertIn(key, real, f"запись без аргумента: {key}")

    def test_value_hooks_are_wired(self):
        # Форма `{_swarm_dyn …}` обязательна: без скобок _arguments вставляет
        # свои опции compadd перед аргументами функции, и $1 перестаёт быть
        # видом — на живом zsh Tab внутри подкоманд молчал.
        self.assertIn(":task:{_swarm_dyn tasks:blocked ", self.script)  # retry
        self.assertIn(":id:{_swarm_dyn tasks:pending,blocked ", self.script)  # close
        self.assertIn(":run:{_swarm_dyn runs ", self.script)
        self.assertIn(":qid:{_swarm_dyn questions ", self.script)
        self.assertIn("--root=[", self.script)
        self.assertIn(":root:_swarm_roots", self.script)

    def test_command_prints_script(self):
        code, out = run_cli("completion", "zsh")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("#compdef swarm"))

    def test_install_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d) / "zf" / "_swarm"
            code, out = run_cli("completion", "zsh", "--install", str(target))
            self.assertEqual(code, 0)
            self.assertTrue(target.read_text().startswith("#compdef swarm"))
            self.assertIn("fpath=", out)


@unittest.skipUnless(shutil.which("zsh"), "нет zsh")
class TestScriptInRealZsh(StandCase):
    """Кавычки и разбиение строк проверяет только сам zsh."""

    def setUp(self):
        super().setUp()
        self.script = clicomplete.zsh_script(cli.build_parser())
        self.file = self.root / "_swarm"
        self.file.write_text(self.script, encoding="utf-8")

    def test_syntax(self):
        r = subprocess.run(
            ["zsh", "-n", str(self.file)], capture_output=True, text=True, check=False
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def _dyn(self, kind):
        """Значения и строки показа, какими их получил бы compadd."""
        # Скрипт без последней строки (`_swarm "$@"` требует compsys);
        # `_wanted` подменён печатью массивов — видно, что дошло до zsh.
        body = self.script.rsplit('_swarm "$@"', 1)[0]
        prog = body + (
            '\n_wanted() { print -rl -- "${vals[@]}" --- "${disp[@]}" }\n'
            f"_swarm_cmd={SW / 'swarm-cli'}\n"
            f"_swarm_root={self.root}\n"
            f"_swarm_dyn {kind} 'x'\n"
        )
        r = subprocess.run(
            ["zsh", "-f", "-c", prog], capture_output=True, text=True, check=False
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        vals, _, disp = r.stdout.partition("---\n")
        return vals.splitlines(), disp.splitlines()

    def test_dyn_reaches_candidates_through_the_wrapper(self):
        vals, disp = self._dyn("tasks:blocked")
        self.assertEqual(vals, ["blok"])
        self.assertEqual(disp, ["blok  -- заблокирована · встала"])

    def test_equal_descriptions_keep_order_and_stay_apart(self):
        # На живом zsh `_describe` склеил прогоны одной минуты без задач в
        # одну строку и пересортировал список: старые оказались первыми.
        for rid in ("20260901T100000-aaaaaa", "20260901T100005-bbbbbb",
                    "20260901T100009-cccccc"):
            self.journal({"run_id": rid, "ts": "2026-09-01T10:00:30+00:00"},
                         name="metrics.jsonl")
        vals, disp = self._dyn("runs")
        self.assertEqual(vals, ["20260901T100009-cccccc", "20260901T100005-bbbbbb",
                                "20260901T100000-aaaaaa"])
        self.assertEqual(len(disp), 3)


# --- кандидаты -----------------------------------------------------------


class TestTaskCandidates(StandCase):
    def test_filter_by_status(self):
        self.assertEqual(self.ids("tasks:blocked"), ["blok"])
        self.assertEqual(sorted(self.ids("tasks:pending,blocked")), ["blok", "pend"])

    def test_active_first_closed_last(self):
        self.assertEqual(self.ids("tasks"), ["work", "blok", "pend", "done"])

    def test_description_names_status_and_title(self):
        line = clitab.candidates("tasks:blocked", self.root)[0]
        self.assertEqual(line, "blok\tзаблокирована · встала")

    def test_corrupt_file_is_silence_not_crash(self):
        (self.root / ".swarm" / "tasks.json").write_text("{не json")
        self.assertEqual(clitab.candidates("tasks", self.root), [])
        self.write_tasks(["строка вместо задачи", {"title": "без id"}])
        self.assertEqual(clitab.candidates("tasks", self.root), [])


class TestJournalCandidates(StandCase):
    def test_open_questions_first(self):
        self.journal(
            {"kind": "question", "qid": "q001", "task": "pend", "question": "а?"},
            {"kind": "question", "qid": "q002", "task": "blok", "question": "б?"},
            {"kind": "answer", "qid": "q001", "text": "да"},
        )
        self.assertEqual(self.ids("questions"), ["q002", "q001"])

    def test_dropped_and_foreign_goal_policies_are_hidden(self):
        self.journal(
            {"kind": "policy", "pid": "p001", "text": "живая", "goal": "цель"},
            {"kind": "policy", "pid": "p002", "text": "снята", "goal": "цель"},
            {"kind": "policy_dropped", "pid": "p002"},
            {"kind": "policy", "pid": "p003", "text": "чужая", "goal": "другая"},
        )
        self.assertEqual(self.ids("policies"), ["p001"])

    def test_tombstoned_lessons_are_hidden(self):
        (self.root / ".swarm" / "memory").mkdir()
        self.journal(
            {"rec": "lesson", "id": "l1", "body": "урок", "outcome": "useful"},
            {"rec": "lesson", "id": "l2", "body": "забыт", "outcome": "useful"},
            {"rec": "tombstone", "id": "l2"},
            name="memory/lessons.jsonl",
        )
        self.assertEqual(self.ids("lessons"), ["l1"])

    def test_colon_in_value_survives(self):
        # Разделитель — табуляция: двоеточие в значении больше не особое.
        self.write_tasks([_task("a:b", "pending")])
        self.assertTrue(clitab.candidates("tasks", self.root)[0].startswith("a:b\t"))


class TestRunCandidates(StandCase):
    def setUp(self):
        super().setUp()
        self.journal(
            {
                "run_id": "20260901T100000-aaaaaa",
                "task": "pend",
                "ts": "2026-09-01T10:00:00+00:00",
            },
            name="metrics.jsonl",
        )
        self.journal(
            {
                "run_id": "20260903T100000-cccccc",
                "task": "work",
                "ts": "2026-09-03T10:00:00+00:00",
            },
            name="metrics.jsonl",
        )
        self.journal(
            {
                "kind": "x",
                "run_id": "20260902T100000-bbbbbb",
                "task": "blok",
                "ts": "2026-09-02T10:00:00+00:00",
            }
        )

    def test_newest_first_across_journal_and_metrics(self):
        self.assertEqual(
            self.ids("runs"),
            [
                "20260903T100000-cccccc",
                "20260902T100000-bbbbbb",
                "20260901T100000-aaaaaa",
            ],
        )

    def test_nothing_is_live_without_the_lock(self):
        lines = clitab.candidates("runs", self.root)
        self.assertTrue(all("завершён" in ln for ln in lines), lines)
        # Файл замка, оставшийся от прошлого прогона, живости не означает.
        (self.root / ".swarm" / "state.lock").write_text("12345\n")
        self.assertNotIn("идёт", "\n".join(clitab.candidates("runs", self.root)))

    def test_held_lock_marks_newest_run_live(self):
        st = cli.state_mod.SwarmState(self.root)
        st.acquire()
        try:
            lines = clitab.candidates("runs", self.root)
        finally:
            st.release()
        self.assertTrue(lines[0].startswith("20260903T100000-cccccc\tидёт"), lines)
        self.assertTrue(all("завершён" in ln for ln in lines[1:]), lines)

    def test_tail_read_drops_the_cut_line(self):
        path = self.root / ".swarm" / "metrics.jsonl"
        # Хвост длиннее последней строки, но короче двух: начало окна
        # приходится на середину первой — её обрезок не должен всплыть.
        last = len(path.read_bytes().splitlines()[-1]) + 1
        rows = clitab._tail_rows(path, limit=last + 10)
        self.assertEqual([r["run_id"] for r in rows], ["20260903T100000-cccccc"])


class TestRegistry(StandCase):
    def test_cli_call_in_a_stand_registers_it(self):
        code, _ = run_cli("--root", str(self.root), "status")
        self.assertEqual(code, 0)
        self.assertIn(str(self.root.resolve()), registry.load())

    def test_loop_taking_the_lock_registers_a_fresh_stand(self):
        # Первый прогон в репозитории без `.swarm/`: отметка в cli.main
        # его не видит, а искать через Tab будут именно его.
        with tempfile.TemporaryDirectory() as d:
            st = cli.state_mod.SwarmState(d)
            self.assertNotIn(str(pathlib.Path(d).resolve()), registry.load())
            st.acquire()
            st.release()
            self.assertIn(str(pathlib.Path(d).resolve()), registry.load())

    def test_plain_directory_is_not_registered(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(registry.touch(d))
            self.assertNotIn(str(pathlib.Path(d).resolve()), registry.load())

    def test_fresh_mark_is_not_rewritten(self):
        self.assertTrue(registry.touch(self.root))
        self.assertFalse(registry.touch(self.root))

    def test_vanished_stand_drops_out(self):
        with tempfile.TemporaryDirectory() as d:
            gone = pathlib.Path(d)
            (gone / "swarm.toml").write_text("")
            registry.touch(gone)
            self.assertIn(gone.resolve(), [p for p, _ in registry.roots()])
        self.assertNotIn(gone.resolve(), [p for p, _ in registry.roots()])
        registry.touch(self.root)
        self.assertNotIn(str(gone.resolve()), registry.load())

    def test_corrupt_registry_is_empty_not_fatal(self):
        self.reg.write_text("[1, 2")
        self.assertEqual(registry.roots(), [])
        self.assertTrue(registry.touch(self.root))

    def test_live_stand_first(self):
        with tempfile.TemporaryDirectory() as d:
            idle = pathlib.Path(d)
            (idle / "swarm.toml").write_text("")
            registry.touch(idle)
            registry.touch(self.root)
            st = cli.state_mod.SwarmState(self.root)
            st.acquire()
            try:
                lines = clitab.candidates("roots", self.root)
            finally:
                st.release()
        self.assertTrue(lines[0].startswith(str(self.root.resolve())), lines)
        self.assertIn("● идёт", lines[0])
        self.assertIn("простаивает", lines[1])

    def test_idle_stands_ordered_by_last_run_not_registry_mark(self):
        with tempfile.TemporaryDirectory() as d:
            old = pathlib.Path(d)
            (old / ".swarm").mkdir()
            (old / ".swarm" / "metrics.jsonl").write_text(
                json.dumps(
                    {"run_id": "20250101T000000-000000", "ts": "2025-01-01T00:00:00"}
                )
                + "\n"
            )
            self.journal(
                {"run_id": "20260901T000000-111111", "ts": "2026-09-01T00:00:00"},
                name="metrics.jsonl",
            )
            registry.touch(self.root)
            registry.touch(old)  # отмечен позже, но работал раньше
            lines = clitab.candidates("roots", self.root)
        self.assertTrue(lines[0].startswith(str(self.root.resolve())), lines)
        self.assertIn("2026-09-01", lines[0])
        self.assertIn("2025-01-01", lines[1])

    def test_scan_finds_nested_stands(self):
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "swarm.toml").write_text("")
        found = registry.scan(self.root)
        self.assertIn((self.root / "a" / "b").resolve(), found)
        self.assertIn(str((self.root / "a" / "b").resolve()), registry.load())


class TestNoTraceNoCrash(StandCase):
    def test_helper_does_not_import_the_swarm(self):
        code = (
            "import sys; sys.argv=['x']; "
            f"sys.path.insert(0, {str(ROOT_DIR)!r}); import clitab; "
            "heavy = {'cli', 'obs', 'state', 'board', 'loop'}; "
            "print(sorted(heavy & set(sys.modules)))"
        )
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual(r.stdout.strip(), "[]")

    def test_foreign_directory_stays_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            for kind in ("tasks", "runs", "questions", "policies", "lessons"):
                self.assertEqual(clitab.candidates(kind, pathlib.Path(d)), [])
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])

    def test_failure_prints_nothing_and_exits_zero(self):
        real = clitab.candidates

        def boom(*_a):
            raise RuntimeError("сломано")

        clitab.candidates = boom
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = clitab.main(["__complete", "tasks", "--root", str(self.root)])
        finally:
            clitab.candidates = real
        self.assertEqual((code, buf.getvalue()), (0, ""))

    def test_wrapper_routes_complete_past_cli(self):
        r = subprocess.run(
            [
                "sh",
                str(SW / "swarm-cli"),
                "__complete",
                "tasks:blocked",
                "--root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(r.stdout.strip(), "blok\tзаблокирована · встала")

    def test_cli_forwards_hidden_command(self):
        code, out = run_cli("__complete", "tasks:done", "--root", str(self.root))
        self.assertEqual((code, out.strip()), (0, "done\tзакрыта · закрыта"))



def _pty_zsh(env, cwd, script, keys, settle=0.8):
    """Прогнать клавиши через интерактивный `zsh -f` в псевдотерминале.

    Меню выбора и вставку совпадения видит только настоящий zle: вызовом
    функций из `zsh -c` их не проверить (две прошлые поломки Tab прошли
    мимо именно таких тестов). Возвращает вывод терминала без ESC-кодов.
    """
    import pty
    import re
    import select
    import time

    def drain(fd, quiet):
        out, last, start = b"", time.time(), time.time()
        while time.time() - start < 15:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                last = time.time()
            elif time.time() - last > quiet:
                break
        return out.decode("utf-8", "replace")

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover — дочерний процесс
        os.chdir(cwd)
        os.execve(shutil.which("zsh"), ["zsh", "-f", "-i"], env)  # noqa: S606 — сам zsh и нужен
    out = ""
    try:
        drain(fd, settle)
        os.write(fd, script.encode() + b"\n")
        drain(fd, settle)
        for k in keys:
            os.write(fd, k)
            out += drain(fd, settle)
        os.write(fd, b"exit\n")
        drain(fd, 0.3)
    finally:
        os.close(fd)
        os.waitpid(pid, 0)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\r", "", out)


@unittest.skipUnless(shutil.which("zsh"), "нет zsh")
class TestMenuFromFirstTab(unittest.TestCase):
    """Стрелки работают с первого Tab — на настоящем zle, без .zshrc."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.zfunc, self.bin, self.cwd = base / "zf", base / "bin", base / "cwd"
        for d in (self.zfunc, self.bin, self.cwd):
            d.mkdir()
        (self.zfunc / "_swarm").write_text(
            clicomplete.zsh_script(cli.build_parser()), encoding="utf-8")
        (self.bin / "swarm").symlink_to(SW / "swarm-cli")
        reg = base / "roots.json"
        # Два стенда: «новый» работал позже — в меню он первый, «старый» второй.
        self.stands = []
        for name, day in (("новый", "2026-09-02"), ("старый", "2026-09-01")):
            st = base / name
            (st / ".swarm").mkdir(parents=True)
            (st / ".swarm" / "metrics.jsonl").write_text(json.dumps(
                {"run_id": day.replace("-", "") + "T100000-aaaaaa",
                 "ts": f"{day}T10:00:00+00:00"}) + "\n")
            self.stands.append(st.resolve())
        reg.write_text(json.dumps(
            {str(s): {"seen": "2026-09-03T00:00:00+00:00"} for s in self.stands}))
        self.env = dict(
            os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
            SWARM_REGISTRY=str(reg), HOME=str(base),
            TERM="xterm-256color", LINES="40", COLUMNS="200")
        self.env.pop("ZDOTDIR", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_tab_then_arrow_picks_the_second_stand(self):
        # Стиль oh-my-zsh (lib/completion.zsh) — как у оператора: наш
        # стиль обязан его перебить, иначе меню ждёт второго Tab.
        setup = (f"fpath=({self.zfunc} $fpath); autoload -Uz compinit; "
                 "compinit -u -D; PS1='> '; "
                 "zstyle ':completion:*:*:*:*:*' menu select")
        out = _pty_zsh(self.env, self.cwd, setup, [
            b"swarm --root ", b"\t", b"\x1b[B", b"\r",  # Tab, вниз, Enter
            b"\x01echo PICKED: \r",                      # ^A, echo, выполнить
        ])
        picked = [ln for ln in out.splitlines() if ln.startswith("PICKED:")]
        self.assertTrue(picked, out[-1500:])
        self.assertEqual(picked[-1].strip(),
                         f"PICKED: swarm --root {self.stands[1]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
