"""Browser management API only. No business routers, signup, or live publication.

ControllerClient is created lazily from operator env for each internal request;
its request() verifies signed responses and binds raw session tokens internally.
"""
import ipaddress
import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from control_plane import registry, releases, security, store, workspace

MAX_BODY = 65536
POLICY_NOTICE = 'Control policy only; business configuration is not implemented. No live source publication.'


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def constant(_value):
        raise ValueError('non-finite number')

    value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('object required')
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 24:
            raise ValueError('depth')
        if isinstance(item, str):
            item.encode('utf-8')
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError('number')
        elif isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
            pending.extend((k, depth + 1) for k in item)
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


def _origin():
    value = os.environ.get('APP_ORIGIN', '')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or parsed.hostname != parsed.hostname.lower()):
            raise ValueError('origin')
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('port')
        hostname = parsed.hostname
        try:
            address = ipaddress.ip_address(hostname)
            hostname = '[' + hostname + ']' if address.version == 6 else hostname
        except ValueError:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', hostname):
                raise ValueError('host')
        host = hostname + (':' + str(port) if port is not None else '')
        if value != parsed.scheme + '://' + host:
            raise ValueError('canonical origin required')
        return value.encode('ascii'), host.encode('ascii')
    except (ValueError, UnicodeError):
        raise registry.ControlError('Manager APP_ORIGIN must be explicitly configured', 503)


