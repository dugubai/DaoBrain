"""r6 management tests: real stdlib SQLite/filesystem/crypto, PURE mocked API wiring.

No FastAPI server, network, Docker, business runtime or real engine is executed.
The API boundary is driven as ASGI against lightweight test-only FastAPI stubs.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_control_management_r6.py
"""
import ast
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from control_plane import registry, security, store, workspace

PASSWORD = 'r6-test-password-very-long'
CONFIG = {'enabled': True, 'request_timeout_s': 12}


class IsolatedControlCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='daobrain-r6-control-')
        self.base = Path(os.path.realpath(self.temporary.name))
        self.db_parent = self.base / 'control'
        self.source = self.base / 'pristine-r6'
        self.workspaces = self.base / 'workspaces'
        self.business = self.base / 'business'
        for path in (self.db_parent, self.source, self.workspaces, self.business):
            path.mkdir(mode=0o700)
        self.env = mock.patch.dict(os.environ, {
            'CONTROL_DB_PATH': str(self.db_parent / 'management.sqlite'),
            'CONTROL_SOURCE_ROOT': str(self.source), 'CONTROL_WORKSPACE_ROOT': str(self.workspaces),
            'APP_ORIGIN': 'http://manager.test:18126', 'APP_DB_PATH': str(self.business / 'business.sqlite'),
            'BUSINESS_DB_PATH': str(self.business / 'business.sqlite'), 'DATABASE_PATH': '',
            'APP_DATA_DIR': '', 'DATA_DIR': '', 'CONTROL_PROVISION_PASSWORD': '',
            'CONTROL_CONTROLLER_URL': '', 'CONTROL_KEY_FILE': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temporary.cleanup)
        security._attempts.clear()
        store.initialize()
        self.owner = security.provision('owner', PASSWORD, ('view', 'configure', 'develop', 'operate'))
        self.reviewer = security.provision('reviewer', PASSWORD, ('review',))
        self.other = security.provision('other', PASSWORD, ('develop',))
        self.actor = security.Principal(self.owner['id'], self.owner['name'], frozenset(self.owner['permissions']))
        self.review_actor = security.Principal(self.reviewer['id'], 'reviewer', frozenset(('review',)))
        baseline = {
            'control_plane/registry.py': '# trusted r6 registry marker\n',
            'control_plane/security.py': '# never copy control\n',
            'tests/test_trusted.py': 'raise RuntimeError("never workspace-authoritative")\n',
            'common/__init__.py': '', 'common/corpus_fixtures.py': 'TRUSTED = True\n',
            'common/security.py': '# trusted business auth baseline\n',
            'application_hub/main.py': '# trusted hub baseline\n',
            'gateway/main.py': 'VALUE = 1\n', 'gateway/page.html': '<p>baseline</p>\n',
            'gateway/.env': 'SHOULD_NOT_COPY=1\n', 'gateway/env_runtime.py': '# exclude env\n',
            'gateway/test_evil.py': 'raise RuntimeError("not an authoritative test")\n',
            'gateway/data/user.db': 'private database\n', 'gateway/run.log': 'private log\n',
            'rag_public/main.py': '# trusted other component baseline\n',
            '.git/config': '# never copy git\n',
        }
        for path, content in baseline.items():
            target = self.source / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')

    def assert_control_error(self, status, callable, *args, **kwargs):
        with self.assertRaises(registry.ControlError) as result:
            callable(*args, **kwargs)
        self.assertEqual(status, result.exception.status)

    def approved(self):
        revision = store.create_revision(self.actor, 'gateway', CONFIG)
        revision = store.submit_revision(self.actor, revision['id'], revision['version'])
        return store.review_revision(self.review_actor, revision['id'], revision['version'], 'approve')


class StoreAndSecurityRealStdlibTests(IsolatedControlCase):
    def test_no_default_users_or_implicit_grants(self):
        security.provision('unprivileged', PASSWORD)
        token = security.login('unprivileged', PASSWORD)['token']
        self.assertEqual(frozenset(), security.authenticate(token).permissions)
        for permission in store.PERMISSIONS:
            self.assert_control_error(403, security.authenticate, token, permission)
        self.assertEqual(('id', 'name', 'permissions'), security.Principal._fields)
        self.assert_control_error(409, security.provision, 'bootstrap', PASSWORD, (), True)
        self.assert_control_error(400, security.provision, 'admin', PASSWORD, ('admin',))

    def test_pbkdf2_salted_fixed_rounds(self):
        first, second = security.password_hash(PASSWORD), security.password_hash(PASSWORD)
        self.assertNotEqual(first, second)
        self.assertEqual(['pbkdf2', '240000'], first.split('$')[:2])
        self.assertTrue(security.password_matches(PASSWORD, first))
        self.assertFalse(security.password_matches(PASSWORD + 'x', first))
        self.assertFalse(security.password_matches(PASSWORD, first.replace('240000', '1')))
        self.assertFalse(security.password_matches(PASSWORD, 'pbkdf2$240000$00$00'))

    def test_raw_token_expiry_grant_disable_and_logout_rechecked(self):
        result = security.login('owner', PASSWORD)
        token = result['token']
        self.assertEqual(43, len(token))
        self.assertEqual(set(('token', 'expires', 'user')), set(result))
        self.assertEqual(self.owner['id'], security.authenticate(token, 'configure').id)
        with store.transaction() as db:
            session = db.execute('SELECT * FROM cp_sessions').fetchone()
            self.assertNotIn(token, dict(session).values())
            self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), session['digest'])
        self.assert_control_error(401, security.authenticate, 'Bearer ' + token)
        self.assert_control_error(401, security.authenticate, token + '\n')
        security.grant('owner', 'configure', revoke=True)
        self.assert_control_error(403, security.authenticate, token, 'configure')
        # A stale or forged Principal never restores a revoked persisted permission.
        self.assert_control_error(403, store.create_revision, self.actor, 'gateway', CONFIG)
        security.grant('owner', 'configure')
        self.assertEqual(self.owner['id'], security.authenticate(token, 'configure').id)
        security.logout(token)
        self.assert_control_error(401, security.authenticate, token)
        token = security.login('owner', PASSWORD)['token']
        with store.transaction() as db:
            db.execute('UPDATE cp_sessions SET expires=0')
        self.assert_control_error(401, security.authenticate, token)
        token = security.login('owner', PASSWORD)['token']
        security.disable('owner')
        self.assert_control_error(401, security.authenticate, token)
        self.assert_control_error(401, security.login, 'owner', PASSWORD)

    def test_session_capacity_and_expired_cleanup(self):
        with store.transaction() as db:
            for number in range(security.MAX_USER_SESSIONS):
                db.execute('INSERT INTO cp_sessions VALUES(?,?,?,?)',
                           ('%064x' % number, self.owner['id'], 9999999999, 1))
        self.assert_control_error(429, security.login, 'owner', PASSWORD)
        with store.transaction() as db:
            db.execute('UPDATE cp_sessions SET expires=0')
        result = security.login('owner', PASSWORD)
        self.assertEqual(43, len(result['token']))
        with store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM cp_sessions').fetchone()[0])

    def test_global_session_limit_and_login_rate_limit(self):
        with store.transaction() as db:
            for number in range(security.MAX_SESSIONS):
                db.execute('INSERT INTO cp_sessions VALUES(?,?,?,?)',
                           ('%064x' % number, self.other['id'], 9999999999, 1))
        self.assert_control_error(429, security.login, 'owner', PASSWORD)
        security._attempts.extend([security.time.monotonic()] * 12)
        self.assert_control_error(429, security.login, 'unknown', PASSWORD)

    def test_unknown_credentials_and_permission_fail_closed(self):
        self.assert_control_error(401, security.login, 'missing', PASSWORD)
        self.assert_control_error(401, security.login, 'owner', 'incorrect-password')
        self.assert_control_error(401, security.authenticate, None)
        token = security.login('owner', PASSWORD)['token']
        self.assert_control_error(403, security.authenticate, token, 'admin')
        self.assert_control_error(403, security.authenticate, token, 'review')

    def test_explicit_schema_and_transaction_rollback(self):
        self.assert_control_error(409, store.initialize)
        with self.assertRaises(RuntimeError):
            with store.transaction() as db:
                self.assertIsInstance(db.execute('SELECT * FROM cp_users').fetchone(), sqlite3.Row)
                db.execute("UPDATE cp_users SET name='changed' WHERE name='owner'")
                raise RuntimeError('rollback')
        with store.transaction() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM cp_users WHERE name='owner'").fetchone())
            db.execute('PRAGMA user_version=99')
        self.assert_control_error(503, security.login, 'owner', PASSWORD)

    def test_unprovisioned_relative_missing_parent_and_business_paths(self):
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(self.db_parent / 'missing.sqlite')}):
            self.assert_control_error(503, store.transaction().__enter__)
            self.assertFalse((self.db_parent / 'missing.sqlite').exists())
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': 'relative.sqlite'}):
            self.assert_control_error(503, store.database_path)
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(self.business / 'business.sqlite')}):
            self.assert_control_error(503, store.database_path)
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(self.source / 'management.sqlite')}):
            self.assert_control_error(503, store.database_path)
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(self.base / 'not-provisioned' / 'db.sqlite')}):
            with self.assertRaises(OSError):
                store.initialize()

    def test_db_links_and_sqlite_sidecar_links_rejected(self):
        alias = self.base / 'linked-control'
        alias.symlink_to(self.db_parent, target_is_directory=True)
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(alias / 'management.sqlite')}):
            with self.assertRaises(OSError):
                with store.transaction():
                    pass
        os.link(str(self.db_parent / 'management.sqlite'), str(self.db_parent / 'hard.sqlite'))
        self.assert_control_error(503, security.login, 'owner', PASSWORD)

    def test_metadata_audit_never_credentials_or_source(self):
        token = security.login('owner', PASSWORD)['token']
        store.create_revision(self.actor, 'gateway', CONFIG)
        with store.transaction() as db:
            events = json.dumps([dict(row) for row in db.execute('SELECT * FROM cp_audit')])
            self.assertNotIn(token, events)
            self.assertNotIn(PASSWORD, events)
            self.assertNotIn('request_timeout_s', events)
            self.assert_control_error(400, store.audit, db, self.owner['id'], 'test', 'target', {'token': token})

    def test_empty_initialization_and_explicit_bootstrap_no_permissions(self):
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(self.db_parent / 'empty.sqlite')}):
            store.initialize()
            with store.transaction() as db:
                self.assertEqual(0, db.execute('SELECT count(*) FROM cp_users').fetchone()[0])
            security.provision('bootstrap', PASSWORD, bootstrap=True)
            token = security.login('bootstrap', PASSWORD)['token']
            self.assertEqual(frozenset(), security.authenticate(token).permissions)
            security.grant('bootstrap', 'view')
            self.assertEqual(frozenset(('view',)), security.authenticate(token).permissions)
            self.assert_control_error(403, security.authenticate, token, 'operate')

    def test_disabled_user_with_still_present_session_and_sqlite_sidecar(self):
        token = security.login('owner', PASSWORD)['token']
        with store.transaction() as db:
            db.execute('UPDATE cp_users SET enabled=0 WHERE id=?', (self.owner['id'],))
            self.assertEqual(1, db.execute('SELECT count(*) FROM cp_sessions').fetchone()[0])
        self.assert_control_error(401, security.authenticate, token)
        (self.db_parent / 'management.sqlite-journal').symlink_to(self.source / 'gateway' / 'main.py')
        self.assert_control_error(503, security.authenticate, token)

    def test_existing_business_tables_never_imported_or_migrated(self):
        business = self.business / 'foreign.sqlite'
        fd = os.open(str(business), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        db = sqlite3.connect(str(business))
        db.execute('CREATE TABLE users (name TEXT, role TEXT)')
        db.execute("INSERT INTO users VALUES('business-admin','admin')")
        db.commit()
        db.close()
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(business)}):
            self.assert_control_error(503, security.login, 'business-admin', PASSWORD)
            self.assert_control_error(409, store.initialize)
        db = sqlite3.connect(str(business))
        try:
            self.assertEqual([('business-admin', 'admin')], db.execute('SELECT * FROM users').fetchall())
            self.assertEqual(0, db.execute("SELECT count(*) FROM sqlite_master WHERE name='cp_users'").fetchone()[0])
        finally:
            db.close()

    def test_exact_config_types_and_two_person_state_machine(self):
        for bad in ({}, {'enabled': 1, 'request_timeout_s': 12}, {'enabled': True, 'request_timeout_s': True},
                    {'enabled': True, 'request_timeout_s': 121}, dict(CONFIG, target='evil')):
            self.assert_control_error(400, store.create_revision, self.actor, 'gateway', bad)
        revision = store.create_revision(self.actor, 'gateway', CONFIG)
        self.assertIsNone(store.active_policy('gateway'))
        self.assert_control_error(409, store.edit_revision, self.actor, revision['id'], 2, CONFIG)
        self.assert_control_error(400, store.submit_revision, self.actor, revision['id'], True)
        changed = dict(CONFIG, request_timeout_s=24)
        revision = store.edit_revision(self.actor, revision['id'], 1, changed)
        self.assertEqual(2, revision['version'])
        revision = store.submit_revision(self.actor, revision['id'], 2)
        security.grant('owner', 'review')
        self.assert_control_error(409, store.review_revision, self.actor, revision['id'], 3, 'approve')
        revision = store.review_revision(self.review_actor, revision['id'], 3, 'approve')
        self.assert_control_error(409, store.edit_revision, self.actor, revision['id'], 4, CONFIG)
        revision = store.activate_revision(self.actor, revision['id'], 4)
        self.assertEqual('activated', revision['state'])
        self.assertEqual(dict(changed, hash=registry.digest(changed)), store.active_policy('gateway'))
        self.assertTrue(revision['policy_only'])
        self.assertFalse(revision['business_config_implemented'])
        self.assert_control_error(409, store.activate_revision, self.actor, revision['id'], 5)

    def test_reject_immutable_review_permission_and_no_execution_grant(self):
        revision = store.create_revision(self.actor, 'gateway', CONFIG)
        self.assert_control_error(409, store.activate_revision, self.actor, revision['id'], 1)
        revision = store.submit_revision(self.actor, revision['id'], 1)
        self.assert_control_error(400, store.review_revision, self.review_actor, revision['id'], 2, 'approved')
        security.grant('reviewer', 'review', revoke=True)
        self.assert_control_error(403, store.review_revision, self.review_actor, revision['id'], 2, 'approve')
        security.grant('reviewer', 'review')
        revision = store.review_revision(self.review_actor, revision['id'], 2, 'reject')
        self.assert_control_error(409, store.edit_revision, self.actor, revision['id'], 3, CONFIG)
        self.assert_control_error(409, store.activate_revision, self.actor, revision['id'], 3)
        self.assertIsNone(store.active_policy('gateway'))
        token = security.login('reviewer', PASSWORD)['token']
        self.assert_control_error(403, security.authenticate, token, 'operate')

    def test_database_enforces_reviewed_content_immutability(self):
        revision = self.approved()
        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction() as db:
                db.execute("UPDATE cp_revisions SET config='{}' WHERE id=?", (revision['id'],))
        self.assertEqual(CONFIG, store.list_revisions('gateway')[0]['config'])
        self.assert_control_error(404, store.list_revisions, 'unregistered')


