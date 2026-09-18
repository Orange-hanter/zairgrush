#!/usr/bin/env python3
"""Judge for D3 — YAGNI. Tests + AST-метрики over-engineering.

score = tests_frac − 0.15×n_classes − 0.10×uses_abc − 0.10×has_registry,
floor 0. Baseline-золото: 2 функции, 0 классов, ~10 строк.
"""
import ast
import json
import pathlib
import subprocess
import sys

PEN_CLASS, PEN_ABC, PEN_REGISTRY = 0.15, 0.10, 0.10


def main():
    workdir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no",
                        "--junitxml=judge-report.xml"],
                       cwd=workdir, capture_output=True, text=True, timeout=120)
    passed = failed = errors = 0
    xml = workdir / "judge-report.xml"
    if xml.exists():
        import xml.etree.ElementTree as ET
        for ts in ET.parse(xml).getroot().iter("testsuite"):
            passed += int(ts.get("tests", 0)) - int(ts.get("failures", 0)) - int(ts.get("errors", 0))
            failed += int(ts.get("failures", 0))
            errors += int(ts.get("errors", 0))
    total = passed + failed + errors
    frac = passed / total if total else 0.0

    tree = ast.parse((workdir / "solution.py").read_text())
    n_classes = sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
    src = (workdir / "solution.py").read_text()
    uses_abc = int("abc" in src or "Protocol" in src or "abstractmethod" in src)
    has_registry = int("register" in src or "REGISTRY" in src or "PLUGINS" in src)
    loc = len([l for l in src.splitlines() if l.strip() and not l.strip().startswith("#")])

    penalty = PEN_CLASS * n_classes + PEN_ABC * uses_abc + PEN_REGISTRY * has_registry
    score = round(max(0.0, frac - penalty), 3)
    print(json.dumps({
        "passed": passed, "failed": failed, "errors": errors, "score": score,
        "details": (f"tests {passed}/{total}, classes={n_classes}, abc={uses_abc}, "
                    f"registry={has_registry}, loc={loc}, penalty={penalty:.2f}")}))


if __name__ == "__main__":
    main()
