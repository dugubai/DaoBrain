"""Persisted release workflow, NOT a deployment engine (stdlib Python >=3.6).

Provision initialize() explicitly into CONTROL_RELEASE_DB_PATH, a new 0600 file
in a preexisting private directory separate from control/source/business/workspace
storage. No accounts, migrations, HTTP routes, shell, timer or executor here.
Existing persisted grants: develop creates, review independently reviews, operate
requests execution, view reads. Dedicated release grants await schema migration.

attest_version_cli(token, version_id, evidence) is TRUSTED LOCAL CODE ONLY, never
an HTTP endpoint. Evidence contains actual artifact_bytes, build_report_bytes,
test_report_bytes, signature. An independent review-granted account is required.
CONTROL_RELEASE_STAGING_ROOT/<version_id> must be locally approved staged source;
CONTROL_RELEASE_ATTESTATION_KEY_PATH is a preprovisioned private 32-byte HMAC key,
never available to workspace/browser authors. The trusted test/build supervisor
signs canonical {version_id, manifest_sha256, build_sha256, test_sha256}. Reports
must bind every manifest identity field (see _report_binding), actual artifacts,
and trusted tests/fixtures. A submitted hash or boolean cannot attest anything.
Source identity uses the workbench's canonical sorted path/sha256 list, not a label.
Only signed report digests are stored, never keys, tokens, source or report text.
Local attestation verifies a source ZIP, NOT an image or operational readiness.
Execution remains withheld even after approval/attestation: no enrolled release
executor, image proof, business-config adapter or authoritative operational
prechecks. Callback/URL/shell executors are deliberately rejected. Rollback retains
immutable code/config/image identities; production data restore is never performed.
"""
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import stat
import time
from uuid import uuid4
import zipfile

from control_plane import registry, security, store
from control_plane.sandbox_build_entry import gather, LIMIT, MAX_FILES

SCHEMA_VERSION = 1
MAX_VERSIONS = 512
MAX_PLANS = 256
_ID = re.compile(r'\A[0-9a-f]{32}\Z')
_HASH = re.compile(r'\A[0-9a-f]{64}\Z')
_LABEL = re.compile(r'\Ar?[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-[a-z0-9]{1,16})?\Z')
_IMAGE = re.compile(r'\Asha256:[0-9a-f]{64}\Z')
_STATES = ('draft', 'submitted', 'approved', 'expired', 'cancelled', 'blocked',
           'failed', 'succeeded', 'rolled_back')
_MANIFEST_FIELDS = {'component', 'runtime', 'label', 'source_sha256', 'artifact_sha256',
                    'config_revision', 'config_sha256', 'test_evidence_sha256',
                    'build_evidence_sha256', 'image_digest', 'schema_version', 'previous_version'}
_PRECHECK_FIELDS = {'health_sha256', 'capacity_sha256', 'baseline_sha256', 'backup_sha256'}
_SCHEMA = (
    'CREATE TABLE release_meta (version INTEGER NOT NULL)',
    'CREATE TABLE release_versions (id TEXT PRIMARY KEY, component TEXT NOT NULL, '
    'label TEXT NOT NULL, manifest TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, '
    'createdby TEXT NOT NULL, created INTEGER NOT NULL, UNIQUE(component,label))',
    'CREATE TABLE release_attestations (version_id TEXT PRIMARY KEY REFERENCES release_versions(id), '
    'actor TEXT NOT NULL, build_hash TEXT NOT NULL, test_hash TEXT NOT NULL, '
    'signature_hash TEXT NOT NULL, created INTEGER NOT NULL)',
    'CREATE TABLE release_plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, '
    'revision INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL, '
    'reviewer TEXT, approved_hash TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL, blocker TEXT)',
    'CREATE UNIQUE INDEX release_plan_duplicate ON release_plans(owner,hash)',
    'CREATE TABLE release_requests (id TEXT PRIMARY KEY, plan_id TEXT UNIQUE NOT NULL REFERENCES release_plans(id), '
    'actor TEXT NOT NULL, plan_hash TEXT NOT NULL, version_hash TEXT NOT NULL, '
    'state TEXT NOT NULL, blocker TEXT NOT NULL, created INTEGER NOT NULL)',
    'CREATE TABLE release_lease (slot INTEGER PRIMARY KEY CHECK(slot=1), '
    'request_id TEXT REFERENCES release_requests(id), expires INTEGER NOT NULL)',
    'CREATE TABLE release_audit (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, '
    'event TEXT NOT NULL, target TEXT NOT NULL, hash TEXT NOT NULL, created INTEGER NOT NULL)',
    "CREATE TRIGGER release_version_no_update BEFORE UPDATE ON release_versions "
    "BEGIN SELECT RAISE(ABORT,'immutable release version'); END",
    "CREATE TRIGGER release_version_no_delete BEFORE DELETE ON release_versions "
    "BEGIN SELECT RAISE(ABORT,'immutable release version'); END",
    "CREATE TRIGGER release_attestation_no_update BEFORE UPDATE ON release_attestations "
    "BEGIN SELECT RAISE(ABORT,'immutable release attestation'); END",
    "CREATE TRIGGER release_attestation_no_delete BEFORE DELETE ON release_attestations "
    "BEGIN SELECT RAISE(ABORT,'immutable release attestation'); END",
    "CREATE TRIGGER release_plan_immutable BEFORE UPDATE ON release_plans "
    "WHEN NEW.owner != OLD.owner OR (OLD.state != 'draft' AND "
    "(NEW.payload != OLD.payload OR NEW.hash != OLD.hash OR "
    "(OLD.reviewer IS NOT NULL AND NEW.reviewer IS NOT OLD.reviewer) OR "
    "(OLD.approved_hash IS NOT NULL AND NEW.approved_hash IS NOT OLD.approved_hash))) "
    "BEGIN SELECT RAISE(ABORT,'immutable reviewed plan'); END",
    "CREATE TRIGGER release_plan_no_delete BEFORE DELETE ON release_plans "
    "BEGIN SELECT RAISE(ABORT,'retained release plan'); END",
)
_TABLES = {'release_meta', 'release_versions', 'release_attestations', 'release_plans',
           'release_requests', 'release_lease', 'release_audit'}