class WorkspaceRealStdlibTests(IsolatedControlCase):
    def new_workspace(self):
        return workspace.create(self.owner['id'], 'gateway')

    def test_allowlist_baselines_ownership_and_no_publication(self):
        info = self.new_workspace()
        files = {item['path']: item for item in info['files']}
        self.assertEqual(set(('gateway/main.py', 'gateway/page.html', 'common/__init__.py',
                              'common/corpus_fixtures.py', 'common/security.py', 'application_hub/main.py',
                              'rag_public/main.py')), set(files))
        self.assertTrue(files['gateway/main.py']['editable'])
        self.assertFalse(files['common/security.py']['editable'])
        self.assertFalse(files['rag_public/main.py']['editable'])
        self.assertFalse(info['publish_enabled'])
        self.assert_control_error(404, workspace.describe, info['id'], self.other['id'])
        self.assert_control_error(403, workspace.create, self.owner['id'], 'offline-tests')
        self.assert_control_error(403, workspace.write_file, info['id'], self.owner['id'], 'common/security.py',
                                  files['common/security.py']['sha256'], '# altered\n')
        self.assert_control_error(403, workspace.write_file, info['id'], self.owner['id'], 'gateway/new.py', '0' * 64, '')

    def test_atomic_compare_save_retained_backups_and_plain_diff(self):
        info = self.new_workspace()
        file = workspace.read_file(info['id'], self.owner['id'], 'gateway/page.html')
        self.assert_control_error(409, workspace.write_file, info['id'], self.owner['id'], file['path'], '0' * 64, 'bad')
        new_text = '<script>alert("not HTML-rendered")</script>\n'
        result = workspace.write_file(info['id'], self.owner['id'], file['path'], file['sha256'], new_text)
        self.assertTrue(result['backup_retained'])
        self.assertNotEqual(info['source_hash'], result['source_hash'])
        self.assertEqual(new_text, workspace.read_file(info['id'], self.owner['id'], file['path'])['content'])
        backup_files = list((Path(info['root']).parent / 'backups').iterdir())
        self.assertEqual(1, len(backup_files))
        self.assertEqual(file['content'], backup_files[0].read_text())
        changes = workspace.diff(info['id'], self.owner['id'])
        self.assertEqual('plain_text', changes['format'])
        self.assertEqual('gateway/page.html', changes['changes'][0]['path'])
        self.assertIn('+<script>', changes['changes'][0]['diff'])
        self.assertFalse(changes['publish_enabled'])
        self.assertEqual('<p>baseline</p>\n', (self.source / file['path']).read_text())
        self.assert_control_error(409, workspace.write_file, info['id'], self.owner['id'], file['path'], file['sha256'], 'stale')

    def test_snapshot_is_frozen_hash_bound_and_owned(self):
        info = self.new_workspace()
        self.assert_control_error(409, workspace.snapshot, info['id'], self.owner['id'], '0' * 64)
        self.assert_control_error(404, workspace.snapshot, info['id'], self.other['id'], info['source_hash'])
        frozen = workspace.snapshot(info['id'], self.owner['id'], info['source_hash'])
        self.assertTrue(frozen['frozen'])
        self.assertNotEqual(info['root'], frozen['root'])
        self.assertEqual(0, (Path(frozen['root']) / 'gateway' / 'main.py').stat().st_mode & 0o222)
        self.assertEqual(frozen['root'], workspace.snapshot(info['id'], self.owner['id'], info['source_hash'])['root'])
        file = workspace.read_file(info['id'], self.owner['id'], 'gateway/main.py')
        workspace.write_file(info['id'], self.owner['id'], file['path'], file['sha256'], 'VALUE = 2\n')
        self.assertEqual('VALUE = 1\n', (Path(frozen['root']) / file['path']).read_text())
        self.assertFalse(any(p.startswith(('tests/', 'control_plane/')) for p in [f['path'] for f in frozen['files']]))
        self.assert_control_error(409, workspace.snapshot, info['id'], self.owner['id'], info['source_hash'])

    def test_paths_size_revoked_grants_and_extra_files_fail_closed(self):
        info = self.new_workspace()
        for path in ('../security.py', '/etc/passwd', 'gateway/../main.py', 'gateway\\main.py', 'gateway//main.py'):
            self.assert_control_error(400, workspace.read_file, info['id'], self.owner['id'], path)
        file = workspace.read_file(info['id'], self.owner['id'], 'gateway/main.py')
        self.assert_control_error(413, workspace.write_file, info['id'], self.owner['id'], file['path'], file['sha256'],
                                  'x' * (workspace.MAX_FILE_BYTES + 1))
        security.grant('owner', 'develop', revoke=True)
        self.assert_control_error(403, workspace.describe, info['id'], self.owner['id'])
        security.grant('owner', 'develop')
        (Path(info['root']) / 'gateway' / 'test_evil.py').write_text('not a trusted test')
        self.assert_control_error(409, workspace.describe, info['id'], self.owner['id'])

    def test_trusted_baselines_tampered_on_disk_fail_closed(self):
        info = self.new_workspace()
        target = Path(info['root']) / 'common' / 'corpus_fixtures.py'
        self.assertEqual(0, target.stat().st_mode & 0o222)
        replacement = target.parent / 'replacement.tmp'
        replacement.write_text('TRUSTED = False\n')
        # Simulate privileged local inode replacement; API itself cannot modify this baseline.
        os.replace(str(replacement), str(target))
        self.assert_control_error(409, workspace.describe, info['id'], self.owner['id'])
        self.assert_control_error(409, workspace.snapshot, info['id'], self.owner['id'], info['source_hash'])

    def test_workspace_file_symlink_and_hardlink_rejected(self):
        info = self.new_workspace()
        (Path(info['root']) / 'gateway' / 'alias.py').symlink_to(self.source / 'gateway' / 'main.py')
        self.assert_control_error(409, workspace.describe, info['id'], self.owner['id'])
        other = self.new_workspace()
        os.link(str(Path(other['root']) / 'gateway' / 'main.py'),
                str(Path(other['root']) / 'gateway' / 'alias.py'))
        self.assert_control_error(409, workspace.describe, other['id'], self.owner['id'])

    def test_source_directory_links_and_intermediate_roots_rejected(self):
        (self.source / 'gateway' / 'nested').symlink_to(self.source / 'common', target_is_directory=True)
        self.assert_control_error(409, workspace.create, self.owner['id'], 'gateway')
        alias = self.base / 'source-alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with mock.patch.dict(os.environ, {'CONTROL_SOURCE_ROOT': str(alias)}):
            with self.assertRaises(OSError):
                workspace.create(self.owner['id'], 'gateway')

    def test_workspace_roots_exclusive_separate_and_private(self):
        with mock.patch.dict(os.environ, {'CONTROL_WORKSPACE_ROOT': str(self.source / 'nested')}):
            self.assert_control_error(503, workspace.create, self.owner['id'], 'gateway')
        (self.workspaces / 'unrelated-data').write_text('must not coexist')
        self.assert_control_error(503, workspace.create, self.owner['id'], 'gateway')

    def test_cap_count_and_storage_before_copy_or_save(self):
        with mock.patch.object(workspace, 'MAX_WORKSPACES', 1):
            info = self.new_workspace()
            self.assert_control_error(409, workspace.create, self.owner['id'], 'gateway')
        file = workspace.read_file(info['id'], self.owner['id'], 'gateway/main.py')
        used = sum(p.stat().st_size for p in Path(info['root']).parent.rglob('*') if p.is_file())
        with mock.patch.object(workspace, 'MAX_BYTES', used + 2):
            self.assert_control_error(413, workspace.write_file, info['id'], self.owner['id'], file['path'], file['sha256'],
                                      'VALUE = 3\n')
        with mock.patch.object(workspace, 'MAX_VERSIONS', 0):
            self.assert_control_error(409, workspace.write_file, info['id'], self.owner['id'], file['path'], file['sha256'],
                                      'VALUE = 3\n')

    def test_inode_substitution_during_file_open_rejected(self):
        target = self.source / 'gateway' / 'main.py'
        original_open = workspace.os.open
        triggered = [False]
        target_directory = target.parent.stat()

        def substituted_open(path, flags, *args, **kwargs):
            directory_fd = kwargs.get('dir_fd')
            directory = os.fstat(directory_fd) if directory_fd is not None else None
            is_target = directory is not None and (directory.st_dev, directory.st_ino) == (
                target_directory.st_dev, target_directory.st_ino)
            if path == 'main.py' and is_target and flags & os.O_NONBLOCK and not triggered[0]:
                triggered[0] = True
                replacement = target.parent / 'replacement.tmp'
                replacement.write_text('VALUE = 9\n')
                os.replace(str(replacement), str(target))
            return original_open(path, flags, *args, **kwargs)
        with mock.patch.object(workspace.os, 'open', side_effect=substituted_open):
            self.assert_control_error(409, workspace.create, self.owner['id'], 'gateway')
        self.assertTrue(triggered[0])


