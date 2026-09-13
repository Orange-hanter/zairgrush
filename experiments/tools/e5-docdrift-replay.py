#!/usr/bin/env python3
"""E5: реплей дрейфа docs↔code из git-истории — A-эвристика против docmap (B).

БЕСПЛАТНАЯ половина замера E5: оба детектора здесь механические, LLM не
вызывается. Платная часть («LLM формулирует правку» / токены хелпера A)
намеренно НЕ реализована — см. итоговую строку DEFERRED.

Земля (два класса, оба из истории, без ручной разметки):
  co_updated    — документ изменён В ТОМ ЖЕ коммите, что и код: автор сам
                  решил, что док затронут. Детектор обязан был его назвать.
  later_updated — документ НЕ изменён в коммите кода, но изменён в одном
                  из следующих --window коммитов: дрейф существовал и был
                  починен позже. Это и есть «пропущенный рассинхрон»,
                  если детектор его не назвал.

Вариант A (прокси §7.2-хелпера «изменённые файлы ↔ упоминания в docs/»):
документ подозреваем, если его текст на момент коммита упоминает имя
изменённого файла (basename с расширением). Вариант B: docmap.suspects
по СЕГОДНЯШНЕЙ карте (оговорка: карта написана постфактум — это снимает
с замера вопрос «поддерживали бы карту», а не «находит ли она»).

Запуск:  python3 e5-docdrift-replay.py [--repo ПУТЬ] [--window 10]
Выход:   сводка в stdout + сырые строки в experiments/e5/drift-replay.jsonl
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import pathlib
import subprocess
import sys
import tomllib

# docmap.py — чистый stdlib, грузим по пути (рой не установлен пакетом).
sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[2] / "tools/swarm/swarm")
)
import docmap  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
CODE_GLOB = "tools/swarm/swarm/*.py"


def git(repo: pathlib.Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout


def commit_files(repo: pathlib.Path, sha: str) -> list[str]:
    out = git(repo, "show", "--name-only", "--format=", sha)
    return [f for f in out.splitlines() if f.strip()]


def doc_text_at(repo: pathlib.Path, sha: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{path}"],
        capture_output=True, text=True, check=False,
    )
    return r.stdout if r.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--window", type=int, default=10,
                    help="сколько следующих коммитов смотреть для later_updated")
    ap.add_argument("--out", default=str(REPO / "experiments/e5/drift-replay.jsonl"))
    args = ap.parse_args()
    repo = pathlib.Path(args.repo)

    entries = docmap.load(repo / docmap.MAP_NAME)
    doc_paths = sorted({ref.path for e in entries for ref in e.docs})

    shas = git(
        repo, "log", "--no-merges", "--format=%H", "--", CODE_GLOB
    ).split()
    rows = []
    for i, sha in enumerate(shas):
        files = commit_files(repo, sha)
        code = [f for f in files if fnmatch.fnmatchcase(f, CODE_GLOB)]
        if not code:
            continue
        touched_docs = {f for f in files if f in doc_paths}
        later_docs = {
            f
            for later in shas[max(0, i - args.window):i]  # shas: новые первыми
            for f in commit_files(repo, later)
            if f in doc_paths
        }
        basenames = {pathlib.Path(f).name for f in code}
        a_docs: set[str] = set()
        for d in doc_paths:
            text = doc_text_at(repo, sha, d)
            if text is not None and any(b in text for b in basenames):
                a_docs.add(d)
        b_docs = {s.doc.split("#")[0] for s in docmap.suspects(code, entries)}
        for d in doc_paths:
            if doc_text_at(repo, sha, d) is None:
                continue  # документа ещё не было — вне замера
            if d in touched_docs:
                truth = "co_updated"
            elif d in later_docs:
                truth = "later_updated"
            else:
                truth = "quiet"
            rows.append({
                "sha": sha[:8], "doc": d, "truth": truth,
                "a_flag": d in a_docs, "b_flag": d in b_docs,
                "code": code,
            })

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )

    print(f"коммитов кода: {len({r['sha'] for r in rows})}, "
          f"строк (коммит×док): {len(rows)}, сырьё: {out_path}")
    for variant, key in (("A (упоминание имени файла)", "a_flag"),
                         ("B (docmap)", "b_flag")):
        print(f"\n=== {variant} ===")
        for truth in ("co_updated", "later_updated"):
            sub = [r for r in rows if r["truth"] == truth]
            hits = [r for r in sub if r[key]]
            missed = [r for r in sub if not r[key]]
            print(f"  {truth}: названо {len(hits)}/{len(sub)}, "
                  f"пропущено {len(missed)}")
            for r in missed:
                print(f"    ПРОПУСК {r['sha']} {r['doc']} ← {r['code']}")
        noise = [r for r in rows if r["truth"] == "quiet" and r[key]]
        print(f"  ложных подозрений (док молчал в окне): {len(noise)}")
    print("\nDEFERRED: токены хелпера и «LLM формулирует правку» — платная "
          "половина E5, здесь не измерялась (обе механические половины — "
          "0 токенов по построению).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
