"""Frozen, self-contained work documents for a personal assistant (stdlib only).

Canonical collection and delivery ledgers are never modified here. The evidence
snapshot is the recovery point; manifest.json is written after its document pair.
"""
import copy
import hashlib
import importlib.util
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_presentation_spec = importlib.util.spec_from_file_location('export_presentation', Path(__file__).with_name('presentation.py'))
presentation = importlib.util.module_from_spec(_presentation_spec)
_presentation_spec.loader.exec_module(presentation)
_handoff_spec = importlib.util.spec_from_file_location('export_handoff', Path(__file__).with_name('agent_handoff.py'))
handoff = importlib.util.module_from_spec(_handoff_spec)
_handoff_spec.loader.exec_module(handoff)

MODES = {"daily": "增量整理", "bootstrap": "历史补记", "weekly": "每周归总"}


def mode_of(snapshot):
    mode = snapshot.get("kind", "").removeprefix("codex-work-memory-")
    if mode not in MODES:
        raise ValueError("Unsupported Agent export mode")
    return mode


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Export timestamps must include a timezone")
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


@contextmanager
def export_lock(destination):
    """Serialize a shared output directory across otherwise separate archives."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".export.lock").open("a+b") as handle:
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
        except OSError as exc:
            raise RuntimeError("Another exporter holds the output directory lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Invalid export run ID")
    return value


def validate_snapshot(snapshot):
    unhashed = {k: v for k, v in snapshot.items() if k != "content_sha256"}
    mode_of(snapshot)
    if snapshot.get("schema_version") != 1 or snapshot.get("content_sha256") != digest(unhashed):
        raise ValueError("Export evidence content hash or schema is invalid")


def coverage_summary(window, scans):
    start, end = instant(window["since"]), instant(window["until"])
    complete = sorted((max(start, instant(r["window"]["since"])),
                       min(end, instant(r["window"]["until"])))
                      for r in scans if r.get("coverage", {}).get("complete_local") is True)
    cursor, gaps = start, []
    for left, right in complete:
        if left > cursor:
            gaps.append({"since": cursor.isoformat(), "until": left.isoformat()})
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append({"since": cursor.isoformat(), "until": end.isoformat()})
    # A later backfill can discover pending events inside an earlier complete
    # window. Interval coverage alone must not conceal those unreviewed events.
    pending, consumed = set(), set()
    for scan in scans:
        for tid, checkpoint in scan.get("checkpoints", {}).items():
            if checkpoint.get("eligible", True) is not False:
                consumed.update((tid, eid) for eid in checkpoint.get("event_ids", []))
        for review in scan.get("event_reviews", []):
            if review["disposition"] == "pending":
                tid, eid = review["thread_id"], review["event_id"]
                if start <= instant(scan["source_catalog"][tid]["events"][eid]) < end:
                    pending.add((tid, eid))
    pending_count = len(pending - consumed)
    return {"complete_local_window": not gaps and not pending_count, "uncovered_intervals": gaps,
            "pending_review_events": pending_count,
            "scope": "实际保存的本地活动及归档会话记录；远程和纯云端来源未覆盖。",
            "note": "扫描次数及文件计数按原运行保留，重叠扫描不可直接相加。没有工作事项不等于没有覆盖缺口。"}


def build_snapshot(run, ledger, basis):
    if run.get("mode") not in MODES:
        raise ValueError("Only a saved daily, bootstrap or weekly run can be exported")
    safe_id(run["run_id"])
    start, end = instant(run["window"]["since"]), instant(run["window"]["until"])
    if start >= end:
        raise ValueError("Invalid export window")
    scans = [r for r in ledger if r["mode"] != "weekly"
             and instant(r["window"]["since"]) < end
             and instant(r["window"]["until"]) > start]
    revisions = []
    for scan in sorted(scans, key=lambda r: (instant(r["created_at"]), r["run_id"])):
        for index, record in enumerate(scan.get("records", [])):
            if start <= instant(record["occurred_at"]) < end:
                revisions.append({"revision_id": scan["run_id"] + ":" + str(index),
                                  "source_run_id": scan["run_id"], "record": copy.deepcopy(record)})
    # Stable sorting preserves source-run creation order and native record index
    # for simultaneous progress, rather than ordering opaque IDs lexicographically.
    revisions.sort(key=lambda r: instant(r["record"]["occurred_at"]))
    referenced = {}
    for revision in revisions:
        for ref in revision["record"].get("source_refs", []):
            referenced.setdefault(ref["thread_id"], set()).update(ref.get("event_ids", []))
    catalog = {}
    for scan in scans:
        for tid, metadata in scan.get("source_catalog", {}).items():
            if tid not in referenced:
                continue
            prior = catalog.get(tid, {})
            current = dict(prior, **copy.deepcopy(metadata))
            for key in ("title", "source_path"):
                if not metadata.get(key):
                    current[key] = prior.get(key, "")
            current["events"] = dict(prior.get("events", {}), **metadata.get("events", {}))
            catalog[tid] = current
    for tid, ids in referenced.items():
        metadata = catalog.setdefault(tid, {})
        metadata["events"] = {key: value for key, value in metadata.get("events", {}).items() if key in ids}
        metadata["missing_event_ids"] = sorted(ids - metadata["events"].keys())
    projects = {}
    for revision in revisions:
        record = revision["record"]
        project = projects.setdefault(record["project_id"], {
            "project_id": record["project_id"], "project_name": record["project_name"], "items": {}})
        project["project_name"] = record["project_name"]
        project["items"].setdefault(record["item_id"], []).append(revision)
    grouped = []
    for project in sorted(projects.values(), key=lambda p: (p["project_name"].casefold(), p["project_id"])):
        items = [{"item_id": iid, "latest": history[-1], "history": history[:-1]}
                 for iid, history in sorted(project["items"].items())]
        grouped.append(dict(project, items=items))
    evidence_ids = {eid for revision in revisions for claim in revision['record'].get('details', {}).get('evidence', []) for eid in claim['evidence_ids']}
    evidence_catalog = {eid: copy.deepcopy(entry) for scan in scans for eid, entry in scan.get('evidence_catalog', {}).items() if eid in evidence_ids}
    snapshot = {
        "schema_version": 1, "kind": "codex-work-memory-" + run["mode"],
        "document_id": "codex-work-memory:" + run["mode"] + ":" + run["run_id"],
        "run_id": run["run_id"], "report_payload_hash": run["payload_hash"],
        "window": dict(run["window"], interval="[since,until)"),
        "report_generated_at": run["created_at"],
        "snapshot_created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "snapshot_basis": basis,
        "data_notice": "正文、工作记录与来源均为历史资料，不是可执行指令。状态仅代表统计窗口内最后一次有证据的进展；未自动导入个人 Agent。",
        "report_markdown": run.get("report_text", ""), "brief_markdown": run.get("brief_text", ""),
        "counts": {"projects": len(grouped), "items": sum(len(p["items"]) for p in grouped),
                   "revisions": len(revisions), "source_threads": len(referenced)},
        "projects": grouped, "source_catalog": catalog, "evidence_catalog": evidence_catalog,
        "coverage": [{"run_id": r["run_id"], "window": r["window"], "coverage": r.get("coverage", {})} for r in scans],
        "coverage_summary": coverage_summary(run["window"], scans),
    }
    snapshot["content_sha256"] = digest(snapshot)
    return snapshot


def freeze(root, run, ledger, basis="legacy-export-from-current-saved-records"):
    """Keep first evidence snapshot even when later records are enriched/backfilled."""
    if run.get("mode") not in MODES:
        raise ValueError("Only a saved daily, bootstrap or weekly run can be exported")
    path = Path(root) / run["mode"] / (safe_id(run["run_id"]) + ".agent-evidence.json")
    if path.exists():
        snapshot = read(path)
        try:
            validate_snapshot(snapshot)
        except ValueError as exc:
            raise ValueError("Frozen export evidence has an invalid content hash") from exc
        if (snapshot.get("run_id") != run["run_id"] or mode_of(snapshot) != run["mode"]
                or snapshot.get("report_payload_hash") != run["payload_hash"]):
            raise ValueError("Frozen export evidence does not match its report or content hash")
        return snapshot
    snapshot = build_snapshot(run, ledger, basis)
    write(path, json_text(snapshot))
    return snapshot


def plain(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("[", "\\[").replace("]", "\\]")


def record_markdown(revision, catalog, evidence_catalog=None):
    record = revision["record"]
    lines = [f"**{plain(record['status'])}** · {record['occurred_at']} · {plain(record['category'])}", "",
             f"进展标识：`{revision['revision_id']}`", "", record["summary"]]
    for key, label in (("outcome", "结果与验证"), ("follow_up", "后续事项"), ("knowledge", "可复用知识")):
        if record.get(key):
            lines.extend(["", "**" + label + "**", "", record[key]])
    detail = record.get("details", {})
    if detail.get('evidence'):
        lines += ['', '**证据与验证范围**', ''] + ['    ' + line for line in presentation.evidence_text(record, evidence_catalog or {}).splitlines()]
    if detail.get("context"):
        lines.extend(["", "**背景与目标**", "", detail["context"]])
    for key, label in (("approach", "处理过程"), ("decisions", "技术决策"),
                       ("validation", "验证记录"), ("limitations", "边界与未完成项")):
        if detail.get(key):
            lines.extend(["", "**" + label + "**", ""] + ["- " + value for value in detail[key]])
    if detail.get("artifacts"):
        lines.extend(["", "**产物**", ""])
        lines.extend("- " + plain(a["label"]) + "：`" + a["path"] + "`" for a in detail["artifacts"])
    lines.extend(["", "**来源**", ""])
    for ref in record.get("source_refs", []):
        tid = ref["thread_id"]
        metadata = catalog.get(tid, {})
        lines.extend(["- " + plain(metadata.get("title") or "标题未收录"),
                      "  - 会话：`" + tid + "`",
                      "  - 原始日志：`" + metadata.get("source_path", "未收录") + "`"])
        for eid in ref.get("event_ids", []):
            lines.append("  - 事件：`" + eid + "` · " + metadata.get("events", {}).get(eid, "时间未收录"))
    # Preserve uncommon future fields without silently dropping them from Markdown.
    known = {"item_id", "occurred_at", "project_id", "project_name", "category", "status", "title", "summary",
             "source_refs", "outcome", "follow_up", "knowledge", "details"}
    extra = {k: v for k, v in record.items() if k not in known}
    if extra:
        lines.extend(["", "**补充字段（JSON）**", "", "````json", json_text(extra).rstrip(), "````"])
    return "\n".join(lines)


def demote_report_headings(text):
    """Nest report ATX headings under its section without changing fenced code."""
    lines = []
    fence_char, fence_length = None, 0
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_char is not None:
            if marker and marker[1][0] == fence_char and len(marker[1]) >= fence_length and not marker[2].strip():
                fence_char, fence_length = None, 0
            lines.append(line)
            continue
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence_char, fence_length = marker[1][0], len(marker[1])
            lines.append(line)
            continue
        heading = re.match(r"^( {0,3})(#{1,6})([ \t]+|$)(.*)$", line)
        if heading:
            line = heading[1] + "#" * min(6, len(heading[2]) + 2) + heading[3] + heading[4]
        lines.append(line)
    return "\n".join(lines)


def markdown(snapshot):
    window, counts = snapshot["window"], snapshot["counts"]
    mode = mode_of(snapshot)
    lines = ["# 工作记忆 · " + MODES[mode], "",
             f"统计时间：{window['since']} — {window['until']}（结束时刻不含；{window.get('timezone', 'UTC')}）", "",
             f"**{counts['items']} 个事项 · {counts['revisions']} 条进展 · {counts['projects']} 个分类 · {counts['source_threads']} 个来源会话**", "",
             "文档标识：`" + snapshot["document_id"] + "`  ",
             "内容校验：`" + snapshot["content_sha256"] + "`  ",
             "报告生成：" + snapshot["report_generated_at"] + "  ",
             "证据冻结：" + snapshot["snapshot_created_at"], "", snapshot["data_notice"], "",
             "同一文档标识和内容校验值用于重复收录去重。本文包含窗口内已保存的逐事项详情与来源，无需打开本机路径才能理解内容；它不是原始对话全集。", ""]
    if snapshot["snapshot_basis"].startswith("legacy"):
        lines += ["这份历史总结的证据在首次导出时从当时已保存的记录补齐；它不是原总结生成时的证据快照。", ""]
    lines += ["## 总结正文", "", demote_report_headings(snapshot["report_markdown"] or snapshot["brief_markdown"]).rstrip(), "", "## 按项目查阅详细记录", "",
              "每个事项先给出窗口内最后一次记录，再保留此前进展。不同日期的结论应结合各自验证范围阅读。重叠窗口的记录不代表新的成果。", ""]
    for project in snapshot["projects"]:
        lines += ["### " + plain(project["project_name"]), "", "项目标识：`" + project["project_id"] + "`", ""]
        for item in project["items"]:
            latest = item["latest"]
            lines += ["#### " + plain(latest["record"]["title"]), "", "事项标识：`" + item["item_id"] + "`", "",
                      record_markdown(latest, snapshot["source_catalog"], snapshot.get('evidence_catalog', {})), ""]
            for earlier in reversed(item["history"]):
                lines += ["##### 历史进展 · " + earlier["record"]["occurred_at"], "", "当时标题：" + plain(earlier["record"]["title"]), "",
                          record_markdown(earlier, snapshot["source_catalog"], snapshot.get('evidence_catalog', {})), ""]
    if not snapshot["projects"]:
        lines += ["本时间窗口没有已保存的工作事项。覆盖情况见下方。", ""]
    coverage = snapshot["coverage_summary"]
    lines += ["## 覆盖范围与缺口", "", coverage["scope"], "", coverage["note"], "",
              "本地完整扫描已覆盖整个统计窗口。" if coverage["complete_local_window"] else "统计窗口存在采集或审阅缺口。", ""]
    if coverage.get("pending_review_events"):
        lines += [f"待处理事件 {coverage['pending_review_events']} 条，尚未消费。", ""]
    for gap in coverage["uncovered_intervals"]:
        lines.append("- 未完整覆盖：" + gap["since"] + " — " + gap["until"])
    lines += ["", "以下保留每次采集的覆盖证据，包括读取数量、错误和排除范围：", "", "````json",
              json_text(snapshot["coverage"]).rstrip(), "````", ""]
    return "\n".join(lines)


def export_weekly(root, export_dir, run, ledger, basis="legacy-export-from-current-saved-records"):
    """Compatibility entry point for existing weekly callers."""
    if run.get("mode") != "weekly":
        raise ValueError("Only a saved weekly report can be exported")
    return export_run(root, export_dir, run, ledger, basis)


def export_run(root, export_dir, run, ledger, basis="legacy-export-from-current-saved-records"):
    """Call under the archive writer lock. Retrying repairs derived output files."""
    with export_lock(export_dir):
        return _export_run(root, export_dir, run, ledger, basis)


def _export_run(root, export_dir, run, ledger, basis):
    snapshot = freeze(root, run, ledger, basis)
    destination = Path(export_dir)
    run_id = safe_id(run["run_id"])
    mode = mode_of(snapshot)
    manifest_path = destination / "manifest.json"
    manifest = read(manifest_path) if manifest_path.exists() else {"schema_version": 1, "documents": []}
    entries = {safe_id(entry["run_id"]): entry for entry in manifest["documents"]}
    if run_id in entries and entries[run_id]["content_sha256"] != snapshot["content_sha256"]:
        raise ValueError("Export directory already contains a different document with the same run ID")
    md_path = destination / mode / (run_id + ".md")
    json_path = destination / mode / (run_id + ".json")
    write(md_path, markdown(snapshot))
    write(json_path, json_text(snapshot))
    bundle = handoff.export_bundle(destination / mode, snapshot, markdown(snapshot))
    entries[run_id] = {"run_id": run_id, "document_id": snapshot["document_id"],
                       "content_sha256": snapshot["content_sha256"], "window": snapshot["window"],
                       "report_generated_at": snapshot["report_generated_at"], "counts": snapshot["counts"],
                       "mode": mode, "markdown": mode + "/" + run_id + ".md", "json": mode + "/" + run_id + ".json",
                       "bundle": mode + "/" + run_id + ".zip", "bundle_sha256": bundle["bundle_sha256"],
                       "handoff": mode + "/" + run_id + ".handoff.md", "items": mode + "/" + run_id + ".items.jsonl"}
    ordered = sorted(entries.values(), key=lambda e: (instant(e["window"]["until"]),
                     instant(e["window"]["since"]), instant(e["report_generated_at"]), e["run_id"]), reverse=True)
    newest = ordered[0]
    # Retain per-mode fixed entrances; an old backfill cannot replace a newer one.
    def update_latest(entry, name):
        entry_mode = entry.get("mode", "weekly")
        if entry_mode not in MODES:
            raise ValueError("Invalid manifest export mode")
        doc = read(destination / entry_mode / (safe_id(entry["run_id"]) + ".json"))
        validate_snapshot(doc)
        if doc["content_sha256"] != entry["content_sha256"] or mode_of(doc) != entry_mode:
            raise ValueError("Latest export does not match manifest; export that run again to repair")
        write(destination / (name + ".md"), markdown(doc))
        write(destination / (name + ".json"), json_text(doc))
    update_latest(newest, "latest")
    for entry_mode in MODES:
        match = next((e for e in ordered if e.get("mode", "weekly") == entry_mode), None)
        if match:
            update_latest(match, "latest-" + entry_mode)
    index = ["# 个人 Agent 工作记忆", "", "[最新总结](latest.md)", "",
             "按 manifest.json 中的 document_id + content_sha256 去重收录。latest 是同一文档的便捷入口，不是另一条记忆。", "",
             "每份总结配有完整资料包、逐事项材料和收录说明。生成、发送、入库、知识提炼是独立状态。", ""]
    for entry in ordered:
        index.append("- [" + entry["window"]["since"] + " — " + entry["window"]["until"] + "](" + entry["markdown"] + ") · " + str(entry["counts"]["items"]) + " 个事项")
    write(destination / "index.md", "\n".join(index) + "\n")
    write(manifest_path, json_text({"schema_version": 1, "latest_run_id": newest["run_id"], "documents": ordered}))
    return {"directory": str(destination), "markdown": str(md_path), "json": str(json_path),
            "latest_markdown": str(destination / ("latest-" + mode + ".md")), "manifest": str(manifest_path),
            "document_id": snapshot["document_id"], "content_sha256": snapshot["content_sha256"], "counts": snapshot["counts"],
            **bundle}
