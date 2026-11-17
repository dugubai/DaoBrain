"""Pure persisted release tests. No FastAPI, network, host commands or deployment.

Run from repository with PYTHONDONTWRITEBYTECODE=1 and TMPDIR in the workspace:
python3 -m unittest discover -s tests -p test_control_releases_r6.py -v
All disposable private test fixtures live outside the source tree, in workspace.
"""
import ast
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from control_plane import releases, registry, security, store
from control_plane.sandbox_build_entry import build

# /app is read-only in cloud regression containers. Use the explicitly provided
# writable temp root rather than inferring a host workspace from source depth.
WORKSPACE = Path(tempfile.gettempdir())


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_temp = tempfile.TemporaryDirectory(prefix='release-template-', dir=str(WORKSPACE))
        cls.template = Path(cls.template_temp.name)
        (cls.template / 'control').mkdir(mode=0o700)
        cls.base_db = cls.template / 'control/control.sqlite'
        with patch.dict(os.environ, {'CONTROL_DB_PATH': str(cls.base_db)}, clear=True):
            store.initialize()
            cls.users = {}
            for name, grants in (
                    ('author', ('view', 'develop', 'configure', 'review', 'operate')),
                    ('reviewer', ('view', 'review', 'configure')),
                    ('operator', ('view', 'operate')),
                    ('viewer', ('view',)),
                    ('otherdev', ('view', 'develop'))):
                cls.users[name] = security.provision(name, 'Synthetic-test-password-123', grants)
            policy = store.create_revision(cls.users['author']['id'], 'card_workflow',
                                           {'enabled': True, 'request_timeout_s': 30})
            policy = store.submit_revision(cls.users['author']['id'], policy['id'], policy['version'])
            cls.policy = store.review_revision(cls.users['reviewer']['id'], policy['id'], policy['version'], 'approve')

    @classmethod
    def tearDownClass(cls):
        cls.template_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-test-', dir=str(WORKSPACE))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('control', 'release', 'source', 'workspace', 'business', 'staging', 'trust'):
            (self.root / name).mkdir(mode=0o700)
        self.control_db = self.root / 'control/control.sqlite'
        self.release_db = self.root / 'release/releases.sqlite'
        shutil.copyfile(str(self.base_db), str(self.control_db))
        self.control_db.chmod(0o600)
        self.key = b'R6-synthetic-test-key-32-bytes!!'
        self.assertEqual(len(self.key), 32)
        self.key_path = self.root / 'trust/supervisor.key'
        self.key_path.write_bytes(self.key)
        self.key_path.chmod(0o600)
        environment = {
            'CONTROL_DB_PATH': str(self.control_db), 'CONTROL_RELEASE_DB_PATH': str(self.release_db),
            'CONTROL_SOURCE_ROOT': str(self.root / 'source'), 'CONTROL_WORKSPACE_ROOT': str(self.root / 'workspace'),
            'APP_DB_PATH': str(self.root / 'business/app.sqlite'),
            'CONTROL_RELEASE_STAGING_ROOT': str(self.root / 'staging'),
            'CONTROL_RELEASE_ATTESTATION_KEY_PATH': str(self.key_path),
        }
        self.env = patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tokens = {}
        with store.transaction() as db:
            now = int(time.time())
            for name, user in self.users.items():
                token = secrets.token_urlsafe(32)
                self.tokens[name] = token
                db.execute('INSERT INTO cp_sessions VALUES(?,?,?,?)',
                           (security.digest_token(token), user['id'], now + 3600, now))
        releases.initialize()

    def assert_error(self, status, function, *args, **kwargs):
        with self.assertRaises(registry.ControlError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.status, status)
        return raised.exception

    def manifest(self, label='r6.0.1', previous=None, schema=1, image=None):
        return {'component': 'card_workflow', 'runtime': 'backend', 'label': label,
                'source_sha256': '1' * 64, 'artifact_sha256': '2' * 64,
                'config_revision': self.policy['id'], 'config_sha256': self.policy['hash'],
                'test_evidence_sha256': '3' * 64, 'build_evidence_sha256': '4' * 64,
                'image_digest': image, 'schema_version': schema, 'previous_version': previous}

    def version(self, **kwargs):
        return releases.register_version(self.tokens['author'], self.manifest(**kwargs))

    def plan_input(self, version_id, offset=0):
        now = int(time.time())
        return {'version_id': version_id, 'planned_at': now - 10 + offset, 'expires_at': now + 600 + offset,
                'strategy': 'serial-app-slot', 'dependencies': [],
                'prechecks': {k: None for k in releases._PRECHECK_FIELDS}}

    def approved(self, version=None, offset=0, owner='author'):
        version = version or self.version()
        plan = releases.create_plan(self.tokens[owner], self.plan_input(version['id'], offset))
        plan = releases.submit_plan(self.tokens[owner], plan['id'], plan['version'])
        return releases.review_plan(self.tokens['reviewer'], plan['id'], plan['version'], 'approve')

    def real_attestation(self, label='r6.0.1', previous=None, schema=1, image=None, build_changes=None, test_changes=None):
        # Real sandbox builder bytes, staged source, and signed trusted reports.
        stage = self.root / ('candidate-' + label)
        stage.mkdir(mode=0o700)
        (stage / 'card_workflow').mkdir(mode=0o700)
        (stage / 'card_workflow/main.py').write_bytes(b"raise RuntimeError('never execute source')\n")
        output = self.root / ('artifact-' + label)
        output.mkdir(mode=0o700)
        built = build(stage, output)
        artifact = (output / built['artifact']).read_bytes()
        source_hash = registry.digest([{'path': 'card_workflow/main.py',
                                       'sha256': hashlib.sha256((stage / 'card_workflow/main.py').read_bytes()).hexdigest()}])
        manifest = self.manifest(label, previous, schema, image)
        manifest.update(source_sha256=source_hash, artifact_sha256=built['sha256'])
        stable = {k: v for k, v in manifest.items() if k not in ('build_evidence_sha256', 'test_evidence_sha256')}
        binding = {'manifest_sha256': registry.digest(stable), 'source_sha256': source_hash,
                   'artifact_sha256': built['sha256'], 'config_revision': manifest['config_revision'],
                   'config_sha256': manifest['config_sha256'], 'schema_version': schema,
                   'image_digest': image, 'component': 'card_workflow', 'runtime': 'backend'}
        build_value = dict(binding, kind='offline-source-artifact', syntax_checked=True, image_built=False)
        test_value = dict(binding, kind='trusted-tests', passed=True, tests_run=3,
                          test_entry_sha256='a' * 64, fixture_sha256='b' * 64)
        build_value.update(build_changes or {})
        test_value.update(test_changes or {})
        build_report = registry.canonical(build_value)
        test_report = registry.canonical(test_value)
        manifest['build_evidence_sha256'] = hashlib.sha256(build_report).hexdigest()
        manifest['test_evidence_sha256'] = hashlib.sha256(test_report).hexdigest()
        version = releases.register_version(self.tokens['author'], manifest)
        shutil.move(str(stage), str(self.root / 'staging' / version['id']))
        signed = {'version_id': version['id'], 'manifest_sha256': version['hash'],
                  'build_sha256': manifest['build_evidence_sha256'], 'test_sha256': manifest['test_evidence_sha256']}
        evidence = {'artifact_bytes': artifact, 'build_report_bytes': build_report,
                    'test_report_bytes': test_report,
                    'signature': hmac.new(self.key, registry.canonical(signed), hashlib.sha256).hexdigest()}
        return version, evidence

    def attest(self, **kwargs):
        version, evidence = self.real_attestation(**kwargs)
        return releases.attest_version_cli(self.tokens['reviewer'], version['id'], evidence)

    def test_new_file_private_and_control_schema_untouched(self):
        self.assertEqual(stat.S_IMODE(self.release_db.stat().st_mode), 0o600)
        with store.transaction() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {'cp_meta', 'cp_users', 'cp_grants', 'cp_sessions', 'cp_revisions', 'cp_active', 'cp_audit'})
        with releases._connection() as db:
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, releases._TABLES)
            self.assertFalse(any('user' in table or 'session' in table for table in releases._TABLES))

    def test_reinitialize_and_uninitialized_refused(self):
        self.assert_error(409, releases.initialize)
        with patch.dict(os.environ, {'CONTROL_RELEASE_DB_PATH': str(self.root / 'release/uninitialized.sqlite')}):
            self.assert_error(503, releases.list_versions, self.tokens['viewer'])
            self.assertFalse((self.root / 'release/uninitialized.sqlite').exists())

    def test_unsafe_overlap_paths_refused_without_writes(self):
        candidates = [self.control_db, self.root / 'control/new.sqlite', self.root / 'source/release.sqlite',
                      self.root / 'workspace/release.sqlite', self.root / 'business/app.sqlite',
                      Path(__file__).resolve().parents[1] / 'release.sqlite', self.root / 'new.sqlite']
        before = self.control_db.read_bytes()
        for candidate in candidates:
            with self.subTest(candidate=str(candidate)), patch.dict(os.environ, {'CONTROL_RELEASE_DB_PATH': str(candidate)}):
                self.assert_error(503, releases.initialize)
        self.assertEqual(self.control_db.read_bytes(), before)

    def test_missing_relative_and_public_parent_refused(self):
        for path in ('relative.sqlite', str(self.root / 'missing/new.sqlite')):
            with patch.dict(os.environ, {'CONTROL_RELEASE_DB_PATH': path}):
                self.assert_error(503, releases.initialize)
        self.root.joinpath('release').chmod(0o755)
        self.assert_error(503, releases.list_versions, self.tokens['viewer'])

    def test_symlink_and_hardlink_refused(self):
        link = self.root / 'release/link.sqlite'
        link.symlink_to(self.release_db)
        with patch.dict(os.environ, {'CONTROL_RELEASE_DB_PATH': str(link)}):
            self.assert_error(503, releases.list_versions, self.tokens['viewer'])
        hard = self.root / 'release/hard.sqlite'
        os.link(str(self.release_db), str(hard))
        self.assert_error(503, releases.list_versions, self.tokens['viewer'])

    def test_unknown_release_schema_refused(self):
        with closing(sqlite3.connect(str(self.release_db))) as db, db:
            db.execute('CREATE TABLE unexpected (value INTEGER)')
        self.assert_error(503, releases.list_versions, self.tokens['viewer'])

    def test_all_public_reads_authenticate(self):
        version = self.version()
        plan = self.approved(version)
        for function, args in ((releases.get_version, (version['id'],)), (releases.get_plan, (plan['id'],)),
                               (releases.list_versions, ()), (releases.list_plans, ()),
                               (releases.list_execution_requests, ()), (releases.list_audit, ())):
            self.assert_error(401, function, 'not-a-token', *args)
        security.grant('viewer', 'view', revoke=True)
        self.assert_error(403, releases.list_versions, self.tokens['viewer'])

    def test_denied_role_revoked_grant_expired_session_disabled_account(self):
        self.assert_error(403, releases.register_version, self.tokens['viewer'], self.manifest())
        security.grant('author', 'develop', revoke=True)
        self.assert_error(403, releases.register_version, self.tokens['author'], self.manifest())
        with store.transaction() as db:
            db.execute('UPDATE cp_sessions SET expires=0 WHERE digest=?', (security.digest_token(self.tokens['operator']),))
        self.assert_error(401, releases.list_plans, self.tokens['operator'])
        security.disable('reviewer')
        self.assert_error(401, releases.list_versions, self.tokens['reviewer'])

    def test_manifest_strict_types_fields_scope_no_paths_commands_or_fake_bools(self):
        cases = [('schema_version', True), ('schema_version', '1'), ('schema_version', 0),
                 ('label', 'file:///r6'), ('label', 'r6;touch'), ('runtime', 'card_workflow'),
                 ('artifact_sha256', 'sha256:' + 'a' * 64), ('source_sha256', 'A' * 64),
                 ('image_digest', 'docker.io/image:latest'), ('previous_version', '../escape')]
        for field, invalid in cases:
            value = self.manifest()
            value[field] = invalid
            with self.subTest(field=field):
                with self.assertRaises(registry.ControlError):
                    releases.register_version(self.tokens['author'], value)
        for field in ('verified', 'image_verified', 'path', 'url', 'env', 'commands', 'token', 'createdby'):
            value = self.manifest()
            value[field] = True
            self.assert_error(400, releases.register_version, self.tokens['author'], value)
        value = self.manifest()
        value['component'], value['runtime'] = 'postgres', 'postgres'
        self.assert_error(403, releases.register_version, self.tokens['author'], value)

    def test_config_and_previous_binding(self):
        value = self.manifest()
        value['config_sha256'] = 'f' * 64
        self.assert_error(409, releases.register_version, self.tokens['author'], value)
        value = self.manifest()
        value['config_revision'] = 'f' * 32
        self.assert_error(409, releases.register_version, self.tokens['author'], value)
        value = self.manifest(previous='a' * 32)
        self.assert_error(404, releases.register_version, self.tokens['author'], value)

    def test_version_immutable_duplicate_pending_and_reopen(self):
        version = self.version()
        self.assertFalse(version['execution_ready'])
        self.assertFalse(version['deployed'])
        self.assertFalse(version['image_verified'])
        self.assertEqual(version['verification_state'], 'pending')
        self.assertEqual(version['createdby'], self.users['author']['id'])
        self.assert_error(409, releases.register_version, self.tokens['author'], self.manifest())
        with releases._connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE release_versions SET label='r9.0.1' WHERE id=?", (version['id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM release_versions WHERE id=?', (version['id'],))
        self.assertEqual(releases.get_version(self.tokens['viewer'], version['id'])['hash'], version['hash'])

    def test_capacity_limits(self):
        with patch.object(releases, 'MAX_VERSIONS', 0):
            self.assert_error(409, releases.register_version, self.tokens['author'], self.manifest())
        version = self.version()
        with patch.object(releases, 'MAX_PLANS', 0):
            self.assert_error(409, releases.create_plan, self.tokens['author'], self.plan_input(version['id']))

    def test_draft_edit_submit_independent_review_stale_revision(self):
        version = self.version()
        value = self.plan_input(version['id'])
        plan = releases.create_plan(self.tokens['author'], value)
        self.assertEqual(plan['owner'], self.users['author']['id'])
        value['expires_at'] += 1
        self.assert_error(409, releases.edit_plan, self.tokens['otherdev'], plan['id'], 1, value)
        edited = releases.edit_plan(self.tokens['author'], plan['id'], 1, value)
        self.assertEqual(edited['version'], 2)
        self.assert_error(409, releases.submit_plan, self.tokens['author'], plan['id'], 1)
        self.assert_error(400, releases.submit_plan, self.tokens['author'], plan['id'], None)
        self.assert_error(400, releases.submit_plan, self.tokens['author'], plan['id'], True)
        submitted = releases.submit_plan(self.tokens['author'], plan['id'], 2)
        self.assert_error(403, releases.review_plan, self.tokens['author'], plan['id'], 3, 'approve')
        self.assert_error(403, releases.review_plan, self.tokens['viewer'], plan['id'], 3, 'approve')
        approved = releases.review_plan(self.tokens['reviewer'], plan['id'], submitted['version'], 'approve')
        self.assertEqual(approved['approved_hash'], approved['hash'])
        self.assertFalse(approved['execution_ready'])
        self.assert_error(409, releases.edit_plan, self.tokens['author'], plan['id'], approved['version'], value)

    def test_version_author_cannot_review_other_authors_plan(self):
        version = self.version()
        plan = releases.create_plan(self.tokens['otherdev'], self.plan_input(version['id']))
        plan = releases.submit_plan(self.tokens['otherdev'], plan['id'], 1)
        self.assert_error(403, releases.review_plan, self.tokens['author'], plan['id'], plan['version'], 'approve')

    def test_owner_injection_strategy_and_precheck_claims_rejected(self):
        version = self.version()
        for field, value in (('owner', self.users['operator']['id']), ('executor', 'https://invalid'),
                             ('scheduling_supported', True)):
            plan = self.plan_input(version['id'])
            plan[field] = value
            self.assert_error(400, releases.create_plan, self.tokens['author'], plan)
        for strategy in ('bluegreen', 'modelrollout', 'shell'):
            plan = self.plan_input(version['id'])
            plan['strategy'] = strategy
            self.assert_error(409, releases.create_plan, self.tokens['author'], plan)
        plan = self.plan_input(version['id'])
        plan['prechecks']['health_sha256'] = True
        self.assert_error(400, releases.create_plan, self.tokens['author'], plan)

    def test_duplicate_plan_dependency_binding_and_claims_pending(self):
        version = self.version()
        dependency = self.version(label='r6.0.2')
        value = self.plan_input(version['id'])
        value['dependencies'] = [dependency['id']]
        value['prechecks'] = {k: 'a' * 64 for k in releases._PRECHECK_FIELDS}
        plan = releases.create_plan(self.tokens['author'], value)
        self.assert_error(409, releases.create_plan, self.tokens['author'], value)
        self.assertFalse(plan['prechecks_verified'])
        self.assertEqual(plan['payload']['dependencies'][0]['manifest_sha256'], dependency['hash'])
        value['dependencies'] = [dependency['id'], dependency['id']]
        self.assert_error(400, releases.create_plan, self.tokens['author'], value)
        value['dependencies'] = [version['id']]
        self.assert_error(400, releases.create_plan, self.tokens['author'], value)

    def test_reject_cancel_expire_and_no_timer(self):
        plan = self.approved()
        cancelled = releases.cancel_plan(self.tokens['author'], plan['id'], plan['version'])
        self.assertEqual(cancelled['status'], 'cancelled')
        second = self.approved(self.version(label='r6.0.2'))
        with patch('control_plane.releases.time.time', return_value=second['payload']['expires_at'] + 1):
            expired = releases.get_plan(self.tokens['viewer'], second['id'])
        self.assertEqual(expired['status'], 'expired')
        self.assertGreater(expired['version'], second['version'])
        self.assertFalse(expired['scheduling_supported'])
        self.assertTrue(expired['manualexecution'])

    def test_operator_approval_blocked_durable_no_fabricated_deployed(self):
        plan = self.approved()
        self.assert_error(403, releases.request_execution, self.tokens['author'], plan['id'], plan['version'])
        self.assert_error(403, releases.request_execution, self.tokens['reviewer'], plan['id'], plan['version'])
        error = self.assert_error(503, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        stored = releases.get_plan(self.tokens['viewer'], plan['id'])
        requests = releases.list_execution_requests(self.tokens['viewer'])
        self.assertEqual(stored['status'], 'blocked')
        self.assertEqual(requests[0]['id'], error.request_id)
        self.assertEqual(requests[0]['state'], 'blocked')
        self.assertFalse(stored['deployed'])
        self.assertFalse(requests[0]['deployed'])
        self.assert_error(409, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        with releases._connection() as db:
            self.assertIsNone(db.execute('SELECT request_id FROM release_lease').fetchone()[0])
        self.assertFalse(hasattr(releases, 'mark_success'))

    def test_no_callback_url_executor_and_revocation_recheck(self):
        plan = self.approved()
        called = []
        self.assert_error(503, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'],
                          executor=lambda: called.append(True))
        self.assert_error(503, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'], executor='https://invalid')
        self.assertEqual(called, [])
        self.assertEqual(releases.list_execution_requests(self.tokens['viewer']), [])
        security.grant('operator', 'operate', revoke=True)
        self.assert_error(403, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        self.assertEqual(releases.get_plan(self.tokens['viewer'], plan['id'])['status'], 'approved')

    def test_future_and_expired_execution_windows(self):
        plan = self.approved(offset=300)
        self.assert_error(409, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        with patch('control_plane.releases.time.time', return_value=plan['payload']['expires_at'] + 1):
            self.assert_error(409, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        self.assertEqual(releases.list_execution_requests(self.tokens['viewer']), [])

    def test_approved_hash_tamper_fails_closed(self):
        plan = self.approved()
        with releases._connection() as db:
            db.execute('DROP TRIGGER release_plan_immutable')
            db.execute('UPDATE release_plans SET approved_hash=? WHERE id=?', ('f' * 64, plan['id']))
        self.assert_error(409, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        self.assertEqual(releases.list_execution_requests(self.tokens['viewer']), [])

    def test_restart_lease_interrupted_never_stolen(self):
        first = self.approved()
        self.assert_error(503, releases.request_execution, self.tokens['operator'], first['id'], first['version'])
        request = releases.list_execution_requests(self.tokens['viewer'])[0]
        second = self.approved(self.version(label='r6.0.2'))
        with closing(sqlite3.connect(str(self.release_db))) as db, db:
            db.execute('UPDATE release_lease SET request_id=?,expires=1 WHERE slot=1', (request['id'],))
        self.assert_error(409, releases.request_execution, self.tokens['operator'], second['id'], second['version'])
        with closing(sqlite3.connect(str(self.release_db))) as db, db:
            self.assertEqual(db.execute('SELECT state FROM release_requests WHERE id=?', (request['id'],)).fetchone()[0],
                             'interrupted')
        requests = releases.list_execution_requests(self.tokens['viewer'])
        self.assertEqual(requests[0]['state'], 'interrupted')
        self.assertEqual(requests[0]['blocker'], 'pending-reconciliation')
        self.assert_error(409, releases.request_execution, self.tokens['operator'], second['id'], second['version'])
        self.assertEqual(releases.get_plan(self.tokens['viewer'], first['id'])['blocker'], 'pending-reconciliation')

    def test_local_attestation_real_bytes_but_never_image_or_execution(self):
        version = self.attest(image='sha256:' + 'c' * 64)
        self.assertEqual(version['verification_state'], 'local-artifact-verified')
        self.assertFalse(version['image_verified'])
        self.assertFalse(version['execution_ready'])
        plan = self.approved(version)
        self.assert_error(503, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        self.assertEqual(releases.get_plan(self.tokens['viewer'], plan['id'])['blocker'], 'release-executor-not-enrolled')

    def test_attestation_absent_selfreview_and_caller_fake_bool_rejected(self):
        version = self.version()
        self.assert_error(400, releases.attest_version_cli, self.tokens['reviewer'], version['id'], {'verified': True})
        self.assert_error(403, releases.attest_version_cli, self.tokens['author'], version['id'], {})
        self.assertEqual(releases.get_version(self.tokens['viewer'], version['id'])['verification_state'], 'pending')

    def test_attestation_signature_artifact_report_and_source_mismatch(self):
        version, evidence = self.real_attestation()
        for field, bad in (('signature', '0' * 64), ('artifact_bytes', b'not-an-artifact'),
                           ('test_report_bytes', b'{"passed":true}'), ('build_report_bytes', b'{}')):
            changed = dict(evidence)
            changed[field] = bad
            self.assert_error(409, releases.attest_version_cli, self.tokens['reviewer'], version['id'], changed)
        source = self.root / 'staging' / version['id'] / 'card_workflow/main.py'
        source.write_bytes(b'print("unapproved-source")\n')
        self.assert_error(409, releases.attest_version_cli, self.tokens['reviewer'], version['id'], evidence)
        self.assertEqual(releases.get_version(self.tokens['viewer'], version['id'])['verification_state'], 'pending')

    def test_attestation_missing_private_trust_key_and_immutable(self):
        version, evidence = self.real_attestation()
        self.key_path.chmod(0o644)
        self.assert_error(503, releases.attest_version_cli, self.tokens['reviewer'], version['id'], evidence)
        self.key_path.chmod(0o600)
        releases.attest_version_cli(self.tokens['reviewer'], version['id'], evidence)
        self.assert_error(409, releases.attest_version_cli, self.tokens['reviewer'], version['id'], evidence)
        with releases._connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM release_attestations WHERE version_id=?', (version['id'],))

    def test_rollback_requires_accepted_ancestor_and_retains_business_data(self):
        old = self.attest(image='sha256:' + 'a' * 64)
        self.approved(old)
        current = self.attest(label='r6.0.2', previous=old['id'], image='sha256:' + 'b' * 64)
        self.approved(current)
        business = self.root / 'business/app.sqlite'
        with closing(sqlite3.connect(str(business))) as db, db:
            db.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, text TEXT)')
            db.execute("INSERT INTO records(text) VALUES('new-business-record')")
        before = business.read_bytes()
        window = {k: self.plan_input(old['id'])[k] for k in ('planned_at', 'expires_at')}
        rollback = releases.create_rollback_plan(self.tokens['author'], old['id'], current['id'], window)
        self.assertEqual(rollback['payload']['version_sha256'], old['hash'])
        self.assertEqual(rollback['payload']['current_version_sha256'], current['hash'])
        self.assertEqual(rollback['payload']['image_digest'], old['manifest']['image_digest'])
        self.assertTrue(rollback['payload']['preserve_business_records'])
        self.assertFalse(rollback['payload']['data_rollback'])
        self.assertFalse(rollback['execution_ready'])
        self.assertEqual(business.read_bytes(), before)
        plan = releases.submit_plan(self.tokens['author'], rollback['id'], 1)
        plan = releases.review_plan(self.tokens['reviewer'], plan['id'], plan['version'], 'approve')
        self.assert_error(503, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        self.assertEqual(business.read_bytes(), before)

    def test_rollback_rejects_unknown_unaccepted_same_unrelated_and_schema(self):
        old = self.version(image='sha256:' + 'a' * 64)
        current = self.version(label='r6.0.2', previous=old['id'], image='sha256:' + 'b' * 64)
        window = {k: self.plan_input(old['id'])[k] for k in ('planned_at', 'expires_at')}
        self.assert_error(409, releases.create_rollback_plan, self.tokens['author'], old['id'], current['id'], window)
        self.assert_error(409, releases.create_rollback_plan, self.tokens['author'], old['id'], old['id'], window)
        self.assert_error(404, releases.create_rollback_plan, self.tokens['author'], 'f' * 32, current['id'], window)
        incompatible = self.version(label='r6.0.3', previous=old['id'], schema=2)
        self.assert_error(409, releases.create_rollback_plan, self.tokens['author'], old['id'], incompatible['id'], window)
        unrelated = self.version(label='r6.0.4')
        self.assert_error(409, releases.create_rollback_plan, self.tokens['author'], old['id'], unrelated['id'], window)

    def test_no_sensitive_bytes_in_release_database_or_audit(self):
        version, evidence = self.real_attestation()
        releases.attest_version_cli(self.tokens['reviewer'], version['id'], evidence)
        plan = self.approved(version)
        self.assert_error(503, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        content = self.release_db.read_bytes()
        for secret in list(self.tokens.values()) + [self.key.decode('ascii'), 'never execute source', 'Synthetic-test-password-123']:
            self.assertNotIn(secret.encode('utf-8'), content)
        for event in releases.list_audit(self.tokens['viewer']):
            self.assertEqual(set(event), {'id', 'actor', 'event', 'target', 'hash', 'created'})

    def test_signed_evidence_semantics_not_accept_all_reports(self):
        cases = [({'image_built': True}, {}, 409), ({'schema_version': True}, {}, 409),
                 ({}, {'passed': False}, 409), ({}, {'schema_version': True}, 409),
                 ({}, {'tests_run': True}, 400), ({}, {'config_sha256': 'f' * 64}, 409),
                 ({}, {'raw_report': 'accept everything'}, 409), ({'syntax_checked': 1}, {}, 409)]
        for index, (build_change, test_change, status) in enumerate(cases):
            version, evidence = self.real_attestation(label='r6.1.' + str(index),
                                                      build_changes=build_change, test_changes=test_change)
            self.assert_error(status, releases.attest_version_cli, self.tokens['reviewer'], version['id'], evidence)
            self.assertEqual(releases.get_version(self.tokens['viewer'], version['id'])['verification_state'], 'pending')

    def test_revocation_between_initial_auth_and_persisted_lock_rechecked(self):
        authenticate = security.authenticate
        def revoke_after_auth(token, permission=None):
            principal = authenticate(token, permission)
            security.grant('author', 'develop', revoke=True)
            return principal
        with patch('control_plane.releases.security.authenticate', side_effect=revoke_after_auth):
            self.assert_error(403, releases.register_version, self.tokens['author'], self.manifest())
        self.assertEqual(releases.list_versions(self.tokens['viewer']), [])

    def test_reviewer_revocation_invalidates_execution_approval(self):
        plan = self.approved()
        security.grant('reviewer', 'review', revoke=True)
        self.assert_error(403, releases.request_execution, self.tokens['operator'], plan['id'], plan['version'])
        self.assertEqual(releases.list_execution_requests(self.tokens['viewer']), [])

    def test_concurrent_execution_duplicate_single_durable_request(self):
        from concurrent.futures import ThreadPoolExecutor
        plan = self.approved()
        def request(_):
            try:
                releases.request_execution(self.tokens['operator'], plan['id'], plan['version'])
            except registry.ControlError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(request, range(2)))
        self.assertEqual(sorted(statuses), [409, 503])
        requests = releases.list_execution_requests(self.tokens['viewer'])
        self.assertEqual(len(requests), 1)
        self.assertFalse(requests[0]['deployed'])
        blocked = releases.get_plan(self.tokens['viewer'], plan['id'])
        self.assert_error(409, releases.request_execution, self.tokens['operator'], plan['id'], blocked['version'])

    def test_rollback_without_immutable_images_withheld(self):
        old = self.attest()
        self.approved(old)
        current = self.attest(label='r6.0.2', previous=old['id'])
        self.approved(current)
        window = {k: self.plan_input(old['id'])[k] for k in ('planned_at', 'expires_at')}
        self.assert_error(409, releases.create_rollback_plan, self.tokens['author'], old['id'], current['id'], window)

    def test_all_public_mutations_reject_invalid_raw_tokens(self):
        version = self.version()
        plan = self.approved(version)
        value = self.plan_input(version['id'])
        calls = [(releases.register_version, (self.manifest(),)), (releases.create_plan, (value,)),
                 (releases.edit_plan, (plan['id'], plan['version'], value)),
                 (releases.submit_plan, (plan['id'], plan['version'])),
                 (releases.review_plan, (plan['id'], plan['version'], 'approve')),
                 (releases.cancel_plan, (plan['id'], plan['version'])),
                 (releases.request_execution, (plan['id'], plan['version'])),
                 (releases.attest_version_cli, (version['id'], {})),
                 (releases.create_rollback_plan, (version['id'], version['id'],
                                                 {k: value[k] for k in ('planned_at', 'expires_at')}))]
        for function, args in calls:
            self.assert_error(401, function, 'not-a-token', *args)

    def test_module_has_python36_syntax_and_no_external_imports(self):
        source = Path(releases.__file__).read_text()
        try:
            tree = ast.parse(source, feature_version=(3, 6))
            ast.parse(Path(__file__).read_text(), feature_version=(3, 6))
        except TypeError:  # Python 3.6 itself has no feature_version parameter.
            tree = ast.parse(source)
        forbidden = {'subprocess', 'socket', 'requests', 'fastapi', 'urllib', 'docker'}
        imports = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imports & forbidden)
        self.assertNotIn('os.system(', source)
        self.assertNotIn('threading.Timer', source)


if __name__ == '__main__':
    unittest.main()
