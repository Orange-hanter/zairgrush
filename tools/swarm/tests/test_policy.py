#!/usr/bin/env python3
"""Тесты политик прогона.

Главное, что здесь проверяется, — что фильтрация не превращается в
намордник на ревьюера: он продолжает сообщать всё, подавленное остаётся
видимым, а корректность не подавляется никогда.
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
lp = cli.loop_mod


def run_cli(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


def finding(severity="minor", category="style", issue="текст замечания"):
    return {"file": "a.py", "severity": severity, "category": category,
            "confidence": 0.6, "issue": issue}


class TestFiltering(unittest.TestCase):
    POLICIES = [{"pid": "p001", "match": ["release note", "reno"]}]

    def test_matching_finding_suppressed(self):
        active, suppressed = lp.apply_policies(
            [finding(issue="No reno release note was added")], self.POLICIES)
        self.assertEqual(active, [])
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["suppressed_by"], "p001")

    def test_unrelated_finding_survives(self):
        active, suppressed = lp.apply_policies(
            [finding(issue="bool arm has no test")], self.POLICIES)
        self.assertEqual(len(active), 1)
        self.assertEqual(suppressed, [])

    def test_blocker_never_suppressed(self):
        active, suppressed = lp.apply_policies(
            [finding("blocker", issue="release note missing and it crashes")],
            self.POLICIES)
        self.assertEqual(len(active), 1,
                         "политика снимает требования к оформлению, "
                         "но не к корректности")
        self.assertEqual(suppressed, [])

    def test_match_looks_at_suggestion_too(self):
        f = finding(issue="документация не обновлена")
        f["suggestion"] = "добавить reno-заметку"
        _active, suppressed = lp.apply_policies([f], self.POLICIES)
        self.assertEqual(len(suppressed), 1)

    def test_no_policies_changes_nothing(self):
        fs = [finding(), finding("major")]
        active, suppressed = lp.apply_policies(fs, [])
        self.assertEqual(len(active), 2)
        self.assertEqual(suppressed, [])

    def test_empty_findings_safe(self):
        self.assertEqual(lp.apply_policies(None, self.POLICIES), ([], []))


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.state = st_mod.SwarmState(self.root)
        self.state.save_tasks({"goal": "цель прогона", "tasks": [
            {"id": "aaaa", "title": "t", "status": "pending", "deps": [],
             "paths": ["a.py"]}]})

    def tearDown(self):
        self.tmp.cleanup()


class TestPolicyLifecycle(PolicyCase):
    def test_add_and_list(self):
        code, _ = run_cli("--root", str(self.root), "policy", "add",
                          "release notes вне scope", "--match", "release note")
        self.assertEqual(code, 0)
        _, out = run_cli("--root", str(self.root), "policy", "list")
        self.assertIn("release notes вне scope", out)
        self.assertIn("p001", out)

    def test_add_without_match_refused(self):
        code, out = run_cli("--root", str(self.root), "policy", "add", "текст")
        self.assertEqual(code, 2, "политика без ключевых слов ничего не значит")
        self.assertIn("--match", out)

    def test_remove(self):
        run_cli("--root", str(self.root), "policy", "add", "текст",
                "--match", "слово")
        code, _ = run_cli("--root", str(self.root), "policy", "remove", "p001")
        self.assertEqual(code, 0)
        _, out = run_cli("--root", str(self.root), "policy", "list")
        self.assertIn("политик нет", out)

    def test_remove_unknown_refused(self):
        code, out = run_cli("--root", str(self.root), "policy", "remove", "p999")
        self.assertEqual(code, 2)
        self.assertIn("не найдена", out)

    def test_policy_dies_with_goal(self):
        self.state.add_policy("текст", ["слово"])
        self.assertEqual(len(self.state.policies()), 1)
        data = self.state.load_tasks()
        data["goal"] = "другая цель"
        self.state.save_tasks(data)
        self.assertEqual(self.state.policies(), [],
                         "при смене цели старые решения теряют силу")

    def test_status_shows_policies(self):
        run_cli("--root", str(self.root), "policy", "add", "решение прогона",
                "--match", "слово")
        _, out = run_cli("--root", str(self.root), "status")
        self.assertIn("политики прогона", out)
        self.assertIn("решение прогона", out)


class TestVisibility(PolicyCase):
    """Подавленное обязано оставаться видимым — иначе теряется recall."""

    def test_suppressed_count_shown(self):
        run_cli("--root", str(self.root), "policy", "add", "текст",
                "--match", "reno")
        self.state.log("policy_suppressed", task="aaaa", round=1, count=3,
                       items=[{"policy": "p001", "issue": "reno note"}])
        _, out = run_cli("--root", str(self.root), "policy", "list")
        self.assertIn("подавлено находок за прогон: 3", out)

    def test_suppressed_visible_in_report(self):
        self.state.log("policy_suppressed", task="aaaa", round=1, count=1,
                       items=[{"policy": "p001", "issue": "reno note missing"}])
        _, out = run_cli("--root", str(self.root), "report")
        # Проверяется ЗНАНИЕ, а не код события: отчёт говорит прозой, и
        # требовать в нём строку `policy_suppressed` значило бы закрепить
        # тестом ровно ту машинную запись, от которой отчёт и уходит.
        self.assertIn("подавлено политикой", out)
        self.assertIn("reno note missing", out)

    def test_suppressed_raw_kind_available_as_json(self):
        """Проза не отменяет первоисточник: сырьё достижимо целиком."""
        self.state.log("policy_suppressed", task="aaaa", round=1, count=1,
                       items=[{"policy": "p001", "issue": "reno note missing"}])
        _, out = run_cli("--root", str(self.root), "report", "--json")
        self.assertIn("policy_suppressed", out)

    def test_reviewer_prompt_unchanged(self):
        """Ревьюеру не говорят молчать: фильтрует оркестратор."""
        spec_a = importlib.util.spec_from_file_location(
            "agents", ROOT_DIR / "agents.py")
        agents = importlib.util.module_from_spec(spec_a)
        sys.modules["agents"] = agents
        spec_a.loader.exec_module(agents)
        self.state.add_policy("release notes вне scope", ["release note"])
        text = agents.Agents(self.state, {}).review_prompt(
            {"id": "aaaa", "title": "t", "spec": "s", "acceptance": ["ок"]},
            "OK", "diff")
        self.assertNotIn("release notes вне scope", text,
                         "политика не должна попадать в промпт ревьюера")
        self.assertIn("Report every finding", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
