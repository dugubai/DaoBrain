"""Gateway backend contracts. HTTP and Ollama are mocked; no real CPU inference is claimed."""
from contextlib import contextmanager
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import pytest

from common.catalog import EXTERNAL_LABEL, OLLAMA_LABEL, catalog, extract, search
from common.config import Settings, get_settings
from common.schemas import Answer, VerificationResult, Zone
from gateway import main

_REAL_CLIENT = httpx.Client
API_HEADERS = {'X-API-Key': 'gateway-route-api-test'}
LOCAL_TRACE = ['hybrid_iface', 'rag_green', 'local_model', 'output_verify']
EXTERNAL_TRACE = ['agent_orchestrator', 'rag_public', 'external_model', 'output_verify']


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    for name, value in {
        'API_KEY': API_HEADERS['X-API-Key'], 'INTERNAL_API_KEY': 'gateway-route-internal-test',
        'APPROVAL_SECRET': 'gateway-route-approval-test',
        'ORCHESTRATOR_URL': 'http://agent_orchestrator:8002',
        'HYBRID_IFACE_URL': 'http://hybrid_iface:8004',
        'RAG_GREEN_URL': 'http://rag_green:8007', 'LOCAL_MODEL_URL': 'http://local_model:8006',
        'OUTPUT_VERIFY_URL': 'http://output_verify:8010',
        'LOCAL_MODEL_BACKEND': 'ollama', 'LOCAL_MODEL_NAME': 'qwen2.5:0.5b',
        'OLLAMA_URL': 'http://ollama:11434', 'MODEL_TIMEOUT_SECONDS': '120',
        # These transports return copy-format answers, independently of real CPU deployment.
        'MODEL_OUTPUT_PROTOCOL': 'copy', 'MODEL_CONTEXT_TOKENS': '4096',
        'MODEL_CPU_THREADS': '2',
        'MODEL_MAX_OUTPUT_TOKENS': '256', 'MAX_TOKENS_PER_TASK': '8000',
        'MAX_ITERATIONS_PER_TASK': '6', 'MAX_COST_CNY_PER_TASK': '0.50',
        'MAX_REQUESTS_PER_MINUTE': '30',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('GATEWAY_GREEN_BACKEND', raising=False)
    main._recent.clear()
    get_settings.cache_clear()
    with TestClient(main.app) as instance:
        yield instance
    main._recent.clear()
    get_settings.cache_clear()


def backend(monkeypatch, value):
    monkeypatch.setenv('GATEWAY_GREEN_BACKEND', value)
    get_settings.cache_clear()


def query(client, **changes):
    return client.post('/v1/query', headers=API_HEADERS,
        json={'query': '四时养生', 'zone': 'green', 'task_id': 'gateway-route-test', **changes})


def valid_answer(payload, local=True):
    evidence = search(payload['query'], 4, Zone.GREEN)
    answer, selected = extract(evidence, OLLAMA_LABEL if local else EXTERNAL_LABEL)
    return Answer(task_id=payload['task_id'], answer=answer,
        evidence=[e.provenance for e in selected], model='ollama:qwen2.5:0.5b' if local else 'external-stub',
        trace=LOCAL_TRACE if local else EXTERNAL_TRACE,
        verification=VerificationResult(task_id=payload['task_id'], status='pass',
            provenance_ok=True, compliance_ok=True)).model_dump(mode='json')


@contextmanager
def mocked_dispatch(handler):
    def post(url, json):
        data = handler(url, json)
        return httpx.Response(200, json=data, request=httpx.Request('POST', url))
    with patch.object(main.httpx, 'Client') as outbound:
        outbound.return_value.__enter__.return_value.post.side_effect = post
        yield outbound


def test_default_gateway_preserves_external_stub_path(client, monkeypatch):
    monkeypatch.setenv('ENERGY_LENS_ENABLED', '0')
    assert get_settings().gateway_green_backend == 'external_stub'

    def handler(url, payload):
        assert url == 'http://agent_orchestrator:8002/plan'
        return valid_answer(payload, local=False)

    with mocked_dispatch(handler) as outbound:
        response = query(client)
        assert outbound.call_args.kwargs == {
            'timeout': 120, 'trust_env': False, 'follow_redirects': False,
            'headers': {'X-Internal-Key': 'gateway-route-internal-test'},
        }
    assert response.status_code == 200 and not response.json()['refused']
    assert response.json()['trace'] == ['gateway'] + EXTERNAL_TRACE


def test_local_green_routes_to_hybrid_not_orchestrator(client, monkeypatch):
    backend(monkeypatch, 'local_ollama')

    def handler(url, payload):
        assert url == 'http://hybrid_iface:8004/dispatch'
        assert payload['user_id'] == 'simulation-user' and payload['gate_approved'] is False
        assert payload['zone'] == 'green'
        return valid_answer(payload)

    with mocked_dispatch(handler) as outbound:
        response = query(client, gate_approved=True, user_id='forged-client-identity')
        assert outbound.return_value.__enter__.return_value.post.call_count == 1
        assert outbound.call_args.kwargs['follow_redirects'] is False
    assert response.status_code == 200, response.text
    assert response.json()['model'] == 'ollama:qwen2.5:0.5b'
    assert response.json()['trace'] == ['gateway'] + LOCAL_TRACE
    assert response.json()['verification']['status'] == 'pass'


@pytest.mark.parametrize('value', ['', 'unknown', 'stub', 'LOCAL_OLLAMA'])
def test_unknown_green_backend_503_without_fallback(client, monkeypatch, value):
    backend(monkeypatch, value)
    with patch.object(main.httpx, 'Client') as outbound:
        response = query(client)
        outbound.assert_not_called()
    assert response.status_code == 503 and 'answer' not in response.json()


@pytest.mark.parametrize('value', ['external_stub', 'local_ollama', 'unknown'])
@pytest.mark.parametrize('payload', [
    {'zone': 'unknown'}, {'query': '四时养生 私人病历'}, {'query': '四时养生 未知主题'},
])
def test_unknown_zone_or_nonpublic_query_refused_before_dispatch(client, monkeypatch, value, payload):
    backend(monkeypatch, value)
    with patch.object(main.httpx, 'Client') as outbound:
        response = query(client, **payload)
        outbound.assert_not_called()
    data = response.json()
    assert response.status_code == 200 and data['refused'] and not data['answer']
    assert data['trace'] == ['gateway'] and data['verification']['status'] == 'reject'


@pytest.mark.parametrize('value', ['external_stub', 'local_ollama', 'unknown'])
def test_red_dispatch_and_final_refusal_unchanged(client, monkeypatch, value):
    backend(monkeypatch, value)

    def handler(url, payload):
        assert url == 'http://hybrid_iface:8004/dispatch' and payload['zone'] == 'red'
        evidence = catalog(Zone.RED)[:1]
        return Answer(task_id=payload['task_id'], answer=evidence[0].text,
            evidence=[e.provenance for e in evidence], model='untrusted-red', trace=['hybrid_iface'],
            verification=VerificationResult(task_id=payload['task_id'], status='pass',
                provenance_ok=True, compliance_ok=True)).model_dump(mode='json')

    with mocked_dispatch(handler):
        response = query(client, query='虚构流程甲', zone='red', gate_approved=True)
    data = response.json()
    assert response.status_code == 200 and data['refused'] and data['model'] == 'none'
    assert not data['answer'] and not data['evidence']


@pytest.mark.parametrize('tamper', ['task', 'verify-task', 'provenance', 'unsupported',
                                   'evidence', 'refused', 'verify', 'provenance-ok', 'compliance-ok'])
def test_local_final_canonical_and_quote_checks_still_reject(client, monkeypatch, tamper):
    backend(monkeypatch, 'local_ollama')

    def handler(url, payload):
        data = valid_answer(payload)
        if tamper == 'task':
            data['task_id'] = 'different-task'
        elif tamper == 'verify-task':
            data['verification']['task_id'] = 'different-task'
        elif tamper == 'provenance':
            data['evidence'][0]['version'] = 'forged-version'
        elif tamper == 'unsupported':
            data['answer'] += '\n未经支持的诊疗建议'
        elif tamper == 'evidence':
            data['evidence'] = []
        elif tamper == 'refused':
            data['refused'] = True
        elif tamper == 'verify':
            data['verification']['status'] = 'reject'
        else:
            data['verification']['provenance_ok' if tamper == 'provenance-ok' else 'compliance_ok'] = False
        return data

    with mocked_dispatch(handler):
        response = query(client)
    data = response.json()
    assert response.status_code == 200 and data['refused'] and not data['answer']
    assert data['model'] == 'none' and not data['evidence']


@pytest.mark.parametrize('code', [301, 302, 307, 308, 500, 503])
def test_local_redirect_or_dependency_failure_503_no_fallback(client, monkeypatch, code):
    backend(monkeypatch, 'local_ollama')
    with patch.object(main.httpx, 'Client') as outbound:
        outbound.return_value.__enter__.return_value.post.return_value = httpx.Response(
            code, headers={'Location': 'http://external.invalid/steal'},
            request=httpx.Request('POST', 'http://hybrid_iface:8004/dispatch'))
        response = query(client)
        assert outbound.return_value.__enter__.return_value.post.call_count == 1
        assert outbound.call_args.kwargs['follow_redirects'] is False
    assert response.status_code == 503 and 'answer' not in response.json()


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ConnectError])
def test_local_timeout_503_without_fake_response_or_retry(client, monkeypatch, error):
    backend(monkeypatch, 'local_ollama')
    with patch.object(main.httpx, 'Client') as outbound:
        outbound.return_value.__enter__.return_value.post.side_effect = error('PRIVATE-ERROR-SECRET')
        response = query(client)
        assert outbound.return_value.__enter__.return_value.post.call_count == 1
    assert response.status_code == 503 and 'PRIVATE-ERROR-SECRET' not in response.text
    assert 'answer' not in response.json()


