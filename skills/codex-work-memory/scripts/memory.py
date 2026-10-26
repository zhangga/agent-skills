"""Durable run ledger and rebuildable views for codex-work-memory (stdlib only)."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

_presentation_spec = importlib.util.spec_from_file_location("codex_work_memory_presentation", Path(__file__).with_name("presentation.py"))
presentation = importlib.util.module_from_spec(_presentation_spec)
_presentation_spec.loader.exec_module(presentation)

_export_spec = importlib.util.spec_from_file_location("codex_work_memory_agent_export", Path(__file__).with_name("agent_export.py"))
agent_export = importlib.util.module_from_spec(_export_spec)
_export_spec.loader.exec_module(agent_export)

TZ = timezone(timedelta(hours=8))

def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def iso(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone is required")
    return dt

def display_time(value):
    return iso(value).astimezone(TZ).isoformat(timespec="seconds")

def digest(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)

def write_json(path, obj):
    write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

@contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".memory.lock"
    handle = path.open("a+b")
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
        except OSError as exc:
            raise RuntimeError("Another memory writer holds the lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

def runs(root):
    return sorted([read(p) for p in (root / "runs").glob("*.json")], key=lambda r: (r["created_at"], r["run_id"]))

def validate_details(details):
    if not isinstance(details, dict) or set(details) - {"context", "approach", "decisions", "validation", "artifacts", "limitations"}:
        raise ValueError("details must contain only supported detail fields")
    for key, value in details.items():
        if key == "context":
            valid = isinstance(value, str)
        elif key == "artifacts":
            valid = isinstance(value, list) and all(isinstance(a, dict) and set(a) == {"label", "path"} and all(isinstance(a[k], str) and a[k] for k in ("label", "path")) for a in value)
        else:
            valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
        if not valid:
            raise ValueError("Invalid details field: " + key)

def source_catalog(batch):
    catalog = {}
    for thread in batch["threads"]:
        times = [e["time"] for e in thread.get("events", []) if not e.get("context_only")]
        catalog[thread["thread_id"]] = {"title": thread.get("title", ""), "source_path": thread.get("source_path", ""), "first_event_at": min(times, key=iso) if times else None, "last_event_at": max(times, key=iso) if times else None, "events": {e["event_id"]: e["time"] for e in thread.get("events", []) if not e.get("context_only")}}
    return catalog

def merged_catalog(ledger):
    result = {}
    for run in ledger:
        for tid, metadata in run.get("source_catalog", {}).items():
            prior = result.get(tid, {})
            current = dict(prior, **metadata)
            for key in ("title", "source_path"):
                if not metadata.get(key):
                    current[key] = prior.get(key, "")
            current["events"] = dict(prior.get("events", {}), **metadata.get("events", {}))
            first = [v for v in (prior.get("first_event_at"), metadata.get("first_event_at")) if v]
            last = [v for v in (prior.get("last_event_at"), metadata.get("last_event_at")) if v]
            current["first_event_at"] = min(first, key=iso) if first else None
            current["last_event_at"] = max(last, key=iso) if last else None
            result[tid] = current
    return result

def resolved_runs(root):
    """Overlay immutable detail revisions without changing canonical run facts."""
    ledger = copy.deepcopy(runs(root))
    targets = {r["run_id"]: r for r in ledger}
    revisions = sorted((read(p) for p in (root / "enrichments").glob("*.json")), key=lambda r: (r["created_at"], r["run_id"]))
    for revision in revisions:
        target = targets.get(revision["target_run"])
        if target is None or revision["target_payload_hash"] != target["payload_hash"]:
            raise ValueError("Detail revision does not match its canonical run")
        for patch in revision["patches"]:
            record = target["records"][patch["record_index"]]
            record.setdefault("details", {}).update(patch["details"])
        target.setdefault("source_catalog", {}).update(revision.get("source_catalog", {}))
    return ledger

def enrichment(root, target_run, revision_id, records, batch=None):
    if not revision_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", revision_id):
        raise ValueError("Invalid revision ID")
    if not target_run or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", target_run):
        raise ValueError("Invalid target-run")
    target = read(root / "runs" / (target_run + ".json"))
    if target["mode"] == "weekly":
        raise ValueError("Only source record runs can be enriched")
    originals = {}
    for index, record in enumerate(target["records"]):
        originals.setdefault((record["item_id"], record["occurred_at"]), []).append(index)
    if not isinstance(records, list):
        raise ValueError("Enriched records must be an array")
    patches = []
    used = set()
    for record in records:
        key = (record.get("item_id"), record.get("occurred_at"))
        indices = originals.get(key, [])
        if len(indices) != 1 or key in used:
            raise ValueError("Enrichment must identify exactly one original record, without duplicates")
        index = indices[0]
        original = target["records"][index]
        if any(k != "details" and (k not in original or v != original[k]) for k, v in record.items()):
            raise ValueError("Enrichment cannot alter original record facts")
        if "details" not in record:
            raise ValueError("Enrichment requires details")
        validate_details(record["details"])
        used.add(key)
        patches.append({"record_index": index, "details": record["details"]})
    catalog = {}
    if batch is not None:
        if any(iso(batch["window"][k]) != iso(target["window"][k]) for k in ("since", "until")):
            raise ValueError("Source catalog batch must use the original run window")
        validate_records(target["records"], batch)
        if {t["thread_id"]: t["checkpoint"] for t in batch["threads"]} != target["checkpoints"]:
            raise ValueError("Source catalog batch must match original checkpoints")
        catalog = source_catalog(batch)
    revision = {"run_id": revision_id, "target_run": target_run, "target_payload_hash": target["payload_hash"], "patches": sorted(patches, key=lambda p: p["record_index"]), "source_catalog": catalog}
    revision["payload_hash"] = digest(revision)
    path = root / "enrichments" / (revision_id + ".json")
    existed = path.exists()
    if existed:
        if read(path)["payload_hash"] != revision["payload_hash"]:
            raise ValueError("revision ID already contains different content; use a new revision ID")
    else:
        revision["created_at"] = datetime.now(TZ).isoformat(timespec="microseconds")
        revision["schema_version"] = 1
        write_json(path, revision)
    # A committed revision is the durable recovery point. Derived views may be
    # safely regenerated on retry; collection state and sent briefs stay intact.
    rebuild(root, preserve_state=True, preserve_briefs=True)
    return {"ok": True, "already_saved": existed, "run_id": revision_id, "target_run": target_run, "records": len(patches), "index": str(root / "index.html")}

def item_text(item):
    result = ["### " + item["title"], "", f"{display_time(item['occurred_at'])} · {item['category']} · {item['status']}", "", item["summary"]]
    for key, label in [("outcome", "结果与验证"), ("follow_up", "后续"), ("knowledge", "可复用结论")]:
        if item.get(key):
            result += ["", f"**{label}：** {item[key]}"]
    result += ["", "来源：" + "；".join(f"`{s['thread_id']}`（{len(s['event_ids'])} 条事件）" for s in item["source_refs"]), ""]
    return "\n".join(result)

def grouped(items):
    groups = {}
    for item in items:
        groups.setdefault((item["project_id"], item["project_name"]), []).append(item)
    return groups

def render_report(run):
    return presentation.report_md(run)

def rebuild(root, preserve_state=False, preserve_briefs=False):
    ledger = resolved_runs(root)
    state = {"schema_version": 1, "sources": {}, "scan_success_until": None, "last_run_id": None, "runs": []}
    scans = [r for r in ledger if r["mode"] != "weekly"]
    # Advance only through contiguous complete windows. A later successful
    # manual backfill must not hide an earlier failed scan's uncovered range.
    if scans:
        state["coverage_since"] = min((r["window"]["since"] for r in scans), key=iso)
        covered = state["coverage_since"]
        for run in sorted(scans, key=lambda r: iso(r["window"]["since"])):
            if run.get("coverage", {}).get("complete_local") is True:
                start, end = run["window"]["since"], run["window"]["until"]
                if iso(start) <= iso(covered) < iso(end):
                    covered = end
                    state["scan_success_until"] = end
    all_items = []
    for run in ledger:
        state["runs"].append(run["run_id"])
        state["last_run_id"] = run["run_id"]
        if run["mode"] != "weekly":
            all_items.extend(run["records"])
            for tid, cp in run.get("checkpoints", {}).items():
                if cp.get("eligible", True) is False:
                    continue
                prior = state["sources"].get(tid, {})
                merged = dict(cp)
                merged["event_ids"] = sorted(set(prior.get("event_ids", [])) | set(cp.get("event_ids", [])))
                times = [x for x in [prior.get("last_event_at"), cp.get("last_event_at")] if x]
                merged["last_event_at"] = max(times, key=iso) if times else None
                state["sources"][tid] = merged
        report_path = root / ("weekly" if run["mode"] == "weekly" else "daily") / (run["run_id"] + ".md")
        write(report_path, run.get("report_text") or render_report(run))
        if not preserve_briefs:
            write(root / "briefs" / (run["run_id"] + ".md"), run["brief_text"])
    # Canonical records stay in runs/. Project views can always be rebuilt.
    groups = presentation.items_grouped(all_items)
    catalog = merged_catalog(ledger)
    total = sum(len(p["items"]) for p in groups.values())
    index = ["# Codex 工作记忆", "", "[打开可搜索的阅读版](index.html)", "", f"**{total} 个工作事项 · {len(groups)} 个分类 · {len(all_items)} 条进展记录**", "", "时间均为上海时间。每个事项先呈现最新状态，再保留历史进展与来源。", "", "## 项目", ""]
    for pid, project in groups.items():
        filename = presentation.project_filename(pid)
        count = sum(len(h) for h in project["items"].values())
        write(root / "projects" / filename, presentation.project_md(pid, project, catalog))
        index.append(f"- [{project['name']}](projects/{filename})：{len(project['items'])} 个事项 · {count} 条进展")
    index += ["", "## 运行报告", ""]
    for run in reversed(ledger):
        folder = "weekly" if run["mode"] == "weekly" else "daily"
        label = "周报" if run["mode"] == "weekly" else "详细工作记录"
        index.append(f"- [{label} · {presentation.when(run['window']['since'], True)} — {presentation.when(run['window']['until'], True)}]({folder}/{run['run_id']}.md) · {len(run.get('records', []))} 条进展")
    index += ["", "## 覆盖说明", "", "本档案仅反映实际读取到的本地会话。远程、纯云端及读取失败来源在各次报告中单独注明。", ""]
    write(root / "index.md", "\n".join(index))
    write(root / "index.html", presentation.html_index(all_items, catalog, ledger))
    if not preserve_state:
        write_json(root / "state.json", state)
    return state

def validate_records(records, batch):
    start, end = iso(batch["window"]["since"]), iso(batch["window"]["until"])
    if start >= end:
        raise ValueError("Invalid collection window")
    allowed = {}
    for thread in batch["threads"]:
        tid, checkpoint = thread["thread_id"], thread["checkpoint"]
        if tid in allowed:
            raise ValueError(f"Duplicate thread in batch: {tid}")
        events = [e for e in thread["events"] if not e.get("context_only")]
        event_ids = {e["event_id"] for e in events}
        if event_ids != set(checkpoint.get("event_ids", [])):
            raise ValueError(f"Checkpoint must match this batch's new events: {tid}")
        if any(not start <= iso(e["time"]) < end for e in events):
            raise ValueError(f"Checkpoint event is outside the scan window: {tid}")
        if events and (not checkpoint.get("last_event_at") or
                       iso(checkpoint["last_event_at"]) != max(iso(e["time"]) for e in events)):
            raise ValueError(f"Checkpoint time does not match its events: {tid}")
        eligible = checkpoint.get("eligible", True) is not False
        if not eligible and batch.get("coverage", {}).get("complete_local") is True:
            raise ValueError("Incomplete source cannot declare complete local coverage")
        allowed[tid] = event_ids if eligible else set()
    for item in records:
        if "details" in item:
            validate_details(item["details"])
        for key in ["item_id", "occurred_at", "project_id", "project_name", "category", "status", "title", "summary", "source_refs"]:
            if not item.get(key):
                raise ValueError(f"Missing record field: {key}")
        if not start <= iso(item["occurred_at"]) < end:
            raise ValueError("Record occurrence is outside the scan window")
        for ref in item["source_refs"]:
            if not ref.get("event_ids") or any(e not in allowed.get(ref["thread_id"], set()) for e in ref["event_ids"]):
                raise ValueError(f"Invalid or context-only source reference: {ref['thread_id']}")

def reject_processed_events(root, batch):
    processed = {}
    for run in runs(root):
        for tid, cp in run.get("checkpoints", {}).items():
            if cp.get("eligible", True) is not False:
                processed.setdefault(tid, set()).update(cp.get("event_ids", []))
    for thread in batch["threads"]:
        cp = thread["checkpoint"]
        if cp.get("eligible", True) is False:
            continue
        if processed.get(thread["thread_id"], set()).intersection(cp.get("event_ids", [])):
            raise ValueError("Batch contains already committed events; re-collect with current state, or retry the original run-id")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["commit", "enrich", "report", "export", "query", "rebuild", "status"])
    for name in ["config", "batch", "records", "brief", "mode", "run-id", "target-run", "since", "until", "report", "out"]:
        ap.add_argument("--" + name)
    args = ap.parse_args()
    config_spec = importlib.util.spec_from_file_location("codex_work_memory_configuration", Path(__file__).with_name("configuration.py"))
    configuration = importlib.util.module_from_spec(config_spec)
    config_spec.loader.exec_module(configuration)
    cfg = configuration.load_config(args.config)
    root = Path(cfg["archive_dir"])
    if args.command in {"status", "query"}:
        ledger = resolved_runs(root)
        if args.command == "status":
            result = {"runs": len(ledger), "state": read(root / "state.json") if (root / "state.json").exists() else None}
        else:
            start = iso(args.since) if args.since else datetime.min.replace(tzinfo=timezone.utc)
            end = iso(args.until) if args.until else datetime.max.replace(tzinfo=timezone.utc)
            result = {"records": [i for r in ledger if r["mode"] != "weekly" for i in r["records"] if start <= iso(i["occurred_at"]) < end], "source_catalog": merged_catalog(ledger), "coverage": [{"run_id": r["run_id"], "coverage": r.get("coverage", {})} for r in ledger if r["mode"] != "weekly" and iso(r["window"]["since"]) < end and iso(r["window"]["until"]) > start]}
        if args.out:
            write_json(args.out, result)
            print(json.dumps({"ok": True, "out": args.out}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return
    with lock(root):
        if args.command == "rebuild":
            state = rebuild(root)
            print(json.dumps({"ok": True, "runs": len(state["runs"])}))
            return
        if not args.run_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args.run_id):
            raise ValueError("run-id must be 1-100 letters, digits, underscores or hyphens")
        if args.command == "export":
            run = read(root / "runs" / (args.run_id + ".json"))
            result = agent_export.export_weekly(root, cfg["agent_export_dir"], run, resolved_runs(root))
            print(json.dumps({"ok": True, "run_id": args.run_id, "agent_export": result}, ensure_ascii=False))
            return
        if args.command == "enrich":
            result = enrichment(root, args.target_run, args.run_id, read(args.records), read(args.batch) if args.batch else None)
            print(json.dumps(result, ensure_ascii=False))
            return
        brief = Path(args.brief).read_text(encoding="utf-8-sig")
        if args.command == "commit":
            batch, records = read(args.batch), read(args.records)
            validate_records(records, batch)
            if args.mode not in {"daily", "bootstrap"}:
                raise ValueError("mode must be daily or bootstrap")
            run = {"run_id": args.run_id, "mode": args.mode, "window": batch["window"], "records": records, "coverage": batch["coverage"], "checkpoints": {t["thread_id"]: t["checkpoint"] for t in batch["threads"]}, "brief_text": brief, "source_catalog": source_catalog(batch)}
        else:
            if iso(args.since) >= iso(args.until):
                raise ValueError("Invalid weekly window")
            run = {"run_id": args.run_id, "mode": "weekly", "window": {"since": args.since, "until": args.until, "timezone": cfg.get("timezone", "Asia/Shanghai")}, "records": [], "brief_text": brief, "report_text": Path(args.report).read_text(encoding="utf-8-sig")}
        run["payload_hash"] = digest(run)
        path = root / "runs" / (args.run_id + ".json")
        existed = path.exists()
        if existed:
            old = read(path)
            if "source_catalog" not in old and args.command == "commit":
                # Retrying an original v1 run uses its original payload contract.
                run.pop("source_catalog", None)
                run.pop("payload_hash", None)
                run["payload_hash"] = digest(run)
            if old["payload_hash"] != run["payload_hash"]:
                raise ValueError("run-id already contains different content; use a new revision ID")
            run = old
        else:
            if args.command == "commit":
                reject_processed_events(root, batch)
            run["created_at"] = datetime.now(TZ).isoformat(timespec="seconds")
            run["schema_version"] = 1
            if args.command == "report":
                # Freeze evidence before the report becomes durable. If a later
                # export write fails, retrying cannot silently absorb new data.
                agent_export.freeze(root, run, resolved_runs(root), basis="report-save")
            write_json(path, run)
        state = rebuild(root)
        result = {"ok": True, "already_saved": existed, "run_id": args.run_id, "records": len(run["records"]), "brief": str(root / "briefs" / (args.run_id + ".md")), "index": str(root / "index.md"), "scan_success_until": state["scan_success_until"]}
        if args.command == "report":
            result["agent_export"] = agent_export.export_weekly(root, cfg["agent_export_dir"], run, resolved_runs(root))
        print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
