import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parent
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'memory.py'
spec = importlib.util.spec_from_file_location('memory', SCRIPT)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)
collector_spec = importlib.util.spec_from_file_location('collector', SCRIPT.with_name('collect.py'))
collector = importlib.util.module_from_spec(collector_spec)
collector_spec.loader.exec_module(collector)

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'archive'
        self.cfg = self.root / 'config.json'
        self.cfg.write_text(json.dumps({'archive_dir': str(self.archive)}), encoding='utf-8')
        self.brief = self.root / 'brief.md'
        self.brief.write_text('Synthetic brief', encoding='utf-8')
        self.batch = {'window': {'since': '2026-10-16T00:00:00Z', 'until': '2026-10-17T00:00:00Z'},
            'coverage': {'complete_local': True}, 'threads': [{'thread_id': 'thread-1',
            'events': [{'event_id': 'evt_1', 'time': '2026-10-16T01:00:00Z'}],
            'checkpoint': {'eligible': True, 'event_ids': ['evt_1'], 'last_event_at': '2026-10-16T01:00:00Z'}}]}
        self.records = [{'item_id': 'item-1', 'occurred_at': '2026-10-16T01:00:00Z',
            'project_id': 'p1', 'project_name': 'Synthetic project', 'category': 'test', 'status': 'done',
            'title': 'Synthetic work', 'summary': 'Completed synthetic work',
            'source_refs': [{'thread_id': 'thread-1', 'event_ids': ['evt_1']}]}]

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), *args, '--config', str(self.cfg)],
                              capture_output=True, text=True, encoding='utf-8')

    def commit(self, run='run-1', batch=None, records=None):
        bf, rf = self.root / 'batch.json', self.root / 'records.json'
        bf.write_text(json.dumps(batch if batch is not None else self.batch), encoding='utf-8')
        rf.write_text(json.dumps(records if records is not None else self.records), encoding='utf-8')
        return self.call('commit', '--batch', str(bf), '--records', str(rf), '--brief', str(self.brief),
                         '--mode', 'bootstrap', '--run-id', run)

    def state(self):
        return json.loads((self.archive / 'state.json').read_text(encoding='utf-8'))

    def assertOk(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_success_and_same_run_idempotence(self):
        self.assertFalse(self.assertOk(self.commit())['already_saved'])
        self.assertTrue(self.assertOk(self.commit())['already_saved'])
        self.assertEqual(len(list((self.archive / 'runs').glob('*.json'))), 1)
        self.assertEqual(self.state()['sources']['thread-1']['event_ids'], ['evt_1'])

    def test_same_run_different_content_is_rejected(self):
        self.assertOk(self.commit())
        changed = copy.deepcopy(self.records)
        changed[0]['summary'] = 'Changed'
        self.assertNotEqual(self.commit(records=changed).returncode, 0)

    def test_different_run_same_batch_is_rejected(self):
        self.assertOk(self.commit())
        second = self.commit(run='run-2')
        self.assertNotEqual(second.returncode, 0, 'Duplicate run must not be accepted')
        self.assertEqual(len(self.assertOk(self.call('query'))['records']), 1)

    def test_partial_source_does_not_advance_global(self):
        self.batch['coverage']['complete_local'] = False
        self.assertOk(self.commit())
        self.assertIsNone(self.state()['scan_success_until'])
        self.assertEqual(self.state()['sources']['thread-1']['event_ids'], ['evt_1'])

    def test_ineligible_source_not_saved_or_checkpointed(self):
        self.batch['coverage']['complete_local'] = False
        self.batch['threads'][0]['checkpoint']['eligible'] = False
        self.assertNotEqual(self.commit().returncode, 0)
        self.assertOk(self.commit(records=[]))
        self.assertEqual(self.state()['sources'], {})

    def test_unknown_and_context_sources_rejected(self):
        bad = copy.deepcopy(self.records)
        bad[0]['source_refs'][0]['event_ids'] = ['not-real']
        self.assertNotEqual(self.commit(records=bad).returncode, 0)
        self.batch['threads'][0]['events'][0]['context_only'] = True
        self.assertNotEqual(self.commit().returncode, 0)

    def test_checkpoint_cannot_claim_unknown_events(self):
        self.batch['threads'][0]['checkpoint']['event_ids'].append('invented')
        self.assertNotEqual(self.commit().returncode, 0)

    def test_weekly_does_not_move_source_or_scan_baseline(self):
        self.assertOk(self.commit())
        before = self.state()
        self.assertOk(self.call('report', '--run-id', 'weekly-1', '--since', '2026-10-16T00:00:00Z',
                               '--until', '2026-10-23T00:00:00Z', '--brief', str(self.brief), '--report', str(self.brief)))
        after = self.state()
        self.assertEqual(before['sources'], after['sources'])
        self.assertEqual(before['scan_success_until'], after['scan_success_until'])

    def test_successful_future_window_cannot_hide_gap(self):
        self.batch['coverage']['complete_local'] = False
        self.assertOk(self.commit())
        later = copy.deepcopy(self.batch)
        later['coverage']['complete_local'] = True
        later['window'] = {'since': '2026-10-18T00:00:00Z', 'until': '2026-10-19T00:00:00Z'}
        later['threads'] = []
        self.assertOk(self.commit(run='run-2', batch=later, records=[]))
        self.assertIsNone(self.state()['scan_success_until'], 'Future complete range must not cover earlier failed range')

    def test_dead_process_lock_is_reusable(self):
        self.archive.mkdir()
        (self.archive / '.memory.lock').write_text('12345')
        self.assertOk(self.commit())

    def test_live_lock_blocks_and_killed_process_releases(self):
        program = ('import importlib.util, pathlib, sys, time; '
                   's=importlib.util.spec_from_file_location("m",sys.argv[1]); '
                   'm=importlib.util.module_from_spec(s);s.loader.exec_module(m); '
                   'guard=m.lock(pathlib.Path(sys.argv[2]));guard.__enter__(); '
                   'print("LOCKED",flush=True);time.sleep(30)')
        child = subprocess.Popen([sys.executable, '-c', program, str(SCRIPT), str(self.archive)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'LOCKED')
            self.assertNotEqual(self.commit().returncode, 0)
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertOk(self.commit())

    def test_collect_commit_resume_old_thread_and_recover_failed_file(self):
        sources = self.root / 'sessions' / '2020' / '01' / '01'
        sources.mkdir(parents=True)
        good, bad = sources / 'good.jsonl', sources / 'bad.jsonl'
        def write_rollout(path, tid, events, malformed=False):
            rows = [{'timestamp': '2020-01-01T00:00:00Z', 'type': 'session_meta',
                     'payload': {'id': tid, 'cwd': 'C:/synthetic/project'}}]
            for stamp, content in events:
                rows.append({'timestamp': stamp, 'type': 'response_item',
                             'payload': {'type': 'message', 'role': 'user',
                                         'content': [{'type': 'input_text', 'text': content}]}})
            text = '\n'.join(json.dumps(row) for row in rows) + '\n'
            path.write_text(text + ('{broken\n' if malformed else ''), encoding='utf-8')
        write_rollout(good, 'good-thread', [('2026-10-16T01:00:00Z', 'First work')])
        write_rollout(bad, 'bad-thread', [('2026-10-16T02:00:00Z', 'Recoverable work')], malformed=True)
        cfg = {'source_roots': [str(sources)], 'projects': [], 'initial_since': '2026-10-16T00:00:00Z'}
        start, end = collector.instant('2026-10-16T00:00:00Z'), collector.instant('2026-10-17T00:00:00Z')
        first = collector.collect(cfg, {}, start, end)
        self.assertFalse(first['coverage']['complete_local'])
        self.assertEqual({t['thread_id'] for t in first['threads']}, {'good-thread', 'bad-thread'})
        self.assertOk(self.commit(batch=first, records=[]))
        self.assertEqual(set(self.state()['sources']), {'good-thread'})
        self.assertIsNone(self.state()['scan_success_until'])
        write_rollout(good, 'good-thread', [('2026-10-16T01:00:00Z', 'First work'),
                                          ('2026-10-17T01:00:00Z', 'Continued old thread')])
        write_rollout(bad, 'bad-thread', [('2026-10-16T02:00:00Z', 'Recoverable work')])
        second = collector.collect(cfg, self.state(), start, collector.instant('2026-10-18T00:00:00Z'))
        self.assertTrue(second['coverage']['complete_local'])
        self.assertEqual(second['coverage']['counts']['new_events'], 2)
        good_thread = next(t for t in second['threads'] if t['thread_id'] == 'good-thread')
        self.assertEqual(sum(not e.get('context_only') for e in good_thread['events']), 1)
        self.assertOk(self.commit(run='run-2', batch=second, records=[]))
        self.assertEqual(self.state()['scan_success_until'], '2026-10-18T00:00:00Z')
        third = collector.collect(cfg, self.state(), start, collector.instant('2026-10-18T00:00:00Z'))
        self.assertEqual(third['threads'], [])

    def test_run_id_path_traversal_is_rejected(self):
        self.assertNotEqual(self.commit(run='../escape').returncode, 0)
        self.assertFalse((self.archive.parent / 'escape.json').exists())

    def test_report_times_are_shanghai_and_coverage_is_readable(self):
        self.assertOk(self.commit())
        text = (self.archive / 'daily' / 'run-1.md').read_text(encoding='utf-8')
        # Human-facing dates keep the same Shanghai conversion without ISO noise.
        self.assertIn('2026-10-16 08:00', text)
        self.assertIn('2026-10-16 09:00', text)
        self.assertNotIn('```json', text)
        self.assertIn('远程及纯云端会话未覆盖', text)

if __name__ == '__main__':
    unittest.main(verbosity=2)
