"""Один замок на все плоские загрузчики петли.

Загрузчики модулей в петле написаны руками и по стандартному рецепту:
модуль кладётся в `sys.modules` ДО выполнения его кода — иначе не
сходятся круговые импорты (agents грузит loop, loop грузит agents). Пока
петля была однопоточной, у рецепта не было цены.

Дуэль (duel.py) запускает два плеча в потоках, и цена появилась сразу:
на ПЕРВОМ настоящем прогоне 2026-08-24 прогон упал на
`module 'obs' has no attribute 'get_logger'`. Это не опечатка и не
пропавшая функция — это второй поток, увидевший в `sys.modules`
наполовину проинициализированный `obs` и решивший, что модуль готов.

Замок обязан быть ОДИН на все семь загрузчиков, а не по одному в каждом.
Модуль-то общий: поток A грузит `obs` через `agents._load`, поток B —
через `codemap._load`, и семь отдельных замков не пересекаются ни в
одной точке. Поэтому замок живёт здесь, в модуле без зависимостей,
который безопасно импортировать откуда угодно.

Реентерабельный (`RLock`), потому что загрузка вложенная: выполняя
`obs.py`, загрузчик может встретить внутри него ещё один `_load`, и
обычный замок встал бы намертво на самом себе.
"""
from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

LOCK = threading.RLock()


def ready(name: str) -> ModuleType | None:
    """Готовый модуль из кэша, либо None.

    Отдельная проверка `_initializing` и есть суть: наличие имени в
    `sys.modules` НЕ означает, что код модуля выполнен. Пока флаг стоит,
    модуль — пустышка, и отдавать его нельзя.
    """
    cached = sys.modules.get(name)
    if cached is None:
        return None
    spec = getattr(cached, "__spec__", None)
    if spec is not None and getattr(spec, "_initializing", False):
        return None
    return cached
