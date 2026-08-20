"""Цикл ревью: ожидание квоты и диагностика несходимости.

Методы класса Loop, связанные с вызовом ревьюера и пост-анализом
раундов, вынесены в свободные функции. В `loop.py` остаются тонкие
делегаты — совместимость с тестами сохранена.
"""
from __future__ import annotations

import pathlib
import sys
import time
from typing import TYPE_CHECKING, Any

# Каталог модуля — в путь поиска: рой не устанавливается пакетом (см. obs.py).
_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from verdicts import (  # noqa: E402
    ESCALATE_MAX,
    quota_exception,
)

if TYPE_CHECKING:
    from loop_types import LoopLike


def _review_with_quota_wait(loop: LoopLike, task: dict[str, Any], tail: str,
                            iteration: int, confirming: bool,
                            ) -> dict[str, Any] | None:
    """§5.3: отказ по квоте — пауза с бэкоффом, а не авария.

    Квота — заведомо временное и заведомо повторяемое состояние:
    блокировать за него задачу значит наказывать её за погоду у
    провайдера. Ровно это и происходило: QuotaExceededError долетал до
    общего `except Exception` в run(), задача уходила в blocked с
    диагнозом «авария», очередь останавливалась, а ветка «пауза по
    квоте» в CLI была недостижима.

    Теперь ждём по нарастающей (1→2→4 мин по умолчанию, конфиг
    `quota_backoff_s`) и повторяем. Не отпустило — работа в stash,
    задача возвращается в очередь как есть (она ни в чём не виновата),
    исключение уходит наверх: прогон ставится на паузу целиком, и
    «когда продолжить» решает человек.
    """
    delays = list(loop.config.get("quota_backoff_s", (60, 120, 240)))
    while True:
        try:
            verdict: dict[str, Any] | None = loop.agents.review(
                task, tail, iteration, confirming=confirming)
        except Exception as e:
                # По имени, не по классу: у Agents своя копия модуля loop,
                # и её QuotaExceededError — другой объект (см. quota_exception).
            if not quota_exception(e):
                raise
            if not delays:
                stash = loop.cleanup(task, "quota-pause")
                loop.state.log("quota_pause", task=task["id"],
                               round=iteration, stash=stash,
                               message=str(e)[:200])
                loop.state.set_status(task["id"], "pending", stash=stash)
                loop.ui(f"    ПАУЗА ПО КВОТЕ: {str(e)[:120]}")
                raise
            delay = delays.pop(0)
            loop.state.log("quota_wait", task=task["id"], round=iteration,
                           wait_s=delay, message=str(e)[:200])
            loop.state.metric(task=task["id"], iter=iteration,
                              phase="review", quota_wait_s=delay)
            loop.ui(f"    квота провайдера: ждём {delay} с")
            time.sleep(delay)
        else:
            return verdict

