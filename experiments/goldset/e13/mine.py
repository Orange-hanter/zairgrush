#!/usr/bin/env python3
"""E13: майнинг кандидатов в датасет расходящихся плеч (NXT-019).

Журнал §21 называет дыру E13: вывод «движок исполнителя не влияет на
качество» снят на задачах, где ОБА плеча сходятся за раунд, а набора, где
они расходятся числом раундов, у программы нет. Этот скрипт собирает такой
набор из того, что уже записано на диске, — офлайн, без платных вызовов.

Критерии (все три опираются на поля, которые метрики стендов реально
пишут: iter у implement/review строк, verdict у review, reason у
implement/result, terminal_reason у review):

- K1 PAIRED-SPLIT — одна задача прогнана парой стендов с одного коммита
  и плечи закрылись РАЗНЫМ числом итераций. Прямое наблюдение
  расхождения; единственный такой случай в данных — s2ky (2 против 3).
- K2 VERDICT-FLIP — один и тот же (задача, итерация) дифф в записанных
  вызовах ревьюера получил и approve, и request_changes. Жребий вердикта
  на первой итерации напрямую меняет число раундов прогона.
- K3 REJECTION-ROUND — хотя бы один записанный прогон задачи ушёл на
  iter >= 2 с настоящим сигналом отката: verdict=request_changes, либо
  implement/result reason в {dispute, crash, max_iterations}. Задача
  доказанно не сходится за раунд — у парных плеч есть где разойтись.

НЕ кандидаты (записываются со status=near-miss): двенадцать задач E13
(один раунд на обоих плечах — негативный контроль), канареечные задачи
(request_changes посеян, меряют ревьюера), задачи, заблокированные на
первой итерации, многораундовые задачи пилота без единого отката
(раунды добавлены механическим доведением minor-находок — путь вердикта
не раздваивался), а также расхождения, сведённые к инфра-сбоям
(structured_output_retry_exhausted, api_error): это не разногласие
судьи, а счёт за сломанный конверт.

Источники — scratch, не в git (см. .gitignore): стенды stand-*/
.swarm/metrics.jsonl, bench/*.jsonl, neg/metrics.jsonl,
canary/metrics.jsonl плюс отслеживаемые goldset/reviews_metrics.jsonl и
goldset/labels.jsonl. Как boundaries/replay.py, скрипт не падает на
отсутствующем стенде — пропускает источник и говорит об этом в сводке.

Запуск: python3 mine.py [--base PATH-TO-experiments]
Пишет candidates.jsonl рядом с собой, печатает сводку в stdout.
"""
import argparse
import collections
import json
import pathlib
import sys

REJECTION_REASONS = {"dispute", "crash", "max_iterations"}
INFRA_TERMINAL = {"structured_output_retry_exhausted", "api_error"}

# Парные стенды: одна задача, один стартовый коммит, разные плечи.
PAIRS = [
    ("stand-e13-a", "stand-e13-b"),
    ("stand-e13r2-a", "stand-e13r2-b"),
    ("stand-e9-exec", "stand-e9-off"),
    ("stand-e9-s2-exec", "stand-e9-s2-off"),
]

# Одиночные прогоны с полной траекторией (iter/reason/verdict).
SINGLE_RUNS = [
    "bench/e8-metrics.jsonl",
    "bench/metrics.jsonl",
    "bench/b2-metrics.jsonl",
    "bench/b3-metrics.jsonl",
    "neg/metrics.jsonl",
]

# Откуда задачу можно переиграть в платном прогоне.
REPLAY = {
    "r1cf": "bench/b2-tasks.json + stand-e13r2-{a,b} (brownfield red-state)",
    "r2lv": "bench/b2-tasks.json + stand-e13r2-{a,b}",
    "r3cs": "bench/b2-tasks.json + stand-e13r2-{a,b}",
    "r4tt": "bench/b2-tasks.json + stand-e13r2-{a,b}",
    "r5rp": "bench/b2-tasks.json + stand-e13r2-{a,b}",
    "r6ng": "bench/b2-tasks.json + stand-e13r2-{a,b}",
    "s5ex": "bench/b3-tasks.json",
    "s2ky": "stand-e9-s2-{exec,off}, стартовый коммит ee53c217 (findings 2026-08-24)",
    "v9lb": "stand-e9-{exec,off} (прогон 2026-08-23 умер на api_error — replay с нуля)",
    "n2trap": "neg/metrics.jsonl, стенда на диске нет — спеку восстанавливать по журналу",
}
PILOT_REPLAY = "~/work/zeus-pilot (стенд пилота; задача и решения — goldset/tasks.jsonl)"


def rows(path):
    out = []
    if not path.exists():
        return out, False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, True


