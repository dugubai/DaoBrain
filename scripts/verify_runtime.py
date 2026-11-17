"""hybrid_iface容器中核验14模块HTTP健康和Qdrant就绪；保留原有分区而不开放端口。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from common.config import get_settings


def main():
    settings = get_settings()
    endpoints = {
        'gateway': settings.gateway_url, 'agent_orchestrator': settings.orchestrator_url,
        'rag_public': settings.rag_public_url, 'hybrid_iface': settings.hybrid_iface_url,
        'external_model': settings.external_model_url, 'local_model': settings.local_model_url,
        'rag_green': settings.rag_green_url, 'rag_red': settings.rag_red_url,
        'redline_gate': settings.redline_gate_url, 'output_verify': settings.output_verify_url,
        'daobrain_tuned': settings.daobrain_tuned_url, 'corpus_kb': settings.corpus_kb_url,
        'card_annotator': settings.card_annotator_url, 'ocr': settings.ocr_url,
    }
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        for name, url in endpoints.items():
            response = client.get(url + '/health')
            response.raise_for_status()
            body = response.json()
            if body.get('module') != name or body.get('status') != 'ok':
                raise RuntimeError('模块健康失败:' + name)
            print('HEALTH-OK', name)
        response = client.get(settings.qdrant_url + '/collections')
        response.raise_for_status()
        if response.json().get('status') != 'ok':
            raise RuntimeError('Qdrant未就绪')
        print('QDRANT-READY（只证明运行，不代表向量业务接入）')
    print('RUNTIME-14-MODULES-OK')


if __name__ == '__main__':
    main()
