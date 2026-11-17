"""Offline notification and workflow regressions. Every HTTP call is mocked."""
import contextlib
import io
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import requests
import notify_telegram as notify


class Notification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "output"
        self.summary = Path(self.tmp.name) / "summary"
        env = {
            "TELEGRAM_BOT_TOKEN": "test-token-do-not-log",
            "TELEGRAM_CHAT_ID": "private-chat-do-not-log",
            "MESSAGE": "private-message-do-not-log",
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.post_patch = patch.object(notify.requests, "post")
        self.post = self.post_patch.start()
        self.addCleanup(self.post_patch.stop)
        self.post.return_value = Mock(status_code=200)
        self.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 123}}

    def run_main(self, args=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = notify.main([] if args is None else args)
        self.logs = stdout.getvalue()
        return code

    def assert_status(self, status):
        self.assertIn(f"telegram_status={status}\n", self.output.read_text())
        self.assertIn(f"Telegram notification: {status}", self.summary.read_text())

    def test_api_acceptance_has_receipt_not_read_claim(self):
        self.assertEqual(self.run_main(), 0)
        self.assert_status("accepted")
        self.assertIn("telegram_message_id=123", self.output.read_text())
        self.assertIn("not a read receipt", self.summary.read_text())
        self.post.assert_called_once()

    def test_missing_configuration_does_not_make_a_request(self):
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            with self.subTest(key=key), patch.dict(os.environ, {key: ""}):
                self.assertEqual(self.run_main(), 0)
                self.assert_status("skipped")
        self.post.assert_not_called()

    def test_read_timeout_is_unknown_without_retry_or_secret_log(self):
        self.post.side_effect = requests.ReadTimeout(
            "https://api.telegram.org/bottest-token-do-not-log/sendMessage")
        self.assertEqual(self.run_main(), 1)
        self.assert_status("unknown")
        self.post.assert_called_once()
        self.assertIn("Do not retry automatically", self.logs)
        self.assertNotIn("test-token-do-not-log", self.logs + self.summary.read_text())
        self.assertNotIn("telegram_message_id", self.output.read_text())

    def test_connection_error_is_conservatively_unknown_without_retry(self):
        self.post.side_effect = requests.ConnectionError("private connection detail")
        self.assertEqual(self.run_main(), 1)
        self.assert_status("unknown")
        self.post.assert_called_once()
        self.assertNotIn("private connection detail", self.logs)

    def test_server_error_is_unknown_even_with_failure_body(self):
        self.post.return_value.status_code = 500
        self.post.return_value.json.return_value = {"ok": False}
        self.assertEqual(self.run_main(), 1)
        self.assert_status("unknown")
        self.post.assert_called_once()

    def test_api_rejection_is_not_reported_as_success(self):
        for status in (200, 400, 401, 403, 429):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.return_value.status_code = status
                self.post.return_value.json.return_value = {
                    "ok": False, "description": "private-message-do-not-log",
                    "parameters": {"retry_after": 1},
                }
                self.assertEqual(self.run_main(), 1)
                self.assert_status("rejected")
                self.post.assert_called_once()
                self.assertNotIn("private-message-do-not-log", self.logs)

    def test_non_json_response_is_unknown(self):
        self.post.return_value.json.side_effect = ValueError("private response")
        self.assertEqual(self.run_main(), 1)
        self.assert_status("unknown")
        self.assertNotIn("private response", self.logs)

    def test_invalid_json_shape_is_unknown(self):
        for data in (None, [], "ok", True):
            with self.subTest(data=data):
                self.post.return_value.json.return_value = data
                self.assertEqual(self.run_main(), 1)
                self.assert_status("unknown")

    def test_missing_or_invalid_receipt_is_unknown(self):
        for receipt in (None, 0, -1, True, "123"):
            with self.subTest(receipt=receipt):
                self.post.return_value.json.return_value = {
                    "ok": True, "result": {"message_id": receipt}}
                self.assertEqual(self.run_main(), 1)
                self.assert_status("unknown")
        self.assertNotIn("telegram_message_id", self.output.read_text())

    def test_success_boolean_and_result_object_are_required(self):
        for data in ({"result": {"message_id": 1}},
                     {"ok": "true", "result": {"message_id": 1}},
                     {"ok": True, "result": []}):
            with self.subTest(data=data):
                self.post.return_value.json.return_value = data
                self.assertEqual(self.run_main(), 1)
                self.assert_status("unknown")

    def test_http_error_cannot_be_success_despite_receipt(self):
        self.post.return_value.status_code = 503
        self.assertEqual(self.run_main(), 1)
        self.assert_status("unknown")

    def test_redirects_disabled_and_bounded_timeouts(self):
        self.post.return_value.status_code = 307
        self.assertEqual(self.run_main(), 1)
        self.assert_status("unknown")
        self.post.assert_called_once()
        self.assertIs(self.post.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(self.post.call_args.kwargs["timeout"], (10, 30))

    def test_two_buttons_and_argument_precedence_are_preserved(self):
        with patch.dict(os.environ, {"BUTTON_URL": "https://example.com/ig",
                                    "BUTTON_TEXT": "Instagram",
                                    "BUTTON2_URL": "https://example.com/fb"}):
            self.assertEqual(self.run_main(["argument message"]), 0)
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["text"], "argument message")
        self.assertEqual(body["reply_markup"]["inline_keyboard"], [[
            {"text": "Instagram", "url": "https://example.com/ig"},
            {"text": "Open", "url": "https://example.com/fb"},
        ]])

    def test_empty_message_fallback_and_no_buttons(self):
        with patch.dict(os.environ, {"MESSAGE": ""}):
            self.assertEqual(self.run_main(), 0)
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["text"], "(empty)")
        self.assertNotIn("reply_markup", body)
        self.assertIs(body["disable_web_page_preview"], True)

    def test_no_private_response_body_or_chat_is_logged(self):
        self.post.return_value.text = "private-chat-do-not-log private-message-do-not-log"
        self.assertEqual(self.run_main(), 0)
        for secret in ("test-token-do-not-log", "private-chat-do-not-log",
                       "private-message-do-not-log"):
            self.assertNotIn(secret, self.logs + self.summary.read_text() + self.output.read_text())

    def test_command_works_without_github_report_paths(self):
        with patch.dict(os.environ, {"GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.summary.exists())

    def test_output_open_error_preserves_acceptance_and_other_sink(self):
        real_open = open

        def fail_output(path, *args, **kwargs):
            if path == str(self.output):
                raise OSError("private-chat-do-not-log")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fail_output):
            self.assertEqual(self.run_main(), 0)
        self.post.assert_called_once()
        self.assertIn("notification: accepted", self.logs)
        self.assertIn("::warning::Telegram GITHUB_OUTPUT reporting failed", self.logs)
        self.assertIn("API outcome is unchanged", self.logs)
        self.assertNotIn("private-chat-do-not-log", self.logs)
        self.assertIn("notification: accepted", self.summary.read_text())
        self.assertIn("API message ID: 123", self.summary.read_text())

    def test_summary_open_error_preserves_acceptance_and_output(self):
        real_open = open

        def fail_summary(path, *args, **kwargs):
            if path == str(self.summary):
                raise OSError("private-message-do-not-log")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fail_summary):
            self.assertEqual(self.run_main(), 0)
        self.post.assert_called_once()
        self.assertIn("telegram_status=accepted", self.output.read_text())
        self.assertIn("telegram_message_id=123", self.output.read_text())
        self.assertIn("::warning::Telegram GITHUB_STEP_SUMMARY reporting failed", self.logs)
        self.assertNotIn("private-message-do-not-log", self.logs)

    def test_both_report_sinks_fail_without_losing_acceptance_or_retrying(self):
        with patch("builtins.open", side_effect=OSError("private-path")) as sink:
            self.assertEqual(self.run_main(), 0)
        self.post.assert_called_once()
        self.assertEqual(sink.call_count, 2)
        self.assertIn("notification: accepted", self.logs)
        self.assertEqual(self.logs.count("reporting failed"), 2)
        self.assertNotIn("private-path", self.logs)

    def test_report_write_and_close_errors_preserve_acceptance(self):
        for method in ("write", "__exit__"):
            with self.subTest(method=method):
                self.post.reset_mock()
                sink = mock_open()
                getattr(sink.return_value, method).side_effect = OSError("private-sink")
                with patch("builtins.open", sink):
                    self.assertEqual(self.run_main(), 0)
                self.post.assert_called_once()
                self.assertEqual(sink.call_count, 2)
                self.assertIn("notification: accepted", self.logs)
                self.assertIn("Do not resend based on a reporting error", self.logs)
                self.assertNotIn("private-sink", self.logs)

    def test_console_oserror_preserves_acceptance_and_warns_on_stderr(self):
        broken_stdout = Mock()
        broken_stdout.write.side_effect = OSError("private-console")
        stderr = io.StringIO()
        with contextlib.redirect_stdout(broken_stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(notify.main([]), 0)
        self.post.assert_called_once()
        self.assert_status("accepted")
        self.assertIn("::warning::Telegram console reporting failed", stderr.getvalue())
        self.assertNotIn("private-console", stderr.getvalue())

    def test_reporting_failure_does_not_change_unknown_rejected_or_skipped(self):
        for status, code in (("unknown", 1), ("rejected", 1), ("skipped", 0)):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.side_effect = requests.ReadTimeout() if status == "unknown" else None
                self.post.return_value.json.return_value = {"ok": False}
                token = "" if status == "skipped" else "test-token-do-not-log"
                with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token}), \
                        patch("builtins.open", side_effect=OSError("private-sink")):
                    self.assertEqual(self.run_main(), code)
                self.assertEqual(self.post.call_count, 0 if status == "skipped" else 1)
                self.assertIn(f"notification: {status}", self.logs)
                self.assertNotIn("private-sink", self.logs)

    def test_separate_invocations_are_not_deduplicated(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.post.call_count, 2)
        self.assertIn("does not prevent duplicates across reruns", self.summary.read_text())


