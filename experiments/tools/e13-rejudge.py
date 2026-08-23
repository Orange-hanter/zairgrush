#!/usr/bin/env python3
"""Пересудить УЖЕ СДЕЛАННЫЕ диффы другой рукой ревьюера.

Зачем. E13 сравнивал движки исполнителя и оба раза получил «неразличимо»,
потому что судья не находил НИЧЕГО: на BENCH-1 и BENCH-2 ревьюер
sonnet/medium закрыл все двенадцать задач с первой итерации и нулём
находок. Прежний прогон тех же brownfield-задач (2026-08-07) дал 10
находок и итерации 2 и 3. Значит, различающая сила бенча упирается в
СУДЬЮ, а не в набор задач, и это надо проверить, не перезапуская
исполнителей: диффы уже лежат в истории стендов.

Дизайн парный и потому дешёвый: один и тот же дифф, две руки. Промпт
строится ТЕМ ЖЕ `promptbuilder.review_prompt`, что и в петле, — иначе
замеряли бы самодельный промпт, а не ревьюера. Ответ читается тем же
разбором конверта, включая спасение вердикта из потока.

Запуск:
    python3 e13-rejudge.py <стенд> --model claude-opus-5 --effort xhigh
    python3 e13-rejudge.py <стенд> --dry-run     # что будет пересуждено

Результат — строки в `<стенд>/.swarm/rejudge.jsonl` и сводка на экран.
Ничего не коммитит и состояние петли не трогает.
"""
import argparse
import json
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SWARM = HERE.parent.parent / "tools" / "swarm"
sys.path.insert(0, str(SWARM / "swarm"))


def _load(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SWARM / "swarm" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def commits_of(tasks: dict) -> dict[str, str]:
    """Коммит на задачу — из состояния, а не из сообщения коммита.

    Сообщение сочиняет исполнитель (или хелпер), и id задачи в нём не
    обязан быть; оркестратор же записывает sha прямо в задачу при
    переводе в `done`. Читать надо то, что записано механически.
    """
    return {tid: str(t["commit"]) for tid, t in tasks.items()
            if t.get("status") == "done" and t.get("commit")}


def judge(prompt: str, schema: str, model: str, effort: str, budget: float,
          cwd: pathlib.Path, timeout: int = 900):
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json",
           "--verbose", "--include-partial-messages", "--json-schema", schema,
           "--allowedTools", "Read,Grep,Glob,Bash(git diff:*)",
           "--max-budget-usd", str(budget), "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    return r.stdout, round(time.time() - t0, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stand")
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--effort", default="xhigh")
    ap.add_argument("--budget", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stand = pathlib.Path(args.stand).resolve()
    pb = _load("promptbuilder")
    driver = _load("driver")
    parsing = _load("parsing")
    verdicts = _load("verdicts")
    schema = (SWARM / "schemas" / "verdict-v1.schema.json").read_text()

    tasks = {t["id"]: t for t in json.loads(
        (stand / ".swarm" / "tasks.json").read_text())["tasks"]}
    shas = commits_of(tasks)
    todo = [(tid, shas[tid]) for tid in tasks if tid in shas]
    todo.sort()
    if not todo:
        print("нечего пересуждать: коммитов задач не найдено", file=sys.stderr)
        return 2
    print(f"стенд {stand.name}: {len(todo)} диффов, рука "
          f"{args.model}/{args.effort}")
    if args.dry_run:
        for tid, sha in todo:
            print(f"  {tid}  {sha[:8]}  {tasks[tid]['title'][:60]}")
        return 0

    out_path = stand / ".swarm" / "rejudge.jsonl"
    total_cost, total_findings = 0.0, 0
    for tid, sha in todo:
        diff = subprocess.run(["git", "show", "--format=", sha], cwd=stand,
                              capture_output=True, text=True,
                              check=True).stdout
        prompt = pb.review_prompt(tasks[tid], "OK (гейт зелёный на этом коммите)",
                                  parsing.condense_diff(diff))
        stream, wall = judge(prompt, schema, args.model, args.effort,
                             args.budget, stand)
        env = driver.extract_result_envelope(stream)
        verdict = (env or {}).get("structured_output")
        salvaged = False
        if not isinstance(verdict, dict):
            payload = driver.last_structured_output(stream)
            fixed = parsing.repair_verdict(payload) if payload else None
            verdict = fixed or payload
            salvaged = isinstance(verdict, dict)
        valid = verdicts.validate_verdict(verdict)
        findings = (verdict or {}).get("findings") or []
        cost = (env or {}).get("total_cost_usd")
        row = {"task": tid, "sha": sha, "model": args.model,
               "effort": args.effort, "wall_s": wall, "cost_usd": cost,
               "valid": valid, "salvaged": salvaged or None,
               "verdict": (verdict or {}).get("verdict"),
               "problem": verdicts.verdict_problem(verdict) if not valid else None,
               "findings": [{k: f.get(k) for k in
                             ("file", "severity", "category", "issue")}
                            for f in findings if isinstance(f, dict)],
               "terminal_reason": (env or {}).get("terminal_reason")}
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        total_cost += cost or 0.0
        if valid:
            total_findings += len(findings)
        sev = ", ".join(sorted({str(f.get("severity")) for f in findings
                                if isinstance(f, dict)})) or "—"
        print(f"  {tid}  {row['verdict'] or '(нет вердикта)':16} "
              f"находок {len(findings):2} [{sev}]  ${cost or 0:.3f}  {wall}с"
              + ("  СПАСЁН" if salvaged else "")
              + ("" if valid else f"  ОТКАЗ: {row['problem']}"))
    print(f"\nитого: находок {total_findings}, ${round(total_cost, 3)}; "
          f"строки в {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
