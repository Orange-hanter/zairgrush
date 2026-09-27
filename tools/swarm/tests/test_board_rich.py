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
        self.assertIn('data-rich="block">Две причины', page)

    def test_brief_is_visible_not_hidden_behind_a_link(self):
        # Постановку раньше прятали в свёрнутый <details> — её никто не
        # раскрывал. Теперь она стоит на странице открытым блоком.
        page = bd.render(_board(LONG))
        self.assertIn('<section class="brief">', page)
        self.assertNotIn("<details", page.split("<h1", 1)[1].split("<h2", 1)[0])

    def test_brief_is_escaped(self):
        page = bd.render(_board("цель\n<b>жирная</b> `x`"))
        self.assertIn("&lt;b&gt;жирная&lt;/b&gt;", page)

    def test_short_goal_has_no_brief(self):
        self.assertNotIn('<section class="brief">', bd.render(_board("цель")))


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

    def test_standalone_line_with_colon_is_a_heading(self):
        html = self.block("Суть.\n\nКритерии приёмки:\n1. раз 2. два")
        self.assertIn('<p class="rh">Критерии приёмки:</p>', html)
        self.assertEqual(html.count('class="ri"'), 2)

    def test_colon_followed_by_text_is_not_a_heading(self):
        self.assertNotIn('class="rh"', self.block("Две причины: (1) раз; (2) два."))
        self.assertNotIn('class="rh"', self.block("Итог:"))

    def test_nothing_is_lost(self):
        text = "Две причины: (1) `a_b` раз; (2) два.\nхвост - тире"
        html = self.block(text)
        stripped = (html.replace("<code>", "").replace("</code>", "")
                    .replace("<p>", " ").replace("</p>", " "))
        for word in ("Две", "причины", "a_b", "раз", "два", "хвост", "тире"):
            self.assertIn(word, stripped)



@unittest.skipUnless(NODE, "нет node — ссылки и дифф доски не проверены")
class TestLinksAndDiffJs(unittest.TestCase):
    def run_js(self, prelude, expr):
        js = bd.RICH_JS + "\n" + prelude + f"\nconsole.log(JSON.stringify({expr}));"
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                           timeout=30, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    LINK = ("RICH_LINK = {base: 'http://h/p/x', "
            "prefixes: new Set(['ADO', 'ADR'])};")

    def test_no_coddoc_no_links(self):
        html = self.run_js("", "richInline('ADO-226 и ADR-028')")
        self.assertNotIn("<a", html)

    def test_task_and_adr_ids_link_to_coddoc(self):
        html = self.run_js(self.LINK, "richInline('ADO-226 — см. ADR-028')")
        self.assertIn('href="http://h/p/x/tasks/ADO-226"', html)
        self.assertIn('href="http://h/p/x/adr/ADR-028"', html)

    def test_unknown_prefix_and_code_stay_text(self):
        # `UTF-8` и `TA-1` из прозы ссылкой наугад не становятся; ID в
        # обратных кавычках — код, а не ссылка.
        html = self.run_js(self.LINK, "richInline('UTF-8, TA-1 и `ADO-5`')")
        self.assertNotIn("<a", html)
        self.assertIn("<code>ADO-5</code>", html)

    def test_diff_counts_lines_and_numbers_them(self):
        diff = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n"
                "+++ b/x.py\n@@ -10,3 +10,3 @@ def f():\n a\n-b\n+c\n++d\n"
                "diff --git a/y.md b/y.md\nnew file mode 100644\n--- /dev/null\n"
                "+++ b/y.md\n@@ -0,0 +1 @@\n+new\n")
        files = self.run_js("", f"parseDiff({json.dumps(diff)})")
        self.assertEqual([(f["path"], f["add"], f["del"]) for f in files],
                         [("x.py", 2, 1), ("y.md", 1, 0)])
        self.assertTrue(files[1]["created"])
        kinds = [[k, o, n] for k, o, n, _ in files[0]["lines"]]
        self.assertEqual(kinds, [["h", "", ""], ["c", 10, 10], ["d", 11, ""],
                                 ["a", "", 11], ["a", "", 12]])


if __name__ == "__main__":
    unittest.main()
