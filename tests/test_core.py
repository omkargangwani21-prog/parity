"""Pure local checks for input validation, comparisons, and spend protection."""
import json
import io
import os
import sys
import tempfile
import threading
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


class IsolationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only isolation check")
    def test_self_test_fails_when_child_reads_environ(self):
        status = server.isolation_self_test(
            lambda *args, **kwargs: (0, b"PATH=/secret", ""))
        self.assertEqual(status, "failed")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux-only isolation check")
    def test_self_test_passes_on_permission_denied(self):
        status = server.isolation_self_test(
            lambda *args, **kwargs: (1, "", "cat: /proc/123/environ: Permission denied"))
        self.assertEqual(status, "ok")

    def test_translate_refuses_when_isolation_failed(self):
        handler = object.__new__(server.Handler)
        handler.path = "/api/translate"
        with patch.object(server, "ISOLATION_STATUS", "failed"), \
                patch.object(server.Handler, "_json") as send_json:
            server.Handler.do_POST(handler)
        send_json.assert_called_once_with(
            503, {"error": "Server isolation check failed, translation disabled"})

    def test_health_reports_cached_isolation_status(self):
        handler = object.__new__(server.Handler)
        handler.path = "/api/health"
        with patch.object(server, "ISOLATION_STATUS", "ok"), \
                patch.object(server.Handler, "_json") as send_json:
            server.Handler.do_GET(handler)
        send_json.assert_called_once_with(200, {"isolation": "ok"})


class CompareTests(unittest.TestCase):
    def test_strict_compare_normalizes_only_one_final_newline(self):
        self.assertEqual(parity.norm("A\n"), parity.norm("A"))
        self.assertNotEqual(parity.norm("A\n\n"), parity.norm("A\n"))
        self.assertNotEqual(parity.norm("A\r\n\r\n"), parity.norm("A\r\n"))

    def test_system_prompt_has_inspect_tallying_rule_15(self):
        self.assertIn("15. INSPECT ... TALLYING adds to the counter's CURRENT value", parity.SYSTEM)

    def test_exact_diff_marks_whitespace_only_changes(self):
        detail, = parity.exact_line_diff("TOTAL : 33,048.90", "TOTAL :  33,048.90")
        self.assertEqual(detail["note"], "whitespace differs: COBOL 17 chars, Java 18 chars")
        self.assertIn("·", detail["expected_exact"])
        self.assertIn("··", detail["actual_exact"])
        self.assertIn("17 chars", detail["expected_exact"])
        self.assertIn("18 chars", detail["actual_exact"])


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


class ResponseErrorTests(unittest.TestCase):
    def test_sse_exception_details_are_not_sent_to_client(self):
        fake_path = "/tmp/private/fake-request.json"
        fake_key = "fake-api-key-never-log-this"
        fake_header = "fake-header-value-never-log-this"
        handler = object.__new__(server.Handler)
        handler.wfile = io.BytesIO()

        def fail(_emit):
            raise RuntimeError(
                f"provider failed at {fake_path}; api_key={fake_key}; "
                f"headers={{'Authorization': 'Bearer {fake_header}'}}")

        with patch.dict(os.environ, {"NEBIUS_API_KEY": fake_key}), \
                patch.object(handler, "send_response"), \
                patch.object(handler, "send_header"), \
                patch.object(handler, "end_headers"), \
                self.assertLogs(server.logger, level="ERROR") as captured:
            handler._sse(fail, threading.Semaphore(1))

        response = handler.wfile.getvalue().decode()
        self.assertIn(server.GENERIC_SSE_ERROR, response)
        self.assertNotIn(fake_path, response)
        self.assertNotIn(fake_key, response)
        self.assertNotIn(fake_header, response)
        self.assertNotIn(fake_key, "\n".join(captured.output))
        self.assertNotIn(fake_header, "\n".join(captured.output))

    def test_assistant_provider_exception_is_generic(self):
        fake_path = "/tmp/private/fake-request.json"
        fake_key = "fake-api-key-never-log-this"
        events = []

        with patch.dict(os.environ, {"NEBIUS_API_KEY": fake_key}), \
                patch.object(assistant.parity, "client",
                             side_effect=RuntimeError(f"failed {fake_path} key={fake_key}")), \
                self.assertLogs(assistant.logger, level="ERROR"):
            assistant.stream({"messages": [{"role": "user", "content": "hello"}]},
                             lambda **event: events.append(event))

        errors = [event["text"] for event in events if event.get("type") == "error"]
        self.assertEqual(errors, [server.GENERIC_SSE_ERROR])
        self.assertNotIn(fake_path, " ".join(errors))
        self.assertNotIn(fake_key, " ".join(errors))


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


