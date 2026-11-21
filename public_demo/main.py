"""公网只接收固定样例ID；密钥留在服务器，不开放任意问题或其他业务接口。"""
from collections import deque
import base64
import hashlib
import os
from pathlib import Path
import re
from threading import Lock
import time
from typing import Literal
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import httpx
from pydantic import BaseModel, ConfigDict, Field

from common.catalog import search, supported_quotes
from common.schemas import Answer, VerificationResult, Zone

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['39.102.49.1', '127.0.0.1'])
PAGE = Path(__file__).with_name('index.html')
SAMPLES = {'yin-yang': '法于阴阳，和于术数', 'inner-calm': '恬淡虚无，真气从之', 'seasons': '四时养生'}
ORIGIN = 'http://39.102.49.1'
GW = os.environ.get('GATEWAY_URL', 'http://gateway:8000').rstrip('/')
_lock = Lock()
_calls = deque()
_active = 0
_scripts = re.findall(r'<script>(.*?)</script>', PAGE.read_text(), re.S)
CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src " + ' '.join(
    "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'" for s in _scripts
) + "; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class DemoInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample_id: Literal['yin-yang', 'inner-calm', 'seasons']


@app.middleware('http')
async def protect(request: Request, call_next):
    if request.method == 'POST':
        if request.headers.get('origin') != ORIGIN:
            return JSONResponse({'detail': '只接受公网样例页同源请求'}, status_code=403)
        if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
            return JSONResponse({'detail': '仅接受JSON样例编号'}, status_code=415)
        try:
            size = int(request.headers.get('content-length', '0'))
        except ValueError:
            size = 0
        if not 0 < size <= 4096:
            return JSONResponse({'detail': '请求体过大或为空'}, status_code=413)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = CSP
    return response


@app.get('/')
def page():
    return FileResponse(PAGE)


@app.get('/health')
def health():
    return {'status': 'ok', 'module': 'public_demo', 'simulated': True}


@app.post('/api/demo', response_model=Answer)
def demo(payload: DemoInput):
    global _active
    key = os.environ.get('API_KEY', '')
    if not key:
        raise HTTPException(503, '样例入口未就绪')
    backend = os.environ.get('PUBLIC_DEMO_BACKEND', 'external_stub')
    if backend == 'external_stub':
        expected_trace = ['gateway', 'agent_orchestrator', 'rag_public', 'external_model', 'output_verify']
        expected_model = 'external-stub'
    elif backend == 'local_ollama':
        model_name = os.environ.get('PUBLIC_DEMO_MODEL_NAME', 'qwen2.5:0.5b')
        if not model_name or len(model_name) > 128 or model_name != model_name.strip():
            raise HTTPException(503, '样例本地模型未配置；未启用回退')
        expected_trace = ['gateway', 'hybrid_iface', 'rag_green', 'local_model', 'output_verify']
        expected_model = 'ollama:' + model_name
    else:
        raise HTTPException(503, '样例后端未配置；未启用回退')
    with _lock:
        now = time.monotonic()
        while _calls and now - _calls[0] >= 60:
            _calls.popleft()
        if len(_calls) >= 12 or _active >= 1:
            raise HTTPException(429, '公开样例限流，请稍后再试')
        _calls.append(now)
        _active += 1
    try:
        query = SAMPLES[payload.sample_id]
        task_id = 'publicdemo-' + uuid.uuid4().hex
        with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
            with client.stream('POST', GW + '/v1/query',
                    headers={'X-API-Key': key},
                    json={'query': query, 'zone': 'green', 'task_id': task_id}) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError('oversized')
        result = Answer.model_validate_json(bytes(body))
        trusted = {e.provenance.source_id: e for e in search(query, 20, Zone.GREEN)}
        evidence = [trusted[p.source_id] for p in result.evidence
                    if p.source_id in trusted and p == trusted[p.source_id].provenance]
        if (result.task_id != task_id or result.verification.task_id != task_id
                or result.verification.status.value != 'pass' or result.refused
                or not result.simulated or not result.verification.provenance_ok
                or not result.verification.compliance_ok or not evidence
                or len(evidence) != len(result.evidence) or not supported_quotes(result.answer, evidence)):
            raise ValueError('unverified')
        if result.trace != expected_trace or result.model != expected_model:
            raise ValueError('unexpected backend chain')
        return Answer(task_id=task_id, answer=result.answer,
            evidence=[e.provenance for e in evidence],
            verification=VerificationResult(task_id=task_id, status='pass',
                provenance_ok=True, compliance_ok=True, hallucination_risk=0,
                reasons=['固定公开样例的出处及摘录格式已核验']),
            note='公开/合成样例词法RAG与规则验证；仅供经典文化参考，不提供诊疗。',
            model=expected_model, trace=expected_trace, refused=False, simulated=True,
            interpretation=result.interpretation)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(503, '样例链路暂时不可用，未发布结果') from None
    finally:
        with _lock:
            _active -= 1


# --- 自由文本问答端点 /api/ask ---
class AskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=200)


