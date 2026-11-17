"""Private descriptor-based development copies; never publish or import workspace code.

snapshot(id, actor_id, expected_hash) -> {id, owner, source_hash, root, files,
snapshot_id, hash, publish_enabled:false}. root is a frozen copy, NOT live source.
Controller must call this after authenticating the raw token with 'develop'; it
must run authoritative tests/fixtures/control entrypoints from trusted source,
never from root. Files marked noneditable are trusted import baselines only.
"""
from contextlib import contextmanager
import difflib
import fcntl
import hashlib
import json
import os
import re
import stat
from uuid import uuid4

from control_plane import registry, store

MAX_WORKSPACES = 32
MAX_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 24 * 1024
MAX_FILES = 512
MAX_DIRS = 256
MAX_VERSIONS = 32
_ID = re.compile(r'\A[0-9a-f]{32}\Z')
_HASH = re.compile(r'\A[0-9a-f]{64}\Z')
_IMPORT_DIRS = frozenset(registry.EDITABLE_DIRS | {'common', 'application_hub', 'portable_frontend'})


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _paths():
    source = os.environ.get('CONTROL_SOURCE_ROOT', '')
    root = os.environ.get('CONTROL_WORKSPACE_ROOT', '')
    for path in (source, root):
        if not path or not os.path.isabs(path) or os.path.normpath(path) != path:
            raise registry.ControlError('Explicit absolute preprovisioned source/workspace roots required', 503)
    control = os.path.dirname(store.database_path())
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    others = [source, control, project]
    for key in ('APP_DATA_DIR', 'DATA_DIR'):
        if os.environ.get(key):
            others.append(os.path.abspath(os.environ[key]))
    for key in ('APP_DB_PATH', 'BUSINESS_DB_PATH', 'DATABASE_PATH'):
        if os.environ.get(key):
            others.append(os.path.dirname(os.path.abspath(os.environ[key])))
    if any(store._inside(root, other) or store._inside(other, root) for other in others):
        raise registry.ControlError('Workspace parent must be exclusive and separate from source/control/data', 503)
    if store._inside(source, control) or store._inside(control, source):
        raise registry.ControlError('Source must be separate from management storage', 503)
    return source, root


def _same(a, b):
    return (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns) == (
        b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns)


def _entry(parent, name):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
        raise registry.ControlError('Symlinks and hardlinks are forbidden', 409)
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise registry.ControlError('Only regular source files/directories are allowed', 409)
    return info


