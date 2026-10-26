"""Read-only record search and explainable, project-scoped item candidates."""
import copy
from datetime import datetime, timezone
import re
import unicodedata


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Query timestamps require an explicit timezone')
    return result


def normalise(value):
    return unicodedata.normalize('NFKC', str(value)).casefold()


def values(value):
    if isinstance(value, dict):
        return '\n'.join(values(v) for v in value.values())
    if isinstance(value, list):
        return '\n'.join(values(v) for v in value)
    return str(value) if value is not None else ''


def entries(ledger):
    result = []
    for run in sorted(ledger, key=lambda r: (instant(r['created_at']), r['run_id'])):
        if run['mode'] == 'weekly':
            continue
        for index, record in enumerate(run['records']):
            result.append({'record': record, 'run_id': run['run_id'], 'record_index': index,
                           'order': (instant(record['occurred_at']), instant(run['created_at']), run['run_id'], index)})
    return result


def item_key(entry):
    record = entry['record']
    return record['project_id'], record['item_id']


def project_ids(all_entries, selectors):
    if not selectors:
        return None
    ids = {e['record']['project_id'] for e in all_entries}
    selected = set()
    for value in selectors:
        if value in ids:
            selected.add(value)
            continue
        matches = {e['record']['project_id'] for e in all_entries
                   if normalise(e['record']['project_name']) == normalise(value)}
        if len(matches) > 1:
            raise ValueError('Ambiguous project name; use an exact project ID: ' + value)
        selected.update(matches)
    return selected


def searchable(record, catalog):
    sources = [catalog.get(ref['thread_id'], {}) for ref in record.get('source_refs', [])]
    # IDs have dedicated exact filters. Including them in prose search would
    # make an old keyword embedded in a stable ID match every future revision.
    body = {key: record.get(key) for key in ('title', 'summary', 'outcome', 'follow_up', 'knowledge',
                                            'details', 'tags', 'category', 'status', 'project_name')}
    return normalise(values(body) + '\n' + '\n'.join(
        str(source.get('title', '')) + '\n' + str(source.get('source_path', '')) for source in sources))


def page(items, limit, offset):
    if limit is not None and limit < 1:
        raise ValueError('limit must be positive')
    if offset < 0:
        raise ValueError('offset must be non-negative')
    selected = items[offset:offset + limit] if limit is not None else items[offset:]
    return selected, {'total': len(items), 'returned': len(selected), 'offset': offset,
                      'limit': limit, 'has_more': offset + len(selected) < len(items)}


def summary_fields(value, keys):
    result, truncated = {}, []
    for key in keys:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str) and len(item) > 600:
            item = item[:600] + '…'
            truncated.append(key)
        result[key] = item
    if truncated:
        result['truncated_fields'] = truncated
    return result


def summary_record(record):
    result = summary_fields(record, ('project_name', 'occurred_at', 'title', 'summary',
                                     'status', 'category', 'importance', 'follow_up'))
    # Keep query identifiers exact; only prose and source previews are shortened.
    result.update({k: record[k] for k in ('project_id', 'item_id', 'original_item_id') if k in record})
    refs = record.get('source_refs', [])
    result['source_refs'] = [{'thread_id': ref['thread_id'], 'event_ids': ref['event_ids'][:5],
                             'event_count': len(ref['event_ids'])} for ref in refs[:5]]
    result['source_ref_count'] = len(refs)
    result['details_available'] = any(record.get(k) for k in ('details', 'knowledge', 'outcome'))
    if len(refs) > 5 or any(len(ref['event_ids']) > 5 for ref in refs):
        result.setdefault('truncated_fields', []).append('source_refs')
    return result


