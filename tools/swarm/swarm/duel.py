"""Дуэль: два исполнителя на ОДНОЙ задаче одновременно, парно.

Это второй способ мерить фактор, и он сильнее фонового жребия.

Фоновый замер (ambient.py) даёт по задаче ОДНО наблюдение, и плечи
набираются из разных задач. Выборка непарная: разброс между задачами не
вычитается, и на малом n он топит эффект — именно это и случилось с
плечом исполнителя E9 дважды. Парный стенд эту беду снимает, но стоит
календарного времени (задача прогоняется дважды подряд) и требует, чтобы
задачу под замер ВЫБРАЛ человек, — а выбирает он под ожидаемый признак.

Дуэль берёт лучшее от обоих. Задача одна, коммит один, спека одна, оба
плеча идут ОДНОВРЕМЕННО — значит наблюдение парное, календарное время
равно медленному плечу, а не сумме, и задачу никто не выбирал: дуэль
происходит на том, что пришло в очередь.

## Что здесь считается честностью

Плечо, работа которого ОСТАЁТСЯ, выбирается жребием ЗАРАНЕЕ и не
меняется потом, как бы плечи ни выглядели. Это не формальность. Если
оставлять то плечо, которое лучше прошло гейт, то замеряется max(A, B), а
не A и не B: доля «плеча B» перестаёт быть свойством B и становится
свойством отбора. Код получился бы лучше, а данные — бесполезны.

Отсюда устройство:

- ЖИВОЕ плечо работает в обычном дереве репозитория, ровно как сегодня.
  Ни одна строка после `implement` не знает, что была дуэль, — а значит
  с выключенным флагом поведение байт-в-байт прежнее.
- ТЕНЕВОЕ плечо работает в отдельном git-worktree и существует только
  чтобы быть измеренным. Его работа не адоптируется; воркtree удаляется.
- Гейт гоняется у обоих: «прошла ли работа плеча гейт» и есть главная
  метрика. У теневого — в его дереве.

## Чего здесь СОЗНАТЕЛЬНО нет

Спасения живого плеча теневым. Соблазн очевиден: живое упало, теневое
прошло — возьми теневое. Но это ровно тот отбор, против которого писан
абзац выше, и вводить его можно только с явным правилом исключения
(задача выпадает из основного сравнения и считается отдельно как
«спасение»). Расширение названо в 06-доке, а не сделано молча.

## Цена

Два вызова исполнителя на задачу вместо одного, плюс второй гейт. Это
решение владельца от 2026-08-24 («не про экономию»): режим денежного
чана снял потолки вызова именно затем. Календарное время растёт на один
гейт, а не вдвое, потому что исполнители идут параллельно.

Предупреждение о железе, потому что оно замеряемо, а не гипотетично: два
исполнителя на Rust-проекте гоняют `cargo` каждый в своей сессии. На
8 ГБ это может уйти в свап. Дуэль поэтому включается флагом на стенд, а
не глобально.
"""
from __future__ import annotations

import concurrent.futures
import pathlib
import shutil
import subprocess
import sys
from typing import Any

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import ambient  # noqa: E402

# Каталог теневых деревьев внутри `.swarm`: он уже игнорируется git
# (SwarmState._self_ignore), поэтому worktree там не попадает в дифф и не
# путает стража границ.
ARMS_DIR = "arms"


def factor(config: dict[str, Any]) -> str | None:
    """Фактор дуэли или None. Список тот же, что у фонового замера."""
    exp = config.get("experiments") or {}
    name = exp.get("duel")
    if name is None:
        return None
    if name not in ambient.FACTORS:
        msg = (f"[experiments] duel = {name!r} — такого фактора нет. "
               f"Допустимо: {', '.join(sorted(ambient.FACTORS))}")
        raise ValueError(msg)
    return str(name)


