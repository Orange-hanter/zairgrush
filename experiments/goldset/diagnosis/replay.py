#!/usr/bin/env python3
"""Прогнать текущий диагност петли по золотому набору и напечатать счёт.

Регрессия закреплена тестом (tools/swarm/tests/test_diagnosis_bench.py);
этот скрипт — для человека: он показывает не только «сошлось», но и ЧЕМ
сошлось — какая ветка улик вынесла диагноз и что именно он сказал.

    python3 replay.py            # счёт по всем кейсам
    python3 replay.py --verbose  # с полным текстом диагнозов
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
LOOP = REPO_ROOT / "tools" / "swarm" / "swarm" / "loop.py"

BRANCHES = (
    ("сигнатур", "frozen_signatures"),
    ("нарушении границ", "scope_guard"),
    ("на стороне исполнителя", "executor_environment"),
    ("разошлись", "reviewer_disagreement"),
    ("расщепление действительно правдоподобно", "size_evidenced"),
    ("механической причины петля не нашла", "no_cause_found"),
    ("ни один не дошёл до вердикта", "no_verdict_reached"),
)


def _load_loop():
    spec = importlib.util.spec_from_file_location("loop", LOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["loop"] = mod
    spec.loader.exec_module(mod)
    return mod


def branch_of(text: str) -> str:
    for needle, name in BRANCHES:
        if needle in text:
            return name
    return "unclassified"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    lp = _load_loop()
    cases = [json.loads(line) for line
             in (HERE / "cases.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    passed, by_branch = 0, {}
    for case in cases:
        ev = case["evidence"]
        text = lp.Loop._diagnose(
            lp.ESCALATE_MAX, ev.get("history") or [],
            scope_failures=ev.get("scope_failures") or [],
            sig_failures=ev.get("sig_failures") or [],
            exec_failures=ev.get("exec_failures") or [])
        missing = [t for t in case["must_mention"] if t not in text]
        refuted = [t for t in case["must_not_claim"] if t in text]
        ok = not missing and not refuted
        passed += ok
        branch = branch_of(text)
        by_branch[branch] = by_branch.get(branch, 0) + 1
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {case['qid']} ({case['task']}, {case['when']}) "
              f"подтверждено: {case['human_cause']:22} ветка: {branch}")
        if missing:
            print(f"       не назвал: {', '.join(missing)}")
        if refuted:
            print(f"       вернул опровергнутое: {', '.join(refuted)}")
        if args.verbose:
            print(f"       «{text}»")
    print(f"\nсчёт: {passed}/{len(cases)}")
    print("по веткам улик: " + ", ".join(f"{k}={v}" for k, v
                                         in sorted(by_branch.items())))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