@app.post('/api/ask', response_model=Answer)
def ask(payload: AskInput):
    global _active
    key = os.environ.get('API_KEY', '')
    if not key:
        raise HTTPException(503, '问答入口未就绪')
    backend = os.environ.get('PUBLIC_DEMO_BACKEND', 'external_stub')
    if backend == 'external_stub':
        expected_trace = ['gateway', 'agent_orchestrator', 'rag_public', 'external_model', 'output_verify']
        expected_model = 'external-stub'
    elif backend == 'local_ollama':
        model_name = os.environ.get('PUBLIC_DEMO_MODEL_NAME', 'qwen2.5:0.5b')
        if not model_name or len(model_name) > 128 or model_name != model_name.strip():
            raise HTTPException(503, '问答本地模型未配置；未启用回退')
        expected_trace = ['gateway', 'hybrid_iface', 'rag_green', 'local_model', 'output_verify']
        expected_model = 'ollama:' + model_name
    else:
        raise HTTPException(503, '问答后端未配置；未启用回退')
    query = ' '.join(payload.query.split())[:200]
    if not query:
        raise HTTPException(400, '问题不能为空')
    with _lock:
        now = time.monotonic()
        while _calls and now - _calls[0] >= 60:
            _calls.popleft()
        if len(_calls) >= 12 or _active >= 1:
            raise HTTPException(429, '公开问答限流，请稍后再试')
        _calls.append(now)
        _active += 1
    try:
        task_id = 'publicask-' + uuid.uuid4().hex
        with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
            with client.stream('POST', GW + '/v1/query',
                    headers={'X-API-Key': key},
                    json={'query': query, 'zone': 'green', 'task_id': task_id}) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError('oversized')
        result = Answer.model_validate_json(bytes(body))
        if result.task_id != task_id or result.verification.task_id != task_id:
            raise ValueError('task mismatch')
        # 绿区未命中：网关在入口处据实拒答（trace=['gateway']、model=none、refused=True、reject）。
        # 不再直接对用户拒答，而是降级到开放推理层 /v1/reason：语料外常识问题由本地小模型自由作答，
        # 明确标注"未经典籍核验"；诊疗/用药类在推理层内部仍据实拒答。必须在完整链路校验之前处理。
        if result.refused:
            if result.evidence or result.trace != ['gateway'] or result.model != 'none':
                raise ValueError('malformed refusal')
            # 安全拒答（隐私/内部/病历类）终态化：绝不降级到自由推理，不把敏感字样送入模型。
            if 'security_block' in (result.verification.reasons if result.verification else []):
                return Answer(task_id=task_id, answer='', evidence=[],
                    verification=VerificationResult(task_id=task_id, status='reject',
                        provenance_ok=False, compliance_ok=True, hallucination_risk=0,
                        reasons=['涉及隐私、内部或病历等敏感信息，未派发公开或推理链路。']),
                    note='涉及隐私、内部或病历等敏感信息，未作答。',
                    model='none', trace=['gateway'], refused=True, simulated=True)
            with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as rclient:
                with rclient.stream('POST', GW + '/v1/reason',
                        headers={'X-API-Key': key},
                        json={'query': query, 'zone': 'unknown', 'task_id': task_id}) as rresponse:
                    rresponse.raise_for_status()
                    rbody = bytearray()
                    for chunk in rresponse.iter_bytes():
                        rbody.extend(chunk)
                        if len(rbody) > 65536:
                            raise ValueError('oversized')
            rres = Answer.model_validate_json(bytes(rbody))
            if rres.task_id != task_id or rres.verification.task_id != task_id:
                raise ValueError('reason task mismatch')
            if rres.refused:
                if rres.trace != ['gateway', 'open_reason'] or rres.model != 'none':
                    raise ValueError('malformed reason refusal')
                return Answer(task_id=task_id, answer='', evidence=[],
                    verification=VerificationResult(task_id=task_id, status='reject',
                        provenance_ok=False, compliance_ok=True, hallucination_risk=0,
                        reasons=rres.verification.reasons or ['诊疗类请求，据实拒答，不提供诊疗']),
                    note=rres.note or '涉及诊断或用药等诊疗内容，模型不予推理作答。',
                    model='none', trace=rres.trace, refused=True, simulated=True)
            if (rres.verification.status.value != 'revise' or not rres.model.startswith('ollama:')
                    or rres.trace != ['gateway', 'open_reason']):
                raise ValueError('unexpected reason result')
            return Answer(task_id=task_id, answer=rres.answer, evidence=[],
                verification=VerificationResult(task_id=task_id, status='revise',
                    provenance_ok=False, compliance_ok=True, hallucination_risk=1.0,
                    reasons=['本地小模型开放推理，未经典籍语料核验，仅作常识参考']),
                note='模型推理·未经典籍核验·不替代执业医师诊疗。',
                model=rres.model, trace=rres.trace, refused=False, simulated=False)
        # 命中的公开答案：必须走完整绿区链路，且证据/摘录/验证全部核验通过
        if result.trace != expected_trace or result.model != expected_model:
            raise ValueError('unexpected backend chain')
        trusted = {e.provenance.source_id: e for e in search(query, 20, Zone.GREEN)}
        evidence = [trusted[p.source_id] for p in result.evidence
                    if p.source_id in trusted and p == trusted[p.source_id].provenance]
        if (result.verification.status.value != 'pass' or not result.simulated
                or not result.verification.provenance_ok or not result.verification.compliance_ok
                or not evidence or len(evidence) != len(result.evidence)
                or not supported_quotes(result.answer, evidence)):
            raise ValueError('unverified')
        return Answer(task_id=task_id, answer=result.answer,
            evidence=[e.provenance for e in evidence],
            verification=VerificationResult(task_id=task_id, status='pass',
                provenance_ok=True, compliance_ok=True, hallucination_risk=0,
                reasons=['公开问答的出处及摘录格式已核验']),
            note='公开/合成样例词法RAG与规则验证；仅供经典文化参考，不提供诊疗。',
            model=expected_model, trace=expected_trace, refused=False, simulated=True,
            interpretation=result.interpretation)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(503, '问答链路暂时不可用，未发布结果') from None
    finally:
        with _lock:
            _active -= 1
