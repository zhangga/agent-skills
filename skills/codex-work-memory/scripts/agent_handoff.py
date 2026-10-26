"""Portable, deterministic handoff materials derived only from a frozen export."""
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def item_units(snapshot):
    """One complete item per line, with evidence inline for bounded ingestion."""
    for project in snapshot["projects"]:
        for item in project["items"]:
            revisions = [*item["history"], item["latest"]]
            tids = {ref["thread_id"] for rev in revisions for ref in rev["record"].get("source_refs", [])}
            eids = {eid for rev in revisions for claim in rev["record"].get("details", {}).get("evidence", [])
                    for eid in claim["evidence_ids"]}
            # Exclude window/document IDs from this hash so unchanged items in
            # overlapping reports have the same content identity.
            material = {
                "project_id": project["project_id"], "project_name": project["project_name"],
                "item_id": item["item_id"], "latest": item["latest"], "history": item["history"],
                "source_catalog": {},
                "evidence_catalog": {eid: snapshot.get("evidence_catalog", {})[eid]
                                     for eid in sorted(eids) if eid in snapshot.get("evidence_catalog", {})},
                "missing_evidence_ids": sorted(eids - snapshot.get("evidence_catalog", {}).keys()),
            }
            for tid in sorted(tids):
                metadata = dict(snapshot["source_catalog"].get(tid, {}))
                events = {eid for rev in revisions for ref in rev["record"].get("source_refs", [])
                          if ref["thread_id"] == tid for eid in ref.get("event_ids", [])}
                metadata["events"] = {eid: at for eid, at in metadata.get("events", {}).items() if eid in events}
                metadata["missing_event_ids"] = sorted(events - metadata["events"].keys())
                material["source_catalog"][tid] = metadata
            yield {
                "schema_version": 1, "kind": "codex-work-memory-item-material",
                "unit_id": "codex-work-memory:item:" + sha(encoded([project["project_id"], item["item_id"]])),
                "content_sha256": sha(encoded(material)),
                "document_id": snapshot["document_id"], "document_sha256": snapshot["content_sha256"],
                "window": snapshot["window"], "extraction_status": "candidate",
                "revision_hashes": {rev["revision_id"]: sha(encoded(rev["record"])) for rev in revisions},
                **material,
            }


def instructions(snapshot):
    template = (Path(__file__).resolve().parents[1] / "assets" / "agent-handoff.md").read_text(encoding="utf-8")
    run_id = snapshot["run_id"]
    loose_files = ("\n\n## 本次单独文件\n\n"
                   f"同目录完整正文：`{run_id}.md`；结构化正文：`{run_id}.json`；逐事项材料：`{run_id}.items.jsonl`。"
                   f"也可读取 `{run_id}.zip`，包内使用上方的 source.md、source.json、items.jsonl 短文件名。")
    identity = {key: snapshot[key] for key in ("document_id", "content_sha256", "window", "counts", "coverage_summary")}
    identity.update(source_generated_at=snapshot["report_generated_at"],
                    evidence_frozen_at=snapshot["snapshot_created_at"],
                    export_status="prepared", ingestion_status="unverified", knowledge_status="unverified")
    return template.rstrip() + loose_files + "\n\n## 本次资料标识\n\n```json\n" + json.dumps(identity, ensure_ascii=False, indent=2) + "\n```\n"


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def export_bundle(destination, snapshot, markdown):
    destination = Path(destination)
    run_id = snapshot["run_id"]  # Validated by the exporter.
    units = list(item_units(snapshot))
    item_text = "".join(encoded(unit).decode("utf-8") + "\n" for unit in units)
    guide = instructions(snapshot)
    files = {
        "source.md": markdown.encode("utf-8"),
        "source.json": (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "items.jsonl": item_text.encode("utf-8"),
        "HANDOFF.md": guide.encode("utf-8"),
    }
    inventory = {
        "schema_version": 1, "kind": "codex-work-memory-handoff",
        "document_id": snapshot["document_id"], "content_sha256": snapshot["content_sha256"],
        "window": snapshot["window"], "counts": snapshot["counts"],
        "files": {name: {"bytes": len(data), "sha256": sha(data)} for name, data in files.items()},
    }
    files["manifest.json"] = (json.dumps(inventory, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            bundle.writestr(info, data)
    zip_data = buffer.getvalue()
    paths = {"bundle": destination / (run_id + ".zip"),
             "handoff": destination / (run_id + ".handoff.md"),
             "items": destination / (run_id + ".items.jsonl")}
    for key, data in (("bundle", zip_data), ("handoff", files["HANDOFF.md"]), ("items", files["items.jsonl"])):
        atomic_bytes(paths[key], data)
    return {**{key: str(path) for key, path in paths.items()}, "bundle_sha256": sha(zip_data),
            "export_status": "prepared", "ingestion_status": "unverified", "knowledge_status": "unverified"}
