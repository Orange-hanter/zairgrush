"""Доска: заголовок из цели и разбор постановок (код, пути, пункты).

Прогон cod-doc 2026-09-23: `--goal` в 2036 знаков целиком стоял в `<h1>`
полужирной стеной, а постановки задач выводились сырым текстом —
`обратные кавычки` буквально, пункты «(1) … (2) …» слеплены в абзац,
пути и идентификаторы неотличимы от прозы.
"""

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bd = _load("board")

LONG = ("AFT-002 — curator_next ≤ 8 КБ: advisory отдельно. "
        + "Две причины: (1) раз; (2) два. " * 8)


def _board(goal):
    return {"goal": goal, "tasks": [], "questions": [], "events": [],
            "run_level": [], "unfinished": [], "spend": {}, "total": 0,
            "root": "/r", "swarm_dir": "/r/.swarm", "built": "сейчас"}


class TestGoalParts(unittest.TestCase):
    def test_long_line_is_cut_at_first_sentence(self):
        title, rest = bd._goal_parts(LONG)
        self.assertEqual(title, "AFT-002 — curator_next ≤ 8 КБ: advisory отдельно")
        self.assertTrue(rest.startswith("Две причины: (1)"))

    def test_first_line_is_the_title(self):
        title, rest = bd._goal_parts("Короткая цель\n\nподробности\nещё")
        self.assertEqual((title, rest), ("Короткая цель", "подробности\nещё"))

    def test_short_goal_has_no_brief(self):
        self.assertEqual(bd._goal_parts("  цель  "), ("цель", ""))

    def test_early_abbreviation_does_not_cut(self):
        goal = "т. е. " + "слово " * 40
        title, _ = bd._goal_parts(goal)
        self.assertGreater(len(title), 20)

    def test_no_sentence_end_keeps_the_line(self):
        goal = "слово " * 40
        self.assertEqual(bd._goal_parts(goal), (goal.strip(), ""))


class TestHeader(unittest.TestCase):
    def test_h1_carries_only_the_title(self):
        page = bd.render(_board(LONG))
        h1 = page.split("<h1", 1)[1].split("</h1>", 1)[0]
        self.assertIn("advisory отдельно", h1)
        self.assertNotIn("Две причины", h1)
        self.assertIn('<details class="goal">', page)
        self.assertIn('data-rich="block">Две причины', page)

    def test_brief_is_escaped(self):
        page = bd.render(_board("цель\n<b>жирная</b> `x`"))
        self.assertIn("&lt;b&gt;жирная&lt;/b&gt;", page)

    def test_short_goal_has_no_details(self):
        self.assertNotIn('<details class="goal">', bd.render(_board("цель")))


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "нет node — разборщик постановок доски не проверен")
class TestRichJs(unittest.TestCase):
    def run_js(self, fn, cases):
        js = (bd.RICH_JS + "\nconst C = " + json.dumps(cases, ensure_ascii=False)
              + f";\nconsole.log(JSON.stringify(C.map({fn})));")
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                           timeout=30, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def inline(self, text):
        return self.run_js("richInline", [text])[0]

    def block(self, text):
        return self.run_js("rich", [text])[0]

    def test_backticks_become_code_and_stay_escaped(self):
        self.assertEqual(self.inline("a <b> `x<y>`"),
                         "a &lt;b&gt; <code>x&lt;y&gt;</code>")

    def test_bare_technical_names_become_code(self):
        cases = {
            "cod_doc/cli/cmd_ctx.py": "путь",
            "tests/x.py:118-126": "путь со строками",
            "tests/cli/test_ctx.py::test_ok": "путь с узлом pytest",
            "card.drift.issues": "точечное имя",
            "include_skill_bodies": "snake_case",
            "_CURATOR_SKILLS": "константа",
            "--include-skill-bodies": "флаг",
            "curator_next(include_skill_bodies, skip_links)": "вызов",
        }
        out = self.run_js("richInline", [f"см. {k} тут" for k in cases])
        for (name, what), html in zip(cases.items(), out, strict=True):
            with self.subTest(what):
                self.assertIn(f"<code>{name}</code>", html)

    def test_prose_and_numbers_stay_text(self):
        for text in ("в 1. версии и 8 КБ", "PR #102 — 2026-09-23",
                     "Критерии приёмки — ниже", "e2e-проверка"):
            with self.subTest(text):
                self.assertNotIn("<code>", self.inline(text))
                self.assertNotIn('class="ri"', self.block(text))

    def test_inline_enumeration_becomes_items(self):
        html = self.block("Две причины: (1) раз; (2) два. Итог.")
        self.assertEqual(html.count('class="ri"'), 2)
        self.assertTrue(html.startswith("<p>Две причины:</p>"))
        self.assertIn('<span class="rn">(2)</span><div>два. Итог.</div>', html)

    def test_two_kinds_in_one_line(self):
        html = self.block("(1) а (2) б. Критерии: 1. в 2. г 3. д")
        self.assertEqual(html.count('class="ri"'), 5)

    def test_lone_number_is_not_a_list(self):
        self.assertNotIn('class="ri"', self.block("Сделать: 1. только это"))

    def test_item_ends_at_newline(self):
        html = self.block("(1) раз\n(2) два\nхвост абзацем")
        self.assertEqual(html.count('class="ri"'), 2)
        self.assertTrue(html.endswith("<p>хвост абзацем</p>"))

    def test_bullets_only_at_line_start(self):
        self.assertEqual(self.block("- раз\n- два").count('class="ri"'), 2)
        self.assertNotIn('class="ri"', self.block("это - не пункт"))

    def test_markup_is_never_injected(self):
        html = self.block("(1) <script>x</script> (2) `<img src=x>`")
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)

    def test_nothing_is_lost(self):
        text = "Две причины: (1) `a_b` раз; (2) два.\nхвост - тире"
        html = self.block(text)
        stripped = (html.replace("<code>", "").replace("</code>", "")
                    .replace("<p>", " ").replace("</p>", " "))
        for word in ("Две", "причины", "a_b", "раз", "два", "хвост", "тире"):
            self.assertIn(word, stripped)


if __name__ == "__main__":
    unittest.main()