class JavaVerdictTests(unittest.TestCase):
    COBOL = 'IDENTIFICATION DIVISION.\nPROGRAM-ID. TINY.\nPROCEDURE DIVISION.\nSTOP RUN.\n'
    DUPLICATE_JAVA = '''import java.math.BigDecimal;
public class Main {
  static void storeNumeric(BigDecimal n, int scale) {}
  static void storeNumeric(BigDecimal n, int scale) {}
  public static void main(String[] args) { System.out.println("OK"); }
}'''
    GOOD_JAVA = 'public class Main { public static void main(String[] a) { System.out.println("OK"); } }'

    def test_zero_compared_inputs_are_not_a_pass(self):
        ok, reason = parity.feedback({"compile_error": None, "tests": []})
        self.assertFalse(ok)
        self.assertIn("no inputs", reason.lower())

    def test_javac_nonzero_with_stdout_only_is_compile_failure(self):
        with tempfile.TemporaryDirectory() as td, patch.object(parity, "SAFE", False), \
                patch.object(parity, "run", return_value=(1, "duplicate method", "")):
            result = parity.run_java_tests(self.GOOD_JAVA, Path(td), [""], ["OK"])
        self.assertIn("duplicate method", result["compile_error"])
        self.assertFalse(parity.feedback(result)[0])

    def test_real_javac_error_is_fed_to_fake_model_fix_round(self):
        replies = [self.DUPLICATE_JAVA, self.GOOD_JAVA]
        calls = []

        def fake_chat(model, messages, *args, **kwargs):
            calls.append(messages)
            return {"text": "```java\n" + replies.pop(0) + "\n```", "finish": "stop",
                    "tin": 10, "tout": 10, "secs": 0, "usage_estimated": False}

        with tempfile.TemporaryDirectory() as td, patch.object(parity, "SAFE", False), \
                patch.object(parity, "chat", side_effect=fake_chat):
            result = parity.solve_tier("ultra", self.COBOL, [""], ["OK"], Path(td), 1)
        self.assertTrue(result["solved"])
        self.assertEqual(len(calls), 2)
        self.assertIn("javac error:", calls[1][-1]["content"])
        self.assertIn("storeNumeric", calls[1][-1]["content"])

    def test_cut_off_reply_keeps_last_failure_and_drops_thinking(self):
        replies = [("stop", self.DUPLICATE_JAVA), ("length", "partial"), ("stop", self.GOOD_JAVA)]
        calls = []

        def fake_chat(model, messages, max_tokens=None, on_progress=None, think=False):
            finish, java = replies.pop(0)
            calls.append((messages, think))
            return {"text": "```java\n" + java + "\n```", "finish": finish,
                    "tin": 10, "tout": 10, "secs": 0, "usage_estimated": False}

        with tempfile.TemporaryDirectory() as td, patch.object(parity, "SAFE", False), \
                patch.object(parity, "THINK_MODE", "auto"), \
                patch.object(parity, "chat", side_effect=fake_chat):
            result = parity.solve_tier("ultra", self.COBOL, [""], ["OK"], Path(td), 3)
        self.assertTrue(result["solved"])
        self.assertEqual(len(calls), 3)
        self.assertFalse(calls[2][1])  # round 3 would normally think; a cut-off reply turns it off
        self.assertIn("javac error:", calls[2][0][-1]["content"])  # same fix request is repeated

    def test_difference_list_shows_up_to_sixteen_lines(self):
        exp = "\n".join(f"line {i}" for i in range(30))
        act = "\n".join(f"LINE {i}" for i in range(30))
        text = parity.line_diff(exp, act)
        self.assertIn("line 16:", text)
        self.assertNotIn("line 17:", text)
        self.assertIn("and 14 more differing lines", text)

    def test_difference_list_reports_a_shifted_line_as_one_extra_line(self):
        exp = "BANNER\n=====\nA\nB"
        act = " \nBANNER\n=====\nA\nB"
        text = parity.line_diff(exp, act)
        self.assertIn("4 of 4 COBOL output lines appear", text)
        self.assertIn("EXTRA line", text)
        self.assertNotIn("EXPECTED", text)

    def test_difference_list_reports_a_missing_line(self):
        text = parity.line_diff("A\nB\nC", "A\nC")
        self.assertIn("2 of 3 COBOL output lines appear", text)
        self.assertIn("did NOT print 1 line", text)

    def test_runtime_timeout_and_output_limit_are_failures(self):
        for failure in ((124, "", "TIMEOUT"),
                        (125, "x" * 100, "Child stdout exceeded the 1 MB output limit")):
            with self.subTest(rc=failure[0]), tempfile.TemporaryDirectory() as td, \
                    patch.object(parity, "SAFE", False), \
                    patch.object(parity, "run", side_effect=[(0, "", ""), failure]):
                result = parity.run_java_tests(self.GOOD_JAVA, Path(td), [""], ["OK"])
                ok, reason = parity.feedback(result)
                self.assertFalse(ok)
                self.assertIn("runtime error", reason.lower())

    def test_good_program_passes_with_outputs_populated(self):
        with tempfile.TemporaryDirectory() as td, patch.object(parity, "SAFE", False):
            result = parity.run_java_tests(self.GOOD_JAVA, Path(td), [""], ["OK"])
        ok, reason = parity.feedback(result)
        self.assertTrue(ok, reason)
        self.assertEqual(len(result["tests"]), 1, result["compile_error"])
        self.assertEqual(result["tests"][0]["actual"], "OK")


if __name__ == "__main__":
    unittest.main()
