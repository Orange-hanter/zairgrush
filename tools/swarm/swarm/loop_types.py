"""Протокол интерфейса Loop для плоских модулей-помощников.

Gitops и reviewcycle не должны импортировать `loop.Loop` напрямую:
это замыкает цикл импортов и mypy перестаёт различать возвращаемые
типы. Протокол описывает только ту часть Loop, которую используют
помощники; сам Loop удовлетворяет ему структурно.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable


class LoopLike(Protocol):
    state: Any
    config: dict[str, Any]
    agents: Any
    ui: Callable[..., None]

    _pre_existing: set[str]
    _run_dirt: set[str]
    _head_before: str | None
    _state_before: str | None

    def _sh(self, cmd: list[str],
            timeout: float = 900) -> subprocess.CompletedProcess[str]:
        ...

    def _state_fingerprint(self) -> str | None:
        ...

    def _declared_state_sha(self) -> str | None:
        ...

    def _state_sha(self, blob: str) -> str:
        ...

    def cleanup(self, task: dict[str, Any], reason: str) -> str | None:
        ...
