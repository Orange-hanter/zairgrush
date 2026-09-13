#!/usr/bin/env python3
"""P1-триаж ревью: измеренная бэнда дешёвого контура (ADR-027, REV-002).

Тесты держат четыре свойства, без которых полоса вредна: бэнда читает те
же числа, что метрика ревью (diff_files/diff_lines по сырому диффу);
guard (защищённый файл, новая зависимость) безусловно отдаёт полное
ревью; канареечный/adversarial стенд (`triage = false`) полосы не имеет
вообще; и каждый маршрут виден в журнале (band_hit / guard_block) —
молчащий триаж был бы невидимой экономией и невидимым риском.

Отдельный класс — реплей опубликованной таблицы REV-002 (18 диффов
пилота): маршрутизация кода обязана побайтово совпасть с замером, на
котором бэнда принята (в полосе ровно p1fn-close; все три земли endorsed
majors — вне её).
"""
import json
import unittest

from tests import test_review_failure as _trf
from tests.test_review_failure import (
    VALID,
    RepoCase,
    _diff_for,
    ag,
    patch_claude_popen,
    rv,
)

tr = _trf._load("triage")

TASK = {"id": "t1", "title": "t", "spec": "s",
        "acceptance": ["гейт зелёный"], "paths": ["mod.py"],
        "type": "feature"}


def _journal(state):
    return [json.loads(line)
            for line in state.journal_path.read_text().splitlines()]


def _review_rows(state):
    rows = [json.loads(x)
            for x in state.metrics_path.read_text().splitlines() if x.strip()]
    return [r for r in rows if r.get("phase") == "review"]


class TestBandClassification(unittest.TestCase):
    """Геометрия бэнды: single_file ≤100 строк ИЛИ docs_only (REV-002 §2)."""

    def test_single_small_file_is_in_band(self):
        d = tr.decide(_diff_for("src/a.py", ["x"] * 40), 1, 40, {})
        self.assertEqual(d["route"], "cheap")

    def test_boundary_is_100_lines_inclusive(self):
        self.assertEqual(
            tr.decide(_diff_for("src/a.py", ["x"] * 100), 1, 100, {})["route"],
            "cheap")
        self.assertEqual(
            tr.decide(_diff_for("src/a.py", ["x"] * 101), 1, 101, {})["route"],
            "full")

    def test_multi_file_is_never_in_band(self):
        """Двухфайловый дифф вне полосы при ЛЮБОМ размере: single_file
        отсекает все три земли endorsed majors, не полагаясь на размер."""
        diff = _diff_for("src/a.py", ["x"]) + _diff_for("src/b.py", ["y"])
        self.assertEqual(tr.decide(diff, 2, 2, {})["route"], "full")

    def test_docs_only_skips_even_multi_file(self):
        diff = _diff_for("docs/a.md", ["x"]) + _diff_for("README.md", ["y"])
        d = tr.decide(diff, 2, 2, {})
        self.assertEqual(d["route"], "skip")
        self.assertTrue(d["docs_only"])

    def test_docs_mixed_with_code_is_not_docs_only(self):
        diff = _diff_for("docs/a.md", ["x"]) + _diff_for("src/a.py", ["y"])
        self.assertEqual(tr.decide(diff, 2, 2, {})["route"], "full")

    def test_test_only_stays_full_review(self):
        """Явное исключение ADR-027, а не следствие guard'а: предикат
        при protected_paths пилота недостижим, и данные говорят держать
        test-only вне бэнды (REV-002 §2)."""
        d = tr.decide(_diff_for("tests/test_a.py", ["x"] * 10), 1, 10,
                      {"protected_paths": []})
        self.assertEqual(d["route"], "full")
        self.assertEqual(d["excluded"], "test_only")

    def test_empty_paths_fail_open(self):
        """Размер говорит «один файл», а заголовков нет — сомнительный
        случай уходит на полное ревью."""
        d = tr.decide("мусор без заголовков\n", 1, 5, {})
        self.assertEqual(d["route"], "full")

    def test_txt_data_file_is_not_docs(self):
        """g1nt-урок: golden-эталон — .txt с ДАННЫМИ, а не документация.
        docs_only, съедающий .txt, пропустил бы эталон на 16К строк."""
        d = tr.decide(_diff_for("fixtures/golden.txt", ["x"] * 5), 1, 5, {})
        self.assertEqual(d["route"], "cheap", ".txt — не docs_only")
        big = tr.decide(_diff_for("fixtures/golden.txt", ["x"] * 200),
                        1, 200, {})
        self.assertEqual(big["route"], "full")


