"""Synthetic cross-day work, literal search and reversible association tests."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/memory.py'
spec = importlib.util.spec_from_file_location('retrieval_memory', SCRIPT)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


def record(iid, day, *, project='p1', title='修复请求超时', status='进行中', thread='t1', summary='定位 timeout'):
    return {'item_id': iid, 'project_id': project, 'project_name': '服务端' if project == 'p1' else '客户端',
            'occurred_at': f'2026-10-{day:02}T01:00:00Z', 'title': title, 'summary': summary,
            'category': '排障', 'status': status, 'follow_up': '',
            'source_refs': [{'thread_id': thread, 'event_ids': [f'{iid}-{day}']}]}


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'archive'
        self.config = self.root / 'config.json'
        memory.write_json(self.config, {'archive_dir': str(self.archive)})
        early = record('timeout', 16)
        early['details'] = {'artifacts': [{'label': '日志', 'path': 'C:/work/error[1].txt'}],
                            'validation': ['仅离线检查']}
        self.save('day-1', [early, record('independent', 16, title='调整连接池', thread='t2', summary='pool size')])
        self.save('day-2', [record('timeout', 17, status='已验证', summary='完成上线验证'),
                            record('duplicate', 17, title='请求超时后续验证', thread='t3', summary='第二个会话继续处理')])
        self.save('day-3', [record('timeout', 18, project='p2', thread='t4', summary='客户端独立问题')])
        memory.rebuild(self.archive)

    def save(self, rid, records):
        run = {'run_id': rid, 'mode': 'daily', 'created_at': f'2026-10-{15 + int(rid[-1]):02}T09:00:00+08:00',
               'window': {'since': '2026-10-16T00:00:00Z', 'until': '2026-10-20T00:00:00Z'},
               'records': records, 'coverage': {'complete_local': True}, 'brief_text': 'Frozen brief',
               'checkpoints': {}, 'source_catalog': {}, 'schema_version': 1}
        for r in records:
            ref = r['source_refs'][0]
            run['source_catalog'][ref['thread_id']] = {'title': '源会话 ' + ref['thread_id'],
                'source_path': 'C:/logs/' + ref['thread_id'] + '.jsonl',
                'events': {ref['event_ids'][0]: r['occurred_at']}}
        run['payload_hash'] = memory.digest({k: v for k, v in run.items() if k not in {'created_at', 'schema_version'}})
        memory.write_json(self.archive / 'runs' / (rid + '.json'), run)

    def call(self, *args):
        return subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPT), *args, '--config', str(self.config)],
                              capture_output=True, text=True, encoding='utf-8')

    def ok(self, *args):
        result = self.call(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def link(self, rid='merge', item='duplicate', target='timeout'):
        with memory.lock(self.archive):
            return memory.associate(self.archive, rid, 'p1', item, target, 'Same incident confirmed from sources')

    def test_project_keyword_status_and_dates_must_match_one_revision(self):
        old = self.ok('query', '--project', 'p1', '--keyword', 'TIMEOUT', '--status', '进行中',
                      '--since', '2026-10-16T00:00:00Z', '--until', '2026-10-17T00:00:00Z')
        self.assertEqual([r['item_id'] for r in old['records']], ['timeout'])
        self.assertFalse(old['matches'][0]['is_current'])
        self.assertEqual(old['matches'][0]['current']['status'], '已验证')
        self.assertEqual(self.ok('query', '--project', 'p1', '--keyword', 'timeout', '--status', '已验证')['total'], 0)
        self.assertEqual(self.ok('query', '--project', 'p1', '--keyword', 'timeout',
                                 '--since', '2026-10-17T00:00:00Z')['total'], 0)
        self.assertEqual(self.ok('query', '--keyword', 'timeout 上线')['total'], 0)

    def test_fullwidth_and_literal_terms_search_details_and_source_titles(self):
        result = self.ok('query', '--keyword', 'ＴＩＭＥＯＵＴ error[1].txt 源会话')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['records'][0]['item_id'], 'timeout')
        self.assertEqual(self.ok('query', '--keyword', 'error.*')['total'], 0)

    def test_latest_filters_current_state_and_history_exposes_current_state(self):
        result = self.ok('query', '--latest', '--item-id', 'timeout')
        self.assertEqual(result['total'], 2)  # One item in each project.
        by_project = {r['project_id']: r for r in result['records']}
        self.assertEqual(by_project['p1']['status'], '已验证')
        self.assertEqual(self.ok('query', '--project', '服务端', '--latest', '--keyword', 'timeout')['total'], 0)
        current_open = self.ok('query', '--project', 'p1', '--item-id', 'timeout', '--latest', '--status', '进行中')
        self.assertEqual(current_open['total'], 0)
        old = self.ok('query', '--project', '服务端', '--keyword', 'timeout')
        self.assertEqual(old['records'][0]['status'], '进行中')
        self.assertEqual(old['matches'][0]['current']['status'], '已验证')

    def test_pagination_counts_and_stable_order(self):
        full = self.ok('query')
        first = self.ok('query', '--limit', '2')
        second = self.ok('query', '--limit', '2', '--offset', '2')
        last = self.ok('query', '--limit', '2', '--offset', '4')
        self.assertEqual(first['total'], 5)
        self.assertEqual(first['records'] + second['records'] + last['records'], full['records'])
        self.assertTrue(first['has_more'])
        self.assertFalse(last['has_more'])
        self.assertEqual(self.ok('query', '--offset', '99')['records'], [])
        self.assertEqual(self.ok('query', '--order', 'oldest')['records'], list(reversed(full['records'])))

    def test_thread_filter_and_bounded_source_catalog(self):
        result = self.ok('query', '--thread-id', 't1', '--limit', '1')
        self.assertEqual(result['total'], 2)
        self.assertEqual(set(result['source_catalog']), {'t1'})
        self.assertEqual(set(result['source_catalog']['t1']['events']), {'timeout-17'})

    def test_default_query_bounds_records_details_and_coverage_at_scale(self):
        records = [record(f'bulk-{i}', 19, thread=f'bulk-thread-{i}', summary='长摘要' * 2000) for i in range(200)]
        for entry in records:
            entry['details'] = {'context': 'Details must stay on disk. ' * 1000}
        self.save('day-4', records)
        # Realistic large catalogs and coverage must not leak into a page.
        run = memory.read(self.archive / 'runs/day-4.json')
        for i in range(300):
            run['source_catalog'].setdefault(f'bulk-thread-{i}', {'title': 'source'})['events'] = {
                f'bulk-{i}-19': records[0]['occurred_at'], **{f'old-{j}': records[0]['occurred_at'] for j in range(100)}}
        run['coverage']['errors'] = [{'error': 'large historical diagnostic ' * 1000} for _ in range(50)]
        memory.write_json(self.archive / 'runs/day-4.json', run)
        response = self.call('query')
        self.assertEqual(response.returncode, 0, response.stderr)
        result = json.loads(response.stdout)
        self.assertEqual((result['total'], result['returned'], result['limit']), (205, 20, 20))
        self.assertTrue(result['has_more'])
        self.assertEqual(result['view'], 'summary')
        self.assertTrue(all('details' not in r for r in result['records']))
        self.assertTrue(all(r['details_available'] for r in result['records']))
        self.assertIn('summary', result['records'][0]['truncated_fields'])
        self.assertEqual(result['coverage'], [])
        self.assertEqual(result['coverage_summary']['error_count'], 50)
        self.assertLess(len(response.stdout.encode('utf-8')), 150_000)
        self.assertEqual(len(result['source_catalog']), 20)
        self.assertEqual(sum(len(s['events']) for s in result['source_catalog'].values()), 20)
        empty = self.ok('query', '--since', '2026-10-26T00:00:00Z')
        self.assertEqual((empty['records'], empty['source_catalog'], empty['evidence_catalog']), ([], {}, {}))
        self.assertLess(len(json.dumps(empty)), 2000)

    def test_full_export_requires_file_and_preserves_all_filtered_records(self):
        self.save('day-4', [record(f'bulk-{i}', 19) for i in range(25)])
        out = self.root / 'full.json'
        for args in [('--all',), ('--coverage', 'full'),
                     ('--all', '--out', str(out), '--limit', '3'),
                     ('--all', '--out', str(out), '--offset', '2'),
                     ('--view', 'typo')]:
            self.assertNotEqual(self.call('query', *args).returncode, 0)
        self.assertFalse(out.exists())
        response = self.ok('query', '--project', 'p1', '--all', '--view', 'full', '--coverage', 'full', '--out', str(out))
        self.assertEqual(set(response), {'ok', 'out'})
        result = memory.read(out)
        self.assertEqual(result['returned'], 29)
        self.assertIsNone(result['limit'])
        self.assertFalse(result['has_more'])
        original = next(r for r in result['records'] if r['item_id'] == 'timeout' and r['status'] == '进行中')
        self.assertEqual(original['details']['validation'], ['仅离线检查'])
        self.assertTrue(result['coverage_detail_included'])
        self.assertEqual(len(result['coverage']), 4)
        # Writing to a file does not implicitly remove the page limit.
        self.ok('query', '--out', str(out))
        self.assertEqual(memory.read(out)['returned'], 20)

    def test_summary_previews_sources_but_full_view_preserves_them(self):
        original = record('long-sources', 19)
        original['source_refs'] = [{'thread_id': f't-{i}', 'event_ids': [f'e-{j}' for j in range(20)]} for i in range(10)]
        self.save('day-4', [original])
        preview = self.ok('query', '--item-id', 'long-sources')['records'][0]
        self.assertEqual(preview['source_ref_count'], 10)
        self.assertEqual(len(preview['source_refs']), 5)
        self.assertEqual(preview['source_refs'][0]['event_count'], 20)
        self.assertEqual(len(preview['source_refs'][0]['event_ids']), 5)
        self.assertIn('source_refs', preview['truncated_fields'])
        full = self.ok('query', '--item-id', 'long-sources', '--view', 'full')['records'][0]
        self.assertEqual(full, original)

    def test_default_pagination_can_retrieve_every_result_for_weekly_summary(self):
        self.save('day-4', [record(f'bulk-{i}', 19) for i in range(47)])
        collected, offset = [], 0
        while True:
            page = self.ok('query', '--offset', str(offset))
            collected.extend(page['records'])
            if not page['has_more']:
                break
            offset += page['returned']
        self.assertEqual(len(collected), 52)
        self.assertEqual(len({(r['project_id'], r['item_id'], r['occurred_at']) for r in collected}), 52)
        self.assertEqual(page['total'], 52)

    def test_invalid_query_windows_and_pagination_fail(self):
        for options in [('--limit', '0'), ('--limit', '-2'), ('--offset', '-1'),
                        ('--since', '2026-10-16'), ('--since', '2026-10-20T00:00:00Z', '--until', '2026-10-16T00:00:00Z')]:
            self.assertNotEqual(self.call('query', *options).returncode, 0)
        self.assertNotEqual(self.call('query', '--title', 'ignored accidentally').returncode, 0)
        self.assertNotEqual(self.call('link', '--project', 'p1', '--item-id', 'duplicate', '--target-item', 'timeout',
                                      '--reason', 'test', '--run-id', 'bad-filter', '--status', '已完成').returncode, 0)

    def test_duplicate_project_names_require_id(self):
        run = memory.read(self.archive / 'runs/day-3.json')
        run['records'][0]['project_name'] = '服务端'
        memory.write_json(self.archive / 'runs/day-3.json', run)
        self.assertNotEqual(self.call('query', '--project', '服务端').returncode, 0)
        self.assertEqual(self.ok('query', '--project', 'p2')['total'], 1)
        self.assertEqual(self.ok('query', '--project', 'not-found')['total'], 0)

    def test_candidates_rank_same_project_titles_and_show_evidence(self):
        found = self.ok('candidates', '--project', 'p1', '--title', '修复请求超时', '--thread-id', 't1')
        self.assertEqual(found['candidates'][0]['item_id'], 'timeout')
        self.assertEqual(found['candidates'][0]['latest']['status'], '已验证')
        self.assertEqual(found['candidates'][0]['revision_count'], 2)
        self.assertIn('标题完全匹配', found['candidates'][0]['match_reasons'])
        self.assertEqual({c['project_id'] for c in found['candidates']}, {'p1'})
        self.assertNotIn('independent', [c['item_id'] for c in found['candidates']])
        self.assertEqual(self.ok('candidates', '--project', 'p1', '--thread-id', 't2')['candidates'][0]['item_id'], 'independent')
        self.assertEqual(self.ok('candidates', '--project', 'new-project', '--title', 'new')['candidates'], [])
        self.assertNotEqual(self.call('candidates', '--title', 'x').returncode, 0)
        self.assertNotEqual(self.call('candidates', '--project', 'p1').returncode, 0)

    def test_equal_times_choose_same_latest_revision_in_queries_candidates_and_views(self):
        run = memory.read(self.archive / 'runs/day-2.json')
        # Same instant in a different timezone and later ledger creation time.
        run['records'][0]['occurred_at'] = '2026-10-16T09:00:00+08:00'
        memory.write_json(self.archive / 'runs/day-2.json', run)
        result = self.ok('query', '--project', 'p1', '--item-id', 'timeout', '--latest')
        self.assertEqual(result['records'][0]['status'], '已验证')
        found = self.ok('candidates', '--project', 'p1', '--title', '修复请求超时')
        self.assertEqual(found['candidates'][0]['latest']['status'], '已验证')
        records = [r for run in memory.resolved_runs(self.archive) for r in run['records']]
        self.assertEqual(memory.presentation.items_grouped(records)['p1']['items']['timeout'][0]['status'], '已验证')

    def test_query_and_candidates_leave_archive_unchanged_and_support_out(self):
        before = {p: p.read_bytes() for p in self.archive.rglob('*') if p.is_file()}
        output = self.root / 'query.json'
        self.ok('query', '--project', 'p1', '--limit', '1', '--out', str(output))
        self.assertEqual(memory.read(output)['returned'], 1)
        self.ok('candidates', '--project', 'p1', '--title', '超时')
        self.assertEqual(before, {p: p.read_bytes() for p in self.archive.rglob('*') if p.is_file()})

    def test_link_groups_history_preserves_sources_state_and_frozen_export(self):
        weekly = {'run_id': 'weekly', 'mode': 'weekly', 'created_at': '2026-10-20T00:00:00Z',
                  'window': {'since': '2026-10-16T00:00:00Z', 'until': '2026-10-20T00:00:00Z'},
                  'records': [], 'brief_text': 'Frozen weekly', 'report_text': 'Original report'}
        weekly['payload_hash'] = memory.digest(weekly)
        memory.write_json(self.archive / 'runs/weekly.json', weekly)
        memory.agent_export.freeze(self.archive, weekly, memory.resolved_runs(self.archive))
        protected = list((self.archive / 'runs').glob('*.json')) + list((self.archive / 'briefs').glob('*.md'))
        protected += [self.archive / 'state.json', self.archive / 'weekly/weekly.agent-evidence.json']
        before = {p: p.read_bytes() for p in protected}
        self.assertFalse(self.link()['already_saved'])
        self.assertEqual(before, {p: p.read_bytes() for p in protected})
        result = self.ok('query', '--project', 'p1', '--item-id', 'timeout')
        self.assertEqual(result['total'], 3)
        self.assertEqual(self.ok('query', '--project', 'p1', '--item-id', 'duplicate')['total'], 3)
        self.assertIn('duplicate', [r.get('original_item_id') for r in result['records']])
        groups = memory.presentation.items_grouped([r for run in memory.resolved_runs(self.archive) for r in run['records']])
        self.assertEqual(set(groups['p1']['items']), {'timeout', 'independent'})
        self.assertEqual(len(groups['p1']['items']['timeout']), 3)
        self.assertEqual(len(groups['p2']['items']['timeout']), 1)
        found = self.ok('candidates', '--project', 'p1', '--thread-id', 't3')['candidates']
        self.assertEqual(found[0]['item_id'], 'timeout')
        self.assertEqual(found[0]['aliases'], ['duplicate'])
        self.assertTrue(self.link()['already_saved'])

    def test_unlink_and_retarget_are_explicit_reversible_corrections(self):
        self.link()
        self.ok('unlink', '--project', 'p1', '--item-id', 'duplicate', '--reason', 'Different incident', '--run-id', 'split')
        self.assertEqual(self.ok('query', '--project', 'p1', '--latest')['total'], 3)
        self.link('corrected', target='independent')
        self.assertEqual(self.ok('query', '--project', 'p1', '--item-id', 'independent')['total'], 2)
        self.assertEqual(len(list((self.archive / 'item_links').glob('*.json'))), 3)

    def test_chain_links_and_unlink_replay_without_losing_original_ids(self):
        self.link()
        self.link('chain', item='timeout', target='independent')
        self.assertEqual(memory.item_aliases(self.archive)[('p1', 'duplicate')], 'independent')
        self.assertEqual(self.ok('query', '--project', 'p1', '--latest')['total'], 1)
        self.ok('unlink', '--project', 'p1', '--item-id', 'timeout', '--reason', 'Undo last merge', '--run-id', 'unchain')
        self.assertEqual(memory.item_aliases(self.archive)[('p1', 'duplicate')], 'timeout')
        self.assertEqual(self.ok('query', '--project', 'p1', '--latest')['total'], 2)
        # An old retry must not undo the newer correction.
        self.assertTrue(self.link('chain', item='timeout', target='independent')['already_saved'])
        self.assertEqual(memory.item_aliases(self.archive)[('p1', 'duplicate')], 'timeout')

    def test_tampered_association_journal_is_rejected(self):
        self.link()
        path = self.archive / 'item_links/merge.json'
        entry = memory.read(path)
        entry['target_item_id'] = 'independent'
        memory.write_json(path, entry)
        result = self.call('query')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('hash is invalid', result.stderr)

    def test_link_rejects_cycles_unknown_cross_project_and_changed_retry(self):
        for item, target in [('timeout', 'timeout'), ('missing', 'timeout'), ('timeout', 'missing')]:
            with self.assertRaises(ValueError):
                self.link('invalid', item=item, target=target)
        self.link()
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.link('cycle', item='timeout', target='duplicate')
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.link('merge', target='independent')
        with memory.lock(self.archive), self.assertRaisesRegex(ValueError, 'same exact project'):
            memory.associate(self.archive, 'cross-project', 'p2', 'timeout', 'duplicate', 'invalid')
        self.assertNotEqual(self.call('link', '--project', 'p1', '--item-id', 'duplicate', '--target-item', 'timeout',
                                      '--reason', 'test', '--run-id', '../escape').returncode, 0)

    def test_link_failure_after_journal_write_recovers_without_changing_state(self):
        state = (self.archive / 'state.json').read_bytes()
        with patch.object(memory.presentation, 'html_index', side_effect=OSError('view failure')):
            with self.assertRaises(OSError):
                self.link()
        journal = (self.archive / 'item_links/merge.json').read_bytes()
        self.assertTrue(self.link()['already_saved'])
        self.assertEqual((self.archive / 'item_links/merge.json').read_bytes(), journal)
        self.assertEqual((self.archive / 'state.json').read_bytes(), state)

    def test_new_progress_uses_canonical_id_and_continues_across_days(self):
        self.link()
        fresh = record('duplicate', 19, status='已完成', thread='t5')
        batch = {'window': {'since': '2026-10-19T00:00:00Z', 'until': '2026-10-20T00:00:00Z'},
                 'coverage': {'complete_local': True}, 'threads': [{'thread_id': 't5',
                 'events': [{'event_id': 'fresh', 'time': fresh['occurred_at']}],
                 'checkpoint': {'eligible': True, 'event_ids': ['fresh'], 'last_event_at': fresh['occurred_at']}}]}
        fresh['source_refs'][0]['event_ids'] = ['fresh']
        bf, rf, vf, brief = [self.root / name for name in ('batch.json', 'record.json', 'review.json', 'brief.md')]
        memory.write_json(bf, batch)
        brief.write_text('New progress', encoding='utf-8')
        command = ('commit', '--batch', str(bf), '--records', str(rf), '--review', str(vf), '--brief', str(brief),
                   '--mode', 'daily', '--run-id', 'next-day')
        for iid, expected in [('duplicate', False), ('timeout', True)]:
            fresh['item_id'] = iid
            memory.write_json(rf, [fresh])
            memory.write_json(vf, [{'thread_id': 't5', 'event_id': 'fresh', 'disposition': 'recorded', 'item_ids': [iid]}])
            result = self.call(*command)
            self.assertEqual(result.returncode == 0, expected, result.stderr)
        latest = self.ok('query', '--project', 'p1', '--item-id', 'timeout', '--latest')
        self.assertEqual(latest['total'], 1)
        self.assertEqual(latest['records'][0]['status'], '已完成')


if __name__ == '__main__':
    unittest.main()
