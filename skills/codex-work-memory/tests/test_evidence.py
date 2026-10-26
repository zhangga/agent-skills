"""Evidence provenance, privacy, persistence and compatibility regressions."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('evidence_eval', SCRIPTS / 'quality_eval.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
memory = evaluation.memory
evidence = memory.evidence


class EvidenceTests(unittest.TestCase):
    def test_device_authorization_redaction_is_idempotent(self):
        source = 'https://example.test/verify?flow_id=synthetic-flow&user_code=synthetic-code'
        cleaned = evidence.redact(source)
        self.assertNotIn('synthetic-', cleaned)
        self.assertEqual(evidence.redact(cleaned), cleaned)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        evaluation.prepare_case(evaluation.DEFAULT_SUITE / 'failure-recovery', self.root / 'case')
        self.case = self.root / 'case'
        self.batch = memory.read(self.case / 'batch.json')
        self.bundle = memory.read(self.case / 'evidence.json')
        self.labels = memory.read(self.case / 'labels.json')
        self.final = next(e for e in self.batch['threads'][0]['events'] if e['event_id'] == self.labels['fix-final']['event_id'])
        self.passed = next(e for e in self.bundle['entries'] if e['kind'] == 'tool_result' and e['call_id'] == 'test-passed')
        self.record = {'item_id': 'fix', 'project_id': 'demo', 'project_name': '示例项目', 'occurred_at': self.final['time'],
                       'title': '修复请求超时', 'summary': '离线测试通过，尚未部署', 'category': '排障', 'status': '已验证',
                       'importance': 'high', 'source_refs': [{'thread_id': 'fix', 'event_ids': [self.final['event_id']]}],
                       'details': {'evidence': [{'claim': '3 个离线回归测试通过', 'level': 'observed', 'result': 'passed',
                                                'scope': '离线 unittest', 'evidence_ids': [self.passed['evidence_id']]}]}}

    def test_extract_keeps_call_results_and_source_lines_but_not_processing_progress(self):
        ids_before = self.batch['threads'][0]['checkpoint']['event_ids'][:]
        selected = evidence.bind([self.record], self.batch, self.bundle)
        self.assertEqual(set(selected), {self.passed['evidence_id']})
        self.assertIn('Ran 3 tests', selected[self.passed['evidence_id']]['excerpt'])
        self.assertGreater(self.passed['source_line'], 0)
        self.assertEqual(self.batch['threads'][0]['checkpoint']['event_ids'], ids_before)

    def test_source_change_and_tampered_bundle_are_rejected(self):
        changed = copy.deepcopy(self.bundle)
        changed['entries'][0]['excerpt'] = 'invented success'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            evidence.bind([self.record], self.batch, changed)
        changed['content_sha256'] = evidence.digest({k: v for k, v in changed.items() if k != 'content_sha256'})
        with self.assertRaisesRegex(ValueError, 'differs from source'):
            evidence.bind([self.record], self.batch, changed)
        path = Path(self.batch['threads'][0]['source_path'])
        path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            evidence.bind([self.record], self.batch, self.bundle)

    def test_claimed_success_cannot_be_promoted_without_tool_result(self):
        statement = next(e for e in self.bundle['entries'] if e['kind'] == 'assistant_message')
        self.record['details']['evidence'][0]['evidence_ids'] = [statement['evidence_id']]
        with self.assertRaisesRegex(ValueError, 'require a cited tool result'):
            evidence.bind([self.record], self.batch, self.bundle)
        self.record['details']['evidence'][0]['level'] = 'reported'
        with self.assertRaisesRegex(ValueError, 'verified/published'):
            evidence.bind([self.record], self.batch, self.bundle)
        self.record['status'] = '进行中'
        self.assertTrue(evidence.bind([self.record], self.batch, self.bundle))

    def test_deployment_requires_environment_and_direct_result(self):
        claim = self.record['details']['evidence'][0]
        claim['level'] = 'deployment'
        with self.assertRaisesRegex(ValueError, 'environment'):
            evidence.bind([self.record], self.batch, self.bundle)
        claim['environment'] = 'staging'
        # The script checks provenance, not whether arbitrary stdout proves
        # deployment semantics; scenario grading/human review covers that.
        self.assertTrue(evidence.bind([self.record], self.batch, self.bundle))

    def test_tool_call_without_result_stays_unknown(self):
        call = next(e for e in self.bundle['entries'] if e['kind'] == 'tool_call')
        self.record['status'] = '进行中'
        claim = self.record['details']['evidence'][0]
        claim.update(level='reported', evidence_ids=[call['evidence_id']])
        with self.assertRaisesRegex(ValueError, 'without a result'):
            evidence.bind([self.record], self.batch, self.bundle)
        claim['result'] = 'unknown'
        self.assertTrue(evidence.bind([self.record], self.batch, self.bundle))

    def test_verified_result_cannot_be_backdated_before_tool_output(self):
        self.record['occurred_at'] = self.batch['window']['since']
        with self.assertRaisesRegex(ValueError, 'cannot postdate'):
            evidence.bind([self.record], self.batch, self.bundle)

    def test_old_logs_without_turn_ids_and_custom_tool_outputs_are_supported(self):
        path = Path(self.batch['threads'][0]['source_path'])
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        rows = [row for row in rows if row['type'] != 'turn_context']
        for row in rows:
            payload = row['payload']
            if payload.get('type') == 'function_call':
                payload['type'] = 'custom_tool_call'
                payload['input'] = payload.pop('arguments')
            elif payload.get('type') == 'function_call_output':
                payload['type'] = 'custom_tool_call_output'
                payload['output'] = [{'type': 'text', 'text': payload['output']}]
        path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        collector = evidence.collector
        batch = collector.collect({'source_roots': [str(path.parent)]}, {},
                                  collector.instant(self.batch['window']['since']), collector.instant(self.batch['window']['until']))
        anchor = batch['threads'][0]['events'][-1]['event_id']
        bundle = evidence.extract(batch, {'fix': [anchor]})
        self.assertTrue(any(e['kind'] == 'tool_result' and 'Ran 3 tests' in e['excerpt'] for e in bundle['entries']))
        self.assertTrue(all(e['turn_id'] is None for e in bundle['entries']))

    def test_evidence_pagination_marks_long_text_and_empty_excerpt_cannot_be_used(self):
        bundle = evidence.extract(self.batch, self.bundle['selection'], max_chars=100, text_offset=10000, limit=200)
        self.assertTrue(all(e['truncated'] for e in bundle['entries']))
        with self.assertRaisesRegex(ValueError, 'Empty evidence'):
            evidence.bind([self.record], self.batch, bundle)

    def test_wrong_thread_unknown_ids_and_missing_important_evidence_rejected(self):
        for refs in [[{'thread_id': 'other', 'event_ids': [self.final['event_id']]}],
                     [{'thread_id': 'fix', 'event_ids': ['unknown']}]]:
            bad = dict(self.record, source_refs=refs)
            with self.assertRaisesRegex(ValueError, 'belong'):
                evidence.bind([bad], self.batch, self.bundle)
        with self.assertRaisesRegex(ValueError, 'Important records'):
            evidence.bind([dict(self.record, details={})], self.batch)

    def test_evidence_cannot_cross_turns_even_when_both_are_selected(self):
        work = self.root / 'cross'
        evaluation.prepare_case(evaluation.DEFAULT_SUITE / 'cross-day', work)
        batch, bundle, labels = [memory.read(work / name) for name in ('batch.json', 'evidence.json', 'labels.json')]
        late = next(e for e in bundle['entries'] if e.get('call_id') == 'cross-passed' and e['kind'] == 'tool_result')
        record = copy.deepcopy(self.record)
        record['source_refs'] = [{'thread_id': 'cross', 'event_ids': [labels['cross-blocked']['event_id']]}]
        record['details']['evidence'][0]['evidence_ids'] = [late['evidence_id']]
        with self.assertRaisesRegex(ValueError, 'selected turn'):
            evidence.bind([record], batch, bundle)

    def test_redaction_large_nonsecret_identifiers_complete_without_backtracking(self):
        # The secret-key matcher must not restart at each byte of a large tool payload.
        script = "import evidence; text = 'A' * 200000 + ' ' + 'long_identifier_' * 15000; assert evidence.redact(text) == text; assert '[REDACTED]' in evidence.redact(text + ' SERVICE_AUTH=123456789')"
        subprocess.run([sys.executable, '-c', script], cwd=SCRIPTS,
                       check=True, timeout=5, capture_output=True)

    def test_redaction_excludes_hidden_reasoning_and_marks_truncation(self):
        work = self.root / 'interrupt'
        evaluation.prepare_case(evaluation.DEFAULT_SUITE / 'interrupted-injection', work)
        bundle = memory.read(work / 'evidence.json')
        content = json.dumps(bundle, ensure_ascii=False)
        self.assertNotIn('sk-syntheticSecret123456789', content)
        self.assertNotIn('PRIVATE_REASONING_SENTINEL', content)
        self.assertIn('[REDACTED]', content)
        self.assertTrue(any(e['redacted'] for e in bundle['entries']))
        self.assertNotIn('secret-value', evidence.redact('Authorization: Bearer secret-value'))
        self.assertNotIn('hidden-pass', evidence.redact('password="hidden-pass"'))
        for secret in ('plat_' + 'synthetic123456789', 'plat\\_' + 'synthetic123456789'):
            self.assertNotIn(secret, evidence.redact('Historical credential ' + secret))
        for assignment in ('token：synthetic-value', 'MONGO_ROOT_PASSWORD=synthetic-value',
                           'SERVICE_API_KEY: synthetic-value'):
            self.assertNotIn('synthetic-value', evidence.redact(assignment))
        for secret in ('AIza' + 'synthetic12345678901234567890', 'inf_' + 'synthetic1234567890',
                       'cred_' + 'synthetic1234567890'):
            self.assertNotIn(secret, evidence.redact('Historical credential ' + secret))
        for assignment in ('tech_claw_auth:123456789', 'SERVICE_AUTH=123456789',
                           'service\\_access\\_token：synthetic-value'):
            cleaned = evidence.redact(assignment)
            self.assertNotIn('123456789', cleaned)
            self.assertNotIn('synthetic-value', cleaned)
        nested = {'token': 'synthetic-secret-value', 'MONGO_ROOT_PASSWORD': 'synthetic-password'}
        for _ in range(4):
            nested = json.dumps(nested)
            self.assertNotIn('synthetic-secret-value', evidence.redact(nested))
            self.assertNotIn('synthetic-password', evidence.redact(nested))
        self.assertEqual(evidence.redact('claw_id:123456789'), 'claw_id:123456789')
        cleaned = evidence.redact('https://example.test/device?flow_id=synthetic-flow&user_code=123456789')
        self.assertNotIn('synthetic-flow', cleaned)
        self.assertNotIn('123456789', cleaned)
        bounded = evidence.extract(self.batch, self.bundle['selection'], limit=1, max_chars=100)
        self.assertEqual(len(bounded['entries']), 1)
        self.assertTrue(bounded['has_more'])
        offset = evidence.extract(self.batch, self.bundle['selection'], limit=1, offset=1, max_chars=100)
        self.assertNotEqual(offset['entries'][0]['evidence_id'], bounded['entries'][0]['evidence_id'])

    def test_commit_enrichment_export_and_retry_preserve_evidence(self):
        archive = self.root / 'archive'
        config, records, review, brief = [self.root / name for name in ('config.json', 'records.json', 'review.json', 'brief.md')]
        memory.write_json(config, {'archive_dir': str(archive)})
        memory.write_json(records, [self.record])
        decisions = [{'thread_id': 'fix', 'event_id': e['event_id'], 'disposition': 'recorded', 'item_ids': ['fix']}
                     if e['event_id'] == self.final['event_id'] else {'thread_id': 'fix', 'event_id': e['event_id'], 'disposition': 'ignored', 'reason': 'Included in final outcome'}
                     for e in self.batch['threads'][0]['events']]
        memory.write_json(review, decisions)
        brief.write_text('Brief', encoding='utf-8')
        command = [sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'memory.py'), 'commit', '--config', str(config),
                   '--batch', str(self.case / 'batch.json'), '--records', str(records), '--review', str(review),
                   '--evidence', str(self.case / 'evidence.json'), '--brief', str(brief), '--mode', 'daily', '--run-id', 'with-evidence']
        first = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(first.returncode, 0, first.stderr)
        saved = (archive / 'runs/with-evidence.json').read_bytes()
        ledger = memory.resolved_runs(archive)
        result = memory.retrieval.query(ledger, memory.merged_catalog(ledger), limit=1, view='full')
        self.assertEqual(set(result['evidence_catalog']), {self.passed['evidence_id']})
        self.assertIn('Ran 3 tests', (archive / 'index.html').read_text(encoding='utf-8'))
        weekly = {'run_id': 'weekly', 'mode': 'weekly', 'created_at': '2026-10-19T00:00:00Z',
                  'window': self.batch['window'], 'records': [], 'brief_text': 'weekly', 'report_text': 'summary', 'payload_hash': 'synthetic'}
        frozen = memory.agent_export.freeze(archive, weekly, ledger)
        self.assertIn('Ran 3 tests', memory.agent_export.markdown(frozen))
        before_state = (archive / 'state.json').read_bytes()
        with memory.lock(archive):
            memory.enrichment(archive, 'with-evidence', 'extra',
                              [dict(self.record, details=dict(self.record['details'], context='补充背景'))], self.batch, self.bundle)
        self.assertEqual((archive / 'state.json').read_bytes(), before_state)
        # Frozen citations make an exact retry independent of live source files.
        Path(self.batch['threads'][0]['source_path']).unlink()
        repeated = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(saved, (archive / 'runs/with-evidence.json').read_bytes())
        self.assertEqual(memory.agent_export.freeze(archive, weekly, []), frozen)


if __name__ == '__main__':
    unittest.main()