class BrowserBoundary:
    """Exact configured Host/Origin, real-byte JSON bound, duplicates and framing rejected."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            await self.app(scope, receive, send)
            return

        async def secure(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers.extend([(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                                (b'referrer-policy', b'no-referrer'), (b'x-frame-options', b'DENY'),
                                (b'content-security-policy', b"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")])
                message = dict(message, headers=headers)
            await send(message)

        async def reject(status, detail):
            await JSONResponse({'detail': detail}, status_code=status)(scope, receive, secure)

        try:
            origin, host = _origin()
        except registry.ControlError:
            await reject(503, 'Manager origin is not configured')
            return
        headers = {}
        if sum(len(k) + len(v) for k, v in scope['headers']) > 16384:
            await reject(431, 'Headers too large')
            return
        for key, value in scope['headers']:
            headers.setdefault(key.lower(), []).append(value)
        if headers.get(b'host') != [host]:
            await reject(400, 'Host does not match manager origin')
            return
        for name in (b'authorization', b'origin', b'content-length', b'content-type',
                     b'transfer-encoding', b'content-encoding', b'sec-fetch-site'):
            if len(headers.get(name, [])) > 1:
                await reject(400, 'Duplicate boundary header')
                return
        if b'origin' in headers and headers[b'origin'] != [origin]:
            await reject(403, 'Same-origin requests only')
            return
        if headers.get(b'sec-fetch-site', [b'none']) not in ([b'none'], [b'same-origin']):
            await reject(403, 'Cross-site requests forbidden')
            return
        method = scope['method']
        if method not in ('GET', 'POST', 'PATCH'):
            await reject(405, 'Method not allowed')
            return
        if method != 'GET' and headers.get(b'origin') != [origin]:
            await reject(403, 'Same-origin mutation required')
            return
        if headers.get(b'content-encoding', [b'identity']) != [b'identity']:
            await reject(415, 'Compressed input forbidden')
            return
        lengths, transfers = headers.get(b'content-length', []), headers.get(b'transfer-encoding', [])
        if ((lengths and transfers) or (transfers and transfers != [b'chunked'])
                or (lengths and not re.fullmatch(rb'[0-9]{1,8}', lengths[0]))):
            await reject(400, 'Invalid request framing')
            return
        declared = int(lengths[0]) if lengths else None
        bound = 0 if method == 'GET' else MAX_BODY
        if declared is not None and declared > bound:
            await reject(413, 'Request too large')
            return
        content_type = headers.get(b'content-type', [])
        if method != 'GET' and content_type and not re.fullmatch(
                rb'application/json(?:;\s*charset=utf-8)?', content_type[0].lower()):
            await reject(415, 'JSON input required')
            return
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            if message['type'] != 'http.request':
                await reject(400, 'Invalid request transport')
                return
            body.extend(message.get('body', b''))
            if len(body) > bound:
                await reject(413, 'Request too large')
                return
            if not message.get('more_body', False):
                break
            if method == 'GET':
                await reject(400, 'GET body forbidden')
                return
        if declared is not None and len(body) != declared:
            await reject(400, 'Request length mismatch')
            return
        if body and not content_type:
            await reject(415, 'JSON input required')
            return
        if body:
            try:
                strict_json(bytes(body))
            except (ValueError, UnicodeError, RecursionError, OverflowError):
                await reject(422, 'Invalid strict JSON')
                return
        delivered = False
        upstream = receive

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await upstream()
        await self.app(scope, bounded_receive, secure)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(BrowserBoundary)


@app.exception_handler(registry.ControlError)
async def control_error(_request, error):
    # Only trusted error attributes, never a browser header/body request identifier.
    content = {'detail': error.detail[:240]}
    request_id = getattr(error, 'request_id', None)
    if isinstance(request_id, str) and re.fullmatch(r'[0-9a-f]{32}', request_id):
        content['request_id'] = request_id
    return JSONResponse(content, status_code=error.status)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _error):
    return JSONResponse({'detail': 'Invalid fields or types'}, status_code=422)


@app.exception_handler(sqlite3.Error)
@app.exception_handler(OSError)
async def storage_error(_request, _error):
    return JSONResponse({'detail': 'Management storage unavailable'}, status_code=503)


@app.exception_handler(Exception)
async def unexpected_error(_request, _error):
    return JSONResponse({'detail': 'Management request failed'}, status_code=500)


def _fields(payload, required):
    if not isinstance(payload, dict) or set(payload) != set(required):
        raise registry.ControlError('Unknown or missing input fields', 422)
    return payload


def _string(value, maximum=128):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise registry.ControlError('Invalid text field', 422)
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise registry.ControlError('Resource not found', 404)
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise registry.ControlError('Valid expected hash required', 422)
    return value


def session(permission=None):
    def dependency(authorization: str = Header(default=None)):
        if not isinstance(authorization, str) or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', authorization):
            raise registry.ControlError('Management login required', 401)
        token = authorization[7:]
        return security.authenticate(token, permission), token
    return dependency


def controller_client():
    """Trusted protocol only; no browser endpoint/headers, proxy, or executable imports."""
    from control_plane.protocol import ControllerClient
    return ControllerClient(url=os.environ.get('CONTROL_CONTROLLER_URL', ''),
                            key_file=os.environ.get('CONTROL_KEY_FILE', ''))


def _controller(method, path, payload, token, permission):
    security.authenticate(token, permission)
    try:
        return controller_client().request(method, path, payload, token)
    except ImportError:
        raise registry.ControlError('Trusted controller protocol unavailable', 503)


@app.get('/', response_class=HTMLResponse)
def homepage():
    parent = store.open_directory(os.path.dirname(os.path.abspath(__file__)))
    try:
        page = workspace._read(parent, 'index.html', 256 * 1024)
    finally:
        os.close(parent)
    # API flag/notice is authoritative even if a browser does not display its UI.
    return HTMLResponse(page.decode('utf-8'), headers={'X-Control-Policy': 'business-config-not-implemented',
                                                     'X-Publish-Enabled': 'false'})


@app.get('/health')
def health():
    with store.transaction() as db:
        db.execute('SELECT 1')
    return {'status': 'ok', 'module': 'control_plane', 'schema_version': store.SCHEMA_VERSION,
            'controller_runtime': 'unobserved', 'business_config_implemented': False,
            'release_workflow_implemented': True, 'publish_enabled': False, 'notice': POLICY_NOTICE}


@app.post('/api/auth/login')
def login(payload: dict = Body(...)):
    _fields(payload, ('name', 'password'))
    return security.login(payload['name'], payload['password'])


@app.get('/api/auth/me')
def me(auth=Depends(session())):
    return security.user_dict(auth[0])


@app.post('/api/auth/logout')
def logout(auth=Depends(session()), payload: dict = Body(default={})):
    _fields(payload, ())
    return security.logout(auth[1])


@app.get('/api/manage/components')
def components(auth=Depends(session('view'))):
    try:
        observed = _controller('GET', '/v1/status', {}, auth[1], 'view')
    except registry.ControlError as error:
        if error.status in (401, 403):
            raise
        observed = None
    items = []
    # The peer controller wraps Runner.status() under runtime. Only signed data enters here.
    runtime_map, runtime = {}, {}
    if isinstance(observed, dict):
        runtime = observed.get('runtime', observed)
        if not isinstance(runtime, dict):
            runtime = {}
        entries = runtime.get('components', runtime.get('runtimes', runtime.get('status', {})))
        if isinstance(entries, dict):
            runtime_map = entries
        elif isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    key = entry.get('id', entry.get('runtime'))
                    if isinstance(key, str):
                        runtime_map[key] = entry
    for key in registry.REGISTRY:
        item = registry.component(key)
        actual = runtime_map.get(key, runtime_map.get(item['runtime']))
        item.update(runtime_state=actual if isinstance(actual, dict) else {'state': 'unobserved'},
                    runtime_observed=isinstance(actual, dict), active_policy=store.active_policy(key),
                    business_config_implemented=False, policy_only=True, publish_enabled=False)
        items.append(item)
    return {'components': items, 'controller_observed': observed is not None,
            'test': runtime.get('test', {'ready': False, 'reason': 'controller unobserved'}),
            'build': runtime.get('build', {'ready': False, 'reason': 'isolated builder not provisioned'}),
            'limits': {'workspaces': workspace.MAX_WORKSPACES, 'workspace_bytes': workspace.MAX_BYTES,
                       'file_bytes': workspace.MAX_FILE_BYTES, 'sessions': security.MAX_SESSIONS},
            'notice': POLICY_NOTICE, 'business_config_implemented': False,
            'release_workflow_implemented': True, 'publish_enabled': False}


@app.get('/api/manage/report')
def report(auth=Depends(session('view'))):
    snapshot = components(auth)
    return {'timestamp': int(time.time()), 'snapshot': snapshot,
            'historical_reports_available': False, 'alerts_available': False}


def _audit_metadata(encoded):
    """Project stored metadata by meaning, not arbitrary strings or secret-key names."""
    try:
        if not isinstance(encoded, str) or len(encoded) > 2048:
            return {}
        value = strict_json(encoded.encode('utf-8'))
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return {}
    result = {}
    for key, item in value.items():
        if key == 'version' and type(item) is int and 1 <= item <= 10000:
            result[key] = item
        elif isinstance(item, str):
            if ((key == 'component' and item in registry.REGISTRY)
                    or (key in ('revision', 'workspace') and re.fullmatch(r'[0-9a-f]{32}', item))
                    or (key in ('hash', 'sha256', 'path_sha256') and re.fullmatch(r'[0-9a-f]{64}', item))
                    or (key == 'decision' and item in ('approve', 'reject', 'submitted', 'approved', 'rejected', 'activated'))
                    or (key == 'kind' and item in ('test', 'build'))
                    or (key == 'permission' and item in store.PERMISSIONS)):
                result[key] = item
    return result


@app.get('/api/manage/audit')
def audit(auth=Depends(session('view'))):
    # Recheck the real session while holding the grants/storage transaction lock.
    with store.transaction() as db:
        security._authenticate(db, auth[1], 'view')
        rows = db.execute('SELECT id,actor,event,target,metadata,created FROM cp_audit '
                          'ORDER BY id DESC LIMIT 512').fetchall()
    events = {'schema.initialize', 'user.create', 'user.disable', 'permission.grant',
              'permission.revoke', 'session.login', 'session.logout', 'policy.create',
              'policy.edit', 'policy.submit', 'policy.review', 'policy.activate',
              'workspace.create', 'workspace.save'}
    items = []
    for row in rows:
        item = {'id': row['id'], 'created': row['created'],
                'metadata': _audit_metadata(row['metadata'])}
        for field in ('actor', 'target'):
            value = row[field]
            item[field] = value if (re.fullmatch(r'[0-9a-f]{32}', value)
                                   or (field == 'actor' and value == 'provision-cli')
                                   or (field == 'target' and value == 'control')) else '[redacted]'
        item['event'] = row['event'] if row['event'] in events else '[redacted]'
        items.append(item)
    return {'audit': items}


@app.get('/api/manage/config/revisions')
def revisions(component: str = Query(default=None), auth=Depends(session('view'))):
    return {'revisions': store.list_revisions(component), 'policy_only': True,
            'business_config_implemented': False, 'notice': POLICY_NOTICE}


@app.post('/api/manage/config/revisions', status_code=201)
def create_revision(payload: dict = Body(...), auth=Depends(session('configure'))):
    _fields(payload, ('component', 'config'))
    return store.create_revision(auth[0], payload['component'], payload['config'])


@app.patch('/api/manage/config/revisions/{id}')
def edit_revision(id: str, payload: dict = Body(...), auth=Depends(session('configure'))):
    _fields(payload, ('expected_version', 'config'))
    return store.edit_revision(auth[0], _id(id), payload['expected_version'], payload['config'])


@app.post('/api/manage/config/revisions/{id}/submit')
def submit_revision(id: str, payload: dict = Body(...), auth=Depends(session('configure'))):
    _fields(payload, ('expected_version',))
    return store.submit_revision(auth[0], _id(id), payload['expected_version'])


@app.post('/api/manage/config/revisions/{id}/review')
def review_revision(id: str, payload: dict = Body(...), auth=Depends(session('review'))):
    _fields(payload, ('expected_version', 'decision'))
    # UI uses resulting-state words; store uses imperative decisions.
    decision = payload['decision']
    if not isinstance(decision, str):
        raise registry.ControlError('Invalid review decision', 422)
    decision = {'approved': 'approve', 'rejected': 'reject'}.get(decision, decision)
    return store.review_revision(auth[0], _id(id), payload['expected_version'], decision)


@app.post('/api/manage/config/revisions/{id}/activate')
def activate_revision(id: str, payload: dict = Body(...), auth=Depends(session('configure'))):
    _fields(payload, ('expected_version',))
    return store.activate_revision(auth[0], _id(id), payload['expected_version'])


@app.get('/api/releases/versions')
def release_versions(auth=Depends(session('view'))):
    return {'versions': releases.list_versions(auth[1])}


@app.post('/api/releases/versions', status_code=201)
def register_release_version(payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('manifest',))
    return releases.register_version(auth[1], payload['manifest'])


@app.get('/api/releases/versions/{id}')
def release_version(id: str, auth=Depends(session('view'))):
    return releases.get_version(auth[1], _id(id))


@app.get('/api/releases/plans')
def release_plans(auth=Depends(session('view'))):
    return {'plans': releases.list_plans(auth[1])}


@app.post('/api/releases/plans', status_code=201)
def create_release_plan(payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('plan',))
    return releases.create_plan(auth[1], payload['plan'])


@app.get('/api/releases/plans/{id}')
def release_plan(id: str, auth=Depends(session('view'))):
    return releases.get_plan(auth[1], _id(id))


@app.patch('/api/releases/plans/{id}')
def edit_release_plan(id: str, payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('expected_version', 'plan'))
    return releases.edit_plan(auth[1], _id(id), payload['expected_version'], payload['plan'])


@app.post('/api/releases/plans/{id}/submit')
def submit_release_plan(id: str, payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('expected_version',))
    return releases.submit_plan(auth[1], _id(id), payload['expected_version'])


@app.post('/api/releases/plans/{id}/review')
def review_release_plan(id: str, payload: dict = Body(...), auth=Depends(session('review'))):
    _fields(payload, ('expected_version', 'decision'))
    if not isinstance(payload['decision'], str) or payload['decision'] not in ('approve', 'reject'):
        raise registry.ControlError('Decision must be approve or reject', 422)
    return releases.review_plan(auth[1], _id(id), payload['expected_version'], payload['decision'])


@app.post('/api/releases/plans/{id}/cancel')
def cancel_release_plan(id: str, payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('expected_version',))
    return releases.cancel_plan(auth[1], _id(id), payload['expected_version'])


@app.post('/api/releases/plans/{id}/execute')
def execute_release_plan(id: str, payload: dict = Body(...), auth=Depends(session('operate'))):
    _fields(payload, ('expected_version',))
    # Persisted request only: no executor argument, callback, or publication path.
    return releases.request_execution(auth[1], _id(id), payload['expected_version'])


@app.post('/api/releases/rollback-plans', status_code=201)
def create_rollback_plan(payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('version_id', 'current_version_id', 'window'))
    return releases.create_rollback_plan(auth[1], _id(payload['version_id']),
                                         _id(payload['current_version_id']), payload['window'])


@app.get('/api/releases/requests')
def release_requests(auth=Depends(session('view'))):
    return {'requests': releases.list_execution_requests(auth[1])}


@app.get('/api/releases/audit')
def release_audit(auth=Depends(session('view'))):
    return {'audit': releases.list_audit(auth[1])}


@app.post('/api/dev/workspaces', status_code=201)
def create_workspace(payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('component',))
    return workspace.create(auth[0].id, payload['component'])


@app.get('/api/dev/workspaces/{id}')
def describe_workspace(id: str, auth=Depends(session('develop'))):
    return workspace.describe(_id(id), auth[0].id)


@app.get('/api/dev/workspaces/{id}/file')
def read_file(id: str, path: str = Query(...), auth=Depends(session('develop'))):
    return workspace.read_file(_id(id), auth[0].id, path)


@app.patch('/api/dev/workspaces/{id}/file')
def write_file(id: str, payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('path', 'expected_sha256', 'content'))
    return workspace.write_file(_id(id), auth[0].id, payload['path'], payload['expected_sha256'], payload['content'])


@app.get('/api/dev/workspaces/{id}/diff')
def diff(id: str, auth=Depends(session('develop'))):
    return workspace.diff(_id(id), auth[0].id)


@app.post('/api/dev/jobs', status_code=202)
def create_job(payload: dict = Body(...), auth=Depends(session('develop'))):
    _fields(payload, ('workspace_id', 'expected_hash', 'kind'))
    workspace_id = _id(payload['workspace_id'])
    expected_hash = _hash(payload['expected_hash'])
    if payload['kind'] not in ('test', 'build'):
        raise registry.ControlError('Job kind must be test or build', 422)
    # Controller independently authenticates and snapshots; browser cannot supply a path or command.
    description = workspace.describe(workspace_id, auth[0].id)
    if description['source_hash'] != expected_hash:
        raise registry.ControlError('Workspace source hash conflict', 409)
    return _controller('POST', '/v1/jobs', {'workspace_id': workspace_id, 'expected_hash': expected_hash,
                                         'kind': payload['kind']}, auth[1], 'develop')


@app.get('/api/dev/jobs')
def jobs(auth=Depends(session('view'))):
    return _controller('GET', '/v1/jobs', {}, auth[1], 'view')


@app.get('/api/dev/jobs/{id}')
def job(id: str, auth=Depends(session('view'))):
    return _controller('GET', '/v1/jobs/' + _id(id), {}, auth[1], 'view')


@app.post('/api/manage/actions')
def action(payload: dict = Body(...), auth=Depends(session('operate'))):
    _fields(payload, ('component', 'action', 'expected_config_hash'))
    registry.component(payload['component'])
    if payload['action'] not in ('start', 'stop', 'restart'):
        raise registry.ControlError('Action must be start, stop or restart', 422)
    _hash(payload['expected_config_hash'])
    policy = store.active_policy(payload['component'])
    if policy is None or policy['hash'] != payload['expected_config_hash']:
        raise registry.ControlError('An active matching control policy is required', 409)
    if payload['action'] != 'stop' and not policy['enabled']:
        raise registry.ControlError('Control policy denies operation admission', 409)
    return _controller('POST', '/v1/actions', dict(payload), auth[1], 'operate')