def _reviewers_disagreed(history: list[dict[str, Any]]
                         ) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Последний раунд был подтверждающим и сменил вердикт?

    Подтверждающий раунд ревьюет ТОТ ЖЕ дифф: исполнитель в нём не
    вызывается, код между раундами не менялся. Значит, смена вердикта
    — свойство ревьюеров, а не работы. Отличить это от несходимости
    задачи можно только здесь: дальше по тексту диагноза информации
    уже нет.
    """
    if len(history) < 2 or not history[-1].get("confirming"):
        return None
    prev, last = history[-2], history[-1]
    if prev.get("verdict") == last.get("verdict"):
        return None
    return prev, last

def _arm(row: dict[str, Any]) -> str:
    """Рука замера словами: без неё «разошлись» нечем проверить."""
    model = row.get("model") or "сессионная модель"
    effort = row.get("effort")
    return f"{model}/{effort}" if effort else str(model)

def _diagnose(outcome: str, history: list[dict[str, Any]],
              scope_failures: list[list[str]] | None = None,
              sig_failures: list[list[str]] | None = None,
              exec_failures: list[dict[str, Any]] | None = None) -> str:
    """Несходимость требует ДИАГНОЗА, а не очередного повтора.

    Закрытый список гипотез (FuguNano): человеку эскалируется не голый
    факт «три раунда подряд», а версия о причине.

    Диагноз обязан называть только то, что диагност МОЖЕТ знать. Пока
    любое исчерпание раундов объявлялось «задача слишком крупная»,
    петля посылала человека расщеплять задачу, которую сама же
    одобрила раундом раньше (пилот, e7in), и заодно прятала главное
    наблюдение прогона — расхождение двух рук на одном диффе. Это
    третий случай того же класса после проверки целостности и стража
    путей: предохранитель, называющий причину, которой не знает.
    """
    if outcome == ESCALATE_MAX:
            # Сначала — то, что диагност ЗНАЕТ наверняка (§5.7.2). Сигнатуры
            # проверяются РАНЬШЕ границ: нарушение замороженного контракта —
            # факт более точный, чем общий выход за paths, и называть общую
            # причину, когда известна точная, значит соврать умолчанием.
        if sig_failures and len(sig_failures) >= 2:
            changed = sorted({s for row in sig_failures for s in row})
            shown = ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
            return (f"{len(sig_failures)} раунд(ов) сгорели на нарушении "
                    f"замороженных сигнатур контракта: {shown}. Это не "
                    f"вопрос размера задачи — исполнитель меняет то, что "
                    f"договором запрещено менять. Если контракт скелета "
                    f"невыполним, нужен пересмотр сигнатур в задаче-"
                    f"скелете или dispute, а не новая попытка")
            # Раунды, сгоревшие на границах, — факт из журнала, а не гипотеза:
            # на пилоте k3ad и s2ky получили «задача слишком крупная —
            # расщепить», когда обе бились об один защищённый файл.
            # Расщепление там не помогло бы: любой осколок упёрся бы туда же.
        if scope_failures and len(scope_failures) >= 2:
            files = sorted({f for row in scope_failures for f in row})
            shown = ", ".join(files[:5]) + ("…" if len(files) > 5 else "")
            return (f"{len(scope_failures)} раунд(ов) сгорели на "
                    f"нарушении границ — исполнитель каждый раз правил: "
                    f"{shown}. Расщепление не поможет: любой осколок "
                    f"упрётся туда же. Добавьте файл в paths задачи явно "
                    f"или пересмотрите protected_paths")
        # Аварии исполнителя — среда, а не работа: процесс умер, был
        # убит по таймауту или не вернул отчёт. На пилоте так сгорели
        # раунды k3ad (квота kimi) и s2ky (таймаут 30 минут), и обе
        # задачи получили обвинение в размере.
        if exec_failures and len(exec_failures) >= 2:
            reasons = sorted({str(f.get("reason") or "?")
                              for f in exec_failures})
            return (f"{len(exec_failures)} раунд(ов) сорвались на стороне "
                    f"исполнителя ({', '.join(reasons)}) — работа не "
                    f"дошла до ревью ни разу. Это среда, а не задача: "
                    f"смотрите квоту провайдера, silence_timeout и "
                    f"wall_clock_cap, а не размер задачи")
        flip = _reviewers_disagreed(history)
        if flip:
            prev, last = flip
            return (
                f"ревьюеры разошлись на ОДНОМ И ТОМ ЖЕ диффе: раунд "
                f"{prev['round']} ({_arm(prev)}) — {prev['verdict']}, "
                f"находок {prev['findings']}; подтверждающий раунд "
                f"{last['round']} ({_arm(last)}) — {last['verdict']}, "
                f"находок {last['findings']}. Исполнитель между раундами "
                f"не вызывался, код не менялся. Расщеплять задачу не "
                f"нужно — прочтите оба вердикта в .swarm/log и решите, "
                f"чья правда")
        # Механической причины нет. Раньше здесь стояло «задача,
        # вероятно, слишком крупная — расщепить»: на золотом наборе
        # PILOT-1 этот диагноз был неверен 6 раз из 6, и каждый раз
        # посылал человека расщеплять задачу, у которой была совсем
        # другая беда. Догадку заменяет ТРАЕКТОРИЯ — то, что диагност
        # действительно знает, — и размер называется гипотезой только
        # когда на него указывают сами цифры: широкий фронт находок,
        # который не сужается.
        if not history:
            return ("раунды исчерпаны, но ни один не дошёл до вердикта: "
                    "механической причины в журнале нет — смотрите "
                    ".swarm/log за этой задачей")
        trail = "; ".join(
            f"раунд {h.get('round')} ({_arm(h)}) — "
            f"{h.get('verdict')}, находок {h.get('findings')}"
            for h in history[-4:])
        seen_cats = {c for h in history for c in (h.get("categories") or [])}
        round_counts = [int(h.get("findings") or 0) for h in history]
        wide = len(seen_cats) >= 3 and round_counts and min(round_counts) >= 3
        hypothesis = (
            "фронт замечаний широкий и не сужается "
            f"({len(seen_cats)} категорий, минимум {min(round_counts)} "
            f"находок за раунд) — вот здесь расщепление действительно "
            f"правдоподобно"
            if wide else
            "механической причины петля не нашла: ни границ, ни "
            "сигнатур, ни аварий исполнителя, ни расхождения ревьюеров. "
            "Размер задачи — НЕ вывод из этих данных, читайте вердикты")
        return f"раунды исчерпаны. Траектория: {trail}. {hypothesis}"
    counts = [h["findings"] for h in history]
    cats = [tuple(h["categories"]) for h in history]
    if len(set(cats)) == 1 and len(cats) > 1:
        return ("замечания одного класса повторяются: либо требование "
                "сформулировано неясно, либо ревьюер строже спецификации")
    if counts and counts == sorted(counts, reverse=True):
        return "находки убывают, но не до нуля: не хватило раундов"
    return ("число находок не убывает — вероятны качели fix→break; "
            "нужна другая реализация, а не правки поверх")