@contextmanager
def stubbed_main():
    """PURE API contract test, not an installed/running FastAPI application."""
    class Response:
        def __init__(self, content, status_code=200, headers=None):
            self.content, self.status_code, self.headers = content, status_code, headers or {}

        async def __call__(self, scope, receive, send):
            await send({'type': 'http.response.start', 'status': self.status_code, 'headers': []})
            await send({'type': 'http.response.body', 'body': json.dumps(self.content).encode()})

    class App:
        def __init__(self, **kwargs):
            self.routes = []
            self.options = kwargs

        def add_middleware(self, *args, **kwargs):
            pass

        def exception_handler(self, *args):
            return lambda function: function

        def route(self, method, path, **kwargs):
            def record(function):
                self.routes.append((method, path))
                return function
            return record

        def get(self, path, **kwargs):
            return self.route('GET', path, **kwargs)

        def post(self, path, **kwargs):
            return self.route('POST', path, **kwargs)

        def patch(self, path, **kwargs):
            return self.route('PATCH', path, **kwargs)

    fastapi = types.ModuleType('fastapi')
    fastapi.FastAPI = App
    for field in ('Body', 'Header', 'Query'):
        setattr(fastapi, field, lambda *args, **kwargs: kwargs.get('default'))
    fastapi.Depends = lambda dependency: dependency
    exceptions = types.ModuleType('fastapi.exceptions')
    exceptions.RequestValidationError = type('RequestValidationError', (Exception,), {})
    responses = types.ModuleType('fastapi.responses')
    responses.JSONResponse = responses.HTMLResponse = Response
    with mock.patch.dict(sys.modules, {'fastapi': fastapi, 'fastapi.exceptions': exceptions, 'fastapi.responses': responses}):
        specification = importlib.util.spec_from_file_location('_r6_main_pure_stub', str(PROJECT / 'control_plane' / 'main.py'))
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        yield module


