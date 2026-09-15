#!/usr/bin/env python3
"""REV-009 (B3) analysis: retention named, anchoring non-panel, price.

Те же joins, что rev003/analyze-b2.py (main-дерево, scratch):
- адресное множество записанного ревью: goldset/verdicts.jsonl
  (cold = stem *-i1-a1-review; union = все раунды)
- named/non-panel сплит: out2/panel-candidates.jsonl, subset трёх присяжных
- рука L: rev009/raw/<key>.json + metrics-rev009.jsonl

Запуск до прогона: baselines + контроль, $0. После: полный скоринг.
"""
import json
import pathlib
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
REV1 = HERE.parent / "rev001"
GROUND = REV1 / "ground"
GOLDSET = HERE.parent.parent / "goldset"
CANDIDATES = HERE.parent / "out2" / "panel-candidates.jsonl"
METRICS = HERE / "metrics-rev009.jsonl"
RAW = HERE / "raw"
STATS = HERE.parent / "rev002" / "stats.json"
SUBSET = ("glm-5.1", "gpt-oss:120b", "qwen3.5:397b")
TIER1 = ["s2ky-i2", "m6pe-close", "e4kb-close", "e6cx-close", "k3ad-close",
         "e5dq-close", "e7in-close", "e2op-close", "e9tf-close", "z8ck-close"]
TIER2 = ["g2pf-close", "g1nt-close"]


def norm(s):
    return s.lstrip("./")


def match(f, s):
    return any(f == x or f.endswith("/" + x) or x.endswith("/" + f)
               or x.endswith(f) for x in s)


def diff_files(key):
    p = GROUND / f"{key}.raw.diff"
    if not p.exists():
        p = GROUND / f"{key}.diff"
    return {norm(l[6:].strip())
            for l in p.read_text(errors="replace").splitlines()
            if l.startswith("+++ b/")}


def load_bases():
    vs = [json.loads(l) for l in
          GOLDSET.joinpath("verdicts.jsonl").read_text().splitlines()]
    cands = [json.loads(l) for l in CANDIDATES.read_text().splitlines()
             if json.loads(l)["by"].split("/")[0] in SUBSET]
    union = defaultdict(set)
    cold = defaultdict(set)
    for v in vs:
        files = {norm(f["file"]) for f in (v.get("findings") or [])
                 if f.get("file")}
        union[v["task"]] |= files
        if v["stem"].endswith("-i1-a1-review"):
            cold[v["task"]] |= files
    panel = defaultdict(set)
    leads = defaultdict(list)
    for c in cands:
        panel[c["task"]].add(norm(c["file"]))
        leads[c["task"]].append(c)
    return union, cold, panel, leads


def addr_sets(task, union, cold, panel):
    named = {f for f in union[task] if match(f, panel[task])}
    nonp = union[task] - named
    c_named = {f for f in cold[task] if match(f, panel[task])}
    c_nonp = {f for f in cold[task] if not match(f, panel[task])}
    return named, nonp, c_named, c_nonp