class TestGuard(unittest.TestCase):
    """Guard держится целиком, иначе полосы нет: каждый пункт — отказ."""

    def test_protected_file_blocks_the_band(self):
        d = tr.decide(_diff_for("secrets/config.py", ["x"] * 5), 1, 5,
                      {"protected_paths": ["secrets/*"]})
        self.assertEqual(d["route"], "full")
        self.assertEqual(d["guard_block"], "protected")

    def test_new_dependency_blocks_the_band(self):
        for manifest in ("Cargo.toml", "pyproject.toml", "package.json",
                         "requirements-dev.txt", "go.mod"):
            d = tr.decide(_diff_for(manifest, ["x"] * 5), 1, 5, {})
            self.assertEqual(d["route"], "full", manifest)
            self.assertEqual(d["guard_block"], "new_dependency", manifest)

    def test_docs_only_under_protected_is_blocked_too(self):
        """Пропуск — самый сильный маршрут, поэтому guard проверяется
        и для docs_only: tests/README.md при умолчаниях защищён."""
        d = tr.decide(_diff_for("tests/README.md", ["x"]), 1, 1, {})
        self.assertEqual(d["route"], "full")
        self.assertEqual(d["guard_block"], "protected")


class TestStandRestriction(unittest.TestCase):
    """Канареечный/adversarial стенд: `triage = false` — полосы нет."""

    def test_disabled_triage_is_plain_full_review(self):
        d = tr.decide(_diff_for("README.md", ["x"]), 1, 1,
                      {"triage": False, "triage_arm": ["m", None]})
        self.assertEqual(d, {"route": "full"},
                         "на стенде-измерителе не должно быть ни маршрута, "
                         "ни фактов бэнды — только полное ревью")


class TestCheapArm(unittest.TestCase):
    def test_forms_match_draw_arm(self):
        self.assertEqual(tr.cheap_arm({"triage_arm": "m"}), ("m", None))
        self.assertEqual(tr.cheap_arm({"triage_arm": ["m", "low"]}),
                         ("m", "low"))
        self.assertEqual(tr.cheap_arm({"triage_arm": {"model": "m"}}),
                         ("m", None))
        self.assertIsNone(tr.cheap_arm({}))
        self.assertIsNone(tr.cheap_arm({"triage_arm": 17}))