def conflict(config: dict[str, Any]) -> str | None:
    """Дуэль вместе с явным флагом того же фактора — отказ.

    Тот же довод, что в ambient.conflict: журнал писал бы «плечо выбрано
    жребием» о прогоне, где выбор был прибит гвоздём. Плюс запрет на
    дуэль и фоновый жребий по ОДНОМУ фактору одновременно — это было бы
    два разных замера с общей записью.
    """
    name = factor(config)
    if name is None:
        return None
    exp = config.get("experiments") or {}
    key, _on, _off = ambient.FACTORS[name]
    if key in exp:
        return (f"[experiments] {key} задан явно ({exp[key]!r}) и он же "
                f"разыгрывается дуэлью duel = {name!r}: убери одно из двух")
    if exp.get("ambient") == name:
        return (f"фактор {name!r} одновременно в дуэли и в фоновом жребии — "
                f"это два разных замера с общей записью, выбери один")
    return None


def plan(config: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Кто живой, кто теневой и с какими флагами. None — дуэли нет.

    Жребий берётся у ambient: одна и та же функция и один и тот же сид,
    поэтому плечо задачи пересчитывается из `(сид, фактор, id)` и здесь.
    """
    name = factor(config)
    if name is None:
        return None
    key, on_value, off_value = ambient.FACTORS[name]
    base = dict(config)
    base["experiments"] = dict(config.get("experiments") or {})
    live_arm = ambient.arm({**config,
                            "experiments": {**(config.get("experiments") or {}),
                                            "ambient": name}}, task_id)
    shadow_arm = "off" if live_arm == "on" else "on"

    def overlay(arm_name: str) -> dict[str, Any]:
        out = dict(base)
        out["experiments"] = dict(base["experiments"])
        out["experiments"][key] = on_value if arm_name == "on" else off_value
        # Внутри плеча ни дуэли, ни фонового жребия быть не должно:
        # иначе плечо разыграло бы фактор ещё раз, само от себя.
        out["experiments"].pop("duel", None)
        out["experiments"].pop("ambient", None)
        return out

    return {"factor": name, "key": key,
            "live": {"arm": live_arm, "config": overlay(live_arm)},
            "shadow": {"arm": shadow_arm, "config": overlay(shadow_arm)}}


def worktree(root: pathlib.Path, swarm_dir: pathlib.Path,
             task_id: str) -> pathlib.Path:
    """Свежий git-worktree на текущем HEAD для теневого плеча.

    Пересоздаётся, а не переиспользуется: остатки прошлой задачи в дереве
    теневого плеча — это чужая работа, попавшая в замер, и заметить её
    было бы нечем.
    """
    path = swarm_dir / ARMS_DIR / task_id
    if path.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                       cwd=root, check=False, capture_output=True)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "worktree", "add", "--detach", str(path), head],
                   cwd=root, check=True, capture_output=True)
    return path


def drop_worktree(root: pathlib.Path, path: pathlib.Path) -> None:
    """Убрать теневое дерево. Отказ не поднимается: замер уже снят."""
    subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                   cwd=root, check=False, capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def shadow_diff_stat(path: pathlib.Path) -> dict[str, int]:
    """Сколько теневое плечо написало: файлы и строки.

    Без этого «плечо не прошло гейт» неотличимо от «плечо ничего не
    сделало», а это разные болезни.
    """
    subprocess.run(["git", "add", "-A"], cwd=path, check=False,
                   capture_output=True)
    out = subprocess.run(["git", "diff", "--cached", "--numstat"], cwd=path,
                         check=False, capture_output=True, text=True).stdout
    files = added = removed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        for i, acc in ((0, "added"), (1, "removed")):
            if parts[i].isdigit():
                if acc == "added":
                    added += int(parts[i])
                else:
                    removed += int(parts[i])
    return {"files": files, "added": added, "removed": removed}


def run_pair(live_call: Any, shadow_call: Any) -> tuple[Any, Any]:
    """Два исполнителя ОДНОВРЕМЕННО: календарное время — по медленному.

    Потоки, не процессы: обе ветки только ждут subprocess, GIL здесь не
    мешает. Исключение теневого плеча НЕ поднимается наружу — замер не
    имеет права ронять работу, которая и так была бы сделана.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        live_future = pool.submit(live_call)
        shadow_future = pool.submit(shadow_call)
        live = live_future.result()
        try:
            shadow = shadow_future.result()
        except Exception:  # noqa: BLE001 — граница деградации, см. ниже
            # Теневое плечо — измерительный прибор. Его поломка обязана
            # оставить след и не тронуть прогон: иначе замер начинает
            # стоить задач, а не только денег.
            shadow = None
    return live, shadow
