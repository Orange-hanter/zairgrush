#!/usr/bin/env python3
"""REVIEWARM Step 0: offline replay of a cheap juror panel over pilot diffs.

Why this exists (09-cheap-review-contour.md §4, Step 0): before a free
panel is wired into the loop, it must be shown to produce material an
expensive adjudicator can use — and not to drown it. That question is
answerable with zero expensive calls, against diffs we already paid for.

What it measures, honestly:

- **candidate yield and noise** per model per diff: how many findings a
  cheap juror emits, how many survive mechanical validation (the file it
  names must actually be in the diff), how long it takes;
- **agreement with the paid reviewer** on the same task: which files the
  panel names that the expensive reviewer also named (file-level, which
  is the only join that survives a diff/commit mismatch).

What it CANNOT measure, and why (measured 2026-08-20, recorded in
findings.jsonl): recall of the gold set's endorsed findings. Every
endorsed reviewer finding of PILOT-1 was fixed BEFORE the commit that
closed its task — `git show <commit>` shows the corrected code, not the
defect the reviewer saw. The defective state exists only inside the
executor session logs (`.swarm/log/<task>-i<N>-executor.jsonl`, which do
carry full tool-call arguments). Recall therefore needs a separate
executor-log replay; claiming it from committed diffs would be a lie
dressed as a number.

Determinism: model calls go through the house wrapper
(`tools/swarm/swarm/helpers.ollama_chat`) — native /api/chat,
`think: false`, temperature 0, top_k 1, fixed seed, secret scrub,
circuit breaker, fail-open. Same input, same roster, same numbers.

Usage:
    python3 replay.py --stand ~/work/zeus-pilot --goldset ../goldset \
        --models deepseek-v4-flash:0731,glm-5.1,qwen3.5:397b --out .
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "swarm" / "swarm"))

import helpers  # noqa: E402 — путь добавлен строкой выше

# Роспись линз: юрор без линзы повторяет соседа, а панель из пяти
# одинаковых взглядов стоит столько же, сколько один. Разведение по углам
# — то же решение, что confirm_lens подтверждающего раунда (E10).
LENSES = {
    "correctness": "logic errors, wrong conditions, off-by-one, unhandled cases",
    "duplication": ("code duplicated from elsewhere in the repository, a second "
                    "source of truth for a rule or ordering, copy-paste"),
    "tests": ("tests that cannot fail, assertions that assert nothing, coverage "
              "missing for the behaviour the diff introduces"),
    "contract": ("code that diverges from the documented contract or spec, "
                 "undocumented behaviour, silent API changes"),
    "safety": "panics, unwraps, resource leaks, unchecked input, injection",
}

PROMPT = """You are one juror on a code-review panel. You see ONE diff.

Your lens: {lens_name} — {lens_desc}.
Report ONLY findings visible through your lens. Other jurors cover the rest.

Rules:
- Report at most {cap} findings. Fewer is better than padded.
- Every finding MUST name a file that appears in the diff.
- No praise, no summary, no restating what the diff does.
- If you see nothing through your lens, return an empty array.

Answer with a JSON array and NOTHING else, each element:
{{"file": "<path from the diff>", "line": <int or null>,
  "severity": "major"|"minor"|"nit", "issue": "<one sentence>"}}

## Task
{title}

