"""Парный стенд: одна задача, два арм-исполнителя, два worktree, одни метрики.

Штатная механика парных армов (A4, WAV-010, решение NXT-028): каждый
A/B-эксперимент раньше собирал такой прогон руками — копировал куски
дуэли и переизобретал сбор метрик. Здесь то, что общего у всех этих
сборок: ОДНА задача прогоняется ДВУМЯ исполнителями ПАРАЛЛЕЛЬНО, каждый
в СВОЁМ git-worktree, а метрики обеих армов падают в общий файл.

Отличие от дуэли (duel.py) принципиально и сознательно:

- Дуэль меряет ОДИН фактор по жребию и ОСТАВЛЯЕТ работу живого плеча:
  это механика обычного прогона с прибором. Стенд здесь — измерение
  само по себе: НИ ОДИН арм не адоптируется, общее дерево и очередь
  не трогаются, статус задачи не меняется. Работа обоих армов после
  замера удаляется вместе с worktree.
- Дуэль гоняет живое плечо в общем дереве; здесь оба плеча в worktree,
  потому что стенд обязан быть неразрушающим: его можно гонять на
  любой задаче очереди, не опасаясь за грязное дерево или чужую работу.

Что переиспользуется из duel.py целиком: worktree()/drop_worktree()
(свежий --detach-чекаут на HEAD), shadow_diff_stat() (объём работы
арма), run_pair() (два потока, календарное время — по медленному).

## Честность замера

- Армы отличаются ТОЛЬКО переданными перекрытиями конфига (--set-a /
  --set-b у команды). Жребия нет и отбора лучшего нет a fortiori:
  работа не берётся никогда, так что некому и переигрываться.
- Метрики обеих армов различимы: каждое сырьё идёт с меткой плеча
  (`-pair-a` / `-pair-b`) в имени файла, а метрика `phase="implement"`
  получает `arm="pair-a"|"pair-b"` (тот же контракт, что у теневого
  плеча дуэли: деньги прибора обязаны быть отличимы от денег работы).
  Сводная строка армов — метрика `phase="pair"`, по ней обе армовые
  строки видны в одном месте.
- Падение одного арма не роняет второй: прибор, ломающийся невидимо,
  превращает замер в односторонний (тот же урок, что у duel.run_pair).

## Чего здесь СОЗНАТЕЛЬНО нет

Повторных раундов и ревью. Стенд меряет исполнителя один раз на арм:
это минимум, который просил A4. Полноценная петля с ревью-циклом
поверх парного стенда — следующий шаг, и эти функции ему не мешают.
"""
from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import time
from typing import Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import duel  # noqa: E402

ARMS = ("a", "b")


def arm_log_tag(arm: str) -> str:
    """Метка плеча для имён файлов сырья: `-pair-a` / `-pair-b`.

    Два потока, пишущие один путь, — молчаливая потеря журнала (тот же
    дефект, что ловят тесты дуэли за теневым плечом).
    """
    if arm not in ARMS:
        raise ValueError(f"неизвестный арм {arm!r}: допустимо {', '.join(ARMS)}")
    return f"-pair-{arm}"


def parse_override(spec: str) -> tuple[tuple[str, ...], Any]:
    """`ключ.подключ=значение` -> ((ключ, подключ), значение).

    Значение читается как JSON, если читается: `true`, `8000`,
    '["python3", "-m", "pytest"]' получают свой настоящий тип, а
    `executor` остаётся строкой. Опечатка вида `--set-a "=x"` или без
    «=» — отказ на входе, а не молчаливое умолчание посреди замера.
    """
    key, sep, raw = spec.partition("=")
    path = tuple(key.split("."))
    if not sep or not path or any(p.strip() != p or not p for p in path):
        raise ValueError(
            f"перекрытие {spec!r} неверно: нужен вид ключ[.подключ]=значение, "
            f"пустые сегменты недопустимы"
        )
    if raw == "":
        raise ValueError(f"перекрытие {spec!r} без значения")
    try:
        value: Any = json.loads(raw)
    except ValueError:
        value = raw
    return path, value


def arm_config(cfg: dict[str, Any], overrides: list[tuple[tuple[str, ...], Any]]
               ) -> dict[str, Any]:
    """Конфиг арм: копия базового + перекрытия. Базовый не мутируется.

    Два арм работают в разных потоках: общий (немутированный) конфиг
    обязан остаться осмысленным после прогона — оператор сравнивает
    замер с обычным прогоном на том же конфиге.
    """
    out = copy.deepcopy(cfg)
    for path, value in overrides:
        node = out
        for part in path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[path[-1]] = value
    return out


