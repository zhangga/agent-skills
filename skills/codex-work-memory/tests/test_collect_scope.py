"""Coverage errors must belong to sources within the selected scope."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('scope_collector',
    Path(__file__).resolve().parents[1] / 'scripts/collect.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class CollectionScopeTests(unittest.TestCase):
    def test_excluded_subagent_and_remote_parse_errors_do_not_block_local_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for tid, extra in [('normal', {}), ('maintenance', {}),
                               ('child', {'source': {'subagent': {}}}), ('remote', {})]:
                rows = [{'type': 'session_meta', 'payload': dict(id=tid, **extra)},
                        {'type': 'response_item', 'timestamp': '2026-10-16T01:00:00Z', 'payload': {
                            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Synthetic work'}]}}]
                (root / (tid + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in rows) + '\n'
                                                   + ('{broken\n' if tid != 'normal' else ''), encoding='utf-8')
            cfg = {'source_roots': [str(root)], 'excluded_thread_ids': ['maintenance'],
                   'thread_metadata': {'remote': {'hostId': 'remote-host'}}}
            start, end = collector.instant('2026-10-16T00:00:00Z'), collector.instant('2026-10-17T00:00:00Z')
            batch = collector.collect(cfg, {}, start, end)
            self.assertTrue(batch['coverage']['complete_local'])
            self.assertEqual(batch['coverage']['errors'], [])
            self.assertEqual([t['thread_id'] for t in batch['threads']], ['normal'])
            counts = batch['coverage']['counts']
            self.assertEqual(counts['files_with_errors'], 0)
            self.assertEqual((counts['excluded_threads'], counts['subagent_threads_skipped'], counts['remote_threads_skipped']), (1, 1, 1))
            # A broken selected source is still ineligible and blocks coverage.
            with (root / 'normal.jsonl').open('a', encoding='utf-8') as handle:
                handle.write('{broken\n')
            broken = collector.collect(cfg, {}, start, end)
            self.assertFalse(broken['coverage']['complete_local'])
            self.assertFalse(broken['threads'][0]['checkpoint']['eligible'])
            self.assertEqual(broken['coverage']['counts']['files_with_errors'], 1)
            # Without an identifiable thread, exclusion cannot be established.
            (root / 'unknown.jsonl').write_text('{broken\n', encoding='utf-8')
            unknown = collector.collect(cfg, {}, start, end)
            self.assertIn('missing_thread_id', [e['kind'] for e in unknown['coverage']['errors']])


if __name__ == '__main__':
    unittest.main()
