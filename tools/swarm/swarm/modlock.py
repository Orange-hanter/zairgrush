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

import importlib.util
import pathlib
import sys
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

LOCK = threading.RLock()

# Каталог плоских модулей петли — общий у всех загрузчиков-обёрток.
HERE = pathlib.Path(__file__).resolve().parent


def _mark_initializing(spec: Any, flag: bool) -> None:
    # Приватный флаг штатного загрузчика CPython (importlib._bootstrap
    # выставляет его ровно так же): воспроизводим поведение загрузчика,
    # а не лезем в чужие данные — SLF осознан, Any, потому что в typeshed
    # атрибут не объявлен.
    spec._initializing = flag  # noqa: SLF001


def load_module(name: str) -> ModuleType:
    """Загрузить плоский модуль петли по имени файла.

    Общая реализация для executor.py и tester.py: раньше функция была
    скопирована между ними слово-в-слово, и правка политики загрузки
    легла бы на обе копии. Публикация в sys.modules ДО выполнения — тот
    же рецепт против гонки дуэльных потоков, см. докстринг модуля.
    """
    with LOCK:
        cached = ready(name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"не удалось загрузить модуль {name}")
        mod = importlib.util.module_from_spec(spec)
        # Порядок ниже — само назначение модуля: флаг «не готов»
        # выставляется ДО публикации в sys.modules. Между этими двумя
        # точками окно для соседнего потока: увидит имя в sys.modules,
        # прочитает __spec__ (он уже выставлен module_from_spec), не
        # найдёт флага — и получит полу-пустышку. Так повторяется
        # механика importlib._bootstrap: _initializing взводится внутри
        # менеджера блокировки до исполнения и до публикации. Снятие —
        # в finally: авария exec не должна оставить модуль «вечно
        # загружающимся» (та самая авария 2026-08-24, ради которой
        # модуль и заведён).
        _mark_initializing(spec, True)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            # Забота importlib._bootstrap (см. ниже): авария exec НЕ
            # оставляет недогруженный модуль в sys.modules — иначе
            # следующий load_module() через ready() отдал бы его за
            # готовый (тот самый «module 'X' has no attribute 'Y'»
            # 2026-08-24, против которого модуль и заведён). Удаляем
            # только то, что опубликовали сами: имя могло стоять тут и
            # до нас.
            if sys.modules.get(name) is mod:
                del sys.modules[name]
            raise
        finally:
            _mark_initializing(spec, False)
        return mod


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
