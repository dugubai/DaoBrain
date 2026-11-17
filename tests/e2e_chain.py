"""真实HTTP联调断言；外部零调用的证明还依赖单元mock和事件审计，不靠答案措辞。"""
import json
import os
import urllib.error
import urllib.request

# assert是本脚本验收契约，禁止优化模式使检查消失后仍打印成功。
if not __debug__:
    raise RuntimeError("联调验收禁止python -O或PYTHONOPTIMIZE")

GW = os.environ.get("GW", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.environ["API_KEY"]
INTERNAL_KEY = os.environ["INTERNAL_API_KEY"]
class NoRedirect(urllib.request.HTTPRedirectHandler):
    # API/内部密钥不能随301/302重定向发送到其他地址。
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request(base, path, payload=None, key=None, internal=False, expected=200):
    headers = {"content-type": "application/json"}
    if key:
        headers["X-Internal-Key" if internal else "X-API-Key"] = key
    req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers=headers, method="GET" if payload is None else "POST")
    try:
        with OPENER.open(req, timeout=125) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    assert status == expected, (path, status, body[:200])
    return json.loads(body)


def assert_refused(body):
    assert body["refused"] is True and body["answer"] == "" and body["evidence"] == [], body
    assert body["verification"]["status"] != "pass", body


def main():
    request(GW, "/v1/query", {"query": "四时养生", "zone": "green"}, expected=401)
    green = request(GW, "/v1/query", {"task_id": "e2e-green", "query": "四时养生 法于阴阳",
                                      "zone": "green", "gate_approved": False}, key=API_KEY)
    assert green["verification"]["status"] == "pass" and not green["refused"], green
    assert green["answer"] and green["evidence"] and green["simulated"] is True
    assert all(e["content_hash"] and e["version"] for e in green["evidence"])
    real_model = os.environ.get("GATEWAY_GREEN_BACKEND", "external_stub") == "local_ollama"
    expected_trace = (["gateway", "hybrid_iface", "rag_green", "local_model", "output_verify"]
                      if real_model else ["gateway", "agent_orchestrator", "rag_public", "external_model", "output_verify"])
    assert green["trace"] == expected_trace, green["trace"]
    if real_model:
        assert green["model"] == "ollama:" + os.environ["LOCAL_MODEL_NAME"]
    print("公开主链路：PASS、可信来源、明确模型路径通过", green["model"])
    # 公开RAG即使主页走本地模型也独立验收，不能遗漏原公开模块。
    public_hits = request(os.environ["RAG_PUBLIC_URL"], "/retrieve", {"query": "法于阴阳", "top_k": 4},
                          key=INTERNAL_KEY, internal=True)
    assert public_hits and public_hits[0]["provenance"]["source_id"] == "suwen-1"
    print("公开RAG模拟资料：真实HTTP命中与来源通过")
    # 客户端随意声称公开/批准不能把内部话题送入公开链路。
    for zone, query in [("green", "内部未公开语料测试"), ("unknown", "四时养生"),
                        ("green", "木星卫星半径"), ("green", "四时养生 内部病历手机号")]:
        result = request(GW, "/v1/query", {"query": query, "zone": zone, "gate_approved": True}, key=API_KEY)
        assert_refused(result)
        assert "external_model" not in result["trace"] and "rag_public" not in result["trace"]
    print("未知/伪公开/敏感请求：派发前拒绝通过")
    red = request(GW, "/v1/query", {"task_id": "e2e-red", "query": "虚构流程甲",
                                    "zone": "red", "gate_approved": True}, key=API_KEY)
    assert_refused(red)
    assert "external_model" not in red["trace"] and "rag_public" not in red["trace"]
    required = ["hybrid_iface", "rag_red", "daobrain_tuned", "redline_gate"]
    assert all(module in red["trace"] for module in required), red
    assert [red["trace"].index(module) for module in required] == sorted(red["trace"].index(module) for module in required)
    print("红区全链路：真实命中合成样本→本地推理→门禁拒绝释放通过")
    hybrid = os.environ["HYBRID_IFACE_URL"]
    local = request(hybrid, "/dispatch", {"task_id": "e2e-local", "query": "四时养生",
                                         "zone": "green"}, key=INTERNAL_KEY, internal=True)
    assert local["verification"]["status"] == "pass" and not local["refused"], local
    assert "rag_green" in local["trace"] and "local_model" in local["trace"]
    assert "external_model" not in local["trace"]
    if real_model:
        assert local["model"] == "ollama:" + os.environ["LOCAL_MODEL_NAME"]
    print("本地绿区：检索→" + ("真实CPU模型" if real_model else "本地模型模拟") + "→验证通过")
    public = request(os.environ["RAG_PUBLIC_URL"], "/retrieve", {"query": "木星卫星半径", "top_k": 4},
                     key=INTERNAL_KEY, internal=True)
    assert public == []
    print("无命中：没有无关证据填充通过")
    print("E2E-CHAIN-OK")


if __name__ == "__main__":
    main()