## Diff
{diff}
"""

SEVERITIES = {"major", "minor", "nit"}


def sh(args: list[str]) -> str:
    """Один git-вызов. Пустая строка вместо исключения: стенд может не
    иметь коммита, и это факт замера, а не авария харвестера."""
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def diff_of(stand: pathlib.Path, commit: str) -> str:
    return sh(["git", f"--git-dir={stand / '.git'}", "show", "--format=",
               "--unified=3", commit])


def changed_files(diff: str) -> set[str]:
    return set(re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE))


def parse_candidates(raw: str, files: set[str]) -> tuple[list[dict[str, Any]], str]:
    """Разбор ответа юрора + механическая проверка (§7.3).

    Возвращает (кандидаты, причина отказа). Формат у дешёвого провайдера
    не гарантирован — он вымогается промптом и проверяется здесь: снятие
    ```-обёрток, разбор JSON, обязательный файл ИЗ ДИФФА (юрор, назвавший
    файл, которого в диффе нет, галлюцинирует адрес, и адъюдикатору такой
    кандидат стоит дороже, чем ничего).
    """
    text = helpers.strip_fences(raw or "").strip()
    if not text:
        return [], "empty"
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        return [], "no_json_array"
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return [], "bad_json"
    if not isinstance(data, list):
        return [], "not_a_list"
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "").strip().lstrip("./")
        issue = str(item.get("issue") or "").strip()
        if not issue:
            continue
        if file not in files:
            # Не отбрасываем молча: доля таких кандидатов — сама по себе
            # мера пригодности модели, поэтому помечаем и считаем.
            out.append({"file": file, "issue": issue, "off_diff": True,
                        "severity": str(item.get("severity") or "").lower(),
                        "line": item.get("line")})
            continue
        sev = str(item.get("severity") or "").lower()
        out.append({"file": file, "issue": issue, "off_diff": False,
                    "severity": sev if sev in SEVERITIES else "unknown",
                    "line": item.get("line")})
    return out, ""


def norm(issue: str) -> frozenset[str]:
    words = re.findall(r"[a-zа-я_]{4,}", issue.lower())
    return frozenset(words)


def dedup(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сначала механически (§7.1): точный (file, issue) и сильное
    пересечение слов. Модель зовут только на остатке — здесь не зовут
    вовсе, потому что остаток уходит адъюдикатору как есть."""
    kept: list[dict[str, Any]] = []
    for c in cands:
        keyc = norm(c["issue"])
        dup = False
        for k in kept:
            if k["file"] != c["file"]:
                continue
            other = norm(k["issue"])
            if not keyc or not other:
                continue
            overlap = len(keyc & other) / max(len(keyc | other), 1)
            if overlap >= 0.6:
                k.setdefault("also", []).append(c["by"])
                dup = True
                break
        if not dup:
            kept.append(dict(c))
    return kept


def paid_findings(goldset: pathlib.Path) -> dict[str, set[str]]:
    """Файлы, которые назвал ДОРОГОЙ ревьюер, по задачам."""
    out: dict[str, set[str]] = {}
    path = goldset / "verdicts.jsonl"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        task = row.get("task")
        for f in (row.get("findings") or []):
            if isinstance(f, dict) and f.get("file"):
                out.setdefault(str(task), set()).add(str(f["file"]).lstrip("./"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stand", required=True)
    ap.add_argument("--goldset", default=str(pathlib.Path(__file__).parent.parent
                                             / "goldset"))
    ap.add_argument("--models", required=True,
                    help="через запятую; линзы раздаются по кругу")
    ap.add_argument("--tasks", default="", help="фильтр по id, через запятую")
    ap.add_argument("--max-diff-lines", type=int, default=1200)
    ap.add_argument("--cap", type=int, default=5, help="потолок находок у юрора")
    ap.add_argument("--max-tokens", type=int, default=900)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    stand = pathlib.Path(args.stand).expanduser()
    goldset = pathlib.Path(args.goldset).expanduser()
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    helpers.configure(out / "helper-metrics.jsonl")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    lenses = list(LENSES)
    roster = [(m, lenses[i % len(lenses)]) for i, m in enumerate(models)]
    only = {t.strip() for t in args.tasks.split(",") if t.strip()}

    tasks = []
    for line in (goldset / "tasks.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("commit") and (not only or row["id"] in only):
            tasks.append(row)

    paid = paid_findings(goldset)
    raw_path = out / "panel-raw.jsonl"
    cand_path = out / "panel-candidates.jsonl"
    raw_f = raw_path.open("w", encoding="utf-8")
    cand_f = cand_path.open("w", encoding="utf-8")
    summary: dict[str, dict[str, Any]] = {m: {"calls": 0, "wall_s": 0.0,
                                              "cands": 0, "off_diff": 0,
                                              "refused": 0, "majors": 0}
                                          for m in models}
    per_task = []

    for task in tasks:
        diff = diff_of(stand, str(task["commit"]))
        if not diff:
            print(f"  {task['id']}: коммит {task['commit']} не читается — пропуск")
            continue
        lines = diff.splitlines()
        skipped = len(lines) > args.max_diff_lines
        if skipped:
            diff = "\n".join(lines[:args.max_diff_lines])
        files = changed_files(diff)
        cands: list[dict[str, Any]] = []
        for model, lens in roster:
            prompt = PROMPT.format(lens_name=lens, lens_desc=LENSES[lens],
                                   cap=args.cap, title=task.get("title", ""),
                                   diff=diff)
            t0 = time.monotonic()
            raw = helpers.ollama_chat(prompt, name=f"panel:{lens}",
                                      max_tokens=args.max_tokens, model=model)
            wall = time.monotonic() - t0
            parsed, refusal = parse_candidates(raw or "", files)
            for c in parsed:
                c["by"] = f"{model}/{lens}"
                c["task"] = task["id"]
            cands.extend(parsed)
            s = summary[model]
            s["calls"] += 1
            s["wall_s"] += wall
            s["cands"] += len(parsed)
            s["off_diff"] += sum(1 for c in parsed if c["off_diff"])
            s["majors"] += sum(1 for c in parsed if c["severity"] == "major")
            if refusal or raw is None:
                s["refused"] += 1
            raw_f.write(json.dumps({
                "task": task["id"], "model": model, "lens": lens,
                "wall_s": round(wall, 2), "parsed": len(parsed),
                "refusal": refusal or (None if raw else "no_answer"),
                "diff_truncated": skipped, "raw": (raw or "")[:4000],
            }, ensure_ascii=False) + "\n")
        merged = dedup([c for c in cands if not c["off_diff"]])
        for c in merged:
            cand_f.write(json.dumps(c, ensure_ascii=False) + "\n")
        named = {c["file"] for c in merged}
        overlap = named & paid.get(task["id"], set())
        per_task.append({"task": task["id"], "files": len(files),
                         "raw": len(cands), "merged": len(merged),
                         "paid_files": len(paid.get(task["id"], set())),
                         "shared_files": len(overlap),
                         "truncated": skipped})
        print(f"  {task['id']}: файлов {len(files)}, кандидатов "
              f"{len(cands)} → после дедупа {len(merged)}, "
              f"пересечение по файлам с дорогим ревью {len(overlap)}")
    raw_f.close()
    cand_f.close()

    print("\n=== по моделям ===")
    for m in models:
        s = summary[m]
        if not s["calls"]:
            continue
        print(f"  {m}: вызовов {s['calls']}, отказов {s['refused']}, "
              f"кандидатов {s['cands']} "
              f"({s['cands'] / s['calls']:.1f} на дифф), "
              f"мимо диффа {s['off_diff']}, major {s['majors']}, "
              f"{s['wall_s'] / s['calls']:.1f} с на вызов")
    if per_task:
        avg = sum(t["merged"] for t in per_task) / len(per_task)
        print(f"\nПосле дедупа в среднем {avg:.1f} кандидатов на дифф "
              f"(ворота плана §4: < 10).")
    (out / "panel-summary.json").write_text(
        json.dumps({"by_model": summary, "by_task": per_task,
                    "roster": roster, "cap": args.cap},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nСырьё: {raw_path}\nКандидаты: {cand_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