@pytest.mark.parametrize('headers', [{}, {'X-API-Key': 'wrong'}])
def test_gateway_auth_preserved(client, headers):
    with patch.object(main.httpx, 'Client') as outbound:
        response = client.post('/v1/query', json={'query': '四时养生', 'zone': 'green'}, headers=headers)
        outbound.assert_not_called()
    assert response.status_code == 401


def test_gateway_unconfigured_key_and_rate_limit_fail_before_dispatch(client, monkeypatch):
    main._recent.extend([main.time.monotonic()] * 30)
    with patch.object(main.httpx, 'Client') as outbound:
        assert query(client).status_code == 429
        monkeypatch.setenv('API_KEY', '')
        get_settings.cache_clear()
        assert query(client).status_code == 503
        outbound.assert_not_called()


def test_in_process_gateway_hybrid_ollama_route_mock_only(client, monkeypatch):
    """Execute real route handlers but mock every transport, including Ollama generation."""
    from hybrid_iface.main import app as hybrid_app
    from local_model.main import app as local_app
    from output_verify.main import app as verify_app
    from rag_green.main import app as rag_app

    backend(monkeypatch, 'local_ollama')
    monkeypatch.setenv('ENERGY_LENS_ENABLED', '0')
    seen = []
    expected, _ = extract(search('四时养生', 4, Zone.GREEN), OLLAMA_LABEL)
    with TestClient(hybrid_app) as hybrid, TestClient(rag_app) as rag, \
            TestClient(local_app) as local, TestClient(verify_app) as verify:
        apps = {'hybrid_iface': hybrid, 'rag_green': rag, 'local_model': local, 'output_verify': verify}

        def handler(request):
            seen.append((request.url.host, request.url.path))
            payload = json.loads(request.content)
            if request.url.host == 'ollama':
                assert request.url.path == '/api/generate'
                assert payload['model'] == 'qwen2.5:0.5b' and payload['stream'] is False
                assert 'X-API-Key' not in request.headers and 'X-Internal-Key' not in request.headers
                return httpx.Response(200, json={'model': 'qwen2.5:0.5b', 'done': True, 'response': expected})
            assert request.url.host in apps  # No external or public fallback may be contacted.
            assert request.headers['X-Internal-Key'] == 'gateway-route-internal-test'
            assert 'X-API-Key' not in request.headers
            response = apps[request.url.host].post(request.url.path, json=payload,
                headers={'X-Internal-Key': request.headers['X-Internal-Key']})
            return httpx.Response(response.status_code, content=response.content)

        def factory(*args, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
            return _REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, 'Client', factory)
        response = query(client)
    assert response.status_code == 200, response.text
    assert response.json()['trace'] == ['gateway'] + LOCAL_TRACE
    assert response.json()['model'] == 'ollama:qwen2.5:0.5b' and not response.json()['refused']
    assert seen == [('hybrid_iface', '/dispatch'), ('rag_green', '/retrieve'),
                    ('local_model', '/infer'), ('ollama', '/api/generate'), ('output_verify', '/verify')]


