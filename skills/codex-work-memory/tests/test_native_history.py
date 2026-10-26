import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location('native_test_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector, evidence, memory = load('collect'), load('evidence'), load('memory')


class NativeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('sessions', 'archived_sessions'):
            (self.root / name).mkdir()
        self.path = self.root / 'thread_history_1.sqlite'
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript('''
            CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,
                created_at_ms INTEGER,item_json TEXT,item_type TEXT,started_at_ms INTEGER,completed_at_ms INTEGER);
            CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,
                final_agent_item_id TEXT);
        ''')
        self.config = {'codex_home': str(self.root), 'source_roots': [str(self.root / n)
                       for n in ('sessions', 'archived_sessions')]}
        self.start = collector.instant('2026-10-01T00:00:00Z')
        self.end = collector.instant('2026-10-02T00:00:00Z')
        self.base_ms = int(self.start.timestamp() * 1000)
        self.add('u', 'userMessage', {'content': [{'type': 'text', 'text': 'Fix timeout'}]}, 1)
        self.add('tool', 'commandExecution', {'command': 'run tests', 'status': 'completed',
                 'aggregatedOutput': '3 tests passed; token=private-value', 'exitCode': 0}, 2)
        self.add('a', 'agentMessage', {'text': 'Fixed timeout; tests passed', 'phase': 'final_answer'}, 3)
        self.conn.execute('INSERT INTO thread_turns VALUES(?,?,?,?)', ('t', 'turn', 0, 'a'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def add(self, item_id, kind, fields, ordinal, tid='t', turn='turn', delta=None):
        item = dict(fields, type=kind, id=item_id)
        millis = self.base_ms + (ordinal if delta is None else delta) * 1000
        self.conn.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?,?)',
            (tid, turn, item_id, ordinal, millis, json.dumps(item), kind, millis, millis + 10))

    def collect(self, state=None):
        return collector.collect(self.config, state or {}, self.start, self.end)

    def test_native_messages_are_bounded_and_final_only(self):
        self.add('c', 'agentMessage', {'text': 'Working', 'phase': 'commentary'}, 4)
        self.add('r', 'reasoning', {'text': 'Private reasoning'}, 5)
        self.add('future', 'userMessage', {'content': [{'type': 'text', 'text': 'Tomorrow'}]},
                 6, delta=86400)
        self.conn.commit()
        batch = self.collect()
        self.assertTrue(batch['coverage']['complete_local'])
        thread = batch['threads'][0]
        self.assertEqual([e['text'] for e in thread['events']], ['Fix timeout', 'Fixed timeout; tests passed'])
        self.assertEqual(thread['source_kind'], 'sqlite')
        self.assertTrue(thread['source_path'].startswith(self.path.as_uri() + '#thread='))
        self.assertNotIn('Private reasoning', json.dumps(batch))
        self.assertEqual(thread['events'][1]['time'], '2026-10-01T00:00:03.010000Z')

    def test_evidence_hash_is_per_thread_and_verifies_tool_results(self):
        batch = self.collect()
        thread = batch['threads'][0]
        selection = {'t': [thread['events'][-1]['event_id']]}
        bundle = evidence.extract(batch, selection)
        result = next(e for e in bundle['entries'] if e['kind'] == 'tool_result')
        self.assertIn('3 tests passed', result['excerpt'])
        self.assertIn('[REDACTED]', result['excerpt'])
        self.assertNotIn('private-value', result['excerpt'])
        self.add('other', 'userMessage', {'content': [{'type': 'text', 'text': 'unrelated'}]},
                 1, tid='other')
        self.conn.commit()
        self.assertEqual(bundle, evidence.extract(batch, selection))
        self.conn.execute("UPDATE thread_items SET item_json=? WHERE item_id='tool'",
                          (json.dumps({'type': 'commandExecution', 'id': 'tool',
                                       'aggregatedOutput': 'Tests failed'}),))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'changed'):
            evidence.extract(batch, selection)

    def test_jsonl_precedence_and_migration_signatures(self):
        rows = [
            {'type': 'session_meta', 'payload': {'id': 't'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn'}},
            {'timestamp': '2026-10-01T00:00:01Z', 'type': 'response_item',
             'payload': {'type': 'message', 'id': 'old-user-id', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': 'Fix timeout'}]}},
            {'timestamp': '2026-10-01T00:00:03Z', 'type': 'response_item',
             'payload': {'type': 'message', 'id': 'old-agent-id', 'role': 'assistant', 'phase': 'final',
                         'content': [{'type': 'output_text', 'text': 'Fixed timeout; tests passed'}]}}
        ]
        path = self.root / 'sessions' / 'old.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        first = self.collect()
        self.assertEqual(len(first['threads']), 1)
        self.assertNotIn('source_kind', first['threads'][0])
        old_ids = first['threads'][0]['checkpoint']['event_ids']
        seed = self.collect({'sources': {'t': {'event_ids': old_ids}}})
        self.assertEqual(seed['threads'][0]['events'], [])
        _, checkpoints, _ = memory.reviewed_checkpoints([], seed, [])
        self.assertEqual(len(checkpoints['t']['message_signatures']), 2)
        path.unlink()
        migrated = self.collect({'sources': {'t': dict(checkpoints['t'], event_ids=old_ids)}})
        self.assertEqual(migrated['coverage']['counts']['new_events'], 0)

    def test_pending_messages_are_not_consumed_as_migration_signatures(self):
        batch = self.collect()
        user, agent = batch['threads'][0]['events']
        reviews = [{'thread_id': 't', 'event_id': user['event_id'], 'disposition': 'ignored', 'reason': 'reviewed'},
                   {'thread_id': 't', 'event_id': agent['event_id'], 'disposition': 'pending', 'reason': 'need review'}]
        _, checkpoints, coverage = memory.reviewed_checkpoints([], batch, reviews)
        self.assertEqual(len(checkpoints['t']['message_signatures']), 1)
        self.assertEqual(coverage['pending_review_events'], 1)
        next_batch = self.collect({'sources': checkpoints})
        self.assertEqual([e['event_id'] for e in next_batch['threads'][0]['events'] if not e.get('context_only')],
                         [agent['event_id']])

    def test_metadata_exclusions_apply_to_native_history(self):
        self.config['excluded_thread_ids'] = ['t']
        self.assertEqual(self.collect()['threads'], [])
        self.config.pop('excluded_thread_ids')
        self.config['thread_metadata'] = {'t': {'hostId': 'devbox'}}
        self.assertEqual(self.collect()['threads'], [])
        self.config.pop('thread_metadata')
        meta = sqlite3.connect(self.root / 'state_5.sqlite')
        meta.execute('CREATE TABLE threads(id TEXT,source TEXT,name TEXT,title TEXT,updated_at INTEGER)')
        meta.execute('INSERT INTO threads VALUES(?,?,?,?,?)',
                     ('t', json.dumps({'subagent': {}}), 'new name', 'old title', self.base_ms // 1000))
        meta.commit()
        meta.close()
        self.assertEqual(self.collect()['threads'], [])

    def test_unknown_native_schema_is_a_coverage_gap(self):
        self.conn.execute('DROP TABLE thread_turns')
        self.conn.commit()
        batch = self.collect()
        self.assertFalse(batch['coverage']['complete_local'])
        self.assertEqual(batch['coverage']['errors'][0]['kind'], 'native_history_read')


if __name__ == '__main__':
    unittest.main()
