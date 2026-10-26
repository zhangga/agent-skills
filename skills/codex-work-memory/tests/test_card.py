"""Transport tests only: fixtures are not cards intended for delivery."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "deliver.py"
spec = importlib.util.spec_from_file_location("deliver_card", MODULE)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class FakeCLI:
    def __init__(self, responses=None, app="cli_test"):
        self.responses = list(responses or [subprocess.CompletedProcess([], 0,
            json.dumps({"ok": True, "data": {"message_id": "om_card"}}), "")])
        self.app = app
        self.sent = 0
        self.commands = []

    def __call__(self, argv, cwd, timeout):
        self.commands.append((argv, cwd))
        if "auth" in argv:
            return subprocess.CompletedProcess([], 0, json.dumps({"appId": self.app}), "")
        assert argv[argv.index("--as") + 1] == "bot"
        assert argv[argv.index("--user-id") + 1] == "ou_test"
        assert len(argv[argv.index("--idempotency-key") + 1]) <= 50
        flag = "--content" if "--content" in argv else "--markdown"
        relative = argv[argv.index(flag) + 1]
        assert relative.startswith("@./") and (cwd / relative[3:]).is_file()
        if flag == "--content":
            assert "--markdown" not in argv
            assert argv[argv.index("--msg-type") + 1] == "interactive"
        if "--dry-run" in argv:
            return subprocess.CompletedProcess([], 0, '{"ok":true,"data":{}}', "")
        self.sent += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class CardDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"delivery": {"enabled": True,
            "bot_app_id": "cli_test", "recipient_open_id": "ou_test", "bot_name": "DemoBot"}}))
        self.card = self.root / "card.json"
        self.card.write_text(json.dumps({"schema": "2.0", "body": {"elements": [
            {"tag": "markdown", "content": "中文 $(literal) `literal`"}]}},
            ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        self.archive = self.root / "archive"

    def tearDown(self):
        self.temp.cleanup()

    def call(self, fake, run_id="card-test", **kwargs):
        return d.deliver(self.config, self.archive, run_id,
                         executable=str(MODULE), runner=fake, card=self.card, **kwargs)

    def ledger_path(self, run_id="card-test"):
        return self.archive / "delivery" / (run_id + ".json")

    def ledger(self, run_id="card-test"):
        return json.loads(self.ledger_path(run_id).read_text(encoding="utf-8"))

    def test_card_send_snapshot_hash_and_dedup(self):
        fake = FakeCLI()
        self.assertEqual(self.call(fake)["status"], "sent")
        self.assertEqual(self.call(fake)["status"], "already_sent")
        state = self.ledger()
        self.assertEqual(state["content_type"], "interactive")
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(Path(state["snapshot_path"]).read_bytes(), self.card.read_bytes())
        self.assertEqual(state["content_hash"], hashlib.sha256(
            b"interactive\0" + self.card.read_bytes()).hexdigest())
        self.assertEqual(fake.sent, 1)

    def test_same_bytes_cannot_change_message_format(self):
        fake = FakeCLI()
        d.deliver(self.config, self.archive, "card-test", self.card,
                  executable=str(MODULE), runner=fake)
        self.assertEqual(self.ledger()["content_type"], "markdown")
        with self.assertRaisesRegex(d.DeliveryError, "Content changed"):
            self.call(fake)
        self.assertEqual(fake.sent, 1)

    def test_legacy_markdown_sent_ledger_is_still_deduplicated(self):
        self.ledger_path().parent.mkdir(parents=True)
        previous = {"schema_version": 1, "bot_app_id": "cli_test",
                    "recipient_open_id": "ou_test", "status": "sent",
                    "message_id": "om_old", "content_hash": hashlib.sha256(self.card.read_bytes()).hexdigest()}
        self.ledger_path().write_text(json.dumps(previous))
        fake = FakeCLI()
        result = d.deliver(self.config, self.archive, "card-test", self.card,
                           executable=str(MODULE), runner=fake)
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(result["message_id"], "om_old")
        with self.assertRaisesRegex(d.DeliveryError, "Content changed"):
            self.call(fake)
        self.assertEqual(fake.commands, [])

    def test_legacy_failed_markdown_migrates_then_deduplicates(self):
        fake = FakeCLI()
        d.deliver(self.config, self.archive, "card-test", self.card,
                  dry_run=True, executable=str(MODULE), runner=fake)
        state = self.ledger()
        state.pop("content_type")
        state["schema_version"] = 1
        state["content_hash"] = hashlib.sha256(self.card.read_bytes()).hexdigest()
        self.ledger_path().write_text(json.dumps(state))
        result = d.deliver(self.config, self.archive, "card-test", self.card,
                           executable=str(MODULE), runner=fake)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.ledger()["schema_version"], 2)
        result = d.deliver(self.config, self.archive, "card-test", self.card,
                           executable=str(MODULE), runner=fake)
        self.assertEqual(result["status"], "already_sent")

    def test_unknown_card_outcome_cannot_retry(self):
        fake = FakeCLI([subprocess.TimeoutExpired("fake", 60)])
        self.assertEqual(self.call(fake)["status"], "uncertain")
        with self.assertRaisesRegex(d.DeliveryError, "reconcile"):
            self.call(fake)
        self.assertEqual(fake.sent, 1)
        self.assertEqual(self.ledger()["content_type"], "interactive")

    def test_changed_card_requires_revision_id(self):
        fake = FakeCLI()
        self.call(fake)
        self.card.write_text(self.card.read_text(encoding="utf-8").replace("literal", "changed"), encoding="utf-8")
        with self.assertRaisesRegex(d.DeliveryError, "Content changed"):
            self.call(fake)
        self.assertEqual(fake.sent, 1)

    def test_card_dry_run_then_send(self):
        fake = FakeCLI()
        self.assertEqual(self.call(fake, dry_run=True)["status"], "dry_run")
        self.assertEqual(fake.sent, 0)
        self.assertEqual(self.ledger()["attempt"], 0)
        self.assertEqual(self.call(fake)["status"], "sent")
        self.assertEqual(self.ledger()["attempt"], 1)

    def test_mutually_exclusive_and_invalid_cards_never_call_cli(self):
        fake = FakeCLI()
        with self.assertRaisesRegex(d.DeliveryError, "exactly one"):
            d.deliver(self.config, self.archive, "test", self.card, runner=fake, card=self.card)
        with self.assertRaisesRegex(d.DeliveryError, "exactly one"):
            d.deliver(self.config, self.archive, "test", runner=fake)
        for content in ("[]", "{", '{"schema":"1.0"}', '{"schema":"2.0","body":{"elements":[]}}'):
            self.card.write_text(content, encoding="utf-8")
            with self.assertRaises(d.DeliveryError):
                self.call(fake)
        self.assertEqual(fake.commands, [])

    def test_url_validation(self):
        value = json.loads(self.card.read_text(encoding="utf-8"))
        element = value["body"]["elements"][0]
        for url in ("file:///C:/private", "javascript:alert(1)", "https://user:secret@example.com", "https://x\n.example", "https://",
                    "codex://other/00000000-0000-4000-8000-000000000001",
                    "codex://threads/00000000-0000-4000-8000-000000000001?query=bad",
                    "codex://threads/not-a-uuid", "lark://msgcard/unsupported_action"):
            element["behaviors"] = [{"type": "open_url", "default_url": url, "url": url}]
            with self.assertRaises(d.DeliveryError, msg=url):
                d.validated_card(json.dumps(value))
        element["behaviors"] = [{"type": "open_url", "url": "https://applink.feishu.cn/client/chat/open?openChatId=oc_test"}]
        self.assertEqual(d.validated_card(json.dumps(value)), value)
        element["behaviors"] = [{"type": "open_url", "url": "codex://threads/00000000-0000-4000-8000-000000000001",
            "pc_url": "codex://threads/00000000-0000-4000-8000-000000000001",
            "ios_url": "lark://msgcard/unsupported_action", "android_url": "lark://msgcard/unsupported_action"}]
        self.assertEqual(d.validated_card(json.dumps(value)), value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
