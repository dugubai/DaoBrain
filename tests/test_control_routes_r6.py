"""r6 routes with real stdlib release/security/storage and strict ASGI boundary.

No installation, server, network, engine, or publication. When FastAPI is present,
ASGI requests use the real app; otherwise explicitly test-only wiring stubs are
used (the review Mac has no FastAPI). Python 3.11 cloud can exercise the real app.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_control_routes_r6.py -v
"""
import ast
import asyncio
from contextlib import contextmanager
import hashlib
import hmac
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from control_plane import registry, releases, security, store, workspace
from control_plane.sandbox_build_entry import build

PASSWORD = 'Synthetic-route-test-password-123'
CONFIG = {'enabled': True, 'request_timeout_s': 30}
ORIGIN = 'http://manager.test:18126'


def load_file(name, path):
    specification = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@contextmanager
def loaded_main():
    try:
        import fastapi  # noqa: F401 -- never install it for these tests
    except ModuleNotFoundError as error:
        if error.name != 'fastapi':
            raise
        helper = load_file('_r6_existing_boundary_fixture', PROJECT / 'tests/test_control_management_r6.py')
        with helper.stubbed_main() as main:
            yield main, False
    else:
        yield load_file('_r6_real_route_app', PROJECT / 'control_plane/main.py'), True


def route_specs():
    """Read fixed decorator paths and grants, not guessed route names."""
    result = {}
    tree = ast.parse((PROJECT / 'control_plane/main.py').read_text())
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute)
                    or not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != 'app'
                    or decorator.func.attr not in ('get', 'post', 'patch')):
                continue
            permission = None
            authenticated = False
            for argument, default in zip(node.args.args[-len(node.args.defaults):], node.args.defaults):
                if argument.arg == 'auth':
                    authenticated = True
                    call = default.args[0]
                    permission = call.args[0].value if call.args else None
            status = next((k.value.value for k in decorator.keywords if k.arg == 'status_code'), 200)
            result[(decorator.func.attr.upper(), decorator.args[0].value)] = (
                node.name, authenticated, permission, status)
    return result


async def stub_dispatch(main, scope, receive, send):
    """Fallback wiring only; real boundary, session dependency and domain calls."""
    from fastapi.responses import JSONResponse
    for (method, path), (name, authenticated, permission, status) in route_specs().items():
        match = re.fullmatch(re.escape(path).replace(r'\{id\}', '(?P<id>[^/]+)'), scope['path'])
        if method != scope['method'] or match is None:
            continue
        try:
            arguments = match.groupdict()
            if authenticated:
                authorization = next((v.decode('ascii') for k, v in scope['headers'] if k == b'authorization'), None)
                arguments['auth'] = main.session(permission)(authorization)
            function = getattr(main, name)
            if 'payload' in inspect.signature(function).parameters:
                body = (await receive())['body']
                if not body:
                    raise registry.ControlError('Invalid fields or types', 422)
                arguments['payload'] = main.strict_json(body)
            result = function(**arguments)
            response = JSONResponse(result, status_code=status)
        except registry.ControlError as error:
            response = await main.control_error(None, error)
        await response(scope, receive, send)
        return
    await JSONResponse({'detail': 'Not Found'}, status_code=404)(scope, receive, send)


def request(main, real, method, path, token=None, payload=None, body=None, headers=None, chunks=None):
    """Drive ASGI without TestClient/httpx or sockets, including real byte framing."""
    async def exercise():
        data = body if body is not None else (registry.canonical(payload) if payload is not None else b'')
        boundary_headers = [(b'host', b'manager.test:18126')]
        if method != 'GET':
            boundary_headers += [(b'origin', ORIGIN.encode()), (b'content-type', b'application/json')]
        if token is not None:
            boundary_headers.append((b'authorization', ('Bearer ' + token).encode()))
        if headers is not None:
            boundary_headers = headers
        queue = list(chunks) if chunks is not None else [data]
        output = []

        async def receive():
            if queue:
                chunk = queue.pop(0)
                return {'type': 'http.request', 'body': chunk, 'more_body': bool(queue)}
            return {'type': 'http.disconnect'}

        async def send(message):
            output.append(message)

        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': b'', 'root_path': '', 'headers': boundary_headers,
                 'client': ('127.0.0.1', 10000), 'server': ('manager.test', 18126)}
        if real:
            await main.app(scope, receive, send)
        else:
            async def dispatch(scope, receive, send):
                await stub_dispatch(main, scope, receive, send)
            await main.BrowserBoundary(dispatch)(scope, receive, send)
        start = next(item for item in output if item['type'] == 'http.response.start')
        content = b''.join(item.get('body', b'') for item in output if item['type'] == 'http.response.body')
        return start['status'], json.loads(content), start['headers']
    return asyncio.run(exercise())


class ControlRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The reviewed repository may be read-only; use the caller's writable TMPDIR.
        cls.temporary_root = tempfile.gettempdir()
        cls.template_temp = tempfile.TemporaryDirectory(prefix='routes-template-', dir=cls.temporary_root)
        cls.template = Path(cls.template_temp.name).resolve()
        (cls.template / 'control').mkdir(mode=0o700)
        cls.template_db = cls.template / 'control/control.sqlite'
        with mock.patch.dict(os.environ, {'CONTROL_DB_PATH': str(cls.template_db)}, clear=True):
            store.initialize()
            cls.users = {}
            for name, grants in (
                    ('author', ('view', 'develop', 'configure', 'review', 'operate')),
                    ('reviewer', ('view', 'review')),
                    ('operator', ('view', 'operate')),
                    ('viewer', ('view',)),
                    ('otherdev', ('view', 'develop'))):
                cls.users[name] = security.provision(name, PASSWORD, grants)
            policy = store.create_revision(cls.users['author']['id'], 'card_workflow', CONFIG)
            policy = store.submit_revision(cls.users['author']['id'], policy['id'], 1)
            cls.policy = store.review_revision(cls.users['reviewer']['id'], policy['id'], 2, 'approve')

    @classmethod
    def tearDownClass(cls):
        cls.template_temp.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='routes-test-', dir=self.temporary_root)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        for name in ('control', 'release', 'source', 'workspaces', 'business', 'staging', 'trust'):
            (self.root / name).mkdir(mode=0o700)
        self.control_db = self.root / 'control/control.sqlite'
        self.release_db = self.root / 'release/releases.sqlite'
        shutil.copyfile(str(self.template_db), str(self.control_db))
        self.control_db.chmod(0o600)
        self.key = b'r6-route-private-test-key-32byte'
        self.assertEqual(32, len(self.key))
        self.key_path = self.root / 'trust/key'
        self.key_path.write_bytes(self.key)
        self.key_path.chmod(0o600)
        self.env = mock.patch.dict(os.environ, {
            'CONTROL_DB_PATH': str(self.control_db), 'CONTROL_RELEASE_DB_PATH': str(self.release_db),
            'CONTROL_SOURCE_ROOT': str(self.root / 'source'), 'CONTROL_WORKSPACE_ROOT': str(self.root / 'workspaces'),
            'APP_DB_PATH': str(self.root / 'business/app.sqlite'), 'APP_ORIGIN': ORIGIN,
            'CONTROL_RELEASE_STAGING_ROOT': str(self.root / 'staging'),
            'CONTROL_RELEASE_ATTESTATION_KEY_PATH': str(self.key_path),
            'CONTROL_CONTROLLER_URL': '', 'CONTROL_KEY_FILE': '',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        security._attempts.clear()
        self.tokens = {name: security.login(name, PASSWORD)['token'] for name in self.users}
        releases.initialize()
        self.main_context = loaded_main()
        self.main, self.real_fastapi = self.main_context.__enter__()
        self.addCleanup(self.main_context.__exit__, None, None, None)

    def auth(self, name='author'):
        return self.main.session()('Bearer ' + self.tokens[name])

    def api(self, method, path, payload=None, name='author', expected=200, **kwargs):
        token = self.tokens[name] if name is not None else None
        status, content, headers = request(self.main, self.real_fastapi, method, path, token, payload, **kwargs)
        self.assertEqual(expected, status, content)
        self.assertIn((b'cache-control', b'no-store'), headers)
        self.assertIn((b'x-content-type-options', b'nosniff'), headers)
        return content

    def error(self, status, function, *args):
        with self.assertRaises(registry.ControlError) as raised:
            function(*args)
        self.assertEqual(status, raised.exception.status)
        return raised.exception

    def manifest(self, label='r6.0.1', previous=None, image=None):
        return {'component': 'card_workflow', 'runtime': 'backend', 'label': label,
                'source_sha256': '1' * 64, 'artifact_sha256': '2' * 64,
                'config_revision': self.policy['id'], 'config_sha256': self.policy['hash'],
                'test_evidence_sha256': '3' * 64, 'build_evidence_sha256': '4' * 64,
                'image_digest': image, 'schema_version': 1, 'previous_version': previous}

    def version(self, **kwargs):
        return self.api('POST', '/api/releases/versions', {'manifest': self.manifest(**kwargs)}, expected=201)

    def plan_input(self, version_id, offset=0):
        now = int(time.time())
        return {'version_id': version_id, 'planned_at': now - 10 + offset, 'expires_at': now + 600 + offset,
                'strategy': 'serial-app-slot', 'dependencies': [],
                'prechecks': {field: None for field in releases._PRECHECK_FIELDS}}

    def approved(self, version=None, offset=0):
        version = version or self.version()
        plan = self.api('POST', '/api/releases/plans', {'plan': self.plan_input(version['id'], offset)}, expected=201)
        plan = self.api('POST', '/api/releases/plans/' + plan['id'] + '/submit', {'expected_version': plan['version']})
        return self.api('POST', '/api/releases/plans/' + plan['id'] + '/review',
                        {'expected_version': plan['version'], 'decision': 'approve'}, name='reviewer')

    def attested_version(self, label='r6.0.1', previous=None, image=None):
        """Actual source ZIP and signed local evidence, NEVER HTTP attestation."""
        stage = self.root / ('candidate-' + label)
        stage.mkdir(mode=0o700)
        (stage / 'card_workflow').mkdir(mode=0o700)
        source = stage / 'card_workflow/main.py'
        source.write_bytes(b"raise RuntimeError('source must never execute')\n")
        output = self.root / ('artifact-' + label)
        output.mkdir(mode=0o700)
        built = build(stage, output)
        artifact = (output / built['artifact']).read_bytes()
        source_hash = registry.digest([{'path': 'card_workflow/main.py', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}])
        manifest = self.manifest(label, previous, image)
        manifest.update(source_sha256=source_hash, artifact_sha256=built['sha256'])
        stable = {k: v for k, v in manifest.items() if k not in ('build_evidence_sha256', 'test_evidence_sha256')}
        binding = {'manifest_sha256': registry.digest(stable), 'source_sha256': source_hash,
                   'artifact_sha256': built['sha256'], 'config_revision': manifest['config_revision'],
                   'config_sha256': manifest['config_sha256'], 'schema_version': 1,
                   'image_digest': image, 'component': 'card_workflow', 'runtime': 'backend'}
        build_report = registry.canonical(dict(binding, kind='offline-source-artifact', syntax_checked=True, image_built=False))
        test_report = registry.canonical(dict(binding, kind='trusted-tests', passed=True, tests_run=3,
                                             test_entry_sha256='a' * 64, fixture_sha256='b' * 64))
        manifest.update(build_evidence_sha256=hashlib.sha256(build_report).hexdigest(),
                        test_evidence_sha256=hashlib.sha256(test_report).hexdigest())
        version = self.api('POST', '/api/releases/versions', {'manifest': manifest}, expected=201)
        shutil.move(str(stage), str(self.root / 'staging' / version['id']))
        signed = {'version_id': version['id'], 'manifest_sha256': version['hash'],
                  'build_sha256': manifest['build_evidence_sha256'], 'test_sha256': manifest['test_evidence_sha256']}
        evidence = {'artifact_bytes': artifact, 'build_report_bytes': build_report, 'test_report_bytes': test_report,
                    'signature': hmac.new(self.key, registry.canonical(signed), hashlib.sha256).hexdigest()}
        return releases.attest_version_cli(self.tokens['reviewer'], version['id'], evidence)

    def test_exact_fixed_routes_and_existing_six_modules(self):
        expected = {
            ('GET', '/api/releases/versions'), ('POST', '/api/releases/versions'),
            ('GET', '/api/releases/versions/{id}'), ('GET', '/api/releases/plans'),
            ('POST', '/api/releases/plans'), ('GET', '/api/releases/plans/{id}'),
            ('PATCH', '/api/releases/plans/{id}'),
            ('POST', '/api/releases/plans/{id}/submit'), ('POST', '/api/releases/plans/{id}/review'),
            ('POST', '/api/releases/plans/{id}/cancel'), ('POST', '/api/releases/plans/{id}/execute'),
            ('POST', '/api/releases/rollback-plans'), ('GET', '/api/releases/requests'), ('GET', '/api/releases/audit'),
        }
        specs = route_specs()
        self.assertEqual(expected, {route for route in specs if route[1].startswith('/api/releases')})
        self.assertEqual(37, len(specs))
        for route in (('GET', '/api/manage/components'), ('GET', '/api/manage/report'),
                      ('GET', '/api/manage/config/revisions'), ('POST', '/api/dev/workspaces'),
                      ('POST', '/api/manage/actions'), ('GET', '/api/manage/audit')):
            self.assertIn(route, specs)
        registered = ({(method, path) for method, path in self.main.app.routes} if not self.real_fastapi else
                      {(method, route.path) for route in self.main.app.routes for method in route.methods})
        self.assertEqual(set(specs), registered)
        for (method, path), (_name, authenticated, permission, _status) in specs.items():
            if path.startswith('/api/releases'):
                self.assertTrue(authenticated)
                wanted = ('view' if method == 'GET' else 'operate' if path.endswith('/execute')
                          else 'review' if path.endswith('/review') else 'develop')
                self.assertEqual(wanted, permission)
        tree = ast.parse((PROJECT / 'control_plane/main.py').read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'releases']
        self.assertFalse(any(node.func.attr in ('initialize', 'attest_version_cli', 'mark_success') for node in calls))
        execute = next(node for node in calls if node.func.attr == 'request_execution')
        self.assertEqual(3, len(execute.args))
        self.assertEqual([], execute.keywords)

    def test_version_registration_listing_detail_and_integrity(self):
        version = self.version()
        self.assertEqual(self.users['author']['id'], version['createdby'])
        self.assertFalse(version['execution_ready'])
        self.assertFalse(version['deployed'])
        self.assertFalse(version['image_verified'])
        listed = self.api('GET', '/api/releases/versions', name='viewer')['versions']
        self.assertEqual([version], listed)
        self.assertEqual(version, self.api('GET', '/api/releases/versions/' + version['id'], name='viewer'))
        self.api('POST', '/api/releases/versions', {'manifest': self.manifest()}, expected=409)
        self.api('GET', '/api/releases/versions/' + 'f' * 32, expected=404)
        for bad in ('bad', 'A' * 32, 'a' * 33):
            self.api('GET', '/api/releases/versions/' + bad, expected=404)
        with releases._connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE release_versions SET label='r9.0.1' WHERE id=?", (version['id'],))

    def test_edit_submit_review_stale_and_independent_actor(self):
        version = self.version()
        value = self.plan_input(version['id'])
        plan = self.api('POST', '/api/releases/plans', {'plan': value}, expected=201)
        path = '/api/releases/plans/' + plan['id']
        self.assertEqual(plan, self.api('GET', path, name='viewer'))
        value['expires_at'] += 10
        self.api('PATCH', path, {'expected_version': 1, 'plan': value}, name='otherdev', expected=409)
        plan = self.api('PATCH', path, {'expected_version': 1, 'plan': value})
        self.assertEqual(2, plan['version'])
        self.api('POST', path + '/submit', {'expected_version': 1}, expected=409)
        self.api('POST', path + '/submit', {'expected_version': True}, expected=400)
        plan = self.api('POST', path + '/submit', {'expected_version': 2})
        self.api('POST', path + '/review', {'expected_version': 3, 'decision': 'approve'}, expected=403)
        plan = self.api('POST', path + '/review', {'expected_version': 3, 'decision': 'approve'}, name='reviewer')
        self.assertEqual('approved', plan['status'])
        self.assertEqual(plan['hash'], plan['approved_hash'])
        self.api('PATCH', path, {'expected_version': 4, 'plan': value}, expected=409)
        self.assertEqual([plan], self.api('GET', '/api/releases/plans', name='viewer')['plans'])

    def test_strict_top_level_fields_for_every_mutation(self):
        version = self.version()
        plan = self.approved(version)
        path = '/api/releases/plans/' + plan['id']
        cases = [
            ('POST', '/api/releases/versions', {'manifest': self.manifest()}, 'author'),
            ('POST', '/api/releases/plans', {'plan': self.plan_input(version['id'])}, 'author'),
            ('PATCH', path, {'expected_version': 4, 'plan': self.plan_input(version['id'])}, 'author'),
            ('POST', path + '/submit', {'expected_version': 4}, 'author'),
            ('POST', path + '/review', {'expected_version': 4, 'decision': 'approve'}, 'reviewer'),
            ('POST', path + '/cancel', {'expected_version': 4}, 'author'),
            ('POST', path + '/execute', {'expected_version': 4}, 'operator'),
            ('POST', '/api/releases/rollback-plans', {'version_id': version['id'], 'current_version_id': version['id'],
                                                   'window': {'planned_at': 1, 'expires_at': 2}}, 'author'),
        ]
        for method, route, body, name in cases:
            for extra in ('token', 'executor', 'callback', 'request_id', 'owner'):
                with self.subTest(route=route, extra=extra):
                    result = self.api(method, route, dict(body, **{extra: self.tokens['author']}), name=name, expected=422)
                    self.assertEqual({'detail'}, set(result))
                    self.assertNotIn(self.tokens['author'], json.dumps(result))
            for field in body:
                missing = {k: v for k, v in body.items() if k != field}
                self.api(method, route, missing, name=name, expected=422)
        self.assertEqual([], self.api('GET', '/api/releases/requests')['requests'])

    def test_nested_claims_and_review_decision_aliases_are_rejected(self):
        for field in ('verified', 'executor', 'token', 'url', 'createdby'):
            self.api('POST', '/api/releases/versions', {'manifest': dict(self.manifest(), **{field: True})}, expected=400)
        bad = self.manifest()
        bad['schema_version'] = True
        self.api('POST', '/api/releases/versions', {'manifest': bad}, expected=400)
        bad = self.manifest()
        bad['config_sha256'] = 'f' * 64
        self.api('POST', '/api/releases/versions', {'manifest': bad}, expected=409)
        version = self.version()
        self.api('POST', '/api/releases/plans', {'plan': dict(self.plan_input(version['id']), owner=self.users['operator']['id'])}, expected=400)
        value = self.plan_input(version['id'])
        value['prechecks']['health_sha256'] = True
        self.api('POST', '/api/releases/plans', {'plan': value}, expected=400)
        plan = self.api('POST', '/api/releases/plans', {'plan': self.plan_input(version['id'])}, expected=201)
        path = '/api/releases/plans/' + plan['id']
        self.api('POST', path + '/submit', {'expected_version': 1})
        for decision in ('approved', 'rejected', 'yes', True, None, {}):
            self.api('POST', path + '/review', {'expected_version': 2, 'decision': decision}, name='reviewer', expected=422)
        self.assertEqual('cancelled', self.api('POST', path + '/review',
                                              {'expected_version': 2, 'decision': 'reject'}, name='reviewer')['status'])

    def test_cancel_draft_and_approved_without_execution(self):
        version = self.version()
        plan = self.api('POST', '/api/releases/plans', {'plan': self.plan_input(version['id'])}, expected=201)
        path = '/api/releases/plans/' + plan['id'] + '/cancel'
        self.api('POST', path, {'expected_version': 1}, name='otherdev', expected=409)
        self.assertEqual('cancelled', self.api('POST', path, {'expected_version': 1})['status'])
        approved = self.approved(version, offset=1)
        self.assertEqual('cancelled', self.api('POST', '/api/releases/plans/' + approved['id'] + '/cancel',
                                              {'expected_version': approved['version']})['status'])
        self.assertEqual([], self.api('GET', '/api/releases/requests')['requests'])

    def test_execution_503_request_id_is_durable_and_no_executor_argument(self):
        plan = self.approved()
        path = '/api/releases/plans/' + plan['id']
        self.api('POST', path + '/execute', {'expected_version': plan['version']}, expected=403)
        with mock.patch.object(releases, 'request_execution', wraps=releases.request_execution) as execution:
            result = self.api('POST', path + '/execute', {'expected_version': plan['version']}, name='operator', expected=503)
            execution.assert_called_once_with(self.tokens['operator'], plan['id'], plan['version'])
        self.assertRegex(result['request_id'], r'\A[0-9a-f]{32}\Z')
        self.assertEqual('Execution withheld: trusted-local-attestation-required', result['detail'])
        requests = self.api('GET', '/api/releases/requests', name='viewer')['requests']
        self.assertEqual(1, len(requests))
        self.assertEqual(result['request_id'], requests[0]['id'])
        self.assertEqual('blocked', requests[0]['state'])
        self.assertFalse(requests[0]['deployed'])
        self.assertFalse(requests[0]['execution_ready'])
        stored = self.api('GET', path, name='viewer')
        self.assertEqual('blocked', stored['status'])
        self.assertEqual('trusted-local-attestation-required', stored['blocker'])
        duplicate = self.api('POST', path + '/execute', {'expected_version': plan['version']}, name='operator', expected=409)
        self.assertNotIn('request_id', duplicate)
        events = self.api('GET', '/api/releases/audit', name='viewer')['audit']
        self.assertTrue(any(row['event'] == 'release.execution-blocked' and row['target'] == plan['id'] for row in events))
        with releases._connection() as db:
            self.assertIsNone(db.execute('SELECT request_id FROM release_lease').fetchone()[0])
        for token in self.tokens.values():
            self.assertNotIn(token, json.dumps([result, stored, requests, events]))
            self.assertNotIn(token.encode(), self.release_db.read_bytes())

    def test_trusted_error_id_only_when_present_and_valid(self):
        for value in (None, 'bad', 'A' * 32, 'a' * 33, 'a' * 32 + '\n', 7, {'request_id': 'a' * 32}):
            error = registry.ControlError('bounded ' + 'x' * 300, 503)
            if value is not None:
                error.request_id = value
            response = asyncio.run(self.main.control_error(None, error))
            body = json.loads(response.body) if self.real_fastapi else response.content
            self.assertEqual({'detail'}, set(body))
            self.assertEqual(240, len(body['detail']))
        error = registry.ControlError('withheld', 503)
        error.request_id = 'a' * 32
        response = asyncio.run(self.main.control_error(None, error))
        body = json.loads(response.body) if self.real_fastapi else response.content
        self.assertEqual({'detail': 'withheld', 'request_id': 'a' * 32}, body)

    def test_permissions_expiry_disable_and_stale_auth_rechecked(self):
        reads = ('/api/releases/versions', '/api/releases/plans', '/api/releases/requests',
                 '/api/releases/audit', '/api/manage/audit', '/api/manage/report')
        for path in reads:
            self.api('GET', path, name=None, expected=401)
        self.api('POST', '/api/releases/versions', {'manifest': self.manifest()}, name='viewer', expected=403)
        auth = self.auth('viewer')
        security.grant('viewer', 'view', revoke=True)
        for path in reads:
            self.api('GET', path, name='viewer', expected=403)
        self.error(403, self.main.audit, auth)
        self.error(403, self.main.release_versions, auth)
        security.grant('viewer', 'view')
        with store.transaction() as db:
            db.execute('UPDATE cp_sessions SET expires=0 WHERE digest=?', (security.digest_token(self.tokens['viewer']),))
        self.api('GET', '/api/releases/plans', name='viewer', expected=401)
        security.disable('otherdev')
        self.api('GET', '/api/releases/versions', name='otherdev', expected=401)
        stale_author = self.auth()
        security.grant('author', 'develop', revoke=True)
        self.error(403, self.main.register_release_version, {'manifest': self.manifest()}, stale_author)

    def test_missing_release_storage_is_503_and_never_initialized_by_http(self):
        missing = self.root / 'release/missing.sqlite'
        with mock.patch.dict(os.environ, {'CONTROL_RELEASE_DB_PATH': str(missing)}):
            self.api('GET', '/api/releases/versions', expected=503)
            self.api('POST', '/api/releases/versions', {'manifest': self.manifest()}, expected=503)
            self.assertFalse(missing.exists())
            self.assertTrue(self.api('GET', '/health', name=None)['release_workflow_implemented'])
        for path in ('/api/releases/initialize', '/api/releases/attest', '/api/releases/executor',
                     '/api/releases/callback', '/api/releases/success', '/api/releases/plans/' + 'a' * 32 + '/success'):
            self.api('POST', path, {}, expected=404)

    def test_management_audit_reads_real_rows_bounded_and_redacts_secrets(self):
        revision = self.main.create_revision({'component': 'gateway', 'config': CONFIG}, self.auth())
        self.main.submit_revision(revision['id'], {'expected_version': 1}, self.auth())
        rows = self.api('GET', '/api/manage/audit', name='viewer')['audit']
        event = next(row for row in rows if row['event'] == 'policy.submit' and row['target'] == revision['id'])
        self.assertEqual(self.users['author']['id'], event['actor'])
        self.assertEqual(2, event['metadata']['version'])
        with store.transaction() as db:
            for number in range(520):
                store.audit(db, self.users['author']['id'], 'workspace.save', '%032x' % number,
                            {'workspace': '%032x' % number, 'sha256': 'c' * 64})
            # Simulate unsafe historical data, not a browser write or new policy.
            token = self.tokens['author']
            metadata = json.dumps({'token': token, 'authorization': 'Bearer ' + token, 'password': PASSWORD,
                                   'source': 'private source', 'config': CONFIG, 'permission': token,
                                   'component': token, 'revision': token, 'kind': token, 'version': True,
                                   'hash': 'd' * 64})
            db.execute('INSERT INTO cp_audit(actor,event,target,metadata,created) VALUES(?,?,?,?,?)',
                       (token, token, token, metadata, int(time.time())))
        rows = self.api('GET', '/api/manage/audit', name='viewer')['audit']
        self.assertEqual(512, len(rows))
        self.assertEqual(sorted((row['id'] for row in rows), reverse=True), [row['id'] for row in rows])
        self.assertEqual({'hash': 'd' * 64}, rows[0]['metadata'])
        self.assertEqual('[redacted]', rows[0]['actor'])
        self.assertEqual('[redacted]', rows[0]['event'])
        self.assertEqual('[redacted]', rows[0]['target'])
        encoded = json.dumps(rows)
        for secret in list(self.tokens.values()) + [PASSWORD, 'private source', 'request_timeout_s', 'Bearer ']:
            self.assertNotIn(secret, encoded)
        for invalid in ('not JSON', '{"version":1,"version":2}', '{"version":NaN}', '[' + '0' * 2049 + ']'):
            self.assertEqual({}, self.main._audit_metadata(invalid))

    def test_report_actual_snapshot_flags_and_unobserved_controller(self):
        auth = self.auth('viewer')
        with mock.patch.object(self.main, 'controller_client', side_effect=registry.ControlError('unavailable', 503)):
            expected = self.main.components(auth)
            response = self.api('GET', '/api/manage/report', name='viewer')
        self.assertEqual({'timestamp', 'snapshot', 'historical_reports_available', 'alerts_available'}, set(response))
        self.assertIs(type(response['timestamp']), int)
        self.assertLessEqual(abs(int(time.time()) - response['timestamp']), 2)
        self.assertEqual(expected, response['snapshot'])
        self.assertFalse(response['historical_reports_available'])
        self.assertFalse(response['alerts_available'])
        self.assertFalse(response['snapshot']['controller_observed'])
        self.assertTrue(response['snapshot']['release_workflow_implemented'])
        self.assertFalse(response['snapshot']['publish_enabled'])
        self.assertTrue(all(item['runtime_state'] == {'state': 'unobserved'} for item in response['snapshot']['components']))
        client = mock.Mock()
        client.request.return_value = {'runtime': {'runtimes': {'backend': {'state': 'stopped', 'running': False}},
                                                  'test': {'ready': False}, 'build': {'ready': False}}}
        with mock.patch.object(self.main, 'controller_client', return_value=client):
            snapshot = self.api('GET', '/api/manage/report', name='viewer')['snapshot']
        client.request.assert_called_once_with('GET', '/v1/status', {}, self.tokens['viewer'])
        actual = next(item for item in snapshot['components'] if item['id'] == 'card_workflow')
        self.assertEqual({'state': 'stopped', 'running': False}, actual['runtime_state'])
        self.assertTrue(actual['runtime_observed'])
        health = self.api('GET', '/health', name=None)
        self.assertTrue(health['release_workflow_implemented'])
        self.assertFalse(health['publish_enabled'])

    def test_config_dev_ops_integration_retains_existing_contracts(self):
        baseline = {'control_plane/registry.py': '# trusted source marker\n',
                    'common/__init__.py': '# trusted imports\n',
                    'application_hub/main.py': '# trusted hub baseline\n',
                    'card_workflow/main.py': 'VALUE = 1\n'}
        for path, content in baseline.items():
            target = self.root / 'source' / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text(content)
        revision = self.main.create_revision({'component': 'card_workflow', 'config': CONFIG}, self.auth())
        revision = self.main.submit_revision(revision['id'], {'expected_version': 1}, self.auth())
        revision = self.main.review_revision(revision['id'], {'expected_version': 2, 'decision': 'approved'}, self.auth('reviewer'))
        self.main.activate_revision(revision['id'], {'expected_version': 3}, self.auth())
        info = self.api('POST', '/api/dev/workspaces', {'component': 'card_workflow'}, expected=201)
        self.assertFalse(info['publish_enabled'])
        self.assertEqual(info['id'], workspace.describe(info['id'], self.users['author']['id'])['id'])
        action = {'component': 'card_workflow', 'action': 'stop', 'expected_config_hash': registry.digest(CONFIG)}
        client = mock.Mock()
        client.request.return_value = {'state': 'not_enrolled', 'running': False}
        with mock.patch.object(self.main, 'controller_client', return_value=client):
            result = self.api('POST', '/api/manage/actions', action, name='operator')
        self.assertEqual('not_enrolled', result['state'])
        client.request.assert_called_once_with('POST', '/v1/actions', action, self.tokens['operator'])
        self.api('POST', '/api/manage/actions', dict(action, callback='https://invalid'), name='operator', expected=422)
        self.api('POST', '/api/manage/actions', dict(action, expected_config_hash='0' * 64), name='operator', expected=409)

    def test_rollback_real_attested_lineage_preserves_business_and_execution_blocker(self):
        old = self.attested_version(image='sha256:' + 'a' * 64)
        self.approved(old)
        current = self.attested_version(label='r6.0.2', previous=old['id'], image='sha256:' + 'b' * 64)
        self.approved(current)
        business = self.root / 'business/app.sqlite'
        db = sqlite3.connect(str(business))
        try:
            with db:
                db.execute('CREATE TABLE records (text TEXT)')
                db.execute("INSERT INTO records VALUES('retain new business record')")
        finally:
            db.close()
        before = business.read_bytes()
        value = self.plan_input(old['id'])
        window = {key: value[key] for key in ('planned_at', 'expires_at')}
        rollback = self.api('POST', '/api/releases/rollback-plans',
                            {'version_id': old['id'], 'current_version_id': current['id'], 'window': window}, expected=201)
        self.assertEqual(old['hash'], rollback['payload']['version_sha256'])
        self.assertEqual(current['hash'], rollback['payload']['current_version_sha256'])
        self.assertEqual(old['manifest']['image_digest'], rollback['payload']['image_digest'])
        self.assertTrue(rollback['payload']['preserve_business_records'])
        self.assertFalse(rollback['payload']['data_rollback'])
        self.assertFalse(rollback['execution_ready'])
        self.assertFalse(rollback['image_verified'])
        path = '/api/releases/plans/' + rollback['id']
        self.api('POST', path + '/submit', {'expected_version': 1})
        approved = self.api('POST', path + '/review', {'expected_version': 2, 'decision': 'approve'}, name='reviewer')
        result = self.api('POST', path + '/execute', {'expected_version': approved['version']}, name='operator', expected=503)
        self.assertEqual('Execution withheld: release-executor-not-enrolled', result['detail'])
        stored = self.api('GET', path, name='viewer')
        self.assertEqual('release-executor-not-enrolled', stored['blocker'])
        self.assertFalse(stored['deployed'])
        self.assertEqual(before, business.read_bytes())

    def test_unaccepted_rollback_is_not_fabricated(self):
        old = self.version(image='sha256:' + 'a' * 64)
        current = self.version(label='r6.0.2', previous=old['id'], image='sha256:' + 'b' * 64)
        value = self.plan_input(old['id'])
        self.api('POST', '/api/releases/rollback-plans',
                 {'version_id': old['id'], 'current_version_id': current['id'],
                  'window': {key: value[key] for key in ('planned_at', 'expires_at')}}, expected=409)
        self.assertEqual([], self.api('GET', '/api/releases/plans')['plans'])

    def test_release_boundary_origin_framing_bytes_and_duplicate_json(self):
        version = self.version()
        route = '/api/releases/plans'
        normal = [(b'host', b'manager.test:18126'), (b'origin', ORIGIN.encode()),
                  (b'content-type', b'application/json'),
                  (b'authorization', ('Bearer ' + self.tokens['author']).encode())]
        cases = [
            ({'body': b'{"plan":{},"plan":{}}'}, 422),
            ({'body': b'{"plan":{"owner":1,"owner":2}}'}, 422),
            ({'body': b'{"plan":{"planned_at":NaN}}'}, 422),
            ({'body': b'{"plan":"\\ud800"}'}, 422),
            ({'body': b'[]'}, 422),
            ({'chunks': [b'x' * 32768, b'x' * 32769]}, 413),
            ({'headers': [(b'host', b'evil.test')]}, 400),
            ({'headers': [item for item in normal if item[0] != b'origin']}, 403),
            ({'headers': [(k, b'https://evil.test' if k == b'origin' else v) for k, v in normal]}, 403),
            ({'headers': normal + [(b'authorization', b'Bearer duplicate')]}, 400),
            ({'headers': normal + [(b'content-length', b'2'), (b'transfer-encoding', b'chunked')]}, 400),
            ({'headers': normal + [(b'content-length', b'2')]}, 400),
            ({'headers': normal + [(b'content-encoding', b'gzip')]}, 415),
            ({'headers': [(k, b'text/plain' if k == b'content-type' else v) for k, v in normal]}, 415),
        ]
        for arguments, status in cases:
            with self.subTest(arguments=arguments):
                self.api('POST', route, {'plan': self.plan_input(version['id'])}, expected=status, **arguments)
        self.api('GET', '/api/releases/versions', expected=413, body=b'x')
        self.api('DELETE', '/api/releases/versions', expected=405)
        self.assertEqual([], self.api('GET', '/api/releases/plans')['plans'])
        with mock.patch.dict(os.environ, {'APP_ORIGIN': ''}):
            self.api('GET', '/api/releases/versions', expected=503)


if __name__ == '__main__':
    unittest.main(verbosity=2)