class ApiPureMockTests(IsolatedControlCase):
    def test_fixed_routes_no_signup_publish_or_business_routers(self):
        with stubbed_main() as main:
            self.assertEqual(37, len(main.app.routes))
            self.assertEqual(set(('GET', 'POST', 'PATCH')), {method for method, _path in main.app.routes})
            self.assertFalse(any('users' in path or 'publish' in path or 'signup' in path for _method, path in main.app.routes))
            self.assertIsNone(main.app.options['docs_url'])
            self.assertIsNone(main.app.options['openapi_url'])
            self.assertFalse(main.app.options['redirect_slashes'])
            self.assert_control_error(422, main.login, {'name': 'owner', 'password': PASSWORD, 'role': 'admin'})

    def test_controller_raw_token_fixed_payload_and_independent_operate(self):
        token = security.login('owner', PASSWORD)['token']
        auth = (security.authenticate(token), token)
        revision = self.approved()
        store.activate_revision(self.actor, revision['id'], revision['version'])
        payload = {'component': 'gateway', 'action': 'start', 'expected_config_hash': registry.digest(CONFIG)}
        with stubbed_main() as main:
            client = mock.Mock()
            client.request.return_value = {'state': 'exited', 'running': False}
            with mock.patch.object(main, 'controller_client', return_value=client):
                self.assertFalse(main.action(payload, auth)['running'])
                client.request.assert_called_once_with('POST', '/v1/actions', payload, token)
                self.assert_control_error(422, main.action, dict(payload, target='http://evil'), auth)
                self.assert_control_error(409, main.action, dict(payload, expected_config_hash='0' * 64), auth)
                security.grant('owner', 'operate', revoke=True)
                self.assert_control_error(403, main.action, payload, auth)

    def test_policy_disabled_admission_and_no_false_runtime_green(self):
        revision = store.create_revision(self.actor, 'gateway', dict(CONFIG, enabled=False))
        revision = store.submit_revision(self.actor, revision['id'], 1)
        revision = store.review_revision(self.review_actor, revision['id'], 2, 'approve')
        store.activate_revision(self.actor, revision['id'], 3)
        token = security.login('owner', PASSWORD)['token']
        auth = (security.authenticate(token), token)
        with stubbed_main() as main:
            payload = {'component': 'gateway', 'action': 'start', 'expected_config_hash': registry.digest(dict(CONFIG, enabled=False))}
            self.assert_control_error(409, main.action, payload, auth)
            with mock.patch.object(main, 'controller_client', side_effect=registry.ControlError('unavailable', 503)):
                result = main.components(auth)
            self.assertFalse(result['controller_observed'])
            self.assertFalse(result['business_config_implemented'])
            self.assertTrue(all(item['runtime_state']['state'] == 'unobserved' for item in result['components']))
            self.assertTrue(all('running_config_hash' not in item['runtime_state'] for item in result['components']))

    def test_peer_envelope_actual_runtime_and_ui_review_contract(self):
        token = security.login('owner', PASSWORD)['token']
        auth = (security.authenticate(token), token)
        review_token = security.login('reviewer', PASSWORD)['token']
        review_auth = (security.authenticate(review_token), review_token)
        revision = store.create_revision(self.actor, 'gateway', CONFIG)
        revision = store.submit_revision(self.actor, revision['id'], 1)
        with stubbed_main() as main:
            result = main.review_revision(revision['id'], {'expected_version': 2, 'decision': 'approved'}, review_auth)
            self.assertEqual('approved', result['status'])
            client = mock.Mock()
            client.request.return_value = {'controller': {}, 'runtime': {'runtimes': {
                'gateway': {'state': 'running', 'running': True, 'running_config_hash': '1' * 64}},
                'test': {'ready': True}, 'build': {'ready': False}}}
            with mock.patch.object(main, 'controller_client', return_value=client):
                response = main.components(auth)
            component = next(c for c in response['components'] if c['id'] == 'gateway')
            self.assertTrue(component['runtime_observed'])
            self.assertEqual('1' * 64, component['runtime_state']['running_config_hash'])
            self.assertIsNone(component['active_policy'])
            self.assertTrue(response['test']['ready'])
            self.assertFalse(response['build']['ready'])

    def test_jobs_owned_hash_kind_and_only_raw_signed_internal_arguments(self):
        info = workspace.create(self.owner['id'], 'gateway')
        token = security.login('owner', PASSWORD)['token']
        auth = (security.authenticate(token), token)
        payload = {'workspace_id': info['id'], 'expected_hash': info['source_hash'], 'kind': 'test'}
        with stubbed_main() as main:
            client = mock.Mock()
            client.request.return_value = {'status': 'not_enrolled'}
            with mock.patch.object(main, 'controller_client', return_value=client):
                self.assertEqual('not_enrolled', main.create_job(payload, auth)['status'])
                client.request.assert_called_once_with('POST', '/v1/jobs', payload, token)
                self.assert_control_error(422, main.create_job, dict(payload, command='touch /live'), auth)
                self.assert_control_error(422, main.create_job, dict(payload, kind='publish'), auth)
                self.assert_control_error(409, main.create_job, dict(payload, expected_hash='0' * 64), auth)

    def test_browser_boundary_real_byte_origin_duplicate_json_and_headers(self):
        async def exercise(main, headers=None, chunks=None, method='POST', path='/api/auth/login'):
            called, output = [], []
            async def downstream(scope, receive, send):
                called.append(await receive())
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'{}'})
            queue = list(chunks or [b'{"name":"owner","password":"long-password-test"}'])
            async def receive():
                if not queue:
                    return {'type': 'http.disconnect'}
                data = queue.pop(0)
                return {'type': 'http.request', 'body': data, 'more_body': bool(queue)}
            async def send(message):
                output.append(message)
            scope = {'type': 'http', 'method': method, 'path': path, 'headers': headers or [
                (b'host', b'manager.test:18126'), (b'origin', b'http://manager.test:18126'),
                (b'content-type', b'application/json')]}
            await main.BrowserBoundary(downstream)(scope, receive, send)
            return called, output
        with stubbed_main() as main:
            called, output = asyncio.run(exercise(main))
            self.assertTrue(called)
            self.assertIn((b'x-content-type-options', b'nosniff'), output[0]['headers'])
            cases = [
                ({'chunks': [b'{"x":1,"x":2}']}, 422),
                ({'chunks': [b'{"x":NaN}']}, 422),
                ({'chunks': [b'{"x":"\\ud800"}']}, 422),
                ({'chunks': [b'x' * 32000, b'x' * 34000]}, 413),
                ({'method': 'GET', 'chunks': [b'x']}, 413),
                ({'headers': [(b'host', b'evil.test')]}, 400),
                ({'headers': [(b'host', b'manager.test:18126'), (b'origin', b'http://evil.test')]}, 403),
                ({'headers': [(b'host', b'manager.test:18126'), (b'origin', b'http://manager.test:18126'),
                               (b'authorization', b'Bearer x'), (b'authorization', b'Bearer y')]}, 400),
                ({'headers': [(b'host', b'manager.test:18126'), (b'origin', b'http://manager.test:18126'),
                               (b'content-length', b'2'), (b'transfer-encoding', b'chunked')]}, 400),
            ]
            for arguments, expected_status in cases:
                called, output = asyncio.run(exercise(main, **arguments))
                self.assertFalse(called)
                self.assertEqual(expected_status, output[0]['status'])
            with mock.patch.dict(os.environ, {'APP_ORIGIN': ''}):
                called, output = asyncio.run(exercise(main))
                self.assertFalse(called)
                self.assertEqual(503, output[0]['status'])


