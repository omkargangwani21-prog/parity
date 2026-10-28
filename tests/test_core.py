"""Pure local checks for input validation, comparisons, and spend protection."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PARITY_SKIP_DOTENV", "1")
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import assistant
import parity
import server
from stress import parse_inputs


class ChildEnvironmentTests(unittest.TestCase):
    def test_child_env_has_only_allowed_variables(self):
        source = {
            "PATH": "/usr/bin", "LANG": "C.UTF-8", "JAVA_HOME": "/jdk",
            "COB_CONFIG_DIR": "/etc/gnucobol", "NEBIUS_API_KEY": "sentinel",
            "NVIDIA_API_KEY": "sentinel", "OPENAI_API_KEY": "sentinel", "CUSTOM": "x",
        }
        with patch.dict(os.environ, source, clear=True), tempfile.TemporaryDirectory() as td:
            env = parity.child_env(td)
        self.assertEqual(set(env), {"PATH", "LANG", "JAVA_HOME", "COB_CONFIG_DIR", "HOME"})
        self.assertEqual(env["HOME"], str(Path(td).resolve()))

    def test_child_output_is_capped(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = parity.run(
                [sys.executable, "-c", "print('x' * (2 * 1024 * 1024))"],
                timeout=10, tmpdir=td)
        self.assertEqual(rc, 125)
        self.assertLessEqual(len(out.encode()), parity._CHILD_OUTPUT_LIMIT)
        self.assertIn("exceeded the 1 MB output limit", err)


class CompareTests(unittest.TestCase):
    def test_strict_compare_normalizes_only_one_final_newline(self):
        self.assertEqual(parity.norm("A\n"), parity.norm("A"))
        self.assertNotEqual(parity.norm("A\n\n"), parity.norm("A\n"))
        self.assertNotEqual(parity.norm("A\r\n\r\n"), parity.norm("A\r\n"))


class MessageTests(unittest.TestCase):
    def test_build_messages_rejects_empty_or_invalid_messages(self):
        self.assertIsNotNone(assistant.build_messages({"messages": []})[1])
        self.assertIsNotNone(assistant.build_messages({"messages": [{"role": "system", "content": "x"}]})[1])

    def test_build_messages_clips_content_and_keeps_final_user_message(self):
        messages, error = assistant.build_messages({"messages": [
            {"role": "assistant", "content": "older"},
            {"role": "user", "content": "x" * (assistant.MAX_MSG_CHARS + 10)},
        ]})
        self.assertIsNone(error)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertLessEqual(len(messages[-1]["content"]), assistant.MAX_MSG_CHARS + len("\n... (cut)"))


class ContentLengthTests(unittest.TestCase):
    def test_content_length_validation(self):
        self.assertEqual(server.parse_content_length("0"), (0, None))
        self.assertEqual(server.parse_content_length(str(server.MAX_BODY)), (server.MAX_BODY, None))
        for value in (None, "x", "-1", str(server.MAX_BODY + 1)):
            length, error = server.parse_content_length(value)
            self.assertIsNone(length)
            self.assertTrue(error)

    def test_server_rejects_unexpanded_copy_and_long_test_input(self):
        program = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TINY.\n"
                   "PROCEDURE DIVISION.\nSTOP RUN.\n")
        _, copy_error = server.validate({"cobol": program + "       COPY MISSING-BOOK.\n"})
        self.assertIn("COPY statements", copy_error)
        _, input_error = server.validate({"cobol": program,
                                          "inputs": ["x" * (server.MAX_INPUT_LEN + 1)]})
        self.assertIn("test input", input_error)


class SpendGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = server.SPEND_FILE
        self.old_spend = dict(server._spend)
        self.old_error = server._spend_error
        self.old_budget = server.BUDGET_USD
        server.SPEND_FILE = Path(self.temp.name) / "spend.json"
        server._spend = {"usd": 0.0, "tin": 0, "tout": 0, "calls": 0}
        server._spend_error = None
        server.BUDGET_USD = 1.0

    def tearDown(self):
        server.SPEND_FILE = self.old_path
        server._spend = self.old_spend
        server._spend_error = self.old_error
        server.BUDGET_USD = self.old_budget
        self.temp.cleanup()

    def test_corrupt_spend_state_fails_closed(self):
        server.SPEND_FILE.write_text("not-json")
        _, error = server._load_spend(server.SPEND_FILE)
        self.assertIsNotNone(error)
        server._spend_error = error
        self.assertTrue(server.over_budget())

    def test_missing_usage_is_estimated_and_call_is_persisted(self):
        self.assertTrue(server.add_spend(None, None))
        saved = json.loads(server.SPEND_FILE.read_text())
        self.assertEqual(saved["calls"], 1)
        self.assertGreaterEqual(saved["tin"], 1)
        self.assertGreaterEqual(saved["tout"], 1)

    def test_failed_spend_write_fails_closed(self):
        with patch.object(server, "_persist_spend", return_value=False):
            self.assertFalse(server.add_spend(10, 10))
        self.assertTrue(server.over_budget())


class StressInputTests(unittest.TestCase):
    def test_parse_inputs_multiline_filtering_and_limit(self):
        sample = "same\n"
        reply = ("### INPUT\nfirst\nsecond\n### END\n"
                 "### INPUT\nsame\n### END\n"
                 "### INPUT\nthird\n### END\n")
        self.assertEqual(parse_inputs(reply, sample, 1), ["first\nsecond\n"])
        self.assertEqual(parse_inputs(reply, sample, 5), ["first\nsecond\n", "third\n"])

    def test_parse_inputs_json_fallback_ignores_non_strings(self):
        self.assertEqual(parse_inputs('["a", 2, "b"]', "sample\n", 4), ["a\n", "b\n"])


if __name__ == "__main__":
    unittest.main()