class TestPilotTableReplay(unittest.TestCase):
    """Маршрутизация обязана совпасть с замером, на котором бэнда принята.

    Строки — таблица REV-002 §1 (experiments/reviewarm/report-rev002.md):
    18 восстановленных диффов пилота, флаги docs/test/prot/dep и
    protected_paths пилота. Сырые диффы на main отсутствуют (лежат на
    ветке контура), поэтому диффы синтетические — воспроизводятся файлы,
    размер и флаги строки, не её содержимое. Проверяется АРИФМЕТИКА
    полосы против опубликованной маршрутизации: в бэнде ровно p1fn-close.
    """

    # (key, файлы, Σ строк, test, prot, dep) — docs в пилоте ноль.
    PILOT = [
        ("g2pf-close", 1, 35, True, True, False),
        ("p1fn-close", 1, 64, False, False, False),
        ("e2op-close", 3, 65, False, False, False),
        ("e9tf-i1", 4, 74, False, False, False),
        ("e9tf-close", 4, 74, False, False, False),
        ("e5dq-close", 3, 232, False, False, False),
        ("e6cx-close", 3, 336, False, True, True),
        ("k3ad-close", 7, 363, False, True, False),
        ("e7in-close", 1, 370, True, True, False),
        ("e7in-i1", 2, 442, True, True, False),
        ("m6pe-close", 5, 491, False, True, False),
        ("e7in-i3", 2, 503, True, True, False),
        ("s2ky-i2", 13, 566, False, True, False),
        ("s2ky-i1", 14, 600, False, True, False),
        ("e4kb-close", 10, 613, False, True, True),
        ("z8ck-close", 4, 736, False, True, True),
        ("g1nt-close", 2, 16161, True, True, False),
        ("c4rp-close", 4, 54936, False, True, False),
    ]
    # protected_paths пилота (rev002 §1): zeus/tests/**, **/tests/**,
    # *.toml@root, zeus/Cargo.lock — fnmatch `*` пересекает `/`, поэтому
    # формы ниже покрывают те же пути.
    PROTECTED = ["zeus/tests/*", "**/tests/*", "*.toml", "zeus/Cargo.lock"]

    @staticmethod
    def _synth(n_files, total, test, prot, dep):
        if test:
            paths = [f"zeus/tests/test_case{i}.rs" for i in range(n_files)]
        else:
            paths = [f"zeus/src/mod{i}.rs" for i in range(n_files)]
        if prot and not test:
            paths[-1] = "zeus/tests/fixture.rs"
        if dep:
            paths[0] = "Cargo.toml"
        bulk = total - (len(paths) - 1)
        return "".join(_diff_for(p, ["x"] * (bulk if i == 0 else 1))
                       for i, p in enumerate(paths))

    def test_exactly_p1fn_is_banded(self):
        config = {"protected_paths": self.PROTECTED}
        routed = {}
        for key, n, total, test, prot, dep in self.PILOT:
            diff = self._synth(n, total, test, prot, dep)
            routed[key] = tr.decide(diff, n, total, config)["route"]
        self.assertEqual([k for k, v in routed.items() if v != "full"],
                         ["p1fn-close"],
                         f"маршрутизация разошлась с замером: {routed}")

    def test_endorsed_major_grounds_stay_out_of_the_band(self):
        """Safety-гейт спецификации: ни одна земля endorsed-находки не
        попадает в полосу ни при каком N до 369 (REV-002 §3)."""
        config = {"protected_paths": self.PROTECTED}
        for key, n, total, test, prot, dep in self.PILOT:
            if key not in ("e9tf-i1", "e7in-i1", "s2ky-i1"):
                continue
            diff = self._synth(n, total, test, prot, dep)
            self.assertEqual(tr.decide(diff, n, total, config)["route"],
                             "full", f"земля {key} попала в полосу")

    def test_test_only_rows_are_excluded_not_guarded(self):
        config = {"protected_paths": self.PROTECTED}
        diff = self._synth(1, 35, True, True, False)  # g2pf-close
        self.assertEqual(tr.decide(diff, 1, 35, config).get("excluded"),
                         "test_only")


class TestSkipRoute(RepoCase):
    """docs_only + guard: вызова нет, вердикт помечен, экономия видна."""

    SKIP_TASK = dict(TASK, paths=["README.md"])

    def test_docs_only_never_calls_the_reviewer(self):
        (self.root / "README.md").write_text("# документация\n")
        calls = []

        def reply(argv):
            calls.append(argv)
            return {"structured_output": VALID, "total_cost_usd": 0.5}

        patch_claude_popen(self, reply)
        agents = ag.Agents(self.state, {})
        verdict = agents.review(dict(self.SKIP_TASK), "OK", 1)
        self.assertEqual(calls, [], "docs_only обязан обходиться без вызова")
        self.assertEqual(verdict["verdict"], "approve")
        self.assertEqual(verdict["triaged"], "skip",
                         "пропуск обязан быть отличим от вердикта руки")

    def test_skip_is_journalled_and_metered(self):
        (self.root / "README.md").write_text("# документация\n")
        patch_claude_popen(self, lambda argv: {"structured_output": VALID})
        agents = ag.Agents(self.state, {})
        agents.review(dict(self.SKIP_TASK), "OK", 1)
        hits = [r for r in _journal(self.state) if r["kind"] == "band_hit"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["action"], "skip")
        rows = _review_rows(self.state)
        self.assertEqual(rows[-1]["triage"], "skip")
        self.assertEqual(rows[-1]["cost_usd"], 0.0,
                         "пропущенное ревью обязано стоить ноль и явно")


