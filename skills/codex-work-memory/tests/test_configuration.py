"""Synthetic profiles only: no local conversations, credentials, or network."""
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def module(name):
    spec = importlib.util.spec_from_file_location("test_cwm_" + name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


configuration = module("configuration")
setup = module("setup")
collector = module("collect")
delivery = module("deliver")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "个人 配置" / "config.json"

    def test_init_custom_paths_and_safe_defaults(self):
        result = setup.initialize(self.profile, "../工作 记录", "../助理 输入", "../codex",
                                  "2026-10-19T00:00:00+08:00", ["maintenance-test"])
        cfg = configuration.load_config(self.profile)
        self.assertEqual(cfg["archive_dir"], str((self.root / "工作 记录").resolve()))
        self.assertEqual(cfg["agent_export_dir"], str((self.root / "助理 输入").resolve()))
        self.assertEqual(cfg["codex_home"], str((self.root / "codex").resolve()))
        self.assertFalse(cfg["delivery"]["enabled"])
        self.assertFalse(cfg["delivery"]["codex_link_bridge"]["enabled"])
        self.assertNotIn("authorization", cfg)
        self.assertNotIn("bot_app_id", cfg["delivery"])
        self.assertEqual(cfg["excluded_thread_ids"], ["maintenance-test"])
        self.assertFalse(Path(result["archive_dir"]).exists())

    def test_existing_profile_and_archive_unchanged(self):
        setup.initialize(self.profile, codex_home=self.root / "codex")
        original = self.profile.read_bytes()
        archive = Path(configuration.load_config(self.profile)["archive_dir"])
        archive.mkdir()
        state = archive / "state.json"
        state.write_text('{"scan_success_until":"2026-10-20T00:00:00Z"}', encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "Nothing was changed"):
            setup.initialize(self.profile, "../new-archive")
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertEqual(json.loads(state.read_text())["scan_success_until"], "2026-10-20T00:00:00Z")

    def test_init_generates_a_usable_seven_day_baseline_without_a_fixed_example_date(self):
        earliest = datetime.now(timezone.utc) - timedelta(days=7, seconds=1)
        setup.initialize(self.profile, codex_home=self.root / 'codex')
        cfg = configuration.load_config(self.profile)
        since = collector.instant(cfg['initial_since'])
        self.assertLessEqual(earliest, since)
        self.assertLessEqual(since, datetime.now(timezone.utc) - timedelta(days=7))
        self.assertEqual(since.utcoffset(), timedelta(0))

    def test_old_profile_defaults_export_and_does_not_mutate_input(self):
        raw = {"archive_dir": "../archive", "codex_home": "../codex", "projects": [{"path": "../project", "projectId": "example"}]}
        cfg = configuration.normalize_config(raw, self.profile)
        self.assertEqual(cfg["agent_export_dir"], str((self.root / "archive" / "agent").resolve()))
        self.assertEqual(cfg["projects"][0]["path"], str((self.root / "project").resolve()))
        self.assertEqual(raw["archive_dir"], "../archive")

    def test_tilde_expansion(self):
        cfg = configuration.normalize_config({"archive_dir": "~/cwm-test-archive", "agent_export_dir": "~/cwm-test-agent"}, self.profile)
        self.assertEqual(cfg["archive_dir"], str((Path.home() / "cwm-test-archive").resolve()))

    def test_resolution_precedence_and_legacy(self):
        legacy = self.root / "skill" / "config.json"
        legacy.parent.mkdir()
        legacy.write_text("{}", encoding="utf-8")
        env = {"CODEX_HOME": str(self.root / "codex"), "CODEX_WORK_MEMORY_CONFIG": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(configuration, "__file__", str(legacy.parent / "scripts/configuration.py")):
            self.assertEqual(configuration.resolve_config_path(), legacy)
            external = configuration.default_config_path()
            external.parent.mkdir(parents=True)
            external.write_text("{}", encoding="utf-8")
            self.assertEqual(configuration.resolve_config_path(), external)
            explicit = self.root / "chosen.json"
            self.assertEqual(configuration.resolve_config_path(explicit), explicit)
            with mock.patch.dict(os.environ, {"CODEX_WORK_MEMORY_CONFIG": str(explicit)}):
                self.assertEqual(configuration.resolve_config_path(), explicit)

    def test_source_and_owned_directory_overlap_rejected(self):
        base = {"archive_dir": "../archive", "source_roots": ["../sources"]}
        for export in ("../archive", "../archive/runs", "../archive/weekly/export", "../sources/export", ".."):
            with self.subTest(export=export), self.assertRaises(ValueError):
                configuration.normalize_config(dict(base, agent_export_dir=export), self.profile)
        with self.assertRaisesRegex(ValueError, "source_roots"):
            configuration.normalize_config(dict(base, archive_dir="../sources/archive"), self.profile)

    def test_reject_unsupported_timezone(self):
        with self.assertRaisesRegex(ValueError, "Asia/Shanghai"):
            configuration.normalize_config({"archive_dir": "../archive", "timezone": "UTC"}, self.profile)

    def test_default_collector_state_is_configured_archive(self):
        codex = self.root / "codex"
        for name in ("sessions", "archived_sessions"):
            (codex / name).mkdir(parents=True)
        setup.initialize(self.profile, codex_home=codex, initial_since="2026-10-01T00:00:00Z")
        cfg = configuration.load_config(self.profile)
        archive = Path(cfg["archive_dir"])
        archive.mkdir()
        (archive / "state.json").write_text(json.dumps({"scan_success_until": "2026-10-24T00:00:00Z"}), encoding="utf-8")
        batch = self.root / "batch.json"
        result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / "collect.py"), "--config", str(self.profile),
                                 "--until", "2026-10-25T00:00:00Z", "--out", str(batch)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(batch.read_text(encoding="utf-8"))["window"]["since"], "2026-10-22T00:00:00Z")

    def test_pending_review_overrides_lookback_and_initial_since_but_not_explicit_window(self):
        codex = self.root / 'codex'
        for name in ('sessions', 'archived_sessions'):
            (codex / name).mkdir(parents=True)
        setup.initialize(self.profile, codex_home=codex, initial_since='2026-10-20T00:00:00Z')
        archive = Path(configuration.load_config(self.profile)['archive_dir'])
        archive.mkdir()
        state = {'scan_success_until': '2026-10-24T00:00:00Z',
                 'pending_review_since': '2026-10-10T02:00:00Z'}
        (archive / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        output = self.root / 'pending.json'
        command = [sys.executable, '-X', 'utf8', str(SCRIPTS / 'collect.py'), '--config', str(self.profile),
                   '--until', '2026-10-25T00:00:00Z', '--out', str(output)]
        for extra, expected in [([], '2026-10-10T02:00:00Z'),
                                (['--since', '2026-10-23T00:00:00Z'], '2026-10-23T00:00:00Z')]:
            result = subprocess.run(command + extra, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['window']['since'], expected)

    def test_posix_case_and_windows_longest_path(self):
        projects = [{"projectId": "upper", "label": "Upper", "path": "/work/Foo"},
                    {"projectId": "lower", "label": "Lower", "path": "/work/foo"},
                    {"projectId": "win", "label": "Windows", "path": "C:/Work/Example"},
                    {"projectId": "nested", "label": "Nested", "path": "c:/work/example/sub"}]
        self.assertEqual(collector.classify({}, {}, "/work/Foo/src", projects)[0], "upper")
        self.assertEqual(collector.classify({}, {}, "/work/foo/src", projects)[0], "lower")
        self.assertEqual(collector.classify({}, {}, "C:\\WORK\\EXAMPLE\\sub\\src", projects)[0], "nested")
        self.assertIsNone(collector.classify({}, {}, "/work/FOO/src", projects)[0])

    def test_non_retryable_delivery_never_calls_cli(self):
        self.profile.parent.mkdir()
        self.profile.write_text(json.dumps({"delivery": {"enabled": True, "bot_app_id": "cli_example", "recipient_open_id": "ou_example"}}), encoding="utf-8")
        brief = self.root / "brief.md"
        brief.write_text("synthetic brief", encoding="utf-8")
        archive = self.root / "archive"
        ledger_dir = archive / "delivery"
        ledger_dir.mkdir(parents=True)
        ledger = {"bot_app_id": "cli_example", "recipient_open_id": "ou_example", "content_type": "markdown",
                  "content_hash": hashlib.sha256(b"markdown\0synthetic brief").hexdigest(), "status": "failed", "retryable": False}
        (ledger_dir / "sample-run.json").write_text(json.dumps(ledger), encoding="utf-8")
        with self.assertRaisesRegex(delivery.DeliveryError, "non-retryable"):
            delivery.deliver(self.profile, archive, "sample-run", brief, runner=lambda *args: self.fail("must not call CLI"))

    def test_set_exclusions_preserves_profile_archive_and_exact_backup(self):
        setup.initialize(self.profile, codex_home=self.root / 'codex', excluded_thread_ids=['original'])
        raw = json.loads(self.profile.read_text(encoding='utf-8'))
        raw.update(archive_dir='../archive', custom={'keep': 'unchanged'})
        raw['delivery']['recipient_open_id'] = 'ou_synthetic'
        self.profile.write_text(json.dumps(raw), encoding='utf-8')
        archive = Path(configuration.load_config(self.profile)['archive_dir'])
        archive.mkdir()
        state = archive / 'state.json'
        state.write_text('{"scan_success_until":"2026-10-20T00:00:00Z"}', encoding='utf-8')
        before, before_state = self.profile.read_bytes(), state.read_bytes()
        result = setup.set_exclusions(self.profile, ['maintenance', 'maintenance'])
        updated = json.loads(self.profile.read_text(encoding='utf-8'))
        self.assertEqual(updated, dict(raw, excluded_thread_ids=['original', 'maintenance']))
        self.assertEqual(Path(result['backup']).read_bytes(), before)
        self.assertEqual(state.read_bytes(), before_state)
        same_bytes = self.profile.read_bytes()
        self.assertFalse(setup.set_exclusions(self.profile, ['maintenance'])['changed'])
        self.assertEqual(self.profile.read_bytes(), same_bytes)
        self.assertEqual(len(list(self.profile.parent.glob('config.json.bak-*'))), 1)
        result = setup.set_exclusions(self.profile, include=['original'])
        self.assertEqual(result['excluded_thread_ids'], ['maintenance'])

    def test_current_thread_cli_and_show_require_no_guessed_session_id(self):
        setup.initialize(self.profile, codex_home=self.root / 'codex')
        env = dict(os.environ, CODEX_THREAD_ID='current-test-thread')
        base = [sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'setup.py')]
        result = subprocess.run(base + ['set', '--config', str(self.profile), '--exclude-current-thread'],
                                env=env, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = subprocess.run(base + ['show', '--config', str(self.profile)],
                               env=env, capture_output=True, text=True, encoding='utf-8')
        result = json.loads(shown.stdout)
        self.assertEqual(result['current_thread_id'], 'current-test-thread')
        self.assertEqual(result['excluded_thread_ids'], ['current-test-thread'])
        before = self.profile.read_bytes()
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': '', 'CODEX_SESSION_ID': 'not-a-thread-id'}):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                setup.set_exclusions(self.profile, exclude_current=True)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_set_rejects_invalid_conflicting_and_unrelated_edits(self):
        setup.initialize(self.profile, codex_home=self.root / 'codex')
        before = self.profile.read_bytes()
        for extra in [[], ['--exclude-thread', 'bad id'], ['--exclude-thread', ''],
                      ['--exclude-thread', 'a', '--include-thread', 'a'],
                      ['--archive-dir', str(self.root / 'elsewhere'), '--exclude-thread', 'a']]:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPTS / 'setup.py'),
                                     'set', '--config', str(self.profile), *extra],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual(list(self.profile.parent.glob('config.json.bak-*')), [])

    def test_set_lock_and_replace_failure_do_not_damage_existing_profile(self):
        setup.initialize(self.profile, codex_home=self.root / 'codex')
        before = self.profile.read_bytes()
        with setup.profile_lock(self.profile), self.assertRaisesRegex(RuntimeError, 'lock'):
            setup.set_exclusions(self.profile, ['maintenance'])
        with mock.patch.object(setup.os, 'replace', side_effect=OSError('synthetic replace failure')):
            with self.assertRaises(OSError):
                setup.set_exclusions(self.profile, ['maintenance'])
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual(list(self.profile.parent.glob('config.json.tmp-*')), [])
        self.assertEqual(next(self.profile.parent.glob('config.json.bak-*')).read_bytes(), before)

    def test_exclusion_update_affects_collection_and_can_be_reversed(self):
        codex = self.root / 'codex'
        sessions = codex / 'sessions'
        sessions.mkdir(parents=True)
        rows = [{'type': 'session_meta', 'payload': {'id': 'maintenance'}},
                {'type': 'response_item', 'timestamp': '2026-10-20T01:00:00Z', 'payload': {
                    'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Synthetic maintenance'}]}}]
        (sessions / 'maintenance.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        setup.initialize(self.profile, codex_home=codex)
        start, end = collector.instant('2026-10-20T00:00:00Z'), collector.instant('2026-10-21T00:00:00Z')
        setup.set_exclusions(self.profile, ['maintenance'])
        batch = collector.collect(configuration.load_config(self.profile), {}, start, end)
        self.assertEqual(batch['threads'], [])
        self.assertTrue(batch['coverage']['complete_local'])
        setup.set_exclusions(self.profile, include=['maintenance'])
        batch = collector.collect(configuration.load_config(self.profile), {}, start, end)
        self.assertEqual([t['thread_id'] for t in batch['threads']], ['maintenance'])


if __name__ == "__main__":
    unittest.main()
