# 第一台独立部署与验收

目标为39.102.49.1，SSH用户admin，免密sudo，工程目录/root/DaoBrain。第一台为2核、1870MiB内存和2GiB交换空间，其他既有站点不能停止。本机没有Docker；下面分别标明本机终端与服务器终端。第二、第三台Windows仅可用于辅助开发或回归，不承载唯一数据库、模型或真实内部资料。

2026-11-13已在第一台执行完整 `deploy_cpu.sh` 并退出0：1179项源码测试、14模块健康、三路RAG完整来源核验、真实 `qwen2.5:0.5b` 三问题与混合问题联调、合成红区拒绝及中间件健康检查通过，19服务运行。公网主页与三个固定样例的实际HTTP验收另行通过。以下步骤用于复现这次开发演示，环境变化后仍须重新验收；浏览器按钮未取得成功验收记录，源码发布以Git仓库实际提交记录为准。

100G数据盘/dev/vdb尚未格式化或挂载，本工程目前仍用系统盘与现有Docker数据目录。没有获得格式化授权，不对该盘操作。存储扩容不能代替内存容量。小模型为 `qwen2.5:0.5b` 量化CPU模型；运行上限512MiB驻留内存、`memswap_limit=1280MiB`（内存与交换合计上限）、`mem_swappiness=100`、1.5核、1并发、30秒空闲卸载。第一台宿主`vm.swappiness=0`会让未覆盖的cgroup难以利用交换空间，配置只覆盖模型容器的交换倾向，保持宿主和其他服务设置不变。业务参数为 `num_ctx=1024`、`num_batch=8`、`num_gpu=0`、`use_mmap=True`、`num_thread=2`、`num_predict=96`，适配器超时120秒。`select` 协议只允许 `source_ids` 数组包含一个可信 `source_id`；真实模型选择证据后由服务器核验并渲染精确引文，不接受自由回答，也不是医疗诊断或训练后的专科模型。`num_batch=8`是请求参数，Ollama 0.5.4实测日志会按运行器最小掩码粒度提升到32，不能把请求参数当实际批次大小。

## 本机源码验收与上传

只打包公开/合成源码，真实.env、权重、数据库、wheels、缓存、动态Nginx配置与备份均不分发。package_source.py同时包含Nginx模板、正式Dockerfile.offline与依赖版本快照，并对每个文件生成SHA256清单。源码与模型必须分别核验，不能用健康端点代替推理验收。

```bash
# 本机终端：先进入你保存DaoBrain源码的目录。
.venv/bin/python -m pytest -q tests
STAMP=$(date +%Y%m%d-%H%M%S)
python3 scripts/package_source.py "../DaoBrain-source-$STAMP.zip"
# 根据实际输出文件名上传。正常主机指纹验证保持开启。
scp "../DaoBrain-source-$STAMP.zip" admin@39.102.49.1:/home/admin/
```

若本机OpenSSH直连报Bad file descriptor，先实测普通SSH；不要把本机通道故障判为服务器不可达。本轮nc也出现相同错误，可只对这次连接使用scripts/ssh_tcp_proxy.py透明TCP转发，SSH认证和主机指纹校验仍保持，不修改全局配置：

```bash
# 本机终端：PROXY必须指向你保存源码的位置；当前Mac使用已验证的arm64 Python。
PROXY="$(pwd)/scripts/ssh_tcp_proxy.py"
ssh -o "ProxyCommand=/usr/local/bin/python3 $PROXY %h %p" admin@39.102.49.1
# scp可加相同-o参数。其他机器替换实际Python绝对路径。
# tar通过SSH传输时不能使用ssh -n。
```

服务器已有源码必须先备份到/root/daobrain-deploy-workspace的独立时间戳目录。上传zip先对照本机输出的真实SHA256，验证source-manifest.json、唯一文件名、无绝对路径/上级跳转/符号链接，再按清单替换。scripts/install_source.py完成这项操作，不打印或覆盖.env。首次安装前管理员创建/root/DaoBrain；旧源码、旧镜像和数据不删除。

```bash
# 服务器终端：替换实际包名和本机给出的SHA256，不使用占位文字执行。
sudo -n python3 /home/admin/daobrain-install-source.py \
  /home/admin/DaoBrain-source-实际时间戳.zip 实际64位SHA256
sudo -n -i
cd /root/DaoBrain
```

install_source.py须单独先上传到/home/admin；脚本本身不含密钥。它与源码包均在本机语法/安全检查之后才执行。

## 依赖与镜像

requirements.txt定义顶层需求及extras；requirements-linux.lock.txt是第一台CPython3.11/Linux x86_64已安装镜像的完整版本快照。在线Dockerfile.app同时使用-r和-c并执行pip check，禁止构建时任意漂移传递依赖。默认在线镜像不含Node，JavaScript回归的skip必须如实报告；第一台使用下述离线镜像包含测试用Node。

Dockerfile.offline从已准备的python:3.11-slim和node:22-bookworm-slim镜像构建。Linux wheel位于daobrain39-wheels，只在离线配方中显式复制，不能提交Git。基础镜像与wheel可在有Docker的Linux amd64/Python3.11环境准备：

