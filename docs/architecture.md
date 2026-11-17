# 架构、CPU 演示与当前能力边界

本工程把报告架构分目录实现，数据仍为可信公开 fixture 与合成红区样本。2026-11-13在39.102.49.1的 `/root/DaoBrain` 完成真实CPU部署验收：1179项源码测试通过，14后端模块、公开入口、三个中间件与Ollama共19服务运行；三路RAG来源核验、真实小模型完整联调和合成红区拒绝均通过。公网三个固定样例也经实际HTTP请求核验通过；浏览器按钮尚无成功验收记录，不把客户端策略拦截当作已通过。以下是已运行的开发演示链路及其能力边界，不是全部真实功能或生产验收结论。源码发布版本以仓库实际提交为准。

## 公网固定样例与 CPU 绿区链路

```text
浏览器固定 sample_id（无任意输入，无业务密钥）
  → 宿主机既有 Nginx :80 /daobrain/
  → public_demo（唯一 daobrain_green 私有 IP :18100）
  → ① gateway（服务端 API_KEY、样本分类、单进程限速）
  → ④ hybrid_iface（INTERNAL_API_KEY、本地分派）
  → ⑦ rag_green（可信 fixture 词法检索）
  → ⑥ local_model → internal local 网内 Ollama qwen2.5:0.5b
  → ⑩ output_verify（出处、版本、字面证据、规则校验）
  → ④ → ①（最终失败关闭）→ public_demo（再次校验模型及完整 trace）
  → 只返回验证通过的原文摘录
```

`public_demo` 只接收 `yin-yang`、`inner-calm`、`seasons` 三个固定 ID，额外字段和未知 ID 拒绝；请求问题由服务器映射。浏览器没有 API_KEY、INTERNAL_API_KEY 或外部模型密钥，也没有任意问题入口。服务端核对同源、Host、请求类型/大小、结果出处及 task_id；仅允许一个活跃请求，配合进程内限速和 Nginx IP 限流。它只注入调用 gateway 所需的 API_KEY，不加载整份 `.env`。

`compose.cpu.yml` 将 gateway、hybrid_iface、local_model 的后端统一覆盖为 Ollama，gateway 绿区分派为 `local_ollama`，public_demo 也要求相同后端和精确模型标识。基线公开编排仍存在，但不是 CPU 演示成功路径，也不会在模型故障时充当回退。

⑥的 `select` 协议不是自由生成答案。请求包含问题与检索出的受信证据；模型返回结构化 JSON，`source_ids` 数组必须且只能包含一个可信 `source_id`。服务器再次核验字段白名单、单个 ID、可信 ID 集合、精确模型名 `qwen2.5:0.5b`、`done` 及正整数 `eval_count`/`prompt_eval_count`，才按选出的来源渲染精确引文与规范正文，不发布模型自由回答。模型超时、未安装、输出无效或选出伪 ID 时返回503，不把默认摘录伪称为真实模型成功。公开成功 trace 必须精确为 `gateway → hybrid_iface → rag_green → local_model → output_verify`。

业务 CPU 参数为 `num_ctx=1024`、`num_batch=8`、`num_gpu=0`、`use_mmap=True`、`num_thread=2`、`num_predict=96`，适配器超时120秒；`num_batch=8`为请求值，Ollama 0.5.4运行器实测提升至32。运行 Ollama 为1.5 CPU、512MiB驻留内存、`memswap_limit=1280MiB`（内存与 swap 合计上限，而非额外1280MiB swap）、局部`mem_swappiness=100`（宿主全局设置不变）、1并发、最大加载1模型、30秒闲置卸载。这些是上限/配置，不是实测吞吐、响应时间或内存承诺。宿主机仅2核1870MiB RAM，已有其他服务；冷启动、swap 和内存竞争必须用实际请求及统计验证。

真实模型仅承担受限证据选择；RAG、业务状态、合规规则仍是模拟，返回 `simulated=true`。结果中的模型标签与页面状态只能说明本次成功链路，不能证明训练过专科模型或可提供医疗建议。规则返回的风险字段也不是经统计验证的临床风险概率。

## 保留的公开 stub 编排

```text
开发门户 → ① gateway → ② agent_orchestrator
        → ③ rag_public → ⑤ external_model（仅 stub）
        → ⑩ output_verify → ① → Answer
```

基线 `external_stub` 用于模拟回归。⑤的批准凭证由服务端签发，绑定任务、问题、证据与短期有效期；客户端批准标记不构成出网授权。live 模式明确返回503，不请求真实外 API。REVISE 在有界迭代内重新检索和验证，仍未通过时不发布正文；REJECT 不交付候选答案。`tests/e2e_chain.py` 按实际后端核对完整trace；CPU验收另外通过 `scripts/verify_cpu_model.py` 检查真实生成、模型digest与正token计数，不能只靠Mock。

## 本地合成红区与人工门禁

```text
④ hybrid_iface → ⑦ rag_green → ⑥ local_model → ⑩ → ④
④ hybrid_iface → ⑧ rag_red → ⑪ daobrain_tuned → ⑨ redline_gate
                                                   → 拒绝未经审核释放
```

