import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "deliver.py"
spec = importlib.util.spec_from_file_location("deliver", MODULE)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def result(ok=True, data=None, error=None):
    value = {"ok": ok}
    value["data" if ok else "error"] = data if ok else error
    return subprocess.CompletedProcess([], 0 if ok else 1,
                                       json.dumps(value) if ok else "",
                                       "" if ok else json.dumps(value))


class FakeCLI:
    def __init__(self, responses=None, app="cli_test"):
        self.responses = list(responses or [result(data={"message_id": "om_test"})])
        self.app = app
        self.sent = 0
        self.commands = []

    def __call__(self, argv, cwd, timeout):
        self.commands.append((argv, cwd))
        if "auth" in argv:
            return subprocess.CompletedProcess([], 0, json.dumps({"appId": self.app}), "")
        self.assert_safe(argv, cwd)
        if "--dry-run" in argv:
            return result(data={"dry_run": True})
        self.sent += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def assert_safe(self, argv, cwd):
        assert argv[argv.index("--as") + 1] == "bot"
        assert argv[argv.index("--user-id") + 1] == "ou_test"
        path = argv[argv.index("--markdown") + 1]
        assert path.startswith("@./") and (cwd / path[3:]).is_file()
        assert len(argv[argv.index("--idempotency-key") + 1]) <= 50


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"delivery": {"enabled": True,
            "bot_app_id": "cli_test", "recipient_open_id": "ou_test", "bot_name": "DemoBot"}}))
        self.brief = self.root / "brief.md"
        self.brief.write_text("# Brief\n\n汉字 & $(do not execute)\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, fake, run_id="test-run", **kwargs):
        return d.deliver(self.config, self.root / "archive", run_id, self.brief,
                         executable=str(MODULE), runner=fake, **kwargs)

    def ledger(self, run_id="test-run"):
        return self.root / "archive" / "delivery" / (run_id + ".json")

    def test_sent_dedup_and_changed_content_rejected(self):
        fake = FakeCLI()
        self.assertEqual(self.call(fake)["status"], "sent")
        self.assertEqual(self.call(fake)["status"], "already_sent")
        self.assertEqual(fake.sent, 1)
        state = json.loads(self.ledger().read_text())
        self.assertEqual(state["attempt"], 1)
        self.assertTrue(state["sent_at"])
        self.brief.write_text("new revision")
        with self.assertRaisesRegex(d.DeliveryError, "Content changed"):
            self.call(fake)

    def test_explicit_rejection_retry_reuses_idempotency_key(self):
        fake = FakeCLI([result(False, error={"type": "authorization", "code": 99991679}),
                        result(data={"message_id": "om_retry"})])
        self.assertEqual(self.call(fake)["status"], "failed")
        old_key = json.loads(self.ledger().read_text())["idempotency_key"]
        self.assertEqual(self.call(fake)["status"], "sent")
        state = json.loads(self.ledger().read_text())
        self.assertEqual(state["attempt"], 2)
        self.assertEqual(state["idempotency_key"], old_key)

    def test_timeout_and_missing_json_never_blindly_retry(self):
        for run_id, response in (("timeout", subprocess.TimeoutExpired("fake", 60)),
                                 ("nojson", subprocess.CompletedProcess([], 0, "lost response", ""))):
            fake = FakeCLI([response])
            self.assertEqual(self.call(fake, run_id)["status"], "uncertain")
            # Even after the provider's dedup window has passed, uncertain
            # status blocks a blind resend, rather than trusting the UUID.
            path = self.ledger(run_id)
            state = json.loads(path.read_text())
            state["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(d.DeliveryError, "reconcile"):
                self.call(fake, run_id)
            self.assertEqual(fake.sent, 1)

    def test_interrupted_sending_is_uncertain_and_lock_released(self):
        fake = FakeCLI()
        self.call(fake, dry_run=True)
        path = self.ledger()
        state = json.loads(path.read_text())
        state["status"] = "sending"
        state["attempt"] = 1
        path.write_text(json.dumps(state))
        with self.assertRaises(d.DeliveryError):
            self.call(fake)
        self.assertEqual(json.loads(path.read_text())["status"], "uncertain")
        self.assertEqual(fake.sent, 0)
        with d.process_lock(path.with_suffix(".lock")):
            with self.assertRaises(d.DeliveryError):
                with d.process_lock(path.with_suffix(".lock")):
                    self.fail("Concurrent lock must fail")

    def test_dry_run_then_actual_send_and_target_guard(self):
        fake = FakeCLI()
        self.assertEqual(self.call(fake, dry_run=True)["status"], "dry_run")
        self.assertEqual(json.loads(self.ledger().read_text())["attempt"], 0)
        self.assertEqual(self.call(fake)["status"], "sent")
        config = json.loads(self.config.read_text())
        config["delivery"]["recipient_open_id"] = "ou_another"
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(d.DeliveryError, "target changed"):
            self.call(fake)

    def test_wrong_bot_does_not_send_and_can_recover(self):
        fake = FakeCLI(app="cli_wrong")
        with self.assertRaisesRegex(d.DeliveryError, "identity verification"):
            self.call(fake)
        self.assertEqual(fake.sent, 0)
        self.assertEqual(json.loads(self.ledger().read_text())["attempt"], 0)
        fake.app = "cli_test"
        self.assertEqual(self.call(fake)["status"], "sent")

    def test_success_without_message_id_is_uncertain(self):
        fake = FakeCLI([result(data={"create_time": "1"})])
        self.assertEqual(self.call(fake)["status"], "uncertain")
        with self.assertRaises(d.DeliveryError):
            self.call(fake)
        self.assertEqual(fake.sent, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