```bash
# 联网准备机：在源码之外的新目录保存下载产物。
python3.11 -m pip download --only-binary=:all: \
  -r requirements.txt -c requirements-linux.lock.txt --dest /准备目录/daobrain39-wheels
# 基础镜像也需准备，服务版本以docker-compose.yml为准。
docker pull python:3.11-slim
docker pull node:22-bookworm-slim
docker pull ollama/ollama:0.5.4
```

Mac的跨平台pip download曾遗漏Linux专用greenlet；仅下载成功不算完整。须在目标Linux容器中pip check及完整测试。导入包先校验SHA256、归档成员路径与类型，不允许覆盖已有目录；如需换依赖，将旧目录原子重命名到备份而非删除。Dockerfile.offline的安装使用--no-index，构建使用--network=none，失败时不偷偷改业务网络为host。

```bash
# 服务器终端：第一台已有完整Linux wheel和基础镜像。
docker build --network=none -f Dockerfile.offline -t daobrain-app:dev .
docker build --network=none -f public_demo/Dockerfile -t daobrain-demo:dev .
```

## 模型下载与业务网络隔离

推荐在服务器执行scripts/download_cpu_model.py。它只访问固定官方模型注册表，不读取.env和语料；TLS不降级，清单大小、每层大小和SHA256逐项校验，固定权重digest避免标签漂移。失败残片保留，独占创建及原子无覆盖安装防止替换已有模型。官方权重层为397807936字节；下载完成不代表推理成功。

```bash
# 服务器终端：宿主Python3.6也能运行下载和Nginx刷新脚本。
python3 scripts/download_cpu_model.py
```

另一种下载方法是Compose download profile，下载容器只接独立download网络及模型卷，无业务env_file或语料；首次下载结束后才能启动仅接local内部网络的Ollama。下面的手动替代命令拒绝在已有Ollama运行时使用下载容器，不是再次部署的必要步骤。实际 `deploy_cpu.sh` 使用上一段的固定digest下载脚本：已有模型层和清单只逐项核验，内容不同立即拒绝、不覆盖；因此完整部署脚本没有同样的停机检查，不等于允许并行更新模型。不能给运行期模型开放互联网来绕过下载失败。

```bash
set -euo pipefail
DC=(docker compose -f docker-compose.yml -f public_demo/compose.yml -f compose.cpu.yml)
"${DC[@]}" --profile download config --quiet
test -z "$("${DC[@]}" --profile model ps --status running -q ollama)"
"${DC[@]}" --profile download run --name "daobrain-model-download-$(date -u +%Y%m%dT%H%M%SZ)" --no-deps model_download
"${DC[@]}" --profile model config --quiet
"${DC[@]}" --profile model up -d --no-build --pull never
```

compose.cpu.yml用!reset取消基线GPU预留，集中覆盖gateway、hybrid_iface、local_model与public_demo，显式启用local_ollama/select协议。若Compose不支持!reset则停止，不能去掉覆盖而误用3B/GPU配置。只保存quiet校验结果，完整config/inspect可能含业务密钥，不输出到公开报告。

现有.env权限须0600，只在没有.env时用scripts/init_dev_env.py独占生成。不要将密钥放进浏览器、模型下载服务、源代码或Git。public_demo只有服务器API_KEY，没有内部密钥、审批签名或数据库密码。

## 公网主页与安全刷新

公网主页地址为http://39.102.49.1/daobrain/，仅允许 `yin-yang`、`inner-calm`、`seasons` 三个固定公开sample_id；本次主页200及三个样例真实模型HTTP验收通过。没有自由问题输入、登录凭证、个人资料或上传入口。HTTP仅适用于这项无保密样例演示；未来真实用户和自由输入必须另做HTTPS、身份及授权验收。

既有记录中，原站点 `/etc/nginx/conf.d/zjg.conf` 已包含唯一 `include /root/DaoBrain/public_demo/nginx.locations.active.conf;`，执行前须重新核对。`initialize_public_nginx.py` 仅供管理员在已有授权范围内初次集成 `/daobrain/` 和上述三个固定样例，不用于每次重建，也不扩大到其他站点或自由输入。它先将当前站点及已有active备份到 `/root/daobrain-deploy-workspace/backup/nginx-initial/` 的独立时间戳目录，核对唯一集成位置后加入include与独立限流区，`nginx -t` 通过才reload；已存在include时提示改用刷新脚本，条件不符即停止，不盲目重写站点。

```bash
# 服务器终端：仅在确认已有授权且尚未初次集成时执行。
sudo -n python3 /root/DaoBrain/scripts/initialize_public_nginx.py
```

Docker内网容器即使显示PortBindings也不一定存在宿主监听。现有Nginx直接代理public_demo的唯一green内网IP及18100端口；每次重建后都必须运行 `refresh_public_upstream.py` 并备份原active，不能固定旧IP或将业务网络改为host。未有include时刷新脚本拒绝，须先完成上述授权初次集成。