def _green_harness(client, monkeypatch, interp_response):
    """Drive the real green HIT route in-process; every transport (incl. Ollama) is mocked.
    The answer-generation Ollama call uses LOCAL_MODEL_NAME; the lens interpretation call uses
    OPEN_REASON_MODEL (qwen-lens), so the mock can tell them apart by request payload['model']."""
    from hybrid_iface.main import app as hybrid_app
    from local_model.main import app as local_app
    from output_verify.main import app as verify_app
    from rag_green.main import app as rag_app

    backend(monkeypatch, 'local_ollama')
    monkeypatch.setenv('OPEN_REASON_MODEL', 'qwen-lens')
    monkeypatch.delenv('ENERGY_LENS_ENABLED', raising=False)  # default ON
    get_settings.cache_clear()
    expected, _ = extract(search('四时养生', 4, Zone.GREEN), OLLAMA_LABEL)
    seen = []

    with TestClient(hybrid_app) as hybrid, TestClient(rag_app) as rag, \
            TestClient(local_app) as local, TestClient(verify_app) as verify:
        apps = {'hybrid_iface': hybrid, 'rag_green': rag, 'local_model': local, 'output_verify': verify}

        def handler(request):
            seen.append((request.url.host, request.url.path))
            payload = json.loads(request.content)
            if request.url.host == 'ollama':
                assert request.url.path == '/api/generate'
                if payload['model'] == 'qwen-lens':
                    return httpx.Response(200, json={
                        'model': 'qwen-lens', 'done': True, 'response': interp_response})
                return httpx.Response(200, json={
                    'model': 'qwen2.5:0.5b', 'done': True, 'response': expected})
            assert request.url.host in apps
            assert request.headers['X-Internal-Key'] == 'gateway-route-internal-test'
            response = apps[request.url.host].post(request.url.path, json=payload,
                headers={'X-Internal-Key': request.headers['X-Internal-Key']})
            return httpx.Response(response.status_code, content=response.content)

        def factory(*args, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
            return _REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, 'Client', factory)
        response = query(client)
    return response, expected, seen


