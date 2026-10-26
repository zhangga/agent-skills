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

_retrieval_spec = importlib.util.spec_from_file_location("codex_work_memory_retrieval", Path(__file__).with_name("retrieval.py"))
retrieval = importlib.util.module_from_spec(_retrieval_spec)
_retrieval_spec.loader.exec_module(retrieval)

_evidence_spec = importlib.util.spec_from_file_location("codex_work_memory_evidence", Path(__file__).with_name("evidence.py"))
evidence = importlib.util.module_from_spec(_evidence_spec)
_evidence_spec.loader.exec_module(evidence)

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
    return sorted([read(p) for p in (root / "runs").glob("*.json")], key=lambda r: (iso(r["created_at"]), r["run_id"]))

def validate_details(details):
    if not isinstance(details, dict) or set(details) - {"context", "approach", "decisions", "validation", "artifacts", "limitations", "evidence"}:
        raise ValueError("details must contain only supported detail fields")
    for key, value in details.items():
        if key == 'evidence':
            evidence.validate_claims(value)
            valid = True
        elif key == "context":
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

def item_edges(root):
    """Replay explicit association corrections; raw records remain immutable."""
    links = sorted((read(p) for p in (root / 'item_links').glob('*.json')),
                   key=lambda entry: (iso(entry['created_at']), entry['run_id']))
    edges = {}
    for entry in links:
        payload = {k: v for k, v in entry.items() if k not in {'created_at', 'schema_version', 'payload_hash'}}
        if entry['payload_hash'] != digest(payload):
            raise ValueError('Item association content hash is invalid')
        key = entry['project_id'], entry['item_id']
        if entry['target_item_id'] is None:
            edges.pop(key, None)
        else:
            edges[key] = entry['target_item_id']
    return edges

def item_aliases(root):
    return resolve_aliases(item_edges(root))

def resolve_aliases(edges):
    aliases = {}
    for project_id, item_id in edges:
        seen, target = set(), item_id
        while (project_id, target) in edges:
            if target in seen:
                raise ValueError('Item associations cannot contain a cycle')
            seen.add(target)
            target = edges[project_id, target]
        aliases[project_id, item_id] = target
    return aliases

def associate(root, run_id, project_id, item_id, target_item_id, reason):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id or ''):
        raise ValueError('Invalid association run-id')
    if not all(isinstance(v, str) and v.strip() for v in (project_id, item_id, reason)):
        raise ValueError('Association requires project ID, item ID and reason')
    if target_item_id is not None and (not isinstance(target_item_id, str) or not target_item_id.strip()):
        raise ValueError('Target item ID must be non-empty')
    payload = {'run_id': run_id, 'project_id': project_id, 'item_id': item_id,
               'target_item_id': target_item_id, 'reason': reason.strip()}
    payload['payload_hash'] = digest(payload)
    path = root / 'item_links' / (run_id + '.json')
    existed = path.exists()
    if existed:
        if read(path)['payload_hash'] != payload['payload_hash']:
            raise ValueError('Association run-id already contains different content')
    else:
        known = {(r['project_id'], r['item_id']) for run in runs(root) if run['mode'] != 'weekly' for r in run['records']}
        if (project_id, item_id) not in known or (target_item_id is not None and (project_id, target_item_id) not in known):
            raise ValueError('Both item IDs must exist in the same exact project ID')
        # Keep direct edges so unlinking one alias can split it back out later.
        edges = item_edges(root)
        resolve_aliases(edges)  # Validate the journal before extending it.
        key = project_id, item_id
        if target_item_id is None:
            if key not in edges:
                raise ValueError('This item has no direct association to unlink')
            edges.pop(key)
        else:
            edges[key] = target_item_id
        resolve_aliases(edges)
        payload.update(created_at=datetime.now(TZ).isoformat(timespec='microseconds'), schema_version=1)
        write_json(path, payload)
    rebuild(root, preserve_state=True, preserve_briefs=True)
    return {'ok': True, 'already_saved': existed, 'run_id': run_id,
            'project_id': project_id, 'item_id': item_id,
            'canonical_item_id': item_aliases(root).get((project_id, item_id), item_id),
            'index': str(root / 'index.html')}

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
        target.setdefault('evidence_catalog', {}).update(revision.get('evidence_catalog', {}))
    aliases = item_aliases(root)
    for run in ledger:
        for record in run['records']:
            target = aliases.get((record['project_id'], record['item_id']))
            if target is not None:
                record['original_item_id'] = record['item_id']
                record['item_id'] = target
    return ledger

