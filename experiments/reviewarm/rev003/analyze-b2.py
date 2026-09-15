#!/usr/bin/env python3
"""B2 (REV-005) analysis: цена, объём, retention инвертированного контракта.

Те же joins, что rev003/analyze.py (main-дерево, scratch), но:
- метрики: metrics-rev003-b2.jsonl, сырьё: raw-b2/
- кандидаты: ../out2/panel-candidates.jsonl РЕГЕНЕРИРОВАННОЙ панели
  (worktree), subset трёх присяжных
- добавлен подсчёт downgrade-пометок в rationale (сигнатура контракта B2)
- без ценового контраста с rev002/stats.json (scratch не переносился)

Запуск до прогона (только пересчёт baselines): файл метрик ещё нет —
скрипт печатает baselines и выходит. Запуск после: полный скоринг.
"""
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
REV1 = HERE.parent / "rev001"
GROUND = REV1 / "ground"
GOLDSET = HERE.parent.parent / "goldset"
CANDIDATES = HERE.parent / "out2" / "panel-candidates.jsonl"
METRICS = HERE / "metrics-rev003-b2.jsonl"
RAW = HERE / "raw-b2"
SUBSET = ("glm-5.1", "gpt-oss:120b", "qwen3.5:397b")
# механическая метка downgrade/неподтверждённости в rationale (RU-контракт)
DOWNGRADE_MARK = re.compile(
    r"не подтвержд|пониз|пониж|downgrad|неуверен", re.I)


def diff_files(task):
    key = "s2ky-i2" if task == "s2ky" else f"{task}-close"
    p = GROUND / f"{key}.raw.diff"
    if not p.exists():
        p = GROUND / f"{key}.diff"
    return {l[6:].strip() for l in p.read_text(errors="replace").splitlines()
            if l.startswith("+++ b/")}


def load_bases():
    verdicts = [json.loads(l) for l in
                GOLDSET.joinpath("verdicts.jsonl").read_text().splitlines()]
    cands = [json.loads(l) for l in
             CANDIDATES.read_text().splitlines()
             if json.loads(l)["by"].split("/")[0] in SUBSET]

    paid_files = {}
    major_files = {}
    for v in verdicts:
        for f in v.get("findings") or []:
            if not f.get("file"):
                continue
            paid_files.setdefault(v["task"], set()).add(f["file"])
            if f.get("severity") == "major":
                major_files.setdefault(v["task"], set()).add(f["file"])

    def norm(s):
        return s.lstrip("./")

    panel_files = {}
    for c in cands:
        panel_files.setdefault(c["task"], set()).add(norm(c["file"]))
    return paid_files, major_files, panel_files, norm


def print_baselines(paid_files, major_files, panel_files):
    print("=== пересчитанные baselines (регенерированный subset, "
          "до адъюдикации) ===")
    addr_base = 0
    all_major = []
    for task in sorted(set(paid_files) | set(panel_files)):
        pf = paid_files.get(task, set())
        ppan = pf & panel_files.get(task, set())
        addr_base += len(ppan)
        mj = major_files.get(task, set())
        mj_hit = [f for f in mj if any(f == p or f.endswith(p) or p.endswith(f)
                                       for p in panel_files.get(task, set()))]
        all_major.append((task, sorted(mj), sorted(mj_hit)))
        print(f"  {task:7} paid={len(pf):2} panel-named={len(ppan):2} "
              f"major-files {len(mj_hit)}/{len(mj)}")
    print(f"address baseline (Σ paid∩panel files): {addr_base} "
          f"(критерий kept >= {addr_base - 2})")
    total_mj = sum(len(mj) for _, mj, _ in all_major)
    print(f"major-files baseline: {total_mj} файла с major записанного "
          f"ревью на задачах, где они есть; критерий kept: все")
    return addr_base, all_major


