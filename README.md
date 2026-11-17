# DaoBrain · 汉唐医道软件模拟工程

本工程按报告①—⑪及应用组件分别建目录，用可信公开样本与合成红区样本演示调用、检索、验证和拒绝逻辑。它不是医疗诊断系统，也不是生产隔离环境。源码部署、离线镜像与 Docker internal 网络都不能代替生产安全验收。

## 当前状态与验收边界

部署服务器为 **39.102.49.1**，登录用户 `admin`，免密 `sudo -n`，工程目录 `/root/DaoBrain`。目标为14个后端模块、公开样例入口、3个中间件和Ollama共19个运行服务。该机为2核、1870MiB内存、2GiB swap，并运行其他站点；系统盘约40G，100G的 `/dev/vdb` 尚未格式化、未挂载。部署不会格式化磁盘、删除用户文件或改全局 Docker daemon/网络。存储空间和模型内存容量分别验收。

2026-11-13已在第一台独立完成CPU部署验收：1179项源码测试通过、14模块健康、三路RAG真实HTTP命中与完整来源核验、真实 `qwen2.5:0.5b` 三问题及混合问题联调、合成红区拒绝、PostgreSQL与Redis健康检查通过，19个服务运行。公网主页及三个固定样例另以实际curl请求通过模型、完整链路、精确引文与禁止接口核验，地址为 http://39.102.49.1/daobrain/ 。这只证明该次开发演示验收，不保证生产能力或响应时限；浏览器曾被客户端策略阻止，未把HTTP通过冒称浏览器按钮验收。后续测试数量及状态以脚本实际输出为准，源码版本以Git仓库的实际提交记录为准。

`compose.cpu.yml` 将 gateway、hybrid_iface、local_model 统一切为 Ollama；公开固定样例的目标链路为①→④→⑦→⑥→⑩。`select` 协议要求 JSON 的 `source_ids` 数组仅含一个可信 `source_id`；模型选择证据后，服务器核验并渲染精确引文，不接受自由回答，失败不回退成 stub 成功。业务 CPU 参数为 `num_ctx=1024`、`num_batch=8`、`num_gpu=0`、`use_mmap=True`、`num_thread=2`、`num_predict=96`，适配器超时120秒；`num_batch=8`为请求值，Ollama 0.5.4运行器实测将其提升至32，不能冒称实际批次为8。真实推理不代表资料或整个系统变成真实生产业务，输出仍保留 `simulated=true`。

向量 embedding、PostgreSQL 业务持久化、Redis 分布式限额、真实 OCR、训练后的专科模型、真实外部 API 和人工红转绿流程均未完成。当前语料为22条 GREEN（14条经核验整理的典籍正文、4条模拟公开FAQ、4条模拟编辑规范）和12条纯合成 RED；public 与 green 共用该公开语料库，red 独立检索合成样本。RAG 使用关键词与双字重叠排序，不是生产向量 RAG；来源及整理说明见 [docs/rag-data.md](docs/rag-data.md)。OCR 只接受明确标记的文本模拟，图片识别返回501；⑪使用合成证据而非训练权重；⑤的 live 模式返回503且不出网。

## 模块与架构

①`gateway/` 提供鉴权入口及开发门户，②`agent_orchestrator/` 提供公开有界编排，③`rag_public/` 提供公开词法检索，④`hybrid_iface/` 提供本地分派，⑤`external_model/` 提供批准凭证绑定的 stub，⑥`local_model/` 提供摘录模拟与受限 Ollama 适配，⑦`rag_green/` 提供绿区词法检索，⑧`rag_red/` 只检索合成红区样本，⑨`redline_gate/` 拒绝未经独立审核的红区释放，⑩`output_verify/` 提供可信版本与规则验证，⑪`daobrain_tuned/` 提供合成红区推理模拟。

应用组件位于 `portal/`、`card_annotator/`、`corpus_kb/`、`ocr/`。公网入口位于 `public_demo/`，仅接受固定 `sample_id`，没有任意问题输入，也不要求浏览器持有业务密钥。共用契约、可信目录、审计和模拟预算位于 `common/`；测试位于 `tests/`；架构及限制见 [docs/architecture.md](docs/architecture.md)。

## 本机模拟回归

以下命令在本机工程根目录执行，目标 Python 为3.11。已有虚拟环境可复用；不要把虚拟环境、`.env`、模型、wheels、缓存或备份纳入源码包或提交。

```bash
cd /你保存源码的位置/DaoBrain
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q -p no:cacheprovider tests
GATEWAY_GREEN_BACKEND=external_stub bash scripts/run_local_chain.sh
```