class WorkflowSafety(unittest.TestCase):
    def setUp(self):
        self.text = (Path(__file__).parent.parent /
                     ".github/workflows/namecode-daily.yml").read_text()

    def job(self, name):
        return self.text.split(f"  {name}:\n", 1)[1].split("\n  validate:", 1)[0]

    def summary_script(self, job):
        block = self.job(job).split(
            "      - name: Summarize content, publication and notification outcomes\n", 1)[1]
        header, script = block.split("        run: |\n", 1)
        self.assertIn("if: ${{ always() }}", header)
        return textwrap.dedent(script)

    def test_only_notification_steps_are_newly_optional(self):
        blocks = self.text.split("      - name: Notify Telegram (status)")[1:]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            before_run = block.split("        run: |", 1)[0]
            self.assertIn("id: notify", before_run)
            self.assertIn("continue-on-error: true", before_run)
            self.assertIn("always()", before_run)
        self.assertIn('python -m unittest discover -s namecode_grid -p "test_notify_telegram.py"', self.text)
        self.assertEqual(len(re.findall(r"^        continue-on-error: true$", self.text, re.M)), 2)

    def test_publishing_still_requires_manual_approval_and_fresh_artwork(self):
        for channel in ("Instagram", "Facebook"):
            block = self.text.split(f"Publish feed image to {channel} (optional)", 1)[1]
            condition = block.split("working-directory:", 1)[0]
            self.assertIn("github.event.inputs.publish == 'true'", condition)
            self.assertIn("steps.gen.outputs.fresh == 'true'", condition)
        self.assertIn("existing artwork preserved", self.text)
        self.assertNotIn("--retry", self.text.split("Notify Telegram (status)", 1)[1])

    def test_publication_failures_cannot_be_masked(self):
        for step in ("Publish feed image to Instagram (optional)",
                     "Publish feed image to Facebook (optional)",
                     "Publish feed image to Facebook"):
            header = self.text.split(f"      - name: {step}\n", 1)[1].split("        run: |", 1)[0]
            self.assertNotIn("continue-on-error:", header)
        # Reports must run even after a failed publication. They cannot turn a
        # preceding mandatory step into a successful GitHub job.
        for job in ("generate", "fb-only"):
            self.summary_script(job)
            notify_header = self.job(job).split("      - name: Notify Telegram (status)\n", 1)[1].split("        run: |", 1)[0]
            self.assertIn("always()", notify_header)

    def test_combined_summaries_show_content_and_notification_independently(self):
        # Execute only the summary shell, never a generation/publish/notify step.
        for job in ("generate", "fb-only"):
            for facebook, notify_step, telegram in (("failure", "success", "accepted"),
                                                     ("success", "failure", "unknown"),
                                                     ("failure", "failure", "unknown"),
                                                     ("skipped", "skipped", "")):
                with self.subTest(job=job, facebook=facebook, telegram=telegram), \
                        tempfile.TemporaryDirectory() as tmp:
                    summary = Path(tmp) / "summary"
                    env = {"PATH": os.defpath, "GITHUB_STEP_SUMMARY": str(summary),
                           "GENERATION": "success", "ARCHIVE": "success", "ARTIFACT": "success",
                           "SAVED": "failure", "INSTAGRAM": "skipped", "FACEBOOK": facebook,
                           "NOTIFY_STEP": notify_step, "TELEGRAM_STATUS": telegram}
                    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", self.summary_script(job)],
                                            env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    text = summary.read_text()
                    self.assertIn(f"| Facebook feed | {facebook} |", text)
                    self.assertIn(f"| Telegram step | {notify_step} |", text)
                    self.assertIn(f"| Telegram API | {telegram or 'unavailable; see notification log'} |", text)
                    self.assertIn("failures remain failures", text)
                    self.assertIn("not deduplication across reruns", text)
                    if job == "generate":
                        self.assertIn("| Save to repository | failure |", text)

    def test_combined_summary_write_failure_warns_without_any_replay(self):
        for job in ("generate", "fb-only"):
            with self.subTest(job=job), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", self.summary_script(job)],
                                        env={"PATH": os.defpath, "GITHUB_STEP_SUMMARY": tmp},
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0)
                self.assertIn("::warning::Unable to write the combined summary", result.stdout)
                self.assertNotIn("python", self.summary_script(job))


if __name__ == "__main__":
    unittest.main()
