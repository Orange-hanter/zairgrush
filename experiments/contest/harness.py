#!/usr/bin/env python3
"""Харнес состязания Kimi vs Sonnet (14-kimi-vs-sonnet-contest.md, §9.1).

Один мини-контур для обеих моделей через OpenRouter: single-shot prompt
(TASK.md) -> извлечение ```python-блока -> скрытый pytest -> метрики.

Запуск: python3 harness.py [--only a1-lru,a2-cron] [--slot kimi|sonnet]
Артефакты: results/<UTC-ts>/{raw,work}/..., results.jsonl, REPORT.md
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
TASKS = HERE / "tasks"

MODELS = {
    "kimi": os.environ.get("CONTEST_MODEL_KIMI", "moonshotai/kimi-k2.7-code"),
    "sonnet": os.environ.get("CONTEST_MODEL_SONNET", "anthropic/claude-sonnet-5"),
}
# Per-slot reasoning effort (env CONTEST_REASONING_<SLOT>). k2.7-code без него
# зацикливается в reasoning и не выдаёт content; "low" ≈ non-thinking режим,
# что соответствует дефолтному контуру sonnet.
REASONING = {
    slot: os.environ.get(f"CONTEST_REASONING_{slot.upper()}")
    for slot in MODELS
}
MAX_TOKENS = int(os.environ.get("CONTEST_MAX_TOKENS", "32000"))
TEST_TIMEOUT = int(os.environ.get("CONTEST_TEST_TIMEOUT", "180"))

PREAMBLE = (
    "Реши задачу ниже. Тесты скрыты и будут прогнаны против твоего "
    "`solution.py` — следуй спецификации буквально, включая формат ответа.\n\n"
)


def call_model(model_id, prompt, reasoning=None):
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
    }
    if reasoning:
        payload["reasoning"] = {"effort": reasoning}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        body = json.loads(r.read())
    dur = round(time.time() - t0, 1)
    choice = body["choices"][0]
    usage = body.get("usage") or {}
    return {
        "text": choice["message"].get("content") or "",
        "finish_reason": choice.get("finish_reason"),
        "dur_s": dur,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost_usd": usage.get("cost"),
    }


def extract_code(text, lang="python"):
    blocks = re.findall(rf"```{lang}\n(.*?)```", text, re.S)
    if len(blocks) == 1:
        return blocks[0]
    if not blocks:
        m = re.search(r"```(?:\w*)\n(.*?)```", text, re.S)
        return m.group(1) if m else None
    return None  # >1 блока — нарушение контракта


def find_python():
    """Интерпретатор с pytest: env CONTEST_PYTHON, иначе первый рабочий."""
    cands = [os.environ.get("CONTEST_PYTHON"),
             "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13",
             sys.executable, "python3"]
    for c in cands:
        if not c:
            continue
        r = subprocess.run([c, "-c", "import pytest"],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return c
    raise RuntimeError("no python with pytest found")


PY = None  # lazy: find_python() в main()


def run_tests(workdir):
    r = subprocess.run(
        [PY, "-m", "pytest", "-q", "--tb=line", "--junitxml=report.xml"],
        cwd=workdir, capture_output=True, text=True, timeout=TEST_TIMEOUT)
    passed = failed = errors = 0
    xml = workdir / "report.xml"
    if xml.exists():
        root = ET.parse(xml).getroot()
        for ts in root.iter("testsuite"):
            passed += int(ts.get("tests", 0)) - int(ts.get("failures", 0)) - int(ts.get("errors", 0))
            failed += int(ts.get("failures", 0))
            errors += int(ts.get("errors", 0))
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-5:])
    return {"passed": passed, "failed": failed, "errors": errors,
            "rc": r.returncode, "tail": tail}


def run_judge(workdir):
    """Кастомный judge.py задачи: печатает JSON в последней строке stdout.

    Контракт: {"passed": int, "failed": int, "errors": int,
               "score": float, "details": str}.
    """
    r = subprocess.run([PY, "judge.py", "."], cwd=workdir,
                       capture_output=True, text=True, timeout=TEST_TIMEOUT)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        out = json.loads(last)
    except ValueError:
        return {"passed": 0, "failed": 0, "errors": 1, "rc": r.returncode,
                "tail": "judge contract fail: " + (r.stdout + r.stderr)[-500:]}
    out["rc"] = r.returncode
    out["tail"] = out.get("details", "")
    return out


def main():
    global PY
    PY = find_python()
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].split(",")
    slots = list(MODELS)
    if "--slot" in sys.argv:
        slots = [sys.argv[sys.argv.index("--slot") + 1]]

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    outdir = HERE / "results" / run_id
    (outdir / "raw").mkdir(parents=True)
    (outdir / "work").mkdir()

    tasks = sorted(p for p in TASKS.iterdir()
                   if p.is_dir() and (not only or p.name in only))
    rows = []
    for task_dir in tasks:
        spec = (task_dir / "TASK.md").read_text()
        test_files = list(task_dir.glob("test_*.py"))
        for slot in slots:
            model_id = MODELS[slot]
            tag = f"{task_dir.name}-{slot}"
            print(f"=== {tag} ({model_id})", flush=True)
            row = {"task": task_dir.name, "slot": slot, "model": model_id}
            try:
                resp = call_model(model_id, PREAMBLE + spec, REASONING.get(slot))
            except Exception as e:
                row.update(error=f"api: {e}")
                rows.append(row)
                print(f"    API ERROR: {e}", flush=True)
                continue
            (outdir / "raw" / f"{tag}.md").write_text(resp["text"])
            row.update(dur_s=resp["dur_s"], finish_reason=resp["finish_reason"],
                       prompt_tokens=resp["prompt_tokens"],
                       completion_tokens=resp["completion_tokens"],
                       cost_usd=resp["cost_usd"])
            judge = task_dir / "judge.py"
            lang = "json" if judge.exists() and not test_files else "python"
            code = extract_code(resp["text"], lang)
            if code is None:
                row.update(error="contract: code block not found or not unique")
                print(f"    CONTRACT FAIL: no unique {lang} block", flush=True)
            else:
                work = outdir / "work" / tag
                work.mkdir()
                if lang == "json":
                    (work / "answer.json").write_text(code)
                else:
                    (work / "solution.py").write_text(code)
                for tf in test_files:
                    (work / tf.name).write_text(tf.read_text())
                try:
                    if judge.exists():
                        (work / "judge.py").write_text(judge.read_text())
                        res = run_judge(work)
                    else:
                        res = run_tests(work)
                    row.update(res)
                    extra = f" score={res['score']}" if "score" in res else ""
                    print(f"    {res['passed']} passed / {res['failed']} failed / "
                          f"{res['errors']} errors{extra} ({resp['dur_s']}s)", flush=True)
                except subprocess.TimeoutExpired:
                    row.update(error="test timeout")
                    print("    TEST TIMEOUT", flush=True)
            rows.append(row)
            with (outdir / "results.jsonl").open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    write_report(outdir, rows)
    print(f"\nREPORT: {outdir / 'REPORT.md'}", flush=True)


def write_report(outdir, rows):
    lines = ["# Contest report", "",
             f"Run: {outdir.name}", "",
             "| Task | Slot | Model | Passed | Failed | Err | Time s | Tokens in/out | Cost $ |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['task']} | {r['slot']} | {r['model']} "
                         f"| — | — | {r['error']} | {r.get('dur_s', '—')} "
                         f"| {r.get('prompt_tokens', '—')}/{r.get('completion_tokens', '—')} "
                         f"| {r.get('cost_usd') or '—'} |")
        else:
            score = f" score={r['score']}" if "score" in r else ""
            lines.append(f"| {r['task']} | {r['slot']} | {r['model']} "
                         f"| {r['passed']} | {r['failed']} | {r['errors']}{score} | {r['dur_s']} "
                         f"| {r.get('prompt_tokens')}/{r.get('completion_tokens')} "
                         f"| {r.get('cost_usd') or '—'} |")
    lines += ["", "## Итог по слотам", ""]
    for slot in {r["slot"] for r in rows}:
        sub = [r for r in rows if r["slot"] == slot]
        p = sum(r.get("passed", 0) for r in sub)
        f_ = sum(r.get("failed", 0) + r.get("errors", 0) for r in sub)
        solved = sum(1 for r in sub
                     if "error" not in r and r.get("failed", 1) == 0 and r.get("errors", 1) == 0)
        lines.append(f"- **{slot}**: задач решено {solved}/{len(sub)}, "
                     f"тестов {p} passed / {p + f_} total")
    (outdir / "REPORT.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
