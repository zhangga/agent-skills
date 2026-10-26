#!/usr/bin/env python3
"""Send an archived Markdown brief or Card 2.0 once through the Lark bot.

No shell is used. Unknown send outcomes are retained for human/read-only
reconciliation, because Lark's server-side idempotency lasts only one hour.
This module never reads or modifies collection/summary checkpoints.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.parse import urlsplit

_config_spec = importlib.util.spec_from_file_location("codex_work_memory_configuration", Path(__file__).with_name("configuration.py"))
configuration = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(configuration)


class DeliveryError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def process_lock(path: Path):
    """OS-held lock: crashes release the lock without deleting lock files."""
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise DeliveryError("Another delivery process holds this run's lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def cli_prefix(executable: str | None = None) -> list[str]:
    selected = executable or shutil.which("lark-cli")
    if not selected:
        raise DeliveryError("lark-cli is not on PATH")
    path = Path(selected).expanduser().resolve()
    if not path.is_file():
        raise DeliveryError("lark-cli executable does not exist")
    # npm's Windows cmd/ps1 shims forward to this entry point. Invoke Node
    # directly, avoiding cmd.exe expansion and PowerShell parsing entirely.
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        entry = path.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        node = path.parent / "node.exe"
        node_name = str(node) if node.is_file() else shutil.which("node")
        if entry.is_file() and node_name:
            return [node_name, str(entry)]
        if path.suffix.lower() == ".ps1":
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if powershell:
                return [powershell, "-NoProfile", "-NonInteractive", "-File", str(path)]
        raise DeliveryError("Unrecognized Windows CLI shim; supply the actual executable")
    if path.suffix.lower() == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def invoke(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return subprocess.run(argv, cwd=str(cwd), env=env, shell=False,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def json_envelope(text: str) -> dict | None:
    """Accept only a JSON object; noisy/partial output needs reconciliation."""
    try:
        value = json.loads(text.lstrip("\ufeff").strip())
        return value if isinstance(value, dict) else None
    except (ValueError, AttributeError):
        return None


def auth_app_id(result: subprocess.CompletedProcess) -> str:
    value = json_envelope(result.stdout)
    if result.returncode != 0 or not value or value.get("ok") is False:
        raise DeliveryError("Cannot verify current bot application; no send attempted")
    # auth status uses an unwrapped object on current CLI versions, while
    # ordinary commands use {ok, data}. Support both without printing tokens.
    data = value.get("data", value)
    if not isinstance(data, dict):
        raise DeliveryError("Unrecognized auth status response; no send attempted")
    app_id = data.get("appId") or data.get("app_id")
    if not app_id:
        bot = data.get("identities", {}).get("bot", {})
        app_id = bot.get("appId") or bot.get("app_id")
    if not app_id:
        raise DeliveryError("Auth status did not identify the bot application")
    return str(app_id)


def send_result(result: subprocess.CompletedProcess) -> tuple[str, dict]:
    value = json_envelope(result.stdout)
    error_value = json_envelope(result.stderr)
    if value and value.get("ok") is True:
        data = value.get("data", {})
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if message_id:
            return "sent", {"message_id": message_id,
                            "chat_id": data.get("chat_id"),
                            "sent_at": utc_now()}
        return "uncertain", {"error": "success_without_message_id"}
    failure = value if value and value.get("ok") is False else error_value
    if failure and failure.get("ok") is False:
        error = failure.get("error", {})
        if not isinstance(error, dict):
            return "uncertain", {"error": "unrecognized_error_envelope"}
        kind = str(error.get("type", "")).lower()
        subtype = str(error.get("subtype", "")).lower()
        code = error.get("code")
        # An explicit API/validation rejection is retryable; transport failures
        # can occur after the server accepted a send, so they are not.
        ambiguous = any(word in kind + " " + subtype for word in
                        ("timeout", "network", "transport", "connection", "internal"))
        if result.returncode == 10:
            return "blocked", {"error": "confirmation_required", "error_code": code}
        definite = (kind in {"authorization", "authentication", "validation", "api"}
                    or (isinstance(code, (str, int)) and str(code).isdigit()
                        and str(code) not in {"0", "500", "502", "503", "504"}))
        if definite and not ambiguous:
            return "failed", {"error": kind or "api_rejected", "error_code": code}
        return "uncertain", {"error": kind or "ambiguous_error", "error_code": code}
    return "uncertain", {"error": "missing_json_send_result",
                         "exit_code": result.returncode}


def validated_card(content: str) -> dict:
    """Check transport invariants; card design is validated by its builder."""
    try:
        value = json.loads(content)
    except ValueError as exc:
        raise DeliveryError("Card must be valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != "2.0":
        raise DeliveryError("Card must be a Card 2.0 root object with schema '2.0'")
    body = value.get("body")
    if (not isinstance(body, dict) or not isinstance(body.get("elements"), list)
            or not body["elements"] or not all(isinstance(item, dict) for item in body["elements"])):
        raise DeliveryError("Card body.elements must be a non-empty list of components")

    def check_urls(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"url", "default_url", "pc_url", "ios_url", "android_url"} and item:
                    if not isinstance(item, str):
                        raise DeliveryError("Card URL must be a string")
                    # These routes are verified in the installed Codex app and
                    # Feishu card docs. Do not broadly allow custom schemes.
                    codex_thread = bool(re.fullmatch(
                        r"codex://threads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", item))
                    mobile_disabled = (key in {"ios_url", "android_url"}
                                       and item == "lark://msgcard/unsupported_action")
                    if codex_thread or mobile_disabled:
                        continue
                    try:
                        parsed = urlsplit(item)
                    except ValueError as exc:
                        raise DeliveryError("Card URL is malformed") from exc
                    if (parsed.scheme not in {"https", "http"} or not parsed.netloc
                            or parsed.username or parsed.password
                            or any(ord(char) < 32 for char in item)):
                        raise DeliveryError("Card URL must be HTTP(S), a verified Codex thread route, "
                                            "or the documented mobile-disabled constant")
                check_urls(item)
        elif isinstance(node, list):
            for item in node:
                check_urls(item)

    check_urls(value)
    return value


def deliver(config_path: Path, archive: Path, run_id: str, brief: Path | None = None,
            dry_run: bool = False, executable: str | None = None,
            runner: Callable = invoke, card: Path | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", run_id):
        raise DeliveryError("run-id must contain 1-120 ASCII letters, digits, '_' or '-'")
    if (brief is None) == (card is None):
        raise DeliveryError("Provide exactly one of brief or card")
    config = configuration.load_config(config_path, require_archive=False)
    delivery = config.get("delivery", {})
    if delivery.get("enabled") is not True:
        return {"ok": True, "status": "disabled", "run_id": run_id}
    app_id = delivery.get("bot_app_id", "")
    recipient = delivery.get("recipient_open_id", "")
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id):
        raise DeliveryError("Invalid configured bot_app_id")
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", recipient):
        raise DeliveryError("Invalid configured recipient_open_id")
    # Freeze exact content before invoking external processes.
    content_type = "interactive" if card is not None else "markdown"
    content = (card if card is not None else brief).read_text(encoding="utf-8-sig")
    if not content.strip():
        raise DeliveryError("Card is empty" if card is not None else "Brief is empty")
    if card is not None:
        validated_card(content)
    content_bytes = content.encode("utf-8")
    digest = hashlib.sha256(content_type.encode("ascii") + b"\0" + content_bytes).hexdigest()
    directory = archive.resolve() / "delivery"
    directory.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / (run_id + ".json")
    with process_lock(directory / (run_id + ".lock")):
        previous = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
        if previous and (previous.get("bot_app_id") != app_id or
                         previous.get("recipient_open_id") != recipient):
            raise DeliveryError("Delivery target changed for this run-id; use a new revision run-id")
        if previous:
            # Legacy ledgers predate content_type and hash only Markdown bytes.
            # Recognize those exact briefs without allowing a format change to
            # reuse an already-sent run-id or its provider idempotency key.
            prior_type = previous.get("content_type", "markdown")
            expected = (hashlib.sha256(content_bytes).hexdigest()
                        if "content_type" not in previous else digest)
            if prior_type != content_type or previous.get("content_hash") != expected:
                raise DeliveryError("Content changed for this run-id; use a new revision run-id")
        status = previous.get("status")
        if status == "sent":
            return {"ok": True, "status": "already_sent", "run_id": run_id,
                    "message_id": previous.get("message_id"), "ledger": str(ledger_path)}
        if previous.get("retryable") is False or previous.get("superseded_by"):
            raise DeliveryError("This delivery was marked non-retryable or superseded; do not resend it")
        if status in {"sending", "uncertain", "blocked"}:
            if status == "sending":
                previous.update(status="uncertain", updated_at=utc_now(),
                                error="interrupted_after_send_started")
                atomic_json(ledger_path, previous)
            raise DeliveryError("Previous send outcome is uncertain or blocked; reconcile it before retrying. "
                                "Do not blindly resend after Lark's one-hour idempotency window.")
        prefix = cli_prefix(executable)
        state = dict(previous) if previous else {
            "schema_version": 2, "run_id": run_id, "content_hash": digest,
            "bot_name": delivery.get("bot_name", ""), "bot_app_id": app_id,
            "recipient_open_id": recipient, "attempt": 0,
            "message_id": None, "sent_at": None, "created_at": utc_now(),
            "idempotency_key": "cwm-" + hashlib.sha256(
                (app_id + ":" + recipient + ":" + run_id).encode()).hexdigest()[:40],
        }
        snapshot = directory / (run_id + (".card.json" if card is not None else ".md"))
        state.update(schema_version=2, content_type=content_type, content_hash=digest,
                     snapshot_path=str(snapshot))
        try:
            actual_app = auth_app_id(runner(prefix + ["auth", "status", "--json"], directory, 30))
            if actual_app != app_id:
                raise DeliveryError("Current bot application does not match configured bot_app_id")
        except (DeliveryError, OSError, subprocess.TimeoutExpired) as exc:
            state.update(status="failed", error="auth_verification_failed", updated_at=utc_now())
            atomic_json(ledger_path, state)
            raise DeliveryError("Bot identity verification failed; no message was sent") from exc
        snapshot.write_bytes(content_bytes)
        content_flags = (["--msg-type", "interactive", "--content", "@./" + snapshot.name]
                         if card is not None else ["--markdown", "@./" + snapshot.name])
        argv = prefix + ["im", "+messages-send", "--as", "bot", "--user-id", recipient,
                         *content_flags,
                         "--idempotency-key", state["idempotency_key"], "--format", "json"]
        if dry_run:
            result = runner(argv + ["--dry-run"], directory, 60)
            value = json_envelope(result.stdout)
            if result.returncode != 0 or (value and value.get("ok") is False):
                raise DeliveryError("CLI dry-run failed; no message was sent")
            state.update(status="dry_run", updated_at=utc_now())
            atomic_json(ledger_path, state)
            return {"ok": True, "status": "dry_run", "run_id": run_id,
                    "ledger": str(ledger_path), "content_hash": digest}
        state.update(status="sending", attempt=state.get("attempt", 0) + 1,
                     updated_at=utc_now(), last_attempt_at=utc_now())
        for key in ("error", "error_code", "exit_code"):
            state.pop(key, None)
        atomic_json(ledger_path, state)
        try:
            result = runner(argv, directory, 60)
            status, fields = send_result(result)
        except subprocess.TimeoutExpired:
            status, fields = "uncertain", {"error": "send_timeout"}
        except OSError as exc:
            # Only definite launch errors guarantee that no send occurred.
            if exc.errno in {errno.ENOENT, errno.EACCES, errno.ENOEXEC}:
                status, fields = "failed", {"error": "process_start_failed"}
            else:
                status, fields = "uncertain", {"error": "send_os_error"}
        except Exception:
            # The durable 'sending' marker already protects crash recovery.
            status, fields = "uncertain", {"error": "unexpected_send_exception"}
        state.update(fields, status=status, updated_at=utc_now())
        atomic_json(ledger_path, state)
        return {"ok": status == "sent", "status": status, "run_id": run_id,
                "message_id": state.get("message_id"), "ledger": str(ledger_path),
                "error": state.get("error")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--run-id", required=True)
    content_group = parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--brief", type=Path, help="Markdown brief to send as a post")
    content_group.add_argument("--card", type=Path, help="Validated Card 2.0 JSON to send as interactive")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lark-executable", help="Optional real executable or test Python CLI")
    args = parser.parse_args()
    try:
        args.config = configuration.resolve_config_path(args.config)
        if args.archive is None:
            args.archive = Path(configuration.load_config(args.config)["archive_dir"])
        result = deliver(args.config, args.archive, args.run_id, args.brief,
                         args.dry_run, args.lark_executable, card=args.card)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except (DeliveryError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
