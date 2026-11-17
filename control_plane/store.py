"""Independent management database; initialization is an explicit provisioning step.

Policy revisions are NOT business application configuration. No business module
is imported here. All public mutation methods recheck persisted grants.
"""
from contextlib import contextmanager
import json
import os
import sqlite3
import stat
import time
from uuid import uuid4

from control_plane import registry

SCHEMA_VERSION = 1
PERMISSIONS = frozenset(('view', 'configure', 'review', 'develop', 'operate'))
MAX_USERS = 100
MAX_REVISIONS = 512

_SCHEMA = (
    'CREATE TABLE cp_meta (version INTEGER NOT NULL)',
    'CREATE TABLE cp_users (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, '
    'password TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))',
    'CREATE TABLE cp_grants (user_id TEXT NOT NULL REFERENCES cp_users(id), '
    "permission TEXT NOT NULL CHECK(permission IN ('view','configure','review','develop','operate')), "
    'PRIMARY KEY(user_id,permission))',
    'CREATE TABLE cp_sessions (digest TEXT PRIMARY KEY, user_id TEXT NOT NULL '
    'REFERENCES cp_users(id), expires INTEGER NOT NULL, created INTEGER NOT NULL)',
    'CREATE INDEX cp_session_expiry ON cp_sessions(expires)',
    'CREATE TABLE cp_revisions (id TEXT PRIMARY KEY, component TEXT NOT NULL, '
    'owner TEXT NOT NULL REFERENCES cp_users(id), version INTEGER NOT NULL, '
    "state TEXT NOT NULL CHECK(state IN ('draft','submitted','approved','rejected','activated')), "
    'config TEXT NOT NULL, hash TEXT NOT NULL, reviewer TEXT REFERENCES cp_users(id), '
    'created INTEGER NOT NULL, updated INTEGER NOT NULL)',
    'CREATE TABLE cp_active (component TEXT PRIMARY KEY, revision TEXT NOT NULL '
    'REFERENCES cp_revisions(id))',
    'CREATE TABLE cp_audit (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, '
    'event TEXT NOT NULL, target TEXT NOT NULL, metadata TEXT NOT NULL, created INTEGER NOT NULL)',
    "CREATE TRIGGER cp_revision_immutable BEFORE UPDATE ON cp_revisions "
    "WHEN OLD.state IN ('approved','activated','rejected') AND "
    "(NEW.config != OLD.config OR NEW.hash != OLD.hash OR NEW.owner != OLD.owner "
    "OR NEW.component != OLD.component OR NEW.reviewer != OLD.reviewer "
    "OR NOT (OLD.state='approved' AND NEW.state='activated')) "
    "BEGIN SELECT RAISE(ABORT,'immutable revision'); END",
)


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def open_directory(path, private=False):
    """Open an absolute directory by descriptors, rejecting symlinks at every level."""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise registry.ControlError('Absolute normalized preprovisioned path required', 503)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split(os.sep)[1:]:
            if not part:
                continue
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                opened = os.fstat(child)
                after = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if (not stat.S_ISDIR(before.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                        or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise registry.ControlError('Directory changed during open', 503)
            except Exception:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise registry.ControlError('Private owner-only directory required', 503)
        return fd
    except Exception:
        os.close(fd)
        raise


def database_path():
    path = os.environ.get('CONTROL_DB_PATH', '')
    if not os.path.isabs(path) or os.path.normpath(path) != path or path.endswith(os.sep):
        raise registry.ControlError('CONTROL_DB_PATH must be an absolute dedicated database path', 503)
    # Never open a business database, even if an operator accidentally points at it.
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    forbidden = [project]
    for key in ('CONTROL_SOURCE_ROOT', 'CONTROL_WORKSPACE_ROOT', 'APP_DATA_DIR', 'DATA_DIR'):
        if os.environ.get(key):
            forbidden.append(os.path.abspath(os.environ[key]))
    if any(_inside(path, root) for root in forbidden):
        raise registry.ControlError('Management storage must be separate from source/data/workspaces', 503)
    for key in ('APP_DB_PATH', 'BUSINESS_DB_PATH', 'DATABASE_PATH'):
        value = os.environ.get(key)
        if value and os.path.realpath(value) == os.path.realpath(path):
            raise registry.ControlError('Business database cannot be management storage', 503)
    return path


def _file_check(parent, name, required=False):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise registry.ControlError('Management database is not provisioned', 503)
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise registry.ControlError('Unsafe management database file', 503)
    return info


@contextmanager
def _connection(initializing=False):
    path = database_path()
    parent = open_directory(os.path.dirname(path), private=True)
    db = None
    try:
        name = os.path.basename(path)
        before = _file_check(parent, name, required=True)
        for suffix in ('-journal', '-wal', '-shm'):
            _file_check(parent, name + suffix)
        parent_info = os.fstat(parent)
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        after = _file_check(parent, name, required=True)
        current_parent = os.stat(os.path.dirname(path), follow_symlinks=False)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (parent_info.st_dev, parent_info.st_ino) != (current_parent.st_dev, current_parent.st_ino)):
            raise registry.ControlError('Management storage changed during open', 503)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('BEGIN IMMEDIATE')
        if not initializing:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {'cp_meta', 'cp_users', 'cp_grants', 'cp_sessions', 'cp_revisions', 'cp_active', 'cp_audit'}
            if tables != expected or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise registry.ControlError('Unsupported management schema; explicit provisioning required', 503)
            versions = db.execute('SELECT version FROM cp_meta').fetchall()
            if len(versions) != 1 or versions[0][0] != SCHEMA_VERSION:
                raise registry.ControlError('Unsupported management schema', 503)
        yield db
        db.commit()
    except Exception:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()
        os.close(parent)


@contextmanager
def transaction():
    """Existing cp schema only; sqlite.Row rows; BEGIN IMMEDIATE, commit/rollback."""
    with _connection() as db:
        yield db


def initialize():
    """Create an empty, exclusively new cp database. No accounts or migrations."""
    path = database_path()
    parent = open_directory(os.path.dirname(path), private=True)
    try:
        fd = os.open(os.path.basename(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        os.close(fd)
    except FileExistsError:
        raise registry.ControlError('Existing database: initialization refused', 409)
    finally:
        os.close(parent)
    with _connection(initializing=True) as db:
        if db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]:
            raise registry.ControlError('Nonempty database: initialization refused', 409)
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute('INSERT INTO cp_meta VALUES(?)', (SCHEMA_VERSION,))
        db.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        audit(db, 'provision-cli', 'schema.initialize', 'control', {})


def require_actor(db, actor, permission):
    """Recheck an actor id (or Principal) in the control database, never trust its grants."""
    if permission not in PERMISSIONS:
        raise registry.ControlError('Unknown permission', 403)
    actor_id = getattr(actor, 'id', actor)
    if not isinstance(actor_id, str):
        raise registry.ControlError('Management account required', 401)
    row = db.execute('SELECT u.id FROM cp_users u JOIN cp_grants g ON g.user_id=u.id '
                     'WHERE u.id=? AND u.enabled=1 AND g.permission=?', (actor_id, permission)).fetchone()
    if not row:
        raise registry.ControlError('Management permission denied', 403)
    return row['id']


def audit(db, actor, event, target, metadata=None):
    """Bounded metadata only: never passwords, tokens, configuration or source content."""
    metadata = metadata or {}
    allowed = {'component', 'revision', 'version', 'decision', 'hash', 'workspace',
               'path_sha256', 'sha256', 'kind', 'permission'}
    if (not isinstance(metadata, dict) or set(metadata) - allowed
            or any(type(v) not in (str, int, bool) for v in metadata.values())):
        raise registry.ControlError('Audit metadata not allowed')
    encoded = registry.canonical(metadata).decode('utf-8')
    if len(encoded) > 2048 or any(not isinstance(v, str) or len(v) > 128 for v in (actor, event, target)):
        raise registry.ControlError('Audit metadata too large')
    db.execute('INSERT INTO cp_audit(actor,event,target,metadata,created) VALUES(?,?,?,?,?)',
               (actor, event, target, encoded, int(time.time())))
    db.execute('DELETE FROM cp_audit WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM cp_audit)')


def _revision(row):
    result = dict(row)
    result['config'] = json.loads(result['config'])
    # Historical activated rows do not assert that they remain the active pointer.
    result.update(status=result['state'], policy_only=True, business_config_implemented=False)
    return result


def _expected(value):
    if type(value) is not int or not 1 <= value <= 10000:
        raise registry.ControlError('Invalid expected_version')


def _get(db, revision_id, expected_version):
    _expected(expected_version)
    if not isinstance(revision_id, str) or len(revision_id) != 32:
        raise registry.ControlError('Revision not found', 404)
    row = db.execute('SELECT * FROM cp_revisions WHERE id=?', (revision_id,)).fetchone()
    if row is None:
        raise registry.ControlError('Revision not found', 404)
    if row['version'] != expected_version:
        raise registry.ControlError('Revision version conflict', 409)
    return row


def create_revision(actor, component, config):
    value = registry.validate_config(component, config)
    with transaction() as db:
        owner = require_actor(db, actor, 'configure')
        if db.execute('SELECT count(*) FROM cp_revisions').fetchone()[0] >= MAX_REVISIONS:
            raise registry.ControlError('Revision capacity reached', 409)
        revision_id, now = uuid4().hex, int(time.time())
        db.execute('INSERT INTO cp_revisions VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (revision_id, component, owner, 1, 'draft', registry.canonical(value).decode('utf-8'),
                    registry.digest(value), None, now, now))
        audit(db, owner, 'policy.create', revision_id, {'component': component, 'version': 1})
        return _revision(db.execute('SELECT * FROM cp_revisions WHERE id=?', (revision_id,)).fetchone())


def edit_revision(actor, id, expected_version, config):
    with transaction() as db:
        actor_id = require_actor(db, actor, 'configure')
        row = _get(db, id, expected_version)
        if row['owner'] != actor_id or row['state'] != 'draft':
            raise registry.ControlError('Only the owner can edit a draft; reviewed content is immutable', 409)
        value = registry.validate_config(row['component'], config)
        db.execute('UPDATE cp_revisions SET config=?,hash=?,version=version+1,updated=? WHERE id=?',
                   (registry.canonical(value).decode('utf-8'), registry.digest(value), int(time.time()), id))
        audit(db, actor_id, 'policy.edit', id, {'version': expected_version + 1})
        return _revision(db.execute('SELECT * FROM cp_revisions WHERE id=?', (id,)).fetchone())


def _transition(actor, id, expected_version, permission, action, decision=None):
    with transaction() as db:
        actor_id = require_actor(db, actor, permission)
        row = _get(db, id, expected_version)
        reviewer = row['reviewer']
        if action == 'submit':
            if row['state'] != 'draft' or row['owner'] != actor_id:
                raise registry.ControlError('Only the owner can submit a draft', 409)
            state = 'submitted'
        elif action == 'review':
            if decision not in ('approve', 'reject'):
                raise registry.ControlError('Decision must be approve or reject')
            if row['state'] != 'submitted' or row['owner'] == actor_id:
                raise registry.ControlError('Review requires a different person and submitted revision', 409)
            state, reviewer = ('approved' if decision == 'approve' else 'rejected'), actor_id
        else:
            if row['state'] != 'approved' or not reviewer or reviewer == row['owner']:
                raise registry.ControlError('Only independently approved policies can activate', 409)
            # Revalidate trusted catalog and immutable content before activation.
            value = registry.validate_config(row['component'], json.loads(row['config']))
            if registry.digest(value) != row['hash']:
                raise registry.ControlError('Policy integrity failure', 409)
            state = 'activated'
            db.execute('INSERT OR REPLACE INTO cp_active(component,revision) VALUES(?,?)',
                       (row['component'], id))
        db.execute('UPDATE cp_revisions SET state=?,reviewer=?,version=version+1,updated=? WHERE id=?',
                   (state, reviewer, int(time.time()), id))
        audit(db, actor_id, 'policy.' + action, id,
              {'version': expected_version + 1, 'decision': state, 'hash': row['hash']})
        return _revision(db.execute('SELECT * FROM cp_revisions WHERE id=?', (id,)).fetchone())


def submit_revision(actor, id, expected_version):
    return _transition(actor, id, expected_version, 'configure', 'submit')


def review_revision(actor, id, expected_version, decision):
    return _transition(actor, id, expected_version, 'review', 'review', decision)


def activate_revision(actor, id, expected_version):
    return _transition(actor, id, expected_version, 'configure', 'activate')


def active_policy(component):
    """Return {enabled, request_timeout_s, hash}, or None. This is CONTROL POLICY only."""
    registry.component(component)
    with transaction() as db:
        row = db.execute('SELECT r.* FROM cp_active a JOIN cp_revisions r ON r.id=a.revision '
                         'WHERE a.component=?', (component,)).fetchone()
        if row is None:
            return None
        value = registry.validate_config(component, json.loads(row['config']))
        if (row['state'] != 'activated' or not row['reviewer'] or row['reviewer'] == row['owner']
                or registry.digest(value) != row['hash']):
            raise registry.ControlError('Active policy integrity failure', 503)
        value['hash'] = row['hash']
        return value


def list_revisions(component=None):
    if component is not None:
        registry.component(component)
    with transaction() as db:
        sql = 'SELECT * FROM cp_revisions'
        args = ()
        if component is not None:
            sql += ' WHERE component=?'
            args = (component,)
        return [_revision(row) for row in db.execute(sql + ' ORDER BY created DESC,id DESC LIMIT 512', args)]