class TestCheapRoute(RepoCase):
    """Код в бэнде: полный контракт вердикта, но дешёвой рукой."""

    def _small_code_diff(self):
        with (self.root / "mod.py").open("a") as f:
            f.write("    return 2\n")

    def test_band_routes_to_the_triage_arm(self):
        self._small_code_diff()
        calls = []

        def reply(argv):
            calls.append(argv)
            return {"structured_output": VALID, "total_cost_usd": 0.04}

        patch_claude_popen(self, reply)
        agents = ag.Agents(self.state,
                           {"triage_arm": ["claude-haiku-4-5", "low"]})
        verdict = agents.review(dict(TASK), "OK", 1)
        self.assertEqual(verdict["verdict"], "approve")
        argv = calls[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-4-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        hits = [r for r in _journal(self.state) if r["kind"] == "band_hit"]
        self.assertEqual(hits[0]["action"], "cheap")
        self.assertEqual(hits[0]["model"], "claude-haiku-4-5")
        row = _review_rows(self.state)[-1]
        self.assertEqual((row["triage"], row["model"]), ("cheap", "claude-haiku-4-5"))

    def test_band_without_arm_is_full_review_but_counted(self):
        """Skip для кода запрещён, а одобрять триаж не вправе: без руки —
        полное ревью, но факт бэнды в журнале (несостоявшаяся экономия
        неотличима от отсутствия бэнды без этой записи)."""
        self._small_code_diff()
        calls = []

        def reply(argv):
            calls.append(argv)
            return {"structured_output": VALID, "total_cost_usd": 0.5}

        patch_claude_popen(self, reply)
        agents = ag.Agents(self.state, {})
        agents.review(dict(TASK), "OK", 1)
        self.assertEqual(len(calls), 1, "без triage_arm код идёт на полное ревью")
        self.assertNotIn("--model", calls[0])
        hits = [r for r in _journal(self.state) if r["kind"] == "band_hit"]
        self.assertEqual(hits[0]["action"], "full")
        self.assertIn("triage_arm", hits[0]["reason"])


class TestGuardRoutes(RepoCase):
    """Guard-блок: дифф бэнды уходит на полное ревью, причина в журнале."""

    def _run(self, config):
        calls = []

        def reply(argv):
            calls.append(argv)
            return {"structured_output": VALID, "total_cost_usd": 0.5}

        patch_claude_popen(self, reply)
        agents = ag.Agents(self.state, config)
        agents.review(dict(TASK), "OK", 1)
        return calls

    def test_protected_file_gets_full_review(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "config.py").write_text("KEY = 1\n")
        calls = self._run({"protected_paths": ["secrets/*"],
                           "triage_arm": ["cheap-m", None]})
        self.assertEqual(len(calls), 1, "guard_block обязан звать полное ревью")
        self.assertNotIn("--model", calls[0])
        blocks = [r for r in _journal(self.state) if r["kind"] == "guard_block"]
        self.assertEqual(blocks[0]["reason"], "protected")

    def test_new_dependency_gets_full_review(self):
        (self.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        calls = self._run({"triage_arm": ["cheap-m", None]})
        self.assertEqual(len(calls), 1)
        blocks = [r for r in _journal(self.state) if r["kind"] == "guard_block"]
        self.assertEqual(blocks[0]["reason"], "new_dependency")

    def test_test_only_gets_full_review_without_band_events(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_mod.py").write_text("def test_x(): pass\n")
        calls = self._run({"triage_arm": ["cheap-m", None]})
        self.assertEqual(len(calls), 1)
        kinds = {r["kind"] for r in _journal(self.state)}
        self.assertNotIn("band_hit", kinds)
        self.assertNotIn("guard_block", kinds,
                         "test-only — исключение бэнды, а не блок стража")

    def test_adversarial_stand_has_no_band_at_all(self):
        """`triage = false` — канареечный/adversarial стенд: даже docs_only
        идёт полным ревью и полоса не оставляет следов."""
        (self.root / "README.md").write_text("# документация\n")
        calls = self._run({"triage": False, "triage_arm": ["cheap-m", None]})
        self.assertEqual(len(calls), 1)
        kinds = {r["kind"] for r in _journal(self.state)}
        self.assertNotIn("band_hit", kinds)
        self.assertNotIn("guard_block", kinds)

    def test_classifier_failure_fails_open(self):
        """Сбой классификатора стоит ноль: полное ревью, как до полосы."""
        with (self.root / "mod.py").open("a") as f:
            f.write("    return 2\n")
        orig = rv.triage_mod.decide
        rv.triage_mod.decide = lambda *a, **k: 1 / 0
        self.addCleanup(lambda: setattr(rv.triage_mod, "decide", orig))
        calls = self._run({"triage_arm": ["cheap-m", None]})
        self.assertEqual(len(calls), 1, "исключение триажа отменило ревью")
        self.assertNotIn("--model", calls[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
