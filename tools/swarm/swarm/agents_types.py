"""Структурный интерфейс `Agents` для вынесенных контуров.

Плоские модули (promptbuilder, executor, reviewer) не импортируют
agents — был бы цикл, — поэтому нужные поля и методы объявлены здесь
структурно. Тот же приём, что LoopLike в loop_types.py для
gitops/reviewcycle.
"""
import random
from types import ModuleType
from typing import Any, Protocol


class AgentsLike(Protocol):
    state: Any
    config: dict[str, Any]
    driver: ModuleType
    loop_mod: ModuleType
    helpers: ModuleType | None
    codemap: ModuleType | None
    map_cache: tuple[tuple[str, int], str] | None
    memory_cache: tuple[str, str] | None
    norms_cache: tuple[str, str] | None
    docs_cache: tuple[str, str] | None
    rng: random.Random
    last_tuning: dict[str, Any]
    last_implement_failure: dict[str, Any] | None
    last_review_failure: str | None

    def work_diff(self) -> str: ...
