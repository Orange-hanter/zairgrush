"""Тесты сборки промптов: doc-context и маппинг путей."""
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

spec = importlib.util.spec_from_file_location("promptbuilder",
                                               ROOT_DIR / "promptbuilder.py")
promptbuilder = importlib.util.module_from_spec(spec)
sys.modules["promptbuilder"] = promptbuilder
spec.loader.exec_module(promptbuilder)


class FakeAgents:
    """Минимальная заглушка для promptbuilder.docs_block."""

    def __init__(self, config: dict):
        self.config = config
        self.docs_cache = None


def _enabled_for_executor():
    return {"experiments": {"doc_context": "executor"}}


class TestDocsBlockPathMapping(unittest.TestCase):
    """Пути doc-context берутся из задачи, затем из конфига, затем пусто."""

    def _capture(self, task: dict, config: dict):
        """Возвращает (block, args_calls)."""
        calls: list[dict] = []

        def fake_codctx(_config, args):
            calls.append(args)
            return True, '{"docs":[],"links_at_risk":[],"token_estimate":0}'

        agents = FakeAgents(config)
        with (mock.patch.object(promptbuilder.docctx, "codctx", fake_codctx),
              mock.patch.object(promptbuilder.docctx,
                                "enabled_for_doc_context",
                                return_value=True)):
            block = promptbuilder.docs_block(agents, task)
        return block, calls

    def test_task_doc_paths_override_config(self):
        """doc_paths в задаче приоритетнее doc_context_paths из swarm.toml."""
        block, calls = self._capture(
            {"id": "t1", "doc_paths": ["docs/from-task.md"]},
            _enabled_for_executor()
            | {"doc_context_paths": ["docs/from-config.md"]})
        self.assertEqual(calls[0]["paths"], ["docs/from-task.md"])
        self.assertNotIn("from-config", calls[0]["paths"])
        self.assertEqual(block, "")

    def test_config_paths_used_when_task_has_none(self):
        """Если у задачи нет doc_paths, берём doc_context_paths из конфига."""
        block, calls = self._capture(
            {"id": "t2"},
            _enabled_for_executor()
            | {"doc_context_paths": ["docs/from-config.md"]})
        self.assertEqual(calls[0]["paths"], ["docs/from-config.md"])
        self.assertEqual(block, "")

    def test_comma_string_config_is_split(self):
        """doc_context_paths может быть строкой с запятыми (TOML-совместимо)."""
        block, calls = self._capture(
            {"id": "t3"},
            _enabled_for_executor()
            | {"doc_context_paths": "a.md, b.md, c.md"})
        self.assertEqual(calls[0]["paths"], ["a.md", "b.md", "c.md"])
        self.assertEqual(block, "")

    def test_empty_task_doc_paths_falls_back_to_config(self):
        """Пустой список doc_paths в задаче не блокирует fallback на конфиг."""
        block, calls = self._capture(
            {"id": "t4", "doc_paths": []},
            _enabled_for_executor()
            | {"doc_context_paths": ["docs/from-config.md"]})
        self.assertEqual(calls[0]["paths"], ["docs/from-config.md"])
        self.assertEqual(block, "")

    def test_fail_open_when_no_paths_anywhere(self):
        """Нет ни task.doc_paths, ни config.doc_context_paths — блок пустой."""
        calls: list[dict] = []

        def fake_codctx(_config, args):
            calls.append(args)
            return True, '{"docs":[],"links_at_risk":[],"token_estimate":0}'

        agents = FakeAgents(_enabled_for_executor())
        with (mock.patch.object(promptbuilder.docctx, "codctx", fake_codctx),
              mock.patch.object(promptbuilder.docctx,
                                "enabled_for_doc_context",
                                return_value=True)):
            block = promptbuilder.docs_block(agents, {"id": "t5"})
        self.assertEqual(block, "")
        self.assertEqual(calls, [])


class TestDocsBlockBodyRendering(unittest.TestCase):
    """Тело документа из cod-doc попадает в блок."""

    def test_body_is_rendered_from_response(self):
        payload = {
            "docs": [
                {"title": "Architecture", "body": "Use adapters."},
            ],
            "links_at_risk": [],
            "token_estimate": 42,
        }
        agents = FakeAgents(_enabled_for_executor())

        def fake_codctx(_config, args):
            return True, json.dumps(payload)

        with (mock.patch.object(promptbuilder.docctx, "codctx", fake_codctx),
              mock.patch.object(promptbuilder.docctx,
                                "enabled_for_doc_context",
                                return_value=True)):
            block = promptbuilder.docs_block(
                agents, {"id": "t6", "doc_paths": ["arch.md"]})
        self.assertIn("## Documents from cod-doc", block)
        self.assertIn("### Architecture", block)
        self.assertIn("Use adapters.", block)
        self.assertIn("[token_estimate: 42]", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