def _directory(parent, name):
    before = _entry(parent, name)
    if not stat.S_ISDIR(before.st_mode):
        raise registry.ControlError('Directory required', 409)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    if (before.st_dev, before.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
        os.close(fd)
        raise registry.ControlError('Directory changed during open', 409)
    return fd


def _read(parent, name, limit=MAX_FILE_BYTES):
    before = _entry(parent, name)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise registry.ControlError('Invalid file or file size limit exceeded', 413)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if not _same(before, opened):
            raise registry.ControlError('File changed during open', 409)
        chunks, length = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > limit:
                raise registry.ControlError('File size limit exceeded', 413)
        if not _same(opened, os.fstat(fd)) or not _same(opened, _entry(parent, name)):
            raise registry.ControlError('File changed during read', 409)
        return b''.join(chunks)
    finally:
        os.close(fd)


def _excluded(name):
    lower = name.lower()
    return (name.startswith('.') or lower.startswith(('env', 'secret', 'credential', 'test_', 'fixture'))
            or lower.endswith(('_test.py', '_fixtures.py')) or lower in ('tests', 'logs', 'data', 'db', '__pycache__')
            or lower.endswith(('.db', '.sqlite', '.sqlite3', '.log', '.pem', '.key')))


def _inventory(fd, source=False):
    """Bounded walk entirely via descriptors; bytes returned, never executable imports."""
    result, stats = {}, [0, 0, 0]

    def walk(directory, prefix, depth):
        if depth > 12:
            raise registry.ControlError('Directory depth exceeded', 413)
        initial = os.fstat(directory)
        names = sorted(os.listdir(directory))
        for name in names:
            info = _entry(directory, name)
            if source:
                # The public fixture baseline is safe only in protected common.
                fixture_baseline = prefix == 'common/' and name == 'corpus_fixtures.py'
                if _excluded(name) and not fixture_baseline:
                    continue
                if not prefix and name not in _IMPORT_DIRS:
                    continue
            path = prefix + name
            if stat.S_ISDIR(info.st_mode):
                stats[2] += 1
                if stats[2] > MAX_DIRS:
                    raise registry.ControlError('Directory count exceeded', 413)
                child = _directory(directory, name)
                try:
                    walk(child, path + '/', depth + 1)
                    if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (
                            _entry(directory, name).st_dev, _entry(directory, name).st_ino):
                        raise registry.ControlError('Directory changed during walk', 409)
                finally:
                    os.close(child)
            elif not source or name.endswith(('.py', '.html')):
                _path(path)
                data = _read(directory, name)
                try:
                    data.decode('utf-8')
                except UnicodeError:
                    raise registry.ControlError('Source must be UTF-8 text', 400)
                stats[0] += len(data)
                stats[1] += 1
                if stats[0] > MAX_BYTES or stats[1] > MAX_FILES:
                    raise registry.ControlError('Workspace content limit exceeded', 413)
                result[path] = data
        final = os.fstat(directory)
        if not _same(initial, final):
            raise registry.ControlError('Directory changed during walk', 409)
    walk(fd, '', 0)
    return result


def _usage(fd):
    totals = [0, 0]

    def walk(directory, depth):
        if depth > 16:
            raise registry.ControlError('Workspace storage depth exceeded', 413)
        for name in os.listdir(directory):
            info = _entry(directory, name)
            totals[1] += 1
            if totals[1] > 4096:
                raise registry.ControlError('Workspace storage entry limit exceeded', 413)
            if stat.S_ISDIR(info.st_mode):
                child = _directory(directory, name)
                try:
                    walk(child, depth + 1)
                finally:
                    os.close(child)
            else:
                totals[0] += info.st_size
                if totals[0] > MAX_BYTES:
                    raise registry.ControlError('Workspace total storage limit exceeded', 413)
    walk(fd, 0)
    return totals[0]


def _new_file(parent, name, data, mode=0o600):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _parent(fd, path, create=False):
    current = os.dup(fd)
    try:
        parts = path.split('/')
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = _directory(current, part)
            os.close(current)
            current = child
        return current, parts[-1]
    except Exception:
        os.close(current)
        raise


def _seal(fd):
    """Remove write permission from an immutable baseline/snapshot using descriptors."""
    for name in os.listdir(fd):
        info = _entry(fd, name)
        if stat.S_ISDIR(info.st_mode):
            child = _directory(fd, name)
            try:
                _seal(child)
            finally:
                os.close(child)
        else:
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                if not _same(info, os.fstat(child)):
                    raise registry.ControlError('File changed during freeze', 409)
                os.fchmod(child, 0o400)
            finally:
                os.close(child)
    os.fchmod(fd, 0o500)
    os.fsync(fd)


def _copy(parent, name, content, readonly=False, editable_paths=None):
    os.mkdir(name, 0o700, dir_fd=parent)
    fd = _directory(parent, name)
    try:
        for path in sorted(content):
            directory, basename = _parent(fd, path, create=True)
            try:
                writable = not readonly and (editable_paths is None or path in editable_paths)
                _new_file(directory, basename, content[path], 0o600 if writable else 0o400)
            finally:
                os.close(directory)
        if readonly:
            _seal(fd)
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _locked():
    source, root = _paths()
    fd = store.open_directory(root, private=True)
    lock = None
    try:
        try:
            _new_file(fd, '.control-lock', b'')
        except FileExistsError:
            pass
        info = _entry(fd, '.control-lock')
        lock = os.open('.control-lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
        opened = os.fstat(lock)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_uid != os.getuid()
                or opened.st_mode & 0o077 or not _same(info, opened)):
            raise registry.ControlError('Unsafe workspace lock', 503)
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in os.listdir(fd):
            _entry(fd, name)
            if name != '.control-lock' and not _ID.fullmatch(name):
                raise registry.ControlError('Workspace parent is not exclusive', 503)
        yield source, root, fd
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def _authorized(actor_id):
    with store.transaction() as db:
        return store.require_actor(db, actor_id, 'develop')


def _manifest(fd):
    try:
        value = json.loads(_read(fd, 'manifest.json', 128 * 1024).decode('utf-8'))
        if (set(value) != {'id', 'owner', 'component', 'baseline_hash', 'files'}
                or not _ID.fullmatch(value['id']) or not _ID.fullmatch(value['owner'])
                or not _HASH.fullmatch(value['baseline_hash']) or not isinstance(value['files'], dict)
                or not 1 <= len(value['files']) <= MAX_FILES):
            raise ValueError('manifest')
        registry.component(value['component'])
        for path, metadata in value['files'].items():
            _path(path)
            if (not isinstance(metadata, dict) or set(metadata) != {'sha256', 'size'}
                    or not _HASH.fullmatch(metadata['sha256']) or type(metadata['size']) is not int
                    or not 0 <= metadata['size'] <= MAX_FILE_BYTES):
                raise ValueError('file metadata')
        return value
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise registry.ControlError('Workspace metadata invalid', 409)


def _linked(parent, name, fd):
    mapped, opened = _entry(parent, name), os.fstat(fd)
    if (mapped.st_dev, mapped.st_ino) != (opened.st_dev, opened.st_ino):
        raise registry.ControlError('Workspace directory was replaced', 409)


@contextmanager
def _owned(workspace_id, actor_id):
    actor_id = _authorized(actor_id)
    if not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id):
        raise registry.ControlError('Workspace not found', 404)
    with _locked() as (_source, root, parent):
        try:
            fd = _directory(parent, workspace_id)
        except FileNotFoundError:
            raise registry.ControlError('Workspace not found', 404)
        try:
            manifest = _manifest(fd)
            if manifest['id'] != workspace_id or manifest['owner'] != actor_id:
                raise registry.ControlError('Workspace not found', 404)
            _usage(fd)
            tree = _directory(fd, 'tree')
            try:
                content = _inventory(tree)
                if set(content) != set(manifest['files']):
                    raise registry.ControlError('Workspace files differ from trusted manifest', 409)
                for path, metadata in manifest['files'].items():
                    if not _editable(manifest, path) and _digest(content[path]) != metadata['sha256']:
                        raise registry.ControlError('Trusted baseline was modified', 409)
                baseline = _directory(fd, 'baseline')
                try:
                    original = _inventory(baseline)
                finally:
                    os.close(baseline)
                if (set(original) != set(content) or _hash(original) != manifest['baseline_hash']
                        or any(_digest(original[p]) != manifest['files'][p]['sha256'] for p in original)):
                    raise registry.ControlError('Immutable baseline integrity failure', 409)
                _linked(parent, workspace_id, fd)
                _linked(fd, 'tree', tree)
                yield root, fd, tree, manifest, content, original
                _linked(parent, workspace_id, fd)
                _linked(fd, 'tree', tree)
            finally:
                os.close(tree)
        finally:
            os.close(fd)