绿区没有命中就拒答，不自动查红区。红区直接由本地入口分派，不先经过公开检索。⑧和⑪只处理合成红区样本；⑪没有训练权重。⑨不能凭客户端 gate_approved 释放原文，独立人工复核/红转绿流程未接入。红区验收目标是“本地处理且无公开正文/出处”，不是“自动脱敏并转绿”。公网 fixed sample_id 入口根本不接受红区输入。

## 网络、第一台依赖与应用组件

```text
第一台 Linux：39.102.49.1 /root/DaoBrain
  green（internal）：public_demo、公开业务、应用组件和中间件
  local（internal）：hybrid_iface、local_model、Ollama 等本地调用
  red（internal）：rag_red、daobrain_tuned、redline_gate，仅合成样本
  download（非 internal）：仅手动 model_download，无 env_file/业务密钥
  模型卷：下载成功退出后由 internal Ollama 读取，不并行下载/运行

第二、第三台 Windows：源码开发和回归，不承载第一台的唯一运行依赖或真红资料
```

Compose 默认只有 gateway 配置回环8000端口发布，public_demo 不发布端口。internal bridge 在特定 Docker 环境下可有 PortBindings 却无宿主机监听，不能拿端口映射元数据当连通性证明。公网使用既有 Nginx 到实际绿区私有 IP 的18100端口，而不是开放业务端口或切换 host 网络。`initialize_public_nginx.py` 仅由管理员在已有授权范围内初次集成 `/daobrain/` 的三个固定样例，先备份当前站点，再加入独立限流区和 active include；已有 include 时不重复集成。`public_demo/nginx.locations.conf` 中 `10.255.1.240` 是模板占位，每次容器重建后都用 `refresh_public_upstream.py` 核验容器/网络、备份原 active 并原子刷新实际 IP；刷新不修改模板、主站点或限流区。

gateway 和 hybrid_iface 连接多个内网，仍具有桥接能力；hybrid_iface 已连接 green/local/red，`verify_cpu_model.py` 与 `verify_runtime.py` 必须在该容器中运行，不为验收新增网络。服务共享代码镜像及开发身份，内部网络不等于生产物理隔离。在线依赖安装需要联网；离线构建将 wheels 安装到镜像中，也不提供额外生产隔离。运行网络、构建网络和模型下载网络是三种不同边界，不能混为一谈。

知识库提供可信 fixture 关键词检索和精确来源定位；GREEN共22条（14条经核验整理的典籍正文、4条模拟公开FAQ、4条模拟编辑规范），RED共12条且全部纯合成。public 与 green 共用同一公开语料库，red 独立；这不是生产 embedding/vector RAG，来源与整理边界见 [rag-data.md](rag-data.md)。字卡审核使用进程内状态及版本区分，未接入真实员工身份或持久化。OCR 只作显式文本模拟，图片输入501。开发门户为同源单文件 Web 界面，不是已发布小程序。

PostgreSQL、Redis、Qdrant 虽有容器定义，但 PostgreSQL 业务持久化、Redis 原子分布式日限额和向量 embedding/索引查询均未接入。健康端点和中间件进程运行不能代替业务就绪。真实 OCR、训练后专科模型、真外 API、人工红转绿、生产认证/TLS及独立安全验收均未完成，不能据模拟测试宣称上述能力。

## 运行与验收证据

`tests/` 覆盖批准、鉴权、来源/规则、预算、检索、模型适配、门禁及应用页面；模型适配与 public_demo 的 mock 测试不是 CPU 实测。`scripts/run_local_chain.py` 启动14个 HTTP 子进程，只证明模拟链路可回归；`tests/test_deployment_contract.py` 只检查部署文件静态契约。

`deploy_cpu.sh` 的源码回归与 `tests/e2e_chain.py` 在 gateway 中运行；`verify_runtime.py` 在 hybrid_iface 中核验14模块 HTTP 健康与 Qdrant 运行就绪，`verify_cpu_model.py` 也在 hybrid_iface 中核验真实生成、三条RAG检索、三样例完整模型链路及红区拒绝。PostgreSQL 的 `pg_isready` 与 Redis 的 `redis-cli ping` 分别在各自容器执行，不以模块健康或数据库进程运行替代业务接入验收。`CPU-MODEL-E2E-OK` 和 `CPU-DEPLOY-ACCEPTANCE-OK` 只对应各脚本实际覆盖项，后者不含公网 HTTP 与浏览器按钮验收。

部署步骤见 [deployment.md](deployment.md)。实际验收须分开记录 Compose 解析、目标 Linux `pip check`、测试输出及 skip、容器状态、私有网络、真实模型请求与公网固定样例结果。本次真实模型全链路与公网HTTP三个样例已通过，各次时延仍以实际输出为准，不作为SLA。默认公网命令使用HTTPX；本机Python直连异常时可显式使用 `--transport curl`，仍发送真实公网请求、不使用代理或重定向、保留全部判定；浏览器按钮另行验收。Nginx模板读取超时123秒、后端120秒、浏览器125秒，避免代理提前截断CPU推理；达到上限仍须拒绝且不发布结果。
