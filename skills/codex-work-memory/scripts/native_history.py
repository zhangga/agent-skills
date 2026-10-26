"""Read Codex's migrated SQLite history without modifying the application store."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote


def connect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def locator(path, thread_id):
    return Path(path).resolve().as_uri() + '#thread=' + quote(thread_id, safe='')


def thread_ids(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'thread_items', 'thread_turns'} <= tables:
        raise ValueError('Native history lacks thread_items or thread_turns')
    return {r[0] for r in conn.execute('SELECT DISTINCT thread_id FROM thread_items')}


def read_thread(path, thread_id, collector, *, conn=None, include_items=False):
    """Fingerprint every persisted item in one read transaction; expose no reasoning."""
    owned = conn is None
    if owned:
        conn = connect(path)
    try:
        turns = [dict(r) for r in conn.execute(
            'SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal,turn_id', (thread_id,))]
        finals = {t['turn_id']: t.get('final_agent_item_id') for t in turns}
        hasher = hashlib.sha256()
        hasher.update(json.dumps(turns, sort_keys=True, ensure_ascii=False).encode('utf-8'))
        events, items, errors = [], [], []
        count = 0
        for row in conn.execute('SELECT * FROM thread_items WHERE thread_id=? '
                                'ORDER BY rollout_ordinal,turn_id,item_id', (thread_id,)):
            raw = dict(row)
            count += 1
            hasher.update(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode('utf-8'))
            try:
                item = json.loads(raw['item_json'])
                if not isinstance(item, dict):
                    raise ValueError('item_json must be an object')
                kind = item.get('type')
                # Parse messages and evidence-worthy items only. Reasoning stays private.
                if include_items and kind in {'userMessage', 'agentMessage', 'commandExecution',
                                               'mcpToolCall', 'dynamicToolCall', 'fileChange'}:
                    items.append((raw, item))
                role, phase, text = None, None, ''
                if kind == 'userMessage':
                    role, phase = 'user', 'user'
                    text = collector.real_user_text(collector.message_text(item))
                elif kind == 'agentMessage' and (item.get('phase') in {'final', 'final_answer'}
                        or (item.get('phase') is None and finals.get(raw['turn_id']) == raw['item_id'])):
                    role, phase, text = 'assistant', 'final', item.get('text', '').strip()
                if not text:
                    continue
                # Agent text may stream: its completion, rather than first token, is the result time.
                stamp = raw.get('completed_at_ms') if role == 'assistant' else None
                stamp = stamp if stamp is not None else raw['created_at_ms']
                payload = {'id': raw['item_id']}
                evt = collector.event(thread_id, {'timestamp': stamp}, payload,
                                      role, phase, text, raw['turn_id'], raw['rollout_ordinal'])
                evt.pop('_native_id', None)
                evt.update(source_kind='sqlite', source_ordinal=raw['rollout_ordinal'],
                           source_item_id=raw['item_id'])
                events.append(evt)
            except (ValueError, TypeError, AttributeError, OverflowError) as exc:
                errors.append({'kind': 'native_history_parse', 'path': locator(path, thread_id),
                               'item_id': raw['item_id'], 'error': str(exc)})
        return sorted(events, key=lambda e: (collector.instant(e['time']), e['event_id'])), errors, hasher.hexdigest(), items, count
    finally:
        if owned:
            conn.close()
