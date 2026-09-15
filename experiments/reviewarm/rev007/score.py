#!/usr/bin/env python3
"""REV-007 (B4, WAV-001): скоринг round-ground адъюдикации против goldset.

Правило соответствия — консервативный quote-anchor REV-001 (prereg B4):
(a) тот же файл-якорь, (b) ключевые идентификаторы лейбла в тексте находки,
(c) тот же класс дефекта; пограничное = не восстановлено. Механический
пред-отбор (якорь + идентификаторы) + финальные ручные суждения в FINAL
с обоснованием на случай (продублированы в report-rev007.md).

Запуск:
    python3 rev007/score.py            # таблица + rev007/score.json
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REVIEWARM = HERE.parent
GOLDSET = REVIEWARM.parent / "goldset"
GROUND = REVIEWARM / "rev001" / "ground"
PANEL = HERE / "panel" / "panel-candidates.jsonl"
RAW_B4 = REVIEWARM / "rev003" / "raw-b4"
OUT = HERE / "score.json"
SUBSET = ("glm-5.1", "gpt-oss:120b", "qwen3.5:397b")

# Финальные суждения по 6 лейблам (механический пред-отбор 2026-09-15 +
# ручная сверка текстов; rationale — в report-rev007.md «Скоринг»).
FINAL = {
    "q002": {"ground": "e9tf-i1", "severity": "major", "panel": None,
             "adjudicated": None,
             "status": "UNMEASURABLE",
             "note": "дефект (побайтовая копия natural_key/digit_chunk/"
                     "NaturalChunk в erc02.rs) ОТСУТСТВУЕТ в восстановленном "
                     "e9tf-i1.raw.diff: дифф содержит уже исправленное "
                     "состояние (use zeus_model::natural_key). Копию писал "
                     "потерянный прогон исполнителя до уцелевшего стрима "
                     "i1 (08:10 request_changes/q002 -> рестарт; первый Read "
                     "erc02.rs в стриме копии не несёт). Земля q002 не "
                     "реконструирована; маппинг REV-001 «q002 -> e9tf-i1» "
                     "опровергнут инспекцией диффа."},
    "q004": {"ground": "e7in-i1", "severity": "batch(1 major + 3 minor)",
             "panel": True, "adjudicated": True,
             "status": "REFOUND",
             "note": "ядро батча (major: атрибуция мутаций по ЧИСЛУ "
                     "диагностик) — панель: qwen3.5/tests «verifies the "
                     "delta of diagnostic counts but does not assert that "
                     "the specific diagnostic removed corresponds to the "
                     "mutated terminal's identity»; адъюдикатор: KEPT major "
                     "«сравнивает только числа диагностик, поэтому не ловит "
                     "подмену выбора клемм». Якорь corpus_erc.rs, класс "
                     "совпадает. Под-находки 2-4 (perf build_nets, "
                     "self-loop TerminalRef, O(n*m) multiset_diff) НЕ "
                     "восстановлены: self_loop-кандидаты — дубли-ниты "
                     "(другой класс), perf и O(n*m) никем не подняты."},
    "q013": {"ground": "s2ky-i1", "severity": "minor", "panel": False,
             "adjudicated": False, "status": "MISSED",
             "note": "CURRENT_KEY_VERSION как параметр (terminal_key_v): "
                     "ни один кандидат панели не касается keys.rs по "
                     "существу версионирования ключа."},
    "q015": {"ground": "s2ky-i1", "severity": "minor", "panel": False,
             "adjudicated": False, "status": "MISSED",
             "note": "check_terminal_name_chars: не упомянут ни одним "
                     "присяжным (ни subset, ни полная панель)."},
    "q017": {"ground": "s2ky-i1", "severity": "major", "panel": False,
             "adjudicated": False, "status": "MISSED",
             "note": "экранирование имён клемм в канонической строке вне "
                     "спеки. Ближайшее — kimi/safety по keys.rs («нет "
                     "валидации длины terminal-строк перед хэшированием»): "
                     "тот же файл, другой класс (input-validation, не "
                     "расхождение код/спека). Консервативно — не "
                     "восстановлено."},
    "q018": {"ground": "s2ky-i1", "severity": "minor", "panel": False,
             "adjudicated": False, "status": "MISSED",
             "note": "прочитанный key_version ни на что не влияет. "
                     "Ближайшее — kimi/safety major по load.rs («нет "
                     "downgrade/migration для старых проектов»): класс "
                     "другой (политика миграции, не мёртвая диспетчеризация "
                     "по версии). Не восстановлено."},
}


def norm_file(f):
    return (f or "").strip().lstrip("./")


def main():
    panel = [json.loads(l) for l in PANEL.read_text().splitlines()]
    subset = [r for r in panel if r["by"].split("/")[0] in SUBSET]
    kept_by_key = {}
    for p in sorted(RAW_B4.glob("*.json")):
        try:
            env = json.loads(p.read_text())
        except ValueError:
            continue
        v = env.get("structured_output")
        if isinstance(v, dict):
            kept_by_key[p.stem] = v.get("kept") or []

    # механический стандарт Step 0: ни одного off-diff файла в kept
    off_diff = []
    for key, kept in kept_by_key.items():
        files = {norm_file(f) for f in
                 __import__("re").findall(r"^\+\+\+ b/(.+)$",
                     (GROUND / f"{key}.raw.diff").read_text(),
                     __import__("re").MULTILINE)}
        for k in kept:
            if norm_file(k.get("file")) not in files:
                off_diff.append((key, k.get("file")))

    measurable = [q for q, r in FINAL.items() if r["status"] != "UNMEASURABLE"]
    panel_hits = sum(1 for q in measurable if FINAL[q]["panel"])
    adj_hits = sum(1 for q in measurable if FINAL[q]["adjudicated"])
    majors_meas = [q for q in measurable
                   if "major" in FINAL[q]["severity"]]
    majors_hit = [q for q in majors_meas if FINAL[q]["adjudicated"]]

    print(f"{'label':6} {'sev':22} {'ground':9} {'panel':6} {'adjud':6} статус")
    for q, r in FINAL.items():
        fmt = lambda v: "n/a" if v is None else ("да" if v else "НЕТ")
        print(f"{q:6} {r['severity']:22} {r['ground']:9} "
              f"{fmt(r['panel']):6} {fmt(r['adjudicated']):6} {r['status']}")
    print()
    print(f"recall панель (subset):  {panel_hits}/{len(measurable)} "
          f"измеримых (пререг-знаменатель 6: {panel_hits}/6)")
    print(f"recall адъюдикатор:      {adj_hits}/{len(measurable)} "
          f"измеримых (пререг 6: {adj_hits}/6)")
    print(f"majors адъюдикатор:      {len(majors_hit)}/{len(majors_meas)} "
          f"измеримых (пререг 3: {len(majors_hit)}/3)")
    print(f"off-diff файлов в kept:  {len(off_diff)} {off_diff or ''}")

    OUT.write_text(json.dumps({
        "exp": "REV-007/B4/WAV-001", "date": "2026-09-15",
        "matching_rule": "quote-anchor REV-001 (консервативно)",
        "labels": FINAL,
        "recall": {"panel_measurable": [panel_hits, len(measurable)],
                   "adjudicated_measurable": [adj_hits, len(measurable)],
                   "adjudicated_prereg_denominator": [adj_hits, 6],
                   "majors_measurable": [len(majors_hit), len(majors_meas)],
                   "majors_prereg_denominator": [len(majors_hit), 3]},
        "off_diff_kept": off_diff,
        "kept_by_key": {k: len(v) for k, v in kept_by_key.items()},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
