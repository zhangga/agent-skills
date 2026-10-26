#!/usr/bin/env python3
"""Read local Codex rollouts into a bounded, resumable evidence bundle.

Only --out is written. The caller commits eligible checkpoints *after* saving
work records. Rollout text is untrusted data, never executable instructions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import ntpath
import posixpath
import os
from pathlib import Path
import re
import sqlite3
import sys

UTC = timezone.utc

_config_spec = importlib.util.spec_from_file_location("codex_work_memory_configuration", Path(__file__).with_name("configuration.py"))
configuration = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(configuration)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def instant(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps require an explicit UTC offset")
    return parsed.astimezone(UTC)


def read_json(path: Path):
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def windows_path(value: str) -> str:
    value = str(value or "").replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value)).rstrip("\\")


def project_path(value: str) -> str:
    """Recognize the source path syntax; POSIX paths remain case-sensitive."""
    value = str(value or "")
    if not value:
        return ""
    if ntpath.splitdrive(value)[0] or "\\" in value:
        return windows_path(value).replace("\\", "/")
    return posixpath.normpath(value).rstrip("/") or "/"


def is_subagent(metadata: dict) -> bool:
    source = metadata.get("source", "")
    if isinstance(source, str) and source.startswith("{"):
        try:
            source = json.loads(source)
        except ValueError:
            pass
    return bool((isinstance(source, dict) and "subagent" in source)
                or metadata.get("agent_path")
                or metadata.get("parent_thread_id"))


def message_text(payload: dict) -> str:
    content = payload.get("content", [])
    if isinstance(content, str):
        return content.strip()
    return "\n".join(part.get("text", "") for part in content
                     if isinstance(part, dict) and part.get("type") in
                     ("input_text", "output_text", "text")).strip()


def real_user_text(text: str) -> str:
    # Codex stores injected context as role=user in some rollout versions.
    if text.startswith("# AGENTS.md instructions for "):
        text = re.sub(r"^# AGENTS\.md instructions for .*?</INSTRUCTIONS>\s*",
                      "", text, count=1, flags=re.S)
        if text.startswith("# AGENTS.md instructions for "):
            return ""
    for tag in ("environment_context", "external_codex_apps_open_page"):
        text = re.sub(rf"<{tag}>.*?</{tag}>\s*", "", text, flags=re.S)
    if text.lstrip().startswith("<send_user_message_question_reply>"):
        match = re.search(r"<send_user_message_question_reply>(.*?)</send_user_message_question_reply>", text, re.S)
        if match:
            try:
                answers = json.loads(match.group(1))
                text = "\n".join(f"Question: {a.get('question', '')}\nAnswer: {a.get('answer', '')}"
                                 for a in answers if isinstance(a, dict))
            except (ValueError, TypeError):
                pass
    return text.strip()


def event(thread_id: str, row: dict, payload: dict, role: str,
          phase: str, text: str, turn_id: str | None) -> dict:
    stamp = instant(row.get("timestamp") or payload.get("completed_at"))
    # Content hash also detects a correction that retains the original message ID.
    native = payload.get("id") or payload.get("message_id")
    key = str(native) if native else "|".join((thread_id, iso(stamp), role, turn_id or ""))
    return {"event_id": "evt_" + digest(key + "|" + role + "|" + text),
            "time": iso(stamp), "role": role, "phase": phase, "text": text,
            "turn_id": turn_id, "_native_id": native}


def load_metadata(roots: list[Path], errors: list[dict]):
    database, index = {}, {}
    parents = {p.parent for p in roots}
    for base in sorted(parents):
        db = base / "state_5.sqlite"
        if db.exists():
            try:
                with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
                    conn.execute("PRAGMA query_only=ON")
                    conn.row_factory = sqlite3.Row
                    available = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
                    columns = [x for x in ("id", "title", "cwd", "source", "agent_path",
                               "project_id", "archived", "rollout_path", "updated_at") if x in available]
                    if "id" in columns:
                        for row in conn.execute("SELECT " + ",".join(columns) + " FROM threads"):
                            database[row["id"]] = dict(row)
            except (OSError, sqlite3.Error) as exc:
                errors.append({"kind": "metadata_read", "path": str(db), "error": str(exc)})
        source_index = base / "session_index.jsonl"
        if source_index.exists():
            try:
                with source_index.open(encoding="utf-8-sig") as handle:
                    for number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            if row.get("id") and row.get("thread_name"):
                                index[row["id"]] = row["thread_name"]
                        except ValueError as exc:
                            errors.append({"kind": "metadata_parse", "path": str(source_index),
                                           "line": number, "error": str(exc)})
            except OSError as exc:
                errors.append({"kind": "metadata_read", "path": str(source_index), "error": str(exc)})
    return database, index


def classify(metadata: dict, db: dict, cwd: str, projects: list[dict], override=None):
    by_id = {p["projectId"]: p for p in projects if p.get("projectId")}
    if override is not None:
        override = {"projectId": override} if isinstance(override, str) else override
        if not isinstance(override, dict) or "projectId" not in override:
            raise ValueError("classification_overrides entries require projectId (or a project-ID string)")
        project_id = override.get("projectId")
        project = by_id.get(project_id, {})
        return project_id, override.get("label") or project.get("label") or project.get("name") or project_id or "待归类", "classification_override"
    project_id = metadata.get("projectId") or db.get("project_id")
    if project_id:
        project = by_id.get(project_id, {})
        return project_id, project.get("label") or project.get("name") or project_id, (
            "thread_metadata.projectId" if metadata.get("projectId") else "sqlite.project_id")
    normalized = project_path(cwd)
    matches = [p for p in projects if p.get("path") and p.get("hostId", "local") in (None, "local")
               and (normalized == project_path(p["path"])
                    or normalized.startswith(project_path(p["path"]).rstrip("/") + "/"))]
    if matches:
        project = max(matches, key=lambda p: len(project_path(p["path"])))
        return project.get("projectId"), project.get("label") or project.get("name"), "cwd_longest_path"
    return None, "待归类", "unclassified"


def read_rollout(path: Path):
    errors, meta, events, completions, unphased = [], {}, [], [], []
    current_turn = None
    try:
        # Bound the read to its initial length. Appends belong to the next scan.
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            raw = handle.read(size)
        fingerprint = hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        return {}, [], [{"kind": "source_read", "path": str(path), "error": str(exc)}], None
    if len(raw) != size:
        errors.append({"kind": "source_changed", "path": str(path), "error": "Source shrank during read"})
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                continue
            kind = row.get("type")
            if kind == "session_meta":
                meta.update(payload)
                continue
            if kind in ("turn_context", "event_msg") and payload.get("turn_id"):
                current_turn = payload["turn_id"]
            thread_id = meta.get("id") or meta.get("session_id")
            if not thread_id:
                continue
            if kind == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                phase = payload.get("phase")
                if role not in ("user", "assistant"):
                    continue
                text = message_text(payload)
                if role == "user":
                    text = real_user_text(text)
                    if text:
                        events.append(event(thread_id, row, payload, role, "user", text, current_turn))
                elif text and phase in ("final", "final_answer"):
                    events.append(event(thread_id, row, payload, role, "final", text, current_turn))
                elif text and phase is None:
                    unphased.append(event(thread_id, row, payload, role, "final", text, current_turn))
            elif kind == "event_msg" and payload.get("type") == "task_complete":
                text = payload.get("last_agent_message")
                if isinstance(text, str) and text.strip():
                    completions.append(event(thread_id, row, payload, "assistant", "completion",
                                             text.strip(), current_turn))
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            errors.append({"kind": "source_parse", "path": str(path), "line": number, "error": str(exc)})
    # A completion repeats the final response; use it only when final phase is absent.
    final_pairs = {(e.get("turn_id"), e["text"]) for e in events if e["role"] == "assistant"}
    for item in completions:
        pair = (item.get("turn_id"), item["text"])
        if pair in final_pairs:
            continue
        matched = next((e for e in unphased if (e.get("turn_id"), e["text"]) == pair), None)
        events.append(matched or item)
        final_pairs.add(pair)
    unique = {e["event_id"]: e for e in events}
    for item in unique.values():
        item.pop("_native_id", None)
    return meta, sorted(unique.values(), key=lambda e: (instant(e["time"]), e["event_id"])), errors, fingerprint


def collect(config: dict, state: dict, since: datetime, until: datetime) -> dict:
    errors, threads, files = [], [], {}
    counts = Counter({key: 0 for key in (
        "files_enumerated", "files_read", "files_with_errors",
        "local_top_level_sources", "eligible_events_in_window",
        "already_processed_events", "new_events", "context_events",
        "subagent_threads_skipped", "excluded_threads", "remote_threads_skipped",
        "threads_with_new_events", "missing_recent_sources", "metadata_threads")})
    roots = [Path(p).expanduser().resolve() for p in config.get("source_roots", [])]
    if not roots:
        raise ValueError("config.source_roots must name local sessions and archived_sessions directories")
    for root in roots:
        if not root.is_dir():
            errors.append({"kind": "missing_source_root", "path": str(root)})
            continue
        def failed(exc):
            errors.append({"kind": "enumeration", "path": str(getattr(exc, "filename", root)), "error": str(exc)})
        for folder, _, names in os.walk(root, onerror=failed, followlinks=False):
            for name in names:
                if name.lower().endswith(".jsonl"):
                    path = Path(folder) / name
                    files[str(path.resolve())] = path
    db, index = load_metadata(roots, errors)
    excluded = set(config.get("excluded_thread_ids", []))
    known_metadata = config.get("thread_metadata", {})
    observed_ids = set()
    for path in sorted(files.values()):
        counts["files_enumerated"] += 1
        meta, events, source_errors, fingerprint = read_rollout(path)
        errors.extend(source_errors)
        counts["files_read"] += int(fingerprint is not None)
        counts["files_with_errors"] += bool(source_errors)
        thread_id = meta.get("id") or meta.get("session_id")
        if not thread_id:
            errors.append({"kind": "missing_thread_id", "path": str(path)})
            continue
        observed_ids.add(thread_id)
        db_meta = db.get(thread_id, {})
        configured = known_metadata.get(thread_id, {})
        if thread_id in excluded:
            counts["excluded_threads"] += 1
            continue
        if is_subagent(meta) or is_subagent(db_meta):
            counts["subagent_threads_skipped"] += 1
            continue
        if configured.get("hostId", "local") not in (None, "local"):
            counts["remote_threads_skipped"] += 1
            continue
        counts["local_top_level_sources"] += 1
        old = state.get("sources", {}).get(thread_id, {})
        processed = set(old.get("event_ids", []))
        in_window = [e for e in events if since <= instant(e["time"]) < until]
        counts["eligible_events_in_window"] += len(in_window)
        new = [e for e in in_window if e["event_id"] not in processed]
        counts["already_processed_events"] += len(in_window) - len(new)
        if not new:
            continue
        # Only the last two prior events, capped at 12k characters, supply context.
        before = [e for e in events if instant(e["time"]) < instant(new[0]["time"])][-2:]
        context = []
        remaining = 12000
        for previous in reversed(before):
            snippet = dict(previous, context_only=True)
            if len(snippet["text"]) > remaining:
                snippet["text"] = snippet["text"][-remaining:]
                snippet["context_truncated"] = True
            context.insert(0, snippet)
            remaining -= len(snippet["text"])
            if remaining <= 0:
                break
        cwd = meta.get("cwd") or db_meta.get("cwd", "")
        project_id, project_name, basis = classify(configured, db_meta, cwd, config.get("projects", []),
                                                  config.get("classification_overrides", {}).get(thread_id))
        title = configured.get("title") or index.get(thread_id) or db_meta.get("title") or thread_id
        archived = "archived_sessions" in path.parts or bool(db_meta.get("archived"))
        threads.append({"thread_id": thread_id, "title": title,
                        "project_id": project_id, "project_name": project_name,
                        "classification_basis": basis, "cwd": cwd, "source_path": str(path),
                        "archived": archived, "host_id": "local", "events": context + new,
                        "checkpoint": {"eligible": not source_errors,
                                       "event_ids": [e["event_id"] for e in new],
                                       "last_event_at": iso(max(instant(e["time"]) for e in new)),
                                       "source_path": str(path), "source_fingerprint": fingerprint}})
        counts["new_events"] += len(new)
        counts["context_events"] += len(context)
    missing = []
    for thread_id, metadata in db.items():
        if thread_id in observed_ids or thread_id in excluded or is_subagent(metadata):
            continue
        configured = known_metadata.get(thread_id, {})
        if configured.get("hostId", "local") not in (None, "local"):
            continue
        try:
            recent = since <= instant(metadata.get("updated_at", 0))
        except (ValueError, OverflowError):
            recent = False
        if recent:
            item = {"kind": "metadata_source_unavailable", "thread_id": thread_id,
                    "path": metadata.get("rollout_path"),
                    "title": configured.get("title") or index.get(thread_id) or metadata.get("title")}
            errors.append(item)
            missing.append(thread_id)
    counts["threads_with_new_events"] = len(threads)
    counts["missing_recent_sources"] = len(missing)
    counts["metadata_threads"] = len(db)
    remote = [k for k, v in known_metadata.items() if v.get("hostId", "local") not in (None, "local")]
    return {"schema_version": 1, "window": {"since": iso(since), "until": iso(until),
            "timezone": config.get("timezone", "Asia/Shanghai"), "interval": "[since,until)"},
            "coverage": {"complete_local": not errors, "source_scope": "local active + archived sessions",
                         "source_roots": [str(p) for p in roots], "errors": errors,
                         "counts": dict(counts), "known_remote_thread_ids_excluded": remote,
                         "limitations": ["Remote/cloud conversations are outside this local-file scan.",
                                         "Only saved user messages and final/completion responses are collected; tools and reasoning are excluded.",
                                         "Deleted local rollouts cannot be reconstructed; recent missing metadata sources are reported.",
                                         "Default incremental scans revisit 48 hours; older timestamped edits or newly discovered history require an explicit --since backfill.",
                                         "All source files are enumerated regardless of creation-directory date."]},
            "threads": sorted(threads, key=lambda t: (t["project_name"] or "", t["title"]))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.config = configuration.resolve_config_path(args.config)
    config = configuration.load_config(args.config)
    if args.state is None:
        args.state = Path(config["archive_dir"]) / "state.json"
    state = read_json(args.state) if args.state and args.state.exists() else {}
    until = instant(args.until) if args.until else datetime.now(UTC)
    initial = instant(config["initial_since"]) if config.get("initial_since") else None
    if args.since:
        since = instant(args.since)
    elif state.get("scan_success_until"):
        since = instant(state["scan_success_until"]) - timedelta(hours=48)
        if initial:
            since = max(since, initial)
    elif initial:
        since = initial
    else:
        parser.error("Supply --since or config.initial_since for the first scan")
    if since >= until:
        parser.error("--since must precede --until")
    out = args.out.resolve()
    protected = {args.config.resolve()}
    if args.state:
        protected.add(args.state.resolve())
    if out in protected or any(out.is_relative_to(Path(p).expanduser().resolve()) for p in config.get("source_roots", [])):
        parser.error("--out must not overwrite config/state or write into source roots")
    result = collect(config, state, since, until)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, out)
    print(json.dumps({"out": str(out), "complete_local": result["coverage"]["complete_local"],
                      "counts": result["coverage"]["counts"], "errors": len(result["coverage"]["errors"])}, ensure_ascii=True))
    return 0 if result["coverage"]["complete_local"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"collect: {exc}", file=sys.stderr)
        sys.exit(1)