```bash
# 服务器终端：每次public_demo重建后执行。
sudo -n python3 /root/DaoBrain/scripts/refresh_public_upstream.py \
  --backup-dir /root/daobrain-deploy-workspace/backup/public-upstream
```

刷新脚本确认固定容器身份、无公开端口、唯一internal绿区、私有IP后，从模板只替换唯一占位IP。原active文件备份到独立工作区，模板与主站点逐字保持不变；原子替换后 `nginx -t` 通过才reload。校验失败恢复原配置且不reload；若已尝试reload后失败，恢复磁盘配置但运行态须人工确认，不再次重载，不删除文件。公网模板读取超时123秒、后端120秒、浏览器125秒，避免30秒代理提前截断CPU推理。

## 一键部署与最终验收

scripts/deploy_cpu.sh在现有/root/DaoBrain基础上保存旧应用/演示镜像标签、核验模型、构建、只重建本项目并刷新上游，不停止其他服务或删除数据。源码回归与 `tests/e2e_chain.py` 在 gateway 中执行；`verify_runtime.py` 和 `verify_cpu_model.py` 必须在已连接 green/local/red 的 hybrid_iface 中执行，不为验收新增网络。PostgreSQL、Redis分别在自身容器独立健康检查。脚本不执行Nginx初次集成，须先确认已有include。原镜像标签带时间戳，可在失败时恢复原标签，再按原Compose配置重建及刷新上游；恢复后须重新测试，不以标签存在宣称回滚完成。

```bash
# 服务器终端：本次已执行通过；再次部署仍须确认前置条件。
sudo -n bash scripts/deploy_cpu.sh /root/DaoBrain
```

完整验收包括源码回归、14模块健康与Qdrant就绪、三条RAG实际HTTP命中、真实模型标签/digest/推理完成/token计数、三个公开问题的本地模型完整链路、未知/敏感拒绝、合成红区门禁拒绝、中间件独立健康检查，以及另行执行的公网样例验收。`verify_runtime.py` 的 `RUNTIME-14-MODULES-OK` 只证明模块健康与Qdrant运行；`verify_cpu_model.py` 输出实际模型大小、digest、token和耗时，必须退出码0且出现 `CPU-MODEL-E2E-OK` 才可称该脚本覆盖项通过。其直接生成探针使用64 token上限，业务链路仍按96 token上限执行。`CPU-DEPLOY-ACCEPTANCE-OK` 只表示部署脚本内全部命令成功，不包含公网HTTP或浏览器按钮验收，不能据此宣称最终全部完成。

```bash
# 服务器终端：以下检查不重建服务；逐项执行，失败即停止。
set -euo pipefail
DC=(docker compose -f docker-compose.yml -f public_demo/compose.yml -f compose.cpu.yml)
"${DC[@]}" exec -T gateway python -m pytest -q -p no:cacheprovider tests
"${DC[@]}" exec -T hybrid_iface python scripts/verify_runtime.py
"${DC[@]}" exec -T hybrid_iface python scripts/verify_cpu_model.py
"${DC[@]}" exec -T gateway python tests/e2e_chain.py
"${DC[@]}" exec -T postgres pg_isready -U daobrain -d daobrain
"${DC[@]}" exec -T redis redis-cli ping
"${DC[@]}" ps
```

公网须在外部开发终端另行顺序运行 `.venv/bin/python scripts/verify_public_demo.py`，核验三个固定样例HTTP返回、`ollama:qwen2.5:0.5b`模型标识、完整trace、可信精确引文与拒绝边界；不需要API密钥，不为此给内部服务新增互联网网络。本机Python3.11直连曾报Bad file descriptor，但curl可直连，因此本次用 `.venv/bin/python scripts/verify_public_demo.py --transport curl` 实际通过公网全部判定；该选项关闭curl用户配置、代理及重定向，不模拟返回，也不降低验收条件。新增20项curl传输离线契约测试仅用于验证传输安全边界，不计作真实模型测试。浏览器按钮仍另行核验，客户端曾出现ERR_BLOCKED_BY_CLIENT，未停用安全策略或虚称按钮通过。部署脚本会在自己的进程内清除同名密钥shell覆盖，使public_demo插值和gateway的env_file使用同一.env，不改变全局环境。真实CPU和联调验收脚本禁止python优化模式，避免断言被移除后虚报成功。Nginx校验/reload或主页可访问不代表样例推理通过。模型断开或不合格响应必须503且无stub回退；不要以Mock、health或模型列表代替真实生成。本次CPU全链路及公网HTTP已通过，后续环境变化必须复验，各次时延不作为SLA；Git发布以实际远端提交为准。

目前RAG语料为22条GREEN（14条经核验整理的典籍正文、4条模拟公开FAQ、4条模拟编辑规范）与12条纯合成RED，public与green共享同一公开库而red独立；检索按关键词与双字重叠排序，不是生产向量RAG，详情见 [rag-data.md](rag-data.md)。PostgreSQL/Redis/Qdrant运行就绪不等于业务持久化、分布式预算或向量检索已接入。图片OCR、真实外部API、专科微调、生产小程序、分布式预算、独立身份与人工红转绿流程均须后续专项验收，不能写成全部真实功能完成。
