#!/usr/bin/env python3
"""Initialize a personal profile or update its excluded threads without resetting archives."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from configuration import absolute_path, default_codex_home, load_config, normalize_config, resolve_config_path


def thread_ids(values):
    if not isinstance(values, list) or any(not isinstance(v, str) or not v or len(v) > 200
                                          or any(c.isspace() or ord(c) < 32 for c in v) for v in values):
        raise ValueError('Thread IDs must be nonempty strings without whitespace (at most 200 characters)')
    return list(dict.fromkeys(values))


def current_thread_id():
    value = os.environ.get('CODEX_THREAD_ID')
    if not value:
        raise ValueError('CODEX_THREAD_ID is unavailable; supply --exclude-thread with the confirmed current thread ID')
    return thread_ids([value])[0]


@contextmanager
def profile_lock(path):
    # OS locks are released on process exit; the marker may safely remain.
    with path.with_name(path.name + '.lock').open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another configuration writer holds the lock; retry after it finishes') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def set_exclusions(config_path, exclude=None, include=None, exclude_current=False):
    path = Path(config_path).expanduser().resolve()
    additions, removals = thread_ids(exclude or []), thread_ids(include or [])
    if exclude_current:
        additions = thread_ids(additions + [current_thread_id()])
    if not additions and not removals:
        raise ValueError('set requires --exclude-thread, --include-thread or --exclude-current-thread')
    if set(additions) & set(removals):
        raise ValueError('Cannot both exclude and include the same thread')
    # Check existence before creating any lock/backup files.
    if not path.is_file():
        raise FileNotFoundError(f'Configuration not found: {path}. Run setup.py init first.')
    with profile_lock(path):
        before = path.read_bytes()
        raw = json.loads(before.decode('utf-8-sig'))
        normalize_config(raw, path)
        previous = thread_ids(raw.get('excluded_thread_ids', []))
        updated = [v for v in previous if v not in removals]
        updated.extend(v for v in additions if v not in updated)
        result = {'ok': True, 'config': str(path), 'changed': updated != previous, 'excluded_thread_ids': updated}
        if updated == previous:
            return result
        raw['excluded_thread_ids'] = updated
        token = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        backup = path.with_name(path.name + '.bak-' + token)
        temp = path.with_name(path.name + '.tmp-' + token)
        try:
            with temp.open('x', encoding='utf-8', newline='\n') as handle:
                json.dump(raw, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != before:
                raise RuntimeError('Configuration changed during update; retry with the current file')
            with backup.open('xb') as handle:
                handle.write(before)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        result['backup'] = str(backup)
        return result


def initialize(config_path, archive_dir=None, agent_export_dir=None, codex_home=None,
               initial_since=None, excluded_thread_ids=None) -> dict:
    path = Path(config_path).expanduser().resolve()
    if path.exists():
        raise FileExistsError(
            f"Configuration already exists: {path}. Nothing was changed. To relocate an existing archive, "
            "pause scheduled writers, copy the entire archive including state.json and runs/, verify it, "
            "then edit this configuration. Changing only archive_dir starts a different archive."
        )
    codex = absolute_path(codex_home, path.parent, "codex_home") if codex_home else default_codex_home()
    since = initial_since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    stamp = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("--initial-since requires an explicit timezone offset")
    config = {
        "schema_version": 1,
        "archive_dir": str(archive_dir) if archive_dir is not None else str(path.parent / "archive"),
        "timezone": "Asia/Shanghai",
        "initial_since": since,
        "codex_home": str(codex),
        "source_roots": [str(codex / "sessions"), str(codex / "archived_sessions")],
        "projects": [], "thread_metadata": {}, "classification_overrides": {},
        "excluded_thread_ids": thread_ids(excluded_thread_ids or []),
        "delivery": {"enabled": False, "message_format": "interactive",
                     "codex_link_bridge": {"enabled": False, "port": 48871}},
    }
    if agent_export_dir is not None:
        config["agent_export_dir"] = str(agent_export_dir)
    config = normalize_config(config, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also prevents a racing initializer from replacing a profile.
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"ok": True, "config": str(path), "archive_dir": config["archive_dir"],
            "agent_export_dir": config["agent_export_dir"], "delivery_enabled": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "show", "set"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--agent-export-dir", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--initial-since")
    parser.add_argument("--exclude-thread", action="append", default=[])
    parser.add_argument('--include-thread', action='append', default=[])
    parser.add_argument('--exclude-current-thread', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == "init":
            if args.include_thread or args.exclude_current_thread:
                parser.error('--include-thread and --exclude-current-thread are supported only by set')
            # Refuse to shadow an existing legacy profile with a fresh baseline.
            path = resolve_config_path(args.config)
            result = initialize(path, args.archive_dir, args.agent_export_dir, args.codex_home,
                                args.initial_since, args.exclude_thread)
        elif args.command == 'set':
            if any((args.archive_dir, args.agent_export_dir, args.codex_home, args.initial_since)):
                parser.error('set only changes excluded threads; archive relocation follows configuration.md')
            result = set_exclusions(resolve_config_path(args.config), args.exclude_thread,
                                    args.include_thread, args.exclude_current_thread)
        else:
            if any((args.archive_dir, args.agent_export_dir, args.codex_home, args.initial_since,
                    args.exclude_thread, args.include_thread, args.exclude_current_thread)):
                parser.error("show accepts only --config")
            path = resolve_config_path(args.config)
            cfg = load_config(path)
            result = {"ok": True, "config": str(path), "archive_dir": cfg["archive_dir"],
                      "agent_export_dir": cfg["agent_export_dir"], "source_roots": cfg["source_roots"],
                      "excluded_thread_ids": cfg.get('excluded_thread_ids', []),
                      "current_thread_id": os.environ.get('CODEX_THREAD_ID') or None,
                      "timezone": cfg["timezone"], "delivery_enabled": cfg.get("delivery", {}).get("enabled", False)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