def _editable(manifest, path):
    top = path.split('/')[0]
    item = registry.component(manifest['component'])
    return (item['editable'] and top == item['directory'] and top in registry.EDITABLE_DIRS
            and top not in registry.PROTECTED_DIRS and path.endswith(('.py', '.html'))
            and not any(_excluded(part) for part in path.split('/')))


def _files(manifest, content):
    return [{'path': path, 'sha256': _digest(data), 'size': len(data), 'editable': _editable(manifest, path)}
            for path, data in sorted(content.items())]


def _hash(content):
    return registry.digest([{'path': p, 'sha256': _digest(data)} for p, data in sorted(content.items())])


def _description(root, manifest, content):
    return {'id': manifest['id'], 'owner': manifest['owner'], 'component': manifest['component'],
            'source_hash': _hash(content), 'baseline_hash': manifest['baseline_hash'],
            'root': os.path.join(root, manifest['id'], 'tree'), 'files': _files(manifest, content),
            'publish_enabled': False, 'authoritative_tests_from_workspace': False}


def create(actor_id, component_id):
    actor_id = _authorized(actor_id)
    item = registry.component(component_id)
    if not item['editable'] or item['directory'] in registry.PROTECTED_DIRS:
        raise registry.ControlError('Component is not editable', 403)
    with _locked() as (source, root, parent):
        if len([n for n in os.listdir(parent) if _ID.fullmatch(n)]) >= MAX_WORKSPACES:
            raise registry.ControlError('Workspace count limit reached', 409)
        source_fd = store.open_directory(source)
        try:
            # Source is operator-provisioned trusted r6. Control/tests never enter a copy.
            control_fd = _directory(source_fd, 'control_plane')
            try:
                _read(control_fd, 'registry.py')
            finally:
                os.close(control_fd)
            content = _inventory(source_fd, source=True)
        finally:
            os.close(source_fd)
        if not any(p.startswith(item['directory'] + '/') for p in content):
            raise registry.ControlError('Component source is unavailable', 503)
        if not any(p.startswith('common/') for p in content) or not any(p.startswith('application_hub/') for p in content):
            raise registry.ControlError('Trusted import baseline is incomplete', 503)
        workspace_id = uuid4().hex
        manifest = {'id': workspace_id, 'owner': actor_id, 'component': component_id,
                    'baseline_hash': _hash(content),
                    'files': {p: {'sha256': _digest(data), 'size': len(data)} for p, data in content.items()}}
        encoded = registry.canonical(manifest)
        if len(encoded) > 128 * 1024 or 2 * sum(len(v) for v in content.values()) + len(encoded) > MAX_BYTES:
            raise registry.ControlError('Workspace baseline storage limit exceeded', 413)
        os.mkdir(workspace_id, 0o700, dir_fd=parent)
        fd = _directory(parent, workspace_id)
        try:
            _new_file(fd, 'manifest.json', encoded, 0o400)
            editable = {path for path in content if _editable(manifest, path)}
            _copy(fd, 'tree', content, editable_paths=editable)
            _copy(fd, 'baseline', content, readonly=True)
            os.mkdir('backups', 0o700, dir_fd=fd)
            os.mkdir('snapshots', 0o700, dir_fd=fd)
            _usage(fd)
        finally:
            os.close(fd)
        with store.transaction() as db:
            store.require_actor(db, actor_id, 'develop')
            store.audit(db, actor_id, 'workspace.create', workspace_id, {'component': component_id})
        return _description(root, manifest, content)