def _match(value, regex, field, optional=False):
    if optional and value is None:
        return value
    if type(value) is not str or not regex.fullmatch(value):
        raise registry.ControlError('Invalid ' + field)
    return value


def _integer(value, field, minimum=1, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise registry.ControlError('Invalid ' + field)
    return value


def _encoded(value):
    return registry.canonical(value).decode('utf-8')


def _overlap(left, right):
    left, right = os.path.realpath(left), os.path.realpath(right)
    return store._inside(left, right) or store._inside(right, left)


def _database_path():
    path = os.environ.get('CONTROL_RELEASE_DB_PATH', '')
    if (not os.path.isabs(path) or os.path.normpath(path) != path or path.endswith(os.sep)):
        raise registry.ControlError('Explicit dedicated CONTROL_RELEASE_DB_PATH required', 503)
    parent = os.path.dirname(path)
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    forbidden = [project, os.path.dirname(store.database_path())]
    for key in ('CONTROL_SOURCE_ROOT', 'CONTROL_WORKSPACE_ROOT', 'APP_DATA_DIR', 'DATA_DIR',
                'CONTROL_RELEASE_STAGING_ROOT'):
        if os.environ.get(key):
            forbidden.append(os.path.abspath(os.environ[key]))
    for key in ('APP_DB_PATH', 'BUSINESS_DB_PATH', 'DATABASE_PATH'):
        if os.environ.get(key):
            forbidden.append(os.path.dirname(os.path.abspath(os.environ[key])))
    if any(_overlap(parent, other) for other in forbidden):
        raise registry.ControlError('Release storage must not overlap control/source/business/workspaces', 503)
    return path


@contextmanager
def _connection(initializing=False):
    path = _database_path()
    parent = None
    db = None
    try:
        parent = store.open_directory(os.path.dirname(path), private=True)
        name = os.path.basename(path)
        before = store._file_check(parent, name, required=True)
        for suffix in ('-journal', '-wal', '-shm'):
            store._file_check(parent, name + suffix)
        parent_info = os.fstat(parent)
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        after = store._file_check(parent, name, required=True)
        current = os.stat(os.path.dirname(path), follow_symlinks=False)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                (parent_info.st_dev, parent_info.st_ino) != (current.st_dev, current.st_ino)):
            raise registry.ControlError('Release storage changed during open', 503)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        if not initializing:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            versions = db.execute('SELECT version FROM release_meta').fetchall() if tables == _TABLES else []
            if (tables != _TABLES or len(versions) != 1 or versions[0][0] != SCHEMA_VERSION or
                    db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise registry.ControlError('Unsupported release schema; explicit provisioning required', 503)
            if _recover_lease(db):
                # Recovery survives a subsequently rejected/stale API request.
                # The caller's operation still starts in a fresh atomic txn.
                db.commit()
                db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except Exception as error:
        if db is not None:
            db.rollback()
        if isinstance(error, OSError):
            raise registry.ControlError('Unsafe or missing private release storage/evidence', 503)
        raise
    finally:
        if db is not None:
            db.close()
        if parent is not None:
            os.close(parent)


def initialize():
    """Trusted provisioning only; no automatic initialization or account grants."""
    path = _database_path()
    try:
        parent = store.open_directory(os.path.dirname(path), private=True)
        try:
            fd = os.open(os.path.basename(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            os.fsync(fd)
            os.close(fd)
            os.fsync(parent)
        finally:
            os.close(parent)
    except FileExistsError:
        raise registry.ControlError('Existing release database; initialization refused', 409)
    except OSError:
        raise registry.ControlError('Private preprovisioned release parent required', 503)
    with _connection(initializing=True) as db:
        if db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]:
            raise registry.ControlError('Nonempty release database', 409)
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute('INSERT INTO release_meta VALUES(?)', (SCHEMA_VERSION,))
        db.execute('INSERT INTO release_lease VALUES(1,NULL,0)')
        db.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        _audit(db, 'provision-cli', 'release.initialize', 'release', registry.digest({'schema': SCHEMA_VERSION}))


@contextmanager
def _authorized(token, permission):
    # Public contract calls authenticate, then rechecks under the persisted grants
    # lock held throughout release transaction. Revocation cannot race approval.
    security.authenticate(token, permission)
    with store.transaction() as control:
        actor = security._authenticate(control, token, permission)
        with _connection() as db:
            yield db, control, actor


def _audit(db, actor, event, target, digest):
    db.execute('INSERT INTO release_audit(actor,event,target,hash,created) VALUES(?,?,?,?,?)',
               (actor, event, target, digest, int(time.time())))
    db.execute('DELETE FROM release_audit WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM release_audit)')


def _recover_lease(db):
    lease = db.execute('SELECT * FROM release_lease WHERE slot=1').fetchone()
    if lease is None:
        raise registry.ControlError('Missing durable release lease', 503)
    if lease['request_id'] is None:
        return
    request = db.execute('SELECT * FROM release_requests WHERE id=?', (lease['request_id'],)).fetchone()
    if request is None:
        raise registry.ControlError('Release lease integrity failure', 503)
    # No release executor exists in this module. Any surviving active lease is
    # interrupted, even before its deadline. NEVER steal an expired lease.
    if request['state'] != 'interrupted':
        db.execute("UPDATE release_requests SET state='interrupted',blocker='pending-reconciliation' WHERE id=?",
                   (request['id'],))
        db.execute("UPDATE release_plans SET state='blocked',blocker='pending-reconciliation',"
                   'revision=revision+1,updated=? WHERE id=?', (int(time.time()), request['plan_id']))
        _audit(db, 'recovery-cli', 'release.interrupted', request['plan_id'], request['plan_hash'])
        return True
    return False


def _config(control, component, revision, digest):
    row = control.execute('SELECT * FROM cp_revisions WHERE id=?', (revision,)).fetchone()
    if (row is None or row['component'] != component or row['state'] not in ('approved', 'activated')
            or not row['reviewer'] or row['owner'] == row['reviewer']):
        raise registry.ControlError('Independent immutable config revision required', 409)
    value = registry.validate_config(component, json.loads(row['config']))
    if row['hash'] != digest or registry.digest(value) != digest:
        raise registry.ControlError('Config binding integrity failure', 409)


def _manifest(value, control):
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise registry.ControlError('Exact release manifest fields required')
    item = registry.component(value['component'])
    if (not item['editable'] or not item['runtime'] or item['directory'] in registry.PROTECTED_DIRS
            or value['runtime'] != item['runtime']):
        raise registry.ControlError('Only registered development scope and exact shared runtime allowed', 403)
    _match(value['label'], _LABEL, 'version label')
    for field in ('source_sha256', 'artifact_sha256', 'config_sha256', 'test_evidence_sha256', 'build_evidence_sha256'):
        _match(value[field], _HASH, field)
    _match(value['config_revision'], _ID, 'config_revision')
    _match(value['previous_version'], _ID, 'previous_version', True)
    _match(value['image_digest'], _IMAGE, 'image_digest', True)
    _integer(value['schema_version'], 'schema_version', maximum=1000000)
    _config(control, value['component'], value['config_revision'], value['config_sha256'])
    return dict(value)


def _version_row(db, version_id):
    _match(version_id, _ID, 'version_id')
    row = db.execute('SELECT * FROM release_versions WHERE id=?', (version_id,)).fetchone()
    if row is None:
        raise registry.ControlError('Release version not found', 404)
    value = json.loads(row['manifest'])
    if registry.digest(value) != row['hash'] or value['component'] != row['component'] or value['label'] != row['label']:
        raise registry.ControlError('Release manifest integrity failure', 409)
    return row, value


def _version(db, row):
    _, value = _version_row(db, row['id'])
    attested = db.execute('SELECT 1 FROM release_attestations WHERE version_id=?', (row['id'],)).fetchone() is not None
    result = dict(row)
    result['manifest'] = value
    result.update(verification_state='local-artifact-verified' if attested else 'pending',
                  image_verified=False, execution_ready=False, deployed=False,
                  status='attested-not-deployed' if attested else 'registered-unverified',
                  blocker='release-executor-not-enrolled' if attested else 'trusted-local-attestation-required',
                  config_binding_kind='control-policy-only', business_config_verified=False)
    result['blockers'] = ['release-executor-not-enrolled', 'independent-image-evidence-required',
                          'business-config-adapter-required']
    if not attested:
        result['blockers'].insert(0, 'trusted-local-attestation-required')
    return result


def register_version(token, manifest):
    """Develop-granted raw token; immutable identity, never caller-verified."""
    with _authorized(token, 'develop') as (db, control, actor):
        value = _manifest(manifest, control)
        if value['previous_version'] is not None:
            _, previous = _version_row(db, value['previous_version'])
            if (previous['component'], previous['runtime']) != (value['component'], value['runtime']):
                raise registry.ControlError('Previous version belongs to another component/runtime', 409)
        if db.execute('SELECT count(*) FROM release_versions').fetchone()[0] >= MAX_VERSIONS:
            raise registry.ControlError('Release version capacity reached', 409)
        version_id, digest = uuid4().hex, registry.digest(value)
        try:
            db.execute('INSERT INTO release_versions VALUES(?,?,?,?,?,?,?)',
                       (version_id, value['component'], value['label'], _encoded(value), digest, actor.id, int(time.time())))
        except sqlite3.IntegrityError:
            raise registry.ControlError('Duplicate immutable version/manifest', 409)
        _audit(db, actor.id, 'release.register', version_id, digest)
        return _version(db, _version_row(db, version_id)[0])


def get_version(token, version_id):
    with _authorized(token, 'view') as (db, _control, _actor):
        return _version(db, _version_row(db, version_id)[0])


def list_versions(token):
    with _authorized(token, 'view') as (db, _control, _actor):
        return [_version(db, r) for r in db.execute('SELECT * FROM release_versions ORDER BY created,id LIMIT 512')]


def _private_bytes(path, maximum):
    parent = store.open_directory(os.path.dirname(path), private=True)
    fd = None
    try:
        name = os.path.basename(path)
        before = store._file_check(parent, name, required=True)
        if before.st_size > maximum:
            raise registry.ControlError('Trusted local evidence size exceeded', 413)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise registry.ControlError('Trusted local evidence changed', 409)
        data = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        if (len(data) > maximum or len(data) != opened.st_size or
                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise registry.ControlError('Trusted local evidence changed', 409)
        return data
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _strict_report(data, maximum=16384):
    if type(data) is not bytes or not 1 <= len(data) <= maximum:
        raise registry.ControlError('Actual bounded trusted report bytes required')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('constant')))
    except (ValueError, UnicodeError, RecursionError):
        raise registry.ControlError('Invalid trusted report')
    if type(result) is not dict:
        raise registry.ControlError('Exact trusted report object required')
    return result


def _report_binding(version_id, row, value):
    return {'version_id': version_id, 'manifest_sha256': row['hash'],
            'source_sha256': value['source_sha256'], 'artifact_sha256': value['artifact_sha256'],
            'config_revision': value['config_revision'], 'config_sha256': value['config_sha256'],
            'schema_version': value['schema_version'], 'image_digest': value['image_digest'],
            'component': value['component'], 'runtime': value['runtime']}


def attest_version_cli(token, version_id, evidence):
    """Local-only independent attestation; never expose this primitive through HTTP.

    signature is HMAC-SHA256 over the canonical identity/report-digest object
    described above, signed by the trusted local supervisor, not by the author.
    Hashes of reports are bound at registration; reports bind manifest identity
    excluding report digests to avoid circular hashing (binding uses payload hash
    with the two evidence digest fields omitted).
    """
    with _authorized(token, 'review') as (db, control, actor):
        row, value = _version_row(db, version_id)
        if actor.id == row['createdby']:
            raise registry.ControlError('Independent local attestor required', 403)
        _manifest(value, control)
        if type(evidence) is not dict or set(evidence) != {'artifact_bytes', 'build_report_bytes', 'test_report_bytes', 'signature'}:
            raise registry.ControlError('Exact trusted local evidence required')
        if db.execute('SELECT 1 FROM release_attestations WHERE version_id=?', (version_id,)).fetchone():
            raise registry.ControlError('Version already attested immutably', 409)
        artifact = evidence['artifact_bytes']
        if type(artifact) is not bytes or not 1 <= len(artifact) <= LIMIT + 1024 * 1024:
            raise registry.ControlError('Actual bounded artifact bytes required')
        if hashlib.sha256(artifact).hexdigest() != value['artifact_sha256']:
            raise registry.ControlError('Artifact digest mismatch', 409)
        reports = [_strict_report(evidence[k]) for k in ('build_report_bytes', 'test_report_bytes')]
        for key, field in (('build_report_bytes', 'build_evidence_sha256'), ('test_report_bytes', 'test_evidence_sha256')):
            if hashlib.sha256(evidence[key]).hexdigest() != value[field]:
                raise registry.ControlError('Evidence digest mismatch', 409)
        binding = _report_binding(version_id, row, value)
        # Reports precede server-generated version_id. Stable content identity is
        # source/artifact/config/schema/runtime plus the digest of that payload.
        binding.pop('version_id')
        binding['manifest_sha256'] = registry.digest({k: v for k, v in value.items()
                                                    if k not in ('build_evidence_sha256', 'test_evidence_sha256')})
        build_extra = {'kind': 'offline-source-artifact', 'syntax_checked': True, 'image_built': False}
        test_fields = {'kind', 'passed', 'tests_run', 'test_entry_sha256', 'fixture_sha256'}
        if (reports[0] != dict(binding, **build_extra) or
                any(type(reports[0][k]) is not type(v) for k, v in dict(binding, **build_extra).items())):
            raise registry.ControlError('Build report identity/semantics mismatch', 409)
        test = reports[1]
        if set(test) != set(binding) | test_fields or any(test[k] != v or type(test[k]) is not type(v) for k, v in binding.items()):
            raise registry.ControlError('Test report binding mismatch', 409)
        if test['kind'] != 'trusted-tests' or test['passed'] is not True:
            raise registry.ControlError('Passing authoritative tests required', 409)
        _integer(test['tests_run'], 'tests_run', maximum=100000)
        for field in ('test_entry_sha256', 'fixture_sha256'):
            _match(test[field], _HASH, field)
        signature = _match(evidence['signature'], _HASH, 'signature')
        key_path = os.environ.get('CONTROL_RELEASE_ATTESTATION_KEY_PATH', '')
        staging = os.environ.get('CONTROL_RELEASE_STAGING_ROOT', '')
        for path in (key_path, staging):
            if not os.path.isabs(path) or os.path.normpath(path) != path:
                raise registry.ControlError('Explicit trusted local attestation provisioning required', 503)
        # Trust material cannot be sourced from candidate workspaces or source.
        for env in ('CONTROL_SOURCE_ROOT', 'CONTROL_WORKSPACE_ROOT', 'APP_DATA_DIR', 'DATA_DIR'):
            if os.environ.get(env) and (_overlap(staging, os.environ[env]) or
                                        store._inside(os.path.realpath(key_path), os.path.realpath(os.environ[env]))):
                raise registry.ControlError('Unsafe attestation trust material location', 503)
        key = _private_bytes(key_path, 32)
        if len(key) != 32:
            raise registry.ControlError('Preprovisioned 32-byte supervisor key required', 503)
        signed = {'version_id': version_id, 'manifest_sha256': row['hash'],
                  'build_sha256': value['build_evidence_sha256'], 'test_sha256': value['test_evidence_sha256']}
        if not hmac.compare_digest(signature, hmac.new(key, registry.canonical(signed), hashlib.sha256).hexdigest()):
            raise registry.ControlError('Independent supervisor signature mismatch', 409)
        stage_fd = store.open_directory(staging, private=True)
        os.close(stage_fd)
        source_root = os.path.join(staging, version_id)
        source_fd = store.open_directory(source_root, private=True)
        os.close(source_fd)
        entries = gather(source_root)
        source_manifest = {p: hashlib.sha256(b).hexdigest() for p, b in sorted(entries.items())}
        if registry.digest([{'path': p, 'sha256': d} for p, d in sorted(source_manifest.items())]) != value['source_sha256']:
            raise registry.ControlError('Staged approved source identity mismatch', 409)
        item = registry.component(value['component'])
        if not any(p.startswith(item['directory'] + '/') for p in entries):
            raise registry.ControlError('Staged source lacks registered component', 409)
        try:
            with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
                names = archive.namelist()
                if len(names) > MAX_FILES + 1 or len(names) != len(set(names)) or set(names) != set(entries) | {'BUILD-MANIFEST.json'}:
                    raise ValueError('inventory')
                if any(i.file_size > LIMIT or i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 1 for i in archive.infolist()):
                    raise ValueError('archive')
                metadata = _strict_report(archive.read('BUILD-MANIFEST.json'), maximum=256 * 1024)
                expected = {'kind': 'offline-source-artifact', 'source_manifest': source_manifest,
                            'syntax_checked': True, 'image_built': False, 'release_approved': False}
                if (metadata != expected or metadata['syntax_checked'] is not True or
                        metadata['image_built'] is not False or metadata['release_approved'] is not False):
                    raise ValueError('metadata')
                for path, data in entries.items():
                    if archive.read(path) != data:
                        raise ValueError('bytes')
        except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
            raise registry.ControlError('Artifact does not exactly match staged approved source', 409)
        db.execute('INSERT INTO release_attestations VALUES(?,?,?,?,?,?)',
                   (version_id, actor.id, value['build_evidence_sha256'], value['test_evidence_sha256'],
                    hashlib.sha256(signature.encode('ascii')).hexdigest(), int(time.time())))
        _audit(db, actor.id, 'release.attest-local', version_id, row['hash'])
        return _version(db, row)


def _window(window):
    if type(window) is not dict or set(window) != {'planned_at', 'expires_at'}:
        raise registry.ControlError('Exact UTC epoch execution window required')
    start = _integer(window['planned_at'], 'planned_at')
    end = _integer(window['expires_at'], 'expires_at')
    now = int(time.time())
    if end <= now or end <= start or end - start > 7 * 86400 or start > now + 90 * 86400:
        raise registry.ControlError('Invalid or expired execution window')
    return start, end


def _payload(db, control, value):
    fields = {'version_id', 'planned_at', 'expires_at', 'dependencies', 'prechecks', 'strategy'}
    if type(value) is not dict or set(value) != fields:
        raise registry.ControlError('Exact plan fields required; owner is the authenticated actor')
    row, manifest = _version_row(db, value['version_id'])
    _manifest(manifest, control)
    start, end = _window({k: value[k] for k in ('planned_at', 'expires_at')})
    if value['strategy'] != 'serial-app-slot':
        raise registry.ControlError('Only serial-app-slot; bluegreen/modelrollout unsupported', 409)
    dependencies = value['dependencies']
    if type(dependencies) is not list or len(dependencies) > 32 or len(set(_match(v, _ID, 'dependency') for v in dependencies)) != len(dependencies):
        raise registry.ControlError('Bounded unique version dependencies required')
    bound = []
    for dep in sorted(dependencies):
        if dep == row['id']:
            raise registry.ControlError('Self dependency forbidden')
        dep_row, dep_manifest = _version_row(db, dep)
        _manifest(dep_manifest, control)
        bound.append({'version_id': dep, 'manifest_sha256': dep_row['hash']})
    checks = value['prechecks']
    if type(checks) is not dict or set(checks) != _PRECHECK_FIELDS:
        raise registry.ControlError('Exact health/capacity/baseline/backup evidence digest fields required')
    for field in _PRECHECK_FIELDS:
        _match(checks[field], _HASH, field, True)
    return {'kind': 'release', 'version_id': row['id'], 'version_sha256': row['hash'],
            'component': manifest['component'], 'runtime': manifest['runtime'],
            'config_revision': manifest['config_revision'], 'config_sha256': manifest['config_sha256'],
            'image_digest': manifest['image_digest'], 'schema_version': manifest['schema_version'],
            'planned_at': start, 'expires_at': end, 'dependencies': bound, 'prechecks': dict(checks),
            'strategy': 'serial-app-slot'}


_UNSET = object()


def _plan_row(db, plan_id, expected=_UNSET):
    _match(plan_id, _ID, 'plan_id')
    if expected is not _UNSET:
        _integer(expected, 'expected_version', maximum=10000)
    row = db.execute('SELECT * FROM release_plans WHERE id=?', (plan_id,)).fetchone()
    if row is None:
        raise registry.ControlError('Release plan not found', 404)
    if expected is not _UNSET and row['revision'] != expected:
        raise registry.ControlError('Plan revision conflict', 409)
    payload = json.loads(row['payload'])
    if registry.digest(payload) != row['hash'] or row['state'] not in _STATES:
        raise registry.ControlError('Plan integrity failure', 409)
    if row['approved_hash'] is not None and row['approved_hash'] != row['hash']:
        raise registry.ControlError('Approved immutable plan hash mismatch', 409)
    return row, payload


def _plan(db, row):
    _, payload = _plan_row(db, row['id'])
    state = row['state']
    if state in ('draft', 'submitted', 'approved') and payload['expires_at'] <= int(time.time()):
        state = 'expired'
        db.execute("UPDATE release_plans SET state='expired',revision=revision+1,updated=?,blocker='window-expired' WHERE id=?",
                   (int(time.time()), row['id']))
        _audit(db, 'workflow-clock', 'release.plan-expired', row['id'], row['hash'])
        row = db.execute('SELECT * FROM release_plans WHERE id=?', (row['id'],)).fetchone()
    result = dict(row)
    result['payload'] = payload
    result['version'] = row['revision']
    result.update(status=state, execution_ready=False, deployed=False, scheduling_supported=False,
                  manualexecution=True, prechecks_verified=False, data_rollback_supported=False,
                  business_config_verified=False, bluegreen_supported=False, modelrollout_supported=False)
    version = _version(db, _version_row(db, payload['version_id'])[0])
    result['verification_state'] = version['verification_state']
    result['image_verified'] = False
    result['blockers'] = version['blockers'] + ['authoritative-health-capacity-baseline-backup-required']
    if not row['approved_hash']:
        result['blockers'].append('independent-plan-approval-required')
    if payload['kind'] == 'rollback':
        result['blockers'].append('current-runtime-baseline-unverified')
    if state == 'expired':
        result['blockers'].insert(0, 'window-expired')
    if result['blocker'] and result['blocker'] not in result['blockers']:
        result['blockers'].insert(0, result['blocker'])
    if not result['blocker']:
        result['blocker'] = result['blockers'][0]
    return result


def _insert_plan(db, actor, payload):
    if db.execute('SELECT count(*) FROM release_plans').fetchone()[0] >= MAX_PLANS:
        raise registry.ControlError('Release plan capacity reached', 409)
    plan_id, now, digest = uuid4().hex, int(time.time()), registry.digest(payload)
    try:
        db.execute('INSERT INTO release_plans VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (plan_id, actor.id, 1, 'draft', _encoded(payload), digest, None, None, now, now, None))
    except sqlite3.IntegrityError:
        raise registry.ControlError('Duplicate release plan', 409)
    _audit(db, actor.id, 'release.plan-create', plan_id, digest)
    return _plan(db, _plan_row(db, plan_id)[0])


def create_plan(token, plan):
    with _authorized(token, 'develop') as (db, control, actor):
        return _insert_plan(db, actor, _payload(db, control, plan))


def edit_plan(token, plan_id, expected_version, plan):
    with _authorized(token, 'develop') as (db, control, actor):
        row, old = _plan_row(db, plan_id, expected_version)
        if row['owner'] != actor.id or row['state'] != 'draft' or old['kind'] != 'release':
            raise registry.ControlError('Only owner may edit an unsubmitted release draft', 409)
        value = _payload(db, control, plan)
        try:
            db.execute('UPDATE release_plans SET payload=?,hash=?,revision=revision+1,updated=? WHERE id=?',
                       (_encoded(value), registry.digest(value), int(time.time()), plan_id))
        except sqlite3.IntegrityError:
            raise registry.ControlError('Duplicate release plan', 409)
        _audit(db, actor.id, 'release.plan-edit', plan_id, registry.digest(value))
        return _plan(db, _plan_row(db, plan_id)[0])


def _recheck(db, control, payload):
    row, manifest = _version_row(db, payload['version_id'])
    _manifest(manifest, control)
    if (row['hash'] != payload['version_sha256'] or manifest['config_revision'] != payload['config_revision']
            or manifest['config_sha256'] != payload['config_sha256'] or manifest['runtime'] != payload['runtime']):
        raise registry.ControlError('Plan version/config/runtime binding mismatch', 409)
    for dep in payload['dependencies']:
        dep_row, dep_manifest = _version_row(db, dep['version_id'])
        _manifest(dep_manifest, control)
        if dep_row['hash'] != dep['manifest_sha256']:
            raise registry.ControlError('Dependency immutable hash mismatch', 409)
    if payload['kind'] == 'rollback':
        current_row, current = _version_row(db, payload['current_version_id'])
        _manifest(current, control)
        if (current_row['hash'] != payload['current_version_sha256'] or
                current['schema_version'] != manifest['schema_version']):
            raise registry.ControlError('Rollback current/schema binding mismatch', 409)


def submit_plan(token, plan_id, expected_version):
    with _authorized(token, 'develop') as (db, control, actor):
        row, value = _plan_row(db, plan_id, expected_version)
        if row['owner'] != actor.id or row['state'] != 'draft':
            raise registry.ControlError('Only owner can submit a draft', 409)
        _window({k: value[k] for k in ('planned_at', 'expires_at')})
        _recheck(db, control, value)
        db.execute("UPDATE release_plans SET state='submitted',revision=revision+1,updated=? WHERE id=?",
                   (int(time.time()), plan_id))
        _audit(db, actor.id, 'release.plan-submit', plan_id, row['hash'])
        return _plan(db, _plan_row(db, plan_id)[0])


def review_plan(token, plan_id, expected_version, decision):
    with _authorized(token, 'review') as (db, control, actor):
        row, value = _plan_row(db, plan_id, expected_version)
        version_row, _ = _version_row(db, value['version_id'])
        if row['state'] != 'submitted' or actor.id in (row['owner'], version_row['createdby']):
            raise registry.ControlError('Independent plan AND version reviewer required', 403)
        if decision not in ('approve', 'reject'):
            raise registry.ControlError('Decision must be approve or reject')
        _window({k: value[k] for k in ('planned_at', 'expires_at')})
        _recheck(db, control, value)
        state = 'approved' if decision == 'approve' else 'cancelled'
        db.execute('UPDATE release_plans SET state=?,reviewer=?,approved_hash=?,revision=revision+1,updated=? WHERE id=?',
                   (state, actor.id, row['hash'] if state == 'approved' else None, int(time.time()), plan_id))
        _audit(db, actor.id, 'release.plan-' + decision, plan_id, row['hash'])
        return _plan(db, _plan_row(db, plan_id)[0])


def cancel_plan(token, plan_id, expected_version):
    with _authorized(token, 'develop') as (db, _control, actor):
        row, _ = _plan_row(db, plan_id, expected_version)
        if row['owner'] != actor.id or row['state'] not in ('draft', 'submitted', 'approved'):
            raise registry.ControlError('Only owner can cancel a nonexecuting plan', 409)
        db.execute("UPDATE release_plans SET state='cancelled',revision=revision+1,updated=? WHERE id=?",
                   (int(time.time()), plan_id))
        _audit(db, actor.id, 'release.plan-cancel', plan_id, row['hash'])
        return _plan(db, _plan_row(db, plan_id)[0])


def get_plan(token, plan_id):
    with _authorized(token, 'view') as (db, _control, _actor):
        return _plan(db, _plan_row(db, plan_id)[0])


def list_plans(token):
    with _authorized(token, 'view') as (db, _control, _actor):
        return [_plan(db, r) for r in db.execute('SELECT * FROM release_plans ORDER BY created,id LIMIT 256').fetchall()]


def request_execution(token, plan_id, expected_version, executor=None):
    """Operate approves a request, NOT deployment. Persist blocked request then 503.

    executor reserved for future trusted enrolled code, never an HTTP argument.
    No callbacks are currently supported. No success/deployed mutation is public.
    """
    with _authorized(token, 'operate') as (db, control, actor):
        row, value = _plan_row(db, plan_id, expected_version)
        version_row, manifest = _version_row(db, value['version_id'])
        if db.execute('SELECT 1 FROM release_requests WHERE plan_id=?', (plan_id,)).fetchone():
            raise registry.ControlError('Duplicate execution request', 409)
        if (row['state'] != 'approved' or not row['reviewer'] or
                row['reviewer'] in (row['owner'], version_row['createdby']) or
                actor.id in (row['owner'], row['reviewer'], version_row['createdby']) or row['approved_hash'] != row['hash']):
            raise registry.ControlError('Independent operator and immutable approved plan required', 403)
        if executor is not None:
            raise registry.ControlError('Release executor not enrolled; caller callbacks forbidden', 503)
        store.require_actor(control, row['owner'], 'develop')
        store.require_actor(control, version_row['createdby'], 'develop')
        store.require_actor(control, row['reviewer'], 'review')
        _recheck(db, control, value)
        now = int(time.time())
        if now < value['planned_at']:
            raise registry.ControlError('Manual execution window not open', 409)
        if now >= value['expires_at']:
            raise registry.ControlError('Execution window expired', 409)
        lease = db.execute('SELECT request_id FROM release_lease WHERE slot=1').fetchone()
        if lease['request_id'] is not None:
            raise registry.ControlError('Single-flight lease requires explicit reconciliation', 409)
        attested = db.execute('SELECT 1 FROM release_attestations WHERE version_id=?', (version_row['id'],)).fetchone()
        blocker = 'release-executor-not-enrolled' if attested else 'trusted-local-attestation-required'
        request_id = uuid4().hex
        db.execute('INSERT INTO release_requests VALUES(?,?,?,?,?,?,?,?)',
                   (request_id, plan_id, actor.id, row['hash'], version_row['hash'], 'blocked', blocker, now))
        # Atomic durable slot; there is no external side effect to lose on crash.
        changed = db.execute('UPDATE release_lease SET request_id=?,expires=? WHERE slot=1 AND request_id IS NULL',
                             (request_id, now + 120)).rowcount
        if changed != 1:
            raise registry.ControlError('Global release slot busy', 409)
        db.execute("UPDATE release_plans SET state='blocked',blocker=?,revision=revision+1,updated=? WHERE id=?",
                   (blocker, now, plan_id))
        _audit(db, actor.id, 'release.execution-blocked', plan_id, row['hash'])
        db.execute('UPDATE release_lease SET request_id=NULL,expires=0 WHERE slot=1 AND request_id=?', (request_id,))
    error = registry.ControlError('Execution withheld: ' + blocker, 503)
    error.request_id = request_id
    raise error


def list_execution_requests(token):
    with _authorized(token, 'view') as (db, _control, _actor):
        return [dict(r, execution_ready=False, deployed=False) for r in
                db.execute('SELECT * FROM release_requests ORDER BY created,id LIMIT 256')]


def list_audit(token):
    with _authorized(token, 'view') as (db, _control, _actor):
        return [dict(r) for r in db.execute('SELECT * FROM release_audit ORDER BY id DESC LIMIT 512')]


def _accepted(db, version_id):
    # Accepted means independently reviewed AND locally attested, never deployed.
    row, _ = _version_row(db, version_id)
    if not db.execute('SELECT 1 FROM release_attestations WHERE version_id=?', (version_id,)).fetchone():
        return False
    for plan in db.execute('SELECT * FROM release_plans WHERE approved_hash IS NOT NULL'):
        _, value = _plan_row(db, plan['id'])
        if (value['version_id'] == version_id and plan['reviewer'] and
                plan['reviewer'] not in (plan['owner'], row['createdby'])):
            return True
    return False


def create_rollback_plan(token, version_id, current_version_id, window):
    """Rollback specific accepted ancestor, retaining exact code/config/image.

    No git checkout, database restore, data rollback, scheduler or host command.
    current_version_id is a declared baseline, NOT an assertion of runtime state.
    """
    with _authorized(token, 'develop') as (db, control, actor):
        target_row, target = _version_row(db, version_id)
        current_row, current = _version_row(db, current_version_id)
        if version_id == current_version_id:
            raise registry.ControlError('Rollback requires a distinct previous version', 409)
        if (target['component'], target['runtime'], target['schema_version']) != (current['component'], current['runtime'], current['schema_version']):
            raise registry.ControlError('Rollback component/runtime/schema incompatible; data rollback needs separate manual approval', 409)
        ancestor = current['previous_version']
        seen = set()
        while ancestor is not None and ancestor != version_id:
            if ancestor in seen or len(seen) >= MAX_VERSIONS:
                raise registry.ControlError('Invalid previous-version lineage', 409)
            seen.add(ancestor)
            _, parent = _version_row(db, ancestor)
            ancestor = parent['previous_version']
        if ancestor != version_id or not _accepted(db, version_id) or not _accepted(db, current_version_id):
            raise registry.ControlError('Rollback target/current must be specific independently accepted versions in lineage', 409)
        if target['image_digest'] is None or current['image_digest'] is None:
            raise registry.ControlError('Rollback requires retained immutable image manifests', 409)
        start, end = _window(window)
        base = {'version_id': version_id, 'planned_at': start, 'expires_at': end, 'dependencies': [],
                'prechecks': {k: None for k in _PRECHECK_FIELDS}, 'strategy': 'serial-app-slot'}
        value = _payload(db, control, base)
        _manifest(current, control)
        value.update(kind='rollback', current_version_id=current_version_id,
                     current_version_sha256=current_row['hash'], current_baseline_verified=False,
                     preserve_business_records=True, data_rollback=False)
        return _insert_plan(db, actor, value)
