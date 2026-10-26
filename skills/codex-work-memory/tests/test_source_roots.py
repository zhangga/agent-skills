"""Fresh profiles must advance while real missing-source errors remain visible."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('root_collector', SCRIPTS / 'collect.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class SourceRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.codex = self.root / 'codex'
        self.active, self.archived = self.codex / 'sessions', self.codex / 'archived_sessions'
        self.active.mkdir(parents=True)
        self.profile = self.root / 'config.json'
        self.archive = self.root / 'memory'
        self.command('setup.py', 'init', '--config', str(self.profile), '--codex-home', str(self.codex),
                     '--archive-dir', str(self.archive), '--initial-since', '2026-10-20T00:00:00Z')
        self.config = collector.configuration.load_config(self.profile)
        self.start, self.end = collector.instant('2026-10-20T00:00:00Z'), collector.instant('2026-10-27T00:00:00Z')

    def command(self, script, *args):
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / script), *args],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def scan(self, state=None):
        return collector.collect(self.config, state or {}, self.start, self.end)

    def commit_empty(self, day):
        output = self.root / 'batch.json'
        self.command('collect.py', '--config', str(self.profile), '--until', f'2026-10-{day:02}T00:00:00Z', '--out', str(output))
        records, review, brief = (self.root / name for name in ('records.json', 'review.json', 'brief.md'))
        records.write_text('[]', encoding='utf-8')
        review.write_text('[]', encoding='utf-8')
        brief.write_text('无新增工作记录', encoding='utf-8')
        self.command('memory.py', 'commit', '--config', str(self.profile), '--batch', str(output),
                     '--records', str(records), '--review', str(review), '--brief', str(brief), '--mode', 'daily', '--run-id', f'day-{day}')
        return json.loads((self.archive / 'state.json').read_text(encoding='utf-8'))

    def test_fresh_profile_without_archived_directory_advances_for_three_days(self):
        for day in (21, 22, 23):
            state = self.commit_empty(day)
            self.assertEqual(state['scan_success_until'], f'2026-10-{day:02}T00:00:00Z')
            self.assertEqual(state['source_roots_seen'], [str(self.active)])
        self.assertFalse(self.archived.exists())
        self.assertIn(str(self.archived), self.config['source_roots'])
        self.assertEqual(self.scan()['coverage']['absent_optional_source_roots'], [str(self.archived)])

    def test_archived_directory_created_later_is_automatically_collected(self):
        state = self.commit_empty(21)
        self.archived.mkdir()
        rows = [{'type': 'session_meta', 'payload': {'id': 'archived-work'}},
                {'type': 'response_item', 'timestamp': '2026-10-22T01:00:00Z', 'payload': {
                    'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '修复归档中的问题'}]}}]
        (self.archived / 'work.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        result = self.scan(state)
        self.assertTrue(result['coverage']['complete_local'])
        self.assertEqual([t['thread_id'] for t in result['threads']], ['archived-work'])
        self.assertEqual(result['coverage']['absent_optional_source_roots'], [])

    def test_previously_observed_archived_directory_disappearing_is_an_error(self):
        self.archived.mkdir()
        state = self.commit_empty(21)
        self.assertIn(str(self.archived), state['source_roots_seen'])
        self.archived.rmdir()  # Only this empty directory created by the test.
        result = self.scan(state)
        self.assertFalse(result['coverage']['complete_local'])
        self.assertEqual(result['coverage']['errors'][0]['kind'], 'missing_source_root')

    def test_legacy_successful_runs_preserve_known_root_existence_on_rebuild(self):
        self.archived.mkdir()
        self.commit_empty(21)
        path = self.archive / 'runs/day-21.json'
        run = json.loads(path.read_text(encoding='utf-8'))
        run['coverage'].pop('source_roots_present')
        path.write_text(json.dumps(run), encoding='utf-8')
        self.command('memory.py', 'rebuild', '--config', str(self.profile))
        state = json.loads((self.archive / 'state.json').read_text(encoding='utf-8'))
        self.assertIn(str(self.archived), state['source_roots_seen'])

    def test_active_and_custom_missing_roots_still_block_coverage(self):
        self.active.rmdir()
        self.assertFalse(self.scan()['coverage']['complete_local'])
        self.active.mkdir()
        self.config['source_roots'].append(str(self.root / 'custom' / 'archived_sessions'))
        result = self.scan()
        self.assertFalse(result['coverage']['complete_local'])
        self.assertEqual(len(result['coverage']['errors']), 1)
        self.assertIn('custom', result['coverage']['errors'][0]['path'])

    def test_legacy_successful_state_does_not_hide_a_missing_archive_on_first_upgrade_scan(self):
        result = self.scan({'scan_success_until': '2026-10-21T00:00:00Z'})
        self.assertFalse(result['coverage']['complete_local'])
        self.assertEqual(result['coverage']['errors'][0]['path'], str(self.archived))

    def test_archived_file_permission_and_enumeration_errors_are_not_optional(self):
        self.archived.write_text('not a directory', encoding='utf-8')
        self.assertFalse(self.scan()['coverage']['complete_local'])
        self.archived.unlink()
        original_stat = Path.stat
        def denied(path, *args, **kwargs):
            if path == self.archived:
                raise PermissionError('synthetic access denial')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', denied):
            result = self.scan()
        self.assertIn('source_root_read', [e['kind'] for e in result['coverage']['errors']])
        self.archived.mkdir()
        original_walk = collector.os.walk
        def unreadable(root, *, onerror, **kwargs):
            if root == self.archived:
                onerror(PermissionError('synthetic enumeration denial'))
                return iter(())
            return original_walk(root, onerror=onerror, **kwargs)
        with patch.object(collector.os, 'walk', unreadable):
            result = self.scan()
        self.assertIn('enumeration', [e['kind'] for e in result['coverage']['errors']])

    def test_metadata_pointing_to_missing_archived_log_still_blocks_coverage(self):
        metadata = {'lost': {'updated_at': '2026-10-25T00:00:00Z', 'archived': True,
                             'rollout_path': str(self.archived / 'lost.jsonl')}}
        with patch.object(collector, 'load_metadata', return_value=(metadata, {})):
            result = self.scan()
        self.assertFalse(result['coverage']['complete_local'])
        self.assertEqual(result['coverage']['errors'][0]['kind'], 'metadata_source_unavailable')


if __name__ == '__main__':
    unittest.main()
