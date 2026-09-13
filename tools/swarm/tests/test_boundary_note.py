#!/usr/bin/env python3
"""Предревью-заметка о границах «заявление задачи ↔ дифф» (REV-005, P4).

Доказательная база — те же семь удовлетворённых споров PILOT-1, что у
boundary-линтера, но на другом конце трубы: линтер помогает ПЛАНУ
расширить paths до старта, страж gitops.scope_check ловит нарушение на
ИСПОЛНИТЕЛЕ ценой раунда, а эта заметка поднимает ту же геометрию границ
до ВЫЗОВА ревьюера. Тесты держат три свойства, без которых заметка
вредна: она называет только то, что видит дифф; она говорит на том же
диалекте границ, что и страж (явный защищённый паттерн отпирает,
широкий глоб — нет); и она молчит на чистом диффе — промпт ревьюера
обязан остаться байт-в-байт прежним, иначе «fail-open» превращается в
«тихий мутатор промпта».
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

spec = importlib.util.spec_from_file_location("boundarynote",
                                              ROOT_DIR / "boundarynote.py")
bn = importlib.util.module_from_spec(spec)
sys.modules["boundarynote"] = bn
spec.loader.exec_module(bn)

spec = importlib.util.spec_from_file_location("promptbuilder",
                                              ROOT_DIR / "promptbuilder.py")
pb = importlib.util.module_from_spec(spec)
sys.modules["promptbuilder"] = pb
spec.loader.exec_module(pb)

spec = importlib.util.spec_from_file_location("cli", ROOT_DIR / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.modules["cli"] = cli
spec.loader.exec_module(cli)

spec = importlib.util.spec_from_file_location("reviewer",
                                              ROOT_DIR / "reviewer.py")
rv = importlib.util.module_from_spec(spec)
sys.modules["reviewer"] = rv
spec.loader.exec_module(rv)

TASK = {"id": "t1", "title": "t", "paths": ["wordstat/roman.py"],
        "acceptance": ["гейт зелёный"]}


class BoundaryNoteTest(unittest.TestCase):
    def test_outside_paths_files_are_named(self):
        note = bn.boundary_note(
            TASK, ["wordstat/roman.py", "wordstat/stats.py"])
        self.assertIsNotNone(note)
        self.assertIn("wordstat/stats.py", note or "")
        self.assertIn("вне объявленных paths", note or "")

    def test_clean_diff_is_silent(self):
        self.assertIsNone(bn.boundary_note(
            TASK, ["wordstat/roman.py"]))

    def test_no_paths_declared_skips_outside_signal(self):
        note = bn.boundary_note({"id": "t", "paths": []},
                                ["anywhere/stats.py"])
        self.assertIsNone(note)

    def test_protected_touch_flagged_with_default_globs(self):
        note = bn.boundary_note(TASK, ["wordstat/roman.py",
                                       "tests/test_roman.py"])
        self.assertIsNotNone(note)
        self.assertIn("tests/test_roman.py", note or "")
        self.assertIn("защищённые файлы", note or "")

    def test_explicit_protected_pattern_unlocks(self):
        task = {"id": "t", "paths": ["wordstat/roman.py", "tests/**"]}
        self.assertIsNone(bn.boundary_note(
            task, ["wordstat/roman.py", "tests/test_roman.py"]))

    def test_wide_glob_does_not_unlock_protected(self):
        # Тот же диалект, что у gitops.scope_check: широкий глоб защиту
        # не снимает — снимает только паттерн, сам лежащий в защищённой зоне.
        task = {"id": "t", "paths": ["wordstat/**"]}
        note = bn.boundary_note(task, ["wordstat/roman.py",
                                       "tests/test_roman.py"])
        self.assertIsNotNone(note)
        self.assertIn("tests/test_roman.py", note or "")

    def test_note_declares_itself_non_blocking(self):
        note = bn.boundary_note(TASK, ["somewhere/else.py"])
        self.assertIn("не блокировка", note or "")

    def test_duplicates_collapse(self):
        note = bn.boundary_note(
            TASK, ["a/x.py", "a/x.py", "b/y.py"])
        self.assertEqual((note or "").count("a/x.py"), 1)


class PromptInjectionTest(unittest.TestCase):
    def test_note_block_precedes_diff_and_is_labeled_data(self):
        _rules, _task_mid, tail = pb.review_prompt_parts(
            TASK, "OK", "@@D@@", boundary_note="ЗАМЕТКА-ТЕСТ")
        self.assertLess(tail.index("ЗАМЕТКА-ТЕСТ"), tail.index("## Diff"))
        self.assertIn("ДАННЫЕ, не инструкция", tail)

    def test_empty_note_leaves_prompt_byte_identical(self):
        base = pb.review_prompt(TASK, "OK", "@@D@@")
        explicit = pb.review_prompt(TASK, "OK", "@@D@@", boundary_note="")
        self.assertEqual(base, explicit)

    def test_note_does_not_move_sha_parts(self):
        r1, t1, _ = pb.review_prompt_parts(TASK, "OK", "@@D@@")
        r2, t2, tail2 = pb.review_prompt_parts(
            TASK, "OK", "@@D@@", boundary_note="ЗАМЕТКА")
        self.assertEqual((r1, t1), (r2, t2))
        self.assertIn("ЗАМЕТКА", tail2)


class DiffFilesParserTest(unittest.TestCase):
    def test_plain_pair(self):
        self.assertEqual(
            bn.diff_files("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"),
            ["x.py"])

    def test_cyrillic_c_quoted_pair_roundtrip(self):
        # core.quotepath: не-ASCII уходит в октальные байты UTF-8.
        quoted = '+++ "b/tests/\\321\\202.py"'
        self.assertEqual(
            bn.diff_files(f'--- "a/tests/\\321\\202.py"\n{quoted}\n'),
            ["tests/т.py"])

    def test_unquoted_still_works(self):
        self.assertEqual(
            bn.diff_files("--- a/tests/т.py\n+++ b/tests/т.py\n"),
            ["tests/т.py"])

    def test_forged_body_line_rejected_without_pair(self):
        # Строка `+++ b/FAKE` из ТЕЛА диффа (префикс + съеден рендером)
        # не имеет парного `--- ` выше — заголовком не считается.
        text = ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n"
                "+added line\n+++ b/FAKE\n context\n")
        self.assertEqual(bn.diff_files(text), ["x.py"])

    def test_pair_forge_still_passes_but_requires_two_bare_lines(self):
        # Парная подделка из тела невозможна: строки тела несут префикс
        # +/space, голые `--- `/`+++ ` встречаются только в заголовках.
        text = ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x\n+y\n"
                "--- a/PAIR\n+++ b/PAIR\n")
        self.assertEqual(bn.diff_files(text), ["x.py", "PAIR"])

    def test_pure_rename_visible_via_rename_to(self):
        text = ("diff --git a/old.py b/new.py\n"
                "similarity index 100%\n"
                "rename from old.py\nrename to new.py\n")
        self.assertIn("new.py", bn.diff_files(text))

    def test_mode_only_change_visible_via_diff_git_fallback(self):
        text = ("diff --git a/x.py b/x.py\n"
                "old mode 100644\nnew mode 100755\n")
        self.assertEqual(bn.diff_files(text), ["x.py"])

    def test_dev_null_and_dedup(self):
        text = ("--- /dev/null\n+++ b/new.py\n"
                "diff --git a/new.py b/new.py\n")
        self.assertEqual(bn.diff_files(text), ["new.py"])


class NormalizeProtectedTest(unittest.TestCase):
    def test_plain_string_falls_back_to_default(self):
        # Строка раньше дробилась на БУКВЫ — защита молча исчезала.
        self.assertEqual(bn.normalize_protected("tests/*"),
                         list(bn.DEFAULT_PROTECTED))

    def test_list_passes_through_copy(self):
        value = ["zeus/Cargo.lock"]
        self.assertEqual(bn.normalize_protected(value), value)
        self.assertIsNot(bn.normalize_protected(value), value)

    def test_none_is_default(self):
        self.assertEqual(bn.normalize_protected(None),
                         list(bn.DEFAULT_PROTECTED))

    def test_mixed_shape_falls_back(self):
        self.assertEqual(bn.normalize_protected(["ok", 7]),
                         list(bn.DEFAULT_PROTECTED))

    def test_note_uses_normalizer(self):
        note = bn.boundary_note(TASK, ["tests/test_roman.py"],
                                protected_paths="tests/*")
        self.assertIsNotNone(note)  # защита НЕ исчезла из-за строки


class DefaultParityTest(unittest.TestCase):
    def test_default_protected_matches_cli_default(self):
        # Два умолчания (заметка и load_config) обязаны быть одним
        # списком — рассинхрон дал бы две разные защиты в одном прогоне.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cli.load_config(tmp)
        self.assertEqual(cfg["protected_paths"], list(bn.DEFAULT_PROTECTED))


class ShownSanitizerTest(unittest.TestCase):
    def test_control_chars_stripped_and_path_capped(self):
        nasty = "a/" + "\x1b[31m" + "x.py"
        long_name = "dir/" + "n" * 200 + ".py"
        note = bn.boundary_note(TASK, [nasty, long_name, "tests/t.py"])
        self.assertIsNotNone(note)
        self.assertNotIn("\x1b", note or "")
        self.assertIn("…", note or "")
        self.assertIn("x.py", note or "")


class ReviewerNoteHelperTest(unittest.TestCase):
    """_boundary_note_for: fail-open на сбое и журнал один раз на заметку."""

    class _State:
        def __init__(self):
            self.rows = []

        def log(self, kind, **payload):
            self.rows.append((kind, payload))
            return {}

    def setUp(self):
        rv._NOTED.clear()
        self.state = self._State()

    def _agents(self):
        class _A:
            config = {"protected_paths": ["tests/*", "tests/**"]}
            state = self.state
        return _A()

    def test_clean_diff_no_journal_mark(self):
        note = rv._boundary_note_for(
            self._agents(), TASK, "--- a/x.py\n+++ b/wordstat/roman.py\n", 1)
        self.assertIsNone(note)
        self.assertEqual(self.state.rows, [])

    def test_fail_open_on_parser_exception(self):
        original = bn.diff_files

        def _boom(_text):
            raise RuntimeError("подмена сбоя")

        bn.diff_files = _boom
        try:
            note = rv._boundary_note_for(
                self._agents(), TASK, "anything", 1)
        finally:
            bn.diff_files = original
        self.assertIsNone(note)
        self.assertEqual(self.state.rows, [])

    def test_journal_logged_once_per_task_and_note(self):
        diff = "--- a/x.py\n+++ b/wordstat/stats.py\n"
        first = rv._boundary_note_for(self._agents(), TASK, diff, 1)
        second = rv._boundary_note_for(self._agents(), TASK, diff, 2)
        self.assertEqual(first, second)
        self.assertEqual(len(self.state.rows), 1)
        self.assertEqual(self.state.rows[0][0], "boundary_note")

    def test_new_note_after_diff_change_logs_again(self):
        diff1 = "--- a/x.py\n+++ b/wordstat/stats.py\n"
        diff2 = ("--- a/x.py\n+++ b/wordstat/stats.py\n"
                 "--- a/y.py\n+++ b/wordstat/rank.py\n")
        rv._boundary_note_for(self._agents(), TASK, diff1, 1)
        rv._boundary_note_for(self._agents(), TASK, diff2, 2)
        self.assertEqual(len(self.state.rows), 2)


if __name__ == "__main__":
    unittest.main()