def trajectory(rs):
    """Сводка одного источника по задаче: итерации, откаты, инфра-сбои."""
    iters, rejections, infra = set(), [], []
    for r in rs:
        if r.get("iter") is not None:
            iters.add(r["iter"])
        ph = r.get("phase")
        if ph == "review":
            if r.get("verdict") == "request_changes":
                rejections.append(f"review rc iter{r.get('iter')} findings={r.get('findings')}")
            if r.get("terminal_reason") in INFRA_TERMINAL:
                infra.append(f"review {r['terminal_reason']} iter{r.get('iter')}")
        elif ph == "implement":
            reason = r.get("reason") or r.get("report_status")
            if reason in REJECTION_REASONS:
                rejections.append(f"implement {reason} iter{r.get('iter')}")
            if r.get("terminal_reason") in INFRA_TERMINAL | {"budget_exhausted"}:
                infra.append(f"implement {r['terminal_reason']} iter{r.get('iter')}")
        elif r.get("result") == "blocked" or r.get("reason") in REJECTION_REASONS:
            rejections.append(f"result {r.get('reason') or r.get('result')} iters={r.get('iters')}")
    return {"iters": iters, "rejections": rejections, "infra": infra}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parent.parent.parent,
                    help="каталог experiments/ (по умолчанию — от расположения скрипта)")
    args = ap.parse_args()
    base = args.base
    out_path = pathlib.Path(__file__).resolve().parent / "candidates.jsonl"

    evidence = collections.defaultdict(list)   # task -> grounds evidence strings
    grounds = collections.defaultdict(set)     # task -> {K1,K2,K3}
    flags = collections.defaultdict(set)
    notes = collections.defaultdict(set)
    near_misses = []
    missing = []

    # --- K1: парные стенды, разное число итераций ---
    for a, b in PAIRS:
        ra, ok_a = rows(base / a / ".swarm" / "metrics.jsonl")
        rb, ok_b = rows(base / b / ".swarm" / "metrics.jsonl")
        if not (ok_a and ok_b):
            missing.append(f"{a}|{b}")
            continue
        ta = collections.defaultdict(list)
        tb = collections.defaultdict(list)
        for r in ra:
            if r.get("task"):
                ta[r["task"]].append(r)
        for r in rb:
            if r.get("task"):
                tb[r["task"]].append(r)
        for task in sorted(set(ta) & set(tb)):
            ja, jb = trajectory(ta[task]), trajectory(tb[task])
            na, nb = max(ja["iters"] or [0]), max(jb["iters"] or [0])
            # Расхождение раундов считается, только когда ОБА плеча дошли до
            # валидного вердикта на последней итерации: плечо, умершее на
            # api_error, не «сошлось за N раундов», а не дошло вовсе.
            conv_a = any(r.get("phase") == "review" and r.get("verdict")
                         and r.get("terminal_reason") == "completed"
                         and r.get("iter") == na for r in ta[task])
            conv_b = any(r.get("phase") == "review" and r.get("verdict")
                         and r.get("terminal_reason") == "completed"
                         and r.get("iter") == nb for r in tb[task])
            if na != nb and conv_a and conv_b:
                grounds[task].add("K1")
                evidence[task].append(
                    f"K1: {a} закрыл за {na} итер., {b} за {nb} ({a}/.swarm/metrics.jsonl)")
            elif na != nb:
                flags[task].add(
                    f"парные плечи {a}|{b} разошлись числом итераций ({na} против {nb}), "
                    "но хотя бы одно плечо не дошло до валидного вердикта — это инфра-сбой, "
                    "не расхождение")
            for side, j, st in ((a, ja, a), (b, jb, b)):
                # Стороны парного стенда — тоже записанные прогоны: кормим K3.
                # Порог «второй раунд» смотрим по ПАРЕ: v9lb off-плечо остановлено
                # оператором после rc на iter1, а не сошлось.
                if max(na, nb) >= 2 and j["rejections"]:
                    grounds[task].add("K3")
                    evidence[task].append(
                        f"K3: {st}/.swarm/metrics.jsonl: {'; '.join(j['rejections'])}")
                if j["infra"]:
                    flags[task].add(f"инфра-сбой в {st}: {'; '.join(j['infra'])}")
            if na <= 1 and nb <= 1 and not ja["rejections"] and not jb["rejections"]:
                notes[task].add(
                    f"в парном прогоне {a}|{b} оба плеча сошлись за 1 раунд — "
                    "расхождение не гарантировано, зависит от розыгрыша вердикта")
                near_misses.append({
                    "task": task, "source": f"{a}|{b}",
                    "why": "оба плеча закрыли за один раунд без откатов — негативный контроль E13"})

    # --- K3: одиночные прогоны с реальным откатом на iter >= 2 ---
    for rel in SINGLE_RUNS:
        rs, ok = rows(base / rel)
        if not ok:
            missing.append(rel)
            continue
        by_task = collections.defaultdict(list)
        for r in rs:
            if r.get("task"):
                by_task[r["task"]].append(r)
        for task, trs in sorted(by_task.items()):
            j = trajectory(trs)
            if max(j["iters"] or [0]) >= 2 and j["rejections"]:
                grounds[task].add("K3")
                evidence[task].append(f"K3: {rel}: {'; '.join(j['rejections'])}")
            elif j["rejections"]:
                near_misses.append({
                    "task": task, "source": rel,
                    "why": f"откат без второго раунда ({'; '.join(j['rejections'])}) — разойтись числом раундов негде"})

    # --- K2/K3 из журнала ревью пилота (отслеживаемый источник) ---
    rs, ok = rows(base / "goldset" / "reviews_metrics.jsonl")
    if not ok:
        missing.append("goldset/reviews_metrics.jsonl")
    by_ti = collections.defaultdict(list)
    max_iter = collections.defaultdict(int)
    for r in rs:
        if r.get("phase") != "review" or not r.get("verdict"):
            continue
        by_ti[(r["task"], r.get("iter"))].append(r)
        max_iter[r["task"]] = max(max_iter[r["task"]], r.get("iter") or 0)
    for (task, it), calls in sorted(by_ti.items()):
        verdicts = {c["verdict"] for c in calls}
        if len(verdicts) > 1:
            hands = {(c.get("model"), c.get("effort")) for c in calls}
            same_hand = len(hands) == 1
            grounds[task].add("K2")
            kind = "переигровка той же руки" if same_hand else "разные руки/усилие"
            desc = "; ".join(f"{c.get('model')}/{c.get('effort')}={c['verdict']}"
                             f"({c.get('findings')} нах.)" for c in calls)
            evidence[task].append(f"K2: iter{it} {kind}: {desc} (goldset/reviews_metrics.jsonl)")
            if same_hand:
                flags[task].add("флип на переигровке той же руки — путь вердикта нестабилен сам по себе")
    for task, mi in sorted(max_iter.items()):
        rcs = [c for (t, _), calls in by_ti.items() if t == task
               for c in calls if c["verdict"] == "request_changes"]
        if mi >= 2 and rcs:
            grounds[task].add("K3")
            evidence[task].append(
                f"K3: пилот, {mi} итер., request_changes: "
                + "; ".join(f"iter{c.get('iter')} {c.get('findings')} нах." for c in rcs))
        elif mi >= 2 and task not in grounds:
            near_misses.append({
                "task": task, "source": "goldset/reviews_metrics.jsonl",
                "why": f"{mi} итерации без единого request_changes — раунды добавлены "
                       "доведением minor-находок, путь вердикта не раздваивался"})

    # --- Споры пилота как дополнительная улика K3 (labels.jsonl) ---
    ls, ok = rows(base / "goldset" / "labels.jsonl")
    if not ok:
        missing.append("goldset/labels.jsonl")
    for l in ls:
        if l.get("subject") == "executor_dispute" and l.get("disposition", "").startswith("endorsed"):
            task = l["task"]
            grounds[task].add("K3")
            evidence[task].append(
                f"K3: спор {l['qid']} {l['disposition']} — «{l['claim'][:90]}» (goldset/labels.jsonl)")

    # --- Канарейки: отдельным классом near-miss ---
    rs, ok = rows(base / "canary" / "metrics.jsonl")
    if ok:
        seen = set()
        for r in rs:
            c = r.get("canary")
            if c and c not in seen and r.get("verdict") == "request_changes":
                seen.add(c)
                near_misses.append({
                    "task": c, "source": "canary/metrics.jsonl",
                    "why": "request_changes посеян по построению — стенд меряет ревьюера, "
                           "расхождение исполнителей здесь не эмерджентно"})
    else:
        missing.append("canary/metrics.jsonl")

    # --- Сборка строк ---
    out = []
    for task in sorted(grounds):
        replay = REPLAY.get(task, PILOT_REPLAY)
        out.append({
            "id": task,
            "status": "candidate",
            "grounds": sorted(grounds[task]),
            "evidence": evidence[task],
            "replay": replay,
            "flags": sorted(flags[task]),
            "notes": sorted(notes[task]),
        })
    nm_keys = set()
    for nm in near_misses:
        key = (nm["task"], nm["source"])
        if key in nm_keys or nm["task"] in grounds:
            continue
        nm_keys.add(key)
        out.append({"id": nm["task"], "status": "near-miss", "source": nm["source"],
                    "why": nm["why"]})

    with out_path.open("w", encoding="utf-8") as f:
        for row in out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    cands = [r for r in out if r["status"] == "candidate"]
    print(f"кандидаты: {len(cands)}")
    for r in cands:
        print(f"  {r['id']:6s} {','.join(r['grounds'])}"
              + (f"  [flags: {'; '.join(r['flags'])}]" if r["flags"] else ""))
    print(f"near-miss: {len(out) - len(cands)}")
    if missing:
        print("источники пропущены (нет на диске): " + ", ".join(missing))
    print(f"записано: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
