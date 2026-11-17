.PHONY: init-env up down build logs ps smoke test e2e local-test local-e2e model-download model-up

init-env:
	python3 scripts/init_dev_env.py
up:
	docker compose up -d --build
down:
	docker compose down
build:
	docker compose build gateway
ps:
	docker compose ps
logs:
	docker compose logs -f --tail=50

# 安装模型前应核实可用磁盘，不能通过清理其他项目腾空间。
model-download:
	docker compose --profile download run --rm model_download
model-up:
	LOCAL_MODEL_BACKEND=ollama docker compose --profile model up -d ollama local_model

# 容器内检查模块自身；任何服务失败都会返回非零。
smoke:
	@set -e; for pair in gateway:8000 agent_orchestrator:8002 rag_public:8003 hybrid_iface:8004 external_model:8005 local_model:8006 rag_green:8007 rag_red:8008 redline_gate:8009 output_verify:8010 daobrain_tuned:8011 corpus_kb:8012 card_annotator:8013 ocr:8014; do \
		svc=$${pair%%:*}; port=$${pair##*:}; \
		docker compose exec -T $$svc python -c "import json,urllib.request;r=json.load(urllib.request.urlopen('http://127.0.0.1:$$port/health',timeout=3));assert r['status']=='ok' and r['module']=='$$svc';print('$$svc ok')"; \
	done

e2e:
	docker compose exec -T gateway python tests/e2e_chain.py
test:
	docker compose run --rm --no-deps gateway python -m pytest -q -p no:cacheprovider tests

local-test:
	.venv/bin/python -m pytest -q tests
local-e2e:
	bash scripts/run_local_chain.sh