本地联调脚本生成临时密钥和回环端口，启动14个 HTTP 子进程，验证公开 stub、本地模拟与红区拒绝，并仅终止自己启动的进程。该测试不下载模型、不证明 Docker 网络或数据库就绪。Node.js 用于页面 JavaScript 回归；在线应用镜像未安装 Node，相关测试可能 skip，不能把 skip 算成完整页面验收。

## CPU 部署入口

完整步骤、可复制变量、本机上传与服务器终端的区分、在线依赖安装、真实离线方案、模型预下载、Nginx 初次集成与回滚、验收命令均见 [docs/deployment.md](docs/deployment.md)。既有服务运行时先检查并备份，不盲目重建全栈；第二、第三台 Windows 机器仅用于开发与回归，不承载第一台的唯一依赖或真实红区资料。

下面只是首次下载后全栈启动的手动摘要，不能跳过部署文档里的镜像、密钥及模型卷准备；已有部署更新使用 `deploy_cpu.sh` 的固定digest核验流程，两者前置条件不同。基线 Compose 默认有 GPU 预留和3B下载配置；本机2核服务器必须始终带 CPU 覆盖文件，不能直接使用 `make model-download` 或 `make model-up`。

```bash
# 服务器终端：admin 登录后进入 root Bash；任何失败立即停止。
sudo -n -i
set -euo pipefail
cd /root/DaoBrain
DC=(docker compose -f docker-compose.yml -f public_demo/compose.yml -f compose.cpu.yml)
"${DC[@]}" --profile model config --quiet
# 已有运行模型时本摘要拒绝并行下载；按部署文档确认更新影响后再处理。
test -z "$("${DC[@]}" --profile model ps --status running -q ollama)"
# 下载阶段只启用 download profile；不用 --rm，不自动删除下载容器。
"${DC[@]}" --profile download run --name "daobrain-model-download-$(date -u +%Y%m%dT%H%M%SZ)" --no-deps model_download
# 前一步成功退出后，才启动读取同一模型卷的 internal 运行服务。
"${DC[@]}" --profile model up -d --no-build --pull never
sudo -n python3 /root/DaoBrain/scripts/refresh_public_upstream.py \
  --backup-dir /root/daobrain-deploy-workspace/backup/public-upstream
```

下载容器仅连接独立 `download` 互联网网络和模型卷，不加载 `env_file` 或业务密钥。运行期 Ollama 只连接 `local` 内网，不发布11434，限512MiB驻留内存、1280MiB内存及swap总上限、局部`mem_swappiness=100`、1.5 CPU、1并发，30秒闲置卸载。宿主交换倾向仍保持0，不修改全局设置。模型文件仍留在第一台服务器，不能依赖 Windows 开发机提供运行服务。

`scripts/deploy_cpu.sh` 在 gateway 中执行源码回归与 `tests/e2e_chain.py`；`verify_cpu_model.py` 和 `verify_runtime.py` 必须在已有 green/local/red 三网络的 hybrid_iface 中运行，不为测试新增网络。PostgreSQL、Redis 分别在各自容器独立健康检查；这些结果及部署脚本成功标记都不能代替公网验收。

## 公网与安全限制

既有 Nginx 的80端口将 `/daobrain/` 代理到 `public_demo` 在 `daobrain_green` 的实际私有 IP 的18100端口。Docker internal 网络即使有 PortBindings，也可能不在宿主机监听；不能因此改业务网络为 host 或公网。`initialize_public_nginx.py` 仅供管理员在已有授权范围内初次集成 `/daobrain/` 及 `yin-yang`、`inner-calm`、`seasons` 三个固定样例，先备份当前站点；已有 include 时改用 `refresh_public_upstream.py`。模板中的 `10.255.1.240` 是占位 IP，不是固定地址；每次容器重建后，刷新脚本都须核验实际 IP、备份原 active 配置并原子更新 `nginx.locations.active.conf`，不修改模板或主站点。`nginx -t` 失败恢复 active 且不 reload；若 reload 已尝试后失败，运行态须人工确认，不再次重载，不删除文件。

客户端的 zone、user_id 或 gate_approved 不构成授权。公开入口只接受可信样本主题，未分类或敏感内容在派发前拒绝；红区只使用合成样本，⑨没有人工审核接入，不能自动红转绿。审计只记录事件元数据和任务哈希，不记录正文或密钥。

共享镜像、开发密钥、多网络 gateway/hybrid_iface、进程内预算与审核状态仅为开发便利，不能抵御已入侵服务或证明物理隔离。规则验证只覆盖有限可信资料，不能代替事实审核或执业医师判断。真实病历和红区资料不得进入工程、镜像、模型下载目录或 Windows 开发机；真实外 API、生产身份/TLS、向量检索与分布式预算必须独立设计及验收。