def main():
    union, cold, panel, leads = load_bases()
    print("=== baselines (reused control, $0) ===")
    scope = TIER1 + TIER2
    tn = tnp = t1n = t1p = 0
    for key in scope:
        t = key.split("-")[0]
        named, nonp, cn, cn_ = addr_sets(t, union, cold, panel)
        tn += len(named)
        tnp += len(nonp)
        if key in TIER1:
            t1n += len(named)
            t1p += len(nonp)
        print(f"  {key:12} named={len(named):2} nonP={len(nonp):2} "
              f"| cold named={len(cn):2} cold nonP={len(cn_):2}")
    print(f"  scope (12): named={tn} nonP={tnp}; Tier-1: named={t1n} "
          f"nonP={t1p} (гейты: retention>=21/23, non-panel>=3/7)")

    if not METRICS.exists():
        print("\n(метрик B3 ещё нет — прогон не выполнен; baselines "
              "зафиксированы ДО платных вызовов)")
        return

    rows = [json.loads(l) for l in METRICS.read_text().splitlines()]
    done = {r["key"]: r for r in rows}
    keys = [k for k in scope if k in done]
    print(f"\n=== рука L: {len(rows)} вызовов, {len(keys)} диффов ===")
    print(f"{'key':12} {'valid':5} {'$':>6} {'find':>4} {'named':>11} "
          f"{'nonP':>8} {'nonlead-f':>9} off-diff")
    ret_hits = ret_base = np_hits = np_base = 0
    cold_np_base = cold_np_hit = 0
    tot_cost_valid = n_valid = 0
    tot_find = 0
    off_diff_all = []
    verds = defaultdict(int)
    for key in keys:
        m = done[key]
        t = key.split("-")[0]
        v = {}
        rp = RAW / f"{key}.json"
        if rp.exists():
            try:
                v = json.load(open(rp)).get("structured_output") or {}
            except ValueError:
                v = {}
        finds = v.get("findings") or []
        ffiles = {norm(f["file"]) for f in finds if f.get("file")}
        named, nonp, cn, cnonp = addr_sets(t, union, cold, panel)
        n_hit = {f for f in named if match(f, ffiles)}
        p_hit = {f for f in nonp if match(f, ffiles)}
        ret_hits += len(n_hit)
        ret_base += len(named)
        np_hits += len(p_hit)
        np_base += len(nonp)
        cold_np_base += len(cnonp)
        cold_np_hit += len({f for f in cnonp if match(f, ffiles)})
        lead_files = {norm(c["file"]) for c in leads[t]}
        nonlead_findings = [f for f in finds
                            if f.get("file") and not match(norm(f["file"]),
                                                           lead_files)]
        files = diff_files(key)
        bad = [f for f in ffiles
               if not any(match(f, {d}) for d in files)]
        if bad:
            off_diff_all.append((key, sorted(bad)))
        verds[(v or {}).get("verdict")] += 1
        tot_find += len(finds)
        if m["valid"]:
            tot_cost_valid += m["cost_usd"] or 0
            n_valid += 1
        print(f"{key:12} {str(m['valid'])[:5]:5} "
              f"{(m['cost_usd'] or 0):6.2f} {len(finds):4} "
              f"{len(n_hit):2}/{len(named):2}     "
              f"{len(p_hit):2}/{len(nonp):2}   "
              f"{len(nonlead_findings):5}/{len(finds):<3} "
              f"{bad or ''}")

    all_cost = sum(r["cost_usd"] or 0 for r in rows)
    costs_valid = sorted(r["cost_usd"] for r in rows if r["valid"])
    toks = sorted(r["tokens_in"] for r in rows if r.get("tokens_in"))
    leads_chars = sorted(r["leads_chars"] for r in rows)
    print()
    print(f"valid: {n_valid}/{len(rows)}")
    print(f"cost: all ${all_cost:.2f}; valid ${tot_cost_valid:.2f} "
          f"(${tot_cost_valid / max(n_valid, 1):.2f}/valid mean, "
          f"${costs_valid[len(costs_valid) // 2]:.2f} median) при капе $15")
    if STATS.exists():
        stats = json.load(open(STATS))
        pilot = {r["task"]: r for r in stats["pilot"] if r["round"] == "close"}
        same = [pilot[k.split("-")[0]]["recorded_cost"] for k in keys
                if k.split("-")[0] in pilot]
        print(f"recorded review (opus/xhigh, rev002/stats.json) на тех же "
              f"диффах: ${sum(same):.2f} суммарно, "
              f"${sum(same) / len(same):.2f} среднее")
    print(f"retention named: {ret_hits}/{ret_base} "
          f"(гейт >=21/23; cold control named 22/23)")
    print(f"anchoring non-panel: {np_hits}/{np_base} "
          f"(гейт >=3/7 cold-ориентира; cold control nonP {cold_np_hit}/"
          f"{cold_np_base}; union 7/7)")
    print(f"findings: {tot_find} всего, "
          f"{tot_find / max(len(keys), 1):.1f}/дифф; вердикты {dict(verds)}")
    if toks:
        print(f"tokens_in: median {toks[len(toks) // 2]}, max {toks[-1]}; "
              f"leads-секция: {leads_chars[0]}-{leads_chars[-1]} chars")
    print(f"off-diff файлов в findings: {off_diff_all or 'NONE'}")


if __name__ == "__main__":
    main()