def run_arm(state: Any, cfg_arm: dict[str, Any], task: dict[str, Any],
            arm: str) -> dict[str, Any]:
    """Один арм в своём worktree: implement, объём диффа, гейт.

    Гейт — в дереве арма и только при явном `gate_command` (тот же
    контракт, что у теневого плеча дуэли): без настроенного гейта
    факта «гейт пройден» нет, и выдумывать его из умолчания петли
    стенду незачем. Факты уходят в журнал (`pair_arm`) и в общую
    метрику (`phase="pair"`); worktree удаляется всегда.
    """
    tid = str(task["id"])
    wt = duel.worktree(state.root, state.dir, f"{tid}-{arm}")
    try:
        # Импорт ленивый: agents грузит loop, модульный импорт обратно
        # замкнул бы круг (тот же приём, что у loop._shadow_arm).
        import agents as agents_mod  # noqa: PLC0415 — круг импорта, см. выше

        arm_agents = agents_mod.Agents(state, cfg_arm)
        arm_agents.work_root = wt
        arm_agents.log_tag = arm_log_tag(arm)
        t0 = time.time()
        report = arm_agents.implement(task, None, 1)
        wall_s = round(time.time() - t0, 1)
        facts: dict[str, Any] = {
            "arm": arm,
            "report": bool(report),
            "wall_s": wall_s,
            "diff": duel.shadow_diff_stat(wt),
        }
        if arm_agents.last_implement_failure:
            facts["reason"] = arm_agents.last_implement_failure.get("reason")
        # Гейт арма — в ЕГО дереве. Это главная метрика стенда:
        # «прошла ли работы арма проверки проекта».
        if report is not None:
            cmd = cfg_arm.get("gate_command")
            if isinstance(cmd, list) and cmd:
                r = subprocess.run(
                    cmd, cwd=wt, capture_output=True, text=True, check=False,
                    timeout=int(cfg_arm.get("gate_timeout", 900)),
                )
                facts["gate"] = r.returncode == 0
        state.log("pair_arm", task=tid, **facts)
        state.metric(task=tid, phase="pair", **facts)
        return facts
    finally:
        duel.drop_worktree(state.root, wt)


def run_pair_task(state: Any, cfg_a: dict[str, Any], cfg_b: dict[str, Any],
                  task: dict[str, Any],
                  overrides: dict[str, list[tuple[tuple[str, ...], Any]]]
                  | None = None) -> dict[str, dict[str, Any]]:
    """Оба арма ОДНОВРЕМЕННО; вернуть факты по каждому.

    Падение арма — это факт о ЗАМЕРЕ, а не повод ронять второй арм:
    guarded-обёртка ловит исключение внутри потока, журналирует
    (`pair_arm_failed`) и возвращает facts с `error`. Без неё первая
    же авария CLI-движка превращала бы «парный прогон одной командой»
    в ручной разбор двух подпроцессов.
    """
    tid = str(task["id"])
    ov = overrides or {}
    state.log(
        "pair_start", task=tid,
        arms={arm: {".".join(k): v for k, v in ov.get(arm, [])}
              for arm in ARMS},
    )

    def guarded(arm: str, cfg_arm: dict[str, Any]) -> dict[str, Any]:
        try:
            return run_arm(state, cfg_arm, task, arm)
        except Exception as exc:  # noqa: BLE001 — граница деградации прибора
            facts: dict[str, Any] = {
                "arm": arm, "error": f"{type(exc).__name__}: {exc}"}
            state.log("pair_arm_failed", task=tid, **facts)
            return facts

    facts_a, facts_b = duel.run_pair(
        lambda: guarded("a", cfg_a), lambda: guarded("b", cfg_b))
    out = {"a": facts_a, "b": facts_b}
    state.log("pair_done", task=tid,
              a_report=facts_a.get("report"), b_report=facts_b.get("report"),
              a_gate=facts_a.get("gate"), b_gate=facts_b.get("gate"))
    return out


def render(task: dict[str, Any], facts: dict[str, dict[str, Any]]) -> str:
    """Оба арма одной таблицей — то, ради чего стенд и зовут.

    «Плечо не прошло гейт» и «плечо ничего не сделало» — разные болезни,
    поэтому объём диффа печатается всегда (тот же довод, что у
    duel.shadow_diff_stat).
    """
    def line(arm: str, label: str) -> str:
        f = facts.get(arm) or {}
        if f.get("error"):
            return f"  арм {label}: ПРИБОР СЛОМАН — {f['error']}"
        parts = []
        if f.get("reason"):
            parts.append(f"исполнитель: {f['reason']}")
        else:
            parts.append("отчёт есть" if f.get("report") else "отчёта нет")
        gate = f.get("gate")
        parts.append("гейт: ЗЕЛЁНЫЙ" if gate else
                     ("гейт: КРАСНЫЙ" if gate is False else "гейт: не настроен"))
        diff = f.get("diff") or {}
        parts.append(f"дифф: {diff.get('files', 0)} файл(ов), "
                     f"+{diff.get('added', 0)} −{diff.get('removed', 0)}")
        parts.append(f"{f.get('wall_s', 0)} с")
        return f"  арм {label}: " + "; ".join(parts)

    out = [f"=== pair {task['id']}: {task.get('title', '')} ===",
           line("a", "A"), line("b", "B")]
    ga, gb = (facts.get("a") or {}).get("gate"), (facts.get("b") or {}).get("gate")
    out.append("расхождение гейта: "
               + ("есть" if ga is not None and gb is not None and ga != gb
                  else "нет"))
    out.append("общее дерево и статус задачи не тронуты — стенд измеряет, "
               "не работает")
    return "\n".join(out)
