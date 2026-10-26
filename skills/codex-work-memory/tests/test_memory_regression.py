import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def commit(self, run='run-1', batch=None, records=None, review=...):
        batch = batch if batch is not None else self.batch
        records = records if records is not None else self.records
        bf, rf = self.root / 'batch.json', self.root / 'records.json'
        bf.write_text(json.dumps(batch), encoding='utf-8')
        rf.write_text(json.dumps(records), encoding='utf-8')
        if review is ...:
            # Fixture events not cited by a record were deliberately reviewed
            # and dismissed. Tests of missing review pass None or a partial list.
            review = []
            for thread in batch['threads']:
                if thread['checkpoint'].get('eligible', True) is False:
                    continue
                for event in thread['events']:
                    if event.get('context_only'):
                        continue
                    ids = sorted({r['item_id'] for r in records for ref in r['source_refs']
                                  if ref['thread_id'] == thread['thread_id'] and event['event_id'] in ref['event_ids']})
                    entry = {'thread_id': thread['thread_id'], 'event_id': event['event_id']}
                    if ids:
                        entry.update(disposition='recorded', item_ids=ids)
                    else:
                        entry.update(disposition='ignored', reason='Reviewed synthetic non-work event')
                    review.append(entry)
        review_args = []
        if review is not None:
            review_file = self.root / 'review.json'
            review_file.write_text(json.dumps(review), encoding='utf-8')
            review_args = ['--review', str(review_file)]
        return self.call('commit', '--batch', str(bf), '--records', str(rf), '--brief', str(self.brief),
                         '--mode', 'bootstrap', '--run-id', run, *review_args)

    def state(self):
        return json.loads((self.archive / 'state.json').read_text(encoding='utf-8'))

    def assertOk(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_success_and_same_run_idempotence(self):
        result = self.assertOk(self.commit())
        self.assertFalse(result['already_saved'])
        self.assertTrue(Path(result['agent_export']['bundle']).is_file())
        self.assertEqual(result['agent_export']['counts']['items'], 1)
        self.assertEqual(result['agent_export']['ingestion_status'], 'unverified')
        self.assertTrue(self.assertOk(self.commit())['already_saved'])
        self.assertEqual(len(list((self.archive / 'runs').glob('*.json'))), 1)
        self.assertEqual(self.state()['sources']['thread-1']['event_ids'], ['evt_1'])

    def test_new_commit_without_review_or_with_unaccounted_event_is_rejected(self):
        for review in (None, []):
            result = self.commit(review=review)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('review', result.stderr.lower())
            self.assertFalse((self.archive / 'runs' / 'run-1.json').exists())
            self.assertFalse((self.archive / 'state.json').exists())

    def test_review_rejects_duplicate_unknown_context_and_ineligible_events(self):
        recorded = {'thread_id': 'thread-1', 'event_id': 'evt_1', 'disposition': 'recorded', 'item_ids': ['item-1']}
        for review in ([recorded, recorded], [dict(recorded, event_id='missing')],
                       [dict(recorded, thread_id='missing')]):
            self.assertNotEqual(self.commit(review=review).returncode, 0)
        self.batch['threads'][0]['events'].insert(0, {'event_id': 'context', 'context_only': True,
                                                   'time': '2026-10-15T01:00:00Z'})
        extra = {'thread_id': 'thread-1', 'event_id': 'context', 'disposition': 'ignored', 'reason': 'old'}
        self.assertNotEqual(self.commit(review=[recorded, extra]).returncode, 0)
        self.batch['coverage']['complete_local'] = False
        self.batch['threads'][0]['checkpoint']['eligible'] = False
        self.assertNotEqual(self.commit(records=[], review=[dict(extra, event_id='evt_1')]).returncode, 0)

    def test_review_requires_matching_items_and_explicit_skip_reason(self):
        base = {'thread_id': 'thread-1', 'event_id': 'evt_1'}
        for review in (dict(base, disposition='recorded', item_ids=['different']),
                       dict(base, disposition='recorded', item_ids=['item-1', 'item-1']),
                       dict(base, disposition='ignored', reason='Cannot ignore a cited event'),
                       dict(base, disposition='pending', reason='Cannot defer a cited event'),
                       dict(base, disposition='unsupported')):
            self.assertNotEqual(self.commit(review=[review]).returncode, 0)
        for disposition in ('ignored', 'pending'):
            for reason in (None, '', '   '):
                self.assertNotEqual(self.commit(records=[], review=[dict(base, disposition=disposition,
                                                                        reason=reason)]).returncode, 0)
        result = self.commit(records=[], review=[dict(base, disposition='ignored', reason='Pure status question')])
        self.assertOk(result)
        run = memory.read(self.archive / 'runs/run-1.json')
        self.assertEqual(run['event_reviews'][0]['reason'], 'Pure status question')
        self.assertEqual(self.state()['sources']['thread-1']['event_ids'], ['evt_1'])

    def test_pending_events_survive_rebuild_and_later_collection_then_resolve(self):
        sessions = self.root / 'sessions'
        sessions.mkdir()
        rows = [{'type': 'session_meta', 'payload': {'id': 'thread-1'}}]
        for stamp, content in [('2026-10-16T01:00:00Z', 'Completed work'),
                               ('2026-10-16T02:00:00Z', 'Needs further review')]:
            rows.append({'type': 'response_item', 'timestamp': stamp, 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': content}]}})
        (sessions / 'thread.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        cfg = {'archive_dir': str(self.archive), 'source_roots': [str(sessions)]}
        memory.write_json(self.cfg, cfg)
        batch = collector.collect(cfg, {}, collector.instant(self.batch['window']['since']),
                                  collector.instant(self.batch['window']['until']))
        first, pending = batch['threads'][0]['events']
        records = copy.deepcopy(self.records)
        records[0]['source_refs'][0]['event_ids'] = [first['event_id']]
        reviews = [dict(thread_id='thread-1', event_id=first['event_id'], disposition='recorded', item_ids=['item-1']),
                   dict(thread_id='thread-1', event_id=pending['event_id'], disposition='pending', reason='Need source context')]
        self.assertOk(self.commit(batch=batch, records=records, review=reviews))
        before = self.state()
        self.assertEqual(before['sources']['thread-1']['event_ids'], [first['event_id']])
        self.assertEqual(before['pending_review_since'], pending['time'])
        self.assertIsNone(before['scan_success_until'])
        self.assertFalse(memory.read(self.archive / 'runs/run-1.json')['coverage']['complete_local'])
        self.assertTrue(self.assertOk(self.commit(batch=batch, records=records, review=reviews))['already_saved'])
        self.assertOk(self.call('rebuild'))
        self.assertEqual(before, self.state())
        # Enrichment still validates the original batch, including deferred events.
        with memory.lock(self.archive):
            memory.enrichment(self.archive, 'run-1', 'details',
                              [dict(records[0], details={'context': 'Synthetic context'})], batch)
        later = dict(self.batch, window={'since': '2026-10-20T00:00:00Z', 'until': '2026-10-21T00:00:00Z'}, threads=[])
        self.assertOk(self.commit(run='later-scan', batch=later, records=[]))
        output = self.root / 'resume.json'
        result = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT.with_name('collect.py')),
                                 '--config', str(self.cfg), '--until', '2026-10-25T00:00:00Z', '--out', str(output)],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        resume = memory.read(output)
        self.assertLessEqual(collector.instant(resume['window']['since']), collector.instant(pending['time']))
        new_ids = [e['event_id'] for t in resume['threads'] for e in t['events'] if not e.get('context_only')]
        self.assertEqual(new_ids, [pending['event_id']])
        final_review = [dict(reviews[1], disposition='ignored', reason='Reviewed: no further work')]
        self.assertOk(self.commit(run='resumed', batch=resume, records=[], review=final_review))
        self.assertEqual(self.state()['pending_reviews'], [])
        self.assertIsNone(self.state()['pending_review_since'])
        self.assertEqual(self.state()['scan_success_until'], '2026-10-25T00:00:00Z')

    def test_changed_review_cannot_rewrite_frozen_run(self):
        review = [{'thread_id': 'thread-1', 'event_id': 'evt_1', 'disposition': 'ignored', 'reason': 'status'}]
        self.assertOk(self.commit(records=[], review=review))
        path = self.archive / 'runs/run-1.json'
        before = path.read_bytes()
        review[0]['reason'] = 'different'
        self.assertNotEqual(self.commit(records=[], review=review).returncode, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_pending_commit_recovers_after_view_failure(self):
        review = [{'thread_id': 'thread-1', 'event_id': 'evt_1', 'disposition': 'pending', 'reason': 'Not reviewed yet'}]
        bf, rf, vf = self.root / 'batch.json', self.root / 'records.json', self.root / 'review.json'
        memory.write_json(bf, self.batch)
        memory.write_json(rf, [])
        memory.write_json(vf, review)
        argv = [str(SCRIPT), 'commit', '--config', str(self.cfg), '--batch', str(bf), '--records', str(rf),
                '--review', str(vf), '--brief', str(self.brief), '--mode', 'bootstrap', '--run-id', 'run-1']
        with patch.object(sys, 'argv', argv), patch.object(memory.presentation, 'html_index', side_effect=OSError('view failure')):
            with self.assertRaisesRegex(OSError, 'view failure'):
                memory.main()
        path = self.archive / 'runs/run-1.json'
        frozen = path.read_bytes()
        self.assertFalse((self.archive / 'state.json').exists())
        self.assertTrue(self.assertOk(self.commit(records=[], review=review))['already_saved'])
        self.assertEqual(path.read_bytes(), frozen)
        self.assertEqual(self.state()['sources']['thread-1']['event_ids'], [])
        self.assertEqual(self.state()['pending_review_since'], '2026-10-16T01:00:00Z')

    def test_legacy_run_with_catalog_retries_without_review_but_cannot_create_new_run(self):
        path = self.archive / 'runs/run-1.json'
        # A real pre-export v1 run has no frozen Agent evidence to mutate.
        old = {'run_id': 'run-1', 'mode': 'bootstrap', 'window': self.batch['window'],
               'records': self.records, 'coverage': self.batch['coverage'],
               'checkpoints': {t['thread_id']: t['checkpoint'] for t in self.batch['threads']},
               'brief_text': self.brief.read_text(encoding='utf-8'),
               'source_catalog': memory.source_catalog(self.batch)}
        old['payload_hash'] = memory.digest(old)
        old.update(schema_version=1, created_at='2026-10-17T00:00:00Z')
        memory.write_json(path, old)
        before = path.read_bytes()
        self.assertTrue(self.assertOk(self.commit(review=None))['already_saved'])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(self.commit(run='new-legacy', review=None).returncode, 0)

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