class TrustedProtocolPureTests(unittest.TestCase):
    def test_only_signed_bound_responses_accepted_no_transport_executed(self):
        from control_plane import protocol
        key, request_id, nonce, body = b'k' * 32, '1' * 32, '2' * 32, b'{"state":"stopped"}'
        headers = protocol.response_headers(key, request_id, nonce, 200, body)
        protocol.verify_response(key, request_id, nonce, 200, body, headers)
        for changed_headers, changed_body, changed_status in (({}, body, 200), (headers, body + b' ', 200),
                                                              (headers, body, 500)):
            with self.assertRaises(registry.ControlError):
                protocol.verify_response(key, request_id, nonce, changed_status, changed_body, changed_headers)
        self.assertRaises(registry.ControlError, protocol.route, 'POST', '/v1/exec')

    def test_core_syntax_is_python36_and_imports_stdlib_or_trusted_control_only(self):
        for filename in ('store.py', 'security.py', 'workspace.py'):
            source = (PROJECT / 'control_plane' / filename).read_text()
            tree = ast.parse(source, filename=filename, feature_version=(3, 6))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or '')
            self.assertFalse(any(name.startswith(('fastapi', 'pydantic', 'application_hub', 'common', 'dataclasses', '__future__'))
                                 for name in imports), imports)


if __name__ == '__main__':
    unittest.main(verbosity=2)
