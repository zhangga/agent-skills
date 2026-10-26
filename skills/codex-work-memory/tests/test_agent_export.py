"""Synthetic fixtures only: no real session, filesystem, or recipient identifiers."""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    spec = importlib.util.spec_from_file_location("test_" + name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


export = module("agent_export")
memory = module("memory")


def record(item="task-a", at="2026-01-05T00:00:00+08:00", status="进行中", project="demo"):
    return {"item_id": item, "occurred_at": at, "project_id": project, "project_name": "示例项目 " + project,
            "category": "排障", "status": status, "title": "修复示例问题", "summary": "已缩小问题范围",
            "outcome": "验证一条请求成功", "follow_up": "补充边界验证", "knowledge": "仅适用于示例环境",
            "details": {"context": "示例背景", "approach": ["重现后定位"], "decisions": ["保留旧接口"],
                        "validation": ["检查响应"], "limitations": ["尚未验证并发"],
                        "artifacts": [{"label": "测试输出", "path": "/example/results.txt"}]},
            "source_refs": [{"thread_id": "demo-thread", "event_ids": ["demo-event"]}],
            "tags": ["synthetic"]}


def scan(run_id="daily-demo", records=None, complete=True):
    result = {"run_id": run_id, "mode": "daily", "created_at": "2026-01-12T00:00:00+08:00",
              "window": {"since": "2026-01-05T00:00:00+08:00", "until": "2026-01-12T00:00:00+08:00", "timezone": "Asia/Shanghai"},
              "records": [record()] if records is None else records, "coverage": {"complete_local": complete,
              "counts": {"files_read": 2}, "errors": [] if complete else [{"kind": "source_read", "path": "/example/missing"}]},
              "source_catalog": {"demo-thread": {"title": "示例会话", "source_path": "/example/session.jsonl",
              "events": {"demo-event": "2026-01-05T00:00:00+08:00", "unused-event": "2026-01-06T01:00:00+08:00"}}},
              "checkpoints": {}, "brief_text": "示例日报"}
    result["payload_hash"] = memory.digest(result)
    return result


def weekly(run_id="weekly-demo", start="2026-01-05T00:00:00+08:00", end="2026-01-12T00:00:00+08:00"):
    result = {"run_id": run_id, "mode": "weekly", "created_at": end,
              "window": {"since": start, "until": end, "timezone": "Asia/Shanghai"}, "records": [],
              "report_text": "# 人工提炼周报\n\n成果与技术取舍均保留。", "brief_text": "本周完成一项验证。"}
    result["payload_hash"] = memory.digest(result)
    return result


class AgentExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        self.destination = Path(self.temp.name) / "agent summaries"

    def save(self, run):
        memory.write_json(self.root / "runs" / (run["run_id"] + ".json"), run)

    def test_full_details_history_and_half_open_window(self):
        first = record()
        second = record(at="2026-01-09T10:00:00+08:00", status="已完成")
        second["outcome"] = "边界验证通过"
        outside = record(item="outside", at="2026-01-12T00:00:00+08:00")
        before = record(item="before", at="2026-01-04T23:59:59+08:00")
        same_id_other_project = record(project="other")
        result = export.export_weekly(self.root, self.destination, weekly(), [scan(records=[first, second, outside, before, same_id_other_project])])
        doc = export.read(result["json"])
        self.assertEqual(doc["counts"], {"projects": 2, "items": 2, "revisions": 3, "source_threads": 1})
        item = doc["projects"][0]["items"][0]
        self.assertEqual(item["latest"]["record"], second)
        self.assertEqual(item["history"][0]["record"], first)
        self.assertEqual(list(doc["source_catalog"]["demo-thread"]["events"]), ["demo-event"])
        text = Path(result["markdown"]).read_text(encoding="utf-8")
        for needle in ["成果与技术取舍均保留", "示例背景", "重现后定位", "保留旧接口", "检查响应", "尚未验证并发",
                       "/example/results.txt", "仅适用于示例环境", "demo-event", "示例会话", "demo-thread", "synthetic"]:
            self.assertIn(needle, text)
        self.assertNotIn('"item_id": "outside"', Path(result["json"]).read_text(encoding="utf-8"))

    def test_retry_is_frozen_and_repairs_deleted_outputs(self):
        run = weekly()
        first = export.export_weekly(self.root, self.destination, run, [scan()])
        original = Path(first["json"]).read_bytes()
        changed = scan(records=[record(item="later-backfill")])
        Path(first["markdown"]).unlink()
        again = export.export_weekly(self.root, self.destination, run, [changed])
        self.assertEqual(first["content_sha256"], again["content_sha256"])
        self.assertEqual(original, Path(again["json"]).read_bytes())
        self.assertTrue(Path(again["markdown"]).exists())

    def test_backfilled_older_week_does_not_replace_latest(self):
        newer = weekly("weekly-newer", "2026-01-12T00:00:00+08:00", "2026-01-19T00:00:00+08:00")
        export.export_weekly(self.root, self.destination, newer, [])
        export.export_weekly(self.root, self.destination, weekly("weekly-older"), [scan()])
        self.assertEqual(export.read(self.destination / "manifest.json")["latest_run_id"], "weekly-newer")
        self.assertEqual(export.read(self.destination / "latest-weekly.json")["run_id"], "weekly-newer")

    def test_shared_destination_contention_and_failure_release(self):
        with export.export_lock(self.destination):
            with self.assertRaisesRegex(RuntimeError, "output directory lock"):
                export.export_weekly(self.root, self.destination, weekly(), [scan()])
        with patch.object(export, "freeze", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                export.export_weekly(self.root, self.destination, weekly(), [scan()])
        # Both the contended handle and the failed writer must release cleanly.
        result = export.export_weekly(self.root, self.destination, weekly(), [scan()])
        self.assertTrue(Path(result["manifest"]).exists())

    def test_report_headings_are_nested_but_fenced_code_is_unchanged(self):
        report = "# 主标题\n\n## 项目\n###### 深层\n```python\n# literal\n## also literal\n```\n~~~text\n# tilde literal\n~~~\n### 决策\n    # indented code\n#not-heading"
        expected = "### 主标题\n\n#### 项目\n###### 深层\n```python\n# literal\n## also literal\n```\n~~~text\n# tilde literal\n~~~\n##### 决策\n    # indented code\n#not-heading"
        self.assertEqual(export.demote_report_headings(report), expected)
        run = weekly()
        run["report_text"] = report
        snapshot = export.build_snapshot(run, [scan()], "report-save")
        self.assertEqual(snapshot["report_markdown"], report)
        self.assertIn(expected, export.markdown(snapshot))

    def test_simultaneous_progress_uses_creation_and_numeric_record_order(self):
        older = scan("daily-z")
        older["created_at"] = "2026-01-10T00:00:00+08:00"
        records = [record() for _ in range(11)]
        for index, item in enumerate(records):
            item["summary"] = "new revision " + str(index)
        newer = scan("bootstrap-a", records)
        newer["created_at"] = "2026-01-11T00:00:00+08:00"
        snapshot = export.build_snapshot(weekly(), [newer, older], "report-save")
        item = snapshot["projects"][0]["items"][0]
        self.assertEqual(item["latest"]["revision_id"], "bootstrap-a:10")
        self.assertEqual(item["history"][0]["revision_id"], "daily-z:0")
        self.assertEqual(item["history"][-1]["revision_id"], "bootstrap-a:9")
        chronological = older['records'] + newer['records']
        history = memory.presentation.items_grouped(chronological)['demo']['items']['task-a']
        self.assertEqual(history[0], item['latest']['record'])
        self.assertEqual(history[1], item['history'][-1]['record'])
        page = memory.presentation.html_index(chronological, {}, [older, newer])
        self.assertIn('<p class=\'preview\'>new revision 10</p>', page)
        markdown = memory.presentation.project_md('demo', memory.presentation.items_grouped(chronological)['demo'], {})
        self.assertLess(markdown.index('new revision 10'), markdown.index('new revision 9'))

    def test_failure_then_retry_preserves_evidence_and_canonical_files(self):
        run = weekly()
        self.save(run)
        self.save(scan())
        state = self.root / "state.json"
        memory.write(state, '{"scan_success_until":"unchanged"}\n')
        before = {p: p.read_bytes() for p in self.root.rglob("*.json")}
        write = export.write
        def fail_manifest(path, value):
            if Path(path).name == "manifest.json":
                raise OSError("simulated unavailable export destination")
            write(path, value)
        with patch.object(export, "write", fail_manifest):
            with self.assertRaises(OSError):
                export.export_weekly(self.root, self.destination, run, [scan()])
        frozen = export.read(self.root / "weekly" / "weekly-demo.agent-evidence.json")
        repaired = export.export_weekly(self.root, self.destination, run, [])
        self.assertEqual(repaired["content_sha256"], frozen["content_sha256"])
        for path, value in before.items():
            self.assertEqual(path.read_bytes(), value)

    def test_empty_week_and_partial_coverage_are_distinct(self):
        snapshot = export.build_snapshot(weekly(), [scan(records=[], complete=False)], "report-save")
        self.assertEqual(snapshot["counts"]["items"], 0)
        self.assertFalse(snapshot["coverage_summary"]["complete_local_window"])
        self.assertEqual(len(snapshot["coverage_summary"]["uncovered_intervals"]), 1)
        self.assertIn("source_read", export.markdown(snapshot))
        complete = export.build_snapshot(weekly(), [scan(records=[])], "report-save")
        self.assertTrue(complete["coverage_summary"]["complete_local_window"])

    def test_overlapping_complete_scan_cannot_hide_pending_reviews(self):
        original = scan('original', records=[])
        backfill = scan('backfill', records=[], complete=False)
        backfill['event_reviews'] = [{'thread_id': 'demo-thread', 'event_id': 'demo-event',
                                     'disposition': 'pending', 'reason': 'Newly discovered history'}]
        snapshot = export.build_snapshot(weekly(), [original, backfill], 'report-save')
        self.assertFalse(snapshot['coverage_summary']['complete_local_window'])
        self.assertEqual(snapshot['coverage_summary']['pending_review_events'], 1)
        self.assertIn('待处理事件 1 条', export.markdown(snapshot))
        resolved = scan('resolved', records=[])
        resolved['checkpoints'] = {'demo-thread': {'eligible': True, 'event_ids': ['demo-event']}}
        complete = export.build_snapshot(weekly(), [original, backfill, resolved], 'report-save')
        self.assertTrue(complete['coverage_summary']['complete_local_window'])
        self.assertEqual(complete['coverage_summary']['pending_review_events'], 0)

    def test_snapshot_tampering_and_run_id_collisions_fail(self):
        run = weekly()
        export.export_weekly(self.root, self.destination, run, [scan()])
        path = self.root / "weekly" / "weekly-demo.agent-evidence.json"
        data = export.read(path)
        data["report_markdown"] = "changed"
        export.write(path, export.json_text(data))
        with self.assertRaisesRegex(ValueError, "Frozen weekly evidence"):
            export.export_weekly(self.root, self.destination, run, [scan()])
        with self.assertRaises(ValueError):
            export.export_weekly(self.root, self.destination, weekly("../unsafe"), [])

    def test_cli_report_then_legacy_export_does_not_move_baseline(self):
        if not (SCRIPTS / "configuration.py").exists():
            self.skipTest("configuration.py integration not available")
        self.save(scan())
        memory.rebuild(self.root)
        state_before = export.read(self.root / "state.json")
        config = Path(self.temp.name) / "config.json"
        memory.write_json(config, {"archive_dir": str(self.root), "agent_export_dir": str(self.destination),
                                   "source_roots": [str(Path(self.temp.name) / "sessions")], "timezone": "Asia/Shanghai"})
        report = Path(self.temp.name) / "report.md"
        brief = Path(self.temp.name) / "brief.md"
        memory.write(report, "# 周报\n\n报告正文")
        memory.write(brief, "简报")
        args = ["memory.py", "report", "--config", str(config), "--run-id", "weekly-cli",
                "--since", "2026-01-05T00:00:00+08:00", "--until", "2026-01-12T00:00:00+08:00",
                "--report", str(report), "--brief", str(brief)]
        with patch.object(sys, "argv", args), redirect_stdout(StringIO()) as output:
            memory.main()
        self.assertIn("agent_export", json.loads(output.getvalue()))
        saved = export.read(self.root / "state.json")
        self.assertEqual(saved["scan_success_until"], state_before["scan_success_until"])
        self.assertEqual(saved["sources"], state_before["sources"])
        frozen = export.read(self.root / "weekly" / "weekly-cli.agent-evidence.json")
        self.assertEqual(frozen["snapshot_basis"], "report-save")
        with patch.object(sys, "argv", args), redirect_stdout(StringIO()):
            memory.main()
        legacy = weekly("weekly-legacy")
        self.save(legacy)
        state_bytes = (self.root / "state.json").read_bytes()
        with patch.object(sys, "argv", ["memory.py", "export", "--config", str(config), "--run-id", "weekly-legacy"]), redirect_stdout(StringIO()):
            memory.main()
        self.assertEqual((self.root / "state.json").read_bytes(), state_bytes)
        self.assertTrue(export.read(self.destination / "weekly" / "weekly-legacy.json")["snapshot_basis"].startswith("legacy"))


if __name__ == "__main__":
    unittest.main()
