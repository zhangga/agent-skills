"""The grader accepts curated responses and rejects distinct summary failures.

These are rubric regressions, not independent model inference measurements.
"""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('scenario_evaluator',
    Path(__file__).resolve().parents[1] / 'scripts/quality_eval.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
memory = evaluation.memory


def reference_response(case_dir, work):
    """Resolve explicit reference citations, never infer a summary from the oracle."""
    sample = memory.read(case_dir / 'reference.json')
    batch, bundle, labels = [memory.read(work / name) for name in ('batch.json', 'evidence.json', 'labels.json')]
    events = {e['event_id']: e for t in batch['threads'] for e in t['events']}
    records = []
    for draft in sample['records']:
        refs = {}
        for label in draft['anchors']:
            ref = labels[label]
            refs.setdefault(ref['thread_id'], []).append(ref['event_id'])
        claims = []
        for original in draft['claims']:
            claim = copy.deepcopy(original)
            citation = claim.pop('citation')
            candidates = [e for e in bundle['entries'] if e['kind'] == citation['kind']
                          and (not citation.get('call_id') or e['call_id'] == citation['call_id'])
                          and (not citation.get('contains') or citation['contains'] in e['excerpt'])]
            if len(candidates) != 1:
                raise ValueError('Reference citation must identify one source entry')
            claim['evidence_ids'] = [candidates[0]['evidence_id']]
            claims.append(claim)
        records.append({'item_id': draft['item_id'], 'project_id': 'demo', 'project_name': '示例项目',
                        'occurred_at': events[labels[draft['at']]['event_id']]['time'], 'importance': 'high',
                        'title': draft['title'], 'summary': draft['summary'], 'status': draft['status'], 'category': '排障',
                        'details': {'evidence': claims},
                        'source_refs': [{'thread_id': tid, 'event_ids': ids} for tid, ids in refs.items()]})
    reviews = []
    for thread in batch['threads']:
        for event in thread['events']:
            ids = sorted({r['item_id'] for r in records for ref in r['source_refs']
                          if ref['thread_id'] == thread['thread_id'] and event['event_id'] in ref['event_ids']})
            entry = {'thread_id': thread['thread_id'], 'event_id': event['event_id']}
            if ids:
                entry.update(disposition='recorded', item_ids=ids)
            else:
                entry.update(disposition='ignored', reason='重复状态询问，无新增工作事实')
            reviews.append(entry)
    memory.write_json(work / 'records.json', records)
    memory.write_json(work / 'review.json', reviews)
    return records, reviews


class QualityScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def prepare(self, name):
        case = evaluation.DEFAULT_SUITE / name
        work = self.root / name
        evaluation.prepare_case(case, work)
        records, review = reference_response(case, work)
        return case, work, records, review

    def test_all_six_curated_scenarios_pass_without_copying_oracles_into_agent_inputs(self):
        cases = sorted(evaluation.DEFAULT_SUITE.iterdir())
        count = 0
        for case in cases:
            if not (case / 'input.json').exists():
                continue
            with self.subTest(case=case.name):
                case, work, _, _ = self.prepare(case.name)
                self.assertFalse((work / 'oracle.json').exists())
                self.assertFalse((work / 'reference.json').exists())
                result = evaluation.grade_case(case, work)
                self.assertTrue(result['passed'], result)
                count += 1
        self.assertEqual(count, 6)

    def test_missing_agent_response_is_a_failure(self):
        case = evaluation.DEFAULT_SUITE / 'unverified-claim'
        work = self.root / 'missing'
        evaluation.prepare_case(case, work)
        self.assertFalse(evaluation.grade_case(case, work)['passed'])

    def test_failed_output_cannot_be_reported_as_passed(self):
        case, work, records, _ = self.prepare('failure-recovery')
        records[0]['details']['evidence'][0]['result'] = 'passed'
        memory.write_json(work / 'records.json', records)
        result = evaluation.grade_case(case, work)
        self.assertFalse(result['passed'])
        self.assertTrue(any(c['name'] == 'tool_result_not_misrepresented' and not c['passed'] for c in result['checks']))

    def test_rollback_cannot_remain_published(self):
        case, work, records, _ = self.prepare('rollback')
        records[0]['status'] = '已发布'
        memory.write_json(work / 'records.json', records)
        self.assertFalse(evaluation.grade_case(case, work)['passed'])

    def test_same_title_different_work_must_not_merge(self):
        case, work, records, review = self.prepare('same-title')
        old = records[1]['item_id']
        records[1]['item_id'] = records[0]['item_id']
        for entry in review:
            if old in entry.get('item_ids', []):
                entry['item_ids'] = [records[0]['item_id']]
        memory.write_json(work / 'records.json', records)
        memory.write_json(work / 'review.json', review)
        result = evaluation.grade_case(case, work)
        self.assertFalse(result['passed'])
        self.assertTrue(any(c['name'] == 'distinct_work_not_merged' and not c['passed'] for c in result['checks']))

    def test_cross_day_work_must_keep_identity_and_failed_history(self):
        case, work, records, review = self.prepare('cross-day')
        records[1]['item_id'] = 'accidental-new-id'
        late_events = {eid for ref in records[1]['source_refs'] for eid in ref['event_ids']}
        for entry in review:
            if entry['event_id'] in late_events:
                entry['item_ids'] = ['accidental-new-id']
        memory.write_json(work / 'records.json', records)
        memory.write_json(work / 'review.json', review)
        self.assertFalse(evaluation.grade_case(case, work)['passed'])

    def test_unsubstantiated_claim_cannot_be_upgraded_to_verified(self):
        case, work, records, _ = self.prepare('unverified-claim')
        records[0]['status'] = '已验证'
        memory.write_json(work / 'records.json', records)
        self.assertFalse(evaluation.grade_case(case, work)['passed'])

    def test_interruption_secret_leak_and_injection_are_not_work_results(self):
        case, work, records, _ = self.prepare('interrupted-injection')
        for injected in ('PWNED', 'sk-syntheticSecret123456789'):
            records[0]['outcome'] = injected
            memory.write_json(work / 'records.json', records)
            self.assertFalse(evaluation.grade_case(case, work)['passed'])

    def test_missing_review_and_omitted_key_work_fail(self):
        case, work, records, review = self.prepare('failure-recovery')
        memory.write_json(work / 'review.json', review[:-1])
        self.assertFalse(evaluation.grade_case(case, work)['passed'])
        memory.write_json(work / 'records.json', [])
        for entry in review:
            entry.pop('item_ids', None)
            entry.update(disposition='ignored', reason='Incorrectly skipped all work')
        memory.write_json(work / 'review.json', review)
        # No evidence argument is needed for a truly empty commit; the evaluator
        # also rejects claiming an entire scenario contains no work.
        self.assertFalse(evaluation.grade_case(case, work)['passed'])


if __name__ == '__main__':
    unittest.main()
