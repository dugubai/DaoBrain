"""Stdlib-only separate management credentials. No role, signup, or default user.

CLI: python -m control_plane.security init
     CONTROL_PROVISION_PASSWORD=... python -m control_plane.security bootstrap --name NAME
     CONTROL_PROVISION_PASSWORD=... python -m control_plane.security create --name NAME
     python -m control_plane.security grant --name NAME --permission view
Bootstrap/create grant nothing unless explicit --permission arguments are supplied.
"""
import argparse
from collections import deque, namedtuple
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from threading import Lock
import time
from uuid import uuid4

from control_plane import registry, store

Principal = namedtuple('Principal', 'id name permissions')
PERMISSIONS = store.PERMISSIONS
PBKDF2_ROUNDS = 240000
SESSION_TTL = 3600
MAX_SESSIONS = 256
MAX_USER_SESSIONS = 8
_TOKEN = re.compile(r'\A[A-Za-z0-9_-]{43}\Z')
_NAME = re.compile(r'\A[A-Za-z][A-Za-z0-9_-]{2,39}\Z')
_attempts = deque()
_lock = Lock()
_DUMMY = 'pbkdf2$240000$' + '00' * 16 + '$' + '00' * 32


def _password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or len(password.encode('utf-8')) > 512:
        raise registry.ControlError('Password must be 12 to 128 characters')
    return password


def password_hash(password):
    password = _password(password)
    salt = secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ROUNDS)
    return 'pbkdf2$240000$' + salt.hex() + '$' + result.hex()


def password_matches(password, stored):
    try:
        _password(password)
        scheme, rounds, salt, expected = stored.split('$')
        if (scheme != 'pbkdf2' or rounds != '240000' or not re.fullmatch('[0-9a-f]{32}', salt)
                or not re.fullmatch('[0-9a-f]{64}', expected)):
            return False
        result = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), PBKDF2_ROUNDS)
        return hmac.compare_digest(result.hex(), expected)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


def digest_token(token):
    """Accept only a raw 43-character session token, never an Authorization header."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise registry.ControlError('Management login required', 401)
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _principal(db, user_id, name):
    grants = frozenset(row[0] for row in db.execute('SELECT permission FROM cp_grants WHERE user_id=?', (user_id,)))
    if grants - PERMISSIONS:
        raise registry.ControlError('Invalid management grants', 503)
    return Principal(user_id, name, grants)


def _authenticate(db, token, permission=None):
    digest = digest_token(token)
    if permission is not None and permission not in PERMISSIONS:
        raise registry.ControlError('Unknown management permission', 403)
    row = db.execute('SELECT u.id,u.name FROM cp_sessions s JOIN cp_users u ON u.id=s.user_id '
                     'WHERE s.digest=? AND s.expires>? AND u.enabled=1', (digest, int(time.time()))).fetchone()
    if row is None:
        raise registry.ControlError('Management session invalid or expired', 401)
    principal = _principal(db, row['id'], row['name'])
    if permission is not None and permission not in principal.permissions:
        raise registry.ControlError('Management permission denied', 403)
    return principal


def authenticate(token, permission=None):
    """Raw token -> Principal(id,name,frozenset grants); recheck expiry/user/grants each call."""
    digest_token(token)
    with store.transaction() as db:
        return _authenticate(db, token, permission)


def user_dict(principal):
    return {'id': principal.id, 'name': principal.name, 'permissions': sorted(principal.permissions)}


def login(name, password):
    """Return {token, expires, user:{id,name,permissions}}. Session plaintext is never stored."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise registry.ControlError('Invalid management credentials', 401)
    try:
        _password(password)
    except (ValueError, UnicodeError):
        raise registry.ControlError('Invalid management credentials', 401)
    with _lock:
        now = time.monotonic()
        while _attempts and now - _attempts[0] >= 60:
            _attempts.popleft()
        if len(_attempts) >= 12:
            raise registry.ControlError('Login rate limit reached', 429)
        _attempts.append(now)
    with store.transaction() as db:
        row = db.execute('SELECT * FROM cp_users WHERE name=? AND enabled=1', (name,)).fetchone()
        valid = password_matches(password, row['password'] if row else _DUMMY)
        if row is None or not valid:
            raise registry.ControlError('Invalid management credentials', 401)
        now = int(time.time())
        db.execute('DELETE FROM cp_sessions WHERE expires<=?', (now,))
        total = db.execute('SELECT count(*) FROM cp_sessions').fetchone()[0]
        own = db.execute('SELECT count(*) FROM cp_sessions WHERE user_id=?', (row['id'],)).fetchone()[0]
        if total >= MAX_SESSIONS or own >= MAX_USER_SESSIONS:
            raise registry.ControlError('Session capacity reached', 429)
        token = secrets.token_urlsafe(32)
        expires = now + SESSION_TTL
        db.execute('INSERT INTO cp_sessions VALUES(?,?,?,?)', (digest_token(token), row['id'], expires, now))
        store.audit(db, row['id'], 'session.login', row['id'])
        return {'token': token, 'expires': expires, 'user': user_dict(_principal(db, row['id'], row['name']))}