def enrichment(root, target_run, revision_id, records, batch=None, evidence_bundle=None):
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
        if {t["thread_id"]: t["checkpoint"] for t in batch["threads"]} != target.get("batch_checkpoints", target["checkpoints"]):
            raise ValueError("Source catalog batch must match original checkpoints")
        catalog = source_catalog(batch)
    evidence_records = [dict(target['records'][p['record_index']], details=p['details']) for p in patches if p['details'].get('evidence')]
    if evidence_records and batch is None:
        raise ValueError('Evidence enrichment requires the original batch')
    evidence_catalog = evidence.bind(evidence_records, batch, evidence_bundle,
                                     verify_source=not (root / 'enrichments' / (revision_id + '.json')).exists())
    revision = {"run_id": revision_id, "target_run": target_run, "target_payload_hash": target["payload_hash"], "patches": sorted(patches, key=lambda p: p["record_index"]), "source_catalog": catalog}
    if evidence_catalog:
        revision['evidence_catalog'] = evidence_catalog
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
    # Older complete scans also prove that their configured roots existed.
    state['source_roots_seen'] = sorted({path for run in scans
        for path in run.get('coverage', {}).get('source_roots_present',
            run.get('coverage', {}).get('source_roots', [])
            if run.get('coverage', {}).get('collection_complete_local', run.get('coverage', {}).get('complete_local')) else [])})
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
    pending = {}
    for run in ledger:
        state["runs"].append(run["run_id"])
        state["last_run_id"] = run["run_id"]
        if run["mode"] != "weekly":
            all_items.extend(run["records"])
            for review in run.get("event_reviews", []):
                if review["disposition"] == "pending":
                    tid, eid = review["thread_id"], review["event_id"]
                    pending[tid, eid] = dict(review, time=run["source_catalog"][tid]["events"][eid])
            for tid, cp in run.get("checkpoints", {}).items():
                if cp.get("eligible", True) is False:
                    continue
                prior = state["sources"].get(tid, {})
                merged = dict(cp)
                merged["event_ids"] = sorted(set(prior.get("event_ids", [])) | set(cp.get("event_ids", [])))
                if 'message_signatures' in prior or 'message_signatures' in cp:
                    merged['message_signatures'] = sorted(set(prior.get('message_signatures', []))
                                                          | set(cp.get('message_signatures', [])))
                times = [x for x in [prior.get("last_event_at"), cp.get("last_event_at")] if x]
                merged["last_event_at"] = max(times, key=iso) if times else None
                state["sources"][tid] = merged
        report_path = root / ("weekly" if run["mode"] == "weekly" else "daily") / (run["run_id"] + ".md")
        write(report_path, run.get("report_text") or render_report(run))
        if not preserve_briefs:
            write(root / "briefs" / (run["run_id"] + ".md"), run["brief_text"])
    state["pending_reviews"] = sorted(
        (review for (tid, eid), review in pending.items()
         if eid not in set(state["sources"].get(tid, {}).get("event_ids", []))),
        key=lambda review: (iso(review["time"]), review["thread_id"], review["event_id"]))
    state["pending_review_since"] = state["pending_reviews"][0]["time"] if state["pending_reviews"] else None
    if state["pending_review_since"] and state["scan_success_until"]:
        state["scan_success_until"] = min(state["scan_success_until"], state["pending_review_since"], key=iso)
    # Canonical records stay in runs/. Project views can always be rebuilt.
    groups = presentation.items_grouped(all_items)
    catalog = merged_catalog(ledger)
    evidence_catalog = {eid: entry for run in ledger for eid, entry in run.get('evidence_catalog', {}).items()}
    total = sum(len(p["items"]) for p in groups.values())
    index = ["# Codex 工作记忆", "", "[打开可搜索的阅读版](index.html)", "", f"**{total} 个工作事项 · {len(groups)} 个分类 · {len(all_items)} 条进展记录**", "", "时间均为上海时间。每个事项先呈现最新状态，再保留历史进展与来源。", "", "## 项目", ""]
    for pid, project in groups.items():
        filename = presentation.project_filename(pid)
        count = sum(len(h) for h in project["items"].values())
        write(root / "projects" / filename, presentation.project_md(pid, project, catalog, evidence_catalog=evidence_catalog))
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

