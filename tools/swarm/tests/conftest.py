"""Общая обвязка набора тестов.

Реестр стендов (`swarm/registry.py`) пишется любым вызовом CLI в
стенде, а почти каждый тест CLI — такой вызов. Без подмены набор
засорял бы `~/.local/state/swarm/roots.json` оператора сотнями
временных каталогов. Подмена — на весь сеанс и до импорта тестов.
"""
import os
import pathlib
import tempfile

_REGISTRY_DIR = tempfile.mkdtemp(prefix="swarm-registry-")
os.environ["SWARM_REGISTRY"] = str(pathlib.Path(_REGISTRY_DIR) / "roots.json")