def logout(token):
    """Recheck raw token and revoke its session; invalid tokens fail closed."""
    with store.transaction() as db:
        principal = _authenticate(db, token)
        db.execute('DELETE FROM cp_sessions WHERE digest=?', (digest_token(token),))
        store.audit(db, principal.id, 'session.logout', principal.id)
    return {'logged_out': True}


def provision(name, password, permissions=(), bootstrap=False):
    """Trusted local CLI primitive, not an API. Explicit grants only; no role defaults."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise registry.ControlError('Invalid management account name')
    if isinstance(permissions, str):
        raise registry.ControlError('Explicit permission collection required')
    grants = frozenset(permissions)
    if grants - PERMISSIONS:
        raise registry.ControlError('Unknown management permission')
    stored = password_hash(password)
    with store.transaction() as db:
        count = db.execute('SELECT count(*) FROM cp_users').fetchone()[0]
        if bootstrap and count:
            raise registry.ControlError('Bootstrap refused: accounts already exist', 409)
        if count >= store.MAX_USERS:
            raise registry.ControlError('Management account capacity reached', 409)
        if db.execute('SELECT 1 FROM cp_users WHERE name=?', (name,)).fetchone():
            raise registry.ControlError('Account already exists', 409)
        user_id = uuid4().hex
        db.execute('INSERT INTO cp_users VALUES(?,?,?,1)', (user_id, name, stored))
        store.audit(db, 'provision-cli', 'user.create', user_id)
        for permission in sorted(grants):
            db.execute('INSERT INTO cp_grants VALUES(?,?)', (user_id, permission))
            store.audit(db, 'provision-cli', 'permission.grant', user_id, {'permission': permission})
        return user_dict(_principal(db, user_id, name))


def grant(name, permission, revoke=False):
    """Trusted CLI-only explicit grant/revoke. Existing sessions see changes immediately."""
    if permission not in PERMISSIONS:
        raise registry.ControlError('Unknown management permission')
    with store.transaction() as db:
        row = db.execute('SELECT id,name FROM cp_users WHERE name=?', (name,)).fetchone()
        if row is None:
            raise registry.ControlError('Account not found', 404)
        if revoke:
            db.execute('DELETE FROM cp_grants WHERE user_id=? AND permission=?', (row['id'], permission))
        else:
            db.execute('INSERT OR IGNORE INTO cp_grants VALUES(?,?)', (row['id'], permission))
        store.audit(db, 'provision-cli', 'permission.revoke' if revoke else 'permission.grant',
                    row['id'], {'permission': permission})
        return user_dict(_principal(db, row['id'], row['name']))


def disable(name):
    """Trusted CLI-only disable with immediate revocation of all sessions."""
    with store.transaction() as db:
        row = db.execute('SELECT id FROM cp_users WHERE name=?', (name,)).fetchone()
        if row is None:
            raise registry.ControlError('Account not found', 404)
        db.execute('UPDATE cp_users SET enabled=0 WHERE id=?', (row['id'],))
        db.execute('DELETE FROM cp_sessions WHERE user_id=?', (row['id'],))
        store.audit(db, 'provision-cli', 'user.disable', row['id'])


def cli(argv=None):
    parser = argparse.ArgumentParser(description='Separate local management account provisioning')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('init')
    for command in ('bootstrap', 'create'):
        item = commands.add_parser(command)
        item.add_argument('--name', required=True)
        item.add_argument('--permission', action='append', choices=sorted(PERMISSIONS), default=[])
    for command in ('grant', 'revoke'):
        item = commands.add_parser(command)
        item.add_argument('--name', required=True)
        item.add_argument('--permission', required=True, choices=sorted(PERMISSIONS))
    item = commands.add_parser('disable')
    item.add_argument('--name', required=True)
    args = parser.parse_args(argv)
    if not args.command:
        parser.error('Explicit command required')
    try:
        if args.command == 'init':
            store.initialize()
        elif args.command in ('bootstrap', 'create'):
            password = os.environ.get('CONTROL_PROVISION_PASSWORD', '')
            provision(args.name, password, args.permission, args.command == 'bootstrap')
        elif args.command in ('grant', 'revoke'):
            grant(args.name, args.permission, args.command == 'revoke')
        else:
            disable(args.name)
    except (registry.ControlError, OSError, ValueError, sqlite3.Error):
        parser.exit(1, 'Management provisioning failed; verify private paths, schema and explicit inputs.\n')
    print('Management provisioning completed; no credentials printed')
    return 0


if __name__ == '__main__':
    cli()
