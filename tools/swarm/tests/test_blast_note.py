#!/usr/bin/env python3
"""Blast radius ревьюера (E3-C, REV-008): кто вызывает изменённые символы.

Тесты держат четыре свойства, без которых секция вредна: она находит
определения, которые правит дифф; она НЕ находит то, чего в диффе нет
(вызов `fn`-строкой — не определение); на чистом диффе промпт ревьюера
байт-в-байт прежний (флаг выключен — «default off» не декорация, а
контракт плеч замера); и включённый флаг ставит секцию СРАЗУ за диффом —
где ревьюер задаёт вопрос «где это аукнется».
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

spec = importlib.util.spec_from_file_location("blastnote",
                                              ROOT_DIR / "blastnote.py")
bn = importlib.util.module_from_spec(spec)
sys.modules["blastnote"] = bn
spec.loader.exec_module(bn)

spec = importlib.util.spec_from_file_location("promptbuilder",
                                              ROOT_DIR / "promptbuilder.py")
pb = importlib.util.module_from_spec(spec)
sys.modules["promptbuilder"] = pb
spec.loader.exec_module(pb)

TASK = {"id": "t1", "title": "t", "paths": ["src/lib.rs"],
        "acceptance": ["гейт зелёный"]}
GATE = "test result: ok"


class ChangedSymbolsTest(unittest.TestCase):
    def test_rust_pub_fn_found(self):
        diff = "@@ -1,1 +1,3 @@\n+pub fn set_once(slot: &mut u32) {\n+}"
        self.assertEqual(bn.changed_symbols(diff), ["set_once"])

    def test_rust_pub_crate_async_fn_found(self):
        diff = "+pub(crate) async fn build_component(x: u8) {}"
        self.assertEqual(bn.changed_symbols(diff), ["build_component"])

    def test_python_def_found(self):
        diff = "+    def wait_exponential(self, x):\n+        pass"
        self.assertEqual(bn.changed_symbols(diff), ["wait_exponential"])

    def test_js_function_found(self):
        diff = "+export async function pick(a, b) { return a; }"
        self.assertEqual(bn.changed_symbols(diff), ["pick"])

    def test_call_line_is_not_a_definition(self):
        # Строка со ВЫЗОВом fn-стиля — не определение: regex требует
        # определение в начале добавленной строки (после + и пробелов).
        diff = "+        self.set_once(&mut slot, value);"
        self.assertEqual(bn.changed_symbols(diff), [])

    def test_removed_and_context_lines_ignored(self):
        diff = ("-fn old_name() {}\n fn context() {}\n"
                "+fn new_name() {}")
        self.assertEqual(bn.changed_symbols(diff), ["new_name"])

    def test_dedup_and_cap(self):
        lines = "\n".join(f"+fn f{i}(x: u8) {{}}" for i in range(12))
        got = bn.changed_symbols(lines)
        self.assertEqual(len(got), bn.MAX_SYMBOLS)
        self.assertEqual(got, [f"f{i}" for i in range(bn.MAX_SYMBOLS)])

    def test_empty_diff_no_symbols(self):
        self.assertEqual(bn.changed_symbols(""), [])


class FakeIndex:
    def __init__(self, mapping):
        self.mapping = mapping

    def impact(self, symbol, max_rows=12):
        return self.mapping.get(symbol, f"{symbol}: ссылок не найдено")


class BlastNoteTest(unittest.TestCase):
    def test_note_joins_impacts(self):
        idx = FakeIndex({"set_once": "set_once — ссылки (2):",
                         "pick": "pick — ссылки (3):"})
        note = bn.blast_note(idx, ["set_once", "pick"])
        self.assertIn("set_once — ссылки (2):", note or "")
        self.assertIn("pick — ссылки (3):", note or "")

    def test_no_symbols_is_silent(self):
        self.assertIsNone(bn.blast_note(FakeIndex({}), []))

    def test_unknown_symbol_still_named(self):
        # Ложный символ дешевле пропущенного: impact честно говорит,
        # что ссылок нет, и заметка не молчит о затронутом имени.
        note = bn.blast_note(FakeIndex({}), ["renamed_thing"])
        self.assertIn("renamed_thing", note or "")


class PromptGeometryTest(unittest.TestCase):
    """Свойства контракта промпта: default off = ноль байт; включённый
    флаг = секция сразу за диффом, до вывода тестов."""

    def test_default_off_byte_identical(self):
        base = pb.review_prompt(TASK, GATE, "+fn a() {}")
        self.assertEqual(base, pb.review_prompt(TASK, GATE, "+fn a() {}",
                                                blast=""))
        self.assertNotIn("Blast radius", base)

    def test_blast_block_after_diff_before_gate(self):
        text = pb.review_prompt(TASK, GATE, "+fn a() {}",
                                blast="a — ссылки (1):")
        self.assertIn("## Blast radius: кто вызывает изменённые символы",
                      text)
        diff_pos = text.index("```diff")
        blast_pos = text.index("## Blast radius")
        gate_pos = text.index("## Вывод тестов")
        self.assertLess(diff_pos, blast_pos)
        self.assertLess(blast_pos, gate_pos)


if __name__ == "__main__":
    unittest.main()
