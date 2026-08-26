#!/usr/bin/env python3
"""Снимок вердиктов ревьюера ДО и ПОСЛЕ правки блока Rules (q023).

ЗАЧЕМ. Рубрика severity и правило про вывод гейта меняют то, что ревьюера
просят СУДИТЬ. «Кэш-бесплатно» не значит «без эффекта», а у роя правило:
у каждой формулировки есть замер (q027). Этот стенд и есть замер: один и
тот же замороженный вход прогоняется через текущий блок Rules и через
исправленный, разница читается на парах.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ.

- Сравнения с ЗАМОРОЖЕННЫМИ вердиктами голдсета как с «до». Они сделаны
  тремя разными руками (claude-opus-5[1m] ×18, claude-opus-5 ×11,
  claude-sonnet-5 ×6) и под старым Rules: разница мерила бы состав рук, а
  не рубрику. «До» — это ПЕРВЫЙ СРЕЗ ЭТОГО СТЕНДА, снятый той же рукой,
  что и «после». Голдсет даёт только список случаев и разметку человека.
- Жребия руки. Рука прибита к одной модели в обоих срезах: при CV 27 % на
  вызов (Бриф 4) разброс между руками перекрыл бы эффект рубрики, и стенд
  выдал бы «рубрика ничего не двигает» как свойство ДИЗАЙНА, а не факт о
  рубрике. Цена: результат — «эффект на ЭТОЙ руке», на пул не переносится.
- Одного вызова на срез. Повтор внутри среза и есть оценка шума: без него
  разницу между срезами не отличить от разброса вызова к вызову.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ ВХОДА. Дифф восстанавливается как `git show <commit>`,
то есть ревьюер видит код УЖЕ ИСПРАВЛЕННЫМ, а не тем, что видел ревьюер
пилота (та же ловушка описана в experiments/reviewarm/replay.py). Это
допустимо ровно потому, что оба среза получают побайтово одинаковый вход и
сравниваются друг с другом, а не с вердиктами пилота.

Запуск:
    python3 snapshot.py --slice before --out out/before.jsonl
    python3 snapshot.py --slice after  --out out/after.jsonl --limit 2
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
SWARM = REPO / "tools" / "swarm" / "swarm"
SCHEMAS = REPO / "tools" / "swarm" / "schemas"
GOLDSET = REPO / "experiments" / "goldset"
STAND = pathlib.Path("~/work/zeus-pilot").expanduser()

sys.path.insert(0, str(SWARM))
import parsing  # noqa: E402
import promptbuilder  # noqa: E402
import reviewer  # noqa: E402


def sh(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True,
                          check=True).stdout


def diff_of(commit: str) -> str:
    """Дифф случая — СВЁРНУТЫЙ, ровно как его видит ревьюер в петле.

    Команда взята из reviewarm/replay.py, чтобы стенды не разошлись, а
    свёртка — из `reviewer.review`, и она здесь обязательна: сырой дифф
    c4rp это 55 589 строк корпусных фикстур и промпт на 3.75 МБ. Петля
    столько не отправляет никогда (`condense_diff` схлопывает файлы
    длиннее 400 строк в сводку), и замер на сыром диффе мерил бы условия,
    которых в проде не существует, — за деньги, которых прод не тратит.
    """
    raw = sh(["git", f"--git-dir={STAND / '.git'}", "show", "--format=",
              "--unified=3", commit])
    return parsing.condense_diff(raw)


def load_cases() -> list[dict[str, object]]:
    """Случаи: id и коммит из голдсета, содержание задачи — со стенда.

    В голдсете у задачи есть только id, title, type и commit. Промпт
    ревьюера строится вокруг приёмки («Acceptance defines done»), а сама
    рубрика q023 определяет blocker через «приёмка фактически не
    выполнена». С пустой приёмкой замер проверял бы рубрику в состоянии,
    которого в проде не бывает, — поэтому spec/acceptance/paths берутся
    из настоящей записи прогона в .swarm/tasks.json стенда.
    """
    gold = [json.loads(x) for x in
            (GOLDSET / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    stand_tasks = json.loads(
        (STAND / ".swarm" / "tasks.json").read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in stand_tasks.get("tasks", [])}
    gates: dict[str, str] = {}
    for line in (STAND / ".swarm" / "metrics.jsonl").read_text(
            encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # Первый гейт задачи — тот, что сопровождал её первое ревью.
        # Брать «худший» значило бы подбирать вход под ожидаемый ответ.
        if row.get("phase") == "gate" and row["task"] not in gates:
            gates[row["task"]] = str(row.get("tail") or "")
    cases = []
    for g in gold:
        commit = g.get("commit")
        task = by_id.get(g["id"])
        if not commit or not task:
            continue
        cases.append({"id": g["id"], "commit": commit, "task": task,
                      "gate_tail": gates.get(g["id"], "")})
    cases.sort(key=lambda c: str(c["id"]))
    return cases


def review_once(prompt: str, model: str, timeout: int) -> dict[str, object]:
    """Один вызов ревьюера. Инструменты те же, что в петле: без обхода
    репозитория ревью было бы дешевле, но это было бы другое ревью."""
    schema = (SCHEMAS / "verdict-v1.schema.json").read_text(encoding="utf-8")
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--json-schema", schema,
           "--allowedTools", "Read,Grep,Glob,Bash(git diff:*)",
           "--model", model]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(STAND), timeout=timeout, check=False)
    wall = round(time.monotonic() - started, 1)
    try:
        env = json.loads(proc.stdout)
    except ValueError:
        return {"wall_s": wall, "cost_usd": None, "verdict": None,
                "error": (proc.stdout or proc.stderr)[:300]}
    verdict = env.get("structured_output")
    row: dict[str, object] = {
        "wall_s": wall,
        "cost_usd": env.get("total_cost_usd"),
        "is_error": env.get("is_error"),
        "terminal_reason": env.get("terminal_reason"),
        "verdict": (verdict or {}).get("verdict"),
        "findings": len((verdict or {}).get("findings") or []),
    }
    row.update(reviewer._severity_counts(verdict))
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", required=True, choices=["before", "after"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="0 = все случаи")
    ap.add_argument("--budget-usd", type=float, default=8.0)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    if not STAND.exists():
        print(f"стенда нет: {STAND} — мерить нечего, это не зелёный прогон",
              file=sys.stderr)
        return 3

    cases = load_cases()
    if args.limit:
        cases = cases[:args.limit]
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Состав и пин пишутся ДО первого вызова: набор, объявленный после
    # результата, — уже не набор.
    header = {"kind": "header", "slice": args.slice, "model": args.model,
              "effort": "CLI default (не задан, одинаков в обоих срезах)",
              "repeats": args.repeats, "budget_usd": args.budget_usd,
              "cases": [c["id"] for c in cases]}
    with out.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
    print(f"срез {args.slice}: {len(cases)} случаев × {args.repeats} повтора "
          f"= {len(cases) * args.repeats} вызовов, рука {args.model}")

    spent = 0.0
    for case in cases:
        task = dict(case["task"])  # type: ignore[arg-type]
        diff = diff_of(str(case["commit"]))
        prompt = promptbuilder.review_prompt(
            task, str(case["gate_tail"]), diff)
        for rep in range(1, args.repeats + 1):
            if spent >= args.budget_usd:
                print(f"ПОТОЛОК {args.budget_usd}$ достигнут (потрачено "
                      f"{spent:.2f}$) — обрываю громко, а не молча",
                      file=sys.stderr)
                return 4
            row = review_once(prompt, args.model, args.timeout)
            row.update({"kind": "review", "case": case["id"], "repeat": rep,
                        "slice": args.slice,
                        "diff_lines": len(diff.splitlines()),
                        "gate_tail_chars": len(str(case["gate_tail"]))})
            spent += float(row.get("cost_usd") or 0.0)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {case['id']} #{rep}: {row.get('verdict')} "
                  f"b/m/n={row.get('blockers')}/{row.get('majors')}/"
                  f"{row.get('minors')} {row.get('wall_s')}s "
                  f"${row.get('cost_usd')} | всего ${spent:.2f}")
    print(f"срез {args.slice} готов: ${spent:.2f}, файл {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