def describe(workspace_id, actor_id):
    with _owned(workspace_id, actor_id) as (root, _fd, _tree, manifest, content, _original):
        return _description(root, manifest, content)


def _path(path):
    if (not isinstance(path, str) or len(path) > 240 or path.startswith('/')
            or not re.fullmatch(r'[A-Za-z0-9_./-]+', path)
            or any(not part or part.startswith('.') for part in path.split('/'))):
        raise registry.ControlError('Invalid relative source path')
    return path


def read_file(workspace_id, actor_id, path):
    path = _path(path)
    with _owned(workspace_id, actor_id) as (_root, _fd, _tree, manifest, content, _original):
        if path not in content:
            raise registry.ControlError('Source file not found', 404)
        data = content[path]
        return {'path': path, 'content': data.decode('utf-8'), 'sha256': _digest(data),
                'editable': _editable(manifest, path), 'size': len(data), 'publish_enabled': False}


def write_file(workspace_id, actor_id, path, expected_sha256, content):
    path = _path(path)
    if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
        raise registry.ControlError('A valid expected_sha256 is required')
    if not isinstance(content, str):
        raise registry.ControlError('Source content must be UTF-8 text')
    try:
        data = content.encode('utf-8')
    except UnicodeError:
        raise registry.ControlError('Source content must be UTF-8 text')
    if len(data) > MAX_FILE_BYTES or '\x00' in content:
        raise registry.ControlError('Source file content limit exceeded', 413)
    with _owned(workspace_id, actor_id) as (_root, fd, tree, manifest, current, _original):
        if path not in current or not _editable(manifest, path):
            raise registry.ControlError('Only existing component source files are editable', 403)
        old = current[path]
        if _digest(old) != expected_sha256:
            raise registry.ControlError('Source hash conflict', 409)
        backup = _directory(fd, 'backups')
        directory, name = _parent(tree, path)
        try:
            if len(os.listdir(backup)) >= MAX_VERSIONS:
                raise registry.ControlError('Retained backup version limit reached', 409)
            # Bound peak storage too: current + retained old + temporary replacement.
            if _usage(fd) + len(old) + len(data) > MAX_BYTES:
                raise registry.ControlError('Workspace total storage limit exceeded', 413)
            # A complete immutable old version is retained before replacing the current file.
            _new_file(backup, _digest(path.encode('utf-8')) + '-' + uuid4().hex, old, 0o400)
            before = _entry(directory, name)
            if _digest(_read(directory, name)) != expected_sha256:
                raise registry.ControlError('Source changed during save', 409)
            temporary = '.save-' + uuid4().hex
            _new_file(directory, temporary, data)
            if not _same(before, _entry(directory, name)):
                raise registry.ControlError('Source changed during save', 409)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            current[path] = data
            if _inventory(tree) != current:
                raise registry.ControlError('Workspace changed during save', 409)
        finally:
            os.close(directory)
            os.close(backup)
        with store.transaction() as db:
            store.require_actor(db, actor_id, 'develop')
            store.audit(db, actor_id, 'workspace.save', workspace_id,
                        {'path_sha256': _digest(path.encode('utf-8')), 'sha256': _digest(data)})
        return {'path': path, 'sha256': _digest(data), 'source_hash': _hash(current),
                'size': len(data), 'backup_retained': True, 'publish_enabled': False}


