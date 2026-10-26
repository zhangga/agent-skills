"""Behavioral checks for overlays, old run compatibility and readable views."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'memory.py'
spec = importlib.util.spec_from_file_location('memory_readability', SCRIPT)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


class ReadabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'archive'
        self.cfg = self.root / 'config.json'
        self.brief = self.root / 'brief.md'
        memory.write_json(self.cfg, {'archive_dir': str(self.archive)})
        self.brief.write_text('Already delivered brief', encoding='utf-8')
        self.batch = {'window': {'since': '2026-10-16T00:00:00Z', 'until': '2026-10-17T00:00:00Z'},
            'coverage': {'complete_local': True}, 'threads': [{'thread_id': 't1', 'title': '排查客户端启动',
            'source_path': 'C:/logs/t1.jsonl', 'events': [{'event_id': 'e1', 'time': '2026-10-16T01:00:00Z'}],
            'checkpoint': {'eligible': True, 'event_ids': ['e1'], 'last_event_at': '2026-10-16T01:00:00Z'}}]}
        self.records = [{'item_id': 'startup', 'occurred_at': '2026-10-16T01:00:00Z', 'project_id': 'client',
            'project_name': '客户端', 'category': '问题修复', 'status': '进行中', 'title': '修复启动故障',
            'summary': '定位启动失败条件', 'follow_up': '补充真实设备验证',
            'source_refs': [{'thread_id': 't1', 'event_ids': ['e1']}]}]
        self.batch_file, self.records_file = self.root / 'batch.json', self.root / 'records.json'
        memory.write_json(self.batch_file, self.batch)
        memory.write_json(self.records_file, self.records)
        self.review_file = self.root / 'review.json'
        memory.write_json(self.review_file, [{'thread_id': 't1', 'event_id': 'e1',
                                           'disposition': 'recorded', 'item_ids': ['startup']}])
        result = self.call('commit', '--batch', self.batch_file, '--records', self.records_file,
                           '--review', self.review_file, '--brief', self.brief, '--mode', 'bootstrap', '--run-id', 'original')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.patch = [{'item_id': 'startup', 'occurred_at': '2026-10-16T01:00:00Z',
            'details': {'context': '特定设备启动失败，需要定位触发条件。',
                        'approach': ['对照正常与失败日志。'], 'validation': ['会话记录显示离线测试通过。'],
                        'limitations': ['尚未复测真实设备。'], 'artifacts': [{'label': '修复说明', 'path': 'D:/work/fix.md'}]}}]

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), *map(str, args), '--config', str(self.cfg)],
                              capture_output=True, text=True, encoding='utf-8')

    def enrich(self, patches=None, run_id='readable'):
        with memory.lock(self.archive):
            return memory.enrichment(self.archive, 'original', run_id, self.patch if patches is None else patches, self.batch)

    def test_enrichment_preserves_original_state_brief_and_delivery(self):
        receipt = self.archive / 'delivery' / 'original.json'
        memory.write_json(receipt, {'status': 'sent', 'message_id': 'synthetic'})
        paths = [self.archive / 'runs' / 'original.json', self.archive / 'state.json', self.archive / 'briefs' / 'original.md', receipt]
        before = {p: p.read_bytes() for p in paths}
        self.assertFalse(self.enrich()['already_saved'])
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        query = self.call('query', '--view', 'full')
        self.assertEqual(query.returncode, 0, query.stderr)
        result = json.loads(query.stdout)
        self.assertEqual(result['records'][0]['details'], self.patch[0]['details'])
        self.assertEqual(result['records'][0]['source_refs'], self.records[0]['source_refs'])
        self.assertEqual(result['source_catalog']['t1']['title'], '排查客户端启动')
        self.assertTrue((self.archive / 'index.html').exists())

    def test_repeat_is_idempotent_and_changed_revision_is_rejected(self):
        self.enrich()
        path = self.archive / 'enrichments' / 'readable.json'
        before = path.read_bytes()
        self.assertTrue(self.enrich()['already_saved'])
        changed = copy.deepcopy(self.patch)
        changed[0]['details']['context'] = 'different'
        with self.assertRaises(ValueError):
            self.enrich(changed)
        self.assertEqual(path.read_bytes(), before)

    def test_full_record_allowed_but_original_facts_cannot_change(self):
        enriched = copy.deepcopy(self.records)
        enriched[0]['details'] = self.patch[0]['details']
        self.enrich(enriched)
        for field, value in [('status', '已完成'), ('source_refs', []), ('summary', 'unverified result')]:
            changed = copy.deepcopy(enriched)
            changed[0][field] = value
            with self.assertRaises(ValueError):
                self.enrich(changed, 'bad-' + field)
        self.assertEqual(len(list((self.archive / 'enrichments').glob('*.json'))), 1)

    def test_duplicate_or_unknown_record_and_invalid_details_rejected(self):
        for bad in [self.patch * 2, [{'item_id': 'missing', 'occurred_at': self.records[0]['occurred_at'], 'details': {}}],
                    [dict(self.patch[0], details={'unknown': 'bad'})],
                    [dict(self.patch[0], details={'validation': 'must be list'})]]:
            with self.assertRaises(ValueError):
                self.enrich(bad)
        self.assertFalse((self.archive / 'enrichments').exists())

    def test_crash_after_revision_commit_recovers_without_baseline_change(self):
        before = (self.archive / 'state.json').read_bytes()
        with patch.object(memory.presentation, 'html_index', side_effect=OSError('simulated view write failure')):
            with self.assertRaises(OSError):
                self.enrich()
        self.assertTrue((self.archive / 'enrichments' / 'readable.json').exists())
        self.assertEqual((self.archive / 'state.json').read_bytes(), before)
        self.assertTrue(self.enrich()['already_saved'])
        self.assertEqual((self.archive / 'state.json').read_bytes(), before)
        self.assertIn('特定设备', (self.archive / 'index.html').read_text(encoding='utf-8'))

    def test_true_old_schema_retry_remains_compatible(self):
        path = self.archive / 'runs' / 'original.json'
        old = memory.read(path)
        old.pop('source_catalog')
        old.pop('event_reviews')
        old.pop('batch_checkpoints')
        old['coverage'] = self.batch['coverage']
        old['schema_version'] = 1
        payload = {k: v for k, v in old.items() if k not in {'payload_hash', 'created_at', 'schema_version'}}
        old['payload_hash'] = memory.digest(payload)
        memory.write_json(path, old)
        before = path.read_bytes()
        result = self.call('commit', '--batch', self.batch_file, '--records', self.records_file,
                           '--brief', self.brief, '--mode', 'bootstrap', '--run-id', 'original')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['already_saved'])
        self.assertEqual(path.read_bytes(), before)
        changed = copy.deepcopy(self.records)
        changed[0]['status'] = '已完成'
        memory.write_json(self.records_file, changed)
        result = self.call('commit', '--batch', self.batch_file, '--records', self.records_file,
                           '--brief', self.brief, '--mode', 'bootstrap', '--run-id', 'original')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_catalog_keeps_old_events_and_source_uses_exact_reference_time(self):
        first = {'source_catalog': memory.source_catalog(self.batch)}
        second = {'source_catalog': {'t1': {'title': '', 'source_path': '', 'first_event_at': '2026-10-16T04:00:00Z',
            'last_event_at': '2026-10-16T04:00:00Z', 'events': {'e2': '2026-10-16T04:00:00Z'}}}}
        catalog = memory.merged_catalog([first, second])
        self.assertEqual(set(catalog['t1']['events']), {'e1', 'e2'})
        self.assertEqual(catalog['t1']['first_event_at'], '2026-10-16T01:00:00Z')
        self.assertEqual(catalog['t1']['title'], '排查客户端启动')
        source_time = memory.presentation.source_time(self.records[0]['source_refs'][0], catalog['t1'])
        self.assertEqual(source_time, '引用时间 2026-10-16 09:00')

    def test_latest_item_history_order_uses_real_time(self):
        early = copy.deepcopy(self.records[0])
        early['occurred_at'] = '2026-10-16T09:00:00+08:00'
        late = copy.deepcopy(early)
        late.update(occurred_at='2026-10-16T02:00:00Z', status='已完成', follow_up='')
        groups = memory.presentation.items_grouped([early, late])
        history = groups['client']['items']['startup']
        self.assertEqual(history[0]['status'], '已完成')
        page = memory.presentation.html_index([early, late], {}, [])
        self.assertEqual(page.count("<article class='card'"), 1)
        self.assertNotIn("<div class='next'>", page)
        self.assertIn('历史进展 · 1 条', page)

    def test_html_escapes_untrusted_data_and_contains_no_remote_resources(self):
        record = copy.deepcopy(self.records[0])
        record['title'] = "<script>alert('x')</script>"
        record['summary'] = '<img src=x onerror=alert(1)>'
        record['details'] = {'artifacts': [{'label': 'unsafe', 'path': 'javascript:alert(1)'}]}
        page = memory.presentation.html_index([record], {}, [])
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<img src=x', page)
        self.assertNotIn("href='javascript:", page)
        self.assertNotIn('<link', page)
        self.assertNotIn('<script src=', page)
        self.assertIn('class=\'status progress\'', page)

    def test_portable_report_readable_dates_and_legacy_project_filename(self):
        self.enrich()
        report = (self.archive / 'daily' / 'original.md').read_text(encoding='utf-8')
        self.assertIn('2026-10-16 08:00', report)
        self.assertIn('最近进展 2026-10-16 09:00', report)
        self.assertIn('解决过程', report)
        self.assertIn('排查客户端启动', report)
        self.assertIn('引用时间 2026-10-16 09:00', report)
        self.assertNotIn('**关键判断**', report)
        expected = 'client.md-' + memory.digest('client')[:8] + '.md'
        self.assertTrue((self.archive / 'projects' / expected).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
