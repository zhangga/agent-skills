#!/usr/bin/env python3
"""Create a personal profile without changing existing archives or settings."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from configuration import absolute_path, default_codex_home, load_config, normalize_config, resolve_config_path


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
        "excluded_thread_ids": excluded_thread_ids or [],
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
    parser.add_argument("command", choices=["init", "show"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--agent-export-dir", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--initial-since")
    parser.add_argument("--exclude-thread", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "init":
            # Refuse to shadow an existing legacy profile with a fresh baseline.
            path = resolve_config_path(args.config)
            result = initialize(path, args.archive_dir, args.agent_export_dir, args.codex_home,
                                args.initial_since, args.exclude_thread)
        else:
            if any((args.archive_dir, args.agent_export_dir, args.codex_home, args.initial_since, args.exclude_thread)):
                parser.error("show accepts only --config")
            path = resolve_config_path(args.config)
            cfg = load_config(path)
            result = {"ok": True, "config": str(path), "archive_dir": cfg["archive_dir"],
                      "agent_export_dir": cfg["agent_export_dir"], "source_roots": cfg["source_roots"],
                      "timezone": cfg["timezone"], "delivery_enabled": cfg.get("delivery", {}).get("enabled", False)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