def test_green_hit_appends_energy_interpretation_without_touching_answer(client, monkeypatch):
    """绿区命中：answer / 证据 / trace 必须与核验原文逐字一致；能量模型解读另存 interpretation 字段。"""
    interp_text = ('依天干取象，此段属木气敷和、'
                   '春生之令，聊备一家之言。')
    response, expected, seen = _green_harness(client, monkeypatch, interp_text)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['answer'] == expected
    assert body['trace'] == ['gateway'] + LOCAL_TRACE
    assert body['model'] == 'ollama:qwen2.5:0.5b' and not body['refused']
    assert body['interpretation'] == interp_text  # 独立字段，不并入逐字核验的 answer
    # 解读确为第二次 Ollama 生成（绿区答案 + 解读各一次）
    assert [s for s in seen if s == ('ollama', '/api/generate')] == \
        [('ollama', '/api/generate'), ('ollama', '/api/generate')]


def test_green_hit_drops_medical_interpretation(client, monkeypatch):
    """能量模型解读若触碰诊疗红线，必须整段丢弃（interpretation=''），已核验答案不受影响。"""
    med_text = '此方可治愈失眠，建议用药，剂量为每日三次。'
    response, expected, seen = _green_harness(client, monkeypatch, med_text)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['answer'] == expected and not body['refused']
    assert body['interpretation'] == ''  # 诊疗红线：解读被丢弃，绝不写入