def main():
    paid_files, major_files, panel_files, norm = load_bases()
    addr_base, all_major = print_baselines(paid_files, major_files,
                                           panel_files)
    if not METRICS.exists():
        print("\n(метрик B2 ещё нет — прогон не выполнен; baselines "
              "зафиксированы ДО адъюдикации)")
        return

    rows = [json.loads(l) for l in METRICS.read_text().splitlines()]
    # retry-invalid добавляет attempt=2: скорим ПОСЛЕДНЮЮ попытку ключа
    last = {}
    for m in rows:
        last[m["key"]] = m
    metrics = [last[k] for k in dict.fromkeys(m["key"] for m in rows)]
    print(f"\n=== результаты B2 ({len(rows)} вызовов, "
          f"{len(metrics)} диффов по последней попытке) ===")
    print(f"{'task':7} {'cand':4} {'kept':4} {'drop':4} {'valid':5} "
          f"{'$':6} {'downgr':6} kept$files")
    tot_kept = tot_cost_valid = n_valid = 0
    tot_downgrades = 0
    off_diff = []
    addr_cov = 0
    ret_major = []
    for m in metrics:
        task = m["key"]
        raw_p = RAW / f"{task}.json"
        v = {}
        if raw_p.exists():
            try:
                v = json.load(open(raw_p)).get("structured_output") or {}
            except ValueError:
                v = {}
        kept = v.get("kept") or []
        files = diff_files(task)
        kfiles = [norm(k["file"]) for k in kept]
        bad = [kf for kf in kfiles
               if not any(kf == f or kf.endswith("/" + f) or f.endswith(kf)
                          for f in files)]
        if bad:
            off_diff.append((task, bad))
        downgraded = [k for k in kept
                      if DOWNGRADE_MARK.search(k.get("rationale") or "")]
        tot_downgrades += len(downgraded)
        mj = major_files.get(task, set())
        mj_hit = [f for f in mj if any(
            f == kf or f.endswith(kf) or kf.endswith(f) for kf in kfiles)]
        if mj:
            ret_major.append((task, sorted(mj), sorted(mj_hit)))
        ppan = paid_files.get(task, set()) & panel_files.get(task, set())
        kset = set(kfiles)
        addr_cov += len([f for f in ppan if any(
            f == kf or f.endswith("/" + kf) or kf.endswith("/" + f)
            or kf.endswith(f) for kf in kset)])
        if m["valid"]:
            tot_kept += len(kept)
            tot_cost_valid += m["cost_usd"] or 0
            n_valid += 1
        print(f"{task:7} {m['n_candidates']:4} {len(kept):4} "
              f"{m['dropped']:4} {str(m['valid'])[:5]:5} "
              f"{(m['cost_usd'] or 0):6.2f} {len(downgraded):6} "
              f"{str(kfiles)[:60]}")

    all_cost = sum(m["cost_usd"] or 0 for m in metrics)
    costs_valid = sorted(m["cost_usd"] for m in metrics if m["valid"])
    print()
    print(f"valid: {n_valid}/14; kept total {tot_kept} "
          f"({tot_kept / max(n_valid, 1):.1f}/valid diff, max "
          f"{max((m['kept'] for m in metrics if m['valid']), default=0)}); "
          f"ceiling <10: {'cleared on every valid diff' if all(m['kept'] < 10 for m in metrics if m['valid']) else 'FAILED'}")
    if costs_valid:
        print(f"cost: all ${all_cost:.2f}; valid-only ${tot_cost_valid:.2f} "
              f"(${tot_cost_valid / n_valid:.2f}/valid mean, "
              f"${costs_valid[len(costs_valid) // 2]:.2f} median)")
    print(f"downgrades (rationale-marked): {tot_downgrades} всего, "
          f"{tot_downgrades / max(n_valid, 1):.1f}/valid diff")
    print(f"address retention: kept names {addr_cov}/{addr_base} "
          f"(гейт >= {addr_base - 2})")
    print("major-file retention (per task where paid had major):")
    for t, mj, hit in ret_major:
        print(f"   {t}: kept {hit} of {mj}")
    print(f"off-diff kept files: {off_diff or 'NONE'}")


if __name__ == "__main__":
    main()
