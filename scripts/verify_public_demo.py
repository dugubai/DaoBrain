"""从Mac/Linux开发终端核验公网三固定样例；无需API密钥，不开放任意提问。

运行：python scripts/verify_public_demo.py
使用与项目相同的Python3.11依赖。每个样例必须真实通过指定模型、出处、链路核验。
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from common.catalog import search, supported_quotes
from common.schemas import Answer, Zone

BASE = "http://39.102.49.1/daobrain/"
ORIGIN = "http://39.102.49.1"
TRACE = ["gateway", "hybrid_iface", "rag_green", "local_model", "output_verify"]
SAMPLES = {"yin-yang": "法于阴阳，和于术数", "inner-calm": "恬淡虚无，真气从之", "seasons": "四时养生"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class CurlTransport(httpx.BaseTransport):
    """显式可选的真实curl传输，用于本机Python socket直连故障；不模拟响应。"""

    def handle_request(self, request):
        require(request.url.scheme == "http" and request.url.host == "39.102.49.1"
                and request.url.port in {None, 80} and request.url.path.startswith("/daobrain/")
                and not request.url.userinfo and request.method in {"GET", "POST"},
                "curl只允许已授权的公网演示地址")
        require(not any(key in request.headers for key in
                        ("Authorization", "X-API-Key", "X-Internal-Key", "Cookie")),
                "公网验收禁止携带凭证")
        # -q必须是首个选项：不读取用户curl配置；不使用shell、不启用-L或代理。
        args = ["curl", "-q", "--silent", "--show-error", "--noproxy", "*",
                "--max-time", "125", "--max-filesize", "65536", "--http1.1",
                "--proto", "=http", "--include", "--request", request.method,
                "--url", str(request.url)]
        for key in ("Origin", "Content-Type"):
            if key in request.headers:
                args.extend(["--header", key + ": " + request.headers[key]])
        if request.content:
            args.extend(["--data-binary", "@-"])
        result = subprocess.run(args, input=request.content, capture_output=True, timeout=130)
        require(result.returncode == 0, "实际curl请求失败: "
                + result.stderr.decode(errors="replace")[:512])
        header, separator, body = result.stdout.partition(b"\r\n\r\n")
        require(bool(separator) and len(header) <= 16384 and len(body) <= 65536,
                "curl响应格式或大小异常")
        lines = header.split(b"\r\n")
        require(lines[0].startswith(b"HTTP/1.1 "), "curl响应协议异常")
        status = int(lines[0].split()[1])
        require(200 <= status <= 599, "curl响应状态异常")
        pairs = [line.split(b":", 1) for line in lines[1:]]
        require(all(len(pair) == 2 for pair in pairs), "curl响应头异常")
        return httpx.Response(status, headers=[(k, v.strip()) for k, v in pairs],
                              content=body, request=request)


def main(transport_name="httpx"):
    require(transport_name in {"httpx", "curl"}, "未知公网传输方式")
    report = {"homepage": BASE, "transport": transport_name, "samples": [], "denied": []}
    # 不读取.env、不携带业务鉴权头、不使用系统代理、不跟随重定向。
    transport = CurlTransport() if transport_name == "curl" else None
    with httpx.Client(timeout=125, trust_env=False, follow_redirects=False,
                      transport=transport) as client:
        page = client.get(BASE)
        require(page.status_code == 200 and len(page.content) <= 65536, "公网主页无效")
        require("text/html" in page.headers.get("content-type", ""), "主页不是HTML")
        require("script-src" in page.headers.get("content-security-policy", ""), "缺少CSP")
        require("X-API-Key" not in page.text and "X-Internal-Key" not in page.text, "页面暴露鉴权接口")
        for sample_id, query in SAMPLES.items():
            started = time.monotonic()
            response = client.post(BASE + "api/demo", headers={"Origin": ORIGIN}, json={"sample_id": sample_id})
            require(response.status_code == 200 and len(response.content) <= 65536,
                    "样例失败: " + sample_id + " HTTP " + str(response.status_code))
            answer = Answer.model_validate(response.json())
            require(answer.model == "ollama:qwen2.5:0.5b" and answer.trace == TRACE, "非真实小模型链路")
            require(not answer.refused and answer.verification.status.value == "pass"
                    and answer.verification.provenance_ok and answer.verification.compliance_ok
                    and answer.task_id == answer.verification.task_id, "样例核验失败")
            sources = {e.provenance.source_id: e for e in search(query, 20, Zone.GREEN)}
            evidence = [sources[p.source_id] for p in answer.evidence if p.source_id in sources
                        and p == sources[p.source_id].provenance]
            require(len(evidence) == len(answer.evidence) == 1 and supported_quotes(answer.answer, evidence),
                    "样例引文或来源不受信")
            report["samples"].append({"sample_id": sample_id, "model": answer.model,
                "trace": answer.trace, "source_ids": [p.source_id for p in answer.evidence],
                "elapsed_seconds": round(time.monotonic() - started, 3), "pass": True})
        for path, payload, origin, expected in [
            ("api/demo", {"sample_id": "seasons", "query": "私人病历"}, ORIGIN, 422),
            ("api/demo", {"sample_id": "seasons", "zone": "red"}, ORIGIN, 422),
            ("api/demo", {"sample_id": "seasons"}, "https://evil.example", 403),
            ("v1/query", None, None, 404), ("dispatch", None, None, 404),
            ("v1/query", {"query": "四时养生", "zone": "green"}, ORIGIN, 404),
            ("dispatch", {"query": "四时养生", "zone": "red"}, ORIGIN, 404),
            ("api/demo", {"sample_id": "unknown"}, ORIGIN, 422),
            ("docs", None, None, 404), ("openapi.json", None, None, 404),
        ]:
            if payload is None:
                response = client.get(BASE + path)
            else:
                response = client.post(BASE + path, headers={"Origin": origin}, json=payload)
            require(response.status_code == expected, "禁止接口未被拒绝: " + path)
            report["denied"].append({"path": path, "status": response.status_code})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("PUBLIC-REAL-MODEL-3-SAMPLES-OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("httpx", "curl"), default="httpx",
                        help="curl用于Python直连故障；两种方式均发送实际公网HTTP")
    main(parser.parse_args().transport)
