"""公网固定样例入口契约：不需要浏览器凭证，不接任意业务请求。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import base64
import hashlib
import json as jsonlib
from pathlib import Path
import re
import shutil
import subprocess
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import pytest

from common.catalog import EXTERNAL_LABEL, OLLAMA_LABEL, extract, search
from common.schemas import Answer, VerificationResult, Zone
from public_demo import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('API_KEY', 'SERVER-ONLY-TEST-KEY')
    monkeypatch.delenv('PUBLIC_DEMO_BACKEND', raising=False)
    monkeypatch.delenv('PUBLIC_DEMO_MODEL_NAME', raising=False)
    main._calls.clear()
    main._active = 0
    with TestClient(main.app, base_url='http://39.102.49.1') as instance:
        yield instance
    main._calls.clear()
    main._active = 0


HEADERS = {'Origin': 'http://39.102.49.1'}


@pytest.mark.parametrize('payload', [
    {'sample_id': 'yin-yang', 'query': '私人病历'},
    {'sample_id': 'yin-yang', 'zone': 'red'},
    {'sample_id': 'yin-yang', 'api_key': 'browser-supplied-key'},
    {'sample_id': 'yin-yang', 'image_base64': 'eA=='},
    {'sample_id': 'yin-yang', 'file_url': 'https://example.invalid/private'},
    {'sample_id': 'http://example.org'},
    {'query': '法于阴阳'},
    {'sample_id': 'unknown'},
])
def test_no_free_input(client, payload):
    with patch.object(main.httpx, 'Client') as outbound:
        response = client.post('/api/demo', json=payload, headers=HEADERS)
    assert response.status_code == 422
    outbound.assert_not_called()


@pytest.mark.parametrize('origin', [None, 'https://evil.example', 'http://39.102.49.1.evil.example'])
def test_origin_denied(client, origin):
    headers = {} if origin is None else {'Origin': origin}
    assert client.post('/api/demo', json={'sample_id': 'seasons'}, headers=headers).status_code == 403


def test_hidden_routes_and_host(client):
    for path in ['/v1/query', '/dispatch', '/docs', '/openapi.json', '/gate']:
        assert client.get(path).status_code == 404
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 400
    page = client.get('/')
    assert page.status_code == 200 and 'SERVER-ONLY-TEST-KEY' not in page.text
    assert 'sha256-' in page.headers['Content-Security-Policy']
    assert 'localStorage' not in page.text and '<textarea' not in page.text


def test_size_and_type(client):
    assert client.post('/api/demo', content='x' * 300,
        headers={**HEADERS, 'Content-Type': 'application/json'}).status_code == 413
    assert client.post('/api/demo', content='abc', headers=HEADERS).status_code == 415


def test_fixed_server_request_and_validated_result(client):
    @contextmanager
    def stream(method, url, headers, json):
        assert method == 'POST' and url == 'http://gateway:8000/v1/query'
        assert headers == {'X-API-Key': 'SERVER-ONLY-TEST-KEY'}
        assert json['query'] == main.SAMPLES['seasons'] and json['zone'] == 'green'
        evidence = search(json['query'], 4, Zone.GREEN)
        answer, selected = extract(evidence, EXTERNAL_LABEL)
        result = Answer(task_id=json['task_id'], answer=answer,
            evidence=[e.provenance for e in selected], simulated=True, model='external-stub',
            note='SERVER-ONLY-TEST-KEY',
            verification=VerificationResult(task_id=json['task_id'], status='pass',
                provenance_ok=True, compliance_ok=True, hallucination_risk=0,
                reasons=['SERVER-ONLY-TEST-KEY']),
            trace=['gateway', 'agent_orchestrator', 'rag_public', 'external_model', 'output_verify'])
        yield httpx.Response(200, content=result.model_dump_json().encode(),
                             request=httpx.Request(method, url))
    with patch.object(main.httpx, 'Client') as outbound:
        outbound.return_value.__enter__.return_value.stream.side_effect = stream
        response = client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS)
        assert outbound.call_args.kwargs['follow_redirects'] is False
        assert outbound.call_args.kwargs['trust_env'] is False
    assert response.status_code == 200, response.text
    assert response.json()['verification']['status'] == 'pass'
    assert 'SERVER-ONLY-TEST-KEY' not in response.text
    assert main._active == 0


def test_redirect_and_invalid_response_never_released(client):
    for code, body in [(302, b''), (200, b'{"answer":"unverified"}')]:
        @contextmanager
        def stream(*args, **kwargs):
            yield httpx.Response(code, content=body,
                request=httpx.Request('POST', 'http://gateway:8000/v1/query'))
        with patch.object(main.httpx, 'Client') as outbound:
            outbound.return_value.__enter__.return_value.stream.side_effect = stream
            response = client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS)
        assert response.status_code == 503 and 'SERVER-ONLY-TEST-KEY' not in response.text
        assert main._active == 0


def test_rate_limit_without_outbound(client):
    main._calls.extend([main.time.monotonic()] * 12)
    with patch.object(main.httpx, 'Client') as outbound:
        assert client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS).status_code == 429
        outbound.assert_not_called()


LOCAL_TRACE = ['gateway', 'hybrid_iface', 'rag_green', 'local_model', 'output_verify']
EXTERNAL_TRACE = ['gateway', 'agent_orchestrator', 'rag_public', 'external_model', 'output_verify']
SECRETS = ['SERVER-ONLY-TEST-KEY', 'INTERNAL-ONLY-TEST-KEY', 'APPROVAL-ONLY-TEST-KEY',
           'PG-ONLY-TEST-PASSWORD']


def demo_result(payload, model='external-stub'):
    """Mocked service answer only; this is not a real CPU model measurement."""
    evidence = search(payload['query'], 4, Zone.GREEN)
    answer, selected = extract(evidence, OLLAMA_LABEL if model.startswith('ollama:') else EXTERNAL_LABEL)
    return Answer(task_id=payload['task_id'], answer=answer,
        evidence=[e.provenance for e in selected], model=model,
        trace=LOCAL_TRACE if model.startswith('ollama:') else EXTERNAL_TRACE,
        note=' '.join(SECRETS),
        verification=VerificationResult(task_id=payload['task_id'], status='pass',
            provenance_ok=True, compliance_ok=True, hallucination_risk=0.7,
            reasons=SECRETS)).model_dump(mode='json')


@contextmanager
def mocked_gateway(handler):
    @contextmanager
    def stream(method, url, headers, json):
        assert method == 'POST' and url == 'http://gateway:8000/v1/query'
        assert headers == {'X-API-Key': SECRETS[0]}
        assert set(json) == {'query', 'zone', 'task_id'} and json['zone'] == 'green'
        assert json['query'] in main.SAMPLES.values()
        body = handler(json)
        if isinstance(body, dict):
            body = jsonlib.dumps(body).encode()
        yield httpx.Response(200, content=body, request=httpx.Request(method, url))
    with patch.object(main.httpx, 'Client') as outbound:
        outbound.return_value.__enter__.return_value.stream.side_effect = stream
        yield outbound


@pytest.mark.parametrize('sample_id', ['yin-yang', 'inner-calm', 'seasons'])
@pytest.mark.parametrize('model_name', [None, 'qwen2.5:0.5b-custom'])
def test_local_backend_exact_model_and_trace_no_secret_release(client, monkeypatch, sample_id, model_name):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', 'local_ollama')
    if model_name is not None:
        monkeypatch.setenv('PUBLIC_DEMO_MODEL_NAME', model_name)
    for name, value in zip(['INTERNAL_API_KEY', 'APPROVAL_SECRET', 'POSTGRES_PASSWORD'], SECRETS[1:]):
        monkeypatch.setenv(name, value)
    expected_model = 'ollama:' + (model_name or 'qwen2.5:0.5b')
    with mocked_gateway(lambda payload: demo_result(payload, expected_model)) as outbound:
        response = client.post('/api/demo', json={'sample_id': sample_id}, headers=HEADERS)
        assert outbound.call_args.kwargs == {'timeout': 120, 'trust_env': False, 'follow_redirects': False}
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['model'] == expected_model and data['trace'] == LOCAL_TRACE
    assert data['simulated'] is True  # Lexical RAG/rule verification are still a simulation.
    assert data['verification']['reasons'] == ['固定公开样例的出处及摘录格式已核验']
    assert data['verification']['hallucination_risk'] == 0
    assert all(secret not in response.text for secret in SECRETS)
    assert main._active == 0


@pytest.mark.parametrize('backend', ['external_stub', 'local_ollama'])
@pytest.mark.parametrize('tamper', ['model', 'trace', 'task', 'verify-task', 'provenance',
                                   'quotes', 'refused', 'verify', 'provenance-ok', 'compliance-ok'])
def test_backend_mismatch_or_unverified_content_never_released(client, monkeypatch, backend, tamper):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', backend)
    model = 'ollama:qwen2.5:0.5b' if backend == 'local_ollama' else 'external-stub'

    def altered(payload):
        data = demo_result(payload, model)
        if tamper == 'model':
            data['model'] = 'local-extract-simulation' if backend == 'local_ollama' else 'ollama:qwen2.5:0.5b'
        elif tamper == 'trace':
            data['trace'] = EXTERNAL_TRACE if backend == 'local_ollama' else LOCAL_TRACE
        elif tamper == 'task':
            data['task_id'] = 'wrong-task'
        elif tamper == 'verify-task':
            data['verification']['task_id'] = 'wrong-task'
        elif tamper == 'provenance':
            data['evidence'][0]['locator'] = SECRETS[1]
        elif tamper == 'quotes':
            data['answer'] += '\n' + SECRETS[0]
        elif tamper == 'refused':
            data['refused'] = True
        elif tamper == 'verify':
            data['verification']['status'] = 'reject'
        else:
            data['verification']['provenance_ok' if tamper == 'provenance-ok' else 'compliance_ok'] = False
        return data

    with mocked_gateway(altered):
        response = client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS)
    assert response.status_code == 503
    assert 'answer' not in response.json() and all(secret not in response.text for secret in SECRETS)
    assert main._active == 0


@pytest.mark.parametrize('model', ['ollama:wrong-model', 'ollama:', 'ollama:qwen2.5:0.5b ', 'external-stub'])
def test_local_backend_never_accepts_wrong_model(client, monkeypatch, model):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', 'local_ollama')
    with mocked_gateway(lambda payload: demo_result(payload, model)):
        assert client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS).status_code == 503
    assert main._active == 0


@pytest.mark.parametrize('backend', ['', 'unknown', 'stub', 'LOCAL_OLLAMA'])
def test_unconfigured_backend_never_dispatches(client, monkeypatch, backend):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', backend)
    with patch.object(main.httpx, 'Client') as outbound:
        response = client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS)
        outbound.assert_not_called()
    assert response.status_code == 503 and main._active == 0 and not main._calls


@pytest.mark.parametrize('name', ['', ' ', 'qwen2.5:0.5b ', 'x' * 129])
def test_local_unconfigured_model_never_dispatches(client, monkeypatch, name):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', 'local_ollama')
    monkeypatch.setenv('PUBLIC_DEMO_MODEL_NAME', name)
    with patch.object(main.httpx, 'Client') as outbound:
        assert client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS).status_code == 503
        outbound.assert_not_called()
    assert main._active == 0


@pytest.mark.parametrize('backend', ['external_stub', 'local_ollama'])
def test_missing_api_key_never_dispatches(client, monkeypatch, backend):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', backend)
    monkeypatch.delenv('API_KEY')
    with patch.object(main.httpx, 'Client') as outbound:
        assert client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS).status_code == 503
        outbound.assert_not_called()


@pytest.mark.parametrize('backend', ['external_stub', 'local_ollama'])
@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_or_failure_503_without_fake_answer_or_fallback(client, monkeypatch, backend, error):
    monkeypatch.setenv('PUBLIC_DEMO_BACKEND', backend)

    def failed(payload):
        raise error(SECRETS[0])

    with mocked_gateway(failed) as outbound:
        response = client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS)
        assert outbound.return_value.__enter__.return_value.stream.call_count == 1
    assert response.status_code == 503 and 'answer' not in response.json()
    assert all(secret not in response.text for secret in SECRETS) and main._active == 0


def test_oversized_response_fails_closed(client):
    with mocked_gateway(lambda payload: b'x' * 65537):
        assert client.post('/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS).status_code == 503
    assert main._active == 0


def test_global_single_concurrency_rejects_second_request(client):
    entered, release = Event(), Event()

    def blocked(payload):
        entered.set()
        assert release.wait(5), 'test did not release mocked gateway'
        return demo_result(payload)

    with mocked_gateway(blocked) as outbound, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, '/api/demo', json={'sample_id': 'seasons'}, headers=HEADERS)
        try:
            assert entered.wait(5)
            assert main._active == 1
            second = client.post('/api/demo', json={'sample_id': 'yin-yang'}, headers=HEADERS)
            assert second.status_code == 429
            assert outbound.return_value.__enter__.return_value.stream.call_count == 1
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert main._active == 0


def test_page_truthful_dynamic_status_csp_and_no_unsafe_sinks(client):
    response = client.get('/')
    html = response.text
    assert '尚未接入真实模型' not in html
    assert '本地小模型证据选取' in html and '公开/合成样例词法RAG与规则验证' in html
    assert '不是训练后专科模型/真实OCR/生产系统' in html and '不提供诊疗' in html
    assert '125000' in html and "data.model.startsWith('ollama:')" in html
    assert set(re.findall(r'data-sample="([^"]+)"', html)) == set(main.SAMPLES)
    for script in re.findall(r'<script>(.*?)</script>', html, re.S):
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert "'sha256-" + digest + "'" in response.headers['Content-Security-Policy']
    for unsafe in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write',
                   'localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', '<input', '<textarea'):
        assert unsafe not in html
    assert response.headers['Cache-Control'] == 'no-store'


def test_compose_only_server_gateway_key_and_explicit_backend_defaults():
    text = (Path(main.__file__).with_name('compose.yml')).read_text()
    assert 'PUBLIC_DEMO_BACKEND: ${PUBLIC_DEMO_BACKEND:-external_stub}' in text
    assert 'PUBLIC_DEMO_MODEL_NAME: ${PUBLIC_DEMO_MODEL_NAME:-qwen2.5:0.5b}' in text
    assert 'API_KEY: ${API_KEY:?' in text
    for forbidden in ('INTERNAL_API_KEY', 'APPROVAL_SECRET', 'POSTGRES', 'env_file:'):
        assert forbidden not in text


def test_page_javascript_dynamic_model_and_safe_rendering():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable for local JavaScript unit test')
    script = re.search(r'<script>([\s\S]*?)</script>', main.PAGE.read_text()).group(1)
    harness = r'''
const assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.textContent='';this.listeners={};this.dataset={};this.hidden=true;this.disabled=false;}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 addEventListener(name,fn){this.listeners[name]=fn;}
 set innerHTML(_){throw Error('unsafe DOM');}
}
const elements=new Map(), samples=['yin-yang','inner-calm','seasons'].map(id=>{const el=new Element();el.dataset.sample=id;return el;});
global.document={querySelectorAll:()=>samples,getElementById:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:()=>new Element()};
const realTimer=setTimeout;
global.setTimeout=(fn,ms)=>{assert.equal(ms,125000);return realTimer(fn,ms);};
let model='external-stub', trace=['gateway','agent_orchestrator','rag_public','external_model','output_verify'], code=200, request;
const attack='<img src=x onerror="alert(1)">';
global.fetch=async(path,options)=>{request={path,options};return {ok:code===200,status:code,json:async()=>({answer:attack,model,trace,simulated:true,refused:false,verification:{status:'pass',provenance_ok:true,compliance_ok:true},evidence:[{title:attack,quote:attack,source_id:'s',locator:'l',version:'v'}]})};};
'''
    assertions = r'''
(async()=>{
 const get=id=>document.getElementById(id);
 await samples[0].listeners.click();
 assert.equal(request.path,'./api/demo');assert.deepEqual(JSON.parse(request.options.body),{sample_id:'yin-yang'});
 assert.deepEqual(request.options.headers,{'Content-Type':'application/json'});assert.equal(request.options.redirect,'error');
 assert.equal(get('answer').textContent,attack);assert.equal(get('sources').children[0].children[0].textContent,attack);
 assert(get('verification').textContent.includes('未调用真实模型'));assert(get('trace').textContent.includes('external-stub'));
 model='ollama:qwen2.5:0.5b';trace=['gateway','hybrid_iface','rag_green','local_model','output_verify'];
 await samples[1].listeners.click();
 assert(get('verification').textContent.includes('本次已调用本地小模型'));assert(!get('verification').textContent.includes('未调用真实模型'));
 assert(get('trace').textContent.includes(model));assert(get('status').textContent.includes(model));
 assert.equal(get('result').hidden,false);assert(samples.every(el=>!el.disabled));
 trace=['gateway'];await samples[0].listeners.click();assert.equal(get('result').hidden,true);
 code=503;await samples[0].listeners.click();assert(get('status').textContent.includes('未发布结果'));assert.equal(get('result').hidden,true);
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', harness + script + assertions], capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