def query(ledger, catalog, *, since=None, until=None, projects=(), statuses=(), item_ids=(),
          thread_ids=(), keyword='', latest=False, limit=20, offset=0, order='newest',
          view='summary', coverage='summary'):
    if view not in {'summary', 'full'} or coverage not in {'summary', 'full'}:
        raise ValueError('view and coverage must be summary or full')
    start = instant(since) if since else datetime.min.replace(tzinfo=timezone.utc)
    end = instant(until) if until else datetime.max.replace(tzinfo=timezone.utc)
    if start >= end:
        raise ValueError('Query since must precede until')
    all_entries = entries(ledger)
    projects = project_ids(all_entries, projects)
    terms = normalise(keyword or '').split()
    current = {}
    for entry in sorted(all_entries, key=lambda e: e['order']):
        current[item_key(entry)] = entry
    selected_items = {item_key(e) for e in all_entries if e['record']['item_id'] in item_ids
                      or e['record'].get('original_item_id') in item_ids} if item_ids else None
    matched = []
    for entry in all_entries:
        record = entry['record']
        if latest and entry is not current[item_key(entry)]:
            continue
        if projects is not None and record['project_id'] not in projects:
            continue
        if statuses and record['status'] not in statuses:
            continue
        if selected_items is not None and item_key(entry) not in selected_items:
            continue
        if thread_ids and not set(thread_ids).intersection(ref['thread_id'] for ref in record['source_refs']):
            continue
        if not start <= instant(record['occurred_at']) < end:
            continue
        if terms and not all(term in searchable(record, catalog) for term in terms):
            continue
        matched.append(entry)
    matched.sort(key=lambda e: e['order'], reverse=True)
    if order == 'oldest':
        matched.reverse()
    selected, pagination = page(matched, limit, offset)
    records = [summary_record(e['record']) if view == 'summary' else copy.deepcopy(e['record']) for e in selected]
    wanted = {}
    contexts = []
    for entry, record in zip(selected, records):
        for ref in record['source_refs']:
            wanted.setdefault(ref['thread_id'], set()).update(ref['event_ids'])
        newest = current[item_key(entry)]
        contexts.append({'run_id': entry['run_id'], 'record_index': entry['record_index'],
                         'is_current': entry is newest,
                         'current': summary_fields(newest['record'], ('occurred_at', 'status', 'title', 'summary', 'follow_up'))
                         if view == 'summary' else {k: newest['record'].get(k) for k in ('occurred_at', 'status', 'title', 'summary', 'follow_up')}})
    sources = {}
    for tid, ids in wanted.items():
        source = catalog.get(tid, {})
        if view == 'summary':
            source = summary_fields(source, ('title', 'source_path', 'first_event_at', 'last_event_at'))
            source['events'] = catalog.get(tid, {}).get('events', {})
        else:
            source = copy.deepcopy(source)
        source['events'] = {eid: stamp for eid, stamp in source.get('events', {}).items() if eid in ids}
        sources[tid] = source
    evidence_ids = {eid for record in records for claim in record.get('details', {}).get('evidence', []) for eid in claim['evidence_ids']}
    evidence_catalog = {eid: copy.deepcopy(value) for run in ledger for eid, value in run.get('evidence_catalog', {}).items() if eid in evidence_ids}
    scans = [r for r in ledger if r['mode'] != 'weekly' and instant(r['window']['since']) < end
             and instant(r['window']['until']) > start]
    coverage_summary = {'scope': 'local scans overlapping the time window, across all projects',
                        'scan_count': len(scans),
                        'complete_scan_count': sum(r.get('coverage', {}).get('complete_local') is True for r in scans),
                        'incomplete_scan_count': sum(r.get('coverage', {}).get('complete_local') is not True for r in scans),
                        'error_count': sum(len(r.get('coverage', {}).get('errors', [])) for r in scans),
                        'scans_with_pending_reviews': sum(bool(r.get('coverage', {}).get('pending_review_events')) for r in scans)}
    return {'records': records, 'matches': contexts, 'evidence_catalog': evidence_catalog,
            'source_catalog': sources, 'view': view, **pagination, 'coverage_summary': coverage_summary,
            'coverage_detail_included': coverage == 'full',
            'coverage': [{'run_id': r['run_id'], 'coverage': r.get('coverage', {})} for r in scans] if coverage == 'full' else []}


def title_tokens(title):
    tokens = set()
    for word in re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', normalise(title)):
        if re.fullmatch(r'[\u3400-\u9fff]+', word) and len(word) > 1:
            tokens.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.add(word)
    return tokens


def candidates(ledger, catalog, *, project, title='', thread_ids=(), limit=10, offset=0):
    if not project or not (title.strip() or thread_ids):
        raise ValueError('Candidates require --project and --title or --thread-id')
    all_entries = entries(ledger)
    projects = project_ids(all_entries, [project])
    groups = {}
    for entry in sorted(all_entries, key=lambda e: e['order']):
        if entry['record']['project_id'] in projects:
            groups.setdefault(item_key(entry), []).append(entry)
    query_tokens = title_tokens(title)
    found = []
    for (pid, iid), history in groups.items():
        records = [e['record'] for e in history]
        sources = {ref['thread_id'] for record in records for ref in record['source_refs']}
        common_threads = sorted(sources.intersection(thread_ids))
        exact = bool(title.strip()) and any(normalise(r['title']).strip() == normalise(title).strip() for r in records)
        similarity = max((len(query_tokens & title_tokens(r['title'])) / max(1, len(query_tokens | title_tokens(r['title'])))
                          for r in records), default=0)
        if not (exact or common_threads or similarity >= 0.2):
            continue
        score = (100 if exact else 0) + (40 if common_threads else 0) + round(30 * similarity, 4)
        reasons = []
        if exact:
            reasons.append('标题完全匹配')
        if common_threads:
            reasons.append('共享来源会话：' + ', '.join(common_threads))
        if similarity:
            reasons.append('标题词汇相似度：' + str(round(similarity, 3)))
        latest = history[-1]
        found.append({'project_id': pid, 'item_id': iid, 'score': score, 'match_reasons': reasons,
                      'latest': copy.deepcopy(latest['record']), 'latest_run_id': latest['run_id'],
                      'revision_count': len(history), 'first_occurred_at': records[0]['occurred_at'],
                      'aliases': sorted({r['original_item_id'] for r in records if r.get('original_item_id')}),
                      'sources': [{'thread_id': tid, 'title': catalog.get(tid, {}).get('title', tid)} for tid in sorted(sources)],
                      '_order': latest['order']})
    found.sort(key=lambda item: (item['score'], item['_order'], item['item_id']), reverse=True)
    selected, pagination = page(found, limit, offset)
    for item in selected:
        item.pop('_order')
    return {'candidates': selected, **pagination,
            'notice': '候选依据为同项目的标题与来源匹配，不代表已经确认同一事项；核对证据后再复用 item_id。'}
