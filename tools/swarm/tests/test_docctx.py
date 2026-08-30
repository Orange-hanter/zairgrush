"""Тесты fail-open интеграции с cod-doc (RFC 22 §3.4, E5-C)."""
import importlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "swarm"
spec = importlib.util.spec_from_file_location("docctx", ROOT_DIR / "docctx.py")
docctx = importlib.util.module_from_spec(spec)
sys.modules["docctx"] = docctx
spec.loader.exec_module(docctx)


class TestCodctxFailOpen(unittest.TestCase):
    """cod-doc недоступен по-разному — петля продолжается с пустым блоком."""

    def _fake_bin(self, path):
        return str(pathlib.Path(self.tmp.name) / path)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"cod_doc_bin": self._fake_bin("cod-doc")}
        self.args = {"project": "zairgrush", "paths": ["src/a.py"],
                     "budget_tokens": 500}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_binary_returns_failure(self):
        """Бинарь отсутствует — ровно одно предупреждение, пустой блок."""
        ok, out = docctx.codctx(self.config, self.args, timeout=1)
        self.assertFalse(ok)
        self.assertIn("not found", out)

    def test_timeout_returns_failure(self):
        """cod-doc завис — таймаут, петля не ждёт вечно."""
        bin_path = pathlib.Path(self.config["cod_doc_bin"])
        # Бесконечный процесс, которому subprocess сможет послать сигнал.
        bin_path.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
        bin_path.chmod(0o755)
        ok, out = docctx.codctx(self.config, self.args, timeout=0.1)
        self.assertFalse(ok)
        self.assertIn("TimeoutExpired", out)

    def test_non_zero_exit_returns_failure(self):
        """cod-doc вернул ошибку — берём stderr как диагностику."""
        bin_path = pathlib.Path(self.config["cod_doc_bin"])
        bin_path.write_text("#!/bin/sh\necho 'no such project' >&2\nexit 1\n",
                            encoding="utf-8")
        bin_path.chmod(0o755)
        ok, out = docctx.codctx(self.config, self.args, timeout=1)
        self.assertFalse(ok)
        self.assertIn("no such project", out)

    def test_garbage_json_returns_failure(self):
        """cod-doc вернул мусор вместо JSON — блок пустой."""
        bin_path = pathlib.Path(self.config["cod_doc_bin"])
        bin_path.write_text("#!/bin/sh\necho 'not json'\n",
                            encoding="utf-8")
        bin_path.chmod(0o755)
        ok, out = docctx.codctx(self.config, self.args, timeout=1)
        self.assertFalse(ok)
        self.assertIn("invalid json", out)

    def test_valid_response_returns_success(self):
        """Корректный JSON проходит проверку структуры."""
        payload = {"docs": [{"title": "x", "body": "y"}],
                   "links_at_risk": ["a -> b"], "token_estimate": 123}
        bin_path = pathlib.Path(self.config["cod_doc_bin"])
        bin_path.write_text(f"#!/bin/sh\necho '{json.dumps(payload)}'\n",
                            encoding="utf-8")
        bin_path.chmod(0o755)
        ok, out = docctx.codctx(self.config, self.args, timeout=1)
        self.assertTrue(ok)
        self.assertEqual(json.loads(out), payload)
