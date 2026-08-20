#!/usr/bin/env python3
"""Harvest a frozen reviewer gold set from a swarm stand.

Extracts, WITHOUT modification, the four artifact streams a
review-quality bench needs:

- ``verdicts.jsonl``  — every ``*-review.json`` envelope on the stand:
  verdict, findings (full), summary, capped analysis, cost, models;
- ``questions.jsonl`` — ask_user question/answer pairs from the run
  journal: the human labels this corpus exists for;
- ``reviews_metrics.jsonl`` — every review-phase metrics row (arms,
  costs, cache telemetry) — unlike the files above this stream keeps
  ALL calls, including ones whose verdict file was later overwritten
  by a requeued iteration;
- ``tasks.jsonl``     — task outcomes and human_decisions.

Deterministic by construction: same stand state -> byte-identical
output (input order is append order; files are sorted by name; no
timestamps are invented). Provenance (run_id, swarm_sha, ts) travels
in every row untouched. The harvester never writes to the stand.

Usage::

    python3 harvest.py --stand ~/work/zeus-pilot --out .
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from typing import Any

ANALYSIS_CAP = 4000
_STEM = re.compile(r"^(?P<task>[a-z0-9]+)-i(?P<iter>\d+)-"
                   r"(?P<phase>[av])(?P<attempt>\d+)-review\.json$")


def _rows(path: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _structured(env: dict[str, Any]) -> dict[str, Any]:
    so = env.get("structured_output")
    if isinstance(so, dict):
        return so
    try:
        parsed = json.loads(env.get("result") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def harvest_verdicts(log: pathlib.Path) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(log.glob("*-review.json")):
        m = _STEM.match(p.name)
        if not m:
            continue
        try:
            env = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            rows.append({"stem": p.stem, "unparsed": True})
            continue
        so = _structured(env)
        analysis = str(so.get("analysis") or "")
        rows.append({
            "stem": p.stem,
            "task": m.group("task"),
            "iter": int(m.group("iter")),
            "phase": m.group("phase"),
            "attempt": int(m.group("attempt")),
            "verdict": so.get("verdict"),
            "summary": so.get("summary"),
            "analysis": analysis[:ANALYSIS_CAP],
            "analysis_truncated": len(analysis) > ANALYSIS_CAP,
            "findings": so.get("findings") or [],
            "total_cost_usd": env.get("total_cost_usd"),
            "terminal_reason": env.get("terminal_reason"),
            "api_error_status": env.get("api_error_status"),
            "models": sorted((env.get("modelUsage") or {}).keys()),
        })
    return rows


def harvest_questions(journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for r in journal:
        kind, qid = r.get("kind"), str(r.get("qid") or "")
        if not qid:
            continue
        if kind == "question":
            merged[qid] = {
                "qid": qid, "task": r.get("task"), "qkind": r.get("qkind"),
                "question": r.get("question"),
                "findings": r.get("findings") or [],
                "asked_ts": r.get("ts"), "run_id": r.get("run_id"),
                "answer": None, "answered_ts": None,
            }
        elif kind == "answer" and qid in merged:
            merged[qid]["answer"] = r.get("text")
            merged[qid]["answered_ts"] = r.get("ts")
    return [merged[q] for q in sorted(merged)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stand", required=True)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    stand = pathlib.Path(args.stand).expanduser()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = stand / ".swarm" / "log"

    journal = _rows(log / "run.jsonl")
    metrics = [r for r in _rows(stand / ".swarm" / "metrics.jsonl")
               if r.get("phase") == "review"]
    tasks = [{k: t.get(k) for k in ("id", "title", "type", "status",
                                    "human_decisions", "diagnosis",
                                    "commit", "deps")}
             for t in json.loads(
                 (stand / ".swarm" / "tasks.json").read_text(
                     encoding="utf-8")).get("tasks", [])]

    streams = {
        "verdicts.jsonl": harvest_verdicts(log),
        "questions.jsonl": harvest_questions(journal),
        "reviews_metrics.jsonl": metrics,
        "tasks.jsonl": tasks,
    }
    for name, rows in streams.items():
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        (out / name).write_text(text, encoding="utf-8")
        print(f"{name}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