def reviewed_checkpoints(records, batch, reviews):
    """Account for every eligible event; only recorded/ignored events are consumed."""
    if not isinstance(reviews, list):
        raise ValueError("Review must be an array of event dispositions")
    events = {(t["thread_id"], e["event_id"]): e for t in batch["threads"]
              if t["checkpoint"].get("eligible", True) is not False
              for e in t["events"] if not e.get("context_only")}
    references = {}
    for record in records:
        for ref in record["source_refs"]:
            for eid in ref["event_ids"]:
                references.setdefault((ref["thread_id"], eid), set()).add(record["item_id"])
    seen, normalized, consumed = set(), [], set()
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Each review must be an object")
        tid, eid = review.get("thread_id"), review.get("event_id")
        if not isinstance(tid, str) or not isinstance(eid, str):
            raise ValueError("Review requires thread_id and event_id")
        key = (tid, eid)
        if key in seen or key not in events:
            raise ValueError("Review contains duplicate, unknown, context-only or ineligible event")
        seen.add(key)
        disposition = review.get("disposition")
        entry = {"thread_id": tid, "event_id": eid, "disposition": disposition}
        if disposition == "recorded":
            item_ids = review.get("item_ids")
            if (not isinstance(item_ids, list) or not item_ids
                    or not all(isinstance(i, str) and i for i in item_ids)
                    or len(set(item_ids)) != len(item_ids)
                    or set(item_ids) != references.get(key, set())):
                raise ValueError("Recorded review item_ids must exactly match record source references")
            entry["item_ids"] = sorted(item_ids)
        elif disposition in {"ignored", "pending"}:
            if references.get(key) or review.get("item_ids"):
                raise ValueError("Ignored/pending events cannot be referenced by records")
            reason = review.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Ignored/pending events require a reason")
            entry["reason"] = reason.strip()
        else:
            raise ValueError("Review disposition must be recorded, ignored or pending")
        if set(review) - set(entry):
            raise ValueError("Unsupported review fields")
        normalized.append(entry)
        if disposition != "pending":
            consumed.add(key)
    if seen != set(events):
        raise ValueError("Review must account for every eligible new event")
    checkpoints = {}
    for thread in batch["threads"]:
        tid = thread["thread_id"]
        cp = copy.deepcopy(thread["checkpoint"])
        ids = [eid for eid in cp.get("event_ids", []) if (tid, eid) in consumed]
        cp["event_ids"] = ids
        cp["last_event_at"] = max((events[tid, eid]["time"] for eid in ids), key=iso, default=None)
        if 'event_signatures' in cp or 'known_message_signatures' in cp:
            signatures = cp.pop('event_signatures', {})
            known_signatures = set(cp.pop('known_message_signatures', []))
            cp['message_signatures'] = sorted(known_signatures | {signatures[eid] for eid in ids if eid in signatures})
        checkpoints[tid] = cp
    coverage = copy.deepcopy(batch["coverage"])
    pending_count = len(events) - len(consumed)
    coverage["collection_complete_local"] = coverage.get("complete_local") is True
    coverage["pending_review_events"] = pending_count
    coverage["complete_local"] = coverage["collection_complete_local"] and not pending_count
    return sorted(normalized, key=lambda r: (r["thread_id"], r["event_id"])), checkpoints, coverage

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
    ap.add_argument("command", choices=["commit", "enrich", "report", "export", "query", "candidates", "link", "unlink", "rebuild", "status"])
    for name in ["config", "batch", "records", "review", "evidence", "brief", "mode", "run-id", "target-run", "since", "until", "report", "out"]:
        ap.add_argument("--" + name)
    for name in ['project', 'status', 'item-id', 'thread-id']:
        ap.add_argument('--' + name, action='append', default=[])
    for name in ['keyword', 'title', 'target-item', 'reason']:
        ap.add_argument('--' + name)
    ap.add_argument('--latest', action='store_true')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--offset', type=int, default=0)
    ap.add_argument('--order', choices=['newest', 'oldest'], default='newest')
    ap.add_argument('--view', choices=['summary', 'full'])
    ap.add_argument('--coverage', choices=['summary', 'full'])
    ap.add_argument('--all', action='store_true', dest='all_records')
    args = ap.parse_args()
    if args.evidence and args.command not in {'commit', 'enrich'}:
        ap.error('--evidence is supported only by commit/enrich')
    supplied = {name for name in ('project', 'status', 'item_id', 'thread_id', 'keyword', 'title',
                                  'target_item', 'reason', 'latest', 'view', 'coverage', 'all_records') if getattr(args, name)}
    if args.limit is not None:
        supplied.add('limit')
    if args.offset:
        supplied.add('offset')
    if args.order != 'newest':
        supplied.add('order')
    supported = {
        'query': {'project', 'status', 'item_id', 'thread_id', 'keyword', 'latest', 'limit', 'offset', 'order', 'view', 'coverage', 'all_records'},
        'candidates': {'project', 'title', 'thread_id', 'limit', 'offset'},
        'link': {'project', 'item_id', 'target_item', 'reason'},
        'unlink': {'project', 'item_id', 'reason'},
    }.get(args.command, set())
    if supplied - supported:
        ap.error(args.command + ' does not accept ' + ', '.join('--' + name.replace('_', '-') for name in sorted(supplied - supported)))
    if args.all_records and (not args.out or args.limit is not None or args.offset):
        ap.error('--all requires --out and cannot be combined with --limit or --offset')
    if args.coverage == 'full' and not args.out:
        ap.error('--coverage full requires --out; summary coverage is available on stdout')
    config_spec = importlib.util.spec_from_file_location("codex_work_memory_configuration", Path(__file__).with_name("configuration.py"))
    configuration = importlib.util.module_from_spec(config_spec)
    config_spec.loader.exec_module(configuration)
    cfg = configuration.load_config(args.config)
    root = Path(cfg["archive_dir"])
    if args.command in {"status", "query", "candidates"}:
        ledger = resolved_runs(root)
        if args.command == "status":
            result = {"runs": len(ledger), "state": read(root / "state.json") if (root / "state.json").exists() else None}
        elif args.command == 'query':
            result = retrieval.query(ledger, merged_catalog(ledger), since=args.since, until=args.until,
                                     projects=args.project, statuses=args.status, item_ids=args.item_id,
                                     thread_ids=args.thread_id, keyword=args.keyword, latest=args.latest,
                                     limit=None if args.all_records else (20 if args.limit is None else args.limit),
                                     offset=args.offset, order=args.order, view=args.view or 'summary',
                                     coverage=args.coverage or 'summary')
        else:
            if len(args.project) != 1:
                ap.error('candidates requires exactly one --project')
            if args.keyword or args.status or args.item_id or args.latest or args.since or args.until or args.order != 'newest':
                ap.error('candidates accepts project, title, thread-id, limit and offset; use query for other filters')
            result = retrieval.candidates(ledger, merged_catalog(ledger), project=args.project[0],
                                          title=args.title or '', thread_ids=args.thread_id,
                                          limit=10 if args.limit is None else args.limit, offset=args.offset)
        if args.out:
            write_json(args.out, result)
            print(json.dumps({"ok": True, "out": args.out}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return
    with lock(root):
        if args.command in {'link', 'unlink'}:
            if len(args.project) != 1 or len(args.item_id) != 1 or not args.reason:
                ap.error('link/unlink requires one --project, one --item-id and --reason')
            if args.command == 'link' and not args.target_item:
                ap.error('link requires --target-item')
            if args.command == 'unlink' and args.target_item:
                ap.error('unlink does not accept --target-item')
            result = associate(root, args.run_id, args.project[0], args.item_id[0], args.target_item, args.reason)
            print(json.dumps(result, ensure_ascii=False))
            return
        if args.command == "rebuild":
            state = rebuild(root)
            print(json.dumps({"ok": True, "runs": len(state["runs"])}))
            return
        if not args.run_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args.run_id):
            raise ValueError("run-id must be 1-100 letters, digits, underscores or hyphens")
        path = root / "runs" / (args.run_id + ".json")
        existed = path.exists()
        old = read(path) if existed else None
        if args.command == "export":
            run = read(root / "runs" / (args.run_id + ".json"))
            result = agent_export.export_run(root, cfg["agent_export_dir"], run, resolved_runs(root))
            print(json.dumps({"ok": True, "run_id": args.run_id, "agent_export": result}, ensure_ascii=False))
            return
        if args.command == "enrich":
            result = enrichment(root, args.target_run, args.run_id, read(args.records), read(args.batch) if args.batch else None,
                                read(args.evidence) if args.evidence else None)
            print(json.dumps(result, ensure_ascii=False))
            return
        brief = Path(args.brief).read_text(encoding="utf-8-sig")
        if args.command == "commit":
            batch, records = read(args.batch), read(args.records)
            validate_records(records, batch)
            if args.mode not in {"daily", "bootstrap"}:
                raise ValueError("mode must be daily or bootstrap")
            run = {"run_id": args.run_id, "mode": args.mode, "window": batch["window"], "records": records, "coverage": batch["coverage"], "checkpoints": {t["thread_id"]: t["checkpoint"] for t in batch["threads"]}, "brief_text": brief, "source_catalog": source_catalog(batch)}
            catalog = evidence.bind(records, batch, read(args.evidence) if args.evidence else None, verify_source=not existed)
            if catalog:
                run['evidence_catalog'] = catalog
            legacy_retry = old is not None and old.get("schema_version", 1) == 1 and "event_reviews" not in old
            if legacy_retry:
                if args.review:
                    raise ValueError("Retry a legacy run without --review; its original payload is frozen")
            else:
                if not args.review:
                    raise ValueError("New commits require --review with an explicit disposition for every eligible event")
                reviews, checkpoints, coverage = reviewed_checkpoints(records, batch, read(args.review))
                run.update(event_reviews=reviews, batch_checkpoints=run["checkpoints"],
                           checkpoints=checkpoints, coverage=coverage)
        else:
            if iso(args.since) >= iso(args.until):
                raise ValueError("Invalid weekly window")
            run = {"run_id": args.run_id, "mode": "weekly", "window": {"since": args.since, "until": args.until, "timezone": cfg.get("timezone", "Asia/Shanghai")}, "records": [], "brief_text": brief, "report_text": Path(args.report).read_text(encoding="utf-8-sig")}
        run["payload_hash"] = digest(run)
        if existed:
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
                aliases = item_aliases(root)
                for record in records:
                    canonical = aliases.get((record['project_id'], record['item_id']))
                    if canonical is not None:
                        raise ValueError('Use canonical item_id for new progress: ' + canonical)
            run["created_at"] = datetime.now(TZ).isoformat(timespec="microseconds")
            run["schema_version"] = 2 if args.command == "commit" else 1
            # Freeze before making the run durable. Retrying a failed export must
            # not silently incorporate later backfills or revisions.
            export_ledger = resolved_runs(root)
            if args.command == "commit":
                export_ledger.append(run)
            agent_export.freeze(root, run, export_ledger, basis=args.command + "-save")
            write_json(path, run)
        state = rebuild(root)
        result = {"ok": True, "already_saved": existed, "run_id": args.run_id, "records": len(run["records"]), "brief": str(root / "briefs" / (args.run_id + ".md")), "index": str(root / "index.md"), "scan_success_until": state["scan_success_until"]}
        result["pending_review_events"] = len(state["pending_reviews"])
        result["agent_export"] = agent_export.export_run(root, cfg["agent_export_dir"], run, resolved_runs(root))
        print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