def diff(workspace_id, actor_id):
    with _owned(workspace_id, actor_id) as (_root, _fd, _tree, manifest, content, original):
        changes, budget = [], 0
        for path in sorted(content):
            if content[path] == original[path]:
                continue
            text = ''.join(difflib.unified_diff(original[path].decode('utf-8').splitlines(True),
                                              content[path].decode('utf-8').splitlines(True),
                                              fromfile=path, tofile=path, n=3))
            budget += len(text.encode('utf-8'))
            if budget > 256 * 1024:
                raise registry.ControlError('Diff response size limit exceeded', 413)
            changes.append({'path': path, 'before_sha256': _digest(original[path]),
                            'sha256': _digest(content[path]), 'diff': text})
        return {'id': manifest['id'], 'source_hash': _hash(content), 'changes': changes,
                'text': '\n'.join(change['diff'] for change in changes),
                'format': 'plain_text', 'publish_enabled': False}


def snapshot(workspace_id, actor_id, expected_hash):
    """Owned immutable job input. expected_hash matches describe()['source_hash'].

    Returned root contains import sources only. Authoritative tests, fixture
    selection and control entrypoints MUST be loaded from CONTROL_SOURCE_ROOT.
    Repeated identical snapshot requests reuse a fully integrity-checked copy.
    """
    if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
        raise registry.ControlError('A valid expected_hash is required')
    with _owned(workspace_id, actor_id) as (root, fd, _tree, manifest, content, _original):
        source_hash = _hash(content)
        if source_hash != expected_hash:
            raise registry.ControlError('Workspace snapshot hash conflict', 409)
        parent = _directory(fd, 'snapshots')
        try:
            try:
                snapshot_fd = _directory(parent, source_hash)
            except FileNotFoundError:
                if len(os.listdir(parent)) >= 8 or _usage(fd) + sum(len(v) for v in content.values()) > MAX_BYTES:
                    raise registry.ControlError('Snapshot storage limit reached', 413)
                _copy(parent, source_hash, content, readonly=True)
                snapshot_fd = _directory(parent, source_hash)
            try:
                frozen = _inventory(snapshot_fd)
                if frozen != content:
                    raise registry.ControlError('Frozen snapshot integrity failure', 409)
            finally:
                os.close(snapshot_fd)
        finally:
            os.close(parent)
        result = _description(root, manifest, content)
        result.update(root=os.path.join(root, workspace_id, 'snapshots', source_hash),
                      snapshot_id=source_hash, hash=source_hash, frozen=True)
        return result
